"""Person segmentation over the fixture clip, and end to end through the transport.

WHY REPLAY AND NOT A CAMERA. Same reason as test_engine_replay.py: a live camera
cannot produce a comparable number, and the standing constraint on this project is
to ask before using it. Every figure here comes from `fixtures/hand_clip.mp4`.

WHAT THE FIXTURE ACTUALLY CONTAINS, because it matters for this file specifically:
a torso, an arm and a hand in a room, framed below the neck. That is a PERSON, which
is the point - a person segmentation request needs one - but it also means the clip
is gitignored and stays that way, and no mask derived from it is committed either.

THE BOUNDS ARE LOOSE ON PURPOSE. These tests assert that segmentation still behaves
as measured, not that this machine performs identically run to run. A tight bound
here fails on a busy laptop and teaches everyone to ignore the suite.

Ref: visionhands/segmentation.py, visionhands/maskbuf.py,
     docs/BUILD_PLAN.md step 9, DESIGN.md 2.18.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Any

import pytest

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "hand_clip.mp4"

# MEASURED 2026-08-22 over this clip, 120 frames per level, frame 0 excluded because
# it carries Vision's model load. Medians: fast 2.21 ms, balanced 8.54, accurate
# 30.73. The ceilings below are roughly 2x, for a loaded machine.
MEASURED_MS = {"fast": 2.21, "balanced": 8.54, "accurate": 30.73}
MAX_MS = {"fast": 6.0, "balanced": 20.0, "accurate": 70.0}

# MEASURED: the mask size per quality level for a LANDSCAPE input. Pinned exactly,
# because these are what `maskbuf` has to be sized for and a silent change would
# show up as a refused write rather than as anything legible.
MASK_SIZE = {"fast": (256, 192), "balanced": (512, 384), "accurate": (2016, 1512)}

# The clip is one person filling much of the frame. Measured coverage sat at 0.52
# for balanced; the band is wide because it is a sanity check on "is this a mask at
# all", not a measurement of the clip.
MIN_COVERAGE = 0.10
MAX_COVERAGE = 0.95

Replay = dict[str, Any]


def _frames(limit: int) -> list[Any]:
    """Decode the fixture to contiguous BGRA. cv2 and numpy imported HERE.

    STANDARDS.md 2 forbids a module-scope cv2 import anywhere in the package:
    TouchDesigner's bundled Python does not have it, and an import that only fails
    at cook time is the worst place to find out.
    """
    cv2 = pytest.importorskip("cv2", reason="cv2 is dev-only")
    numpy = pytest.importorskip("numpy")
    capture = cv2.VideoCapture(str(FIXTURE))
    if not capture.isOpened():
        pytest.skip("cv2 could not open %s" % FIXTURE)
    out: list[Any] = []
    while len(out) < limit:
        ok, bgr = capture.read()
        if not ok:
            break
        # Contiguous, and KEPT ALIVE by the caller: the pixel buffer built from each
        # of these does not copy, so letting one be collected while Vision reads it
        # corrupts the frame (DESIGN.md 3).
        out.append(numpy.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)))
    capture.release()
    return out


@pytest.fixture(scope="module")
def replay() -> Replay:
    """Run every quality level over the same frames, once for the whole module.

    Module-scoped because `accurate` is 30 ms a frame and three levels over 60
    frames is about four seconds. Every test here wants the same result.
    """
    pytest.importorskip("Vision", reason="pyobjc not installed")
    if not FIXTURE.is_file():
        pytest.skip("no fixture at %s - record one with tools/record_fixture.py"
                    % FIXTURE)

    from visionhands.engine import pixel_buffer_from_bgra
    from visionhands.segmentation import SegmentationDetector

    bgra_frames = _frames(60)
    if len(bgra_frames) < 10:
        pytest.skip("fixture decoded only %d frames" % len(bgra_frames))

    runs: dict[str, dict[str, Any]] = {}
    for quality in MEASURED_MS:
        detector = SegmentationDetector(quality)
        masks, timings = [], []
        for index, bgra in enumerate(bgra_frames):
            mask = detector.detect_pixel_buffer(
                pixel_buffer_from_bgra(bgra), seq=index + 1,
                captured_at=time.monotonic())
            if mask is None:
                continue
            masks.append(mask)
            if index:                   # frame 0 carries Vision's model load
                timings.append(mask.inference_ms)
        runs[quality] = {"masks": masks, "timings": timings,
                         "detector": detector}
    height, width = bgra_frames[0].shape[:2]
    return {"runs": runs, "source": (width, height), "keepalive": bgra_frames}


# -- the API surface -----------------------------------------------------


def test_the_api_surface_is_present() -> None:
    pytest.importorskip("Vision", reason="pyobjc not installed")
    from visionhands.segmentation import verify_segmentation_support
    verify_segmentation_support()            # raises if it is not


def test_init_is_unavailable_and_the_real_initialiser_is_the_handler_one() -> None:
    """PINNED because it cost a probe. Every other Vision request in this project is
    `alloc().init()`; on this class `init` and `new` are both NS_UNAVAILABLE. If a
    future pyobjc makes `init` work, this test failing is the notice."""
    Vision = pytest.importorskip("Vision")
    cls = Vision.VNGeneratePersonSegmentationRequest
    with pytest.raises(TypeError, match="NS_UNAVAILABLE"):
        cls.alloc().init()
    request = cls.alloc().initWithCompletionHandler_(None)
    assert request is not None


def test_only_revision_one_exists() -> None:
    """Recorded rather than pinned in the code, so a future macOS adding a revision
    shows up here instead of silently changing behaviour."""
    Vision = pytest.importorskip("Vision")
    revisions = list(Vision.VNGeneratePersonSegmentationRequest.supportedRevisions())
    assert revisions == [1]


def test_the_output_format_is_one_component_8() -> None:
    """The claim `maskbuf` depends on - one byte per pixel. Readable off the request,
    so it is checked rather than commented."""
    pytest.importorskip("Vision")
    from visionhands.segmentation import ONE_COMPONENT_8, SegmentationDetector
    assert SegmentationDetector("fast").output_format == ONE_COMPONENT_8


def test_a_bad_quality_name_is_refused_at_construction() -> None:
    pytest.importorskip("Vision")
    from visionhands.engine import EngineError
    from visionhands.segmentation import SegmentationDetector
    with pytest.raises(EngineError, match="quality must be one of"):
        SegmentationDetector("hyperspeed")


def test_visions_own_default_is_accurate_and_ours_is_not() -> None:
    """Worth pinning: the expensive level is the DEFAULT, which is the most likely
    explanation for the user's datum of a C++ plugin pinning the frame rate. This
    module deliberately defaults to `balanced` instead."""
    Vision = pytest.importorskip("Vision")
    from visionhands.segmentation import VISION_DEFAULT_QUALITY, SegmentationDetector
    fresh = Vision.VNGeneratePersonSegmentationRequest.alloc()\
        .initWithCompletionHandler_(None)
    assert VISION_DEFAULT_QUALITY == "accurate"
    assert int(fresh.qualityLevel()) == int(
        Vision.VNGeneratePersonSegmentationRequestQualityLevelAccurate)
    assert SegmentationDetector().quality == "balanced"


# -- what the fixture produces -------------------------------------------


def test_every_frame_produced_a_mask(replay: Replay) -> None:
    """A person is in frame for the whole clip, so no result would be a regression -
    and `failures` is the counter that distinguishes "found nobody" from "never ran"."""
    for quality, run in replay["runs"].items():
        assert run["detector"].failures == 0, quality
        assert len(run["masks"]) >= 10, quality


@pytest.mark.parametrize("quality", sorted(MASK_SIZE))
def test_the_mask_size_is_fixed_per_quality_level(replay: Replay, quality: str) -> None:
    sizes = {(mask.width, mask.height) for mask in replay["runs"][quality]["masks"]}
    assert sizes == {MASK_SIZE[quality]}


@pytest.mark.parametrize("quality", sorted(MASK_SIZE))
def test_the_payload_is_exactly_one_byte_per_pixel(replay: Replay, quality: str) -> None:
    """The row padding check. `bytesPerRow` may exceed the width, and passing the
    padded bytes on as an image shears it progressively down the frame - which looks
    like a bad mask rather than like a bug."""
    for mask in replay["runs"][quality]["masks"]:
        assert len(mask.pixels) == mask.width * mask.height


@pytest.mark.parametrize("quality", sorted(MASK_SIZE))
def test_the_mask_is_neither_empty_nor_everything(replay: Replay, quality: str) -> None:
    """Both failures produce a plausible image, and neither is a mask."""
    coverages = [mask.coverage for mask in replay["runs"][quality]["masks"]]
    assert MIN_COVERAGE < statistics.median(coverages) < MAX_COVERAGE


@pytest.mark.parametrize("quality", sorted(MEASURED_MS))
def test_inference_cost_is_still_what_was_measured(replay: Replay, quality: str) -> None:
    timings = replay["runs"][quality]["timings"]
    median = statistics.median(timings)
    assert median < MAX_MS[quality], (
        "%s: %.2f ms against a measured %.2f - if this is real, DESIGN.md 2.18 "
        "needs updating, not this bound" % (quality, median, MEASURED_MS[quality]))


def test_the_quality_levels_are_ordered_by_cost(replay: Replay) -> None:
    """fast < balanced < accurate, which is what makes the level a real choice
    rather than three names for the same thing."""
    median = {q: statistics.median(run["timings"])
              for q, run in replay["runs"].items()}
    assert median["fast"] < median["balanced"] < median["accurate"]


def test_fast_is_a_hard_alpha_and_balanced_is_feathered(replay: Replay) -> None:
    """MEASURED, and it is a difference beyond resolution that a compositor cares
    about: `fast` returns ONLY 0 and 255, where `balanced` has real intermediate
    values. So `fast` is not simply a smaller `balanced` - it has no soft edge."""
    fast = replay["runs"]["fast"]["masks"][-1]
    balanced = replay["runs"]["balanced"]["masks"][-1]
    assert set(fast.pixels) <= {0, 255}
    assert any(0 < value < 255 for value in balanced.pixels)


def test_the_mask_does_not_match_the_source_aspect_ratio(replay: Replay) -> None:
    """THE finding that shaped the transport's header. A 1280x720 source gets a
    256x192 mask - 16:9 in, 4:3 out - so the mask is anisotropically stretched and
    nothing in the pixels says by how much. This is why `MaskFrame` carries the
    source geometry: a consumer scaling by a single factor would be exactly right on
    a 4:3 camera and quietly wrong on every other one."""
    source_w, source_h = replay["source"]
    mask = replay["runs"]["fast"]["masks"][0]
    source_aspect = source_w / source_h
    mask_aspect = mask.width / mask.height
    assert abs(source_aspect - 16 / 9) < 0.01
    assert abs(mask_aspect - 4 / 3) < 0.01
    assert abs(source_aspect - mask_aspect) > 0.4       # not a rounding difference


def test_the_mask_shape_follows_orientation_and_not_the_input_aspect() -> None:
    """Landscape in, 4:3 out; portrait in, 3:4 out. A SQUARE input gets a PORTRAIT
    mask, which is the case that makes "just stretch it" clearly wrong.

    Its own frames, not the module fixture: this one needs the same image at four
    different shapes, which is a different question from the cost measurements.
    """
    pytest.importorskip("Vision")
    cv2 = pytest.importorskip("cv2", reason="cv2 is dev-only")
    numpy = pytest.importorskip("numpy")
    if not FIXTURE.is_file():
        pytest.skip("no fixture at %s" % FIXTURE)

    from visionhands.engine import pixel_buffer_from_bgra
    from visionhands.segmentation import SegmentationDetector

    capture = cv2.VideoCapture(str(FIXTURE))
    for _ in range(30):
        ok, bgr = capture.read()
    capture.release()
    if not ok:
        pytest.skip("could not read a frame from the fixture")

    detector = SegmentationDetector("fast")
    got = {}
    keepalive = []
    for label, image in (("landscape", bgr),
                         ("square", bgr[:, 280:1000]),
                         ("portrait", cv2.resize(bgr, (480, 640)))):
        bgra = numpy.ascontiguousarray(cv2.cvtColor(image, cv2.COLOR_BGR2BGRA))
        keepalive.append(bgra)
        mask = detector.detect_pixel_buffer(pixel_buffer_from_bgra(bgra), seq=1,
                                            captured_at=0.0)
        assert mask is not None, label
        got[label] = (mask.width, mask.height)

    assert got["landscape"] == (256, 192)
    assert got["portrait"] == (192, 256)
    assert got["square"] == (192, 256), (
        "a 1:1 input returned %r - measured as portrait 192x256, which is the case "
        "that proves the shape is chosen by orientation, not by aspect" % (
            got["square"],))


# -- end to end, through the transport -----------------------------------


def test_a_mask_survives_the_transport_byte_for_byte(replay: Replay, tmp_path: Path) -> None:
    """The whole point of both modules, in one test: Vision's mask goes into the
    shared buffer and comes out of a separate reader identical, with the source
    geometry that makes it alignable.

    Sized for `balanced`, and `fast` published into the same buffer without a
    remap - which is the case that justifies sizing the file once for the largest
    frame and letting the header say what is live.
    """
    from visionhands.maskbuf import MaskReader, MaskWriter

    source = replay["source"]
    big_w, big_h = MASK_SIZE["balanced"]
    path = str(tmp_path / "mask.buf")
    with MaskWriter(path, big_w, big_h) as writer, MaskReader(path) as reader:
        for quality in ("balanced", "fast"):
            mask = replay["runs"][quality]["masks"][-1]
            writer.write(mask.pixels, width=mask.width, height=mask.height,
                         timestamp=mask.captured_at, source=source)
            got = reader.read()
            assert got is not None, quality
            assert got.pixels == mask.pixels, quality
            assert (got.width, got.height) == (mask.width, mask.height), quality
            assert (got.source_width, got.source_height) == source, quality
            # The stretch, recovered. Both factors, because they differ.
            scale_x, scale_y = got.source_scale
            assert abs(scale_x * mask.width - source[0]) < 1e-6
            assert abs(scale_y * mask.height - source[1]) < 1e-6
        assert reader.misses == 0


def test_the_accurate_mask_needs_a_buffer_sized_for_it(replay: Replay, tmp_path: Path) -> None:
    """3 MB at 2016x1512, against 49 KB for `fast`. A buffer sized for `fast` REFUSES
    it rather than truncating - which is the difference between an error and a mask
    that is the top third of the frame."""
    from visionhands.maskbuf import MaskWriter

    mask = replay["runs"]["accurate"]["masks"][-1]
    small_w, small_h = MASK_SIZE["fast"]
    path = str(tmp_path / "small.buf")
    with MaskWriter(path, small_w, small_h) as writer:
        with pytest.raises(ValueError, match="does not fit this buffer's capacity"):
            writer.write(mask.pixels, width=mask.width, height=mask.height)
