"""A file-backed mmap carrying one image, with a seqlock. The segmentation transport.

WHAT THIS OWNS. One shared buffer holding one frame of single-component pixels, and
the coherence protocol that lets a writer in the sidecar process and a reader inside
TouchDesigner use it with no locks held across the boundary. It owns nothing else: it
does not know what a mask is, does not import Vision, and does not import
TouchDesigner. Both ends are plain files and bytes.

WHY A FILE AND NOT TOUCHDESIGNER'S SHARED MEMORY. Researched and decided
2026-08-21, and `docs/BUILD_PLAN.md` step 9 has the compiled header layouts. The
short version: TD's `UT_SharedMem` names its lock `myName + "Mutex"`, which for the
info segment is 36 characters against macOS's 31-character `shm_open` limit, so a
segment has to be registered in TD's own name directory - and it only registers for
the segment's OWNER. A foreign owner would have to reimplement that directory
protocol AND initialise process-shared `pthread_mutex_t` in shared memory, where the
receiver's `tryLock(5000)` turns a mistake into a five-second stall of
TouchDesigner's main thread per frame rather than a clean failure.

A file in /tmp has none of that. `mmap` on both sides is the same physical pages, so
there is no copy and no kernel round trip per frame, and the whole thing is testable
with no TouchDesigner running - which is why this module has tests and TD's protocol
never could.

WHY A SEQLOCK. The reader runs on TouchDesigner's main thread. Anything that can
block there is disqualified: a held lock in the writer becomes dropped frames in the
whole project, not just in this operator. A seqlock never blocks the reader - it
either gets a coherent frame or it detects that it did not and says so - and the
caller decides what to do with a miss, which for a mask is "show the previous one".

THE PROTOCOL, and each of the three layers earns its place:

    writer                              reader
    seq += 1        (now ODD)           s1 = seq;  if s1 is odd -> retry
    write pixels                        copy the pixels
    FENCE                               FENCE
    seq_tail = seq                      if seq_tail != s1 -> retry
    seq += 1        (now EVEN)

  * `seq` ODD says a write is in progress. Catches a reader that arrives mid-write.
  * `seq_tail` catches a whole write that STARTED and FINISHED inside the reader's
    copy, which the leading counter alone cannot see.
  * the FENCE is the honest part. Python's interpreter does not reorder these
    statements, but the CPU may make the tail counter visible before the pixels, and
    on Apple Silicon that is a weakly ordered store buffer rather than a theoretical
    one. An uncontended `threading.Lock` acquire/release is a real acquire/release
    barrier and costs about 50 ns, so it is used as one. It is NOT a C11
    `atomic_thread_fence` and this module does not claim to be provably correct
    against the memory model.

    WHAT THE RESIDUAL RISK COSTS, because that is what makes the trade acceptable:
    one torn frame - a mask with a band of the previous frame in it - on a stream
    where the next frame arrives in 16 ms. Not a crash, not a stall, not corruption
    that persists. If that ever proves visible, the fix is a checksum in the header,
    which the reserved bytes are there for.

NOT A RING BUFFER. One slot, deliberately. A mask is only ever wanted at its newest,
so a queue would add latency to deliver frames nobody will draw. The writer
overwrites; a reader that misses gets the next one 16 ms later.

THREADS AND PROCESSES. `MaskWriter` is written to be used from the capture queue -
one writer, one thread, no internal locking beyond the fence. `MaskReader` is
read-only and may be used from any single thread. Two writers on one path is not
supported and is not detected.

Ref: docs/BUILD_PLAN.md step 9 (the decision and what TD's protocol costs),
     appletd/coords.py (the y-axis convention this deliberately does not apply).
"""

from __future__ import annotations

import mmap
import os
import struct
import threading
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, NamedTuple

if TYPE_CHECKING:
    # Type-checking only, so the runtime import path stays numpy-free - see
    # `as_numpy` for why that is a package rule and not a preference.
    import numpy
    import numpy.typing

# `<` - little-endian and no padding, spelled out rather than left native. The
# writer and the reader are different processes and one day may be different
# builds; a native-order header is a bug that only appears on the machine you do
# not own.
_HEADER: Final = struct.Struct("<IIIIIIIIQdII")
# 32 bytes of AUX, written and read verbatim, whose meaning belongs to whoever put
# it there. It exists because the depth stream needs to publish the affine fit it
# solved for this frame - alpha, beta and how well the pins agreed - and that has to
# travel WITH the pixels it describes or it is describing a different frame
# (appletd/pins.py packs it, appletd/depth.py hands it over).
#
# Opaque on purpose. This module moves an image and says nothing about what the
# numbers mean; teaching it about metres would make it a depth transport rather than
# a transport. The same reasoning that keeps the y-flip out of here.
AUX_BYTES: Final = 32
# 128, not 64. Raised 2026-08-22 when the aux block landed, because 56 + 32 = 88 and
# the old reserve had 8 bytes left - a reserve with nothing in it is not a reserve.
# The payload is hundreds of KB, so 64 more bytes of header costs nothing measurable.
HEADER_BYTES: Final = 128
assert _HEADER.size + AUX_BYTES <= HEADER_BYTES, "header + aux must fit the reserve"

MAGIC: Final = 0x56484D42          # 'VHMB' - appletd mask buffer
# 2 as of 2026-08-22: the header grew from 64 bytes to 128 and gained the aux block.
# A version bump rather than a silent layout change, so a stale buffer from the
# previous version fails LOUDLY - the reader says which version it found and which it
# speaks. Misparsing an old header would give a plausible geometry and garbage pixels.
VERSION: Final = 2

# The SOURCE frame this image was derived from, and it is in the header because the
# alternative is a side channel that goes stale. MEASURED 2026-08-22 and this is not
# a nicety: `VNGeneratePersonSegmentationRequest` does NOT return a mask matching its
# input's aspect ratio. It snaps to one of two shapes per quality level, chosen only
# by whether the input is wider than it is tall:
#
#     input 1280x720 (16:9)  ->  mask 256x192 (4:3)
#     input  720x720 (1:1)   ->  mask 192x256 (3:4)
#     input  640x480 (4:3)   ->  mask 256x192 (4:3)
#
# So the mask is ANISOTROPICALLY STRETCHED relative to the frame it describes, and
# nothing in the mask says by how much. A consumer that draws it at the source's
# aspect gets a mask that is subtly wrong - and on a 4:3 camera it would be exactly
# right, which is how this ships broken. `source_width`/`source_height` are what let
# the consumer undo the stretch. Zero means "not stated".

# One 8-bit component, which is what `VNGeneratePersonSegmentationRequest` produces
# (`L008`, revision 1 - DESIGN.md 2.12 and BUILD_PLAN step 9). The field exists so a
# reader can REFUSE a buffer it does not understand rather than reinterpret it: a
# mask read at the wrong component count is a plausible-looking wrong image.
DTYPE_U8: Final = 0
# FP16, for Depth Anything V2: its output buffer is `OneComponent16Half` ('L00h') with
# several hundred distinct values in a typical frame, and reading it as 8-bit throws
# that away twice over - once by quantising to 256 levels and once by applying a
# second per-frame normalisation on top of the reduce_max already in the model's
# graph. The whole point of the pin solve is arithmetic on those values, so they
# travel at full precision.
DTYPE_F16: Final = 1
DTYPE_F32: Final = 2
BYTES_PER_COMPONENT: Final = {DTYPE_U8: 1, DTYPE_F16: 2, DTYPE_F32: 4}
# What each code means to numpy. Separate from the size table so a reader that
# understands the size but not the interpretation cannot silently guess.
NUMPY_DTYPE: Final = {DTYPE_U8: "uint8", DTYPE_F16: "float16", DTYPE_F32: "float32"}

# What a reader does when it keeps losing the race. Four attempts, because each one
# costs a full copy of the pixels and this runs on TouchDesigner's main thread: at
# 256x144 that is 37 KB a go, and a writer that beats the reader four times running
# is not a race any number of retries will win.
DEFAULT_ATTEMPTS: Final = 4

# The fence. One module-level lock, uncontended, used for its barrier and never to
# guard state - see the module docstring. It is shared because a lock's barrier is a
# property of the operation, not of the object.
_FENCE: Final = threading.Lock()


def _fence() -> None:
    """A memory barrier, by way of an uncontended lock acquire/release."""
    _FENCE.acquire()
    _FENCE.release()


class MaskFrame(NamedTuple):
    """One coherent read. `pixels` is a COPY and outlives the buffer it came from.

    A copy and not a view of the mapping, deliberately: a view would let the writer
    change the bytes under a caller who is still looking at them, which is the exact
    tearing this module exists to prevent - moved from a 60 microsecond window into
    an unbounded one.
    """

    width: int
    height: int
    components: int
    dtype: int
    seq: int
    frame: int          # the writer's own frame counter, monotonic, never reset
    timestamp: float    # seconds, from the writer's clock. NOT comparable to ours
    pixels: bytes
    source_width: int   # the frame this describes. 0 = not stated
    source_height: int
    aux: bytes          # 32 opaque bytes, meaning owned by the writer

    @property
    def stride(self) -> int:
        """Bytes per row. No row padding: the buffer is packed."""
        return self.width * self.components * BYTES_PER_COMPONENT[self.dtype]

    @property
    def source_scale(self) -> tuple[float, float]:
        """`(x, y)` multipliers taking mask pixels to source pixels.

        `(1.0, 1.0)` when the source is not stated, which is the honest answer for
        "I do not know" - and it is why this is a property rather than something the
        writer bakes in: a caller that gets 1.0 and draws the mask unscaled is
        wrong in exactly the way it would have been anyway, not silently misled.

        The two are DIFFERENT numbers for a 16:9 source, and that is the whole point.
        """
        if not self.source_width or not self.source_height or not self.width:
            return (1.0, 1.0)
        return (self.source_width / self.width, self.source_height / self.height)

    def as_numpy(self) -> numpy.typing.NDArray[Any]:
        """`(height, width)` for one component, else `(height, width, components)`.

        numpy is imported HERE and not at module scope, and that is a package rule
        rather than a preference: TouchDesigner's bundled Python has numpy but
        `appletd/__init__.py` promises the core layer does not need it, and
        requirements-dev.txt says nothing dev-only may leak into an import path.

        NO Y FLIP. Vision's mask arrives with its origin at the TOP left and
        TouchDesigner's TOP arrays are bottom-up (coords.py), so somebody has to
        flip. It is not this module: this module moves bytes, and a transport that
        silently reorients its payload is a transport you cannot test against a
        known pattern. The Script TOP flips, and `docs/ATTRIBUTES.md` says so.
        """
        import numpy

        if self.dtype not in NUMPY_DTYPE:
            raise ValueError("dtype code %d has a size but no numpy meaning - a "
                             "reader that guessed would produce a plausible wrong "
                             "image" % self.dtype)
        flat = numpy.frombuffer(self.pixels, dtype=NUMPY_DTYPE[self.dtype])
        if self.components == 1:
            return flat.reshape(self.height, self.width)
        return flat.reshape(self.height, self.width, self.components)


def capacity_for(width: int, height: int, components: int = 1,
                 dtype: int = DTYPE_U8) -> int:
    """Total file size needed to carry frames up to `width` x `height`.

    Contract: raises on a dtype this module cannot size, rather than guessing 1 byte
              and producing a file that is quietly too small.
    """
    if dtype not in BYTES_PER_COMPONENT:
        raise ValueError("unknown dtype code %r; known: %s"
                         % (dtype, sorted(BYTES_PER_COMPONENT)))
    if width <= 0 or height <= 0 or components <= 0:
        raise ValueError("width, height and components must all be positive, got "
                         "%r x %r x %r" % (width, height, components))
    return HEADER_BYTES + width * height * components * BYTES_PER_COMPONENT[dtype]


class MaskWriter:
    """The producing end. One per path, one thread.

    The file is sized ONCE, for the largest frame it will ever carry, and the header
    then says how much of it is live. So a resolution change is a header write rather
    than a remap, and the reader never has to handle the mapping moving under it -
    which on the reader's side is TouchDesigner's main thread and not somewhere to be
    discovering that a file just got shorter.
    """

    def __init__(self, path: str, width: int, height: int,
                 components: int = 1, dtype: int = DTYPE_U8) -> None:
        self._capacity = capacity_for(width, height, components, dtype)
        self._path = path
        self._width = width
        self._height = height
        self._components = components
        self._dtype = dtype
        self._seq = 0
        self._frame = 0
        self._source = (0, 0)
        self._aux = b"\x00" * AUX_BYTES

        # Created with the size written before the mapping exists, because mmap on a
        # file shorter than the map is a SIGBUS when the pages are touched, not an
        # exception. `os.open` with O_CREAT rather than open() so the mode is ours:
        # 0600, since a mask of a person in a room is not world-readable data.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.ftruncate(fd, self._capacity)
            # The magic goes down on the DESCRIPTOR, before the mapping exists.
            # MEASURED as a real race, not a theoretical one: between `ftruncate`
            # and the first header write the file is full-size and all zeros, and a
            # reader that opens in that window sees magic 0. It is a handful of
            # microseconds, and a Script TOP starting at the same moment as the
            # sidecar hits it. Narrowed here, and handled on the reader's side too -
            # an all-zero header reads as "nothing published yet", not as an error.
            os.pwrite(fd, struct.pack("<II", MAGIC, VERSION), 0)
            self._map = mmap.mmap(fd, self._capacity, access=mmap.ACCESS_WRITE)
        finally:
            # The mapping keeps the file alive on its own, so the descriptor is not
            # ours to hold - and a leaked fd per restart is a real leak in a process
            # meant to run for hours.
            os.close(fd)
        self._write_header()

    # -- context manager, so a test cannot leak a mapping ------------------
    def __enter__(self) -> MaskWriter:
        return self

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()

    @property
    def path(self) -> str:
        return self._path

    @property
    def capacity(self) -> int:
        return self._capacity

    def _write_header(self, seq: int | None = None, seq_tail: int | None = None,
                      timestamp: float = 0.0) -> None:
        self._map[0:_HEADER.size] = _HEADER.pack(
            MAGIC, VERSION,
            self._seq if seq is None else seq,
            self._seq if seq_tail is None else seq_tail,
            self._width, self._height, self._components, self._dtype,
            self._frame, timestamp, *self._source)
        self._map[_HEADER.size:_HEADER.size + AUX_BYTES] = self._aux

    def write(self, pixels: bytes | bytearray | memoryview, *,
              width: int | None = None, height: int | None = None,
              timestamp: float = 0.0, source: tuple[int, int] = (0, 0),
              aux: bytes = b"") -> int:
        """Publish one frame. Returns its frame number.

        `width`/`height` default to the geometry this writer was opened with, and
        may be SMALLER on any given frame - a segmentation request that changes
        quality level changes its output size, and re-creating the file for that
        would mean the reader's mapping going away underneath it.

        Contract: raises if the frame does not fit the capacity, or if `pixels` is
                  not exactly `width * height * components` bytes. Both are the same
                  mistake - a geometry that disagrees with the payload - and both
                  would otherwise publish a frame that reads as a torn image.
        """
        live_w = self._width if width is None else width
        live_h = self._height if height is None else height
        expected = live_w * live_h * self._components * BYTES_PER_COMPONENT[self._dtype]
        view = memoryview(pixels)
        if view.nbytes != expected:
            raise ValueError("%d x %d x %d wants %d bytes, got %d"
                             % (live_w, live_h, self._components, expected,
                                view.nbytes))
        if HEADER_BYTES + expected > self._capacity:
            raise ValueError("%d x %d does not fit this buffer's capacity of %d "
                             "bytes (opened for %d x %d)"
                             % (live_w, live_h, self._capacity, self._width,
                                self._height))

        if len(aux) > AUX_BYTES:
            raise ValueError("aux is %d bytes, the header reserves %d"
                             % (len(aux), AUX_BYTES))
        self._width, self._height = live_w, live_h
        self._source = source
        # Padded, not truncated, and re-set every frame: aux describes THIS frame, so
        # carrying the previous frame's over would be worse than carrying zeros.
        self._aux = aux.ljust(AUX_BYTES, b"\x00")
        self._frame += 1

        # ODD, and published BEFORE the pixels move: a reader arriving now must see
        # a write in progress rather than a half-written frame.
        self._seq += 1
        self._write_header(seq=self._seq, seq_tail=self._seq - 1,
                           timestamp=timestamp)
        _fence()

        self._map[HEADER_BYTES:HEADER_BYTES + expected] = view

        # The pixels are down. Fence, THEN the trailing counter - the whole point of
        # the tail is that it is the last thing to become visible.
        _fence()
        self._seq += 1
        self._write_header(seq=self._seq, seq_tail=self._seq, timestamp=timestamp)
        return self._frame

    def close(self) -> None:
        """Idempotent, because a sidecar's teardown path runs on both the clean and
        the crashing route and must not raise on the second visit."""
        mapping = getattr(self, "_map", None)
        if mapping is not None and not mapping.closed:
            mapping.close()

    def unlink(self) -> None:
        """Remove the file. Separate from `close` on purpose: a reader may still be
        holding the mapping, and on POSIX that keeps working after the unlink."""
        self.close()
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass


class MaskReader:
    """The consuming end. Read-only, never blocks, may miss.

    Opened read-only so a bug here cannot corrupt what the writer publishes - the
    reader runs inside TouchDesigner, where the blast radius is somebody's project.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._map: mmap.mmap | None = None
        self._capacity = 0
        self._identity = (0, 0)
        self.misses = 0                 # cumulative, for the profiler to report
        self.remaps = 0                 # how often the writer replaced its buffer
        self._open()

    def _open(self) -> None:
        """(Re)map the file, remembering which file and what size it was.

        The identity is `(st_ino, st_size)`, not just the size: a writer that
        unlinks and recreates its buffer at the SAME size gets a new inode, and a
        reader still mapped to the old inode would sit on a file nobody writes to
        and report "nothing published yet" for ever.
        """
        stat = os.stat(self._path)
        if stat.st_size < HEADER_BYTES:
            raise ValueError("%s is %d bytes, too small to hold a %d-byte header"
                             % (self._path, stat.st_size, HEADER_BYTES))
        if self._map is not None and not self._map.closed:
            self._map.close()
        fd = os.open(self._path, os.O_RDONLY)
        try:
            self._map = mmap.mmap(fd, stat.st_size, access=mmap.ACCESS_READ)
        finally:
            os.close(fd)
        self._capacity = stat.st_size
        self._identity = (stat.st_ino, stat.st_size)

    def __enter__(self) -> MaskReader:
        return self

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()

    @property
    def path(self) -> str:
        return self._path

    def read(self, attempts: int = DEFAULT_ATTEMPTS) -> MaskFrame | None:
        """One coherent frame, or None.

        None means one of three things, and the caller treats them all the same way -
        keep showing the last frame:
          * nothing has been published yet: `seq` is 0, or the header is still all
            zeros because the writer is between `ftruncate` and its first stamp;
          * the writer won the race `attempts` times;
          * a torn header, whose geometry does not fit the file.

        A NON-ZERO wrong magic is different and raises. That is somebody else's file
        or a different build, which retrying forever would hide behind a mask that is
        simply always blank.
        """
        # A stat FIRST, every read, and it is not paranoia - it is the difference
        # between an exception and a SIGBUS. If the writer restarts at a different
        # quality level it re-creates its buffer at a different size, and touching a
        # page past the end of a file that SHRANK kills the process. Inside
        # TouchDesigner that is the user's whole project, with no traceback.
        #
        # `os.stat` is about a microsecond against the read's twenty, and a file that
        # has GONE is left alone deliberately: POSIX keeps the mapping valid after an
        # unlink, which is what lets a sidecar restart without faulting the reader.
        try:
            stat = os.stat(self._path)
        except OSError:
            stat = None
        if stat is not None and (stat.st_ino, stat.st_size) != self._identity:
            self._open()
            self.remaps += 1

        assert self._map is not None                    # _open sets it or raises
        for _ in range(max(1, attempts)):
            (magic, version, seq, seq_tail, width, height, components, dtype,
             frame, timestamp, source_w,
             source_h) = _HEADER.unpack(self._map[0:_HEADER.size])
            if magic == 0:
                # An all-zero header is a buffer whose writer has created the file
                # and not yet stamped it - `ftruncate` gives zeros. That is a
                # STARTUP RACE, not a wiring mistake, and it resolves in
                # microseconds, so it reads the same way an empty buffer does.
                return None
            if magic != MAGIC:
                raise ValueError("%s: magic %#x is not %#x - this is not a mask "
                                 "buffer, or the writer is a different build"
                                 % (self._path, magic, MAGIC))
            if version != VERSION:
                # Named with the FIX, because this is what an upgrade looks like: the
                # header layout changed, a buffer written by the old code is still
                # sitting in /tmp, and the answer is always the same. A version
                # mismatch that only said the numbers would send somebody reading
                # source code.
                raise ValueError(
                    "%s: buffer version %d, this reader speaks %d. Delete the file - "
                    "the writer recreates it on its next frame:  rm %s"
                    % (self._path, version, VERSION, self._path))
            if seq == 0:
                return None                 # nothing published yet, not a miss
            if seq % 2:
                self.misses += 1
                continue                    # a write is in progress
            if dtype not in BYTES_PER_COMPONENT:
                raise ValueError("%s: dtype code %d is not one this reader knows"
                                 % (self._path, dtype))
            size = width * height * components * BYTES_PER_COMPONENT[dtype]
            if size <= 0 or HEADER_BYTES + size > self._capacity:
                # A geometry that does not fit is a torn header, not a torn frame -
                # so treat it as a miss and look again rather than raising, because
                # the next read is 16 ms away and will be fine.
                self.misses += 1
                continue
            pixels = self._map[HEADER_BYTES:HEADER_BYTES + size]
            # INSIDE the seqlock, with the pixels: the aux describes this frame, so a
            # read that paired one frame's fit with another's map would be a metric
            # depth map that is quietly scaled from the wrong solve.
            aux = self._map[_HEADER.size:_HEADER.size + AUX_BYTES]

            # Fence, then re-read the counters. `seq_tail` from the SAME read as the
            # pixels is not enough: a whole write can start and finish while the
            # copy above is running, and only a fresh look at the head counter sees
            # that.
            _fence()
            after_seq, after_tail = struct.unpack_from("<II", self._map, 8)
            if after_seq != seq or after_tail != seq or seq_tail != seq:
                self.misses += 1
                continue
            return MaskFrame(width, height, components, dtype, seq, frame,
                             timestamp, bytes(pixels), source_w, source_h,
                             bytes(aux))
        return None

    def close(self) -> None:
        mapping = getattr(self, "_map", None)
        if mapping is not None and not mapping.closed:
            mapping.close()
