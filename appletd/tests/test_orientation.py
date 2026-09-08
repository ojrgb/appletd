"""The orientation constants are the ones CoreGraphics means.

`streams.py` carries them as plain ints because the TouchDesigner builders import
that module and TouchDesigner cannot load pyobjc. This is the other half of that
bargain: here, where pyobjc IS available, the numbers are held against Quartz.

Without it the two could drift with nothing to notice - and a wrong orientation
value does not raise, it silently rotates or mirrors every frame.
"""

from __future__ import annotations

import pytest

from appletd.streams import ORIENTATION_UP, ORIENTATION_UP_MIRRORED, orientation_for


def test_flip_off_is_up() -> None:
    assert orientation_for(False) == ORIENTATION_UP


def test_flip_on_is_mirrored() -> None:
    assert orientation_for(True) == ORIENTATION_UP_MIRRORED


def test_the_constants_match_coregraphics() -> None:
    Quartz = pytest.importorskip("Quartz")
    assert ORIENTATION_UP == int(Quartz.kCGImagePropertyOrientationUp)
    assert ORIENTATION_UP_MIRRORED == int(
        Quartz.kCGImagePropertyOrientationUpMirrored)
