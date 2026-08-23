"""What an install needs, decided in Python rather than in TouchDesigner.

WHY THIS IS A PACKAGE MODULE AND NOT A BUILDER. Everything here is arithmetic over
paths and subprocesses: which interpreter works, what is already on disk, whether it
is the right version. None of it touches a TouchDesigner object, so all of it can be
tested against a real filesystem and a real interpreter with no TD running - which is
the same split that lets `pins.py` be tested against a synthetic frame.
`tools/td_add_install.py` builds the panel; this decides what the panel is looking at.

THE ONE RULE, and it was learned expensively on 2026-08-23: an interpreter is
verified by RUNNING `import objc` IN A SUBPROCESS OF IT. Not by its version, not by
its path, not by `codesign`. Four signals agreed that TouchDesigner's own Python
would work - `pip install --dry-run` resolved, `pip install --target` exited 0, the
version was right, and the hardened-runtime flag was in output already read - and the
import failed on library validation. See docs/BUILD_PLAN.md 21.1.

THREE ROUTES WERE TRIED. TouchDesigner's bundled python3.11 cannot load pyobjc: the
`disable-library-validation` entitlement is on TouchDesigner.app, not on the
interpreter inside it. Apple's /usr/bin/python3 is 3.9.6 and pyobjc-core has no cp39
wheel worth having. What works is a relocatable CPython, downloaded - which is also
what `uv` does.

ARM ONLY, by decision 2026-08-23. Intel Macs are out of scope: they have no Neural
Engine, so every figure in docs/BENCHMARKS.md is meaningless there and depth in
particular is several times slower. `require_arm64()` refuses rather than installing
something that will disappoint.

Ref: docs/BUILD_PLAN.md step 21, docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Final, NamedTuple

# ---------------------------------------------------------------------------
# The interpreter we download when nothing on the machine will do
# ---------------------------------------------------------------------------
# PINNED, not "latest", and not resolved through the GitHub API. An install that
# worked yesterday must not break because an asset was renamed, and querying an API
# makes the result unreproducible. Bumping this is a deliberate one-line change with
# a re-run of the verification below.
#
# `install_only` is the flavour without the build artefacts - just a working Python.
PYTHON_VERSION: Final = "3.11.16"
PYTHON_RELEASE: Final = "20260814"
PYTHON_URL: Final = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    "%s/cpython-%s%%2B%s-aarch64-apple-darwin-install_only.tar.gz"
    % (PYTHON_RELEASE, PYTHON_VERSION, PYTHON_RELEASE)
)
# OUR OWN HASH OF THE BUILD WE TESTED, and it is worth being exact about what that
# means: the release publishes no `.sha256` asset at this path, so this is not a
# vendor checksum. It is the fingerprint of the exact tarball that was downloaded,
# pip-installed against `requirements.txt`, and run through all 544 tests on
# 2026-08-23. A mismatch means "this is not what we verified" - which is a reason to
# re-verify, never a reason to skip the check.
PYTHON_SHA256: Final = (
    "fcba9f3f676c83e07225e38116649f0c6eb94cb4fcc166632cf92769462b6e39"
)
PYTHON_BYTES: Final = 27_248_161

# Where the tarball unpacks to, relative to the install root, and the interpreter
# inside it. The archive contains a single `python/` directory.
PYTHON_DIRNAME: Final = "python"
PYTHON_BIN: Final = os.path.join(PYTHON_DIRNAME, "bin", "python3")

# ---------------------------------------------------------------------------
# Where an install lives
# ---------------------------------------------------------------------------
# Application Support rather than beside the .toe: always writable, survives the
# project being moved, and versioned by the stamp inside it so two .toe files cannot
# fight. Exposed as `Installroot` on the Advanced page so it is never magic.
DEFAULT_INSTALL_ROOT: Final = os.path.expanduser(
    "~/Library/Application Support/appletd")

STAMP_NAME: Final = "INSTALLED.json"
SITE_PACKAGES: Final = "site-packages"
MODELS_DIRNAME: Final = "models"

# The 19 modules an install has to contain. COMPUTED once and written out as a
# literal, because the thing that writes them out reads Text DATs rather than the
# package - and checked against the real import graph by
# `appletd/tests/test_install.py`, so it cannot drift.
#
# `__init__.py` IS THE TRAP HERE. Nothing imports FROM it, so an import-closure walk
# finds 18 modules and misses the one that makes the other 18 a package - and the
# failure is at the first cook, not at install. It is listed explicitly for that
# reason, and the test that guards this list adds it explicitly too.
RUNTIME_MODULES: Final[tuple[str, ...]] = (
    "__init__",
    "depth",
    "derive",
    "engine",
    "face",
    "face_types",
    "maskbuf",
    "osc",
    "pins",
    "pose",
    "pose_types",
    "segmentation",
    "sidecar",
    "slots",
    "source",
    "spaces",
    "streams",
    "td_layout",
    "types",
)

# What `verify_interpreter` imports. Both matter and neither is optional: `objc` is
# the whole point, and `numpy` was a runtime dependency declared only in the dev
# requirements until 2026-08-23 - an interpreter with pyobjc and no numpy runs until
# `appletd.pins` is imported and then dies.
_VERIFY_IMPORTS: Final = "import objc, numpy"
_VERIFY_TIMEOUT_S: Final = 30.0


def require_arm64() -> None:
    """Raise unless this is an Apple Silicon Mac.

    Called before anything is downloaded. `PYTHON_URL` is an aarch64 build, and an
    Intel Mac has no Neural Engine - so an install there would succeed and then
    perform nothing like the documented figures, which is a worse outcome than a
    refusal that says why.
    """
    machine = platform.machine()
    if machine != "arm64":
        raise RuntimeError(
            "appletd is Apple Silicon only; this machine reports %r. Every timing in "
            "docs/BENCHMARKS.md depends on the Neural Engine, which an Intel Mac does "
            "not have." % machine)


class InterpreterCheck(NamedTuple):
    """The answer to "can this interpreter run the sidecar", and why.

    `detail` carries the failure verbatim when there is one. The library-validation
    error that killed the original design is a wall of dlopen output whose one useful
    phrase is "different Team IDs", and paraphrasing it would have lost that.
    """

    path: str
    ok: bool
    detail: str

    @property
    def summary(self) -> str:
        """One line, for a status field."""
        return "%s: %s" % (self.path, "ok" if self.ok else self.detail.strip()[:120])


def verify_interpreter(python: str,
                       site_packages: str | None = None) -> InterpreterCheck:
    """Can `python` import pyobjc and numpy? Runs it and finds out.

    Contract: NEVER raises for an interpreter that is missing, broken, or refuses to
              load a library - all of those are `ok=False` with the reason in
              `detail`, because "find the first one that works" needs to keep
              looking rather than stop.
    Thread:   spawns a subprocess and waits, bounded by `_VERIFY_TIMEOUT_S`. Not for
              a cook; call it from a button or a probe.
    Why the environment is scrubbed: inheriting our own PYTHONPATH would let the
              candidate import OUR working pyobjc and report success for an
              interpreter that has none of its own.
    """
    if not os.path.exists(python):
        return InterpreterCheck(python, False, "not found")
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    if site_packages:
        environment["PYTHONPATH"] = site_packages
    try:
        done = subprocess.run(
            [python, "-c", _VERIFY_IMPORTS],
            capture_output=True, text=True, timeout=_VERIFY_TIMEOUT_S,
            env=environment, check=False)
    except OSError as problem:
        return InterpreterCheck(python, False, "cannot run: %s" % problem)
    except subprocess.TimeoutExpired:
        return InterpreterCheck(python, False,
                                "timed out after %.0fs" % _VERIFY_TIMEOUT_S)
    if done.returncode == 0:
        return InterpreterCheck(python, True, "")
    return InterpreterCheck(python, False, (done.stderr or done.stdout or "").strip())


def candidate_interpreters(install_root: str = DEFAULT_INSTALL_ROOT,
                           override: str | None = None) -> list[str]:
    """Interpreters to try, best first. Existence is not checked here.

    The ORDER is the policy, and downloading is last on purpose: somebody who has
    already made a venv should not be handed 26 MB they do not need, and somebody who
    has set `Sidecarpython` has said what they want.
    """
    found: list[str] = []

    def add(path: str) -> None:
        if path and path not in found:
            found.append(path)

    if override:
        add(os.path.expanduser(override))
    # An install we have already done, before anything on the wider machine.
    add(os.path.join(install_root, PYTHON_BIN))
    # The convention the README documents.
    add(os.path.expanduser("~/.venvs/appletd/bin/python"))
    for name in ("python3.11", "python3.12", "python3.13", "python3"):
        which = shutil.which(name)
        if which:
            add(which)
    for pattern in ("/opt/homebrew/bin/python3.1*",
                    "~/.pyenv/versions/3.1*/bin/python3"):
        for path in sorted(Path("/").glob(os.path.expanduser(pattern).lstrip("/"))):
            add(str(path))
    return found


def find_interpreter(install_root: str = DEFAULT_INSTALL_ROOT,
                     override: str | None = None) -> InterpreterCheck | None:
    """The first candidate that verifies, or None.

    Returns None rather than raising: "nothing here works yet" is the normal state
    before an install, not an error.
    """
    site_packages = os.path.join(install_root, SITE_PACKAGES)
    extra = site_packages if os.path.isdir(site_packages) else None
    for candidate in candidate_interpreters(install_root, override):
        checked = verify_interpreter(candidate, extra)
        if checked.ok:
            return checked
    return None


class InstallState(NamedTuple):
    """What is on disk, and the one word a status field should show.

    `state` is deliberately the last field computed rather than the first thing
    decided: every part of it is a fact about the filesystem, so a status that says
    "installed" can be traced to the files that made it say so.
    """

    root: str
    modules_present: int
    modules_wanted: int
    packages: bool
    model: bool
    stamp_version: str | None
    wanted_version: str
    state: str

    @property
    def complete(self) -> bool:
        return self.state == "installed"


def read_stamp(root: str = DEFAULT_INSTALL_ROOT) -> dict[str, object] | None:
    """`INSTALLED.json`, or None if it is absent or unreadable.

    Unreadable counts as absent on purpose: a truncated stamp - an install killed
    part way - must read as "install again", not as an exception in a status probe.
    """
    path = os.path.join(root, STAMP_NAME)
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def write_stamp(root: str, version: str, python: str) -> str:
    """Record what was installed. Returns the path written.

    The VERSION is the point of the file. With the package on disk and the same
    package embedded in the `.toe`, a newer `.toe` opened over an older install runs
    the old code every cook with no error and wrong channels - and only a stamp can
    see that. Existence checks cannot.
    """
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, STAMP_NAME)
    payload = {
        "version": version,
        "python": python,
        "python_version": PYTHON_VERSION,
        "modules": list(RUNTIME_MODULES),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return path


def probe(root: str = DEFAULT_INSTALL_ROOT, wanted_version: str = "") -> InstallState:
    """What is installed at `root`, without running anything.

    Cheap by design - it is called when the component loads, so it stats files and
    reads one small JSON. No subprocess, no import, no network. Verifying the
    interpreter is a separate and slower question (`find_interpreter`).

    The states, and each is a fact rather than a guess:
        missing        no stamp, so nothing has ever been installed here
        incomplete     a stamp, but a module, the packages or the model is absent
        stale          everything is there and the stamp names another version
        installed      everything is there and the version matches
    """
    stamp = read_stamp(root)
    package_dir = os.path.join(root, "appletd")
    present = sum(1 for module in RUNTIME_MODULES
                  if os.path.exists(os.path.join(package_dir, module + ".py")))
    packages = os.path.isdir(os.path.join(root, SITE_PACKAGES))
    model = os.path.isdir(os.path.join(root, MODELS_DIRNAME))
    version = None
    if stamp is not None:
        raw = stamp.get("version")
        version = raw if isinstance(raw, str) else None

    if stamp is None:
        state = "missing"
    elif present < len(RUNTIME_MODULES) or not packages:
        state = "incomplete"
    elif wanted_version and version != wanted_version:
        state = "stale"
    else:
        state = "installed"
    return InstallState(root=root, modules_present=present,
                        modules_wanted=len(RUNTIME_MODULES), packages=packages,
                        model=model, stamp_version=version,
                        wanted_version=wanted_version, state=state)
