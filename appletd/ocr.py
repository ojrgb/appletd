"""Text recognition: `VNRecognizeTextRequest` -> a TextFrame of fixed slots.

Owns the request, its three knobs - level, language, correction - and the conversion
from `VNRecognizedTextObservation` to floats and strings. It never imports
TouchDesigner, and nothing here reaches the wire: `text_types.py` packs the frame and
`sidecar.py` sends it.

THE KNOBS ARE THE COST. MEASURED at 1280x720 over six lines (docs/BENCHMARKS.md):
`accurate` with Vision's defaults 60.6 ms, `fast` with defaults 8.6 ms, `fast` with one
language pinned and correction off 5.9 ms. There is no smaller English model to fetch;
the two levels ARE the two models. Correction is a dictionary pass over the result and
autodetection a second pass over the image, so both are off by default.

Thread: NOT thread-safe, like every other detector. One detector, one serial capture
        queue.
Ref: DESIGN.md 6.4, appletd/text_types.py.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final

import Vision

from appletd.engine import EngineError
from appletd.streams import (
    DEFAULT_OCR_LANGUAGE,
    DEFAULT_OCR_LEVEL,
    OCR_LEVELS,
    ORIENTATION_UP,
)
from appletd.text_types import (
    DEFAULT_TEXT_LINES,
    MAX_TEXT_LINES,
    TextFrame,
    TextLine,
    fill_slots,
)
from appletd.types import Confidence, NormX, NormY

if TYPE_CHECKING:  # pragma: no cover - typing only
    from objc import ObjCObject

# `VNRequestTextRecognitionLevel`: accurate is 0 and fast is 1. MEASURED - a fresh
# request reports level 0 and Apple's header names 0 `Accurate`. The names live in
# `streams.py`, which the builders import with no pyobjc; only the mapping is here.
_LEVEL_VALUES: Final = {"accurate": 0, "fast": 1}

# One candidate per line. The observation can rank several; the table has one text
# column, and the alternatives are what `usesLanguageCorrection` already chose among.
TOP_CANDIDATES: Final = 1


def verify_text_support() -> None:
    if getattr(Vision, "VNRecognizeTextRequest", None) is None:
        raise EngineError("this Vision has no VNRecognizeTextRequest - text recognition "
                          "needs macOS 10.15 or newer.")


def parse_languages(text: str) -> tuple[str, ...]:
    """`"en-US, fr-FR"` -> `("en-US", "fr-FR")`; empty means detect the language."""
    return tuple(part.strip() for part in text.split(",") if part.strip())


def supported_languages(level: str = DEFAULT_OCR_LEVEL) -> tuple[str, ...]:
    """What this Vision can read at `level`, as BCP 47 tags. Thirty on macOS 26."""
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(_LEVEL_VALUES[level])
    languages, err = request.supportedRecognitionLanguagesAndReturnError_(None)
    #                                                                  ^ TRAP: out-param
    if languages is None:
        raise EngineError("Vision would not list its languages: %s" % (err,))
    return tuple(str(name) for name in languages)


def line_from_observation(observation: ObjCObject) -> TextLine | None:
    """One VNRecognizedTextObservation -> one TextLine, or None with no candidate.

    Contract: the box is `boundingBox()`, which Vision reports NORMALISED with the
              origin BOTTOM-LEFT (MEASURED: the top line of a test image came back at
              y 0.857, not 0.1). Nothing here flips it, so `y1` is the bottom edge and
              `y2` the top - the same convention as every landmark in this system.
    Traps: `topCandidates_` can be empty for an observation that exists, and
              `confidence()` is a flat 0.5 at `fast` - reported as such, not as ours.
    """
    candidates = observation.topCandidates_(TOP_CANDIDATES)
    if not candidates:
        return None
    candidate = candidates[0]
    box = observation.boundingBox()
    x1 = float(box.origin.x)
    y1 = float(box.origin.y)
    return TextLine(text=str(candidate.string()),
                    confidence=Confidence(float(candidate.confidence())),
                    x1=NormX(x1), y1=NormY(y1),
                    x2=NormX(x1 + float(box.size.width)),
                    y2=NormY(y1 + float(box.size.height)))


def text_frame_from_observations(observations: Sequence[ObjCObject], seq: int,
                                 captured_at: float, width: int, height: int,
                                 slots: int) -> TextFrame:
    """Every readable observation into exactly `slots` lines, in reading order."""
    lines = [line for line in (line_from_observation(obs) for obs in observations)
             if line is not None]
    return TextFrame(seq=seq, captured_at=captured_at, width=width, height=height,
                     slots=slots, found=len(lines), lines=fill_slots(lines, slots))


class TextDetector:
    """Holds the text request and turns buffers into TextFrames.

    Thread: NOT thread-safe, and does not need to be - one detector belongs to one
            serial capture queue.
    Its own `VNSequenceRequestHandler`, like every other detector, so it is timed
            separately and one stream's failure cannot fail another's call.
    """

    def __init__(self, level: str = DEFAULT_OCR_LEVEL,
                 language: str = DEFAULT_OCR_LANGUAGE,
                 correction: bool = False,
                 slots: int = DEFAULT_TEXT_LINES) -> None:
        if level not in OCR_LEVELS:
            raise EngineError("level must be one of %s, not %r"
                              % (", ".join(OCR_LEVELS), level))
        if not 1 <= slots <= MAX_TEXT_LINES:
            raise EngineError("slots must be 1..%d, not %d" % (MAX_TEXT_LINES, slots))
        verify_text_support()
        self._sequence = Vision.VNSequenceRequestHandler.alloc().init()
        self._request = Vision.VNRecognizeTextRequest.alloc().init()
        self._request.setRecognitionLevel_(_LEVEL_VALUES[level])
        self._request.setUsesLanguageCorrection_(bool(correction))
        self.languages = parse_languages(language)
        if self.languages:
            self._request.setRecognitionLanguages_(list(self.languages))
            # Off explicitly: with languages given, detection would run anyway and
            # cost the second pass the pinning exists to avoid.
            self._request.setAutomaticallyDetectsLanguage_(False)
        else:
            self._request.setAutomaticallyDetectsLanguage_(True)
        self.level = level
        self.correction = bool(correction)
        self.slots = slots
        self.revision = int(self._request.revision())
        self.last_inference_ms = 0.0
        # Lines beyond the slot count, dropped after the confidence sort. Counted
        # because "there was more text than the table shows" is invisible otherwise.
        self.n_lines_dropped = 0

    def detect_sample_buffer(self, sample_buffer: ObjCObject, seq: int,
                             captured_at: float, width_px: int, height_px: int,
                             orientation: int = ORIENTATION_UP) -> TextFrame:
        """The live path: the same CMSampleBuffer the other detectors just saw."""
        return self._detect(
            lambda: self._sequence.performRequests_onCMSampleBuffer_orientation_error_(
                [self._request], sample_buffer, orientation, None),    # TRAP: out-param
            seq, captured_at, width_px, height_px)

    def detect_pixel_buffer(self, pixel_buffer: ObjCObject, seq: int,
                            captured_at: float, width_px: int, height_px: int,
                            orientation: int = ORIENTATION_UP) -> TextFrame:
        """The replay path, and the one the tests use."""
        return self._detect(
            lambda: self._sequence.performRequests_onCVPixelBuffer_orientation_error_(
                [self._request], pixel_buffer, orientation, None),     # TRAP: out-param
            seq, captured_at, width_px, height_px)

    def _detect(self, perform: Callable[[], tuple[bool, ObjCObject]], seq: int,
                captured_at: float, width_px: int, height_px: int) -> TextFrame:
        started_s = time.perf_counter()
        ok, err = perform()
        self.last_inference_ms = (time.perf_counter() - started_s) * 1e3
        if not ok:
            raise EngineError("Vision text performRequests failed: %s" % (err,))
        results = list(self._request.results() or [])
        frame = text_frame_from_observations(results, seq, captured_at, width_px,
                                             height_px, self.slots)
        if frame.found > self.slots:
            self.n_lines_dropped += frame.found - self.slots
        return frame
