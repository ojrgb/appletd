"""Tests for the CHOP channel contract (DESIGN.md 6.2).

The property under test is not "the names are pretty", it is that the channel
list is FIXED. A CHOP whose channels appear and disappear breaks every
downstream reference in a TD project silently, at the moment they vanish.

Extended in milestone 4 to cover the CHOP actually writing these values; for
now it pins the list itself, which is what the engine and the CHOP have to
agree on.
"""

from __future__ import annotations

from visionhands.types import (
    BLANK_HAND,
    MAX_HANDS,
    N_CHANNELS,
    N_JOINTS,
    blank_frame,
    channel_names,
)


def test_channel_count_is_135() -> None:
    """3 frame scalars + 2 hands * (3 hand scalars + 21 joints * 3) = 135.

    Hardcoded rather than recomputed from the constants, so that changing
    MAX_HANDS or N_JOINTS fails here loudly instead of quietly redefining a
    contract that TD projects are wired against.
    """
    assert MAX_HANDS == 2
    assert N_JOINTS == 21
    assert N_CHANNELS == 135
    assert len(channel_names()) == 135


def test_channel_names_are_unique_and_ordered() -> None:
    names = channel_names()
    assert len(set(names)) == len(names)
    # Frame scalars first: downstream reads them to decide whether to trust the
    # rest of the frame at all.
    assert names[:3] == ("n_hands", "seq", "age_ms")
    # Then each hand as a contiguous block, so a TD selector can grab one hand
    # by name pattern.
    assert names[3:6] == ("h0_found", "h0_score", "h0_chirality")
    assert names[6:9] == ("h0_lm00_x", "h0_lm00_y", "h0_lm00_conf")
    assert names[-3:] == ("h1_lm20_x", "h1_lm20_y", "h1_lm20_conf")


def test_joint_indices_are_zero_padded() -> None:
    """lm08 not lm8, so the names are fixed-width and sort correctly in TD."""
    names = channel_names()
    assert "h0_lm08_x" in names
    assert "h0_lm8_x" not in names
    assert "h1_lm20_conf" in names


def test_blank_frame_fills_every_slot() -> None:
    """An absent hand is zeros with all its channels present, never a gap."""
    frame = blank_frame()
    assert len(frame.hands) == MAX_HANDS
    for hand in frame.hands:
        assert hand.found is False
        assert hand.confidence == 0.0
        assert len(hand.joints) == N_JOINTS
        assert all(j.x == 0.0 and j.y == 0.0 and j.conf == 0.0 for j in hand.joints)


def test_blank_hand_is_shared_not_copied() -> None:
    """The no-hand path must allocate nothing per cook.

    BLANK_HAND is frozen, so sharing one instance across every empty slot on
    every frame is safe, and it keeps the CHOP's worst case (no hands at all,
    every cook) free of allocation.
    """
    frame = blank_frame()
    assert frame.hands[0] is BLANK_HAND
    assert frame.hands[1] is BLANK_HAND
