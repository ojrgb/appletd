"""The parser that turns `--list-cameras` output into the Camera menu and its default.

WHY THIS FILE EXISTS. `_parse_camera_lines` lives inside a GENERATED Text DAT - the
control module has to work with no repository behind it - so nothing in the suite could
reach it. Its predecessor took the first tab-separated field of every line, and so
listed "(no video capture devices found)" as a camera. This pulls the REAL TEXT out of
the builder's template, as test_sidecar_match.py does, and holds it to the sidecar's
contract: name, uniqueID, and DEFAULT_CAMERA_MARK on the one device `(default)` means.

Ref: tools/td_build_vision.py, appletd/sidecar.py _print_cameras.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Callable
from typing import cast

from appletd.streams import DEFAULT_CAMERA_MARK

BUILDER = (pathlib.Path(__file__).resolve().parents[2]
           / "tools" / "td_build_vision.py")

Parser = Callable[[str], tuple[list[str], str]]


def _shipped_parser() -> Parser:
    """`_parse_camera_lines` as it is actually generated, with `DEFAULT_MARK` in scope."""
    text = BUILDER.read_text(encoding="utf-8")
    match = re.search(r"\ndef _parse_camera_lines\(text\):\n(?:.*?\n)*?"
                      r"    return names, default\n", text)
    assert match, "no _parse_camera_lines in the builder - has it been renamed?"
    source = match.group(0).replace("%%", "%")
    scope: dict[str, object] = {"DEFAULT_MARK": DEFAULT_CAMERA_MARK}
    exec(compile(source, "td_build_vision.py:_parse_camera_lines", "exec"), scope)
    parser = scope["_parse_camera_lines"]
    assert callable(parser)
    return cast("Parser", parser)


PARSE = _shipped_parser()

REPORTED = ("FaceTime HD Camera\t0x1400000005AC8514\t%s\n"
            "NDI Virtual Camera\tDB3431B9-1CDF-41C3-A7E7-78D4CB5BC9E9\t\n"
            "Merlin Cray Camera\t7626645E-4425-469E-9D8B-97E0FA59AC75\t\n"
            % DEFAULT_CAMERA_MARK)


def test_names_come_out_in_order_and_the_marked_one_is_the_default() -> None:
    names, default = PARSE(REPORTED)
    assert names == ["FaceTime HD Camera", "NDI Virtual Camera", "Merlin Cray Camera"]
    assert default == "FaceTime HD Camera"


def test_the_no_devices_notice_is_not_a_camera() -> None:
    assert PARSE("(no video capture devices found)\n") == ([], "")


def test_an_older_sidecar_with_two_columns_lists_and_marks_nothing() -> None:
    names, default = PARSE("MacBook Pro Camera\t6C707041\nOBS Virtual Camera\t7626645E\n")
    assert names == ["MacBook Pro Camera", "OBS Virtual Camera"]
    assert default == ""


def test_blank_lines_and_stray_whitespace_are_ignored() -> None:
    names, default = PARSE("\n  MacBook Pro Camera \t 6C707041 \t %s \n\n"
                           % DEFAULT_CAMERA_MARK)
    assert names == ["MacBook Pro Camera"]
    assert default == "MacBook Pro Camera"


def test_the_mark_is_interpolated_from_streams_not_retyped() -> None:
    text = BUILDER.read_text(encoding="utf-8")
    assert "DEFAULT_MARK = %(default_mark)r" in text
    assert '"default_mark": DEFAULT_CAMERA_MARK' in text
