"""Synthetic bodies: a `PoseFrame` from a handful of numbers. No camera, no Vision.

The pose counterpart of `synth.py`, and it exists for the same reason: the camera
on this machine usually has another owner, a fixture with a person in it cannot be
committed (STANDARDS.md 3), and the TouchDesigner side still has to be verifiable
end to end. So this generates a plausible standing figure that can be driven
along a path, and `tools/send_synthetic_pose.py` puts it on the wire in exactly
the bytes the sidecar would send.

WHAT IT IS FOR: proving the plumbing. Channel names, ports, ordering, the
left-to-right slot rule, and whatever a project does with the numbers. What it is
NOT for: validating anything about Vision. These joints are proportions from a
diagram, not measurements of a person, so a threshold tuned against them is a
guess - which is exactly the mistake the trigger thresholds recorded in
docs/ATTRIBUTES.md were three times too loose for.

THE MIRROR, and it is the one thing in here that is easy to get silently wrong.
Vision reports a person's OWN left and right. Someone facing the camera has their
anatomical left toward the image's +x side, so `left_shoulder` sits at a LARGER x
than `right_shoulder`. Getting this backwards produces a skeleton that looks
completely correct and is laterally flipped - the same defect the hand synthesiser
shipped with until it was caught by eye (docs/JOURNAL.md), where both hands' thumbs
pointed the same way because a left hand was never mirrored.

Thread: pure functions over immutable values. Safe anywhere.
Ref: DESIGN.md 6.4, 7 (coordinates); visionhands/pose_types.py; visionhands/synth.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from visionhands.pose_types import (
    BODY_JOINT_NAMES,
    MAX_BODIES,
    Body,
    PoseFrame,
    order_bodies,
)
from visionhands.types import Confidence, Joint, NormX, NormY

# Heights up the frame, as fractions of the frame, for a person standing fully in
# shot. Proportions from a figure diagram - NOT measured from anybody - scaled by
# `BodyPose.height` about the hips.
_Y: Final[dict[str, float]] = {
    "ankle": 0.06,
    "knee": 0.26,
    "hip": 0.46,
    # 0.68, not 0.72. MEASURED in TouchDesigner: at `wave` = 1.0 the wrist
    # reached y = 1.012 and was CLAMPED to exactly 1.0, so a full raise sat
    # pinned to the top of frame - which is not a position Vision would report
    # and would teach any consumer the wrong thing about the range. The whole arm
    # (0.16 + 0.14, less the elbow bend) now fits below the top with ~0.03 spare.
    "shoulder": 0.68,
    "neck": 0.74,
    "nose": 0.85,
    "eye": 0.87,
    "ear": 0.86,
}

# Half-widths either side of the centre line, same units. `left_` is +x for a
# person facing the camera - see THE MIRROR above.
_HALF: Final[dict[str, float]] = {
    "hip": 0.045,
    "shoulder": 0.075,
    "ear": 0.035,
    "eye": 0.018,
}

# Arm segment lengths, in the same fractions. Upper arm shoulder-to-elbow,
# forearm elbow-to-wrist, the forearm slightly shorter as it is on a person.
_UPPER_ARM: Final = 0.16
_FOREARM: Final = 0.14

# Where the hips sit when `height` is 1.0. The scale pivots here rather than at
# the feet, so a shorter figure (someone further away) shrinks toward its middle
# instead of sinking through the floor.
_PIVOT_Y: Final = _Y["hip"]


@dataclass(frozen=True)
class BodyPose:
    """The parameters of one synthetic person.

    center_x  where the centre line sits, 0..1 across the frame
    height    overall scale about the hips; 1.0 fills the frame vertically
    wave      0 = both arms hanging down, 1 = the person's RIGHT arm straight up.
              Anything between is the arm swinging through, which is what makes a
              downstream velocity or angle channel have something to read.
    conf      the confidence written on every joint, so a test can drive a body
              below a gate as easily as above one.
    """

    center_x: float = 0.5
    height: float = 1.0
    wave: float = 0.0
    conf: float = 0.9


def _place(x: float, y: float, conf: float, height: float) -> Joint:
    """One joint, scaled about the hips and clamped into frame.

    Clamped rather than allowed off-frame: Vision returns normalised points inside
    the image, so a synthetic stream that emits 1.4 would be exercising a case the
    real one cannot produce - and every consumer would be tuned against it.
    """
    scaled_y = _PIVOT_Y + (y - _PIVOT_Y) * height
    return Joint(NormX(min(1.0, max(0.0, x))),
                 NormY(min(1.0, max(0.0, scaled_y))),
                 Confidence(conf))


def synthetic_body(pose: BodyPose) -> Body:
    """One `BodyPose` -> one immutable `Body`, all 19 joints. Pure.

    Contract: joints are in BODY_JOINTS order, normalised, origin BOTTOM LEFT, y
              up - the same frame of reference Vision uses and TouchDesigner
              wants (DESIGN.md 7). Nothing here flips y.
    """
    cx, h, conf = pose.center_x, pose.height, pose.conf
    # Every half-width scales with the figure too, or a distant person would keep
    # shoulders as wide as a near one.
    half = {name: value * h for name, value in _HALF.items()}

    # The arm. `wave` sweeps the person's RIGHT arm from straight down (-90 deg,
    # measuring counter-clockwise from +x as every angle in this system does) to
    # straight up (+90). The left arm hangs.
    angle_deg = -90.0 + 180.0 * min(1.0, max(0.0, pose.wave))
    angle = math.radians(angle_deg)

    right_shoulder = (cx - half["shoulder"], _Y["shoulder"])
    left_shoulder = (cx + half["shoulder"], _Y["shoulder"])
    # The waving arm bends at the elbow: the forearm continues the upper arm's
    # direction, rotated a little, so the limb reads as jointed rather than as one
    # straight stick.
    right_elbow = (right_shoulder[0] + _UPPER_ARM * h * math.cos(angle),
                   right_shoulder[1] + _UPPER_ARM * h * math.sin(angle))
    forearm = angle + math.radians(20.0)
    right_wrist = (right_elbow[0] + _FOREARM * h * math.cos(forearm),
                   right_elbow[1] + _FOREARM * h * math.sin(forearm))
    left_elbow = (left_shoulder[0], left_shoulder[1] - _UPPER_ARM * h)
    left_wrist = (left_elbow[0], left_elbow[1] - _FOREARM * h)

    positions: dict[str, tuple[float, float]] = {
        "nose": (cx, _Y["nose"]),
        # `left_` at LARGER x: a person facing the camera has their own left on
        # the image's right (see THE MIRROR in the module docstring).
        "left_eye": (cx + half["eye"], _Y["eye"]),
        "right_eye": (cx - half["eye"], _Y["eye"]),
        "left_ear": (cx + half["ear"], _Y["ear"]),
        "right_ear": (cx - half["ear"], _Y["ear"]),
        "neck": (cx, _Y["neck"]),
        "left_shoulder": left_shoulder,
        "right_shoulder": right_shoulder,
        "left_elbow": left_elbow,
        "right_elbow": right_elbow,
        "left_wrist": left_wrist,
        "right_wrist": right_wrist,
        "root": (cx, _Y["hip"]),
        "left_hip": (cx + half["hip"], _Y["hip"]),
        "right_hip": (cx - half["hip"], _Y["hip"]),
        "left_knee": (cx + half["hip"], _Y["knee"]),
        "right_knee": (cx - half["hip"], _Y["knee"]),
        "left_ankle": (cx + half["hip"], _Y["ankle"]),
        "right_ankle": (cx - half["hip"], _Y["ankle"]),
    }

    joints = tuple(_place(positions[name][0], positions[name][1], conf, h)
                   for name in BODY_JOINT_NAMES)
    return Body(
        # 1.0, matching what a real observation is EXPECTED to report - hands'
        # equivalent is a measured constant 1.0 and the body one is unmeasured
        # (DESIGN.md 2.12). If it turns out to vary, this is a place that has to
        # change with it.
        confidence=Confidence(1.0),
        conf_median=Confidence(conf),
        joints=joints,
        found=True,
    )


def synthetic_pose_frame(poses: tuple[BodyPose, ...], seq: int = 1,
                         captured_at: float = 0.0, width: int = 1280,
                         height: int = 720) -> PoseFrame:
    """A whole frame from up to MAX_BODIES poses, ordered as the engine orders them.

    Contract: passes the bodies through `order_bodies`, so this stream has the
              same left-to-right slot rule the live one has. A synthesiser that
              published its own order would be the one place slot ordering could
              not be exercised end to end - the same reason
              `tools/send_synthetic.py` runs the real `SlotAssigner`.
    """
    return PoseFrame(
        seq=seq, captured_at=captured_at, width=width, height=height,
        bodies=order_bodies([synthetic_body(pose) for pose in poses[:MAX_BODIES]]),
    )
