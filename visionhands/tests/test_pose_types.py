"""The body-pose contract: the joint table, the channel list, and the ordering.

What is worth testing here is what `verify_body_joint_table()` CANNOT check
against the live framework. That function proves every code we hardcode exists
and means what we say it does; it cannot notice a REORDERED table, because each
row stays internally consistent when two rows are swapped. Order is what
`p0_nose_x` means, so order is pinned here.

The other half is the left-to-right ordering, which is the pose answer to the
problem chirality solves for hands (DESIGN.md 6.3 / 6.4): Vision's observation
order is not stable, so without a rule `p0` swaps between two people frame to
frame and every consumer reads a blend of them.

No camera, no pyobjc: everything below is a pure function of a `Body`.

Ref: DESIGN.md 6.4, 2.12; visionhands/pose_types.py.
"""

from __future__ import annotations

import dataclasses

import pytest

from visionhands.pose_types import (
    BLANK_BODY,
    BODY_JOINT_INDEX_BY_CODE,
    BODY_JOINT_NAMES,
    BODY_JOINTS,
    MAX_BODIES,
    N_BODY_JOINTS,
    N_POSE_CHANNELS,
    SORT_CONF_FLOOR,
    Body,
    blank_pose_frame,
    order_bodies,
    pose_channel_names,
    pose_channel_values,
)
from visionhands.types import Confidence, Joint, NormX, NormY


def _body(x: float, conf: float = 1.0, *, joint: str = "root") -> Body:
    """A body located at `x` by one named joint, with everything else at zero."""
    joints = [Joint(NormX(0.0), NormY(0.0), Confidence(0.0))
              for _ in range(N_BODY_JOINTS)]
    joints[BODY_JOINT_NAMES.index(joint)] = Joint(
        NormX(x), NormY(0.5), Confidence(conf))
    return Body(confidence=Confidence(1.0), conf_median=Confidence(conf),
                joints=tuple(joints), found=True)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------
def test_the_joint_order_is_exactly_this() -> None:
    """The one property the framework check cannot cover. This order IS the
    channel order, so changing it silently rewires every downstream reference in
    a TouchDesigner project."""
    assert BODY_JOINT_NAMES == (
        "nose", "left_eye", "right_eye", "left_ear", "right_ear", "neck",
        "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
        "left_wrist", "right_wrist", "root", "left_hip", "right_hip",
        "left_knee", "right_knee", "left_ankle", "right_ankle",
    )


def test_the_arkit_names_are_mapped_to_the_right_anatomy() -> None:
    """The trap in DESIGN.md 2.12, pinned. Vision's constant NAMES and their
    VALUES disagree: an elbow's key is a forearm and a wrist's is a hand. A table
    built from the values maps them one joint out and looks plausible on screen.
    """
    codes = {joint.name: joint.code for joint in BODY_JOINTS}
    assert codes["left_elbow"] == "left_forearm_joint"
    assert codes["left_wrist"] == "left_hand_joint"
    assert codes["nose"] == "head_joint"
    assert codes["left_hip"] == "left_upLeg_joint"
    assert codes["left_knee"] == "left_leg_joint"


def test_the_code_index_agrees_with_the_table() -> None:
    """The hot path looks joints up by code; a wrong index puts one person's
    elbow where their knee should be."""
    for index, joint in enumerate(BODY_JOINTS):
        assert BODY_JOINT_INDEX_BY_CODE[joint.code] == index


def test_the_code_index_cannot_be_mutated() -> None:
    """It is shared across threads, and `Final` stops rebinding rather than
    mutation - so the promise is made true by the read-only view, not intended."""
    with pytest.raises(TypeError):
        BODY_JOINT_INDEX_BY_CODE["root"] = 99       # type: ignore[index]


# ---------------------------------------------------------------------------
# The channel list
# ---------------------------------------------------------------------------
def test_the_channel_count_is_123() -> None:
    assert N_POSE_CHANNELS == 123 == len(pose_channel_names())


def test_names_and_values_stay_the_same_length() -> None:
    """The misalignment this prevents would put one joint's y into another
    joint's x channel and still look plausible on screen."""
    assert len(pose_channel_values(blank_pose_frame(), 0.0, 0)) == \
        len(pose_channel_names())


def test_pose_seq_is_at_index_one() -> None:
    """The sidecar wraps `values[1]` for float32 exactness, so it needs this to be
    true rather than to believe it is (DESIGN.md 2.9)."""
    assert pose_channel_names()[1] == "pose_seq"


def test_the_channel_list_is_the_same_whatever_is_in_frame() -> None:
    """A channel list that varies breaks every downstream reference the moment it
    shrinks, and it breaks it silently (DESIGN.md 6.2)."""
    empty = blank_pose_frame()
    crowded = dataclasses.replace(
        blank_pose_frame(), bodies=order_bodies([_body(0.2), _body(0.8)]))
    assert len(pose_channel_values(empty, 0.0, 0)) == \
        len(pose_channel_values(crowded, 0.0, 2))


def test_an_absent_person_publishes_zeros_not_nothing() -> None:
    reading = dict(zip(pose_channel_names(),
                       pose_channel_values(blank_pose_frame(), 0.0, 0), strict=True))
    assert reading["p0_found"] == 0.0
    assert reading["p1_right_ankle_conf"] == 0.0
    # 0.0, not the 1.0 a real observation might report: an empty slot must not
    # look like a confident detection.
    assert reading["p0_score"] == 0.0


def test_values_are_where_the_names_say_they_are() -> None:
    """One end-to-end alignment check, with a body whose joints are all distinct,
    so a shifted index cannot pass."""
    joints = tuple(Joint(NormX(i / 100.0), NormY(i / 200.0), Confidence(i / 20.0))
                   for i in range(N_BODY_JOINTS))
    body = Body(confidence=Confidence(0.9), conf_median=Confidence(0.5),
                joints=joints, found=True)
    frame = dataclasses.replace(blank_pose_frame(), bodies=(body, BLANK_BODY))
    reading = dict(zip(pose_channel_names(),
                       pose_channel_values(frame, 12.0, 1), strict=True))
    assert reading["pose_n_bodies"] == 1.0
    assert reading["pose_age_ms"] == 12.0
    assert reading["p0_score"] == pytest.approx(0.9)
    assert reading["p0_nose_x"] == pytest.approx(0.0)
    assert reading["p0_root_x"] == pytest.approx(12 / 100.0)
    assert reading["p0_right_ankle_conf"] == pytest.approx(18 / 20.0)


# ---------------------------------------------------------------------------
# The ordering - the pose answer to unstable observation order
# ---------------------------------------------------------------------------
def test_bodies_are_ordered_left_to_right() -> None:
    """p0 is the leftmost person, whatever order Vision listed them in."""
    right, left = _body(0.8), _body(0.2)
    ordered = order_bodies([right, left])
    assert ordered[0] is left
    assert ordered[1] is right


def test_the_order_does_not_depend_on_the_input_order() -> None:
    """The property that matters: the same two people in either order produce the
    same slots. Vision's order is not stable between frames."""
    a, b = _body(0.3), _body(0.7)
    assert order_bodies([a, b]) == order_bodies([b, a])


def test_a_body_with_no_confident_reference_joint_sorts_last() -> None:
    """An occluded root reads x = 0.0, which is the far LEFT of frame - so a plain
    x sort would put a person we cannot locate in slot 0 and push a perfectly
    visible one to slot 1."""
    located = _body(0.9)
    lost = _body(0.1, conf=SORT_CONF_FLOOR / 2)
    assert order_bodies([lost, located])[0] is located


def test_the_neck_is_the_fallback_when_the_root_is_not_visible() -> None:
    """The common case for a seated installation: hips behind a desk, neck in
    plain view."""
    seated = _body(0.9, joint="neck")
    standing = _body(0.4)
    assert order_bodies([seated, standing]) == (standing, seated)


def test_the_slots_are_padded_and_capped() -> None:
    """Always exactly MAX_BODIES, which is what makes the fixed channel list
    unconditionally true rather than something the sender has to remember."""
    assert len(order_bodies([])) == MAX_BODIES
    assert len(order_bodies([_body(0.1), _body(0.2), _body(0.3)])) == MAX_BODIES


def test_the_two_people_published_are_the_two_LEFTMOST() -> None:
    """A stable rule, rather than 'whichever two Vision happened to list first'.
    Vision has no maximum-person setting (DESIGN.md 2.12), so this choice is
    ours."""
    ordered = order_bodies([_body(0.9), _body(0.1), _body(0.5)])
    assert [body.joints[BODY_JOINT_NAMES.index("root")].x for body in ordered] == \
        [pytest.approx(0.1), pytest.approx(0.5)]


def test_an_unfound_body_never_takes_a_slot_from_a_found_one() -> None:
    assert order_bodies([BLANK_BODY, _body(0.5)])[0].found is True


def test_empty_slots_share_one_frozen_object() -> None:
    """Per-frame allocation on the capture thread, avoided the same way
    BLANK_HAND avoids it."""
    assert order_bodies([])[0] is BLANK_BODY
    assert blank_pose_frame().bodies[1] is BLANK_BODY
