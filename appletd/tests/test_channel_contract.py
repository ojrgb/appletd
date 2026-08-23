"""Tests for the CHOP channel contract (DESIGN.md 6.2).

The property under test is not "the names are pretty", it is that the channel
list is FIXED. A CHOP whose channels appear and disappear breaks every
downstream reference in a TD project silently, at the moment they vanish.

Extended in milestone 4 to cover the CHOP actually writing these values; for
now it pins the list itself, which is what the engine and the CHOP have to
agree on.
"""

from __future__ import annotations

from appletd.types import (
    BLANK_HAND,
    MAX_HANDS,
    N_CHANNELS,
    N_JOINTS,
    Confidence,
    Joint,
    NormX,
    NormY,
    blank_frame,
    channel_names,
    median_joint_confidence,
)


def test_channel_count_is_137() -> None:
    """3 frame scalars + 2 hands * (4 hand scalars + 21 joints * 3) = 137.

    Hardcoded rather than recomputed from the constants, so that changing
    MAX_HANDS or N_JOINTS fails here loudly instead of quietly redefining a
    contract that TD projects are wired against.

    Was 135 until h<i>_conf_median joined the per-hand scalars, because Vision's
    own observation confidence turned out to be a measured constant (DESIGN.md
    2.6) and the channel DESIGN.md 6.2 told everyone to gate on was therefore
    carrying no information at all.
    """
    assert MAX_HANDS == 2
    assert N_JOINTS == 21
    assert N_CHANNELS == 137
    assert len(channel_names()) == 137


def test_channel_names_are_unique_and_ordered() -> None:
    names = channel_names()
    assert len(set(names)) == len(names)
    # Frame scalars first: downstream reads them to decide whether to trust the
    # rest of the frame at all.
    assert names[:3] == ("n_hands", "seq", "age_ms")
    # Then each hand as a contiguous block, so a TD selector can grab one hand
    # by name pattern.
    assert names[3:7] == ("h0_found", "h0_score", "h0_conf_median", "h0_chirality")
    assert names[7:10] == ("h0_wrist_x", "h0_wrist_y", "h0_wrist_conf")
    assert names[-3:] == ("h1_little_tip_x", "h1_little_tip_y", "h1_little_tip_conf")


def test_joints_are_named_not_numbered() -> None:
    """Channel names answer "which joint is this" without a lookup table.

    The wire format used to emit h0_lm08_x and leave every consumer to
    translate. `h0_index_tip_x` costs ~400 bytes across the bundle and removes a
    translation layer from every downstream project (types.channel_names).
    """
    names = channel_names()
    assert "h0_index_tip_x" in names
    assert "h0_lm08_x" not in names
    assert "h1_little_tip_conf" in names
    # Every joint name from the table appears for both hands, with all three
    # values - so a renamed joint cannot silently drop a channel.
    from appletd.types import JOINT_NAMES
    for hand in range(MAX_HANDS):
        for joint in JOINT_NAMES:
            for value in ("x", "y", "conf"):
                assert "h%d_%s_%s" % (hand, joint, value) in names


def test_blank_frame_fills_every_slot() -> None:
    """An absent hand is zeros with all its channels present, never a gap."""
    frame = blank_frame()
    assert len(frame.hands) == MAX_HANDS
    for hand in frame.hands:
        assert hand.found is False
        # 0.0, not the 1.0 Vision reports for every real observation - an empty
        # slot must not read as a confident hand.
        assert hand.confidence == 0.0
        assert hand.conf_median == 0.0
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


def _joints(*confs: float) -> tuple[Joint, ...]:
    """N_JOINTS joints whose confidences are the given values, padded with 1.0."""
    values = list(confs) + [1.0] * (N_JOINTS - len(confs))
    return tuple(Joint(NormX(0.5), NormY(0.5), Confidence(c)) for c in values[:N_JOINTS])


def test_median_joint_confidence_is_robust_to_zeros() -> None:
    """The reason for median over mean, stated as a test.

    11.0% of real joint confidences arrive at exactly 0.0 (MEASURED, DESIGN.md
    2.6). A hand with a few occluded joints and seventeen good ones is a good
    hand, and the published score has to say so.
    """
    mostly_good = _joints(0.0, 0.0, 0.0, 0.0)      # 4 zeros, 17 at 1.0
    assert median_joint_confidence(mostly_good) == 1.0

    # Past half the joints, the median does move - which is correct: that is no
    # longer a hand we can see.
    mostly_bad = _joints(*([0.0] * 11))
    assert median_joint_confidence(mostly_bad) == 0.0


def test_median_joint_confidence_picks_the_middle_value() -> None:
    """N_JOINTS is odd, so there is a real middle element and no averaging."""
    ramp = tuple(
        Joint(NormX(0.5), NormY(0.5), Confidence(i / (N_JOINTS - 1)))
        for i in range(N_JOINTS)
    )
    assert median_joint_confidence(ramp) == 0.5


def test_median_joint_confidence_of_nothing_is_zero() -> None:
    """So a blank hand needs no special case at the call site."""
    assert median_joint_confidence(()) == 0.0
    assert median_joint_confidence(BLANK_HAND.joints) == 0.0
