#!/usr/bin/env python
"""Add the one-euro position filter to the visionhands COMP. Paste, Run Script.

    WHAT IT DOES. Smooths the 84 landmark positions - every `h{i}_{joint}_x` and
    `_y` - upstream of everything else, so every derived distance, angle, curl,
    velocity and bounding box inherits the smoothing from one place. Everything
    that is not a position passes through untouched.

    ON THE FILTER PAGE
        Smoothing   toggle    off BYPASSES the filter - bit-exact passthrough,
                              and it stops it computing (0.0010 ms)
        Mincutoff   Hz        how heavily a SLOW-moving hand is smoothed
        Beta        1/units   how far a FAST-moving hand is allowed through

TOUCHDESIGNER HAS A ONE-EURO FILTER BUILT IN, and this uses it. That is worth
stating plainly because an earlier version of this file did not: it built the
filter by hand out of a Feedback CHOP and twelve Math CHOPs, on the conclusion that
the Filter CHOP could not work here. That conclusion was wrong, and the correction
is measured:

    MEASURED, same input, same 84 channels, mid-swipe:

        hand-built chain   0.6949 ms across 21 operators
        Filter CHOP        0.0153 ms in 1 operator

    ...a 45x reduction, and about 38% of the whole COMP's cook time. Output is
    functionally identical - within 0.17% under motion - and on a still hand the
    native one converges EXACTLY where the hand-built chain left a float32
    residual of 9e-8.

WHY THE WRONG CONCLUSION LOOKED RIGHT. The Filter CHOP was tested on `tmp_slope`,
a Math CHOP fed by a Feedback - a NON-time-sliced, one-sample-per-frame operator -
where all nine of its filter types returned the identical value, unchanged by
width. That reading was correct and the generalisation drawn from it was not: the
Filter CHOP is inert on a non-time-sliced one-sample CHOP, and works perfectly on
a TIME-SLICED one. `oef_in` derives from the OSC In CHOP, which is time-sliced, so
the filter has real slices to work on. DESIGN.md 2.11 carries the precise version.

WHAT THE NATIVE FILTER DOES NOT EXPOSE: `dcutoff`, the smoothing on the speed
estimate that drives the adaptation. It takes `cutoff` (the minimum cutoff) and
`speedcoeff` (beta) only. No loss in practice - 1.0 Hz is the published default and
there was never a reason to move it - and the `Dcutoff` parameter is therefore gone
rather than kept as a slider that does nothing.

WHY A ONE-EURO FILTER AT ALL. Landmark jitter and hand motion occupy the same
frequency band, so a fixed cutoff must choose: smooth enough to still a resting
hand, and a fast hand lags visibly; responsive enough to track a fast hand, and a
resting one shimmers. The one-euro filter makes the cutoff a function of estimated
speed, which is exactly the trade a constant cannot make. MEASURED on the
hand-built version, and the reason it was worth having: at rest the cutoff sat at
1.5 Hz, and a hand crossing the frame in 0.8 s lifted it to 4.35 Hz - 2.3x more
responsive on a real gesture than at rest.

**ABOUT BETA, because the published default is wrong for these units.** Every
one-euro reference sets `beta` near 0.007, and every one of them is filtering
PIXELS, where speeds run to hundreds of units per second. These channels are
normalised 0..1, so the same motion measures about a thousand times smaller and
`1.5 + 0.007 * 1.4` is no adaptation at all. Copying the published constant gives a
filter that behaves like a fixed one and invites the conclusion that one-euro does
not help. The default here is scaled for normalised units, and it is a GUESS -
measuring per-joint jitter is what should set it (DESIGN.md 11).

ONE FILTER PER STREAM, ONE SET OF CONTROLS. This builds a `filter` group inside
EVERY stream under `/project1/vision` - hands, pose and face - and all of them read
the same `Smoothing`, `Mincutoff` and `Beta` on the master COMP. Which channels get
filtered is decided by `visionhands/spaces.py` rather than by a pattern here:
positions, extents and angles are smoothed, and confidences, counters and flags are
not - a smoothed confidence makes every gate that reads it lag behind the thing it
gates.

Three groups rather than one shared filter operator, deliberately: each stream
arrives on its own port with its own time-slicing, and merging them into one filter
would make every stream cook when any of them arrived, and reintroduce exactly the
cross-stream name coupling that separate ports were chosen to avoid (DESIGN.md 6.4).

Ref: DESIGN.md 2.11 (the time-slicing rule), 6.4 (the streams), spaces.py (which
channels), docs/ATTRIBUTES.md (the smoothing section), docs/BUILD_PLAN.md step 7.
"""

import sys

REPO_ROOT = "/Users/omer/Documents/GitHub/visionhands-touchdesigner"
MASTER_PATH = "/project1/vision"
# The group this builds, as a child COMP of the one above. Everything it owns lives
# inside, so the top-level network shows one node called `filter` rather than five
# loose operators - and `allowCooking` on it gates the whole group at once
# (docs/BUILD_PLAN.md step 7.3).
GROUP = "filter"
PREFIX = "oef_"

# Every user parameter lives on the master COMP, and a nested operator reaches it
# through the Global OP Shortcut. NOT `parent(2)`, which is what this used before
# the streams were wrapped: a group is now two levels below the parameters instead
# of one, so a relative reference would have to be counted per operator and would
# break the next time anything moved. `op.Vision` is depth-independent and survives
# a rename (VERIFIED to resolve from a nested network).
MASTER_PAR = "op.Vision.par.%s"

BYPASS_CALLBACK = '''# Generated by tools/td_add_filter.py
#
# `Smoothing` off sets the Filter CHOP's BYPASS flag, which is a bit-exact
# passthrough that also stops it computing - MEASURED, 0.0110 ms -> 0.0010 ms. It
# replaced a Switch CHOP between a raw branch and a filtered one, which needed an
# extra operator and a second path through the group, and which pulled both inputs
# so the toggle never gated the cost.
#
# A DAT rather than an expression because `bypass` is an ATTRIBUTE, not a
# parameter: nothing to bind an expression to. Same pattern as the latch bank's
# threshold refresh.
STREAMS = %(streams)r
GROUP = %(group)r
NODE = %(node)r


def _apply(comp):
    """Set the bypass flag on EVERY stream's filter. One toggle, three groups."""
    wanted = not comp.par.Smoothing.eval()
    for stream in STREAMS:
        node = comp.op('%%s/%%s/%%s' %% (stream, GROUP, NODE))
        if node is not None:
            node.bypass = wanted


def onValueChange(par, prev):
    _apply(par.owner)


def onValuesChanged(changes):
    if changes:
        _apply(changes[0].par.owner)
'''

# The Filter PAGE is not built here any more: `tools/td_build_vision.py` owns every
# user parameter, so that one Smoothing toggle drives all three streams. One owner
# per page - two scripts appending to the same page is how a tuned value gets reset
# by whichever runs second (DESIGN.md 2.11 on append* clobbering attributes).
#
# The values, for reference, and both are GUESSES: Mincutoff 1.5 Hz stops a resting
# hand shimmering without visibly lagging a slow move; Beta 2.0 is scaled for
# normalised units, where hand speeds run about 0.1 to 3.0 units per second. What
# should set them is per-joint jitter, which is unmeasured.
FILTER_PARAMETERS = ("Smoothing", "Mincutoff", "Beta")

ROW_Y = -2100


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
    # TouchDesigner caches our modules for the life of the process, so an edited
    # contract would otherwise be invisible. DESIGN.md 2.11.
    for stale in [n for n in list(sys.modules)
                  if n == "visionhands" or n.startswith("visionhands.")]:
        del sys.modules[stale]
    from visionhands.spaces import passthrough_names, smoothed_names
    from visionhands.streams import STREAM_NAMES

    master = op(MASTER_PATH)
    if master is None:
        print("FAIL no COMP at %s - run tools/td_build_vision.py first" % MASTER_PATH)
        return
    missing = [name for name in FILTER_PARAMETERS if not hasattr(master.par, name)]
    if missing:
        print("FAIL %s has no %s - run tools/td_build_vision.py first"
              % (MASTER_PATH, ", ".join(missing)))
        return

    failures = []
    built = []
    for stream in STREAM_NAMES:
        child = master.op(stream)
        if child is None:
            # Not an error: a project can legitimately hold fewer streams than the
            # contract knows about. Reported, so an absent one is never a surprise.
            print("   (no `%s` stream in %s - skipped)" % (stream, MASTER_PATH))
            continue
        built.append(_build_one(td, master, child, stream,
                                smoothed_names(stream), passthrough_names(stream),
                                failures))

    # One DAT for all three groups. Beside the streams rather than inside one, so it
    # keeps working if any group is rebuilt without it - and because a single toggle
    # driving three groups has no business living in one of them.
    bypass_dat = master.op("filter_callbacks") or master.create(
        td.parameterexecuteDAT, "filter_callbacks")
    bypass_dat.nodeX, bypass_dat.nodeY = -900, 550
    bypass_dat.text = BYPASS_CALLBACK % {
        "streams": list(STREAM_NAMES), "group": GROUP, "prefix": PREFIX,
        "node": PREFIX + "smooth"}
    bypass_dat.par.op = master.path
    bypass_dat.par.pars = "Smoothing"
    bypass_dat.par.valuechange = True
    bypass_dat.par.active = True

    print()
    for stream, arrived, produced, cost in built:
        print("   %-5s %3d channels in, %3d out, filter %.4f ms"
              % (stream, arrived, produced, cost))
    print()
    print("   Smoothing=%s, Mincutoff=%.3f, Beta=%.3f - one set of controls on %s,"
          % (bool(master.par.Smoothing.eval()), master.par.Mincutoff.eval(),
             master.par.Beta.eval(), master.path))
    print("   read by every stream's filter.")

    if failures:
        print("\nFAILURES (%d):" % len(failures))
        for failure in failures:
            print("   " + failure)
    else:
        print("\nstructure verified. A filter cannot be judged from one frame:")
        print("   send_synthetic.py open  - a still hand: filtered == raw exactly")
        print("   send_synthetic.py swipe - a moving one: filtered lags")
        print("   then toggle Smoothing and compare")


def _build_one(td, master, child, stream, smoothed, passthrough, failures):
    """Build one stream's filter group. Returns (stream, chans in, chans out, ms).

    The two Selects must PARTITION the stream exactly: they are merged back together
    at the end, so a channel in neither list leaves the COMP's output entirely, with
    no error anywhere. That happened - four `sc_*` status channels vanished this way
    (DESIGN.md 2.11) - which is why the split comes from `visionhands/spaces.py` and
    why the check at the bottom compares the output against the INPUT rather than
    against the contract.
    """
    source = child.op("in1")
    if source is None:
        failures.append("no in1 in %s" % child.path)
        return (stream, 0, 0, 0.0)

    # REUSE the group COMP and clear its CHILDREN. Destroying and recreating the
    # group looks equivalent and is not: everything wired to its output connector is
    # orphaned, and a dangling input cannot be told apart from one that was never
    # connected - measured, on the second run of the destroy-and-recreate version,
    # as the COMP output falling from 495 channels to 57 while the builder reported
    # success (DESIGN.md 2.11).
    group = child.op(GROUP)
    if group is None:
        group = child.create(td.baseCOMP, GROUP)
    else:
        for existing in list(group.children):
            existing.destroy()
    group.nodeX, group.nodeY = -400, ROW_Y
    group.color = (0.35, 0.45, 0.55)

    def make(kind, name, x, y=0):
        node = group.create(kind, PREFIX + name)
        node.nodeX, node.nodeY = x, y
        return node

    group_in = group.create(td.inCHOP, "in1")
    group_in.nodeX, group_in.nodeY = -1600, 0
    group.inputConnectors[0].connect(source)

    # -- the input, split in two -------------------------------------------
    raw = make(td.selectCHOP, "in", -1400)
    raw.inputConnectors[0].connect(group_in)
    raw.par.channames = " ".join(smoothed)
    rest = make(td.selectCHOP, "rest", -1400, -150)
    rest.inputConnectors[0].connect(group_in)
    rest.par.channames = " ".join(passthrough)

    # -- the filter, which is one operator ---------------------------------
    smooth = make(td.filterCHOP, "smooth", -1200)
    smooth.inputConnectors[0].connect(raw)
    smooth.par.type = "oneeuro"
    # Time-sliced, which is the whole reason this works: the filter needs real
    # slices to carry state across, and `raw` inherits time-slicing from the OSC In
    # CHOP. On a non-time-sliced one-sample CHOP it is a silent passthrough.
    smooth.par.timeslice = True
    smooth.par.cutoff.expr = MASTER_PAR % "Mincutoff"
    smooth.par.speedcoeff.expr = MASTER_PAR % "Beta"
    # `Smoothing` drives the BYPASS flag: a bit-exact passthrough that also stops
    # the operator computing - measured, 0.0110 ms down to 0.0010 ms - so unlike a
    # Switch it gates the cost as well as the output. The consequence, and it is the
    # right trade: a bypassed filter is not advancing its state, so re-enabling it
    # settles from wherever it left off. It converges in a fraction of a second.
    smooth.bypass = not bool(master.par.Smoothing.eval())

    out = make(td.mergeCHOP, "out", -800)
    out.inputConnectors[0].connect(smooth)
    out.inputConnectors[1].connect(rest)
    group_out = group.create(td.outCHOP, "out1")
    group_out.nodeX, group_out.nodeY = -600, 0
    group_out.inputConnectors[0].connect(out)

    # -- verification -------------------------------------------------------
    group_in.cook(force=True)
    group_out.cook(force=True)
    arrived = tuple(c.name for c in group_in.chans())
    produced = tuple(c.name for c in group_out.chans())
    dropped = sorted(set(arrived) - set(produced))
    if dropped:
        failures.append("%s: %d channel(s) entered the filter and did not leave: "
                        "%r. Every channel this port carries has to be classified "
                        "in visionhands/spaces.py."
                        % (stream, len(dropped), dropped[:6]))
    duplicated = len(produced) - len(set(produced))
    if duplicated:
        failures.append("%s: %d duplicate channel(s) out of the filter - the two "
                        "Selects overlap" % (stream, duplicated))
    if not smooth.isTimeSlice:
        failures.append("%s: %ssmooth is not time-sliced, so it is a silent "
                        "passthrough - see the docstring" % (stream, PREFIX))
    for node in (raw, rest, smooth, out):
        if node.errors():
            failures.append("%s/%s: %s" % (stream, node.name, node.errors()))

    # -- into the stream's output ------------------------------------------
    # NOTHING is repointed at the filter here, and that is a deliberate change.
    # This script used to force a named list of consumers onto the group -
    # `derive_chop`, `sel_tx`, `sel_ty`, `sel_px`, `sel_py` - which failed two ways
    # at once: the four `sel_*` names moved inside the `coords` group and matched
    # nothing, and `derive_chop` does not EXIST yet when this runs in the documented
    # build order, so it wired itself to the raw input afterwards and stayed there.
    # Measured on the live network: `derive_chop` read `in1`, so every derived
    # attribute was computed on UNSMOOTHED landmarks while `coords` used smoothed
    # ones - two consumers of the same positions disagreeing about the hand.
    #
    # Each consumer now states its own input, which is the only arrangement that
    # cannot depend on build order.
    merge = child.op("merge_out")
    if merge is not None:
        _attach_once(merge, group, drop_names=(source.name, PREFIX + "out"))
        owners = [x.owner for conn in merge.inputConnectors
                  for x in conn.connections]
        if owners.count(group) != 1:
            failures.append("%s: `%s` feeds merge_out %d times, not once - the only "
                            "symptom a Merge CHOP gives is the channel count"
                            % (stream, GROUP, owners.count(group)))
    return (stream, len(arrived), len(produced), smooth.cookTime)


main()
