"""The face contract: the region table, the channel list, and the ordering.

Two things here are load-bearing in a way the pose tests are not.

**The point counts must stay unmeasured rather than plausible.** The temptation is
to write down 11 for the face contour and 8 for an eye and move on. A wrong count
does not fail - it lays one region's points into another's channels, and a nose in
an eyebrow's channels looks entirely correct until somebody wonders why the eyebrows
never move. So there is a test that the table contains no guessed number, and a test
that a partial table is refused.

**Angles are degrees.** Vision reports radians; the conversion happens once, in
`face.py`. A missed conversion is a silent 57x that keeps every number small and
plausible - so `test_face.py` pins the conversion and this file pins the contract's
units in its own docstrings.

Ref: DESIGN.md 6.4, 2.12; appletd/face_types.py.
"""

from __future__ import annotations

import dataclasses

import pytest

from appletd.face_types import (
    BLANK_FACE,
    CONSTELLATION_DISTINCT_POINTS,
    FACE_KEYPOINTS,
    FACE_REGION_NAMES,
    FACE_REGIONS,
    FACE_SCALARS,
    LANDMARKS_PUBLISHED,
    MAX_FACES,
    N_FACE_CHANNELS,
    REGION_SLOT_TOTAL,
    Face,
    blank_face_frame,
    face_channel_names,
    face_channel_values,
    face_keypoints,
    nose_tip_point,
    order_faces,
    region_center,
)
from appletd.types import Confidence, NormX, NormY


def _face(center_x: float, *, width: float = 0.2, quality: float = 0.8) -> Face:
    return Face(
        confidence=Confidence(1.0), quality=Confidence(quality),
        roll_deg=0.0, yaw_deg=0.0, pitch_deg=0.0,
        bbox_x=NormX(center_x - width / 2), bbox_y=NormY(0.4),
        bbox_w=width, bbox_h=0.3, found=True)


# ---------------------------------------------------------------------------
# The region table, and the numbers it refuses to invent
# ---------------------------------------------------------------------------
def test_the_twelve_regions_are_named_in_this_order() -> None:
    """It will BE the channel order once the counts land, so it is pinned before a
    project can reference it rather than after."""
    assert FACE_REGION_NAMES == (
        "face_contour", "median_line", "left_eyebrow", "right_eyebrow",
        "left_eye", "right_eye", "left_pupil", "right_pupil",
        "nose", "nose_crest", "outer_lips", "inner_lips",
    )


def test_no_region_carries_a_GUESSED_point_count() -> None:
    """The point of the whole arrangement. Nothing in the framework publishes these
    counts before a face has been observed (DESIGN.md 2.12), so a number here that
    nobody measured is a number that will quietly misalign a region.

    When `tools/probe_face_regions.py` has been run and the counts pasted in, this
    test is the one to delete - and deleting it should be a deliberate act, which is
    why it says so here.
    """
    if LANDMARKS_PUBLISHED:
        pytest.skip("counts have been measured; this test has served its purpose")
    assert all(region.point_count is None for region in FACE_REGIONS)


def test_the_counts_sum_to_the_SLOT_total_not_the_distinct_one() -> None:
    """The distinction that made the first attempt at this table fail its own check.

    MEASURED: 76 DISTINCT coordinates, 87 region SLOTS, because the regions are not
    a partition - `medianLine` shares points with five other regions and the nose
    tip sits in three. So the sum legitimately exceeds the constellation's total,
    and checking against 76 refused a correctly-measured table.
    """
    if not LANDMARKS_PUBLISHED:
        pytest.skip("counts not measured yet")
    assert sum(r.point_count or 0 for r in FACE_REGIONS) == REGION_SLOT_TOTAL
    assert REGION_SLOT_TOTAL > CONSTELLATION_DISTINCT_POINTS
    # The 11 duplicates are the price of named points, and they are deliberate.
    assert REGION_SLOT_TOTAL - CONSTELLATION_DISTINCT_POINTS == 11


def test_a_half_filled_table_is_refused_at_import(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Half a table is worse than none: it would publish some regions and silently
    omit others, and an omission is invisible in a channel list of zeros. Simulated
    rather than waited for, because the real thing would be somebody's
    half-finished edit - and `monkeypatch` puts the real table back afterwards,
    which matters because every other test in this file reads it.
    """
    from appletd import face_types

    # One count REMOVED, now that they are all measured: eleven filled and one None
    # is the state a half-finished edit leaves behind, and it is the one that would
    # publish some regions and silently omit others.
    half = list(FACE_REGIONS)
    half[0] = dataclasses.replace(half[0], point_count=None)
    monkeypatch.setattr(face_types, "FACE_REGIONS", tuple(half))
    with pytest.raises(RuntimeError, match="Fill in ALL of them or none"):
        face_types._self_check()


def test_a_mistyped_count_is_refused_at_import(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard that matters now the table is full: a full table whose counts do not
    sum to the measured slot total. A wrong count does not shift a value, it shifts
    every LATER channel onto the wrong data - so it has to fail at import rather
    than in a channel list."""
    from appletd import face_types

    typo = list(FACE_REGIONS)
    typo[0] = dataclasses.replace(typo[0], point_count=71)      # 17 fat-fingered
    monkeypatch.setattr(face_types, "FACE_REGIONS", tuple(typo))
    with pytest.raises(RuntimeError, match="region slots"):
        face_types._self_check()


def test_every_region_has_a_distinct_accessor() -> None:
    """Two regions sharing a selector would publish the same points twice and drop
    one region entirely."""
    assert len({r.accessor for r in FACE_REGIONS}) == len(FACE_REGIONS)


# ---------------------------------------------------------------------------
# The channel list
# ---------------------------------------------------------------------------
def test_the_channel_count_is_what_the_table_implies() -> None:
    """387 with the landmarks published: 3 frame scalars, 10 per face, 4 key points
    and 87 region slots, both x 2 axes x 2 faces. MEASURED at 12,640 bytes on the
    wire - 77% of a 16 KB datagram, which makes this stream the size constraint and
    MAX_FACES = 2 the ceiling for a single bundle (appletd/osc.py)."""
    assert N_FACE_CHANNELS == len(face_channel_names())
    scalars = 3 + MAX_FACES * len(FACE_SCALARS)
    if LANDMARKS_PUBLISHED:
        points = MAX_FACES * (len(FACE_KEYPOINTS) + REGION_SLOT_TOTAL) * 2
        assert N_FACE_CHANNELS == scalars + points == 387
    else:
        assert N_FACE_CHANNELS == scalars == 23


def test_names_and_values_stay_the_same_length() -> None:
    assert len(face_channel_values(blank_face_frame(), 0.0, 0)) == \
        len(face_channel_names())


def test_face_seq_is_at_index_one() -> None:
    """The sidecar wraps `values[1]` for float32 exactness, so this has to be true
    rather than believed (DESIGN.md 2.9)."""
    assert face_channel_names()[1] == "face_seq"


def test_the_angles_are_named_without_units_because_they_are_degrees() -> None:
    """Stated as a test because the alternative convention is radians, which is what
    Vision hands over, and the channel names are where a consumer looks for the
    answer. docs/ATTRIBUTES.md: every angle in this system is degrees."""
    names = set(face_channel_names())
    assert {"f0_roll", "f0_yaw", "f0_pitch"} <= names
    assert not any(n.endswith(("_rad", "_radians")) for n in names)


def test_values_are_where_the_names_say_they_are() -> None:
    """One end-to-end alignment check with every field distinct, so a shifted index
    cannot pass."""
    face = Face(confidence=Confidence(0.9), quality=Confidence(0.7),
                roll_deg=10.0, yaw_deg=-20.0, pitch_deg=30.0,
                bbox_x=NormX(0.1), bbox_y=NormY(0.2), bbox_w=0.3, bbox_h=0.4,
                found=True)
    frame = dataclasses.replace(blank_face_frame(), faces=(face, BLANK_FACE))
    reading = dict(zip(face_channel_names(),
                       face_channel_values(frame, 5.0, 1), strict=True))
    assert reading["face_n"] == 1.0
    assert reading["face_age_ms"] == 5.0
    assert reading["f0_score"] == pytest.approx(0.9)
    assert reading["f0_quality"] == pytest.approx(0.7)
    assert reading["f0_roll"] == pytest.approx(10.0)
    assert reading["f0_yaw"] == pytest.approx(-20.0)
    assert reading["f0_pitch"] == pytest.approx(30.0)
    assert reading["f0_bbox_x"] == pytest.approx(0.1)
    assert reading["f0_bbox_h"] == pytest.approx(0.4)
    assert reading["f1_found"] == 0.0


def test_an_absent_face_publishes_zeros_not_nothing() -> None:
    reading = dict(zip(face_channel_names(),
                       face_channel_values(blank_face_frame(), 0.0, 0), strict=True))
    assert reading["f0_found"] == 0.0
    # 0.0, not the 1.0 a real observation reports: an empty slot must not look like
    # a confident detection to anything that skips `found`.
    assert reading["f0_score"] == 0.0
    assert reading["f0_quality"] == 0.0


# ---------------------------------------------------------------------------
# The ordering
# ---------------------------------------------------------------------------
def test_faces_are_ordered_left_to_right_by_box_centre() -> None:
    """`f0` is the leftmost face, whatever order Vision listed them in - it gives no
    tracking ID and no stable order."""
    left, right = _face(0.2), _face(0.8)
    assert order_faces([right, left]) == (left, right)


def test_the_order_does_not_depend_on_the_input_order() -> None:
    a, b = _face(0.3), _face(0.7)
    assert order_faces([a, b]) == order_faces([b, a])


def test_a_wide_box_is_sorted_by_its_CENTRE_not_its_edge() -> None:
    """A face close to the camera has a wide box. Sorting on `bbox_x` - the left
    edge - would put a near face left of a far one that is actually further left."""
    near = _face(0.55, width=0.5)        # left edge 0.30
    far = _face(0.40, width=0.1)         # left edge 0.35, but further LEFT
    assert order_faces([near, far]) == (far, near)


def test_the_slots_are_padded_and_capped() -> None:
    assert len(order_faces([])) == MAX_FACES
    assert len(order_faces([_face(0.1), _face(0.5), _face(0.9)])) == MAX_FACES


def test_the_two_faces_published_are_the_two_LEFTMOST() -> None:
    ordered = order_faces([_face(0.9), _face(0.1), _face(0.5)])
    assert [round(f.center_x(), 3) for f in ordered] == [0.1, 0.5]


def test_empty_slots_share_one_frozen_object() -> None:
    """No per-frame allocation on the capture thread."""
    assert order_faces([])[0] is BLANK_FACE
    assert blank_face_frame().faces[1] is BLANK_FACE


# ---------------------------------------------------------------------------
# The four key points
#
# Three of the four are not points Vision publishes, so each of them is a small
# piece of arithmetic that can be silently wrong: a mouth centre averaged over the
# wrong ring, or a nose tip taken from the wrong index, both look entirely plausible
# on screen. These check the arithmetic against hand-built landmark tables rather
# than against a face.
# ---------------------------------------------------------------------------
def _landmarks(**regions: tuple[tuple[float, float], ...]
               ) -> tuple[tuple[str, tuple[tuple[float, float], ...]], ...]:
    """A landmarks tuple in `Face.landmarks` shape, from keyword regions."""
    return tuple(regions.items())


def _face_with(**regions: tuple[tuple[float, float], ...]) -> Face:
    return dataclasses.replace(BLANK_FACE, found=True,
                               landmarks=_landmarks(**regions))


def test_the_nose_tip_is_the_point_all_three_regions_carry() -> None:
    """The whole identification rule, and the reason no index is pinned anywhere:
    MEASURED 2026-08-21, exactly one point belongs to `nose`, `nose_crest` and
    `median_line` at once (DESIGN.md 2.12)."""
    tip = (0.5, 0.38)
    found = nose_tip_point(_landmarks(
        nose=((0.4, 0.34), tip, (0.6, 0.34)),
        nose_crest=((0.5, 0.6), (0.5, 0.5), tip),
        median_line=((0.5, 0.9), tip, (0.5, 0.1))))
    assert found == tip


def test_the_nose_tip_is_published_unrounded() -> None:
    """The rounding is how the point is IDENTIFIED, not what is published. A tip
    quantised to 1e-6 while the 348 channels beside it are not would be a second
    precision convention for one channel."""
    tip = (0.5000004999, 0.3800002001)
    found = nose_tip_point(_landmarks(
        nose=(tip,), nose_crest=(tip,), median_line=(tip,)))
    assert found == tip


def test_a_nose_tip_shared_by_only_two_regions_is_not_one() -> None:
    """A point in two regions is one of the NINE ordinary duplicates. Accepting it
    would publish a nostril and call it a tip."""
    assert nose_tip_point(_landmarks(
        nose=((0.5, 0.38),),
        nose_crest=((0.5, 0.38),),
        median_line=((0.5, 0.9), (0.5, 0.1)))) is None


def test_two_triple_shared_points_are_refused() -> None:
    """Two candidates is as wrong as none: the assumption this is built on - one
    triple-shared point - would no longer hold, and picking either would be a
    guess."""
    a, b = (0.5, 0.38), (0.5, 0.40)
    assert nose_tip_point(_landmarks(
        nose=(a, b), nose_crest=(a, b), median_line=(a, b))) is None


def test_a_missing_region_gives_no_nose_tip() -> None:
    """`_region_points` returns () for a region this Vision does not have, and an
    empty region must not silently become an intersection of the other two."""
    assert nose_tip_point(_landmarks(
        nose=((0.5, 0.38),), nose_crest=((0.5, 0.38),), median_line=())) is None
    assert nose_tip_point(_landmarks(nose=((0.5, 0.38),))) is None


def test_the_region_centre_is_the_mean_of_its_points() -> None:
    """Not the centre of its bounding box: an opening mouth moves one side only, and
    a box centre would follow the extreme rather than the aperture."""
    assert region_center(_landmarks(inner_lips=((0.0, 0.0), (1.0, 0.5))),
                         "inner_lips") == (0.5, 0.25)
    assert region_center(_landmarks(inner_lips=()), "inner_lips") is None
    assert region_center(_landmarks(), "inner_lips") is None


def test_the_pupils_are_taken_as_the_eye_centres_untouched() -> None:
    """`leftPupil` and `rightPupil` are the only SINGLE-POINT regions Vision gives,
    so each already IS a centre. Averaging one would be arithmetic for its own
    sake."""
    face = _face_with(left_pupil=((0.7, 0.64),), right_pupil=((0.3, 0.64),))
    eye_left, eye_right, _tip, _mouth = face_keypoints(face)
    assert eye_left == (0.7, 0.64)
    assert eye_right == (0.3, 0.64)


def test_every_key_point_a_face_cannot_supply_reads_zero() -> None:
    """Never omitted: a short list would shift every later channel onto the wrong
    data, which is the failure the fixed-list discipline here exists to prevent."""
    assert face_keypoints(BLANK_FACE) == ((0.0, 0.0),) * len(FACE_KEYPOINTS)
    assert len(face_keypoints(_face_with())) == len(FACE_KEYPOINTS)
    assert face_keypoints(_face_with(left_pupil=((0.7, 0.6),))) == (
        (0.7, 0.6), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0))


def test_the_key_point_channels_carry_the_key_point_values() -> None:
    """Names and values are built in two separate loops, so the thing worth checking
    is that the second lands where the first says - by INDEX, not by count."""
    face = _face_with(left_pupil=((0.7, 0.64),), right_pupil=((0.3, 0.64),),
                      inner_lips=((0.4, 0.2), (0.6, 0.2)))
    frame = dataclasses.replace(blank_face_frame(),
                                faces=(face, BLANK_FACE))
    names = face_channel_names()
    values = face_channel_values(frame, 0.0, 1)
    channels = dict(zip(names, values, strict=False))
    assert channels["f0_eye_left_x"] == 0.7
    assert channels["f0_eye_right_x"] == 0.3
    assert channels["f0_mouth_x"] == 0.5
    assert channels["f0_nose_tip_x"] == 0.0
    assert channels["f1_eye_left_x"] == 0.0


def test_the_key_points_come_before_the_regions() -> None:
    """Order is the contract. A project reading the list should meet four points
    before it meets 174, and `spaces.compact_pattern` compares ORDERED lists - a
    reordering here would silently defeat every pattern it verifies."""
    names = list(face_channel_names())
    first_region = names.index("f0_face_contour_00_x")
    for point in FACE_KEYPOINTS:
        assert names.index("f0_%s_x" % point) < first_region


def test_no_key_point_name_collides_with_a_landmark_pattern() -> None:
    """`f0_*_[0-9][0-9]_x` is what separates the 348 landmarks from the key points in
    every Select CHOP this contract feeds. It works because a landmark carries a
    two-digit point index and a key point carries none."""
    from fnmatch import fnmatchcase
    for point in FACE_KEYPOINTS:
        for axis in ("x", "y"):
            name = "f0_%s_%s" % (point, axis)
            assert not fnmatchcase(name, "f0_*_[0-9][0-9]_%s" % axis), name
