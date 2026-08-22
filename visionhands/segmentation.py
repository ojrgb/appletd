"""Person segmentation: `VNGeneratePersonSegmentationRequest` -> a mask of bytes.

WHAT THIS OWNS. One Vision request and the extraction of its mask into plain bytes.
It owns neither the camera nor the transport: `engine.py` owns the capture session
and hands this module a buffer that has already been delivered, and
`visionhands/maskbuf.py` owns getting the bytes to TouchDesigner. So this file is the
only place that knows anything about Vision's pixel formats, and `maskbuf.py` is the
only place that knows anything about sharing memory. Neither imports the other.

WHY IT IS SHAPED LIKE face.py AND pose.py. Same reason and the same boundary rules:
this is a plug-in on top of engine.py, engine.py does not import it, and a
segmentation failure cannot break the hands path that a live project is using.

THE BOUNDARY RULES, which are not stylistic:

  * imports nothing from TouchDesigner - not `td`, not `op`;
  * NO pyobjc object escapes the capture thread. A `CVPixelBuffer` handed to
    another thread would be released by whichever thread dropped the last
    reference, which is a crash that arrives weeks later (DESIGN.md 4.2). What
    leaves this module is `bytes`, and that is the whole reason the mask is COPIED
    out of the buffer here rather than passed along as a buffer.

THE FORMAT, and it is CHECKED rather than assumed. The request has a readable
`outputPixelFormat`, which reports `0x4c303038` - `L008`, one 8-bit component per
pixel, 0 for background and 255 for person with soft edges between. So the claim is
verified at construction instead of being a comment that might have gone stale, and
`mask_bytes_from_pixel_buffer` checks the buffer it is actually handed as well.
`supportedRevisions()` is `[1]`: revision 1 is the only one there is. Three quality
levels, and they change the OUTPUT SIZE as well as the cost, which is why
`maskbuf.py` carries the live geometry in its header instead of assuming it
(BUILD_PLAN step 9).

CONSTRUCTION IS NOT LIKE THE OTHER REQUESTS, and this cost a probe to find:
`init` and `new` are both **NS_UNAVAILABLE** on this class, where every other Vision
request in this project is `alloc().init()`. The designated initialiser is
`initWithCompletionHandler_`, and nil is the right handler here - with a
`VNSequenceRequestHandler` the results are on the request object synchronously once
`performRequests` returns, so a completion block would be a second path to the same
data.

ROW PADDING IS THE TRAP. `CVPixelBufferGetBytesPerRow` is NOT
`width * bytesPerPixel`: CoreVideo aligns rows, and at 256 wide the padding has been
observed as zero while at 253 it is not. Reading `height * bytesPerRow` bytes and
handing them on as a packed image gives a picture that shears progressively down the
frame - plausible enough on screen to be mistaken for a bad mask. So this module
un-pads, row by row, and its tests assert that it does.

THREADS. Every function here runs on the GCD capture queue, called from the engine's
sample-buffer callback, except `verify_segmentation_support` and
`SegmentationDetector.__init__`, which run on the caller's thread at construction so
a framework mismatch is reported where it can be acted on.

Ref: docs/BUILD_PLAN.md step 9 (the transport decision and the L008 finding),
     visionhands/maskbuf.py (where the bytes go), DESIGN.md 6.4 (the stream contract).
"""

from __future__ import annotations

import time
from typing import Final, NamedTuple

import Quartz
import Vision

from visionhands.engine import EngineError, ObjCObject

# The three quality levels, by their Vision constant names. Looked up by name at
# construction rather than hardcoded to an integer, so a framework that does not
# have one says so instead of silently selecting a different level.
QUALITY_LEVELS: Final[tuple[str, ...]] = ("accurate", "balanced", "fast")
_QUALITY_CONSTANTS: Final[dict[str, str]] = {
    "accurate": "VNGeneratePersonSegmentationRequestQualityLevelAccurate",
    "balanced": "VNGeneratePersonSegmentationRequestQualityLevelBalanced",
    "fast": "VNGeneratePersonSegmentationRequestQualityLevelFast",
}

# `kCVPixelFormatType_OneComponent8`. Spelled as the integer as well as named,
# because the four-character code is what appears in a debugger and in Vision's own
# logs, and 'L008' being 0x4C303038 is the fact that makes those legible.
ONE_COMPONENT_8: Final = 0x4C303038

# Vision's own default quality is ACCURATE (the constant is 0, and a fresh request
# reports it). This module defaults to `balanced` instead - a deliberate deviation,
# because the cost of each level on this machine is still unmeasured and the user's
# one datum is a C++ plugin pinning the frame rate at 50 fps, which the default being
# Accurate now supports. Changed only with a measurement, not a preference.
VISION_DEFAULT_QUALITY: Final = "accurate"


class MaskImage(NamedTuple):
    """One mask, un-padded and packed. `pixels` is `width * height` bytes.

    `bytes` and not a buffer, deliberately - see the module docstring on why no
    pyobjc object may cross a thread boundary here.
    """

    width: int
    height: int
    pixels: bytes
    seq: int
    captured_at: float
    inference_ms: float

    @property
    def coverage(self) -> float:
        """Fraction of the frame that is not background, 0.0 to 1.0.

        A cheap sanity signal rather than a real measurement: it is what tells you
        the difference between "the mask is empty" and "the mask is the whole frame",
        both of which look like a plausible image and neither of which is useful.
        """
        if not self.pixels:
            return 0.0
        return sum(1 for value in self.pixels if value >= 128) / len(self.pixels)


def verify_segmentation_support() -> None:
    """Fail at construction if this Vision cannot do what the rest of the file assumes.

    Contract: raises EngineError naming everything missing, rather than the first
              thing missing. A framework mismatch is usually a version, and one
              message listing four absent symbols identifies it; four runs each
              reporting one does not.
    """
    problems = []
    request_class = getattr(Vision, "VNGeneratePersonSegmentationRequest", None)
    if request_class is None:
        raise EngineError("this Vision has no VNGeneratePersonSegmentationRequest - "
                          "person segmentation needs macOS 12 or later")
    for level, constant in _QUALITY_CONSTANTS.items():
        if not hasattr(Vision, constant):
            problems.append("no %s, so quality level %r cannot be selected"
                            % (constant, level))
    if not hasattr(Vision, "VNPixelBufferObservation"):
        problems.append("no VNPixelBufferObservation, which is the result type")
    if problems:
        raise EngineError("this Vision cannot be trusted for segmentation: %s"
                          % "; ".join(problems))


def mask_bytes_from_pixel_buffer(pixel_buffer: ObjCObject) -> tuple[int, int, bytes]:
    """`(width, height, packed_pixels)` from a one-component CoreVideo buffer.

    Contract: raises on a pixel format this cannot read, rather than reinterpreting
              the bytes. A two-component buffer read as one is an image, and it is
              the wrong image.
    Why it copies: the buffer belongs to Vision and is only valid while locked, so
              anything that outlives the lock has to be a copy. The copy is 37 KB at
              256x144 and is the cheapest part of the frame.

    ROW PADDING is un-done here. `bytesPerRow` may exceed `width`, and handing the
    padded bytes on as an image shears it - see the module docstring.
    """
    fmt = int(Quartz.CVPixelBufferGetPixelFormatType(pixel_buffer))
    if fmt != ONE_COMPONENT_8:
        raise EngineError(
            "segmentation mask is pixel format %#x (%r), not OneComponent8 (%#x). "
            "Reading it as one 8-bit component would produce a plausible wrong image"
            % (fmt, _four_cc(fmt), ONE_COMPONENT_8))

    width = int(Quartz.CVPixelBufferGetWidth(pixel_buffer))
    height = int(Quartz.CVPixelBufferGetHeight(pixel_buffer))
    stride = int(Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer))

    # `kCVPixelBufferLock_ReadOnly` is 1. Locked for the shortest possible span: the
    # base address is only valid while the lock is held, and Vision may be reusing
    # this buffer for the next frame the moment it is released.
    Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, 1)
    try:
        base = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
        if base is None:
            raise EngineError("segmentation buffer has no base address while locked")
        raw = bytes(base.as_buffer(stride * height))
    finally:
        Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, 1)

    if stride == width:
        return width, height, raw
    # Un-pad. Done as one join of slices rather than a loop of concatenations,
    # because the loop is quadratic and this runs per frame.
    return width, height, b"".join(
        raw[row * stride:row * stride + width] for row in range(height))


def _four_cc(value: int) -> str:
    """A CoreVideo format code as its four characters, for an error message.

    Not decoration: `0x34323076` means nothing and `'420v'` is immediately
    recognisable as the camera's native format, which is the mistake most likely to
    reach this function.
    """
    try:
        text = value.to_bytes(4, "big").decode("ascii")
    except (OverflowError, UnicodeDecodeError):
        return "?"
    return text if text.isprintable() else "?"


class SegmentationDetector:
    """Holds Vision's segmentation request state and turns buffers into MaskImages.

    Thread: NOT thread-safe, and does not need to be. One detector belongs to one
            serial capture queue, so its request object is only ever touched by one
            thread at a time. Sharing one across two queues would corrupt Vision's
            inter-frame state.
    Why one VNSequenceRequestHandler for the whole session: the sequence handler is
            what carries Vision's inter-frame state, and rebuilding it per frame
            throws that away (DESIGN.md 2.3). The same reasoning as HandDetector.
    """

    def __init__(self, quality: str = "balanced") -> None:
        if quality not in QUALITY_LEVELS:
            raise EngineError("quality must be one of %s, not %r"
                              % (", ".join(QUALITY_LEVELS), quality))
        verify_segmentation_support()
        self._sequence = Vision.VNSequenceRequestHandler.alloc().init()
        # `initWithCompletionHandler_`, NOT `alloc().init()`: `init` and `new` are
        # both NS_UNAVAILABLE on this class, unlike every other request here. See
        # the module docstring. Nil handler, because the sequence handler puts the
        # results on the request object synchronously.
        self._request = (Vision.VNGeneratePersonSegmentationRequest.alloc()
                         .initWithCompletionHandler_(None))
        self._request.setQualityLevel_(getattr(Vision, _QUALITY_CONSTANTS[quality]))
        # Recorded rather than set. `supportedRevisions()` is [1], so pinning it
        # would be pinning to the only option and would hide a future macOS adding
        # another - which is exactly what the stats are for.
        self.revision = int(self._request.revision())
        # The format claim, CHECKED. It is readable and settable, so leaving it as a
        # docstring promise would be leaving a comment to go stale (STANDARDS.md 1.5).
        self.output_format = int(self._request.outputPixelFormat())
        if self.output_format != ONE_COMPONENT_8:
            raise EngineError(
                "this Vision's segmentation output format is %#x (%r), not "
                "OneComponent8 (%#x) - maskbuf.py carries one byte per pixel and "
                "would publish a wrong image" % (self.output_format,
                                                 _four_cc(self.output_format),
                                                 ONE_COMPONENT_8))
        self.quality = quality
        self.last_inference_ms = 0.0
        self.failures = 0

    def detect_pixel_buffer(self, pixel_buffer: ObjCObject, seq: int,
                            captured_at: float) -> MaskImage | None:
        """The replay path: a CVPixelBuffer, from a decoded fixture or a converted frame.

        Contract: returns None when Vision produced no observation, and COUNTS it in
                  `self.failures`, so an empty stream is distinguishable from a
                  stream nobody asked for. Raises only when Vision itself failed,
                  which is a different thing from finding nobody in frame.
        """
        started_s = time.perf_counter()
        ok, err = self._sequence.performRequests_onCVPixelBuffer_error_(
            [self._request], pixel_buffer, None)            # TRAP: out-param
        self.last_inference_ms = (time.perf_counter() - started_s) * 1e3
        if not ok:
            raise EngineError("Vision performRequests failed: %s" % (err,))
        return self._mask_from_results(seq, captured_at)

    def detect_sample_buffer(self, sample_buffer: ObjCObject, seq: int,
                             captured_at: float) -> MaskImage | None:
        """The live path: a CMSampleBuffer straight from the camera, unconverted.

        The camera's native 420v goes into Vision with no conversion at all -
        converting to BGRA first would cost a full-frame copy for nothing
        (DESIGN.md 3). Only the MASK comes back as one component.
        """
        started_s = time.perf_counter()
        ok, err = self._sequence.performRequests_onCMSampleBuffer_error_(
            [self._request], sample_buffer, None)           # TRAP: out-param
        self.last_inference_ms = (time.perf_counter() - started_s) * 1e3
        if not ok:
            raise EngineError("Vision performRequests failed: %s" % (err,))
        return self._mask_from_results(seq, captured_at)

    def _mask_from_results(self, seq: int, captured_at: float) -> MaskImage | None:
        results = list(self._request.results() or [])
        if not results:
            # Not an error. Vision returns no observation when it finds nobody, and
            # a caller publishing a blank mask for that is making the right choice -
            # but it has to know, hence the counter.
            self.failures += 1
            return None
        buffer = results[0].pixelBuffer()
        if buffer is None:
            self.failures += 1
            return None
        width, height, pixels = mask_bytes_from_pixel_buffer(buffer)
        return MaskImage(width, height, pixels, seq, captured_at,
                         self.last_inference_ms)
