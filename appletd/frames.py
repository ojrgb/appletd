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

WHAT TOUCHDESIGNER SENDS. BGRA, 8 bits a channel, top row first, through
`appletd/maskbuf.py` - which is a generic seqlock image buffer despite its name, and
already carries the width, height and component count in its header. Reading a 720p
TOP back to the CPU costs 0.26 ms on TD's main thread, which is what makes this
affordable at all (docs/BENCHMARKS.md).

Thread: `read_into` runs on the sidecar's reader thread and touches no TouchDesigner
        object. The pyobjc objects it creates never cross back.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

import CoreMedia
import numpy

from appletd.engine import pixel_buffer_from_bgra
from appletd.maskbuf import MaskReader

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
    pixel_buffer = pixel_buffer_from_bgra(bgra)
    status, description = CoreMedia.CMVideoFormatDescriptionCreateForImageBuffer(
        None, pixel_buffer, None)
    if status != 0 or description is None:
        raise FrameError("could not describe a %r frame for Vision: status %d"
                         % (getattr(bgra, "shape", "?"), status))
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

    def latest(self) -> tuple[Any, float] | None:
        """The newest unseen frame as an HxWx4 BGRA array, or None.

        None means "nothing new", which is the common case: TouchDesigner writes at
        its frame rate and this is polled faster, so most reads have nothing to do.
        It also covers "the writer has not created the buffer yet" - see the class
        docstring for why that is a wait and not an error.
        """
        reader = self._opened()
        if reader is None:
            return None
        frame = reader.read()
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
                "TOP Input expects BGRA, four components - this buffer says %d. The "
                "Script TOP writing it is publishing a mask, not a frame."
                % frame.components)
        # `bytearray` and not `bytes`, which is the whole reason this line has a
        # comment: `numpy.frombuffer` over an immutable `bytes` gives a READ-ONLY
        # array, and `CVPixelBufferCreateWithBytes` refuses one - "buffer source
        # array is read-only". A test built on `numpy.zeros` never sees it, because
        # that array is writable; this was found by reading a frame TouchDesigner had
        # actually written.
        #
        # It is a copy of a copy - `MaskReader` already copied out of the mmap - and
        # that is accepted rather than optimised: `pixel_buffer_from_bgra` PINS the
        # array it wraps for the lifetime of the pixel buffer, so handing it a view
        # of the shared buffer would hold the writer's memory hostage.
        pixels = numpy.frombuffer(bytearray(frame.pixels), dtype=numpy.uint8)
        expected = frame.width * frame.height * frame.components
        if pixels.size != expected:
            raise FrameError(
                "a %dx%d BGRA frame is %d bytes, got %d"
                % (frame.width, frame.height, expected, pixels.size))
        return (pixels.reshape((frame.height, frame.width, frame.components)),
                time.monotonic())
