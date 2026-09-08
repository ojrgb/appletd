"""Things that were WRONG where being wrong was invisible.

Each of these had a working-looking implementation, a comment describing behaviour it
did not have, and no test. They are grouped because they were found together and
because the shape is the same: the code ran, produced a number, and the number was not
what anything believed it was.

Ref: docs/internals/review-2026-09-08.md section 8.
"""

from __future__ import annotations

import pytest

from appletd.engine import SESSION_COUNTERS
from appletd.segmentation import MaskImage


# ---------------------------------------------------------------------------
# MaskImage.coverage
# ---------------------------------------------------------------------------
def _mask(pixels: bytes, people: int = 0) -> MaskImage:
    return MaskImage(width=len(pixels), height=1, pixels=pixels, seq=1,
                     captured_at=0.0, inference_ms=1.0, people=people)


def test_an_instance_mask_reports_the_people_in_it() -> None:
    """THE DEFECT. An instance mask's byte is a person INDEX, 1..4 - not an alpha -
    and `coverage` thresholded it at 128. So the one signal for "is this mask empty"
    returned 0.0 for every multi-person frame there has ever been, which is exactly
    when it is not empty."""
    assert _mask(bytes([0, 0, 1, 2]), people=2).coverage == pytest.approx(0.5)
    assert _mask(bytes([1, 1, 1, 1]), people=1).coverage == pytest.approx(1.0)
    assert _mask(bytes([0, 0, 0, 0]), people=1).coverage == pytest.approx(0.0)


def test_a_single_person_mask_still_counts_from_half() -> None:
    """The other mask IS an alpha, 0..255, and half is the sensible line. The two
    masks mean different things by a byte, which is what the one formula got wrong."""
    assert _mask(bytes([0, 127, 128, 255])).coverage == pytest.approx(0.5)
    assert _mask(bytes([127, 127])).coverage == pytest.approx(0.0)


def test_an_empty_mask_is_zero_not_a_division_by_zero() -> None:
    assert _mask(b"").coverage == 0.0
    assert _mask(b"", people=3).coverage == 0.0


def test_coverage_does_not_iterate_the_frame_in_python() -> None:
    """It was a generator expression over every byte: 29.0 ms on a 1920x1080 mask,
    for a diagnostic. `translate` and `count` do it in one pass of C."""
    import time

    pixels = bytes(range(256)) * (1920 * 1080 // 256)
    started = time.perf_counter()
    soft = _mask(pixels).coverage
    soft_ms = (time.perf_counter() - started) * 1e3
    started = time.perf_counter()
    instance = _mask(pixels, people=2).coverage
    instance_ms = (time.perf_counter() - started) * 1e3
    # Generous, because a loaded CI box is not a benchmark - the point is that
    # neither path is the 29 ms the comprehension cost.
    assert soft_ms < 20.0, soft_ms
    assert instance_ms < 20.0, instance_ms
    # And both still answer, which is the reason for measuring the fast one at all.
    assert 0.0 < soft < 1.0 and 0.0 < instance <= 1.0


# ---------------------------------------------------------------------------
# Session counters
# ---------------------------------------------------------------------------
def test_every_session_counter_is_in_the_reset_list() -> None:
    """`_reset_session_state` cleared three of fifteen. The twelve that survived a
    stop/start meant every drop RATE read after a restart was the sum of two
    sessions - wrong exactly where it is most needed.

    Read off the class rather than restated: `__init__` assigns each counter by hand
    so a type checker can see it and so the comment saying what it counts stays next
    to it, and this is what binds those assignments to `SESSION_COUNTERS`.
    """
    import ast
    import pathlib

    import appletd.engine

    source = pathlib.Path(appletd.engine.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    engine = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.ClassDef) and node.name == "HandEngine")
    init = next(node for node in engine.body
                if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    assigned = {target.attr for node in ast.walk(init)
                if isinstance(node, ast.Assign)
                for target in node.targets
                if isinstance(target, ast.Attribute)
                and target.attr.startswith("n_")}
    assert assigned == set(SESSION_COUNTERS), (
        "these counters are set up but never reset: %s; and these are reset but "
        "never set up: %s" % (sorted(assigned - set(SESSION_COUNTERS)),
                              sorted(set(SESSION_COUNTERS) - assigned)))


def test_the_sequence_number_is_not_a_session_counter() -> None:
    """It stays monotonic across a restart on purpose, so a consumer reads a gap
    rather than time running backwards (DESIGN.md 6.1). A reset would defeat every
    downstream `seq > last_seq` check until the new engine caught up."""
    assert "_seq" not in SESSION_COUNTERS
    assert not any(name.endswith("seq") for name in SESSION_COUNTERS)


# ---------------------------------------------------------------------------
# The pin solver's refusal message
# ---------------------------------------------------------------------------
def test_a_close_pair_after_a_drop_is_blamed_on_the_spread() -> None:
    """`_refusal` was handed EVERY reading, including the pin the solver had just
    thrown away. So when the dropped pin was the one providing the spread, the
    survivors could be 2.4% of the frame apart and the message still said "fit went
    non-physical - a pin ended up behind the camera" - sending somebody to look for a
    pin behind the camera when the fix was to move one nearer or further.

    These exact numbers came out of a 400,000-case search, because the arrangement is
    not obvious: least squares fits an ISOLATED point well, so the pin at one end of
    the range is usually the last one a residual pass will drop. About 1 in 3,000
    random three-to-five-pin arrangements reaches it.
    """
    import numpy

    from appletd.pins import Pin, solve

    readings = (0.491, 0.479, 0.183)
    depth = numpy.zeros((40, 40), dtype=numpy.float32)
    for index, value in enumerate(readings):
        depth[:, index * 13:(index + 1) * 13] = value
    pins = tuple(Pin(x=(index + 0.5) / 3, y=0.5, metres=metres)
                 for index, metres in enumerate((0.503, 2.339, 6.761)))

    answer = solve(depth, pins, drop_m=0.2)
    assert answer.alpha is None, "this arrangement is supposed to refuse"
    assert answer.dropped == 2, answer.dropped
    assert "too close in depth" in answer.note, answer.note
    assert "behind the camera" not in answer.note, answer.note
