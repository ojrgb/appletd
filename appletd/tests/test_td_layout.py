"""The generated layout: it has to be data, and the data has to not collide.

Two builders once placed their group at the identical coordinate and nothing in
TouchDesigner or in either script could notice - a network is not something a test
can look at, so what a test CAN do is assert the numbers that produce it.

Ref: appletd/td_layout.py, docs/BUILD_PLAN.md step 10.
"""

from __future__ import annotations

import pytest

from appletd.streams import STREAM_NAMES
from appletd.td_layout import (
    COL_W,
    COMP_H,
    COMP_W,
    MASTER_CHAIN,
    MASTER_NODES,
    ROW_H,
    STREAM_NODES,
    chain_pairs,
    master_xy,
    overlaps,
    placement,
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
    path = ["in1", "filter", "merge_out", "out1"]
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
    for name in ("in1", "filter", "merge_out", "out1",
                 "derive_chop", "derive_callbacks", "temporal", "latches",
                 "notes"):
        assert name in STREAM_NODES, name
    # And the two that LEFT the streams on 2026-08-24 must not still have a stream
    # position, or a stale entry would place a node nothing builds (BUILD_PLAN 25).
    for gone in ("coords", "screen_only", "screen_only_notes"):
        assert gone not in STREAM_NODES, gone
    for name in ("status", "sidecar_control", "sidecar_callbacks", "sidecar_exit",
                 "filter_callbacks", "groups_callbacks",
                 "lat_threshold_callbacks", "screenspace_callbacks",
                 "profiler", "notes",
                 # The single output path - six stages as of 2026-08-24.
                 *MASTER_CHAIN):
        assert name in MASTER_NODES, name


def test_the_single_output_path_reads_left_to_right() -> None:
    """merge -> trim -> out, on the hands row, in that order. The three streams sit
    at x = 0, so the path has to start to the RIGHT of them and stay on one row -
    a reader should be able to follow the only output without scrolling."""
    xs = [master_xy(name)[0] for name in MASTER_CHAIN]
    assert xs == sorted(xs)
    assert xs[0] > 0  # right of the stream COMPs, which sit at x = 0
    ys = {master_xy(name)[1]
          for name in ("merge_streams", "trim_empty", "out1")}
    assert ys == {stream_row(0)}


def test_overlaps_actually_detects_an_overlap() -> None:
    """A collision checker that never fires is worse than none - and this one is the
    only thing standing between two builders and the same coordinate."""
    assert overlaps({"a": (0, 0), "b": (0, 0)}) == [("a", "b")]
    assert overlaps({"a": (0, 0), "b": (COMP_W, 0)}) == []
    assert overlaps({"a": (0, 0), "b": (0, COMP_H)}) == []
    assert overlaps({"a": (0, 0), "b": (COMP_W - 1, COMP_H - 1)}) == [("a", "b")]


def test_keeplayout_holds_an_existing_node_and_never_a_new_one() -> None:
    """A new node has to be placed whatever the flag says: TouchDesigner puts a
    freshly created operator wherever it likes, so "leave it alone" would mean
    piling every new operator somewhere arbitrary."""
    xy = (200, -300)
    assert placement(xy, keep_layout=False, existed=False) == xy
    assert placement(xy, keep_layout=False, existed=True) == xy
    assert placement(xy, keep_layout=True, existed=False) == xy
    assert placement(xy, keep_layout=True, existed=True) is None


# ---------------------------------------------------------------------------
# The master chain, built by four different builders
# ---------------------------------------------------------------------------
def test_the_chain_skips_what_does_not_exist_yet() -> None:
    """A partial build has to produce a shorter WORKING chain, not a broken one.
    Every builder calls `rewire_master_chain` after creating its own stage, and the
    stages arrive in `tools/td_rebuild.py` order rather than chain order."""
    assert chain_pairs(["merge_streams", "trim_empty", "out1"]) == [
        ("merge_streams", "trim_empty"), ("trim_empty", "out1")]
    assert chain_pairs(["merge_streams", "out1"]) == [("merge_streams", "out1")]
    assert chain_pairs(["out1"]) == []
    assert chain_pairs([]) == []


def test_the_chain_is_in_chain_order_however_the_names_arrive() -> None:
    """`present` comes from whatever a builder found in the network, in no
    particular order. The order of the RESULT is the contract."""
    assert chain_pairs(reversed(MASTER_CHAIN)) == chain_pairs(MASTER_CHAIN)
    assert len(chain_pairs(MASTER_CHAIN)) == len(MASTER_CHAIN) - 1


def test_trim_empty_is_always_the_last_stage_before_the_output() -> None:
    """It has to be: a frozen COMP HOLDS its channels rather than dropping them, so
    a coords half frozen by `Coordstx` leaves stale coordinates on the output unless
    something downstream removes them. That is what this operator is for
    (DESIGN.md 2.15), and it stops being true the moment it is not last."""
    assert MASTER_CHAIN[-2:] == ("trim_empty", "out1")
    for present in (MASTER_CHAIN, ["merge_streams", "coords", "trim_empty", "out1"],
                    ["coords", "screen_only", "trim_empty", "out1"]):
        pairs = chain_pairs(present)
        assert pairs[-1] == ("trim_empty", "out1"), present


def test_screen_only_comes_after_coords_and_before_the_trim() -> None:
    """After, because it deletes the raw channels coords READS - in front of it there
    would be nothing left to compose. Before the trim, because the trim's job is
    frozen groups and this one's is duplicate copies."""
    order = list(MASTER_CHAIN)
    assert order.index("coords") < order.index("screen_only")
    assert order.index("screen_only") < order.index("trim_empty")


def test_early_trim_runs_before_the_composition() -> None:
    """The entire point of a second trim. What it drops is never converted into a
    coordinate space, which is what makes `Onefaceonly` and `Fingertipsonly` cost
    savings rather than list-shorteners."""
    order = list(MASTER_CHAIN)
    assert order.index("early_trim") < order.index("coords")
