"""TOP Input's transport, without TouchDesigner and without a camera.

What is checked here is the seam: bytes written the way the Script TOP writes them come
back the right shape, and turn into something Vision's live path accepts. Whether a
frame from a TOP finds a hand is `test_engine`'s question, not this file's.
"""

from __future__ import annotations

import pathlib
from pathlib import Path

import numpy
import pytest

from appletd.frames import FrameBufferReader, FrameError, sample_buffer_from_bgra
from appletd.maskbuf import MaskWriter


def _bgra(width: int, height: int, value: int = 0) -> bytes:
    return bytes(bytearray([value]) * (width * height * 4))


def test_a_written_frame_comes_back_the_same_shape(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "frames.buf")
    with MaskWriter(path, 32, 16, components=4) as writer:
        writer.write(_bgra(32, 16, 7))
        reader = FrameBufferReader(path)
        frame = reader.latest()
        assert frame is not None
        pixels, _captured_at = frame
        assert pixels.shape == (16, 32, 4)
        assert int(pixels[0][0][0]) == 7
        reader.close()


def test_the_same_frame_is_only_delivered_once(tmp_path: pathlib.Path) -> None:
    """The reader is polled faster than TouchDesigner writes, so "nothing new" is the
    common case and has to be cheap and unambiguous."""
    path = str(tmp_path / "frames.buf")
    with MaskWriter(path, 8, 8, components=4) as writer:
        writer.write(_bgra(8, 8, 1))
        reader = FrameBufferReader(path)
        assert reader.latest() is not None
        assert reader.latest() is None
        writer.write(_bgra(8, 8, 2))
        assert reader.latest() is not None
        assert reader.frames_read == 2
        reader.close()


def test_a_mask_buffer_is_refused_rather_than_reshaped(tmp_path: pathlib.Path) -> None:
    """One component is a MASK. Reshaping it as BGRA would hand Vision a quarter of
    an image and no error at all."""
    path = str(tmp_path / "mask.buf")
    with MaskWriter(path, 16, 16, components=1) as writer:
        writer.write(bytes(16 * 16))
        reader = FrameBufferReader(path)
        with pytest.raises(FrameError, match="four components"):
            reader.latest()
        reader.close()


def test_bgra_becomes_a_sample_buffer_the_live_path_accepts() -> None:
    """The whole point of the module: a frame from a TOP enters through the SAME
    method the camera delegate calls, so nothing downstream can tell them apart."""
    array = numpy.zeros((16, 32, 4), dtype=numpy.uint8)
    buffer = sample_buffer_from_bgra(array, 1.0)
    assert buffer is not None


def test_a_dropped_frame_is_counted_not_hidden(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "frames.buf")
    with MaskWriter(path, 8, 8, components=4) as writer:
        writer.write(_bgra(8, 8, 1))
        reader = FrameBufferReader(path)
        reader.latest()
        writer.write(_bgra(8, 8, 2))
        writer.write(_bgra(8, 8, 3))       # published before the reader looked again
        reader.latest()
        assert reader.frames_skipped == 1
        reader.close()


def test_the_frame_is_writable_because_vision_needs_it_to_be(tmp_path: pathlib.Path) -> None:
    """`CVPixelBufferCreateWithBytes` refuses a read-only array, and
    `numpy.frombuffer` over `bytes` produces exactly that. Asserted on a frame that
    came through the BUFFER rather than on a hand-built array, because a hand-built
    one is writable and hides this."""
    path = str(tmp_path / "frames.buf")
    with MaskWriter(path, 8, 8, components=4) as writer:
        writer.write(_bgra(8, 8, 3))
        reader = FrameBufferReader(path)
        got = reader.latest()
        assert got is not None
        pixels, captured_at = got
        assert pixels.flags.writeable
        assert sample_buffer_from_bgra(pixels, captured_at) is not None
        reader.close()


def test_a_missing_buffer_is_a_wait_and_not_an_error(tmp_path: Path) -> None:
    """The one reader whose file TOUCHDESIGNER writes.

    Every other shared buffer is written by the sidecar and exists before anything
    reads it. This one does not exist until a Script TOP has cooked, so opening it
    eagerly killed the sidecar with FileNotFoundError whenever it started in TOP
    Input mode before a frame had been published - the normal case.
    """
    from appletd.frames import FrameBufferReader

    missing = tmp_path / "never_written.buf"
    reader = FrameBufferReader(str(missing))
    assert reader.latest() is None
    assert reader.latest() is None
    assert reader.waits_for_writer == 2
    reader.close()


def test_a_truncated_buffer_is_also_a_wait(tmp_path: Path) -> None:
    """The writer got as far as the file and not as far as the header."""
    from appletd.frames import FrameBufferReader

    half_written = tmp_path / "partial.buf"
    half_written.write_bytes(b"\x00" * 8)
    reader = FrameBufferReader(str(half_written))
    assert reader.latest() is None
    assert reader.waits_for_writer == 1
    reader.close()


def test_it_opens_once_the_writer_appears(tmp_path: Path) -> None:
    """And picks the buffer up without being restarted."""
    from appletd.frames import FrameBufferReader
    from appletd.maskbuf import MaskWriter

    path = tmp_path / "late.buf"
    reader = FrameBufferReader(str(path))
    assert reader.latest() is None
    assert reader.waits_for_writer == 1

    with MaskWriter(str(path), 8, 8, components=4) as writer:
        writer.write(_bgra(8, 8, 3))
        got = reader.latest()
    assert got is not None, "the reader did not pick up the buffer once it existed"
    assert reader.frames_read == 1
    reader.close()
