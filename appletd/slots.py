"""Which physical hand goes in which slot, and keeping it there.

Vision hands back observations with NO tracking ID and no stable order, so `h0` swaps
to the other physical hand whenever Vision reorders them. That is the most dangerous
defect this system can have, because it looks fine on screen: the landmarks still
track two hands, they are just attributed to the wrong one. Everything with memory
then measures a hand that changed identity mid-gesture.

The fix is nearly free: `VNHumanHandPoseObservation.chirality` already reports left or
right, so the slot is chosen by chirality rather than by arrival order. Where
chirality is unavailable, a proximity fallback matches against the previous frame.

Thread: pure functions plus one small carried state. Safe anywhere.
Ref: DESIGN.md 6.3.
"""

from __future__ import annotations

import math
from typing import Final

from appletd.types import (
    BLANK_HAND,
    CHIRALITY_LEFT,
    CHIRALITY_RIGHT,
    MAX_HANDS,
    Hand,
    LandmarkFrame,
)

# The two modes the toggle selects between.
#
# `chirality` is the one that fixes the problem. `off` is Vision's own order,
# preserved exactly, so the toggle is a true A/B rather than one path being a
# slightly different version of the other - which matters when the question being
# asked is "is the assignment doing something wrong or is the tracker?".
SLOT_MODE_OFF: Final = "off"
SLOT_MODE_CHIRALITY: Final = "chirality"
SLOT_MODES: Final = (SLOT_MODE_OFF, SLOT_MODE_CHIRALITY)

# Which chirality each slot belongs to, when assignment is on. Slot 0 is the right
# hand because that is what was asked for; nothing else depends on the choice.
SLOT_CHIRALITY: Final = (CHIRALITY_RIGHT, CHIRALITY_LEFT)

# INVARIANT: this module is written for exactly two slots. The chirality partition
# has no meaning for a third hand, and the proximity fallback below assumes it can
# enumerate both pairings by hand. Asserted rather than left to be discovered.
if MAX_HANDS != 2:                                          # pragma: no cover
    raise ImportError("slots.py assumes MAX_HANDS == 2, found %d" % MAX_HANDS)


def _wrist(hand: Hand) -> tuple[float, float]:
    """The wrist position. JOINT_NAMES[0] is the wrist - see types.py."""
    joint = hand.joints[0]
    return float(joint.x), float(joint.y)


def _distance(a: Hand, b: Hand) -> float:
    ax, ay = _wrist(a)
    bx, by = _wrist(b)
    return math.hypot(ax - bx, ay - by)


def _by_proximity(found: list[Hand], previous: tuple[Hand, ...]) -> list[Hand | None]:
    """Assign by wrist distance to the previous frame's slots. The fallback.

    Used only when chirality cannot decide - both hands reported the same
    handedness, or Vision reported UNKNOWN. With two slots there are exactly two
    pairings, so this enumerates both and takes the cheaper one rather than
    reaching for an assignment algorithm.

    Why wrist and not palm: the palm is a mean of six joints and is steadier, but
    it is derived downstream in `derive.py` and this module runs before any of that
    exists. The wrist is in the frame contract.
    """
    slots: list[Hand | None] = [None, None]
    occupied = [index for index in range(MAX_HANDS) if previous[index].found]

    if not occupied or not found:
        # Nothing to match against: keep Vision's order, which is no worse than
        # inventing an assignment from nothing.
        for index, hand in enumerate(found[:MAX_HANDS]):
            slots[index] = hand
        return slots

    if len(found) == 1:
        # One hand: put it in whichever occupied slot it is closer to. This is what
        # stops a single remaining hand hopping slots when the other one drops out.
        hand = found[0]
        best = min(occupied, key=lambda index: _distance(hand, previous[index]))
        slots[best] = hand
        return slots

    # Two hands, two pairings. Straight is (found[0] -> 0, found[1] -> 1); crossed
    # is the other. Only distances to slots that were actually occupied count, so a
    # blank previous slot never attracts a hand.
    def cost(mapping: tuple[int, int]) -> float:
        total = 0.0
        for hand_index, slot_index in enumerate(mapping):
            if previous[slot_index].found:
                total += _distance(found[hand_index], previous[slot_index])
        return total

    straight, crossed = (0, 1), (1, 0)
    mapping = straight if cost(straight) <= cost(crossed) else crossed
    for hand_index, slot_index in enumerate(mapping):
        slots[slot_index] = found[hand_index]
    return slots


def assign_slots(hands: tuple[Hand, ...], previous: tuple[Hand, ...],
                 mode: str = SLOT_MODE_CHIRALITY) -> tuple[Hand, ...]:
    """Reorder `hands` into slots. Pure: same inputs, same output, no state.

    Contract: `hands` and the result are both exactly MAX_HANDS long, padded with
              the shared BLANK_HAND - the fixed length is what makes the channel
              contract unconditionally true (DESIGN.md 6.2). `previous` is the
              PREVIOUS frame's already-assigned hands, used only by the proximity
              fallback.
    Why:      see the module docstring. The short version is that Vision has no
              tracking IDs, so without this `h0` changes identity between frames.
    """
    if mode not in SLOT_MODES:
        raise ValueError("unknown slot mode %r, expected one of %r"
                         % (mode, SLOT_MODES))
    if mode == SLOT_MODE_OFF:
        return hands

    found = [hand for hand in hands if hand.found]
    if not found:
        return (BLANK_HAND,) * MAX_HANDS

    chiralities = [hand.chirality for hand in found]
    # DISTINCT and CONFIDENT, which is the condition DESIGN.md 6.3 names. Both
    # terms matter: two hands both reported RIGHT tell us nothing about which is
    # which, and UNKNOWN tells us nothing at all. Either way the answer is the
    # fallback rather than a guess dressed up as an assignment.
    confident = all(c in SLOT_CHIRALITY for c in chiralities)
    distinct = len(set(chiralities)) == len(chiralities)

    if confident and distinct:
        slots: list[Hand | None] = [None] * MAX_HANDS
        for hand in found:
            slots[SLOT_CHIRALITY.index(hand.chirality)] = hand
    else:
        slots = _by_proximity(found, previous)

    return tuple(hand if hand is not None else BLANK_HAND for hand in slots)


class SlotAssigner:
    """Holds the previous assignment so the proximity fallback has something to
    match against. One per capture session.

    Thread: whichever thread owns the frame sequence - the capture queue, in the
            engine. Not thread-safe, and does not need to be: there is exactly one
            producer of frames per engine, and it is the same thread every time.
    Why a class: `assign_slots` is pure and testable on its own; the single piece
            of state that a fallback needs lives here, where it can be reset with
            the session rather than surviving a stop/start like a module global
            would.
    """

    __slots__ = ("_previous", "mode")

    def __init__(self, mode: str = SLOT_MODE_CHIRALITY) -> None:
        if mode not in SLOT_MODES:
            raise ValueError("unknown slot mode %r, expected one of %r"
                             % (mode, SLOT_MODES))
        self.mode = mode
        self._previous: tuple[Hand, ...] = (BLANK_HAND,) * MAX_HANDS

    def reset(self) -> None:
        """Forget the previous frame. Called when a session starts.

        Without this, a restart would match the first frame of the new session
        against the last frame of the old one - which is exactly the class of
        stale-state bug it turned out three of.
        """
        self._previous = (BLANK_HAND,) * MAX_HANDS

    def assign(self, frame: LandmarkFrame) -> LandmarkFrame:
        """One frame in, the same frame with its hands in the right slots.

        Returns the frame UNCHANGED when the mode is off or the order already
        matches, so the common case allocates nothing - `LandmarkFrame` is frozen,
        so returning it as-is is safe.
        """
        if self.mode == SLOT_MODE_OFF:
            return frame
        assigned = assign_slots(frame.hands, self._previous, self.mode)
        self._previous = assigned
        if assigned == frame.hands:
            return frame
        return LandmarkFrame(seq=frame.seq, captured_at=frame.captured_at,
                             width=frame.width, height=frame.height,
                             hands=assigned)
