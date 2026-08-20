"""The data contract: what the engine produces and what the CHOP publishes.

This module is the one place both halves of the system agree on, so it is
deliberately the most boring file in the repo: frozen dataclasses, tuples of
strings, and a self-check at import. It imports nothing but the standard
library.

Thread: everything here is immutable, so any object defined by this module can
        be read from any thread with no lock at all. That is not a convenience,
        it is the mechanism the whole design rests on - see source.py and
        DESIGN.md 4.3. Freezing these classes is load-bearing; unfreezing one
        would silently reintroduce the torn-read the lock-free handoff exists to
        avoid.

Ref: DESIGN.md 6.1 (LandmarkFrame), 6.2 (channel contract), 2.3 (joint codes).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, NewType

# ---------------------------------------------------------------------------
# Units, as types.
#
# Vision reports positions NORMALISED, with the origin at the BOTTOM LEFT and y
# increasing upwards. TouchDesigner agrees - its TOP numpy arrays are bottom-up
# and its UVs are bottom-left - so nothing in this pipeline flips y (DESIGN.md
# 7). The failure mode this guards against is not a crash: a flipped hand looks
# entirely plausible and is simply upside down, which is why the frame of
# reference is in the type rather than in a comment somewhere.
#
# NewType costs nothing at runtime (NormX(0.5) is 0.5) and makes mypy reject
# passing a normalised value where a pixel value is expected, or a
# bottom-left-origin value where a top-left one is.
# ---------------------------------------------------------------------------
NormX = NewType("NormX", float)      # 0..1 across the frame, left to right
NormY = NewType("NormY", float)      # 0..1 up the frame, BOTTOM to top
Confidence = NewType("Confidence", float)   # 0..1, Vision's own estimate

# Chirality, as Vision reports it (MEASURED, DESIGN.md 2.3: it is an int, and it
# was correct for a right hand palm-to-camera). Named because -1/0/+1 in a
# channel is unreadable at the far end.
CHIRALITY_LEFT: Final = -1
CHIRALITY_UNKNOWN: Final = 0
CHIRALITY_RIGHT: Final = 1

# How many hands we track and publish. 2 because Vision's maximumHandCount is
# set to 2 and because the channel list must be fixed at 135 (see below); a
# third hand in shot is dropped, not appended.
MAX_HANDS: Final = 2

# Joints per hand. Fixed by Vision, not by us: the `All` joints group returns
# exactly these 21 (the per-finger groups return 4 each, 20 total - the wrist
# belongs to no finger). DESIGN.md 2.3.
N_JOINTS: Final = 21


# ---------------------------------------------------------------------------
# Joint identity.
#
# Three names for the same joint, and all three are needed:
#
#   name    ours. Used in logs, tests and the debug overlay.
#   suffix  the tail of the Vision constant,
#           VNHumanHandPoseObservationJointName<suffix>.
#   code    the constant's *value*, and the actual key in the dict that
#           recognizedPointsForJointsGroupName_error_ returns.
#
# The codes are recorded here so that this module stays free of pyobjc - td/
# needs the joint order to name channels and must not import Vision to get it.
# They are NOT trusted on that basis: engine.py verifies every code in this
# table against getattr(Vision, "VNHumanHandPoseObservationJointName" + suffix)
# at start-up and refuses to run if any disagrees. Hardcoding without that check
# would be reckless, because two of these differ by one character - ThumbIP is
# VNHLKTIP and IndexTip is VNHLKITIP - and the little finger is internally 'P'
# for pinky.
#
# VERIFIED 2026-08-20 against pyobjc 12.2.2 / macOS 26.5.2: all 21 present, all
# codes distinct, and identical to the table in DESIGN.md 2.3.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class JointSpec:
    name: str
    suffix: str      # Vision constant name, minus the long prefix
    code: str        # the constant's value = the dict key Vision hands back


JOINTS: Final[tuple[JointSpec, ...]] = (
    # The wrist first, then thumb to little finger, each root to tip. This order
    # IS the channel order (h0_lm00 is the wrist), so changing it silently
    # rewires every downstream reference in a TD project. Do not reorder.
    JointSpec("wrist", "Wrist", "VNHLKWRI"),
    JointSpec("thumb_cmc", "ThumbCMC", "VNHLKTCMC"),
    JointSpec("thumb_mp", "ThumbMP", "VNHLKTMP"),
    JointSpec("thumb_ip", "ThumbIP", "VNHLKTIP"),       # NOT IndexTip - one char apart
    JointSpec("thumb_tip", "ThumbTip", "VNHLKTTIP"),
    JointSpec("index_mcp", "IndexMCP", "VNHLKIMCP"),
    JointSpec("index_pip", "IndexPIP", "VNHLKIPIP"),
    JointSpec("index_dip", "IndexDIP", "VNHLKIDIP"),
    JointSpec("index_tip", "IndexTip", "VNHLKITIP"),    # NOT ThumbIP
    JointSpec("middle_mcp", "MiddleMCP", "VNHLKMMCP"),
    JointSpec("middle_pip", "MiddlePIP", "VNHLKMPIP"),
    JointSpec("middle_dip", "MiddleDIP", "VNHLKMDIP"),
    JointSpec("middle_tip", "MiddleTip", "VNHLKMTIP"),
    JointSpec("ring_mcp", "RingMCP", "VNHLKRMCP"),
    JointSpec("ring_pip", "RingPIP", "VNHLKRPIP"),
    JointSpec("ring_dip", "RingDIP", "VNHLKRDIP"),
    JointSpec("ring_tip", "RingTip", "VNHLKRTIP"),
    JointSpec("little_mcp", "LittleMCP", "VNHLKPMCP"),   # 'P' for pinky, internally
    JointSpec("little_pip", "LittlePIP", "VNHLKPPIP"),
    JointSpec("little_dip", "LittleDIP", "VNHLKPDIP"),
    JointSpec("little_tip", "LittleTip", "VNHLKPTIP"),
)

JOINT_NAMES: Final[tuple[str, ...]] = tuple(j.name for j in JOINTS)
JOINT_CODES: Final[tuple[str, ...]] = tuple(j.code for j in JOINTS)
# code -> index into a Hand's joints tuple. The engine's hot path does one dict
# lookup per joint per frame, so it is built once here rather than per frame.
JOINT_INDEX_BY_CODE: Final[dict[str, int]] = {j.code: i for i, j in enumerate(JOINTS)}

# The joints group to ask Vision for: one call returns all 21. MEASURED
# (DESIGN.md 2.3): the per-finger groups return 4 points each and would need
# five calls to cover the same ground, minus the wrist.
JOINTS_GROUP_ALL_CODE: Final = "VNIPOAll"


# ---------------------------------------------------------------------------
# The frame contract
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Joint:
    """One landmark. Normalised, bottom-left origin, exactly as Vision gave it.

    Contract: x,y are 0..1 with the origin BOTTOM LEFT (DESIGN.md 7). No
              conversion has been applied and none may be, before the CHOP.
    Why conf is here and not filtered out: Vision returns points for occluded
              joints with *lower confidence* rather than omitting them
              (MEASURED, DESIGN.md 2.4), so a missing joint is not how occlusion
              presents. Dropping low-confidence joints here would throw away the
              only signal downstream has. Gate on conf, never on presence.
    """

    x: NormX
    y: NormY
    conf: Confidence


@dataclass(frozen=True)
class Hand:
    """One hand: 21 joints in JOINTS order, plus Vision's own two judgements.

    Contract: joints always has exactly N_JOINTS entries, in JOINTS order, even
              when the hand is absent - see BLANK_HAND. Downstream code indexes
              this tuple positionally, so a short tuple is a contract violation
              rather than a missing-data case.
    """

    chirality: int          # CHIRALITY_LEFT / _UNKNOWN / _RIGHT
    confidence: Confidence  # the observation's own confidence, not a joint's
    joints: tuple[Joint, ...]
    # True when this slot holds a real detection. A slot can be held through a
    # brief dropout by the slot-assignment grace period (DESIGN.md 6.3), so
    # "there is a Hand object here" and "a hand was seen this frame" are
    # genuinely different questions and both get published.
    found: bool = True


@dataclass(frozen=True)
class LandmarkFrame:
    """One inference result, ready to cross the thread boundary.

    Thread: constructed on the capture thread, read on TD's main thread, shared
            by a single atomic reference rebind and no lock (DESIGN.md 4.3).
            Frozen, so a reader can never see a half-built one.
    Contract: hands has exactly MAX_HANDS entries, always, slot-assigned, with
            BLANK_HAND in the empty slots. Not "0..MAX_HANDS hands" as first
            drafted in DESIGN.md 6.1 - a fixed-length tuple is what makes the
            fixed channel list in 6.2 trivially true instead of conditional.
    INVARIANT: contains floats, ints, bools and tuples only. No pyobjc object
            may ever be referenced from here: it would then be released on
            whichever thread happened to drop the last reference, which is a
            crash waiting for a quiet afternoon.
    """

    seq: int                # monotonic, from the engine. Never resets while running.
    captured_at: float      # time.monotonic() at capture, for age_ms downstream
    width: int              # pixels, as DELIVERED (never as requested - DESIGN.md 3)
    height: int
    hands: tuple[Hand, ...]


# A hand-shaped hole. Immutable and shared, so the CHOP's "no hand" path
# allocates nothing per cook: every empty slot on every frame is this one
# object. Zeros rather than NaN because a CHOP channel is a float and NaN
# propagates silently through TD maths, where a zero at least draws at the
# origin and looks wrong.
BLANK_JOINT: Final = Joint(NormX(0.0), NormY(0.0), Confidence(0.0))
BLANK_HAND: Final = Hand(
    chirality=CHIRALITY_UNKNOWN,
    confidence=Confidence(0.0),
    joints=tuple(BLANK_JOINT for _ in range(N_JOINTS)),
    found=False,
)


def blank_frame(seq: int = 0, captured_at: float = 0.0,
                width: int = 0, height: int = 0) -> LandmarkFrame:
    """A frame with every slot empty.

    Why it exists: the CHOP has to publish a full, fixed channel list before the
    engine has ever produced anything - on the very first cook, and after a
    reload. Without this, that path would be the only one that has to invent
    channel values, and inventing them in two places is how contracts drift.
    """
    return LandmarkFrame(
        seq=seq, captured_at=captured_at, width=width, height=height,
        hands=tuple(BLANK_HAND for _ in range(MAX_HANDS)),
    )


# ---------------------------------------------------------------------------
# The channel contract
#
# Lives here, rather than in td/hands_chop.py where it is used, because it is a
# contract between the CHOP and everything downstream of it, and because the
# tests must be able to assert it without importing TouchDesigner. This is a
# deliberate widening of what DESIGN.md 5 lists for this module.
# ---------------------------------------------------------------------------
# Per-hand scalars, in publication order.
_HAND_SCALARS: Final = ("found", "score", "chirality")
# Per-joint values, in publication order.
_JOINT_VALUES: Final = ("x", "y", "conf")
# Frame-level scalars, in publication order.
_FRAME_SCALARS: Final = ("n_hands", "seq", "age_ms")


def channel_names() -> tuple[str, ...]:
    """The complete, fixed channel list, in publication order.

    Contract: 3 + MAX_HANDS * (3 + N_JOINTS * 3) names. At MAX_HANDS=2 that is
              3 + 2 * 66 = 135 (DESIGN.md 6.2).
    Why fixed and never conditional: a CHOP whose channels appear and disappear
              breaks every downstream reference in the TD project the moment
              they vanish, and it breaks it silently - the operator that
              referenced h1_lm08_x just stops receiving. An absent hand
              therefore publishes zeros with all of its channels present.
    Why joint INDEX and not joint name in the channel name: index keeps the
              names short and fixed-width (h0_lm08_x), and the index-to-name
              mapping is JOINT_NAMES, right here in this module. Renaming a
              joint then costs nothing downstream.
    """
    names: list[str] = list(_FRAME_SCALARS)
    for hand_i in range(MAX_HANDS):
        for scalar in _HAND_SCALARS:
            names.append("h%d_%s" % (hand_i, scalar))
        for joint_i in range(N_JOINTS):
            for value in _JOINT_VALUES:
                # %02d, so lm08 and lm18 sort next to each other and line up in
                # TD's channel list. 21 joints will never need three digits.
                names.append("h%d_lm%02d_%s" % (hand_i, joint_i, value))
    return tuple(names)


N_CHANNELS: Final = 3 + MAX_HANDS * (len(_HAND_SCALARS) + N_JOINTS * len(_JOINT_VALUES))


# ---------------------------------------------------------------------------
# Import-time self-check.
#
# Deliberately `raise`, not `assert`: assertions vanish under python -O, and
# these are contract checks, not debugging aids. They cost microseconds once.
# A table this repetitive is exactly where a copy-paste error hides, and every
# one of these failures would otherwise surface as plausible-looking wrong
# numbers much later.
# ---------------------------------------------------------------------------
def _self_check() -> None:
    if len(JOINTS) != N_JOINTS:
        raise RuntimeError("JOINTS has %d entries, expected %d" % (len(JOINTS), N_JOINTS))
    if len(set(JOINT_NAMES)) != N_JOINTS:
        raise RuntimeError("duplicate joint name in JOINTS")
    if len(set(JOINT_CODES)) != N_JOINTS:
        raise RuntimeError("duplicate joint code in JOINTS - check ThumbIP vs IndexTip")
    if len(BLANK_HAND.joints) != N_JOINTS:
        raise RuntimeError("BLANK_HAND has the wrong number of joints")
    channels = channel_names()
    if len(channels) != N_CHANNELS:
        raise RuntimeError("channel_names() produced %d names, N_CHANNELS says %d"
                           % (len(channels), N_CHANNELS))
    if len(set(channels)) != N_CHANNELS:
        raise RuntimeError("duplicate channel name")


_self_check()
