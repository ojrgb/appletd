"""Where every generated operator sits. The one place any of it is decided.

WHY THIS EXISTS. Seven different scripts build the inside of `/project1/appletd`, and
each one owns a few operators. Every one of them carried its own coordinates, and
the results were exactly what that arrangement predicts: `tools/td_add_filter.py`
and `tools/td_add_coords.py` both placed their group at (-400, -300), directly on
top of each other, and nothing anywhere could notice. Inside the big groups it was
worse - `temporal` and `latches` positioned 125 operators with a SINGLE running
column counter, so every operator went one column further right whatever ROW it was
on, and the two networks came out 11,590 and 9,800 units wide with their rows all
starting somewhere different.

So the coordinates are data, in one module, and a test asserts they do not collide.
A builder asks this module where its node goes.

THE GRID. A TouchDesigner CHOP or DAT node is 130 x 90 and a base COMP is
160 x 130 - MEASURED, from `nodeWidth`/`nodeHeight` on a live network, not assumed.
So a 200-wide column leaves a 40-unit gap beside a COMP and a 150-tall row leaves
20 beside one; both are comfortable and neither wastes screen.

WHAT IS NOT HERE. The positions of operators INSIDE a group. Those are generated -
one row per concern, one column per stage, with a counter per row - and the row
constants live at the top of the builder that generates them, next to the code that
decides what the rows ARE. Putting them here would separate a row's name from the
operators it holds.

Pure stdlib. No pyobjc, no TouchDesigner - it is a table of numbers, and the same
rule `types.py` follows.

Thread: pure data and pure functions. Safe anywhere.
Ref: docs/BUILD_PLAN.md step 10, DESIGN.md 6.5 (the COMP structure).
"""

from __future__ import annotations

from typing import Final

# MEASURED on a live network: a CHOP/DAT node is 130 x 90, a base COMP 160 x 130.
NODE_W: Final = 130
NODE_H: Final = 90
COMP_W: Final = 160
COMP_H: Final = 130

# The grid every generated network uses.
COL_W: Final = 200
ROW_H: Final = 150

# ---------------------------------------------------------------------------
# Inside a STREAM (`/project1/appletd/hands` and its siblings)
#
# The data path runs left to right along row 0; everything that only READS the
# stream hangs below it, one row each, so the row a node is on says whether it is
# in the path or beside it.
#
#   in1 -> filter -> coords ----+
#                              |
#          derive_chop --------+-> merge_out -> screen_only -> out1
#          temporal -----------+
#          latches ------------+
#
# `filter` is the only group IN the data path, which is why it cannot be gated by
# `allowCooking` - freezing it would freeze every position rather than bypass the
# filter (docs/BUILD_PLAN.md 7.2).
# ---------------------------------------------------------------------------
STREAM_NODES: Final[dict[str, tuple[int, int]]] = {
    # the data path, row 0
    "in1": (-4 * COL_W, 0),
    "filter": (-3 * COL_W, 0),
    "coords": (-1 * COL_W, 0),
    "merge_out": (2 * COL_W, 0),
    "screen_only": (3 * COL_W, 0),
    "out1": (4 * COL_W, 0),
    # the attribute layer, one row per group, all reading `filter`
    "derive_chop": (-1 * COL_W, -2 * ROW_H),
    "derive_callbacks": (-3 * COL_W, -2 * ROW_H),
    "temporal": (-1 * COL_W, -4 * ROW_H),
    "latches": (-1 * COL_W, -6 * ROW_H),
    # documentation, above the path and out of it
    "notes": (-4 * COL_W, 2 * ROW_H),
    "screen_only_notes": (3 * COL_W, 2 * ROW_H),
    # `tools/td_verify_latches.py`'s stored counter baseline. Not part of the
    # network - it holds one run's numbers so the next run can diff them - so it
    # sits well clear of everything, below the attribute layer.
    "ver_baseline": (-4 * COL_W, -8 * ROW_H),
}

# ---------------------------------------------------------------------------
# Inside the MASTER COMP (`/project1/appletd`)
#
# One row per stream, and the sidecar's own machinery below them all.
# ---------------------------------------------------------------------------
MASTER_ROW_H: Final = 400

MASTER_NODES: Final[dict[str, tuple[int, int]]] = {
    # The single output path, on row 0 to the right of the three stream COMPs.
    # ONE output as of 2026-08-22: the three streams merge, the merge feeds a Select
    # that keeps only what is being computed, and that feeds the only Out CHOP. A
    # component handed to a beginner should not open with 1,861 channels.
    #
    # A SELECT, not a Delete, and the reason is measured: for the same reduction of
    # the same 1,764 channels a Delete CHOP costs 0.6697 ms against the Select's
    # 0.0555 ms, and 3.3618 ms if the list is long. DESIGN.md 2.15.
    "merge_streams": (COL_W, 0),
    "trim_empty": (2 * COL_W, 0),
    "out1": (3 * COL_W, 0),
    # The housekeeping channels, one row BELOW the output path - the same convention
    # the streams use, where the data path is a row and anything that only reads it
    # hangs underneath. Nothing downstream reads these; they exist to be looked at.
    "housekeeping_sel": (2 * COL_W, -ROW_H),
    "housekeeping": (3 * COL_W, -ROW_H),
    # The segmentation mask, a TOP path and the COMP's only TOP output. Its own row
    # below the housekeeping, because it shares nothing with the CHOP network - it
    # arrives by shared memory rather than over OSC (BUILD_PLAN step 16).
    "seg_mask": (COL_W, -2 * ROW_H),
    "seg_fit": (2 * COL_W, -2 * ROW_H),
    "outmask": (3 * COL_W, -2 * ROW_H),
    # The depth map, its own row below the mask's. Same shape of path and the same
    # reason it is not in the CHOP network: it arrives by shared memory.
    "depth_map": (COL_W, -3 * ROW_H),
    "depth_fit": (2 * COL_W, -3 * ROW_H),
    "outdepth": (3 * COL_W, -3 * ROW_H),
    "status": (-3 * COL_W, MASTER_ROW_H),
    "sidecar_control": (-5 * COL_W, -4 * MASTER_ROW_H),
    "sidecar_callbacks": (-3 * COL_W, -4 * MASTER_ROW_H),
    "sidecar_exit": (-1 * COL_W, -4 * MASTER_ROW_H),
    "filter_callbacks": (-5 * COL_W, -4 * MASTER_ROW_H - ROW_H),
    "groups_callbacks": (-3 * COL_W, -4 * MASTER_ROW_H - ROW_H),
    "lat_threshold_callbacks": (-1 * COL_W, -4 * MASTER_ROW_H - ROW_H),
    "screenspace_callbacks": (-5 * COL_W, -4 * MASTER_ROW_H - 2 * ROW_H),
    "seg_callbacks": (-3 * COL_W, -4 * MASTER_ROW_H - 3 * ROW_H),
    "seg_par_callbacks": (-1 * COL_W, -4 * MASTER_ROW_H - 3 * ROW_H),
    "depth_callbacks": (-5 * COL_W, -4 * MASTER_ROW_H - 4 * ROW_H),
    "depth_par_callbacks": (-3 * COL_W, -4 * MASTER_ROW_H - 4 * ROW_H),
    "profiler": (-3 * COL_W, -4 * MASTER_ROW_H - 2 * ROW_H),
    # The two Parameter Execute DATs that watch the render and the camera, beside
    # the other things that listen rather than control.
    "renderchange": (-1 * COL_W, -4 * MASTER_ROW_H - 2 * ROW_H),
    "camchange": (-1 * COL_W, -4 * MASTER_ROW_H - 4 * ROW_H),
    "notes": (-5 * COL_W, MASTER_ROW_H),
    # The embedded package container. Off to one side: nothing wires to it and
    # nothing cooks it - the Install button reads it on demand.
    "src": (-7 * COL_W, MASTER_ROW_H),
    # The install panel's three DATs, in their own row below the sidecar's.
    "install_control": (-5 * COL_W, -4 * MASTER_ROW_H - 5 * ROW_H),
    "install_callbacks": (-3 * COL_W, -4 * MASTER_ROW_H - 5 * ROW_H),
    "install_start": (-1 * COL_W, -4 * MASTER_ROW_H - 5 * ROW_H),
}


def stream_xy(name: str) -> tuple[int, int]:
    """Where `name` goes inside a stream COMP.

    Contract: raises on an unknown name rather than returning a default. A builder
              that places a node the table does not know about would put it at the
              same coordinate as whatever else fell back, which is the failure this
              module exists to prevent.
    """
    if name not in STREAM_NODES:
        raise KeyError("no stream position for %r; known: %s"
                       % (name, ", ".join(sorted(STREAM_NODES))))
    return STREAM_NODES[name]


def master_xy(name: str) -> tuple[int, int]:
    """Where `name` goes inside the master COMP. Raises on an unknown name."""
    if name not in MASTER_NODES:
        raise KeyError("no master position for %r; known: %s"
                       % (name, ", ".join(sorted(MASTER_NODES))))
    return MASTER_NODES[name]


def stream_row(index: int) -> int:
    """The y of the `index`th stream inside the master COMP. 0 is hands."""
    return -index * MASTER_ROW_H


def placement(xy: tuple[int, int], keep_layout: bool,
              existed: bool) -> tuple[int, int] | None:
    """Where to put a node, or None to leave it exactly where it is.

    Contract: returns `xy` unless the user has asked to keep their own layout AND
              the node already existed before this build. A NEW node is always
              placed, whatever the flag says - TouchDesigner puts a freshly created
              operator wherever it likes, so "leave it alone" would mean piling
              every new operator somewhere arbitrary.
    Why here: the RULE is one line and belongs in one place, even though each
              builder has to call it - a builder must survive being pasted into a
              Text DAT on its own, so they cannot share a helper module of their own.
    What `Keeplayout` CANNOT hold, and the parameter's label says so: anything
              INSIDE a group. `temporal` destroys and regenerates all 73 of its
              operators on every run, so there is nothing there to preserve. It
              holds the group COMPs, the stream shell and the loose operators at
              the top level - which is the layer a person actually rearranges.
    """
    if keep_layout and existed:
        return None
    return xy


def overlaps(placed: dict[str, tuple[int, int]],
             width: int = COMP_W, height: int = COMP_H) -> list[tuple[str, str]]:
    """Every pair in `placed` whose node boxes would intersect, as sorted pairs.

    Contract: `width`/`height` are the box to assume for EVERY entry - so passing
              the COMP size, the larger of the two, is the conservative check and
              the one the tests use.
    Why a function here rather than in the test: `tools/td_verify_layout.py` runs
              the same check against the LIVE network, where the real node sizes are
              readable, and the two must agree about what "overlapping" means.
    """
    names = sorted(placed)
    found: list[tuple[str, str]] = []
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            ax, ay = placed[first]
            bx, by = placed[second]
            if abs(ax - bx) < width and abs(ay - by) < height:
                found.append((first, second))
    return found


def _self_check() -> None:
    """Nothing collides. Cheap, and it is the whole point of the module."""
    for label, table in (("stream", STREAM_NODES), ("master", MASTER_NODES)):
        clashes = overlaps(table)
        if clashes:
            raise RuntimeError("%s layout has overlapping nodes: %s"
                               % (label, clashes[:4]))


_self_check()
