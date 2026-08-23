"""The face data contract: head pose and bounding box now, landmarks pending one number.

The third of the stream contracts, after `types.py` (hands) and `pose_types.py`
(bodies), and the same boring shape: frozen dataclasses, tuples of strings, an
import-time self-check, nothing imported but the standard library.

WHY THIS ONE IS NOT JUST ANOTHER JOINT TABLE. Hands and bodies both come back as a
`VNRecognizedPointsObservation` - a flat dict of named joints, fixed count, known in
advance. A face does not. `VNFaceObservation.landmarks()` returns a
`VNFaceLandmarks2D` of **12 named REGIONS** (MEASURED, DESIGN.md 2.12), each a
`VNFaceLandmarkRegion2D` with its own `pointCount`, and **nothing in the framework
publishes those counts before a face has been seen**. The constellation is
selectable - 65 or 76 points, default 76 - so the TOTAL is pinned by our own
request, but the split across regions is not.

So this module shipped in two stages, and the second one is the reason the first
was worth the trouble:

  * `FACE_SCALARS` - found, confidence, capture quality, roll/yaw/pitch, and the
    bounding box. All of it comes off the observation itself, needs no landmarks,
    and is verified against the live framework at start-up by `face.py`. 23 channels.
  * `FACE_REGIONS` - the 12 regions, whose point counts sat at `None` for as long as
    nobody had measured them. **MEASURED 2026-08-21** by
    `tools/probe_face_regions.py` against a real face, and the numbers were not what
    a reasonable guess would have produced: the regions sum to 87 slots over 76
    distinct points, because they OVERLAP. 371 channels now.

There is no guessed count anywhere in this file, and the reason to hold that line
was borne out: a guessed count does not fail loudly - it lays a nose's points into
an eyebrow's channels and looks entirely plausible - and the first self-check this
module carried was written around an assumption (that the regions partition the
constellation) that turned out to be false. To RE-measure, on another macOS or after
a Vision update, run the probe again; it prints a paste-ready table.

ANGLES ARE DEGREES HERE AND RADIANS IN VISION. Every angle in this system is
degrees (docs/ATTRIBUTES.md), and `roll`/`yaw`/`pitch` arrive in radians. The
conversion happens once, in `face.py`, and getting it wrong is a silent 57x - the
numbers stay small and plausible and every downstream rotation is wrong.

Thread: everything here is immutable, so any object from this module can be read
        from any thread with no lock. Same mechanism as `types.py`.
Ref: DESIGN.md 6.4 (the stream contract), 2.12 (the API surface, measured).
"""

from __future__ import annotations

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
    below were measured on 2026-08-21; `tools/probe_face_regions.py` re-measures.
    """

    name: str
    accessor: str
    point_count: int | None = None


# The 12 regions, VERIFIED to exist on `VNFaceLandmarks2D` 2026-08-21 (DESIGN.md
# 2.12). Order is head-outward then top-down, and it will BE the channel order once
# the counts land, so it is pinned by a test now rather than after a project
# references it.
FACE_REGIONS: Final[tuple[FaceRegionSpec, ...]] = (
    # MEASURED 2026-08-21 by tools/probe_face_regions.py, against a real face on
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
# its own self-check. MEASURED 2026-08-21, both:
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
    "roll", "yaw", "pitch",
    # Normalised, origin BOTTOM LEFT, as Vision gives it. `bbox_y` is the BOTTOM
    # edge, not the top - the trap that DESIGN.md 7 exists for.
    "bbox_x", "bbox_y", "bbox_w", "bbox_h",
)
_FRAME_SCALARS: Final = ("face_n", "face_seq", "face_age_ms")


def face_channel_names() -> tuple[str, ...]:
    """The complete, fixed face channel list, in publication order.

    Contract: 3 + MAX_FACES * (len(FACE_SCALARS) + REGION_SLOT_TOTAL * 2), which is
              **371**. It was 23 until the point counts were measured, and that
              growth was a one-time event before anything consumed the stream -
              `Streamface` ships off and the COMP was new. The list is fixed now,
              like every other one here (DESIGN.md 6.2).
    Size: 12208 bytes on the wire, MEASURED - which is 75% of a 16 KB datagram and
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
        values.append(float(face.roll_deg))
        values.append(float(face.yaw_deg))
        values.append(float(face.pitch_deg))
        values.append(float(face.bbox_x))
        values.append(float(face.bbox_y))
        values.append(float(face.bbox_w))
        values.append(float(face.bbox_h))
        if not LANDMARKS_PUBLISHED:
            continue
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
    names = face_channel_names()
    if len(set(names)) != len(names):
        raise RuntimeError("duplicate face channel name")
    if len(face_channel_values(blank_face_frame(), 0.0, 0)) != len(names):
        raise RuntimeError("face_channel_values() and face_channel_names() disagree")


_self_check()
