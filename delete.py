#!/usr/bin/env python3
"""Delete all photos from the Logitech Pocket Digital camera."""

import sys

from common import find_camera, init_camera, release_camera, delete_all_pictures, start_periodic_stdout_flush


start_periodic_stdout_flush(1.0)

dev = find_camera()
init_camera(dev)
answer = input("Delete all photos from camera? [y/N] ")
if answer.strip().lower() == 'y':
    delete_all_pictures(dev)
else:
    print("Aborted.")
release_camera(dev)
