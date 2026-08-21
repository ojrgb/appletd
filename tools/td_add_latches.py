#!/usr/bin/env python
"""Add the proximity latch bank to the visionhands COMP. Paste, Run Script.

    WHAT IT BUILDS
        h{i}_pinching       h{i}_e_pinch_start   h{i}_e_pinch_end
        h{i}_snapping       h{i}_e_snap          h{i}_e_snap_end
        hands_together      e_clap               e_apart
        h{i}_pinch_count    h{i}_snap_count      clap_count
        both_pinching

    ...which is pinch, snap and clap. 21 channels, no Python in the cook path.

Idempotent: every operator it owns is prefixed `lat_` and destroyed before the
rebuild, so running it twice leaves one network rather than two. Custom
parameters keep any value you have tuned - see `_threshold` below.

THE ONE MECHANISM, from docs/ATTRIBUTES.md:

    state = valid AND ( below_on OR (prev AND NOT above_off) )

Pinch, snap and clap are that same recurrence over three different distances, so
this builds ONE five-channel-wide network rather than three copies of a
one-channel one. Five latches, five channels, one set of operators. The
alternative - a base COMP duplicated three times - was the original plan in
docs/BUILD_PLAN.md; it was abandoned because duplication is how two of the three
copies end up subtly different.

WHY EVERY OPERATOR HERE IS NATIVE. The state has memory, and Python state inside
a Script CHOP survives a project reload in ways nothing controls - a latch that
comes back engaged after a reload emits a release pulse for a gesture that never
happened. Native CHOPs keep their state where TouchDesigner manages it, and the
whole recurrence is visible in the network rather than hidden in a callback.
docs/ATTRIBUTES.md makes this call and explains it at length.

THREE THINGS TOUCHDESIGNER DOES THAT THIS DEPENDS ON, all verified live against
the running instance (099) rather than taken from documentation:

  1. The Logic CHOP's `convert` parameter is a COMPARATOR: `lt` is "On When Less
     Than Zero" and `gt` is "On When Greater Than Zero". So `dist < on` is a Math
     CHOP subtract followed by one Logic CHOP - no Expression CHOP, no Python,
     and the threshold arrives as a channel rather than as a string of code.
  2. The Feedback CHOP takes NO target parameter. Input 0 is the operator whose
     PREVIOUS output it emits, and input 1 supplies the channel template and the
     initial values. With input 1 unconnected it produces ZERO channels, silently
     and with no error - and everything downstream then evaluates on nothing.
     That is the trap in this file; see `lat_zero`.
  3. The Logic CHOP defaults to `match = index`, so it pairs channels by POSITION
     and keeps the first input's names. Verified: ANDing a channel called `aaa`
     with one called `zzz` produced one channel called `aaa`, no error and no
     warning. Every multi-input operator here is set to `match = name`, and
     `_verify` then checks the names really are what the wiring assumed.

Ref: docs/ATTRIBUTES.md (the definitions, the thresholds, why a latch and not a
counter), docs/BUILD_PLAN.md step 1, visionhands/sequences.py (how it is tested).
"""

import sys

REPO_ROOT = "/Users/omer/Documents/GitHub/visionhands-touchdesigner"
COMP_PATH = "/project1/visionhands"

# Every operator this script owns. Destroyed before rebuilding, so the script is
# idempotent and so a renamed operator cannot leave an orphan behind cooking.
PREFIX = "lat_"

# ---------------------------------------------------------------------------
# The five latches
# ---------------------------------------------------------------------------
# ONE row per latch, and this order IS the channel order through every operator
# in the bank. Read it as: watch `distance`, gate on `valid`, engage below the
# `on` parameter, release above `off`, and publish under the last three names.
#
# `working` is the internal channel name. All five distances, all five validity
# flags and both threshold sets are renamed to these, which is what lets one set
# of operators serve all five latches: with the names identical across inputs,
# every Logic CHOP pairs the right channels by name instead of by luck.
LATCHES = (
    # working    distance            valid         on            off
    ("pinch0",   "h0_pinch_index",   "h0_valid",   "Pinchon",    "Pinchoff",
     "h0_pinching",    "h0_e_pinch_start", "h0_e_pinch_end", "h0_pinch_count"),
    ("pinch1",   "h1_pinch_index",   "h1_valid",   "Pinchon",    "Pinchoff",
     "h1_pinching",    "h1_e_pinch_start", "h1_e_pinch_end", "h1_pinch_count"),
    ("snap0",    "h0_pinch_middle",  "h0_valid",   "Snapon",     "Snapoff",
     "h0_snapping",    "h0_e_snap",        "h0_e_snap_end",  "h0_snap_count"),
    ("snap1",    "h1_pinch_middle",  "h1_valid",   "Snapon",     "Snapoff",
     "h1_snapping",    "h1_e_snap",        "h1_e_snap_end",  "h1_snap_count"),
    # `both_valid` rather than either hand's own flag: a two-hand distance is
    # meaningless unless both hands are there, and derive() already publishes 0
    # for `hands_distance` in that case - which is below every threshold, which
    # is exactly the absent-hand trap this gate exists to stop.
    ("together", "hands_distance",   "both_valid", "Togetheron", "Togetheroff",
     "hands_together", "e_clap",           "e_apart",        "clap_count"),
)

WORKING = tuple(row[0] for row in LATCHES)

# Tuning defaults, straight from docs/ATTRIBUTES.md. Every `off` is looser than
# its `on`: that gap IS the hysteresis and it IS the re-arm rule, so a build with
# them equal has neither and will chatter.
THRESHOLD_DEFAULTS = {
    "Pinchon": 0.35, "Pinchoff": 0.50,
    "Snapon": 0.25, "Snapoff": 0.45,
    "Togetheron": 0.60, "Togetheroff": 0.95,
}

# Where the bank is laid out, below the derive Script CHOP at y = -800.
ROW_Y = -1100
COLUMN_W = 200


def _threshold_page(comp, page_name="Tuning"):
    """The Tuning page, created once and never overwriting a tuned value.

    Traps: `appendCustomPage` with an existing name creates a SECOND page of that
           name, so the existing one is looked up first. And `appendFloat`
           replaces a parameter's attributes wholesale, so `.default` is set every
           time while `.val` is set only on creation - the same mistake in
           tools/td_build_comp.py once snapped a tuned Orthowidth back to 1.0.
    """
    page = None
    for existing in comp.customPages:
        if existing.name == page_name:
            page = existing
            break
    if page is None:
        page = comp.appendCustomPage(page_name)

    # Read the tuned values BEFORE touching anything, because appendFloat on an
    # existing parameter resets its VALUE to zero - not to its default, to zero.
    # Setting `.default` afterwards does not bring the value back. Measured: a
    # second run of this script left every threshold at 0.000, which disabled
    # every latch while the network still looked correct and reported no error.
    #
    # The note in tools/td_build_comp.py - ".default always, .val only when
    # creating" - is necessary but NOT sufficient. It stops a rebuild overwriting
    # a tuned value with a default; it does nothing about the append itself
    # clearing it. Only saving and restoring works.
    # `> 0` and not merely "present": a threshold is a ratio of hand size, so
    # zero is never a value anyone chose - it disables the latch it belongs to
    # while the network still cooks cleanly. Treating it as untuned means a COMP
    # damaged by the bug above heals itself on the next build instead of
    # preserving the damage faithfully for ever.
    tuned = {p.name: p.eval() for p in comp.customPars
             if p.name in THRESHOLD_DEFAULTS and p.eval() > 0.0}
    for name in ("Pinchon", "Pinchoff", "Snapon", "Snapoff",
                 "Togetheron", "Togetheroff"):
        par = page.appendFloat(name, label=name.replace("on", " On").replace(
            "off", " Off"))[0]
        par.default = THRESHOLD_DEFAULTS[name]
        par.normMin, par.normMax = 0.0, 2.0     # slider range; ratios of hand size
        # A value the user had tuned wins; otherwise the spec default.
        par.val = tuned.get(name, THRESHOLD_DEFAULTS[name])
    return page


def _rewire_merge(merge, nodes):
    """Set merge_out's inputs to exactly: what was there, then `nodes`, once each.

    Why it rebuilds the whole list instead of adding what is missing. Two
    behaviours of the Merge CHOP make the obvious approach wrong, and both were
    measured here rather than read anywhere:

      * Connecting to an input connector that reports itself FREE can INSERT
          rather than fill, pushing the existing connection down and leaving it
          attached at both positions. Five nodes connected that way produced five
          duplicate `derive_chop` connections - and `derive_chop` is 171 channels,
          so the COMP published 1710 where 171 were meant. A Merge CHOP accepts
          the same operator on any number of inputs without a word of complaint,
          and the duplicates are invisible downstream because `chan()` returns the
          first match. The output channel count is the only symptom.
      * The connection list does not settle after a destroy, even across a forced
          cook, so "is it already connected?" cannot be answered reliably at the
          moment a builder wants to know.

    So nothing is inspected and patched. The end state is asserted: disconnect
    everything, then connect the intended list in order. Idempotent however many
    times this script runs - which matters, because the MCP bridge has been
    observed executing it TWICE per call.

    Returns the names it wired, in order.
    """
    owned = {node.name for node in nodes}
    keep = []
    seen = set()
    for existing in merge.inputs:
        # First occurrence only, and never one of ours - ours are appended below
        # in a known order.
        if existing.name not in owned and existing.name not in seen:
            keep.append(existing)
            seen.add(existing.name)
    desired = keep + list(nodes)

    # Disconnect connector 0 repeatedly rather than iterating: the list shifts
    # under a disconnect, and a single pass leaves entries behind.
    for _attempt in range(256):         # bounded; a runaway loop is not a fix
        if not merge.inputs:
            break
        merge.inputConnectors[0].disconnect()
    for index, node in enumerate(desired):
        merge.inputConnectors[index].connect(node)
    return [node.name for node in desired]


def _verify_hysteresis(comp):
    """Every `off` must exceed its `on`, or there is no dead band to hold in."""
    problems = []
    for name in ("Pinch", "Snap", "Together"):
        on = float(getattr(comp.par, name + "on").eval())
        off = float(getattr(comp.par, name + "off").eval())
        if off <= on:
            problems.append("%s: on %.3f >= off %.3f - no dead band, will chatter"
                            % (name, on, off))
    return problems


def main():
    import td

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)

    comp = op(COMP_PATH)
    if comp is None:
        print("FAIL no COMP at %s - run tools/td_build_comp.py first" % COMP_PATH)
        return
    source = comp.op("derive_chop")
    if source is None:
        print("FAIL no derive_chop - run tools/td_add_derive.py first")
        return

    # -- clear our own operators ------------------------------------------
    removed = 0
    for child in list(comp.children):
        if child.name.startswith(PREFIX):
            child.destroy()
            removed += 1
    print("1. cleared %d existing %s* operators" % (removed, PREFIX))

    _threshold_page(comp)
    print("2. Tuning page: " + ", ".join(
        "%s=%.2f" % (n, float(getattr(comp.par, n).eval()))
        for n in sorted(THRESHOLD_DEFAULTS)))

    column = [0]

    def make(kind, name, y=ROW_Y):
        node = comp.create(kind, PREFIX + name)
        node.nodeX, node.nodeY = -1400 + column[0] * COLUMN_W, y
        column[0] += 1
        return node

    def wire(node, *inputs):
        for index, source_op in enumerate(inputs):
            node.inputConnectors[index].connect(source_op)
        return node

    def named(node):
        node.cook(force=True)
        return [c.name for c in node.chans()]

    # -- the five distances, renamed to the working names -----------------
    # Select's channames order determines the OUTPUT order (verified live), and
    # its built-in rename with from='*' maps the To list positionally onto that
    # order (also verified). So one operator both picks and renames.
    dist = make(td.selectCHOP, "dist")
    wire(dist, source)
    dist.par.chops = ""
    dist.par.channames = " ".join(row[1] for row in LATCHES)
    dist.par.renamefrom = "*"
    dist.par.renameto = " ".join(WORKING)

    # -- the five validity flags, renamed to the SAME working names -------
    # Three Selects rather than one, because `h0_valid` has to appear twice -
    # once gating the pinch latch and once gating the snap latch - and a single
    # Select cannot emit the same source channel under two names.
    #
    # This is the alignment problem docs/BUILD_PLAN.md warned about, solved at
    # the source: rather than gate `h0_pinch_index` with a channel called
    # `h0_valid` and hope the operator pairs them, both are called `pinch0` by
    # the time they meet.
    valid_parts = []
    for tag, rows in (("p", LATCHES[0:2]), ("s", LATCHES[2:4]), ("t", LATCHES[4:5])):
        part = make(td.selectCHOP, "valid_" + tag, ROW_Y - 150)
        wire(part, source)
        part.par.channames = " ".join(row[2] for row in rows)
        part.par.renamefrom = "*"
        part.par.renameto = " ".join(row[0] for row in rows)
        valid_parts.append(part)
    valid = make(td.mergeCHOP, "valid", ROW_Y - 150)
    wire(valid, *valid_parts)

    # -- the thresholds, as channels ---------------------------------------
    # Constant CHOPs with the working names, values bound by expression to the
    # Tuning parameters. Thresholds as CHANNELS rather than as numbers inside an
    # expression is what makes the comparison one operator wide instead of five:
    # `dist - on` is a channel-wise subtract, and every latch gets its own
    # threshold from its own channel.
    def thresholds(name, column_index, y):
        node = make(td.constantCHOP, name, y)
        node.seq.const.numBlocks = len(LATCHES)     # default is 1
        for index, row in enumerate(LATCHES):
            node.par["name%d" % index] = row[0]
            # An expression, not a value: retuning on the Tuning page takes
            # effect on the next cook, with nothing to rebuild.
            node.par["value%d" % index].expr = "parent().par.%s" % row[column_index]
        return node

    on = thresholds("on", 3, ROW_Y - 300)
    off = thresholds("off", 4, ROW_Y - 450)

    # The latch's INITIAL state: released. Also the Feedback CHOP's channel
    # template - see trap 2 in the module docstring. Zeros rather than the
    # validity flags, because initialising to "valid" would engage every latch on
    # the first frame a hand appeared and emit a start pulse for a gesture nobody
    # made.
    zero = make(td.constantCHOP, "zero", ROW_Y - 600)
    zero.seq.const.numBlocks = len(LATCHES)
    for index, row in enumerate(LATCHES):
        zero.par["name%d" % index] = row[0]
        zero.par["value%d" % index] = 0.0

    # -- the two comparisons -----------------------------------------------
    # Math sub is input1 - input2 (verified live), so these are dist - on and
    # dist - off, and the Logic CHOP's convert does the sign test.
    def compare(name, threshold, convert):
        sub = make(td.mathCHOP, "sub_" + name)
        wire(sub, dist, threshold)
        sub.par.chopop = "sub"
        sub.par.match = "name"
        test = make(td.logicCHOP, name)
        wire(test, sub)
        test.par.convert = convert          # 'lt' = On When Less Than Zero
        return test

    below_on = compare("below_on", on, "lt")        # dist < on
    above_off = compare("above_off", off, "gt")     # dist > off

    # -- the recurrence -----------------------------------------------------
    def logic(name, *inputs, **kwargs):
        node = make(td.logicCHOP, name)
        wire(node, *inputs)
        for key, value in kwargs.items():
            node.par[key] = value
        if len(inputs) > 1:
            # Never leave this at the default `index`. See trap 3.
            node.par.match = "name"
        return node

    # `prev` - the state on the previous frame. Input 0 closes the loop from
    # lat_state; input 1 is the template and the initial value.
    previous = make(td.feedbackCHOP, "prev")
    previous.par.output = "previous"                 # Previous Channels at Previous Time

    not_off = logic("not_above_off", above_off, preop="invert")
    held = logic("held", previous, not_off, chopop="and")        # prev AND NOT above_off
    engage = logic("engage", below_on, held, chopop="or")        # below_on OR held
    state = logic("state", engage, valid, chopop="and")          # ... AND valid

    # Close the loop. Wired last because lat_state has to exist first, and this
    # is the edge that makes the whole thing a latch rather than a comparator.
    previous.inputConnectors[0].connect(state)
    previous.inputConnectors[1].connect(zero)

    # -- the edge pulses ----------------------------------------------------
    # Derived from `prev` rather than from a Logic CHOP's own `rise`/`fall`
    # convert. Both work, but `prev` is the SAME previous state the latch itself
    # consulted, so a start pulse is provably the transition the latch made. An
    # independent edge detector keeps its own history, and if it ever cooks a
    # different number of times than the loop, the pulses drift away from the
    # state they are supposed to describe.
    not_prev = logic("not_prev", previous, preop="invert")
    not_state = logic("not_state", state, preop="invert")
    start = logic("start", state, not_prev, chopop="and")        # rose this frame
    end = logic("end", previous, not_state, chopop="and")        # fell this frame

    # -- the fire counters --------------------------------------------------
    # docs/ATTRIBUTES.md asks for `h{i}_pinch_count` - how many times the gesture
    # has fired - and is explicit that a Count CHOP is the right tool for that and
    # the wrong tool for the state itself.
    #
    # It counts every frame `start` is HIGH, not every off-to-on transition, and
    # that choice is deliberate: `start = state AND NOT prev` is one frame wide by
    # construction, so with a correct network the two counting modes agree
    # exactly. Counting frames-high therefore measures the pulse WIDTH as well as
    # the fire count - four sweeps must give exactly 4, and a pulse that ever
    # lingered two frames would give 8 and fail loudly. Off-to-on counting would
    # have quietly reported 4 either way.
    # An accumulator - Feedback plus an add - rather than a Count CHOP.
    #
    # The Count CHOP is the obvious operator and it was tried first. Two things
    # made it the harder of two equivalent options. Its `offtoon`/`on`/`ontooff`/
    # `off` parameters LOOK like toggles and are actually menus of actions
    # (none / inc / dec / inctime / dectime / reset), so assigning True leaves
    # them at 'none' with no error and the counter sits at zero for ever; and once
    # that was corrected it still read zero through four full sweeps, with Time
    # Slice as the remaining suspect and no way to see inside it.
    #
    # This is the same construction as the latch's own `prev`, which is already
    # proven to carry state across frames in this exact network - so it adds no
    # new unknowns, and what it does is legible in the network rather than
    # depending on how one operator interprets its own parameters:
    #
    #     count = count_on_the_previous_frame + start
    #
    # `start` is 0 or 1, so this counts the FRAMES the pulse was high. That is
    # deliberately stricter than counting rising edges: `start = state AND NOT
    # prev` is one frame wide by construction, so the two agree exactly when the
    # network is right, and a pulse that ever lingered two frames doubles the
    # count instead of passing silently.
    counter_prev = make(td.feedbackCHOP, "count_prev", ROW_Y - 600)
    counter_prev.par.output = "previous"
    counter = make(td.mathCHOP, "count", ROW_Y - 600)
    wire(counter, counter_prev, start)
    counter.par.chopop = "add"
    counter.par.match = "name"
    # Same trap as the latch's own feedback: input 1 is the channel template and
    # the initial value, and without it the operator emits nothing at all.
    counter_prev.inputConnectors[0].connect(counter)
    counter_prev.inputConnectors[1].connect(zero)

    # -- publish under the documented names --------------------------------
    def publish(node, name, column_index, y):
        out = make(td.renameCHOP, "out_" + name, y)
        wire(out, node)
        out.par.renamefrom = "*"
        out.par.renameto = " ".join(row[column_index] for row in LATCHES)
        return out

    out_state = publish(state, "state", 5, ROW_Y)
    out_start = publish(start, "start", 6, ROW_Y - 150)
    out_end = publish(end, "end", 7, ROW_Y - 300)
    out_count = publish(counter, "count", 8, ROW_Y - 600)

    # both_pinching: the one channel that combines two latches rather than
    # running alongside them. `chanop` combines channels WITHIN one CHOP, which
    # is what collapses two into one; `chopop` would have combined inputs.
    both_sel = make(td.selectCHOP, "both_sel", ROW_Y - 450)
    wire(both_sel, out_state)
    both_sel.par.channames = "h0_pinching h1_pinching"
    both = make(td.logicCHOP, "both_pinching", ROW_Y - 450)
    wire(both, both_sel)
    both.par.chanop = "and"
    both.par.renamefrom = "*"
    both.par.renameto = "both_pinching"

    # -- the clock ----------------------------------------------------------
    # THE MOST IMPORTANT OPERATOR IN THIS FILE, and the least obvious.
    #
    # TouchDesigner cooks on demand, per branch. A Merge CHOP whose downstream
    # consumer selects only `*_tx` and `*_ty` - which is exactly what this project
    # does - never pulls the branch carrying the latch channels. MEASURED on the
    # live network: `merge_out` had cooked 283,930 times while `lat_state` had
    # cooked TWICE, both of them because this script forced it.
    #
    # For a stateless branch that is a pure optimisation and entirely correct: an
    # uncooked distance is recomputed the moment anyone asks. For a branch with
    # MEMORY it is fatal. The recurrence advances once per cook, so a bank that
    # cooks twice an hour has a `prev` two cooks old, and every edge pulse -
    # `e_clap`, `e_snap`, every start and end - is computed between two frames
    # that were never adjacent. The state itself would eventually be right,
    # because the recurrence is level-driven and self-correcting; the pulses never
    # would be.
    #
    # It also cannot be left to the project downstream. Consuming any latch
    # channel does pull the whole recurrence - so a project that reads
    # `h0_pinching` gets correct behaviour by accident - but one that reads only
    # the raw landmarks silently gets a latch bank frozen in the past, and nothing
    # anywhere reports it.
    #
    # A Null CHOP with Cook Type = Always is the whole fix: it consumes the
    # deepest point of the bank, so everything upstream of it cooks every frame
    # whether or not anyone is watching the output.
    clock = make(td.nullCHOP, "tick", ROW_Y - 750)
    wire(clock, counter)
    clock.par.cooktype = "always"

    print("3. built %d operators" % sum(1 for c in comp.children
                                        if c.name.startswith(PREFIX)))

    # -- structural verification -------------------------------------------
    # Not a formality. `match = name` silently drops a channel it cannot pair,
    # and a Feedback CHOP missing its second input emits nothing at all - both
    # would leave a network that cooks cleanly and computes the wrong thing.
    print("4. structure:")
    failures = []
    for node, expected in ((dist, WORKING), (valid, WORKING), (on, WORKING),
                           (off, WORKING), (zero, WORKING), (below_on, WORKING),
                           (above_off, WORKING), (previous, WORKING),
                           (held, WORKING), (engage, WORKING), (state, WORKING),
                           (start, WORKING), (end, WORKING),
                           (out_state, tuple(r[5] for r in LATCHES)),
                           (out_start, tuple(r[6] for r in LATCHES)),
                           (out_end, tuple(r[7] for r in LATCHES)),
                           (counter, WORKING),
                           (out_count, tuple(r[8] for r in LATCHES)),
                           (clock, WORKING),
                           (both, ("both_pinching",))):
        actual = tuple(named(node))
        mark = "ok " if actual == tuple(expected) else "BAD"
        if actual != tuple(expected):
            failures.append("%s: expected %r, got %r" % (node.name, expected, actual))
        print("   %s %-22s %d chans  %s" % (mark, node.name, len(actual),
                                            " ".join(actual[:5])))
        if node.errors():
            failures.append("%s: %s" % (node.name, node.errors()))

    for problem in _verify_hysteresis(comp):
        failures.append(problem)

    # -- into the COMP's output -------------------------------------------
    merge = comp.op("merge_out")
    if merge is not None:
        wired = _rewire_merge(merge, (out_state, out_start, out_end,
                                      out_count, both))
        print("   merge_out inputs: %s" % ", ".join(wired))
        merge.cook(force=True)
        names = [o.name for o in merge.inputs]
        if len(names) != len(set(names)):
            failures.append("merge_out still has duplicate inputs: %r" % names)

    out_chop = comp.op("out1")
    out_chop.cook(force=True)
    print("5. COMP output: %d channels" % out_chop.numChans)
    for probe in ("h0_pinching", "h0_e_pinch_start", "h0_snapping",
                  "hands_together", "e_clap", "both_pinching", "h0_pinch_count"):
        channel = out_chop.chan(probe)
        print("   %-18s %s" % (probe, "MISSING" if channel is None
                               else round(channel[0], 3)))

    print("6. lat_tick cooks=%d (must climb on its own - that is the bank's clock)"
          % clock.totalCooks)

    if failures:
        print("\nFAILURES (%d):" % len(failures))
        for failure in failures:
            print("   " + failure)
    else:
        print("\nstructure verified. Now exercise it over TIME - a latch cannot be")
        print("checked from one frame:")
        print("   ~/.venvs/visionhands/bin/python tools/send_synthetic.py ramp --cycles 4")


main()
