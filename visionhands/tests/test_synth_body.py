"""The synthetic body: is it a plausible person, and is it the right way round?

The mirror is the reason this file exists. `synth.py` shipped with unmirrored left
hands - both thumbs pointing the same way - and nothing caught it until the user
looked at the stream, because a laterally flipped skeleton is completely plausible
on screen. Every property below is one a wrong figure would fail while still
looking fine.

Ref: visionhands/synth_body.py, DESIGN.md 7 (coordinates).
"""

from __future__ import annotations

import pytest

from visionhands.pose_types import BODY_JOINT_NAMES, MAX_BODIES, Body
from visionhands.synth_body import BodyPose, synthetic_body, synthetic_pose_frame


def _at(body: Body, name: str) -> tuple[float, float]:
    joint = body.joints[BODY_JOINT_NAMES.index(name)]
    return (joint.x, joint.y)


# ---------------------------------------------------------------------------
# The mirror - the failure that looks fine
# ---------------------------------------------------------------------------
def test_a_persons_left_is_on_the_images_right() -> None:
    """Vision reports a person's OWN left and right. Facing the camera, their left
    is toward +x. Backwards, this produces a skeleton that is laterally flipped and
    entirely believable - exactly how the hand synthesiser was wrong for a week.
    """
    body = synthetic_body(BodyPose(center_x=0.5))
    for part in ("shoulder", "hip", "knee", "ankle", "ear", "eye"):
        left = _at(body, "left_" + part)[0]
        right = _at(body, "right_" + part)[0]
        assert left > right, part


def test_the_head_is_above_the_feet() -> None:
    """y is UP in this pipeline and nothing flips it (DESIGN.md 7). A flipped
    figure would be upside down and would still draw."""
    body = synthetic_body(BodyPose())
    assert _at(body, "nose")[1] > _at(body, "neck")[1]
    assert _at(body, "neck")[1] > _at(body, "root")[1]
    assert _at(body, "root")[1] > _at(body, "left_knee")[1]
    assert _at(body, "left_knee")[1] > _at(body, "left_ankle")[1]


# ---------------------------------------------------------------------------
# The figure
# ---------------------------------------------------------------------------
def test_every_joint_is_in_frame() -> None:
    """Vision only ever returns normalised points inside the image, so a
    synthetic stream that emitted 1.4 would exercise a case the real one cannot
    produce - and every consumer would be tuned against it."""
    for pose in (BodyPose(center_x=0.0), BodyPose(center_x=1.0),
                 BodyPose(height=1.0, wave=1.0), BodyPose(height=0.2)):
        for joint in synthetic_body(pose).joints:
            assert 0.0 <= joint.x <= 1.0
            assert 0.0 <= joint.y <= 1.0


def test_the_wave_raises_the_wrist_above_the_shoulder() -> None:
    """`wave` is what gives a downstream velocity or angle channel something to
    read, so it has to actually move the limb it claims to."""
    down = synthetic_body(BodyPose(wave=0.0))
    up = synthetic_body(BodyPose(wave=1.0))
    assert _at(down, "right_wrist")[1] < _at(down, "right_shoulder")[1]
    assert _at(up, "right_wrist")[1] > _at(up, "right_shoulder")[1]


def test_a_full_raise_stays_INSIDE_the_frame_rather_than_clamped_to_it() -> None:
    """Stronger than "every joint is in frame", which the clamp guarantees for
    free. MEASURED in TouchDesigner: the first version reached y = 1.012 and was
    clamped to exactly 1.0, so a full wave sat pinned to the top of frame - a
    position Vision would never report, teaching every consumer the wrong range.
    """
    wrist = _at(synthetic_body(BodyPose(wave=1.0)), "right_wrist")
    assert wrist[1] < 1.0
    assert wrist[1] > 0.9              # and it really is a full raise


def test_the_wave_moves_the_right_arm_and_leaves_the_left_alone() -> None:
    """Named in the docstring as the person's RIGHT arm; asserted here so the two
    cannot drift apart."""
    down = synthetic_body(BodyPose(wave=0.0))
    up = synthetic_body(BodyPose(wave=1.0))
    assert _at(down, "left_wrist") == _at(up, "left_wrist")
    assert _at(down, "right_wrist") != _at(up, "right_wrist")


def test_height_scales_about_the_hips() -> None:
    """A shorter figure is someone further away; scaling about the FEET would sink
    them through the floor instead."""
    tall = synthetic_body(BodyPose(height=1.0))
    short = synthetic_body(BodyPose(height=0.5))
    assert _at(short, "root")[1] == pytest.approx(_at(tall, "root")[1])
    assert _at(short, "nose")[1] < _at(tall, "nose")[1]
    assert _at(short, "left_ankle")[1] > _at(tall, "left_ankle")[1]


def test_a_narrower_figure_has_narrower_shoulders() -> None:
    """Width scales with the figure, or a distant person keeps the shoulders of a
    near one - and any consumer measuring across them reads a constant."""
    def width(height: float) -> float:
        body = synthetic_body(BodyPose(height=height))
        return _at(body, "left_shoulder")[0] - _at(body, "right_shoulder")[0]
    assert width(0.5) < width(1.0)


def test_confidence_is_settable_so_a_body_can_be_driven_below_a_gate() -> None:
    body = synthetic_body(BodyPose(conf=0.05))
    assert body.conf_median == pytest.approx(0.05)
    assert all(joint.conf == pytest.approx(0.05) for joint in body.joints)


# ---------------------------------------------------------------------------
# The frame
# ---------------------------------------------------------------------------
def test_a_frame_is_always_max_bodies_wide() -> None:
    """What makes the fixed channel list unconditionally true."""
    for poses in ((), (BodyPose(),), (BodyPose(), BodyPose(center_x=0.8))):
        assert len(synthetic_pose_frame(poses).bodies) == MAX_BODIES


def test_the_frame_applies_the_real_ordering_rule() -> None:
    """The synthesiser runs `order_bodies`, not its own order - otherwise the slot
    rule would be the one thing no end-to-end test could reach, the same reason
    tools/send_synthetic.py runs the real SlotAssigner."""
    frame = synthetic_pose_frame((BodyPose(center_x=0.9), BodyPose(center_x=0.1)))
    assert _at(frame.bodies[0], "root")[0] == pytest.approx(0.1)
    assert _at(frame.bodies[1], "root")[0] == pytest.approx(0.9)


def test_a_third_person_is_dropped_rather_than_appended() -> None:
    frame = synthetic_pose_frame((BodyPose(center_x=0.2), BodyPose(center_x=0.5),
                                  BodyPose(center_x=0.8)))
    assert len(frame.bodies) == MAX_BODIES
    assert sum(1 for body in frame.bodies if body.found) == MAX_BODIES


def test_nobody_in_frame_still_gives_two_blank_slots() -> None:
    frame = synthetic_pose_frame(())
    assert all(not body.found for body in frame.bodies)
