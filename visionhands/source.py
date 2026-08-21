"""The handoff: how a LandmarkFrame gets from the capture thread to TD's cook.

This module is the insulation layer between the engine and TouchDesigner
(DESIGN.md 5). It imports NO pyobjc at module scope - `InProcessSource` imports
`engine` lazily, inside `start()`. That is not tidiness:

  * `td/hands_chop.py` needs the `HandSource` type and the box to write its
    cook function against, and must be importable, testable and readable
    without dragging AVFoundation into the process.
  * The whole point of a `HandSource` interface is that an out-of-process or
    C++-backed source (DESIGN.md 9) can be swapped in as a *transport* change.
    A module-scope `import Vision` here would make pyobjc a hard dependency of
    the interface itself, which is precisely backwards.

THE CONTRACT THAT MATTERS: `latest()` NEVER BLOCKS. TouchDesigner cooks on a
single main thread and everything on it is blocking, so a Script CHOP that could
wait - on a lock, on a queue, on a condition variable - is a Script CHOP that can
freeze the whole application (DESIGN.md 4.1). There is no code path here from
the reader to the writer. Not a fast one; none.

WHY A SINGLE SLOT AND NOT A QUEUE. TD cooks at its own rate, unrelated to the
camera's. A queue either grows without bound - latency creeping up until the
overlay is following a hand that has already moved - or needs draining logic
that amounts to "throw away everything but the last one", which is what this is,
without the intermediate allocations. For a realtime overlay the correct policy
is "most recent result, discard what the renderer never saw" (DESIGN.md 4.3).

Ref: DESIGN.md 4.3 (the lock-free slot), 5 (module layout), 6.1 (LandmarkFrame).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

# visionhands.slots depends only on visionhands.types - no pyobjc - so importing
# it at module scope does not compromise this module's promise to be importable,
# and testable, with no frameworks present. test_boundaries.py enforces that.
from visionhands.slots import SLOT_MODE_CHIRALITY
from visionhands.types import LandmarkFrame, blank_frame

if TYPE_CHECKING:
    # Type-checking only: this import does NOT execute at runtime, so the module
    # keeps its property of being importable with no pyobjc present, while mypy
    # still gets the real HandEngine type instead of a pile of `# type: ignore`.
    from visionhands.engine import HandEngine

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


class LatestFrameBox:
    """A single-slot, lock-free latest-value box. One writer, many readers.

    Thread: `publish()` is called from the capture thread and ONLY from there.
            `latest()` may be called from any thread, as often as it likes, and
            cannot block.

    WHY THIS NEEDS NO LOCK, precisely - because "it's atomic" is the kind of
    claim that is right until it isn't:

      * The only shared mutable state is `_frame`, and updating it is a single
        attribute rebind. CPython's GIL makes that store atomic with respect to
        other bytecode: a reader sees either the old reference or the new one,
        never a half-written pointer.
      * `LandmarkFrame` is a FROZEN dataclass of immutable values, so a reader
        holding the old reference keeps a fully-formed frame. Nothing mutates
        under it. This is the property that makes the missing lock safe, and it
        is why unfreezing those dataclasses would be a concurrency change rather
        than a style change.
      * Every counter has exactly ONE writer thread. `+=` is not atomic by the
        language definition - it is a load, an add and a store - so two writers
        could in principle lose counts. Honesty about the evidence: the M3
        review could not actually lose a count on CPython 3.11, even with six
        threads and a 1 microsecond switch interval, because the eval breaker is
        not polled between those three bytecodes. So the single-writer rule here
        is discipline against an implementation detail changing, not a fix for
        an observed race. It costs nothing to keep. `_n_published` is written
        only by the publishing thread and `_n_read` only by a reader.

    What a reader can observe: a frame one publish behind the very latest. That
    is not a race to fix, it is the design - the alternative is waiting, and
    waiting is the one thing the main thread must never do.
    """

    def __init__(self) -> None:
        # Starts as a real frame rather than None, so every reader - including
        # the first cook, before the camera has produced anything - gets the
        # full fixed shape and never has to branch on None. The fixed channel
        # list in DESIGN.md 6.2 depends on this being true from the first cook.
        self._frame: LandmarkFrame = blank_frame()
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

    def publish(self, frame: LandmarkFrame) -> None:
        """Make `frame` the latest. Called on the capture thread.

        This is the entire write path: one store, one increment. It is
        deliberately trivial, because it runs inside the camera callback and
        anything slower here shows up as dropped frames.
        """
        self._frame = frame
        self._n_published += 1

    def latest(self) -> LandmarkFrame:
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
        # and the age comes out NEGATIVE. Measured by the M3 review: 55 negative
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


@runtime_checkable
class HandSource(Protocol):
    """Where a Script CHOP gets its landmarks from.

    runtime_checkable so `isinstance(source, HandSource)` works and conformance
    can be ASSERTED rather than assumed. Without it, nothing in the repo bound a
    HandSource, so mypy never checked conformance either - the M3 review deleted
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


class InProcessSource:
    """A HandSource backed by the camera and Vision, in TouchDesigner's process.

    Thread: `start()`/`stop()` on the caller's thread; the engine publishes into
            the box from the capture queue. `latest()` is lock-free.
    Lifecycle: DESIGN.md 8. `stop()` must be wired into the TD project's unload
            path, and `start()` is idempotent so a reload cannot end up with two
            engines fighting over the camera.
    """

    def __init__(self, camera_name: str | None = None,
                 width_px: int | None = None,
                 height_px: int | None = None,
                 slot_mode: str = SLOT_MODE_CHIRALITY) -> None:
        self._slot_mode = slot_mode
        self.box = LatestFrameBox()
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
            from visionhands.engine import (
                DEFAULT_CAMERA_NAME,
                DEFAULT_HEIGHT_PX,
                DEFAULT_WIDTH_PX,
                HandEngine,
            )

            engine = HandEngine(
                on_frame=self.box.publish,
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
                # visionhands/slots.py.
                slot_mode=self._slot_mode,
            )
            # Marked before starting, so the age of a camera that never delivers
            # is measured from the attempt rather than from the first success.
            self.box.mark_started()
            try:
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

    def stop(self) -> None:
        """Stop the engine and release it. Idempotent, and safe after a failed start.

        ORDER: stop the engine, THEN drop the reference, and only in a finally.
        Nulling first - as the first version did - means that if `engine.stop()`
        raises (a pyobjc exception out of stopRunning, say) the live capture
        session becomes unreachable: `running` reports False, a retry of stop()
        is a no-op, and nothing can ever shut it down. Keeping the reference
        until the stop has actually returned makes a retry possible.
        """
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

    def latest(self) -> LandmarkFrame:
        return self.box.latest()

    def age_ms(self) -> float:
        return self.box.age_ms()

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
        # The first version kept a single Event and cleared it in start(). The
        # M3 review demonstrated what that costs: if a publishing thread had not
        # finished when stop() returned, a later start() cleared the shared Event
        # and RESURRECTED the orphan, which then published alongside the new
        # thread - two writers into one LatestFrameBox, breaking the
        # single-writer invariant this module's correctness rests on.
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
                                  name="visionhands-fake", daemon=True)
        # Started BEFORE being recorded: if Thread.start() raises - thread
        # exhaustion - the first version left _thread set, which made running
        # report True, start() a permanent no-op, and stop() raise
        # "cannot join thread before it is started" on the teardown path.
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
                about to assert on - and the first version promised exactly that
                in its docstring while never checking the join result, so a slow
                frame_factory made it a lie (MEASURED, M3 review: stop()
                returned with the publisher still running and `running`
                reporting False). A test double that lies about its own state is
                worse than no test double.
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
        thread is still writing - the exact lie the M3 review caught.
        """
        thread = self._thread
        return thread is not None and thread.is_alive()
