#!/usr/bin/env python3
"""Zoom gate for the upstream image engine, not the retired shared viewer.

Run with the supported test interpreter through the isolated project runner.
Upstream's navigation-preserves-zoom checks remain authoritative. Existing
composed Chromium checks exercise pinch/pan/keyboard/focus; local input checks
also execute sequentially in WebKit. No test or CSP policy is weakened.
"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    raise SystemExit(subprocess.call([
        sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
        'tests/test_img_lightbox_pinch_zoom.py',
        'tests/test_img_lightbox_gestures_behavior.py',
        'tests/test_img_lightbox_gestures_composed.py',
        'tests/test_img_lightbox_local_inputs.py',
        'tests/test_img_lightbox_pointer_ownership.py',
        *sys.argv[1:],
    ], cwd=ROOT))
