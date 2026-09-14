"""Frames from somewhere other than a camera, on the same path as the camera's.

WHY THIS EXISTS. `Input Mode = TOP Input` lets a project hand the component whatever a
TOP holds - a movie, a Syphon feed, a render, a corrected camera image - instead of
opening a capture device. TouchDesigner writes the pixels into a shared buffer and the
sidecar reads them here.

ONE FRAME PATH, NOT TWO, and that is the design decision worth stating. Vision's live
path takes a `CMSampleBuffer`, and everything downstream of it - the sequence number,
`captured_at`, which streams run, how a mask is published, how errors are recorded -
lives in `HandEngine._on_sample_buffer`. Forking that for a second input would mean two
copies of an orchestration that is already the most delicate code in the project, and
they would drift.

So this module does the smallest possible thing instead: it turns bytes into a
`CMSampleBuffer` and hands it to the SAME method the camera delegate calls. A frame
from a TOP is then indistinguishable from a frame from a camera, everywhere after this
file.

WHAT TOUCHDESIGNER SENDS. RGBA float32, BOTTOM row first - exactly what its
`numpyArray` hands back, untouched - through `appletd/maskbuf.py`, which is a generic
seqlock image buffer despite its name and carries width, height, components and dtype
in its header. Nothing here converts it: Vision takes a float RGBA pixel buffer as it
is, and the row order is told to it as an orientation. That is what makes 1080p
affordable - MEASURED, the conversion that used to run on TD's main thread was 14 ms
a frame at 1080p and the memcpy that replaced it is 0.5 (docs/BENCHMARKS.md).

An 8-bit BGRA buffer, top row first, is still accepted - it is what the tests and the
probes write - and is read as the camera's frames are.

Thread: `read_into` runs on the sidecar's reader thread and touches no TouchDesigner
        object. The pyobjc objects it creates never cross back.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any, NamedTuple

import CoreMedia
import numpy

from appletd.engine import pixel_buffer_from_bgra, pixel_buffer_from_rgba_float
from appletd.maskbuf import DTYPE_F32, DTYPE_U8, MaskReader

if TYPE_CHECKING:  # pragma: no cover - typing only
    from objc import ObjCObject


# `maskbuf` is a SEQLOCK, so `seq` advances by TWO per publication: odd while the
# writer is mid-frame, even once it is coherent (its module docstring has the protocol).
# So consecutive frames differ by 2, and a gap of 4 means one publication was missed.
# Written here rather than assumed, because the obvious `seq + 1` counts every frame as
# a dropped one and the number looks plausible.
_SEQ_PER_PUBLISH = 2


class FrameError(RuntimeError):
    """A frame arrived that cannot be turned into something Vision accepts."""


class TopFrame(NamedTuple):
    """One frame out of the buffer, with how it is to be read.

    Contract: `pixels` is HxWx4 and writable. uint8 is BGRA with the top row first,
              as a camera delivers; float32 is RGBA with the BOTTOM row first, as
              TouchDesigner's `numpyArray` returns it, and `upside_down` says so.
              The caller turns that flag into an orientation on the Vision request
              (`streams.orientation_for`); nothing flips a pixel.
    """

    pixels: Any
    captured_at: float
    upside_down: bool

    def to_sample_buffer(self) -> ObjCObject:
        if self.upside_down:
            return sample_buffer_from_rgba_float(self.pixels, self.captured_at)
        return sample_buffer_from_bgra(self.pixels, self.captured_at)


def sample_buffer_from_rgba_float(rgba: Any, captured_at: float) -> ObjCObject:
    """Wrap a contiguous HxWx4 float32 RGBA array as a ready `CMSampleBuffer`."""
    return _ready_sample_buffer(pixel_buffer_from_rgba_float(rgba), captured_at,
                                getattr(rgba, "shape", "?"))


def sample_buffer_from_bgra(bgra: Any, captured_at: float) -> ObjCObject:
    """Wrap a contiguous HxWx4 BGRA array as a ready `CMSampleBuffer`.

    `CMSampleBufferCreateReadyWithImageBuffer` rather than `CMSampleBufferCreate`:
    "Ready" means the data is already there, which is exactly true of an array we
    have in hand, and it avoids the make-ready callback dance for nothing.

    The timing is filled in because a sample buffer without it is not valid, but
    NOTHING DOWNSTREAM READS IT - `_on_sample_buffer` takes `captured_at` from
    `time.monotonic()` at delivery, deliberately, because the buffer's presentation
    timestamp sits on a different clock and mixing the two produces an age that is
    confidently wrong. It is passed here only so the value is not invented twice.
    """
    return _ready_sample_buffer(pixel_buffer_from_bgra(bgra), captured_at,
                                getattr(bgra, "shape", "?"))


def _ready_sample_buffer(pixel_buffer: ObjCObject, captured_at: float,
                         shape: Any) -> ObjCObject:
    status, description = CoreMedia.CMVideoFormatDescriptionCreateForImageBuffer(
        None, pixel_buffer, None)
    if status != 0 or description is None:
        raise FrameError("could not describe a %r frame for Vision: status %d"
                         % (shape, status))
    # A duration of "invalid" and one sample. Vision does not sequence on these, and
    # claiming a frame rate we do not know would be worse than declining to.
    timing = CoreMedia.CMSampleTimingInfo(
        CoreMedia.kCMTimeInvalid,
        CoreMedia.CMTimeMakeWithSeconds(captured_at, 1_000_000),
        CoreMedia.kCMTimeInvalid)
    status, sample_buffer = CoreMedia.CMSampleBufferCreateReadyWithImageBuffer(
        None, pixel_buffer, description, timing, None)
    if status != 0 or sample_buffer is None:
        raise FrameError("CMSampleBufferCreateReadyWithImageBuffer failed: %d"
                         % status)
    return sample_buffer


class FrameBufferReader:
    """Pulls frames out of the shared buffer TouchDesigner writes.

    Holds no Vision state of its own: it reads bytes, shapes them, and calls back.
    A miss - the writer publishing while this reads - is handled by `MaskReader`'s
    seqlock, which returns the previous frame rather than a torn one.

    THE FILE IS OPENED LAZILY, and this is the one reader in the package that has to
    be. Every other shared buffer is written by the sidecar, so it exists before
    anything reads it; this one is written by TOUCHDESIGNER, and it does not exist
    until a Script TOP has cooked at least once. Opening it eagerly means the sidecar
    dies with FileNotFoundError whenever it is started in TOP Input mode before a
    frame has been published - which is the normal case, since the component's image
    input may be empty or the project paused.

    So an absent buffer means "no frame yet", exactly like an empty one.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._reader: MaskReader | None = None
        self._last_seq = -1
        self.frames_read = 0
        self.frames_skipped = 0
        # GAUGES for the status line, so the cost of this path can be read off a
        # running sidecar rather than argued about: the last read out of the seqlock
        # (a copy), and the last wrap into a sample buffer (set by the caller).
        self.last_read_ms = 0.0
        self.last_wrap_ms = 0.0
        # How many times `latest()` found nothing to open. A gauge, so "no frames
        # are arriving" can be told from "the writer has never run".
        self.waits_for_writer = 0

    @property
    def path(self) -> str:
        return self._path

    def _opened(self) -> MaskReader | None:
        """The reader, once the writer has created the file. None until then."""
        if self._reader is not None:
            return self._reader
        if not self._path or not os.path.exists(self._path):
            self.waits_for_writer += 1
            return None
        try:
            self._reader = MaskReader(self._path)
        except (OSError, ValueError):
            # Created but not yet a valid buffer - the writer got as far as the file
            # and not as far as the header. Wait for it like any other absence.
            self.waits_for_writer += 1
            return None
        return self._reader

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def latest(self) -> TopFrame | None:
        """The newest unseen frame, or None.

        None means "nothing new", which is the common case: TouchDesigner writes at
        its frame rate and this is polled faster, so most reads have nothing to do.
        It also covers "the writer has not created the buffer yet" - see the class
        docstring for why that is a wait and not an error.
        """
        reader = self._opened()
        if reader is None:
            return None
        # PEEK before copying. This is polled every 2 ms and TouchDesigner writes at
        # 30 fps, so most polls find the same frame - and `read()` copies 33 MB at
        # 1080p before it can say so. MEASURED: the peek took the sidecar from 55% of
        # a core to the cost of Vision alone (docs/BENCHMARKS.md).
        if reader.seq() == self._last_seq:
            return None
        started_s = time.perf_counter()
        frame = reader.read(writable=True)
        if frame is None:
            return None
        if frame.seq == self._last_seq:
            return None
        # A gap means TouchDesigner published faster than this could read. Counted
        # rather than reported, because dropping the older of two frames is the
        # right answer for a live path and a silent drop is not.
        if self._last_seq >= 0 and frame.seq > self._last_seq + _SEQ_PER_PUBLISH:
            missed = (frame.seq - self._last_seq) // _SEQ_PER_PUBLISH - 1
            self.frames_skipped += missed
        self._last_seq = frame.seq
        self.frames_read += 1
        # The header states the shape, so it is CHECKED rather than assumed: a buffer
        # written with one component is a mask, and reshaping it as BGRA would give
        # Vision a quarter of an image and no error.
        if frame.components != 4:
            raise FrameError(
                "TOP Input expects four components - this buffer says %d. The "
                "Script TOP writing it is publishing a mask, not a frame."
                % frame.components)
        if frame.dtype not in (DTYPE_U8, DTYPE_F32):
            raise FrameError("TOP Input expects uint8 BGRA or float32 RGBA, not dtype %d"
                             % frame.dtype)
        upside_down = frame.dtype == DTYPE_F32
        dtype = numpy.float32 if upside_down else numpy.uint8
        # WRITABLE, which is why `read(writable=True)` above: `numpy.frombuffer` over
        # immutable `bytes` gives a READ-ONLY array, and `CVPixelBufferCreateWithBytes`
        # refuses one - "buffer source array is read-only". A test built on
        # `numpy.zeros` never sees it; this was found on a frame TouchDesigner had
        # actually written. The bytearray is the one copy out of the mapping, and a
        # copy there IS wanted: `pixel_buffer_from_*` PINS the array it wraps for the
        # pixel buffer's lifetime, so a view of the shared buffer would hold the
        # writer's memory hostage.
        pixels = numpy.frombuffer(frame.pixels, dtype=dtype)
        expected = frame.width * frame.height * frame.components
        if pixels.size != expected:
            raise FrameError(
                "a %dx%d frame is %d values, got %d"
                % (frame.width, frame.height, expected, pixels.size))
        self.last_read_ms = (time.perf_counter() - started_s) * 1e3
        return TopFrame(pixels.reshape((frame.height, frame.width, frame.components)),
                        time.monotonic(), upside_down)
