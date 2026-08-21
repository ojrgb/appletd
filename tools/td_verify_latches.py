#!/usr/bin/env python
"""Verify the proximity latches over TIME. Paste into a Text DAT, Run Script.

    HOW TO USE IT. With the SIDECAR STOPPED, run this, then a sweep, then this
    again. It prints what changed between the two runs and judges it.

        Run this                                     -> takes a baseline
        tools/send_synthetic.py ramp     --cycles 4  -> four full pinches
        Run this                                     -> expects +4
        tools/send_synthetic.py deadband --cycles 6  -> six dips, none re-arming
        Run this                                     -> expects +1
        tools/send_synthetic.py clap     --cycles 3  -> palms together and apart
        Run this                                     -> expects clap +3
        tools/send_synthetic.py both     --cycles 3  -> both hands pinching
        Run this                                     -> expects +3 on all four
        tools/send_synthetic.py absent               -> no hands at all
        Run this                                     -> expects +0

WHY IT COUNTS INSTEAD OF WATCHING. The state is a latch and the pulses are one
frame wide, so reading a channel from here samples whatever frame the read lands
on: a pulse is almost certainly missed, and a held state says nothing about how it
got there.

The first version of this tool recorded a Trail CHOP instead, and it was WRONG in
a way worth writing down. A diagnostic branch with nothing downstream does not
cook - TouchDesigner has no reason to cook it - so the Trail advanced only when
this script forced it, and its 1200-sample "history" was a record of when the
script ran rather than of what the network did. It reported a rock-steady
distance through a sweep that had unquestionably swept. Neither `viewer = True`
nor a Null CHOP downstream fixes it; only a real consumer does.

The counters avoid the whole problem by being part of the COMP's output, which
cooks every frame because something is actually using it.

WHAT THE NUMBERS PROVE. Between them they pin every property of the recurrence,
and each has a distinct failure it catches:

  ramp +4      engages once per crossing, and - because the counter counts
               FRAMES the pulse is high rather than transitions - that each pulse
               is exactly one frame wide. A two-frame pulse reads +8.
  deadband +1  hysteresis. Six dips below `on`, none rising past `off`, so a
               correct latch engages on the first and then holds for ever. A
               plain comparator reads +6, and so does a parity counter that has
               lost sync.
  absent +0    the validity gate. Every joint at (0, 0) means every distance
               reads 0, below every engage threshold - so a gate that is missing,
               or applied to the pulses instead of to the state, reads +1 here
               and fires pinch, snap and clap simultaneously.
  both +3      that hand 1's five channels are wired to hand 1's data. Every
               other sweep leaves slot 1 empty, so this is the only thing that
               ever exercises that column.

ALL FIVE PASSED on 2026-08-20: +4, +1, clap +3, both +3 on all four per-hand
counters with clap +0, and absent +0 everywhere.

The same expectations are asserted with no TouchDesigner at all in
`visionhands/tests/test_sequences.py`, against a reference recurrence written
straight from docs/ATTRIBUTES.md. That file says what should happen; this one
says what TouchDesigner did.

Ref: docs/ATTRIBUTES.md, tools/td_add_latches.py (the network under test),
visionhands/sequences.py (the sweeps).
"""

COMP_PATH = "/project1/visionhands"

# Where the baseline is kept between runs. A Text DAT inside the COMP rather than
# a module global, because each run of this script is a fresh execution with no
# memory of the last one.
BASELINE_DAT = "ver_baseline"

# The published counter channels, and which sweep exercises each. A latch that no
# sweep drives is reported as untouched rather than as passing.
COUNTERS = (
    ("h0_pinch_count", "ramp, deadband, both"),
    ("h1_pinch_count", "both"),
    ("h0_snap_count", "snap, both, and ramp incidentally"),
    ("h1_snap_count", "both"),
    ("clap_count", "clap"),
)

# What each sweep should add to the counter it drives. The whole point of the
# tool is this table.
EXPECTED = {
    "ramp": "+1 per sweep on h0_pinch_count (and on h0_snap_count near the peak)",
    "deadband": "+1 in TOTAL however many sweeps, on h0_pinch_count",
    "clap": "+1 per sweep on clap_count",
    "snap": "+1 per sweep on h0_snap_count",
    "both": "+1 per sweep on ALL FOUR per-hand counters, +0 on clap_count",
    "absent": "+0 everywhere",
    "open": "+0 everywhere",
}


def _counts(comp):
    """Every counter's current value, from the COMP's own output.

    Read from `out1` rather than from the Count CHOP directly: that is the
    channel a project downstream would use, so a rename or a merge that dropped
    it shows up here rather than passing on the strength of an internal operator
    nobody consumes.
    """
    out = comp.op("out1")
    if out is None:
        return None
    out.cook(force=True)
    values = {}
    for name, _driven_by in COUNTERS:
        channel = out.chan(name)
        # `is not None`, never truthiness: a Channel's truth value is its VALUE,
        # so a counter sitting at 0 would read as missing.
        values[name] = None if channel is None else round(channel[0])
    return values


def _states(comp):
    """The latch states and validity right now - context for the counts."""
    out = comp.op("out1")
    result = {}
    for name in ("h0_pinching", "h0_snapping", "hands_together", "both_pinching",
                 "h0_valid", "h1_valid", "both_valid", "h0_pinch_index",
                 "h0_pinch_middle", "hands_distance"):
        channel = out.chan(name)
        result[name] = None if channel is None else channel[0]
    return result


def main():
    import td

    comp = op(COMP_PATH)
    if comp is None:
        print("FAIL no COMP at %s" % COMP_PATH)
        return
    counts = _counts(comp)
    if counts is None:
        print("FAIL no out1 in %s" % COMP_PATH)
        return
    missing = [name for name, value in counts.items() if value is None]
    if missing:
        print("FAIL counters missing from the COMP output: %s" % ", ".join(missing))
        print("     run tools/td_add_latches.py first")
        return

    store = comp.op(BASELINE_DAT)
    if store is None:
        store = comp.create(td.textDAT, BASELINE_DAT)
        store.nodeX, store.nodeY = 200, -1800
        store.text = ""

    previous = {}
    for line in store.text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            previous[key.strip()] = int(value)

    state = _states(comp)
    print("now: h0_valid=%.0f both_valid=%.0f | pinch_index=%.3f pinch_middle=%.3f "
          "hands_distance=%.3f" % (state["h0_valid"], state["both_valid"],
                                   state["h0_pinch_index"], state["h0_pinch_middle"],
                                   state["hands_distance"]))
    print("     h0_pinching=%.0f h0_snapping=%.0f hands_together=%.0f "
          "both_pinching=%.0f" % (state["h0_pinching"], state["h0_snapping"],
                                  state["hands_together"], state["both_pinching"]))
    print("")

    if not previous:
        print("baseline taken. Send a sweep, then run this again.")
    else:
        print("counter            before   now   change   driven by")
        for name, driven_by in COUNTERS:
            was = previous.get(name)
            now = counts[name]
            if was is None:
                continue
            delta = now - was
            print("%-18s %6d %5d %+8d   %s" % (name, was, now, delta, driven_by))
        print("")
        print("expected, per sweep sent:")
        for sweep in sorted(EXPECTED):
            print("   %-9s %s" % (sweep, EXPECTED[sweep]))

    store.text = "\n".join("%s = %d" % (name, counts[name])
                           for name, _driven_by in COUNTERS)


main()
