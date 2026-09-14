"""The text contract, with no Vision and no TouchDesigner.

What a Table DAT downstream depends on: the slot count never moves, the rows read top
to bottom, and what the sidecar packs is what the callback unpacks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from appletd.text_types import (
    BLANK_LINE,
    DEFAULT_TEXT_LINES,
    MAX_TEXT_LINES,
    TEXT_ARGS_PER_SLOT,
    TEXT_COLUMNS,
    TEXT_HEADER_ARGS,
    TextFrame,
    TextLine,
    blank_text_frame,
    fill_slots,
    pack_text_args,
    unpack_text_args,
)
from appletd.types import Confidence, NormX, NormY


def _line(text: str, conf: float, y1: float, x1: float = 0.1) -> TextLine:
    return TextLine(text=text, confidence=Confidence(conf), x1=NormX(x1), y1=NormY(y1),
                    x2=NormX(x1 + 0.2), y2=NormY(y1 + 0.05))


def test_a_blank_frame_has_every_slot_and_nothing_in_it() -> None:
    frame = blank_text_frame(slots=5)
    assert frame.slots == 5 and frame.found == 0
    assert len(frame.lines) == 5
    assert all(line is BLANK_LINE for line in frame.lines)
    assert blank_text_frame().slots == DEFAULT_TEXT_LINES


def test_fill_slots_drops_the_least_confident_and_reads_top_to_bottom() -> None:
    """Three lines into two slots: the 0.3 goes, and the survivors come back in
    IMAGE order, highest first, whatever order they arrived in."""
    lines = [_line("bottom", 0.9, y1=0.1), _line("weak", 0.3, y1=0.5),
             _line("top", 0.8, y1=0.8)]
    kept = fill_slots(lines, 2)
    assert [line.text for line in kept] == ["top", "bottom"]


def test_fill_slots_pads_with_blanks_and_keeps_vision_order_on_ties() -> None:
    """At `fast` every confidence is 0.5, so the cap must keep Vision's own order -
    the sort is stable - and then reading order decides the rows."""
    lines = [_line("a", 0.5, y1=0.9), _line("b", 0.5, y1=0.6), _line("c", 0.5, y1=0.3)]
    kept = fill_slots(lines, 4)
    assert [line.text for line in kept] == ["a", "b", "c", ""]
    assert kept[3] is BLANK_LINE
    assert [line.text for line in fill_slots(lines, 2)] == ["a", "b"]


def test_fill_slots_orders_left_to_right_within_a_row() -> None:
    lines = [_line("right", 0.5, y1=0.5, x1=0.6), _line("left", 0.5, y1=0.5, x1=0.1)]
    assert [line.text for line in fill_slots(lines, 2)] == ["left", "right"]


def test_zero_slots_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        fill_slots([], 0)


def test_pack_and_unpack_agree_and_the_layout_is_what_the_callback_expects() -> None:
    frame = TextFrame(seq=41, captured_at=0.0, width=1280, height=720, slots=2, found=3,
                      lines=fill_slots([_line("EXIT £5", 0.9, y1=0.7),
                                        _line("two words", 0.5, y1=0.2)], 2))
    args = pack_text_args(frame, 41.0)
    assert len(args) == TEXT_HEADER_ARGS + TEXT_ARGS_PER_SLOT * 2
    assert args[:3] == [41.0, 3.0, 2.0]
    assert args[3] == "EXIT £5" and isinstance(args[4], float)
    unpacked = unpack_text_args(args)
    assert unpacked is not None
    found, slots, rows = unpacked
    assert (found, slots) == (3, 2)
    assert rows[0][0] == "EXIT £5" and rows[1][0] == "two words"
    assert rows[0][1:] == ["0.9000", "0.1000", "0.7000", "0.3000", "0.7500"]
    assert len(rows[0]) == len(TEXT_COLUMNS)


def test_unpack_returns_none_rather_than_raising_on_anything_malformed() -> None:
    """The copy of this that runs in TouchDesigner is inside an OSC callback on the
    main thread, where an exception costs the operator an error state."""
    good = pack_text_args(blank_text_frame(slots=2), 0.0)
    assert unpack_text_args(good) is not None
    assert unpack_text_args([]) is None
    assert unpack_text_args(good[:-1]) is None                   # one argument short
    assert unpack_text_args([*good, 1.0]) is None                # one too many
    assert unpack_text_args(["x", "y", "z"]) is None             # header not numeric
    assert unpack_text_args([0.0, 0.0, 0.0]) is None             # zero slots
    broken = list(good)
    broken[4] = "not a number"
    assert unpack_text_args(broken) is None


def test_the_slot_bounds_hold_and_the_columns_are_the_arguments() -> None:
    assert 1 <= DEFAULT_TEXT_LINES <= MAX_TEXT_LINES
    assert len(TEXT_COLUMNS) == TEXT_ARGS_PER_SLOT
    assert TEXT_COLUMNS[0] == "text"


def test_the_copy_in_the_touchdesigner_builder_behaves_like_the_original() -> None:
    """`tools/td_add_ocr.py` carries `unpack_text_args` as text inside a DAT template,
    because the callback runs on TouchDesigner's main thread on every message and
    must import nothing. A copy drifts; this holds it to the original on the same
    inputs, malformed ones included."""
    builder = Path(__file__).resolve().parents[2] / "tools" / "td_add_ocr.py"
    head = builder.read_text(encoding="utf-8").split("def main()")[0]
    namespace: dict[str, Any] = {"__file__": str(builder)}
    exec(compile(head, str(builder), "exec"), namespace)
    rendered = namespace["CALLBACK_SOURCE"] % {
        "address": "/ocr", "table": "ocr_table", "columns": TEXT_COLUMNS,
        "header": TEXT_HEADER_ARGS, "per_slot": TEXT_ARGS_PER_SLOT}
    start = rendered.index("# -- unpack: copied from appletd/text_types.py --")
    end = rendered.index("# -- end unpack --")
    copy_ns: dict[str, Any] = {"TEXT_HEADER_ARGS": TEXT_HEADER_ARGS,
                               "TEXT_ARGS_PER_SLOT": TEXT_ARGS_PER_SLOT}
    exec(compile(rendered[start:end], "ocr_callbacks", "exec"), copy_ns)
    copy = copy_ns["unpack_text_args"]

    frame = TextFrame(seq=3, captured_at=0.0, width=1280, height=720, slots=2, found=5,
                      lines=fill_slots([_line("Tickets £5", 0.9, y1=0.7)], 2))
    good = pack_text_args(frame, 3.0)
    broken = list(good)
    broken[4] = "x"
    for case in (good, good[:-1], [*good, 1.0], [], ["a", "b", "c"], broken,
                 pack_text_args(blank_text_frame(slots=1), 0.0)):
        assert copy(case) == unpack_text_args(case), case
