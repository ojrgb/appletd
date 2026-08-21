#!/usr/bin/env python
"""A `coords` COMP in every stream: world and pixel spaces. Paste, Run. Idempotent.

    WHAT IT BUILDS, in each of `/project1/vision`'s streams, from the normalised
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
stream's branches read them through `op.Vision`.

WHICH CHANNELS, AND WITH WHAT RULE, comes from `visionhands/spaces.py` rather than
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

import sys

REPO_ROOT = "/Users/omer/Documents/GitHub/visionhands-touchdesigner"
MASTER_PATH = "/project1/vision"
GROUP = "coords"

# Every user parameter lives on the master COMP, reached through its Global OP
# Shortcut. NOT `parent(2)`: a group is two levels below the parameters now, so a
# relative reference would have to be counted per operator and would break the next
# time anything moved. `op.Vision` is depth-independent and survives a rename.
MASTER_PAR = "op.Vision.par.%s"

# The GAIN each output space applies, by suffix - the `K` in the docstring's
# arithmetic. The pre-offset and the channel list come from visionhands/spaces.py;
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

# What feeds this. The FILTERED stream, so the derived coordinates inherit the
# smoothing like everything else - a project reading `_tx` and a project reading
# `derive`'s output should not disagree about where the hand is. It is also what
# makes the box-relative composition consistent: the points and the box a builder
# composes them through are both smoothed, or neither is.
SOURCE = "filter"

# ---- layout -----------------------------------------------------------------
# The group's position inside its stream comes from `visionhands/td_layout.py`, the
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
HALVES = (
    # name        toggle          the suffixes            box-relative?
    ("world", "Coordstx", ("_tx", "_ty", "_tw", "_th"), False),
    ("pixels", "Coordspx", ("_px", "_py", "_pw", "_ph"), False),
    ("lm_world", "Lmcoordstx", ("_tx", "_ty"), True),
    ("lm_pixels", "Lmcoordspx", ("_px", "_py"), True),
)

HALF_NOTES = """%(name)s - the %(space)s half of the coordinate spaces.

Everything in here is gated by op.Vision.par.%(toggle)s. `allowCooking` on this COMP
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
      -> lm_world  (Lmcoordstx) --+-> out -> out1
      -> lm_pixels (Lmcoordspx) --+

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
    expected = _renamed(names, old_suffix, new_suffix)
    node.par.renamefrom = "*" + old_suffix
    node.par.renameto = "*" + new_suffix
    node.cook(force=True)
    if [c.name for c in node.chans()] == expected:
        return "wildcard"

    # Pairwise BY NAME: a literal space-separated From/To list is applied name to
    # name, so a source that is absent keeps its own name while the rest still map
    # correctly - one channel wrong instead of every later one shifted onto the
    # wrong data.
    node.par.renamefrom = " ".join(names)
    node.par.renameto = " ".join(expected)
    node.cook(force=True)
    if [c.name for c in node.chans()] != expected:
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
                  if n == "visionhands" or n.startswith("visionhands.")]:
        del sys.modules[stale]
    from visionhands.spaces import box_branches, box_expressions, transform_branches
    from visionhands.streams import STREAM_NAMES
    from visionhands.td_layout import stream_xy

    master = op(MASTER_PATH)
    if master is None:
        print("FAIL no COMP at %s - run tools/td_build_vision.py first" % MASTER_PATH)
        return

    failures = []
    built = []
    for stream in STREAM_NAMES:
        child = master.op(stream)
        if child is None:
            print("   (no `%s` stream in %s - skipped)" % (stream, MASTER_PATH))
            continue
        built.append(_build_one(td, child, stream, transform_branches(stream),
                                box_branches(stream), box_expressions, failures,
                                stream_xy(GROUP)))

    print()
    for stream, operators, channels, renames in built:
        print("   %-5s %2d operators, %4d channels, renames: %s"
              % (stream, operators, channels,
                 ", ".join("%s x%d" % (k, v) for k, v in sorted(renames.items()))))
    print()
    print("   Orthowidth=%.3f, render %dx%d, source %dx%d - one set of controls on"
          % (master.par.Orthowidth.eval(), master.par.Renderw.eval(),
             master.par.Renderh.eval(), master.par.Resw.eval(),
             master.par.Resh.eval()))
    print("   %s, read by every stream's branches." % master.path)

    if failures:
        print("\nFAILURES (%d):" % len(failures))
        for failure in failures:
            print("   " + failure)
    else:
        print()
        print("   verified. `_ty` uses the RENDER's aspect, not the source image's:")
        print("   with Orthowidth %.3f and a %dx%d render, a point at the top of"
              % (master.par.Orthowidth.eval(), master.par.Renderw.eval(),
                 master.par.Renderh.eval()))
        print("   frame reads %+.3f."
              % (0.5 * master.par.Orthowidth.eval() * master.par.Renderh.eval()
                 / master.par.Renderw.eval()))


def _build_one(td, child, stream, branches, boxes, box_expressions, failures,
               group_xy):
    """Build one stream's coords group. Returns (stream, operators, channels, how)."""
    source = child.op(SOURCE) or child.op("in1")
    if source is None:
        failures.append("%s has neither `%s` nor `in1`" % (child.path, SOURCE))
        return (stream, 0, 0, {})
    if source.name != SOURCE:
        # Not fatal - the coordinates are still correct - but it means the spaces
        # are computed on UNSMOOTHED values while anything reading the filter is
        # smoothed, and the two would disagree about where the hand is. For the
        # box-relative branches it is worse: the points would be raw and the box
        # they compose through smoothed.
        failures.append("%s: no `%s` group, so these branches read the RAW input. "
                        "Run tools/td_add_filter.py first." % (stream, SOURCE))

    # REUSE the group and clear its CHILDREN. Destroying the group orphans
    # everything wired to its output, and a dangling input cannot be told apart from
    # one that was never connected - measured as a COMP output falling from 495
    # channels to 57 while the builder reported success (DESIGN.md 2.11).
    group = child.op(GROUP)
    if group is None:
        group = child.create(td.baseCOMP, GROUP)
    kept = _clear_keeping_ports(td, group, ("in1", "out1"))
    group.nodeX, group.nodeY = group_xy
    group.color = (0.45, 0.4, 0.3)

    group_in = kept.get("in1") or group.create(td.inCHOP, "in1")
    group_in.nodeX, group_in.nodeY = COL_IN * COL_W, 0
    # `outputConnectors[0]`, not the COMP: connect() accepts an operator only where
    # it has one unambiguous output, and a base COMP does not (DESIGN.md 2.11).
    group.inputConnectors[0].connect(
        source.outputConnectors[0] if source.isCOMP else source)

    if not branches and not boxes:
        # A stream with nothing to transform is legitimate - it just gets an empty
        # group rather than a missing one, so a later run has something to fill.
        group_out = kept.get("out1") or group.create(td.outCHOP, "out1")
        group_out.nodeX, group_out.nodeY = COL_OUT * COL_W, 0
        group_out.inputConnectors[0].connect(group_in)
        print("   (`%s` has no channels to transform)" % stream)
        return (stream, len(group.children), group_out.numChans, {})

    halves = []
    summary = []
    renames = {}
    row = 0
    for name, toggle, suffixes, box_half in HALVES:
        # A half takes EITHER the image-space branches or the box-relative ones,
        # never both - that separation is the whole reason there are four.
        mine = [] if box_half else [b for b in branches if b.suffix in suffixes]
        boxes_mine = [b for b in boxes if b.suffix in suffixes] if box_half else []
        if not mine and not boxes_mine:
            continue
        half = _build_half(td, group, group_in, stream, name, toggle, mine,
                           boxes_mine, box_expressions, renames, failures, row)
        halves.append(half)
        summary.append("  %-8s gated by %-10s %2d branches, %4d channels"
                       % (name, toggle, len(mine) + len(boxes_mine),
                          sum(len(b.names) for b in mine)
                          + sum(len(b.names) for b in boxes_mine)))
        # Stack the halves downward, leaving a row of clearance below the taller.
        row -= len(mine) + len(boxes_mine) + 2

    # One output rather than one per half: the stream's Merge takes a single input
    # for coordinates, which is the readability this whole exercise is for.
    merged = group.create(td.mergeCHOP, "out")
    merged.nodeX, merged.nodeY = COL_MERGE * COL_W, 0
    for index, half in enumerate(halves):
        merged.inputConnectors[index].connect(half.outputConnectors[0])
    group_out = kept.get("out1") or group.create(td.outCHOP, "out1")
    group_out.nodeX, group_out.nodeY = COL_OUT * COL_W, 0
    group_out.inputConnectors[0].connect(merged)
    group_out.cook(force=True)

    note = group.create(td.textDAT, "notes")
    note.nodeX, note.nodeY = COL_IN * COL_W, ROW_H
    note.text = NOTES % {
        "box_note": BOX_NOTE if boxes else "",
        "branches": "\n".join(["", "HALVES", *summary])}

    expected = sum(len(branch.names) for branch in branches) + sum(
        len(box.names) for box in boxes)
    if group_out.numChans != expected:
        failures.append("%s: `%s` produced %d channels, expected %d"
                        % (stream, GROUP, group_out.numChans, expected))
    # And no branch may collide with another: two branches renaming to the same
    # suffix would put duplicate names on the merge, which a Merge CHOP accepts
    # silently and only the channel count betrays.
    produced = [c.name for c in group_out.chans()]
    if len(produced) != len(set(produced)):
        failures.append("%s: `%s` has %d duplicate channel name(s)"
                        % (stream, GROUP, len(produced) - len(set(produced))))

    merge = child.op("merge_out")
    if merge is not None:
        stale = tuple(name for name in
                      (o.name for conn in merge.inputConnectors
                       for x in conn.connections for o in [x.owner])
                      if name.startswith(("ren_", "sel_", "math_", "lm_", "box_")))
        _attach_once(merge, group, drop_names=stale)
        owners = [x.owner for conn in merge.inputConnectors
                  for x in conn.connections]
        if owners.count(group) != 1:
            failures.append("%s: `%s` feeds merge_out %d times, not once"
                            % (stream, GROUP, owners.count(group)))
    return (stream, len(group.findChildren(depth=2)), group_out.numChans, renames)


def _build_half(td, group, group_in, stream, name, toggle, branches, boxes,
                box_expressions, renames, failures, top_row):
    """One gateable half of a coords group - `world` or `pixels`. Returns the COMP.

    Why a COMP per half rather than loose operators: `allowCooking` is what freezes
    a half, and it freezes a COMP and everything in it. Loose operators would each
    need writing individually, and the network would not say which ones belong
    together.
    """
    half = group.create(td.baseCOMP, name)
    half.nodeX, half.nodeY = COL_SELECT * COL_W, top_row * ROW_H
    half.color = (0.4, 0.42, 0.34)
    half_in = half.create(td.inCHOP, "in1")
    half_in.nodeX, half_in.nodeY = COL_IN * COL_W, 0
    half.inputConnectors[0].connect(group_in)

    outputs = []
    described = []
    row = ROW_FIRST

    # -- the channels already in image space -------------------------------
    for branch in branches:
        select = half.create(td.selectCHOP, "sel_%s" % branch.label)
        select.nodeX, select.nodeY = COL_SELECT * COL_W, row * ROW_H
        select.inputConnectors[0].connect(half_in)
        # A verified PATTERN, not a list of names: `spaces.py` expanded it against
        # this stream's own contract and returned it only on an exact match.
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
        described.append("  %-10s %-24s %3d ch  preoff %+.1f"
                         % (branch.label, branch.pattern, len(branch.names),
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
        gain_expr, postoff_expr = box_expressions(box, GAINS[box.suffix],
                                                  "op('%s')['%%s']" % half_in.name)
        math.par.preoff = 0.0
        math.par.gain.expr = gain_expr
        math.par.postoff.expr = postoff_expr
        # On the MATH, for the same reason as above.
        how = _apply_rename(math, box.names, box.source, box.suffix,
                            failures, "%s/%s/%s" % (stream, name, math.name))
        renames[how] = renames.get(how, 0) + 1
        outputs.append(math)
        described.append("  %-10s %-24s %3d ch  through %s / %s"
                         % (box.label, box.pattern, len(box.names),
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
        "space": "world" if name == "world" else "pixel",
        "branches": "\n".join(["BRANCHES", *described])}
    return half


main()
