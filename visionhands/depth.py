"""Monocular depth: Depth Anything V2 Small through Core ML -> a fp16 map.

WHAT THIS OWNS. One `VNCoreMLRequest`, the model's compilation and cache, and the
extraction of its output into plain bytes plus a per-frame affine fit. It owns neither
the camera nor the transport: `engine.py` hands it a buffer that has already been
delivered, `visionhands/pins.py` does the arithmetic, and `visionhands/maskbuf.py`
gets the bytes to TouchDesigner. Same shape as `segmentation.py`, and the same
boundary rules - nothing from TouchDesigner, and no pyobjc object escapes the capture
thread.

THE MODEL IS NOT IN THIS REPO, and that is deliberate. It is 47 MB of weights from
Apple's Hugging Face account, and a repository that ships to everybody on GitHub has no
business carrying it: it would be in every clone, every fork and every diff. So it is
downloaded once with `tools/fetch_models.sh` and `models/` is gitignored.
`docs/DEPTH.md` is the first-run instructions, and `resolve_model()` below raises with
that path in the message rather than a FileNotFoundError - somebody meeting this for
the first time should be told what to run, not what stat() returned.

WHAT THE OUTPUT ACTUALLY IS, measured rather than assumed:

  * 518 x 392 in, 518 x 392 out. That is 1.32:1 against a webcam's 1.78:1, so
    something has to give - see CROP below.
  * The buffer is `OneComponent16Half` ('L00h'), a HALF-FLOAT image with several
    hundred distinct values in a typical frame. Reading it as 8-bit throws that away
    twice: once by quantising to 256 levels, and once by applying a second per-frame
    normalisation on top of the `reduce_max` already in the graph. The reference
    implementation this was ported from records getting that wrong and what it cost.
  * BIGGER MEANS NEARER. The model predicts INVERSE depth, and the graph divides by
    the frame's own maximum - so the scale is not comparable between frames and
    nothing may be built on the raw numbers without pinning. `pins.py` is that.

WHY VISION AND NOT CORE ML DIRECTLY. `VNCoreMLRequest` resizes the camera buffer to
the model's input size on the GPU with no trip through numpy, and takes the same
`CMSampleBuffer` the other requests are already being handed - so depth sees the SAME
frame as the hands in it, which is the entire point of one process and one queue
(DESIGN.md 6.4).

THREADS. Everything here runs on the GCD capture queue, called from the engine's
sample-buffer callback, except `resolve_model`, `verify_depth_support` and
`DepthDetector.__init__`, which run on the caller's thread at construction - so a
missing model or a framework mismatch is reported where somebody can act on it, before
a camera is open.

Ref: docs/DEPTH.md (first run and the pins), design/DESIGN.md 2.22,
     visionhands/pins.py, apple-vision-examples/examples/depth/depth.py.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import CoreML
import Foundation
import Quartz
import Vision

from visionhands.engine import EngineError, ObjCObject
from visionhands.pins import Pin, Solve, solve

if TYPE_CHECKING:
    import numpy.typing

# Where `tools/fetch_models.sh` puts things. Repo-relative, because the sidecar is
# started from the repo root by TouchDesigner and by hand, and an absolute path would
# be wrong on everybody else's machine - which is the whole audience for this file.
REPO_ROOT: Final = Path(__file__).resolve().parent.parent
MODELS_DIR: Final = REPO_ROOT / "models"
MODEL_NAME: Final = "DepthAnythingV2SmallF16.mlpackage"

# `kCVPixelFormatType_OneComponent16Half`. Spelled as the integer as well as named,
# because the four-character code is what appears in a debugger and 'L00h' being
# 0x4C303068 is the fact that makes it legible.
ONE_COMPONENT_16_HALF: Final = 0x4C303068
ONE_COMPONENT_8: Final = 0x4C303038
ONE_COMPONENT_32_FLOAT: Final = 0x4C303066

# Compute units, by name. `all` lets Core ML use the Neural Engine, which is where this
# model wants to be - CPU-only is several times slower and there is no reason to ask
# for it outside a comparison.
COMPUTE_UNITS: Final[dict[str, int]] = {"cpu": 0, "gpu": 1, "all": 2, "ane": 3}

# How the camera frame is fitted into the model's 1.32:1 input.
#
# `fill` SQUASHES the whole frame in. It distorts, and it is still the right default:
# every pixel the camera saw is seen by the model, and the map that comes back covers
# exactly the pixels you can see - which is what makes it line up with anything else
# in the project. `crop` looks sharper and quietly lies about geometry, because the
# edges of the frame are invisible to the model while the map still gets drawn over
# them.
CROP_OPTIONS: Final[dict[str, str]] = {
    "fill": "VNImageCropAndScaleOptionScaleFill",
    "crop": "VNImageCropAndScaleOptionCenterCrop",
    "fit": "VNImageCropAndScaleOptionScaleFit",
}


class DepthFrame(NamedTuple):
    """One depth map, un-padded and packed, plus the fit for THIS frame.

    `pixels` is fp16 - `width * height * 2` bytes - and it is the model's own numbers,
    not a display-ready image. `fit` is what makes them mean metres, and it travels
    with them because a fit paired with a different frame's map is a plausible wrong
    answer (pins.py).
    """

    width: int
    height: int
    pixels: bytes
    fit: Solve
    seq: int
    captured_at: float
    inference_ms: float


def resolve_model(name: str = MODEL_NAME) -> Path:
    """The model's path, or an error that says how to get it.

    Contract: raises EngineError naming `tools/fetch_models.sh` and `docs/DEPTH.md`
              rather than letting Core ML report a missing file. This is the first
              thing a new clone hits, and "No such file or directory" is not an
              instruction.
    """
    path = Path(name)
    if not path.is_absolute():
        path = MODELS_DIR / name
    compiled = path.with_suffix(".mlmodelc")
    if compiled.exists() or path.exists():
        return path
    raise EngineError(
        "no depth model at %s.\n"
        "The weights are 47 MB from Apple's Hugging Face account and are NOT in this "
        "repository - `models/` is gitignored on purpose.\n"
        "Get them with:    ./tools/fetch_models.sh\n"
        "First-run notes:  docs/DEPTH.md" % path)


def compiled_model(path: Path) -> Path:
    """Compile the .mlpackage to a .mlmodelc, cached beside it. Idempotent.

    `.mlpackage` is a DISTRIBUTION format; the runtime wants a compiled `.mlmodelc`.
    Core ML will compile one at load time with no Xcode involved, so that is what
    happens here - and the result is cached because compiling costs a few seconds and
    the output is deterministic. Delete the `.mlmodelc` to force a rebuild.

    Contract: the compile happens on the CALLER's thread, at construction, and prints
              that it is happening. A silent multi-second stall on first run looks
              exactly like a hang.
    """
    target = path.with_suffix(".mlmodelc")
    if target.exists():
        return target
    print("compiling %s (one time, a few seconds)" % path.name, flush=True)
    url = Foundation.NSURL.fileURLWithPath_(str(path))
    temporary, error = CoreML.MLModel.compileModelAtURL_error_(url, None)
    if temporary is None:
        raise EngineError("Core ML could not compile %s: %s" % (path.name, error))
    ok, error = Foundation.NSFileManager.defaultManager().copyItemAtURL_toURL_error_(
        temporary, Foundation.NSURL.fileURLWithPath_(str(target)), None)
    if not ok:
        raise EngineError("could not cache the compiled model at %s: %s"
                          % (target, error))
    return target


def verify_depth_support() -> None:
    """Fail at construction if this Vision or Core ML cannot do what this file assumes.

    Contract: raises EngineError naming everything missing rather than the first thing
              missing - a framework mismatch is usually one version, and one message
              listing four absent symbols identifies it.
    """
    problems = []
    for symbol in ("VNCoreMLRequest", "VNCoreMLModel", "VNPixelBufferObservation"):
        if not hasattr(Vision, symbol):
            problems.append("Vision has no %s" % symbol)
    for name in CROP_OPTIONS.values():
        if not hasattr(Vision, name):
            problems.append("Vision has no %s" % name)
    if not hasattr(CoreML, "MLModel"):
        problems.append("CoreML has no MLModel - is pyobjc-framework-CoreML "
                        "installed? It is in requirements.txt")
    if problems:
        raise EngineError("this machine cannot be trusted for depth: %s"
                          % "; ".join(problems))


def depth_bytes_from_pixel_buffer(
        pixel_buffer: ObjCObject) -> tuple[int, int, bytes, int]:
    """`(width, height, packed_pixels, dtype_code)` from a one-component buffer.

    Returns `maskbuf`'s dtype code alongside the bytes, so nothing downstream has to
    guess: this model returns fp16 today and a future revision returning fp32 would
    otherwise be read at half its width, silently.

    ROW PADDING is un-done here. `CVPixelBufferGetBytesPerRow` is NOT
    `width * bytesPerPixel` - CoreVideo aligns rows - and handing the padded bytes on
    as an image shears it progressively down the frame, which reads as a bad depth map
    rather than as a bug. Same trap as `segmentation.py`, same fix.

    Contract: raises on a pixel format it cannot read, rather than reinterpreting the
              bytes. A fp16 buffer read as 8-bit is an image, and it is the wrong one.
    """
    # Imported here, not at module scope: `maskbuf`'s dtype codes are the wire
    # contract for the buffer, and this is the one place that needs them.
    from visionhands.maskbuf import DTYPE_F16, DTYPE_F32, DTYPE_U8

    formats = {ONE_COMPONENT_16_HALF: (2, DTYPE_F16),
               ONE_COMPONENT_32_FLOAT: (4, DTYPE_F32),
               ONE_COMPONENT_8: (1, DTYPE_U8)}
    fmt = int(Quartz.CVPixelBufferGetPixelFormatType(pixel_buffer))
    if fmt not in formats:
        raise EngineError(
            "depth output is pixel format %#x (%r), not one this reads. Expected "
            "OneComponent16Half (%#x, 'L00h'). Reading it anyway would produce a "
            "plausible wrong map" % (fmt, _four_cc(fmt), ONE_COMPONENT_16_HALF))
    per_pixel, dtype = formats[fmt]

    width = int(Quartz.CVPixelBufferGetWidth(pixel_buffer))
    height = int(Quartz.CVPixelBufferGetHeight(pixel_buffer))
    stride = int(Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer))
    row_bytes = width * per_pixel

    # Locked for the shortest possible span: the base address is only valid while the
    # lock is held, and Core ML may be reusing this buffer for the next frame the
    # moment it is released. 1 is `kCVPixelBufferLock_ReadOnly`.
    Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, 1)
    try:
        base = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
        if base is None:
            raise EngineError("depth buffer has no base address while locked")
        raw = bytes(base.as_buffer(stride * height))
    finally:
        Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, 1)

    if stride == row_bytes:
        return width, height, raw, dtype
    return width, height, b"".join(
        raw[row * stride:row * stride + row_bytes] for row in range(height)), dtype


def _four_cc(value: int) -> str:
    """A CoreVideo format code as its four characters, for an error message.

    Not decoration: `0x4c303068` means nothing and `'L00h'` is immediately
    recognisable, which is the difference between a message somebody can act on and
    one they have to decode.
    """
    try:
        text = value.to_bytes(4, "big").decode("ascii")
    except (OverflowError, UnicodeDecodeError):
        return "?"
    return text if text.isprintable() else "?"


class DepthDetector:
    """Holds the Core ML request and turns buffers into DepthFrames.

    Thread: NOT thread-safe and does not need to be. One detector belongs to one
            serial capture queue, so its request is only ever touched by one thread.
    Why the model is loaded at CONSTRUCTION: it compiles on first use, which takes
            seconds, and it can fail for reasons a user has to fix - a missing
            download, a Core ML that will not load it. Both belong on the caller's
            thread before a camera is open, not on the capture queue where they could
            only be recorded.
    """

    def __init__(self, pins: tuple[Pin, ...] = (), drop_m: float = 0.5,
                 compute: str = "all", crop: str = "fill",
                 model: str = MODEL_NAME) -> None:
        if compute not in COMPUTE_UNITS:
            raise EngineError("compute must be one of %s, not %r"
                              % (", ".join(sorted(COMPUTE_UNITS)), compute))
        if crop not in CROP_OPTIONS:
            raise EngineError("crop must be one of %s, not %r"
                              % (", ".join(sorted(CROP_OPTIONS)), crop))
        verify_depth_support()
        path = compiled_model(resolve_model(model))

        configuration = CoreML.MLModelConfiguration.alloc().init()
        configuration.setComputeUnits_(COMPUTE_UNITS[compute])
        core_model, error = CoreML.MLModel.modelWithContentsOfURL_configuration_error_(
            Foundation.NSURL.fileURLWithPath_(str(path)), configuration, None)
        if core_model is None:
            raise EngineError("Core ML could not load %s: %s" % (path.name, error))
        wrapped, error = Vision.VNCoreMLModel.modelForMLModel_error_(core_model, None)
        if wrapped is None:
            raise EngineError("Vision refused %s: %s" % (path.name, error))

        self._sequence = Vision.VNSequenceRequestHandler.alloc().init()
        self._request = Vision.VNCoreMLRequest.alloc().initWithModel_(wrapped)
        self._request.setImageCropAndScaleOption_(
            getattr(Vision, CROP_OPTIONS[crop]))

        self.pins = pins
        self.drop_m = drop_m
        self.compute = compute
        self.crop = crop
        self.model_path = path
        self.input_size = self._input_size()
        self.last_inference_ms = 0.0
        self.failures = 0
        # The most recent fit, for a status line. A GAUGE, not state the solve reads:
        # `pins.solve` is called fresh every frame and this is only ever written to.
        self.last_fit: Solve | None = None

    def _input_size(self) -> tuple[int, int]:
        """The model's input image size, read off the request rather than assumed.

        Worth having: it is the resolution the model actually sees, and it is what
        decides whether `crop` is throwing away the edges of the frame.
        """
        description = self._request.model().model().modelDescription()
        for feature in description.inputDescriptionsByName().values():
            constraint = feature.imageConstraint()
            if constraint is not None:
                return (int(constraint.pixelsWide()), int(constraint.pixelsHigh()))
        return (0, 0)

    def detect_sample_buffer(self, sample_buffer: ObjCObject, seq: int,
                             captured_at: float) -> DepthFrame | None:
        """The live path: a CMSampleBuffer straight from the camera, unconverted.

        Contract: returns None when Core ML produced no observation, and counts it in
                  `self.failures`. Raises only when the request itself failed, which
                  is a different thing from an empty result.
        """
        started = time.perf_counter()
        ok, error = self._sequence.performRequests_onCMSampleBuffer_error_(
            [self._request], sample_buffer, None)          # TRAP: out-param
        self.last_inference_ms = (time.perf_counter() - started) * 1e3
        if not ok:
            raise EngineError("Core ML performRequests failed: %s" % (error,))
        return self._frame_from_results(seq, captured_at)

    def detect_pixel_buffer(self, pixel_buffer: ObjCObject, seq: int,
                            captured_at: float) -> DepthFrame | None:
        """The replay path: a CVPixelBuffer from a decoded fixture."""
        started = time.perf_counter()
        ok, error = self._sequence.performRequests_onCVPixelBuffer_error_(
            [self._request], pixel_buffer, None)           # TRAP: out-param
        self.last_inference_ms = (time.perf_counter() - started) * 1e3
        if not ok:
            raise EngineError("Core ML performRequests failed: %s" % (error,))
        return self._frame_from_results(seq, captured_at)

    def _frame_from_results(self, seq: int, captured_at: float) -> DepthFrame | None:
        results = list(self._request.results() or [])
        if not results:
            self.failures += 1
            return None
        buffer = results[0].pixelBuffer()
        if buffer is None:
            self.failures += 1
            return None
        width, height, pixels, dtype = depth_bytes_from_pixel_buffer(buffer)
        fit = solve(self._as_array(pixels, width, height, dtype), self.pins,
                    self.drop_m) if self.pins else _NO_PINS
        self.last_fit = fit
        return DepthFrame(width, height, pixels, fit, seq, captured_at,
                          self.last_inference_ms)

    @staticmethod
    def _as_array(pixels: bytes, width: int, height: int,
                  dtype: int) -> numpy.typing.NDArray[Any]:
        """The packed bytes as a 2-D array, for the solve. No copy.

        numpy is imported HERE, per the package rule - and unlike `pins.py`, which
        needs it at module scope, this module can honour it because the only thing it
        does with numpy is hand `pins.solve` a view.
        """
        import numpy

        from visionhands.maskbuf import NUMPY_DTYPE

        flat = numpy.frombuffer(pixels, dtype=NUMPY_DTYPE[dtype])
        return flat.reshape(height, width)


# The fit reported when there are no pins: not a failure, just no metric claim. A
# module-level constant rather than a fresh Solve per frame, because it is immutable
# and this runs 30 times a second.
_NO_PINS: Final = Solve(None, None, (), None, (), 0.0, "no pins configured")
