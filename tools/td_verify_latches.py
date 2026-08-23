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
`appletd/tests/test_sequences.py`, against a reference recurrence written
straight from docs/ATTRIBUTES.md. That file says what should happen; this one
says what TouchDesigner did.

Ref: docs/ATTRIBUTES.md, tools/td_add_latches.py (the network under test),
appletd/sequences.py (the sweeps).
"""

# The hands STREAM, inside the master COMP - the latch channels live on its
# output, not on the master's (which exposes each stream on its own connector).
COMP_PATH = "/project1/appletd/hands"

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

# The matching release counters. Without these NOTHING measured the falling
# edges: every sweep passed with `h{i}_e_pinch_end`, `h{i}_e_snap_end` and
# `e_apart` structurally present and behaviourally untested. Paired with the
# counters above they give a standing invariant, checked on every run:
#
#     fires - releases == 1 if the gesture is engaged right now, else 0
#
# which catches a missing end pulse, a doubled one, and a missing start.
END_COUNTERS = (
    ("h0_pinch_count", "h0_pinch_end_count", "h0_pinching"),
    ("h1_pinch_count", "h1_pinch_end_count", "h1_pinching"),
    ("h0_snap_count", "h0_snap_end_count", "h0_snapping"),
    ("h1_snap_count", "h1_snap_end_count", "h1_snapping"),
    ("clap_count", "apart_count", "hands_together"),
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


# Operators read for health rather than for a count. `lat_state` must be cooking
# on its own or every measurement below is meaningless; `lat_live` says whether
# anything is sending at all, which is the difference between "the gate correctly
# refused" and "nothing arrived".
# `lat_end` is here because it is the one that was silently dead: the clock pulled
# only the start counter, so `lat_end` had cooked 4 times against `lat_start`'s
# 4,845 and every release counter sat at zero while the fire counters climbed. Any
# terminal branch of the bank that cooks less often than the rest is that bug
# again, so the two are compared on every run rather than trusted.
HEALTH_OPS = ("lat_state", "lat_start", "lat_end", "lat_live", "merge_out")


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
    wanted = [n for n, _d in COUNTERS] + [e for _f, e, _s in END_COUNTERS]
    for name in wanted:
        channel = out.chan(name)
        # `is not None`, never truthiness: a Channel's truth value is its VALUE,
        # so a counter sitting at 0 would read as missing.
        values[name] = None if channel is None else round(channel[0])
    return values


# Read for context in the printed summary. The channels the INVARIANT needs are
# not listed here: they are derived from END_COUNTERS below, because a
# hand-maintained list of them drifted and the tool raised KeyError on
# `h1_pinching` instead of checking the second hand's invariant at all - so the
# h1 rows had been structurally present and never once evaluated.
CONTEXT_CHANNELS = ("h0_valid", "h1_valid", "both_valid", "both_pinching",
                    "h0_pinch_index", "h0_pinch_middle", "hands_distance")


def _states(comp):
    """The latch states and validity right now - context for the counts.

    Covers CONTEXT_CHANNELS plus every state channel the standing invariant
    checks, so the two cannot come apart again.
    """
    out = comp.op("out1")
    result = {}
    wanted = list(CONTEXT_CHANNELS) + [s for _f, _e, s in END_COUNTERS]
    for name in wanted:
        channel = out.chan(name)
        # `is not None`, never truthiness: a Channel's truth value is its VALUE, so
        # a state sitting at 0 would read as missing.
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
        # Out of the way, from the one layout table. It used to land wherever
        # `create` put it, which was in the middle of the attribute layer.
        from appletd.td_layout import stream_xy
        store.nodeX, store.nodeY = stream_xy(BASELINE_DAT)
        store.nodeX, store.nodeY = 200, -1800
        store.text = ""

    previous = {}
    for line in store.text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            if key.strip() == "_lat_state_cooks":
                stored_cooks = int(value)
            else:
                previous[key.strip()] = int(value)

    # Health first, because a stuck clock makes every number below a lie.
    health = {}
    for name in HEALTH_OPS:
        node = comp.op(name)
        health[name] = None if node is None else node.totalCooks
    live_op = comp.op("lat_live")
    live = None if live_op is None else live_op.chan("seq")[0]
    stored_cooks = None

    state = _states(comp)
    print("now: h0_valid=%.0f both_valid=%.0f | pinch_index=%.3f pinch_middle=%.3f "
          "hands_distance=%.3f" % (state["h0_valid"], state["both_valid"],
                                   state["h0_pinch_index"], state["h0_pinch_middle"],
                                   state["hands_distance"]))
    print("     h0_pinching=%.0f h0_snapping=%.0f hands_together=%.0f "
          "both_pinching=%.0f | h1_pinching=%.0f h1_snapping=%.0f"
          % (state["h0_pinching"], state["h0_snapping"], state["hands_together"],
             state["both_pinching"], state["h1_pinching"], state["h1_snapping"]))
    print("     lat_live=%s (0 = nothing has sent recently, so every latch is "
          "forced released)" % ("?" if live is None else int(live)))
    starts, ends = health.get("lat_start"), health.get("lat_end")
    if starts and ends:
        # Equal, not merely both non-zero: they are the same depth in the same
        # bank and must be pulled by the same clock.
        gap = abs(starts - ends)
        print("     lat_start cooks %d, lat_end cooks %d: %s"
              % (starts, ends, "ok" if gap <= 2 else
                 "MISMATCH - a terminal branch is not being clocked"))
    if stored_cooks is not None and health.get("lat_state") is not None:
        advanced = health["lat_state"] - stored_cooks
        verdict = "ok" if advanced > 0 else "STUCK - nothing below is meaningful"
        print("     lat_state cooked %+d times since the last run: %s"
              % (advanced, verdict))
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
        print("standing invariant  fires - releases  should equal  engaged now")
        broken = []
        for fire, release, state_channel in END_COUNTERS:
            balance = counts[fire] - counts[release]
            engaged = state[state_channel]
            engaged_now = 0 if engaged is None else round(engaged)
            mark = "ok " if balance == engaged_now else "BAD"
            if balance != engaged_now:
                broken.append("%s: %d fires - %d releases = %d but the state is %d"
                              % (fire, counts[fire], counts[release], balance,
                                 engaged_now))
            print("   %s %-18s %8d %8d %11d" % (mark, fire, counts[fire],
                                                counts[release], engaged_now))
        if broken:
            print("")
            print("BROKEN INVARIANT - an edge pulse is missing or doubled:")
            for problem in broken:
                print("   " + problem)
        print("")
        print("expected, per sweep sent:")
        for sweep in sorted(EXPECTED):
            print("   %-9s %s" % (sweep, EXPECTED[sweep]))

    lines = ["%s = %d" % (name, value) for name, value in sorted(counts.items())]
    if health.get("lat_state") is not None:
        lines.append("_lat_state_cooks = %d" % health["lat_state"])
    store.text = "\n".join(lines)


main()
