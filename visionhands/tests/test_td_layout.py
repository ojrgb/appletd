"""The generated layout: it has to be data, and the data has to not collide.

Two builders once placed their group at the identical coordinate and nothing in
TouchDesigner or in either script could notice - a network is not something a test
can look at, so what a test CAN do is assert the numbers that produce it.

Ref: visionhands/td_layout.py, docs/BUILD_PLAN.md step 10.
"""

from __future__ import annotations

import pytest

from visionhands.streams import STREAM_NAMES
from visionhands.td_layout import (
    COL_W,
    COMP_H,
    COMP_W,
    MASTER_NODES,
    ROW_H,
    STREAM_NODES,
    master_xy,
    overlaps,
    stream_row,
    stream_xy,
)


def test_no_two_operators_share_a_place() -> None:
    """The bug this module exists for: td_add_filter.py and td_add_coords.py both
    placed their group at (-400, -300), exactly on top of each other."""
    assert overlaps(STREAM_NODES) == []
    assert overlaps(MASTER_NODES) == []


def test_the_grid_leaves_a_gap_beside_the_widest_node() -> None:
    """A column narrower than a COMP is a layout that overlaps by construction."""
    assert COL_W > COMP_W - COL_W          # a one-column step clears a COMP
    assert ROW_H > COMP_H - ROW_H


def test_the_data_path_is_one_row_and_reads_left_to_right() -> None:
    """Row 0 is the data path, and the order of the row IS the order of the chain -
    which is the whole reason a reader can follow it."""
    path = ["in1", "filter", "coords", "merge_out", "screen_only", "out1"]
    xs = [stream_xy(name)[0] for name in path]
    assert xs == sorted(xs), list(zip(path, xs, strict=True))
    assert all(stream_xy(name)[1] == 0 for name in path)


def test_everything_that_only_reads_the_stream_hangs_below_the_path() -> None:
    """So the ROW a node is on says whether it is in the data path or beside it.
    `filter` is in the path, which is why it cannot be frozen by allowCooking."""
    for name in ("derive_chop", "temporal", "latches"):
        assert stream_xy(name)[1] < 0, name


def test_the_notes_sit_out_of_the_wire_path() -> None:
    """Above row 0, so a Text DAT never reads as part of the chain."""
    assert stream_xy("notes")[1] > 0
    assert master_xy("notes")[1] > 0


def test_an_unknown_name_raises_rather_than_defaulting() -> None:
    """A builder that placed a node the table does not know about would put it
    wherever the fallback was - which is the collision this module prevents."""
    with pytest.raises(KeyError, match="no stream position"):
        stream_xy("nonesuch")
    with pytest.raises(KeyError, match="no master position"):
        master_xy("nonesuch")


def test_the_streams_are_stacked_and_do_not_collide() -> None:
    rows = [stream_row(index) for index in range(len(STREAM_NAMES))]
    assert rows == sorted(rows, reverse=True)
    assert len(set(rows)) == len(rows)
    assert min(abs(a - b) for a in rows for b in rows if a != b) > COMP_H


def test_every_operator_a_builder_places_has_an_entry() -> None:
    """The names the seven builders ask for, listed here so adding an operator to a
    builder without giving it a home fails in the test suite rather than by landing
    on top of something."""
    for name in ("in1", "filter", "coords", "merge_out", "screen_only", "out1",
                 "derive_chop", "derive_callbacks", "temporal", "latches",
                 "notes", "screen_only_notes"):
        assert name in STREAM_NODES, name
    for name in ("status", "sidecar_control", "sidecar_callbacks", "sidecar_exit",
                 "filter_callbacks", "groups_callbacks",
                 "lat_threshold_callbacks", "screenspace_callbacks",
                 "profiler", "notes"):
        assert name in MASTER_NODES, name


def test_overlaps_actually_detects_an_overlap() -> None:
    """A collision checker that never fires is worse than none - and this one is the
    only thing standing between two builders and the same coordinate."""
    assert overlaps({"a": (0, 0), "b": (0, 0)}) == [("a", "b")]
    assert overlaps({"a": (0, 0), "b": (COMP_W, 0)}) == []
    assert overlaps({"a": (0, 0), "b": (0, COMP_H)}) == []
    assert overlaps({"a": (0, 0), "b": (COMP_W - 1, COMP_H - 1)}) == [("a", "b")]
