"""Manual text mode: the request runs once per trigger and not otherwise.

The engine is built with a stub detector and `_publish_text` is called directly, so
no camera, no Vision request and no frame are involved - only the gate.
"""

from __future__ import annotations

import pytest

pytest.importorskip("Vision")

from appletd.engine import HandEngine
from appletd.text_types import TextFrame, blank_text_frame


class _StubTextDetector:
    def __init__(self) -> None:
        self.calls = 0
        self.last_inference_ms = 0.0

    def detect_sample_buffer(self, sample_buffer: object, seq: int, captured_at: float,
                             width_px: int, height_px: int,
                             orientation: int = 1) -> TextFrame:
        self.calls += 1
        return blank_text_frame(seq=seq, slots=2)


def _engine(mode: str) -> tuple[HandEngine, _StubTextDetector, list[TextFrame]]:
    detector = _StubTextDetector()
    published: list[TextFrame] = []
    engine = HandEngine(on_frame=lambda _f: None, hands=False,
                        text_detector=detector,  # type: ignore[arg-type]
                        on_text=published.append, text_mode=mode)
    return engine, detector, published


def test_auto_runs_on_every_frame() -> None:
    engine, detector, published = _engine("auto")
    for _ in range(3):
        engine._publish_text(None, 0.0, 1)
    assert detector.calls == 3 and len(published) == 3


def test_manual_runs_nothing_until_armed_and_then_exactly_once() -> None:
    engine, detector, published = _engine("manual")
    for _ in range(5):
        engine._publish_text(None, 0.0, 1)
    assert detector.calls == 0 and published == []
    engine.arm_text_once()
    assert engine.text_armed
    engine._publish_text(None, 0.0, 1)
    engine._publish_text(None, 0.0, 1)
    assert detector.calls == 1 and len(published) == 1
    assert not engine.text_armed


def test_the_mode_switches_live_and_a_pending_trigger_survives_it() -> None:
    engine, detector, _published = _engine("auto")
    engine.set_text_mode("manual")
    engine._publish_text(None, 0.0, 1)
    assert detector.calls == 0
    engine.arm_text_once()
    engine.set_text_mode("auto")
    engine._publish_text(None, 0.0, 1)
    assert detector.calls == 1 and not engine.text_armed     # consumed, not lost
    engine.set_text_mode("manual")
    engine._publish_text(None, 0.0, 1)
    assert detector.calls == 1
