"""The text contract: what one frame of recognised text is, and how it goes on the wire.

The fourth contract after hands, bodies and faces, and the first whose output is not
channels. A CHOP cannot carry a string, so text lands in a Table DAT - which means
this module owns two things the others do not: a FIXED SLOT COUNT that the table's
row count follows, and the packing of a frame into one typed OSC message.

Pure standard library. No pyobjc, no numpy, no TouchDesigner: the sidecar packs with
this and the TouchDesigner callback unpacks with a copy of `unpack_text_args`, so both
ends can be tested against each other with neither framework present.

Thread: pure data and pure functions. Safe anywhere.
Ref: DESIGN.md 6.4 ("a request with a port and no channels"), docs/ATTRIBUTES.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from appletd.types import Confidence, NormX, NormY

# How many rows the table carries when nobody says otherwise, and the most it may.
#
# A slot count rather than "whatever was found", because a Table DAT that changes
# shape per frame breaks a cell reference downstream silently - the same failure a
# vanishing channel is (DESIGN.md 6.2). Eight is a sign, a label and some change;
# 32 keeps the message inside one datagram on the default socket (osc.py) with
# room for a long line in every slot.
DEFAULT_TEXT_LINES: Final = 8
MAX_TEXT_LINES: Final = 32

# The OSC address, and the argument layout that follows it:
#
#     seq  found  slots  [text conf x1 y1 x2 y2] * slots
#
# `seq` and the counts travel as floats like every other number on this wire, so the
# receiver has ONE numeric type to handle; `seq` is wrapped for float32 exactness
# by the sender, as the channel streams wrap theirs. `found` is how many lines Vision
# reported BEFORE the slot cap, so a table with every slot full can say whether
# something was dropped. `text` is the only string type this project sends.
TEXT_ADDRESS: Final = "/ocr"
TEXT_HEADER_ARGS: Final = 3
TEXT_ARGS_PER_SLOT: Final = 6

# The Table DAT's columns, in order, and the header row `outtext` always carries.
TEXT_COLUMNS: Final[tuple[str, ...]] = ("text", "conf", "x1", "y1", "x2", "y2")


@dataclass(frozen=True)
class TextLine:
    """One recognised line.

    Contract: the box is normalised with the origin BOTTOM-LEFT, exactly as Vision
              reports it and exactly as `h{i}_bbox_x1/_y1/_x2/_y2` are - `x1, y1` is
              the bottom-left corner and `x2, y2` the top-right. `confidence` is
              Vision's own for the top candidate: a real 0..1 at `accurate`, and a
              flat 0.5 at `fast`, which is the framework's behaviour and not ours.
    """

    text: str
    confidence: Confidence
    x1: NormX
    y1: NormY
    x2: NormX
    y2: NormY
    found: bool = True

    def top_y(self) -> float:
        """The sort key for reading order: higher on the image comes first."""
        return float(self.y2)


BLANK_LINE: Final = TextLine(text="", confidence=Confidence(0.0),
                             x1=NormX(0.0), y1=NormY(0.0), x2=NormX(0.0), y2=NormY(0.0),
                             found=False)


@dataclass(frozen=True)
class TextFrame:
    """One text-recognition result, ready to cross the thread boundary.

    Thread: built on the capture queue, read on the send loop's thread, shared by one
            atomic reference rebind and no lock - exactly as `LandmarkFrame` is.
    Contract: `lines` has exactly `slots` entries, always, in reading order (top of
            the image first), with BLANK_LINE in the empty ones. `found` is the
            count BEFORE the cap, so `found > slots` means lines were dropped.
    INVARIANT: strings, floats, ints and bools only. No pyobjc object may be
            referenced from here.
    """

    seq: int
    captured_at: float
    width: int
    height: int
    slots: int
    found: int
    lines: tuple[TextLine, ...]


def blank_text_frame(seq: int = 0, captured_at: float = 0.0, width: int = 0,
                     height: int = 0, slots: int = DEFAULT_TEXT_LINES) -> TextFrame:
    """A frame with every slot empty - what a disabled stream sends, forever."""
    return TextFrame(seq=seq, captured_at=captured_at, width=width, height=height,
                     slots=slots, found=0, lines=tuple(BLANK_LINE for _ in range(slots)))


def fill_slots(lines: Sequence[TextLine], slots: int) -> tuple[TextLine, ...]:
    """Cap `lines` to `slots`, dropping the least confident first, in reading order.

    Contract: returns exactly `slots` entries. The survivors are re-sorted top to
              bottom, then left to right, so slot 0 is the highest line in the image
              whatever order Vision returned them in - a table somebody reads should
              read like the sign did. At `fast` every confidence is 0.5, so the cap
              then keeps Vision's own order, which is already reading order.
    """
    if slots < 1:
        raise ValueError("slots must be at least 1, got %d" % slots)
    # Stable, so equal confidences keep Vision's order and the cap is deterministic.
    kept = sorted(lines, key=lambda line: -float(line.confidence))[:slots]
    kept.sort(key=lambda line: (-line.top_y(), float(line.x1)))
    return tuple(kept) + tuple(BLANK_LINE for _ in range(slots - len(kept)))


def pack_text_args(frame: TextFrame, seq_wrapped: float) -> list[float | str]:
    """The argument list of the one `/ocr` message a frame becomes.

    Contract: `seq_wrapped` is the sequence number already reduced modulo the
              sender's float32 modulus - the same treatment every `*_seq` channel
              gets - because this module does not own that constant.
    """
    args: list[float | str] = [float(seq_wrapped), float(frame.found), float(frame.slots)]
    for line in frame.lines:
        args.extend((line.text, float(line.confidence), float(line.x1), float(line.y1),
                     float(line.x2), float(line.y2)))
    return args


def unpack_text_args(args: Sequence[object]) -> tuple[int, int, list[list[str]]] | None:
    """The receiver's half: `(found, slots, rows)`, or None for anything malformed.

    Rows are STRINGS, formatted for a table cell: the text as it came, the numbers to
    four places. None rather than an exception, because the copy of this that runs in
    TouchDesigner runs on the main thread inside an OSC callback, where raising costs
    the operator an error state and the frame nothing was wrong with.

    The body of this function is copied verbatim into `tools/td_add_ocr.py`; a test
    holds the two copies together.
    """
    if len(args) < TEXT_HEADER_ARGS:
        return None
    try:
        found, slots = int(float(args[1])), int(float(args[2]))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if slots < 1 or len(args) != TEXT_HEADER_ARGS + TEXT_ARGS_PER_SLOT * slots:
        return None
    rows = []
    for index in range(slots):
        base = TEXT_HEADER_ARGS + TEXT_ARGS_PER_SLOT * index
        text = args[base]
        numbers = args[base + 1:base + TEXT_ARGS_PER_SLOT]
        try:
            cells = [format(float(value), ".4f") for value in numbers]  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        rows.append([str(text), *cells])
    return found, slots, rows


def _self_check() -> None:
    if not 1 <= DEFAULT_TEXT_LINES <= MAX_TEXT_LINES:
        raise RuntimeError("the default slot count %d is outside 1..%d"
                           % (DEFAULT_TEXT_LINES, MAX_TEXT_LINES))
    if len(TEXT_COLUMNS) != TEXT_ARGS_PER_SLOT:
        raise RuntimeError("TEXT_COLUMNS and TEXT_ARGS_PER_SLOT disagree: a slot's "
                           "arguments ARE the table's columns")


_self_check()
