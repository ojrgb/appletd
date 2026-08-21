"""The body-pose data contract: 19 joints per person, and the channels for them.

The pose counterpart of `types.py`, and deliberately the same shape of file:
frozen dataclasses, tuples of strings, an import-time self-check, and nothing
imported but the standard library. Separate from `types.py` rather than folded
into it because a body is not a hand - no chirality, a different joint table, a
different number of them - and because the hands contract is referenced by a live
TouchDesigner project. Nothing here can change a hand channel.

Thread: everything defined here is immutable, so any object from this module can
        be read from any thread with no lock. Same mechanism as `types.py`: the
        capture thread builds a `PoseFrame` and rebinds one reference; a reader
        holding the old one keeps a whole, consistent frame.

WHY THREE NAMES PER JOINT, again. Vision's constant names and their VALUES
disagree about anatomy (MEASURED, DESIGN.md 2.12): `LeftElbow` is
`left_forearm_joint`, `LeftWrist` is `left_hand_joint`, `Nose` is `head_joint`.
A table built from the values would map an elbow to a forearm. So each row
carries the constant suffix (what to look up in the framework), the value (the
dict key Vision hands back), and our own published name - and `pose.py` checks
every suffix and value against the live framework at start-up.

Ref: DESIGN.md 6.4 (the stream contract), 2.12 (the API surface, measured).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from visionhands.types import Confidence, Joint, NormX, NormY

# How many people we publish. Two, mirroring MAX_HANDS.
#
# NOTE what this does and does not cap, because it differs from hands in a way
# that matters (DESIGN.md 2.12): `VNDetectHumanHandPoseRequest` has
# `setMaximumHandCount_`, so MAX_HANDS caps the WORK. The body-pose request has
# no equivalent - Vision finds every person in shot regardless - so this caps
# only the CHANNEL LIST. A third person costs inference and is then dropped.
MAX_BODIES: Final = 2

# Joints per person. Fixed by Vision: the request itself reports exactly these 19
# through supportedJointNamesAndReturnError_ (MEASURED, DESIGN.md 2.12).
N_BODY_JOINTS: Final = 19


@dataclass(frozen=True)
class BodyJointSpec:
    name: str        # ours: what appears in the channel name
    suffix: str      # VNHumanBodyPoseObservationJointName<suffix>
    code: str        # the constant's value = the key in the dict Vision returns


BODY_JOINTS: Final[tuple[BodyJointSpec, ...]] = (
    # Head down to feet, left before right. This order IS the channel order, so
    # changing it silently rewires every downstream reference in a TD project.
    # Do not reorder. (`tests/test_pose_types.py` pins it.)
    BodyJointSpec("nose", "Nose", "head_joint"),          # TRAP: 'head', not 'nose'
    BodyJointSpec("left_eye", "LeftEye", "left_eye_joint"),
    BodyJointSpec("right_eye", "RightEye", "right_eye_joint"),
    BodyJointSpec("left_ear", "LeftEar", "left_ear_joint"),
    BodyJointSpec("right_ear", "RightEar", "right_ear_joint"),
    BodyJointSpec("neck", "Neck", "neck_1_joint"),
    BodyJointSpec("left_shoulder", "LeftShoulder", "left_shoulder_1_joint"),
    BodyJointSpec("right_shoulder", "RightShoulder", "right_shoulder_1_joint"),
    # TRAP: elbow is 'forearm' and wrist is 'hand' in the ARKit skeleton names
    # Vision uses as keys. The suffix is the anatomy; the code is not.
    BodyJointSpec("left_elbow", "LeftElbow", "left_forearm_joint"),
    BodyJointSpec("right_elbow", "RightElbow", "right_forearm_joint"),
    BodyJointSpec("left_wrist", "LeftWrist", "left_hand_joint"),
    BodyJointSpec("right_wrist", "RightWrist", "right_hand_joint"),
    BodyJointSpec("root", "Root", "root"),                # hip centre
    BodyJointSpec("left_hip", "LeftHip", "left_upLeg_joint"),
    BodyJointSpec("right_hip", "RightHip", "right_upLeg_joint"),
    BodyJointSpec("left_knee", "LeftKnee", "left_leg_joint"),
    BodyJointSpec("right_knee", "RightKnee", "right_leg_joint"),
    BodyJointSpec("left_ankle", "LeftAnkle", "left_foot_joint"),
    BodyJointSpec("right_ankle", "RightAnkle", "right_foot_joint"),
)

BODY_JOINT_NAMES: Final[tuple[str, ...]] = tuple(j.name for j in BODY_JOINTS)
BODY_JOINT_CODES: Final[tuple[str, ...]] = tuple(j.code for j in BODY_JOINTS)
# code -> index into a Body's joints tuple, built once. Read-only, so the
# module's promise that everything in it is thread-safe stays true rather than
# merely intended (`Final` stops rebinding, not mutation).
BODY_JOINT_INDEX_BY_CODE: Final[Mapping[str, int]] = MappingProxyType(
    {j.code: i for i, j in enumerate(BODY_JOINTS)})

# The joints group that returns all 19 in one call - the SAME constant hands
# uses for its 21 (MEASURED, DESIGN.md 2.12).
BODY_JOINTS_GROUP_ALL_CODE: Final = "VNIPOAll"

# Which joint decides a person's left-to-right order, and what to fall back to.
#
# The root (hip centre) rather than the nose: it is the most central joint on a
# person and the least affected by which way they are facing or leaning. Neck
# next, because it is the other reliably-central one, and it survives a lower
# body that is out of frame or behind a desk - which is the common case for a
# seated installation. If neither is confident the body sorts last, which is
# deliberate: an unlocatable person should not push a located one out of slot 0.
SORT_JOINT_NAMES: Final[tuple[str, ...]] = ("root", "neck")

# Below this, a joint's position is noise rather than a location. A GUESS, not a
# measurement: it is the same order as the hand confidences we have measured
# (DESIGN.md 2.6 - 11% of joint confidences arrive at exactly 0.0), and it is
# used only to choose between sort keys, so being wrong costs an ordering rather
# than a value. Measure it when a fixture with two people in it exists.
SORT_CONF_FLOOR: Final = 0.1

# Resolved once. `BODY_JOINT_NAMES.index(name)` inside the sort key would be a
# linear scan per body per frame for a table that never changes.
SORT_JOINT_INDICES: Final[tuple[int, ...]] = tuple(
    BODY_JOINT_NAMES.index(name) for name in SORT_JOINT_NAMES)


@dataclass(frozen=True)
class Body:
    """One person: 19 joints in BODY_JOINTS order, plus two scalars.

    Contract: joints always has exactly N_BODY_JOINTS entries, in BODY_JOINTS
              order, even when the slot is empty (see BLANK_BODY). Downstream
              indexes this tuple positionally, so a short tuple is a contract
              violation rather than a missing-data case.
    No chirality field, and that absence is the whole reason DESIGN.md 6.4 sorts
              bodies spatially instead of partitioning them the way `slots.py`
              partitions hands: Vision labels a hand left or right and has no
              equivalent for a person.
    """

    # Vision's own observation confidence, passed through unchanged.
    #
    # UNMEASURED, unlike its hand equivalent: hands' `confidence` is a measured
    # constant 1.0 (DESIGN.md 2.6) and this one has never been seen, because
    # measuring it needs a person in frame. Published as a faithful mirror of
    # the API. Do not gate on it until it has been measured.
    confidence: Confidence
    # Ours: the median of the 19 joint confidences, and the one to gate on. See
    # `types.median_joint_confidence` for why a median and not a mean.
    conf_median: Confidence
    joints: tuple[Joint, ...]
    # True when this slot holds a real detection, so "there is a Body object
    # here" and "a person was seen this frame" stay different questions.
    found: bool = True

    def sort_key(self) -> tuple[float, float]:
        """Where this body sits horizontally, for the left-to-right ordering.

        Contract: returns (rank, x) where rank is 0 for a body with a confident
                  reference joint and 1 for one without, so that unlocatable
                  bodies sort AFTER located ones however small their x is.
                  Ascending order puts the leftmost person first.
        Why not just x: an empty slot and a person whose hips are occluded both
                  produce x = 0.0, which is the far left of frame - so a plain x
                  sort would put a body we cannot locate in slot 0 and push a
                  perfectly visible person to slot 1.
        Ref: DESIGN.md 6.4.
        """
        if not self.found:
            return (1.0, 0.0)
        for index in SORT_JOINT_INDICES:
            joint = self.joints[index]
            if joint.conf >= SORT_CONF_FLOOR:
                return (0.0, float(joint.x))
        return (1.0, 0.0)


@dataclass(frozen=True)
class PoseFrame:
    """One body-pose inference result, ready to cross the thread boundary.

    Thread: built on the capture thread, read on the send loop's thread, shared
            by one atomic reference rebind and no lock - exactly as
            `LandmarkFrame` is (DESIGN.md 4.3).
    Contract: bodies has exactly MAX_BODIES entries, always, ordered left to
            right, with BLANK_BODY in the empty slots. The fixed length is what
            makes the fixed channel list unconditionally true.
    INVARIANT: floats, ints, bools and tuples only. No pyobjc object may ever be
            referenced from here - it would then be released on whichever thread
            dropped the last reference.
    """

    seq: int                # monotonic, from the engine; shares the hands frame's
    captured_at: float      # time.monotonic() at capture
    width: int              # pixels, as DELIVERED
    height: int
    bodies: tuple[Body, ...]


BLANK_BODY: Final = Body(
    # 0.0, not 1.0: an empty slot must not look like a confident detection to
    # anything that reads this channel without checking `found` first.
    confidence=Confidence(0.0),
    conf_median=Confidence(0.0),
    joints=tuple(Joint(NormX(0.0), NormY(0.0), Confidence(0.0))
                 for _ in range(N_BODY_JOINTS)),
    found=False,
)


def blank_pose_frame(seq: int = 0, captured_at: float = 0.0,
                     width: int = 0, height: int = 0) -> PoseFrame:
    """A frame with every slot empty.

    Why it exists: the sidecar has to send a full, fixed channel list before any
    inference has happened - and, when the pose stream is DISABLED, forever
    (DESIGN.md 6.4: a disabled stream sends zeros rather than nothing). Without
    this, that path would have to invent channel values, and inventing them in
    two places is how contracts drift.
    """
    return PoseFrame(seq=seq, captured_at=captured_at, width=width, height=height,
                     bodies=tuple(BLANK_BODY for _ in range(MAX_BODIES)))


def order_bodies(bodies: Sequence[Body]) -> tuple[Body, ...]:
    """Left to right by the reference joint, padded to MAX_BODIES. Pure.

    Contract: returns exactly MAX_BODIES entries. Extra bodies beyond MAX_BODIES
              are DROPPED after sorting, so the two leftmost people are the two
              that get published - a stable rule, rather than "whichever two
              Vision happened to list first".
    Why sort at all: Vision's observation order is not stable between frames, so
              `p0` would swap between two people - the same defect DESIGN.md 6.3
              exists to fix for hands, where chirality solves it. There is no
              chirality for a person, so this is the cheapest rule that is stable
              while people do not cross over. It holds no state.
    """
    found = sorted((body for body in bodies if body.found),
                   key=lambda body: body.sort_key())
    ordered = list(found[:MAX_BODIES])
    while len(ordered) < MAX_BODIES:
        ordered.append(BLANK_BODY)      # shared and frozen: no allocation
    return tuple(ordered)


# ---------------------------------------------------------------------------
# The channel contract
#
# Frame scalars are PREFIXED here where the hands equivalents (`n_hands`, `seq`,
# `age_ms`) are not. Hands went first and those bare names are referenced in a
# live project, so they stay; nothing new goes unprefixed (DESIGN.md 6.4).
# ---------------------------------------------------------------------------
_BODY_SCALARS: Final = ("found", "score", "conf_median")
_JOINT_VALUES: Final = ("x", "y", "conf")
_FRAME_SCALARS: Final = ("pose_n_bodies", "pose_seq", "pose_age_ms")


def pose_channel_names() -> tuple[str, ...]:
    """The complete, fixed pose channel list, in publication order.

    Contract: 3 + MAX_BODIES * (3 + N_BODY_JOINTS * 3) names. At MAX_BODIES = 2
              that is 3 + 2 * 60 = 123 (DESIGN.md 6.4).
    Why fixed and never conditional: the same reason as hands (DESIGN.md 6.2). A
              CHOP whose channels come and go breaks every downstream reference
              the moment they vanish, and it breaks it SILENTLY - the operator
              that referenced the channel just stops receiving.
    """
    names: list[str] = list(_FRAME_SCALARS)
    for body_i in range(MAX_BODIES):
        for scalar in _BODY_SCALARS:
            names.append("p%d_%s" % (body_i, scalar))
        for joint_name in BODY_JOINT_NAMES:
            for value in _JOINT_VALUES:
                names.append("p%d_%s_%s" % (body_i, joint_name, value))
    return tuple(names)


def pose_channel_values(frame: PoseFrame, age_ms: float,
                        n_bodies: int) -> list[float]:
    """The values, in `pose_channel_names()` order. Pure, and therefore testable.

    Contract: returns exactly len(pose_channel_names()) floats in that order. The
              two are kept in step by a test rather than by inspection, because a
              misalignment here would put one joint's y into another joint's x
              channel and still look plausible on screen.
    Coordinates: normalised, origin BOTTOM LEFT, exactly as Vision produced them.
              No flip happens here or anywhere else (DESIGN.md 7).
    """
    values: list[float] = [float(n_bodies), float(frame.seq), float(age_ms)]
    for body in frame.bodies:
        values.append(1.0 if body.found else 0.0)
        values.append(float(body.confidence))
        values.append(float(body.conf_median))
        for joint in body.joints:
            values.append(float(joint.x))
            values.append(float(joint.y))
            values.append(float(joint.conf))
    return values


N_POSE_CHANNELS: Final = 3 + MAX_BODIES * (len(_BODY_SCALARS)
                                           + N_BODY_JOINTS * len(_JOINT_VALUES))


# ---------------------------------------------------------------------------
# Import-time self-check. `raise`, not `assert` - the same reasoning as
# types.py: assertions vanish under python -O and these are contract checks.
# ---------------------------------------------------------------------------
def _self_check() -> None:
    if len(BODY_JOINTS) != N_BODY_JOINTS:
        raise RuntimeError("BODY_JOINTS has %d entries, expected %d"
                           % (len(BODY_JOINTS), N_BODY_JOINTS))
    if len(set(BODY_JOINT_NAMES)) != N_BODY_JOINTS:
        raise RuntimeError("duplicate joint name in BODY_JOINTS")
    if len(set(BODY_JOINT_CODES)) != N_BODY_JOINTS:
        raise RuntimeError("duplicate joint code in BODY_JOINTS")
    if len(BLANK_BODY.joints) != N_BODY_JOINTS:
        raise RuntimeError("BLANK_BODY has the wrong number of joints")
    for name in SORT_JOINT_NAMES:
        if name not in BODY_JOINT_NAMES:
            raise RuntimeError("sort joint %r is not in the table" % (name,))
    channels = pose_channel_names()
    if len(channels) != N_POSE_CHANNELS:
        raise RuntimeError("pose_channel_names() produced %d names, "
                           "N_POSE_CHANNELS says %d"
                           % (len(channels), N_POSE_CHANNELS))
    if len(set(channels)) != N_POSE_CHANNELS:
        raise RuntimeError("duplicate pose channel name")


_self_check()
