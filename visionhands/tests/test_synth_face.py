"""The synthetic face, and specifically the parts of it that can be wrong quietly.

A synthetic stream's whole job is to be trustworthy enough that a failure downstream
means the failure is downstream. Three classes of mistake would break that, and all
three are cheap to assert:

  * **a mirrored face.** The subject's LEFT is at LARGER x, because a person facing
    an unmirrored camera has their left side on the right of the image. `synth.py`
    got this wrong for hands first, and it looked entirely plausible - so it is
    checked here rather than noticed later.
  * **an incomplete constellation.** A region short of its measured point count gets
    padded with zeros by `face_channel_values`, so the stream still has 371 channels
    and some of them are silently flat. That is exactly the failure the fixed channel
    list exists to prevent, and it would hide inside it.
  * **a face that does not move.** The point of driving a landmark channel is to see
    it change. A rigid template with the angles silently dropped produces a stream
    that looks perfect and tests nothing.

Ref: visionhands/synth_face.py, visionhands/face_types.py, DESIGN.md 6.4.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from visionhands.face_types import (
    FACE_REGION_NAMES,
    FACE_REGIONS,
    MAX_FACES,
    face_channel_names,
    face_channel_values,
)
from visionhands.synth_face import FacePose, synthetic_face, synthetic_face_frame


def _channels(*poses: FacePose) -> dict[str, float]:
    """One frame's channels as a name -> value map, through the real contract."""
    frame = synthetic_face_frame(poses)
    return dict(zip(face_channel_names(),
                    face_channel_values(frame, 0.0, len(poses)), strict=True))


# ---------------------------------------------------------------------------
# The constellation is complete
# ---------------------------------------------------------------------------
def test_every_region_has_at_least_its_measured_point_count() -> None:
    """A short region is padded with zeros and vanishes into a correct-looking
    channel count."""
    points = dict(synthetic_face(FacePose()).landmarks)
    for region in FACE_REGIONS:
        assert region.name in points, region.name
        assert len(points[region.name]) >= (region.point_count or 0), region.name


def test_the_region_names_are_exactly_the_contract_names() -> None:
    """A typo in a region name here does not raise: the region is padded with zeros
    and the misspelled one is ignored."""
    points = dict(synthetic_face(FacePose()).landmarks)
    assert set(points) == set(FACE_REGION_NAMES)


def test_no_landmark_channel_of_a_present_face_is_exactly_zero() -> None:
    """Zero is what an ABSENT face's channels read, so a present face's landmark
    sitting on exactly 0.0 makes "is this slot empty" unanswerable. Which is why the
    template is inset from the box edge."""
    channels = _channels(FacePose())
    landmarks = {name: value for name, value in channels.items()
                 if name.startswith("f0_") and "bbox" not in name
                 and name[-2:] in ("_x", "_y")}
    assert len(landmarks) == 174           # 87 slots x 2 axes
    assert [n for n, v in landmarks.items() if v == 0.0] == []


def test_an_absent_face_is_all_zeros_and_still_present() -> None:
    """The contract does not shrink when nobody is in frame (DESIGN.md 6.2)."""
    channels = _channels(FacePose())       # one face, so f1 is the blank
    assert channels["f1_found"] == 0.0
    blank = [v for n, v in channels.items() if n.startswith("f1_")]
    assert blank and set(blank) == {0.0}
    assert len(channels) == len(face_channel_names())


# ---------------------------------------------------------------------------
# It is not mirrored, and the features are where a face keeps them
# ---------------------------------------------------------------------------
def test_the_subjects_left_is_at_larger_x() -> None:
    """The trap this file exists for. A person facing an unmirrored camera has their
    left side on the RIGHT of the image, so every left_* feature sits at larger x
    than its right_* twin."""
    channels = _channels(FacePose())
    for region in ("pupil", "eye", "eyebrow"):
        left = channels["f0_left_%s_00_x" % region]
        right = channels["f0_right_%s_00_x" % region]
        assert left > right, region


def test_the_features_are_stacked_the_way_a_face_is() -> None:
    """Eyes above the nose above the mouth, in box space with the origin at the
    BOTTOM - so a vertical flip shows up here as an inverted face rather than as a
    plausible one."""
    channels = _channels(FacePose())
    eye = channels["f0_left_pupil_00_y"]
    nose = channels["f0_nose_crest_05_y"]        # the tip end of the bridge
    mouth = channels["f0_inner_lips_00_y"]
    chin = channels["f0_median_line_09_y"]
    assert eye > nose > mouth > chin


def test_every_landmark_stays_inside_its_own_bounding_box() -> None:
    """`normalizedPoints` are normalised to the box, so 0..1 is the whole range that
    can occur. A consumer tuned against 1.4 would be tuned against nothing real."""
    for pose in (FacePose(), FacePose(yaw=80.0, roll=45.0, pitch=-60.0),
                 FacePose(size=0.4), FacePose(expression=1.0)):
        for _name, points in synthetic_face(pose).landmarks:
            for x, y in points:
                assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


# ---------------------------------------------------------------------------
# It moves, which is the only reason to drive it
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("field", "axis"), [("yaw", "_x"), ("pitch", "_y")])
def test_turning_the_head_moves_the_landmarks_on_that_axis(field: str,
                                                           axis: str) -> None:
    """A rigid template with the angle silently dropped produces a stream that looks
    perfect and exercises nothing."""
    still = _channels(FacePose())
    turned = _channels(FacePose(**{field: 35.0}))
    moved = [name for name in still
             if name.startswith("f0_") and name.endswith(axis)
             and "bbox" not in name and abs(still[name] - turned[name]) > 1e-6]
    # Not "all of them": a point already against the box edge is clamped, and one
    # on the axis of the transform does not move. A clear majority is the property.
    assert len(moved) > 60, (field, len(moved))


def test_a_roll_moves_both_axes() -> None:
    """Unlike yaw and pitch, a roll is a rotation, so it has to move x AND y."""
    still = _channels(FacePose())
    rolled = _channels(FacePose(roll=30.0))
    for axis in ("_x", "_y"):
        moved = [name for name in still
                 if name.startswith("f0_") and name.endswith(axis)
                 and "bbox" not in name and abs(still[name] - rolled[name]) > 1e-6]
        assert len(moved) > 60, axis


def test_opening_the_mouth_moves_the_lips_and_nothing_else() -> None:
    """The one non-rigid parameter. If it moved the eyes too it would be a scale, not
    an expression."""
    closed = _channels(FacePose(expression=0.0))
    open_wide = _channels(FacePose(expression=1.0))
    changed = {name for name in closed
               if abs(closed[name] - open_wide[name]) > 1e-6}
    assert changed, "the mouth did not open at all"
    assert all("lips" in name for name in changed), sorted(changed)[:4]
    # And it opens DOWNWARD: the lower lip travels further than the upper.
    lower = "f0_outer_lips_10_y"            # below the mouth centre in the template
    assert open_wide[lower] < closed[lower]


def test_the_head_angles_arrive_in_degrees() -> None:
    """Vision reports radians and the conversion happens once, in face.py. A
    synthetic stream that sent radians would leave that conversion untested and
    every downstream rotation 57x too small."""
    channels = _channels(FacePose(roll=30.0, yaw=-20.0, pitch=15.0))
    assert channels["f0_roll"] == pytest.approx(30.0)
    assert channels["f0_yaw"] == pytest.approx(-20.0)
    assert channels["f0_pitch"] == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Two faces, and the slot rule
# ---------------------------------------------------------------------------
def test_two_faces_are_ordered_left_to_right_by_box_centre() -> None:
    """`f0` is the leftmost face. Stateless and spatial, so two faces that cross
    exchange slots - the known limit, made visible rather than hidden."""
    channels = _channels(FacePose(center_x=0.75), FacePose(center_x=0.25))
    assert channels["f0_bbox_x"] < channels["f1_bbox_x"]
    assert channels["f0_found"] == channels["f1_found"] == 1.0


def test_more_poses_than_slots_are_dropped_rather_than_overflowing() -> None:
    frame = synthetic_face_frame(tuple(FacePose(center_x=0.1 * i)
                                       for i in range(MAX_FACES + 3)))
    assert len(frame.faces) == MAX_FACES


def test_the_box_stays_inside_the_frame() -> None:
    """Vision's box is always inside the image, so a synthetic one that is not would
    let a consumer be tuned against something that cannot happen."""
    for pose in (FacePose(center_x=0.0), FacePose(center_x=1.0, center_y=1.0),
                 FacePose(size=3.0)):
        face = synthetic_face(pose)
        assert 0.0 <= face.bbox_x and face.bbox_x + face.bbox_w <= 1.0 + 1e-9
        assert 0.0 <= face.bbox_y and face.bbox_y + face.bbox_h <= 1.0 + 1e-9


def test_a_face_is_immutable_and_carries_no_native_object() -> None:
    """`Face` crosses a thread boundary by one atomic rebind and no lock, which only
    works because everything in it is a float, a bool or a tuple."""
    face = synthetic_face(FacePose())
    with pytest.raises(FrozenInstanceError):
        face.bbox_x = 0.5                       # type: ignore[misc,assignment]
    for _name, points in face.landmarks:
        assert isinstance(points, tuple)
        assert all(isinstance(v, float) for point in points for v in point)
