"""The lock-free handoff, tested with real threads and no camera.

The claim under test is the one TouchDesigner actually depends on: a reader can
take the latest frame without ever waiting for the writer (DESIGN.md 4.1, 4.3).
A claim like that verified only against a live camera is a claim verified on
whatever the hardware happened to do that afternoon, so everything here runs
against `FakeSource` - a genuine background thread publishing genuine frames,
deterministically enough to run anywhere.

Three properties, in order of how expensive they are to get wrong:

  1. NO TORN READS. A reader must never see a half-updated frame. Enforced by
     `LandmarkFrame` being frozen and the update being a single reference
     rebind; checked here by embedding a checksum in every frame and verifying
     it on every read, under contention.
  2. THE READER NEVER BLOCKS. Checked by timing reads while a writer hammers.
  3. NO COUNTS ARE LOST. Each counter has exactly one writer thread, because
     `+=` is not atomic; checked by publishing a known number of frames.
"""

from __future__ import annotations

import dataclasses
import dis
import statistics
import threading
import time

import pytest

from visionhands.source import (
    SEQ_NEVER_PUBLISHED,
    FakeSource,
    HandSource,
    InProcessSource,
    LatestFrameBox,
)
from visionhands.types import (
    MAX_HANDS,
    N_JOINTS,
    Confidence,
    Hand,
    Joint,
    LandmarkFrame,
    NormX,
    NormY,
    blank_frame,
)


def _checksummed_frame(seq: int) -> LandmarkFrame:
    """A frame whose every field is derived from `seq`.

    That is the whole trick: if a reader ever observes a frame whose joints
    disagree with its seq, the frame was assembled from two different writes and
    the no-torn-reads claim is false. With immutable frames it cannot happen -
    which is exactly why it is worth asserting rather than assuming, because the
    day someone unfreezes `Hand` for convenience, this test is what notices.
    """
    value = (seq % 1000) / 1000.0
    joints = tuple(
        Joint(NormX(value), NormY(value), Confidence(value)) for _ in range(N_JOINTS)
    )
    hand = Hand(chirality=1, confidence=Confidence(value),
                conf_median=Confidence(value), joints=joints, found=True)
    return LandmarkFrame(seq=seq, captured_at=time.monotonic(), width=1280,
                         height=720, hands=tuple(hand for _ in range(MAX_HANDS)))


def _is_internally_consistent(frame: LandmarkFrame) -> bool:
    expected = (frame.seq % 1000) / 1000.0
    return all(
        joint.x == expected and joint.y == expected and joint.conf == expected
        for hand in frame.hands for joint in hand.joints
    )


# ---------------------------------------------------------------------------
# The box itself
# ---------------------------------------------------------------------------
def test_box_starts_with_a_full_blank_frame_not_none() -> None:
    """The first cook happens before the camera has produced anything.

    If `latest()` could return None, every consumer would need a branch for it,
    and the fixed channel list in DESIGN.md 6.2 would stop being unconditional.
    """
    box = LatestFrameBox()
    frame = box.latest()
    assert frame.seq == SEQ_NEVER_PUBLISHED
    assert len(frame.hands) == MAX_HANDS
    assert all(len(hand.joints) == N_JOINTS for hand in frame.hands)
    assert not any(hand.found for hand in frame.hands)


def test_publish_then_latest_returns_the_new_frame() -> None:
    box = LatestFrameBox()
    box.publish(_checksummed_frame(7))
    assert box.latest().seq == 7
    box.publish(_checksummed_frame(8))
    assert box.latest().seq == 8


def test_latest_does_not_consume() -> None:
    """Reading twice returns the same frame. A CHOP may cook more often than the
    camera delivers, and must see the current frame each time rather than a gap."""
    box = LatestFrameBox()
    box.publish(_checksummed_frame(3))
    assert box.latest() is box.latest()


def test_intermediate_frames_are_dropped_not_queued() -> None:
    """The design decision, asserted: most recent wins, the rest are discarded.

    A queue here would grow without bound whenever TD cooks slower than the
    camera delivers, and the overlay would drift steadily further behind the
    hand (DESIGN.md 4.3).
    """
    box = LatestFrameBox()
    for seq in range(1, 101):
        box.publish(_checksummed_frame(seq))
    assert box.latest().seq == 100
    assert box.n_published == 100
    assert box.n_read == 1          # 99 frames were never seen, by design


def test_age_of_a_box_that_never_started_is_zero() -> None:
    """Nothing was expected yet, so nothing is late."""
    assert LatestFrameBox().age_ms() == 0.0


def test_age_rises_when_a_started_producer_delivers_nothing() -> None:
    """The failure DESIGN.md 6.2 wants visible: a camera that never delivers.

    If this returned 0 until the first frame, a dead engine would look perfectly
    healthy forever - which is the opposite of what a staleness channel is for.
    """
    box = LatestFrameBox()
    box.mark_started()
    time.sleep(0.02)
    assert box.age_ms() > 10.0


def test_age_is_measured_from_capture_not_from_publish() -> None:
    """age_ms answers "how old is this data", not "how long since it arrived"."""
    box = LatestFrameBox()
    box.mark_started()
    captured = time.monotonic() - 0.5            # half a second old already
    box.publish(LandmarkFrame(seq=1, captured_at=captured, width=1280,
                              height=720, hands=blank_frame().hands))
    assert 400.0 < box.age_ms() < 600.0


# ---------------------------------------------------------------------------
# Under contention - the part that matters
# ---------------------------------------------------------------------------
def test_no_torn_reads_under_contention() -> None:
    """A writer thread at full speed against a reader thread at full speed.

    Every frame the reader sees must be internally consistent. This is the
    property that makes the absent lock correct, and it holds because
    LandmarkFrame is frozen: a reader either has the old reference or the new
    one, and both are complete.
    """
    box = LatestFrameBox()
    stop = threading.Event()
    published = {"n": 0}

    def writer() -> None:
        seq = 0
        while not stop.is_set():
            seq += 1
            box.publish(_checksummed_frame(seq))
        published["n"] = seq

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        torn = 0
        reads = 0
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            frame = box.latest()
            reads += 1
            if not _is_internally_consistent(frame):
                torn += 1
    finally:
        stop.set()
        thread.join(timeout=2.0)

    assert reads > 1000, "not enough reads to be meaningful: %d" % reads
    assert published["n"] > 1000, "writer barely ran: %d" % published["n"]
    assert torn == 0, "%d torn reads out of %d" % (torn, reads)


def test_read_latency_under_a_realistic_producer() -> None:
    """What TD's main thread actually experiences (DESIGN.md 4.1).

    The producer here publishes at ~60 Hz and yields in between, which is how
    the real capture thread behaves: it spends its time blocked inside
    AVFoundation and Vision, where pyobjc has RELEASED the GIL (MEASURED,
    DESIGN.md 2.2), and holds it only for the brief Python work around each
    frame.

    An earlier version of this test used a producer that published in a tight
    loop with no yield at all, and measured a worst-case read of 6.3 ms against
    a median of 0.0001 ms. That was not a lock and not a defect in the box - it
    was the GIL switch interval, 5 ms by default, with a thread that never gave
    it up. Worth knowing (it is recorded in DESIGN.md 2.7), but it measures
    CPython's scheduler rather than this module, and a test that fails for a
    reason it is not testing is a test people learn to ignore.

    Lock-freedom itself is proved structurally, in
    test_latest_calls_nothing_at_all - a timing test cannot tell an uncontended
    lock from no lock.
    """
    box = LatestFrameBox()
    stop = threading.Event()
    published = {"n": 0}

    def writer() -> None:
        seq = 0
        while not stop.is_set():
            seq += 1
            box.publish(_checksummed_frame(seq))
            # Yields, exactly as the capture thread does while it sits inside
            # AVFoundation and Vision with the GIL released.
            stop.wait(1.0 / 60.0)
        published["n"] = seq

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        durations_ms = []
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            started = time.perf_counter()
            box.latest()
            durations_ms.append((time.perf_counter() - started) * 1e3)
            time.sleep(1.0 / 240.0)     # cook far faster than the producer
    finally:
        stop.set()
        thread.join(timeout=2.0)

    # Guards against the test passing with no contention at all: the M3 review
    # killed the writer immediately and watched an earlier version go green.
    assert len(durations_ms) > 50, "not enough reads: %d" % len(durations_ms)
    assert published["n"] > 10, "producer barely ran (%d)" % published["n"]

    # 1 ms is already enormous for an attribute read - measured worst case is
    # ~0.03 ms. The bound is loose enough to survive a busy machine and tight
    # enough that a lock held across a Vision inference (~3.4 ms) fails it.
    worst_ms = max(durations_ms)
    assert worst_ms < 1.0, ("worst read took %.3f ms (median %.4f) - something "
                            "is blocking the reader"
                            % (worst_ms, statistics.median(durations_ms)))


# ---------------------------------------------------------------------------
# FakeSource - the substitute the CHOP layer is developed against
# ---------------------------------------------------------------------------
def test_fake_source_publishes_and_stops() -> None:
    source = FakeSource(_checksummed_frame, interval_s=0.002)
    source.start()
    try:
        deadline = time.monotonic() + 1.0
        while source.box.n_published < 10 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert source.box.n_published >= 10
        assert source.running
        assert source.latest().seq > 0
        assert source.age_ms() < 500.0
    finally:
        source.stop()

    assert not source.running
    # Nothing is still publishing after stop() returns, so a test may assert on
    # the box safely. stop() joins the thread for exactly this reason.
    settled = source.box.n_published
    time.sleep(0.05)
    assert source.box.n_published == settled


def test_fake_source_start_and_stop_are_idempotent() -> None:
    """The property DESIGN.md 8 requires of every source: a TD reload calls these
    in whatever order it likes, possibly twice."""
    source = FakeSource(_checksummed_frame, interval_s=0.005)
    source.stop()                        # before ever starting
    source.start()
    source.start()                       # no second thread
    time.sleep(0.05)
    source.stop()
    source.stop()
    assert not source.running


def test_fake_source_stop_is_prompt() -> None:
    """Stopping must not wait out the publishing interval.

    The publisher waits on an Event rather than sleeping, so a stop is immediate.
    A sleep-based loop would take up to a full interval to notice - which at a
    slow interval is a visible hang on TD's unload path.
    """
    source = FakeSource(_checksummed_frame, interval_s=2.0)
    source.start()
    time.sleep(0.05)
    started = time.perf_counter()
    source.stop()
    assert (time.perf_counter() - started) < 0.5


def test_a_source_satisfies_the_protocol_the_chop_will_use() -> None:
    """Milestone 4 writes the CHOP against HandSource, not against a camera.

    Checked structurally rather than with isinstance: HandSource is a plain
    Protocol, and what matters is that the methods exist with the right shapes
    before td/ is written against them.
    """
    source = FakeSource(_checksummed_frame)
    for name in ("start", "stop", "latest", "age_ms"):
        assert callable(getattr(source, name)), name
    assert isinstance(source.running, bool)
    assert isinstance(source.latest(), LandmarkFrame)
    assert isinstance(source.age_ms(), float)


# ---------------------------------------------------------------------------
# The claims themselves, made checkable.
#
# The M3 review mutated the code and found that this milestone's central claims
# were untested: unfreezing every dataclass, and adding a real lock around both
# publish() and latest(), each left all 60 tests green. Timing tests cannot
# distinguish an uncontended lock from no lock - so these check the two
# structural properties directly instead.
# ---------------------------------------------------------------------------
def test_the_frame_types_are_actually_frozen() -> None:
    """Immutability is not a style choice here, it is the reason no lock is needed.

    A reader holding the previous frame reference must keep a complete, unchanging
    object. If `Hand` were unfrozen so someone could "just update the confidence
    in place", a reader could observe a frame mid-mutation and the missing lock
    would become a real race. Nothing in the repo mutates a frame today, so no
    behavioural test can catch that change - only this one.
    """
    frame = _checksummed_frame(1)
    for obj, field, value in (
        (frame, "seq", 2),
        (frame.hands[0], "found", False),
        (frame.hands[0].joints[0], "x", NormX(0.9)),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, field, value)


def test_latest_calls_nothing_at_all() -> None:
    """Lock-freedom, checked structurally rather than by timing.

    A timing test cannot tell an uncontended lock from no lock - the M3 review
    added a real threading.Lock around publish() and latest() and every test
    stayed green. So this reads the bytecode: `latest()` must contain no CALL of
    any kind. A lock acquisition is a call. So is a property, a descriptor, a
    copy, a comparison, or any other way of accidentally introducing something
    that can block TD's main thread (DESIGN.md 4.1).

    If this fails because latest() legitimately needs to call something, that is
    a design decision to make deliberately, not a test to relax casually.
    """
    instructions = list(dis.get_instructions(LatestFrameBox.latest))
    calls = [i.opname for i in instructions if "CALL" in i.opname]
    assert not calls, "latest() calls something: %s" % calls

    # And the box holds no synchronisation primitive at all.
    box = LatestFrameBox()
    lock_types = (type(threading.Lock()), type(threading.RLock()), threading.Condition,
                  threading.Semaphore, threading.Event)
    held = [name for name, value in vars(box).items() if isinstance(value, lock_types)]
    assert not held, "the box holds a synchronisation primitive: %s" % held


def test_both_sources_satisfy_the_protocol_at_runtime() -> None:
    """Conformance asserted, not assumed.

    The M3 review deleted `FakeSource.errors` - breaking the interface the CHOP
    will be written against - and ruff, mypy --strict and all 60 tests stayed
    green, because nothing in the repo ever bound a HandSource. isinstance
    against a runtime_checkable Protocol checks that every member EXISTS; the
    annotated bindings below are what make mypy check the signatures.
    """
    fake: HandSource = FakeSource(_checksummed_frame)
    in_process: HandSource = InProcessSource()
    for source in (fake, in_process):
        assert isinstance(source, HandSource), type(source).__name__
        for member in ("start", "stop", "latest", "age_ms", "running", "errors"):
            assert hasattr(source, member), "%s lacks %s" % (type(source).__name__, member)


# ---------------------------------------------------------------------------
# The lifecycle defects the M3 review confirmed.
# ---------------------------------------------------------------------------
def test_stop_refuses_to_lie_about_a_publisher_that_will_not_stop() -> None:
    """A test double that misreports its own state is worse than none.

    The old stop() called join(timeout=2) and ignored the result, so a slow
    frame_factory left it returning cleanly with the thread still writing into
    the box - while `running` reported False.
    """
    release = threading.Event()

    def blocking_factory(seq: int) -> LandmarkFrame:
        release.wait(10.0)              # refuses to return until released
        return _checksummed_frame(seq)

    source = FakeSource(blocking_factory, interval_s=0.001)
    source.JOIN_TIMEOUT_S = 0.2         # keep the test quick
    source.start()
    time.sleep(0.05)
    try:
        with pytest.raises(RuntimeError, match="did not stop"):
            source.stop()
        assert source.running, "a publisher that is still alive must report running"
    finally:
        release.set()
        time.sleep(0.05)


def test_a_stalled_publisher_cannot_be_resurrected_by_a_later_start() -> None:
    """The single-writer invariant, defended.

    With one shared Event, start() cleared it and brought an orphaned publisher
    back to life alongside the new one - two threads writing one box, which is
    exactly what LatestFrameBox's correctness argument forbids. Each run now owns
    its own Event, so an orphan stays stopped.
    """
    release = threading.Event()
    threads_that_ran = set()

    def blocking_factory(seq: int) -> LandmarkFrame:
        threads_that_ran.add(threading.get_ident())
        release.wait(10.0)
        return _checksummed_frame(seq)

    source = FakeSource(blocking_factory, interval_s=0.001)
    source.JOIN_TIMEOUT_S = 0.2
    source.start()
    time.sleep(0.05)
    try:
        with pytest.raises(RuntimeError):
            source.stop()
        # start() must decline while the first publisher is still alive.
        source.start()
        time.sleep(0.05)
        assert len(threads_that_ran) == 1, (
            "a second publisher started while the first was still alive: %d"
            % len(threads_that_ran))
    finally:
        release.set()
        time.sleep(0.1)


def test_errors_survive_being_stopped() -> None:
    """Why a failed start must leave its reason behind.

    `errors` used to return [] the moment the engine was released, so a CHOP
    could report "stale" but never "stale because there is no such camera".
    """
    source = InProcessSource(camera_name="no-such-camera-xyz")
    with pytest.raises(Exception, match="no capture device"):
        source.start()
    assert any("no-such-camera-xyz" in e for e in source.errors), source.errors
    source.stop()
    assert source.errors, "the reason vanished when the source was stopped"


def test_age_is_never_negative() -> None:
    """A negative age would sail through every staleness gate.

    Two defences, both needed. `age_ms` reads the frame before the clock, which
    narrows the window in which a newly published frame can appear to be from
    the future; and the result is clamped, because "narrow" is not "closed" and
    a probabilistic test cannot prove otherwise - the M3 review needed 7.5M
    reads to observe 55 negatives.

    The deterministic half of this test uses an explicitly-passed clock, which
    is the only way to hit the case reliably.
    """
    # Deterministic: a clock reading from before the frame was captured.
    box = LatestFrameBox()
    box.mark_started()
    captured = time.monotonic()
    box.publish(LandmarkFrame(seq=1, captured_at=captured, width=1280,
                              height=720, hands=blank_frame().hands))
    assert box.age_ms(now=captured - 0.5) == 0.0
    assert box.age_ms(now=captured + 0.5) > 400.0

    # And under real contention, with a producer stamping the future.
    box = LatestFrameBox()
    box.mark_started()
    stop = threading.Event()

    def writer() -> None:
        seq = 0
        while not stop.is_set():
            seq += 1
            # Stamped in the FUTURE on purpose: the harshest version of the
            # interleaving, and one a monotonic clock cannot actually produce.
            box.publish(LandmarkFrame(seq=seq, captured_at=time.monotonic() + 0.001,
                                      width=1280, height=720, hands=blank_frame().hands))

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        worst = min(box.age_ms() for _ in range(20000))
    finally:
        stop.set()
        thread.join(timeout=2.0)
    assert worst >= 0.0, "age_ms went negative: %.4f ms" % worst
