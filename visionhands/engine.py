"""Camera to LandmarkFrame. The only module that touches AVFoundation or Vision.

This file imports pyobjc and MUST NOT import anything from TouchDesigner - not
`td`, not `op`, nothing. That boundary is what lets the same engine be driven by
a Script CHOP, by a test, or by a future out-of-process transport without being
rewritten (DESIGN.md 5). The reverse boundary is just as strict: `td/` must never
import this module's dependencies.

WHAT CROSSES THE THREAD BOUNDARY. Exactly one thing: an immutable
`LandmarkFrame` of floats, ints, bools and tuples. No pyobjc object may escape
the capture thread, ever. A `VNHumanHandPoseObservation` handed to TD's main
thread would be released by whichever thread happened to drop the last
reference, which is a crash that arrives weeks later on a quiet afternoon
(DESIGN.md 4.2).

THREADS. Two, and no more (MEASURED, DESIGN.md 2.5):

  * whichever thread calls start()/stop() - in production TD's main thread
  * the GCD capture queue, where AVFoundation delivers sample buffers and where
    every line of `_CaptureDelegate` and `HandDetector` runs

There is no run-loop thread. The spike believed AVFoundation needed one; it does
not - `AVCaptureVideoDataOutput` delivers on a dispatch queue, and milestone 1
measured 48 frames arriving with nothing turning a run loop, inside TD and out.

PERFORMANCE. Vision costs ~3.4 ms per 720p frame (MEASURED, DESIGN.md 2.6) and
pyobjc releases the GIL for the duration (MEASURED, DESIGN.md 2.2), so this
thread cannot stall TD's. The camera's ~30 fps is the limit, not inference.

Ref: DESIGN.md 4 (architecture), 6 (data contracts), 8 (lifecycle).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import AVFoundation as AVF
import CoreMedia
import objc
import Quartz
import Vision
from Foundation import NSObject
from libdispatch import dispatch_queue_attr_make_with_qos_class, dispatch_queue_create

from visionhands.types import (
    BLANK_HAND,
    CHIRALITY_LEFT,
    CHIRALITY_RIGHT,
    CHIRALITY_UNKNOWN,
    JOINT_INDEX_BY_CODE,
    JOINTS,
    JOINTS_GROUP_ALL_CODE,
    MAX_HANDS,
    N_JOINTS,
    Confidence,
    Hand,
    Joint,
    LandmarkFrame,
    NormX,
    NormY,
    median_joint_confidence,
)

# ---------------------------------------------------------------------------
# Constants. Every value has a reason; see the referenced measurement.
# ---------------------------------------------------------------------------

# 1280x720. MEASURED (DESIGN.md 2.1, confirmed 2.6): ~3.4 ms per frame, and
# 640x480 is consistently SLOWER because Vision normalises internally. This is
# both the accurate and the cheap choice, so there is no tradeoff to tune.
DEFAULT_WIDTH_PX = 1280
DEFAULT_HEIGHT_PX = 720

# Substring matched case-insensitively against localizedName(). NOT an index:
# this machine enumerates Camo, OBS Virtual Camera and a Continuity iPhone
# alongside the built-in camera, and index 0 moves between reboots (DESIGN.md 3).
DEFAULT_CAMERA_NAME = "MacBook"

# QOS_CLASS_USER_INITIATED. A default-QoS queue can be scheduled onto efficiency
# cores, which was a large part of the run-to-run variance in the spike.
QOS_CLASS_USER_INITIATED = 25

# Seconds to let in-flight delegate callbacks finish after stopRunning() before
# anything they touch is released. Carried over from the spike; a drain, not a
# deadline - a callback needing longer than this means a worse problem exists.
CALLBACK_DRAIN_S = 0.2

# AVAuthorizationStatus.authorized. The TCC prompt attaches to the HOST app
# (TouchDesigner.app), never to this code (DESIGN.md 8).
AUTH_STATUS_AUTHORIZED = 3

# kCVPixelBufferLock_ReadOnly.
LOCK_READ_ONLY = 1

# The prefix every joint-name constant shares. Used only by verify_joint_table.
JOINT_CONSTANT_PREFIX = "VNHumanHandPoseObservationJointName"

# How many distinct error strings to keep. Bounded because this list is appended
# to from the capture thread at up to 30 Hz, and an unbounded list on a
# long-running engine is a memory leak that presents as TD slowly dying.
MAX_RECORDED_ERRORS = 20


# pyobjc's modules are generated at import time and carry no type information,
# so every Objective-C handle is, to a type checker, an opaque value. Naming that
# `ObjCObject` rather than writing `Any` inline says WHICH kind of unknown it is:
# "a native handle we hold and pass back to the framework", not "we could not be
# bothered to work out the type". Annotating them as `object` would be worse than
# either - it actively claims they have no attributes.
ObjCObject = Any


class EngineError(RuntimeError):
    """Raised on the CALLING thread, for faults the caller can act on.

    Never raised from the capture thread - an exception crossing an Objective-C
    callback boundary unwinds into native frames, which is its own crash
    (STANDARDS.md 2). Faults detected on the capture thread are recorded in
    `HandEngine.errors` instead.
    """


# ---------------------------------------------------------------------------
# Start-up verification
# ---------------------------------------------------------------------------
def verify_joint_table() -> None:
    """Check every hardcoded joint code against the live framework.

    Why: `types.py` hardcodes the 21 short codes ('VNHLKWRI' and friends) so
         that the pure core, and therefore `td/`, needs no pyobjc. Hardcoding
         without verification would be reckless - two of the codes differ by one
         character and the little finger is internally 'P' for pinky (DESIGN.md
         2.3). This is the check that makes the hardcoding safe.
    What it CANNOT catch: a reordering of the table, because each row stays
         internally consistent when rows are swapped. Order is pinned by
         `tests/test_joint_table.py` instead, and order is what h0_lm00 means.
    Cost: 21 getattr calls, once per engine construction. Not per frame.
    Raises: EngineError listing every mismatch, rather than the first - if Apple
         has changed something, seeing all of it at once is the difference
         between one investigation and twenty-one.
    """
    problems: list[str] = []
    for joint in JOINTS:
        constant = getattr(Vision, JOINT_CONSTANT_PREFIX + joint.suffix, None)
        if constant is None:
            problems.append("%s: no %s%s in this Vision"
                            % (joint.name, JOINT_CONSTANT_PREFIX, joint.suffix))
        elif str(constant) != joint.code:
            problems.append("%s: table says %s, framework says %s"
                            % (joint.name, joint.code, constant))

    group = getattr(Vision, "VNHumanHandPoseObservationJointsGroupNameAll", None)
    if group is None or str(group) != JOINTS_GROUP_ALL_CODE:
        problems.append("joints group All: table says %s, framework says %s"
                        % (JOINTS_GROUP_ALL_CODE, group))

    if problems:
        raise EngineError(
            "the joint table in types.py disagrees with this macOS's Vision "
            "framework, so landmarks would be silently mislabelled:\n  "
            + "\n  ".join(problems))


# ---------------------------------------------------------------------------
# Observations -> our own immutable types
#
# This is the boundary. Above it, pyobjc objects. Below it, floats and tuples.
# ---------------------------------------------------------------------------
def _chirality_of(observation: ObjCObject) -> int:
    """Vision's chirality as one of our three constants.

    Why not just pass the int through: Vision's VNChirality happens to use the
    same -1/0/+1 encoding we do, which is convenient and not guaranteed. Mapping
    it explicitly means a future change breaks here, loudly, instead of silently
    relabelling every left hand as unknown.
    UNVERIFIED: whether the fixture that produced our chirality figures is
    horizontally mirrored is still open (DESIGN.md 2.6), so treat the *value* as
    stable but not yet ground-truthed.
    """
    raw = int(observation.chirality())
    if raw == CHIRALITY_LEFT:
        return CHIRALITY_LEFT
    if raw == CHIRALITY_RIGHT:
        return CHIRALITY_RIGHT
    return CHIRALITY_UNKNOWN


def hand_from_observation(observation: ObjCObject) -> Hand | None:
    """One VNHumanHandPoseObservation -> one immutable Hand. None if unreadable.

    Thread: capture queue only.
    Contract: the returned Hand always has exactly N_JOINTS joints in JOINTS
              order. Joints Vision did not return - which should not happen, it
              returns low-confidence points rather than omitting them - become
              zero-confidence entries rather than gaps, because downstream
              indexes this tuple positionally.
    Coordinates: passed through UNTOUCHED. Vision's normalised bottom-left
              origin is what TouchDesigner wants (DESIGN.md 7). If you find
              yourself writing `1 - y` in this function, stop.
    Traps: recognizedPointsForJointsGroupName_error_ is an out-param selector -
              trailing None, returns a tuple. And the dict it returns is keyed
              by the short CODE ('VNHLKWRI'), not by the long constant name.
    """
    group_all = Vision.VNHumanHandPoseObservationJointsGroupNameAll
    points, err = observation.recognizedPointsForJointsGroupName_error_(
        group_all, None)                                  # TRAP: out-param
    if err is not None or points is None:
        return None

    # Start from all-zero and fill what Vision gave us, so a missing joint is a
    # zero-confidence joint rather than a short tuple or a None hole.
    joints: list[Joint] = [
        Joint(NormX(0.0), NormY(0.0), Confidence(0.0)) for _ in range(N_JOINTS)
    ]
    for code, point in points.items():
        index = JOINT_INDEX_BY_CODE.get(str(code))
        if index is None:
            # An unknown code means Apple added a joint. Ignore it rather than
            # crash - but this is exactly what verify_joint_table() exists to
            # notice at start-up, loudly, before it matters.
            continue
        # float() on every value: these are pyobjc CGFloat wrappers, and the
        # whole point of this function is that nothing from pyobjc survives it.
        joints[index] = Joint(
            NormX(float(point.x())),
            NormY(float(point.y())),
            Confidence(float(point.confidence())),
        )

    frozen = tuple(joints)
    return Hand(
        chirality=_chirality_of(observation),
        # Vision's own number. MEASURED as a constant 1.0 across 336
        # observations (DESIGN.md 2.6) - published as a faithful mirror of the
        # API, never to be gated on.
        confidence=Confidence(float(observation.confidence())),
        # Ours, and the one to gate on.
        conf_median=median_joint_confidence(frozen),
        joints=frozen,
        found=True,
    )


def frame_from_observations(observations: list[ObjCObject], seq: int,
                            captured_at: float, width_px: int,
                            height_px: int) -> LandmarkFrame:
    """Observations -> one LandmarkFrame with exactly MAX_HANDS slots.

    Thread: capture queue only.
    Contract: hands is always MAX_HANDS long, padded with the shared BLANK_HAND.
              That fixed length is what makes the fixed channel list in
              DESIGN.md 6.2 unconditionally true instead of something the CHOP
              has to remember.

    TODO(M5): SLOT ASSIGNMENT IS NAIVE HERE, ON PURPOSE. Hands land in whatever
              order Vision returned them. Vision has no tracking IDs and its
              observation order is not stable, so h0 can and will swap to the
              other physical hand between frames. DESIGN.md 6.3 specifies the
              real algorithm - chirality first, then wrist proximity, then an
              N-frame grace period - and milestone 5 implements it against a
              two-hand fixture. Until then, treat two-hand output as unusable
              for anything that cares which hand is which. Silent slot swapping
              looks fine on screen and corrupts everything downstream, which is
              why this is a labelled gap and not a quiet approximation.
    """
    hands: list[Hand] = []
    for observation in observations[:MAX_HANDS]:
        hand = hand_from_observation(observation)
        if hand is not None:
            hands.append(hand)
    while len(hands) < MAX_HANDS:
        hands.append(BLANK_HAND)        # shared, frozen: no allocation

    return LandmarkFrame(
        seq=seq,
        captured_at=captured_at,
        width=width_px,
        height=height_px,
        hands=tuple(hands),
    )


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------
class HandDetector:
    """Holds Vision's request state and turns buffers into LandmarkFrames.

    Thread: NOT thread-safe, and does not need to be. One detector belongs to
            one capture queue, which is serial, so its request object is only
            ever touched by one thread at a time. Sharing one across two queues
            would corrupt Vision's inter-frame state.
    Why one VNSequenceRequestHandler for the whole session rather than one per
            frame: the sequence handler is what carries Vision's inter-frame
            state, and rebuilding it per frame throws that away (DESIGN.md 2.3).
    """

    def __init__(self, max_hands: int = MAX_HANDS) -> None:
        verify_joint_table()            # once, before any frame is trusted
        self._sequence = Vision.VNSequenceRequestHandler.alloc().init()
        self._request = Vision.VNDetectHumanHandPoseRequest.alloc().init()
        self._request.setMaximumHandCount_(max_hands)
        # MEASURED (DESIGN.md 2.3): only revision 1 exists. Recorded rather than
        # set, so a future macOS adding one is visible in the stats instead of
        # silently changing behaviour.
        self.revision = int(self._request.revision())
        self.last_inference_ms = 0.0

    def detect_sample_buffer(self, sample_buffer: ObjCObject, seq: int,
                             captured_at: float) -> LandmarkFrame | None:
        """The live path: a CMSampleBuffer straight from the camera.

        Contract: returns None if Vision failed or the buffer had no image, so
                  the caller can count it rather than publish a bogus frame.
        Why CMSampleBuffer and not a converted array: the buffer the camera
                  produced goes into Vision with no conversion at all, in its
                  native 420v pixel format. Converting to BGRA first would cost
                  a full-frame copy for nothing (DESIGN.md 3).
        """
        pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
        if pixel_buffer is None:
            return None
        width_px = int(Quartz.CVPixelBufferGetWidth(pixel_buffer))
        height_px = int(Quartz.CVPixelBufferGetHeight(pixel_buffer))

        started_s = time.perf_counter()
        ok, err = self._sequence.performRequests_onCMSampleBuffer_error_(
            [self._request], sample_buffer, None)         # TRAP: out-param
        self.last_inference_ms = (time.perf_counter() - started_s) * 1e3
        if not ok:
            raise EngineError("Vision performRequests failed: %s" % (err,))

        return frame_from_observations(
            list(self._request.results() or []), seq, captured_at,
            width_px, height_px)

    def detect_pixel_buffer(self, pixel_buffer: ObjCObject, seq: int,
                            captured_at: float) -> LandmarkFrame:
        """The replay path: a CVPixelBuffer we built ourselves.

        Why it exists: performance claims may only come from replaying a fixed
                  clip (DESIGN.md 3), and a replay harness that used a different
                  code path from the camera would be measuring the wrong thing.
                  This and detect_sample_buffer share everything downstream of
                  performRequests.
        """
        width_px = int(Quartz.CVPixelBufferGetWidth(pixel_buffer))
        height_px = int(Quartz.CVPixelBufferGetHeight(pixel_buffer))

        started_s = time.perf_counter()
        ok, err = self._sequence.performRequests_onCVPixelBuffer_error_(
            [self._request], pixel_buffer, None)          # TRAP: out-param
        self.last_inference_ms = (time.perf_counter() - started_s) * 1e3
        if not ok:
            raise EngineError("Vision performRequests failed: %s" % (err,))

        return frame_from_observations(
            list(self._request.results() or []), seq, captured_at,
            width_px, height_px)


def pixel_buffer_from_bgra(bgra: Any) -> ObjCObject:
    """Wrap a contiguous HxWx4 BGRA buffer as a CVPixelBuffer, without copying.

    Used by the replay harness and its tests, never by the live path.

    TRAPS, both of which corrupt silently rather than failing:
      * CVPixelBufferCreateWithBytes does NOT copy. The caller MUST keep a
        reference to `bgra` alive for as long as the returned buffer is used.
        The idiom is to hold both in the same scope; a helper that returned only
        the buffer would be a use-after-free generator.
      * The row stride must be the array's real stride, not width*4. Pass
        `bgra.strides[0]` and never compute it.
    """
    height_px, width_px = bgra.shape[:2]
    rc, pixel_buffer = Quartz.CVPixelBufferCreateWithBytes(
        None, width_px, height_px, Quartz.kCVPixelFormatType_32BGRA,
        bgra, bgra.strides[0],
        None, None, None, None)
    if rc != 0:
        raise EngineError("CVPixelBufferCreateWithBytes failed: %d" % rc)
    return pixel_buffer


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
def discover_devices() -> list[ObjCObject]:
    """Every video capture device, built-in and external."""
    device_types = [AVF.AVCaptureDeviceTypeBuiltInWideAngleCamera]
    for optional in ("AVCaptureDeviceTypeExternal", "AVCaptureDeviceTypeContinuityCamera"):
        # These constants come and go between macOS releases; a missing one must
        # not take the engine down at import time.
        if hasattr(AVF, optional):
            device_types.append(getattr(AVF, optional))
    session = (AVF.AVCaptureDeviceDiscoverySession
               .discoverySessionWithDeviceTypes_mediaType_position_(
                   device_types, AVF.AVMediaTypeVideo,
                   AVF.AVCaptureDevicePositionUnspecified))
    return list(session.devices())


def pick_device(name_substr: str) -> ObjCObject:
    """Find a camera by name. Raises EngineError naming what IS available.

    Why the error lists the alternatives: the single most likely first-run
    failure is a name that does not match, and an error that says "no match"
    without saying what was there sends the reader to the wrong file.
    """
    devices = discover_devices()
    if not devices:
        raise EngineError("no video capture devices found")
    for device in devices:
        if name_substr.lower() in device.localizedName().lower():
            return device
    raise EngineError("no capture device name contains %r. available: %s"
                      % (name_substr, [d.localizedName() for d in devices]))


def video_settings(width_px: int, height_px: int) -> dict[Any, Any]:
    """Output-side scale - the ONLY resolution control that works on macOS.

    Traps: setActiveFormat_ is silently reverted by the session preset (default
           High = 1080p) whether set before or after addInput_, and
           AVCaptureSessionPresetInputPriority, the iOS escape hatch, raises
           NSInvalidArgumentException here. Two spike runs silently produced
           1080p while the log claimed 720p, which is why the delegate asserts
           the delivered size instead of trusting this (DESIGN.md 3).
    Pixel format: deliberately absent. Leaving it unset keeps the camera in its
           native 420v, which Vision consumes directly - specifying BGRA would
           buy a full-frame conversion per frame that nothing in the engine
           reads. Only the recorder, which has to hand pixels to cv2, pays that.
    """
    return {
        Quartz.kCVPixelBufferWidthKey: width_px,
        Quartz.kCVPixelBufferHeightKey: height_px,
    }


# ---------------------------------------------------------------------------
# The capture delegate.
#
# Defined ONCE, at module scope. Registering an Objective-C class name that
# already exists raises `objc.error: ... is overriding existing Objective-C
# class`, so this module cannot be importlib.reload()ed - by design. A stale
# reloaded class would be worse: it would silently run the previous edit's code.
# If you are iterating on this file inside TouchDesigner, restart TD rather than
# reloading, and see DESIGN.md 8 on why engine lifetime is the thing to manage
# instead.
# ---------------------------------------------------------------------------
class _CaptureDelegate(NSObject):
    """AVCaptureVideoDataOutput delegate. Every method runs on the capture queue.

    Thread: capture queue only, and the queue is serial, so there is exactly one
            writer for every counter here. The main thread reads them only after
            stop() has flagged, stopped and drained.
    """

    def initWithEngine_(self, engine: HandEngine) -> _CaptureDelegate | None:
        self = objc.super(_CaptureDelegate, self).init()
        if self is None:
            return None
        # A plain reference, not weak: the engine outlives its delegate by
        # construction (stop() tears the delegate down), and a weakref here
        # would add a failure mode - a dead engine mid-callback - to solve a
        # cycle that stop() already breaks.
        self._engine = engine
        return self

    def captureOutput_didOutputSampleBuffer_fromConnection_(
            self, output: ObjCObject, sample_buffer: ObjCObject,
            connection: ObjCObject) -> None:
        engine = self._engine
        # INVARIANT: once stopping is set, touch nothing native. A Vision call
        # landing on a torn-down session is a process kill, and no `except`
        # catches it (DESIGN.md 8).
        if engine._stopping:
            return
        try:
            engine._on_sample_buffer(sample_buffer)
        except Exception as exc:                        # noqa: BLE001
            # The one sanctioned except Exception (STANDARDS.md 2): a Python
            # exception unwinding out of an Objective-C callback is its own
            # crash. Recorded so it surfaces in engine.errors, never swallowed
            # silently. It cannot catch the native crash it sits beside.
            engine._record_error("capture callback: %r" % (exc,))


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class HandEngine:
    """Runs the camera and Vision on a background queue; hands out LandmarkFrames.

    Usage, and the ONLY safe shape of it:

        engine = HandEngine(on_frame=box.publish)
        engine.start()
        ...
        engine.stop()                      # idempotent, and always called

    Thread: start() and stop() are called from one thread (TD's main thread in
            production) and are not safe to call concurrently with each other.
            `on_frame` is invoked on the CAPTURE queue - whatever it does, it
            must not block, must not touch the TD API, and must not raise.
    Why a callback and not a queue: TD cooks at its own rate, unrelated to the
            camera's. The right policy is "most recent result, discard what the
            renderer never saw", which is a single-slot box in source.py, not a
            growing queue (DESIGN.md 4.3). The engine stays ignorant of that
            policy so the same engine can feed a box, a test, or a recorder.
    Ref: DESIGN.md 4.2, 8.
    """

    def __init__(self, on_frame: Callable[[LandmarkFrame], None],
                 camera_name: str = DEFAULT_CAMERA_NAME,
                 width_px: int = DEFAULT_WIDTH_PX,
                 height_px: int = DEFAULT_HEIGHT_PX,
                 max_hands: int = MAX_HANDS) -> None:
        self._on_frame = on_frame
        self._camera_name = camera_name
        self._requested_px = (width_px, height_px)
        self._max_hands = max_hands

        # Built here rather than in start(), so that a joint-table mismatch or a
        # missing framework fails at construction - on the caller's thread,
        # where it can be reported - instead of on the capture queue where it
        # could only be recorded.
        self._detector = HandDetector(max_hands=max_hands)

        self._session: ObjCObject | None = None
        self._output: ObjCObject | None = None
        self._delegate: ObjCObject | None = None
        self._queue: ObjCObject | None = None

        # `_stopping` is checked by the delegate before it touches anything
        # native. A plain bool is enough: CPython's GIL makes the read and the
        # write atomic, there is exactly one writer, and the only requirement is
        # that the capture thread sees it promptly - not that it sees it at an
        # exact instant.
        self._stopping = False
        self._running = False

        self._seq = 0
        self.n_delivered = 0        # every buffer the camera handed us
        self.n_published = 0        # frames that reached on_frame
        self.n_dropped = 0          # buffers with no image, or failed inference
        self.delivered_px: tuple[int, int] | None = None
        self.errors: list[str] = []
        # Set on the first delivered frame. Lets a caller confirm liveness by
        # WAITING rather than polling - a Python poll loop next to a pyobjc
        # callback thread starves it (MEASURED: 28 fps -> 8.4, DESIGN.md 3).
        self.first_frame = threading.Event()

    # -- capture-thread side ------------------------------------------------
    def _record_error(self, message: str) -> None:
        """Record a fault from either thread. Bounded, and deduplicated.

        Thread: safe from the capture queue. list.append is atomic under the GIL
                and this is not on the hot path unless something is already
                wrong.
        Why deduplicated: a fault that recurs at 30 Hz would otherwise fill the
                list with one message and hide everything else.
        """
        if message in self.errors:
            return
        if len(self.errors) < MAX_RECORDED_ERRORS:
            self.errors.append(message)

    def _on_sample_buffer(self, sample_buffer: ObjCObject) -> None:
        """One delivered camera buffer -> at most one published LandmarkFrame.

        Thread: capture queue only. Called from the delegate, inside its
                exception guard.
        """
        self.n_delivered += 1

        # captured_at is taken HERE, as close to delivery as we can get, because
        # everything downstream reports age from it. Vision's ~3.4 ms then counts
        # as age, which is correct: it is latency the consumer really is seeing.
        # (The sample buffer's own presentation timestamp would be a touch
        # earlier but sits on a different clock from time.monotonic(), and
        # mixing the two would produce an age that is confidently wrong.)
        captured_at = time.monotonic()

        pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
        if pixel_buffer is None:
            self.n_dropped += 1
            self._record_error("delivered buffer had no image buffer")
            return

        if self.delivered_px is None:
            self.delivered_px = (int(Quartz.CVPixelBufferGetWidth(pixel_buffer)),
                                 int(Quartz.CVPixelBufferGetHeight(pixel_buffer)))
            # TRAP: always assert delivered against requested. The session
            # preset silently reverts setActiveFormat_ and two spike runs
            # produced 1080p while the log said 720p (DESIGN.md 3). Recorded
            # rather than raised: the frames are still usable, they are just not
            # the size any measurement assumed, and a benchmark quoting them
            # would be quoting a different resolution's numbers.
            if self.delivered_px != self._requested_px:
                self._record_error(
                    "camera delivered %dx%d, requested %dx%d - any performance "
                    "number from this run is not comparable"
                    % (self.delivered_px[0], self.delivered_px[1],
                       self._requested_px[0], self._requested_px[1]))

        self._seq += 1
        frame = self._detector.detect_sample_buffer(
            sample_buffer, self._seq, captured_at)
        if frame is None:
            self.n_dropped += 1
            return

        self.n_published += 1
        # Set AFTER the first frame is actually built, so a waiter that wakes on
        # this event knows a real frame exists and not merely that a buffer
        # arrived.
        if not self.first_frame.is_set():
            self.first_frame.set()

        # The consumer's callback. Anything it raises is caught by the
        # delegate's guard and recorded - but a consumer that raises per frame
        # is broken, and a consumer that BLOCKS here stalls the camera queue and
        # will show up as rising age_ms downstream.
        self._on_frame(frame)

    # -- calling-thread side ------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def inference_ms(self) -> float:
        """Cost of the most recent inference. A gauge, not a measurement -
        DESIGN.md 3 forbids quoting live-camera timings, because the cost tracks
        what is in shot. Benchmark by replaying the fixture."""
        return self._detector.last_inference_ms

    def start(self) -> None:
        """Configure and start the capture session. Idempotent.

        Idempotent on purpose: a TD project reload is the most likely way this
        gets called twice, and two sessions fighting over one camera is the
        failure DESIGN.md 8 names. Calling start() twice is a no-op, not a
        second session.
        Raises: EngineError for anything the caller can fix - no camera
                permission, no matching device, a session that will not build.
        """
        if self._running:
            return

        status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeVideo)
        if status != AUTH_STATUS_AUTHORIZED:
            # Checked first because an unauthorised host delivers no frames
            # rather than an error, and every other symptom looks the same
            # (DESIGN.md 8). The prompt attaches to the host app.
            raise EngineError(
                "camera not authorised for this host application (status %d; 3 "
                "means authorised). Grant camera access to the host - "
                "TouchDesigner.app, or your terminal - in System Settings > "
                "Privacy & Security > Camera." % status)

        device = pick_device(self._camera_name)

        session = AVF.AVCaptureSession.alloc().init()
        session.beginConfiguration()
        try:
            device_input, err = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(
                device, None)                             # TRAP: out-param
            if device_input is None:
                raise EngineError("could not open %s: %s"
                                  % (device.localizedName(), err))
            session.addInput_(device_input)

            output = AVF.AVCaptureVideoDataOutput.alloc().init()
            # Drop late frames rather than queue them: an overloaded consumer
            # should lose frames, not accumulate latency. For a realtime overlay
            # a stale frame is worthless, and a backlog is worse than a gap.
            output.setAlwaysDiscardsLateVideoFrames_(True)
            output.setVideoSettings_(video_settings(*self._requested_px))

            delegate = _CaptureDelegate.alloc().initWithEngine_(self)
            queue = dispatch_queue_create(
                b"visionhands.engine.capture",
                dispatch_queue_attr_make_with_qos_class(
                    None, QOS_CLASS_USER_INITIATED, 0))
            output.setSampleBufferDelegate_queue_(delegate, queue)
            session.addOutput_(output)
        finally:
            # Always commit, even on the failure path: leaving a session in a
            # begun-but-uncommitted configuration is a state nothing else here
            # knows how to clean up.
            session.commitConfiguration()

        # Published only once the session is fully built, so a delegate callback
        # can never observe a half-constructed engine.
        self._session = session
        self._output = output
        self._delegate = delegate
        self._queue = queue
        self._stopping = False

        try:
            session.startRunning()
        except Exception as exc:
            # Inside a try so a failure to start cannot leave the engine
            # believing it is stopped while holding a live session - the same
            # ordering bug the M0 review found in the tools.
            self._teardown()
            raise EngineError("startRunning failed: %r" % (exc,)) from exc

        self._running = True

    def stop(self) -> None:
        """Stop capture and release everything. Idempotent, and safe to call
        from a teardown path that does not know whether start() succeeded.

        ORDER IS LOAD-BEARING (DESIGN.md 8):
          1. flag, so the delegate stops touching anything native
          2. stop the session, so no new callbacks are scheduled
          3. drain, so callbacks already in flight finish
          4. unregister the delegate, then drop references

        Getting this order wrong is not a leak, it is a native crash inside
        TouchDesigner that no `except` can catch.
        """
        if not self._running and self._session is None:
            return
        self._stopping = True
        if self._session is not None:
            self._session.stopRunning()
        time.sleep(CALLBACK_DRAIN_S)        # blocking, and deliberately not a poll
        self._teardown()
        self._running = False

    def _teardown(self) -> None:
        """Release native objects. Assumes the session is already stopped.

        The explicit setSampleBufferDelegate_queue_(None, None) is the part the
        M0 review flagged as owed here: the probe and the recorder both rely on
        the session's own release ordering, which is fine for a hand-run script
        and not fine for something that starts and stops repeatedly across TD
        project reloads. Unregistering first means the output cannot be holding
        our delegate when we drop our last reference to it.
        """
        if self._output is not None:
            self._output.setSampleBufferDelegate_queue_(None, None)
        self._session = None
        self._output = None
        self._delegate = None
        self._queue = None

    def __del__(self) -> None:
        """Last-resort safety net, not a teardown strategy.

        A live capture session surviving into interpreter shutdown is how a
        native callback ends up running against half-freed state. Callers are
        required to call stop(); this exists so that forgetting is a warning
        rather than a crash. It deliberately does nothing clever - __del__ runs
        at unpredictable times and may see a partly-torn-down interpreter.
        """
        try:
            if self._running:
                self.stop()
        except Exception:                                 # noqa: BLE001
            pass                        # nothing useful is possible this late
