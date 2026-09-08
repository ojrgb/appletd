"""Person segmentation: Vision's mask requests -> a mask of bytes.

Owns one Vision request and the extraction of its mask into plain bytes. It owns
neither the camera nor the transport: `engine.py` hands it a buffer that has already
been delivered, and `maskbuf.py` gets the bytes to TouchDesigner. So this is the only
file that knows Vision's pixel formats, and `maskbuf.py` the only one that knows about
sharing memory. Neither imports the other.

Two detectors behind one protocol. `SegmentationDetector` answers "is this pixel a
person" and gives 0 or 255; `InstanceSegmentationDetector` answers "WHICH person",
giving an instance index per pixel with 0 for background.

Boundary rules, not stylistic: imports nothing from TouchDesigner, and NO pyobjc
object escapes the capture thread - a `CVPixelBuffer` released by the wrong thread
crashes weeks later. What leaves here is `bytes`, which is why the mask is COPIED out
of the buffer rather than passed along.

The pixel format is CHECKED rather than assumed: reading a two-component buffer as one
produces an image, and it is the wrong image.

Thread: one detector, one serial capture queue. Not thread-safe.
Ref: DESIGN.md 2.18, 4.2.
"""

from __future__ import annotations

import time
from typing import Final, NamedTuple, Protocol, runtime_checkable

import Quartz
import Vision

from appletd.engine import EngineError, ObjCObject
from appletd.streams import ORIENTATION_UP, SEGMENT_QUALITIES

# The quality levels come from `streams.py`, which is pyobjc-free: `sidecar.py` needs
# the same list for its command line and must stay importable with no frameworks
# present, and `tools/td_build_vision.py` needs it for a menu inside TouchDesigner.
# One list, imported by everything that needs it.
QUALITY_LEVELS: Final[tuple[str, ...]] = SEGMENT_QUALITIES

# Their Vision constant NAMES, which are Vision's business and stay here. Looked up
# by name at construction rather than hardcoded to an integer, so a framework that
# does not have one says so instead of silently selecting a different level.
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


# A 256-byte lookup that is 1 where a SOFT mask says "more person than background".
# `bytes.translate` does the whole frame in one pass of C; the comprehension it
# replaces was 29.0 ms on a 1920x1080 mask against 6.4 for this.
_ABOVE_HALF: Final = bytes(1 if value >= 128 else 0 for value in range(256))


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
    # How many people the instance model separated, 0 for the single-person request.
    # Defaulted, so every existing construction is unchanged.
    people: int = 0

    @property
    def coverage(self) -> float:
        """Fraction of the frame that is not background, 0.0 to 1.0.

        A cheap sanity signal rather than a real measurement: it is what tells you
        the difference between "the mask is empty" and "the mask is the whole frame",
        both of which look like a plausible image and neither of which is useful.

        THE TWO MASKS MEAN DIFFERENT THINGS BY A BYTE, which is what was wrong here.
        A single-person mask is an alpha, 0..255, and half is the sensible line. An
        INSTANCE mask is a person index, 1..4 - so a `>= 128` threshold made this
        return 0.0 for every multi-person frame there has ever been, and the one
        signal for "is this mask empty" said empty exactly when it was not.
        """
        if not self.pixels:
            return 0.0
        if self.people:
            return (len(self.pixels) - self.pixels.count(0)) / len(self.pixels)
        return self.pixels.translate(_ABOVE_HALF).count(1) / len(self.pixels)


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


@runtime_checkable
class MaskDetector(Protocol):
    """What the engine needs of a segmentation detector, whichever request it holds.

    Two implementations: `SegmentationDetector` answers "is this pixel a person" and
    `InstanceSegmentationDetector` answers "which person is it". The engine drives
    them identically, so it should depend on the shape rather than on either class.
    """

    failures: int
    last_inference_ms: float

    def detect_pixel_buffer(self, pixel_buffer: ObjCObject, seq: int,
                            captured_at: float,
                            orientation: int = ORIENTATION_UP) -> MaskImage | None: ...

    def detect_sample_buffer(self, sample_buffer: ObjCObject, seq: int,
                             captured_at: float,
                             orientation: int = ORIENTATION_UP) -> MaskImage | None: ...


def verify_instance_segmentation_support() -> None:
    """`VNGeneratePersonInstanceMaskRequest` exists. macOS 14+."""
    if getattr(Vision, "VNGeneratePersonInstanceMaskRequest", None) is None:
        raise EngineError(
            "this Vision has no VNGeneratePersonInstanceMaskRequest - multi-person "
            "masks need macOS 14 or newer. Switch Multi-Person off to use the "
            "single-person request, which works everywhere this component runs.")


class InstanceSegmentationDetector:
    """People separated from each other, not just from the background.

    THE MASK MEANS SOMETHING DIFFERENT HERE, and that is the whole point.
    `VNGeneratePersonSegmentationRequest` answers "is this pixel a person" and gives
    0 or 255. This one answers "WHICH person is this pixel", and Vision hands that
    back natively as `instanceMask` - one component per pixel, carrying the instance
    INDEX, with 0 for background.

    So a project that wants the old union does `> 0` and has it. Going the other way
    is impossible, which is why the index form is what gets published.

    Vision separates at most FOUR people. `people` reports how many it actually
    found, so a project can tell "nobody" from "five in frame and one merged away".

    Thread: NOT thread-safe, exactly like `SegmentationDetector`. One detector, one
            serial capture queue.
    """

    def __init__(self) -> None:
        verify_instance_segmentation_support()
        self._sequence = Vision.VNSequenceRequestHandler.alloc().init()
        # `initWithCompletionHandler_` for the same reason the other request uses it:
        # `init` and `new` are NS_UNAVAILABLE on these classes.
        self._request = (Vision.VNGeneratePersonInstanceMaskRequest.alloc()
                         .initWithCompletionHandler_(None))
        self.revision = int(self._request.revision())
        # No quality level on this request - it has none to set, which is why
        # `Segquality` is documented as applying to the single-person path only.
        self.quality = "instances"
        self.last_inference_ms = 0.0
        self.failures = 0

    def detect_pixel_buffer(self, pixel_buffer: ObjCObject, seq: int,
                            captured_at: float,
                            orientation: int = ORIENTATION_UP) -> MaskImage | None:
        started_s = time.perf_counter()
        ok, err = self._sequence.performRequests_onCVPixelBuffer_orientation_error_(
            [self._request], pixel_buffer, orientation, None)            # TRAP: out-param
        self.last_inference_ms = (time.perf_counter() - started_s) * 1e3
        if not ok:
            raise EngineError("Vision performRequests failed: %s" % (err,))
        return self._mask_from_results(seq, captured_at)

    def detect_sample_buffer(self, sample_buffer: ObjCObject, seq: int,
                             captured_at: float,
                             orientation: int = ORIENTATION_UP) -> MaskImage | None:
        started_s = time.perf_counter()
        ok, err = self._sequence.performRequests_onCMSampleBuffer_orientation_error_(
            [self._request], sample_buffer, orientation, None)           # TRAP: out-param
        self.last_inference_ms = (time.perf_counter() - started_s) * 1e3
        if not ok:
            raise EngineError("Vision performRequests failed: %s" % (err,))
        return self._mask_from_results(seq, captured_at)

    def _mask_from_results(self, seq: int, captured_at: float) -> MaskImage | None:
        results = list(self._request.results() or [])
        if not results:
            self.failures += 1
            return None
        observation = results[0]
        # `instanceMask` rather than `generateScaledMaskForImage...`: the scaled call
        # returns ONE mask for a chosen set of instances, so index-encoding through it
        # would mean one Vision pass per person and a composite afterwards. The
        # instance mask already carries the indices, and the existing `Maskfit` path
        # is built to scale a small mask up (docs/ATTRIBUTES.md, Segmentation).
        buffer = observation.instanceMask()
        if buffer is None:
            self.failures += 1
            return None
        width, height, pixels = mask_bytes_from_pixel_buffer(buffer)
        instances = observation.allInstances()
        people = int(instances.count()) if instances is not None else 0
        return MaskImage(width, height, pixels, seq, captured_at,
                         self.last_inference_ms, people)


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
                            captured_at: float,
                            orientation: int = ORIENTATION_UP) -> MaskImage | None:
        """The replay path: a CVPixelBuffer, from a decoded fixture or a converted frame.

        Contract: returns None when Vision produced no observation, and COUNTS it in
                  `self.failures`, so an empty stream is distinguishable from a
                  stream nobody asked for. Raises only when Vision itself failed,
                  which is a different thing from finding nobody in frame.
        """
        started_s = time.perf_counter()
        ok, err = self._sequence.performRequests_onCVPixelBuffer_orientation_error_(
            [self._request], pixel_buffer, orientation, None)            # TRAP: out-param
        self.last_inference_ms = (time.perf_counter() - started_s) * 1e3
        if not ok:
            raise EngineError("Vision performRequests failed: %s" % (err,))
        return self._mask_from_results(seq, captured_at)

    def detect_sample_buffer(self, sample_buffer: ObjCObject, seq: int,
                             captured_at: float,
                             orientation: int = ORIENTATION_UP) -> MaskImage | None:
        """The live path: a CMSampleBuffer straight from the camera, unconverted.

        The camera's native 420v goes into Vision with no conversion at all -
        converting to BGRA first would cost a full-frame copy for nothing
        (DESIGN.md 3). Only the MASK comes back as one component.
        """
        started_s = time.perf_counter()
        ok, err = self._sequence.performRequests_onCMSampleBuffer_orientation_error_(
            [self._request], sample_buffer, orientation, None)           # TRAP: out-param
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
