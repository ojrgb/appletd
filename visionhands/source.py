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
from typing import TYPE_CHECKING, Protocol

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
      * Every counter has exactly ONE writer thread. `+=` is NOT atomic - it is
        a load, an add and a store - so two writers would silently lose counts.
        `_n_published` is only ever written by the publishing thread and
        `_n_read` only by a reader; a second reader thread would need this
        revisited, and there is deliberately no code that creates one.

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
        now = time.monotonic() if now is None else now
        frame = self._frame
        if frame.seq != SEQ_NEVER_PUBLISHED:
            return (now - frame.captured_at) * 1e3
        if self._started_at is None:
            return 0.0
        return (now - self._started_at) * 1e3

    @property
    def n_published(self) -> int:
        return self._n_published

    @property
    def n_read(self) -> int:
        return self._n_read


class HandSource(Protocol):
    """Where a Script CHOP gets its landmarks from.

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
                 height_px: int | None = None) -> None:
        self.box = LatestFrameBox()
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
        )
        # Marked before starting, so the age of a camera that never delivers is
        # measured from the attempt rather than from the first success.
        self.box.mark_started()
        try:
            engine.start()
        except Exception:
            # Not a blind swallow: the exception propagates. The engine is
            # dropped so that a failed start leaves this source in the same
            # state it began in, which is what makes a retry safe rather than a
            # second half-live engine.
            self._engine = None
            raise
        self._engine = engine

    def stop(self) -> None:
        """Stop the engine and release it. Idempotent, and safe after a failed start."""
        engine = self._engine
        self._engine = None
        if engine is not None:
            engine.stop()

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
        """Faults recorded by the engine, readable from the main thread.

        Returns a COPY: the underlying list is appended to from the capture
        thread, and handing out the live object would let a caller iterate it
        while it grows.
        """
        engine = self._engine
        if engine is None:
            return []
        return list(engine.errors)


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

    def __init__(self, frame_factory: Callable[[int], LandmarkFrame],
                 interval_s: float = 1.0 / 30.0) -> None:
        """`frame_factory(seq) -> LandmarkFrame`, called on the publishing thread."""
        self.box = LatestFrameBox()
        self._frame_factory = frame_factory
        self._interval_s = interval_s
        self._thread: threading.Thread | None = None
        # An Event rather than a bool: the publisher waits on it, so stopping is
        # immediate instead of taking up to one interval. Waiting on an Event
        # also releases the GIL, unlike a sleep-and-poll loop, which is the
        # pattern DESIGN.md 3 measured starving the capture thread.
        self._stop = threading.Event()
        self.errors: list[str] = []

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self.box.mark_started()
        self._thread = threading.Thread(target=self._run, name="visionhands-fake",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        seq = 0
        while not self._stop.is_set():
            seq += 1
            self.box.publish(self._frame_factory(seq))
            # wait() returns True the moment stop is set, so a stop never has to
            # wait out the remaining interval.
            self._stop.wait(self._interval_s)

    def stop(self) -> None:
        """Idempotent, and joins - so a test that stops the source knows nothing
        is still publishing into the box it is about to assert on."""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)

    def latest(self) -> LandmarkFrame:
        return self.box.latest()

    def age_ms(self) -> float:
        return self.box.age_ms()

    @property
    def running(self) -> bool:
        return self._thread is not None
