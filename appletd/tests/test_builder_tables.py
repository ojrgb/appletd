"""The tables in `tools/` that have to agree with each other.

WHY THIS FILE EXISTS. Every defect it guards has the same shape: a feature touches
several parallel lists and only some of them get updated. Nothing fails - a builder
creates an operator nobody registered, a layer gets no dependencies because it has no
entry, a page is named in two places and only one is right - and the symptom arrives
later as something quiet and wrong. Six of these landed in one day.

Read out of the source with `ast` rather than restated here, because a copy of a table
is one more thing to keep in step.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"


def _module(name: str) -> ast.Module:
    return ast.parse((TOOLS / name).read_text(encoding="utf-8"))


def _constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"`, which is how every builder names operators."""
    return {node.targets[0].id: node.value.value for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)}


def _rebuild_tables() -> dict[str, Any]:
    """`td_rebuild.py`'s LAYERS and REQUIRES, executed without running `main()`.

    The file calls `main()` at import - it is meant to be `run()` from inside
    TouchDesigner - so only the part above the first `def main(` is executed.
    """
    source = (TOOLS / "td_rebuild.py").read_text(encoding="utf-8")
    namespace: dict[str, Any] = {"__file__": str(TOOLS / "td_rebuild.py")}
    exec(compile(source.split("def main(")[0], "td_rebuild", "exec"),
         namespace)
    return namespace


# ---------------------------------------------------------------------------
# td_rebuild.py: which layers drag which along
# ---------------------------------------------------------------------------
def test_every_layer_has_a_requires_entry() -> None:
    """`REQUIRES.get(name, ())` made a MISSING entry look exactly like a considered
    empty one, and six layers were missing. The file's own docstring says each entry
    is written by hand and says why; six of them were not written at all."""
    tables = _rebuild_tables()
    layers = [name for name, _script in tables["LAYERS"]]
    assert sorted(tables["REQUIRES"]) == sorted(layers)


def test_every_builder_that_appends_parameters_requires_pages() -> None:
    """THE "why did my parameters move to General" DEFECT. `td_add_pages.py` is what
    puts a custom parameter on its page and under its heading. A builder that appends
    parameters and does not drag `pages` along leaves them wherever they were
    appended, which is what rebuilding a single layer used to do."""
    tables = _rebuild_tables()
    appends = re.compile(r"\.append(?:Toggle|Float|Int|Str|Pulse|Menu|File|COMP"
                         r"|OP|XY|RGB|Header)\(")
    missing = []
    for layer, script in tables["LAYERS"]:
        if layer == "pages":
            continue
        source = (TOOLS / script).read_text(encoding="utf-8")
        if appends.search(source) and "pages" not in tables["REQUIRES"][layer]:
            missing.append("%s (%s)" % (layer, script))
    assert missing == [], (
        "these append custom parameters but do not pull in `pages`, so rebuilding "
        "one of them alone leaves its parameters unsorted: %s" % ", ".join(missing))


def test_pages_requires_nothing() -> None:
    """It is what everything else requires. A requirement of its own is a cycle
    waiting to be written, and `plan()` has no cycle detection - it would just be
    slow, or wrong, depending on the order things came off the queue."""
    assert _rebuild_tables()["REQUIRES"]["pages"] == ()


# ---------------------------------------------------------------------------
# td_build_vision.py: what a master rebuild is allowed to destroy
# ---------------------------------------------------------------------------
def _master_level_operators(tree: ast.Module) -> set[str]:
    """Names a builder places with `master_xy()`, which is what "on the master COMP"
    means in this codebase - `stream_xy()` is the nested equivalent.

    Loop variables are resolved from the literal the loop walks, because
    `td_add_install.py` creates its three DATs from one tuple.
    """
    constants = _constants(tree)

    def literal(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return constants.get(node.id)
        return None

    def bindings(node: ast.For) -> dict[str, set[str]]:
        """What one `for` binds, POSITIONALLY.

        `for name, source, subs in ((CONTROL, CONTROL_SOURCE, {...}), ...)` binds
        `name` to element 0 of each row. Taking every string anywhere in the iterable
        instead sweeps up the DAT bodies, which are also strings and are large; and
        merging across every loop that happens to use the variable `name` sweeps up
        the parameter names from the loop next to it.
        """
        rows = node.iter.elts if isinstance(node.iter, ast.Tuple | ast.List) else []
        if isinstance(node.target, ast.Name):
            found = {value for row in rows if (value := literal(row)) is not None}
            return {node.target.id: found} if found else {}
        if not isinstance(node.target, ast.Tuple):
            return {}
        out: dict[str, set[str]] = {}
        for index, element in enumerate(node.target.elts):
            if not isinstance(element, ast.Name):
                continue
            found = set()
            for row in rows:
                if isinstance(row, ast.Tuple | ast.List) and index < len(row.elts):
                    value = literal(row.elts[index])
                    if value is not None:
                        found.add(value)
            if found:
                out[element.id] = found
        return out

    def placed(scope: ast.AST) -> list[ast.expr]:
        return [call.args[0] for call in ast.walk(scope)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "master_xy" and call.args]

    names: set[str] = set()
    for argument in placed(tree):
        value = literal(argument)
        if value is not None:
            names.add(value)
    # And once more per loop, resolving only against what THAT loop binds.
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        bound = bindings(node)
        for statement in node.body:
            for argument in placed(statement):
                if isinstance(argument, ast.Name):
                    names.update(bound.get(argument.id, ()))
    return {name for name in names if name}


def _registered() -> dict[str, str]:
    """`OTHER_BUILDERS_OWN`, read as text - the file calls `main()` at import."""
    source = (TOOLS / "td_build_vision.py").read_text(encoding="utf-8")
    block = source.split("OTHER_BUILDERS_OWN = {", 1)[1].split("\n}", 1)[0]
    return dict(re.findall(r'"([a-z_0-9]+)":\s*"([^"]+)"', block))


def test_every_master_level_operator_a_builder_makes_is_registered() -> None:
    """THE ABOUT-PAGE DEFECT, and it had already happened rather than being a risk.
    `td_build_vision.py` destroys every child of the master it does not recognise, and
    `about_control` and `about_callbacks` were not on the list. The saved .toe had all
    four About pulses with neither DAT behind them: Check For Update, Apply Update,
    Open In Browser and Licence all did nothing, silently, because a pulse with
    nothing watching it produces no error and no cook.
    """
    registered = _registered()
    unregistered = []
    for path in sorted(TOOLS.glob("td_add_*.py")):
        for name in sorted(_master_level_operators(_module(path.name))):
            if name not in registered:
                unregistered.append("%s (%s)" % (name, path.name))
    assert unregistered == [], (
        "a master rebuild destroys every child it does not recognise, and these are "
        "not in OTHER_BUILDERS_OWN: %s" % ", ".join(unregistered))


def test_nothing_is_registered_to_a_builder_that_does_not_exist() -> None:
    """The other direction: a stale entry protects a name nothing creates any more,
    which is how a retired operator survives a rebuild forever."""
    missing = sorted({script for script in _registered().values()
                      if not (TOOLS / script.split("/")[-1]).is_file()})
    assert missing == []


# ---------------------------------------------------------------------------
# Parameter Execute DATs
# ---------------------------------------------------------------------------
def test_every_parameter_execute_names_the_op_parameter_the_same_way() -> None:
    """The parameter is called `op`. `par.ops` also worked - TouchDesigner resolved it
    to the same parameter - which is exactly why one builder could use it for months
    without anything going wrong, and why the next reader has to check.
    """
    wrong = []
    for path in sorted(TOOLS.glob("*.py")):
        for line, text in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            if re.search(r"\.par\.ops\s*=", text):
                wrong.append("%s:%d" % (path.name, line))
    assert wrong == [], "the parameter is `op`, not `ops`: %s" % ", ".join(wrong)


# ---------------------------------------------------------------------------
# Page names, which live in two places
# ---------------------------------------------------------------------------
def test_overlay_pages_are_real_pages() -> None:
    """`td_add_overlay.py` creates the page it needs when it is missing, so a typo in
    its table would make a page that `td_add_pages.py` never sorts and the user never
    finds. `td_pages.LAYOUT` is the authority."""
    from appletd.td_pages import LAYOUT

    tree = _module("td_add_overlay.py")
    overlays = next((node for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef) and node.name == "_overlays"),
                    None)
    assert overlays is not None, "td_add_overlay.py no longer has _overlays()"
    # The page name is the fifth field of each row. Read positionally because that is
    # how the builder itself unpacks it.
    pages: set[str] = set()
    for row in ast.walk(overlays):
        if not (isinstance(row, ast.Tuple) and len(row.elts) == 6):
            continue
        fields = row.elts[:5]
        if all(isinstance(field, ast.Constant) for field in fields):
            page_name = fields[4]
            assert isinstance(page_name, ast.Constant)     # for mypy; checked above
            assert isinstance(page_name.value, str)
            pages.add(page_name.value)
    assert pages, "no overlay rows found - the table's shape changed"
    assert pages <= set(LAYOUT), sorted(pages - set(LAYOUT))


@pytest.mark.parametrize("page", ("Body Pose", "Face", "Hands"))
def test_each_overlay_page_has_an_overlay_section(page: str) -> None:
    """And the parameters the overlay builder appends have somewhere to be sorted to.
    Without the section they land at the bottom of the page under no heading."""
    from appletd.td_pages import LAYOUT

    sections = dict(LAYOUT[page])
    assert "Overlay" in sections, sorted(sections)
    assert len(sections["Overlay"]) == 2, sections["Overlay"]


# ---------------------------------------------------------------------------
# The half of td_add_groups.py that is copied by hand
# ---------------------------------------------------------------------------
def _generated_gating() -> ast.FunctionDef:
    """`_apply_gating` as it is written into `groups_callbacks`, parsed.

    The builder holds two of these: the one it runs at build time, and a copy inside
    the DAT source that runs on every toggle change. The file's own docstring calls
    the second one drift-prone, and it drifted - it lost the empty-keep-list guard.
    """
    import re

    source = (TOOLS / "td_add_groups.py").read_text(encoding="utf-8")
    block = next(match.group(3) for match in
                 re.finditer(r"^([A-Z][A-Z0-9_]*)\s*=\s*('''|\"\"\")(.*?)\2",
                             source, re.S | re.M)
                 if "def _apply_gating" in match.group(3))
    filled = re.sub(r"%\(\w+\)[rsd]", "_placeholder", block).replace("%%", "%")
    tree = ast.parse(filled)
    return next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "_apply_gating")


def test_the_generated_gating_bypasses_on_an_empty_keep_list() -> None:
    """AN EMPTY KEEP LIST IS NOT "KEEP NOTHING". It means every group reported
    disabled - every `Stream*` toggle off - and writing "" to a Select CHOP that is
    not bypassed empties the output in silence: no error, `out1` at 0 channels, every
    downstream consumer reading nothing.

    The builder's own copy has carried this guard since it was measured. The
    generated copy lost it, and the loss was masked by the corrective second pass a
    frame later - which is guarded on `moved`, and turning every stream off moves
    nothing.
    """
    gating = _generated_gating()
    bypass = [node for node in ast.walk(gating)
              if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Attribute) and t.attr == "bypass"
                      for t in node.targets)]
    assert bypass, "the generated _apply_gating no longer sets a bypass"
    expression = ast.unparse(bypass[-1].value)
    assert "not keep" in expression, (
        "an empty keep list has to bypass the trim, and this reads %r" % expression)


def test_the_generated_gating_does_not_write_an_empty_keep_list() -> None:
    """The other half of the same guard: `channames` must be written only when there
    is something to write, or the bypass above is racing its own parameter."""
    gating = _generated_gating()
    writes = [node for node in ast.walk(gating)
              if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Attribute) and t.attr == "channames"
                      for t in node.targets)]
    assert writes, "the generated _apply_gating no longer writes a keep list"
    guards = [ast.unparse(node.test) for node in ast.walk(gating)
              if isinstance(node, ast.If)
              and any(write in ast.walk(node) for write in writes)]
    assert any(test.strip() == "keep" for test in guards), (
        "`channames` is written under %s, and none of those is a plain `if keep`"
        % guards)


def test_the_overlay_gate_names_every_op_type_it_creates() -> None:
    """The overlay is drawn with POPs, which arrived in TouchDesigner 2025. On an
    older build every `td.choptoPOP` is an AttributeError halfway through a build - a
    half-made COMP and a traceback naming a missing attribute rather than a missing
    feature. `REQUIRED_OP_TYPES` is the gate, and it has to list the types that
    actually need gating or it gates nothing.

    Only the POPs and the MAT are required: `baseCOMP`, `renderTOP` and the CHOPs
    have been there for years, and demanding them would make the message wrong about
    why it refused.
    """
    import re

    source = (TOOLS / "td_add_overlay.py").read_text(encoding="utf-8")
    created = set(re.findall(r"td\.(\w+(?:POP|MAT))\b", source))
    tree = _module("td_add_overlay.py")
    gate = next(node.value for node in tree.body
                if isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "REQUIRED_OP_TYPES")
    assert isinstance(gate, ast.Tuple), "REQUIRED_OP_TYPES is no longer a tuple"
    listed = {element.value for element in gate.elts
              if isinstance(element, ast.Constant)
              and isinstance(element.value, str)}
    assert created <= listed, (
        "these POP/MAT types are created but not gated, so an older TouchDesigner "
        "fails on them mid-build: %s" % sorted(created - listed))
    assert listed <= created, (
        "these are gated but never created, so the refusal message would be wrong "
        "about why: %s" % sorted(listed - created))


def test_nothing_hardcodes_the_pin_count() -> None:
    """`MAX_PINS` is one number that four places have to agree on: the parameters
    themselves, the launch signature, the Parameter Execute's watch list, and the page
    layout. Three of them had `range(1, 9)` typed in, so raising the count would have
    built a panel with rows nothing watched, nothing placed and nothing restarted
    for - each failing silently, and each differently."""
    import re

    from appletd.td_layout import MAX_PINS

    hardcoded = []
    for path in [*sorted(TOOLS.glob("td_*.py")),
                 *sorted((TOOLS.parent / "appletd").glob("td_*.py"))]:
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            if "Depthpin" in line or "pin" in line.lower():
                if re.search(r"range\(1,\s*%d\)" % (MAX_PINS + 1), line):
                    hardcoded.append("%s:%d" % (path.name, number))
    assert hardcoded == [], (
        "these count pin rows with a literal instead of MAX_PINS: %s"
        % ", ".join(hardcoded))


def test_no_builder_places_a_master_operator_without_asking_keeplayout() -> None:
    """`Keeplayout` promises the master network stays where you put it. Thirty
    operators across ten builders wrote `nodeX`/`nodeY` straight from the layout
    table, so tidying it up and switching the parameter on lasted exactly until the
    next rebuild.

    `ensure()` is the way now - it captures whether the node existed, which the old
    `op(N) or create(N)` had already thrown away by the time anything could ask.

    Nodes INSIDE a group are not covered and are not meant to be: `placement()`'s
    docstring says so, and `temporal` regenerates all 73 of its operators every run.
    This checks master-level placement only, which is the layer a person rearranges.
    """
    import re

    offenders = []
    for path in sorted(TOOLS.glob("*.py")):
        if path.name == "td_build_vision.py":
            continue                   # exempt, and the next test says why
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            if re.search(r"\.nodeX,\s*\w+\.nodeY\s*=\s*master_xy\(", line):
                offenders.append("%s:%d" % (path.name, number))
    assert offenders == [], (
        "these place a master-level operator without consulting Keeplayout - use "
        "`ensure()` from appletd.td_layout: %s" % ", ".join(offenders))


def test_the_master_builder_earns_its_exemption() -> None:
    """`td_build_vision.py` DESTROYS its own operators and makes them again, so by
    the time each is placed there is nothing to ask where it used to be - `ensure()`
    cannot help it, because every node is genuinely new.

    It snapshots every child's position before the destroy loop and puts them back at
    the end instead. That is a different mechanism for the same promise, and this
    checks it is still there rather than taking the exemption on trust.
    """
    source = (TOOLS / "td_build_vision.py").read_text(encoding="utf-8")
    snapshot = source.index("was_at = {")
    destroy = source.index("for child in list(comp.children):")
    restore = source.index("if keep_layout(comp):")
    assert snapshot < destroy, "the snapshot has to be taken BEFORE the destroy"
    assert destroy < restore, "and the restore after everything is rebuilt"
    assert "was_at.get(child.name)" in source


def test_there_is_one_keep_layout_helper() -> None:
    """Seven builders had grown an identical private copy of it, which is six more
    chances for one of them to be fixed and the others not."""
    import re

    copies = [path.name for path in sorted(TOOLS.glob("*.py"))
              if re.search(r"^def _keep_layout\(", path.read_text(encoding="utf-8"),
                           re.M)]
    assert copies == [], (
        "these define their own _keep_layout instead of importing keep_layout from "
        "appletd.td_layout: %s" % ", ".join(copies))
