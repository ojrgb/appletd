"""Every generated DAT source renders and compiles, before TouchDesigner sees it.

WHY THIS FILE EXISTS. The builders write Python by `%`-formatting a template, and
several of those templates contain Python that itself formats a string - so a literal
`%` needs one, two or three levels of escaping depending on how deep it sits. Getting
it wrong produces one of two failures, and both are late:

  * `TypeError: not enough arguments for format string` when the builder runs, which
    is at least loud;
  * or a template that renders into a DAT and then breaks when somebody presses the
    button that uses it - on another machine, days later.

That has now happened five times in this repository (docs/JOURNAL.md). The check is
cheap and mechanical: render every template with plausible substitutions and compile
the result. It catches the escaping, unterminated strings from a stray `\\n`, and any
syntax error in code that no test can otherwise reach.

It does NOT check behaviour - `test_sidecar_match.py` does that for the one predicate
worth it. This is the seatbelt: the generated file is at least valid Python.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "tools"

# Every builder that renders a template, and the substitutions its own `main()` passes.
# Kept here rather than parsed out of the call site: a wrong dict here fails loudly,
# and parsing the call site would only prove the parser works.
TEMPLATES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("td_add_about.py", "CONTROL_SOURCE",
     {"comp": "/project1/appletd", "magic": b"\x00\x00\x00\x0110",
      "min_bytes": 50_000, "licence": "/blob/main/LICENSE"}),
    ("td_add_about.py", "CALLBACK_SOURCE",
     {"control": "/project1/appletd/about_control"}),
)


def _constants(builder: str) -> dict[str, Any]:
    """The builder's module-level names, without running `main()`.

    Split on `def main()` rather than importing: `main()` calls `op()` and expects to
    be inside TouchDesigner, and everything a template needs is above it.
    """
    path = TOOLS / builder
    source = path.read_text(encoding="utf-8").split("def main()")[0]
    namespace: dict[str, Any] = {"__file__": str(path)}
    exec(compile(source, str(path), "exec"), namespace)
    return namespace


@pytest.mark.parametrize(("builder", "name", "values"), TEMPLATES,
                         ids=[f"{b}:{n}" for b, n, _ in TEMPLATES])
def test_the_template_renders_and_compiles(builder: str, name: str,
                                           values: dict[str, Any]) -> None:
    rendered = _constants(builder)[name] % values
    compile(rendered, f"{builder}:{name}", "exec")


def test_no_stray_escape_survives_into_the_dat() -> None:
    """A `%%` left in the OUTPUT means one level of escaping too many - the DAT would
    print a literal `%%` at somebody, or format wrongly at the next layer down."""
    for builder, name, values in TEMPLATES:
        rendered = _constants(builder)[name] % values
        assert "%%" not in rendered, f"{builder}:{name} renders a literal %%"


def test_the_about_swap_script_renders_and_compiles() -> None:
    """The one that is a template inside a template. `_SWAP` reaches the DAT through
    `CONTROL_SOURCE`, is written to a temp file with four values prepended as
    assignments, and is then exec'd by `run()` after `loadTox` has destroyed the DAT
    it came from - so nothing at any earlier stage can check it. This can.
    """
    constants = _constants("td_add_about.py")
    dat_text = constants["CONTROL_SOURCE"] % {
        "comp": "/project1/appletd", "magic": constants["TOX_MAGIC"],
        "min_bytes": constants["TOX_MIN_BYTES"],
        "licence": constants["LICENCE_PATH"]}
    module: dict[str, Any] = {"op": lambda _path: None, "run": None}
    exec(compile(dat_text, "about_control", "exec"), module)

    header = ("COMP_PATH = %r\nTOX = %r\nSNAPSHOT = %r\nETAG = %r\n"
              % ("/project1/appletd", "/tmp/x.tox", "/tmp/x.json", "etag"))
    compile(header + module["_SWAP"], "appletd_do_update.py", "exec")


def test_the_swap_script_uses_only_names_the_header_defines() -> None:
    """It runs with no arguments and no imports beyond its own, after the DAT that
    generated it no longer exists. A name it expects and does not get is a
    NameError inside a scheduled string, which TouchDesigner reports SILENTLY
    (DESIGN.md 2.11)."""
    constants = _constants("td_add_about.py")
    dat_text = constants["CONTROL_SOURCE"] % {
        "comp": "/project1/appletd", "magic": constants["TOX_MAGIC"],
        "min_bytes": constants["TOX_MIN_BYTES"],
        "licence": constants["LICENCE_PATH"]}
    module: dict[str, Any] = {"op": lambda _path: None, "run": None}
    exec(compile(dat_text, "about_control", "exec"), module)
    body = module["_SWAP"]
    for name in ("COMP_PATH", "TOX", "SNAPSHOT", "ETAG"):
        assert name in body, f"the swap script never uses {name}"
    # And nothing from the DAT's own scope, which will not exist by then.
    for leaked in ("_say(", "_url(", "_comp(", "TOX_MAGIC", "TOX_MIN_BYTES"):
        assert leaked not in body, f"the swap script reaches back for {leaked}"


def test_the_snapshot_records_mode_and_the_restore_puts_it_back() -> None:
    """A REGRESSION GUARD, 2026-09-03. The first updater snapshotted `par.eval()` and
    restored with `par.val = value`, which reads a live expression as the NUMBER it
    happens to evaluate to and writes it back as a constant. The expression TEXT
    survives, so the damage is invisible: `Renderw` still says
    `op('render1').width ...` and simply stops tracking. Three parameters on the live
    component were frozen that way before anybody noticed.

    Textual, because the behaviour needs TouchDesigner and this does not.
    """
    constants = _constants("td_add_about.py")
    source = constants["CONTROL_SOURCE"]
    assert "values[par.name] = par.eval()" not in source, (
        "the snapshot is reading expressions as their evaluated value again")
    assert '"mode": par.mode.name' in source, "the snapshot does not record par.mode"
    swap = source.split("_SWAP = ")[1]
    for needed in ("par.expr = ", "par.bindExpr = ", 'entry.get("mode")'):
        assert needed in swap, f"the restore never uses {needed!r}"


def test_the_swap_loads_into_the_parent_and_not_into_itself() -> None:
    """THE REGRESSION THAT MATTERED, 2026-09-03. `comp.loadTox(path)` does not replace
    the contents of `comp` - it loads the .tox's root component as a CHILD of it. The
    first updater called it on the component being updated, which nested a copy inside
    the thing it meant to replace, left every original operator and parameter in place,
    and printed "Updated". Two nested copies were found in the live component: 1,239
    descendants where there should have been 309.

    Every check that updater passed was one that passed just as well when nothing had
    happened - a value "surviving" the swap survives best when the parameter was never
    replaced. So this asserts the SHAPE that makes a real replacement possible.
    """
    constants = _constants("td_add_about.py")
    swap = constants["CONTROL_SOURCE"].split("_SWAP = ")[1]
    # CODE ONLY. The comment above the call names the wrong form in order to warn
    # about it, and a substring check would match the warning and call it the bug.
    code = "\n".join(line for line in swap.splitlines()
                     if not line.lstrip().startswith("#"))
    assert re.search(r"(?<!parent_)comp\.loadTox\(", code) is None, (
        "the swap is loading into the component again - that nests, it does not replace")
    assert "parent_comp.loadTox(TOX)" in code, "the swap must load into the PARENT"
    # And the order: the replacement has to exist before the original is destroyed.
    assert code.index("parent_comp.loadTox") < code.index("comp.destroy()"), (
        "the old component is destroyed before the new one has loaded")
