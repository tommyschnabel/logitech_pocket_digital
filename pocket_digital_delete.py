#!/usr/bin/env python3
"""Delete all photos from the Logitech Pocket Digital camera."""

import usb.util
from pocket_digital_download import find_camera, init_camera, delete_all_pictures

dev = find_camera()
init_camera(dev)
answer = input("Delete all photos from camera? [y/N] ")
if answer.strip().lower() == 'y':
    delete_all_pictures(dev)
else:
    print("Aborted.")
usb.util.release_interface(dev, 0)
usb.util.dispose_resources(dev)
