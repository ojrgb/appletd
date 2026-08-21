"""Which channels get smoothed, which get transformed, and with what rule.

Both TouchDesigner builders read this module instead of pattern-matching names, so
what is tested here is what stops them dropping channels or transforming the wrong
ones. Three properties carry the weight:

  * **the filter split PARTITIONS every stream.** The two halves are merged back
    together inside the filter group, so a channel in neither leaves the COMP's
    output entirely - measured, with no error anywhere, when four `sc_*` channels
    were added to a port and matched no list (DESIGN.md 2.11).
  * **an extent gets no offset.** A box is 0.2 wide wherever it sits; subtracting
    0.5 from a width gives a negative size that draws as nothing.
  * **face landmark points are NOT positions.** They are normalised to the face's
    bounding box rather than to the image, so transforming them with the image rules
    would put every facial feature in one corner of the frame.

Ref: visionhands/spaces.py, DESIGN.md 7, 6.4.
"""

from __future__ import annotations

import pytest

from visionhands.face_types import FACE_REGIONS, face_channel_names
from visionhands.pose_types import pose_channel_names
from visionhands.spaces import (
    ROLE_ANGLE,
    ROLE_BOX_RELATIVE,
    ROLE_EXTENT_X,
    ROLE_EXTENT_Y,
    ROLE_POSITION_X,
    ROLE_POSITION_Y,
    ROLE_SCALAR,
    SMOOTHED_ROLES,
    TRANSFORMED_ROLES,
    channel_roles,
    names_by_role,
    passthrough_names,
    smoothed_names,
    transform_branches,
)
from visionhands.streams import STREAM_NAMES, status_channel_names
from visionhands.types import channel_names


# ---------------------------------------------------------------------------
# The partition, which is the property both builders depend on
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stream", STREAM_NAMES)
def test_the_filter_split_partitions_the_stream(stream: str) -> None:
    smoothed, passthrough = set(smoothed_names(stream)), set(passthrough_names(stream))
    assert not smoothed & passthrough
    assert smoothed | passthrough == set(channel_roles(stream))


@pytest.mark.parametrize("stream", STREAM_NAMES)
def test_every_contract_channel_is_classified(stream: str) -> None:
    """A channel with no role is a channel a builder cannot place, and an unplaced
    channel does not error - it disappears."""
    expected = {"hands": set(channel_names()) | set(status_channel_names()),
                "pose": set(pose_channel_names()),
                "face": set(face_channel_names())}[stream]
    assert set(channel_roles(stream)) == expected


def test_the_hands_stream_includes_the_sidecar_status_channels() -> None:
    """They arrive on the hands port, so the hands filter has to know about them.
    This is the exact omission that dropped four channels silently."""
    roles = channel_roles("hands")
    for name in status_channel_names():
        assert roles[name] == ROLE_SCALAR


def test_an_unknown_stream_raises() -> None:
    """Rather than returning an empty map, which would build a network that
    transforms nothing and reports success."""
    with pytest.raises(ValueError, match="unknown stream"):
        channel_roles("elbows")


# ---------------------------------------------------------------------------
# Roles: hands and pose
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stream", ["hands", "pose"])
def test_joint_coordinates_are_positions_and_confidences_are_not(stream: str) -> None:
    roles = channel_roles(stream)
    prefix = "h0_" if stream == "hands" else "p0_"
    joint = "wrist" if stream == "hands" else "nose"
    assert roles["%s%s_x" % (prefix, joint)] == ROLE_POSITION_X
    assert roles["%s%s_y" % (prefix, joint)] == ROLE_POSITION_Y
    assert roles["%s%s_conf" % (prefix, joint)] == ROLE_SCALAR


def test_counters_ages_and_flags_are_left_alone() -> None:
    """Smoothing a counter or an age is meaningless, and smoothing a flag makes a
    boolean take values between its two states."""
    roles = channel_roles("hands")
    for name in ("n_hands", "seq", "age_ms", "h0_found", "h0_chirality",
                 "h0_conf_median"):
        assert roles[name] == ROLE_SCALAR
        assert name in passthrough_names("hands")


# ---------------------------------------------------------------------------
# Roles: face, where the distinctions actually bite
# ---------------------------------------------------------------------------
def test_the_box_is_two_positions_and_two_extents() -> None:
    roles = channel_roles("face")
    assert roles["f0_bbox_x"] == ROLE_POSITION_X
    assert roles["f0_bbox_y"] == ROLE_POSITION_Y
    assert roles["f0_bbox_w"] == ROLE_EXTENT_X
    assert roles["f0_bbox_h"] == ROLE_EXTENT_Y


def test_head_pose_is_an_angle_smoothed_but_never_transformed() -> None:
    """There is no yaw in pixels. It still wants smoothing, like any other
    continuous physical quantity."""
    roles = channel_roles("face")
    for name in ("f0_roll", "f0_yaw", "f0_pitch"):
        assert roles[name] == ROLE_ANGLE
        assert name in smoothed_names("face")
    assert ROLE_ANGLE in SMOOTHED_ROLES
    assert ROLE_ANGLE not in TRANSFORMED_ROLES


def test_confidence_and_quality_are_scalars() -> None:
    """A smoothed confidence makes every gate reading it lag behind the thing it is
    gating - which is the opposite of what a gate is for."""
    roles = channel_roles("face")
    assert roles["f0_score"] == ROLE_SCALAR
    assert roles["f0_quality"] == ROLE_SCALAR


def test_a_face_landmark_point_is_box_relative_not_a_position() -> None:
    """The trap this module exists to record. `normalizedPoints` are normalised to
    the FACE's bounding box (DESIGN.md 2.12), so image-space rules would put every
    feature in one corner. Simulated, because the points are not published yet -
    which is exactly when the rule needs to already be right.
    """
    from visionhands.spaces import _role_of

    for region in FACE_REGIONS:
        name = "f0_%s_03_x" % region.name
        assert _role_of("face", name) == ROLE_BOX_RELATIVE
    assert ROLE_BOX_RELATIVE in SMOOTHED_ROLES        # still worth smoothing
    assert ROLE_BOX_RELATIVE not in TRANSFORMED_ROLES  # never transformed


# ---------------------------------------------------------------------------
# The branches the coordinate builder gets
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stream", STREAM_NAMES)
def test_a_position_branch_is_offset_and_an_extent_branch_is_not(stream: str) -> None:
    """The one arithmetic distinction between a point and a size."""
    for label, suffix, _names, offset in transform_branches(stream):
        if suffix in ("_tx", "_ty"):
            assert offset == -0.5, label
        else:
            # Pixel branches never offset either: pixel space starts at the corner.
            assert offset == 0.0, label


def test_hands_and_pose_get_four_branches_and_the_face_gets_eight() -> None:
    """The face is the only stream with extents, so it is the only one with eight."""
    assert len(transform_branches("hands")) == 4
    assert len(transform_branches("pose")) == 4
    assert len(transform_branches("face")) == 8


def test_every_branch_channel_actually_belongs_to_the_stream() -> None:
    """A branch naming a channel the stream does not carry would select nothing and
    silently produce an empty space."""
    for stream in STREAM_NAMES:
        known = set(channel_roles(stream))
        for _label, _suffix, names, _offset in transform_branches(stream):
            assert names and set(names) <= known


def test_nothing_box_relative_or_angular_reaches_a_branch() -> None:
    branches = {name for _l, _s, names, _o in transform_branches("face")
                for name in names}
    grouped = names_by_role("face")
    assert not branches & set(grouped[ROLE_ANGLE])
    assert not branches & set(grouped[ROLE_BOX_RELATIVE])


def test_transform_branches_is_empty_for_a_stream_with_nothing_to_transform() -> None:
    """Not an error state: a stream of pure scalars gets an empty coords group
    rather than a missing one. Simulated with a stream whose channels are all
    scalars, since every real one has positions."""
    from visionhands import spaces

    original = spaces.channel_roles
    try:
        # Monkeypatched by hand rather than with the fixture, because the target is
        # the function `transform_branches` looks up at call time - and restoring it
        # matters, since every other test in this file reads the real one.
        spaces.channel_roles = lambda stream: {"a": ROLE_SCALAR}
        assert transform_branches("hands") == []
    finally:
        spaces.channel_roles = original
