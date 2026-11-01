"""Setup and launch agree on a verified runtime, including broken existing installs.

Generated DAT functions are exercised without opening a camera or downloading files.
Ref: DESIGN.md Setup recovery.
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from appletd import install
from appletd.tests.test_generated_templates import _constants


def panel() -> dict[str, Any]:
    source = _constants("td_add_install.py")["CONTROL_SOURCE"] % {
        "comp": "/project1/appletd", "log": repr("/tmp/test-install.log"),
        "control": "install_control"}
    scope: dict[str, Any] = {}
    exec(compile(source, "install_control", "exec"), scope)
    return scope


class Pars:
    def __init__(self, **values: Any) -> None:
        object.__setattr__(self, "values", values)

    def __getattr__(self, name: str) -> Any:
        return SimpleNamespace(eval=lambda: self.values[name])

    def __setattr__(self, name: str, value: Any) -> None:
        self.values[name] = value


def test_stamp_interpreter_is_preferred_to_path(tmp_path: Path) -> None:
    install.write_stamp(str(tmp_path), "version", "/custom/runtime/bin/python")
    candidates = install.candidate_interpreters(str(tmp_path))
    assert candidates[0] == "/custom/runtime/bin/python"
    assert candidates[1] == str(tmp_path / install.PYTHON_BIN)


def test_invalid_explicit_python_does_not_silently_use_another(
        monkeypatch: pytest.MonkeyPatch) -> None:
    tried: list[str] = []

    def verify(path: str, site: str | None = None) -> install.InterpreterCheck:
        tried.append(path)
        return install.InterpreterCheck(path, path != "/broken", "bad runtime")

    monkeypatch.setattr(install, "verify_interpreter", verify)
    assert install.find_interpreter(override="/broken") is None
    assert tried == ["/broken"]


def test_launcher_uses_the_install_panels_verified_selection() -> None:
    constants = _constants("td_build_vision.py")
    source = constants["SIDECAR_CONTROL_SOURCE"] % {
        "resolver": "", "port": 10000, "comp": "/project1/appletd",
        "request_toggles": (("hands", "Streamhands"),), "max_pins": 8,
        "default_mark": "default"}
    scope: dict[str, Any] = {}
    exec(compile(source, "sidecar_control", "exec"), scope)
    selected: list[Any] = []

    def resolve(comp: Any, root: str) -> str:
        selected.append((comp, root))
        return "/verified/python"

    control = SimpleNamespace(module=SimpleNamespace(runtime_python=resolve))
    comp = SimpleNamespace(op=lambda name: control)
    scope["package_root"] = lambda comp: "/installed"
    assert scope["sidecar_python"](comp) == "/verified/python"
    assert selected == [(comp, "/installed")]


def test_no_runtime_produces_an_actionable_error() -> None:
    scope = panel()
    module = SimpleNamespace(DEFAULT_INSTALL_ROOT="/installed",
                             find_interpreter=lambda *a, **kw: None)
    scope["_install_module"] = lambda: module
    comp = SimpleNamespace(par=Pars(Installroot="", Sidecarpython=""),
                           op=lambda name: None)
    with pytest.raises(RuntimeError, match="press Install"):
        scope["runtime_python"](comp)


@pytest.mark.parametrize("file_state", ["stale", "installed"])
def test_install_repairs_or_updates_verified_files_without_network(
        file_state: str, tmp_path: Path) -> None:
    scope = panel()
    written: list[str] = []
    stamps: list[tuple[str, str, str]] = []
    module = SimpleNamespace(
        DEFAULT_INSTALL_ROOT=str(tmp_path),
        require_arm64=lambda: None, require_install_space=lambda root: None,
        probe=lambda *a, **kw: SimpleNamespace(state=file_state, packages=True, model=True),
        find_interpreter=lambda *a, **kw: install.InterpreterCheck("/verified", True, ""),
        requirements_match=lambda root: True,
        write_stamp=lambda *args: stamps.append(args))
    comp = SimpleNamespace(par=Pars(Installroot="", Sidecarpython="", Sourceversion="new",
                                   Installstate="Installed - ready"),
                           op=lambda name: None)
    scope.update(_install_module=lambda: module, _comp=lambda: comp,
                 write_package=lambda root: written.append(root),
                 state=lambda comp: "Installed - ready")
    result = scope["install"]()
    assert result == "Installed - ready"
    assert written == [str(tmp_path)]
    assert stamps == [(str(tmp_path), "new", "/verified")]


@pytest.mark.parametrize("override", ["", "/valid-python-needing-packages"])
def test_broken_runtime_is_not_skipped_when_files_match(
        tmp_path: Path, override: str) -> None:
    scope = panel()
    comp = SimpleNamespace(par=Pars(Installroot="", Sidecarpython=override,
                                   Sourceversion="new"))
    module = SimpleNamespace(
        DEFAULT_INSTALL_ROOT=str(tmp_path), EMBEDDED_MODULES=(), REQUIREMENTS=(),
        require_arm64=lambda: None, require_install_space=lambda root: None,
        probe=lambda *a, **kw: SimpleNamespace(state="installed", packages=True, model=True),
        find_interpreter=lambda *a, **kw: None,
        verify_interpreter=lambda path, **kw: install.InterpreterCheck(path, True, ""))
    scope.update(_install_module=lambda: module, _comp=lambda: comp)
    # Reaching the write step proves matching stamps did not short-circuit repair.
    def write(root: str) -> None:
        raise RuntimeError("repair reached")
    scope["write_package"] = write
    assert scope["install"]() == "Install failed - repair reached"


def test_low_space_is_reported_on_target_volume(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    with pytest.raises(RuntimeError, match="disk space"):
        install.require_install_space(str(tmp_path / "not-created-yet"))


def test_windows_arm_is_not_mistaken_for_an_apple_silicon_mac(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    with pytest.raises(RuntimeError, match="Windows"):
        install.require_arm64()


def test_old_python_is_rejected_before_importing_numpy() -> None:
    # Replace only sys's version in the child, avoiding dependence on a local Python 3.9.
    script = "import sys; sys.version_info = (3, 9); " + install._VERIFY_IMPORTS
    import subprocess
    import sys
    done = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, check=False, env=dict(os.environ))
    assert done.returncode != 0
    assert "Python 3.11" in done.stderr
    assert "numpy" not in done.stderr


def test_launch_reuses_verified_runtime_and_invalidates_changed_files(tmp_path: Path) -> None:
    scope = panel()
    python = tmp_path / "python"
    python.write_text("first")
    calls: list[str] = []

    def find(root: str, **kwargs: Any) -> install.InterpreterCheck:
        calls.append(root)
        return install.InterpreterCheck(str(python), True, "")

    module = SimpleNamespace(DEFAULT_INSTALL_ROOT=str(tmp_path), find_interpreter=find)
    comp = SimpleNamespace(par=Pars(Installroot="", Sidecarpython=""),
                           op=lambda name: None)
    scope["_install_module"] = lambda: module
    assert scope["runtime_python"](comp, verify=True) == str(python)
    assert scope["runtime_python"](comp) == str(python)
    assert len(calls) == 1
    python.write_text("changed runtime")
    with pytest.raises(RuntimeError, match="changed"):
        scope["runtime_python"](comp)
    assert len(calls) == 1


def test_runtime_verification_survives_dat_module_reload(tmp_path: Path) -> None:
    scope = panel()
    python = tmp_path / "python"
    python.touch()
    module = SimpleNamespace(
        DEFAULT_INSTALL_ROOT=str(tmp_path),
        find_interpreter=lambda *a, **kw: install.InterpreterCheck(str(python), True, ""))
    comp = SimpleNamespace(par=Pars(Installroot="", Sidecarpython=""),
                           op=lambda name: None)
    scope["_install_module"] = lambda: module
    assert scope["runtime_python"](comp, verify=True) == str(python)
    reloaded = panel()
    reloaded["_install_module"] = lambda: module
    assert reloaded["runtime_python"](comp) == str(python)
    python.write_text("changed runtime")
    with pytest.raises(RuntimeError, match="changed"):
        reloaded["runtime_python"](comp)
    assert comp.par.Installstate.eval() == "Runtime verification needed - press Install"


def test_runtime_cache_uses_the_actual_development_root(tmp_path: Path) -> None:
    scope = panel()
    dev = str(tmp_path / "checkout")
    seen: list[str] = []

    def find(root: str, **kwargs: Any) -> install.InterpreterCheck:
        seen.append(root)
        return install.InterpreterCheck("/verified/python", True, "")

    module = SimpleNamespace(DEFAULT_INSTALL_ROOT=str(tmp_path / "managed"),
                             find_interpreter=find)
    control = SimpleNamespace(module=SimpleNamespace(package_root=lambda comp: dev))
    comp = SimpleNamespace(par=Pars(Installroot="", Sidecarpython=""),
                           op=lambda name: control)
    scope["_install_module"] = lambda: module
    assert scope["runtime_python"](comp, verify=True) == "/verified/python"
    assert scope["runtime_python"](comp, root=dev) == "/verified/python"
    assert seen == [dev]


def test_state_primes_a_checkout_without_an_install_stamp(tmp_path: Path) -> None:
    scope = panel()
    (tmp_path / "appletd").mkdir()
    (tmp_path / "appletd" / "__init__.py").touch()
    control = SimpleNamespace(module=SimpleNamespace(package_root=lambda comp: str(tmp_path)))
    comp = SimpleNamespace(
        par=Pars(Installroot=str(tmp_path), Sidecarpython="", Sourceversion="version"),
        op=lambda name: control)
    module = SimpleNamespace(
        DEFAULT_INSTALL_ROOT="/unused",
        probe=lambda *a, **kw: SimpleNamespace(state="missing"),
        find_interpreter=lambda *a, **kw: install.InterpreterCheck("/verified", True, ""))
    scope.update(_install_module=lambda: module, _migrate_root=lambda comp: None)
    assert scope["state"](comp) == "Not installed - press Install"
    assert scope["runtime_python"](comp, root=str(tmp_path)) == "/verified"
