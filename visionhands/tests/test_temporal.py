"""The prototype temporal recurrences, against the specification rather than the CHOPs.

`visionhands/temporal.py` is an experiment - the question is whether one Script CHOP
beats 74 native operators, and `docs/BUILD_PLAN.md` step 12 has the verdict. But the
experiment is only worth its measurement if the thing being measured is CORRECT, and
a debounce that is subtly wrong would be faster for the wrong reason.

So these tests assert the recurrence `docs/ATTRIBUTES.md` specifies:

    active = all_valid_over_last_A  OR  (prev AND any_valid_over_last_D)

and the properties that follow from it, which are the ones a comparator would fail:
it takes A frames to turn on, D frames of absence to turn off, and a single dropped
frame in the middle changes nothing.

Ref: visionhands/temporal.py, docs/ATTRIBUTES.md (Presence and Motion).
"""

from __future__ import annotations

import math

import pytest

from visionhands.temporal import (
    TemporalParams,
    TemporalState,
    advance,
    channel_names,
)

DT = 1.0 / 60.0


def _run(frames: list[dict[str, float]],
         params: TemporalParams | None = None) -> list[dict[str, float]]:
    """Feed a sequence through one state object, as a cook loop would."""
    params = params or TemporalParams()
    state = TemporalState()
    return [advance(values, state, params, DT) for values in frames]


def _valid(seq: int, hand0: bool = True, **extra: float) -> dict[str, float]:
    """One frame: `seq` moving, so liveness holds, and h0 valid or not.

    NOTE the one-frame warm-up every sequence below allows for. Liveness is "has
    `seq` CHANGED", which cannot be answered from a single frame, so frame 1 is never
    live and no hand is ever active on it. The native group has exactly the same
    property - its liveness is a Slope on `seq` into a Trail - so matching it is the
    point rather than a limitation of this prototype.
    """
    return {"seq": float(seq), "h0_valid": 1.0 if hand0 else 0.0, **extra}


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
def test_the_channel_list_is_stable_and_complete() -> None:
    """28 channels: the 27 `temporal` publishes plus `ready`, the gate the latch
    bank arms on."""
    names = channel_names()
    assert len(names) == 28
    assert names == tuple(sorted(names))
    for expected in ("h0_active", "h1_exiting", "both_active", "n_active",
                     "h0_vel_x", "h0_speed", "h0_accel", "h0_dir", "h0_dir_x",
                     "h0_moving", "hands_approach", "ready"):
        assert expected in names


def test_every_cook_returns_the_same_channels() -> None:
    """A Script CHOP caches its channel list and writes by POSITION, so a set that
    changed between cooks would put one attribute's value in another's channel."""
    frames = _run([_valid(1), _valid(2, False), _valid(3)])
    assert all(tuple(sorted(f)) == channel_names() for f in frames)


# ---------------------------------------------------------------------------
# The Schmitt debounce, which is the whole point
# ---------------------------------------------------------------------------
def test_it_takes_activate_frames_to_turn_on() -> None:
    """A comparator would read active on frame 1. The window is what makes it a
    debounce, and 3 is the default."""
    params = TemporalParams(activate_frames=3, deactivate_frames=6)
    out = _run([_valid(i + 1) for i in range(6)], params)
    # Frame 1 is the liveness warm-up; frames 2, 3 and 4 are the three valid ones.
    assert [f["h0_active"] for f in out] == [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]


def test_it_takes_deactivate_frames_of_absence_to_turn_off() -> None:
    """And `deactivate_frames` is deliberately larger than `activate_frames`:
    dropping a hand mid-gesture is worse than acquiring one a frame late."""
    params = TemporalParams(activate_frames=2, deactivate_frames=4)
    frames = [_valid(i + 1) for i in range(4)]
    frames += [_valid(i + 5, False) for i in range(6)]
    out = _run(frames, params)
    # On from index 2: index 0 is the warm-up, then two valid frames.
    assert [f["h0_active"] for f in out[:3]] == [0.0, 0.0, 1.0]
    # Still on while ANY of the last four frames was valid - indices 3 to 6.
    assert [f["h0_active"] for f in out[3:7]] == [1.0, 1.0, 1.0, 1.0]
    # And off once all four are invalid.
    assert [f["h0_active"] for f in out[7:]] == [0.0, 0.0, 0.0]


def test_one_dropped_frame_does_not_drop_the_hand() -> None:
    """The failure this exists to prevent: a single low-confidence frame mid-pinch
    used to drop the state, fire a spurious release and re-engage with a bumped
    counter."""
    params = TemporalParams(activate_frames=2, deactivate_frames=6)
    frames = [_valid(1), _valid(2), _valid(3), _valid(4, False), _valid(5),
              _valid(6)]
    out = _run(frames, params)
    assert [f["h0_active"] for f in out[2:]] == [1.0, 1.0, 1.0, 1.0]
    assert not any(f["h0_exiting"] for f in out)


def test_entering_and_exiting_are_one_frame_wide() -> None:
    """An edge pulse two frames wide double-counts everything downstream."""
    params = TemporalParams(activate_frames=1, deactivate_frames=1)
    frames = [_valid(1), _valid(2), _valid(3, False), _valid(4, False)]
    out = _run(frames, params)
    assert [f["h0_entering"] for f in out] == [0.0, 1.0, 0.0, 0.0]
    assert [f["h0_exiting"] for f in out] == [0.0, 0.0, 1.0, 0.0]


def test_a_dead_sender_deactivates_everything() -> None:
    """`seq` frozen means the sidecar stopped, and DESIGN.md 2.9 records that
    TouchDesigner's channels then hold their last values rather than zeroing. A hand
    that stays 'valid' for ever because nothing is updating it is the stuck-latch bug
    that a review found once already."""
    params = TemporalParams(activate_frames=1, deactivate_frames=1)
    # `seq` never moves, so liveness is false however valid the hand looks.
    out = _run([{"seq": 7.0, "h0_valid": 1.0} for _ in range(6)], params)
    assert all(f["h0_active"] == 0.0 for f in out)
    assert all(f["ready"] == 0.0 for f in out)


def test_held_counts_seconds_and_resets_on_release() -> None:
    params = TemporalParams(activate_frames=1, deactivate_frames=1)
    out = _run([_valid(1), _valid(2), _valid(3), _valid(4, False)], params)
    assert out[0]["h0_held"] == 0.0                # the liveness warm-up
    assert out[1]["h0_held"] == pytest.approx(DT)
    assert out[2]["h0_held"] == pytest.approx(2 * DT)
    assert out[3]["h0_held"] == 0.0


# ---------------------------------------------------------------------------
# Velocity, and the direction hold
# ---------------------------------------------------------------------------
def test_velocity_is_the_derivative_of_the_palm() -> None:
    """Averaged over the filter window, so it takes a few frames to reach the true
    rate - which is the point of the window, not a bug in it."""
    params = TemporalParams(velocity_filter_s=2 * DT)
    frames = [_valid(i + 1, True, h0_palm_x=0.1 * i) for i in range(6)]
    out = _run(frames, params)
    # 0.1 per frame at 60 fps is 6.0 units per second.
    assert out[-1]["h0_vel_x"] == pytest.approx(6.0, rel=1e-6)
    assert out[-1]["h0_speed"] == pytest.approx(6.0, rel=1e-6)
    assert out[-1]["h0_moving"] == 1.0


def test_a_still_hand_is_not_moving_and_holds_its_heading() -> None:
    """A direction below the speed floor is noise, so `dir` holds its last value
    instead of spinning - which is memory, and is why this lives here rather than in
    the stateless `motion.py`."""
    params = TemporalParams(velocity_filter_s=2 * DT, speedfloor=0.05)
    moving = [_valid(i + 1, True, h0_palm_x=0.1 * i) for i in range(5)]
    still = [_valid(i + 6, True, h0_palm_x=0.4) for i in range(6)]
    out = _run(moving + still, params)
    heading = out[4]["h0_dir"]
    assert out[4]["h0_moving"] == 1.0
    assert out[-1]["h0_moving"] == 0.0
    # Held, not reset to zero.
    assert out[-1]["h0_dir"] == pytest.approx(heading)
    assert out[-1]["h0_dir_x"] == pytest.approx(math.cos(math.radians(heading)))


def test_the_unit_vector_stays_a_unit_vector() -> None:
    out = _run([_valid(i + 1, True, h0_palm_x=0.1 * i, h0_palm_y=0.05 * i)
                for i in range(6)])
    last = out[-1]
    assert math.hypot(last["h0_dir_x"], last["h0_dir_y"]) == pytest.approx(1.0)


def test_two_hands_and_the_frame_wide_channels() -> None:
    params = TemporalParams(activate_frames=1, deactivate_frames=1)
    both = [{"seq": float(i + 1), "h0_valid": 1.0, "h1_valid": 1.0}
            for i in range(3)]
    out = _run(both, params)
    assert out[-1]["n_active"] == 2.0
    assert out[-1]["both_active"] == 1.0
    one = _run([_valid(i + 1) for i in range(3)], params)
    assert one[-1]["n_active"] == 1.0
    assert one[-1]["both_active"] == 0.0


def test_approach_is_the_rate_the_hands_close(  ) -> None:
    """Negative when they come together, which is the sign a consumer reads."""
    params = TemporalParams(velocity_filter_s=2 * DT)
    frames = [{"seq": float(i + 1), "h0_valid": 1.0, "h1_valid": 1.0,
               "hands_distance": 1.0 - 0.1 * i} for i in range(6)]
    out = _run(frames, params)
    assert out[-1]["hands_approach"] == pytest.approx(-6.0, rel=1e-6)


# ---------------------------------------------------------------------------
# The things that would make it unsafe rather than merely slow
# ---------------------------------------------------------------------------
def test_a_zero_dt_does_not_divide_by_zero() -> None:
    """A cook with no elapsed time happens - a forced cook, a paused timeline - and
    every rate here has `dt` in its denominator."""
    state = TemporalState()
    params = TemporalParams()
    advance(_valid(1, True, h0_palm_x=0.0), state, params, DT)
    out = advance(_valid(2, True, h0_palm_x=0.5), state, params, 0.0)
    assert all(v == v for v in out.values())          # no NaN
    assert out["h0_accel"] == 0.0


def test_no_deque_grows_without_bound() -> None:
    """The one failure mode that would be worse in Python than in a CHOP. Every
    window is bounded by construction, so 10,000 cooks must not accumulate."""
    state = TemporalState()
    params = TemporalParams()
    for i in range(10_000):
        advance(_valid(i + 1, True, h0_palm_x=0.001 * i), state, params, DT)
    assert len(state.seq) <= (state.seq.maxlen or 0)
    for index in range(2):
        assert len(state.valid[index]) <= (state.valid[index].maxlen or 0)
        assert len(state.vel_x[index]) <= (state.vel_x[index].maxlen or 0)
    assert len(state.approach) <= (state.approach.maxlen or 0)


def test_changing_a_window_while_running_does_not_lose_the_state() -> None:
    """A window length is a PARAMETER and a user can move it mid-session. A deque
    cannot be re-windowed in place, so this checks the rebuild keeps what fits -
    which is what a Trail CHOP does when its length changes."""
    state = TemporalState()
    for i in range(10):
        advance(_valid(i + 1), state, TemporalParams(activate_frames=3), DT)
    assert state.prev_active[0] == 1.0
    out = advance(_valid(11), state, TemporalParams(activate_frames=8), DT)
    # Still active: the history survived the re-window, so it is not starting over.
    assert out["h0_active"] == 1.0
