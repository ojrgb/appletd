"""The builders' own contract self-checks, run from the test suite.

WHAT THESE GUARD. Several builders hold a LITERAL copy of something the package owns
- `NON_TIP_JOINTS` against `types.JOINT_NAMES`, `FACE_KEYPOINT_NAMES` against
`face_types.FACE_KEYPOINTS`, and so on. They are literals because they live inside
TRIM SCOPE markers and get copied verbatim into a generated DAT that may import
nothing but the standard library. A literal copy of a contract is a second source of
truth, and every one of these drifts SILENTLY: a joint added to the contract simply
never gets trimmed, and `Fingertipsonly` quietly leaves it on the output.

WHY THIS FILE. Each builder already calls its own guards - from `main()`, which runs
only inside TouchDesigner at build time. So they fired when somebody rebuilt, on one
machine, and never in CI or on a commit. A guard that runs only where the failure is
already happening is most of the way to not existing.

They are pure: they import from `appletd` and raise, and touch no TouchDesigner
object. So they run here exactly as they run there.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"


def _builder(name: str) -> dict[str, Any]:
    """A builder module's globals, WITHOUT running its `main()`.

    Every builder ends in a bare `main()` so it can be `run()` from TouchDesigner.
    That call is dropped - and only that call - by rewriting the parsed module, which
    keeps everything else including the definitions after it. Splitting the text at
    `def main(` would lose `td_add_screenspace.py`'s guard, which is defined 400
    lines below its `main`.
    """
    path = TOOLS / name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.Expr)
                         and isinstance(node.value, ast.Call)
                         and isinstance(node.value.func, ast.Name)
                         and node.value.func.id == "main")]
    namespace: dict[str, Any] = {"__name__": name[:-3], "__file__": str(path)}
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace


GUARDS = (
    ("td_add_groups.py", "_check_joint_split"),
    ("td_add_groups.py", "_check_stream_patterns"),
    ("td_add_groups.py", "_check_face_slots"),
    ("td_add_groups.py", "_check_keypoint_names"),
    ("td_add_screenspace.py", "_check_keypoints"),
    ("td_add_latches.py", "_check_latches"),
)


@pytest.mark.parametrize(("builder", "guard"), GUARDS)
def test_a_build_time_guard_passes(builder: str, guard: str) -> None:
    """Each guard, called. They raise RuntimeError naming the drift when they fail."""
    _builder(builder)[guard]()


def test_every_guard_in_the_builders_is_in_this_file() -> None:
    """The list above is a list, so it can go stale the way every list here can. A
    guard added to a builder and not added here would be back to running only inside
    TouchDesigner, which is the situation this file exists to end."""
    found = set()
    for path in sorted(TOOLS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name.startswith("_check_")
                    and not node.args.args):
                found.add((path.name, node.name))
    assert found == set(GUARDS), (
        "not run by this file: %s; listed here but gone from the builders: %s"
        % (sorted(found - set(GUARDS)), sorted(set(GUARDS) - found)))


# ---------------------------------------------------------------------------
# What no single builder can check about itself
# ---------------------------------------------------------------------------
def test_every_latch_output_is_covered_by_a_stream_pattern() -> None:
    """`td_add_latches.py` decides what the latches publish; `td_add_groups.py`
    decides what a stream's toggle removes. Neither may import the other - both
    execute `main()` at import - so the only place the two can be held together is
    here.

    THE FOUR THAT WERE MISSING. The `together` latch publishes `e_clap`, `e_apart`,
    `clap_count` and `apart_count`, none of which carry an `h?_` or `hands_` prefix.
    With `Streamhands` off they stayed on the output, and the two counters HOLD their
    last value rather than falling to zero, so `trim_empty` did not sweep them up
    either: a frozen count that used to mean something, which is the exact "plausible
    wrong number" that layer exists to prevent.
    """
    from fnmatch import fnmatchcase

    latches = _builder("td_add_latches.py")["LATCHES"]
    patterns = _builder("td_add_groups.py")["STREAM_CHANNELS"]

    published = {name for row in latches for name in row[5:10]}
    assert len(published) > 40, "the LATCHES table's shape changed"

    uncovered = sorted(
        name for name in published
        if not any(fnmatchcase(name, pattern)
                   for pattern in patterns["hands"]))
    assert uncovered == [], (
        "these latch channels are on the hands stream and no STREAM_CHANNELS "
        "['hands'] pattern removes them, so `Streamhands` off leaves them on the "
        "output holding their last value: %s" % " ".join(uncovered))


def test_no_latch_output_is_claimed_by_another_stream() -> None:
    """The other direction. A bare name like `ready` in the hands patterns could
    just as easily match something pose or face publishes later."""
    from fnmatch import fnmatchcase

    latches = _builder("td_add_latches.py")["LATCHES"]
    patterns = _builder("td_add_groups.py")["STREAM_CHANNELS"]

    published = {name for row in latches for name in row[5:10]}
    stolen = sorted(name for name in published
                    for stream in ("pose", "face")
                    if any(fnmatchcase(name, p) for p in patterns[stream]))
    assert stolen == [], stolen
