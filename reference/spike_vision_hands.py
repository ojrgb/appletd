#!/usr/bin/env python
"""Spike: hand, body and face landmarks out of Apple's Vision framework via pyobjc.

Throwaway interface validation - one image in, one annotated PNG out, plus a
landmark table and timing figures on stdout. No realtime loop, no other ML stack.

Run with the spike venv (NOT the repo .venv):
    ~/.venvs/vision-spike/bin/python reference/spike_vision_hands.py temp/hand.jpg

All three detectors run by default; each is turnoffable, and the timing section
prices them individually as well as together:
    --no-hands / --no-body / --no-face      drop a detector
    --body-3d                               add the 3D body request (opt in)
    --face-points 65                        the smaller face constellation

WHY ONE HANDLER AND NOT ONE PASS PER DETECTOR. Vision takes an ARRAY of requests
per image, so hands, body and face come off the same decode. Running them as
three separate handlers pays for the image setup three times for nothing, and
that is the shape the live spike and any TouchDesigner integration must copy.

MEASURED on one 1280x720 frame, still-image path, 20 runs each (see DESIGN.md 2
for why the live CMSampleBuffer path is roughly half these numbers):

    hands            5.4 ms      hands+body+face  12.4 ms
    body             4.4 ms      body3d          94.0 ms
    face             5.4 ms      (both face requests)

Two findings worth having before you build on any of this:

  * --body-3d costs ~94 ms A FRAME, twenty times the 2D request, whether or not
    it finds anybody. It cannot hold 30 fps and is not a realtime option. It
    also only works on THIS path: fed the same pixels as a CVPixelBuffer or a
    CMSampleBuffer it returns zero observations, while a CGImage or raw file
    data returns a full skeleton (measured on one frame; the face request
    returned its observation through all four containers, so the buffers are
    fine). Whatever it needs, an image handler over a CGImage provides and a
    camera buffer does not.
  * The 2D body request wants more of a body than a webcam crop shows: on a
    head-and-shoulders frame it returned NOTHING while the 3D request returned a
    full 17-joint skeleton by extrapolating everything below the chin - joints
    that project thousands of pixels off the image and are pure invention.

Built against pyobjc 12.2.2 / macOS 26.5.2 on Apple silicon.
"""

import argparse
import statistics
import time
from pathlib import Path

import Quartz
import Vision
from Foundation import NSURL
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Joint identifiers.
#
# The public constants are long; their *values* are short internal codes
# ('VNHLKWRI' etc) and it is those codes that come back as the keys of the
# recognised-points dict. Two of them are a hazard: ThumbIP is 'VNHLKTIP' and
# IndexTip is 'VNHLKITIP' - one character apart - and the little finger is
# internally 'P' for pinky. Always go through the constants, never hand-type
# the codes.
# ---------------------------------------------------------------------------
V = Vision
JOINTS = [
    ("wrist", V.VNHumanHandPoseObservationJointNameWrist),
    ("thumb_cmc", V.VNHumanHandPoseObservationJointNameThumbCMC),
    ("thumb_mp", V.VNHumanHandPoseObservationJointNameThumbMP),
    ("thumb_ip", V.VNHumanHandPoseObservationJointNameThumbIP),
    ("thumb_tip", V.VNHumanHandPoseObservationJointNameThumbTip),
    ("index_mcp", V.VNHumanHandPoseObservationJointNameIndexMCP),
    ("index_pip", V.VNHumanHandPoseObservationJointNameIndexPIP),
    ("index_dip", V.VNHumanHandPoseObservationJointNameIndexDIP),
    ("index_tip", V.VNHumanHandPoseObservationJointNameIndexTip),
    ("middle_mcp", V.VNHumanHandPoseObservationJointNameMiddleMCP),
    ("middle_pip", V.VNHumanHandPoseObservationJointNameMiddlePIP),
    ("middle_dip", V.VNHumanHandPoseObservationJointNameMiddleDIP),
    ("middle_tip", V.VNHumanHandPoseObservationJointNameMiddleTip),
    ("ring_mcp", V.VNHumanHandPoseObservationJointNameRingMCP),
    ("ring_pip", V.VNHumanHandPoseObservationJointNameRingPIP),
    ("ring_dip", V.VNHumanHandPoseObservationJointNameRingDIP),
    ("ring_tip", V.VNHumanHandPoseObservationJointNameRingTip),
    ("little_mcp", V.VNHumanHandPoseObservationJointNameLittleMCP),
    ("little_pip", V.VNHumanHandPoseObservationJointNameLittlePIP),
    ("little_dip", V.VNHumanHandPoseObservationJointNameLittleDIP),
    ("little_tip", V.VNHumanHandPoseObservationJointNameLittleTip),
]

# (label, colour) per finger, then the bone chains by label name.
FINGERS = [
    ("thumb", (255, 96, 96), ["wrist", "thumb_cmc", "thumb_mp", "thumb_ip", "thumb_tip"]),
    ("index", (255, 196, 64), ["wrist", "index_mcp", "index_pip", "index_dip", "index_tip"]),
    ("middle", (96, 230, 120), ["wrist", "middle_mcp", "middle_pip", "middle_dip", "middle_tip"]),
    ("ring", (96, 176, 255), ["wrist", "ring_mcp", "ring_pip", "ring_dip", "ring_tip"]),
    ("little", (208, 128, 255), ["wrist", "little_mcp", "little_pip", "little_dip", "little_tip"]),
]
# Knuckle line across the palm, so a vertical flip is obvious at a glance.
PALM = ["index_mcp", "middle_mcp", "ring_mcp", "little_mcp"]

CHIRALITY = {
    V.VNChiralityLeft: "left",
    V.VNChiralityRight: "right",
    V.VNChiralityUnknown: "unknown",
}

# ---------------------------------------------------------------------------
# Body joint identifiers - 19 of them, 2D.
#
# The same constant-vs-value split as the hands, with a nastier twist: here the
# internal codes are borrowed from a skeletal rig and DISAGREE with the public
# names. Nose is 'head_joint', Elbow is 'forearm_joint', Wrist is 'hand_joint',
# Hip is 'upLeg_joint', Knee is 'leg_joint', Ankle is 'foot_joint'. Reading the
# codes as anatomy will put your elbow where your forearm is. Go through the
# constants; the label on the left is ours, for output only.
# ---------------------------------------------------------------------------
BODY_JOINTS = [
    ("nose", V.VNHumanBodyPoseObservationJointNameNose),
    ("left_eye", V.VNHumanBodyPoseObservationJointNameLeftEye),
    ("right_eye", V.VNHumanBodyPoseObservationJointNameRightEye),
    ("left_ear", V.VNHumanBodyPoseObservationJointNameLeftEar),
    ("right_ear", V.VNHumanBodyPoseObservationJointNameRightEar),
    ("neck", V.VNHumanBodyPoseObservationJointNameNeck),
    ("left_shoulder", V.VNHumanBodyPoseObservationJointNameLeftShoulder),
    ("right_shoulder", V.VNHumanBodyPoseObservationJointNameRightShoulder),
    ("left_elbow", V.VNHumanBodyPoseObservationJointNameLeftElbow),
    ("right_elbow", V.VNHumanBodyPoseObservationJointNameRightElbow),
    ("left_wrist", V.VNHumanBodyPoseObservationJointNameLeftWrist),
    ("right_wrist", V.VNHumanBodyPoseObservationJointNameRightWrist),
    ("root", V.VNHumanBodyPoseObservationJointNameRoot),
    ("left_hip", V.VNHumanBodyPoseObservationJointNameLeftHip),
    ("right_hip", V.VNHumanBodyPoseObservationJointNameRightHip),
    ("left_knee", V.VNHumanBodyPoseObservationJointNameLeftKnee),
    ("right_knee", V.VNHumanBodyPoseObservationJointNameRightKnee),
    ("left_ankle", V.VNHumanBodyPoseObservationJointNameLeftAnkle),
    ("right_ankle", V.VNHumanBodyPoseObservationJointNameRightAnkle),
]

# Left limbs warm, right limbs cool, spine neutral - so a left/right mirror is
# as obvious at a glance as the palm line makes a vertical flip.
BODY_BONES = [
    ((235, 235, 235), ["neck", "root"]),
    ((235, 235, 235), ["left_shoulder", "neck", "right_shoulder"]),
    ((235, 235, 235), ["left_hip", "root", "right_hip"]),
    ((255, 150, 90), ["left_shoulder", "left_elbow", "left_wrist"]),
    ((255, 150, 90), ["left_hip", "left_knee", "left_ankle"]),
    ((90, 190, 255), ["right_shoulder", "right_elbow", "right_wrist"]),
    ((90, 190, 255), ["right_hip", "right_knee", "right_ankle"]),
    ((190, 255, 140), ["left_ear", "left_eye", "nose", "right_eye", "right_ear"]),
]

# ---------------------------------------------------------------------------
# 3D body joints - 17 of them, a DIFFERENT and smaller set than the 2D request:
# no ears, no eyes, no nose; a spine and a top-of-head instead. Do not assume
# one maps onto the other.
# ---------------------------------------------------------------------------
BODY3D_JOINTS = [
    ("top_head", V.VNHumanBodyPose3DObservationJointNameTopHead),
    ("center_head", V.VNHumanBodyPose3DObservationJointNameCenterHead),
    ("center_shoulder", V.VNHumanBodyPose3DObservationJointNameCenterShoulder),
    ("left_shoulder", V.VNHumanBodyPose3DObservationJointNameLeftShoulder),
    ("right_shoulder", V.VNHumanBodyPose3DObservationJointNameRightShoulder),
    ("left_elbow", V.VNHumanBodyPose3DObservationJointNameLeftElbow),
    ("right_elbow", V.VNHumanBodyPose3DObservationJointNameRightElbow),
    ("left_wrist", V.VNHumanBodyPose3DObservationJointNameLeftWrist),
    ("right_wrist", V.VNHumanBodyPose3DObservationJointNameRightWrist),
    ("spine", V.VNHumanBodyPose3DObservationJointNameSpine),
    ("root", V.VNHumanBodyPose3DObservationJointNameRoot),
    ("left_hip", V.VNHumanBodyPose3DObservationJointNameLeftHip),
    ("right_hip", V.VNHumanBodyPose3DObservationJointNameRightHip),
    ("left_knee", V.VNHumanBodyPose3DObservationJointNameLeftKnee),
    ("right_knee", V.VNHumanBodyPose3DObservationJointNameRightKnee),
    ("left_ankle", V.VNHumanBodyPose3DObservationJointNameLeftAnkle),
    ("right_ankle", V.VNHumanBodyPose3DObservationJointNameRightAnkle),
]

BODY3D_BONES = [
    ((235, 235, 235), ["top_head", "center_head", "center_shoulder", "spine", "root"]),
    ((235, 235, 235), ["left_shoulder", "center_shoulder", "right_shoulder"]),
    ((235, 235, 235), ["left_hip", "root", "right_hip"]),
    ((255, 150, 90), ["left_shoulder", "left_elbow", "left_wrist"]),
    ((255, 150, 90), ["left_hip", "left_knee", "left_ankle"]),
    ((90, 190, 255), ["right_shoulder", "right_elbow", "right_wrist"]),
    ((90, 190, 255), ["right_hip", "right_knee", "right_ankle"]),
]

HEIGHT_ESTIMATION = {0: "reference", 1: "measured"}

# ---------------------------------------------------------------------------
# Face landmark regions.
#
# (accessor, colour, closed) - `closed` says whether the region is a loop that
# should be drawn back to its first point (eyes, lips) or an open polyline
# (contour, brows, nose ridge). The two pupils are single points.
#
# The point count is NOT fixed per region: it depends on the constellation, so
# always read pointCount() rather than assuming a layout.
# ---------------------------------------------------------------------------
FACE_REGIONS = [
    ("faceContour", (200, 200, 210), False),
    ("leftEyebrow", (170, 230, 255), False),
    ("rightEyebrow", (170, 230, 255), False),
    ("leftEye", (90, 220, 255), True),
    ("rightEye", (90, 220, 255), True),
    ("leftPupil", (255, 255, 255), False),
    ("rightPupil", (255, 255, 255), False),
    ("nose", (255, 210, 130), False),
    ("noseCrest", (255, 210, 130), False),
    ("medianLine", (150, 150, 160), False),
    ("outerLips", (255, 120, 150), True),
    ("innerLips", (255, 180, 200), True),
]

FACE_CONSTELLATIONS = {
    65: V.VNRequestFaceLandmarksConstellation65Points,
    76: V.VNRequestFaceLandmarksConstellation76Points,
}

# Head orientation comes from a SECOND request, and only at one revision.
# MEASURED, on one 1280x720 frame with a real head in it, every revision of both
# requests:
#
#   VNDetectFaceLandmarksRequest   rev 1, 2, 3  ->  roll 0.0   yaw  0.0   pitch None
#   VNDetectFaceRectanglesRequest  rev 1, 2     ->  roll 0.0   yaw  0.0   pitch None
#   VNDetectFaceRectanglesRequest  rev 3        ->  roll 0.037 yaw -0.045 pitch 0.407
#
# A landmarks request on its own therefore reports "head perfectly level, pitch
# unknown" for every head in every frame - the same trap as h0_score in
# DESIGN.md 2.6, a channel that looks like a signal and is a constant.
#
# Stranger, and also measured: pairing the two requests in ONE handler call
# upgrades the landmarks observation's own angles to the real values, in either
# array order, and a rev-1 or rev-2 rectangles request does not do it. The
# detection is evidently shared within a handler call. This spike still reads
# the angles off the rectangles observation, because leaning on a cross-request
# side effect that Apple never documented is a bad bet for anything downstream.
FACE_POSE_REVISION = 3

DETECTOR_ORDER = ("hands", "body", "body3d", "face")


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------
def make_requests(kind, max_hands=2, face_points=76, no_cache=False):
    """The Vision request(s) for one short detector name. Shared with the live spike.

    Returns a LIST, because "face" is two requests: landmarks, plus the rev-3
    rectangles request that is the only source of real head angles (see
    FACE_POSE_REVISION above). Everything else is a list of one.

    no_cache defeats Vision's result cache, which matters ONLY when timing the
    same still image repeatedly - a repeated identical request can be served
    from cache and the timing loop then measures nothing. Live frames always
    differ, so the live path leaves it alone.
    """
    if kind == "hands":
        req = Vision.VNDetectHumanHandPoseRequest.alloc().init()
        req.setMaximumHandCount_(max_hands)
        reqs = [req]
    elif kind == "body":
        reqs = [Vision.VNDetectHumanBodyPoseRequest.alloc().init()]
    elif kind == "body3d":
        # TRAP: alone among these, this request's -init is NS_UNAVAILABLE, and
        # pyobjc surfaces that as `TypeError: 'init' is NS_UNAVAILABLE` at the
        # alloc().init() call. The completion-handler initialiser is the only
        # one; None is a legal handler because we read .results() ourselves.
        reqs = [Vision.VNDetectHumanBodyPose3DRequest.alloc()
                .initWithCompletionHandler_(None)]
    elif kind == "face":
        lm = Vision.VNDetectFaceLandmarksRequest.alloc().init()
        lm.setConstellation_(FACE_CONSTELLATIONS[face_points])
        # supportedRevisions is a CLASS method - asking the instance for it is
        # an AttributeError, not a fallback.
        available = list(Vision.VNDetectFaceRectanglesRequest.supportedRevisions())
        pose = Vision.VNDetectFaceRectanglesRequest.alloc().init()
        if FACE_POSE_REVISION in available:
            pose.setRevision_(FACE_POSE_REVISION)
        else:
            print("WARNING: no revision %d rectangles request here (have %s) - "
                  "head angles will be missing" % (FACE_POSE_REVISION, available))
        reqs = [lm, pose]
    else:
        raise ValueError("unknown detector: %s" % kind)
    if no_cache:
        for req in reqs:
            if hasattr(req, "setAllowsCachingOfResults_"):
                req.setAllowsCachingOfResults_(False)
    return reqs


def load_cgimage(path):
    """Decode to a CGImage. No EXIF transform is applied - the CGImage is the
    raw stored pixel buffer, which is what we also hand to PIL for drawing, so
    the two stay in the same frame of reference."""
    url = NSURL.fileURLWithPath_(str(path))
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        raise SystemExit("could not open image source: %s" % path)
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if cg is None:
        raise SystemExit("could not decode image: %s" % path)
    return cg


def detect(cg, requests):
    """One detection pass over a whole request list. Returns elapsed seconds.

    Results are read back off the request objects by the caller, which is why
    this returns only the timing. performRequests_error_ is a pyobjc
    out-parameter method: pass None for the trailing error: and get a tuple.
    """
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    t0 = time.perf_counter()
    ok, err = handler.performRequests_error_(list(requests), None)   # <- out-param
    dt = time.perf_counter() - t0
    if not ok:
        raise SystemExit("performRequests failed: %s" % err)
    return dt


# ---------------------------------------------------------------------------
# reading observations
# ---------------------------------------------------------------------------
def points_for(obs):
    """All 21 hand joints in one call via the group accessor. Also an out-param."""
    pts, err = obs.recognizedPointsForJointsGroupName_error_(
        Vision.VNHumanHandPoseObservationJointsGroupNameAll, None)   # <- out-param
    if err is not None:
        raise SystemExit("recognizedPointsForJointsGroupName failed: %s" % err)
    return pts


def body_points_for(obs):
    """All 19 body joints in one call. Same shape of API as the hands, different
    group constant - and a different (rig-derived) set of internal codes."""
    pts, err = obs.recognizedPointsForJointsGroupName_error_(
        Vision.VNHumanBodyPoseObservationJointsGroupNameAll, None)   # <- out-param
    if err is not None or pts is None:
        return {}
    return pts


def to_pixels(x, y, w, h):
    """Vision normalises to [0,1] with the origin at the BOTTOM-left, so the y
    axis runs opposite to image row order. Flip it to get a row index."""
    return x * w, (1.0 - y) * h


def face_region_points(region, w, h, flip=True):
    """VNFaceLandmarkRegion2D -> [(px, py), ...].

    THREE traps, and the first one is silent. normalizedPoints() is normalised
    to the FACE BOUNDING BOX, not to the image, unlike every other normalised
    coordinate in Vision - so scaling it by the image size scatters the
    landmarks across the whole frame, which reads as a detection problem rather
    than a units problem. pointsInImageOfSize_ does the box-to-image map itself,
    and is why this function is two lines instead of ten. MEASURED on one frame:
    pointsInImageOfSize_((w, h)) matches
    (box.origin + norm * box.size) * (w, h) to a tenth of a pixel.

    Second: pyobjc returns that C array of CGPoint as an objc.varlist, which
    carries no length of its own, so it MUST be indexed against pointCount() -
    read past that and you walk off the end of the buffer instead of getting an
    IndexError. Third: the elements are plain CGPoint structs, so coordinates
    are ATTRIBUTES (p.x), whereas hand and body joints are VNRecognizedPoint
    objects where they are METHODS (p.x()).

    "InImage" still means bottom-left origin, hence h - y rather than a scale.
    """
    n = int(region.pointCount()) if region is not None else 0
    if n == 0:
        return []
    raw = region.pointsInImageOfSize_((w, h))
    return [(raw[i].x, h - raw[i].y) if flip else (raw[i].x, raw[i].y)
            for i in range(n)]


def face_landmarks(obs, w, h, flip=True):
    """One face observation -> {region_name: [(px, py), ...]}."""
    lm = obs.landmarks()
    if lm is None:
        return {}
    out = {}
    for name, _colour, _closed in FACE_REGIONS:
        pts = face_region_points(getattr(lm, name)(), w, h, flip)
        if pts:
            out[name] = pts
    return out


def match_by_centre(observations, others):
    """{index in observations: matching observation from others or None}.

    Two requests over one image produce two observation lists with no id to join
    on, and nothing promises they come back in the same order, so faces are
    paired by nearest bounding-box centre. Exact enough for the handful of faces
    a spike ever sees; a real integration would need to think harder.
    """
    pool = list(others)
    out = {}
    for i, obs in enumerate(observations):
        b = obs.boundingBox()
        cx, cy = b.origin.x + b.size.width / 2, b.origin.y + b.size.height / 2
        best, best_d = None, None
        for cand in pool:
            cb = cand.boundingBox()
            d = ((cb.origin.x + cb.size.width / 2 - cx) ** 2
                 + (cb.origin.y + cb.size.height / 2 - cy) ** 2)
            if best_d is None or d < best_d:
                best, best_d = cand, d
        out[i] = best
        if best is not None:
            pool.remove(best)
    return out


def _num(value):
    """roll/yaw/pitch are NSNumber and are None when Vision did not estimate
    them, so every read needs the None case."""
    return None if value is None else float(value)


def body3d_points(obs, w, h, flip=True, metres=True):
    """3D body joints, in image pixels and (with metres=True) in metres.

    Returns {label: (px, py, x_m, y_m, z_m)}. Both accessors are out-parameter
    methods with awkward shapes: pointInImageForJointName_error_ returns
    (VNPoint, error) and getCameraRelativePosition_forJointName_error_ returns
    (ok, simd_float4x4, error) - the matrix is an OUT parameter, so the joint
    name is the SECOND argument, not the first.

    The 4x4 is a rigid transform in camera space; its translation column is the
    joint position in metres, and simd matrices are column-major, so that is
    the LAST of the four columns.
    """
    out = {}
    for label, name in BODY3D_JOINTS:
        # Despite the name, this returns a VNPoint in NORMALISED image space,
        # bottom-left origin, like the rest of Vision (measured: center_head
        # came back as 0.4436, 0.3507 on a 1280x720 frame).
        point, err = obs.pointInImageForJointName_error_(name, None)
        if point is None or err is not None:
            continue
        px, py = (to_pixels(point.x(), point.y(), w, h) if flip
                  else (point.x() * w, point.y() * h))
        xyz = (None, None, None)
        if not metres:
            # The live overlay only needs the projection; the metric read is
            # another selector call per joint, per frame, for a number nothing
            # is going to draw.
            out[label] = (px, py) + xyz
            continue
        ok, matrix, _e = obs.getCameraRelativePosition_forJointName_error_(
            None, name, None)                 # <- matrix is the OUT param, name is 2nd
        if ok and matrix is not None:
            # simd_float4x4 arrives as a one-field struct WRAPPING the four
            # column vectors, so the columns are matrix[0] - tuple(matrix)[3]
            # looks right and raises IndexError, which is how this silently
            # printed "--" for every joint on the first run.
            col = matrix[0][3]                # column-major: translation last
            xyz = (float(col[0]), float(col[1]), float(col[2]))
        out[label] = (px, py) + xyz
    return out


# ---------------------------------------------------------------------------
# drawing (PIL)
# ---------------------------------------------------------------------------
def draw_chain(d, coords, chain, colour, width):
    for a, b in zip(chain, chain[1:]):
        if a in coords and b in coords:
            d.line([coords[a][:2], coords[b][:2]], fill=colour, width=width)


def draw_hand(d, coords, r):
    for _, colour, chain in FINGERS:
        draw_chain(d, coords, chain, colour, max(2, r // 2))
    draw_chain(d, coords, PALM, (220, 220, 220), max(2, r // 2))
    for label, xy in coords.items():
        px, py = xy[0], xy[1]
        colour = next((c for _, c, ch in FINGERS if label in ch and label != "wrist"),
                      (255, 255, 255))
        d.ellipse([px - r, py - r, px + r, py + r], fill=colour, outline=(20, 20, 20))
    if "wrist" in coords:
        px, py = coords["wrist"][:2]
        d.ellipse([px - r * 1.6, py - r * 1.6, px + r * 1.6, py + r * 1.6],
                  fill=(255, 255, 255), outline=(20, 20, 20), width=2)


def draw_body(d, coords, r, bones=BODY_BONES):
    for colour, chain in bones:
        draw_chain(d, coords, chain, colour, max(2, r // 2))
    for _label, xy in coords.items():
        px, py = xy[0], xy[1]
        d.ellipse([px - r * 0.8, py - r * 0.8, px + r * 0.8, py + r * 0.8],
                  fill=(255, 255, 255), outline=(20, 20, 20))


def draw_face(d, obs, regions, w, h, r, flip=True):
    """Landmarks plus the detector's own bounding box, which is drawn because a
    face found with no usable landmarks looks identical to no face at all."""
    box = obs.boundingBox()
    x0 = box.origin.x * w
    x1 = (box.origin.x + box.size.width) * w
    if flip:
        y0 = (1.0 - box.origin.y - box.size.height) * h
        y1 = (1.0 - box.origin.y) * h
    else:
        y0, y1 = box.origin.y * h, (box.origin.y + box.size.height) * h
    d.rectangle([x0, y0, x1, y1], outline=(120, 220, 160), width=max(1, r // 3))

    dot = max(1, int(r * 0.45))
    for name, colour, closed in FACE_REGIONS:
        pts = regions.get(name)
        if not pts:
            continue
        if len(pts) > 1:
            d.line(pts + ([pts[0]] if closed else []), fill=colour, width=max(1, r // 3))
        for px, py in pts:
            d.ellipse([px - dot, py - dot, px + dot, py + dot], fill=colour)


# ---------------------------------------------------------------------------
def stats_line(name, xs):
    if not xs:
        return "  %-24s (no samples)" % name
    return ("  %-24s mean %6.2f  median %6.2f  min %6.2f  max %6.2f  stdev %5.2f ms"
            % (name, statistics.mean(xs), statistics.median(xs), min(xs), max(xs),
               statistics.stdev(xs) if len(xs) > 1 else 0.0))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image", nargs="?", default="temp/hand.jpg")
    ap.add_argument("-o", "--out", default=None, help="annotated PNG path")
    ap.add_argument("-n", "--runs", type=int, default=20, help="timed runs")
    ap.add_argument("--max-hands", type=int, default=2)
    ap.add_argument("--no-hands", action="store_true", help="skip hand landmarks")
    ap.add_argument("--no-body", action="store_true", help="skip 2D body pose")
    ap.add_argument("--no-face", action="store_true", help="skip face landmarks")
    ap.add_argument("--body-3d", action="store_true",
                    help="also run the 3D body request (17 joints, metric, "
                         "camera-relative; a different joint set from --body)")
    ap.add_argument("--face-points", type=int, choices=sorted(FACE_CONSTELLATIONS),
                    default=76, help="face landmark constellation")
    ap.add_argument("--no-flip", action="store_true",
                    help="skip the y flip, to show what getting it wrong looks like")
    args = ap.parse_args()

    switched_on = {"hands": not args.no_hands, "body": not args.no_body,
                   "body3d": args.body_3d, "face": not args.no_face}
    active = [k for k in DETECTOR_ORDER if switched_on[k]]
    if not active:
        raise SystemExit("every detector is switched off - nothing to do")

    src_path = Path(args.image)
    if not src_path.exists():
        raise SystemExit("no such image: %s" % src_path)
    out_path = Path(args.out) if args.out else src_path.with_name(
        src_path.stem + "_vision_landmarks.png")

    cg = load_cgimage(src_path)
    w, h = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
    flip = not args.no_flip
    print("image      : %s  (%dx%d)" % (src_path, w, h))
    print("detectors  : %s" % " + ".join(active))

    # ---- detect -----------------------------------------------------------
    reqs = {k: make_requests(k, args.max_hands, args.face_points) for k in active}
    # One handler, every request: hands, body and face come off a single decode.
    first_dt = detect(cg, [r for k in active for r in reqs[k]])
    results = {k: list(reqs[k][0].results() or []) for k in active}
    # The face bundle's second request carries the head angles, nothing else.
    face_pose = list(reqs["face"][1].results() or []) if "face" in reqs else []
    print("found      : %s   (first call %.1f ms, includes model load)"
          % (", ".join("%d %s" % (len(results[k]), k) for k in active), first_dt * 1e3))

    # ---- hands ------------------------------------------------------------
    hands_px = []
    for hi, obs in enumerate(results.get("hands", [])):
        pts = points_for(obs)
        chir = CHIRALITY.get(obs.chirality(), "?(%s)" % obs.chirality())
        print("\nhand %d  chirality=%s  observation_confidence=%.3f  joints=%d"
              % (hi, chir, obs.confidence(), len(pts)))
        print("  %-11s %-10s %8s %8s %8s %8s  %s"
              % ("joint", "code", "norm_x", "norm_y", "px_x", "px_y", "conf"))
        coords = {}
        for label, key in JOINTS:
            p = pts.get(key)
            if p is None:
                print("  %-11s %-10s %s" % (label, key, "-- not returned --"))
                continue
            nx, ny = p.x(), p.y()
            px, py = to_pixels(nx, ny, w, h) if flip else (nx * w, ny * h)
            coords[label] = (px, py, p.confidence())
            print("  %-11s %-10s %8.4f %8.4f %8.1f %8.1f  %.3f"
                  % (label, key, nx, ny, px, py, p.confidence()))
        hands_px.append(coords)

    # ---- body 2D ----------------------------------------------------------
    bodies_px = []
    for bi, obs in enumerate(results.get("body", [])):
        pts = body_points_for(obs)
        print("\nbody %d  observation_confidence=%.3f  joints=%d"
              % (bi, obs.confidence(), len(pts)))
        print("  %-15s %-20s %8s %8s %8s %8s  %s"
              % ("joint", "code", "norm_x", "norm_y", "px_x", "px_y", "conf"))
        coords = {}
        for label, key in BODY_JOINTS:
            p = pts.get(key)
            if p is None:
                print("  %-15s %-20s %s" % (label, key, "-- not returned --"))
                continue
            nx, ny = p.x(), p.y()
            px, py = to_pixels(nx, ny, w, h) if flip else (nx * w, ny * h)
            coords[label] = (px, py, p.confidence())
            print("  %-15s %-20s %8.4f %8.4f %8.1f %8.1f  %.3f"
                  % (label, key, nx, ny, px, py, p.confidence()))
        bodies_px.append(coords)

    # ---- body 3D ----------------------------------------------------------
    bodies3d_px = []
    for bi, obs in enumerate(results.get("body3d", [])):
        est = HEIGHT_ESTIMATION.get(int(obs.heightEstimation()),
                                    "?(%s)" % obs.heightEstimation())
        print("\nbody3d %d  bodyHeight=%.3f m (%s estimate)"
              % (bi, float(obs.bodyHeight()), est))
        coords = body3d_points(obs, w, h, flip)
        print("  %-16s %8s %8s %8s %8s %8s"
              % ("joint", "px_x", "px_y", "x_m", "y_m", "z_m"))
        for label, _name in BODY3D_JOINTS:
            v = coords.get(label)
            if v is None:
                print("  %-16s %s" % (label, "-- not returned --"))
                continue
            px, py, x, y, z = v
            print("  %-16s %8.1f %8.1f %8s %8s %8s"
                  % (label, px, py,
                     "--" if x is None else "%8.3f" % x,
                     "--" if y is None else "%8.3f" % y,
                     "--" if z is None else "%8.3f" % z))
        # Vision EXTRAPOLATES 3D joints it cannot see: on a head-and-shoulders
        # webcam frame the knees and ankles project thousands of pixels below the
        # image (measured: right_ankle at py=33487 on a 720-pixel-tall frame).
        # They are printed above, because that is the truth about the data, and
        # dropped here, because drawing them turns the overlay into a scribble.
        margin = 0.1 * max(w, h)
        bodies3d_px.append({k: v for k, v in coords.items()
                            if -margin <= v[0] <= w + margin
                            and -margin <= v[1] <= h + margin})

    # ---- faces ------------------------------------------------------------
    faces_px = []
    pose_for = match_by_centre(results.get("face", []), face_pose)
    for fi, obs in enumerate(results.get("face", [])):
        lm = obs.landmarks()
        box = obs.boundingBox()
        pose = pose_for.get(fi)
        print("\nface %d  detection_confidence=%.3f  landmark_confidence=%s"
              % (fi, obs.confidence(),
                 "--" if lm is None else "%.3f" % lm.confidence()))
        print("  bbox norm  x=%.4f y=%.4f w=%.4f h=%.4f"
              % (box.origin.x, box.origin.y, box.size.width, box.size.height))
        # Radians in the API. Degrees here because a spike is for reading. From
        # the rev-3 rectangles request, NOT from this observation: obs.roll() and
        # obs.yaw() on a landmarks result are a constant 0.0 and obs.pitch() is
        # None, whatever the head is doing (see FACE_POSE_REVISION).
        if pose is None:
            print("  head       no rev-%d rectangles observation matched this face - "
                  "angles unavailable" % FACE_POSE_REVISION)
        else:
            angles = (_num(pose.roll()), _num(pose.yaw()), _num(pose.pitch()))
            print("  head       roll=%s  yaw=%s  pitch=%s  (degrees, rev-%d "
                  "rectangles request)"
                  % (tuple("--" if v is None
                           else "%+7.2f" % (v * 180.0 / 3.141592653589793)
                           for v in angles) + (FACE_POSE_REVISION,)))
            # Printed side by side deliberately: this is where you SEE that
            # the landmarks observation only has real angles because the rev-3
            # request is in the same handler call. Run it with the rectangles
            # request removed and this line goes to 0.0 / 0.0 / None.
            print("             same face off the landmarks request: roll=%s "
                  "yaw=%s pitch=%s" % (obs.roll(), obs.yaw(), obs.pitch()))
        regions = face_landmarks(obs, w, h, flip)
        total = sum(len(v) for v in regions.values())
        print("  regions    %d points over %d regions (constellation %d)"
              % (total, len(regions), args.face_points))
        for name, _colour, _closed in FACE_REGIONS:
            pts = regions.get(name, [])
            first = "" if not pts else "  first px (%.1f, %.1f)" % pts[0]
            print("    %-13s %3d pts%s" % (name, len(pts), first))
        faces_px.append((obs, regions))

    if not any(results.values()):
        print("\nnothing detected - writing the unannotated image anyway, because "
              "'no detections' and 'the draw code broke' must look different")

    # ---- timing -----------------------------------------------------------
    # Each detector alone AND the whole set together, because the interesting
    # number is not what face landmarks cost but what ADDING them costs.
    print("\nVision request only, %d runs each (excludes decode + draw):" % args.runs)
    sets = [[k] for k in active] + ([active] if len(active) > 1 else [])
    for subset in sets:
        times = []
        for _ in range(args.runs):
            # Fresh requests per run, with result caching off: reusing one
            # request over an unchanging image measures the cache, not Vision.
            fresh = [r for k in subset
                     for r in make_requests(k, args.max_hands, args.face_points,
                                            no_cache=True)]
            times.append(detect(cg, fresh) * 1e3)
        print(stats_line("+".join(subset), times))

    # ---- draw -------------------------------------------------------------
    im = Image.open(src_path).convert("RGB")
    if im.size != (w, h):
        # Would mean PIL applied an orientation the CGImage did not.
        print("WARNING: PIL size %s != CGImage size %s" % (im.size, (w, h)))
    d = ImageDraw.Draw(im)
    r = max(3, int(min(w, h) * 0.008))

    # Body first, then face, then hands: hands are the subject of this repo, so
    # they go on top where a body wrist and a hand wrist overlap.
    for coords in bodies_px:
        draw_body(d, coords, r)
    for coords in bodies3d_px:
        draw_body(d, coords, r, bones=BODY3D_BONES)
    for obs, regions in faces_px:
        draw_face(d, obs, regions, w, h, r, flip)
    for coords in hands_px:
        draw_hand(d, coords, r)

    im.save(out_path)
    print("\nwrote %s" % out_path)


if __name__ == "__main__":
    main()
