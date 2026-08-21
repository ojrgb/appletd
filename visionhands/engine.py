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
import weakref
from collections.abc import Callable
from typing import Any

import AVFoundation as AVF
import CoreMedia
import objc
import Quartz
import Vision
from Foundation import NSObject
from libdispatch import (
    dispatch_queue_attr_make_with_qos_class,
    dispatch_queue_create,
    dispatch_sync,
)

from visionhands.slots import SLOT_MODE_CHIRALITY, SlotAssigner
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

# Belt, worn with the braces of a dispatch_sync barrier (see _drain_capture_queue).
#
# This value used to be the ONLY thing standing between stop() and a callback
# still running. The M2b review demonstrated that it is not sufficient: with a
# consumer slower than this, stop() returned while a delegate callback was still
# executing, and a following start() then put two dispatch queues into the same
# HandDetector - which was measured publishing found=False for a hand that was
# in shot, with no error and no dropped-frame count. A guessed duration cannot
# be a barrier. dispatch_sync is one; this now only covers the window between
# stopRunning() and the barrier.
CALLBACK_DRAIN_S = 0.2

# AVAuthorizationStatus.authorized. The TCC prompt attaches to the HOST app
# (TouchDesigner.app), never to this code (DESIGN.md 8).
AUTH_STATUS_AUTHORIZED = 3

# Frames per second to PIN the camera to, both floor and ceiling.
#
# MEASURED, and this is the fix for the single most visible quality problem.
# The built-in camera advertises a 15-30 fps range, and its
# activeVideoMaxFrameDuration sits at 1/15 s - which means auto-exposure is free
# to HALVE the frame rate in dim light to buy a longer exposure. It does. A TD
# session left running for 25 minutes drifted from 25 fps to 13.4 fps with
# inter-frame gaps swinging from 15 ms to 101 ms: visibly choppy, and nothing to
# do with our code or with TouchDesigner. Short standalone runs never showed it
# because the camera had not settled yet.
#
# Pinning min == max forces a constant rate. The cost is real and worth stating:
# in low light the image gets darker rather than slower, because the camera can
# no longer buy brightness with time. That is the right trade for hand tracking,
# where a late landmark is worse than a dim one - but it is a trade, and
# TARGET_FPS is a constructor argument so it can be changed or disabled.
TARGET_FPS = 30

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

    Raised on the capture thread too, by HandDetector - but never allowed to
    ESCAPE it. An exception crossing an Objective-C callback boundary unwinds
    into native frames, which is its own crash (STANDARDS.md 2), so
    `_on_sample_buffer` catches this type explicitly and counts it, and the
    delegate's guard catches anything else. What reaches the caller's thread is
    a recorded string in `HandEngine.errors` and a rising `n_dropped`.
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

    # The other direction: does Vision know about a joint WE do not? Checking
    # only our rows against the framework would silently ignore a 22nd joint -
    # `hand_from_observation` skips unknown codes, so the extra landmark would
    # simply never reach TD and nothing would say why. Vision exposes exactly 21
    # of these constants today (MEASURED), so the comparison costs one dir().
    # `len(name) > len(prefix)` is not defensive padding: Vision also exports
    # the bare prefix `VNHumanHandPoseObservationJointName` as a type alias, and
    # without this the check reports a phantom joint whose suffix is the empty
    # string. Found by this check failing on its first run.
    framework_suffixes = {
        name[len(JOINT_CONSTANT_PREFIX):] for name in dir(Vision)
        if name.startswith(JOINT_CONSTANT_PREFIX) and len(name) > len(JOINT_CONSTANT_PREFIX)
    }
    unknown = framework_suffixes - {joint.suffix for joint in JOINTS}
    if unknown:
        problems.append(
            "this Vision has joints our table does not: %s. They would be "
            "silently dropped rather than published." % sorted(unknown))

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

    Slots: hands land here in whatever order Vision returned them, which is NOT
              stable between frames - Vision provides no tracking ID. Assignment
              happens one level up, in `HandEngine._publish`, via
              `visionhands.slots.SlotAssigner`. Deliberately not here: this
              function is shared with the replay path, and the assigner holds
              per-session state that a pure observations-to-frame conversion has
              no business owning. So a caller of THIS function gets Vision's
              order and must not assume otherwise.
    """
    hands: list[Hand] = []
    n_unreadable = 0
    for observation in observations[:MAX_HANDS]:
        hand = hand_from_observation(observation)
        if hand is None:
            n_unreadable += 1
            continue
        hands.append(hand)
    while len(hands) < MAX_HANDS:
        hands.append(BLANK_HAND)        # shared, frozen: no allocation

    if n_unreadable:
        # An observation whose points could not be read is a hand that was
        # detected and then lost between Vision and the channel list. Rare
        # enough that it has never been seen, silent enough to be worth
        # surfacing if it ever happens (STANDARDS.md 2: never silently dropped).
        raise EngineError(
            "%d observation(s) had unreadable joint points and were dropped"
            % n_unreadable)

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
        # A knob that cannot be honoured is worse than no knob. MAX_HANDS is not
        # a preference, it is the width of the published channel list
        # (DESIGN.md 6.2) - asking for three hands would pay Vision's cost for a
        # third and then drop it with nowhere to put it, which the M2b review
        # found happening silently.
        if not 1 <= max_hands <= MAX_HANDS:
            raise EngineError(
                "max_hands must be between 1 and MAX_HANDS (%d); the channel "
                "contract has no room for more" % MAX_HANDS)
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

    LIFETIME - and this correction matters, because the obvious worry is the
    wrong one. The C function does not copy, so in C the caller must outlive the
    buffer. Through pyobjc it is safe: MEASURED in the M2b review, wrapping an
    array raises its refcount and the array survives `del` plus a gc pass,
    dying only when the pixel buffer is released. The cost is the other way
    round - each live buffer PINS its array, so wrapping a whole clip at once
    holds every frame in memory (~1 GB for this fixture).

    The real trap is shape. The row stride must be the array's own stride, never
    width*4, and the array must genuinely be 4-channel: an HxWx3 array is
    accepted by the C call and Vision then reads past the end of the
    allocation - measured returning found=True on memory it did not own. Hence
    the validation below, which is cheap and once per frame.
    """
    if getattr(bgra, "ndim", None) != 3 or bgra.shape[2] != 4:
        raise EngineError(
            "expected an HxWx4 BGRA array, got shape %r - a 3-channel array is "
            "accepted by CVPixelBufferCreateWithBytes and read out of bounds"
            % (getattr(bgra, "shape", None),))
    if str(getattr(bgra, "dtype", "")) != "uint8":
        raise EngineError("expected uint8, got %s" % getattr(bgra, "dtype", "?"))
    if not bgra.flags["C_CONTIGUOUS"]:
        raise EngineError("array must be C-contiguous; use np.ascontiguousarray")

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


def pin_frame_rate(device: ObjCObject, fps: int) -> str | None:
    """Force a constant frame rate. Returns None on success, or a reason.

    Thread: the starting thread, never the capture queue.
    Why both min AND max: setting only the minimum duration caps the top speed
          and leaves the camera free to slow down, which is the behaviour being
          fixed. Equal min and max is what "constant rate" means to AVFoundation.
    Order: this must happen AFTER the input is added to the session. Adding an
          input resets the device to whatever the session preset implies - the
          same trap that silently reverts setActiveFormat_ (DESIGN.md 3).
    Traps: lockForConfiguration_ is an out-param selector, and the lock MUST be
          released on every path or the device stays locked for the life of the
          process and nothing else can configure it.
    Returns a string rather than raising: a camera that will not be pinned is
          still a usable camera, so this is a degradation to report, not a
          failure to abort on.
    """
    ranges = device.activeFormat().videoSupportedFrameRateRanges()
    supported = [(float(r.minFrameRate()), float(r.maxFrameRate())) for r in ranges]
    if not any(low <= fps <= high for low, high in supported):
        return ("cannot pin %d fps; this format supports %s" % (fps, supported))

    ok, err = device.lockForConfiguration_(None)          # TRAP: out-param
    if not ok:
        return "could not lock camera for configuration: %s" % (err,)
    try:
        duration = CoreMedia.CMTimeMake(1, fps)
        device.setActiveVideoMinFrameDuration_(duration)
        device.setActiveVideoMaxFrameDuration_(duration)
    finally:
        # Unconditionally: a device left locked cannot be reconfigured by
        # anything, including us on the next start().
        device.unlockForConfiguration()

    # Read back rather than trust. The whole reason this function exists is that
    # the camera quietly ignored what we assumed about frame rate.
    got_min = CoreMedia.CMTimeGetSeconds(device.activeVideoMinFrameDuration())
    got_max = CoreMedia.CMTimeGetSeconds(device.activeVideoMaxFrameDuration())
    want = 1.0 / fps
    if abs(got_min - want) > 1e-4 or abs(got_max - want) > 1e-4:
        return ("asked for %d fps but the camera reports min %.4f s / max %.4f s"
                % (fps, got_min, got_max))
    return None


def video_settings(width_px: int, height_px: int) -> dict[Any, Any]:
    """Output-side scale - the ONLY resolution control that works on macOS.

    Traps: setActiveFormat_ is silently reverted by the session preset (default
           High = 1080p) whether set before or after addInput_, and
           AVCaptureSessionPresetInputPriority, the iOS escape hatch, raises
           NSInvalidArgumentException here. Two spike runs silently produced
           1080p while the log claimed 720p, which is why the delegate asserts
           the delivered size instead of trusting this (DESIGN.md 3).
    Pixel format: deliberately absent, which leaves the camera in its native
           420v. MEASURED in the M2b review: Vision does consume 420v directly
           via performRequests_onCMSampleBuffer_error_, with detection identical
           to BGRA on the same clip (273/284 both).
           What is NOT true - and the first version of this comment claimed it -
           is that 420v is cheaper. Vision converts internally, and 420v
           measured 3.94-4.16 ms against BGRA's 3.26-3.39 ms, about 20% SLOWER.
           420v is kept because it avoids a host-side conversion of a buffer
           nothing in the engine reads, and 0.6 ms sits comfortably inside a
           33 ms budget - but if latency ever matters, requesting BGRA here is a
           measured improvement in Vision's half, and the camera-side cost of
           that conversion is still unmeasured.
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
        # WEAK, and this is load-bearing rather than tidy.
        #
        # The engine holds the delegate strongly (self._delegate), because
        # AVCaptureVideoDataOutput does NOT retain its delegate - measured in
        # the M2b review, retainCount unchanged across
        # setSampleBufferDelegate_queue_ - so if we did not hold it, nothing
        # would. A strong reference back to the engine therefore closes a cycle,
        # and that cycle is NOT collectable: pyobjc's Objective-C subclass
        # instances do not expose their Python attributes to the garbage
        # collector, so gc.collect() sees nothing to break. The measured
        # consequence was that any started engine became immortal, __del__ could
        # never fire, and forgetting stop() leaked the engine, the session and
        # the camera permanently - strictly worse than the "two sessions
        # fighting over the camera" that DESIGN.md 8 anticipated, because the
        # old one could never be reclaimed.
        #
        # The failure mode a weakref introduces - the engine dying mid-callback -
        # is handled at the top of the callback, and is anyway the state we want
        # to detect rather than one we want to prevent by keeping a dead engine
        # alive.
        self._engine_ref = weakref.ref(engine)
        return self

    def captureOutput_didOutputSampleBuffer_fromConnection_(
            self, output: ObjCObject, sample_buffer: ObjCObject,
            connection: ObjCObject) -> None:
        engine = self._engine_ref()
        # A dead engine means its owner dropped it without calling stop(). There
        # is nothing safe to do with this buffer and nowhere to report it, so
        # return quietly. The barrier in stop() is what makes this rare rather
        # than routine.
        if engine is None:
            return
        # INVARIANT: once stopping is set, touch nothing native. A Vision call
        # landing on a torn-down session is a process kill, and no `except`
        # catches it (DESIGN.md 8).
        #
        # NOTE precisely what this is: a filter on callbacks that have not
        # started yet. It is NOT a barrier on one already running - the M2b
        # review confirmed a callback still executing after stop() returned.
        # The barrier is dispatch_sync, in _drain_capture_queue.
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
                 max_hands: int = MAX_HANDS,
                 seq_start: int = 0,
                 target_fps: int | None = TARGET_FPS,
                 slot_mode: str = SLOT_MODE_CHIRALITY) -> None:
        """seq_start continues an existing frame sequence rather than restarting it.

        Why it exists: this engine keeps `_seq` monotonic across its OWN
        stop/start, but a caller that replaces the engine object - which is what
        InProcessSource does on a TouchDesigner project reload - would otherwise
        reset the sequence to 1 while consumers still hold frames numbered in
        the hundreds. The M3 review caught exactly that, measuring seq going
        75 -> 2 across a reload, which silently defeats any downstream
        `seq > last_seq` freshness test until the new engine catches up.
        """
        self._on_frame = on_frame
        self._camera_name = camera_name
        self._requested_px = (width_px, height_px)
        self._max_hands = max_hands
        # None leaves the camera's own adaptive rate alone. See TARGET_FPS.
        self._target_fps = target_fps

        # Built here rather than in start(), so that a joint-table mismatch or a
        # missing framework fails at construction - on the caller's thread,
        # where it can be reported - instead of on the capture queue where it
        # could only be recorded.
        self._detector = HandDetector(max_hands=max_hands)

        # Which physical hand goes in which slot. Applied HERE rather than inside
        # HandDetector because the detector is also the replay path's inference
        # step, and a replay of a fixture should be able to ask for either mode
        # without the detector carrying session state it does not own.
        #
        # Constructed rather than validated later, so a bad mode fails on the
        # caller's thread at construction - the same reason the detector is built
        # here rather than in start().
        self._slots = SlotAssigner(slot_mode)

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

        self._seq = seq_start
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
        try:
            frame = self._detector.detect_sample_buffer(
                sample_buffer, self._seq, captured_at)
        except EngineError as exc:
            # Caught here, not left to the delegate's blanket guard, so that a
            # failing inference COUNTS. The M2b review found five consecutive
            # failed frames reporting n_dropped=0 while nothing was published:
            # the error was recorded, but the drop counter a CHOP health channel
            # would publish read perfectly clean. Narrow except on purpose - an
            # unexpected exception type is still a bug and still goes to the
            # delegate's guard.
            self.n_dropped += 1
            self._record_error(str(exc))
            return
        if frame is None:
            self.n_dropped += 1
            return

        # Slots BEFORE the consumer sees anything, so `h0` means the same physical
        # hand to every consumer - the CHOP, the OSC stream, the fixture harness -
        # rather than each of them having to reorder for itself. Vision hands back
        # observations with no tracking ID and in no stable order (DESIGN.md 6.3).
        frame = self._slots.assign(frame)

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

        # Per-SESSION state, reset here rather than only in __init__.
        #
        # The M2b review found all three of these surviving a stop()/start()
        # cycle, which is exactly the TD project-reload sequence DESIGN.md 8
        # cares about:
        #   * delivered_px is only computed when it is None, so the
        #     delivered-versus-requested assertion - the trap that silently lied
        #     twice in the spike - ran once per ENGINE rather than once per
        #     session. A camera that came back at 1080p after a reload would
        #     never have been flagged.
        #   * first_frame stayed set, so a caller waiting on it after a restart
        #     got an immediate false liveness signal, on the very mechanism this
        #     class offers as the way to confirm liveness without polling.
        #   * errors persisted and are deduplicated by message, so a fault
        #     recurring in the new session was suppressed as a duplicate of the
        #     old one.
        #
        # _seq is deliberately NOT reset: DESIGN.md 6.1 wants it monotonic, and a
        # consumer should see a restart as a gap in the sequence rather than as
        # time running backwards.
        self.delivered_px = None
        self.first_frame.clear()
        self.errors.clear()
        self.n_delivered = 0
        self.n_published = 0
        self.n_dropped = 0
        # The slot assigner's memory is per-session too. Without this, the first
        # frame of a new session would be matched against the LAST frame of the
        # old one by the proximity fallback - the same class of stale-state defect
        # the M2b review found three of in this file.
        self._slots.reset()

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

        # Pin the frame rate AFTER addInput_ and commitConfiguration, because
        # adding an input resets the device's rate to whatever the preset
        # implies. Reported rather than raised: an unpinnable camera still works.
        if self._target_fps is not None:
            problem = pin_frame_rate(device, self._target_fps)
            if problem is not None:
                self._record_error("frame rate not pinned: %s" % problem)

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
            # Full shutdown, not a bare _teardown(). The M2b review found this
            # path calling _teardown() directly, which skips flagging and skips
            # stopRunning() - on a session that may already have begun
            # delivering - while _teardown's own contract says it assumes a
            # stopped session. Every guarantee stop() provides has to hold here
            # too, because this is precisely when the state is least known.
            self._shutdown()
            # Drop the local references before raising. The EngineError chains
            # this frame, and TouchDesigner keeps sys.last_traceback, so leaving
            # them bound would keep a dead session and its queue alive for as
            # long as the caller holds the exception.
            del session, output, delegate, queue
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
        self._shutdown()
        self._running = False

    def _shutdown(self) -> None:
        """Flag, stop, drain, unregister, release. The whole sequence, one place.

        Called by stop() and by start()'s failure path, because both need every
        step and neither can be trusted to remember them separately.
        """
        self._stopping = True
        if self._session is not None:
            self._session.stopRunning()
        # Covers the gap between stopRunning() returning and the barrier below:
        # cheap, and it means the barrier usually has nothing left to wait for.
        time.sleep(CALLBACK_DRAIN_S)        # blocking, and deliberately not a poll
        self._drain_capture_queue()
        self._teardown()

    def _drain_capture_queue(self) -> None:
        """Block until any in-flight delegate callback has finished. A REAL barrier.

        Why this exists, and why the sleep above it is not enough. The capture
        queue is serial, so a no-op submitted with dispatch_sync cannot begin
        until everything already on the queue has finished - which is precisely
        the guarantee "wait 200 ms and hope" does not give. The M2b review
        confirmed the old code returning from stop() with a callback still
        running, and confirmed the consequence: a following start() built a
        second queue whose callbacks entered the SAME HandDetector, and two
        threads in one detector was measured publishing found=False for a hand
        that was in shot - no exception, no dropped-frame count, just a hand
        that quietly vanished.

        Thread: called from the stopping thread, never from the capture queue.
                Calling dispatch_sync ON the queue you are running on is an
                instant deadlock; this is only ever reached from stop() or from
                start()'s failure path, both on the caller's thread.
        Cost: microseconds when the queue is idle, up to one frame's inference
              (~3.4 ms) when it is not.
        """
        if self._queue is None:
            return
        # Unregister FIRST, so no new callback can be scheduled behind the one
        # we are about to wait for. AVCaptureVideoDataOutput does not retain its
        # delegate, so this also has to happen before we drop our reference to
        # it, or the output would be left pointing at a freed object.
        if self._output is not None:
            self._output.setSampleBufferDelegate_queue_(None, None)
        dispatch_sync(self._queue, lambda: None)

    def _teardown(self) -> None:
        """Drop references to the native objects.

        Contract: the session must already be stopped, the delegate already
                  unregistered, and the queue already drained - all of which
                  _shutdown() guarantees by calling this last. Called on its own,
                  this is just a way to lose track of a live capture session.
        """
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
