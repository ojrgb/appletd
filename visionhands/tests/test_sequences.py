"""The sequences are the instrument that verifies the latches, so test the
instrument.

The load-bearing tests here are not "does the triangle peak at 1". They are:

  * does the swept channel actually cross BOTH thresholds, with frames to spare
    on either side - because a sweep that stops short of `off` never releases,
    and would make a broken latch look like a working one;
  * does a correct Schmitt trigger, run over this sweep, engage exactly once per
    cycle - because that count is the number the TouchDesigner network is
    checked against, and if the sequence cannot produce a clean count then
    neither can the network.

The reference recurrence is implemented here, in eight lines, straight from
docs/ATTRIBUTES.md. That is deliberate: the native CHOP network is the thing
under test, and a test that shares its implementation tests nothing.
"""

from __future__ import annotations

import math

import pytest

from visionhands.derive import Params, derive
from visionhands.sequences import SEQUENCES, frames, triangle
from visionhands.synth import pinching, snapping, synthetic_frame
from visionhands.tuning import LATCH_THRESHOLDS
from visionhands.types import LandmarkFrame, channel_names, channel_values

# Which channel each sweep drives, and which threshold pair judges it. The NUMBERS
# come from visionhands/tuning.py rather than being re-typed here - see that
# module for the silent failure that caused.
THRESHOLDS = {
    "ramp": ("h0_pinch_index", *LATCH_THRESHOLDS["Pinch"]),
    "snap": ("h0_pinch_middle", *LATCH_THRESHOLDS["Snap"]),
    "clap": ("hands_distance", *LATCH_THRESHOLDS["Together"]),
}
# The dead-band sweep aims at the pinch thresholds specifically.
DEADBAND_ON, DEADBAND_OFF = LATCH_THRESHOLDS["Pinch"]

FPS = 30.0
PERIOD_S = 2.0


def _channels(frame: LandmarkFrame) -> dict[str, float]:
    """One frame all the way through the real derive(), as TouchDesigner sees it."""
    values = channel_values(frame, 5.0, 2)
    return derive(dict(zip(channel_names(), values, strict=True)), Params())


def _swept(name: str, cycles: float = 2.0) -> list[float]:
    """The watched channel's value on every frame of `cycles` cycles."""
    channel = THRESHOLDS[name][0]
    return [_channels(frame)[channel]
            for frame in frames(SEQUENCES[name], FPS, PERIOD_S, cycles)]


# ---------------------------------------------------------------------------
# The reference latch - docs/ATTRIBUTES.md, implemented once, here only
# ---------------------------------------------------------------------------
def schmitt(series: list[float], on: float, off: float,
            valid: list[bool] | None = None) -> tuple[list[int], int, int]:
    """state = valid AND (below_on OR (prev AND NOT above_off)). Also counts edges.

    Returns (states, engage_count, release_count).
    """
    states: list[int] = []
    previous = False
    engages = release = 0
    for index, value in enumerate(series):
        is_valid = True if valid is None else valid[index]
        state = is_valid and (value < on or (previous and not value > off))
        if state and not previous:
            engages += 1
        if previous and not state:
            release += 1
        states.append(1 if state else 0)
        previous = state
    return states, engages, release


# ---------------------------------------------------------------------------
# The phase shape
# ---------------------------------------------------------------------------
def test_triangle_starts_and_ends_apart_and_peaks_in_the_middle() -> None:
    # Starting at 0 matters: the latch initialises released, so a cycle that
    # begins already engaged would make the first pulse an artefact of the
    # initial state rather than a crossing.
    assert triangle(0.0) == 0.0
    assert triangle(0.5) == 1.0
    assert triangle(1.0) == pytest.approx(0.0)
    assert triangle(0.25) == pytest.approx(0.5)
    assert triangle(0.75) == pytest.approx(0.5)


def test_triangle_is_monotonic_over_each_half() -> None:
    rising = [triangle(i / 100.0) for i in range(51)]
    falling = [triangle(0.5 + i / 100.0) for i in range(51)]
    assert rising == sorted(rising)
    assert falling == sorted(falling, reverse=True)


# ---------------------------------------------------------------------------
# Frame generation
# ---------------------------------------------------------------------------
def test_frame_count_and_sequence_numbers() -> None:
    produced = list(frames(SEQUENCES["open"], FPS, PERIOD_S, 3.0, seq_start=100))
    assert len(produced) == int(FPS * PERIOD_S * 3)
    assert [f.seq for f in produced] == list(range(100, 100 + len(produced)))
    # captured_at is a real timeline, so a caller pacing against it reproduces
    # the intended sweep rate.
    assert produced[0].captured_at == 0.0
    assert produced[-1].captured_at == pytest.approx((len(produced) - 1) / FPS)


def test_the_last_cycle_reaches_the_same_extremes_as_the_first() -> None:
    """Phase is computed from the frame index, not accumulated.

    An accumulating `phase += step` drifts, and a drifting sweep stops reaching
    its endpoints - which are precisely where the thresholds are. Forty cycles
    in, the peak must still be the peak.
    """
    values = _swept("ramp", cycles=40.0)
    per_cycle = int(FPS * PERIOD_S)
    first = values[:per_cycle]
    last = values[-per_cycle:]
    assert min(last) == pytest.approx(min(first), abs=1e-9)
    assert max(last) == pytest.approx(max(first), abs=1e-9)


def test_frames_rejects_a_rate_that_cannot_produce_frames() -> None:
    with pytest.raises(ValueError):
        list(frames(SEQUENCES["open"], 0.0, PERIOD_S, 1.0))
    with pytest.raises(ValueError):
        list(frames(SEQUENCES["open"], FPS, 0.0, 1.0))


# ---------------------------------------------------------------------------
# The sweeps genuinely straddle the thresholds
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(THRESHOLDS))
def test_each_sweep_crosses_both_thresholds_with_room_to_spare(name: str) -> None:
    channel, on, off = THRESHOLDS[name]
    values = _swept(name)
    assert min(values) < on, "%s never gets below Xon: min %.3f" % (channel, min(values))
    assert max(values) > off, "%s never gets above Xoff: max %.3f" % (channel, max(values))
    # Not a value in the dead band, but many: the dead band is where the latch's
    # memory is the only thing deciding the output, so a sweep that crosses it in
    # one frame cannot tell a Schmitt trigger from a plain comparator.
    in_band = sum(1 for v in values if on <= v <= off)
    assert in_band >= 8, "%s spends only %d frames in the dead band" % (channel, in_band)


def test_snapping_aims_at_the_middle_finger_and_pinching_at_the_index() -> None:
    """The `pinch_finger` field is load-bearing, so assert it does something.

    SURVIVING MUTATION, found by review: replacing the target-tip lookup in
    synth.py with a hard-coded index tip left all 164 tests green. Nothing
    distinguished a snap from a pinch, because a full index pinch ALREADY brings
    the thumb to 0.21 hand-sizes of the middle tip - under the `Snapon` default of
    0.25 - so the only assertion on the snap sweep (that `pinch_middle` crosses
    its thresholds) passed on a sequence that was secretly sending a pinch.

    The discriminating property is which distance goes to ZERO: the tip the thumb
    was actually sent to.
    """
    pinch = _channels(synthetic_frame([pinching(1.0, size=0.12), None]))
    snap = _channels(synthetic_frame([snapping(1.0, size=0.12), None]))
    assert pinch["h0_pinch_index"] < pinch["h0_pinch_middle"]
    assert snap["h0_pinch_middle"] < snap["h0_pinch_index"]
    # And to the point: each target is reached, not merely approached.
    assert pinch["h0_pinch_index"] == pytest.approx(0.0, abs=1e-6)
    assert snap["h0_pinch_middle"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("name", sorted(THRESHOLDS))
def test_each_sweep_clears_its_thresholds_by_a_real_margin(name: str) -> None:
    """"Crosses the threshold" is not enough - it has to clear it.

    SURVIVING MUTATION, found by review: doubling the clap sweep's separation left
    the minimum `hands_distance` at 0.5999999999999996, four parts in 1e17 below
    `Togetheron` 0.60. Every Python assertion passed. But the value reaches
    TouchDesigner as a float32, where it rounds to exactly 0.6 and the latch never
    engages - so the suite would be green while `clap --cycles 3` reported +0 and
    the network took the blame.

    A margin of 10% of the dead band, and a REQUIREMENT that several frames sit
    clear on each side, so one lucky sample cannot satisfy it.
    """
    channel, on, off = THRESHOLDS[name]
    values = _swept(name)
    margin = (off - on) * 0.1
    below = sum(1 for v in values if v < on - margin)
    above = sum(1 for v in values if v > off + margin)
    assert below >= 4, ("%s only clears Xon by a margin on %d frames (min %.6f "
                        "against on %.3f) - float32 would swallow that"
                        % (channel, below, min(values), on))
    assert above >= 4, ("%s only clears Xoff by a margin on %d frames (max %.6f "
                        "against off %.3f)" % (channel, above, max(values), off))


@pytest.mark.parametrize("name", sorted(THRESHOLDS))
def test_no_swept_value_is_nan(name: str) -> None:
    assert not any(math.isnan(v) for v in _swept(name))


# ---------------------------------------------------------------------------
# The counts the TouchDesigner network is checked against
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(THRESHOLDS))
def test_a_correct_latch_engages_exactly_once_per_cycle(name: str) -> None:
    _channel, on, off = THRESHOLDS[name]
    cycles = 3
    states, engages, releases = schmitt(_swept(name, cycles=float(cycles)), on, off)
    assert engages == cycles
    # One release per engage: each cycle ends apart, so the last engage closes
    # inside the sweep rather than being left latched at the end.
    assert releases == cycles
    assert states[0] == 0
    assert states[-1] == 0


def test_the_state_holds_through_the_dead_band_rather_than_chattering() -> None:
    """The property that distinguishes this from a comparator.

    Inside the dead band the state must be whatever it was on the way in - 1 if
    the sweep arrived from below `on`, 0 if from above `off`. A comparator would
    switch somewhere in the middle of the band, and the transition count is what
    catches it: a Schmitt trigger changes state exactly twice per cycle.
    """
    channel, on, off = THRESHOLDS["ramp"]
    values = _swept("ramp", cycles=1.0)
    states, _, _ = schmitt(values, on, off)
    transitions = sum(1 for i in range(1, len(states)) if states[i] != states[i - 1])
    assert transitions == 2, "%s changed state %d times in one sweep" % (channel,
                                                                        transitions)
    # And explicitly: at least one frame sits in the band engaged, and at least
    # one sits in the band released. Both, or the dead band is untested in one
    # direction.
    banded = [(v, s) for v, s in zip(values, states, strict=True) if on <= v <= off]
    assert any(s == 1 for _, s in banded)
    assert any(s == 0 for _, s in banded)


# ---------------------------------------------------------------------------
# The dead-band sweep - the one that separates a latch from a comparator
# ---------------------------------------------------------------------------
def test_deadband_crosses_on_but_never_off() -> None:
    """Pins the two amplitudes the `deadband` sequence depends on.

    If the trough stops short of `Pinchon` the latch never engages and the test
    downstream passes for the wrong reason; if the crest reaches `Pinchoff` the
    latch legitimately re-arms and the sequence stops being a hysteresis test at
    all. Both failures look like success from the outside, which is why the
    amplitudes are asserted here rather than trusted.
    """
    on, off = DEADBAND_ON, DEADBAND_OFF
    values = [_channels(f)["h0_pinch_index"]
              for f in frames(SEQUENCES["deadband"], FPS, PERIOD_S, 2.0)]
    assert min(values) < on, "trough %.4f never gets below Pinchon" % min(values)
    assert max(values) < off, "crest %.4f reaches Pinchoff and re-arms" % max(values)
    # STRICTLY INSIDE the band, with a margin. This is the assertion that makes
    # the sweep a hysteresis test at all: if the crest slips below `on` the
    # distance never leaves the engaged region, the latch is never asked to hold
    # anything, and TouchDesigner still reports the expected +1 - a pass that
    # means nothing. Raising Pinchon to 0.45 in the builder alone used to do
    # exactly that, silently.
    margin = (off - on) * 0.1
    assert max(values) > on + margin, (
        "crest %.4f is not clear of Pinchon %.4f - the sweep no longer traverses "
        "the dead band, so +1 would prove nothing" % (max(values), on))


def test_a_correct_latch_fires_once_over_many_deadband_cycles() -> None:
    """The number the TouchDesigner network is checked against.

    Six dips below `on`, and a correct latch engages exactly ONCE - because
    nothing ever rises past `off` to re-arm it. A comparator, or a parity
    counter that has lost sync, gives six.
    """
    values = [_channels(f)["h0_pinch_index"]
              for f in frames(SEQUENCES["deadband"], FPS, PERIOD_S, 6.0)]
    states, engages, releases = schmitt(values, DEADBAND_ON, DEADBAND_OFF)
    assert engages == 1
    assert releases == 0
    assert states[-1] == 1          # still engaged at the end, never re-armed
    # The first frames sit in the band having never engaged, which is the other
    # direction of the hold.
    assert states[0] == 0


# ---------------------------------------------------------------------------
# The absent-hand trap
# ---------------------------------------------------------------------------
def test_absent_hands_read_zero_distance_and_zero_validity() -> None:
    """The trap docs/ATTRIBUTES.md opens with, as a sequence rather than a frame.

    Every distance reads 0 - below every engage threshold - so the ONLY thing
    stopping pinch, snap and clap firing together is the validity gate. This
    asserts the trap is really present in the stream, which is what makes sending
    `absent` at TouchDesigner a meaningful test of the gate rather than a no-op.
    """
    for frame in frames(SEQUENCES["absent"], FPS, PERIOD_S, 0.2):
        out = _channels(frame)
        assert out["h0_valid"] == 0.0
        assert out["h1_valid"] == 0.0
        assert out["both_valid"] == 0.0
        for channel, on, _off in THRESHOLDS.values():
            assert out[channel] == 0.0
            assert out[channel] < on          # the trap, stated as the test


def test_a_correct_latch_never_engages_on_absent_hands() -> None:
    """Same stream through the reference latch, with the validity gate applied."""
    per_frame = [_channels(f) for f in frames(SEQUENCES["absent"], FPS, PERIOD_S, 1.0)]
    for channel, on, off in THRESHOLDS.values():
        valid_channel = "both_valid" if channel.startswith("hands") else "h0_valid"
        states, engages, _ = schmitt([p[channel] for p in per_frame], on, off,
                                     valid=[p[valid_channel] > 0.5 for p in per_frame])
        assert engages == 0
        assert set(states) == {0}


def test_without_the_validity_gate_the_absent_hand_fires_everything() -> None:
    """The negative control: proves the previous test is testing the gate.

    Run the SAME stream with `valid` forced true and every latch engages on the
    first frame. Without this, a latch that never engages for some unrelated
    reason would pass the test above and look correct.
    """
    per_frame = [_channels(f) for f in frames(SEQUENCES["absent"], FPS, PERIOD_S, 0.2)]
    for channel, on, off in THRESHOLDS.values():
        _states, engages, _ = schmitt([p[channel] for p in per_frame], on, off)
        assert engages == 1


def test_both_hands_pinch_together_without_becoming_a_clap() -> None:
    """The `both` sequence must exercise h1 and leave the clap latch alone."""
    per_frame = [_channels(f)
                 for f in frames(SEQUENCES["both"], FPS, PERIOD_S, 2.0)]
    assert all(p["both_valid"] == 1.0 for p in per_frame)
    for hand in ("h0", "h1"):
        _states, engages, releases = schmitt(
            [p["%s_pinch_index" % hand] for p in per_frame], *LATCH_THRESHOLDS["Pinch"])
        assert engages == 2, "%s engaged %d times over two sweeps" % (hand, engages)
        assert releases == 2
    # ... and never close enough to count as hands together.
    _s, clap_engages, _r = schmitt([p["hands_distance"] for p in per_frame],
                                   *LATCH_THRESHOLDS["Together"])
    assert clap_engages == 0
    assert min(p["hands_distance"] for p in per_frame) > LATCH_THRESHOLDS["Together"][1]


# ---------------------------------------------------------------------------
# The static baseline
# ---------------------------------------------------------------------------
def test_the_open_baseline_engages_nothing() -> None:
    per_frame = [_channels(f) for f in frames(SEQUENCES["open"], FPS, PERIOD_S, 1.0)]
    assert all(p["both_valid"] == 1.0 for p in per_frame)       # hands ARE there
    for channel, on, off in THRESHOLDS.values():
        _states, engages, _ = schmitt([p[channel] for p in per_frame], on, off)
        assert engages == 0, "%s engaged on two open hands held still" % channel


def test_the_clap_sweep_keeps_the_centre_still() -> None:
    """`hands_center_x` must not move while the hands close on each other.

    The sweep moves both hands symmetrically, so a drifting centre means the
    two-hand geometry is being computed from the wrong points - and it would be
    invisible in `hands_distance`, which is symmetric either way.
    """
    for frame in frames(SEQUENCES["clap"], FPS, PERIOD_S, 1.0):
        out = _channels(frame)
        assert out["hands_center_x"] == pytest.approx(0.5, abs=1e-6)
        assert out["hands_center_y"] == pytest.approx(0.5, abs=1e-6)
