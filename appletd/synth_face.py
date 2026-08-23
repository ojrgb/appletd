"""Synthetic faces: a `FaceFrame` from a handful of numbers. No camera, no Vision.

The face counterpart of `synth.py` and `synth_body.py`. It produces the whole face
contract - the observation's own numbers AND all 87 landmark slots per face - from a
bounding box and three angles.

WHAT THE LANDMARKS ARE, and this is the honest description: a CARICATURE, not a
model. A neutral 76-point template laid out by hand in box-normalised coordinates,
with the three head angles applied as the simplest transform that moves each feature
in the right direction. It is exactly enough to prove that a landmark channel is
wired to the right name, that it moves when the head moves, and that the
box-relative composition in `tools/td_add_coords.py` puts a mouth where a mouth
belongs. It is emphatically NOT enough to tune a threshold on, and any number
measured against it is a number about this file.

TWO THINGS IT DELIBERATELY DOES NOT REPRODUCE:

  * **The region OVERLAP.** Vision's 12 regions share 11 points across 12 pairs -
    measured, DESIGN.md 2.12 - so a real `f0_nose_crest_00_x` and a real
    `f0_median_line_05_x` can be the same coordinate. Which indices pair up was not
    recorded in a usable form, and inventing it would be a guess wearing a
    measurement's clothes. Each region here is generated independently. A consumer
    that relies on two regions agreeing cannot be tested against this stream.
  * **Perspective.** Yaw compresses x and shifts it; pitch does the same to y; roll
    rotates in BOX space rather than image space, so on a non-square box a rolled
    face shears slightly. All three are monotonic in the right direction, which is
    the property a wiring test needs.

WHY IT IS WORTH HAVING AT ALL. The alternative for exercising 348 landmark channels
is a camera and a person in frame, and almost nothing here needs one. It also
closed a real gap: the face landmark coordinate transform could be verified for the
BOX with landmarks at zero, and the multiply term - `point * bbox_w` - is only
exercised by a non-zero point.

Thread: pure functions over immutable values. Safe anywhere.
Ref: DESIGN.md 6.4, 2.12, appletd/face_types.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from appletd.face_types import MAX_FACES, Face, FaceFrame, order_faces
from appletd.types import Confidence, NormX, NormY

# A head's width as a fraction of the frame at `size` = 1.0, and its height. Taller
# than wide, as a head is - a square box would make any aspect-sensitive consumer
# look correct here and wrong on a real face.
_WIDTH: Final = 0.22
_HEIGHT: Final = 0.30

# How far a fully-turned head shifts its features across the box, and how far a
# fully-pitched one shifts them up or down. Fractions of the box. GUESSED, and they
# only have to be monotonic: what they are for is proving that `f0_nose_00_x` moves
# when the head turns, not predicting where a real nose goes.
_YAW_SHIFT: Final = 0.28
_PITCH_SHIFT: Final = 0.22


@dataclass(frozen=True)
class FacePose:
    """The parameters of one synthetic face.

    center_x, center_y  where the head's CENTRE sits, 0..1, origin bottom-left
    size                scale on the bounding box; 1.0 is a head at conversational
                        distance from a 1280x720 camera
    roll, yaw, pitch    DEGREES, as the channels carry them - Vision's radians are
                        converted in face.py, and a synthetic stream that emitted
                        radians would be the one place that conversion was not
                        exercised end to end
    quality             faceCaptureQuality, so a test can drive a face below a gate
    expression          0.0 mouth closed, 1.0 mouth fully open. The one non-rigid
                        parameter, because a closed and an open mouth is the
                        cheapest way to see whether the inner-lip channels are wired
                        to the inner lips
    """

    center_x: float = 0.5
    center_y: float = 0.55
    size: float = 1.0
    roll: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    quality: float = 0.8
    expression: float = 0.0


# ---------------------------------------------------------------------------
# The neutral template
#
# Box-normalised: x 0..1 across the bounding box, y 0..1 up it, origin BOTTOM LEFT,
# exactly as `VNFaceLandmarkRegion2D.normalizedPoints` reports them (DESIGN.md 7).
#
# LEFT IS AT LARGER X, and that is the whole reason this comment exists. These are
# the SUBJECT's left and right, and a person facing an unmirrored camera has their
# left side on the right of the image. The same trap as hands, where a first attempt
# put a person's left hand at smaller x and looked entirely plausible.
# ---------------------------------------------------------------------------
def _arc(cx: float, cy: float, rx: float, ry: float, count: int,
         start_deg: float, end_deg: float) -> tuple[tuple[float, float], ...]:
    """`count` points along an elliptical arc, inclusive of both ends.

    The one shape every region here is made of. `count` == 1 returns the arc's
    START, not its midpoint, so a single-point region (a pupil) is placed by its
    own centre rather than by an averaging accident.
    """
    if count <= 1:
        angle = math.radians(start_deg)
        return ((cx + rx * math.cos(angle), cy + ry * math.sin(angle)),)
    step = (end_deg - start_deg) / (count - 1)
    return tuple(
        (cx + rx * math.cos(math.radians(start_deg + step * index)),
         cy + ry * math.sin(math.radians(start_deg + step * index)))
        for index in range(count))


def _line(x0: float, y0: float, x1: float, y1: float,
          count: int) -> tuple[tuple[float, float], ...]:
    """`count` evenly spaced points from one end to the other, inclusive."""
    if count <= 1:
        return ((x0, y0),)
    return tuple((x0 + (x1 - x0) * index / (count - 1),
                  y0 + (y1 - y0) * index / (count - 1))
                 for index in range(count))


# Feature centres, so a change to "where the eyes are" is one number rather than
# four. All box-normalised.
_EYE_Y: Final = 0.64
_EYE_DX: Final = 0.22          # from the centre line to an eye's centre
_MOUTH_Y: Final = 0.20
_NOSE_TIP_Y: Final = 0.38


def _template(expression: float) -> dict[str, tuple[tuple[float, float], ...]]:
    """The neutral constellation, with the mouth opened by `expression`.

    Contract: keys are exactly `FACE_REGION_NAMES`; each value has at least that
              region's measured `point_count`, so `face_channel_values` never has to
              pad. Coordinates are box-normalised, origin bottom left.
    Why a function rather than a constant: the mouth opens. Everything else is
              rigid, and rebuilding the rigid part per frame costs a few hundred
              nanoseconds against the clarity of one code path.
    """
    # A mouth opens DOWNWARD, mostly - the jaw drops - so the lower lip travels
    # about three times as far as the upper. Guessed, and the direction is the part
    # that matters: an open mouth whose top lip rose would read as a wrong sign.
    open_down = 0.10 * expression
    open_up = 0.03 * expression
    return {
        # The jaw and cheek outline: one temple, round the chin, up to the other.
        # 180 -> 0 degrees over the lower half of an ellipse.
        #
        # INSET from the box edge on purpose. At rx 0.50 the first point landed at
        # exactly x = 0.0 and the lowest at exactly y = 0.0, which makes a landmark
        # channel indistinguishable from an ABSENT one - and "is this face slot
        # empty" is a question tests ask constantly. Real points do not sit on the
        # boundary to the bit either.
        "face_contour": _arc(0.5, 0.76, 0.48, 0.74, 17, 180.0, 360.0),
        # Forehead to chin down the centre, inset for the same reason.
        "median_line": _line(0.5, 0.99, 0.5, 0.02, 10),
        # The subject's RIGHT is at SMALLER x. Both brows arc upward over the eye.
        "right_eyebrow": _arc(0.5 - _EYE_DX, _EYE_Y + 0.11, 0.13, 0.05, 6,
                              200.0, 340.0),
        "left_eyebrow": _arc(0.5 + _EYE_DX, _EYE_Y + 0.11, 0.13, 0.05, 6,
                             340.0, 200.0),
        # An eye as a closed hexagon, so consecutive points trace the lid.
        "right_eye": _arc(0.5 - _EYE_DX, _EYE_Y, 0.11, 0.05, 6, 0.0, 300.0),
        "left_eye": _arc(0.5 + _EYE_DX, _EYE_Y, 0.11, 0.05, 6, 0.0, 300.0),
        "right_pupil": ((0.5 - _EYE_DX, _EYE_Y),),
        "left_pupil": ((0.5 + _EYE_DX, _EYE_Y),),
        # The nose's base and nostrils, spread either side of the tip.
        "nose": _arc(0.5, _NOSE_TIP_Y, 0.11, 0.06, 8, 200.0, 340.0),
        # The bridge, from between the eyes down to the tip.
        "nose_crest": _line(0.5, _EYE_Y - 0.03, 0.5, _NOSE_TIP_Y, 6),
        "outer_lips": tuple(
            (x, y - open_down if y < _MOUTH_Y else y + open_up)
            for x, y in _arc(0.5, _MOUTH_Y, 0.17, 0.075, 14, 0.0, 360.0 * 13 / 14)),
        "inner_lips": tuple(
            (x, y - open_down if y < _MOUTH_Y else y + open_up)
            for x, y in _arc(0.5, _MOUTH_Y, 0.11, 0.035, 6, 0.0, 300.0)),
    }


def _oriented(points: tuple[tuple[float, float], ...], roll: float, yaw: float,
              pitch: float) -> tuple[tuple[float, float], ...]:
    """Apply the three head angles to one region's points. Pure.

    Contract: input and output are both box-normalised and CLAMPED to 0..1, because
              Vision's `normalizedPoints` sit inside the bounding box - a consumer
              tuned against 1.4 would be tuned against something that cannot happen.
              The same reasoning clamps the box itself in `synthetic_face`.
    Why this shape: yaw and pitch compress one axis and shift it, which is what
              turning away from a camera does to first order; roll rotates. All
              three are applied about the box's centre, and in BOX space rather than
              image space - so a rolled face on a non-square box shears a little.
              Stated in the module docstring rather than corrected, because
              correcting it would need the box aspect and would still not be a
              perspective model.
    """
    yaw_rad, pitch_rad, roll_rad = (math.radians(yaw), math.radians(pitch),
                                    math.radians(roll))
    # cos() for the foreshortening, sin() for the shift: a head turned 90 degrees
    # has zero horizontal extent and its features piled at one edge.
    x_scale, x_shift = math.cos(yaw_rad), math.sin(yaw_rad) * _YAW_SHIFT
    y_scale, y_shift = math.cos(pitch_rad), math.sin(pitch_rad) * _PITCH_SHIFT
    cos_roll, sin_roll = math.cos(roll_rad), math.sin(roll_rad)

    out = []
    for x, y in points:
        # About the centre, so a turn does not also translate the whole face.
        dx, dy = (x - 0.5) * x_scale + x_shift, (y - 0.5) * y_scale + y_shift
        rx = dx * cos_roll - dy * sin_roll
        ry = dx * sin_roll + dy * cos_roll
        out.append((min(1.0, max(0.0, rx + 0.5)), min(1.0, max(0.0, ry + 0.5))))
    return tuple(out)


def synthetic_face(pose: FacePose) -> Face:
    """One `FacePose` -> one immutable `Face`, landmarks included. Pure.

    Contract: the bounding box is normalised with the origin BOTTOM LEFT and
              `bbox_y` is its BOTTOM edge, exactly as `VNFaceObservation` reports it
              (DESIGN.md 7). Clamped into frame, because Vision's box is always
              inside the image and a consumer tuned against 1.4 would be tuned
              against something that cannot happen.
              `landmarks` is box-normalised, NOT image-normalised - the same space
              Vision uses, and the reason `tools/td_add_coords.py` has to compose
              through the box before it can put a landmark on screen.
    """
    width = _WIDTH * pose.size
    height = _HEIGHT * pose.size
    left = min(1.0 - width, max(0.0, pose.center_x - width * 0.5))
    bottom = min(1.0 - height, max(0.0, pose.center_y - height * 0.5))
    template = _template(pose.expression)
    return Face(
        # 1.0: what a detected face is expected to report. UNMEASURED for faces
        # (DESIGN.md 2.12), which is why `quality` is the one to gate on and the
        # one this dataclass lets a caller vary.
        confidence=Confidence(1.0),
        quality=Confidence(pose.quality),
        roll_deg=pose.roll,
        yaw_deg=pose.yaw,
        pitch_deg=pose.pitch,
        bbox_x=NormX(left),
        bbox_y=NormY(bottom),
        bbox_w=width,
        bbox_h=height,
        # A tuple of pairs rather than a dict, because `Face` is frozen and has to
        # be safe to read from another thread - the same rule `LandmarkFrame`
        # follows. `face_channel_values` rebuilds the mapping.
        landmarks=tuple(
            (name, _oriented(points, pose.roll, pose.yaw, pose.pitch))
            for name, points in template.items()),
        found=True,
    )


def synthetic_face_frame(poses: tuple[FacePose, ...], seq: int = 1,
                         captured_at: float = 0.0, width: int = 1280,
                         height: int = 720) -> FaceFrame:
    """A whole frame from up to MAX_FACES poses, ordered as the engine orders them.

    Runs the real `order_faces`, so this stream has the same left-to-right slot rule
    the live one has - the same reason `tools/send_synthetic.py` runs the real
    `SlotAssigner` rather than publishing its own order.
    """
    return FaceFrame(
        seq=seq, captured_at=captured_at, width=width, height=height,
        faces=order_faces([synthetic_face(pose) for pose in poses[:MAX_FACES]]),
    )
