#!/usr/bin/env python
"""A `coords` COMP in every stream: world and pixel spaces. Paste, Run. Idempotent.

    WHAT IT BUILDS, in each of `/project1/appletd`'s streams, from the normalised
    values the sidecar sends:

        _tx, _ty   world units for an orthographic camera
        _px, _py   pixels in the SOURCE image
        _tw, _th   world size, and _pw, _ph in pixels, where there is an extent

    Hands: 168 channels. Pose: 152. Face: 712 - and the face is the interesting
    one, because a face carries both a bounding box and 174 points that are
    normalised to that box rather than to the image.

EVERY STREAM, ONE SET OF PARAMETERS. This used to be hands-only, inside the hands
COMP, because hands came first. A body's joints and a face's box want exactly the
same conversion, and one Ortho Width has to serve all three or they disagree about
where the world is - which is why the parameters live on the master COMP and every
stream's branches read them through `op.Appletd`.

WHICH CHANNELS, AND WITH WHAT RULE, comes from `appletd/spaces.py` rather than
from a pattern written here. Three distinctions it makes that a pattern cannot:

  * a POSITION gets the -0.5 offset, because world space is centred on the camera;
  * an EXTENT does not. A box is 0.2 wide wherever it sits, and subtracting 0.5
    from a width produces a negative size that draws as nothing;
  * an ANGLE is not transformed at all. There is no yaw in pixels.

THE ARITHMETIC FOR A CHANNEL ALREADY IN IMAGE SPACE. Each branch is Select -> Math:
pick the channels, then do the sum and the rename. The Math CHOP
evaluates `(value + preoff) * gain + postoff` - MEASURED - so `(v - 0.5) * aspect`
is exactly `preoff = -0.5, gain = aspect` with no expression gymnastics.

    _tx = (x - 0.5) * Orthowidth              _tw = w * Orthowidth
    _ty = (y - 0.5) * Orthowidth * Renderh / Renderw
    _px =  x * Resw                           _pw = w * Resw
    _py =  y * Resh                           _th = h * Orthowidth * Renderh / Renderw
                                              _ph = h * Resh

**`_ty` uses the RENDER's aspect, not the source image's.** Ortho Width describes
the visible WIDTH, and the vertical extent follows from the resolution of whatever
is rendering - which is not the resolution of the camera feeding the sidecar. Those
two were the same 1280x720 while this was first built, which is exactly why the bug
survived: the wrong expression and the right one agreed. DESIGN.md 7.

AND THE ARITHMETIC FOR A FACE LANDMARK, which is the part this file grew for. A
face's landmark points are normalised to the FACE'S BOUNDING BOX, not to the image
(`normalizedPoints`, DESIGN.md 2.12). Transforming one with the rules above would
put every facial feature in the same corner of the frame. So a landmark composes
through its own face's box first:

    image_x = bbox_x + point_x * bbox_w        image_y = bbox_y + point_y * bbox_h

and then takes the same centre-and-scale as any other position. Written out, the
world coordinate of a landmark is

    _tx = (bbox_x + point_x * bbox_w - 0.5) * K
        = point_x * (bbox_w * K)  +  (bbox_x - 0.5) * K
          ^^^^^^^   ^^^^^^^^^^^^     ^^^^^^^^^^^^^^^^^^
          value       gain                 postoff

...which is ONE Math CHOP whose `gain` and `postoff` are EXPRESSIONS reading that
face's box. Two operators per branch, and the composition costs two expression
evaluations per cook rather than one per landmark. The alternative considered and
rejected: an Expression CHOP with the composition per channel, which is 608 Python
evaluations per frame on TouchDesigner's main thread.

Both halves of that arithmetic are built from `spaces.box_expressions()`, and
`test_spaces.py` EVALUATES the strings it returns against hand-computed numbers -
because a coordinate formula that is subtly wrong here looks plausible on screen,
which is how the `_ty` aspect bug survived its first review.

NO LONG CHANNEL LISTS ANYWHERE IN THIS NETWORK, and that is a change worth stating.
Every Select takes a PATTERN that `spaces.py` has verified against the stream's own
contract, and every rename is the wildcard form `*_x` -> `*_tx`. MEASURED: a Select
carrying 362 literal names cost 0.1725 ms per cook. The wildcard rename is the form
DESIGN.md 2.11 warns about, so each branch's output channel NAMES are checked
against the expected list, and a branch that fails is rebuilt with the explicit
pairwise mapping - cheap first, correct always.

Ref: DESIGN.md 7 (the coordinate contract), 2.12 (box-relative points),
     docs/BUILD_PLAN.md step 7.3.
"""

import os
import sys

# Derived from this file's own location. A literal path here meant the project
# opened on exactly one machine; `__file__` is set by the shell, by
# TouchDesigner's run(), and by tools/td_rebuild.py before each exec.
# tools/td_paths.py has the full reasoning and why it is not imported from there.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_PATH = "/project1/appletd"
GROUP = "coords"

# Every user parameter lives on the master COMP, reached through its Global OP
# Shortcut. NOT `parent(2)`: a group is two levels below the parameters now, so a
# relative reference would have to be counted per operator and would break the next
# time anything moved. `op.Appletd` is depth-independent and survives a rename.
MASTER_PAR = "op.Appletd.par.%s"

# The GAIN each output space applies, by suffix - the `K` in the docstring's
# arithmetic. The pre-offset and the channel list come from appletd/spaces.py;
# only these expressions are here, because only this script knows how to write a
# TouchDesigner expression.
GAINS = {
    "_tx": MASTER_PAR % "Orthowidth",
    "_tw": MASTER_PAR % "Orthowidth",
    "_ty": "%s * %s / %s" % (MASTER_PAR % "Orthowidth", MASTER_PAR % "Renderh",
                             MASTER_PAR % "Renderw"),
    "_th": "%s * %s / %s" % (MASTER_PAR % "Orthowidth", MASTER_PAR % "Renderh",
                             MASTER_PAR % "Renderw"),
    "_px": MASTER_PAR % "Resw",
    "_pw": MASTER_PAR % "Resw",
    "_py": MASTER_PAR % "Resh",
    "_ph": MASTER_PAR % "Resh",
}

# TWO inputs, in connector order, and which one a branch reads is the design.
#
#   in1   whatever precedes `coords` in `appletd.td_layout.MASTER_CHAIN` - normally
#         `early_trim`, and `merge_streams` on a build where `td_add_groups.py` has
#         not run yet. Every Select reads this, so a channel the trim dropped is
#         never composed.
#   in2   `merge_streams` itself, UNTRIMMED, read ONLY by the box expressions. It is
#         what makes a post-trim group safe: see `_build_half` and BUILD_PLAN 25.2.
#
# Both are downstream of every stream's `filter`, so the coordinates still inherit
# the smoothing - a project reading `_tx` and a project reading `derive`'s output
# should not disagree about where the hand is - and the box-relative composition
# stays consistent, because the points and the box they compose through are both
# smoothed or neither is.
IN_TRIMMED = "in1"
FULL_INPUT = "in2"
FULL_SOURCE = "merge_streams"

# ---- layout -----------------------------------------------------------------
# The group's position inside its stream comes from `appletd/td_layout.py`, the
# one table for it - this script and td_add_filter.py both used to place their group
# at (-400, -300), on top of each other, with nothing able to notice. The positions
# INSIDE the group are here, beside the code that builds them.
COL_W = 200
ROW_H = 150
# Column 0 is the group's In CHOP; each branch occupies one ROW at columns 1 and 2;
# the Merge and the Out CHOP sit to the right of the widest branch.
COL_IN = 0
COL_SELECT = 1
COL_MATH = 2
COL_MERGE = 3
COL_OUT = 4
# The first branch row sits below the In CHOP so the fan-out reads downward.
ROW_FIRST = -1

# ---- the two halves, and which toggle gates each ----------------------------
# Every branch lands in one of two sub-groups by its suffix, and the sub-group is
# what `allowCooking` can freeze. MEASURED, and the reason this split exists at all:
# the face landmark branches cost 1.86 ms per cook - 696 output channels through two
# operators each, at about 1.2 microseconds per channel per operator - which is more
# than the rest of the COMP put together.
#
# The two toggles are the ones docs/ATTRIBUTES.md already has, and which
# tools/td_add_groups.py has been printing as "channels only, gates no cost yet"
# since they were added. They gate cost now.
# `box` says whether the sub-group takes the BOX-RELATIVE branches (a face's
# landmark points) or the ones already in image space. They are separated because
# they cost two orders of magnitude apart: the eight image-space branches on the
# face stream are 8 channels each, and the four box-relative world branches are 87
# each. MEASURED, per cook, all four sub-groups cooking:
#
#     face/coords/world      1.2099 ms   (4 landmark branches, 348 channels)
#     face/coords/lm_world   is that 1.21 ms, on its own toggle now
#     hands/coords/world     0.1039 ms   (4 branches, 168 channels)
#
# So a project that wants a face's bounding box in world space but not 348 landmark
# coordinates can have exactly that, which it could not when one toggle covered
# both.
#
# THE FOURTH COLUMN is which set of BOX-RELATIVE branches the half also takes, and
# it is what puts the four key points in the cheap half rather than the expensive
# one. A key point composes through the face's bounding box exactly as a landmark
# does - identical arithmetic, identical operators - but there are 4 of them per
# branch against 87, so they ride with the bounding box under `Coordstx` instead of
# in the landmark halves. That is the whole mechanism behind `Face Key Points`: it
# freezes `lm_world` and `lm_pixels` and nothing else, so the eyes, nose and mouth
# stay live when the 348 landmark channels stop.
HALVES = (
    # name        toggle          the suffixes            box branches
    ("world", "Coordstx", ("_tx", "_ty", "_tw", "_th"), "keypoints"),
    ("pixels", "Coordspx", ("_px", "_py", "_pw", "_ph"), "keypoints"),
    # STILL SEPARATE COMPS, on the SAME toggle since 2026-08-24. `Lmcoordstx` and
    # `Lmcoordspx` were collapsed into the two above by request - needing both on
    # before a face landmark reached world space was a distinction nobody wanted -
    # and the COMPs stay split because `Facekeypoints` freezes exactly these two and
    # nothing else. A toggle is not a boundary.
    ("lm_world", "Coordstx", ("_tx", "_ty"), "landmarks"),
    ("lm_pixels", "Coordspx", ("_px", "_py"), "landmarks"),
)

# `dv_world` and `dv_pixels` WERE here - two more halves for the branches whose
# source was `derive_chop` and `temporal` rather than the wire contract, because in a
# PER-STREAM group those read a different INPUT and `allowCooking` freezes a whole
# COMP. Merged, they are not a special case at all: `derive_chop` and `temporal`
# publish into the hands stream's own Merge, so by the time `merge_streams` has run
# their channels are simply more positions and rates with the same roles as any
# other. Four halves instead of six, and 16 fewer operators. BUILD_PLAN step 25.

HALF_NOTES = """%(name)s - the %(space)s half of the coordinate spaces.

Everything in here is gated by op.Appletd.par.%(toggle)s. `allowCooking` on this COMP
is written by groups_callbacks whenever that toggle changes - an attribute cannot
take an expression, so it has to be written rather than bound.

FROZEN IS NOT ABSENT. A COMP that is not cooking holds its last output, so these
channels stay on the stream with their last values rather than disappearing. That is
deliberate: a channel that VANISHES breaks every reference to it with no error
anywhere (DESIGN.md 6.2), which is a far worse failure than a stale number on a
toggle the user just turned off.

%(branches)s

Built by tools/td_add_coords.py. Edits here are overwritten on the next run."""


NOTES = """COORDS - normalised values into TouchDesigner's world and pixel spaces.

  in1 -> world     (Coordstx)   --+
      -> pixels    (Coordspx)   --+
      -> lm_world  (Coordstx)   --+-> out -> out1
      -> lm_pixels (Coordspx)   --+

Inside each half, one ROW per branch, and every branch is TWO operators: the Select
picks the channels with a PATTERN and renames them in the same operator, and the
Math does the arithmetic. There is no Rename CHOP and no literal channel list
anywhere in here - a Select carrying 362 names cost 0.1725 ms per cook, which was
more than the filter it fed.

WHY FOUR SUB-GROUPS. So each can be frozen on its own. MEASURED: the face LANDMARK
branches cost 1.2099 ms per cook in world space alone - more than everything else in
the COMP put together - while the eight image-space branches on the same stream cost
under a tenth of that. One toggle over both would make "I want the face box in world
space" cost 348 landmark channels. A frozen half keeps its channels, holding their
last values.

A Math CHOP computes (value + preoff) * gain + postoff. So:

  _tx = (x - 0.5) * Orthowidth               preoff -0.5, gain Orthowidth
  _ty = (y - 0.5) * Orthowidth * Rh / Rw     ...the RENDER's aspect, not the
  _px =  x * Resw                            source image's. Those two were the
  _py =  y * Resh                            same 1280x720 when this was written,
  _tw =  w * Orthowidth                      which is why the wrong version agreed
  _pw =  w * Resw                            with the right one.

A POSITION takes the -0.5; an EXTENT does not. A box is 0.2 wide wherever it sits,
and subtracting 0.5 from a width gives a negative size that draws as nothing.
An ANGLE is not here at all: there is no yaw in pixels.

%(box_note)s
%(branches)s

Built by tools/td_add_coords.py. Edits here are overwritten on the next run."""

BOX_NOTE = """BOX-RELATIVE BRANCHES (the lm_* rows). A face's landmark points are
normalised to that face's BOUNDING BOX, not to the image, so they compose through
the box before they take the usual centre-and-scale:

  image_x = bbox_x + point_x * bbox_w
  _tx     = point_x * (bbox_w * K) + (bbox_x - 0.5) * K

...which is one Math CHOP with `gain` and `postoff` as EXPRESSIONS reading that
face's box. Two expression evaluations per cook, against one per landmark if the
composition were done per channel. Each face needs ITS OWN box, which is why there
is a row per face rather than one for all of them - and a Math CHOP does not
broadcast a one-channel input across many, so there is no shorter way."""


def _attach_once(merge, node, drop_names=()):
    """Normalise a Merge CHOP's inputs: `node` exactly once, anything in
    `drop_names` gone, every other source exactly once. Returns what it removed.

    Traps, all four measured in this repo rather than anticipated (DESIGN.md 2.11):
      * `.inputs` is STALE inside the same script that rewired anything, so this
        reads `inputConnectors[i].connections` throughout - that is the truth. It
        once reported an input list containing the COMP's own `out1`, which would
        have been a loop, while the connectors showed the correct thirteen.
      * disconnecting SHIFTS the connector list, so a single pass skips entries.
        This restarts after every removal. A single-pass version dropped two of
        four stale branches and left the other two wired to operators it then
        destroyed.
      * a Merge CHOP accepts the SAME operator on any number of inputs, silently,
        and the only symptom is the channel count. `filter` reached three
        connections and put 274 duplicate channels on the output; `derive_chop`
        reached two and put 87. So this dedupes EVERY source, not just `node` -
        which means any builder run repairs the whole merge rather than only its
        own contribution.
      * `connect()` accepts an operator only where it has one unambiguous output,
        which a base COMP does not - hence `outputConnectors[0]`.
    """
    removed = []
    for _attempt in range(256):
        seen = []
        cut = False
        for conn in merge.inputConnectors:
            owners = [x.owner for x in conn.connections]
            if not owners:
                continue
            owner = owners[0]
            if owner.name in drop_names or owner in seen:
                removed.append(owner.name)
                conn.disconnect()
                cut = True
                break                       # the list has shifted; start again
            seen.append(owner)
        if not cut:
            break

    attached = any(x.owner is node for conn in merge.inputConnectors
                   for x in conn.connections)
    if not attached:
        target = node.outputConnectors[0] if node.isCOMP else node
        for conn in merge.inputConnectors:
            if not conn.connections:
                conn.connect(target)
                break
    return removed


def _keep_layout(master):
    """Has the user asked the builders to leave their node arrangement alone?

    `getattr` with a default, because this parameter is younger than several of the
    builders and a COMP built before it existed must still build rather than raise.
    """
    par = getattr(master.par, "Keeplayout", None)
    return bool(par is not None and par.eval())


def _place(node, xy, keep, existed):
    """Write nodeX/nodeY unless `Keeplayout` is on and the node already existed.

    See `appletd.td_layout.placement` for the rule and for what the flag can
    and cannot hold - the short version is that it holds this group COMP and not
    the operators inside it, which are regenerated on every run.
    """
    from appletd.td_layout import placement

    where = placement(xy, keep, existed)
    if where is not None:
        node.nodeX, node.nodeY = where
    return node


def _clear_keeping_ports(td, group, ports):
    """Empty a group of its working operators but KEEP its In/Out CHOPs.

    MEASURED, and it is the reason this function exists rather than a plain
    "destroy every child": a group's In and Out CHOPs ARE its connectors, and
    destroying the Out CHOP drops an external connection from a plain CHOP
    consumer while leaving a COMP-to-COMP connection intact.

    Reproduced deterministically: re-running the filter builder left `coords` (a
    COMP) reading `filter` and silently disconnected `derive_chop` (a Script CHOP)
    from the same output connector - so every derived attribute went to zero
    channels, the COMP output fell from 499 to 366, and the builder reported
    success. Keeping the ports makes a rebuild invisible to everything downstream,
    whatever kind of operator it is.

    `ports` names the operators to preserve; they are re-wired by the caller, since
    what they connect to is what the rebuild changes.
    """
    kept = {}
    for child in list(group.children):
        # TRAP: `child` may ALREADY BE GONE by the time this loop reaches it.
        # Destroying a Script CHOP also destroys the callbacks DAT docked to it, so
        # a snapshot of `children` taken before the loop can hold a reference to an
        # operator a previous iteration removed - and touching it raises "Invalid OP
        # object. The node this python object referenced has likely been deleted."
        #
        # MEASURED, twice, and the first time it was written off as MCP flakiness:
        # it only started happening when `tmp_motion_callbacks` moved inside this
        # group, and it left the group half-built at 5 operators of 74.
        if not child.valid:
            continue
        if child.name in ports:
            kept[child.name] = child
        else:
            child.destroy()
    return kept


def _renamed(names, old_suffix, new_suffix):
    """What a branch's output channels must be called, in order. The list every
    rename in this file is checked against."""
    return [name[:-len(old_suffix)] + new_suffix for name in names]


def _apply_rename(node, names, old_suffix, new_suffix, failures, label):
    """Rename a branch's channels, cheaply if that works and safely if it does not.

    Contract: on return, `node` carries exactly `_renamed(names, ...)` in order, or
              a failure has been appended saying it does not.
    Why two forms: the wildcard `*_x` -> `*_tx` is one short pattern instead of two
              lists of up to 87 names, and VERIFIED to substitute the matched part
              correctly. It is also one character away from the form DESIGN.md 2.11
              records as a disaster - `renamefrom = '*'` with a literal `renameto`
              turned 126 channels into `h0_wrist_x`, `h0_wrist_x1`, `h0_wrist_x2`.
              So the cheap form is TRIED and CHECKED, and the explicit pairwise
              mapping is what happens if the check fails. Neither is trusted.
    """
    # WHAT ARRIVED, not what the contract lists. `early_trim` sits before this group
    # and legitimately removes channels - `Fingertipsonly` takes fifteen joints per
    # hand, `Facekeypoints` takes the 348 landmarks or the 16 key points - so a branch
    # whose Select found six joints where the contract has 21 is doing exactly what it
    # was asked. Checking against `names` would report every one of those as a
    # failure, which is the crying-wolf shape this project keeps deleting.
    #
    # It is NOT tautological: it renames the input's own names OURSELVES and compares,
    # so a wildcard that misfires still produces a different answer. `names` is still
    # what the pairwise fallback maps, because that form is by name and an absent
    # source simply does not match.
    arrived = ([c.name for c in node.inputs[0].chans()] if node.inputs else [])
    expected = _renamed(arrived, old_suffix, new_suffix)
    node.par.renamefrom = "*" + old_suffix
    node.par.renameto = "*" + new_suffix
    node.cook(force=True)
    # SORTED, and this is a 2026-08-24 correction rather than a loosening.
    #
    # MEASURED: a Select CHOP emits in PATTERN-TERM order, not input order. With one
    # term - which is every per-stream pattern this file ever used - the two are the
    # same. The merged position pattern has twelve, so `h?_w*_x h?_t*_x ...` produced
    # `h0_wrist_tx, h1_wrist_tx, h0_thumb_cmc_tx` where the contract order is
    # `h0_wrist, h0_thumb_cmc, h0_thumb_mp`. Nothing was wrong; the sequence was.
    #
    # A sorted comparison still catches everything this check exists for. The failure
    # DESIGN.md 2.11 records - `renamefrom = '*'` against a literal `renameto` - turns
    # 126 channels into `h0_wrist_x`, `h0_wrist_x1`, `h0_wrist_x2`, which is a
    # different SET of names, and a rename that dropped or duplicated one changes the
    # multiset. What it stops checking is the order, and the order of the composed
    # channels is not part of any contract: they are appended after the raw ones,
    # every downstream list is built from the live channel order, and every consumer
    # selects by name.
    if sorted(c.name for c in node.chans()) == sorted(expected):
        return "wildcard"

    # Pairwise BY NAME: a literal space-separated From/To list is applied name to
    # name, so a source that is absent keeps its own name while the rest still map
    # correctly - one channel wrong instead of every later one shifted onto the
    # wrong data.
    node.par.renamefrom = " ".join(names)
    node.par.renameto = " ".join(_renamed(names, old_suffix, new_suffix))
    node.cook(force=True)
    if sorted(c.name for c in node.chans()) != sorted(expected):
        # UNVERIFIABLE is not the same as wrong. If this node's input carries no
        # channels there is nothing to rename, and the rename map is still correct -
        # it just cannot be demonstrated. That happens whenever the branch reads a
        # group `allowCooking` has frozen, or a stream that is switched off, and it
        # used to be reported as four failures on a network that was fine. A check
        # that cries wolf hides the next real failure among its noise.
        if not any(inp.numChans for inp in node.inputs):
            return "unverified"
        failures.append("%s: renaming produced %r, expected %r"
                        % (label, [c.name for c in node.chans()][:3], expected[:3]))
        return "FAILED"
    return "pairwise"


def main():
    import td

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    # TouchDesigner caches our modules for the life of the process. DESIGN.md 2.11.
    for stale in [n for n in list(sys.modules)
                  if n == "appletd" or n.startswith("appletd.")]:
        del sys.modules[stale]
    from appletd.spaces import (
        STREAM_MERGED,
        box_branches,
        box_expressions,
        keypoint_branches,
        transform_branches,
    )
    from appletd.streams import STREAM_NAMES
    from appletd.td_layout import master_xy, rewire_master_chain

    master = op(MASTER_PATH)
    if master is None:
        print("FAIL no COMP at %s - run tools/td_build_vision.py first" % MASTER_PATH)
        return

    print("=" * 70)
    print("coords: ONE group at %s, reading all three streams merged" % master.path)
    print("=" * 70)

    failures = []
    # THE PER-STREAM GROUPS GO FIRST, before the master one is built. Leaving them
    # would leave three groups computing the same channels into the same merge, and
    # `merge_out` would rename the collisions - 348 channels of `f0_left_eye_00_tx1`
    # that look like data (DESIGN.md 2.11).
    retired = []
    for stream in STREAM_NAMES:
        child = master.op(stream)
        old = None if child is None else child.op(GROUP)
        if old is not None:
            _attach_once(child.op("merge_out"), old, drop_names=(GROUP,))
            old.destroy()
            retired.append("%s/%s" % (stream, GROUP))
    if retired:
        print("   retired the per-stream groups: %s" % ", ".join(retired))

    built = _build_one(td, master, STREAM_MERGED,
                       [(IN_TRIMMED, b) for b in transform_branches(STREAM_MERGED)],
                       {"landmarks": box_branches(STREAM_MERGED),
                        "keypoints": keypoint_branches(STREAM_MERGED)},
                       box_expressions, failures,
                       master_xy(GROUP), _keep_layout(master))
    if built is None:
        return
    operators, channels, renames = built

    # LAST, and this is what connects the group into the data path. The order lives
    # in `appletd.td_layout.MASTER_CHAIN` because four builders own a stage of it.
    wired = rewire_master_chain(master)

    print()
    print("   %d operators, %d channels, renames: %s"
          % (operators, channels,
             ", ".join("%s x%d" % (how, count)
                       for how, count in sorted(renames.items()))))
    print("   chain: %s" % " -> ".join(
        [pair[0] for pair in wired] + [wired[-1][1]] if wired else ["(nothing)"]))
    print()
    print("   Orthowidth=%.3f, render %dx%d, source %dx%d - one set of controls on"
          % (master.par.Orthowidth.eval(), master.par.Renderw.eval(),
             master.par.Renderh.eval(), master.par.Resw.eval(),
             master.par.Resh.eval()))
    print("   %s, read by every branch." % master.path)

    print()
    if failures:
        print("FAILURES (%d):" % len(failures))
        for failure in failures:
            print("   " + failure)
    else:
        print("   verified. `_ty` uses the RENDER's aspect, not the source image's:")
        print("   with Orthowidth %.3f and a %dx%d render, a point at the top of"
              % (master.par.Orthowidth.eval(), master.par.Renderw.eval(),
                 master.par.Renderh.eval()))
        print("   frame reads %+.3f."
              % (0.5 * master.par.Orthowidth.eval() * master.par.Renderh.eval()
                 / master.par.Renderw.eval()))
    print()


def _build_one(td, master, stream, pairs, boxes, box_expressions, failures,
               group_xy, keep):
    """Build the ONE coords group on the master. Returns (operators, channels, how).

    `pairs` is [(input name, Branch)] - every branch reads `in1`, the trimmed merge,
    and the tuple shape is kept so `_build_half` did not have to change.
    `boxes` is {kind: [BoxBranch]}, keyed by the names in `HALVES`' fourth column:
    `landmarks` for the 348 that live in the freezable `lm_*` halves, `keypoints`
    for the 16 that ride with the bounding box.
    """
    branches = [b for _src, b in pairs]
    upstream = _upstream(master)
    if upstream is None:
        failures.append("%s has no operator for `coords` to read" % master.path)
        return None
    full = master.op(FULL_SOURCE)
    if full is None:
        failures.append("%s has no `%s` for the box expressions to read"
                        % (master.path, FULL_SOURCE))
        return None

    group = master.op(GROUP)
    existed = group is not None
    if group is None:
        group = master.create(td.baseCOMP, GROUP)
    _place(group, group_xy, keep, existed)
    group.color = (0.28, 0.38, 0.5)
    kept = _clear_keeping_ports(td, group, ("out1",))

    # TWO INPUTS, and the second one is the whole reason this group can sit after a
    # trim. See the module docstring and docs/BUILD_PLAN.md 25.2.
    group_ins = {}
    for index, (name, source) in enumerate(((IN_TRIMMED, upstream),
                                            (FULL_INPUT, full))):
        port = kept.get(name) or group.create(td.inCHOP, name)
        port.nodeX, port.nodeY = COL_IN * COL_W, -index * ROW_H
        group.inputConnectors[index].connect(
            source.outputConnectors[0] if source.isCOMP else source)
        group_ins[name] = port

    halves = []
    summary = []
    renames = {}
    row = 0
    for name, toggle, suffixes, box_kind in HALVES:
        # A LANDMARK half takes nothing but its box branches; the world and pixel
        # halves take their own transform branches AND the key points.
        mine = ([] if box_kind == "landmarks" else
                [(src, b) for src, b in pairs if b.suffix in suffixes])
        boxes_mine = [b for b in boxes.get(box_kind, ()) if b.suffix in suffixes]
        if not mine and not boxes_mine:
            continue
        half = _build_half(td, group, group_ins, stream, name, toggle, mine,
                           boxes_mine, box_expressions, renames, failures, row)
        halves.append(half)
        summary.append("  %-9s gated by %-11s %2d branches, %4d channels"
                       % (name, toggle, len(mine) + len(boxes_mine),
                          sum(len(b.names) for _s, b in mine)
                          + sum(len(b.names) for b in boxes_mine)))
        row -= len(mine) + len(boxes_mine) + 2

    # THE INPUT GOES INTO THE MERGE FIRST, and that is the difference between a
    # group that hangs off the data path and one that is IN it.
    #
    # Per stream, `coords` was a BRANCH: `merge_out` took the filter's raw channels
    # and this group's composed ones side by side. At the master it sits in the chain
    # between the trim and `screen_only`, so anything it does not pass through is
    # gone - all 945 raw channels of it. First rather than last so the raw names keep
    # their positions and a project reading the output by index sees the same
    # channels in the same order it always did, with the composed ones appended.
    merged = group.create(td.mergeCHOP, "out")
    merged.nodeX, merged.nodeY = COL_MERGE * COL_W, 0
    merged.inputConnectors[0].connect(group_ins[IN_TRIMMED])
    for index, half in enumerate(halves):
        merged.inputConnectors[index + 1].connect(half.outputConnectors[0])
    group_out = kept.get("out1") or group.create(td.outCHOP, "out1")
    group_out.nodeX, group_out.nodeY = COL_OUT * COL_W, 0
    group_out.inputConnectors[0].connect(merged)
    group_out.cook(force=True)

    note = group.create(td.textDAT, "notes")
    note.nodeX, note.nodeY = COL_IN * COL_W, ROW_H
    note.text = NOTES % {
        "box_note": BOX_NOTE,
        "branches": "\n".join(["", "HALVES", *summary])}

    # WHAT THE MERGE OWES: its input passed through, plus every channel the halves
    # actually produced. Checking against the CONTRACT total instead would report a
    # failure every time `early_trim` did its job, and the number this check exists
    # for is whether the Merge dropped a half - a Merge CHOP with too few connectors
    # loses a whole branch and says nothing.
    contract = (sum(len(b.names) for b in branches)
                + sum(len(box.names) for kind in boxes.values() for box in kind))
    composed = sum(half.op("out1").numChans for half in halves
                   if half.op("out1") is not None)
    if composed < contract:
        print("   (%d of %d contract channels were removed before this group - "
              "`early_trim` and the output-shaping toggles. Not a fault.)"
              % (contract - composed, contract))
    expected = group_ins[IN_TRIMMED].numChans + composed
    produced = group_out.numChans
    if produced != expected:
        # UNVERIFIABLE is not the same as wrong. A half `allowCooking` has frozen
        # contributes nothing to this count, and `Coordspx` ships in whatever state
        # the user left it - so a mismatch is only a failure when every half is
        # actually cooking.
        frozen = [half.name for half in halves if not half.allowCooking]
        if frozen:
            print("   (%s frozen - built, not counted: %d of %d channels)"
                  % (", ".join(frozen), produced, expected))
        else:
            failures.append("`%s` produced %d channels, expected %d (%d through "
                            "plus %d composed) - the Merge has lost a half"
                            % (GROUP, produced, expected,
                               group_ins[IN_TRIMMED].numChans, composed))
    return (len(group.children), produced, renames)


def _upstream(master):
    """The operator `coords` reads: the stage before it in MASTER_CHAIN.

    Why not a literal name: `early_trim` is built by tools/td_add_groups.py, which
    runs AFTER this script (tools/td_rebuild.py), so on a first build it is not there
    yet and the group has to read `merge_streams` instead. Asking the chain rather
    than naming a neighbour is the same rule `rewire_master_chain` exists for.
    """
    from appletd.td_layout import MASTER_CHAIN

    before = MASTER_CHAIN[:MASTER_CHAIN.index(GROUP)]
    for name in reversed(before):
        node = master.op(name)
        if node is not None:
            return node
    return None


def _build_half(td, group, group_ins, stream, name, toggle, branch_pairs, boxes,
                box_expressions, renames, failures, top_row):
    """One gateable half of the coords group - `world`, `pixels`, `lm_*`. Returns it.

    Why a COMP per half rather than loose operators: `allowCooking` is what freezes
    a half, and it freezes a COMP and everything in it. Loose operators would each
    need writing individually, and the network would not say which ones belong
    together. It is also what lets `Facekeypoints` freeze the two landmark halves and
    nothing else.
    """
    half = group.create(td.baseCOMP, name)
    half.nodeX, half.nodeY = COL_SELECT * COL_W, top_row * ROW_H
    half.color = (0.4, 0.42, 0.34)

    # TWO In CHOPs, and which one a branch reads is the whole design.
    #
    #   in1  the TRIMMED merge. Every Select reads this, so a channel `early_trim`
    #        dropped is never composed - and a branch whose channels are all gone
    #        contributes nothing at no cost, which is what makes `Onefaceonly` and
    #        `Fingertipsonly` savings rather than list-shorteners.
    #   in2  `merge_streams`, UNTRIMMED. Read only by the box expressions.
    #
    # WHY THE BOX CANNOT COME FROM in1. Every landmark branch carries its face's box
    # as EXPRESSIONS on `gain` and `postoff`, and `Onefaceonly` deletes `f1_bbox_x`
    # along with the rest of `f1_*`. Indexing a CHOP for a missing channel returns
    # **None**, not zero (DESIGN.md 2.11), so the composition would raise - and a
    # guard that turned it into zero would be a plausible wrong number, which is
    # worse. in2 cannot be starved: the wire contract is FIXED (DESIGN.md 6.2) and a
    # frozen stream still publishes its channels holding their last value.
    # docs/BUILD_PLAN.md 25.2.
    used = [IN_TRIMMED] + ([FULL_INPUT] if boxes else [])
    half_ins = {}
    for index, src in enumerate(used):
        port = half.create(td.inCHOP, "in%d" % (index + 1))
        port.nodeX, port.nodeY = COL_IN * COL_W, -index * ROW_H
        half.inputConnectors[index].connect(group_ins[src])
        half_ins[src] = port
    half_in = half_ins[IN_TRIMMED]

    outputs = []
    described = []
    row = ROW_FIRST

    # -- the channels already in image space -------------------------------
    for _src, branch in branch_pairs:
        select = half.create(td.selectCHOP, "sel_%s" % branch.label)
        select.nodeX, select.nodeY = COL_SELECT * COL_W, row * ROW_H
        select.inputConnectors[0].connect(half_in)
        # A verified PATTERN, not a list of names: `spaces.py` expanded it against
        # the MERGED contract and returned it only on an exact match.
        select.par.channames = branch.pattern

        math = half.create(td.mathCHOP, "math_%s" % branch.label)
        math.nodeX, math.nodeY = COL_MATH * COL_W, row * ROW_H
        math.inputConnectors[0].connect(select)
        math.par.preoff = branch.offset
        math.par.gain.expr = GAINS[branch.suffix]
        # The rename goes on the MATH, not on the Select. Still two operators, but
        # the Select then does ONE pattern match instead of two - measured in the
        # live network at 0.0418 ms for a Select doing both against 0.0138 for a
        # Select doing only its `channames`.
        how = _apply_rename(math, branch.names, branch.source, branch.suffix,
                            failures, "%s/%s/%s" % (stream, name, math.name))
        renames[how] = renames.get(how, 0) + 1
        outputs.append(math)
        described.append("  %-14s %-3s %3d ch  preoff %+.1f"
                         % (branch.label, branch.suffix, len(branch.names),
                            branch.offset))
        row -= 1

    # -- the channels normalised to a bounding box -------------------------
    for box in boxes:
        select = half.create(td.selectCHOP, "lm_%s" % box.label)
        select.nodeX, select.nodeY = COL_SELECT * COL_W, row * ROW_H
        select.inputConnectors[0].connect(half_in)
        select.par.channames = box.pattern

        math = half.create(td.mathCHOP, "box_%s" % box.label)
        math.nodeX, math.nodeY = COL_MATH * COL_W, row * ROW_H
        math.inputConnectors[0].connect(select)
        # ONE operator for the whole two-step composition. `preoff` stays at zero:
        # the centring lives in `postoff`, multiplied by the same gain, because
        # `bbox_x - 0.5` has to be scaled and `point * bbox_w` has to be scaled by
        # the identical factor. Putting -0.5 in `preoff` would centre the POINT
        # inside its own box, which is a different and wrong thing.
        #
        # The accessor names in2 - the UNTRIMMED merge - for the reason in the
        # comment above the In CHOPs.
        gain_expr, postoff_expr = box_expressions(
            box, GAINS[box.suffix],
            "op('%s')['%%s']" % half_ins[FULL_INPUT].name)
        math.par.preoff = 0.0
        math.par.gain.expr = gain_expr
        math.par.postoff.expr = postoff_expr
        # On the MATH, for the same reason as above.
        how = _apply_rename(math, box.names, box.source, box.suffix,
                            failures, "%s/%s/%s" % (stream, name, math.name))
        renames[how] = renames.get(how, 0) + 1
        outputs.append(math)
        described.append("  %-10s %-24s %3d ch  through %s / %s"
                         % (box.label, box.pattern[:24], len(box.names),
                            box.origin, box.extent))
        row -= 1

    merged = half.create(td.mergeCHOP, "out")
    merged.nodeX, merged.nodeY = COL_MERGE * COL_W, 0
    for index, node in enumerate(outputs):
        merged.inputConnectors[index].connect(node)
    half_out = half.create(td.outCHOP, "out1")
    half_out.nodeX, half_out.nodeY = COL_OUT * COL_W, 0
    half_out.inputConnectors[0].connect(merged)

    note = half.create(td.textDAT, "notes")
    note.nodeX, note.nodeY = COL_IN * COL_W, ROW_H
    note.text = HALF_NOTES % {
        "name": name.upper(), "toggle": toggle,
        "space": "world" if name.endswith("world") else "pixel",
        "branches": "\n".join(["BRANCHES", *described])}
    return half


main()
