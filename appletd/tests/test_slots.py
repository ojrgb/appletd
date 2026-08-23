"""Slot assignment: does `h0` stay the same physical hand?

The test that matters is the crossing one. Two hands swapping sides is exactly
when Vision reorders its observations, and it is the case where a wrong answer
looks completely correct on screen - the landmarks still track two hands, they are
just attributed to the wrong one.

So most of these assert on IDENTITY rather than on position: each synthetic hand is
given a distinguishing size, and the tests follow that size through the slots. A
test that only checked "slot 0 has a hand in it" would pass on the bug.
"""

from __future__ import annotations

import pytest

from appletd.slots import (
    SLOT_MODE_CHIRALITY,
    SLOT_MODE_OFF,
    SlotAssigner,
    assign_slots,
)
from appletd.synth import open_hand, synthetic_frame
from appletd.types import (
    BLANK_HAND,
    CHIRALITY_LEFT,
    CHIRALITY_RIGHT,
    CHIRALITY_UNKNOWN,
    MAX_HANDS,
    Hand,
    LandmarkFrame,
)

# Distinguishing sizes, so a hand can be identified after it has been moved
# between slots. Nothing in the code reads size, which is what makes it usable as
# a label.
RIGHT_SIZE = 0.11
LEFT_SIZE = 0.13

BLANKS = (BLANK_HAND,) * MAX_HANDS


def _frame(hands: tuple[Hand, ...], seq: int = 1) -> LandmarkFrame:
    """A frame carrying exactly these hands, in exactly this order.

    Built directly rather than through `synthetic_frame`, because the whole point
    is to control the ORDER Vision would have handed them over in - which is the
    variable under test.
    """
    return LandmarkFrame(seq=seq, captured_at=seq / 30.0, width=1280, height=720,
                         hands=hands)


def _hand(chirality: int, x: float, size: float) -> Hand:
    """One synthetic hand with a known chirality, position and identifying size."""
    frame = synthetic_frame([open_hand(palm_x=x, palm_y=0.5, size=size,
                                      chirality=chirality), None])
    return frame.hands[0]


def _right(x: float = 0.7) -> Hand:
    return _hand(CHIRALITY_RIGHT, x, RIGHT_SIZE)


def _left(x: float = 0.3) -> Hand:
    return _hand(CHIRALITY_LEFT, x, LEFT_SIZE)


def _identity(hand: Hand) -> str:
    """Which hand is this, by its label rather than by what slot it is in."""
    if not hand.found:
        return "blank"
    size = round(abs(hand.joints[9].y - hand.joints[0].y), 4)
    # joints[9] is middle_mcp, joints[0] the wrist: their distance IS the size the
    # generator was asked for, so it survives being moved between slots.
    if abs(size - RIGHT_SIZE) < 1e-3:
        return "right"
    if abs(size - LEFT_SIZE) < 1e-3:
        return "left"
    return "unknown(%.4f)" % size


def _slots(hands: tuple[Hand, ...]) -> list[str]:
    return [_identity(hand) for hand in hands]


# ---------------------------------------------------------------------------
# The clean case: chirality decides
# ---------------------------------------------------------------------------
def test_right_hand_goes_to_slot_zero_left_to_slot_one() -> None:
    assigned = assign_slots((_right(), _left()), BLANKS)
    assert _slots(assigned) == ["right", "left"]


def test_visions_order_does_not_matter() -> None:
    """The whole point: the same two hands, handed over the other way round."""
    assert _slots(assign_slots((_left(), _right()), BLANKS)) == ["right", "left"]
    assert _slots(assign_slots((_right(), _left()), BLANKS)) == ["right", "left"]


def test_hands_crossing_over_never_swap_slots() -> None:
    """The case this module exists for, and the one that hides the bug.

    Two hands cross the frame, and Vision hands them back in a DIFFERENT order
    every frame - which is what it really does, and is simulated here by
    alternating. Without assignment, `h0` alternates between the two physical
    hands and every temporal channel measures a hand that changed identity.
    """
    assigner = SlotAssigner(SLOT_MODE_CHIRALITY)
    seen = []
    for step in range(20):
        # The right hand travels right-to-left, the left hand left-to-right: they
        # cross in the middle, which is when a proximity-only tracker gets it wrong.
        progress = step / 19.0
        right = _hand(CHIRALITY_RIGHT, 0.7 - 0.4 * progress, RIGHT_SIZE)
        left = _hand(CHIRALITY_LEFT, 0.3 + 0.4 * progress, LEFT_SIZE)
        pair = (right, left) if step % 2 else (left, right)   # Vision reorders
        seen.append(_slots(assigner.assign(_frame(pair, step)).hands))
    assert all(row == ["right", "left"] for row in seen), seen


def test_without_assignment_the_crossing_really_does_swap() -> None:
    """The negative control, so the test above is known to be testing something.

    With the mode off, the alternating order passes straight through - which is the
    bug, reproduced deliberately. If this ever stopped swapping, the test above
    would be passing for the wrong reason.
    """
    assigner = SlotAssigner(SLOT_MODE_OFF)
    first = []
    for step in range(6):
        right, left = _right(), _left()
        pair = (right, left) if step % 2 else (left, right)
        first.append(_slots(assigner.assign(_frame(pair, step)).hands)[0])
    assert set(first) == {"right", "left"}, (
        "with assignment off, slot 0 should alternate: got %r" % first)


# ---------------------------------------------------------------------------
# One hand, which is where the behavioural change bites
# ---------------------------------------------------------------------------
def test_a_lone_right_hand_fills_slot_zero_and_leaves_slot_one_blank() -> None:
    assigned = assign_slots((_right(), BLANK_HAND), BLANKS)
    assert _slots(assigned) == ["right", "blank"]


def test_a_lone_LEFT_hand_leaves_slot_zero_BLANK() -> None:
    """The consequence worth knowing about, asserted so it cannot drift silently.

    "h0 is always the right hand" means a single left hand puts NOTHING in h0:
    every `h0_*` channel reads zero and `h1_*` carries the hand. That is correct
    and it will surprise anyone reading `h0` as "the hand", which is why slot
    assignment is a toggle rather than always on.
    """
    assigned = assign_slots((_left(), BLANK_HAND), BLANKS)
    assert _slots(assigned) == ["blank", "left"]


def test_no_hands_gives_two_blanks() -> None:
    assert assign_slots(BLANKS, BLANKS) == BLANKS


# ---------------------------------------------------------------------------
# When chirality cannot decide
# ---------------------------------------------------------------------------
def test_two_hands_of_the_SAME_chirality_fall_back_to_proximity() -> None:
    """Vision does report two rights sometimes. Then handedness says nothing.

    Both hands are RIGHT, so the partition is meaningless and the proximity
    fallback has to keep each hand in the slot it was in. The hands are handed over
    in the reversed order to prove the fallback is doing the work rather than the
    input order.
    """
    near_zero = _hand(CHIRALITY_RIGHT, 0.20, RIGHT_SIZE)
    near_one = _hand(CHIRALITY_RIGHT, 0.80, LEFT_SIZE)
    previous = (near_zero, near_one)
    assigned = assign_slots((near_one, near_zero), previous)
    assert _slots(assigned) == ["right", "left"], (
        "proximity should have put each hand back where it was")


def test_unknown_chirality_falls_back_to_proximity() -> None:
    a = _hand(CHIRALITY_UNKNOWN, 0.20, RIGHT_SIZE)
    b = _hand(CHIRALITY_UNKNOWN, 0.80, LEFT_SIZE)
    assigned = assign_slots((b, a), (a, b))
    assert _slots(assigned) == ["right", "left"]


def test_one_confident_and_one_unknown_is_not_confident_enough() -> None:
    """`all` and not `any`: a partition needs BOTH hands placed, not one.

    Placing the confident hand and guessing the other would be worse than the
    fallback, because the guess would look like an assignment.
    """
    right = _hand(CHIRALITY_RIGHT, 0.20, RIGHT_SIZE)
    unknown = _hand(CHIRALITY_UNKNOWN, 0.80, LEFT_SIZE)
    # With no history the fallback keeps Vision's order, so the confident hand does
    # NOT get moved to slot 0 - which is the point being pinned.
    assigned = assign_slots((unknown, right), BLANKS)
    assert _slots(assigned) == ["left", "right"]


def test_the_lone_survivor_stays_in_its_own_slot() -> None:
    """One hand drops out; the other must not hop into the empty slot.

    Both are UNKNOWN so chirality cannot help. The remaining hand is nearer to slot
    1's old position, so it belongs in slot 1 - and a naive "first found goes to
    slot 0" would move it.
    """
    a = _hand(CHIRALITY_UNKNOWN, 0.20, RIGHT_SIZE)
    b = _hand(CHIRALITY_UNKNOWN, 0.80, LEFT_SIZE)
    assigned = assign_slots((b, BLANK_HAND), (a, b))
    assert _slots(assigned) == ["blank", "left"]


# ---------------------------------------------------------------------------
# The assigner's own state
# ---------------------------------------------------------------------------
def test_reset_forgets_the_previous_frame() -> None:
    """Otherwise a restart matches frame one against the last session's last frame.

    That is the stale-state class of bug the M2b review found three instances of,
    so it gets a test rather than a comment.
    """
    a = _hand(CHIRALITY_UNKNOWN, 0.20, RIGHT_SIZE)
    b = _hand(CHIRALITY_UNKNOWN, 0.80, LEFT_SIZE)
    assigner = SlotAssigner(SLOT_MODE_CHIRALITY)

    assigner.assign(_frame((a, b)))
    assigner.reset()
    # After a reset there is no history, so the fallback keeps Vision's order
    # instead of matching against a session that has ended.
    assert _slots(assigner.assign(_frame((b, BLANK_HAND))).hands) == ["left", "blank"]


def test_mode_off_returns_the_very_same_object() -> None:
    """Not merely equal - the same object, so the common path allocates nothing."""
    frame = synthetic_frame([open_hand(), open_hand()])
    assigner = SlotAssigner(SLOT_MODE_OFF)
    assert assigner.assign(frame) is frame


def test_an_already_correct_order_is_not_reallocated() -> None:
    frame = _frame((_right(), _left()))
    assert SlotAssigner(SLOT_MODE_CHIRALITY).assign(frame) is frame


def test_the_frame_scalars_survive_a_reorder() -> None:
    """A reordered frame must keep its seq, timestamp and dimensions.

    Rebuilding a frozen dataclass by hand is exactly where a field gets dropped,
    and `seq` going backwards is a defect the M3 review already caught once.
    """
    frame = LandmarkFrame(seq=4242, captured_at=12.5, width=1280, height=720,
                          hands=(_left(), _right()))
    out = SlotAssigner(SLOT_MODE_CHIRALITY).assign(frame)
    assert out.hands != frame.hands              # it really was reordered
    assert (out.seq, out.captured_at, out.width, out.height) == (4242, 12.5,
                                                                 1280, 720)


def test_an_unknown_mode_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ValueError):
        SlotAssigner("sometimes")
    with pytest.raises(ValueError):
        assign_slots(BLANKS, BLANKS, mode="sometimes")


def test_the_result_is_always_exactly_max_hands_long() -> None:
    """The fixed length is what makes the channel contract unconditional."""
    for hands in ((_right(), _left()), (_left(), BLANK_HAND), BLANKS,
                  (_right(), _right())):
        assert len(assign_slots(hands, BLANKS)) == MAX_HANDS
