#!/usr/bin/env python3
"""
Download and process photos from the Logitech Pocket Digital.

Bayer pattern: RGGB (empirically confirmed; datasheet says BGGR).
  Row 0 (even): R G R G ...
  Row 1 (odd):  G B G B ...

Processing pipeline: extract Bayer → bilinear debayer → global stretch → saturation boost.
"""

import sys
import argparse
from pathlib import Path
from PIL import Image, ImageEnhance

from common import (
    find_camera, init_camera, release_camera,
    list_pictures, download_picture, parse_header,
    IMG_START, DARK_COLS, IMG_W, IMG_H,
    start_periodic_stdout_flush
)

# (row_parity, col_parity) → channel
PATTERNS = {
    'RGGB': {(0,0):'R', (0,1):'G', (1,0):'G', (1,1):'B'},
    'BGGR': {(0,0):'B', (0,1):'G', (1,0):'G', (1,1):'R'},
    'GRBG': {(0,0):'G', (0,1):'R', (1,0):'B', (1,1):'G'},
    'GBRG': {(0,0):'G', (0,1):'B', (1,0):'R', (1,1):'G'},
}


def extract_bayer(raw_data, sensor_width, active_width, active_height, dark_subtract=True):
    """Copy active Bayer pixels and optionally subtract per-row dark current.

    Each row is sensor_width bytes wide; the last (sensor_width - active_width)
    bytes are dark reference columns. When dark_subtract is True, their average
    is subtracted from every pixel in that row to remove dark-current bias.
    """
    num_dark = sensor_width - active_width
    out = bytearray(active_width * active_height)
    for row in range(active_height):
        row_start = IMG_START + row * sensor_width
        row_data = raw_data[row_start:row_start + active_width]
        if dark_subtract and num_dark > 0:
            dark = sum(raw_data[row_start + active_width:row_start + sensor_width]) // num_dark
            if dark > 0:
                lut = bytes(max(0, v - dark) for v in range(256))
                row_data = row_data.translate(lut)
        out[row * active_width:row * active_width + active_width] = row_data
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


def save_png(png_path, raw_data, width=IMG_W, height=IMG_H,
             sensor_width=None, saturation=2.0, wb_gains=(1.0, 1.0, 1.0),
             dark_subtract=True):
    """Full pipeline: extract Bayer → [dark subtract] → [WB] → debayer → stretch → save PNG."""
    sw = sensor_width if sensor_width is not None else width + DARK_COLS
    pixels = extract_bayer(raw_data, sw, width, height, dark_subtract=dark_subtract)
    if wb_gains != (1.0, 1.0, 1.0):
        pixels = apply_wb(pixels, width, height, wb_gains)
    rgb = global_stretch(debayer_bilinear(pixels, width, height))
    img = ImageEnhance.Color(Image.frombytes('RGB', (width, height), rgb)).enhance(saturation)
    img.save(png_path, 'PNG')
    print(f"  Saved: {png_path}")


def save_ppm(ppm_path, raw_data, width=IMG_W, height=IMG_H, sensor_width=None,
             dark_subtract=True):
    """Save extracted Bayer data as a grayscale P5 PPM (no debayering)."""
    sw = sensor_width if sensor_width is not None else width + DARK_COLS
    pixels = extract_bayer(raw_data, sw, width, height, dark_subtract=dark_subtract)
    with open(ppm_path, 'wb') as f:
        f.write(f"P5\n{width} {height}\n255\n".encode())
        f.write(pixels)
    print(f"  Saved: {ppm_path}")
    

def main():
    start_periodic_stdout_flush(1.0)

    output_dir = Path(args.output_dir) if args.output_dir else Path('pictures')
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output: {output_dir}  sat={args.saturation}×  wb={wb_gains}  dark_subtract={dark_subtract}")

    dev = None
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
                         saturation=args.saturation, wb_gains=wb_gains,
                         dark_subtract=dark_subtract)

                if args.raw:
                    save_ppm(str(output_dir / f"{safe_name}.ppm"), data,
                             width=w, height=h, sensor_width=sw,
                             dark_subtract=dark_subtract)

            except Exception as e:
                import traceback
                print(f"ERROR: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)

        print(f"\nDone. Files saved to: {output_dir}")

    except Exception as e:
        import traceback
        print(f"Error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    finally:
        if dev:
            release_camera(dev)


if __name__ == "__main__":
    main()
