"""Tests for the coordinate contract - the highest-value tests in the repo.

A y-flip does not crash and does not look broken (DESIGN.md 7, coords.py). It
produces landmarks that track the hand and are upside down. These tests are the
only mechanism that catches it early, so they check the *direction* of y and not
merely that a number came out.
"""

from __future__ import annotations

from visionhands.coords import (
    clamp_unit,
    norm_to_px_bl,
    norm_to_px_tl,
    norm_y_bl_to_tl,
)
from visionhands.types import NormX, NormY

# A 16:9 frame with distinct width and height, so a transposed x/y shows up as a
# wrong number rather than coincidentally passing. 1280x720 is what we run at
# (MEASURED, DESIGN.md 2.1).
W_PX = 1280
H_PX = 720


def test_bottom_left_path_does_not_flip_y() -> None:
    """Vision's origin and TD's origin agree, so this conversion is pure scale."""
    x_px, y_px = norm_to_px_bl(NormX(0.0), NormY(0.0), W_PX, H_PX)
    assert (x_px, y_px) == (0.0, 0.0)

    x_px, y_px = norm_to_px_bl(NormX(1.0), NormY(1.0), W_PX, H_PX)
    assert (x_px, y_px) == (float(W_PX), float(H_PX))

    # The load-bearing case: a point LOW in Vision's frame must stay low.
    _, y_px = norm_to_px_bl(NormX(0.5), NormY(0.25), W_PX, H_PX)
    assert y_px == 0.25 * H_PX


def test_top_left_path_flips_y_and_leaves_x_alone() -> None:
    """The one legal flip, for cv2/PIL only."""
    x_px, y_px = norm_to_px_tl(NormX(0.0), NormY(0.0), W_PX, H_PX)
    assert x_px == 0.0
    assert y_px == float(H_PX)          # bottom of the image, in a top-left space

    x_px, y_px = norm_to_px_tl(NormX(0.25), NormY(1.0), W_PX, H_PX)
    assert x_px == 0.25 * W_PX          # x is never touched by a y flip
    assert y_px == 0.0


def test_the_two_spaces_disagree_everywhere_except_the_middle() -> None:
    """Regression guard against 'simplifying' the two paths into one.

    If someone removes the flip from norm_to_px_tl, or adds one to
    norm_to_px_bl, the two functions become identical and this fails. That is
    the entire point: the flip must exist in exactly one of them.
    """
    for y in (0.0, 0.1, 0.49, 0.51, 0.9, 1.0):
        _, y_bl = norm_to_px_bl(NormX(0.5), NormY(y), W_PX, H_PX)
        _, y_tl = norm_to_px_tl(NormX(0.5), NormY(y), W_PX, H_PX)
        # float() on both sides deliberately: PixelYBL and PixelYTL are distinct
        # types precisely so they cannot be compared by accident, and comparing
        # them anyway is this test's whole job. mypy flags it without the cast,
        # which is the type system working rather than getting in the way.
        assert float(y_bl) != float(y_tl), \
            "the two spaces collapsed - the flip is wrong at y=%r" % y

    # y = 0.5 is the fixed point of the flip. Included so nobody "fixes" the
    # loop above by making the spaces differ there too.
    _, y_bl = norm_to_px_bl(NormX(0.5), NormY(0.5), W_PX, H_PX)
    _, y_tl = norm_to_px_tl(NormX(0.5), NormY(0.5), W_PX, H_PX)
    assert float(y_bl) == float(y_tl) == H_PX / 2.0


def test_asymmetric_pose_keeps_its_orientation() -> None:
    """DESIGN.md 7's visual check, as an assertion.

    An open palm with fingers up: the wrist sits low in Vision's frame and the
    fingertips high. Bottom-left pixels must preserve that ordering; top-left
    pixels must invert it. Anything else means a flip has leaked in.
    """
    wrist_y = NormY(0.20)
    tip_y = NormY(0.80)

    _, wrist_bl = norm_to_px_bl(NormX(0.5), wrist_y, W_PX, H_PX)
    _, tip_bl = norm_to_px_bl(NormX(0.5), tip_y, W_PX, H_PX)
    assert wrist_bl < tip_bl, "wrist should be BELOW the fingertips in y-up space"

    _, wrist_tl = norm_to_px_tl(NormX(0.5), wrist_y, W_PX, H_PX)
    _, tip_tl = norm_to_px_tl(NormX(0.5), tip_y, W_PX, H_PX)
    assert wrist_tl > tip_tl, "wrist should have a LARGER row index in y-down space"


def test_flip_is_its_own_inverse() -> None:
    """Applying the flip twice returns the original, to float precision.

    Cheap, and it catches an off-by-one dressed up as a flip - `height - y`
    instead of `1 - y` scaled, for instance.
    """
    for y in (0.0, 0.125, 0.5, 0.875, 1.0):
        once = norm_y_bl_to_tl(NormY(y))
        twice = norm_y_bl_to_tl(NormY(once))
        assert twice == y


def test_clamp_unit_only_clamps() -> None:
    assert clamp_unit(-0.4) == 0.0
    assert clamp_unit(1.4) == 1.0
    # In-range values pass through untouched, including the boundaries. A clamp
    # that nudges 0.0 or 1.0 would quietly shift every edge landmark.
    for value in (0.0, 0.0001, 0.5, 0.9999, 1.0):
        assert clamp_unit(value) == value


def test_out_of_frame_overshoot_is_preserved_by_the_converters() -> None:
    """Vision can place a joint outside the frame when the hand is half out.

    That overshoot says which way the hand left, so the converters must not
    silently clamp it (coords.clamp_unit is opt-in, for drawing).
    """
    _, y_px = norm_to_px_bl(NormX(1.2), NormY(-0.1), W_PX, H_PX)
    assert y_px < 0.0
