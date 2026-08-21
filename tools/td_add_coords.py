#!/usr/bin/env python
"""Group the coordinate branches into a `coords` COMP. Paste, Run Script. Idempotent.

    WHAT IT BUILDS. Every joint position in three more coordinate spaces, from the
    normalised ones the sidecar sends:

        _tx, _ty   world units for an orthographic camera
        _px, _py   pixels in the SOURCE image

    168 channels, twelve operators, inside one node.

Moved out of tools/td_build_comp.py, which built these as twelve loose operators
at the top level. Two reasons: the top-level network reads better as a handful of
named nodes, and td_build_comp destroys every child of the COMP when it runs -
which made it impossible to rebuild the coordinate branches without also flattening
`filter`, `latches` and `temporal`. Each concern owning its own builder is what
makes any one of them re-runnable.

THE ARITHMETIC, which is the part that has been wrong twice. Each branch is
Select -> Math -> Rename: pick the channels, do the sum, give the result its own
suffix. The Math CHOP applies pre-offset then gain, so `(v - 0.5) * aspect` is
exactly `preoff = -0.5, gain = aspect` with no expression gymnastics.

    _tx = (x - 0.5) * Orthowidth
    _ty = (y - 0.5) * Orthowidth * Renderh / Renderw
    _px =  x * Resw
    _py =  y * Resh

**`_ty` uses the RENDER's aspect, not the source image's.** Ortho Width describes
the visible WIDTH, and the vertical extent follows from the resolution of whatever
is rendering - which is not the resolution of the camera feeding the sidecar. Those
two were the same 1280x720 while this was first built, which is exactly why the bug
survived: the wrong expression and the right one agreed. DESIGN.md 7.

Ref: DESIGN.md 7 (the coordinate contract), docs/BUILD_PLAN.md step 7.3.
"""

import sys

REPO_ROOT = "/Users/omer/Documents/GitHub/visionhands-touchdesigner"
COMP_PATH = "/project1/visionhands"
GROUP = "coords"

# Parameters stay on the TOP-LEVEL COMP so all tuning is in one place, which means
# an expression inside the group reaches them through the grandparent. `parent(2)`
# rather than a path, so it survives the COMP being renamed or moved.
PARENT_PAR = "parent(2).par.%s"

# label, which channels, output suffix, pre-offset, gain expression
BRANCHES = (
    ("tx", "*_x", "tx", -0.5, PARENT_PAR % "Orthowidth"),
    ("ty", "*_y", "ty", -0.5,
     "%s * %s / %s" % (PARENT_PAR % "Orthowidth", PARENT_PAR % "Renderh",
                       PARENT_PAR % "Renderw")),
    ("px", "*_x", "px", 0.0, PARENT_PAR % "Resw"),
    ("py", "*_y", "py", 0.0, PARENT_PAR % "Resh"),
)

# What feeds this. The FILTERED stream, so the derived coordinates inherit the
# smoothing like everything else - a project reading `_tx` and a project reading
# `derive`'s output should not disagree about where the hand is.
SOURCE = "filter"


def _attach_once(merge, node, drop_names=()):
    """Make `node` feed `merge` exactly once, dropping anything named in
    `drop_names`, and leave every other input alone. Returns what it removed.

    Traps, all of them measured in this repo (DESIGN.md 2.11):
      * `.inputs` is STALE inside the same script that rewired anything, so this
        reads `inputConnectors[i].connections` throughout - that is the truth.
      * disconnecting SHIFTS the connector list, so a single pass over it skips
        entries. This restarts after every removal. A single-pass version dropped
        two of four old branches and left the other two to be destroyed while
        still wired.
      * "attach if not already attached" is not enough on its own: piecemeal
        wiring of a Merge CHOP has produced a duplicate three times now, and a
        Merge accepts the same operator on any number of inputs in silence. The
        only symptom is the channel count - `filter` connected three times put 274
        extra channels on the COMP output. So this asserts the END STATE: exactly
        one connection from `node`.
      * `connect()` accepts an operator only where it has one unambiguous output,
        which a base COMP does not - hence `outputConnectors[0]`.
    """
    removed = []
    for _attempt in range(256):
        seen = False
        cut = False
        for conn in merge.inputConnectors:
            owners = [x.owner for x in conn.connections]
            if not owners:
                continue
            owner = owners[0]
            if owner.name in drop_names or (owner is node and seen):
                removed.append(owner.name)
                conn.disconnect()
                cut = True
                break                       # the list has shifted; start again
            if owner is node:
                seen = True
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

    comp = op(COMP_PATH)
    if comp is None:
        print("FAIL no COMP at %s - run tools/td_build_comp.py first" % COMP_PATH)
        return
    source = comp.op(SOURCE)
    if source is None:
        print("FAIL no `%s` group - run tools/td_add_filter.py first" % SOURCE)
        return

    # REUSE the group and clear its CHILDREN. Destroying the group orphans
    # everything wired to its output, and a dangling input cannot be told apart
    # from one that was never connected - measured elsewhere in this repo as a
    # COMP output falling from 495 channels to 57 while the builder reported
    # success. DESIGN.md 2.11.
    group = comp.op(GROUP)
    if group is None:
        group = comp.create(td.baseCOMP, GROUP)
    else:
        for child in list(group.children):
            child.destroy()
    group.nodeX, group.nodeY = -1100, -300
    group.color = (0.45, 0.4, 0.3)

    def make(kind, name, x, y):
        node = group.create(kind, name)
        node.nodeX, node.nodeY = x, y
        return node

    group_in = make(td.inCHOP, "in1", -600, 0)
    # `outputConnectors[0]`, not the COMP: connect() accepts an operator only where
    # it has one unambiguous output, and a base COMP does not (DESIGN.md 2.11).
    group.inputConnectors[0].connect(source.outputConnectors[0])

    outputs = []
    for index, (label, pattern, suffix, pre_offset, gain) in enumerate(BRANCHES):
        y = -150 * index
        select = make(td.selectCHOP, "sel_%s" % label, -400, y)
        select.inputConnectors[0].connect(group_in)
        select.par.channames = pattern

        math = make(td.mathCHOP, "math_%s" % label, -200, y)
        math.inputConnectors[0].connect(select)
        math.par.preoff = pre_offset
        math.par.gain.expr = gain

        rename = make(td.renameCHOP, "ren_%s" % label, 0, y)
        rename.inputConnectors[0].connect(math)
        rename.par.renamefrom = pattern
        rename.par.renameto = pattern.replace("_x", "_%s" % suffix).replace(
            "_y", "_%s" % suffix)
        outputs.append(rename)

    # One output rather than four. The parent used to take all four branches into
    # merge_out separately; a single 168-channel output means the top-level Merge
    # has one input for coordinates instead of four, which is the readability this
    # whole exercise is for.
    merged = make(td.mergeCHOP, "out", 200, 0)
    for index, node in enumerate(outputs):
        merged.inputConnectors[index].connect(node)
    group_out = make(td.outCHOP, "out1", 400, 0)
    group_out.inputConnectors[0].connect(merged)

    print("1. `%s`: %d operators, %d channels"
          % (GROUP, len(group.children), group_out.numChans))

    # -- into the COMP's output, replacing the four loose branches ----------
    failures = []
    merge = comp.op("merge_out")
    if merge is not None:
        # The four loose branches this group replaces, by name prefix.
        stale = tuple(name for name in
                      (o.name for conn in merge.inputConnectors
                       for x in conn.connections for o in [x.owner])
                      if name.startswith(("ren_", "sel_", "math_")))
        dropped = _attach_once(merge, group, drop_names=stale)
        owners = [x.owner for conn in merge.inputConnectors
                  for x in conn.connections]
        if owners.count(group) != 1:
            failures.append("`%s` feeds merge_out %d times, not once"
                            % (GROUP, owners.count(group)))
        print("2. merge_out: dropped %s, `%s` attached once"
              % (", ".join(sorted(set(dropped))) or "nothing", GROUP))

    # The loose operators this replaces, now that nothing reads them.
    removed = 0
    for child in list(comp.children):
        if child.name.startswith(("sel_", "math_", "ren_")):
            child.destroy()
            removed += 1
    print("3. removed %d loose coordinate operators from the top level" % removed)

    out_chop = comp.op("out1")
    out_chop.cook(force=True)
    print("4. COMP output: %d channels" % out_chop.numChans)
    for probe in ("h0_wrist_tx", "h0_wrist_ty", "h0_wrist_px", "h0_wrist_py"):
        channel = out_chop.chan(probe)
        print("   %-14s %s" % (probe, "MISSING" if channel is None
                               else round(channel[0], 4)))
        if channel is None:
            failures.append("%s is missing from the COMP output" % probe)

    if failures:
        print("\nFAILURES (%d):" % len(failures))
        for failure in failures:
            print("   " + failure)
    else:
        print("\nverified. `_ty` uses the RENDER aspect, so check it against your")
        print("camera: with Orthowidth %.3f and a %dx%d render, a wrist at the top"
              % (comp.par.Orthowidth.eval(), comp.par.Renderw.eval(),
                 comp.par.Renderh.eval()))
        print("of frame should read %+.3f."
              % (0.5 * comp.par.Orthowidth.eval() * comp.par.Renderh.eval()
                 / comp.par.Renderw.eval()))


main()
