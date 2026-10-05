#!/usr/bin/env python3
"""Compatibility shim: the app now lives in the ixel_mat package (run `ixel`).

Kept because launchers from installs before 0.3 run this file directly.
"""
from ixel_mat.cli import main

if __name__ == "__main__":
    main()
