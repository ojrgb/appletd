#!/usr/bin/env python
"""A `coords` COMP in every stream: world and pixel spaces. Paste, Run. Idempotent.

    WHAT IT BUILDS, in each of `/project1/vision`'s streams, from the normalised
    values the sidecar sends:

        _tx, _ty   world units for an orthographic camera
        _px, _py   pixels in the SOURCE image

    Hands: 168 channels. Pose: 152. Face: 16 - and the face is the interesting one,
    because a bounding box is not four positions.

EVERY STREAM, ONE SET OF PARAMETERS. This used to be hands-only, inside the hands
COMP, because hands came first. A body's joints and a face's box want exactly the
same conversion, and one Ortho Width has to serve all three or they disagree about
where the world is - which is why the parameters live on the master COMP and every
stream's branches read them through `op.Vision`.

WHICH CHANNELS, AND WITH WHAT RULE, comes from `visionhands/spaces.py` rather than
from a pattern here. Three distinctions it makes that a pattern cannot:

  * a POSITION gets the -0.5 offset, because world space is centred on the camera;
  * an EXTENT does not. A box is 0.2 wide wherever it sits, and subtracting 0.5
    from a width produces a negative size that draws as nothing;
  * an ANGLE is not transformed at all. There is no yaw in pixels.

And the one that would be a silent disaster: face LANDMARK points are normalised to
the FACE's bounding box, not to the image, so they are not positions in this sense
at all. Transforming them with the image rules would put every facial feature in
one corner of the frame. `spaces.py` marks them `box_relative` and they are left
alone here.

THE ARITHMETIC, which is the part that has been wrong twice. Each branch is
Select -> Math -> Rename: pick the channels, do the sum, give the result its own
suffix. The Math CHOP applies pre-offset then gain, so `(v - 0.5) * aspect` is
exactly `preoff = -0.5, gain = aspect` with no expression gymnastics.

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

Ref: DESIGN.md 7 (the coordinate contract), docs/BUILD_PLAN.md step 7.3.
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

# The GAIN each output space applies, by suffix. The pre-offset and the channel list
# come from visionhands/spaces.py; only the expressions are here, because only this
# script knows how to write a TouchDesigner expression.
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
# `derive`'s output should not disagree about where the hand is.
SOURCE = "filter"


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


def main():
    import td

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    # TouchDesigner caches our modules for the life of the process. DESIGN.md 2.11.
    for stale in [n for n in list(sys.modules)
                  if n == "visionhands" or n.startswith("visionhands.")]:
        del sys.modules[stale]
    from visionhands.spaces import source_suffix, transform_branches
    from visionhands.streams import STREAM_NAMES

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
                               source_suffix, failures))

    print()
    for stream, operators, channels in built:
        print("   %-5s %2d operators, %3d channels" % (stream, operators, channels))
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


def _build_one(td, child, stream, branches, source_suffix, failures):
    """Build one stream's coords group. Returns (stream, operators, channels)."""
    source = child.op(SOURCE) or child.op("in1")
    if source is None:
        failures.append("%s has neither `%s` nor `in1`" % (child.path, SOURCE))
        return (stream, 0, 0)
    if source.name != SOURCE:
        # Not fatal - the coordinates are still correct - but it means the spaces
        # are computed on UNSMOOTHED values while anything reading the filter is
        # smoothed, and the two would disagree about where the hand is.
        failures.append("%s: no `%s` group, so these branches read the RAW input. "
                        "Run tools/td_add_filter.py first." % (stream, SOURCE))

    # REUSE the group and clear its CHILDREN. Destroying the group orphans
    # everything wired to its output, and a dangling input cannot be told apart from
    # one that was never connected - measured as a COMP output falling from 495
    # channels to 57 while the builder reported success (DESIGN.md 2.11).
    group = child.op(GROUP)
    if group is None:
        group = child.create(td.baseCOMP, GROUP)
    else:
        for existing in list(group.children):
            existing.destroy()
    group.nodeX, group.nodeY = -1100, -300
    group.color = (0.45, 0.4, 0.3)

    def make(kind, name, x, y):
        node = group.create(kind, name)
        node.nodeX, node.nodeY = x, y
        return node

    group_in = make(td.inCHOP, "in1", -600, 0)
    # `outputConnectors[0]`, not the COMP: connect() accepts an operator only where
    # it has one unambiguous output, and a base COMP does not (DESIGN.md 2.11).
    group.inputConnectors[0].connect(
        source.outputConnectors[0] if source.isCOMP else source)

    if not branches:
        # A stream with nothing to transform is legitimate - it just gets an empty
        # group rather than a missing one, so a later run has something to fill.
        group_out = make(td.outCHOP, "out1", 400, 0)
        group_out.inputConnectors[0].connect(group_in)
        print("   (`%s` has no channels to transform)" % stream)
        return (stream, len(group.children), group_out.numChans)

    outputs = []
    for index, (label, suffix, names, pre_offset) in enumerate(branches):
        y = -150 * index
        select = make(td.selectCHOP, "sel_%s" % label, -400, y)
        select.inputConnectors[0].connect(group_in)
        # The channel NAMES, not a pattern: `spaces.py` decided which ones, and a
        # pattern here would silently reclassify the moment a contract gained a
        # channel whose name happened to end the same way (DESIGN.md 2.11).
        select.par.channames = " ".join(names)

        math = make(td.mathCHOP, "math_%s" % label, -200, y)
        math.inputConnectors[0].connect(select)
        math.par.preoff = pre_offset
        math.par.gain.expr = GAINS[suffix]

        rename = make(td.renameCHOP, "ren_%s" % label, 0, y)
        rename.inputConnectors[0].connect(math)
        # Pairwise BY NAME rather than by wildcard: a literal space-separated
        # From/To list is applied name to name, and a source that is absent keeps
        # its own name while the rest still map correctly - so a missing channel
        # costs one channel instead of shifting every later one onto the wrong data
        # (DESIGN.md 2.11). The wildcard form is what turned 126 channels into
        # `h0_wrist_x`, `h0_wrist_x1`, `h0_wrist_x2`.
        old_suffix = source_suffix(_role_for_suffix(suffix))
        rename.par.renamefrom = " ".join(names)
        rename.par.renameto = " ".join(
            name[:-len(old_suffix)] + suffix for name in names)
        outputs.append(rename)

    # One output rather than one per branch: the stream's Merge takes a single
    # input for coordinates, which is the readability this whole exercise is for.
    merged = make(td.mergeCHOP, "out", 200, 0)
    for index, node in enumerate(outputs):
        merged.inputConnectors[index].connect(node)
    group_out = make(td.outCHOP, "out1", 400, 0)
    group_out.inputConnectors[0].connect(merged)
    group_out.cook(force=True)

    expected = sum(len(names) for _label, _suffix, names, _off in branches)
    if group_out.numChans != expected:
        failures.append("%s: `%s` produced %d channels, expected %d"
                        % (stream, GROUP, group_out.numChans, expected))

    merge = child.op("merge_out")
    if merge is not None:
        stale = tuple(name for name in
                      (o.name for conn in merge.inputConnectors
                       for x in conn.connections for o in [x.owner])
                      if name.startswith(("ren_", "sel_", "math_")))
        _attach_once(merge, group, drop_names=stale)
        owners = [x.owner for conn in merge.inputConnectors
                  for x in conn.connections]
        if owners.count(group) != 1:
            failures.append("%s: `%s` feeds merge_out %d times, not once"
                            % (stream, GROUP, owners.count(group)))
    return (stream, len(group.children), group_out.numChans)


def _role_for_suffix(suffix):
    """Which role a target suffix came from, so the Rename knows what to strip.

    `_tx` and `_px` both come from an `_x`; `_tw` and `_pw` from a `_w`. Derived
    rather than tabled, because the two tables would be one edit away from
    disagreeing.
    """
    from visionhands.spaces import (
        ROLE_EXTENT_X,
        ROLE_EXTENT_Y,
        ROLE_POSITION_X,
        ROLE_POSITION_Y,
    )

    return {"x": ROLE_POSITION_X, "y": ROLE_POSITION_Y,
            "w": ROLE_EXTENT_X, "h": ROLE_EXTENT_Y}[suffix[-1]]


main()
