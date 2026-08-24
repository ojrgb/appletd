"""The face conversion: radians to degrees, nil to zero, and a box that means what it says.

`face.py` imports Vision, but the function under test does not use it - it reads an
observation through six selectors. So the tests below hand it a stub with those six
selectors and no camera, no framework and no face are involved.

WHAT IS WORTH TESTING HERE, and it is a short list, because everything else in the
module is a pass-through:

  * **radians to degrees.** Vision reports radians; every angle in this system is
    degrees (docs/ATTRIBUTES.md). Missed, it is a silent 57x that keeps every value
    small and plausible and makes every downstream rotation wrong.
  * **nil.** `roll`, `yaw`, `pitch` and `faceCaptureQuality` are NSNumber-OR-NIL,
    and `float(None)` raises - inside a capture callback, where an exception
    unwinding into Objective-C is its own crash (DESIGN.md 4.2).
  * **the bounding box origin.** `CGRect.origin` is the BOTTOM-left corner. Reading
    it as the top produces a face that tracks correctly and sits upside down, which
    is the failure class DESIGN.md 7 exists for.

Ref: appletd/face.py, DESIGN.md 2.12, 7.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from appletd.face import (
    face_frame_from_observations,
    keypoint_report,
    region_point_counts,
)
from appletd.face_types import FACE_REGION_NAMES, MAX_FACES, Face


class _StubRegion:
    """Stands in for VNFaceLandmarkRegion2D: a point count and normalised points.

    The camelCase method names are Objective-C SELECTORS, not our style: `face.py`
    calls `pointCount()` and `normalizedPoints()`, so a stub has to answer to those
    exact names or it is not standing in for anything.
    """

    def __init__(self, n_points: int) -> None:
        self._points = [SimpleNamespace(x=i / 100.0, y=i / 200.0)
                        for i in range(n_points)]

    def pointCount(self) -> int:
        return len(self._points)

    def normalizedPoints(self) -> list[SimpleNamespace]:
        return self._points


class _StubLandmarks:
    """Stands in for VNFaceLandmarks2D: one accessor per region.

    Built with a per-region count so a test can give the regions DIFFERENT counts -
    which is the whole reason the real ones have to be measured rather than assumed.
    """

    def __init__(self, counts: dict[str, int]) -> None:
        from appletd.face_types import FACE_REGIONS

        for region in FACE_REGIONS:
            count = counts.get(region.name, 0)
            setattr(self, region.accessor, _StubRegion(count))


class _StubObservation:
    """Stands in for VNFaceObservation, through the six selectors face.py reads."""

    def __init__(self, *, x: float = 0.1, y: float = 0.2, w: float = 0.3,
                 h: float = 0.4, roll: float | None = 0.0,
                 yaw: float | None = 0.0, pitch: float | None = 0.0,
                 confidence: float = 1.0, quality: float | None = 0.5,
                 landmark_counts: dict[str, int] | None = None) -> None:
        self._box = SimpleNamespace(origin=SimpleNamespace(x=x, y=y),
                                    size=SimpleNamespace(width=w, height=h))
        self._roll, self._yaw, self._pitch = roll, yaw, pitch
        self._confidence, self._quality = confidence, quality
        self._landmarks = (None if landmark_counts is None
                           else _StubLandmarks(landmark_counts))

    def boundingBox(self) -> SimpleNamespace:
        return self._box

    def landmarks(self) -> _StubLandmarks | None:
        return self._landmarks

    def confidence(self) -> float:
        return self._confidence

    def faceCaptureQuality(self) -> float | None:
        return self._quality

    def roll(self) -> float | None:
        return self._roll

    def yaw(self) -> float | None:
        return self._yaw

    def pitch(self) -> float | None:
        return self._pitch


def _one(**kwargs: object) -> Face:
    """One stub observation through the real conversion, returning the Face.

    Annotated as returning a `Face` rather than `object` so the assertions below
    need no `type: ignore` - the stub is duck-typed on purpose, and the one cast
    belongs here rather than at every call site.
    """
    frame = face_frame_from_observations(
        [_StubObservation(**kwargs)],   # type: ignore[arg-type]
        seq=1, captured_at=0.0, width_px=1280, height_px=720)
    return frame.faces[0]


# ---------------------------------------------------------------------------
# Units: the silent 57x
# ---------------------------------------------------------------------------
def test_angles_arrive_in_radians_and_leave_in_degrees() -> None:
    face = _one(roll=math.pi / 2, yaw=-math.pi / 4, pitch=math.pi)
    assert face.roll_deg == pytest.approx(90.0)
    assert face.yaw_deg == pytest.approx(-45.0)
    assert face.pitch_deg == pytest.approx(180.0)


def test_a_radian_value_is_not_passed_through_unconverted() -> None:
    """The specific failure: 0.7854 rad reads as a plausible small angle if nobody
    converts it, and every rotation downstream is 57x too small."""
    face = _one(roll=math.pi / 4)
    assert face.roll_deg == pytest.approx(45.0)
    assert face.roll_deg != pytest.approx(math.pi / 4)


@pytest.mark.parametrize("field", ["roll", "yaw", "pitch"])
def test_a_nil_angle_reads_zero_rather_than_raising(field: str) -> None:
    """`float(None)` raises, and it would raise on the capture thread inside an
    Objective-C callback. Revision 3 supplies all three, which is exactly why the
    day it does not would otherwise be a crash."""
    face = _one(**{field: None})
    assert getattr(face, field + "_deg") == 0.0


def test_a_nil_quality_reads_zero_rather_than_raising() -> None:
    assert _one(quality=None).quality == 0.0


# ---------------------------------------------------------------------------
# The bounding box
# ---------------------------------------------------------------------------
def test_the_box_is_passed_through_with_a_BOTTOM_left_origin() -> None:
    """CGRect.origin is the bottom-left corner and nothing here flips it
    (DESIGN.md 7). Read as a top-left origin, a face tracks perfectly and sits
    upside down."""
    face = _one(x=0.25, y=0.10, w=0.20, h=0.30)
    assert face.bbox_x == pytest.approx(0.25)
    assert face.bbox_y == pytest.approx(0.10)
    assert face.bbox_w == pytest.approx(0.20)
    assert face.bbox_h == pytest.approx(0.30)


def test_the_sort_key_is_the_box_centre() -> None:
    face = _one(x=0.20, w=0.40)
    assert face.center_x() == pytest.approx(0.40)


# ---------------------------------------------------------------------------
# Shape, ordering and the measurement the contract is waiting for
# ---------------------------------------------------------------------------
def test_a_frame_is_always_max_faces_wide() -> None:
    for count in (0, 1, 2, 3):
        observations = [_StubObservation(x=0.1 * i) for i in range(count)]
        frame = face_frame_from_observations(observations, 1, 0.0, 1280, 720)
        assert len(frame.faces) == MAX_FACES


def test_faces_come_out_ordered_left_to_right() -> None:
    """Vision's order is not stable between frames, so `f0` would otherwise swap
    between two people - the defect DESIGN.md 6.3 fixes for hands with chirality,
    which a face does not have."""
    observations = [_StubObservation(x=0.8, w=0.1), _StubObservation(x=0.1, w=0.1)]
    frame = face_frame_from_observations(observations, 1, 0.0, 1280, 720)
    assert frame.faces[0].bbox_x == pytest.approx(0.1)
    assert frame.faces[1].bbox_x == pytest.approx(0.8)


def test_the_regions_are_read_even_though_their_points_are_not_published() -> None:
    """The conversion reads all 12 regions now, so that filling in the point counts
    is the only change needed later - and so that the probe tool has something to
    report."""
    counts = {name: index + 1 for index, name in enumerate(FACE_REGION_NAMES)}
    face = _one(landmark_counts=counts)
    assert dict((name, len(points))
                for name, points in face.landmarks) == counts


def test_region_point_counts_reports_the_first_found_face() -> None:
    """What `tools/probe_face_regions.py` prints. One face's counts, not a merge of
    several, because the counts are a property of the constellation and a second
    face would only ever confirm or contradict."""
    counts = {name: 3 for name in FACE_REGION_NAMES}
    frame = face_frame_from_observations(
        [_StubObservation(landmark_counts=counts)], 1, 0.0, 1280, 720)
    assert region_point_counts(frame) == counts


def test_region_point_counts_is_empty_when_nothing_was_seen() -> None:
    """{} rather than a dictionary of zeros: a measurement that did not happen must
    not look like a measurement that came back empty-handed."""
    from appletd.face_types import blank_face_frame

    assert region_point_counts(blank_face_frame()) == {}


def test_an_observation_with_no_landmarks_still_yields_a_face() -> None:
    """`landmarks()` is nil until the landmarks request has run. Head pose and the
    box are still real, and that is most of what this stream publishes today."""
    face = _one(landmark_counts=None, roll=0.0)
    assert face.found is True
    assert face.landmarks == ()


# ---------------------------------------------------------------------------
# What `--keypoints` prints
#
# It changes no constant - the nose tip is found by intersection at runtime - so
# what these check is that a REPORT nobody can verify by eye says the right thing.
# ---------------------------------------------------------------------------
def test_the_keypoint_report_says_which_index_the_nose_tip_occupies() -> None:
    """The one number in the report that is genuinely hard to get any other way, and
    the reassurance that the intersection landed where a person would have pointed."""
    from appletd.synth_face import FacePose, synthetic_face_frame
    report = keypoint_report(synthetic_face_frame((FacePose(),)))
    indices = report["nose_tip_indices"]
    assert isinstance(indices, dict)
    assert set(indices) == {"nose", "nose_crest", "median_line"}
    assert all(index is not None for index in indices.values())
    # The synthetic template places the tip first in `nose` and last in the six-point
    # `nose_crest`; if that stops being true the template has been redrawn, and the
    # report is what would say so.
    assert indices["nose"] == 0
    assert indices["nose_crest"] == 5


def test_the_keypoint_report_answers_which_pupil_is_at_larger_x() -> None:
    """The open question in face_types.py, and the only thing in this report worth
    pasting anywhere. The synthetic face is built with the SUBJECT's left at larger
    x, so this is what a real face should also say if Vision names regions from the
    subject's point of view."""
    from appletd.synth_face import FacePose, synthetic_face_frame
    report = keypoint_report(synthetic_face_frame((FacePose(),)))
    assert report["left_pupil_is_right_of_image_centre"] is True


def test_the_keypoint_report_is_empty_when_nothing_was_seen() -> None:
    """{} rather than four zeroed points: a measurement that did not happen must not
    look like one that came back empty-handed. Same rule as `region_point_counts`."""
    from appletd.face_types import blank_face_frame
    assert keypoint_report(blank_face_frame()) == {}


def test_an_unanswerable_pupil_question_comes_back_as_None() -> None:
    """Not False. A face whose pupils Vision did not report cannot say which side
    `leftPupil` is on, and a confident False would be read as an answer."""
    counts = dict.fromkeys(FACE_REGION_NAMES, 3)
    counts["left_pupil"] = 0
    frame = face_frame_from_observations(
        [_StubObservation(landmark_counts=counts)], 1, 0.0, 1280, 720)
    report = keypoint_report(frame)
    assert report["left_pupil_is_right_of_image_centre"] is None
