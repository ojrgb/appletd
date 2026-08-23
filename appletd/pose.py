"""Body pose: `VNDetectHumanBodyPoseRequest` -> `PoseFrame`. The pose half of engine.py.

WHAT THIS OWNS. One Vision request, its sequence handler, and the conversion from
`VNHumanBodyPoseObservation` to our own immutable `Body`. It does NOT own the
camera: `engine.py` owns the capture session and hands this module a sample
buffer that has already been delivered, so both streams see the SAME frame
(DESIGN.md 6.4 - one process, one camera, several requests).

WHY A SEPARATE MODULE and not more of engine.py. Direction of dependency:
`engine.py` is the base - camera, queue, teardown, hands - and this is a plug-in
on top of it. So this module imports engine and engine does not import this one,
except under TYPE_CHECKING for the annotation. `source.py` wires the two
together. The practical payoff is that a pose failure is a pose failure: nothing
here can break the hands path, which is what a live TouchDesigner project is
using.

THE SAME BOUNDARY RULES AS engine.py, and they are not stylistic:

  * imports nothing from TouchDesigner - not `td`, not `op`;
  * no pyobjc object may escape the capture thread. A
    `VNHumanBodyPoseObservation` handed to another thread would be released by
    whichever thread dropped the last reference, which is a crash that arrives
    weeks later (DESIGN.md 4.2). Everything crossing out of here is floats and
    tuples.

THREADS. Every function in this file runs on the GCD capture queue, called from
`HandEngine._on_sample_buffer`, except `verify_body_joint_table` and
`PoseDetector.__init__`, which run on the caller's thread at construction so a
framework mismatch is reported where it can be acted on.

Ref: DESIGN.md 2.12 (the API surface, measured), 6.4 (the stream contract),
     appletd/pose_types.py (the joint table and the channel list).
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
    PoseFrame,
    order_bodies,
)
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

    Why: `pose_types.py` hardcodes 19 constant suffixes and their values so the
         pure core needs no pyobjc. Hardcoding without verification would be
         reckless here for a sharper reason than it was for hands - the constant
         NAMES and their VALUES disagree about anatomy (`LeftElbow` is
         `left_forearm_joint`, MEASURED, DESIGN.md 2.12), so a table built from
         one and used against the other maps an elbow to a forearm and looks
         entirely plausible on screen.
    Contract: `request` is a live VNDetectHumanBodyPoseRequest, which is asked
         what it supports. Passing None checks the CONSTANTS only, which is
         weaker - the request's own answer is the authority on what it will
         actually return.
    What it CANNOT catch: a reordering of the table, because each row stays
         internally consistent when rows are swapped. Order is what `p0_nose_x`
         means and it is pinned by tests/test_pose_types.py instead.
    Cost: 19 getattr calls plus one framework call, once per detector. Not per
         frame.
    Raises: EngineError listing EVERY mismatch rather than the first - if Apple
         has changed something, seeing all of it at once is one investigation
         instead of nineteen.
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


def pose_frame_from_observations(observations: list[ObjCObject], seq: int,
                                 captured_at: float, width_px: int,
                                 height_px: int) -> PoseFrame:
    """Observations -> one PoseFrame with exactly MAX_BODIES slots.

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

    if n_unreadable:
        # A person detected and then lost between Vision and the channel list.
        # Surfaced rather than swallowed (STANDARDS.md 2), and raised after the
        # loop so one bad observation does not discard the good ones' work.
        raise EngineError(
            "%d body observation(s) had unreadable joint points and were dropped"
            % n_unreadable)

    return PoseFrame(seq=seq, captured_at=captured_at, width=width_px,
                     height=height_px, bodies=order_bodies(bodies))


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

    def detect_sample_buffer(self, sample_buffer: ObjCObject, seq: int,
                             captured_at: float, width_px: int,
                             height_px: int) -> PoseFrame:
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
            lambda: self._sequence.performRequests_onCMSampleBuffer_error_(
                [self._request], sample_buffer, None),    # TRAP: out-param
            seq, captured_at, width_px, height_px)

    def detect_pixel_buffer(self, pixel_buffer: ObjCObject, seq: int,
                            captured_at: float, width_px: int,
                            height_px: int) -> PoseFrame:
        """The replay path: a CVPixelBuffer we built ourselves.

        Why it exists: performance claims may only come from replaying a fixed
                  clip (DESIGN.md 3), and a replay harness using a different code
                  path from the camera would be measuring the wrong thing. This
                  and detect_sample_buffer share everything either side of
                  performRequests.
        """
        return self._detect(
            lambda: self._sequence.performRequests_onCVPixelBuffer_error_(
                [self._request], pixel_buffer, None),     # TRAP: out-param
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
        if len(results) > MAX_BODIES:
            # Counted, not silent: "a third person is in shot and is not in the
            # channels" is invisible otherwise, and this is the one number that
            # says whether MAX_BODIES is the right size for a room.
            self.n_bodies_dropped += len(results) - MAX_BODIES
        return pose_frame_from_observations(results, seq, captured_at,
                                            width_px, height_px)
