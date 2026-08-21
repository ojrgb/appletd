"""Threshold defaults — one table, imported by everything that needs them.

WHY THIS FILE EXISTS. These numbers were written down in three places: the
builder that creates the TouchDesigner parameters, the tests that assert the
generated sweeps straddle them, and prose in docs/ATTRIBUTES.md. Nothing tied
them together, and the failure that makes is silent in the worst way.

Concretely, the failure the review found: raise `Pinchon` from 0.35 to 0.45 in the
builder alone. Every Python test still passes, because they compare against 0.35.
TouchDesigner still reports `deadband +1`, which is the expected answer. But the
dead-band sweep's crest sits at 0.44, which is now BELOW the engage threshold - so
the distance never leaves the engaged region, the latch never has to hold
anything, and the test that exists to prove hysteresis has quietly stopped
testing it while continuing to report success.

So the thresholds live here, and `visionhands/tests/test_sequences.py` asserts the
relationship that actually matters: that each sweep's crest lands strictly INSIDE
its dead band. Change a number here and the test either still holds or fails
loudly; it can no longer pass vacuously.

Ref: docs/ATTRIBUTES.md (the Tuning page, which this mirrors and which remains
the prose specification), tools/td_add_latches.py (creates the TD parameters from
this), visionhands/sequences.py (the sweeps aimed at these).
"""

from __future__ import annotations

from typing import Final

# Every `off` is looser than its `on`. That gap IS the hysteresis and it IS the
# re-arm rule - a pair set equal has neither and will chatter - so the invariant
# is asserted at import rather than left to a reader's care.
#
# Distances here are ratios of hand size, never raw normalised distances: that is
# what makes a pinch tuned at arm's length still work when the hand comes closer
# (docs/ATTRIBUTES.md). `Grab` is the exception and says so at its own entry.
LATCH_THRESHOLDS: Final[dict[str, tuple[float, float]]] = {
    # name       (on,   off)
    "Pinch": (0.35, 0.50),
    "Snap": (0.25, 0.45),
    "Together": (0.60, 0.95),
    # One pair shared by all eight finger triggers - index, middle, ring and
    # little, on both hands. Not one pair per finger: that would be sixteen
    # sliders for a grid whose whole point is uniformity, and the resting distance
    # differing per finger changes how far the thumb has to travel, not whether
    # the contact fires.
    #
    # Deliberately a little looser than `Pinch`, because the ring and little
    # fingers meet the thumb less squarely than the index does and a threshold
    # tuned on the index tends to make them feel dead. GUESSED - this is one of
    # the numbers the threshold-feel session exists to settle.
    "Trigger": (0.40, 0.55),
    # Grab is the ONE pair here that is not a ratio of hand size: `openness` is
    # already a 0..1 quantity - the mean of the four long-finger curls, inverted -
    # so these are absolute positions on that scale. Below `on` is a closed hand.
    # Called out because every other pair in this table is a ratio, and reading
    # 0.30 as "a third of a hand-size" would be wrong here.
    "Grab": (0.30, 0.55),
}

# Flattened to the parameter names TouchDesigner carries, since that is the shape
# the builder wants: {"Pinchon": 0.35, "Pinchoff": 0.50, ...}
THRESHOLD_DEFAULTS: Final[dict[str, float]] = {
    prefix + suffix: value
    for prefix, pair in LATCH_THRESHOLDS.items()
    for suffix, value in (("on", pair[0]), ("off", pair[1]))
}


def _self_check() -> None:
    """Run at import. A build with no dead band must not be constructible.

    Import-time rather than in a test: the builder imports this module inside
    TouchDesigner, where the test suite is not going to run, and a threshold pair
    in the wrong order there produces a latch that chatters rather than an error.
    """
    for name, (on, off) in LATCH_THRESHOLDS.items():
        if not 0.0 < on < off:
            raise ValueError(
                "%s thresholds must satisfy 0 < on < off, got on=%r off=%r - "
                "no dead band means no hysteresis and no re-arm rule"
                % (name, on, off))


_self_check()
