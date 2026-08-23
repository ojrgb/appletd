#!/usr/bin/env python
"""Add the one-euro position filter to the appletd COMP. Paste, Run Script.

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

ONE OPERATOR, NOT FOUR, AND THAT IS THE WHOLE GROUP NOW. This used to be

    in1 -> Select(smoothed) -> Filter -> Merge <- Select(everything else) -> out1

and it is now `in1 -> Filter(scope=...) -> out1`. A CHOP's `scope` parameter names
which channels the operator processes and passes every other one through
BIT-EXACT - VERIFIED by lagging a scoped channel and comparing an unscoped one to
the last bit. Two things follow, and the second matters more than the first:

    MEASURED, per cook, with data flowing, BEFORE this change - the four-operator
    form, summed over the group:

        face   Select 0.1725 + Filter 0.0346 + Select 0.0105 + Merge 0.0299 = 0.2475
        hands  0.1008 over 5 operators          pose  0.0897 over 5

    The Select was the expensive one, and it was expensive for a stupid reason: its
    `channames` carried 362 literal channel names, matched against every channel
    every frame. What survives the change is the Filter CHOP alone. The figures
    after it are in docs/JOURNAL.md and DESIGN.md 2.13, measured the same way.

    ...and the SILENT FAILURE the old form invited is gone with it. Two Selects
    merged back together had to partition the stream exactly, and four `sc_*`
    channels once fell into neither list and left the COMP's output with no error
    anywhere (DESIGN.md 2.11). There is no Select to get wrong now: a scoped
    operator cannot drop a channel it was not asked about.

Which channels get filtered is still decided by `appletd/spaces.py` rather than
by a pattern written here - positions, extents and angles are smoothed, and
confidences, counters and flags are not, because a smoothed confidence makes every
gate that reads it lag behind the thing it gates. `spaces.scope_pattern()` turns
that list into a `scope` string and VERIFIES the string selects exactly the list
before handing it over; if no pattern can, it returns the literal names.

Three groups rather than one shared filter operator, deliberately: each stream
arrives on its own port with its own time-slicing, and merging them into one filter
would make every stream cook when any of them arrived, and reintroduce exactly the
cross-stream name coupling that separate ports were chosen to avoid (DESIGN.md 6.4).

Ref: DESIGN.md 2.11 (the time-slicing rule), 6.4 (the streams), spaces.py (which
channels), docs/ATTRIBUTES.md (the smoothing section), docs/BUILD_PLAN.md step 7.
"""

import os
import sys

# Derived from this file's own location. A literal path here meant the project
# opened on exactly one machine; `__file__` is set by the shell, by
# TouchDesigner's run(), and by tools/td_rebuild.py before each exec.
# tools/td_paths.py has the full reasoning and why it is not imported from there.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_PATH = "/project1/appletd"
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
# break the next time anything moved. `op.Appletd` is depth-independent and survives
# a rename (VERIFIED to resolve from a nested network).
MASTER_PAR = "op.Appletd.par.%s"

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

# ---- layout -----------------------------------------------------------------
# The group's position inside its stream comes from `appletd/td_layout.py`,
# which is the ONE table for it: this script and td_add_coords.py both used to
# place their group at (-400, -300), directly on top of each other, and nothing
# could notice. The positions INSIDE this group are here, next to the code that
# builds them.
COL_W = 200
ROW_H = 150
# in1, the filter, out1 - three operators on one row, and the notes above them.
ORIGIN = (0, 0)
NOTE_XY = (0, ROW_H)

NOTES = """FILTER - the one-euro position filter, one operator.

  in1 -> %(node)s -> out1

%(node)s is a Filter CHOP at type=oneeuro with `scope` set to the channels that
should be smoothed. Everything else passes through BIT-EXACT: `scope` restricts
what the operator PROCESSES, not what it passes on.

  scope       %(scope)s
  smoothed    %(n_smoothed)d channels of %(n_total)d
  cutoff      op.Appletd.par.Mincutoff   (Hz, how hard a SLOW move is smoothed)
  speedcoeff  op.Appletd.par.Beta        (how much of a FAST move gets through)

WHY ONE OPERATOR. This was Select -> Filter -> Merge <- Select. Two things were
wrong with that. It cost 0.2475 ms per cook on the face stream, nearly all of it
in a Select matching 362 literal channel names; and the two Selects had to
partition the stream exactly, because they were merged back together - four sc_*
channels once fell into neither and left the COMP's output with no error anywhere.
A scoped operator cannot drop a channel it was not asked about.

WHY IT MUST BE TIME-SLICED. The Filter CHOP is a silent passthrough on a
non-time-sliced one-sample CHOP and works correctly on a time-sliced one. The
input is time-sliced because the OSC In CHOP is. Do not turn that off.

SMOOTHING OFF sets this operator's BYPASS flag, from filter_callbacks on the
master COMP - a bit-exact passthrough that also stops it computing. `bypass` is an
attribute, not a parameter, so nothing can bind an expression to it.

Built by tools/td_add_filter.py. Edits here are overwritten on the next run."""



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
                  if n == "appletd" or n.startswith("appletd.")]:
        del sys.modules[stale]
    from appletd.spaces import passthrough_names, scope_pattern, smoothed_names
    from appletd.streams import STREAM_NAMES
    from appletd.td_layout import master_xy, stream_xy

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
                                failures, scope_pattern(stream),
                                stream_xy(GROUP), _keep_layout(master)))

    # One DAT for all three groups. Beside the streams rather than inside one, so it
    # keeps working if any group is rebuilt without it - and because a single toggle
    # driving three groups has no business living in one of them.
    bypass_dat = master.op("filter_callbacks") or master.create(
        td.parameterexecuteDAT, "filter_callbacks")
    bypass_dat.nodeX, bypass_dat.nodeY = master_xy("filter_callbacks")
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
    for stream in STREAM_NAMES:
        pattern = scope_pattern(stream)
        print("   %-5s scope %s" % (stream, pattern if len(pattern) < 60
                                    else "%d literal names" % len(pattern.split())))
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


def _build_one(td, master, child, stream, smoothed, passthrough, failures, scope,
               group_xy, keep):
    """Build one stream's filter group. Returns (stream, chans in, chans out, ms).

    The check at the bottom compares the output against the INPUT rather than
    against the contract, and it stays even though `scope` makes a dropped channel
    structurally impossible now. It cost nothing and it is the check that caught
    four `sc_*` channels leaving the network when the group was a pair of Selects
    merged back together (DESIGN.md 2.11); a builder that stops asking is a builder
    that will not notice the next time the shape changes.
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
    existed = group is not None
    if group is None:
        group = child.create(td.baseCOMP, GROUP)
    kept = _clear_keeping_ports(td, group, ("in1", "out1"))
    _place(group, group_xy, keep, existed)
    group.color = (0.35, 0.45, 0.55)

    group_in = kept.get("in1") or group.create(td.inCHOP, "in1")
    group_in.nodeX, group_in.nodeY = ORIGIN
    group.inputConnectors[0].connect(source)

    # -- the filter, which is the entire group -----------------------------
    smooth = group.create(td.filterCHOP, PREFIX + "smooth")
    smooth.nodeX, smooth.nodeY = ORIGIN[0] + COL_W, ORIGIN[1]
    smooth.inputConnectors[0].connect(group_in)
    smooth.par.type = "oneeuro"
    # Time-sliced, which is the whole reason this works: the filter needs real
    # slices to carry state across, and the input inherits time-slicing from the OSC
    # In CHOP. On a non-time-sliced one-sample CHOP it is a silent passthrough.
    smooth.par.timeslice = True
    # WHICH channels, from spaces.py and verified there against the stream's own
    # contract. Everything not in scope passes through bit-exact.
    smooth.par.scope = scope
    smooth.par.cutoff.expr = MASTER_PAR % "Mincutoff"
    smooth.par.speedcoeff.expr = MASTER_PAR % "Beta"
    # `Smoothing` drives the BYPASS flag: a bit-exact passthrough that also stops
    # the operator computing - measured, 0.0110 ms down to 0.0010 ms - so unlike a
    # Switch it gates the cost as well as the output. The consequence, and it is the
    # right trade: a bypassed filter is not advancing its state, so re-enabling it
    # settles from wherever it left off. It converges in a fraction of a second.
    smooth.bypass = not bool(master.par.Smoothing.eval())

    group_out = kept.get("out1") or group.create(td.outCHOP, "out1")
    group_out.nodeX, group_out.nodeY = ORIGIN[0] + 2 * COL_W, ORIGIN[1]
    group_out.inputConnectors[0].connect(smooth)

    # -- what this network is, beside the network --------------------------
    note = group.create(td.textDAT, "notes")
    note.nodeX, note.nodeY = NOTE_XY
    note.text = NOTES % {"node": smooth.name, "scope": scope[:70],
                         "n_smoothed": len(smoothed),
                         "n_total": len(smoothed) + len(passthrough)}

    # -- verification -------------------------------------------------------
    group_in.cook(force=True)
    group_out.cook(force=True)
    arrived = tuple(c.name for c in group_in.chans())
    produced = tuple(c.name for c in group_out.chans())
    dropped = sorted(set(arrived) - set(produced))
    if dropped:
        failures.append("%s: %d channel(s) entered the filter and did not leave: "
                        "%r - which `scope` should make impossible, so the shape of "
                        "this group has changed" % (stream, len(dropped), dropped[:6]))
    duplicated = len(produced) - len(set(produced))
    if duplicated:
        failures.append("%s: %d duplicate channel(s) out of the filter"
                        % (stream, duplicated))
    if not smooth.isTimeSlice:
        failures.append("%s: %ssmooth is not time-sliced, so it is a silent "
                        "passthrough - see the docstring" % (stream, PREFIX))
    # And the thing `scope` itself can get wrong: selecting the wrong SET. Read off
    # the live operator rather than trusting the string that was written to it.
    #
    # Both sides counted from WHAT ARRIVED, not from the contract. MEASURED 2026-08-22:
    # adding `sc_segment` to the status channels made the contract 142 channels while
    # the OSC In CHOP still held the previous session's 141, and this check reported
    # 83 against 84 on a filter that was correct. A contract that has grown but has
    # not yet been SENT is the normal state between a code change and the next Start,
    # and it must not read as a fault - the sidecar is a separate process and its
    # channel list arrives when it restarts (DESIGN.md 6.4).
    #
    # What this still catches, which is the point: `scope` selecting the wrong subset
    # of the channels that DID arrive.
    present = set(arrived)
    in_scope = smooth.numChans - len(present.intersection(passthrough))
    expected_in_scope = len(present.intersection(smoothed))
    if arrived and in_scope != expected_in_scope:
        failures.append("%s: the filter's scope covers %d of the channels that "
                        "arrived, expected %d - spaces.scope_pattern() and the live "
                        "contract disagree"
                        % (stream, in_scope, expected_in_scope))
    missing = sorted(set(smoothed) - present)
    if missing:
        # NOT a failure: the contract grew and the sidecar has not restarted. Said
        # out loud, though, because "83 of 84" with no explanation is exactly the
        # kind of quiet discrepancy that gets ignored until it matters.
        print("   (%s: %d contract channel(s) have not arrived yet - the sidecar "
              "sends them after a restart: %s)"
              % (stream, len(missing), ", ".join(missing[:4])))
    if smooth.errors():
        failures.append("%s/%s: %s" % (stream, smooth.name, smooth.errors()))

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
