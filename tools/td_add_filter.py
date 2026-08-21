#!/usr/bin/env python
"""Add a one-euro filter to the visionhands COMP. Paste into a Text DAT, Run Script.

    WHAT IT DOES. Smooths the 84 landmark positions - every `h{i}_{joint}_x` and
    `_y` - upstream of everything else, so every derived distance, angle, curl,
    velocity and bounding box inherits the smoothing from one place. Everything
    that is not a position passes through untouched.

    ON THE FILTER PAGE
        Smoothing   toggle    off = a bit-exact passthrough, not a filter set to 0
        Mincutoff   Hz        how heavily a SLOW-moving hand is smoothed
        Beta        1/units   how hard a FAST-moving hand is allowed through
        Dcutoff     Hz        smoothing on the speed estimate itself

WHY A ONE-EURO FILTER and not a Lag or a Filter CHOP. Landmark jitter and hand
motion live in the same frequency band, so any fixed-cutoff filter has to choose:
smooth enough to kill the jitter of a still hand, and a fast hand lags visibly;
responsive enough to track a fast hand, and a still hand shimmers. The one-euro
filter makes the cutoff a function of the estimated speed - heavy smoothing when
slow, almost none when fast - which is exactly the trade that cannot be made with
a constant.

    dx      = (x - x_prev) * rate
    dx_hat  = exp_smooth(dx, alpha(Dcutoff))
    cutoff  = Mincutoff + Beta * |dx_hat|
    x_hat   = x_hat_prev + alpha(cutoff) * (x - x_hat_prev)
    alpha(c) = 2*pi*c / (2*pi*c + rate)

BUILT FROM NATIVE CHOPS, no Python in the cook path, for the reason
docs/ATTRIBUTES.md gives: this has memory, and memory belongs where TouchDesigner
manages it and where the network shows what it is doing. That looks unpromising at
first, because the whole point is a coefficient that varies per channel per frame
and no filter CHOP offers that. It falls out of two things: exponential smoothing
IS `prev + alpha * (x - prev)`, which is a Feedback and three Math CHOPs; and the
Math CHOP's `preop` menu turns out to carry a unary toolkit - `pos` is absolute
value, `inverse` is 1/x (and 1/0 = 0, verified, so there is no division to guard).
Twenty operators, all channel-wise, all matched by name.

**ABOUT BETA, because the published default is wrong for these units.** Every
one-euro reference sets `beta` around 0.007, and every one of them is filtering
PIXELS, where speeds run to hundreds or thousands of units per second. These
channels are normalised 0..1, so the same motion measures about a thousand times
smaller - and `cutoff = 1.0 + 0.007 * 2.0` is no adaptation at all. Copying the
published constant gives a filter that behaves exactly like a fixed one and invites
the conclusion that one-euro does not help. The default here is scaled for
normalised units, and it is a GUESS: measuring per-joint jitter is what should set
it (DESIGN.md 11).

Ref: docs/ATTRIBUTES.md (why memory is native), DESIGN.md 2.11 (the operator
behaviours this leans on), 2.10 (why it needs a clock).
"""

import math
import sys

REPO_ROOT = "/Users/omer/Documents/GitHub/visionhands-touchdesigner"
COMP_PATH = "/project1/visionhands"
PREFIX = "oef_"

# Defaults for the Filter page. All three are GUESSED - see the note on beta.
#
# Mincutoff 1.5 Hz: a hand held still should stop shimmering, and 1.5 Hz is slow
# enough to do that without visibly lagging a deliberate slow move.
# Beta 2.0: scaled for normalised units, where hand speeds run about 0.1 to 3.0
# units per second, so this lifts the cutoff by a few Hz on a fast move - enough
# to get out of the way.
# Dcutoff 1.0 Hz: smoothing on the speed estimate. Left at the published value,
# because it filters a DERIVATIVE whose scale does not enter the comparison.
FILTER_DEFAULTS = {
    "Mincutoff": 1.5,
    "Beta": 2.0,
    "Dcutoff": 1.0,
}

# The cook rate the recurrence is sampled at, as an expression. This is TD's rate
# rather than the camera's: the filter advances once per cook, so the cook rate is
# what its own maths must use. `me.time.rate` reads the local timeline.
RATE_EXPR = "me.time.rate"
TWO_PI = 2.0 * math.pi

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
    toggle_was = None
    for p in comp.customPars:
        if p.name == "Smoothing":
            toggle_was = p.eval()

    toggle = page.appendToggle("Smoothing", label="Smoothing")[0]
    toggle.default = True
    toggle.val = True if toggle_was is None else toggle_was

    for parameter, default in (("Mincutoff", FILTER_DEFAULTS["Mincutoff"]),
                               ("Beta", FILTER_DEFAULTS["Beta"]),
                               ("Dcutoff", FILTER_DEFAULTS["Dcutoff"])):
        par = page.appendFloat(parameter, label=parameter)[0]
        par.default = default
        par.normMin, par.normMax = 0.0, 10.0
        # A cutoff of zero is not a heavier filter, it is a filter that never
        # updates: alpha = 0, so x_hat stays wherever it started. Treated as
        # untuned so a COMP cannot be left in that state.
        previous = tuned.get(parameter)
        par.val = previous if previous and previous > 0.0 else default
    return page


def main():
    import td

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    # TouchDesigner caches our modules across runs of this script, so an edited
    # contract would otherwise be invisible. See tools/td_add_latches.py.
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
    # exactly - anything dropped from both is a channel that vanishes from the
    # COMP's output.
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
            # Never the `index` default: these all carry the same 84 names, and
            # pairing them by position would be relying on luck (DESIGN.md 2.11).
            node.par.match = "name"
        return node

    # -- the input, split in two -------------------------------------------
    raw = make(td.selectCHOP, "in")
    wire(raw, source)
    raw.par.channames = " ".join(positions)
    rest = make(td.selectCHOP, "rest", ROW_Y - 150)
    wire(rest, source)
    rest.par.channames = " ".join(others)

    # An all-zero CHOP carrying the same 84 names, as the template and initial
    # value for every Feedback below. A Math with gain 0 rather than a Constant
    # with 84 hand-written channels: it cannot drift out of step with the contract.
    zero = maths("zero", raw, gain=0.0)

    # -- dx: how fast is each coordinate moving -----------------------------
    x_prev = make(td.feedbackCHOP, "x_prev")
    x_prev.par.output = "previous"
    wire(x_prev, raw, zero)          # input 1 is the template AND the initial value
    dx = maths("dx", raw, x_prev, chopop="sub")
    dx = maths("dx_rate", dx, gain_expr=RATE_EXPR)

    # -- dx_hat: exponential smoothing of dx, with a CONSTANT coefficient ---
    # alpha(Dcutoff) is a scalar, so it can ride on the gain parameter rather
    # than needing a channel of its own.
    alpha_d = "(%r * parent().par.Dcutoff) / (%r * parent().par.Dcutoff + %s)" % (
        TWO_PI, TWO_PI, RATE_EXPR)
    dh_prev = make(td.feedbackCHOP, "dh_prev")
    dh_prev.par.output = "previous"
    dd = maths("dd", dx, dh_prev, chopop="sub")
    dds = maths("dds", dd, gain_expr=alpha_d)
    dx_hat = maths("dx_hat", dh_prev, dds, chopop="add")
    wire(dh_prev, dx_hat, zero)

    # -- the adaptive cutoff, and alpha from it -----------------------------
    # `preop = pos` is absolute value (verified live). |dx_hat| is what makes the
    # cutoff rise for motion in either direction.
    speed = maths("speed", dx_hat, preop="pos", y=ROW_Y - 150)
    cutoff = maths("cutoff", speed, y=ROW_Y - 150,
                   gain_expr="parent().par.Beta",
                   postoff_expr="parent().par.Mincutoff")
    tc = maths("tc", cutoff, gain=TWO_PI, y=ROW_Y - 150)
    den = maths("den", tc, postoff_expr=RATE_EXPR, y=ROW_Y - 150)
    alpha = maths("alpha", tc, den, chopop="div", y=ROW_Y - 150)

    # -- and the filter itself ---------------------------------------------
    h_prev = make(td.feedbackCHOP, "h_prev", ROW_Y - 300)
    h_prev.par.output = "previous"
    diff = maths("diff", raw, h_prev, chopop="sub", y=ROW_Y - 300)
    step = maths("step", diff, alpha, chopop="mul", y=ROW_Y - 300)
    x_hat = maths("x_hat", h_prev, step, chopop="add", y=ROW_Y - 300)
    wire(h_prev, x_hat, zero)

    # -- the toggle, and back to 137 channels ------------------------------
    # A Switch rather than bypassing the filter: with Smoothing off, index 0 sends
    # the RAW channels through, bit-exact. Bypassing the last Math would leave the
    # feedback chain running and the toggle would still have a settling transient.
    switch = make(td.switchCHOP, "switch", ROW_Y - 300)
    wire(switch, raw, x_hat)
    switch.par.index.expr = "1 if parent().par.Smoothing else 0"

    out = make(td.mergeCHOP, "out", ROW_Y - 300)
    wire(out, switch, rest)

    print("3. built %d operators" % sum(1 for c in comp.children
                                        if c.name.startswith(PREFIX)))

    # -- verification -------------------------------------------------------
    failures = []
    out.cook(force=True)
    produced = tuple(c.name for c in out.chans())
    if set(produced) != set(names):
        missing = sorted(set(names) - set(produced))
        extra = sorted(set(produced) - set(names))
        failures.append("oef_out does not reproduce the contract: missing %r, "
                        "extra %r" % (missing[:5], extra[:5]))
    if len(produced) != len(names):
        failures.append("oef_out has %d channels, the contract has %d"
                        % (len(produced), len(names)))
    for node in (raw, zero, dx, dx_hat, speed, cutoff, alpha, x_hat, switch, out):
        if node.errors():
            failures.append("%s: %s" % (node.name, node.errors()))
    print("4. oef_out: %d channels (contract has %d)" % (len(produced), len(names)))

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

    if failures:
        print("\nFAILURES (%d):" % len(failures))
        for failure in failures:
            print("   " + failure)
    else:
        print("\nstructure verified. A filter cannot be judged from one frame:")
        print("   tools/send_synthetic.py open   - a still hand: filtered == raw")
        print("   tools/send_synthetic.py ramp   - a moving one: filtered lags")
        print("   then toggle Smoothing and compare")


main()
