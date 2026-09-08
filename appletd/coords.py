"""Every coordinate conversion in the system, and nothing else.

    Vision reports normalised positions with the origin at the BOTTOM LEFT, y
    increasing upwards. TouchDesigner's TOP arrays are bottom-up and its UVs are
    bottom-left. THEY ALREADY AGREE. NOTHING IN THE PIPELINE FLIPS Y.

The engine keeps Vision-native bottom-left coordinates end to end, across the CHOP
boundary and out into the project. The only consumers needing a top-left origin are
cv2 and PIL - the debug overlay - and that flip exists in exactly one function here.

Getting this wrong does not crash and does not look broken: it produces landmarks that
track a hand correctly and upside down, which is why the rule is stated once, here.

Thread: pure functions. Safe anywhere.
Ref: DESIGN.md 7.
"""

from __future__ import annotations

from typing import NewType

from appletd.types import NormX, NormY

# Pixel spaces, distinguished by where their origin is. These are separate types
# from each other and from NormX/NormY on purpose: the mistake this module
# exists to prevent is passing a value from one space to a function expecting
# another, and mypy can catch that for free if the spaces are not all `float`.
PixelXBL = NewType("PixelXBL", float)   # 0..width,  origin bottom-left
PixelYBL = NewType("PixelYBL", float)   # 0..height, origin bottom-left, y UP
PixelXTL = NewType("PixelXTL", float)   # 0..width,  origin top-left
PixelYTL = NewType("PixelYTL", float)   # 0..height, origin top-left, y DOWN
NormYTL = NewType("NormYTL", float)     # 0..1, origin top-left, y DOWN


def norm_y_bl_to_tl(y_norm: NormY) -> NormYTL:
    """Flip normalised y from bottom-left origin to top-left origin.

    THIS IS THE ONLY FLIP IN THE CODEBASE. Every top-left consumer routes
    through it, so there is exactly one expression to check when the picture
    comes out upside down, and exactly one place a reviewer has to look.

    Contract: bottom-left normalised in, top-left normalised out.
    Why not inline `1 - y` at the call sites: two call sites become four, and
              then one of them gets a flip added "to fix" an unrelated bug and
              the two disagree. One expression, one place.
    """
    return NormYTL(1.0 - y_norm)


def norm_to_px_bl(x_norm: NormX, y_norm: NormY,
                  width_px: int, height_px: int) -> tuple[PixelXBL, PixelYBL]:
    """Normalised bottom-left -> pixels, still bottom-left. NO flip.

    Contract: (0,0) -> (0, 0) is the bottom-left corner of the image, and
              (1,1) -> (width, height) is the top-right. y increases upwards,
              exactly as Vision and TouchDesigner both expect.
    Why it exists at all, given the engine publishes normalised values: TD-side
              maths that wants pixels (a hit test against a TOP's resolution,
              say) should get them from here rather than multiplying by hand in
              a project somewhere, where the convention would then be invisible.
    """
    return PixelXBL(x_norm * width_px), PixelYBL(y_norm * height_px)


def norm_to_px_tl(x_norm: NormX, y_norm: NormY,
                  width_px: int, height_px: int) -> tuple[PixelXTL, PixelYTL]:
    """Normalised bottom-left -> pixels, top-left origin. FLIPS y.

    Contract: (0,0) -> (0, height) - Vision's bottom-left corner is cv2's
              bottom-left corner, which in a top-left-origin space is y=height.
              (0,1) -> (0, 0).
    Who may call this: the cv2/PIL debug overlay, and nothing else. If engine.py
              or td/ ever calls it, the coordinate contract in DESIGN.md 7 has
              been broken and the landmarks will be mirrored.
    """
    y_tl = norm_y_bl_to_tl(y_norm)          # the one flip, borrowed not repeated
    return PixelXTL(x_norm * width_px), PixelYTL(y_tl * height_px)


def clamp_unit(value: float) -> float:
    """Clamp to 0..1.

    Why this is not applied to landmarks before publishing: Vision can place a
    joint slightly outside the frame when part of the hand is out of shot, and
    that overshoot is real information - it says which way the hand left. The
    engine publishes it raw. Clamping is for drawing only, where an out-of-range
    pixel index is an exception rather than a nuance.
    """
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value
