"""Pins the joint table: its contents, and - the part that nearly escaped - its ORDER.

Why this file exists. The M0/M2a review mutation-tested the table and found that
all three of the failures DESIGN.md 2.3 explicitly warns about passed the entire
suite silently:

  * swapping thumb_ip's VNHLKTIP with index_tip's VNHLKITIP (one character apart)
  * moving the wrist from index 0 to index 20
  * changing the pinky's internal 'P' to an 'L'

`types._self_check()` only tests that the codes are *distinct*, which all three
mutations preserve. The start-up check planned for engine.py - comparing each
row's code against getattr(Vision, prefix + row.suffix) - catches the first and
third, but CANNOT catch a reorder, because a reordered row is still internally
consistent.

Order is the dangerous one. JOINTS order IS the channel order: h0_lm00 is the
wrist. Reordering it rewires every landmark reference in a TD project with no
error raised anywhere, at any layer, ever. So it gets pinned literally, here,
where a diff to the table forces a diff to the test.

Ref: DESIGN.md 2.3, 6.2.
"""

from __future__ import annotations

import pytest

from appletd.types import (
    JOINT_CODES,
    JOINT_INDEX_BY_CODE,
    JOINT_NAMES,
    JOINTS,
    JOINTS_GROUP_ALL_CODE,
    N_JOINTS,
)

# The table as it must be, written out in full rather than derived from JOINTS.
# Deriving it from the thing under test would prove only that the code equals
# itself. VERIFIED against pyobjc 12.2.2 / macOS 26.5.2 on 2026-08-20 and
# against the table in DESIGN.md 2.3.
EXPECTED_NAMES = (
    "wrist",
    "thumb_cmc", "thumb_mp", "thumb_ip", "thumb_tip",
    "index_mcp", "index_pip", "index_dip", "index_tip",
    "middle_mcp", "middle_pip", "middle_dip", "middle_tip",
    "ring_mcp", "ring_pip", "ring_dip", "ring_tip",
    "little_mcp", "little_pip", "little_dip", "little_tip",
)
EXPECTED_CODES = (
    "VNHLKWRI",
    "VNHLKTCMC", "VNHLKTMP", "VNHLKTIP", "VNHLKTTIP",
    "VNHLKIMCP", "VNHLKIPIP", "VNHLKIDIP", "VNHLKITIP",
    "VNHLKMMCP", "VNHLKMPIP", "VNHLKMDIP", "VNHLKMTIP",
    "VNHLKRMCP", "VNHLKRPIP", "VNHLKRDIP", "VNHLKRTIP",
    "VNHLKPMCP", "VNHLKPPIP", "VNHLKPDIP", "VNHLKPTIP",
)


def test_names_and_order_are_exactly_as_specified() -> None:
    """The whole tuple, in order. A reorder fails here and nowhere else."""
    assert JOINT_NAMES == EXPECTED_NAMES


def test_codes_and_order_are_exactly_as_specified() -> None:
    assert JOINT_CODES == EXPECTED_CODES


def test_the_two_one_character_apart_codes_are_not_swapped() -> None:
    """thumb_ip is VNHLKTIP; index_tip is VNHLKITIP. Called out separately
    because a swap reads as correct in a diff and is the single most likely
    transcription error in this table (DESIGN.md 2.3)."""
    by_name = {j.name: j.code for j in JOINTS}
    assert by_name["thumb_ip"] == "VNHLKTIP"
    assert by_name["index_tip"] == "VNHLKITIP"


def test_little_finger_is_internally_p_for_pinky() -> None:
    """All four little-finger codes use P, not L. The 'L' spelling is plausible,
    wrong, and would return no points for that finger at all."""
    for joint in JOINTS:
        if joint.name.startswith("little_"):
            assert joint.code.startswith("VNHLKP"), joint


def test_wrist_is_first_and_index_lookup_agrees() -> None:
    """h0_lm00 is the wrist. Downstream TD projects index on this."""
    assert JOINTS[0].name == "wrist"
    assert JOINT_INDEX_BY_CODE["VNHLKWRI"] == 0
    assert JOINT_INDEX_BY_CODE[JOINT_CODES[N_JOINTS - 1]] == N_JOINTS - 1


def test_fingers_run_root_to_tip_in_blocks_of_four() -> None:
    """Wrist, then five fingers of four joints each, thumb to little.

    Checked structurally rather than by name so that this still means something
    if the naming convention ever changes: after the wrist, the table must be
    five contiguous groups of four sharing a prefix, each ending in a tip.
    """
    assert len(JOINTS) == 1 + 5 * 4
    for finger_i, prefix in enumerate(("thumb", "index", "middle", "ring", "little")):
        block = JOINTS[1 + finger_i * 4: 1 + finger_i * 4 + 4]
        assert all(j.name.startswith(prefix + "_") for j in block), block
        assert block[-1].name == prefix + "_tip", block


def test_table_matches_the_live_framework() -> None:
    """Cross-check every row against Vision itself.

    Skipped rather than failed where pyobjc is unavailable, so the pure core's
    tests still run anywhere; on the target machine this is the check that would
    catch Apple renaming or re-coding a joint under us. This is also the check
    engine.py performs at start-up - duplicated here so it fails in CI-time
    rather than at first camera frame.
    """
    Vision = pytest.importorskip("Vision", reason="pyobjc not installed")
    prefix = "VNHumanHandPoseObservationJointName"
    for joint in JOINTS:
        constant = getattr(Vision, prefix + joint.suffix, None)
        assert constant is not None, "no %s%s in Vision" % (prefix, joint.suffix)
        assert str(constant) == joint.code, (
            "%s: table says %s, framework says %s" % (joint.name, joint.code, constant))
    assert str(Vision.VNHumanHandPoseObservationJointsGroupNameAll) == JOINTS_GROUP_ALL_CODE
