"""Face detection: `VNDetectFaceLandmarksRequest` -> `FaceFrame`.

Owns one Vision request and the conversion out of pyobjc. It does not own the camera:
`engine.py` hands it a sample buffer that has already been delivered, so every enabled
stream sees the same frame and shares its `seq`.

Publishes 387 channels - confidence, capture quality, the three head angles, the
bounding box, 76 landmark points across 12 regions, and four computed key points.

Two units traps, both silent if missed:

  * Vision reports the head angles in RADIANS; every angle this project publishes is
    DEGREES. Converted once, here.
  * they arrive as NSNumber-or-nil, and `float(None)` raises inside a capture
    callback. Each is checked.

Nothing from TouchDesigner, and no pyobjc object leaves the capture thread: everything
crossing out of here is floats, strings and tuples.

Thread: the GCD capture queue, except `FaceDetector.__init__` and
        `verify_face_regions`, which run on the caller's thread at construction.
Ref: DESIGN.md 2.12, 6.4. docs/internals/vision-notes.md for how the region counts
     were established.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

import Vision

from appletd.engine import EngineError, ObjCObject
from appletd.face_types import (
    CONSTELLATION_76,
    FACE_KEYPOINTS,
    FACE_REGION_NAMES,
    FACE_REGIONS,
    MAX_FACES,
    NOSE_TIP_REGIONS,
    Face,
    FaceFrame,
    face_angles,
    face_keypoints,
    nose_tip_point,
    order_faces,
)
from appletd.streams import ORIENTATION_UP
from appletd.types import Confidence, NormX, NormY


# ---------------------------------------------------------------------------
# Start-up verification
# ---------------------------------------------------------------------------
def verify_face_regions() -> None:
    """Check that this Vision has every region and accessor our table names.

    Why: `face_types.py` names 12 regions and the selector for each. A renamed or
         missing accessor would make that region silently absent from every face -
         `_region_points` skips what it cannot read - and an absent region is
         invisible in a channel list of zeros.
    What it CANNOT check: the point counts. Nothing in the framework publishes them
         before a face has been observed, which is exactly why they are None in the
         table rather than guessed (see that module's docstring).
    Cost: 12 attribute lookups, once per detector. Not per frame.
    Raises: EngineError listing every mismatch rather than the first.
    """
    problems: list[str] = []
    landmarks_class = getattr(Vision, "VNFaceLandmarks2D", None)
    if landmarks_class is None:
        raise EngineError("this Vision has no VNFaceLandmarks2D at all")
    for region in FACE_REGIONS:
        if not hasattr(landmarks_class, region.accessor):
            problems.append("%s: no VNFaceLandmarks2D.%s in this Vision"
                            % (region.name, region.accessor))

    for name in ("boundingBox", "roll", "yaw", "pitch", "confidence",
                 "faceCaptureQuality", "landmarks"):
        if not hasattr(Vision.VNFaceObservation, name):
            problems.append("VNFaceObservation has no %s" % name)

    if problems:
        raise EngineError(
            "the face table in face_types.py disagrees with this macOS's Vision "
            "framework, so channels would be silently empty:\n  "
            + "\n  ".join(problems))


# ---------------------------------------------------------------------------
# Observations -> our own immutable types
#
# This is the boundary. Above it, pyobjc objects. Below it, floats and tuples.
# ---------------------------------------------------------------------------
def _region_points(landmarks: ObjCObject,
                   accessor: str) -> tuple[tuple[float, float], ...]:
    """One region's normalised points, or () if this Vision has no such region.

    Contract: points are normalised to the FACE's bounding box, not to the image -
              that is what `VNFaceLandmarkRegion2D.normalizedPoints` means, and
              `VNImagePointForFaceLandmarkPoint` exists precisely because the two
              are different. Left as Vision gives them: converting to image space
              here would bake in a resolution the consumer may not want, and the
              bounding box travels in the same frame for anyone who needs it.
    """
    region = getattr(landmarks, accessor, None)
    if region is None:
        return ()
    handle = region() if callable(region) else region
    if handle is None:
        return ()
    count = int(handle.pointCount())
    points = handle.normalizedPoints()
    out: list[tuple[float, float]] = []
    for index in range(count):
        point = points[index]
        # float() on both: nothing from pyobjc survives this function.
        out.append((float(point.x), float(point.y)))
    return tuple(out)


def face_from_observation(observation: ObjCObject, width_px: int = 0,
                          height_px: int = 0) -> Face:
    """One VNFaceObservation -> one immutable Face.

    Thread: capture queue only.
    Contract: angles in DEGREES, bounding box normalised with the origin BOTTOM
              LEFT exactly as Vision reports it (DESIGN.md 7). Never returns None:
              unlike a hand, every field here comes off the observation directly,
              so there is no "unreadable" case to signal - a face Vision reports is
              a face we can publish.
    Traps: the FRAME SIZE is needed, and only for the angles. They are measured in
              pixels because normalised image space is not square - see
              `face_types.face_angles`. Defaulted so a test can build a Face without
              one and get consistent, if not geometrically true, angles.
    """
    box = observation.boundingBox()
    landmarks = observation.landmarks()
    regions: list[tuple[str, tuple[tuple[float, float], ...]]] = []
    if landmarks is not None:
        for region in FACE_REGIONS:
            regions.append((region.name, _region_points(landmarks, region.accessor)))

    quality = observation.faceCaptureQuality()
    face = Face(
        confidence=Confidence(float(observation.confidence())),
        # NSNumber-or-nil as well: documented as nil when the revision cannot
        # produce it. Same treatment as the angles.
        quality=Confidence(0.0 if quality is None else float(quality)),
        # ZERO HERE, filled in from the LANDMARKS below. Vision's own roll, yaw and
        # pitch are QUANTISED - MEASURED at revision 3, yaw in 45-degree steps and
        # roll in 30 - so `face_angles` recomputes all three from the key points,
        # which cannot be quantised because the arithmetic is ours.
        roll_deg=0.0,
        yaw_deg=0.0,
        pitch_deg=0.0,
        # CGRect origin is the BOTTOM-LEFT corner, normalised. Not the top.
        bbox_x=NormX(float(box.origin.x)),
        bbox_y=NormY(float(box.origin.y)),
        bbox_w=float(box.size.width),
        bbox_h=float(box.size.height),
        landmarks=tuple(regions),
        found=True,
    )
    # AND NOW THE ANGLES, from the landmarks that face carries. Two passes because
    # `face_angles` needs the box and the key points, and both come off the Face - so
    # it is built once, measured, and replaced with the same thing plus its
    # orientation. `dataclasses.replace` and not mutation: a Face is frozen.
    pitch, yaw, roll = face_angles(face, width_px, height_px)
    return replace(face, pitch_deg=pitch, yaw_deg=yaw, roll_deg=roll)


def face_frame_from_observations(observations: list[ObjCObject], seq: int,
                                 captured_at: float, width_px: int,
                                 height_px: int) -> FaceFrame:
    """Observations -> one FaceFrame with exactly MAX_FACES slots.

    Thread: capture queue only.
    Contract: faces is always MAX_FACES long, ordered LEFT TO RIGHT by bounding-box
              centre, padded with the shared BLANK_FACE. Vision returns faces in no
              stable order and gives no tracking ID, so publishing its order would
              make `f0` swap between two people - the defect DESIGN.md 6.3 exists
              to fix for hands, where chirality solves it and here nothing does.
    """
    faces = [face_from_observation(observation, width_px, height_px)
             for observation in observations]
    return FaceFrame(seq=seq, captured_at=captured_at, width=width_px,
                     height=height_px, faces=order_faces(faces))


def region_point_report(frame: FaceFrame) -> dict[str, object]:
    """What the regions actually contain, for the FIRST found face. Pure.

    Returns counts per region, their sum, the distinct-coordinate count both exactly
    and rounded, and every overlapping pair with how many points it shares.

    The distinct count is what settles whether the regions overlap: 12 regions summing
    to 87 points on a 76-point constellation means either 11 shared points or a
    constellation that is not 76. Rounded as well as exact, because two regions
    carrying "the same" point may differ in the last bit - an almost-duplicate is a
    different finding from a duplicate.
    """
    for face in frame.faces:
        if not (face.found and face.landmarks):
            continue
        counts = {name: len(points) for name, points in face.landmarks}
        every: list[tuple[float, float]] = [
            point for _name, points in face.landmarks for point in points]
        rounded = [(round(x, 6), round(y, 6)) for x, y in every]
        overlaps = {}
        for i, (name_a, points_a) in enumerate(face.landmarks):
            for name_b, points_b in face.landmarks[i + 1:]:
                shared = len({(round(x, 6), round(y, 6)) for x, y in points_a}
                             & {(round(x, 6), round(y, 6)) for x, y in points_b})
                if shared:
                    overlaps["%s + %s" % (name_a, name_b)] = shared
        return {
            "counts": counts,
            "sum": len(every),
            "distinct_exact": len(set(every)),
            "distinct_rounded": len(set(rounded)),
            "overlaps": overlaps,
        }
    return {}


def keypoint_report(frame: FaceFrame) -> dict[str, object]:
    """Where each of the four key points came from, for the FIRST found face. Pure.

    The measurement `tools/probe_face_regions.py --keypoints` prints, and the only
    thing in this system that can answer two questions a fixture cannot:

      * WHICH INDEX the nose tip occupies in each of its three regions. Nothing
        depends on the answer - `face_types.nose_tip_point` finds the point by
        intersection, deliberately - but it is worth having written down once, so
        the next person can see that the intersection found the point they would
        have picked by eye.
      * WHETHER `leftPupil` is Vision's left or the IMAGE's left. `face_types.py`
        publishes `f{i}_eye_left` as a faithful mirror of the region name and says
        in as many words that which side of the frame that is has not been
        measured. This is what measures it, and the answer belongs in
        docs/ATTRIBUTES.md rather than in a guess.

    Returns {} when no face in the frame has landmarks, the same as
    `region_point_report`.
    """
    for face in frame.faces:
        if not (face.found and face.landmarks):
            continue
        regions = dict(face.landmarks)
        tip = nose_tip_point(face.landmarks)
        indices: dict[str, int | None] = {}
        for name in NOSE_TIP_REGIONS:
            indices[name] = None
            if tip is None:
                continue
            for index, point in enumerate(regions.get(name, ())):
                if (round(point[0], 6), round(point[1], 6)) == (round(tip[0], 6),
                                                                round(tip[1], 6)):
                    indices[name] = index
                    break
        left = regions.get("left_pupil") or ()
        right = regions.get("right_pupil") or ()
        return {
            "points": dict(zip(FACE_KEYPOINTS, face_keypoints(face), strict=True)),
            "nose_tip": tip,
            "nose_tip_indices": indices,
            # None when either pupil is missing: an unanswerable question must not
            # come back as a confident False.
            "left_pupil_is_right_of_image_centre": (
                None if not (left and right) else left[0][0] > right[0][0]),
            "missing": [name for name in FACE_REGION_NAMES
                        if not regions.get(name)],
        }
    return {}


def region_point_counts(frame: FaceFrame) -> dict[str, int]:
    """What each region actually carried, for the FIRST found face. Pure.

    The measurement `face_types.FACE_REGIONS` is waiting for, and the reason the
    conversion above reads the regions even though their points are not published
    yet: `tools/probe_face_regions.py` calls this and prints the answer, so
    settling the contract costs one camera frame and writes no image.

    Returns {} when nothing was found, which is the honest answer rather than a
    dictionary of zeros that looks like a measurement.
    """
    for face in frame.faces:
        if face.found and face.landmarks:
            return {name: len(points) for name, points in face.landmarks}
    return {}


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------
class FaceDetector:
    """Holds the face-landmarks request and turns buffers into FaceFrames.

    Thread: NOT thread-safe, and does not need to be - one detector belongs to one
            serial capture queue.
    Its own `VNSequenceRequestHandler`, like every other detector: each is timed
            separately, and one stream's failure cannot fail another's call.
    """

    def __init__(self) -> None:
        verify_face_regions()
        self._sequence = Vision.VNSequenceRequestHandler.alloc().init()
        self._request = Vision.VNDetectFaceLandmarksRequest.alloc().init()
        # PINNED rather than left at the default, so the total point count is a
        # decision of ours instead of something to discover - even though the
        # default happens to be this today (MEASURED, DESIGN.md 2.12).
        self._request.setConstellation_(CONSTELLATION_76)
        self.revision = int(self._request.revision())
        self.constellation = int(self._request.constellation())
        self.last_inference_ms = 0.0
        # Faces beyond MAX_FACES, dropped after the left-to-right sort. Counted
        # because "a third face is in shot and is not in the channels" is invisible
        # otherwise.
        self.n_faces_dropped = 0

    def detect_sample_buffer(self, sample_buffer: ObjCObject, seq: int,
                             captured_at: float, width_px: int,
                             height_px: int,
                             orientation: int = ORIENTATION_UP) -> FaceFrame:
        """The live path: the same CMSampleBuffer the other detectors just saw."""
        return self._detect(
            lambda: self._sequence.performRequests_onCMSampleBuffer_orientation_error_(
                [self._request], sample_buffer, orientation, None),    # TRAP: out-param
            seq, captured_at, width_px, height_px)

    def detect_pixel_buffer(self, pixel_buffer: ObjCObject, seq: int,
                            captured_at: float, width_px: int,
                            height_px: int,
                            orientation: int = ORIENTATION_UP) -> FaceFrame:
        """The replay path, and the one the probe tool uses."""
        return self._detect(
            lambda: self._sequence.performRequests_onCVPixelBuffer_orientation_error_(
                [self._request], pixel_buffer, orientation, None),     # TRAP: out-param
            seq, captured_at, width_px, height_px)

    def _detect(self, perform: Callable[[], tuple[bool, ObjCObject]], seq: int,
                captured_at: float, width_px: int, height_px: int) -> FaceFrame:
        """Time the request, check it, convert the results. Shared by both paths."""
        started_s = time.perf_counter()
        ok, err = perform()
        self.last_inference_ms = (time.perf_counter() - started_s) * 1e3
        if not ok:
            raise EngineError("Vision face performRequests failed: %s" % (err,))

        results = list(self._request.results() or [])
        if len(results) > MAX_FACES:
            self.n_faces_dropped += len(results) - MAX_FACES
        return face_frame_from_observations(results, seq, captured_at,
                                            width_px, height_px)
