"""`Fingertipsonly` is applied in a different PLACE when a hand overlay is on.

The hand overlay draws a skeleton, so it needs all 21 joints in world space - and
`coords` only composes what reaches it. `early_trim` sits before `coords`, so
trimming there leaves the overlay five tips and no bones to draw between them.

The fix is to move WHERE the toggle is enforced, not WHETHER: `early_trim` holds it
back while the overlay is on and `trim_empty`'s keep list takes it instead. That
keeps the output contract identical, which is the one property worth a test - a
regression here would put fifteen joints per hand on somebody's output with no
error anywhere, and "my channel count changed" is a bad way to find out.

Ref: tools/td_add_groups.py `removing_patterns`.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "tools"


class _Par:
    """The two things `_shaping` asks of a parameter."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def eval(self) -> Any:
        return self._value


def _comp(**toggles: Any) -> SimpleNamespace:
    """A stand-in COMP carrying only the toggles a case sets.

    A toggle left out is ABSENT rather than off, which is the half-built network
    `removing_patterns` promises to tolerate.
    """
    return SimpleNamespace(
        par=SimpleNamespace(**{name: _Par(value)
                               for name, value in toggles.items()}))


@pytest.fixture(scope="module")
def groups() -> dict[str, Any]:
    """`tools/td_add_groups.py`'s module scope, without running `main()`.

    Split on `def main()` rather than imported: `main()` calls `op()` and expects to
    be inside TouchDesigner. Everything this file needs is above it.
    """
    path = TOOLS / "td_add_groups.py"
    source = path.read_text(encoding="utf-8").split("def main()")[0]
    namespace: dict[str, Any] = {"__file__": str(path)}
    exec(compile(source, str(path), "exec"), namespace)
    return namespace


def _tip_patterns(groups: dict[str, Any], patterns: list[str]) -> list[str]:
    """The subset of `patterns` that `Fingertipsonly` asked for."""
    wanted = {"h?_%s_*" % joint for joint in groups["NON_TIP_JOINTS"]}
    return [pattern for pattern in patterns if pattern in wanted]


def test_the_early_trim_keeps_the_joints_while_the_overlay_needs_them(
        groups: dict[str, Any]) -> None:
    comp = _comp(Fingertipsonly=True, Handsoverlay=True)
    early = groups["removing_patterns"](comp, "early")
    assert _tip_patterns(groups, early) == []


def test_the_output_still_drops_them(groups: dict[str, Any]) -> None:
    """The contract: overlay or no overlay, `Fingertipsonly` shapes the output."""
    comp = _comp(Fingertipsonly=True, Handsoverlay=True)
    late = groups["removing_patterns"](comp, "late")
    assert len(_tip_patterns(groups, late)) == len(groups["NON_TIP_JOINTS"])


def test_the_two_stages_agree_when_no_overlay_is_on(
        groups: dict[str, Any]) -> None:
    """Nothing changes for a project that does not use the overlay - including the
    cost, which is the reason the early trim exists at all."""
    comp = _comp(Fingertipsonly=True, Handsoverlay=False)
    assert (groups["removing_patterns"](comp, "early")
            == groups["removing_patterns"](comp, "late"))


def test_the_late_stage_is_the_default(groups: dict[str, Any]) -> None:
    """`_trim_keep` calls this with no stage, and it must get the full list."""
    comp = _comp(Fingertipsonly=True, Handsoverlay=True)
    assert (groups["removing_patterns"](comp)
            == groups["removing_patterns"](comp, "late"))


def test_a_missing_overlay_toggle_is_not_an_error(
        groups: dict[str, Any]) -> None:
    """A half-built network: the parameter does not exist yet."""
    comp = _comp(Fingertipsonly=True)
    assert (len(_tip_patterns(groups, groups["removing_patterns"](comp, "early")))
            == len(groups["NON_TIP_JOINTS"]))


def test_fingertips_off_removes_nothing_either_way(
        groups: dict[str, Any]) -> None:
    comp = _comp(Fingertipsonly=False, Handsoverlay=True)
    for stage in ("early", "late"):
        assert _tip_patterns(groups, groups["removing_patterns"](comp, stage)) == []


def test_the_stream_toggle_vetoes_the_overlay(groups: dict[str, Any]) -> None:
    """`Show Overlay` on with `Hands` off is not an overlay - the channels are frozen
    at their last value - so it does not get to hold the joints open."""
    comp = _comp(Fingertipsonly=True, Handsoverlay=True, Streamhands=False)
    assert (len(_tip_patterns(groups, groups["removing_patterns"](comp, "early")))
            == len(groups["NON_TIP_JOINTS"]))


def test_the_stream_toggle_on_allows_it(groups: dict[str, Any]) -> None:
    comp = _comp(Fingertipsonly=True, Handsoverlay=True, Streamhands=True)
    assert _tip_patterns(groups, groups["removing_patterns"](comp, "early")) == []
