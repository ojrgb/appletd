"""The person boxes have a PRODUCER, not just a contract.

They shipped without one. `pose_types.py` had the `HumanRect` type, the 13 channel
names, the roles, the trim patterns and a section in `ATTRIBUTES.md` — and nothing ever
constructed `VNDetectHumanRectanglesRequest`, so every `human*` channel published 0.0
for ever while `sc_pose` read 1.0. Every test passed, because they all asserted the
SHAPE of the channel list, which was correct.

So these tests assert the producer exists and reaches the frame. The shape is already
covered by `test_pose_types.py`; duplicating it here would repeat the mistake.

Ref: appletd/pose.py.
"""

from __future__ import annotations

import pytest

from appletd.pose_types import BLANK_HUMAN, MAX_BODIES, HumanRect, order_humans
from appletd.types import Confidence, NormX, NormY


def test_the_detector_builds_the_rectangles_request() -> None:
    """The defect in one line: this attribute did not exist.

    The ONLY test here that needs pyobjc, so the skip is on it rather than on the
    module. A module-level `importorskip` turned all five green-by-absence wherever
    Vision is missing - which is the shape of failure this file exists to catch.
    """
    pytest.importorskip("Vision")
    from appletd.pose import PoseDetector

    detector = PoseDetector()
    assert detector._rect_request is not None
    assert detector.rect_revision >= 1


def test_the_converter_reads_a_box_bottom_left() -> None:
    """CGRect's origin IS the bottom-left corner and `boundingBox` is already
    normalised, so nothing here flips or scales. A stand-in observation, because the
    real one needs a person."""

    class _Origin:
        x, y = 0.25, 0.10

    class _Size:
        width, height = 0.30, 0.80

    class _Box:
        origin, size = _Origin(), _Size()

    class _Observation:
        def boundingBox(self) -> _Box:
            return _Box()

        def confidence(self) -> float:
            return 0.75

    from appletd.pose import human_from_observation

    human = human_from_observation(_Observation())
    assert (human.x, human.y, human.w, human.h) == (0.25, 0.10, 0.30, 0.80)
    assert human.confidence == 0.75
    assert human.found is True


def test_the_frame_carries_what_the_request_returned() -> None:
    """`pose_frame_from_observations` used to take no rectangles at all, so it always
    filled the slots with BLANK_HUMAN."""
    from appletd.pose import pose_frame_from_observations

    class _Observation:
        def __init__(self, x: float) -> None:
            self._x = x

        def boundingBox(self) -> object:
            origin = type("O", (), {"x": self._x, "y": 0.1})()
            size = type("S", (), {"width": 0.2, "height": 0.5})()
            return type("B", (), {"origin": origin, "size": size})()

        def confidence(self) -> float:
            return 0.9

    frame, unreadable = pose_frame_from_observations(
        [], seq=1, captured_at=0.0, width_px=1280, height_px=720,
        rectangles=[_Observation(0.7), _Observation(0.2)])
    assert unreadable == 0
    assert sum(1 for h in frame.humans if h.found) == 2
    # leftmost first, like every other slot in this system
    assert frame.humans[0].x == pytest.approx(0.2)
    assert frame.humans[1].x == pytest.approx(0.7)


def test_no_rectangles_still_fills_every_slot() -> None:
    """Absent publishes zeros with the channels present, not a short list."""
    from appletd.pose import pose_frame_from_observations

    frame, unreadable = pose_frame_from_observations(
        [], seq=1, captured_at=0.0, width_px=1280, height_px=720)
    assert unreadable == 0
    assert len(frame.humans) == MAX_BODIES
    assert all(human == BLANK_HUMAN for human in frame.humans)


def test_ordering_puts_empty_slots_last() -> None:
    found = HumanRect(x=NormX(0.6), y=NormY(0.1), w=0.1, h=0.5,
                      confidence=Confidence(0.8), found=True)
    ordered = order_humans([BLANK_HUMAN, found])
    assert ordered[0] is found
    assert ordered[1] == BLANK_HUMAN
