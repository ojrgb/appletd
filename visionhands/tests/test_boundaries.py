"""Enforces the module boundaries by machine, because discipline does not scale.

STANDARDS.md 2 states three rules that the whole design rests on, and the M0
review noted that none of them were enforced by anything but good intentions:

  * `engine.py` imports nothing from TouchDesigner.
  * `td/*` imports no pyobjc.
  * NOTHING in the package imports cv2 or PIL at module scope.

Those boundaries are what let the same engine be driven by a Script CHOP, a
test, or a future out-of-process transport without a rewrite (DESIGN.md 5), and
what makes a C++ swap a transport change rather than a rewrite (9). The third
rule is the one with teeth today: TouchDesigner's bundled Python has numpy but
not cv2, so a stray module-scope `import cv2` anywhere in the package turns into
an ImportError at COOK TIME, inside TD, in front of whatever the project was
being used for.

HOW, and why not by importing. These tests parse the source with `ast` rather
than importing the modules and inspecting `sys.modules`. Importing `td/*`
outside TouchDesigner would fail on the very API the rule is about, and
importing `engine.py` proves nothing about `td/`. Parsing also distinguishes a
module-scope import from one inside a function body, which is exactly the
distinction the third rule turns on - a lazy import inside the function that
draws the debug overlay is fine, the same line at the top of the file is not.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Every module pyobjc gives us that this package might plausibly reach for.
PYOBJC_MODULES = frozenset({
    "objc", "Vision", "Quartz", "AVFoundation", "CoreMedia", "CoreFoundation",
    "Foundation", "libdispatch", "AppKit", "CoreVideo", "CoreImage",
})

# TouchDesigner's own API. `op`, `me` and `ops` are injected names rather than
# modules, so they cannot be imported and do not need listing here.
TD_MODULES = frozenset({"td", "TDFunctions", "TDStoreTools", "TDJSON"})

# Present in the dev venv, absent from TouchDesigner's bundled Python.
DEV_ONLY_MODULES = frozenset({"cv2", "PIL"})


def _module_scope_imports(path: Path) -> set[str]:
    """Every name-ish token an import at module scope pulls in.

    Contract: returns a generous token set, not just root modules - for
              `from visionhands.td import bootstrap` it yields `visionhands`,
              `visionhands.td`, `td` and `bootstrap`. Callers intersect it with
              a set of forbidden module names, so over-collecting costs nothing
              and under-collecting is a silent hole.

              Imports inside a FUNCTION body are excluded - those run only when
              called, which is exactly what makes a lazy `import cv2` legal.
              Imports inside a class body or a module-level `try`/`if` ARE
              included: they execute at import time.

    Two blind spots the M2b review found in the first version, both fixed here
    and both pinned by test_the_detector_itself_catches_a_violation:
      * `level == 0` skipped every RELATIVE import, so `from .td import x` in
        engine.py was invisible to the rule that forbids exactly that.
      * root-name-only matching meant `from visionhands.td import bootstrap`
        reported `visionhands`, which is in no forbidden set, so it passed.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()

    def record(dotted: str) -> None:
        """Record a dotted path as all the tokens a rule might match on."""
        if not dotted:
            return
        parts = dotted.split(".")
        found.add(dotted)                       # the full path
        # EVERY component, not just the root and the leaf: `import
        # visionhands.td.bootstrap` hides the forbidden name in the middle, and
        # a root/leaf-only version of this passed that case (caught by this
        # test's own `import visionhands.td.bootstrap` line).
        found.update(parts)
        for i in range(1, len(parts)):
            found.add(".".join(parts[:i + 1]))  # every prefix

    def visit(node: ast.AST, inside_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            is_function = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            if not inside_function and not is_function:
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        record(alias.name)
                elif isinstance(child, ast.ImportFrom):
                    # child.module is None for `from . import x`; the names are
                    # the only signal there, and they are what matters.
                    record(child.module or "")
                    for alias in child.names:
                        record(alias.name)
            visit(child, inside_function or is_function)

    visit(tree, False)
    return found


def _package_files(subdir: str = "") -> list[Path]:
    root = PACKAGE_ROOT / subdir if subdir else PACKAGE_ROOT
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.py") if "tests" not in p.parts)


def test_engine_does_not_import_touchdesigner() -> None:
    """The engine must be drivable with no TD in the process at all.

    Every test in this suite is proof that it currently is - they import and run
    the engine outside TouchDesigner entirely.
    """
    engine = PACKAGE_ROOT / "engine.py"
    assert engine.is_file()
    offenders = _module_scope_imports(engine) & TD_MODULES
    assert not offenders, "engine.py imports TouchDesigner: %s" % sorted(offenders)


def test_td_layer_does_not_import_pyobjc() -> None:
    """The CHOP side must be importable with no pyobjc present.

    Vacuously true until milestone 4 creates td/ - kept now so that the rule
    exists before the code it constrains, rather than being retrofitted onto a
    layer that already broke it.
    """
    td_files = _package_files("td")
    for path in td_files:
        offenders = _module_scope_imports(path) & PYOBJC_MODULES
        assert not offenders, "%s imports pyobjc: %s" % (path.name, sorted(offenders))


def test_pure_core_imports_neither_pyobjc_nor_td() -> None:
    """types.py and coords.py are the contract both sides share.

    They are imported by the engine (which has pyobjc) AND by td/ (which does
    not), so they may depend on neither. This is the rule that makes the other
    two possible.
    """
    for name in ("types.py", "coords.py"):
        path = PACKAGE_ROOT / name
        assert path.is_file(), name
        imports = _module_scope_imports(path)
        offenders = imports & (PYOBJC_MODULES | TD_MODULES | DEV_ONLY_MODULES)
        assert not offenders, "%s must be pure Python, imports %s" % (name, sorted(offenders))


def test_nothing_imports_cv2_or_pil_at_module_scope() -> None:
    """The rule with teeth: TD's Python has no cv2, and a top-level import there
    fails at cook time rather than at development time.

    Covers the tests directory too - `test_engine_replay.py` needs cv2 and gets
    it inside the fixture function, which is the pattern this rule is meant to
    force.
    """
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        offenders = _module_scope_imports(path) & DEV_ONLY_MODULES
        assert not offenders, ("%s imports %s at module scope - move it inside "
                              "the function that needs it"
                              % (path.relative_to(PACKAGE_ROOT), sorted(offenders)))


def test_the_detector_itself_catches_a_violation() -> None:
    """Proves the check can fail, so a green result means something.

    A boundary test that silently stopped parsing - a renamed directory, an ast
    API change - would pass forever while enforcing nothing. This pins the
    detector's behaviour on known-bad and known-good input, including the
    module-scope versus function-body distinction the third rule turns on.
    """
    import tempfile

    dev_cases = {
        "import cv2\n": {"cv2"},
        "from cv2 import imread\n": {"cv2"},
        "import cv2.aruco\n": {"cv2"},
        "class A:\n    import cv2\n": {"cv2"},          # class body runs at import
        "try:\n    import cv2\nexcept ImportError:\n    pass\n": {"cv2"},
        "def f():\n    import cv2\n    return cv2\n": set(),   # lazy: allowed
        "def f():\n    from cv2 import imread\n": set(),
        "async def f():\n    import cv2\n": set(),
    }
    # The two forms the M2b review found slipping past the first version.
    td_cases = {
        "from .td import bootstrap\n": {"td"},          # relative: was invisible
        "from . import td\n": {"td"},                   # module is None here
        "from visionhands.td import bootstrap\n": {"td"},  # was reported as `visionhands`
        "import visionhands.td.bootstrap\n": {"td"},
        "import td\n": {"td"},
        "def f():\n    from .td import bootstrap\n": set(),
    }
    for cases, forbidden in ((dev_cases, DEV_ONLY_MODULES), (td_cases, TD_MODULES)):
        for source, expected in cases.items():
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
                handle.write(source)
                temp_path = Path(handle.name)
            try:
                assert _module_scope_imports(temp_path) & forbidden == expected, source
            finally:
                temp_path.unlink()
