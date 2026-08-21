"""Synthetic hands, for testing every derived attribute against known truth.

A real hand cannot tell you what its openness *should* be. A generated one can:
ask for a fist and `openness` must come out 0, ask for a hand rotated 30 degrees
and `rotation` must come out 30. That turns docs/ATTRIBUTES.md from prose into
assertions, and it means the whole attribute layer can be built and verified with
no camera and nobody in frame.

Pure Python: no pyobjc, no TouchDesigner, no numpy. Anatomy is approximate - this
is a test instrument, not a hand model - but it is consistent, parameterised and
anatomically ordered, which is all an assertion needs.

Ref: docs/ATTRIBUTES.md, DESIGN.md 6.1 (the frame contract), 7 (bottom-left y-up).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from visionhands.types import (
    CHIRALITY_RIGHT,
    MAX_HANDS,
    Confidence,
    Hand,
    Joint,
    LandmarkFrame,
    NormX,
    NormY,
    blank_frame,
)

# Finger layout as fractions of hand size, measured off a flat right hand with
# the fingers pointing along +y. Order matches JOINT_NAMES.
#
#   (x offset from the wrist, length of the finger in size units)
#
# These are not anthropometric data; they are a consistent geometry that produces
# plausible landmark relationships. What matters for testing is that a finger's
# segments sum to its length, so a fully extended finger has
# dist(mcp, tip) == bone length and therefore curl 0.
_FINGERS: dict[str, tuple[float, float]] = {
    "thumb": (-0.42, 0.62),
    "index": (-0.16, 1.00),
    # Dead centre on purpose: `size` and `rotation` are both defined off the
    # wrist-to-middle-MCP vector, so any x offset here tilts that axis and makes
    # a hand generated at 30 degrees measure 28.65. Zero keeps the two exact and
    # therefore assertable.
    "middle": (0.00, 1.08),
    "ring": (0.20, 0.98),
    "little": (0.36, 0.80),
}

# Where each finger's base sits, as a fraction of the wrist-to-MCP distance.
_MCP_HEIGHT = 0.85

# The thumb's base is lower and further out than the other fingers.
_THUMB_CMC_HEIGHT = 0.28


@dataclass
class HandPose:
    """Everything needed to place one synthetic hand.

    Angles in degrees, positions normalised 0..1 with y UP (DESIGN.md 7).
    `curl` is per finger, 0 straight and 1 fully curled - the same convention the
    derived `h{i}_curl_*` channels use, so a test can assert one against the
    other directly.
    """

    palm_x: float = 0.5
    palm_y: float = 0.5
    size: float = 0.12          # wrist-to-middle-MCP distance, normalised units
    rotation: float = 90.0      # 90 = fingers pointing up the frame
    curl: dict[str, float] = field(default_factory=dict)
    spread: float = 1.0         # scales the sideways fan of the fingers
    # Pinch, 0 apart to 1 tips touching. Separate from curl because curling the
    # thumb and index does NOT bring their tips together - measured, a fully
    # curled thumb-index pair still sits 0.91 hand-sizes apart. A pinch is the
    # thumb reaching ACROSS to the index, so it is modelled directly.
    pinch: float = 0.0
    chirality: int = CHIRALITY_RIGHT
    confidence: float = 0.9

    def curl_of(self, finger: str) -> float:
        return float(self.curl.get(finger, 0.0))


def _rotate(x: float, y: float, degrees: float) -> tuple[float, float]:
    radians = math.radians(degrees)
    cos_a, sin_a = math.cos(radians), math.sin(radians)
    return x * cos_a - y * sin_a, x * sin_a + y * cos_a


def _finger_chain(finger: str, pose: HandPose) -> list[tuple[float, float]]:
    """The four joints of one finger, in local space, root to tip.

    Curl is applied as progressive rotation at each joint - each segment bends a
    little more than the last, which is how a finger actually closes and, more
    importantly for testing, makes dist(mcp, tip) shrink monotonically with curl
    while the segment lengths stay constant.
    """
    base_x, length = _FINGERS[finger]
    base_x *= pose.spread
    base_y = _THUMB_CMC_HEIGHT if finger == "thumb" else _MCP_HEIGHT
    segment = length / 3.0
    curl = pose.curl_of(finger)
    # 75 degrees per joint at full curl brings the tip back towards the palm
    # without folding it through itself.
    bend = 75.0 * curl

    points = [(base_x, base_y)]
    x, y = base_x, base_y
    direction = 0.0                     # 0 = straight along +y
    for _ in range(3):
        direction += bend
        step_x, step_y = _rotate(0.0, segment, direction)
        x, y = x + step_x, y + step_y
        points.append((x, y))
    return points


def synthetic_hand(pose: HandPose) -> Hand:
    """One anatomically-ordered Hand from a pose.

    Contract: 21 joints in JOINT_NAMES order, normalised, y up, so this is
    interchangeable with a Hand the engine produced.
    """
    local: list[tuple[float, float]] = [(0.0, 0.0)]      # wrist at the origin
    for finger in ("thumb", "index", "middle", "ring", "little"):
        local.extend(_finger_chain(finger, pose))

    # Normalise so the wrist-to-middle-MCP distance is exactly 1 in local space.
    # Without this, `pose.size` is a scale factor rather than the hand size that
    # comes out the other end, and every test would have to know the local
    # geometry to predict `h{i}_size`. JOINT_NAMES index 9 is middle_mcp - the
    # wrist is 0, the thumb takes 1..4, the index 5..8.
    middle_mcp_local = local[9]
    unit = math.hypot(*middle_mcp_local)
    local = [(x / unit, y / unit) for x, y in local]

    # The pinch: move the thumb's IP and tip towards the index tip. Applied in
    # local space, before rotation, so it is unaffected by hand orientation.
    if pose.pinch > 0.0:
        index_tip_local = local[8]                       # JOINT_NAMES index 8
        for joint_index in (3, 4):                       # thumb_ip, thumb_tip
            x, y = local[joint_index]
            local[joint_index] = (
                x + (index_tip_local[0] - x) * pose.pinch,
                y + (index_tip_local[1] - y) * pose.pinch,
            )

    # Rotate about the wrist, scale, then place. The pose's palm_x/palm_y name
    # where the PALM lands, so the wrist is offset back along the hand's axis -
    # otherwise asking for a palm at the centre of frame would put the wrist
    # there instead and every two-hand distance would be measured from the wrong
    # point.
    # The palm centroid, computed from the SAME six joints the derived
    # `h{i}_palm_x/y` averages - wrist, thumb CMC and the four finger MCPs - so
    # that asking for a palm at (0.3, 0.7) puts it exactly there. An earlier
    # version estimated it as a fraction of the MCP height and landed 0.021
    # short, which would have made every two-hand distance assertion approximate.
    palm_indices = (0, 1, 5, 9, 13, 17)
    palm_local_x = sum(local[i][0] for i in palm_indices) / len(palm_indices)
    palm_local_y = sum(local[i][1] for i in palm_indices) / len(palm_indices)
    joints: list[Joint] = []
    rotated_palm = _rotate(palm_local_x, palm_local_y, pose.rotation - 90.0)
    offset_x = rotated_palm[0] * pose.size
    offset_y = rotated_palm[1] * pose.size
    for local_x, local_y in local:
        rotated_x, rotated_y = _rotate(local_x, local_y, pose.rotation - 90.0)
        joints.append(Joint(
            NormX(pose.palm_x + rotated_x * pose.size - offset_x),
            NormY(pose.palm_y + rotated_y * pose.size - offset_y),
            Confidence(pose.confidence),
        ))

    frozen = tuple(joints)
    from visionhands.types import median_joint_confidence
    return Hand(chirality=pose.chirality, confidence=Confidence(1.0),
                conf_median=median_joint_confidence(frozen), joints=frozen, found=True)


def synthetic_frame(poses: list[HandPose | None], seq: int = 1,
                    captured_at: float = 0.0,
                    width: int = 1280, height: int = 720) -> LandmarkFrame:
    """A full LandmarkFrame. `None` in a slot means no hand there.

    Uses the shared BLANK_HAND for empty slots, exactly as the engine does, so a
    test exercises the absent-hand path for real - which is where the all-zeros
    trap lives (docs/ATTRIBUTES.md).
    """
    blank = blank_frame().hands[0]
    hands = [synthetic_hand(p) if p is not None else blank for p in poses[:MAX_HANDS]]
    while len(hands) < MAX_HANDS:
        hands.append(blank)
    return LandmarkFrame(seq=seq, captured_at=captured_at, width=width,
                         height=height, hands=tuple(hands))


# ---------------------------------------------------------------------------
# Named poses, so tests read as intent rather than as numbers
# ---------------------------------------------------------------------------
def fist(**kwargs: float) -> HandPose:
    return HandPose(curl={f: 1.0 for f in _FINGERS}, **kwargs)      # type: ignore[arg-type]


def open_hand(**kwargs: float) -> HandPose:
    return HandPose(curl={f: 0.0 for f in _FINGERS}, **kwargs)      # type: ignore[arg-type]


def pointing(**kwargs: float) -> HandPose:
    curl = {"thumb": 0.5, "index": 0.0, "middle": 1.0, "ring": 1.0, "little": 1.0}
    return HandPose(curl=curl, **kwargs)                            # type: ignore[arg-type]


def peace(**kwargs: float) -> HandPose:
    curl = {"thumb": 0.8, "index": 0.0, "middle": 0.0, "ring": 1.0, "little": 1.0}
    return HandPose(curl=curl, **kwargs)                            # type: ignore[arg-type]


def pinching(amount: float = 1.0, **kwargs: float) -> HandPose:
    """A pinch, `amount` 0 apart to 1 tips touching.

    Drives the `pinch` field rather than the curls - see the note there. At
    amount 1 the thumb tip coincides with the index tip, so `pinch_index` is 0
    and any engage threshold must fire.
    """
    return HandPose(pinch=float(amount), **kwargs)                  # type: ignore[arg-type]
