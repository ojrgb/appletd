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

Ref: DESIGN.md 6.4, 2.12; visionhands/face_types.py.
"""

from __future__ import annotations

import dataclasses

import pytest

from visionhands.face_types import (
    BLANK_FACE,
    EXPECTED_TOTAL_POINTS,
    FACE_REGION_NAMES,
    FACE_REGIONS,
    FACE_SCALARS,
    LANDMARKS_PUBLISHED,
    MAX_FACES,
    N_FACE_CHANNELS,
    Face,
    blank_face_frame,
    face_channel_names,
    face_channel_values,
    order_faces,
)
from visionhands.types import Confidence, NormX, NormY


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


def test_the_counts_must_sum_to_the_constellation_total() -> None:
    """The check that catches a typo in a pasted table: the request is pinned to the
    76-point constellation, so the parts have to add up to the whole."""
    if not LANDMARKS_PUBLISHED:
        pytest.skip("counts not measured yet")
    assert sum(r.point_count or 0 for r in FACE_REGIONS) == EXPECTED_TOTAL_POINTS


def test_a_half_filled_table_is_refused_at_import(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Half a table is worse than none: it would publish some regions and silently
    omit others, and an omission is invisible in a channel list of zeros. Simulated
    rather than waited for, because the real thing would be somebody's
    half-finished edit - and `monkeypatch` puts the real table back afterwards,
    which matters because every other test in this file reads it.
    """
    from visionhands import face_types

    half = list(FACE_REGIONS)
    half[0] = dataclasses.replace(half[0], point_count=11)
    monkeypatch.setattr(face_types, "FACE_REGIONS", tuple(half))
    with pytest.raises(RuntimeError, match="Fill in ALL of them or none"):
        face_types._self_check()


def test_every_region_has_a_distinct_accessor() -> None:
    """Two regions sharing a selector would publish the same points twice and drop
    one region entirely."""
    assert len({r.accessor for r in FACE_REGIONS}) == len(FACE_REGIONS)


# ---------------------------------------------------------------------------
# The channel list
# ---------------------------------------------------------------------------
def test_the_channel_list_is_head_pose_and_box_for_now() -> None:
    """23 channels: 3 frame scalars plus 10 per face. It grows once - when the point
    counts land - and that growth happens before anything consumes the stream,
    which is why `Streamface` ships off."""
    assert N_FACE_CHANNELS == len(face_channel_names())
    if not LANDMARKS_PUBLISHED:
        assert N_FACE_CHANNELS == 3 + MAX_FACES * len(FACE_SCALARS) == 23


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
