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

Ref: appletd/spaces.py, DESIGN.md 7, 6.4.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

import pytest

from appletd.face_types import FACE_KEYPOINTS, FACE_REGIONS, face_channel_names
from appletd.pose_types import pose_channel_names
from appletd.spaces import (
    DERIVE_CHOP,
    DERIVED_SOURCES,
    ROLE_ANGLE,
    ROLE_BOX_KEYPOINT,
    ROLE_BOX_RELATIVE,
    ROLE_EXTENT_X,
    ROLE_EXTENT_Y,
    ROLE_POSITION_X,
    ROLE_POSITION_Y,
    ROLE_SCALAR,
    SMOOTHED_ROLES,
    TEMPORAL,
    TRANSFORMED_ROLES,
    box_branches,
    box_expressions,
    channel_roles,
    compact_pattern,
    companioned_names,
    derived_branches,
    derived_companioned_names,
    derived_roles,
    keypoint_branches,
    names_by_role,
    passthrough_names,
    scope_pattern,
    smoothed_names,
    transform_branches,
)
from appletd.streams import STREAM_NAMES, status_channel_names
from appletd.types import channel_names


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
    from appletd.spaces import _role_of

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
    for branch in transform_branches(stream):
        if branch.suffix in ("_tx", "_ty"):
            assert branch.offset == -0.5, branch.label
        else:
            # Pixel branches never offset either: pixel space starts at the corner.
            assert branch.offset == 0.0, branch.label


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
        for branch in transform_branches(stream):
            assert branch.names and set(branch.names) <= known


def test_nothing_box_relative_or_angular_reaches_a_branch() -> None:
    branches = {name for branch in transform_branches("face")
                for name in branch.names}
    grouped = names_by_role("face")
    assert not branches & set(grouped[ROLE_ANGLE])
    assert not branches & set(grouped[ROLE_BOX_RELATIVE])


def test_transform_branches_is_empty_for_a_stream_with_nothing_to_transform() -> None:
    """Not an error state: a stream of pure scalars gets an empty coords group
    rather than a missing one. Simulated with a stream whose channels are all
    scalars, since every real one has positions."""
    from appletd import spaces

    original = spaces.channel_roles
    try:
        # Monkeypatched by hand rather than with the fixture, because the target is
        # the function `transform_branches` looks up at call time - and restoring it
        # matters, since every other test in this file reads the real one.
        spaces.channel_roles = lambda stream: {"a": ROLE_SCALAR}
        assert transform_branches("hands") == []
    finally:
        spaces.channel_roles = original


# ---------------------------------------------------------------------------
# The patterns, which are the part a builder trusts and must not have to
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stream", STREAM_NAMES)
def test_every_pattern_selects_exactly_what_it_claims(stream: str) -> None:
    """The whole point of `compact_pattern`. A pattern that selects one channel too
    many transforms something twice; one too few makes a channel VANISH from the
    COMP output with no error anywhere (DESIGN.md 2.11)."""
    universe = list(channel_roles(stream))

    def expand(pattern: str) -> list[str]:
        tokens = pattern.split()
        return [name for name in universe
                if any(fnmatchcase(name, token) for token in tokens)]

    assert expand(scope_pattern(stream)) == smoothed_names(stream)
    for branch in transform_branches(stream):
        assert expand(branch.pattern) == branch.names, branch.label
    for box in box_branches(stream):
        assert expand(box.pattern) == box.names, box.label


def test_a_pattern_that_cannot_partition_falls_back_to_the_literal_names() -> None:
    """The fallback is not decoration: it is what makes a wildcard safe to attempt
    at all. Asked for a set no candidate can express, `compact_pattern` must return
    the names rather than the closest pattern."""
    universe = ["a_x", "b_x", "c_x"]
    assert compact_pattern(["a_x", "b_x"], universe, ["*_x"]) == "a_x b_x"
    # And it takes the pattern when the pattern is exact.
    assert compact_pattern(universe, universe, ["*_x"]) == "*_x"
    # Order matters, not just membership: a branch's Rename maps name to name.
    assert compact_pattern(["b_x", "a_x"], universe, ["*_x"]) == "b_x a_x"


def test_the_face_landmark_pattern_excludes_the_bounding_box() -> None:
    """`f0_*_x` would sweep `f0_bbox_x` in with 87 landmark points, and the box is
    in IMAGE space while the points are in box space - so the box would be composed
    through itself."""
    for box in box_branches("face"):
        assert box.origin not in box.names
        assert box.extent not in box.names
        assert not any(name.endswith("_bbox_x") or name.endswith("_bbox_y")
                       for name in box.names)


def test_hands_and_pose_have_no_box_relative_branches() -> None:
    """A hand joint is already in image space. A branch here would compose it
    through a bounding box that does not exist."""
    assert box_branches("hands") == []
    assert box_branches("pose") == []
    assert len(box_branches("face")) == 8      # 2 faces x 2 axes x 2 spaces


def test_a_box_branch_names_real_box_channels() -> None:
    known = set(channel_roles("face"))
    for box in box_branches("face"):
        assert box.origin in known
        assert box.extent in known
        # The x axis rides on the LEFT edge and the WIDTH; y on the BOTTOM and the
        # HEIGHT. Swapping them is plausible on screen and wrong.
        assert box.origin.endswith("_x" if box.suffix in ("_tx", "_px") else "_y")
        assert box.extent.endswith("_w" if box.suffix in ("_tx", "_px") else "_h")


# ---------------------------------------------------------------------------
# The composed arithmetic, evaluated rather than eyeballed
# ---------------------------------------------------------------------------
def test_box_expressions_compose_through_the_box_and_then_the_space() -> None:
    """Evaluate the two strings the builder puts on a Math CHOP and check the
    number, because this is the arithmetic that has been silently wrong twice.

    A Math CHOP computes `(value + preoff) * gain + postoff` - MEASURED - with
    `preoff` at zero here, so the test applies exactly that.
    """
    box = {"f0_bbox_x": 0.30, "f0_bbox_y": 0.20, "f0_bbox_w": 0.22, "f0_bbox_h": 0.30}

    class _FakeOp:
        def __getitem__(self, name: str) -> float:
            return box[name]

    scope = {"op": lambda _path: _FakeOp()}
    point = 0.75                                   # three quarters across the box
    for branch in box_branches("face"):
        if not branch.label.startswith("f0"):
            continue
        gain_expr, postoff_expr = box_expressions(branch, "2.0")
        # eval on our OWN generated strings, with `op` faked - which is the only
        # way to check a TouchDesigner expression without TouchDesigner.
        gain = eval(gain_expr, dict(scope))
        postoff = eval(postoff_expr, dict(scope))
        got = point * gain + postoff
        origin, extent = box[branch.origin], box[branch.extent]
        assert got == pytest.approx(
            ((origin + point * extent) + branch.offset) * 2.0), branch.label


def test_a_world_branch_centres_and_a_pixel_branch_does_not() -> None:
    """The centring term is the only difference between the world and pixel forms of
    the same landmark, and it belongs to world space alone."""
    world = [b for b in box_branches("face") if b.suffix == "_tx"]
    pixels = [b for b in box_branches("face") if b.suffix == "_px"]
    assert world and pixels
    assert all(b.offset == -0.5 for b in world)
    assert all(b.offset == 0.0 for b in pixels)
    # Same source channels, same box, different space.
    assert world[0].names == pixels[0].names
    assert world[0].extent == pixels[0].extent


# ---------------------------------------------------------------------------
# What the screen-space-only toggle is allowed to remove
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stream", STREAM_NAMES)
def test_the_toggle_only_ever_removes_a_second_copy(stream: str) -> None:
    """Nothing without a screen-space companion may be in the delete list. An angle
    has no pixel form and a confidence has no meaning in one, so dropping either
    would lose information rather than a duplicate."""
    doomed = set(companioned_names(stream))
    grouped = names_by_role(stream)
    assert not doomed & set(grouped[ROLE_SCALAR])
    assert not doomed & set(grouped[ROLE_ANGLE])


@pytest.mark.parametrize("stream", STREAM_NAMES)
def test_every_transformed_channel_is_in_the_delete_list(stream: str) -> None:
    """The other half of the same property: if a channel HAS a companion it must be
    in the list, or the toggle leaves a duplicate behind and does nothing useful."""
    doomed = set(companioned_names(stream))
    for branch in transform_branches(stream):
        assert set(branch.names) <= doomed, branch.label
    for box in box_branches(stream):
        assert set(box.names) <= doomed, box.label


def test_the_delete_list_is_in_contract_order() -> None:
    """A Delete CHOP does not care, but a printed build report and a diff both do -
    and a list that reorders itself between runs shows as a modified project."""
    for stream in STREAM_NAMES:
        ordered = [n for n in channel_roles(stream)
                   if n in set(companioned_names(stream))]
        assert companioned_names(stream) == ordered


def test_every_branch_carries_the_suffix_its_channels_actually_end_with() -> None:
    """A builder peels `source` off to build the rename. Getting it from the PATTERN
    works for `*_x` and breaks the moment `compact_pattern` falls back to a literal
    list, so it is carried explicitly and checked here."""
    for stream in STREAM_NAMES:
        for branch in transform_branches(stream):
            assert all(n.endswith(branch.source) for n in branch.names), branch.label
            assert branch.suffix.endswith(branch.source[-1]), branch.label
        for box in box_branches(stream):
            assert all(n.endswith(box.source) for n in box.names), box.label
            assert box.suffix.endswith(box.source[-1]), box.label


# ---------------------------------------------------------------------------
# The channels that never arrive on the wire
# ---------------------------------------------------------------------------
def test_a_derived_position_is_transformed_like_a_joint() -> None:
    """palm, pinch and the two centres are points in the image. Nothing about them
    is different from a wrist except that TouchDesigner computes them."""
    roles = derived_roles(DERIVE_CHOP)
    for stem in ("h0_palm", "h1_palm", "h0_pinch", "hands_center", "index_center"):
        assert roles[stem + "_x"] == ROLE_POSITION_X, stem
        assert roles[stem + "_y"] == ROLE_POSITION_Y, stem


def test_a_rate_takes_the_extent_rule_and_therefore_no_offset() -> None:
    """A velocity of 0.2 is 0.2 wherever the hand is, so centring it would be as
    wrong as centring a width."""
    roles = derived_roles(TEMPORAL)
    assert roles["h0_vel_x"] == ROLE_EXTENT_X
    assert roles["h0_vel_y"] == ROLE_EXTENT_Y
    for branch in derived_branches(TEMPORAL):
        assert branch.offset == 0.0, branch.label


def test_a_unit_vector_is_never_transformed() -> None:
    """The one that is a CONCLUSION rather than an omission. World space scales y by
    the render's aspect, so a transformed unit vector is neither unit length nor
    pointing where the image-space one pointed."""
    for source, stems in ((DERIVE_CHOP, ("h0_point", "h1_point")),
                          (TEMPORAL, ("h0_dir", "h1_dir"))):
        roles = derived_roles(source)
        for stem in stems:
            assert roles[stem + "_x"] == ROLE_SCALAR, stem
            assert roles[stem + "_y"] == ROLE_SCALAR, stem
        # And therefore in no branch at all.
        touched = {n for b in derived_branches(source) for n in b.names}
        assert not touched & {s + a for s in stems for a in ("_x", "_y")}


def test_the_hand_local_descriptor_is_not_in_the_table_at_all() -> None:
    """The 84 `h{i}_d_<joint>_x/y` channels are joint offsets in the HAND's frame,
    divided by hand size. A shape signature, with no image position in it — so they
    are absent rather than marked, and a branch cannot reach them."""
    for source in DERIVED_SOURCES:
        assert not any("_d_" in name for name in derived_roles(source))
        assert not any("_d_" in n for b in derived_branches(source) for n in b.names)


def test_a_derived_branch_has_the_same_shape_as_a_wire_branch() -> None:
    """They go through the identical builder code, so a difference in shape is a
    crash at build time — and the arithmetic must not be duplicated."""
    for source in DERIVED_SOURCES:
        for branch in derived_branches(source):
            assert isinstance(branch, type(transform_branches("hands")[0]))
            assert branch.names
            assert branch.source in ("_x", "_y")
            assert branch.suffix in ("_tx", "_ty", "_px", "_py")
            assert all(n.endswith(branch.source) for n in branch.names)


def test_a_derived_branch_selects_by_NAME_not_by_pattern() -> None:
    """`derive_chop`'s output holds `h0_palm_x` AND all 84 `h0_d_<joint>_x`
    descriptor channels, and DESIGN.md 2.11 records that `h?_*_x` cannot tell those
    apart. There are at most twelve names, so a literal list costs nothing."""
    for source in DERIVED_SOURCES:
        for branch in derived_branches(source):
            assert branch.pattern == " ".join(branch.names), branch.label
            assert "*" not in branch.pattern


def test_the_derived_delete_list_is_exactly_the_companioned_channels() -> None:
    """What `Screenspaceonly` removes from the derived half — 16 channels, and not
    one of the eight unit vectors."""
    doomed = derived_companioned_names()
    assert len(doomed) == 16
    assert not any("point" in n or "dir" in n for n in doomed)
    every = {n for s in DERIVED_SOURCES
             for b in derived_branches(s) for n in b.names}
    assert set(doomed) == every


def test_an_unknown_derived_source_raises() -> None:
    with pytest.raises(ValueError, match="unknown derived source"):
        derived_roles("nonesuch")


def test_the_derived_and_wire_delete_lists_do_not_overlap() -> None:
    """They are concatenated into one Delete CHOP list, and a duplicated name there
    would be harmless — but it would also mean one of the two registries had grown a
    channel that belongs to the other."""
    for stream in STREAM_NAMES:
        assert not set(companioned_names(stream)) & set(derived_companioned_names())


# ---------------------------------------------------------------------------
# The four key points
#
# Same arithmetic as a landmark, a different COMP, and one thing that has to stay
# true for either to work: the two sets must not overlap, because the patterns that
# separate them are what the two toggles are built on.
# ---------------------------------------------------------------------------
def test_a_key_point_is_box_relative_and_not_a_landmark() -> None:
    """It shares the landmarks' SPACE - normalised to the face's box - and not their
    role, because sharing the role would put 16 cheap channels inside the COMP that
    holds 348 expensive ones."""
    roles = channel_roles("face")
    for point in FACE_KEYPOINTS:
        assert roles["f0_%s_x" % point] == ROLE_BOX_KEYPOINT
    assert roles["f0_left_eye_00_x"] == ROLE_BOX_RELATIVE
    assert roles["f0_bbox_x"] == ROLE_POSITION_X


def test_the_key_points_are_smoothed_like_every_other_point() -> None:
    """A jittering pupil is the same defect as a jittering landmark, and the filter
    splits on ROLE - a role missing from SMOOTHED_ROLES drops out of the filter
    group's scope and is passed through raw with nothing saying so."""
    assert ROLE_BOX_KEYPOINT in SMOOTHED_ROLES
    smoothed = set(smoothed_names("face"))
    for point in FACE_KEYPOINTS:
        assert "f0_%s_y" % point in smoothed


def test_hands_and_pose_have_no_key_points() -> None:
    assert keypoint_branches("hands") == []
    assert keypoint_branches("pose") == []
    assert len(keypoint_branches("face")) == 8   # 2 faces x 2 axes x 2 spaces


def test_a_key_point_branch_carries_four_channels_and_a_landmark_one_87() -> None:
    """The ratio is the point of the whole split: 1.21 ms of landmark composition
    against a fiftieth of it."""
    for branch in keypoint_branches("face"):
        assert len(branch.names) == len(FACE_KEYPOINTS)
    for branch in box_branches("face"):
        assert len(branch.names) == 87


def test_the_two_kinds_of_branch_never_name_the_same_channel() -> None:
    """They are composed in different COMPs, so a channel in both would be computed
    twice and merged into a name collision."""
    landmarks = {name for b in box_branches("face") for name in b.names}
    points = {name for b in keypoint_branches("face") for name in b.names}
    assert landmarks & points == set()


def test_no_branch_label_is_used_twice() -> None:
    """Labels become operator names, and the two roles each produce a branch per
    (face, axis, space) - so without the `kp_` prefix both would ask to be `f0_tx`
    and TouchDesigner would silently rename one of them."""
    labels = [b.label for b in box_branches("face") + keypoint_branches("face")]
    assert len(set(labels)) == len(labels)


def test_a_key_point_composes_through_the_same_box_as_a_landmark() -> None:
    """The arithmetic is evaluated, not eyeballed - the same reason
    `test_box_expressions_compose_through_the_box_and_then_the_space` exists."""
    box = {"f0_bbox_x": 0.30, "f0_bbox_y": 0.20, "f0_bbox_w": 0.22, "f0_bbox_h": 0.30}

    class _FakeOp:
        def __getitem__(self, name: str) -> float:
            return box[name]

    scope = {"op": lambda _path: _FakeOp()}
    point = 0.75
    for branch in keypoint_branches("face"):
        if not branch.label.startswith("kp_f0"):
            continue
        gain_expr, postoff_expr = box_expressions(branch, "2.0")
        value = point * eval(gain_expr, scope) + eval(postoff_expr, scope)
        origin, extent = box[branch.origin], box[branch.extent]
        assert value == pytest.approx(
            (origin + point * extent + branch.offset) * 2.0)


def test_the_key_points_have_screen_space_companions() -> None:
    """`Screenspaceonly` deletes a raw channel only when a transformed twin carries
    the same information. A key point that reached that list without a companion
    would be deleted outright."""
    companioned = set(companioned_names("face"))
    for point in FACE_KEYPOINTS:
        assert "f0_%s_x" % point in companioned
        assert "f0_%s_y" % point in companioned
