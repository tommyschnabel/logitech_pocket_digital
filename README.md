# Logitech Pocket Digital

An open-source Python driver for the Logitech Pocket Digital (2003), a credit-card-sized
2MP digital camera built around the SMaL Camera Technologies UltraPocket sensor.

The original macOS driver shipped as a PowerPC binary that stopped working after Rosetta
was removed in OS X Lion (2011). This project reverse-engineers the USB protocol and
image processing pipeline so the camera works on any modern system.

---

## The Camera

The Logitech Pocket Digital was released in 2003. It stores photos on internal flash
memory and transfers them over USB. Key specs:

- **Sensor:** SMaL UltraPocket (VID `0x046D` / PID `0x0950`)
- **Resolution:** 640×480 (VGA) and nominally 1024×768 (XGA)
- **Image format:** Raw Bayer (RGGB), 8-bit companded, stored on-device

The sensor uses SMaL's piecewise-linear companding to fit a 10-bit linear ADC reading
into 8 bits — similar to a gamma curve, with parameters stored in each image's header.

---

## Requirements

```
pip install pyusb Pillow
```

On Linux you may need a udev rule to access the camera without root:

```
SUBSYSTEM=="usb", ATTR{idVendor}=="046d", ATTR{idProduct}=="0950", MODE="0666"
```

On macOS, PyUSB works directly via libusb (`brew install libusb`).

---

## Usage

```
python download.py [output_dir] [options]
python delete.py  # delete all photos from camera
```

By default photos are saved to `pictures/` in the current directory.

| Option | Description |
|---|---|
| `--raw` | Also save raw Bayer data as grayscale PPM files |
| `--saturation 2.0` | Colour saturation multiplier (default: 2.0) |
| `--wb R,G,B` | Per-channel white-balance gains (default: 1.0,1.0,1.0) |
| `--no-dark-subtract` | Disable dark-current subtraction using reference columns |

---

## Android / Termux

The camera can be used on Android via Termux. Android requires explicit user permission
before apps can access USB devices; the scripts work around this using the
[termux-usb](https://wiki.termux.com/wiki/Termux-usb) tool from termux-api.

### 1. Install Termux:API

Install **Termux:API** from the same source as Termux (F-Droid or Google Play).

### 2. Install packages inside Termux

```bash
pkg update
pkg install termux-api libusb python pyusb Pillow
```

**Important:** You need libusb ≥ 1.0.29-1 which includes the Termux USB patch
(`TERMUX_USB_FD` support). Check with `pkg show libusb`.

### 3. Find the camera

```bash
termux-usb -l
```

Note the device path (e.g. `/dev/bus/usb/001/002`).

### 4. Run the scripts

You must run the scripts **through** `termux-usb -E` so Android can pass an
authorized file descriptor to libusb. The `-E` flag sets `TERMUX_USB_FD` which
the patched libusb reads internally.

**First time** (grants permission — shows a system dialog):

```bash
termux-usb -r -E -e "./download.py" /dev/bus/usb/001/002
```

**After permission is granted** (no dialog needed):

```bash
termux-usb -E -e "./download.py" /dev/bus/usb/001/002
```

The scripts will print instructions if you run them directly without `termux-usb`.

---

## How It Works

### USB Protocol

The camera uses three bulk-transfer commands over a single USB interface:

| Command | Code | Description |
|---|---|---|
| List pictures | `0x12` | Returns a 64 KB response; `[0x105]` is the count, filenames follow at `0x106 + i*16` as 11-byte DOS 8.3 names |
| Download picture | `0x11` | Returns up to 10 × 32 KB packets; first 41 bytes are the image header, remainder is raw pixel data |
| Delete all | `0x18` | Erases all stored photos (must be preceded by a list command) |

### Image Format

Each download begins with a 41-byte header containing sensor dimensions, exposure
metadata, and the SMaL companding parameters. Pixel data follows immediately after.

The camera sends each row at `sensor_width` (e.g. 644) bytes — it does **not** strip
the 4 dark reference columns before transfer. The active image occupies the first
`active_width` (640) columns of each row; the trailing bytes are discarded during
extraction.

### Processing Pipeline

```
Raw USB download
    └─ extract_bayer()      strip dark reference columns, pack to active_width × active_height
    └─ debayer_bilinear()   bilinear demosaicking (RGGB pattern)
    └─ global_stretch()     2–98% percentile stretch across all channels (preserves colour ratios)
    └─ ImageEnhance.Color   saturation boost (2× default)
```

This matches the broad shape of the original driver's pipeline (dark remap → interpolation →
gamma/matrix → stretch → sharpen), minus the steps that require parameters not yet fully
decoded from the binary.

---

## Reverse Engineering Notes

The original driver is a PowerPC Mach-O binary (`Logitech Pocket Digital.app`) buried
inside `Logitech Pocket Digital.pkg`. It links against three SMaL Camera Technologies
libraries: `CSMaLUltrapocketPublicApi`, `CSMaLUltrapocketApi`, and `CSMaLCameraBaseApi`.

Key findings from analysis of the binary and empirical USB testing:

- **USB protocol** was mapped by cross-referencing `strings` output from the binary
  with observed USB traffic.
- **Stride = 644, not 640.** The camera does not crop dark columns before transfer.
  Using the wrong stride causes diagonal tearing across the entire image.
- **Bayer pattern is RGGB**, not BGGR as the SMaL datasheet implies. Confirmed
  empirically by swapping R and B channels and checking that known-red subjects
  turned the correct colour.
- **Global stretch preserves colour.** Per-channel histogram stretching (the
  obvious approach) equalises all three channels to the same range, which destroys
  colour. Stretching all channels by a single global window keeps the ratios intact.

Full protocol documentation, header field offsets, and known-unknown fields are in
[CLAUDE.md](CLAUDE.md).

---

## Current Status

| Feature | Status |
|---|---|
| USB download | ✅ |
| Header parsing (dims, gamma, tint, gain) | ✅ |
| Bayer demosaicking (RGGB, bilinear) | ✅ |
| Global histogram stretch | ✅ |
| Android / Termux support | ✅ requires libusb ≥ 1.0.29-1 with `-E` flag |
| SMaL companding linearisation | ❌ `t_bp[0..6]` breakpoints not yet decoded |
| Dark-current subtraction | ❌ |
| Colour matrix | ❌ |
| XGA download (>10 USB packets) | ❌ |
