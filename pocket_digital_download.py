#!/usr/bin/env python3
"""
Logitech Pocket Digital - Photo Downloader
Reverse-engineered USB protocol for the SMaL UltraPocket sensor (VID:046D PID:0950).

USB response format: 41-byte header followed by raw Bayer pixel data.
Download stride = sensor_width (e.g. 644); the camera does NOT pre-crop the
2 dark reference columns per side. The active image is the first active_width
(e.g. 640) columns of each row.

Bayer pattern: RGGB (empirically confirmed; datasheet says BGGR).
  Row 0 (even): R G R G ...
  Row 1 (odd):  G B G B ...
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
EP_IN  = 0x81

CMD_LIST_PICTURES = 0x12
CMD_GET_PICTURE   = 0x11
CMD_DELETE_ALL    = 0x18

IMG_START = 0x29  # pixel data begins 41 bytes into the USB response

# The camera sends pixel rows at stride=sensor_width; dark reference columns
# are NOT stripped by firmware. Active image = first active_width columns,
# active_height rows. For VGA captures: sensor 644×482 → active 640×480.
DARK_COLS = 4
DARK_ROWS = 2

IMG_W = 640
IMG_H = 480

# (row_parity, col_parity) → channel for each supported Bayer pattern.
PATTERNS = {
    'RGGB': {(0,0):'R', (0,1):'G', (1,0):'G', (1,1):'B'},
    'BGGR': {(0,0):'B', (0,1):'G', (1,0):'G', (1,1):'R'},
    'GRBG': {(0,0):'G', (0,1):'R', (1,0):'B', (1,1):'G'},
    'GBRG': {(0,0):'G', (0,1):'B', (1,0):'R', (1,1):'G'},
}


def parse_header(raw_data):
    """
    Decode the 41-byte camera response header (all multi-byte fields little-endian).

      0x00        magic byte    always 0xFF
      0x0C–0x0D   sensor_height includes DARK_ROWS dark reference rows (e.g. 482)
      0x0E–0x0F   sensor_width  includes DARK_COLS dark reference cols (e.g. 644)
      0x10–0x11   img_start     byte offset of first pixel (always 0x29)
      0x22        gamma_code    SMaL companding curve selector (0–7)
      0x23        tint          white-balance tint code
      0x24        gain_shift    log₂ of sensor gain (0=1×, 1=2×, 2=4×, …)

    Returns a dict with both sensor and derived active dimensions.
    """
    if len(raw_data) < IMG_START:
        return {}
    h = raw_data[:IMG_START]
    sensor_w = struct.unpack_from('<H', h, 0x0E)[0]
    sensor_h = struct.unpack_from('<H', h, 0x0C)[0]
    return {
        'sensor_width':  sensor_w,
        'sensor_height': sensor_h,
        'active_width':  sensor_w - DARK_COLS,
        'active_height': sensor_h - DARK_ROWS,
        'img_start':     struct.unpack_from('<H', h, 0x10)[0],
        'gamma_code':    h[0x22],
        'tint':          h[0x23],
        'gain_shift':    h[0x24],
    }


def extract_bayer(raw_data, sensor_width, active_width, active_height):
    """Copy the active Bayer pixels out of the raw USB download.

    Each row in the download is sensor_width bytes wide; the last DARK_COLS
    bytes are dark reference columns. Builds a compact active_width×active_height
    buffer by taking only the first active_width bytes of each row.
    """
    out = bytearray(active_width * active_height)
    for row in range(active_height):
        src = IMG_START + row * sensor_width
        dst = row * active_width
        out[dst:dst + active_width] = raw_data[src:src + active_width]
    return bytes(out)


def apply_wb(pixels, width, height, gains, pattern='RGGB'):
    """Apply per-channel white balance gains to raw Bayer pixels.

    gains: (R, G, B) floats. Call before debayering so interpolation works
    with colour-corrected values (matches the original driver's pipeline order).
    """
    tile = PATTERNS[pattern]
    channel_gain = dict(zip('RGB', gains))
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
    """Bilinear Bayer demosaicking.

    pixels: compact width×height Bayer bytes from extract_bayer.
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
            p  = pixels[y * width + x]

            if ch == 'G':
                G = p
                if tile[(y % 2, (x + 1) % 2)] == 'R':
                    R = avg(get(y, x-1), get(y, x+1))    # R left/right
                    B = avg(get(y-1, x), get(y+1, x))    # B above/below
                else:
                    R = avg(get(y-1, x), get(y+1, x))    # R above/below
                    B = avg(get(y, x-1), get(y, x+1))    # B left/right
            elif ch == 'R':
                R = p
                G = avg(get(y, x-1), get(y, x+1), get(y-1, x), get(y+1, x))
                B = avg(get(y-1, x-1), get(y-1, x+1), get(y+1, x-1), get(y+1, x+1))
            else:  # B
                B = p
                G = avg(get(y, x-1), get(y, x+1), get(y-1, x), get(y+1, x))
                R = avg(get(y-1, x-1), get(y-1, x+1), get(y+1, x-1), get(y+1, x+1))

            i = (y * width + x) * 3
            rgb[i], rgb[i+1], rgb[i+2] = R, G, B

    return bytes(rgb)


def global_stretch(rgb_bytes, lo_pct=2.0, hi_pct=98.0):
    """Linear histogram stretch across all channels combined.

    A single percentile window applied to R+G+B together preserves colour
    ratios, unlike per-channel stretching which normalises them away.
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
    print("Camera initialized.")
    return dev


def list_pictures(dev):
    cmd = bytearray(16)
    cmd[0] = CMD_LIST_PICTURES
    dev.write(EP_OUT, cmd, timeout=1000)
    data = (bytes(dev.read(EP_IN, 32768, timeout=5000)) +
            bytes(dev.read(EP_IN, 32768, timeout=5000)))
    num_pics = data[0x105]
    print(f"Found {num_pics} picture(s).")
    pictures = []
    for i in range(num_pics):
        offset = 0x106 + i * 16
        if offset + 11 <= len(data):
            name = data[offset:offset+11].decode('ascii', errors='ignore')
            if len(name) == 11 and name[7] == ' ':
                name = name[:7] + '.' + name[8:]
            pictures.append(name.strip())
    return pictures


def download_picture(dev, filename):
    cmd = bytearray(16)
    cmd[0] = CMD_GET_PICTURE
    cmd[1] = 0x01
    fname_bytes = filename.encode('ascii')[:11].ljust(11)
    for i, c in enumerate(fname_bytes):
        cmd[3 + i] = c
    dev.write(EP_OUT, cmd, timeout=1000)
    data = bytearray()
    for _ in range(10):
        try:
            data.extend(dev.read(EP_IN, 32768, timeout=5000))
        except usb.core.USBError:
            break
    return bytes(data)


def delete_all_pictures(dev):
    # Camera requires a list command before it will accept a delete command.
    cmd = bytearray(16)
    cmd[0] = CMD_LIST_PICTURES
    dev.write(EP_OUT, cmd, timeout=1000)
    dev.read(EP_IN, 32768, timeout=5000)
    dev.read(EP_IN, 32768, timeout=5000)
    cmd = bytearray(16)
    cmd[0] = CMD_DELETE_ALL
    cmd[1] = 0x01
    dev.write(EP_OUT, cmd, timeout=1000)
    print("All pictures deleted.")


def save_png(png_path, raw_data, width=IMG_W, height=IMG_H,
             sensor_width=None, pattern='RGGB', stretch=True, saturation=2.0):
    """Full pipeline: extract Bayer → debayer → stretch → save PNG."""
    from PIL import ImageEnhance
    sw = sensor_width if sensor_width is not None else width + DARK_COLS
    pixels = extract_bayer(raw_data, sw, width, height)
    rgb = debayer_bilinear(pixels, width, height, pattern)
    if stretch:
        rgb = global_stretch(rgb)
    img = Image.frombytes('RGB', (width, height), rgb)
    if saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(saturation)
    img.save(png_path, 'PNG')
    print(f"  Saved: {png_path}")


def save_ppm(ppm_path, raw_data, width=IMG_W, height=IMG_H, sensor_width=None):
    """Save extracted Bayer data as a grayscale P5 PPM (no debayering)."""
    sw = sensor_width if sensor_width is not None else width + DARK_COLS
    pixels = extract_bayer(raw_data, sw, width, height)
    with open(ppm_path, 'wb') as f:
        f.write(f"P5\n{width} {height}\n255\n".encode())
        f.write(pixels)
    print(f"  Saved: {ppm_path}")


def main():
    parser = argparse.ArgumentParser(description='Download photos from Logitech Pocket Digital')
    parser.add_argument('output_dir', nargs='?', default=None)
    parser.add_argument('--raw', action='store_true',
                        help='Also save raw Bayer PPM files')
    parser.add_argument('--saturation', type=float, default=2.0,
                        help='Colour saturation multiplier (default: 2.0)')
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else Path('pictures')
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output: {output_dir}  sat={args.saturation}×")

    try:
        dev = find_camera()
        init_camera(dev)

        pictures = list_pictures(dev)
        if not pictures:
            print("No pictures found.")
            return

        print(f"Downloading {len(pictures)} picture(s)...")
        for i, pic in enumerate(pictures):
            safe_name = pic.replace(' ', '_').replace('/', '_')
            print(f"[{i+1}/{len(pictures)}] {pic}", end=" ")
            sys.stdout.flush()

            try:
                data = download_picture(dev, pic)
                if len(data) <= IMG_START:
                    print(f"ERROR: too little data ({len(data)} bytes)")
                    continue

                meta = parse_header(data)
                w  = meta.get('active_width',  IMG_W)
                h  = meta.get('active_height', IMG_H)
                sw = meta.get('sensor_width',  w + DARK_COLS)
                print(f"({meta['sensor_width']}×{meta['sensor_height']} → {w}×{h}  "
                      f"gamma={meta['gamma_code']}  tint={meta['tint']}  "
                      f"gain={2**meta['gain_shift']}×)", end=" ")

                save_png(str(output_dir / f"{safe_name}.png"), data,
                         width=w, height=h, sensor_width=sw,
                         saturation=args.saturation)

                if args.raw:
                    save_ppm(str(output_dir / f"{safe_name}.ppm"), data,
                             width=w, height=h, sensor_width=sw)

            except Exception as e:
                import traceback
                print(f"ERROR: {e}")
                traceback.print_exc()

        print(f"\nDone. Files saved to: {output_dir}")

    except usb.core.USBError as e:
        print(f"USB error: {e}")
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
