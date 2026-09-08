"""Optical flow: `VNTrackOpticalFlowRequest` -> two floats per pixel.

WHAT IT PRODUCES, MEASURED rather than assumed: a `VNPixelBufferObservation`
whose buffer is pixel format `0x32433066` - `'2C0f'`, TwoComponent32Float. Eight bytes a
pixel: the x and y displacement from the PREVIOUS frame to this one, in pixels of the
input image.

So it is an IMAGE and not channels, and it goes down the same shared-memory path the
mask and the depth map use. `appletd/maskbuf.py` already carries a component count and
a dtype in its header, and already supports `DTYPE_F32`, so this needed no new
transport.

TWO FRAMES OR NOTHING. Flow is a comparison, so the first frame after a start produces
no observation at all - that is not a failure and is not counted as one. Anything
downstream sees the first flow arrive one frame late.

SIGN AND ORIGIN. The values are in the image's own coordinates, which for CoreVideo is
TOP-LEFT origin - unlike every channel this project publishes, which are bottom-left
(`DESIGN.md` 7). Nothing here flips it: a flow field is consumed as an image, by a TOP,
and flipping the y COMPONENT without flipping the image would be a field that disagrees
with the picture it describes. The TOP side is where that decision belongs.

Thread: NOT thread-safe, exactly like the other detectors. One detector, one serial
        capture queue - the sequence handler holds Vision's inter-frame state, which
        for this request is the whole point.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final, NamedTuple

import Quartz
import Vision

from appletd.engine import EngineError
from appletd.streams import FLOW_ACCURACIES, FLOW_COMPONENTS, ORIENTATION_UP

if TYPE_CHECKING:  # pragma: no cover - typing only
    from objc import ObjCObject

# `'2C0f'` - two 32-bit floats per pixel. Checked rather than assumed, because reading
# a different format as this one produces a plausible, wrong field.
TWO_COMPONENT_32_FLOAT: Final = 0x32433066
COMPONENTS: Final = FLOW_COMPONENTS
BYTES_PER_PIXEL: Final = 8

# The names live in `streams.py`, which is importable with no pyobjc - the builder and
# the sidecar's command line both need them and neither can import this module. Only
# the MAPPING to `VNTrackOpticalFlowRequestComputationAccuracy` belongs here.
_ACCURACY_VALUES: Final = {"low": 0, "medium": 1, "high": 2, "veryhigh": 3}


class FlowImage(NamedTuple):
    """One flow field, un-padded and packed. `pixels` is `width * height * 8` bytes."""

    width: int
    height: int
    pixels: bytes
    seq: int
    captured_at: float
    inference_ms: float


def verify_flow_support() -> None:
    if getattr(Vision, "VNTrackOpticalFlowRequest", None) is None:
        raise EngineError(
            "this Vision has no VNTrackOpticalFlowRequest - optical flow needs "
            "macOS 14 or newer.")


def flow_bytes_from_pixel_buffer(pixel_buffer: ObjCObject) -> tuple[int, int, bytes]:
    """`(width, height, packed_pixels)` from a TwoComponent32Float buffer.

    The same shape as `segmentation.mask_bytes_from_pixel_buffer`, and the same two
    reasons for every line of it: the buffer belongs to Vision and is valid only while
    locked, so what outlives the lock is a copy; and `bytesPerRow` may exceed the row's
    real width, so the padding is removed or the field shears.
    """
    fmt = int(Quartz.CVPixelBufferGetPixelFormatType(pixel_buffer))
    if fmt != TWO_COMPONENT_32_FLOAT:
        raise EngineError(
            "optical flow is pixel format %#x, not TwoComponent32Float (%#x). "
            "Reading it as two floats would produce a plausible wrong field"
            % (fmt, TWO_COMPONENT_32_FLOAT))

    width = int(Quartz.CVPixelBufferGetWidth(pixel_buffer))
    height = int(Quartz.CVPixelBufferGetHeight(pixel_buffer))
    stride = int(Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer))
    row_bytes = width * BYTES_PER_PIXEL

    Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, 1)     # 1 = read-only
    try:
        base = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
        if base is None:
            raise EngineError("optical flow buffer has no base address while locked")
        raw = bytes(base.as_buffer(stride * height))
    finally:
        Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, 1)

    if stride == row_bytes:
        return width, height, raw
    return width, height, b"".join(
        raw[row * stride:row * stride + row_bytes] for row in range(height))


class FlowDetector:
    """Holds the request and the sequence handler that carries frame-to-frame state."""

    def __init__(self, accuracy: str = "medium") -> None:
        if accuracy not in FLOW_ACCURACIES:
            raise EngineError("accuracy must be one of %s, not %r"
                              % (", ".join(FLOW_ACCURACIES), accuracy))
        verify_flow_support()
        self._sequence = Vision.VNSequenceRequestHandler.alloc().init()
        # `initWithCompletionHandler_`: `init` is NS_UNAVAILABLE on this class too,
        # exactly like the segmentation requests. MEASURED - `alloc().init()` raises
        # `TypeError: 'init' is NS_UNAVAILABLE`.
        self._request = (Vision.VNTrackOpticalFlowRequest.alloc()
                         .initWithCompletionHandler_(None))
        self._request.setComputationAccuracy_(_ACCURACY_VALUES[accuracy])
        self.accuracy = accuracy
        self.revision = int(self._request.revision())
        self.last_inference_ms = 0.0
        self.failures = 0
        self.frames_seen = 0

    def detect_pixel_buffer(self, pixel_buffer: ObjCObject, seq: int,
                            captured_at: float,
                            orientation: int = ORIENTATION_UP) -> FlowImage | None:
        started_s = time.perf_counter()
        ok, err = self._sequence.performRequests_onCVPixelBuffer_orientation_error_(
            [self._request], pixel_buffer, orientation, None)            # TRAP: out-param
        self.last_inference_ms = (time.perf_counter() - started_s) * 1e3
        if not ok:
            raise EngineError("Vision performRequests failed: %s" % (err,))
        return self._flow_from_results(seq, captured_at)

    def detect_sample_buffer(self, sample_buffer: ObjCObject, seq: int,
                             captured_at: float,
                             orientation: int = ORIENTATION_UP) -> FlowImage | None:
        started_s = time.perf_counter()
        ok, err = self._sequence.performRequests_onCMSampleBuffer_orientation_error_(
            [self._request], sample_buffer, orientation, None)           # TRAP: out-param
        self.last_inference_ms = (time.perf_counter() - started_s) * 1e3
        if not ok:
            raise EngineError("Vision performRequests failed: %s" % (err,))
        return self._flow_from_results(seq, captured_at)

    def _flow_from_results(self, seq: int, captured_at: float) -> FlowImage | None:
        self.frames_seen += 1
        results = list(self._request.results() or [])
        if not results:
            # THE FIRST FRAME HAS NOTHING TO COMPARE TO, so it legitimately produces
            # no observation. Counted as a failure only after that, or a stream that
            # starts correctly looks like one that is broken.
            if self.frames_seen > 1:
                self.failures += 1
            return None
        buffer = results[0].pixelBuffer()
        if buffer is None:
            self.failures += 1
            return None
        width, height, pixels = flow_bytes_from_pixel_buffer(buffer)
        return FlowImage(width, height, pixels, seq, captured_at,
                         self.last_inference_ms)
