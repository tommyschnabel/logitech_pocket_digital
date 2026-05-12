#!/usr/bin/env python3
"""
Logitech Pocket Digital - Photo Downloader
Reverse-engineered USB protocol for the SMaL UltraPocket sensor (VID:046D PID:0950).

Raw image format: 41-byte header followed by Bayer pixel data at stride=sensor_width.
The camera does NOT pre-crop dark reference columns; each row is sensor_width (e.g. 644)
bytes wide. The active image is the first active_width (e.g. 640) columns of each row.

Bayer pattern: RGGB (empirically confirmed — R/B swap vs sensor datasheet BGGR).
  Row 0: R G R G ...  (even rows: R at even cols, G at odd cols)
  Row 1: G B G B ...  (odd rows:  G at even cols, B at odd cols)
"""

import usb.core
import usb.util
import sys
from pathlib import Path
from PIL import Image
import argparse
import struct

VID = 0x046D
PID = 0x0950
EP_OUT = 0x01
EP_IN = 0x81

CMD_LIST_PICTURES = 0x12
CMD_GET_PICTURE = 0x11
CMD_DELETE_ALL = 0x18

IMG_START = 0x29   # byte offset where pixel data begins in the USB response

# The sensor outputs sensor_width × sensor_height pixels. The camera does NOT
# pre-crop dark reference columns, so the USB download stride = sensor_width.
# Active image = first active_width columns of each row, active_height rows.
DARK_COLS = 4   # sensor_width - active_width (dark reference cols per frame)
DARK_ROWS = 2   # sensor_height - active_height (dark reference rows per frame)

# Default active image dimensions (derived from the sensor header values).
IMG_W = 640
IMG_H = 480

# RGGB tile (row_parity, col_parity) -> channel
# Empirically confirmed for extracted active area:
#   Row 0 (even): R G R G ...
#   Row 1 (odd):  G B G B ...
BAYER_BGGR = {(0, 0): 'B', (0, 1): 'G', (1, 0): 'G', (1, 1): 'R'}
BAYER_GRBG = {(0, 0): 'G', (0, 1): 'R', (1, 0): 'B', (1, 1): 'G'}
BAYER_RGGB = {(0, 0): 'R', (0, 1): 'G', (1, 0): 'G', (1, 1): 'B'}
BAYER_GBRG = {(0, 0): 'G', (0, 1): 'B', (1, 0): 'R', (1, 1): 'G'}

PATTERNS = {
    'BGGR': BAYER_BGGR,
    'GRBG': BAYER_GRBG,
    'RGGB': BAYER_RGGB,
    'GBRG': BAYER_GBRG,
}

# White balance gains (R, G, B) keyed by header tint code.
# Applied to raw Bayer pixels before demosaicking (same stage as the original driver).
# The exact gain table was not recovered from the driver binary — all values are
# currently neutral (1.0) placeholders. Calibrate by sampling a known neutral
# (white wall, grey card) and adjusting until R ≈ G ≈ B on that region.
TINT_WB = {
    # Calibrated from tint=15 (observed on all test images) by sampling
    # white doors under natural daylight in IMG0249. All other codes default
    # to the same gains until more scenes are available for calibration.
    15: (1.112, 1.000, 1.147),
}
# Default gains for uncalibrated tint codes
_WB_DEFAULT = (1.112, 1.000, 1.147)


def parse_header(raw_data):
    """
    Decode the 41-byte (0x29) camera response header.

    Confirmed layout (little-endian):
      0x00       magic byte  (0xFF)
      0x0C-0x0D  sensor_height  (e.g. 482 = active_h + DARK_ROWS)
      0x0E-0x0F  sensor_width   (e.g. 644 = active_w + DARK_COLS)
      0x10-0x11  img_start      (always 0x29 = 41)
      0x22       gammaCode      (0-7, SMaL companding curve selector)
      0x23       tint           (white-balance tint code)
      0x24       gain_shift     (log2 of sensor gain; 0=1×, 1=2×, 2=4×, …)

    Derived:
      active_width  = sensor_width  - DARK_COLS  (e.g. 640)
      active_height = sensor_height - DARK_ROWS  (e.g. 480)
    Download stride = sensor_width (dark columns are NOT stripped by firmware).
    Active image = first active_width columns × active_height rows.
    """
    if len(raw_data) < IMG_START:
        return {}
    h = raw_data[:IMG_START]
    sensor_h = struct.unpack_from('<H', h, 0x0C)[0]
    sensor_w = struct.unpack_from('<H', h, 0x0E)[0]
    active_w = sensor_w - DARK_COLS
    active_h = sensor_h - DARK_ROWS
    return {
        'sensor_width':  sensor_w,
        'sensor_height': sensor_h,
        'active_width':  active_w,
        'active_height': active_h,
        'img_start':     struct.unpack_from('<H', h, 0x10)[0],
        'gamma_code':    h[0x22],
        'tint':          h[0x23],
        'gain_shift':    h[0x24],
    }


def extract_bayer(raw_data, sensor_width, active_width, active_height):
    """
    Extract the active Bayer pixels from the raw USB download.

    The camera sends pixel rows at stride=sensor_width (dark cols not stripped).
    This copies the first active_width columns from each of active_height rows
    into a compact active_width×active_height buffer.
    """
    out = bytearray(active_width * active_height)
    for row in range(active_height):
        src = IMG_START + row * sensor_width
        dst = row * active_width
        out[dst:dst + active_width] = raw_data[src:src + active_width]
    return bytes(out)


def apply_wb(pixels, width, height, gains, pattern='RGGB'):
    """
    Apply per-channel white balance gains to raw Bayer pixels.

    gains: (R_gain, G_gain, B_gain) floats — G is typically 1.0 (reference).
    Applied before demosaicking so interpolation uses colour-corrected values.
    """
    tile = PATTERNS[pattern]
    r_gain, g_gain, b_gain = gains
    channel_gain = {'R': r_gain, 'G': g_gain, 'B': b_gain}
    luts = {
        pos: bytes(min(255, int(v * channel_gain[ch])) for v in range(256))
        for pos, ch in tile.items()
    }
    out = bytearray(len(pixels))
    for y in range(height):
        rp = y % 2
        for x in range(width):
            out[y * width + x] = luts[(rp, x % 2)][pixels[y * width + x]]
    return bytes(out)


def debayer_bilinear(pixels, width=IMG_W, height=IMG_H, pattern='RGGB'):
    """
    Bilinear Bayer demosaicking.

    pixels: compact width*height Bayer bytes (output of extract_bayer).
    Each channel is directly measured at its native positions and bilinearly
    interpolated at all other positions from the nearest same-color neighbors.
    """
    tile = PATTERNS[pattern]

    def get(y, x):
        if 0 <= y < height and 0 <= x < width:
            return pixels[y * width + x]
        return None

    def avg(*vals):
        v = [x for x in vals if x is not None]
        return sum(v) // len(v) if v else 0

    rgb = bytearray(width * height * 3)

    for y in range(height):
        for x in range(width):
            ch = tile[(y % 2, x % 2)]
            p = pixels[y * width + x]

            if ch == 'G':
                G = p
                # Determine which axis has R and which has B based on tile neighbors
                if tile[(y % 2, (x + 1) % 2)] == 'R':
                    # Even row G: left/right are R, above/below are B
                    R = avg(get(y, x - 1), get(y, x + 1))
                    B = avg(get(y - 1, x), get(y + 1, x))
                else:
                    # Odd row G: above/below are R, left/right are B
                    R = avg(get(y - 1, x), get(y + 1, x))
                    B = avg(get(y, x - 1), get(y, x + 1))

            elif ch == 'R':
                R = p
                G = avg(get(y, x - 1), get(y, x + 1),
                        get(y - 1, x),  get(y + 1, x))
                B = avg(get(y - 1, x - 1), get(y - 1, x + 1),
                        get(y + 1, x - 1), get(y + 1, x + 1))

            else:  # 'B'
                B = p
                G = avg(get(y, x - 1), get(y, x + 1),
                        get(y - 1, x),  get(y + 1, x))
                R = avg(get(y - 1, x - 1), get(y - 1, x + 1),
                        get(y + 1, x - 1), get(y + 1, x + 1))

            i = (y * width + x) * 3
            rgb[i] = R
            rgb[i + 1] = G
            rgb[i + 2] = B

    return bytes(rgb)


def global_stretch(rgb_bytes, lo_pct=2.0, hi_pct=98.0):
    """
    Global linear histogram stretch across all channels combined.

    Uses a single lo/hi percentile computed over all R+G+B values so channel
    ratios (and therefore colour) are preserved.
    """
    s = sorted(rgb_bytes)
    n = len(s)
    lo = s[int(n * lo_pct / 100)]
    hi = s[int(n * hi_pct / 100)]
    span = max(hi - lo, 1)
    return bytes(min(255, max(0, int((v - lo) * 255 / span))) for v in rgb_bytes)


def find_camera():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise RuntimeError("Logitech Pocket Digital camera not found!")
    return dev


def init_camera(dev):
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except usb.core.USBError:
        pass
    usb.util.claim_interface(dev, 0)
    print("Camera initialized and interface claimed.")
    return dev


def list_pictures(dev):
    cmd = bytearray(16)
    cmd[0] = CMD_LIST_PICTURES
    dev.write(EP_OUT, cmd, timeout=1000)
    data1 = dev.read(EP_IN, 32768, timeout=5000)
    data2 = dev.read(EP_IN, 32768, timeout=5000)
    full_data = bytes(data1) + bytes(data2)
    num_pics = full_data[0x105]
    print(f"Found {num_pics} pictures on camera.")
    pictures = []
    for i in range(num_pics):
        offset = 0x106 + (i * 16)
        if offset + 11 <= len(full_data):
            filename = full_data[offset:offset+11].decode('ascii', errors='ignore')
            if len(filename) == 11 and filename[7] == ' ':
                filename = filename[:7] + '.' + filename[8:]
            pictures.append(filename.strip())
    return pictures


def download_picture(dev, filename):
    cmd = bytearray(16)
    cmd[0] = CMD_GET_PICTURE
    cmd[1] = 0x01
    fname_bytes = filename.encode('ascii')[:11].ljust(11)
    for i, c in enumerate(fname_bytes):
        cmd[3 + i] = c
    dev.write(EP_OUT, cmd, timeout=1000)
    image_data = bytearray()
    for _ in range(10):
        try:
            pkt = dev.read(EP_IN, 32768, timeout=5000)
            image_data.extend(pkt)
        except usb.core.USBError:
            break
    return bytes(image_data)


def delete_all_pictures(dev):
    print("Sending delete all command...")
    cmd = bytearray(16)
    cmd[0] = CMD_LIST_PICTURES
    dev.write(EP_OUT, cmd, timeout=1000)
    dev.read(EP_IN, 32768, timeout=5000)
    dev.read(EP_IN, 32768, timeout=5000)
    cmd = bytearray(16)
    cmd[0] = CMD_DELETE_ALL
    cmd[1] = 0x01
    dev.write(EP_OUT, cmd, timeout=1000)
    print("All pictures deleted from camera.")


def save_png(png_path, raw_data, width=IMG_W, height=IMG_H,
             sensor_width=None, pattern='RGGB', stretch=True,
             saturation=2.0, tint=None):
    """Debayer and save as PNG."""
    from PIL import ImageEnhance
    sw = sensor_width if sensor_width is not None else width + DARK_COLS
    pixels = extract_bayer(raw_data, sw, width, height)
    if tint is not None:
        gains = TINT_WB.get(tint, _WB_DEFAULT)
        pixels = apply_wb(pixels, width, height, gains, pattern)
    rgb = debayer_bilinear(pixels, width, height, pattern)
    if stretch:
        rgb = global_stretch(rgb)
    img = Image.frombytes('RGB', (width, height), rgb)
    if saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(saturation)
    img.save(png_path, 'PNG')
    print(f"  Saved: {png_path}")


def save_ppm(ppm_path, raw_data, width=IMG_W, height=IMG_H, sensor_width=None):
    """Save raw Bayer data as grayscale PPM (no debayering)."""
    sw = sensor_width if sensor_width is not None else width + DARK_COLS
    pixels = extract_bayer(raw_data, sw, width, height)
    with open(ppm_path, 'wb') as f:
        f.write(f"P5\n{width} {height}\n255\n".encode())
        f.write(pixels)
    print(f"  Saved: {ppm_path}")


def main():
    parser = argparse.ArgumentParser(
        description='Download photos from Logitech Pocket Digital')
    parser.add_argument('output_dir', nargs='?', default=None)
    parser.add_argument('--raw', action='store_true',
                        help='Also save raw Bayer PPM files')
    parser.add_argument('--delete', action='store_true',
                        help='Delete photos from camera after downloading')
    parser.add_argument('--pattern', default='RGGB',
                        choices=list(PATTERNS.keys()),
                        help='Bayer CFA pattern (default: RGGB)')
    parser.add_argument('--no-stretch', action='store_true',
                        help='Disable global histogram stretching')
    parser.add_argument('--saturation', type=float, default=2.0,
                        help='Colour saturation multiplier (default: 2.0)')
    parser.add_argument('--no-wb', action='store_true',
                        help='Disable white balance correction')
    args = parser.parse_args()

    output_dir = (Path(args.output_dir) if args.output_dir
                  else Path.home() / "Pictures" / "PocketDigital")
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {output_dir}")
    print(f"Bayer: {args.pattern}  Stretch: {not args.no_stretch}  "
          f"Saturation: {args.saturation}x  WB: {not args.no_wb}")

    try:
        print("Searching for camera...")
        dev = find_camera()
        init_camera(dev)

        pictures = list_pictures(dev)
        if not pictures:
            print("No pictures found.")
            return

        print(f"\nDownloading {len(pictures)} picture(s)...")

        for i, pic in enumerate(pictures):
            safe_name = pic.replace(' ', '_').replace('/', '_')
            print(f"[{i+1}/{len(pictures)}] {pic}...", end=" ")
            sys.stdout.flush()

            try:
                data = download_picture(dev, pic)
                if len(data) <= IMG_START:
                    print(f"ERROR: Too little data ({len(data)} bytes)")
                    continue

                meta = parse_header(data)
                w = meta.get('active_width', IMG_W)
                h = meta.get('active_height', IMG_H)
                sw = meta.get('sensor_width', w + DARK_COLS)
                tint_code = meta.get('tint', 0)
                if meta:
                    wb_gains = TINT_WB.get(tint_code, _WB_DEFAULT)
                    print(f"(sensor {meta['sensor_width']}x{meta['sensor_height']} "
                          f"-> {w}x{h} "
                          f"gamma={meta['gamma_code']} tint={tint_code} "
                          f"gain={2**meta['gain_shift']}x "
                          f"wb={wb_gains})", end=" ")

                png_path = output_dir / f"{safe_name}.png"
                save_png(str(png_path), data, width=w, height=h,
                         sensor_width=sw, pattern=args.pattern,
                         stretch=not args.no_stretch,
                         saturation=args.saturation,
                         tint=None if args.no_wb else tint_code)

                if args.raw:
                    ppm_path = output_dir / f"{safe_name}.ppm"
                    save_ppm(str(ppm_path), data, width=w, height=h,
                             sensor_width=sw)

            except Exception as e:
                import traceback
                print(f"ERROR: {e}")
                traceback.print_exc()

        if args.delete:
            delete_all_pictures(dev)

        print(f"\nDownload complete! Files saved to: {output_dir}")

    except usb.core.USBError as e:
        print(f"USB Error: {e}")
    except Exception as e:
        import traceback
        print(f"Error: {e}")
        traceback.print_exc()
    finally:
        try:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        except Exception:
            pass


if __name__ == "__main__":
    main()
