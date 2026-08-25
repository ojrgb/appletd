"""The `ps` line test that decides what gets SIGTERMed.

WHY THIS FILE EXISTS. `_is_sidecar_line` lives inside a GENERATED Text DAT - it has to,
because the sidecar control has to work with no repository behind it - so nothing in the
suite could reach it, and it has now been wrong twice about the same thing:

  * it read `comm=`, which macOS truncates to 16 characters, so a venv interpreter came
    back as `/Users/omer/.ven` and matched nothing;
  * then it read `command=` and split on whitespace to get argv[0], which is only the
    interpreter when the interpreter's path has no space in it. The DEFAULT install root
    is `~/Library/Application Support/appletd`, so on every machine but the author's the
    first token was `/Users/<name>/Library/Application` and a running sidecar read as
    Stopped - with Stop and Restart signalling nothing, so Restart piled up orphans.

Both were invisible on the machine that wrote them. So the function is now pure, and
this pulls the REAL TEXT out of the builder's template and exercises it - not a copy,
which would be the third way to get this wrong.

Ref: tools/td_build_vision.py, docs/JOURNAL.md.
"""

from __future__ import annotations

import pathlib
import re

import pytest

BUILDER = (pathlib.Path(__file__).resolve().parents[2]
           / "tools" / "td_build_vision.py")


def _shipped_predicate():
    """`_is_sidecar_line` as it is actually generated, with `MATCH` in scope.

    Extracted from the builder's source rather than imported: the function only ever
    exists inside a Text DAT, and a copy here would be a second thing to keep right.
    """
    text = BUILDER.read_text(encoding="utf-8")
    match = re.search(r"\ndef _is_sidecar_line\(line\):\n(?:.*?\n)*?    return False\n",
                      text)
    assert match, "no _is_sidecar_line in the builder - has it been renamed?"
    source = match.group(0)
    # The template escapes `%` for its own formatting pass; undo that exactly as
    # `CALLBACK % {...}` would.
    source = source.replace("%%", "%")
    scope: dict[str, object] = {"MATCH": "appletd.sidecar"}
    exec(compile(source, "td_build_vision.py:_is_sidecar_line", "exec"), scope)
    return scope["_is_sidecar_line"]


IS_SIDECAR = _shipped_predicate()

VENV = "/Users/omer/.venvs/appletd/bin/python"
INSTALLED = "/Users/zezelai/Library/Application Support/appletd/python/bin/python3"


@pytest.mark.parametrize("interpreter", [VENV, INSTALLED, "python3",
                                         "/usr/local/bin/python3.11"])
def test_a_sidecar_is_recognised_whatever_its_interpreter_path(interpreter) -> None:
    """Including one with a SPACE in it, which is the default install root and the
    case that read as Stopped on every machine but the author's."""
    assert IS_SIDECAR("%s -m appletd.sidecar --streams face" % interpreter)


def test_the_space_in_application_support_is_the_regression() -> None:
    """Named on its own so a future change that reintroduces an argv[0] split fails
    with an obvious message rather than as one parametrised case among four."""
    assert IS_SIDECAR(INSTALLED + " -m appletd.sidecar")


def test_a_shell_that_merely_mentions_the_sidecar_is_not_one() -> None:
    """`pgrep -f` matches anywhere in a command line, and this function is what stands
    between that and a SIGTERM. The first version of it killed the shell that was
    grepping for the sidecar."""
    assert not IS_SIDECAR("pgrep -f appletd.sidecar")
    assert not IS_SIDECAR("tail -f /tmp/appletd_sidecar.log")
    assert not IS_SIDECAR("/bin/zsh -c 'python -m appletd.sidecar'")
    assert not IS_SIDECAR("vim tools/../appletd/sidecar.py")


def test_the_module_name_must_follow_an_actual_dash_m() -> None:
    """A python whose command line CONTAINS the text is not a sidecar."""
    assert not IS_SIDECAR("/usr/bin/python3 -c x='-m appletd.sidecar'")
    assert not IS_SIDECAR("/usr/bin/python3 appletd.sidecar")
    assert not IS_SIDECAR("/usr/bin/python3 -m appletd.sidecar.helper")


def test_an_empty_line_is_a_process_that_has_already_gone() -> None:
    assert not IS_SIDECAR("")
    assert not IS_SIDECAR("   ")
