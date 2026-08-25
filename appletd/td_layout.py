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

from collections.abc import Iterable
from itertools import pairwise
from typing import Any, Final

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
    # Row 0 starts one column further left than it used to, to make room for
    # `strip` between the OSC In CHOP and the filter.
    "in1": (-5 * COL_W, 0),
    "strip": (-4 * COL_W, 0),
    "filter": (-3 * COL_W, 0),
    "merge_out": (2 * COL_W, 0),
    "out1": (4 * COL_W, 0),
    # `coords` and `screen_only` were HERE, one of each per stream, until 2026-08-24.
    # Both moved to the master: one coordinate group reading all three streams merged
    # instead of three near-identical ones, and one Delete CHOP instead of three.
    # BUILD_PLAN step 25. Their names stay out of this table so a stale position
    # cannot place a node nothing builds.
    # the attribute layer, one row per group, all reading `filter`
    "derive_chop": (-1 * COL_W, -2 * ROW_H),
    "derive_callbacks": (-3 * COL_W, -2 * ROW_H),
    "temporal": (-1 * COL_W, -4 * ROW_H),
    "latches": (-1 * COL_W, -6 * ROW_H),
    # documentation, above the path and out of it
    "notes": (-5 * COL_W, 2 * ROW_H),
    # `tools/td_verify_latches.py`'s stored counter baseline. Not part of the
    # network - it holds one run's numbers so the next run can diff them - so it
    # sits well clear of everything, below the attribute layer.
    "ver_baseline": (-5 * COL_W, -8 * ROW_H),
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
    # SIX STAGES as of 2026-08-24, and the order is the whole design - see
    # MASTER_CHAIN below for what each one is for and why it is where it is.
    "merge_streams": (COL_W, 0),
    "early_trim": (2 * COL_W, 0),
    "coords": (3 * COL_W, 0),
    "screen_only": (4 * COL_W, 0),
    "trim_empty": (5 * COL_W, 0),
    "out1": (6 * COL_W, 0),
    # The housekeeping channels, one row BELOW the output path - the same convention
    # the streams use, where the data path is a row and anything that only reads it
    # hangs underneath. Nothing downstream reads these; they exist to be looked at.
    # They read `merge_streams` rather than the trim, deliberately: `sc_*` and `seq`
    # are diagnostics and should be visible whatever the toggles are doing.
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


# ---------------------------------------------------------------------------
# The master's data path, in order
#
# SIX STAGES BUILT BY FOUR DIFFERENT BUILDERS, which is exactly why the order lives
# here rather than in any of them. Before 2026-08-24 the chain was three nodes and
# `tools/td_build_vision.py` wired all three; now `coords` comes from
# `td_add_coords.py`, `screen_only` from `td_add_screenspace.py` and `early_trim`
# from `td_add_groups.py`, and each of those runs at a different point in the chain
# (`tools/td_rebuild.py`). A builder that guessed what sat either side of its own
# operator would be right only for the build order it was written against - the same
# mistake DESIGN.md 2.11 records as "a builder that repoints its CONSUMERS depends on
# build order, and lost".
#
# So every builder creates its own stage and then calls `rewire_master_chain`, which
# connects whatever exists in this order and ignores what does not. A partial build
# produces a shorter but working chain rather than a broken one.
#
#   merge_streams   the three streams, side by side
#   early_trim      the CHANNEL-REMOVING toggles - Fingertipsonly, Handbox,
#                   Onefaceonly, Facekeypoints. A Delete CHOP carrying PATTERNS, so
#                   it is cheap, and it runs BEFORE the composition: what it drops is
#                   never converted into a coordinate space. That is the whole reason
#                   there are two trims and not one.
#   coords          world and pixel spaces, one group for all three streams
#   screen_only     `Screenspaceonly`: the raw normalised channels whose composed
#                   twin now exists. It has to be AFTER coords - it deletes what
#                   coords reads - and it is one Delete instead of three.
#   trim_empty      the channels of every group that is not COOKING, which now
#                   includes a frozen coords half. It has to be LAST: a frozen COMP
#                   HOLDS its channels rather than dropping them, and stale
#                   coordinates on the output is the exact failure this operator was
#                   built for (DESIGN.md 2.15).
#   out1            the only CHOP output.
# ---------------------------------------------------------------------------
MASTER_CHAIN: Final[tuple[str, ...]] = (
    "merge_streams", "early_trim", "coords", "screen_only", "trim_empty", "out1",
)


# The head of a STREAM's data path, for the same reason MASTER_CHAIN exists: two
# builders own stages of it. `in1` and `filter` come from tools/td_build_vision.py and
# tools/td_add_filter.py; `strip` comes from tools/td_add_groups.py, which owns the
# toggles that fill it.
#
# `strip` is the OUTPUT-SHAPING toggles applied as early as they possibly can be -
# immediately after the OSC In CHOP, so what they remove never reaches the filter, the
# merge or anything at the master. Only channels NOTHING ELSE IN THE STREAM NEEDS can
# go here, which in practice means the face's: `Facekeypoints` and `Onefaceonly`.
#
# WHY HANDS HAS NO `strip`. `derive_chop` reads `filter` and needs all 21 joints for
# its curls, spreads and angles, so `Fingertipsonly` cannot be applied before it - the
# earliest safe place for that one is a FORK after the filter, feeding the raw branch
# of `merge_out` only. And `Handbox`'s channels do not exist here at all: `h?_bbox_*`
# and `h?_size` are computed by `derive_chop`, and `hands_overlap` needs them.
STREAM_HEAD: Final[tuple[str, ...]] = ("in1", "strip", "filter")


def rewire_stream_head(child: Any) -> list[tuple[str, str]]:
    """Connect one stream's `in1 -> [strip] -> filter`. Returns what it wired.

    Idempotent, and safe from either builder: a stream with no `strip` gets the
    two-node chain it always had.
    """
    present = [name for name in STREAM_HEAD if child.op(name) is not None]
    wired: list[tuple[str, str]] = []
    for upstream, downstream in _pairs(STREAM_HEAD, present):
        source = child.op(upstream)
        target = child.op(downstream)
        target.inputConnectors[0].connect(
            source.outputConnectors[0] if source.isCOMP else source)
        wired.append((upstream, downstream))
    return wired


def _pairs(order: Iterable[str], present: Iterable[str]) -> list[tuple[str, str]]:
    """Adjacent pairs of whichever names in `order` are `present`, in `order` order."""
    available = set(present)
    return list(pairwise([name for name in order if name in available]))


def chain_pairs(present: Iterable[str]) -> list[tuple[str, str]]:
    """`(upstream, downstream)` for every adjacent pair of stages that EXISTS. Pure.

    Contract: returns pairs in MASTER_CHAIN order, skipping absent stages, so
              `{merge_streams, trim_empty, out1}` gives the three-node chain this
              component had before step 25 and a full set gives the six-node one.
              Names not in MASTER_CHAIN are ignored rather than raising - the master
              holds plenty of operators that are not on the data path.
    Why pure: so a test can assert the order without TouchDesigner, which is the
              only way to check the one property that matters - that `trim_empty` is
              always last before `out1` however many stages are missing.
    """
    # `_pairs` materialises `present` ONCE. `set(present)` inside a comprehension is
    # re-evaluated per item, so an ITERATOR argument would be consumed by the first
    # name tested and every later one would miss - a chain that silently drops stages
    # depending on how the caller happened to build its list.
    return _pairs(MASTER_CHAIN, present)


def rewire_master_chain(master: Any) -> list[tuple[str, str]]:
    """Connect the master's data path in MASTER_CHAIN order. Returns what it wired.

    Idempotent, and safe to call from every builder that owns a stage: connecting an
    input that is already connected costs nothing, and a stage that does not exist
    yet is simply not in the chain.

    Traps: `inputConnectors[0].connect()` and NOT `.inputs`, because `op.inputs` goes
           stale and has lied about this exact question before (DESIGN.md 2.11).
           And a COMP is connected by its OUTPUT CONNECTOR, not by itself - passing
           the COMP raises `Invalid number or type of arguments`, which is what
           `coords` becoming a baseCOMP at the master turned up on its first run.
    """
    present = [name for name in MASTER_CHAIN if master.op(name) is not None]
    wired: list[tuple[str, str]] = []
    for upstream, downstream in chain_pairs(present):
        source = master.op(upstream)
        target = master.op(downstream)
        target.inputConnectors[0].connect(
            source.outputConnectors[0] if source.isCOMP else source)
        wired.append((upstream, downstream))
    return wired


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


# ---------------------------------------------------------------------------
# The resolver every generated DAT embeds
# ---------------------------------------------------------------------------
# WRITTEN ONCE HERE and pasted verbatim into the generated DATs, because it was
# four copies of the same twenty lines and they drifted the moment one of them was
# fixed.
#
# NOTHING MACHINE-SPECIFIC IS BAKED INTO IT, and that is the whole point. Until
# 2026-08-24 each copy carried
#
#     BUILT_AT = "/Users/<whoever>/Documents/GitHub/appletd"
#
# - the checkout the BUILDER ran from - and preferred it over everything else. Two
# consequences, both found by opening the component on somebody else's Mac, which is
# the only place either could be found:
#
#   * a shipped `.tox` named a stranger's home directory in its own error messages;
#   * and it could not find a SUCCESSFUL install, because a blank `Installroot` was
#     appended as "" and never expanded to where Install actually writes.
#
# THE ORDER, and why each entry earns its place:
#
#   1. `Installroot`, when set. An explicit answer wins; it is also the documented
#      way to point the component at a checkout.
#   2. `$APPLETD_REPO`, for a developer who wants the .toe to read a working tree
#      rather than an install - the case BUILT_AT existed to serve. Opt-in, and
#      carried by the environment rather than by the file, so it cannot ship.
#   3. `~/Library/Application Support/appletd`, where Install puts it. Expanded at
#      CALL time, so it is the home of whoever opened the file.
PACKAGE_ROOT_SOURCE = '''
# Where the `appletd` package is, resolved on the machine that OPENS this file.
# Generated from tools/td_paths.py - see there for why nothing is baked in.
INSTALL_ROOT_DEFAULT = "~/Library/Application Support/appletd"
DEV_ROOT_ENV = "APPLETD_REPO"
# The development interpreter, for a machine that has a venv rather than an install.
# An ENVIRONMENT VARIABLE and not a parameter, because a parameter is SAVED INTO THE
# .tox - which is how `~/.venvs/appletd/bin/python` came to be baked into a file
# handed to somebody else. `Sidecarpython` on the Advanced page is still there for a
# deliberate, per-project answer; this one is for "my machine, always".
DEV_PYTHON_ENV = "APPLETD_PYTHON"


def _package_root_candidates(comp):
    """Every directory that might contain `appletd`, best answer first."""
    tried = []
    if comp is not None:
        par = getattr(comp.par, "Installroot", None)
        if par is not None and str(par.eval()).strip():
            tried.append(os.path.expanduser(str(par.eval()).strip()))
    checkout = os.environ.get(DEV_ROOT_ENV, "").strip()
    if checkout:
        tried.append(os.path.expanduser(checkout))
    tried.append(os.path.expanduser(INSTALL_ROOT_DEFAULT))
    return tried


def _find_package_root(comp):
    """The first candidate that actually holds `appletd`, or "" if none does."""
    for root in _package_root_candidates(comp):
        if root and os.path.isdir(os.path.join(root, "appletd")):
            return root
    return ""


def _package_root_error(comp):
    """What to tell somebody when it is not there. Names only THEIR paths."""
    return ("cannot find the `appletd` package. Looked in %s. Press Install on the "
            "Vision page, or point `Installroot` at a checkout."
            % ", ".join(repr(t) for t in _package_root_candidates(comp) if t))
'''

