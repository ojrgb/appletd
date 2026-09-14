"""Text recognition over a synthetic image, no camera.

Text drawn onto a 1280x720 frame is enough to pin what a consumer depends on: the box
origin, the reading order, the slot cap, and that the language pinning actually took.
The cost bound is deliberately loose - it exists to catch `accurate` becoming the
default by accident, not to re-measure the 5.92 ms in docs/BENCHMARKS.md.
"""

from __future__ import annotations

import statistics
import time

import numpy
import numpy.typing
import pytest

pytest.importorskip("Vision")
Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")
ImageFont = pytest.importorskip("PIL.ImageFont")

from appletd.engine import EngineError, pixel_buffer_from_bgra  # noqa: E402
from appletd.ocr import TextDetector, parse_languages, supported_languages  # noqa: E402
from appletd.text_types import BLANK_LINE  # noqa: E402

LINES = ("EXIT", "Platform 4", "No entry after 22:00", "Mind the gap")
# Generous: the MEASURED figure is 5.92 ms and the suite shares the Neural Engine
# with the depth model's tests, but `accurate` sits near 50.
MAX_FAST_MS = 30.0


def _frame(lines: tuple[str, ...] = LINES, width: int = 1280,
           height: int = 720) -> numpy.typing.NDArray[numpy.uint8]:
    image = Image.new("RGB", (width, height), (235, 235, 230))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=44)
    y = 60
    for line in lines:
        draw.text((80, y), line, fill=(20, 20, 20), font=font)
        y += 120
    rgb = numpy.asarray(image, dtype=numpy.uint8)
    bgra = numpy.zeros((height, width, 4), dtype=numpy.uint8)
    bgra[..., 0], bgra[..., 1], bgra[..., 2], bgra[..., 3] = (
        rgb[..., 2], rgb[..., 1], rgb[..., 0], 255)
    return numpy.ascontiguousarray(bgra)


def test_lines_come_back_top_first_with_bottom_left_boxes() -> None:
    detector = TextDetector(slots=8)
    frame = detector.detect_pixel_buffer(pixel_buffer_from_bgra(_frame()), 1,
                                         time.monotonic(), 1280, 720)
    texts = [line.text for line in frame.lines if line.found]
    assert texts == list(LINES), texts
    assert frame.found == len(LINES) and frame.slots == 8
    top = frame.lines[0]
    # The first line was drawn 60 px from the TOP, so in a bottom-left frame its box
    # sits near y = 1. Read as top-left it would sit near 0.
    assert top.y1 > 0.8, top
    assert 0 < top.x1 < top.x2 < 0.3
    assert top.y1 < top.y2
    # And the rows descend the image.
    tops = [line.y2 for line in frame.lines if line.found]
    assert tops == sorted(tops, reverse=True)
    assert all(line is BLANK_LINE for line in frame.lines[len(LINES):])


def test_the_slot_cap_drops_lines_and_counts_them() -> None:
    detector = TextDetector(slots=2)
    frame = detector.detect_pixel_buffer(pixel_buffer_from_bgra(_frame()), 1,
                                         time.monotonic(), 1280, 720)
    assert len(frame.lines) == 2
    assert frame.found == len(LINES)
    assert detector.n_lines_dropped == len(LINES) - 2


def test_an_empty_image_publishes_a_blank_frame_of_the_full_shape() -> None:
    detector = TextDetector(slots=3)
    frame = detector.detect_pixel_buffer(pixel_buffer_from_bgra(_frame(())), 1,
                                         time.monotonic(), 1280, 720)
    assert frame.found == 0 and len(frame.lines) == 3
    assert all(line is BLANK_LINE for line in frame.lines)


def test_the_language_is_pinned_and_the_level_is_fast_by_default() -> None:
    detector = TextDetector()
    assert detector.languages == ("en-US",)
    assert detector.level == "fast" and detector.correction is False
    assert parse_languages(" en-US, fr-FR ") == ("en-US", "fr-FR")
    assert parse_languages("") == ()
    assert "en-US" in supported_languages()


def test_fast_stays_fast() -> None:
    """A regression guard on the default, not a measurement."""
    detector = TextDetector()
    buffer = pixel_buffer_from_bgra(_frame())
    times = []
    for index in range(8):
        started = time.perf_counter()
        detector.detect_pixel_buffer(buffer, index, time.monotonic(), 1280, 720)
        if index:
            times.append((time.perf_counter() - started) * 1e3)
    assert statistics.median(times) < MAX_FAST_MS, times


def test_bad_arguments_are_refused_by_name() -> None:
    with pytest.raises(EngineError, match="level must be one of"):
        TextDetector(level="quick")
    with pytest.raises(EngineError, match="slots must be"):
        TextDetector(slots=0)
