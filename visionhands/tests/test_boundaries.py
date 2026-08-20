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
    """Top-level names imported when this file is loaded.

    Contract: returns root module names only - `Quartz` for
              `import Quartz.CoreGraphics`, `cv2` for `from cv2 import imread`.
              Imports inside a function body are EXCLUDED, because those run
              only when the function is called, which is the whole point of a
              lazy import. Imports inside a class body or a module-level
              try/if ARE included: those execute at import time.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()

    def visit(node: ast.AST, inside_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            is_function = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            if not inside_function and not is_function:
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        found.add(alias.name.split(".")[0])
                elif isinstance(child, ast.ImportFrom) and child.module and child.level == 0:
                    found.add(child.module.split(".")[0])
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

    cases = {
        "import cv2\n": {"cv2"},
        "from cv2 import imread\n": {"cv2"},
        "import cv2.aruco\n": {"cv2"},
        "class A:\n    import cv2\n": {"cv2"},          # class body runs at import
        "try:\n    import cv2\nexcept ImportError:\n    pass\n": {"cv2"},
        "def f():\n    import cv2\n    return cv2\n": set(),   # lazy: allowed
        "def f():\n    from cv2 import imread\n": set(),
    }
    for source, expected in cases.items():
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write(source)
            temp_path = Path(handle.name)
        try:
            assert _module_scope_imports(temp_path) & DEV_ONLY_MODULES == expected, source
        finally:
            temp_path.unlink()
