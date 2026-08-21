#!/usr/bin/env python
"""Add the one-euro position filter to the visionhands COMP. Paste, Run Script.

    WHAT IT DOES. Smooths the 84 landmark positions - every `h{i}_{joint}_x` and
    `_y` - upstream of everything else, so every derived distance, angle, curl,
    velocity and bounding box inherits the smoothing from one place. Everything
    that is not a position passes through untouched.

    ON THE FILTER PAGE
        Smoothing   toggle    off is a bit-exact passthrough
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

Ref: DESIGN.md 2.11 (the time-slicing rule), docs/ATTRIBUTES.md (the smoothing
section), docs/BUILD_PLAN.md step 7 (the cook-time profile this came out of).
"""

import sys

REPO_ROOT = "/Users/omer/Documents/GitHub/visionhands-touchdesigner"
COMP_PATH = "/project1/visionhands"
PREFIX = "oef_"

# Filter-page defaults. Both GUESSED - see the note on beta.
#
# Mincutoff 1.5 Hz: a hand held still should stop shimmering, and 1.5 Hz does that
# without visibly lagging a deliberate slow move.
# Beta 2.0: scaled for normalised units, where hand speeds run about 0.1 to 3.0
# units per second, so this lifts the cutoff by a few Hz on a fast move.
FILTER_DEFAULTS = {
    "Mincutoff": 1.5,
    "Beta": 2.0,
}

ROW_Y = -2100


def _page(comp, name="Filter"):
    """The Filter page, created once, never overwriting a tuned value.

    Traps: `appendCustomPage` with an existing name makes a SECOND page of that
           name, so look it up first. And `appendFloat` on an existing parameter
           resets its value to ZERO - not to its default - so tuned values are read
           before the append and written back after. Both cost time elsewhere in
           this repo; see tools/td_add_latches.py.
    """
    page = None
    for existing in comp.customPages:
        if existing.name == name:
            page = existing
            break
    if page is None:
        page = comp.appendCustomPage(name)

    tuned = {p.name: p.eval() for p in comp.customPars if p.name in FILTER_DEFAULTS}
    toggle_was = next((p.eval() for p in comp.customPars
                       if p.name == "Smoothing"), None)

    toggle = page.appendToggle("Smoothing", label="Smoothing")[0]
    toggle.default = True
    toggle.val = True if toggle_was is None else toggle_was

    for parameter, default in sorted(FILTER_DEFAULTS.items()):
        par = page.appendFloat(parameter, label=parameter)[0]
        par.default = default
        par.normMin, par.normMax = 0.0, 10.0
        previous = tuned.get(parameter)
        # A cutoff of zero is not a heavier filter, it is a filter that never
        # updates. Treated as untuned so the COMP cannot be left in that state.
        par.val = previous if previous and previous > 0.0 else default
    return page


def main():
    import td

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    # TouchDesigner caches our modules for the life of the process, so an edited
    # contract would otherwise be invisible. DESIGN.md 2.11.
    for stale in [n for n in list(sys.modules)
                  if n == "visionhands" or n.startswith("visionhands.")]:
        del sys.modules[stale]
    from visionhands.types import channel_names

    comp = op(COMP_PATH)
    if comp is None:
        print("FAIL no COMP at %s - run tools/td_build_comp.py first" % COMP_PATH)
        return
    source = comp.op("in1")
    if source is None:
        print("FAIL no in1 in %s" % COMP_PATH)
        return

    # The split is computed from the CONTRACT, not from a wildcard pattern. A
    # pattern would silently reclassify a channel the moment the contract gained
    # one whose name happened to end in _x, and the two halves have to partition
    # exactly - anything dropped from both vanishes from the COMP's output.
    names = channel_names()
    positions = [n for n in names if n.endswith(("_x", "_y"))]
    others = [n for n in names if not n.endswith(("_x", "_y"))]
    assert len(positions) + len(others) == len(names)

    removed = 0
    for child in list(comp.children):
        if child.name.startswith(PREFIX):
            child.destroy()
            removed += 1
    print("1. cleared %d existing %s* operators" % (removed, PREFIX))

    _page(comp)
    print("2. Filter page: Smoothing=%s, %s" % (
        bool(comp.par.Smoothing.eval()),
        ", ".join("%s=%.3f" % (n, float(getattr(comp.par, n).eval()))
                  for n in sorted(FILTER_DEFAULTS))))

    def make(kind, name, x, y=ROW_Y):
        node = comp.create(kind, PREFIX + name)
        node.nodeX, node.nodeY = x, y
        return node

    # -- the input, split in two -------------------------------------------
    raw = make(td.selectCHOP, "in", -1400)
    raw.inputConnectors[0].connect(source)
    raw.par.channames = " ".join(positions)
    rest = make(td.selectCHOP, "rest", -1400, ROW_Y - 150)
    rest.inputConnectors[0].connect(source)
    rest.par.channames = " ".join(others)

    # -- the filter, which is one operator ---------------------------------
    smoothed = make(td.filterCHOP, "smooth", -1200)
    smoothed.inputConnectors[0].connect(raw)
    smoothed.par.type = "oneeuro"
    # Time-sliced, which is the whole reason this works: the filter needs real
    # slices to carry state across, and `raw` inherits time-slicing from the OSC
    # In CHOP. On a non-time-sliced one-sample CHOP it is a silent passthrough.
    smoothed.par.timeslice = True
    smoothed.par.cutoff.expr = "parent().par.Mincutoff"
    smoothed.par.speedcoeff.expr = "parent().par.Beta"

    # -- the toggle, and back to 137 channels ------------------------------
    # A Switch rather than bypassing: with Smoothing off, index 0 sends the RAW
    # channels through bit-exact, and the filter's own state keeps advancing so
    # re-enabling it has no settling transient. It costs 0.015 ms to leave running,
    # which is not worth a transient to save.
    switch = make(td.switchCHOP, "switch", -1000)
    switch.inputConnectors[0].connect(raw)
    switch.inputConnectors[1].connect(smoothed)
    switch.par.index.expr = "1 if parent().par.Smoothing else 0"

    out = make(td.mergeCHOP, "out", -800)
    out.inputConnectors[0].connect(switch)
    out.inputConnectors[1].connect(rest)

    print("3. built %d operators (was 21 by hand - see the docstring)"
          % sum(1 for c in comp.children if c.name.startswith(PREFIX)))

    # -- verification -------------------------------------------------------
    failures = []
    out.cook(force=True)
    produced = tuple(c.name for c in out.chans())
    if set(produced) != set(names) or len(produced) != len(names):
        missing = sorted(set(names) - set(produced))
        extra = sorted(set(produced) - set(names))
        failures.append("oef_out does not reproduce the contract: %d channels, "
                        "missing %r, extra %r" % (len(produced), missing[:5],
                                                  extra[:5]))
    for node in (raw, rest, smoothed, switch, out):
        if node.errors():
            failures.append("%s: %s" % (node.name, node.errors()))
    # The filter must actually be filtering, not silently passing through - which
    # is precisely the failure the hand-built version was built to work around.
    if not smoothed.isTimeSlice:
        failures.append("oef_smooth is not time-sliced, so it will be a silent "
                        "passthrough - see the docstring")
    print("4. oef_out: %d channels (contract has %d), filter time-sliced: %s"
          % (len(produced), len(names), smoothed.isTimeSlice))

    # -- repoint everything that read in1 ----------------------------------
    # `lat_seq` deliberately stays on the RAW input: it watches `seq` to decide
    # whether anything is still arriving, and liveness should not depend on a
    # filter chain that could itself be misconfigured.
    moved = []
    for consumer in list(source.outputs):
        if consumer.name.startswith(PREFIX) or consumer.name == "lat_seq":
            continue
        for connector in consumer.inputConnectors:
            for connection in list(connector.connections):
                if connection.owner is source:
                    connector.connect(out)
                    moved.append(consumer.name)
    print("5. repointed to oef_out: %s" % ", ".join(sorted(set(moved))))
    print("   (lat_seq stays on the raw input, so liveness cannot depend on the "
          "filter)")

    comp.op("out1").cook(force=True)
    print("6. COMP output: %d channels" % comp.op("out1").numChans)
    print("   filter cost: %.4f ms" % smoothed.cookTime)

    if failures:
        print("\nFAILURES (%d):" % len(failures))
        for failure in failures:
            print("   " + failure)
    else:
        print("\nstructure verified. A filter cannot be judged from one frame:")
        print("   send_synthetic.py open  - a still hand: filtered == raw exactly")
        print("   send_synthetic.py swipe - a moving one: filtered lags")
        print("   then toggle Smoothing and compare")


main()
