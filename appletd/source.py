"""The handoff: how a LandmarkFrame gets from the capture thread to TD's cook.

The insulation layer between the engine and TouchDesigner. Imports NO pyobjc at module
scope - `InProcessSource` imports `engine` lazily inside `start()` - so the interface
and the box stay importable and testable without dragging AVFoundation into the
process, and an out-of-process source can be swapped in as a transport change.

THE CONTRACT THAT MATTERS: `latest()` NEVER BLOCKS. TouchDesigner cooks on one main
thread, so a Script CHOP that could wait on a lock, a queue or a condition variable is
one that can freeze the application. There is no code path here from the reader to the
writer - not a fast one, none.

A SINGLE SLOT AND NOT A QUEUE: TD cooks at its own rate, unrelated to the camera's. A
queue either grows without bound, with latency creeping up until the overlay follows a
hand that has already moved, or needs draining logic amounting to "keep only the last
one" - which is this, without the allocations.

Thread: the writer is the capture queue; the reader is whichever thread cooks.
Ref: DESIGN.md 4.3, 5, 6.1.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar, runtime_checkable

# appletd.slots depends only on appletd.types - no pyobjc - so importing
# it at module scope does not compromise this module's promise to be importable,
# and testable, with no frameworks present. test_boundaries.py enforces that.
from appletd.face_types import FaceFrame, blank_face_frame
from appletd.pose_types import PoseFrame, blank_pose_frame
from appletd.slots import SLOT_MODE_CHIRALITY
from appletd.streams import (
    DEFAULT_SEGMENT_QUALITY,
    DEFAULT_STREAMS,
    REQUEST_DEPTH,
    REQUEST_FLOW,
    REQUEST_SEGMENT,
    STREAM_FACE,
    STREAM_HANDS,
    STREAM_POSE,
)
from appletd.types import LandmarkFrame, blank_frame

if TYPE_CHECKING:
    # Type-checking only: these imports do NOT execute at runtime, so the module
    # keeps its property of being importable with no pyobjc present, while mypy
    # still gets the real types instead of a pile of `# type: ignore`.
    from appletd.depth import DepthFrame
    from appletd.engine import HandEngine
    from appletd.flow import FlowImage

    # `pins` imports numpy AT MODULE SCOPE, and this module promises to be importable
    # with neither pyobjc nor numpy present - `appletd/__init__.py` says the core
    # layer needs no numpy and `test_boundaries.py` enforces it. So the type comes in
    # here and the VALUE is passed straight through without this module ever touching
    # it. That is the same reason `HandEngine` is a TYPE_CHECKING import.
    from appletd.pins import Pin
    from appletd.segmentation import MaskImage

# Frames whose seq is 0 have never been published: `blank_frame()` uses 0 and the
# engine's counter starts at 1. Named because "if frame.seq" reads like a
# truthiness accident rather than a documented sentinel.
SEQ_NEVER_PUBLISHED = 0


def _never_negative(age_ms: float) -> float:
    """Clamp an age at zero.

    A negative age is never a real condition. Every timestamp here comes from
    `time.monotonic()` in this one process, so a frame cannot genuinely have
    been captured in the future; a negative value can only be a sampling
    artifact - the reader's clock read landing either side of a publish. The
    ordering in `age_ms` narrows that window to almost nothing, but "almost"
    is not a guarantee worth publishing to a CHOP, where a negative age reads
    as fresher than fresh and would sail through any staleness gate
    (DESIGN.md 6.2).

    Note what this does NOT hide: a stale frame still reports a large age, and a
    producer that never delivers still reports a rising one. Only the physically
    impossible case is flattened.
    """
    return age_ms if age_ms > 0.0 else 0.0


class TimedFrame(Protocol):
    """What the box needs of whatever it carries: a sequence number and a capture
    time. Read-only properties, because the frames are FROZEN dataclasses and
    that immutability is what makes the missing lock safe (see LatestBox)."""

    @property
    def seq(self) -> int: ...
    @property
    def captured_at(self) -> float: ...


# Invariant, not covariant: a box is written as well as read.
FrameT = TypeVar("FrameT", bound=TimedFrame)


class LatestBox(Generic[FrameT]):
    """A single-slot, lock-free latest-value box. One writer, many readers.

    Generic over the frame type: every subtlety below applies identically to a hand,
    a pose and a face frame, and it is the reasoning rather than the code that is
    expensive to duplicate.

    Thread: `publish()` is called from the capture thread and ONLY from there.
            `latest()` may be called from any thread, as often as it likes, and
            cannot block.

    It needs no lock because the only shared mutable state is `_frame`, and updating
    it is a single attribute rebind - atomic under the GIL, so a reader sees the old
    reference or the new one and never a half-written pointer. The frame itself is
    frozen and its contents immutable, so a reader that got the old one can keep
    reading it safely while the writer moves on.

    `age_ms` reads the FRAME before the clock, deliberately: the other order can
    report a negative age when a publish lands between the two reads.
    """

    def __init__(self, blank: FrameT) -> None:
        # Starts as a real frame rather than None, so every reader - including
        # the first cook, before the camera has produced anything - gets the
        # full fixed shape and never has to branch on None. The fixed channel
        # list in DESIGN.md 6.2 depends on this being true from the first cook.
        self._frame: FrameT = blank
        self._n_published = 0        # written by the capture thread only
        self._n_read = 0             # written by readers only
        self._started_at: float | None = None

    def mark_started(self) -> None:
        """Record when the producer started, for the age of a box that has never
        received anything. Called from the starting thread, before any publish.

        Why the age of nothing is not zero: a source that starts and never
        delivers a frame is the failure this whole diagnostic exists to make
        visible (DESIGN.md 6.2 - "treat a rising age_ms as engine death"). If
        `age_ms` read 0 until the first frame arrived, a camera that never
        started would look perfectly healthy forever, which is exactly backwards.
        Measuring from the start time means the number rises from the moment
        there was an expectation of data.
        """
        self._started_at = time.monotonic()

    def publish(self, frame: FrameT) -> None:
        """Make `frame` the latest. Called on the capture thread.

        This is the entire write path: one store, one increment. It is
        deliberately trivial, because it runs inside the camera callback and
        anything slower here shows up as dropped frames.
        """
        self._frame = frame
        self._n_published += 1

    def latest(self) -> FrameT:
        """The most recent frame. NEVER blocks. Called on TD's main thread.

        Returns a blank frame with seq == SEQ_NEVER_PUBLISHED if nothing has been
        published yet, so the caller always has the full fixed shape to write.
        """
        frame = self._frame          # single atomic read; nothing else touches it
        self._n_read += 1
        return frame

    def age_ms(self, now: float | None = None) -> float:
        """Milliseconds since the latest frame was captured.

        Contract: measured from the last frame's capture time, or - if nothing
                  has ever been published - from when the producer started, so
                  a source that never delivers produces a steadily rising number
                  rather than a reassuring zero. Returns 0.0 only if the producer
                  has not been started at all, which is the one case where "no
                  data" genuinely means "nothing was expected yet".
        Thread: safe from any thread; reads two values that each have one writer.
        """
        # Frame FIRST, clock second. Sampling `now` first leaves a window - the
        # reader can be preempted between the two lines, default switch interval
        # 5 ms - in which a newer frame lands with captured_at later than `now`,
        # and the age comes out NEGATIVE. Measured: 55 negative
        # readings in 7.5M against a fast synthetic producer, worst -6.3 ms. It
        # never happened against the real engine, because Vision's ~3.4 ms
        # covers the window - but FakeSource is what the CHOP is developed
        # against, and a negative age there would read as a fresh frame.
        frame = self._frame
        now = time.monotonic() if now is None else now
        if frame.seq != SEQ_NEVER_PUBLISHED:
            return _never_negative((now - frame.captured_at) * 1e3)
        if self._started_at is None:
            return 0.0
        return _never_negative((now - self._started_at) * 1e3)

    @property
    def n_published(self) -> int:
        return self._n_published

    @property
    def n_read(self) -> int:
        return self._n_read


class LatestFrameBox(LatestBox[LandmarkFrame]):
    """The hands box. Constructed with no arguments, as it always has been - the
    generic base took a blank frame parameter and every existing call site says
    `LatestFrameBox()`."""

    def __init__(self) -> None:
        super().__init__(blank_frame())


class LatestPoseBox(LatestBox[PoseFrame]):
    """The body-pose box (DESIGN.md 6.4).

    A box of its own rather than a second field on the hands frame: the two
    streams are published by two separate Vision requests, either can be off, and
    a shared frame would mean one stream's failure blanking the other's channels.
    """

    def __init__(self) -> None:
        super().__init__(blank_pose_frame())


@runtime_checkable
class HandSource(Protocol):
    """Where a Script CHOP gets its landmarks from.

    runtime_checkable so `isinstance(source, HandSource)` works and conformance
    can be ASSERTED rather than assumed. Without it, nothing in the repo bound a
    HandSource, so mypy never checked conformance either - it turned out
    `FakeSource.errors` and the whole suite stayed green under ruff and
    mypy --strict alike. Note what isinstance does and does not buy: it checks
    that the attributes exist, not that they have the right signatures. The
    typed binding in the tests is what covers the signatures.

    The point of this interface is that `td/hands_chop.py` is written against it
    and never against a camera. That is what makes an out-of-process source or a
    C++ backend (DESIGN.md 9) a transport change rather than a rewrite, and what
    lets the CHOP be tested with a fake source and no hardware at all.

    Implementations must guarantee: `latest()` and `age_ms()` never block,
    `start()` and `stop()` are idempotent, and `stop()` is safe to call whether
    or not `start()` succeeded.
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def latest(self) -> LandmarkFrame: ...
    def age_ms(self) -> float: ...
    @property
    def running(self) -> bool: ...
    @property
    def errors(self) -> list[str]: ...


class LatestFaceBox(LatestBox[FaceFrame]):
    """The face box (DESIGN.md 6.4). Same argument as LatestPoseBox."""

    def __init__(self) -> None:
        super().__init__(blank_face_frame())


@runtime_checkable
class PoseSource(Protocol):
    """The body-pose half of a source, kept separate from `HandSource`.

    Separate rather than folded in, for two reasons. `HandSource` is what
    `td/hands_chop.py` and every existing test are written against, and widening
    it would make every one of them owe a pose implementation they have no use
    for. And a source genuinely can support one stream and not the other - the
    sidecar asks `isinstance(source, PoseSource)` and falls back to sending the
    blank frame's zeros, which is the contract a disabled stream has anyway
    (DESIGN.md 6.4).

    Implementations must guarantee the same things `HandSource` does:
    `latest_pose()` and `pose_age_ms()` never block.
    """

    def latest_pose(self) -> PoseFrame: ...
    def pose_age_ms(self) -> float: ...
    @property
    def streams_started(self) -> tuple[str, ...]: ...


@runtime_checkable
class FaceSource(Protocol):
    """The face half of a source. Separate from `PoseSource` for the same reason
    that one is separate from `HandSource`: a source can implement one stream and
    not another, and the sidecar asks before it reads (DESIGN.md 6.4)."""

    def latest_face(self) -> FaceFrame: ...
    def face_age_ms(self) -> float: ...


class InProcessSource:
    """A HandSource and PoseSource backed by the camera and Vision.

    Thread: `start()`/`stop()` on the caller's thread; the engine publishes into
            the box from the capture queue. `latest()` is lock-free.
    Lifecycle: DESIGN.md 8. `stop()` must be wired into the TD project's unload
            path, and `start()` is idempotent so a reload cannot end up with two
            engines fighting over the camera.
    """

    def __init__(self, camera_name: str | None = None,
                 width_px: int | None = None,
                 height_px: int | None = None,
                 slot_mode: str = SLOT_MODE_CHIRALITY,
                 streams: tuple[str, ...] = DEFAULT_STREAMS,
                 on_mask: Callable[[MaskImage], None] | None = None,
                 mask_quality: str = DEFAULT_SEGMENT_QUALITY,
                 mask_instances: bool = False,
                 flow_accuracy: str = "low",
                 frames_path: str | None = None,
                 on_depth: Callable[[DepthFrame], None] | None = None,
                 on_flow: Callable[[FlowImage], None] | None = None,
                 depth_pins: tuple[Pin, ...] = (),
                 depth_drop_m: float = 0.5,
                 depth_compute: str = "all",
                 flip: bool = False) -> None:
        """`streams` is which Vision requests to run - see appletd/streams.py.

        Read once, here, because it is a launch flag: the sidecar is started with
        it on its command line and there is no channel back into a running
        process (DESIGN.md 6.4). Defaults to hands alone, which is exactly what
        ran before there was a choice.
        """
        self._slot_mode = slot_mode
        self._streams = streams
        # What `start()` managed to build, filled in there. Empty here rather than
        # equal to `streams`, so nothing reports a stream as live before a start.
        self._started: tuple[str, ...] = ()
        # The MASK's destination, injected. This module does not own it and does not
        # import `maskbuf` - a mask goes to a shared buffer rather than into a
        # LatestBox, because a box is for something TouchDesigner PULLS on its own
        # cook and the mask is PUSHED into memory TD maps. The sidecar owns the
        # buffer, the way it owns the socket. See appletd/sidecar.py.
        self._on_mask = on_mask
        # `fast`, not segmentation.py's own `balanced` default: MEASURED at 2.21 ms
        # against 8.54 (DESIGN.md 2.18), and this is the LIVE path where it shares a
        # 16 ms frame with hands' 3.41. A project that wants the feathered edge
        # `balanced` gives can ask for it; the default should not spend a third of
        # the frame budget without being asked.
        self._mask_quality = mask_quality
        self._mask_instances = mask_instances
        self._flow_accuracy = flow_accuracy
        # Set means TOP Input: frames arrive through this buffer and no camera is
        # opened at all. None means the camera, which is the default everywhere.
        self._frames_path = frames_path
        self._frames_stop: threading.Event | None = None
        self._frames_thread: threading.Thread | None = None
        # Held by the reader thread around the ONE call that reaches into the engine.
        # `stop()` takes it to know that no frame is inside the engine before it
        # begins tearing that engine down - see `_stop_frame_reader`.
        self._frames_submitting: threading.Lock | None = None
        # DEPTH's destination and its configuration. The PINS live here rather than in
        # the detector's defaults because they are a property of the ROOM, not of the
        # model - and a default pin list that silently produced metres for somebody
        # else's room would be the most convincing wrong number in the project. An
        # empty tuple means "no metric claim", which `pins.py` reports as such.
        self._on_depth = on_depth
        self._on_flow = on_flow
        self._depth_pins = depth_pins
        self._depth_drop_m = depth_drop_m
        self._depth_compute = depth_compute
        self.box = LatestFrameBox()
        # Always constructed, even with pose disabled, so `latest_pose()` has the
        # full fixed shape to publish from the first tick. A disabled stream sends
        # zeros rather than nothing (DESIGN.md 6.4), and this is where the zeros
        # come from.
        self.pose_box = LatestPoseBox()
        self.face_box = LatestFaceBox()
        # Guards start()/stop() against each other. It is NEVER taken by
        # latest() or age_ms() - the read path stays lock-free, which is the
        # whole point of this module. This only stops two callers building two
        # engines through the check-then-act at the top of start(); TD calls
        # these from its main thread, so it is cheap insurance rather than a
        # response to an observed failure.
        self._lifecycle_lock = threading.Lock()
        # Errors from engines that have since been stopped or failed to start.
        # Without this, `errors` returns [] the moment the engine is released -
        # which is precisely when a caller most wants to know why. The M3 review
        # measured a failed start reporting a rising age_ms and an empty error
        # list, leaving the CHOP able to say "stale" but not "why".
        self._retained_errors: list[str] = []
        # Stored rather than applied, because the engine - and therefore pyobjc -
        # is not imported until start(). None means "whatever engine.py's
        # measured defaults are", so the defaults live in exactly one place.
        # CAMERA FLIP. Held rather than acted on: it reaches Vision as an image
        # ORIENTATION on the request, so nothing here touches a pixel.
        self._flip = flip
        self._camera_name = camera_name
        self._width_px = width_px
        self._height_px = height_px
        self._engine: HandEngine | None = None

    def start(self) -> None:
        """Import the engine, start it, and point it at the box. Idempotent.

        The import is HERE, not at module scope, so that this module stays
        importable - and the CHOP layer stays testable - without pyobjc present.
        Python caches modules, so the cost is paid once and never on a cook.
        """
        with self._lifecycle_lock:
            if self._engine is not None:
                return
            from appletd.engine import (
                DEFAULT_CAMERA_NAME,
                DEFAULT_HEIGHT_PX,
                DEFAULT_WIDTH_PX,
                HandEngine,
            )

            # The pose request is built HERE and passed in, so `engine.py` never
            # imports `pose.py` and a project that has not asked for body pose
            # never constructs the request (DESIGN.md 6.4). Inside the lifecycle
            # lock and before the engine, so a joint-table mismatch raises on
            # this thread with no camera open yet.
            pose_detector = None
            if STREAM_POSE in self._streams:
                from appletd.pose import PoseDetector
                pose_detector = PoseDetector()
            face_detector = None
            if STREAM_FACE in self._streams:
                from appletd.face import FaceDetector
                face_detector = FaceDetector()
            # Built only when it has somewhere to publish. `REQUEST_SEGMENT` in the
            # streams with no `on_mask` would be a request that costs 2.21 ms a frame
            # and throws the result away, so it is treated as off - and `errors`
            # records it, because a launch flag that quietly did nothing is worse
            # than one that refuses.
            # Annotated to the PROTOCOL rather than to either class: the engine
            # drives the single-person and the instance detector identically.
            from appletd.segmentation import MaskDetector
            seg_detector: MaskDetector | None = None
            if REQUEST_SEGMENT in self._streams:
                if self._on_mask is None:
                    self._retained_errors.append(
                        "segment was requested but no mask destination was given, "
                        "so the request was not built")
                else:
                    if self._mask_instances:
                        from appletd.segmentation import (
                            InstanceSegmentationDetector,
                        )
                        seg_detector = InstanceSegmentationDetector()
                    else:
                        from appletd.segmentation import SegmentationDetector
                        seg_detector = SegmentationDetector(self._mask_quality)

            flow_detector = None
            if REQUEST_FLOW in self._streams:
                if self._on_flow is None:
                    self._retained_errors.append(
                        "flow was requested but no destination was given, so the "
                        "request was not built")
                else:
                    from appletd.flow import FlowDetector
                    flow_detector = FlowDetector(self._flow_accuracy)

            depth_detector = None
            if REQUEST_DEPTH in self._streams:
                if self._on_depth is None:
                    self._retained_errors.append(
                        "depth was requested but no map destination was given, so "
                        "the request was not built")
                else:
                    # Built HERE, inside the lifecycle lock and before the engine, so
                    # a missing model - which is the first thing a new clone hits -
                    # raises on THIS thread with no camera open. The message names
                    # tools/fetch_models.sh; depth.py owns that wording.
                    from appletd.depth import DepthDetector
                    depth_detector = DepthDetector(
                        pins=self._depth_pins, drop_m=self._depth_drop_m,
                        compute=self._depth_compute)

            # WHAT WAS ACTUALLY BUILT, which is not the same as what was asked for.
            # Three of these requests are dropped when they have no destination -
            # `segment`, `flow` and `depth` above - and `_retained_errors` says so,
            # but `streams_started` was returning `self._streams` and so published
            # `sc_segment = 1` for a request that had not been made. Its own
            # docstring promises the opposite (DESIGN.md 6.4).
            self._started = tuple(
                name for name in self._streams
                if not (name == REQUEST_SEGMENT and seg_detector is None)
                and not (name == REQUEST_FLOW and flow_detector is None)
                and not (name == REQUEST_DEPTH and depth_detector is None))

            engine = HandEngine(
                on_frame=self.box.publish,
                hands=STREAM_HANDS in self._streams,
                pose_detector=pose_detector,
                on_pose=self.pose_box.publish if pose_detector is not None else None,
                face_detector=face_detector,
                on_face=self.face_box.publish if face_detector is not None else None,
                segmentation_detector=seg_detector,
                on_mask=self._on_mask if seg_detector is not None else None,
                depth_detector=depth_detector,
                on_depth=self._on_depth if depth_detector is not None else None,
                flow_detector=flow_detector,
                on_flow=self._on_flow if flow_detector is not None else None,
                flip=self._flip,
                camera_name=self._camera_name or DEFAULT_CAMERA_NAME,
                width_px=self._width_px or DEFAULT_WIDTH_PX,
                height_px=self._height_px or DEFAULT_HEIGHT_PX,
                # Continue the sequence rather than restarting it. A new engine
                # object starts counting from zero, and the box still holds the
                # previous session's frame until the new one publishes - so a
                # reload made seq jump 75 -> 2 (MEASURED, M3 review), which
                # silently defeats every downstream `seq > last_seq` check until
                # the new engine catches up. DESIGN.md 6.1 says monotonic.
                seq_start=self.box.latest().seq,
                # Which physical hand lands in which slot - see
                # appletd/slots.py.
                slot_mode=self._slot_mode,
            )
            # Marked before starting, so the age of a camera that never delivers
            # is measured from the attempt rather than from the first success.
            self.box.mark_started()
            # The pose box is marked only when the stream is ON. A disabled
            # stream's age must stay 0 rather than climbing: a rising age means
            # "expected data that never came" (DESIGN.md 6.2), and nothing is
            # expected from a stream nobody asked for.
            if pose_detector is not None:
                self.pose_box.mark_started()
            if face_detector is not None:
                self.face_box.mark_started()
            try:
                if self._frames_path:
                    # No camera. The engine is made ready to RECEIVE frames and a
                    # reader thread feeds it from the buffer TouchDesigner writes.
                    engine.start_frames_only()
                    self._start_frame_reader(engine)
                else:
                    engine.start()
            except Exception as exc:
                # Not a blind swallow: the exception propagates. But it is
                # RECORDED first - a caller that only watches the CHOP's health
                # channels never sees this traceback, and "stale with no reason"
                # is the least actionable diagnostic there is.
                self._retained_errors.append("start failed: %s" % exc)
                self._engine = None
                raise
            self._engine = engine

    def _start_frame_reader(self, engine: HandEngine) -> None:
        """Feed the engine from the frame buffer until told to stop.

        A THREAD AND NOT A QUEUE, unlike the camera path, because there is no
        AVFoundation session here to deliver on one - but the guarantee it has to
        provide is the same: exactly one thread ever calls into the engine, because
        the detectors hold Vision's inter-frame state and are not thread-safe.
        """
        from appletd.frames import FrameBufferReader

        reader = FrameBufferReader(self._frames_path or "")
        # HELD, so the status line can say whether frames are actually arriving. In
        # TOP Input mode a sidecar with nothing wired to the COMP's image input runs
        # perfectly and finds nothing, and without this that is indistinguishable
        # from a tracking failure.
        self.frame_reader = reader
        stop_event = threading.Event()
        submitting = threading.Lock()

        def pump() -> None:
            from appletd.frames import sample_buffer_from_bgra
            while not stop_event.is_set():
                try:
                    newest = reader.latest()
                except Exception as exc:               # noqa: BLE001
                    # Recorded, not raised: a malformed frame is the writer's fault
                    # and killing the reader would take every stream down with it.
                    self._retained_errors.append("frame read failed: %s" % exc)
                    stop_event.wait(0.25)
                    continue
                if newest is None:
                    # Nothing new. TouchDesigner writes at its frame rate and this
                    # polls faster, so this is the common case - a short sleep rather
                    # than a spin, and short enough not to add latency of its own.
                    stop_event.wait(0.002)
                    continue
                pixels, captured_at = newest
                # THE ONE CALL THAT REACHES INTO THE ENGINE, and the only thing the
                # lock covers. Re-checking `stop_event` INSIDE it is what makes the
                # barrier a barrier: a teardown that has set the event and taken the
                # lock knows both that no frame is in the engine now and that none
                # can enter, because the next pass through here is holding nothing
                # and will see the flag.
                with submitting:
                    if stop_event.is_set():
                        break
                    try:
                        engine.submit_sample_buffer(
                            sample_buffer_from_bgra(pixels, captured_at))
                    except Exception as exc:           # noqa: BLE001
                        self._retained_errors.append("frame submit failed: %s" % exc)
                        stop_event.wait(0.25)
            reader.close()

        thread = threading.Thread(target=pump, name="appletd-frames", daemon=True)
        thread.start()
        self._frames_stop = stop_event
        self._frames_thread = thread
        self._frames_submitting = submitting

    def _stop_frame_reader(self) -> None:
        """Stop the reader thread, and do not return while a frame is in the engine.

        `stop()`'s own comment says this has to happen "before the engine, or a frame
        can arrive mid-teardown" - and a bounded `join` does not deliver that. A
        reader that had not finished in a second was simply left running, and
        `engine.stop()` went ahead underneath it: `submit_sample_buffer` runs Vision
        requests against detectors the stop is releasing, which is a segfault rather
        than an exception.

        So the join is still bounded - a reader that will not go must not hang
        TouchDesigner - but a timeout falls through to the submit lock, which is the
        actual barrier. Anything past both is recorded, because at that point the
        guarantee is genuinely gone and saying so is all that is left.
        """
        stop_event, thread = self._frames_stop, self._frames_thread
        submitting = self._frames_submitting
        self._frames_stop, self._frames_thread = None, None
        self._frames_submitting = None
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive():
            # Bounded, like `stop()` in the launcher: a reader that will not go is
            # reported rather than waited on for ever.
            thread.join(timeout=1.0)
        if thread is not None and thread.is_alive() and submitting is not None:
            # It did not finish. Take the lock the pump holds around its submit: once
            # this returns, no frame is inside the engine and none can enter, because
            # the pump re-checks the stop event under the same lock.
            if submitting.acquire(timeout=2.0):
                submitting.release()
            else:
                self._retained_errors.append(
                    "the TOP Input reader was still inside the engine 3 s after "
                    "being told to stop; tearing down anyway")

    def stop(self) -> None:
        """Stop the engine and release it. Idempotent, and safe after a failed start.

        ORDER: stop the engine, THEN drop the reference, and only in a finally.
        Nulling first means that if `engine.stop()` raises - a pyobjc exception
        out of stopRunning, say - the live capture session becomes unreachable:
        `running` reports False, a retry of stop() is a no-op, and nothing can ever
        shut it down. Keeping the reference until the stop has actually returned
        makes a retry possible.
        """
        # BEFORE the lock and before the engine: the reader thread calls INTO the
        # engine, so it has to be stopped first or a frame can arrive mid-teardown.
        self._stop_frame_reader()
        with self._lifecycle_lock:
            engine = self._engine
            if engine is None:
                return
            try:
                # Copy the engine's errors out BEFORE releasing it, so a caller
                # can still ask why after the source has been stopped.
                self._retained_errors.extend(
                    e for e in engine.errors if e not in self._retained_errors)
                engine.stop()
            finally:
                self._engine = None
                self._started = ()

    def latest(self) -> LandmarkFrame:
        return self.box.latest()

    def age_ms(self) -> float:
        return self.box.age_ms()

    def latest_pose(self) -> PoseFrame:
        """The most recent body-pose frame, or the blank one with pose disabled.

        Never blocks, never None, and never absent: the caller publishes a fixed
        channel list whatever is enabled (DESIGN.md 6.4).
        """
        return self.pose_box.latest()

    def pose_age_ms(self) -> float:
        return self.pose_box.age_ms()

    def latest_face(self) -> FaceFrame:
        """The most recent face frame, or the blank one with face disabled."""
        return self.face_box.latest()

    def face_age_ms(self) -> float:
        return self.face_box.age_ms()

    @property
    def streams_started(self) -> tuple[str, ...]:
        """Which streams are ACTUALLY running, not which were asked for.

        Empty until `start()` has succeeded, and empty again after `stop()`, which
        is the distinction the sidecar's `sc_*` channels publish: a stream that
        was requested and failed to start must not report itself as live
        (DESIGN.md 6.4).

        `_started`, not `_streams`: `segment`, `flow` and `depth` are each dropped
        when nothing was given to publish them to, and this used to report them
        anyway - which is the one thing the sentence above says it does not do.
        """
        return self._started if self.running else ()

    @property
    def running(self) -> bool:
        engine = self._engine
        return bool(engine is not None and engine.running)

    @property
    def errors(self) -> list[str]:
        """Faults from the current engine AND from engines already released.

        Returns a COPY: the engine's list is appended to from the capture thread,
        and handing out the live object would let a caller iterate it while it
        grows.

        Retained errors come first because they are the older ones. A failed
        start leaves its reason here even though there is no engine to ask,
        which is what lets a CHOP publish "stale, and here is why" rather than
        just "stale".
        """
        engine = self._engine
        live = list(engine.errors) if engine is not None else []
        return self._retained_errors + [e for e in live if e not in self._retained_errors]


class FakeSource:
    """A HandSource that publishes synthetic frames from a thread. No camera.

    This exists so the concurrency contract can be tested for real - a genuine
    background thread writing while a foreground thread reads - without a
    camera, without pyobjc, and deterministically enough to run in CI. Milestone
    3 in DESIGN.md 10 asks for exactly this, and the reason is that a
    lock-freedom claim verified only against hardware is a claim verified on
    whatever the hardware happened to do that afternoon.

    It is also what `td/hands_chop.py` will be developed against, since a Script
    CHOP that can only be exercised with a live camera is a Script CHOP nobody
    tests.
    """

    # How long stop() waits for the publisher. Generous: the publisher checks
    # its Event every interval and between frames, so anything approaching this
    # means frame_factory itself is blocking, which is a caller bug worth
    # raising on rather than a timing tolerance worth widening.
    JOIN_TIMEOUT_S = 2.0

    def __init__(self, frame_factory: Callable[[int], LandmarkFrame],
                 interval_s: float = 1.0 / 30.0) -> None:
        """`frame_factory(seq) -> LandmarkFrame`, called on the publishing thread."""
        self.box = LatestFrameBox()
        self._frame_factory = frame_factory
        self._interval_s = interval_s
        self._thread: threading.Thread | None = None
        # ONE EVENT PER RUN, created in start() and owned by that run's thread.
        #
        # A single Event cleared in start() costs this: if a publishing thread has
        # not finished when stop() returns, a later start() clears the shared Event
        # and RESURRECTS the orphan, which then publishes alongside the new thread -
        # two writers into one LatestFrameBox, breaking the single-writer invariant
        # this module's correctness rests on.
        #
        # Honest accounting of which fix does the work: the barrier that
        # actually prevents this now is stop() keeping `_thread` set when the
        # join times out, so start() sees a live publisher via is_alive() and
        # declines. Mutating the Event back to shared-and-cleared, on its own,
        # does NOT reproduce the bug - verified. The per-run Event is
        # defence-in-depth: it makes an orphan un-resurrectable even if that
        # guard is ever weakened, and it costs one object per start.
        self._stop: threading.Event | None = None
        self.errors: list[str] = []

    def start(self) -> None:
        """Idempotent. Refuses to start a second publisher while one is alive."""
        # is_alive(), not `is not None`: stop() leaves _thread set when a join
        # times out, precisely so this check can see that a publisher is still
        # running and decline to add another.
        if self._thread is not None and self._thread.is_alive():
            return
        stop_event = threading.Event()
        self.box.mark_started()
        thread = threading.Thread(target=self._run, args=(stop_event,),
                                  name="appletd-fake", daemon=True)
        # Started BEFORE being recorded: if Thread.start() raises - thread
        # exhaustion - recording it first leaves `_thread` set, which makes running
        # report True, start() a permanent no-op, and stop() raise "cannot join
        # thread before it is started" on the teardown path.
        thread.start()
        self._thread = thread
        self._stop = stop_event

    def _run(self, stop_event: threading.Event) -> None:
        """The publisher. Takes its stop Event as an ARGUMENT, not from self.

        That is what makes an orphaned run un-resurrectable: this thread watches
        the Event it was born with, and a later start() creating a new Event
        cannot reach it.
        """
        seq = 0
        while not stop_event.is_set():
            seq += 1
            self.box.publish(self._frame_factory(seq))
            # wait() returns the moment stop is set, so stopping never has to
            # wait out the remaining interval.
            stop_event.wait(self._interval_s)

    def stop(self) -> None:
        """Stop publishing and WAIT for it to have stopped. Idempotent.

        Raises: RuntimeError if the publisher will not stop. That is deliberate.
                The whole value of this class as a test double is that after
                stop() returns, nothing is still writing into the box a test is
                about to assert on. Promising that without checking the join
                result makes it a lie the moment `frame_factory` is slow: stop()
                returns with the publisher still running and `running` reporting
                False. A test double that lies about its own state is worse than
                no test double.
        """
        stop_event = self._stop
        if stop_event is not None:
            stop_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=self.JOIN_TIMEOUT_S)
        if thread.is_alive():
            # Keep _thread set: start() checks is_alive(), so this stops a
            # second publisher joining the first.
            raise RuntimeError(
                "FakeSource publisher did not stop within %.1fs - it is still "
                "writing into the box. Is frame_factory blocking?"
                % self.JOIN_TIMEOUT_S)
        self._thread = None
        self._stop = None

    def latest(self) -> LandmarkFrame:
        return self.box.latest()

    def age_ms(self) -> float:
        return self.box.age_ms()

    @property
    def running(self) -> bool:
        """True while a publisher thread is actually alive.

        is_alive() rather than a flag, so this cannot report False while a
        thread is still writing - the exact lie it turned out.
        """
        thread = self._thread
        return thread is not None and thread.is_alive()
