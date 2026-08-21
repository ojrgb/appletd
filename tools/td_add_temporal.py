#!/usr/bin/env python
"""Add the temporal channels to the visionhands COMP. Paste, Run Script. Idempotent.

    WHAT IT BUILDS
        h{i}_active     h{i}_entering   h{i}_exiting   h{i}_held
        both_active     n_active
        h{i}_vel_x  h{i}_vel_y  h{i}_speed  h{i}_accel
        hands_approach

    Presence with a real debounce, and the velocity family. 21 channels.

THE DEBOUNCE IS THE SAME SCHMITT TRIGGER AS THE LATCHES, which is worth seeing
because it looks like a different problem. docs/ATTRIBUTES.md asks for `active` =
valid held for `Activateframes` before turning on and `Deactivateframes` before
turning off. Written as a recurrence that is:

    active = all_valid_over_last_A OR ( prev AND any_valid_over_last_D )

- `all_valid_over_last_A` is the minimum of `valid` over a Trail of A samples: 1
  only if every one of those frames was valid. That is the turn-on condition.
- `any_valid_over_last_D` is the MAXIMUM over a Trail of D samples: 0 only if all
  D frames were invalid. So its negation is the turn-off condition.
- Between them the state holds, exactly as a dead band does.

So the asymmetry docs/ATTRIBUTES.md asks for - slower to drop a hand than to
acquire one, because losing one mid-gesture is worse than gaining one late - is
just two different window lengths on the same shape, and it inherits the same
self-correcting property: every kind of desynchronisation resolves on the next
frame where the input is unambiguous.

WHY THIS MATTERS BEYOND PRESENCE. The latch bank currently gates on raw per-frame
validity, so a single low-confidence frame mid-pinch drops the state, fires a
spurious end pulse, and re-engages with a spurious start and a bumped counter.
`h{i}_active` is what it should gate on. This builder publishes it; the latch
builder ANDs it in if it finds it.

EVERY MEMORY GROUP NEEDS ITS OWN CLOCK. DESIGN.md 2.10: TouchDesigner does not
cook a branch whose channels nothing downstream asks for, and a recurrence that
cooks intermittently produces correct-looking states with nonsense edges. So this
builds its own Cook Type = Always Null pulling all of its terminal branches,
exactly as the latch bank does. Do not remove it, and do not assume the COMP's
output being consumed is enough - measured, it is not.

Ref: docs/ATTRIBUTES.md (Presence and Motion), docs/BUILD_PLAN.md step 2,
DESIGN.md 2.10 (the clock), 2.11 (the operator behaviours).
"""

import sys

REPO_ROOT = "/Users/omer/Documents/GitHub/visionhands-touchdesigner"
COMP_PATH = "/project1/visionhands"
PREFIX = "tmp_"

# Advanced-page defaults, from docs/ATTRIBUTES.md.
#
# Deactivate is higher than Activate on purpose: dropping a hand mid-gesture is
# worse than acquiring one a frame late, so the filter is deliberately asymmetric.
DEFAULTS = {
    "Activateframes": 3,
    "Deactivateframes": 6,
}
# Seconds of smoothing on velocity before speed and acceleration read it.
# Differentiating anything amplifies its noise, and the positions are already
# one-euro filtered, so this is the second stage rather than the only one.
# GUESSED - per-joint jitter is what should set it (DESIGN.md 11).
# 0.15 s rather than the 0.08 that docs/ATTRIBUTES.md guessed. This is a moving
# average now, and its width has to span several DATA frames to cancel the 30-into-
# 60 sampling pattern - 0.15 s is about nine cooks, so four or five data frames.
# 0.08 s spans two and leaves the alternation visible in the output.
VELOCITY_FILTER_S = 0.15

ROW_Y = -2600


def _page(comp, name="Advanced"):
    """The Advanced page. Created once; tuned values survive a rebuild.

    Traps: `appendCustomPage` with an existing name makes a second page of that
           name, and `appendInt`/`appendFloat` on an existing parameter reset its
           value to zero rather than to its default. Both cost time elsewhere in
           this repo; see tools/td_add_latches.py.
    """
    page = None
    for existing in comp.customPages:
        if existing.name == name:
            page = existing
            break
    if page is None:
        page = comp.appendCustomPage(name)

    wanted = dict(DEFAULTS)
    tuned = {p.name: p.eval() for p in comp.customPars if p.name in wanted}
    for parameter, default in sorted(wanted.items()):
        par = page.appendInt(parameter, label=parameter)[0]
        par.default = default
        par.normMin, par.normMax = 1, 30
        previous = tuned.get(parameter)
        # A window of zero frames is not a shorter debounce, it is a Trail with no
        # samples. Treated as untuned.
        par.val = previous if previous and previous >= 1 else default

    was = {p.name: p.eval() for p in comp.customPars if p.name == "Velocityfilter"}
    par = page.appendFloat("Velocityfilter", label="Velocity Filter")[0]
    par.default = VELOCITY_FILTER_S
    par.normMin, par.normMax = 0.0, 0.5
    par.val = was.get("Velocityfilter", VELOCITY_FILTER_S)
    return page


def main():
    import td

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    # TouchDesigner caches our modules across runs. See tools/td_add_latches.py.
    for stale in [n for n in list(sys.modules)
                  if n == "visionhands" or n.startswith("visionhands.")]:
        del sys.modules[stale]

    comp = op(COMP_PATH)
    if comp is None:
        print("FAIL no COMP at %s - run tools/td_build_comp.py first" % COMP_PATH)
        return
    source = comp.op("derive_chop")
    if source is None:
        print("FAIL no derive_chop - run tools/td_add_derive.py first")
        return

    removed = 0
    for child in list(comp.children):
        if child.name.startswith(PREFIX):
            child.destroy()
            removed += 1
    print("1. cleared %d existing %s* operators" % (removed, PREFIX))

    _page(comp)
    print("2. Advanced page: %s, Velocityfilter=%.3f s" % (
        ", ".join("%s=%d" % (n, int(getattr(comp.par, n).eval()))
                  for n in sorted(DEFAULTS)), float(comp.par.Velocityfilter.eval())))

    column = [0]

    def make(kind, name, y=ROW_Y):
        node = comp.create(kind, PREFIX + name)
        node.nodeX, node.nodeY = -1400 + column[0] * 170, y
        column[0] += 1
        return node

    def wire(node, *inputs):
        for index, source_op in enumerate(inputs):
            node.inputConnectors[index].connect(source_op)
        return node

    def maths(name, *inputs, y=ROW_Y, **pars):
        node = make(td.mathCHOP, name, y)
        wire(node, *inputs)
        for key, value in pars.items():
            if key.endswith("_expr"):
                node.par[key[:-5]].expr = value
            else:
                node.par[key] = value
        if len(inputs) > 1:
            node.par.match = "name"     # never the `index` default, DESIGN.md 2.11
        return node

    def derivative(name, input_op, y):
        """d/dt of every channel, per second, frame to frame.

        NOT the Slope CHOP. Slope differentiates along the SAMPLE axis, and every
        CHOP here carries one sample per frame - a snapshot, not a time-sliced
        signal - so it has no neighbour to difference against. Measured: it
        reported palm velocities of 12.5 and 32.5 units per second on coordinates
        that cannot leave 0..1, and an acceleration of 1950.

        So the same construction as everything else with memory, and the same one
        the one-euro filter uses for its own speed estimate:

            v = (x - x_on_the_previous_frame) * rate

        Returns the raw derivative; the caller filters it.
        """
        previous = make(td.feedbackCHOP, name + "_prev", y)
        previous.par.output = "previous"
        # Input 1 is the Feedback's template AND its initial value, and here it is
        # the INPUT rather than a zero CHOP. Seeding it with zeros makes the very
        # first frame report `x * rate` - measured, a palm at 0.208 gave a velocity
        # of 12.5 units per second on a coordinate that cannot leave 0..1, which
        # then propagated into acceleration as 1950 and took the velocity filter
        # 0.08 s to shed. Seeded from the input, the first frame reports 0, which is
        # the only defensible answer when there is no previous frame to difference
        # against.
        wire(previous, input_op, input_op)
        difference = maths(name + "_d", input_op, previous, chopop="sub", y=y)
        return maths(name, difference, gain_expr="me.time.rate", y=y)

    def moving_average(name, input_op, y, width_par="Velocityfilter"):
        """The mean of every channel over `width_par` seconds. Trail + Analyze.

        NOT the Filter CHOP, and NOT the Lag CHOP. The Filter CHOP smooths along
        the SAMPLE axis, and every CHOP here carries one sample per frame, so it
        has nothing to smooth across. Measured: all nine of its filter types
        returned the identical value, unchanged by width - it was a passthrough,
        silently. Same family as the Slope CHOP above.

        The rule that falls out, and it applies to everything still to build:
        operators that CONSUME a sample axis (Slope, Filter) are inert on a
        one-sample-per-frame CHOP; operators that CREATE one from successive frames
        (Trail) work, which is why the debounce above does.

        And a moving average is not merely a smoothing choice here, it is the
        correction. The raw derivative divides a position step by the COOK interval,
        but positions only change when a frame ARRIVES - at 30 fps into a 60 fps
        cook that is every other frame, so the per-cook derivative alternates
        between double-size and zero. Measured: a palm step of 0.0592 in one cook,
        where 0.0292 was a cook's worth of motion. The mean over a window is
        (x_now - x_then) / elapsed, which is exactly right regardless of how the
        updates land, so averaging is what makes the number true rather than just
        smooth.
        """
        trail = make(td.trailCHOP, name + "_trail", y)
        wire(trail, input_op)
        trail.par.wlengthunit = "seconds"
        trail.par.wlength.expr = "parent().par.%s" % width_par
        averaged = make(td.analyzeCHOP, name, y)
        wire(averaged, trail)
        averaged.par.function = "average"
        return averaged

    def select(name, source_op, names, renames=None, y=ROW_Y):
        node = make(td.selectCHOP, name, y)
        wire(node, source_op)
        node.par.channames = " ".join(names)
        if renames:
            # Keyed on names, so a missing source costs one channel rather than
            # shifting every later one onto the wrong data.
            node.par.renamefrom = " ".join(names)
            node.par.renameto = " ".join(renames)
        return node

    # =====================================================================
    # Presence: the debounce
    # =====================================================================
    valid_raw = select("valid_raw", source, ["h0_valid", "h1_valid"],
                       ["h0_active", "h1_active"])

    # LIVENESS BELONGS IN HERE TOO. `h{i}_valid` is computed from the last frame
    # TouchDesigner received, and a dead sidecar leaves that frozen rather than
    # zeroed (DESIGN.md 2.9) - so without this, `h0_active` reads 1 and `h0_held`
    # keeps climbing for ever after the stream stops. Measured: held reached 54 s
    # with nothing sending. The latch gate was already immune because it ANDs
    # liveness separately, but a project reading `h0_active` would not be.
    #
    # ANDed BEFORE the debounce rather than after, so presence decays through the
    # normal Deactivateframes path instead of snapping off.
    #
    # An optional dependency: liveness is currently owned by tools/td_add_latches.py
    # as `lat_live`. It is a property of the STREAM rather than of either group and
    # should move somewhere shared - noted rather than done, because moving it means
    # touching both builders and this is not the moment.
    live_source = comp.op("lat_live")
    if live_source is not None:
        live2 = make(td.constantCHOP, "live2")
        live2.seq.const.numBlocks = 2
        for index, hand in enumerate(("h0_active", "h1_active")):
            live2.par["name%d" % index] = hand
            live2.par["value%d" % index].expr = "op('lat_live')['seq']"
        gated = make(td.logicCHOP, "valid")
        wire(gated, valid_raw, live2)
        gated.par.chopop = "and"
        gated.par.match = "name"
        valid = gated
    else:
        valid = valid_raw
    zero = maths("zero", valid, gain=0.0)       # template for the feedbacks

    # ALL valid over the last Activateframes: the minimum of a Trail. `samples`
    # rather than `frames` or `seconds`, because the parameter is a frame count and
    # this branch is clocked once per frame by tmp_tick.
    trail_on = make(td.trailCHOP, "trail_on")
    wire(trail_on, valid)
    trail_on.par.wlengthunit = "samples"
    trail_on.par.wlength.expr = "parent().par.Activateframes"
    all_valid = make(td.analyzeCHOP, "all_valid")
    wire(all_valid, trail_on)
    all_valid.par.function = "minimum"

    # ANY valid over the last Deactivateframes: the maximum. Its negation is the
    # turn-off condition, so this is the term that HOLDS the state.
    trail_off = make(td.trailCHOP, "trail_off")
    wire(trail_off, valid)
    trail_off.par.wlengthunit = "samples"
    trail_off.par.wlength.expr = "parent().par.Deactivateframes"
    any_valid = make(td.analyzeCHOP, "any_valid")
    wire(any_valid, trail_off)
    any_valid.par.function = "maximum"

    prev = make(td.feedbackCHOP, "active_prev")
    prev.par.output = "previous"
    held_on = make(td.logicCHOP, "held_on")
    wire(held_on, prev, any_valid)
    held_on.par.chopop = "and"
    held_on.par.match = "name"
    active = make(td.logicCHOP, "active")
    wire(active, all_valid, held_on)
    active.par.chopop = "or"
    active.par.match = "name"
    # Input 1 is the template AND the initial value: starting released means a hand
    # already in shot when the project loads produces an `entering` pulse, which is
    # the honest reading - nothing knows it was there before.
    wire(prev, active, zero)

    not_prev = make(td.logicCHOP, "not_active_prev")
    wire(not_prev, prev)
    not_prev.par.preop = "invert"
    not_active = make(td.logicCHOP, "not_active")
    wire(not_active, active)
    not_active.par.preop = "invert"
    entering = make(td.logicCHOP, "entering")
    wire(entering, active, not_prev)
    entering.par.chopop = "and"
    entering.par.match = "name"
    exiting = make(td.logicCHOP, "exiting")
    wire(exiting, prev, not_active)
    exiting.par.chopop = "and"
    exiting.par.match = "name"

    # `held`: seconds active, reset to zero the moment it is not.
    #
    #     held = (held_prev + 1/rate) * active
    #
    # The multiply is the reset - no separate reset path, and no state that can be
    # left stale, because an inactive frame zeroes it unconditionally.
    held_prev = make(td.feedbackCHOP, "held_prev", ROW_Y - 150)
    held_prev.par.output = "previous"
    ticked = maths("held_tick", held_prev, y=ROW_Y - 150,
                   postoff_expr="1.0 / me.time.rate")
    held = maths("held", ticked, active, chopop="mul", y=ROW_Y - 150)
    wire(held_prev, held, zero)

    both_active = make(td.logicCHOP, "both_active", ROW_Y - 150)
    wire(both_active, active)
    both_active.par.chanop = "and"          # combine CHANNELS, not inputs
    both_active.par.renamefrom = "*"
    both_active.par.renameto = "both_active"
    n_active = maths("n_active", active, chanop="add", y=ROW_Y - 150,
                     renamefrom="*", renameto="n_active")

    out_active = select("out_active", active, ["h0_active", "h1_active"], y=ROW_Y)
    out_entering = select("out_entering", entering, ["h0_active", "h1_active"],
                          ["h0_entering", "h1_entering"], y=ROW_Y)
    out_exiting = select("out_exiting", exiting, ["h0_active", "h1_active"],
                         ["h0_exiting", "h1_exiting"], y=ROW_Y)
    out_held = select("out_held", held, ["h0_active", "h1_active"],
                      ["h0_held", "h1_held"], y=ROW_Y)

    # =====================================================================
    # Motion: velocity, speed, acceleration
    # =====================================================================
    # The palm rather than the wrist, because the palm is the mean of six joints
    # and does not swing as the hand rotates (docs/ATTRIBUTES.md).
    palm = select("palm", source, ["h0_palm_x", "h0_palm_y",
                                   "h1_palm_x", "h1_palm_y"], y=ROW_Y - 300)
    slope = derivative("slope", palm, ROW_Y - 300)
    # Averaged, not merely smoothed - see moving_average. Differentiating also
    # amplifies noise, and the positions are already one-euro filtered upstream, so
    # this is the second stage as well as the correction.
    velocity = moving_average("vel", slope, ROW_Y - 300)
    out_vel = select("out_vel", velocity,
                     ["h0_palm_x", "h0_palm_y", "h1_palm_x", "h1_palm_y"],
                     ["h0_vel_x", "h0_vel_y", "h1_vel_x", "h1_vel_y"], y=ROW_Y - 300)

    # speed = hypot(vel_x, vel_y). Both components are renamed to the SAME channel
    # name so the add pairs them, then `preop = root` is the square root (verified
    # live, 9 -> 3).
    vx = select("vx", velocity, ["h0_palm_x", "h1_palm_x"],
                ["h0_speed", "h1_speed"], y=ROW_Y - 450)
    vy = select("vy", velocity, ["h0_palm_y", "h1_palm_y"],
                ["h0_speed", "h1_speed"], y=ROW_Y - 450)
    vx2 = maths("vx2", vx, preop="square", y=ROW_Y - 450)
    vy2 = maths("vy2", vy, preop="square", y=ROW_Y - 450)
    sumsq = maths("sumsq", vx2, vy2, chopop="add", y=ROW_Y - 450)
    speed = maths("speed", sumsq, preop="root", y=ROW_Y - 450)

    accel_slope = derivative("accel_slope", speed, ROW_Y - 450)
    accel = select("accel", accel_slope, ["h0_speed", "h1_speed"],
                   ["h0_accel", "h1_accel"], y=ROW_Y - 450)

    # =====================================================================
    # Two hands: the closing rate
    # =====================================================================
    # `hands_approach` is the derivative of a distance, so it has no wrapping
    # problem and needs nothing special. `hands_twist` is the derivative of an
    # ANGLE and does - see the note at the end.
    approach_in = select("approach_in", source, ["hands_distance"], y=ROW_Y - 600)
    approach_slope = derivative("approach_slope", approach_in, ROW_Y - 600)
    approach = moving_average("approach", approach_slope, ROW_Y - 600)
    out_approach = select("out_approach", approach, ["hands_distance"],
                          ["hands_approach"], y=ROW_Y - 600)

    # =====================================================================
    # The clock, and the output
    # =====================================================================
    terminals = [out_active, out_entering, out_exiting, out_held, both_active,
                 n_active, out_vel, speed, accel, out_approach]
    merged = make(td.mergeCHOP, "out", ROW_Y - 750)
    wire(merged, *terminals)

    # Its own clock, for the reason in DESIGN.md 2.10: this branch has memory - two
    # Trails and three Feedbacks - and TouchDesigner will not cook it just because
    # merge_out exists downstream. Measured on the latch bank: terminal branches
    # cooked 4 times against merge_out's 420,613.
    clock = make(td.nullCHOP, "tick", ROW_Y - 750)
    wire(clock, merged)
    clock.par.cooktype = "always"

    print("   presence gate: validity%s" % (
        " AND liveness" if comp.op("lat_live") is not None
        else " ONLY (no lat_live - run tools/td_add_latches.py, then this again)"))
    print("3. built %d operators" % sum(1 for c in comp.children
                                        if c.name.startswith(PREFIX)))

    # -- verification -------------------------------------------------------
    failures = []
    expected = {
        "tmp_out_active": ("h0_active", "h1_active"),
        "tmp_out_entering": ("h0_entering", "h1_entering"),
        "tmp_out_exiting": ("h0_exiting", "h1_exiting"),
        "tmp_out_held": ("h0_held", "h1_held"),
        "tmp_both_active": ("both_active",),
        "tmp_n_active": ("n_active",),
        "tmp_out_vel": ("h0_vel_x", "h0_vel_y", "h1_vel_x", "h1_vel_y"),
        "tmp_speed": ("h0_speed", "h1_speed"),
        "tmp_accel": ("h0_accel", "h1_accel"),
        "tmp_out_approach": ("hands_approach",),
    }
    print("4. structure:")
    for node in terminals:
        node.cook(force=True)
        actual = tuple(c.name for c in node.chans())
        want = expected[node.name]
        ok = set(actual) == set(want) and len(actual) == len(want)
        if not ok:
            failures.append("%s: expected %r, got %r" % (node.name, want, actual))
        if node.errors():
            failures.append("%s: %s" % (node.name, node.errors()))
        print("   %s %-20s %d chans  %s" % ("ok " if ok else "BAD", node.name,
                                            len(actual), " ".join(sorted(actual))))

    # -- into the COMP's output -------------------------------------------
    merge_out = comp.op("merge_out")
    if merge_out is not None:
        # Rebuild the whole input list rather than appending: a Merge CHOP accepts
        # the same operator on any number of inputs silently, and connecting to a
        # connector that reports itself free can INSERT rather than fill. See
        # tools/td_add_latches.py.
        keep, seen = [], set()
        for existing in merge_out.inputs:
            if existing.name != merged.name and existing.name not in seen:
                keep.append(existing)
                seen.add(existing.name)
        for _attempt in range(256):
            if not merge_out.inputs:
                break
            merge_out.inputConnectors[0].disconnect()
        for index, node in enumerate([*keep, merged]):
            merge_out.inputConnectors[index].connect(node)
        merge_out.cook(force=True)
        names = [o.name for o in merge_out.inputs]
        if len(names) != len(set(names)):
            failures.append("merge_out has duplicate inputs: %r" % names)

    out_chop = comp.op("out1")
    out_chop.cook(force=True)
    print("5. COMP output: %d channels" % out_chop.numChans)
    for probe in ("h0_active", "h0_entering", "h0_held", "both_active", "n_active",
                  "h0_vel_x", "h0_speed", "h0_accel", "hands_approach"):
        channel = out_chop.chan(probe)
        print("   %-16s %s" % (probe, "MISSING" if channel is None
                               else round(channel[0], 4)))

    if failures:
        print("\nFAILURES (%d):" % len(failures))
        for failure in failures:
            print("   " + failure)
    else:
        print("\nstructure verified. Exercise it over TIME:")
        print("   send_synthetic.py swipe  --period 0.8 --cycles 20   (velocity)")
        print("   send_synthetic.py absent --cycles 1                 (exiting)")
        print("   send_synthetic.py open   --cycles 2                 (entering, held)")
    print("\nNOT YET BUILT, and each needs something this file does not have:")
    print("   h{i}_dir      atan2 of the velocity pair - no native operator does")
    print("                 atan2, so it needs an Expression CHOP per hand or a")
    print("                 small stateless Script CHOP")
    print("   hands_twist   the derivative of an ANGLE, which wraps at +/-180 and")
    print("                 spikes if differentiated naively. Publish sin/cos of")
    print("                 hands_angle from derive() and use")
    print("                 cos*d(sin) - sin*d(cos), which is wrap-free")
    print("   steadiness    Trail(Steadywindow) into Analyze, inverted")
    print("   dwell         Count gated on a radius test against a Hold")
    print("   hands_scale   needs a Hold latched by the Scalereset pulse")
    print("   swipe/wave/cross/twist events - thresholds plus a refractory Count")


main()
