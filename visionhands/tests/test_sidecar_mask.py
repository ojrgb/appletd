"""The segmentation mask through the sidecar: the buffer, the geometry, the teardown.

NO CAMERA AND NO PYOBJC. The mask itself is a stand-in NamedTuple with the four
fields `_write_mask` actually reads, so this file runs on a machine with no
frameworks at all - the same property `test_boundaries.py` enforces for the rest of
the package. What is being tested is the SIDECAR's half: that it sizes the buffer
from what arrived, states the source geometry, counts what it wrote, and releases the
buffer in the right order. Vision's half is `test_segmentation.py`, and the
transport's is `test_maskbuf.py`.

WHY THE TEARDOWN ORDER GETS ITS OWN TEST. `_write_mask` runs on the capture queue.
If `stop()` closed the buffer before stopping the source, a frame already in flight
would be writing into a closed mmap from a thread still running - a ValueError at
best and a segfault at worst. That ordering is not visible by reading either method
alone, so it is asserted here.

Ref: visionhands/sidecar.py, visionhands/maskbuf.py, DESIGN.md 2.19.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

import pytest

from visionhands.maskbuf import MaskReader
from visionhands.sidecar import Sidecar
from visionhands.source import FakeSource
from visionhands.streams import REQUEST_SEGMENT, STREAM_HANDS
from visionhands.types import blank_frame

if TYPE_CHECKING:
    # For the cast in `_write` only. A TYPE_CHECKING import, so this file still runs
    # with no pyobjc present - which is the property the module docstring claims.
    from visionhands.segmentation import MaskImage


class StandInMask(NamedTuple):
    """The four fields `Sidecar._write_mask` reads off a `MaskImage`.

    A stand-in rather than the real thing, so this file needs no pyobjc. If
    `_write_mask` ever starts reading a fifth field, this breaks - which is the
    point: it is a structural contract, not a mock that agrees with anything.
    """

    width: int
    height: int
    pixels: bytes
    captured_at: float


def _mask(width: int = 256, height: int = 192, value: int = 0x40,
          captured_at: float = 1.5) -> StandInMask:
    return StandInMask(width, height, bytes([value]) * (width * height), captured_at)


def _sidecar(tmp_path: Path,
             streams: tuple[str, ...] = (STREAM_HANDS, REQUEST_SEGMENT)) -> Sidecar:
    """A sidecar with a fake source, so nothing opens a camera."""
    return Sidecar(source=FakeSource(lambda seq: blank_frame(), interval_s=0.01),
                   streams=streams, mask_path=str(tmp_path / "mask.buf"))


def _write(sidecar: Sidecar, mask: StandInMask) -> None:
    """Publish a stand-in mask.

    ONE cast, here, rather than an ignore comment on twelve call sites.
    `_write_mask` is annotated for a `MaskImage` and `StandInMask` is structurally
    the same four fields - which is the point of the stand-in, and the cast is where
    that claim is written down.
    """
    sidecar._write_mask(cast("MaskImage", mask))


# -- the buffer ----------------------------------------------------------


def test_no_buffer_exists_until_the_first_mask_arrives(tmp_path: Path) -> None:
    """Lazily, and not at construction: the size depends on the quality level AND
    the camera's orientation, and a sidecar that opened the camera and found nobody
    should not leave a file behind either."""
    import os
    sidecar = _sidecar(tmp_path)
    try:
        assert not os.path.exists(sidecar.mask_path)
        assert sidecar.n_masks_written == 0
    finally:
        sidecar.stop()


def test_the_buffer_is_sized_from_the_mask_that_arrived(tmp_path: Path) -> None:
    """No table of geometries per quality per orientation. MEASURED: `fast` gives
    256x192 for a landscape frame and 192x256 for a portrait one, and `accurate`
    gives 2016x1512 - so anything guessed in advance is a buffer that refuses every
    write on the day Vision changes a number."""
    from visionhands.maskbuf import capacity_for

    sidecar = _sidecar(tmp_path)
    try:
        _write(sidecar, _mask(320, 240))
        assert sidecar.n_masks_written == 1
        with MaskReader(sidecar.mask_path) as reader:
            frame = reader.read()
        assert frame is not None
        assert (frame.width, frame.height) == (320, 240)
        import os
        assert os.path.getsize(sidecar.mask_path) == capacity_for(320, 240)
    finally:
        sidecar.stop()


def test_the_buffer_is_created_once_and_reused(tmp_path: Path) -> None:
    sidecar = _sidecar(tmp_path)
    try:
        _write(sidecar, _mask(value=0x11))
        first = sidecar._mask_writer
        _write(sidecar, _mask(value=0x22))
        assert sidecar._mask_writer is first
        assert sidecar.n_masks_written == 2
        with MaskReader(sidecar.mask_path) as reader:
            frame = reader.read()
        assert frame is not None and set(frame.pixels) == {0x22}
    finally:
        sidecar.stop()


def test_a_mask_larger_than_the_buffer_raises_rather_than_truncating(tmp_path: Path) -> None:
    """The engine catches this, counts the frame dropped and records why - so a
    quality change mid-session costs masks and says so, rather than publishing the
    top third of a frame."""
    sidecar = _sidecar(tmp_path)
    try:
        _write(sidecar, _mask(256, 192))
        with pytest.raises(ValueError, match="does not fit this buffer's capacity"):
            _write(sidecar, _mask(2016, 1512))
    finally:
        sidecar.stop()


# -- the source geometry -------------------------------------------------


def test_the_source_geometry_is_unstated_when_the_camera_has_not_delivered(
        tmp_path: Path) -> None:
    """(0, 0), not a plausible default. `MaskFrame.source_scale` turns it into
    (1.0, 1.0), which leaves a consumer exactly as wrong as having no field - and
    not misled into undoing a stretch by the wrong factor."""
    sidecar = _sidecar(tmp_path)
    try:
        assert sidecar._source_px() == (0, 0)
        _write(sidecar, _mask())
        with MaskReader(sidecar.mask_path) as reader:
            frame = reader.read()
        assert frame is not None
        assert (frame.source_width, frame.source_height) == (0, 0)
        assert frame.source_scale == (1.0, 1.0)
    finally:
        sidecar.stop()


def test_the_source_geometry_is_what_the_camera_DELIVERED(tmp_path: Path) -> None:
    """Delivered, not requested. The session preset silently reverts
    `setActiveFormat_` and two spike runs produced 1080p while the log said 720p
    (DESIGN.md 3) - a mask scaled against the requested size would be off by the
    difference, invisibly."""

    class Engine:
        delivered_px = (1920, 1080)

    sidecar = _sidecar(tmp_path)
    try:
        sidecar.source._engine = Engine()          # type: ignore[attr-defined]
        assert sidecar._source_px() == (1920, 1080)
        _write(sidecar, _mask(256, 192))
        with MaskReader(sidecar.mask_path) as reader:
            frame = reader.read()
        assert frame is not None
        assert (frame.source_width, frame.source_height) == (1920, 1080)
        # The two scale factors DIFFER, which is the whole reason the field exists:
        # 1920/256 is not 1080/192.
        scale_x, scale_y = frame.source_scale
        assert scale_x == 7.5
        assert scale_y == 5.625
    finally:
        sidecar.stop()


def test_the_timestamp_rides_with_the_mask(tmp_path: Path) -> None:
    sidecar = _sidecar(tmp_path)
    try:
        _write(sidecar, _mask(captured_at=42.25))
        with MaskReader(sidecar.mask_path) as reader:
            frame = reader.read()
        assert frame is not None and frame.timestamp == 42.25
    finally:
        sidecar.stop()


# -- teardown ------------------------------------------------------------


def test_stop_stops_the_source_BEFORE_closing_the_buffer(tmp_path: Path) -> None:
    """The order is load-bearing and invisible in either method alone. `_write_mask`
    runs on the capture queue; closing the buffer first would leave a frame in flight
    writing into a closed mmap from a thread still running."""
    order = []

    class WatchedSource(FakeSource):
        def stop(self) -> None:
            order.append("source")
            super().stop()

    sidecar = Sidecar(source=WatchedSource(lambda seq: blank_frame(),
                                           interval_s=0.01),
                      streams=(STREAM_HANDS, REQUEST_SEGMENT),
                      mask_path=str(tmp_path / "mask.buf"))
    _write(sidecar, _mask())
    writer = sidecar._mask_writer
    assert writer is not None

    original_close = writer.close

    def watched_close() -> None:
        order.append("buffer")
        original_close()

    writer.close = watched_close                   # type: ignore[method-assign]
    sidecar.stop()
    assert order == ["source", "buffer"]


def test_stop_leaves_the_FILE_so_a_reader_keeps_its_last_mask(tmp_path: Path) -> None:
    """`close`, not `unlink`. A reader inside TouchDesigner may still hold the
    mapping, and on POSIX that keeps working after the file goes - so leaving the
    file is what lets TD show the last mask instead of going black the instant the
    sidecar exits."""
    import os
    sidecar = _sidecar(tmp_path)
    _write(sidecar, _mask(value=0x5A))
    path = sidecar.mask_path
    sidecar.stop()
    assert os.path.exists(path)
    with MaskReader(path) as reader:
        frame = reader.read()
    assert frame is not None and set(frame.pixels) == {0x5A}


def test_stop_is_idempotent_with_a_buffer_open(tmp_path: Path) -> None:
    sidecar = _sidecar(tmp_path)
    _write(sidecar, _mask())
    sidecar.stop()
    sidecar.stop()
    assert sidecar._mask_writer is None


def test_stop_closes_the_socket_even_if_stopping_the_source_raises(tmp_path: Path) -> None:
    """Both releases are in a finally, and each in its own, so a camera that will
    not stop cannot leave the socket and the buffer behind as well."""

    class BadSource(FakeSource):
        def stop(self) -> None:
            raise RuntimeError("camera will not let go")

    sidecar = Sidecar(source=BadSource(lambda seq: blank_frame(), interval_s=0.01),
                      streams=(STREAM_HANDS, REQUEST_SEGMENT),
                      mask_path=str(tmp_path / "mask.buf"))
    _write(sidecar, _mask())
    with pytest.raises(RuntimeError, match="will not let go"):
        sidecar.stop()
    assert sidecar._mask_writer is None
    assert sidecar.socket.fileno() == -1           # closed


# -- the wiring, without starting anything -------------------------------


def test_the_real_source_is_given_the_sidecars_writer_as_its_destination() -> None:
    """Constructing `InProcessSource` opens NOTHING - the camera is opened by
    `start()` - so this checks the whole wiring with no device and no pyobjc: the
    sidecar's `_write_mask` really is what the engine will publish masks to."""
    from visionhands.source import InProcessSource

    sidecar = Sidecar(streams=(STREAM_HANDS, REQUEST_SEGMENT))
    try:
        assert isinstance(sidecar.source, InProcessSource)
        assert sidecar.source._on_mask is not None
        assert sidecar.source._on_mask.__func__ is Sidecar._write_mask  # type: ignore[attr-defined]
    finally:
        sidecar.socket.close()


def test_no_mask_destination_is_wired_when_segment_is_not_requested() -> None:
    """Both or neither. A detector with nowhere to publish would burn 2.21 ms a
    frame and drop the result, so `on_mask` is None and `source.start()` never builds
    the request."""
    sidecar = Sidecar(streams=(STREAM_HANDS,))
    try:
        assert sidecar.source._on_mask is None      # type: ignore[attr-defined]
    finally:
        sidecar.socket.close()


def test_the_quality_reaches_the_source() -> None:
    sidecar = Sidecar(streams=(STREAM_HANDS, REQUEST_SEGMENT),
                      mask_quality="balanced")
    try:
        assert sidecar.source._mask_quality == "balanced"   # type: ignore[attr-defined]
        assert sidecar.mask_quality == "balanced"
    finally:
        sidecar.socket.close()


def test_requesting_segment_with_no_destination_is_recorded_not_silent() -> None:
    """A launch flag that quietly did nothing is worse than one that refuses. The
    source cannot raise - it is constructed before anything is known about the
    camera - so it records, and `errors` is where a caller looks."""
    from visionhands.source import InProcessSource

    source = InProcessSource(streams=(STREAM_HANDS, REQUEST_SEGMENT), on_mask=None)
    assert source._on_mask is None
    # The error is appended by `start()`, which needs a camera, so what is asserted
    # here is the pre-condition that makes it happen rather than the message.
    assert REQUEST_SEGMENT in source._streams
