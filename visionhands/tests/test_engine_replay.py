"""The engine's inference path, replayed over the fixture clip. Deterministic.

Why replay and not a camera. A live camera cannot produce a comparable number:
identical 720p configs gave medians from 2.12 to 16.30 ms in the spike, an 8x
spread, because Vision's cost tracks what is in shot (MEASURED, DESIGN.md 3).
Replaying one recorded clip holds the content constant so a code change is the
only variable. Every performance claim in this repo comes from here.

Why this exercises the SAME code the camera uses. `HandDetector` differs between
the two paths only in which `performRequests_on*_error_` selector it calls;
everything downstream - the observation-to-Hand conversion, the joint ordering,
the slot padding - is shared. A replay harness with its own conversion logic
would be testing itself.

SKIPPED, not failed, when the fixture or pyobjc is missing. The fixture is
gitignored because it shows the person who recorded it (DESIGN.md 2.6), so a
fresh clone genuinely does not have it and these tests cannot run there. That is
a real limitation of this repo, recorded rather than papered over: anyone
rebuilding it has to record their own clip, hands only.

Ref: DESIGN.md 2.6 (the fixture's own numbers), 3, 6.1.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Any

import pytest

from visionhands.types import (
    CHIRALITY_LEFT,
    CHIRALITY_RIGHT,
    CHIRALITY_UNKNOWN,
    MAX_HANDS,
    N_JOINTS,
)

# What the `replay` fixture hands each test. Not a TypedDict: two of the values
# are pyobjc-adjacent and one exists purely to keep numpy arrays alive, so a
# precise type would be more ceremony than information.
Replay = dict[str, Any]

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "hand_clip.mp4"

# MEASURED over this exact clip (DESIGN.md 2.6). The bounds are deliberately
# loose: these tests assert that the engine still behaves as measured, not that
# the machine performs identically run to run. A tight bound here would fail on
# a busy laptop and teach everyone to ignore it.
EXPECTED_FRAMES = 284
EXPECTED_DIMS_PX = (1280, 720)
MIN_HAND_PRESENCE = 0.90        # measured 96%
MAX_MEDIAN_MS = 8.0            # measured 3.41 ms; 2x headroom for a loaded machine


@pytest.fixture(scope="module")
def replay() -> Replay:
    """Decode the fixture once and run the engine's detector over every frame.

    Module-scoped because decoding 284 frames of 720p and running Vision over
    them takes about a second, and every test in this file wants the same
    result. Vision's first call also carries model load (~68 ms to 4.3 s,
    DESIGN.md 2.4/2.5), which is excluded below rather than measured.

    cv2 and numpy are imported HERE, inside the function, not at module scope.
    STANDARDS.md 2 forbids a module-scope cv2 import anywhere in the package:
    TouchDesigner's bundled Python does not have it, and an import that only
    fails at cook time is the worst place to find out.
    """
    pytest.importorskip("Vision", reason="pyobjc not installed")
    cv2 = pytest.importorskip("cv2", reason="cv2 is dev-only")
    np = pytest.importorskip("numpy")
    if not FIXTURE.is_file():
        pytest.skip("no fixture at %s - record one with tools/record_fixture.py"
                    % FIXTURE)

    from visionhands.engine import HandDetector, pixel_buffer_from_bgra

    capture = cv2.VideoCapture(str(FIXTURE))
    if not capture.isOpened():
        pytest.skip("cv2 could not open %s" % FIXTURE)
    bgra_frames = []
    while True:
        ok, bgr = capture.read()
        if not ok:
            break
        # Contiguous, and KEPT ALIVE for the whole test: the pixel buffer built
        # from each of these does not copy, so letting one be collected while
        # Vision reads it corrupts the frame (DESIGN.md 3).
        bgra_frames.append(np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)))
    capture.release()

    detector = HandDetector()
    frames = []
    timings_ms = []
    for i, bgra in enumerate(bgra_frames):
        pixel_buffer = pixel_buffer_from_bgra(bgra)
        started = time.perf_counter()
        frame = detector.detect_pixel_buffer(pixel_buffer, seq=i + 1,
                                             captured_at=time.monotonic())
        elapsed_ms = (time.perf_counter() - started) * 1e3
        frames.append(frame)
        if i:                       # frame 0 carries Vision's model load
            timings_ms.append(elapsed_ms)

    return {"frames": frames, "timings_ms": timings_ms,
            "detector": detector, "keepalive": bgra_frames}


def test_fixture_decodes_as_measured(replay: Replay) -> None:
    """The clip is the one DESIGN.md 2.6's numbers came from.

    If this fails, someone re-recorded the fixture and every figure in 2.6 needs
    re-measuring - which is exactly why the count is pinned rather than inferred.
    """
    assert len(replay["frames"]) == EXPECTED_FRAMES


def test_every_frame_has_the_full_fixed_shape(replay: Replay) -> None:
    """The contract that makes the 137-channel list unconditional.

    Every frame carries exactly MAX_HANDS hands and every hand exactly N_JOINTS
    joints, whether or not a hand was in shot. A CHOP whose channel count
    depended on what the camera saw would break every downstream reference the
    moment a hand left frame (DESIGN.md 6.2).
    """
    for frame in replay["frames"]:
        assert len(frame.hands) == MAX_HANDS
        for hand in frame.hands:
            assert len(hand.joints) == N_JOINTS


def test_dimensions_come_from_the_buffer_not_from_a_constant(replay: Replay) -> None:
    """width/height are what was actually delivered, never what was requested."""
    for frame in replay["frames"]:
        assert (frame.width, frame.height) == EXPECTED_DIMS_PX


def test_seq_is_monotonic_and_gapless(replay: Replay) -> None:
    """age_ms and staleness detection downstream depend on seq being trustworthy."""
    seqs = [f.seq for f in replay["frames"]]
    assert seqs == list(range(1, len(seqs) + 1))


def test_hand_presence_matches_the_measurement(replay: Replay) -> None:
    """96% measured; asserting >=90% so a slightly worse macOS release is a
    warning shot rather than a red suite."""
    with_hand = sum(1 for f in replay["frames"] if f.hands[0].found)
    presence = with_hand / len(replay["frames"])
    assert presence >= MIN_HAND_PRESENCE, "hand presence fell to %.1f%%" % (100 * presence)


def test_inference_cost_is_in_the_measured_range(replay: Replay) -> None:
    """3.41 ms measured (DESIGN.md 2.6). The bound is 8 ms: this test exists to
    catch a regression of the kind that would blow the 33 ms frame budget, not
    to police a busy laptop."""
    median_ms = statistics.median(replay["timings_ms"])
    assert median_ms < MAX_MEDIAN_MS, "median inference rose to %.2f ms" % median_ms


def test_coordinates_stay_normalised_and_unflipped(replay: Replay) -> None:
    """No conversion may happen between Vision and the frame contract.

    Values stay in roughly 0..1 - Vision can overshoot slightly when a hand is
    part way out of frame, and that overshoot is real information, so the bound
    is generous rather than exact (coords.py).

    The unflipped part is checked by wrist-versus-fingertip ordering in
    test_coords.py; here the point is only that nobody has scaled these into
    pixels or clamped them on the way through.
    """
    for frame in replay["frames"]:
        for hand in frame.hands:
            if not hand.found:
                continue
            for joint in hand.joints:
                assert -0.5 <= joint.x <= 1.5
                assert -0.5 <= joint.y <= 1.5
                assert 0.0 <= joint.conf <= 1.0


def test_the_hand_actually_moves_through_the_frame(replay: Replay) -> None:
    """Guards the FIXTURE, not the code.

    A clip of a motionless hand would pass every other test here while
    exercising nothing. DESIGN.md 2.6 measured the wrist covering 0.930 of x and
    0.704 of y; if a future fixture is a static take, this fails and says so.
    """
    xs = [f.hands[0].joints[0].x for f in replay["frames"] if f.hands[0].found]
    ys = [f.hands[0].joints[0].y for f in replay["frames"] if f.hands[0].found]
    assert max(xs) - min(xs) > 0.5, "wrist barely moved in x - is this a static clip?"
    assert max(ys) - min(ys) > 0.4, "wrist barely moved in y - is this a static clip?"


def test_score_is_the_constant_it_was_measured_to_be(replay: Replay) -> None:
    """Pins the finding that changed the channel contract.

    Vision's observation confidence was 1.0 on all 336 observations (DESIGN.md
    2.6), which is why h<i>_score is published as a mirror and h<i>_conf_median
    exists at all. If Apple ever starts varying it, this test fails - and that
    failure is the notification we want, not a nuisance.
    """
    scores = {hand.confidence for f in replay["frames"] for hand in f.hands if hand.found}
    assert scores == {1.0}, "observation confidence is no longer constant: %s" % scores


def test_conf_median_is_the_channel_with_actual_signal(replay: Replay) -> None:
    """The counterpart to the test above: our own aggregate does vary, and
    varies across the range that makes it useful for gating."""
    medians = [hand.conf_median for f in replay["frames"] for hand in f.hands if hand.found]
    assert len(set(medians)) > 50, "conf_median barely varies - is it computed?"
    assert min(medians) < 0.6
    assert max(medians) > 0.8


def test_blank_slots_are_the_shared_blank_hand(replay: Replay) -> None:
    """The no-hand path allocates nothing per frame.

    BLANK_HAND is frozen and shared, so an absent hand costs one reference and
    not 21 Joint objects - which matters because the worst case (no hands, every
    frame) is also the case that runs longest in an installation.
    """
    from visionhands.types import BLANK_HAND
    blanks = [hand for f in replay["frames"] for hand in f.hands if not hand.found]
    assert blanks, "no empty slots in the fixture - expected dropouts and single-hand frames"
    for hand in blanks:
        assert hand is BLANK_HAND


def test_chirality_is_one_of_the_three_constants(replay: Replay) -> None:
    """Vision's encoding happens to match ours; this proves the mapping holds.

    UNVERIFIED and deliberately not asserted: whether the fixture is
    horizontally mirrored, which would invert left and right without changing
    anything this test checks (DESIGN.md 2.6). Deferred to the two-hand fixture.
    """
    seen = {hand.chirality for f in replay["frames"] for hand in f.hands if hand.found}
    assert seen <= {CHIRALITY_LEFT, CHIRALITY_UNKNOWN, CHIRALITY_RIGHT}
    assert seen, "no hands at all in the fixture"
