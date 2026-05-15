"""
Logitech Pocket Digital — USB protocol and camera I/O.
SMaL UltraPocket sensor, VID:046D PID:0950.

Commands are 16-byte bulk-OUT packets; responses are bulk-IN.
Each image download begins with a 41-byte header followed by raw Bayer data
at stride=sensor_width (dark reference columns are NOT stripped by firmware).
"""

import struct
import os
import sys
import threading
import time
import usb.core
import usb.util

VID = 0x046D
PID = 0x0950
EP_OUT = 0x01
EP_IN  = 0x81

CMD_LIST_PICTURES = 0x12
CMD_GET_PICTURE   = 0x11
CMD_DELETE_ALL    = 0x18

IMG_START = 0x29

DARK_COLS = 4
DARK_ROWS = 2

IMG_W = 640
IMG_H = 480


def start_periodic_stdout_flush(interval=1.0):
    """Start a daemon thread that flushes sys.stdout every *interval* seconds."""
    def loop():
        while True:
            time.sleep(interval)
            sys.stdout.flush()
    t = threading.Thread(target=loop, daemon=True)
    t.start()


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


def _is_android():
    """Check if we're running inside Termux on Android."""
    if os.environ.get('TERMUX_VERSION') or os.environ.get('TERMUX_API_VERSION'):
        return True
    if 'com.termux' in os.environ.get('PREFIX', ''):
        return True
    if 'com.termux' in os.environ.get('HOME', ''):
        return True
    return os.path.exists('/data/data/com.termux')


def _has_termux_usb_fd():
    """Check if TERMUX_USB_FD is set (from termux-usb -E)."""
    fd = os.environ.get('TERMUX_USB_FD')
    return fd is not None and fd.isdigit() and int(fd) >= 0


def _termux_list_devices():
    """Return list of USB device paths from termux-usb -l, or empty list."""
    import subprocess
    try:
        result = subprocess.run(
            ['termux-usb', '-l'], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            import json
            try:
                devices = json.loads(result.stdout)
                if isinstance(devices, list):
                    return devices
            except json.JSONDecodeError:
                pass
    except (OSError, subprocess.TimeoutExpired):
        pass
    return []


def _termux_usage(devices):
    """Print instructions for running via termux-usb and exit."""
    me = os.path.basename(sys.argv[0])
    print("\nAndroid / Termux detected.", file=sys.stderr)
    print("The script must be run via termux-usb -E so Android grants USB permission.\n", file=sys.stderr)
    if devices:
        for path in devices:
            print(f"  termux-usb -r -E -e './{me}' {path}", file=sys.stderr)
    else:
        print("No USB devices found. Make sure the camera is plugged in.", file=sys.stderr)
        print(f"\n  termux-usb -l                    # list devices", file=sys.stderr)
        print(f"  termux-usb -r -E -e './{me}' <device_path>", file=sys.stderr)
    print("\nThe -r flag shows the permission dialog (needed once).", file=sys.stderr)
    print("The -E flag is required -- it sets TERMUX_USB_FD for the patched libusb.", file=sys.stderr)
    print("\nAfter permission is granted you can omit -r:", file=sys.stderr)
    if devices:
        print(f"  termux-usb -E -e './{me}' {devices[0]}", file=sys.stderr)
    print("\nRequires: libusb >= 1.0.29-1 (Termux package with termux-usb support)", file=sys.stderr)
    sys.exit(1)


def find_camera():
    """Find the camera via pyusb; on Android require termux-usb -E."""
    if _is_android():
        if _has_termux_usb_fd():
            dev = usb.core.find(idVendor=VID, idProduct=PID)
            if dev is None:
                dev = usb.core.find()
                if dev is None:
                    raise RuntimeError("No USB device found via termux-usb.")
            return dev
        devices = _termux_list_devices()
        _termux_usage(devices)

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
    except (usb.core.USBError, NotImplementedError):
        pass
    usb.util.claim_interface(dev, 0)
    print("Camera initialized.")
    return dev


def release_camera(dev):
    try:
        usb.util.release_interface(dev, 0)
        usb.util.dispose_resources(dev)
    except Exception:
        pass


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
    while True:
        try:
            data.extend(dev.read(EP_IN, 32768, timeout=5000))
        except usb.core.USBError:
            break
    return bytes(data)


def delete_all_pictures(dev):
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
