"""The mask transport: the header, the seqlock, and a real two-process race.

WHAT IS ACTUALLY BEING TESTED, because most of these are cheap and one is not:
the cheap ones pin the header contract and the refusals. The expensive one runs a
WRITER IN ANOTHER PROCESS at speed and demands that every frame the reader accepts
is internally consistent - which is the only way to test a memory protocol, since
a single-process test shares an address space and proves nothing about visibility.

The consistency check is a self-describing payload: every frame is filled with a
single byte value derived from its own frame number, so a torn frame is one that
contains two different values, and the test does not need to guess which frame it
was going to get.

Ref: visionhands/maskbuf.py, docs/BUILD_PLAN.md step 9.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from visionhands.maskbuf import (
    DTYPE_U8,
    HEADER_BYTES,
    MAGIC,
    VERSION,
    MaskReader,
    MaskWriter,
    capacity_for,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def path(tmp_path: Path) -> str:
    return str(tmp_path / "mask.buf")


# -- the size contract ---------------------------------------------------


def test_capacity_is_the_header_plus_packed_pixels() -> None:
    assert capacity_for(256, 144) == HEADER_BYTES + 256 * 144
    assert capacity_for(4, 4, components=3) == HEADER_BYTES + 48


def test_capacity_refuses_what_it_cannot_size() -> None:
    """An unknown dtype must raise rather than assume one byte - a file sized for
    the wrong dtype is a SIGBUS when the pages are touched, not an exception."""
    with pytest.raises(ValueError, match="unknown dtype"):
        capacity_for(8, 8, dtype=99)
    with pytest.raises(ValueError, match="must all be positive"):
        capacity_for(0, 8)


def test_the_file_is_created_at_full_size_before_anything_is_mapped(path: str) -> None:
    """mmap over a short file is a SIGBUS on first touch, so the size has to be
    real before the mapping exists - not grown as frames arrive."""
    with MaskWriter(path, 64, 32) as writer:
        assert os.path.getsize(path) == capacity_for(64, 32) == writer.capacity


def test_the_file_is_not_world_readable(path: str) -> None:
    """A segmentation mask of somebody in a room is not 0644 data."""
    with MaskWriter(path, 8, 8):
        assert os.stat(path).st_mode & 0o077 == 0


# -- the header ----------------------------------------------------------


def test_a_fresh_buffer_has_a_header_but_no_frame(path: str) -> None:
    """`seq == 0` reads as "nothing published yet", which is NOT a miss: a reader
    that starts before the writer must not count those as lost races."""
    with MaskWriter(path, 8, 8), MaskReader(path) as reader:
        assert reader.read() is None
        assert reader.misses == 0


def test_the_header_is_little_endian_and_where_the_module_says(path: str) -> None:
    """Pinned by unpacking it by hand. The writer and the reader are different
    processes, so this layout is a wire format and moving a field is a break."""
    with MaskWriter(path, 8, 4) as writer:
        writer.write(bytes(32), timestamp=1.5, source=(1280, 720))
    with open(path, "rb") as handle:
        (magic, version, seq, tail, w, h, comp, dtype, frame, stamp, src_w,
         src_h) = struct.unpack("<IIIIIIIIQdII", handle.read(56))
    assert (magic, version) == (MAGIC, VERSION)
    assert (w, h, comp, dtype) == (8, 4, 1, DTYPE_U8)
    assert (frame, stamp) == (1, 1.5)
    assert (src_w, src_h) == (1280, 720)
    assert seq == tail and seq % 2 == 0     # settled, and even means "not writing"


def test_a_foreign_magic_raises_rather_than_retrying(path: str) -> None:
    """A wrong magic is a wiring mistake, not a race. Retrying it would turn "you
    pointed this at the wrong file" into "the mask is always blank"."""
    with MaskWriter(path, 8, 8) as writer:
        writer.write(bytes(64))
    with open(path, "r+b") as handle:
        handle.write(struct.pack("<I", 0xDEADBEEF))
    with MaskReader(path) as reader, pytest.raises(ValueError, match="not a mask"):
        reader.read()


def test_an_all_zero_header_reads_as_nothing_yet_and_not_as_an_error(tmp_path: Path) -> None:
    """The startup race, and it is a REAL one - found by a measurement harness, not
    reasoned about. `ftruncate` gives a full-size file of zeros, so between creating
    the buffer and stamping it the writer leaves a window where magic is 0. A Script
    TOP starting at the same moment as the sidecar lands in it, and an exception
    there is wrong: it resolves in microseconds and means "not ready", not "wrong
    file". A non-zero wrong magic still raises - that one really is a mistake.
    """
    blank = tmp_path / "blank.buf"
    with open(blank, "wb") as handle:
        handle.write(bytes(capacity_for(8, 8)))
    with MaskReader(str(blank)) as reader:
        assert reader.read() is None
        assert reader.misses == 0           # not ready is not a lost race


def test_the_writer_stamps_its_magic_before_the_mapping_exists(path: str) -> None:
    """Narrowing the same window from the other side: by the time `MaskWriter`
    returns, the file already identifies itself, even though no frame exists yet."""
    with MaskWriter(path, 8, 8):
        with open(path, "rb") as handle:
            magic, version = struct.unpack("<II", handle.read(8))
    assert (magic, version) == (MAGIC, VERSION)


def test_a_future_version_raises(path: str) -> None:
    with MaskWriter(path, 8, 8) as writer:
        writer.write(bytes(64))
    with open(path, "r+b") as handle:
        handle.seek(4)
        handle.write(struct.pack("<I", VERSION + 1))
    with MaskReader(path) as reader, pytest.raises(ValueError, match="this reader speaks"):
        reader.read()


def test_a_reader_refuses_a_file_too_small_to_hold_a_header(tmp_path: Path) -> None:
    stub = tmp_path / "stub"
    stub.write_bytes(b"xx")
    with pytest.raises(ValueError, match="too small"):
        MaskReader(str(stub))


# -- round trip ----------------------------------------------------------


def test_a_frame_survives_the_round_trip_byte_for_byte(path: str) -> None:
    pixels = bytes(range(256)) * 4          # 1024 bytes, every value represented
    with MaskWriter(path, 32, 32) as writer, MaskReader(path) as reader:
        writer.write(pixels, timestamp=12.25)
        got = reader.read()
    assert got is not None
    assert got.pixels == pixels
    assert (got.width, got.height, got.components) == (32, 32, 1)
    assert (got.frame, got.timestamp) == (1, 12.25)
    assert got.stride == 32


def test_the_frame_counter_is_monotonic_and_the_reader_sees_the_newest(path: str) -> None:
    """One slot, not a queue: a reader that reads once after three writes gets the
    THIRD frame, not the first. That is the design - a mask is only wanted newest."""
    with MaskWriter(path, 4, 4) as writer, MaskReader(path) as reader:
        for index in range(3):
            assert writer.write(bytes([index]) * 16) == index + 1
        got = reader.read()
    assert got is not None
    assert got.frame == 3
    assert set(got.pixels) == {2}


def test_the_payload_is_a_copy_and_not_a_view_of_the_mapping(path: str) -> None:
    """The whole point of the copy: a caller holding a frame must not have it
    change under them when the writer moves on."""
    with MaskWriter(path, 4, 4) as writer, MaskReader(path) as reader:
        writer.write(b"\x11" * 16)
        first = reader.read()
        writer.write(b"\x22" * 16)
    assert first is not None
    assert set(first.pixels) == {0x11}


def test_a_smaller_frame_may_be_published_without_remapping(path: str) -> None:
    """A quality-level change changes the mask's size, and re-creating the file for
    that would pull the mapping out from under TouchDesigner's main thread. So the
    file is sized once and the header says how much is live."""
    with MaskWriter(path, 64, 64) as writer, MaskReader(path) as reader:
        writer.write(b"\x07" * (16 * 8), width=16, height=8)
        got = reader.read()
        assert os.path.getsize(path) == capacity_for(64, 64)
    assert got is not None
    assert (got.width, got.height) == (16, 8)
    assert len(got.pixels) == 128


def test_a_payload_that_disagrees_with_its_geometry_is_refused(path: str) -> None:
    """Publishing it would put a torn image on the output and blame the transport."""
    with MaskWriter(path, 8, 8) as writer:
        with pytest.raises(ValueError, match="wants 64 bytes, got 10"):
            writer.write(bytes(10))
        with pytest.raises(ValueError, match="does not fit this buffer's capacity"):
            writer.write(bytes(999 * 999), width=999, height=999)


def test_nothing_is_published_when_a_write_is_refused(path: str) -> None:
    """The size check has to happen BEFORE the sequence counter moves, or a refused
    write leaves the buffer marked as mid-write for ever and every read misses."""
    with MaskWriter(path, 8, 8) as writer, MaskReader(path) as reader:
        writer.write(b"\x05" * 64)
        with pytest.raises(ValueError):
            writer.write(bytes(3))
        got = reader.read()
    assert got is not None
    assert set(got.pixels) == {0x05}
    assert reader.misses == 0


def test_the_source_geometry_rides_with_the_mask(path: str) -> None:
    """MEASURED and the reason this field exists: a segmentation mask does NOT match
    its input's aspect ratio - Vision returns 256x192 for a 1280x720 frame. So the
    stretch is anisotropic and only the source geometry can undo it. A consumer that
    scaled by one factor would be right on a 4:3 camera and wrong on 16:9, which is
    how this would have shipped broken."""
    with MaskWriter(path, 256, 192) as writer, MaskReader(path) as reader:
        writer.write(bytes(256 * 192), source=(1280, 720))
        got = reader.read()
    assert got is not None
    assert (got.source_width, got.source_height) == (1280, 720)
    scale_x, scale_y = got.source_scale
    assert (scale_x, scale_y) == (5.0, 3.75)        # different, and that is the point


def test_an_unstated_source_scales_by_one_rather_than_guessing(path: str) -> None:
    """Zero means "not stated". Returning 1.0 leaves a caller exactly as wrong as it
    would have been with no field at all - which is better than a plausible guess."""
    with MaskWriter(path, 8, 8) as writer, MaskReader(path) as reader:
        writer.write(bytes(64))
        got = reader.read()
    assert got is not None
    assert (got.source_width, got.source_height) == (0, 0)
    assert got.source_scale == (1.0, 1.0)


# -- the seqlock ---------------------------------------------------------


def test_an_odd_sequence_reads_as_a_miss_not_as_a_frame(path: str) -> None:
    """Forged by hand, because a single process cannot be interrupted mid-write.
    Four attempts, four misses, no frame - and crucially no exception: a miss is a
    normal outcome that the caller answers by keeping the previous frame."""
    with MaskWriter(path, 8, 8) as writer:
        writer.write(bytes(64))
    with open(path, "r+b") as handle:
        handle.seek(8)
        handle.write(struct.pack("<II", 7, 7))          # odd: a write in progress
    with MaskReader(path) as reader:
        assert reader.read() is None
        assert reader.misses == 4


def test_a_trailing_counter_that_disagrees_reads_as_a_miss(path: str) -> None:
    """The tail is what catches a whole write completing inside the reader's copy."""
    with MaskWriter(path, 8, 8) as writer:
        writer.write(bytes(64))
    with open(path, "r+b") as handle:
        handle.seek(8)
        handle.write(struct.pack("<II", 4, 2))          # head settled, tail stale
    with MaskReader(path) as reader:
        assert reader.read() is None
        assert reader.misses == 4


def test_a_torn_header_geometry_is_a_miss_and_not_a_crash(path: str) -> None:
    """A width that does not fit the file is a torn HEADER. Reading it would index
    past the mapping; raising would be wrong too, because the next frame is fine."""
    with MaskWriter(path, 8, 8) as writer:
        writer.write(bytes(64))
    with open(path, "r+b") as handle:
        handle.seek(16)
        handle.write(struct.pack("<II", 100000, 100000))
    with MaskReader(path) as reader:
        assert reader.read() is None
        assert reader.misses == 4


def test_attempts_is_honoured_and_never_less_than_one(path: str) -> None:
    with MaskWriter(path, 8, 8) as writer:
        writer.write(bytes(64))
    with open(path, "r+b") as handle:
        handle.seek(8)
        handle.write(struct.pack("<II", 3, 3))
    with MaskReader(path) as reader:
        assert reader.read(attempts=1) is None
        assert reader.misses == 1
        assert reader.read(attempts=0) is None
        assert reader.misses == 2       # 0 is clamped to one attempt, not to none


# -- lifecycle -----------------------------------------------------------


def test_close_is_idempotent(path: str) -> None:
    """A sidecar's teardown runs on both the clean and the crashing route."""
    writer = MaskWriter(path, 8, 8)
    writer.close()
    writer.close()
    reader = MaskReader(path)
    reader.close()
    reader.close()


def test_a_reader_keeps_working_after_the_file_is_unlinked(path: str) -> None:
    """POSIX: the mapping holds the inode. So a sidecar restarting and removing its
    buffer does not fault the TouchDesigner process that is still reading it."""
    writer = MaskWriter(path, 4, 4)
    writer.write(b"\x09" * 16)
    reader = MaskReader(path)
    writer.unlink()
    assert not os.path.exists(path)
    got = reader.read()
    assert got is not None and set(got.pixels) == {0x09}
    reader.close()


def test_unlink_is_idempotent(path: str) -> None:
    writer = MaskWriter(path, 4, 4)
    writer.unlink()
    writer.unlink()


# -- numpy view ----------------------------------------------------------


def test_the_numpy_view_is_row_major_and_not_flipped() -> None:
    """Shape `(height, width)`, and the FIRST row of the array is the first row of
    the payload. Vision's mask is top-down and TouchDesigner's TOPs are bottom-up,
    so a flip is needed somewhere - and it is deliberately not here, because a
    transport that reorients its payload cannot be tested against a known pattern.
    """
    with tempfile.TemporaryDirectory() as directory:
        target = os.path.join(directory, "m.buf")
        with MaskWriter(target, 3, 2) as writer, MaskReader(target) as reader:
            writer.write(bytes([1, 2, 3, 4, 5, 6]))
            got = reader.read()
    assert got is not None
    array = got.as_numpy()
    assert array.shape == (2, 3)
    assert array[0].tolist() == [1, 2, 3]
    assert array[1].tolist() == [4, 5, 6]


# -- the only test that proves anything about memory ---------------------

_WRITER_PROGRAM = r"""
import sys, time
sys.path.insert(0, %(root)r)
from visionhands.maskbuf import MaskWriter

path, width, height, seconds = sys.argv[1], %(w)d, %(h)d, %(seconds)f
size = width * height
deadline = time.monotonic() + seconds
with MaskWriter(path, width, height) as writer:
    frame = 0
    while time.monotonic() < deadline:
        frame += 1
        # Every byte of the frame is the same value, derived from the frame number.
        # So a TORN frame is one holding two different values, and the reader can
        # say so without knowing which frame it was going to get.
        writer.write(bytes([frame %% 251]) * size, timestamp=time.monotonic())
    print(frame)
"""


@pytest.mark.parametrize("shape", [(64, 64), (256, 144)])
def test_two_processes_and_every_accepted_frame_is_whole(
        path: str, shape: tuple[int, int]) -> None:
    """THE test. A writer in another process, going as fast as it can, while this
    one reads - and every frame the reader ACCEPTS must be internally consistent.

    Why another process: a single-process test shares one address space and one
    interpreter, so it cannot expose a visibility or ordering problem. This is the
    only shape that tests the thing the module claims.

    What it does NOT prove: that the fence is sufficient under the ARM64 memory
    model. Absence of tearing over a few thousand frames is evidence, not proof, and
    maskbuf.py says so. What it does prove is that the protocol's own bookkeeping is
    right - counters, geometry, refusals - under real concurrency.
    """
    width, height = shape
    program = _WRITER_PROGRAM % {"root": REPO_ROOT, "w": width, "h": height,
                                 "seconds": 1.5}
    child = subprocess.Popen([sys.executable, "-c", program, path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    try:
        # Wait for the file, then for a first frame. The writer creates it, so a
        # reader opened too early is a FileNotFoundError rather than a miss.
        for _ in range(500):
            if os.path.exists(path) and os.path.getsize(path) >= HEADER_BYTES:
                break
            time.sleep(0.01)
        assert os.path.exists(path), "the writer never created its buffer"

        torn = []
        accepted = 0
        misses_at_start = 0
        seen_frames = []
        with MaskReader(path) as reader:
            misses_at_start = reader.misses
            deadline = time.monotonic() + 1.4
            while time.monotonic() < deadline:
                got = reader.read()
                if got is None:
                    continue
                accepted += 1
                seen_frames.append(got.frame)
                values = set(got.pixels)
                if len(values) != 1:
                    torn.append((got.frame, sorted(values)[:4]))
                elif values != {got.frame % 251}:
                    # Consistent bytes but not the ones this frame number should
                    # carry: a header from one frame paired with another's pixels,
                    # which is the failure the trailing counter exists to catch.
                    torn.append((got.frame, sorted(values)))
                assert (got.width, got.height) == (width, height)
            total_misses = reader.misses - misses_at_start
    finally:
        child.wait(timeout=10)
        stdout, stderr = child.communicate()

    assert child.returncode == 0, "writer failed: %s" % stderr
    assert accepted > 50, "only %d frames accepted - the race is not being run" % accepted
    assert not torn, "%d torn frames out of %d accepted: %r" % (
        len(torn), accepted, torn[:5])
    # Monotonic, because there is one writer and one slot. A frame number going
    # BACKWARDS would mean a stale header being accepted.
    assert seen_frames == sorted(seen_frames)
    # Not asserted as a bound, only recorded: how often the reader lost. It is
    # workload-dependent and a threshold here would be a flaky test.
    print("accepted %d frames, %d misses, %d written by the child"
          % (accepted, total_misses, int(stdout.strip() or 0)))
