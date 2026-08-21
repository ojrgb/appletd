"""Face detection: `VNDetectFaceLandmarksRequest` -> `FaceFrame`.

The third stream, built the same way as `pose.py`: it owns one Vision request and
the conversion out of pyobjc, and it does NOT own the camera - `engine.py` hands it
a sample buffer that has already been delivered, so every enabled stream sees the
same frame and shares its `seq` (DESIGN.md 6.4).

WHAT IT PUBLISHES: the observation's own numbers - confidence, capture quality,
roll, yaw, pitch and the bounding box - and, since the region point counts were
MEASURED on 2026-08-21, all 76 landmark points across the 12 regions. 371 channels.

The points spent a day unpublished on purpose, because their split across the
regions cannot be known before a face has been seen and this repo does not ship
guessed constants. `face_types.py` has that argument and the measurement, and it was
worth holding: the regions turned out to OVERLAP - 87 slots over 76 distinct
points - which no reasonable guess would have produced.

THE TWO UNITS TRAPS, both silent:

  * `roll`/`yaw`/`pitch` are RADIANS. Every angle in this system is DEGREES
    (docs/ATTRIBUTES.md). Converted once, here. Missed, it is a 57x error that
    keeps every number small and plausible.
  * they are also NSNumber-OR-NIL. `float(None)` raises, inside a capture
    callback, where an exception unwinding into Objective-C is its own crash. Each
    one is checked.

THE SAME BOUNDARY RULES AS engine.py: nothing from TouchDesigner, and no pyobjc
object may escape the capture thread. Everything crossing out of here is floats,
strings and tuples.

Thread: every function runs on the GCD capture queue, called from
        `HandEngine._on_sample_buffer`, except `FaceDetector.__init__` and
        `verify_face_regions`, which run on the caller's thread at construction so
        a framework mismatch is reported where it can be acted on.
Ref: DESIGN.md 2.12 (the API surface, measured), 6.4 (the stream contract).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

import Vision

from visionhands.engine import EngineError, ObjCObject
from visionhands.face_types import (
    CONSTELLATION_76,
    FACE_REGIONS,
    MAX_FACES,
    Face,
    FaceFrame,
    order_faces,
)
from visionhands.types import Confidence, NormX, NormY


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
def _degrees_or_zero(value: ObjCObject) -> float:
    """One of roll/yaw/pitch, in degrees. 0.0 when Vision returned nil.

    TRAP: these are NSNumber-or-nil, and `float(None)` raises TypeError inside a
          capture callback. Revision 3 supplies all three (MEASURED, DESIGN.md
          2.12), so nil should not occur - which is precisely why it would go
          unnoticed until the day it does.
    TRAP: RADIANS in, DEGREES out. The one conversion in this file, and a 57x
          silent error if it is skipped.
    """
    if value is None:
        return 0.0
    return math.degrees(float(value))


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


def face_from_observation(observation: ObjCObject) -> Face:
    """One VNFaceObservation -> one immutable Face.

    Thread: capture queue only.
    Contract: angles in DEGREES, bounding box normalised with the origin BOTTOM
              LEFT exactly as Vision reports it (DESIGN.md 7). Never returns None:
              unlike a hand, every field here comes off the observation directly,
              so there is no "unreadable" case to signal - a face Vision reports is
              a face we can publish.
    """
    box = observation.boundingBox()
    landmarks = observation.landmarks()
    regions: list[tuple[str, tuple[tuple[float, float], ...]]] = []
    if landmarks is not None:
        for region in FACE_REGIONS:
            regions.append((region.name, _region_points(landmarks, region.accessor)))

    quality = observation.faceCaptureQuality()
    return Face(
        confidence=Confidence(float(observation.confidence())),
        # NSNumber-or-nil as well: documented as nil when the revision cannot
        # produce it. Same treatment as the angles.
        quality=Confidence(0.0 if quality is None else float(quality)),
        roll_deg=_degrees_or_zero(observation.roll()),
        yaw_deg=_degrees_or_zero(observation.yaw()),
        pitch_deg=_degrees_or_zero(observation.pitch()),
        # CGRect origin is the BOTTOM-LEFT corner, normalised. Not the top.
        bbox_x=NormX(float(box.origin.x)),
        bbox_y=NormY(float(box.origin.y)),
        bbox_w=float(box.size.width),
        bbox_h=float(box.size.height),
        landmarks=tuple(regions),
        found=True,
    )


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
    faces = [face_from_observation(observation) for observation in observations]
    return FaceFrame(seq=seq, captured_at=captured_at, width=width_px,
                     height=height_px, faces=order_faces(faces))


def region_point_report(frame: FaceFrame) -> dict[str, object]:
    """What the regions actually contain, for the FIRST found face. Pure.

    The measurement that had to come after the counts, because the counts alone
    produced an arithmetic that did not add up: the 12 regions summed to 87 points
    on a request pinned to the 76-point constellation (MEASURED 2026-08-21). Two
    explanations fit, and they lead to different channel contracts:

      * the regions SHARE points - `medianLine` runs down the centre of the face and
        could plausibly repeat points that `faceContour`, `nose` and `noseCrest`
        also carry - in which case 87 is the sum with 11 duplicates and the
        constellation total is intact;
      * or the regions are disjoint and the "76-point constellation" does not mean
        what we assumed, in which case the total is 87 and the assumption goes.

    Distinct COORDINATES answer it without needing `allPoints`: if the distinct
    count is 76, the regions overlap.

    Returns counts per region, the sum, the distinct-coordinate count both exactly
    and rounded, and every overlapping pair with how many points it shares. Rounded
    as well as exact because two regions carrying "the same" point could differ in
    the last bit if Vision computes them separately - an almost-duplicate is a
    different finding from a duplicate, and it should be visible rather than
    rounded away silently.
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

    Thread: NOT thread-safe and does not need to be - one detector belongs to one
            serial capture queue.
    Why the LANDMARKS request rather than the cheaper rectangles one, when the
            landmark points are not published yet: the rectangles request would
            publish the same head pose today and would have to be swapped out the
            moment the point counts land, changing what the stream costs and what
            it means in the same edit. And `region_point_counts` above - the thing
            that settles the contract - needs the landmarks.
    Why its own VNSequenceRequestHandler: same reasoning as PoseDetector. Each
            detector self-contained, each timed separately, and one stream's
            failure cannot fail another's call.
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
                             height_px: int) -> FaceFrame:
        """The live path: the same CMSampleBuffer the other detectors just saw."""
        return self._detect(
            lambda: self._sequence.performRequests_onCMSampleBuffer_error_(
                [self._request], sample_buffer, None),    # TRAP: out-param
            seq, captured_at, width_px, height_px)

    def detect_pixel_buffer(self, pixel_buffer: ObjCObject, seq: int,
                            captured_at: float, width_px: int,
                            height_px: int) -> FaceFrame:
        """The replay path, and the one the probe tool uses."""
        return self._detect(
            lambda: self._sequence.performRequests_onCVPixelBuffer_error_(
                [self._request], pixel_buffer, None),     # TRAP: out-param
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
