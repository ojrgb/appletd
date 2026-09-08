"""The face's three angles are OUR arithmetic, not Vision's numbers.

Vision QUANTISES `roll`, `yaw` and `pitch` on `VNFaceObservation` — measured on a live
face at revision 3, the highest the request offers: yaw arrives in 45-degree steps and
roll in 30, so a head turned slowly reads 0, 0, 0, then -45. Nothing downstream can use
that, and no revision fixes it.

`face_angles` recomputes all three from the four key points, which cannot be quantised
because the arithmetic is ours. This checks the properties that make it worth trusting:
roll is exact, the three axes do not leak into each other, and a missing point reports
zero rather than a plausible wrong angle.

Ref: appletd/face_types.py.
"""

from __future__ import annotations

import math

import pytest

from appletd.face_types import Face, face_angles
from appletd.types import Confidence, NormX, NormY

# A face looking straight at the camera, in BOX-relative coordinates: eyes level and
# symmetric, nose on the midline, mouth below. `_FACE_ASPECT_AT_REST` is the eye-line
# to mouth distance over the eye separation, so these are laid out to match it.
EYE_SEPARATION = 0.4
REST = 1.05


def _face(eye_l: tuple[float, float], eye_r: tuple[float, float],
          nose: tuple[float, float], mouth: tuple[float, float], *,
          found: bool = True,
          box: tuple[float, float, float, float] = (0.25, 0.25, 0.5, 0.5),
          ) -> Face:
    """A Face carrying just the four key points, as pupil/nose/lip regions."""
    return Face(
        confidence=Confidence(1.0), quality=Confidence(1.0),
        roll_deg=0.0, yaw_deg=0.0, pitch_deg=0.0,
        bbox_x=NormX(box[0]), bbox_y=NormY(box[1]),
        bbox_w=box[2], bbox_h=box[3],
        landmarks=(
            ("left_pupil", (eye_l,)),
            ("right_pupil", (eye_r,)),
            # `nose_tip_point` finds the point shared by these three regions.
            ("nose", (nose,)),
            ("nose_crest", (nose,)),
            ("median_line", (nose,)),
            ("inner_lips", (mouth,)),
        ),
        found=found)


def _level(yaw_offset: float = 0.0, mouth_drop: float | None = None) -> Face:
    """A level face, optionally with the nose pushed off the midline."""
    if mouth_drop is None:
        mouth_drop = EYE_SEPARATION * REST
    return _face(
        eye_l=(0.5 - EYE_SEPARATION / 2, 0.7),
        eye_r=(0.5 + EYE_SEPARATION / 2, 0.7),
        nose=(0.5 + yaw_offset, 0.7 - mouth_drop / 2),
        mouth=(0.5, 0.7 - mouth_drop))


def test_a_face_that_was_not_found_reports_zeros() -> None:
    from dataclasses import replace

    assert face_angles(replace(_level(), found=False)) == (0.0, 0.0, 0.0)


def test_a_level_face_has_no_roll() -> None:
    _pitch, _yaw, roll = face_angles(_level())
    assert roll == pytest.approx(0.0, abs=1e-9)


def test_a_level_face_has_no_yaw() -> None:
    _pitch, yaw, _roll = face_angles(_level())
    assert yaw == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("degrees", [10.0, 30.0, -20.0, 45.0, -60.0])
def test_roll_is_exact(degrees: float) -> None:
    """A projection determines an in-plane rotation fully, so this is not an
    approximation and is checked as an identity."""
    radians = math.radians(degrees)
    half = EYE_SEPARATION / 2
    face = _face(
        eye_l=(0.5 - half * math.cos(radians), 0.7 - half * math.sin(radians)),
        eye_r=(0.5 + half * math.cos(radians), 0.7 + half * math.sin(radians)),
        nose=(0.5, 0.55), mouth=(0.5, 0.4),
        # A SQUARE box, so the composition through it cannot change the angle. A
        # non-square box legitimately does - the points are box-relative.
        box=(0.25, 0.25, 0.5, 0.5))
    _pitch, _yaw, roll = face_angles(face)
    assert roll == pytest.approx(degrees, abs=1e-6)


def test_yaw_follows_the_nose_off_the_midline() -> None:
    """Sign and monotonicity, which is what a project actually reads."""
    left = face_angles(_level(yaw_offset=-0.08))[1]
    centre = face_angles(_level(yaw_offset=0.0))[1]
    right = face_angles(_level(yaw_offset=+0.08))[1]
    assert left < centre < right


def test_yaw_saturates_rather_than_raising() -> None:
    """The nose past an eye is beyond what the approximation covers; `asin` would
    raise on a ratio over 1 and this clamps instead."""
    for offset in (-1.0, 1.0):
        yaw = face_angles(_level(yaw_offset=offset))[1]
        assert -90.0 <= yaw <= 90.0


def test_roll_does_not_leak_into_yaw() -> None:
    """Measured in the FACE's basis, not the image's - so a tilted head with its nose
    on the midline still reads no yaw. Measuring in image coordinates would not."""
    radians = math.radians(35.0)
    half = EYE_SEPARATION / 2
    drop = EYE_SEPARATION * REST
    # the whole face rotated about the eye midpoint
    def spun(point: tuple[float, float]) -> tuple[float, float]:
        dx, dy = point[0] - 0.5, point[1] - 0.7
        return (0.5 + dx * math.cos(radians) - dy * math.sin(radians),
                0.7 + dx * math.sin(radians) + dy * math.cos(radians))
    face = _face(
        eye_l=spun((0.5 - half, 0.7)), eye_r=spun((0.5 + half, 0.7)),
        nose=spun((0.5, 0.7 - drop / 2)), mouth=spun((0.5, 0.7 - drop)),
        box=(0.25, 0.25, 0.5, 0.5))
    _pitch, yaw, roll = face_angles(face)
    assert roll == pytest.approx(35.0, abs=1e-6)
    assert yaw == pytest.approx(0.0, abs=1e-6)


def test_a_missing_key_point_reports_zeros_not_a_plausible_angle() -> None:
    """A face with no landmarks at all. Zero is the convention every channel here
    uses for absent, and a plausible wrong angle is the failure to avoid."""
    bare = Face(confidence=Confidence(1.0), quality=Confidence(1.0),
                roll_deg=0.0, yaw_deg=0.0, pitch_deg=0.0,
                bbox_x=NormX(0.25), bbox_y=NormY(0.25), bbox_w=0.5, bbox_h=0.5,
                landmarks=(), found=True)
    assert face_angles(bare) == (0.0, 0.0, 0.0)


def test_coincident_eyes_do_not_divide_by_zero() -> None:
    """Two pupils at the same point is degenerate rather than impossible."""
    face = _face(eye_l=(0.5, 0.7), eye_r=(0.5, 0.7), nose=(0.5, 0.55),
                 mouth=(0.5, 0.4))
    assert face_angles(face) == (0.0, 0.0, 0.0)


def test_it_costs_almost_nothing() -> None:
    """Omer asked. It is a handful of atan2/hypot per face, so the bar is generous -
    the whole face stream costs about 8 ms an inference."""
    import time

    face = _level(yaw_offset=0.03)
    face_angles(face)                                   # warm
    started = time.perf_counter()
    for _ in range(2000):
        face_angles(face)
    per_call_us = (time.perf_counter() - started) / 2000 * 1e6
    assert per_call_us < 50, "%.1f us per face" % per_call_us
    print("\nface_angles: %.2f us per face" % per_call_us)


def test_the_frame_aspect_is_taken_into_account() -> None:
    """NORMALISED IMAGE SPACE IS NOT SQUARE, and this is the check that says so.

    x and y are each normalised by their own dimension, so a 16:9 frame stretches y
    against x. MEASURED on a real face: the eye-line-to-mouth over eye-separation
    ratio read 2.09 in normalised space where the anatomy is about 1.15. An angle
    measured there is not an angle.

    A face rolled 45 degrees IN PIXELS must read 45. In normalised space the same
    points read something else, and that difference is the bug this prevents.
    """
    width, height = 1280, 720
    # a 45-degree eye line in PIXEL space, inside a square-in-pixels box
    box_px = 200.0
    box = (0.3, 0.3, box_px / width, box_px / height)
    half_px = 40.0
    # box-relative coordinates that put the eyes on a true 45 degrees in pixels
    dx = half_px / box_px
    dy = half_px / box_px
    face = _face(eye_l=(0.5 - dx, 0.5 - dy), eye_r=(0.5 + dx, 0.5 + dy),
                 nose=(0.5, 0.4), mouth=(0.5, 0.2), box=box)

    _pitch, _yaw, roll_px = face_angles(face, width, height)
    assert roll_px == pytest.approx(45.0, abs=1e-6)

    # and without a frame size it is measured in normalised space, which for a
    # non-square frame is a DIFFERENT number - consistent, but not the true angle
    _pitch, _yaw, roll_norm = face_angles(face)
    assert abs(roll_norm - 45.0) > 5.0


def test_pitch_grows_as_the_face_foreshortens() -> None:
    """The vertical face shortens as the head nods, which is where the magnitude
    comes from. Approximate, and only the direction of travel is asserted."""
    width, height = 1280, 720
    upright = _level()
    nodded = _level(mouth_drop=EYE_SEPARATION * REST * 0.7)
    assert abs(face_angles(nodded, width, height)[0]) \
        > abs(face_angles(upright, width, height)[0])


def test_pitch_is_not_pinned_at_zero_for_a_longer_than_average_face() -> None:
    """The bug this shape exists to avoid.

    `acos(ratio / rest)` clamps at 1, so it reads exactly 0 for EVERY face longer
    than the constant, whatever that face does - measured, 700 frames of a real face
    all reading 0.00. Taking the difference from rest is continuous through it.
    """
    width, height = 1280, 720
    longer = _level(mouth_drop=EYE_SEPARATION * REST * 1.4)
    pitch = face_angles(longer, width, height)[0]
    assert pitch != 0.0
    # and it still moves when that face nods
    nodded = _level(mouth_drop=EYE_SEPARATION * REST * 1.2)
    assert face_angles(nodded, width, height)[0] > pitch


def test_pitch_is_monotonic_through_rest() -> None:
    """Direction of travel is what a project reads; the absolute magnitude is not
    claimed. Longer face -> more negative, shorter -> more positive, no step."""
    width, height = 1280, 720
    drops = [EYE_SEPARATION * REST * k for k in (1.4, 1.2, 1.0, 0.8, 0.6)]
    pitches = [face_angles(_level(mouth_drop=d), width, height)[0] for d in drops]
    assert pitches == sorted(pitches), pitches
    assert len(set(pitches)) == len(pitches), "a step means something clamped"
