"""The face data contract: head pose, bounding box, landmarks and key points.

The third stream contract, after `types.py` (hands) and `pose_types.py` (bodies), and
the same shape: frozen dataclasses, tuples of strings, an import-time self-check,
nothing imported but the standard library.

A face is not another joint table. Hands and bodies come back as a flat dict of named
joints with a fixed count; `VNFaceObservation.landmarks()` returns 12 named REGIONS,
each with its own `pointCount`, and nothing in the framework publishes those counts
before a face has been seen. The constellation is selectable - 65 or 76 points - so
the total is pinned by our request, but the split across regions is not.

  * `FACE_SCALARS` - found, confidence, capture quality, the three angles and the
    bounding box. 23 channels, none of which need landmarks.
  * `FACE_REGIONS` - the 12 regions. They OVERLAP: 87 slots over 76 distinct points,
    so summing two regions double-counts the eleven shared ones.
  * `FACE_KEYPOINTS` - four points per face, two of which no region publishes
    directly and are computed instead.

Thread: pure data and pure functions. Safe anywhere.
Ref: DESIGN.md 2.12, docs/ATTRIBUTES.md.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from appletd.types import Confidence, NormX, NormY

# How many faces we publish, mirroring MAX_HANDS and MAX_BODIES.
#
# As with bodies, this caps the CHANNEL LIST and not the work:
# `VNDetectFaceLandmarksRequest` has no maximum-face setting, so Vision finds every
# face in shot and the third one is dropped after the left-to-right sort.
MAX_FACES: Final = 2


@dataclass(frozen=True)
class FaceRegionSpec:
    """One landmark region, and how many points it carries.

    `accessor` is the selector on `VNFaceLandmarks2D`; `name` is ours, and appears
    in the channel names. `point_count` stays None until somebody MEASURES it -
    nothing here may be filled in from memory or from a blog post, because a wrong
    count silently lays one region's points into another's channels. The counts
    below were measured; `tools/probe_face_regions.py` re-measures.
    """

    name: str
    accessor: str
    point_count: int | None = None


# The 12 regions, VERIFIED to exist on `VNFaceLandmarks2D` (DESIGN.md
# 2.12). Order is head-outward then top-down, and it will BE the channel order once
# the counts land, so it is pinned by a test now rather than after a project
# references it.
FACE_REGIONS: Final[tuple[FaceRegionSpec, ...]] = (
    # MEASURED by tools/probe_face_regions.py, against a real face on
    # this machine: pyobjc 12.2.2 / macOS 26.5.2, request revision 3, constellation
    # 76 points. Not copied from anywhere.
    FaceRegionSpec("face_contour", "faceContour", 17),
    FaceRegionSpec("median_line", "medianLine", 10),
    FaceRegionSpec("left_eyebrow", "leftEyebrow", 6),
    FaceRegionSpec("right_eyebrow", "rightEyebrow", 6),
    FaceRegionSpec("left_eye", "leftEye", 6),
    FaceRegionSpec("right_eye", "rightEye", 6),
    FaceRegionSpec("left_pupil", "leftPupil", 1),
    FaceRegionSpec("right_pupil", "rightPupil", 1),
    FaceRegionSpec("nose", "nose", 8),
    FaceRegionSpec("nose_crest", "noseCrest", 6),
    FaceRegionSpec("outer_lips", "outerLips", 14),
    FaceRegionSpec("inner_lips", "innerLips", 6),
)

FACE_REGION_NAMES: Final[tuple[str, ...]] = tuple(r.name for r in FACE_REGIONS)

# True only when every region's count has been measured. While this is False the
# landmark channels are not published - the stream is head pose and bounding box,
# which is the part that is knowable without a face in front of the camera.
LANDMARKS_PUBLISHED: Final = all(r.point_count is not None for r in FACE_REGIONS)

# What the request is pinned to, so the point count is ours to choose rather than
# something to discover. MEASURED: the request's default constellation is 2, which
# is `VNRequestFaceLandmarksConstellation76Points`.
CONSTELLATION_76: Final = 2

# TWO totals, and conflating them is what made the first attempt at this table fail
# its own self-check. MEASURED, both:
#
#   76  DISTINCT coordinates across all 12 regions - which is what "the 76-point
#       constellation" means;
#   87  region SLOTS, because the regions are not a partition. `medianLine` runs
#       down the centre of the face and shares points with `noseCrest` (4), `nose`
#       (2), `outerLips` (2), `innerLips` (2) and `faceContour` (1), and `nose`
#       shares one more with `noseCrest`. Twelve pair-overlaps for eleven duplicate
#       points, which means nine points sit in two regions and one sits in three -
#       the nose tip.
#
# The channel list is built from the SLOT total, so 11 points are published twice,
# under both of their regions' names. That is the price of `f0_nose_crest_00_x`
# instead of `f0_lm43_x`, and it is the same trade DESIGN.md 6.2 made for hands:
# "which one is lm43" is exactly the question a name should answer. 44 duplicate
# channels across two faces, against a translation layer in every project.
CONSTELLATION_DISTINCT_POINTS: Final = 76
REGION_SLOT_TOTAL: Final = 87


@dataclass(frozen=True)
class Face:
    """One face: the observation's own numbers, plus landmarks when we publish them.

    Contract: angles are DEGREES - converted once, in `face.py`, from Vision's
              radians. `bbox` is normalised with the origin BOTTOM LEFT, exactly as
              Vision reports it, so it agrees with every landmark in this system
              (DESIGN.md 7).
    `landmarks` is a mapping from region name to a tuple of (x, y) pairs, empty
              while the point counts are unmeasured. Empty rather than absent, so
              consumers of this dataclass need no branch.
    """

    confidence: Confidence     # Vision's own, UNMEASURED for faces - see below
    quality: Confidence        # faceCaptureQuality: a real 0..1 signal, per Apple
    roll_deg: float
    yaw_deg: float
    pitch_deg: float
    bbox_x: NormX              # left edge, normalised, origin bottom-left
    bbox_y: NormY              # BOTTOM edge
    bbox_w: float
    bbox_h: float
    landmarks: tuple[tuple[str, tuple[tuple[float, float], ...]], ...] = ()
    found: bool = True

    def center_x(self) -> float:
        """The bounding box's horizontal centre - the sort key, and nothing else.

        Not published as a channel: it is one Math CHOP downstream and this stream
        is plumbing, with no derived layer (DESIGN.md 6.4).
        """
        return float(self.bbox_x) + float(self.bbox_w) * 0.5


BLANK_FACE: Final = Face(
    # 0.0, not the 1.0 a real observation may report: an empty slot must not look
    # like a confident detection to anything that skips `found`.
    confidence=Confidence(0.0),
    quality=Confidence(0.0),
    roll_deg=0.0, yaw_deg=0.0, pitch_deg=0.0,
    bbox_x=NormX(0.0), bbox_y=NormY(0.0), bbox_w=0.0, bbox_h=0.0,
    landmarks=(),
    found=False,
)


@dataclass(frozen=True)
class FaceFrame:
    """One face-detection result, ready to cross the thread boundary.

    Thread: built on the capture thread, read on the send loop's thread, shared by
            one atomic reference rebind and no lock - as `LandmarkFrame` is.
    Contract: faces has exactly MAX_FACES entries, always, ordered left to right,
            padded with the shared BLANK_FACE.
    INVARIANT: floats, ints, bools, strings and tuples only. No pyobjc object may
            ever be referenced from here.
    """

    seq: int
    captured_at: float
    width: int
    height: int
    faces: tuple[Face, ...]


def blank_face_frame(seq: int = 0, captured_at: float = 0.0,
                     width: int = 0, height: int = 0) -> FaceFrame:
    """A frame with every slot empty. Also what a DISABLED face stream sends."""
    return FaceFrame(seq=seq, captured_at=captured_at, width=width, height=height,
                     faces=tuple(BLANK_FACE for _ in range(MAX_FACES)))


def order_faces(faces: Sequence[Face]) -> tuple[Face, ...]:
    """Left to right by bounding-box centre, padded to MAX_FACES. Pure.

    Same rule and same reason as `pose_types.order_bodies`: Vision's observation
    order is not stable between frames and it gives no tracking ID, so `f0` would
    swap between two people. A face has no chirality to partition on either, so the
    cheapest stable rule is spatial. It holds no state, and two faces that cross
    over exchange slots - deliberately visible rather than hidden.

    Unlike a body, a face needs no confidence fallback: a found face always has a
    bounding box, which is the thing being sorted on.
    """
    found = sorted((face for face in faces if face.found),
                   key=lambda face: face.center_x())
    ordered = list(found[:MAX_FACES])
    while len(ordered) < MAX_FACES:
        ordered.append(BLANK_FACE)
    return tuple(ordered)


# ---------------------------------------------------------------------------
# The channel contract
#
# Prefixed `f{i}_`, and the frame scalars prefixed too - only hands' `n_hands`,
# `seq` and `age_ms` go bare, because they predate there being more than one stream
# (DESIGN.md 6.4).
# ---------------------------------------------------------------------------
# Per face. Every one of these comes off the observation itself, with no landmarks
# involved, which is why this half of the contract is publishable today.
FACE_SCALARS: Final[tuple[str, ...]] = (
    "found",
    # Vision's own confidence. UNMEASURED for faces - hands' equivalent is a
    # measured constant 1.0 (DESIGN.md 2.6) and this one has never been seen.
    # Published as a faithful mirror; gate on `quality` instead.
    "score",
    # faceCaptureQuality: documented by Apple as a real 0..1 comparison metric for
    # the same subject. Also UNMEASURED here, and the better candidate of the two.
    "quality",
    # DEGREES. Vision reports radians; the conversion is in face.py.
    #
    # RENAMED, from `roll`/`yaw`/`pitch`. One vocabulary for every stream
    # that has an orientation: `angle_x` is pitch (nodding), `angle_y` is yaw
    # (turning), `angle_z` is roll (tilting), right-handed about the camera axes.
    # The dataclass fields keep Vision's names, because they mirror Vision's API; it
    # is the PUBLISHED channel that is ours to name.
    #
    # In x, y, z order - so `face_values` appends pitch, yaw, roll, in that order.
    # The names and the values are paired by POSITION and nothing checks it at run
    # time, which is why they are edited together and tested together.
    "angle_x", "angle_y", "angle_z",
    # Normalised, origin BOTTOM LEFT, as Vision gives it. `bbox_y` is the BOTTOM
    # edge, not the top - the trap that DESIGN.md 7 exists for.
    "bbox_x", "bbox_y", "bbox_w", "bbox_h",
)
_FRAME_SCALARS: Final = ("face_n", "face_seq", "face_age_ms")


# ---------------------------------------------------------------------------
# The four key points
#
# 348 landmark channels is the right answer for a project that wants a face MESH
# and the wrong one for a project that wants to know where somebody is looking. So
# four points per face are published beside the regions, and `Facekeypoints` on the
# Attributes page is what swaps one for the other.
#
# WHY THEY ARE COMPUTED HERE rather than selected in TouchDesigner: two of the four
# are not points Vision publishes. Every region except the pupils is a RING -
# `left_eye_00`..`_05` trace around the eye and none of them is its centre - so a
# mouth centre has to be averaged, and the nose tip has to be identified. Doing that
# in the sidecar costs a few microseconds on the capture thread; doing it in
# TouchDesigner would be eight Math CHOPs on the main thread, per frame, for ever.
#
# LEFT AND RIGHT ARE VISION'S, not the image's. `eye_left` is `leftPupil`, whatever
# side of the frame that turns out to be on. UNMEASURED - the answer needs a face in
# front of the camera, `tools/probe_face_regions.py --keypoints` prints it, and
# guessing it here would put a confident wrong word in docs/ATTRIBUTES.md.
# ---------------------------------------------------------------------------
FACE_KEYPOINTS: Final[tuple[str, ...]] = (
    # Free: `leftPupil` and `rightPupil` are the only SINGLE-POINT regions Vision
    # gives, so each already IS the centre of its eye. No averaging, no guessing.
    "eye_left",
    "eye_right",
    # Identified structurally - see `nose_tip_point` below.
    "nose_tip",
    # The mean of `inner_lips`. The inner ring rather than the outer: it is six
    # points against fourteen, it closes when the mouth closes, and its centre is
    # the aperture rather than the middle of the lip flesh.
    "mouth",
)

# The three regions that all carry the nose tip, and the whole of how it is found.
#
# MEASURED (DESIGN.md 2.12): the 12 regions cover 76 distinct points in
# 87 slots, and the arithmetic of the overlaps pins down exactly one point that
# belongs to THREE regions - nine points sit in two, one sits in three, which is
# 11 duplicate slots and 12 overlapping pairs. That point is the tip of the nose,
# where `medianLine` running down the face meets `noseCrest` running down the bridge
# and `nose` tracing the base.
#
# WHY THIS AND NOT A PINNED INDEX. An index would be one number measured once,
# unverifiable afterwards, and silently wrong on a macOS that renumbers a region -
# it would publish a nostril and call it a tip. The intersection is checked against
# the data on every frame instead, and when it is not exactly one point the channel
# publishes zero rather than a plausible neighbour.
NOSE_TIP_REGIONS: Final[tuple[str, ...]] = ("nose", "nose_crest", "median_line")

# Where two regions are taken to carry the SAME point. Vision computes a shared
# point once and hands it to both regions, so the values have matched to every bit
# in what has been seen - but `region_point_report` has always compared them rounded
# as well as exactly, and this uses the rounded form for the same reason: an
# almost-duplicate should still be recognised as the tip, not silently dropped.
_SHARED_POINT_DP: Final = 6

# What a key point reads when it cannot be computed - no landmarks at all, an empty
# slot, or a nose tip that is not in exactly three regions. The BOX ORIGIN, and
# deliberately the same value an empty face slot carries, so `f{i}_found` stays the
# one thing to gate on rather than a second convention per channel.
_ABSENT_POINT: Final[tuple[float, float]] = (0.0, 0.0)


def nose_tip_point(
        landmarks: Sequence[tuple[str, tuple[tuple[float, float], ...]]],
) -> tuple[float, float] | None:
    """The one point `nose`, `nose_crest` and `median_line` all carry, or None. Pure.

    Contract: box-normalised, origin bottom left, like every landmark here.
              None when the three regions do not share exactly one point - which is
              a missing region, an empty face, or a Vision that has changed its
              constellation. The caller publishes zero and says so; nothing here
              falls back to an index, because a fallback would be the guess this
              function exists to avoid.
    Why:      see `NOSE_TIP_REGIONS`. 24 points intersected, per face per frame -
              cheap enough that caching it would buy a stale index rather than time.
    """
    regions = dict(landmarks)
    shared: set[tuple[float, float]] | None = None
    for name in NOSE_TIP_REGIONS:
        points = regions.get(name)
        if not points:
            return None
        rounded = {(round(x, _SHARED_POINT_DP), round(y, _SHARED_POINT_DP))
                   for x, y in points}
        shared = rounded if shared is None else (shared & rounded)
        if not shared:
            return None
    if shared is None or len(shared) != 1:
        # Two candidates is as wrong as none: it means the assumption this is built
        # on - one triple-shared point - no longer holds on this machine.
        return None
    key = next(iter(shared))
    # The rounding is how the point is IDENTIFIED, not what gets published: return
    # the coordinate Vision actually reported, so this channel carries the same
    # precision as the 348 beside it.
    for x, y in regions[NOSE_TIP_REGIONS[0]]:
        if (round(x, _SHARED_POINT_DP), round(y, _SHARED_POINT_DP)) == key:
            return (x, y)
    return None


def region_center(
        landmarks: Sequence[tuple[str, tuple[tuple[float, float], ...]]],
        region: str,
) -> tuple[float, float] | None:
    """The mean of one region's points, or None if it has none. Pure.

    Why a mean and not a bounding-box centre: the ring is sampled roughly evenly, so
    the mean is the centroid; a box centre would be pulled by whichever extreme the
    expression moved most, and an opening mouth moves exactly one side.
    """
    points = dict(landmarks).get(region)
    if not points:
        return None
    return (math.fsum(x for x, _y in points) / len(points),
            math.fsum(y for _x, y in points) / len(points))


def face_keypoints(face: Face) -> tuple[tuple[float, float], ...]:
    """The four key points of one face, in `FACE_KEYPOINTS` order. Pure.

    Contract: exactly len(FACE_KEYPOINTS) pairs, box-normalised, origin bottom left.
              A point that cannot be computed is (0, 0) - never omitted, because a
              short list would shift every later channel onto the wrong data, which
              is the failure the whole fixed-list discipline here exists to prevent.
    """
    if not (face.found and face.landmarks):
        return tuple(_ABSENT_POINT for _ in FACE_KEYPOINTS)
    regions = dict(face.landmarks)
    left = regions.get("left_pupil") or ()
    right = regions.get("right_pupil") or ()
    return (
        left[0] if left else _ABSENT_POINT,
        right[0] if right else _ABSENT_POINT,
        nose_tip_point(face.landmarks) or _ABSENT_POINT,
        region_center(face.landmarks, "inner_lips") or _ABSENT_POINT,
    )


def face_angles(face: Face, width_px: int = 0,
                height_px: int = 0) -> tuple[float, float, float]:
    """(pitch, yaw, roll) in DEGREES, from the key points. Pure.

    WHY NOT `observation.roll()` AND FRIENDS, which is where these used to come from:
    Vision QUANTISES them. MEASURED on a live face at revision 3, the highest the
    request offers - yaw arrives in 45-degree steps and roll in 30, so a head turned
    slowly reads 0, 0, 0, then -45. That is unusable for anything continuous, and no
    revision fixes it.

    These are our own arithmetic on continuous inputs, so they cannot be quantised.

    Contract: degrees. Roll is EXACT - a projection determines an in-plane rotation
              fully. Yaw is well behaved near centre and compresses towards the
              extremes. Pitch is APPROXIMATE and carries a population assumption -
              see below. All three are 0.0 for a face that was not found, or when a
              point they need is missing, which is the same convention every other
              channel here uses.
    Traps: the key points are normalised to the face's BOUNDING BOX, not the image
              (DESIGN.md 2.12), so they are composed through the box before any angle
              is taken. An angle measured in box coordinates is distorted by the
              box's aspect and is not an angle at all.
    Traps: NORMALISED IMAGE SPACE IS NOT SQUARE EITHER, and this one is easy to
              miss. x and y are each normalised by their own dimension, so a 1280x720
              frame stretches y by 16:9 against x - MEASURED on a real face, the
              eye-line-to-mouth over eye-separation ratio read 2.09 in normalised
              space where the anatomy is about 1.15. So the points go to PIXELS
              before any angle, using the frame size the caller passes. With no size
              given the angles are computed in normalised space and are consistent
              but not geometrically true; the sidecar always passes one.
    Ref: docs/ATTRIBUTES.md, and `appletd/derive.py` `_tilt` for the same honesty
              about what a 2D projection can and cannot determine.
    """
    if not face.found:
        return (0.0, 0.0, 0.0)
    points = face_keypoints(face)
    if any(point == _ABSENT_POINT for point in points):
        return (0.0, 0.0, 0.0)
    # Box-relative -> normalised image -> PIXELS, which is the only one of the three
    # where an angle is an angle. See the second trap above.
    scale_x = float(width_px) if width_px else 1.0
    scale_y = float(height_px) if height_px else 1.0
    eye_l, eye_r, nose, mouth = (
        ((face.bbox_x + x * face.bbox_w) * scale_x,
         (face.bbox_y + y * face.bbox_h) * scale_y)
        for x, y in points)

    # ROLL: the eye line, and nothing else is needed. Positive tilts the subject's
    # right eye UP in the image.
    across = (eye_r[0] - eye_l[0], eye_r[1] - eye_l[1])
    separation = math.hypot(*across)
    if separation <= 0.0:
        return (0.0, 0.0, 0.0)
    roll = math.degrees(math.atan2(across[1], across[0]))

    # The eye midpoint, and the face's own axes: `across` along the eye line, `down`
    # perpendicular to it. Measuring in THIS basis rather than the image's is what
    # keeps a rolled head from leaking roll into the other two.
    mid = ((eye_l[0] + eye_r[0]) / 2.0, (eye_l[1] + eye_r[1]) / 2.0)
    unit_across = (across[0] / separation, across[1] / separation)
    unit_down = (unit_across[1], -unit_across[0])
    offset = (nose[0] - mid[0], nose[1] - mid[1])
    along = offset[0] * unit_across[0] + offset[1] * unit_across[1]

    # YAW: the nose leaves the midline as the head turns. Normalised by HALF the eye
    # separation, so +-1 is the nose over an eye, and `asin` turns that into degrees
    # rather than a bare ratio.
    yaw = math.degrees(math.asin(max(-1.0, min(1.0, along / (separation / 2.0)))))

    # PITCH, and this is the approximate one. The vertical face - eye line to mouth -
    # FORESHORTENS as the head nods, so its length against the eye separation carries
    # the signal. What it cannot give is the resting ratio for THIS face, which varies
    # between people, so `_FACE_ASPECT_AT_REST` is a population figure and a stated
    # guess.
    #
    # SIGNED AND UNCLAMPED AT REST, which is the whole shape of this expression. The
    # obvious `acos(ratio / rest)` reads exactly 0 for every face LONGER than the
    # constant, whatever it does - MEASURED, 700 frames of a real face all reading
    # 0.00 - because the argument clamps at 1. Taking the DIFFERENCE from rest
    # instead is continuous through it: negative for a longer-than-average face at
    # rest, positive as a nod foreshortens it, and monotonic either way.
    #
    # So the DIRECTION and the CHANGE are trustworthy and the absolute magnitude is
    # not. Calibrating `rest` per face, from a frame where the head is known to be
    # level, is what would make it absolute.
    down_face = ((mouth[0] - mid[0]) * unit_down[0] + (mouth[1] - mid[1]) * unit_down[1])
    ratio = abs(down_face) / separation
    shortening = (_FACE_ASPECT_AT_REST - ratio) / _FACE_ASPECT_AT_REST
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, shortening))))
    return (pitch, yaw, roll)


# The eye-line-to-mouth distance over the INTERPUPILLARY distance, in pixels, for a
# face looking straight at the camera.
#
# MEASURED at 1.17 on one real face, and about 1.15 from published anatomy - so it is
# a POPULATION FIGURE, not a measurement of anyone. It only sets the scale of
# `face_angles`' pitch: too small and an upright face clamps at zero, too large and a
# nod reads shallow. A face longer than this reads 0 until it nods enough to
# foreshorten past it, which is the honest limit of doing this without calibrating
# per face from a frame where the head is known to be level.
_FACE_ASPECT_AT_REST: Final = 1.15


def face_channel_names() -> tuple[str, ...]:
    """The complete, fixed face channel list, in publication order.

    Contract: 3 + MAX_FACES * (len(FACE_SCALARS) + len(FACE_KEYPOINTS) * 2
              + REGION_SLOT_TOTAL * 2), which is **387**. It was 23 until the point
              counts were measured and 371 until the key points were added; both
              growths happened before anything consumed the stream, and the list is
              fixed now, like every other one here (DESIGN.md 6.2).
    Size: 12640 bytes on the wire, MEASURED - which is 77% of a 16 KB datagram and
              MORE than a default UDP send buffer allows. `osc.datagram_socket()` is
              what makes it sendable, and MAX_FACES = 2 is the practical ceiling for
              one bundle.
    """
    names: list[str] = list(_FRAME_SCALARS)
    for face_i in range(MAX_FACES):
        for scalar in FACE_SCALARS:
            names.append("f%d_%s" % (face_i, scalar))
        if not LANDMARKS_PUBLISHED:
            continue
        # BEFORE the regions, so the four points a project usually wants come
        # first in every channel list, every printed report and every Select.
        for point in FACE_KEYPOINTS:
            for axis in ("x", "y"):
                names.append("f%d_%s_%s" % (face_i, point, axis))
        for region in FACE_REGIONS:
            # `point_count is not None` is guaranteed by LANDMARKS_PUBLISHED; the
            # assertion is in _self_check rather than repeated here.
            for point_i in range(region.point_count or 0):
                for axis in ("x", "y"):
                    names.append("f%d_%s_%02d_%s"
                                 % (face_i, region.name, point_i, axis))
    return tuple(names)


def face_channel_values(frame: FaceFrame, age_ms: float,
                        n_faces: int) -> list[float]:
    """The values, in `face_channel_names()` order. Pure, and therefore testable.

    Contract: returns exactly len(face_channel_names()) floats. A region whose
              points are missing from a Face contributes zeros rather than being
              skipped, because skipping would shift every later channel onto the
              wrong data - the failure this whole fixed-list discipline exists to
              prevent.
    """
    values: list[float] = [float(n_faces), float(frame.seq), float(age_ms)]
    for face in frame.faces:
        values.append(1.0 if face.found else 0.0)
        values.append(float(face.confidence))
        values.append(float(face.quality))
        # x, y, z - pitch, yaw, roll. Paired by POSITION with FACE_SCALARS above.
        values.append(float(face.pitch_deg))
        values.append(float(face.yaw_deg))
        values.append(float(face.roll_deg))
        values.append(float(face.bbox_x))
        values.append(float(face.bbox_y))
        values.append(float(face.bbox_w))
        values.append(float(face.bbox_h))
        if not LANDMARKS_PUBLISHED:
            continue
        for point_x, point_y in face_keypoints(face):
            values.append(float(point_x))
            values.append(float(point_y))
        points = dict(face.landmarks)
        for region in FACE_REGIONS:
            found = points.get(region.name, ())
            for point_i in range(region.point_count or 0):
                if point_i < len(found):
                    values.append(float(found[point_i][0]))
                    values.append(float(found[point_i][1]))
                else:
                    values.extend((0.0, 0.0))
    return values


N_FACE_CHANNELS: Final = len(face_channel_names())


# ---------------------------------------------------------------------------
# Import-time self-check. `raise`, not `assert` - the same reasoning as types.py.
# ---------------------------------------------------------------------------
def _self_check() -> None:
    if len(set(FACE_REGION_NAMES)) != len(FACE_REGIONS):
        raise RuntimeError("duplicate region name in FACE_REGIONS")
    if len({r.accessor for r in FACE_REGIONS}) != len(FACE_REGIONS):
        raise RuntimeError("duplicate region accessor in FACE_REGIONS")
    counted = [r for r in FACE_REGIONS if r.point_count is not None]
    if counted and len(counted) != len(FACE_REGIONS):
        # Half a table is worse than none: it would publish some regions and
        # silently omit others, and the omission is invisible in a channel list.
        raise RuntimeError(
            "%d of %d face regions have a measured point_count. Fill in ALL of "
            "them or none - run tools/probe_face_regions.py"
            % (len(counted), len(FACE_REGIONS)))
    if LANDMARKS_PUBLISHED:
        total = sum(r.point_count or 0 for r in FACE_REGIONS)
        if total != REGION_SLOT_TOTAL:
            # The SLOT total, not the constellation's 76: the regions overlap, so
            # the sum legitimately exceeds the number of distinct points. Checking
            # against 76 here is what refused the correctly-measured table on the
            # first attempt. This still catches the thing worth catching - a typo in
            # a pasted count.
            raise RuntimeError(
                "the region point counts sum to %d, expected %d region slots "
                "(%d distinct points plus %d carried under two names). A count has "
                "been mistyped, or this macOS reports a different constellation - "
                "re-run tools/probe_face_regions.py."
                % (total, REGION_SLOT_TOTAL, CONSTELLATION_DISTINCT_POINTS,
                   REGION_SLOT_TOTAL - CONSTELLATION_DISTINCT_POINTS))
    if len(set(FACE_KEYPOINTS)) != len(FACE_KEYPOINTS):
        raise RuntimeError("duplicate name in FACE_KEYPOINTS")
    if not set(NOSE_TIP_REGIONS) <= set(FACE_REGION_NAMES):
        # A region renamed in FACE_REGIONS and not here would make `nose_tip_point`
        # return None on every frame, and the channel would read zero for ever with
        # nothing saying why.
        raise RuntimeError("NOSE_TIP_REGIONS names a region FACE_REGIONS does not: "
                           "%s" % ", ".join(sorted(set(NOSE_TIP_REGIONS)
                                                   - set(FACE_REGION_NAMES))))
    names = face_channel_names()
    if len(set(names)) != len(names):
        raise RuntimeError("duplicate face channel name")
    if len(face_channel_values(blank_face_frame(), 0.0, 0)) != len(names):
        raise RuntimeError("face_channel_values() and face_channel_names() disagree")


_self_check()
