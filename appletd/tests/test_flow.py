"""Optical flow, without a camera.

Synthetic frames shifted a known number of pixels, which is enough to pin the two
things a consumer has to get right: the sign convention and the packing.
"""

from __future__ import annotations

import time

import numpy
import numpy.typing
import pytest

from appletd.engine import EngineError, pixel_buffer_from_bgra
from appletd.flow import COMPONENTS, FlowDetector, FlowImage


def _textured(width: int, height: int,
              seed: int = 7) -> numpy.typing.NDArray[numpy.uint8]:
    """Texture, not a flat square. Flow on a textureless region is ill-posed - the
    aperture problem - and a test built on one measures the solver's guess."""
    rng = numpy.random.default_rng(seed)
    frame = rng.integers(0, 255, (height, width, 4), dtype=numpy.uint8)
    frame[..., 3] = 255
    return frame


def _pair(shift: int = 6) -> tuple[numpy.typing.NDArray[numpy.uint8],
                                   numpy.typing.NDArray[numpy.uint8]]:
    base = _textured(128, 128)
    return base, numpy.roll(base, shift, axis=1)


def test_the_first_frame_has_nothing_to_compare_to() -> None:
    """And that is not a failure. A stream that starts correctly must not look like
    one that is broken."""
    detector = FlowDetector("low")
    base, _moved = _pair()
    assert detector.detect_pixel_buffer(
        pixel_buffer_from_bgra(base), 1, time.monotonic()) is None
    assert detector.failures == 0


def test_a_field_comes_back_packed_and_the_right_size() -> None:
    detector = FlowDetector("low")
    base, moved = _pair()
    detector.detect_pixel_buffer(pixel_buffer_from_bgra(base), 1, time.monotonic())
    image = detector.detect_pixel_buffer(
        pixel_buffer_from_bgra(moved), 2, time.monotonic())
    assert isinstance(image, FlowImage)
    assert (image.width, image.height) == (128, 128)
    # Two float32 components a pixel, un-padded.
    assert len(image.pixels) == image.width * image.height * COMPONENTS * 4


def test_the_flow_is_BACKWARD_and_a_consumer_depends_on_it() -> None:
    """MEASURED 2026-09-07: content moved +6 px in x reads about -5.5. The vector
    points from the current frame back to where the content came from, which is what
    a warp wants - and the opposite of what "how did it move" suggests."""
    detector = FlowDetector("medium")
    base, moved = _pair(shift=6)
    detector.detect_pixel_buffer(pixel_buffer_from_bgra(base), 1, time.monotonic())
    image = detector.detect_pixel_buffer(
        pixel_buffer_from_bgra(moved), 2, time.monotonic())
    assert image is not None
    field = numpy.frombuffer(image.pixels, dtype=numpy.float32).reshape(
        image.height, image.width, COMPONENTS)
    middle = field[40:88, 40:88]
    assert middle[..., 0].mean() < -3.0, "x flow should be negative and substantial"
    assert abs(float(middle[..., 1].mean())) < 1.0, "nothing moved in y"


def test_an_unknown_accuracy_is_refused_by_name() -> None:
    with pytest.raises(EngineError, match="accuracy must be one of"):
        FlowDetector("ludicrous")
