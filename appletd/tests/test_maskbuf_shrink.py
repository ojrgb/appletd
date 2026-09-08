"""A shared buffer is never shrunk, because shrinking one kills its reader.

`MaskWriter` opens with `O_CREAT` and no unlink, so an existing buffer keeps its
INODE. `ftruncate` to a smaller size takes the pages out from under any reader already
mapped to the old length, and touching one of those pages is **SIGBUS** — a signal, so
nothing catches it and no traceback is written.

REPRODUCED before the fix: map 1 MB, shrink to 4 KB on a second descriptor, touch
offset 512 KB, process dies with exit 138 (128 + SIGBUS).

Both directions are reachable in normal use:

  * a TOP whose resolution drops rebuilds the frames writer smaller and kills the
    SIDECAR reading it — with nothing in its log, while the panel still says Running;
  * `Segquality` from `accurate` to `fast` rebuilds the mask writer smaller and kills
    TOUCHDESIGNER, whose Script TOP reads that buffer on the main thread.

Ref: appletd/maskbuf.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from appletd.maskbuf import MaskReader, MaskWriter, capacity_for

BIG = (2016, 1512)          # `accurate` segmentation
SMALL = (256, 192)          # `fast`


def test_a_smaller_writer_does_not_shrink_the_file(tmp_path: Path) -> None:
    """The property, asserted directly so a regression fails rather than crashes."""
    path = str(tmp_path / "mask.buf")

    with MaskWriter(path, *BIG, components=1) as writer:
        writer.write(bytes(BIG[0] * BIG[1]), width=BIG[0], height=BIG[1])
        big_capacity = writer.capacity
    on_disk_after_big = os.path.getsize(path)

    with MaskWriter(path, *SMALL, components=1) as writer:
        writer.write(bytes(SMALL[0] * SMALL[1]), width=SMALL[0], height=SMALL[1])
        small_capacity = writer.capacity

    assert os.path.getsize(path) == on_disk_after_big, "the file was shrunk"
    assert small_capacity == big_capacity, (
        "the writer reported a smaller capacity than the file it is mapped to")


def test_a_bigger_writer_still_grows_the_file(tmp_path: Path) -> None:
    """Grow-only must still grow, or a large frame would not fit."""
    path = str(tmp_path / "mask.buf")
    with MaskWriter(path, *SMALL, components=1) as writer:
        writer.write(bytes(SMALL[0] * SMALL[1]), width=SMALL[0], height=SMALL[1])
    small = os.path.getsize(path)

    with MaskWriter(path, *BIG, components=1) as writer:
        writer.write(bytes(BIG[0] * BIG[1]), width=BIG[0], height=BIG[1])
    assert os.path.getsize(path) > small
    assert os.path.getsize(path) >= capacity_for(*BIG, components=1)


def test_a_reader_mapped_to_the_old_size_survives(tmp_path: Path) -> None:
    """The behaviour, in a SUBPROCESS.

    A regression here is a SIGBUS, which would take the whole pytest session down with
    it and report as a crash rather than a failed assertion. Running it out of process
    turns that back into a legible failure with the signal named.
    """
    path = tmp_path / "mask.buf"
    script = textwrap.dedent(f'''
        from appletd.maskbuf import MaskReader, MaskWriter
        path = {str(path)!r}
        big = MaskWriter(path, {BIG[0]}, {BIG[1]}, components=1)
        big.write(bytes({BIG[0]} * {BIG[1]}), width={BIG[0]}, height={BIG[1]})
        reader = MaskReader(path)            # mapped to the BIG length
        assert reader.read().width == {BIG[0]}
        big.close()

        small = MaskWriter(path, {SMALL[0]}, {SMALL[1]}, components=1)
        small.write(bytes({SMALL[0]} * {SMALL[1]}),
                    width={SMALL[0]}, height={SMALL[1]})

        frame = reader.read()                # the read that used to SIGBUS
        assert frame is not None
        assert frame.width == {SMALL[0]}, frame.width
        assert len(frame.pixels) == {SMALL[0]} * {SMALL[1]}
        print("ok")
    ''')
    done = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, timeout=60,
                          cwd=str(Path(__file__).resolve().parents[2]))
    if done.returncode < 0:
        raise AssertionError(
            "the reader died on signal %d - the buffer was shrunk under it"
            % -done.returncode)
    assert done.returncode == 0, done.stderr
    assert "ok" in done.stdout


def test_an_existing_larger_file_is_adopted_not_remade(tmp_path: Path) -> None:
    """A writer opening a file bigger than it needs takes the file's size as its
    capacity, so its own bounds check matches the mapping it actually has."""
    path = str(tmp_path / "mask.buf")
    with open(path, "wb") as handle:
        handle.truncate(capacity_for(*BIG, components=1))
    with MaskWriter(path, *SMALL, components=1) as writer:
        assert writer.capacity == capacity_for(*BIG, components=1)
        # and a frame of the size it was opened for still writes and reads back
        writer.write(bytes(SMALL[0] * SMALL[1]), width=SMALL[0], height=SMALL[1])
        frame = MaskReader(path).read()
        assert frame is not None
        assert frame.width == SMALL[0]
