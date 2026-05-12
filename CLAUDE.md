# Logitech Pocket Digital — Reverse Engineering Notes

Camera: Logitech Pocket Digital (2003)
Sensor: SMaL Camera Technologies UltraPocket
USB:    VID=0x046D  PID=0x0950

---

## USB Protocol

### Endpoints
- Bulk OUT: 0x01  (host → camera, 16-byte command packets)
- Bulk IN:  0x81  (camera → host, variable-length response)

### Commands (byte 0 of the 16-byte command packet)
| Code | Constant           | Description                         |
|------|--------------------|-------------------------------------|
| 0x12 | CMD_LIST_PICTURES  | Enumerate stored photos             |
| 0x11 | CMD_GET_PICTURE    | Download a single photo             |
| 0x18 | CMD_DELETE_ALL     | Erase all stored photos             |

### List Pictures (0x12)
- Send 16-byte command with cmd[0]=0x12, rest zeros.
- Read two 32 KB IN packets (64 KB total response).
- `response[0x105]` = number of pictures.
- Each picture entry starts at `0x106 + i*16` and is an 11-byte DOS 8.3 filename
  (`IMG0192 XGA` → `IMG0192.XGA`).

### Download Picture (0x11)
- Send command: cmd[0]=0x11, cmd[1]=0x01, cmd[3..13]=11-byte ASCII filename.
- Read up to 10 × 32 KB = 327,680 bytes of IN data (stop on USB timeout).
- First 41 bytes (0x29) are the image header; remaining bytes are raw Bayer pixels.

---

## Raw Image Header (41 bytes / 0x29)

All multi-byte integers are little-endian.

| Offset    | Size   | Field          | Notes                                                          |
|-----------|--------|----------------|----------------------------------------------------------------|
| 0x00      | 1 B    | magic          | Always 0xFF                                                    |
| 0x0C–0x0D | LE16   | sensor_height  | Full sensor height including dark rows (e.g. 482)              |
| 0x0E–0x0F | LE16   | sensor_width   | Full sensor width including dark cols (e.g. 644)               |
| 0x10–0x11 | LE16   | img_start      | Byte offset of first pixel (always 0x29 = 41)                  |
| 0x22      | 1 B    | gamma_code     | SMaL companding curve selector (0–7)                           |
| 0x23      | 1 B    | tint           | White-balance tint code                                        |
| 0x24      | 1 B    | gain_shift     | log₂ of sensor gain (0=1×, 1=2×, 2=4×, 3=8×, …)              |

### Active image dimensions
```
active_width  = sensor_width  - 4   (2 dark reference columns per side)
active_height = sensor_height - 2   (1 dark reference row per side)
```
Example: sensor 644×482 → active image 640×480.

**Download stride = sensor_width (644), NOT active_width (640).**
The camera does NOT strip dark reference columns before the USB transfer.
Each row in the pixel data is `sensor_width` bytes wide; the active image
occupies the first `active_width` columns of each row. The last
`sensor_width - active_width` (= 4) bytes of each row are dark reference
columns (observed values ≈ 3–36) and must be skipped during extraction.

### Partially-decoded fields (offsets 0x12–0x21)
Two 9-byte blocks appear at 0x12 and 0x1B with a repeated pattern
(`e3 ff 43 XX 00 00 00 YY ZZ`). Likely camera register dump or exposure
telemetry. Exact decoding TBD.

### Fields not yet located
Documented in the original driver binary but not yet pinpointed in the header:
`frame_rate`, `t_bp[0..6]` (companding breakpoints), `ave` (image average),
`num_dark_columns`, `sensor_type`, `darkCol`, `fpnRow`,
`interpBorderTop/Bot/Left/Right`.

---

## Pixel Data Format

- **Encoding:** 8-bit companded Bayer values (SMaL piecewise-linear companding).
  Raw values do NOT represent linear light intensity; the SMaL sensor
  compresses a 10-bit linear ADC to 8 bits via a gamma-like curve parameterized
  by `gamma_code` and `t_bp[0..6]`. Proper linearization requires inverting
  this curve (not yet implemented).
- **Bayer pattern:** BGGR
  ```
  Row 0 (even): B G B G B G …
  Row 1 (odd):  G R G R G R …
  ```
- **Dimensions:** `active_width × active_height` (e.g. 640×480 for VGA captures).
- **Layout:** row-major, `active_width` bytes per row, no row padding.

---

## Original Driver Processing Pipeline (SMaLUltrapocketPublicApi)

Extracted from the PPC Mach-O binary in the installer package:

1. **Dark-current remap** (`processing.remap.darkcurrent.enable`)
   Subtract per-column dark offsets using the reference dark columns.
2. **Crosstalk remap** (`processing.remap.crosstalkraw.enable`)
   Correct sensor colour cross-talk in the raw domain.
3. **RGB interpolation** (`processing.rgb.interpolationtype`)
   Bilinear (or higher-order) Bayer demosaicking using BGGR pattern.
4. **Gamma / linearisation** (`processing.rgb.gamma.correct.enable`)
   Expand companded 8-bit values back to linear using the `gamma_code` curve.
   Manual override available via `processing.rgb.gamma.manual.value`.
5. **Colour matrix** (`processing.rgb.matrix.enable`)
   3×3 matrix white-balance and colour-space correction.
   Manual matrix via `processing.rgb.matrix.manual.value`.
6. **YCbCr conversion** (`processing.rgb.ycbcr.enable`)
   Convert to YCbCr for sharpening / noise reduction.
7. **Sharpening** (`processing.rgb.ycbcr.sharpen.enable`)

---

## Original Driver Binary

Location inside the installer:
```
Logitech Pocket Digital.pkg/Contents/Archive.pax.gz
→ System/Library/Image Capture/Devices/Logitech Pocket Digital.app/
    Contents/MacOS/Logitech Pocket Digital   (PPC Mach-O, 2003)
    Contents/Resources/t03-a02-f02.pack      (32 KB — possibly bytecode for
                                              the image-processing pipeline)
```
The binary links against `CSMaLUltrapocketPublicApi`, `CSMaLUltrapocketApi`,
and `CSMaLCameraBaseApi` from SMaL Camera Technologies.

---

## Current Implementation Status

| Step                        | Status                                        |
|-----------------------------|-----------------------------------------------|
| USB download                | ✅ Working                                    |
| Header parsing              | ✅ sensor dims, img_start, gamma_code, tint   |
| Bayer debayering (BGGR)     | ✅ Bilinear interpolation, correct stride=644 |
| Auto-level stretch          | ✅ Per-channel percentile stretch (1–99 %)    |
| Companding linearisation    | ❌ Not implemented (t_bp curve unknown)       |
| Dark-current subtraction    | ❌ Not implemented                            |
| Colour matrix / white bal.  | ❌ Not implemented                            |
| XGA (1024×768) support      | ❌ Download loop only reads 10 × 32 KB        |

---

## Android / Termux Support

The scripts work on Android via Termux using a patched libusb that reads `TERMUX_USB_FD`.

### How It Works

1. `termux-usb -E -e './download.py' /dev/bus/usb/001/002` runs the script with:
   - `-r` flag: shows Android permission dialog (first time only)
   - `-E` flag: sets `TERMUX_USB_FD=<fd>` environment variable
   - The fd is an authorized file descriptor from Android's UsbManager

2. The Termux libusb package (≥1.0.29-1) is patched to:
   - Detect `TERMUX_USB_FD` environment variable during `libusb_init()`
   - Read the fd and create an internal device entry
   - Return that device from `libusb_get_device_list()`
   - Use `dup(fd)` when `libusb_open()` is called

3. PyUSB/usb.core.find() works normally — the patched libusb handles everything.

### Key Insight

The Termux patch avoids the `libusb_wrap_sys_device` assertion crash by patching
the Linux usbfs backend directly instead of using the wrap functions. The device
is created internally with proper refcounting, so `libusb_open()` doesn't assert.

See: https://github.com/termux/termux-packages/pull/21620

### Requirements

- Termux:API app (for termux-usb command)
- `pkg install termux-api libusb python pyusb Pillow`
- libusb ≥ 1.0.29-1 (includes the TERMUX_USB_FD patch)

---

## File Naming Convention

The camera stores files with DOS 8.3 names: `IMGnnnn.EXT` where `EXT` is
`VGA` or `XGA` (resolution/quality mode). Both appear to produce 640×480
pixels in current captures; XGA support requires more download packets.
