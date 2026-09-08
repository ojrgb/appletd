"""Body pose: `VNDetectHumanBodyPoseRequest` -> `PoseFrame`.

Owns one Vision request, its sequence handler, and the conversion from
`VNHumanBodyPoseObservation` to our own immutable `Body`. It does not own the camera:
`engine.py` hands it a buffer that has already been delivered, so every enabled stream
sees the same frame and shares its `seq`.

A plug-in on top of `engine.py`, not more of it: this imports engine and engine does
not import this. So a pose failure is a pose failure, and cannot break the hands path
a live project is using.

Also publishes the person BOXES from `VNDetectHumanRectanglesRequest` when they are
enabled. They are NOT matched to the skeletons: `human0` is the leftmost rectangle and
`p0` the leftmost skeleton, and nothing guarantees they are the same person.

Nothing from TouchDesigner, and no pyobjc object leaves the capture thread.

Thread: the GCD capture queue, except `__init__` and the verifiers.
Ref: DESIGN.md 6.4.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import Vision

from appletd.engine import EngineError, ObjCObject
from appletd.pose_types import (
    BODY_JOINT_INDEX_BY_CODE,
    BODY_JOINTS,
    BODY_JOINTS_GROUP_ALL_CODE,
    MAX_BODIES,
    N_BODY_JOINTS,
    Body,
    HumanRect,
    PoseFrame,
    order_bodies,
    order_humans,
)
from appletd.streams import ORIENTATION_UP
from appletd.types import (
    Confidence,
    Joint,
    NormX,
    NormY,
    median_joint_confidence,
)

# The prefix every body-joint constant shares. Used only by the verifier.
BODY_JOINT_CONSTANT_PREFIX = "VNHumanBodyPoseObservationJointName"


# ---------------------------------------------------------------------------
# Start-up verification
# ---------------------------------------------------------------------------
def verify_body_joint_table(request: ObjCObject | None = None) -> None:
    """Check every hardcoded body-joint code against the live framework.

    `pose_types.py` hardcodes 19 joint codes so the pure core needs no pyobjc, and
    hardcoding without verification would be reckless here for a sharp reason: the
    constant NAMES and their VALUES disagree about anatomy - `LeftElbow` is
    `left_forearm_joint` - so a table built from one and used against the other maps
    an elbow to a forearm and looks entirely plausible on screen.

    Raises on a mismatch at construction, where it can be acted on.
    """
    problems: list[str] = []
    for joint in BODY_JOINTS:
        constant = getattr(Vision, BODY_JOINT_CONSTANT_PREFIX + joint.suffix, None)
        if constant is None:
            problems.append("%s: no %s%s in this Vision"
                            % (joint.name, BODY_JOINT_CONSTANT_PREFIX, joint.suffix))
        elif str(constant) != joint.code:
            problems.append("%s: table says %s, framework says %s"
                            % (joint.name, joint.code, constant))

    group = getattr(Vision, "VNHumanBodyPoseObservationJointsGroupNameAll", None)
    if group is None or str(group) != BODY_JOINTS_GROUP_ALL_CODE:
        problems.append("joints group All: table says %s, framework says %s"
                        % (BODY_JOINTS_GROUP_ALL_CODE, group))

    if request is not None:
        # The REQUEST's own list, which is better evidence than a dir() scrape:
        # it is what this request will actually return points for (MEASURED,
        # DESIGN.md 2.12 - it answers with all 19). Hands has no equivalent
        # call, so this check is strictly stronger than the one hands gets.
        #
        # TRAP: out-param selector - pass None, get (names, error) back.
        supported, err = request.supportedJointNamesAndReturnError_(None)
        if err is not None or supported is None:
            problems.append("supportedJointNamesAndReturnError_ failed: %s" % (err,))
        else:
            live = {str(name) for name in supported}
            ours = {joint.code for joint in BODY_JOINTS}
            missing = ours - live
            extra = live - ours
            if missing:
                problems.append("our table has joints this request does not "
                                "support: %s" % sorted(missing))
            if extra:
                # Apple added a joint. `body_from_observation` skips unknown
                # codes, so the extra landmark would simply never reach TD and
                # nothing would say why.
                problems.append("this request supports joints our table does "
                                "not: %s. They would be silently dropped rather "
                                "than published." % sorted(extra))

    if problems:
        raise EngineError(
            "the body-joint table in pose_types.py disagrees with this macOS's "
            "Vision framework, so landmarks would be silently mislabelled:\n  "
            + "\n  ".join(problems))


# ---------------------------------------------------------------------------
# Observations -> our own immutable types
#
# This is the boundary. Above it, pyobjc objects. Below it, floats and tuples.
# ---------------------------------------------------------------------------
def body_from_observation(observation: ObjCObject) -> Body | None:
    """One VNHumanBodyPoseObservation -> one immutable Body. None if unreadable.

    Thread: capture queue only.
    Contract: the returned Body always has exactly N_BODY_JOINTS joints in
              BODY_JOINTS order. Joints Vision did not return become
              zero-confidence entries rather than gaps, because downstream
              indexes this tuple positionally. Unlike hands, a body genuinely
              does arrive with joints missing - a person cut off at the waist has
              no ankles - so this path is normal rather than defensive.
    Coordinates: passed through UNTOUCHED. Vision's normalised bottom-left origin
              is what TouchDesigner wants (DESIGN.md 7). If you find yourself
              writing `1 - y` here, stop.
    Traps: `recognizedPointsForJointsGroupName_error_` is an out-param selector,
              and the dict it returns is keyed by the joint's VALUE
              ('left_forearm_joint'), not by the constant's name.
    """
    points, err = observation.recognizedPointsForJointsGroupName_error_(
        Vision.VNHumanBodyPoseObservationJointsGroupNameAll, None)  # TRAP: out-param
    if err is not None or points is None:
        return None

    # Start from all-zero and fill what Vision gave us, so a missing joint is a
    # zero-confidence joint rather than a short tuple or a None hole.
    joints: list[Joint] = [
        Joint(NormX(0.0), NormY(0.0), Confidence(0.0)) for _ in range(N_BODY_JOINTS)
    ]
    for code, point in points.items():
        index = BODY_JOINT_INDEX_BY_CODE.get(str(code))
        if index is None:
            # Apple added a joint. Ignore it rather than crash - and this is
            # exactly what verify_body_joint_table() notices at start-up,
            # loudly, before it matters.
            continue
        # float() on every value: these are pyobjc CGFloat wrappers, and the
        # whole point of this function is that nothing from pyobjc survives it.
        joints[index] = Joint(
            NormX(float(point.x())),
            NormY(float(point.y())),
            Confidence(float(point.confidence())),
        )

    frozen = tuple(joints)
    return Body(
        # Vision's own number. UNMEASURED for bodies - hands' equivalent is a
        # measured constant 1.0 (DESIGN.md 2.6) and this one has never been seen,
        # because seeing it needs a person in frame. Mirror it, do not gate on it.
        confidence=Confidence(float(observation.confidence())),
        # Ours, and the one to gate on. 19 is odd, so this is a true median.
        conf_median=median_joint_confidence(frozen),
        joints=frozen,
        found=True,
    )


def human_from_observation(observation: ObjCObject) -> HumanRect:
    """One VNHumanObservation -> one HumanRect. Pure apart from the ObjC reads.

    Thread: capture queue only.
    Contract: normalised, origin BOTTOM LEFT exactly as Vision reports it, so `y` is
              the BOTTOM edge (DESIGN.md 7). Never returns None: unlike a body there
              are no per-joint reads to fail, so a rectangle Vision reports is one we
              can publish.
    Traps: CGRect's origin IS the bottom-left corner, and `boundingBox` is already
              normalised - no flip and no scaling here. The same treatment the face's
              box gets.
    """
    box = observation.boundingBox()
    return HumanRect(
        x=NormX(float(box.origin.x)),
        y=NormY(float(box.origin.y)),
        w=float(box.size.width),
        h=float(box.size.height),
        confidence=Confidence(float(observation.confidence())),
        found=True)


def pose_frame_from_observations(observations: list[ObjCObject], seq: int,
                                 captured_at: float, width_px: int,
                                 height_px: int,
                                 rectangles: list[ObjCObject] | None = None
                                 ) -> tuple[PoseFrame, int]:
    """Observations -> one PoseFrame with exactly MAX_BODIES slots, and a count.

    The count is how many observations could not be read. RETURNED rather than
    raised: raising discarded the skeletons that WERE readable, so three people in
    shot and one bad observation published none of them - and the comment on that
    raise said it was placed after the loop so exactly that would not happen. The
    caller records it, so nothing is dropped silently (STANDARDS.md 2).

    Thread: capture queue only.
    Contract: bodies is always MAX_BODIES long, ordered LEFT TO RIGHT by
              `order_bodies`, padded with the shared BLANK_BODY. Vision returns
              people in no stable order and gives no tracking ID, so publishing
              its order would make `p0` swap between two people frame to frame -
              the defect DESIGN.md 6.3 exists to fix for hands.
    Why every observation is converted before any is dropped: the cap applies to
              the two LEFTMOST people, which cannot be known until they have been
              located. Vision has already paid for all of them - there is no
              maximum-person setting on this request (DESIGN.md 2.12) - so the
              only saving available would be a conversion, and choosing which two
              to publish arbitrarily is what this avoids.
    """
    bodies: list[Body] = []
    n_unreadable = 0
    for observation in observations:
        body = body_from_observation(observation)
        if body is None:
            n_unreadable += 1
            continue
        bodies.append(body)

    # `n_unreadable` goes back to the caller: a person detected and then lost
    # between Vision and the channel list, surfaced rather than swallowed, without
    # the people who WERE readable going with it.
    return PoseFrame(
        seq=seq, captured_at=captured_at, width=width_px, height=height_px,
        bodies=order_bodies(bodies),
        # A SEPARATE request's observations, ordered on their own. `human0` is the
        # leftmost RECTANGLE and `p0` the leftmost SKELETON, and nothing pairs them -
        # see `HumanRect`.
        humans=order_humans([human_from_observation(rectangle)
                             for rectangle in (rectangles or [])])), n_unreadable


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------
class PoseDetector:
    """Holds the body-pose request and turns buffers into PoseFrames.

    Thread: NOT thread-safe, and does not need to be. One detector belongs to one
            capture queue, which is serial, so its request object is only ever
            touched by one thread at a time. Sharing one across two queues would
            corrupt Vision's inter-frame state.
    Why its OWN VNSequenceRequestHandler rather than sharing the hand detector's
            and performing both requests in one call: each detector stays
            self-contained, each is timed separately, and a pose failure cannot
            fail the hands call it would have shared. UNMEASURED whether one call
            with two requests is cheaper than two calls; the inference itself
            dominates, and measuring it needs a person in frame.
    Why one handler for the whole session rather than one per frame: the sequence
            handler is what carries Vision's inter-frame state, and rebuilding it
            per frame throws that away (DESIGN.md 2.3).
    """

    def __init__(self) -> None:
        self._sequence = Vision.VNSequenceRequestHandler.alloc().init()
        self._request = Vision.VNDetectHumanBodyPoseRequest.alloc().init()
        # THE PERSON BOXES, in the SAME `performRequests` call as the skeletons.
        #
        # THAT SAVES NOTHING, and the tempting assumption is that it does. MEASURED
        # over 35 fixture frames: body pose alone 3.91 ms, rectangles alone 2.68 ms,
        # both in one call 6.82 ms. Vision does not share its analysis between these
        # two requests, so the pair costs what the two cost - the marginal price of
        # the boxes is 2.91 ms and `Streampose` pays it whether it wants them or not.
        # One call is simply one place where the orientation and the error check are
        # got right, rather than two.
        #
        # `upperBodyOnly` is left OFF: a box that stops at the waist is a different
        # measurement, and nothing here asked for one.
        self._rect_request = Vision.VNDetectHumanRectanglesRequest.alloc().init()
        self.rect_revision = int(self._rect_request.revision())
        # Built BEFORE verifying, because the request is the authority on which
        # joints it supports - the strongest form of this check needs it alive.
        verify_body_joint_table(self._request)
        # MEASURED (DESIGN.md 2.12): only revision 1 exists. Recorded rather than
        # set, so a future macOS adding one is visible in the stats instead of
        # silently changing behaviour.
        self.revision = int(self._request.revision())
        self.last_inference_ms = 0.0
        # Bodies Vision found beyond MAX_BODIES, dropped after the left-to-right
        # sort. See _detect.
        self.n_bodies_dropped = 0
        # How many observations the LAST detect could not read. Reported by the
        # engine after a successful publish.
        self.last_unreadable = 0

    def detect_sample_buffer(self, sample_buffer: ObjCObject, seq: int,
                             captured_at: float, width_px: int,
                             height_px: int,
                             orientation: int = ORIENTATION_UP) -> PoseFrame:
        """The live path: the same CMSampleBuffer the hand detector just saw.

        Contract: the image dimensions are passed IN rather than re-read here.
                  The caller has already read them off the buffer for the hands
                  frame, and the two frames from one buffer must not disagree
                  about the image they came from.
        Why no conversion: the buffer the camera produced goes into Vision in its
                  native 420v pixel format, exactly as it does for hands
                  (DESIGN.md 3).
        """
        return self._detect(
            lambda: self._sequence.performRequests_onCMSampleBuffer_orientation_error_(
                [self._request, self._rect_request],
                sample_buffer, orientation, None),                # TRAP: out-param
            seq, captured_at, width_px, height_px)

    def detect_pixel_buffer(self, pixel_buffer: ObjCObject, seq: int,
                            captured_at: float, width_px: int,
                            height_px: int,
                            orientation: int = ORIENTATION_UP) -> PoseFrame:
        """The replay path: a CVPixelBuffer we built ourselves.

        Why it exists: performance claims may only come from replaying a fixed
                  clip (DESIGN.md 3), and a replay harness using a different code
                  path from the camera would be measuring the wrong thing. This
                  and detect_sample_buffer share everything either side of
                  performRequests.
        """
        return self._detect(
            lambda: self._sequence.performRequests_onCVPixelBuffer_orientation_error_(
                [self._request, self._rect_request],
                pixel_buffer, orientation, None),                 # TRAP: out-param
            seq, captured_at, width_px, height_px)

    def _detect(self, perform: Callable[[], tuple[bool, ObjCObject]], seq: int,
                captured_at: float, width_px: int, height_px: int) -> PoseFrame:
        """Time the request, check it, convert the results. Shared by both paths.

        The two entry points differ in exactly one selector, so everything that
        can be got wrong - the timing, the out-param check, the dropped-body
        count, the conversion - is written once. The first draft had it written
        twice and the two copies had already diverged by three lines.
        """
        started_s = time.perf_counter()
        ok, err = perform()
        self.last_inference_ms = (time.perf_counter() - started_s) * 1e3
        if not ok:
            raise EngineError("Vision body-pose performRequests failed: %s" % (err,))

        results = list(self._request.results() or [])
        # EACH REQUEST'S OWN RESULTS. They went in as one list and they come back on
        # the objects, not merged - reading `self._request.results()` alone would have
        # published skeletons and no boxes, which is what it did until this was built.
        rectangles = list(self._rect_request.results() or [])
        if len(results) > MAX_BODIES:
            # Counted, not silent: "a third person is in shot and is not in the
            # channels" is invisible otherwise, and this is the one number that
            # says whether MAX_BODIES is the right size for a room.
            self.n_bodies_dropped += len(results) - MAX_BODIES
        frame, self.last_unreadable = pose_frame_from_observations(
            results, seq, captured_at, width_px, height_px, rectangles)
        return frame
