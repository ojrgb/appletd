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

import statistics
import threading
import time

from visionhands.source import SEQ_NEVER_PUBLISHED, FakeSource, LatestFrameBox
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


def test_the_reader_never_blocks() -> None:
    """The contract TouchDesigner depends on (DESIGN.md 4.1).

    A Script CHOP that can wait can freeze TD. There is no lock between reader
    and writer, so the worst a read can cost is the GIL handoff - measured here
    as a worst-case rather than an average, because an average would hide
    exactly the stall this is looking for.
    """
    box = LatestFrameBox()
    stop = threading.Event()

    def writer() -> None:
        seq = 0
        while not stop.is_set():
            seq += 1
            box.publish(_checksummed_frame(seq))

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        durations_ms = []
        for _ in range(2000):
            started = time.perf_counter()
            box.latest()
            durations_ms.append((time.perf_counter() - started) * 1e3)
    finally:
        stop.set()
        thread.join(timeout=2.0)

    # 5 ms is enormous for an attribute read - the bound is loose on purpose, so
    # this fails only for a genuine stall and not for a busy CI machine. A lock
    # held across a Vision inference would show up here as ~3.4 ms.
    worst_ms = max(durations_ms)
    assert worst_ms < 5.0, ("worst read took %.3f ms (median %.4f) - something "
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
