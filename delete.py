#!/usr/bin/env python3
"""Delete all photos from the Logitech Pocket Digital camera."""

from common import find_camera, init_camera, release_camera, delete_all_pictures

dev = find_camera()
init_camera(dev)
answer = input("Delete all photos from camera? [y/N] ")
if answer.strip().lower() == 'y':
    delete_all_pictures(dev)
else:
    print("Aborted.")
release_camera(dev)
