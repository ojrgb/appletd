"""A PROTOTYPE: the whole `temporal` group as one pure function plus explicit state.

    THIS IS AN EXPERIMENT, NOT THE SHIPPING PATH. `tools/td_add_temporal.py` builds
    74 native operators and that is still what runs. This module exists to answer
    one question with a number instead of an argument: **would collapsing a chain of
    native CHOPs into a single Script CHOP be faster, and by enough to matter?**

    The verdict, and how it was reached, is in `docs/BUILD_PLAN.md` step 12. Read
    that before deciding anything on the strength of this file.

WHY IT IS WORTH MEASURING. `temporal` is 74 operators for 27 channels, and every one
of those operators pays TouchDesigner's per-channel-per-operator cost - MEASURED at
about 1.2 microseconds (DESIGN.md 2.14). A Script CHOP pays that once, plus one
Python call. From the numbers `derive_chop` gives up, the arithmetic looked like it
should land near half.

WHY IT MIGHT STILL BE THE WRONG ANSWER, and this is not hedging - it is the reason
`docs/ATTRIBUTES.md` put memory in native CHOPs in the first place, and all three
reasons survive being measured:

  1. **TouchDesigner does it properly.** A Trail CHOP's window is a window whatever
     the cook rate does. The deque below is a window in COOKS, which is the same
     thing only as long as nothing ever cooks twice in a frame or skips one.
  2. **It is inspectable.** 74 operators can be looked at in the network with a
     hand in front of the camera. A dict in operator storage cannot.
  3. **No hidden Python state for a project reload to treat unpredictably.** This is
     the one that decided it, and it turned out STRONGER than the argument for it.
     The state does not merely reload unpredictably: with it in operator storage,
     **the project would not save.** TouchDesigner pickles storage into the .toe, and
     `PicklingError: Can't pickle <class 'appletd.temporal.TemporalState'>: it's
     not the same object as appletd.temporal.TemporalState` is what a save
     produced - because pickle compares class identity and the builders re-import this
     module. The prototype keeps its state in its callback DAT's module globals now,
     which are not pickled. A shipping version would have to answer this properly.

WHAT IS FAITHFULLY REPRODUCED. The Schmitt debounce, exactly as
`tools/td_add_temporal.py` builds it and `docs/ATTRIBUTES.md` specifies it:

    active = all_valid_over_last_A  OR  (prev AND any_valid_over_last_D)

A Trail plus an Analyze taking the MINIMUM is "valid on every one of the last A
frames"; taking the MAXIMUM over D frames is "valid on any of the last D". Between
them the state holds, which is what makes it a dead band rather than a comparator.
D > A on purpose - dropping a hand mid-gesture is worse than acquiring one late.

WHAT IS DELIBERATELY NOT REPRODUCED. `dir` and its unit vector come from
`appletd.motion.directions`, which is already pure and already used by the
native group's own Script CHOP. Re-implementing it here would measure this file
against a copy of itself.

Thread: pure functions over an explicit state object. Nothing here touches
        TouchDesigner or pyobjc. In the prototype it runs on TD's main thread
        inside a Script CHOP cook.
Ref: docs/BUILD_PLAN.md step 12 (the verdict), docs/ATTRIBUTES.md (Presence and
     Motion), DESIGN.md 2.10 (why a recurrence needs a clock), 2.14 (the cost).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Final

from appletd.motion import MotionParams, directions
from appletd.types import MAX_HANDS

# The frame rate the windows are counted in. A Trail CHOP measures its window in
# SAMPLES and inherits the cook rate; a deque here measures it in cooks, which is
# the same thing only while the cook rate is what we think it is. Stated as a
# constant rather than buried, because it is the assumption most likely to be wrong
# on someone else's machine.
#
# UNMEASURED as a general fact: TouchDesigner's default is 60 fps and this project
# has always run there, but nothing here checks it.
ASSUMED_COOK_FPS: Final = 60.0


@dataclass(frozen=True)
class TemporalParams:
    """Everything the recurrences need, in the units the parameters use.

    activate_frames / deactivate_frames  the two Schmitt windows, in FRAMES, from
        the Advanced page. Deactivate is larger on purpose (see the module
        docstring).
    velocity_filter_s  seconds of moving average on velocity before speed and
        acceleration read it. Differentiating amplifies noise, so this is the second
        stage - the positions are already one-euro filtered.
    liveness_window_s  how far back to look for a change in `seq` before calling the
        sender dead. Long enough to bridge 30 fps delivery into a 60 fps cook.
    speedfloor  below this, a direction is noise rather than a heading, so `dir`
        holds instead of spinning. Normalised units per second.
    """

    activate_frames: int = 3
    deactivate_frames: int = 6
    velocity_filter_s: float = 0.15
    liveness_window_s: float = 0.5
    speedfloor: float = 0.05


@dataclass
class TemporalState:
    """Every recurrence's memory, in one place so it can be reasoned about.

    NOT frozen, and that is the whole point of the experiment: this is the hidden
    Python state that `docs/ATTRIBUTES.md` gave as the third reason to keep memory
    native. A save/reload knows nothing about it.

    Every deque is bounded by construction, so none of this grows without limit -
    which is the one failure mode that would be worse in Python than in a CHOP.
    """

    # Per hand: the validity history the two Schmitt windows read.
    valid: list[deque[float]] = field(default_factory=list)
    # Per hand: whether `active` was true on the previous cook, and for how long.
    prev_active: list[float] = field(default_factory=list)
    held_s: list[float] = field(default_factory=list)
    # Per hand: the previous palm position, and the velocity history being averaged.
    prev_palm: list[tuple[float, float] | None] = field(default_factory=list)
    vel_x: list[deque[float]] = field(default_factory=list)
    vel_y: list[deque[float]] = field(default_factory=list)
    prev_speed: list[float] = field(default_factory=list)
    prev_dir: list[float] = field(default_factory=list)
    # Frame-wide: `seq` history for liveness, and the two-hand distance for approach.
    seq: deque[float] = field(default_factory=lambda: deque(maxlen=2))
    prev_distance: float | None = None
    approach: deque[float] = field(default_factory=lambda: deque(maxlen=2))

    def sized(self, params: TemporalParams) -> TemporalState:
        """Grow the per-hand lists and re-window the deques for `params`. Returns self.

        Called on every cook, because a window length is a PARAMETER and a user can
        move it while the thing is running. A deque cannot be re-windowed in place,
        so a changed length rebuilds it keeping whatever history still fits - which
        is the same thing a Trail CHOP does when its length changes.
        """
        window = max(1, int(params.velocity_filter_s * ASSUMED_COOK_FPS))
        live = max(1, int(params.liveness_window_s * ASSUMED_COOK_FPS))
        keep = max(1, params.activate_frames, params.deactivate_frames)
        while len(self.prev_active) < MAX_HANDS:
            self.valid.append(deque(maxlen=keep))
            self.prev_active.append(0.0)
            self.held_s.append(0.0)
            self.prev_palm.append(None)
            self.vel_x.append(deque(maxlen=window))
            self.vel_y.append(deque(maxlen=window))
            self.prev_speed.append(0.0)
            self.prev_dir.append(0.0)
        for index in range(MAX_HANDS):
            if self.valid[index].maxlen != keep:
                self.valid[index] = deque(self.valid[index], maxlen=keep)
            if self.vel_x[index].maxlen != window:
                self.vel_x[index] = deque(self.vel_x[index], maxlen=window)
                self.vel_y[index] = deque(self.vel_y[index], maxlen=window)
        if self.seq.maxlen != live:
            self.seq = deque(self.seq, maxlen=live)
        if self.approach.maxlen != window:
            self.approach = deque(self.approach, maxlen=window)
        return self


def _mean(values: deque[float]) -> float:
    """The moving average a Trail-into-Analyze pair computes. Empty reads 0."""
    return (sum(values) / len(values)) if values else 0.0


def advance(values: dict[str, float], state: TemporalState,
            params: TemporalParams, dt: float) -> dict[str, float]:
    """One cook: read the derived attributes, mutate `state`, return the channels.

    Contract: `values` is `derive_chop`'s output plus `seq` off the RAW stream -
              liveness must not depend on the filter chain it is watching, which is
              why the native group takes two inputs. `dt` is seconds since the last
              cook; a non-positive `dt` leaves every rate at its previous value
              rather than dividing by zero.
    Returns:  exactly the 27 channels `temporal` publishes, plus `ready` - the
              presence gate the latch bank arms on.
    Thread:   pure apart from `state`, which it owns. Never call it twice for one
              frame: every recurrence here would advance twice, which is precisely
              the failure DESIGN.md 2.10 records for a branch with memory and no
              clock. The Feedback CHOP it replaces is immune to that, because its
              `output = previous` is time-based.
    """
    state.sized(params)
    out: dict[str, float] = {}

    # -- liveness: has `seq` moved inside the window ----------------------
    # ONE COOK OF WARM-UP, and it is a property rather than an off-by-one: "has `seq`
    # changed" cannot be answered from a single sample, so nothing is ever live or
    # active on the first cook after a reset. The native group has the same shape - a
    # Slope on `seq` into a Trail - so this matches it rather than differing from it.
    state.seq.append(values.get("seq", 0.0))
    live = 1.0 if len(set(state.seq)) > 1 else 0.0

    n_active = 0.0
    for index in range(MAX_HANDS):
        prefix = "h%d_" % index

        # -- the Schmitt debounce ------------------------------------------
        valid = 1.0 if (values.get(prefix + "valid", 0.0) > 0.5 and live) else 0.0
        state.valid[index].append(valid)
        recent = list(state.valid[index])
        # The MINIMUM over the last A: 1 only if every one of them was valid.
        turn_on = min(recent[-params.activate_frames:] or [0.0])
        # The MAXIMUM over the last D: 0 only if none of them was.
        stay_on = max(recent[-params.deactivate_frames:] or [0.0])
        prev = state.prev_active[index]
        active = 1.0 if (turn_on > 0.5 or (prev > 0.5 and stay_on > 0.5)) else 0.0

        out[prefix + "active"] = active
        out[prefix + "entering"] = 1.0 if (active > 0.5 and prev < 0.5) else 0.0
        out[prefix + "exiting"] = 1.0 if (active < 0.5 and prev > 0.5) else 0.0
        # `held` counts seconds, and resets on release rather than on acquire, so a
        # consumer reading it during a gesture sees how long the hand has been there.
        state.held_s[index] = (state.held_s[index] + dt) if active > 0.5 else 0.0
        out[prefix + "held"] = state.held_s[index]
        state.prev_active[index] = active
        n_active += active

        # -- velocity, and the moving average over it ----------------------
        palm = (values.get(prefix + "palm_x", 0.0),
                values.get(prefix + "palm_y", 0.0))
        previous_palm = state.prev_palm[index]
        if previous_palm is not None and dt > 0.0:
            state.vel_x[index].append((palm[0] - previous_palm[0]) / dt)
            state.vel_y[index].append((palm[1] - previous_palm[1]) / dt)
        state.prev_palm[index] = palm
        vel_x, vel_y = _mean(state.vel_x[index]), _mean(state.vel_y[index])
        out[prefix + "vel_x"], out[prefix + "vel_y"] = vel_x, vel_y

        speed = math.hypot(vel_x, vel_y)
        out[prefix + "speed"] = speed
        out[prefix + "accel"] = ((speed - state.prev_speed[index]) / dt
                                 if dt > 0.0 else 0.0)
        state.prev_speed[index] = speed
        out[prefix + "moving"] = 1.0 if speed > params.speedfloor else 0.0

    out["both_active"] = 1.0 if n_active >= MAX_HANDS else 0.0
    out["n_active"] = n_active
    # The gate the latch bank arms on. Named `ready` because that is what the native
    # group calls its second output.
    out["ready"] = 1.0 if (live and n_active > 0.0) else 0.0

    # -- the two hands closing on each other ------------------------------
    distance = values.get("hands_distance", 0.0)
    if state.prev_distance is not None and dt > 0.0:
        state.approach.append((distance - state.prev_distance) / dt)
    state.prev_distance = distance
    out["hands_approach"] = _mean(state.approach)

    # -- direction, from the module that already does it ------------------
    # `motion.directions` is pure and is what the native group's own Script CHOP
    # calls, so reusing it measures this file against the native CHOPs rather than
    # against a second copy of the same arithmetic. The HOLDING below `speedfloor`
    # is memory, so it is here rather than there.
    heading = directions({**values, **out}, MotionParams(speedfloor=params.speedfloor))
    for index in range(MAX_HANDS):
        prefix = "h%d_" % index
        fresh = heading.get(prefix + "dir", 0.0)
        if out[prefix + "moving"] > 0.5:
            state.prev_dir[index] = fresh
        held_dir = state.prev_dir[index]
        out[prefix + "dir"] = held_dir
        out[prefix + "dir_x"] = math.cos(math.radians(held_dir))
        out[prefix + "dir_y"] = math.sin(math.radians(held_dir))
    return out


def channel_names() -> tuple[str, ...]:
    """The channels `advance` returns, in sorted order. For a Script CHOP's cache.

    Derived by RUNNING it on an empty frame rather than listed by hand, so the list
    and the function cannot drift apart - the same discipline `face_channel_names`
    follows in reverse.
    """
    return tuple(sorted(advance({}, TemporalState(), TemporalParams(), 1 / 60.0)))
