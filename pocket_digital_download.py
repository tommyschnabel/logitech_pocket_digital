#!/usr/bin/env python3
"""
Logitech Pocket Digital - Photo Downloader
"""

import usb.core
import usb.util
import sys
from pathlib import Path
from PIL import Image
import argparse

VID = 0x046D
PID = 0x0950
EP_OUT = 0x01
EP_IN = 0x81

CMD_LIST_PICTURES = 0x12
CMD_GET_PICTURE = 0x11
CMD_DELETE_ALL = 0x18


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


def debayer_simple(raw_data, width=640, height=480):
    """Simple debayer for BGGR Bayer pattern - outputs RGB.
    BGGR 2x2:
    B G
    G R
    """
    IMG_START = 0x29

    # Create output RGB image
    rgb_data = bytearray(width * height * 3)

    for y in range(height):
        for x in range(width):
            src_idx = IMG_START + y * width + x
            if src_idx >= len(raw_data):
                # Out of bounds
                rgb_idx = (y * width + x) * 3
                rgb_data[rgb_idx] = 0
                rgb_data[rgb_idx + 1] = 0
                rgb_data[rgb_idx + 2] = 0
                continue

            pixel = raw_data[src_idx]

            # Determine position in Bayer pattern
            # BGGR: (0,0)=B, (0,1)=G, (1,0)=G, (1,1)=R
            is_even_row = (y % 2 == 0)
            is_even_col = (x % 2 == 0)

            rgb_idx = (y * width + x) * 3

            if is_even_row and is_even_col:
                # B pixel
                b = pixel
                # G from neighbors
                g = pixel  # fallback
                r = pixel  # fallback
                # Simple interpolation
                neighbors = []
                if y > 0: neighbors.append(raw_data[src_idx - width])
                if y < height - 1: neighbors.append(raw_data[src_idx + width])
                if x > 0: neighbors.append(raw_data[src_idx - 1])
                if x < width - 1: neighbors.append(raw_data[src_idx + 1])
                if neighbors:
                    g = sum(neighbors) // len(neighbors)
                r = pixel  # R at B position needs interpolation

            elif is_even_row and not is_even_col:
                # G pixel (top row of 2x2)
                g = pixel
                b = pixel
                r = pixel

            elif not is_even_row and is_even_col:
                # G pixel (bottom row of 2x2)
                g = pixel
                b = pixel
                r = pixel

            else:
                # R pixel
                r = pixel
                b = pixel
                g = pixel

            rgb_data[rgb_idx] = r
            rgb_data[rgb_idx + 1] = g
            rgb_data[rgb_idx + 2] = b

    return bytes(rgb_data)


def save_png_debayered(png_path, raw_data, width=640, height=480):
    """Debayer raw Bayer data and save as PNG."""
    rgb = debayer_simple(raw_data, width, height)
    img = Image.frombytes('RGB', (width, height), rgb)
    img.save(png_path, 'PNG')
    print(f"  Saved: {png_path}")


def save_ppm(ppm_path, raw_data, width=640, height=480):
    """Save raw Bayer data as PPM (grayscale)."""
    IMG_START = 0x29
    with open(ppm_path, 'wb') as f:
        header = f"P5\n{width} {height}\n255\n".encode()
        f.write(header)
        f.write(raw_data[IMG_START:IMG_START + width * height])
    print(f"  Saved: {ppm_path}")


def main():
    parser = argparse.ArgumentParser(description='Download photos from Logitech Pocket Digital')
    parser.add_argument('output_dir', nargs='?', default=None)
    parser.add_argument('--raw', action='store_true', help='Also save raw PPM files')
    parser.add_argument('--delete', action='store_true', help='Delete photos after downloading')
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else Path.home() / "Pictures" / "PocketDigital"
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {output_dir}")

    try:
        print("Searching for camera...")
        dev = find_camera()
        init_camera(dev)

        pictures = list_pictures(dev)
        if not pictures:
            print("No pictures found.")
            return

        print(f"\nDownloading {len(pictures)} pictures...")

        for i, pic in enumerate(pictures):
            safe_name = pic.replace(' ', '_').replace('/', '_')
            print(f"[{i+1}/{len(pictures)}] {pic}...", end=" ")
            sys.stdout.flush()

            try:
                data = download_picture(dev, pic)
                if len(data) > 0x29:
                    png_path = output_dir / f"{safe_name}.png"
                    save_png_debayered(str(png_path), data)
                    if args.raw:
                        ppm_path = output_dir / f"{safe_name}.ppm"
                        save_ppm(str(ppm_path), data)
                else:
                    print(f"ERROR: Too little data ({len(data)} bytes)")
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
        except:
            pass


if __name__ == "__main__":
    main()