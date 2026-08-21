"""Gesture sequences over time: synthetic hands as a function of a phase.

WHY THIS EXISTS. `derive()` is stateless, so one frame is enough to test it. The
latches are not: `state = valid AND (below_on OR (prev AND NOT above_off))` has a
dead band, and the whole point of the dead band is that the state depends on how
the input ARRIVED rather than on where it is now. A single frame cannot exercise
that, and neither can waving at the camera - a hand that happens to cross both
thresholds in the same frame proves nothing, and you cannot tell from the outside
whether it did.

So the input is generated instead: a distance that descends past `on`, stops
inside the dead band, and ascends past `off`, at a rate we choose. Then "the
state engaged exactly once" is an assertion rather than an impression.

Pure Python, no sockets, no TouchDesigner, no camera. `tools/send_synthetic.py`
is the thin CLI that puts these frames on the wire.

WHAT A SEQUENCE IS. A function from a phase in 0..1 to the poses of both hands.
Phase, not frame number and not seconds, so the same sequence can be replayed at
any rate: fast enough to test that a pulse fires once, slow enough to watch, and
in a unit test with no clock at all.

Ref: docs/ATTRIBUTES.md (the latch definitions and the thresholds these sweep
across), visionhands/synth.py (the hand geometry), DESIGN.md 6.1.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Final

from visionhands.synth import HandPose, open_hand, pinching, synthetic_frame
from visionhands.types import (
    CHIRALITY_LEFT,
    CHIRALITY_RIGHT,
    LandmarkFrame,
)

# One sequence: phase in 0..1 -> the pose of each hand slot, None for absent.
SequenceFn = Callable[[float], list[HandPose | None]]

# Hand size used throughout, in normalised units - the wrist-to-middle-MCP
# distance. Every threshold in docs/ATTRIBUTES.md is a RATIO of this, so its
# actual value does not affect any of them; it only has to be small enough that
# two hands fit in frame.
_SIZE: Final = 0.12

# Where a single hand sits. Centre frame, fingers up.
_CENTRE_X: Final = 0.5
_CENTRE_Y: Final = 0.5

# The two-hand sweep, in hand-size units of palm separation. `hands_distance` is
# the palm separation divided by mean hand size, so these ARE the values that
# channel takes - and they straddle the `Togetheron` 0.60 / `Togetheroff` 0.95
# defaults with room on both sides, so the sweep provably crosses both.
_APART_RATIO: Final = 1.60
_TOGETHER_RATIO: Final = 0.30


# ---------------------------------------------------------------------------
# Phase shapes
# ---------------------------------------------------------------------------
def triangle(phase: float) -> float:
    """0 -> 1 -> 0 across phase 0..1. The shape every ramp sequence uses.

    Why a triangle and not a step: a step crosses `on` and `off` in the same
    frame, which is the one input that cannot distinguish a working Schmitt
    trigger from a plain comparator. A ramp spends frames inside the dead band,
    which is where the latch's memory is the only thing deciding the output.

    Why it starts and ends at 0 (fully apart): the latch initialises released, so
    a cycle that begins apart makes the first engage a genuine crossing rather
    than an artefact of the initial state.
    """
    return 2.0 * phase if phase < 0.5 else 2.0 * (1.0 - phase)


# ---------------------------------------------------------------------------
# The sequences
# ---------------------------------------------------------------------------
def _ramp_to_finger(finger: str) -> SequenceFn:
    """One hand, thumb sweeping to `finger`'s tip and back.

    A factory rather than four near-identical functions, because there are now
    eight finger-trigger latches and each one wants its own sweep - the ONLY thing
    that distinguishes them is which distance channel goes to zero, so writing
    them out separately is how three of the four end up subtly different.

    Sweeps `h0_pinch_<finger>` from its resting ratio down to 0.00 and back:
    index 1.16, middle 1.31, and further for ring and little, which sit wider of
    the thumb. All four straddle the `Trigger` defaults of 0.40 / 0.55 with room
    to spare, which `test_each_sweep_clears_its_thresholds_by_a_real_margin`
    checks rather than assumes.

    NOTE, a property of hand geometry rather than of this generator: a full index
    pinch also brings the thumb within 0.21 hand-sizes of the MIDDLE fingertip,
    below `Snapon`. So the index sweep engages the middle latch too near its peak.
    That is what a real hand does, and it means one ramp exercises two latches -
    but it also means the middle-finger state is not a clean discriminator between
    a pinch and a snap at these defaults. For the threshold-feel session; the
    arithmetic is not what is in question.
    """
    def sequence(phase: float) -> list[HandPose | None]:
        pose = pinching(triangle(phase), palm_x=_CENTRE_X, palm_y=_CENTRE_Y,
                        size=_SIZE)
        pose.pinch_finger = finger
        return [pose, None]
    sequence.__doc__ = "One hand, thumb sweeping to the %s tip and back." % finger
    return sequence


def _ramp_clap(phase: float) -> list[HandPose | None]:
    """Two open hands, palms sweeping together and apart.

    Sweeps `hands_distance` over 1.60 -> 0.30 -> 1.60 hand-sizes, straddling
    `Togetheron` 0.60 / `Togetheroff` 0.95. The hands genuinely overlap at the
    peak, which is what a clap is.
    """
    ratio = _APART_RATIO + (_TOGETHER_RATIO - _APART_RATIO) * triangle(phase)
    # ratio is in hand-size units and hands_distance divides by mean hand size,
    # so the separation in normalised units is ratio * size. Half each side of
    # centre, so `hands_center_x` stays at 0.5 throughout and a drifting centre
    # would show up as an obvious fault rather than as noise.
    half = ratio * _SIZE / 2.0
    return [
        open_hand(palm_x=_CENTRE_X - half, palm_y=_CENTRE_Y, size=_SIZE),
        open_hand(palm_x=_CENTRE_X + half, palm_y=_CENTRE_Y, size=_SIZE),
    ]


# The dead-band oscillation, as pinch amounts. SOLVED, not guessed: these are
# the amounts for which derive() reports `h0_pinch_index` = 0.29 and 0.44, found
# by bisection against the real generator and pinned by
# test_deadband_crosses_on_but_never_off.
#
# 0.29 is below `Pinchon` 0.35. 0.44 is INSIDE the dead band 0.35..0.50 and never
# reaches `Pinchoff` 0.50. That is the whole point: a Schmitt trigger engages on
# the first dip and then holds for ever, because nothing ever re-arms it. A plain
# comparator toggles on every dip. One number - the fire count - separates them.
_DEADBAND_LOW_PINCH: Final = 0.6203      # -> pinch_index 0.44, inside the band
_DEADBAND_HIGH_PINCH: Final = 0.7497     # -> pinch_index 0.29, below Pinchon


def _deadband(phase: float) -> list[HandPose | None]:
    """One hand, pinching just deep enough to engage and releasing only into the
    dead band. The test that a latch is a latch.

    Starts at the shallow end - inside the dead band, having never engaged - so
    the first frames also check the OTHER direction of the hold: a state that is
    released must stay released while the distance sits in the band.
    """
    amount = _DEADBAND_LOW_PINCH + (
        _DEADBAND_HIGH_PINCH - _DEADBAND_LOW_PINCH) * triangle(phase)
    return [pinching(amount, palm_x=_CENTRE_X, palm_y=_CENTRE_Y, size=_SIZE), None]


def _ramp_both(phase: float) -> list[HandPose | None]:
    """BOTH hands pinching together, held far apart. The only h1 coverage there is.

    Every other sequence leaves slot 1 empty or open, so hand 1's latches - a
    separate five-channel column through the same operators - were never once
    exercised. They share the operators, so a fault would have to be in the
    channel WIRING rather than in the logic; that is exactly the class of fault
    the working-name scheme in tools/td_add_latches.py exists to prevent, which
    makes it the class worth testing.

    Held `_APART_RATIO` apart so `hands_together` stays released throughout: two
    hands pinching is not a clap, and if this fired the clap latch the sequence
    would be testing two things at once and diagnosing neither.
    """
    amount = triangle(phase)
    half = _APART_RATIO * _SIZE / 2.0
    return [
        pinching(amount, palm_x=_CENTRE_X - half, palm_y=_CENTRE_Y, size=_SIZE),
        pinching(amount, palm_x=_CENTRE_X + half, palm_y=_CENTRE_Y, size=_SIZE),
    ]


# How far across the frame a swipe travels, and where it starts. Wide enough that
# the motion is unambiguous and the palm stays in frame at both ends.
_SWIPE_FROM: Final = 0.15
_SWIPE_TO: Final = 0.85


def _swipe(phase: float) -> list[HandPose | None]:
    """One open hand traversing the frame and coming back. SUSTAINED motion.

    Distinct from the pinch ramps in the one way that matters to anything
    velocity-based: the hand moves in ONE direction for half the cycle. The pinch
    sweeps oscillate a fingertip, and a smoothed signed velocity of an oscillation
    averages toward zero - measured, `|dx_hat|` peaked at 0.037 units/s even on
    0.6-second sweeps, so the adaptive filter barely opened up and the swipe
    thresholds would have had nothing to fire on. A traverse produces the velocity
    a real gesture produces.

    At the default period of 2 s that is 0.70 units in 1 s, so roughly 0.7 units
    per second, and `--period` scales it: `--period 0.7` gives about 2 units/s,
    which is a fast deliberate swipe.
    """
    across = _SWIPE_FROM + (_SWIPE_TO - _SWIPE_FROM) * triangle(phase)
    return [open_hand(palm_x=across, palm_y=_CENTRE_Y, size=_SIZE), None]


def _crossing(phase: float) -> list[HandPose | None]:
    """A LEFT and a RIGHT hand crossing over. The slot-assignment test.

    Two hands swapping sides is exactly when Vision reorders its observations, and
    a wrong answer there looks perfectly correct on screen - the landmarks still
    track two hands, they are just attributed to the wrong one. So the hands are
    given real chirality, and `tools/send_synthetic.py --unstable` reverses the
    order every frame to simulate the reordering.

    The observable is `h0_chirality`: with assignment on it must read +1 (right)
    for the whole sweep; with it off it alternates. Nothing else in the channel
    set distinguishes the two cases so cleanly.

    They also differ in SIZE, which is what lets a test follow a hand between
    slots - the same trick tests/test_slots.py uses.
    """
    right_x = _CENTRE_X + 0.2 - 0.4 * triangle(phase)
    left_x = _CENTRE_X - 0.2 + 0.4 * triangle(phase)
    return [
        open_hand(palm_x=right_x, palm_y=_CENTRE_Y, size=_SIZE,
                  chirality=CHIRALITY_RIGHT),
        open_hand(palm_x=left_x, palm_y=_CENTRE_Y, size=_SIZE * 1.2,
                  chirality=CHIRALITY_LEFT),
    ]


def _static_open(_phase: float) -> list[HandPose | None]:
    """Two open hands, held still and far apart. The resting baseline.

    Every latch should read 0 and every pulse should stay 0 for as long as this
    runs. A latch that engages here is engaging on nothing.
    """
    half = _APART_RATIO * _SIZE / 2.0
    return [
        open_hand(palm_x=_CENTRE_X - half, palm_y=_CENTRE_Y, size=_SIZE),
        open_hand(palm_x=_CENTRE_X + half, palm_y=_CENTRE_Y, size=_SIZE),
    ]


def _absent(_phase: float) -> list[HandPose | None]:
    """No hands at all - every joint at (0, 0).

    This is the trap docs/ATTRIBUTES.md opens with: every distance reads 0, which
    is below every engage threshold, so a validity gate that is missing or
    applied only to the pulses fires pinch, snap and clap simultaneously here.
    Cheap to send, and the single most valuable thing to send.
    """
    return [None, None]


SEQUENCES: Final[dict[str, SequenceFn]] = {
    # `ramp` and `snap` keep their names: they are the index and middle sweeps,
    # and every verification run and journal entry so far refers to them.
    "ramp": _ramp_to_finger("index"),
    "snap": _ramp_to_finger("middle"),
    "ring": _ramp_to_finger("ring"),
    "little": _ramp_to_finger("little"),
    "clap": _ramp_clap,
    "swipe": _swipe,
    "crossing": _crossing,
    "deadband": _deadband,
    "both": _ramp_both,
    "open": _static_open,
    "absent": _absent,
}


# ---------------------------------------------------------------------------
# Turning a sequence into frames
# ---------------------------------------------------------------------------
def frames(sequence: SequenceFn, fps: float, period_s: float,
           cycles: float, seq_start: int = 1) -> Iterator[LandmarkFrame]:
    """Yield LandmarkFrames for `cycles` cycles of `sequence`.

    Contract: `fps` frames per cycle-second, `period_s` seconds per cycle. The
              yielded frames carry `captured_at` in seconds from zero, so a
              caller that sleeps between them reproduces the intended rate and a
              caller that does not still gets a coherent timeline.
    Why an iterator: a long sweep at 60 fps is thousands of frames, and a test
              that only needs the first twenty should not pay for the rest.
    """
    if fps <= 0.0 or period_s <= 0.0:
        raise ValueError("fps and period_s must be positive: %r, %r" % (fps, period_s))
    total = round(fps * period_s * cycles)
    frames_per_cycle = fps * period_s
    for index in range(total):
        # Phase wraps per cycle. Modulo on the frame index rather than on a
        # running float, so cycle 40 is as exact as cycle 1 - accumulating
        # `phase += step` drifts, and a drifting sweep silently stops reaching
        # its endpoints, which is exactly the range the thresholds live in.
        phase = (index % frames_per_cycle) / frames_per_cycle
        yield synthetic_frame(sequence(phase), seq=seq_start + index,
                              captured_at=index / fps)
