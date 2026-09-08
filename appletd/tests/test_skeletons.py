"""The overlay's three derived lists agree, and stay attached to the joint tables.

`appletd/skeletons.py` generates a Select CHOP's channels, a CHOP to SOP's scope and
a Delete SOP's primitive list from one connection table. The failure mode is silent
in every direction: a channel nobody publishes emits NOTHING from a Select, and a
wrong Delete list draws a line across the image rather than raising.

The hand case is checked against the network Omer built by hand, which is the only
independent description of what correct looks like.

Ref: appletd/skeletons.py, tools/td_add_overlay.py.
"""

from __future__ import annotations

from appletd.face_types import FACE_REGIONS
from appletd.pose_types import BODY_JOINT_NAMES
from appletd.skeletons import (
    BODY_CONNECTIONS,
    CLOSED_FACE_REGIONS,
    HAND_CONNECTIONS,
    INDEX_CONNECTIONS,
    channel_names,
    endpoints,
    face_landmark_connections,
    jump_primitives,
)
from appletd.types import JOINT_NAMES

HANDS = ("h0", "h1")

# The Delete SOP list from the network Omer built and verified on a live hand,
# 2026-09-08. Kept as a literal precisely because it was arrived at independently:
# it is the one check here that is not the generator marking its own homework.
OMERS_JUMPS = [7, 15, 23, 31, 39, 41, 49, 57, 65, 73, 81]


def test_the_hand_matches_the_network_it_came_from() -> None:
    assert jump_primitives(HANDS, HAND_CONNECTIONS) == OMERS_JUMPS


def test_the_hand_channel_list_is_the_one_that_was_pasted() -> None:
    names = channel_names(HANDS, HAND_CONNECTIONS)
    assert len(names) == 168
    assert names[0] == "h0_wrist_tx"
    assert names[84] == "h0_wrist_ty"
    # Both halves are the same points in the same order, which is what lets a
    # Shuffle fold them into one sample per point.
    assert [n[:-3] for n in names[:84]] == [n[:-3] for n in names[84:]]


def test_every_jump_is_a_real_discontinuity() -> None:
    """A deleted primitive must join two DIFFERENT points. Deleting a zero-length one
    would be harmless but pointless; deleting a real segment would lose a bone."""
    for connections, prefixes in ((HAND_CONNECTIONS, HANDS),
                                  (BODY_CONNECTIONS, ("p0", "p1")),
                                  (INDEX_CONNECTIONS, HANDS),
                                  (face_landmark_connections(), ("f0", "f1"))):
        points = endpoints(prefixes, connections)
        for index in jump_primitives(prefixes, connections):
            assert index % 2 == 1, "an even gap is a segment, not a jump"
            assert points[index] != points[index + 1]


def test_no_segment_is_ever_deleted() -> None:
    """The even gaps ARE the skeleton. None of them may appear in the delete list."""
    for connections, prefixes in ((HAND_CONNECTIONS, HANDS),
                                  (BODY_CONNECTIONS, ("p0", "p1")),
                                  (face_landmark_connections(), ("f0", "f1"))):
        jumps = set(jump_primitives(prefixes, connections))
        segments = set(range(0, len(endpoints(prefixes, connections)) - 1, 2))
        assert not jumps & segments


def test_the_point_count_follows_the_segments() -> None:
    for connections in (HAND_CONNECTIONS, INDEX_CONNECTIONS, BODY_CONNECTIONS):
        assert len(endpoints(HANDS, connections)) == 2 * 2 * len(connections)


def test_the_joints_all_exist() -> None:
    """The module self-checks on import; this says so out loud, and covers pose."""
    for table, names in ((HAND_CONNECTIONS, JOINT_NAMES),
                         (INDEX_CONNECTIONS, JOINT_NAMES),
                         (BODY_CONNECTIONS, BODY_JOINT_NAMES)):
        for pair in table:
            for point in pair:
                assert point in names


def test_the_hand_skeleton_reaches_every_joint() -> None:
    """21 joints, 21 segments, nothing orphaned - a joint no line touches would be
    invisible with no error anywhere."""
    drawn = {point for pair in HAND_CONNECTIONS for point in pair}
    assert drawn == set(JOINT_NAMES)


def test_the_body_skeleton_reaches_every_joint() -> None:
    drawn = {point for pair in BODY_CONNECTIONS for point in pair}
    assert drawn == set(BODY_JOINT_NAMES)


def test_the_face_closes_only_the_loops() -> None:
    """A closed region joins its last point to its first; an arc must not, or a line
    is drawn straight across the face."""
    pairs = set(face_landmark_connections())
    for region in FACE_REGIONS:
        if region.point_count is None or region.point_count < 2:
            continue
        first = "%s_00" % region.name
        last = "%s_%02d" % (region.name, region.point_count - 1)
        closes = (last, first) in pairs
        assert closes == (region.name in CLOSED_FACE_REGIONS), region.name


def test_a_one_point_region_draws_nothing() -> None:
    """The pupils are a single point each."""
    pairs = face_landmark_connections()
    for region in FACE_REGIONS:
        if region.point_count is not None and region.point_count == 1:
            assert not [p for p in pairs if p[0].startswith(region.name)]


def test_the_face_default_uses_only_what_a_default_project_publishes() -> None:
    """The point of item 5.

    `Facekeypoints` ships ON and strips the 348 landmark channels at the stream, so a
    landmark overlay selects NOTHING in a default project - a Select CHOP emits no
    channel for a name nobody publishes, silently. The default face skeleton has to be
    drawn from the four key points, which are what survives.
    """
    from appletd.face_types import FACE_KEYPOINTS
    from appletd.skeletons import FACE_KEYPOINT_CONNECTIONS

    drawn = {point for pair in FACE_KEYPOINT_CONNECTIONS for point in pair}
    assert drawn == set(FACE_KEYPOINTS), "every key point should be drawn, and only those"


def test_the_face_default_is_connected() -> None:
    """Four points, four segments, no orphan: every point reachable from any other."""
    from appletd.skeletons import FACE_KEYPOINT_CONNECTIONS

    reached = {FACE_KEYPOINT_CONNECTIONS[0][0]}
    for _ in FACE_KEYPOINT_CONNECTIONS:
        for a, b in FACE_KEYPOINT_CONNECTIONS:
            if a in reached or b in reached:
                reached |= {a, b}
    assert reached == {point for pair in FACE_KEYPOINT_CONNECTIONS for point in pair}


def test_the_landmark_mode_is_still_derivable() -> None:
    """Kept for a project that has turned `Facekeypoints` off."""
    from appletd.skeletons import face_landmark_connections

    assert len(face_landmark_connections()) > len(FACE_REGIONS)
