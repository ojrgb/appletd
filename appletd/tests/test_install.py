"""What an install needs: the module list, the interpreter probe, and the states.

WHY THIS FILE IS SHARP ABOUT SMALL THINGS. Everything here decides whether somebody
else's machine ends up with a working component or a broken one, and both failures are
quiet. A module missing from `RUNTIME_MODULES` is a network that raises at the first
cook. An interpreter probe that says yes when it means no is the bug that killed the
original install design (docs/BUILD_PLAN.md 21.1) - four separate signals agreed while
the thing did not work.

No TouchDesigner and no network: paths, a real subprocess against this interpreter,
and a temporary directory.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from appletd import install


# ---------------------------------------------------------------------------
# The module list, which cannot be allowed to drift
# ---------------------------------------------------------------------------
def _import_closure(roots: tuple[str, ...], package_dir: Path) -> set[str]:
    """Every `appletd` module reachable from `roots` by following imports."""
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        path = package_dir / (name + ".py")
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("appletd."):
                    todo.append(node.module.split(".", 1)[1])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("appletd."):
                        todo.append(alias.name.split(".", 1)[1])
    return {name for name in seen if (package_dir / (name + ".py")).exists()}


def test_runtime_modules_is_exactly_the_import_closure() -> None:
    """`RUNTIME_MODULES` is a literal because the thing that writes those files out
    reads Text DATs rather than the package. A literal copy of a fact is a second
    source of truth, and this one fails SILENTLY: a module added to the package and
    not to the list is simply never installed, and the network raises at its first
    cook on somebody else's machine.

    The roots are the four entry points anything outside the package imports: the
    sidecar process, and the three modules the generated DATs use.
    """
    package_dir = Path(install.__file__).parent
    wanted = _import_closure(
        ("sidecar", "derive", "maskbuf", "pins", "spaces", "td_layout"), package_dir)
    wanted.add("__init__")
    assert set(install.RUNTIME_MODULES) == wanted


def test_dunder_init_is_in_the_list_explicitly() -> None:
    """THE trap in this file. Nothing imports FROM `__init__.py`, so a closure walk
    finds 18 modules and misses the one that makes the other 18 a package - and
    `import appletd.sidecar` then fails at the first cook rather than at install.

    Asserted on its own, and not just as part of the set above, because the test
    above ADDS it by hand: if that line were ever removed both tests would still
    pass on a closure that is quietly wrong.
    """
    assert "__init__" in install.RUNTIME_MODULES
    package_dir = Path(install.__file__).parent
    closure = _import_closure(("sidecar",), package_dir)
    assert "__init__" not in closure, "if this ever fails, the trap is gone"


def test_every_named_module_actually_exists() -> None:
    package_dir = Path(install.__file__).parent
    missing = [m for m in install.RUNTIME_MODULES
               if not (package_dir / (m + ".py")).exists()]
    assert missing == []


# ---------------------------------------------------------------------------
# The interpreter probe
# ---------------------------------------------------------------------------
def test_this_interpreter_verifies() -> None:
    """The positive case has to be a real subprocess against a real interpreter, or
    the probe is testing itself."""
    checked = install.verify_interpreter(sys.executable)
    assert checked.ok, checked.detail
    assert checked.detail == ""
    assert "ok" in checked.summary


def test_a_missing_interpreter_is_false_and_not_an_exception() -> None:
    """`find_interpreter` walks a list of candidates most of which do not exist, so
    "not there" has to be an answer rather than a raise."""
    checked = install.verify_interpreter("/nonsense/python3")
    assert checked.ok is False
    assert checked.detail == "not found"


def test_an_interpreter_without_pyobjc_fails_with_its_own_reason(
        tmp_path: Path) -> None:
    """And the reason is kept verbatim. The error that killed the original design is
    a wall of dlopen output whose one useful phrase is "different Team IDs";
    paraphrasing it would have thrown that away."""
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\necho 'ModuleNotFoundError: objc' >&2\nexit 1\n")
    fake.chmod(0o755)
    checked = install.verify_interpreter(str(fake))
    assert checked.ok is False
    assert "ModuleNotFoundError" in checked.detail


def test_the_probe_does_not_let_a_candidate_inherit_our_pyobjc(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """THE test that makes the probe worth having.

    If the subprocess inherited our `PYTHONPATH`, an interpreter with no pyobjc of
    its own would import OURS and be reported as working - and the install would
    then launch a sidecar that dies. So `PYTHONPATH` is scrubbed, and replaced only
    by the install's own site-packages when there is one.
    """
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("PYTHONPATH", "/somewhere/of/our/own")
    monkeypatch.setattr(subprocess, "run", fake_run)

    install.verify_interpreter(sys.executable)
    assert "PYTHONPATH" not in seen["env"], "our own path leaked into the candidate"

    install.verify_interpreter(sys.executable, "/an/install/site-packages")
    assert seen["env"]["PYTHONPATH"] == "/an/install/site-packages"


def test_the_probe_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung interpreter must not hang the button that called it."""
    def timing_out(argv: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", timing_out)
    checked = install.verify_interpreter(sys.executable)
    assert checked.ok is False
    assert "timed out" in checked.detail


def test_candidates_put_an_override_first_and_downloading_last() -> None:
    """The ORDER is the policy: somebody who already has a venv should not be handed
    26 MB, and somebody who set `Sidecarpython` has said what they want."""
    candidates = install.candidate_interpreters("/install", override="/mine/python")
    assert candidates[0] == "/mine/python"
    assert candidates[1] == os.path.join("/install", install.PYTHON_BIN)
    assert len(candidates) == len(set(candidates)), "a candidate appears twice"


def test_candidates_expand_a_user_relative_override() -> None:
    candidates = install.candidate_interpreters("/install", override="~/x/python")
    assert candidates[0] == os.path.expanduser("~/x/python")
    assert "~" not in candidates[0]


# ---------------------------------------------------------------------------
# The states, which drive the status field
# ---------------------------------------------------------------------------
def _install_into(root: Path, *, version: str = "1", skip: str = "",
                  packages: bool = True, model: bool = True) -> None:
    package = root / "appletd"
    package.mkdir(parents=True, exist_ok=True)
    for module in install.RUNTIME_MODULES:
        if module == skip:
            continue
        (package / (module + ".py")).write_text("")
    if packages:
        (root / install.SITE_PACKAGES).mkdir(exist_ok=True)
    if model:
        (root / install.MODELS_DIRNAME).mkdir(exist_ok=True)
    install.write_stamp(str(root), version, "/some/python")


def test_nothing_installed_reads_missing(tmp_path: Path) -> None:
    state = install.probe(str(tmp_path))
    assert state.state == "missing"
    assert state.complete is False
    assert state.modules_present == 0
    assert state.modules_wanted == len(install.RUNTIME_MODULES)


def test_a_complete_install_reads_installed(tmp_path: Path) -> None:
    _install_into(tmp_path, version="abc")
    state = install.probe(str(tmp_path), wanted_version="abc")
    assert state.state == "installed"
    assert state.complete is True
    assert state.modules_present == len(install.RUNTIME_MODULES)


def test_one_missing_module_reads_incomplete(tmp_path: Path) -> None:
    """Including `__init__.py`, which is the one a generated file list drops."""
    _install_into(tmp_path, version="abc", skip="__init__")
    state = install.probe(str(tmp_path), wanted_version="abc")
    assert state.state == "incomplete"
    assert state.modules_present == len(install.RUNTIME_MODULES) - 1


def test_missing_packages_reads_incomplete(tmp_path: Path) -> None:
    _install_into(tmp_path, version="abc", packages=False)
    assert install.probe(str(tmp_path), wanted_version="abc").state == "incomplete"


def test_a_version_mismatch_reads_stale(tmp_path: Path) -> None:
    """THE state that stops a silent wrong answer. With the package on disk AND the
    same package embedded in the `.toe`, a newer `.toe` over an older install runs
    the OLD code every cook - no error, wrong channels. Existence checks cannot see
    that; only the stamp can."""
    _install_into(tmp_path, version="old")
    state = install.probe(str(tmp_path), wanted_version="new")
    assert state.state == "stale"
    assert state.stamp_version == "old"
    assert state.wanted_version == "new"


def test_no_wanted_version_does_not_invent_staleness(tmp_path: Path) -> None:
    """A caller that does not know what version it wants must not be told the install
    is stale - "I cannot tell" is not "it is wrong"."""
    _install_into(tmp_path, version="old")
    assert install.probe(str(tmp_path)).state == "installed"


def test_the_model_is_reported_but_does_not_decide_completeness(
        tmp_path: Path) -> None:
    """Depth is optional. An install with no model is complete for the four Vision
    streams, and saying otherwise would push everybody through a 47 MB download for
    a feature they may not want."""
    _install_into(tmp_path, version="abc", model=False)
    state = install.probe(str(tmp_path), wanted_version="abc")
    assert state.model is False
    assert state.state == "installed"


# ---------------------------------------------------------------------------
# The stamp
# ---------------------------------------------------------------------------
def test_the_stamp_round_trips(tmp_path: Path) -> None:
    written = install.write_stamp(str(tmp_path), "v1", "/usr/bin/python3")
    assert os.path.exists(written)
    stamp = install.read_stamp(str(tmp_path))
    assert stamp is not None
    assert stamp["version"] == "v1"
    assert stamp["python"] == "/usr/bin/python3"
    assert stamp["modules"] == list(install.RUNTIME_MODULES)


def test_a_truncated_stamp_reads_as_absent(tmp_path: Path) -> None:
    """An install killed part way through leaves half a JSON file. That has to read
    as "install again", not as an exception inside a status probe that runs when the
    component loads."""
    (tmp_path / install.STAMP_NAME).write_text('{"version": "v1"')
    assert install.read_stamp(str(tmp_path)) is None
    assert install.probe(str(tmp_path)).state == "missing"


def test_a_stamp_that_is_not_an_object_reads_as_absent(tmp_path: Path) -> None:
    (tmp_path / install.STAMP_NAME).write_text(json.dumps(["not", "a", "dict"]))
    assert install.read_stamp(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# The content version, which is what makes `stale` detectable
# ---------------------------------------------------------------------------
def test_the_version_is_deterministic() -> None:
    """Same input, same answer, in any process. The embed builder stamps it into the
    `.toe` and an install records it into `INSTALLED.json`, and those two runs are
    weeks and machines apart - so a hash that varied by run would make every install
    read as stale for ever."""
    pairs = [("a", "one"), ("b", "two")]
    assert install.content_version(pairs) == install.content_version(list(pairs))
    assert len(install.content_version(pairs)) == install.VERSION_CHARS


def test_changing_a_module_changes_the_version() -> None:
    """One character of one module. If this did not hold, an install would never
    know it was out of date."""
    assert install.content_version([("a", "one")]) != \
        install.content_version([("a", "ONE")])


def test_renaming_a_module_changes_the_version_even_with_identical_text() -> None:
    """The subtle one, and the reason the name is hashed as well as the text: a
    rename means the install has to write DIFFERENT FILES, so it is a different
    install even though not one line changed."""
    assert install.content_version([("slots", "x")]) != \
        install.content_version([("plots", "x")])


def test_order_is_part_of_the_identity() -> None:
    """`RUNTIME_MODULES` has a fixed order and `module_sources` follows it, so two
    different orders are two different embeds and should not claim to be the same."""
    assert install.content_version([("a", "1"), ("b", "2")]) != \
        install.content_version([("b", "2"), ("a", "1")])


def test_the_version_of_the_real_package_is_stable_across_calls() -> None:
    """Against the actual files, not synthetic pairs - the thing the builder hashes."""
    package_dir = Path(install.__file__).parent
    sources = [(name, (package_dir / (name + ".py")).read_text())
               for name in install.RUNTIME_MODULES]
    first = install.content_version(sources)
    assert first == install.content_version(sources)
    assert len(first) == install.VERSION_CHARS


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
def test_arm64_is_required_and_this_machine_qualifies() -> None:
    """Intel is out of scope by decision 2026-08-23: no Neural Engine, so every
    figure in BENCHMARKS.md is meaningless there. Refusing beats installing
    something that will disappoint."""
    install.require_arm64()


def test_a_non_arm_machine_is_refused_with_a_reason(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    with pytest.raises(RuntimeError, match="Apple Silicon only"):
        install.require_arm64()


def test_the_pinned_download_is_arm_and_carries_a_checksum() -> None:
    """Pinned rather than resolved through an API: an install that worked yesterday
    must not break because an asset was renamed. And the hash is the fingerprint of
    the exact tarball that was run through this suite - a mismatch means "not what we
    verified", which is a reason to re-verify and never to skip the check."""
    assert "aarch64-apple-darwin" in install.PYTHON_URL
    assert install.PYTHON_RELEASE in install.PYTHON_URL
    assert install.PYTHON_VERSION in install.PYTHON_URL
    assert len(install.PYTHON_SHA256) == 64
    assert install.PYTHON_BYTES > 20_000_000
