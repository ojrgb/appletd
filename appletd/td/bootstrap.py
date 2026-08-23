"""sys.path wiring and engine lifecycle, for use inside TouchDesigner.

    HOW TO USE IT, minimally: put this in a Text DAT and run it once.

        import sys
        sys.path.append('/path/to/your/appletd')   # wherever you cloned it
        from appletd.td import bootstrap
        bootstrap.start()

    ...and this in the project's onExit / unload path, which is NOT optional:

        from appletd.td import bootstrap
        bootstrap.stop()

WHY THE TEARDOWN IS NOT OPTIONAL. A background thread calling into native
frameworks is a liveness hazard, not a tidiness problem: a Vision call landing
on a torn-down capture session is a native crash that `except` cannot catch and
that takes the whole of TouchDesigner with it (DESIGN.md 8). Module globals -
including the source this module holds - survive a TD project reload, because
the interpreter is not restarted. So without an explicit stop, a reload leaves
the old camera session running and starts a second one beside it.

This module is built to make that failure hard rather than to trust anyone to
remember: `start()` stops any existing source before creating a new one, and
`stop()` is safe to call at any time, including twice, including before any
start.

Thread: everything here runs on TD's main thread. Nothing here blocks for long
        except `stop()`, which deliberately waits for the capture queue to drain
        (~0.2 s) because returning early is what causes the crash above.

Ref: DESIGN.md 8 (lifecycle), 11 (venv strategy), 2.5 (why append, measured).
"""

from __future__ import annotations

import os
import sys

# The dev venv's site-packages. This is the ONE path that has to be right for
# any of this to work inside TouchDesigner.
DEFAULT_VENV_SITE_PACKAGES = os.path.expanduser(
    "~/.venvs/appletd/lib/python3.11/site-packages")

# The module-level source. A global, deliberately: a Script CHOP cooks 60 times
# a second and cannot afford to construct anything, so the source has to outlive
# a cook. That this global survives a project reload is the whole reason stop()
# exists (DESIGN.md 8).
_source: object | None = None


def add_venv_to_path(site_packages: str = DEFAULT_VENV_SITE_PACKAGES) -> bool:
    """Make pyobjc importable inside TouchDesigner. Returns True if usable.

    APPEND, NEVER INSERT. TouchDesigner ships its own numpy, compiled against
    TD's own extension modules. Putting our site-packages first would shadow it
    with the venv's copy, which is an ABI mismatch inside TD's process - a
    crash, not an ImportError. MEASURED (DESIGN.md 2.5): with an append, numpy
    correctly resolved to TD's own 2.1.2 rather than the venv's 2.2.6, while
    pyobjc still loaded fine from the venv. Appending means our path is only
    consulted for names TD does not already provide, which is exactly the pyobjc
    frameworks and nothing else.

    Returns False rather than raising if the path is missing, so a caller can
    report it usefully instead of catching an exception from an import three
    frames down.
    """
    if not os.path.isdir(site_packages):
        return False
    if site_packages not in sys.path:
        sys.path.append(site_packages)      # INVARIANT: append, never insert(0)
    return True


def start(camera_name: str | None = None,
          site_packages: str = DEFAULT_VENV_SITE_PACKAGES) -> object:
    """Start the camera and return the running source. Safe to call repeatedly.

    Stops any existing source FIRST. That ordering is the point: a TD project
    reload re-runs this DAT while the previous source is still holding the
    camera, and two capture sessions fighting over one device is the failure
    DESIGN.md 8 names explicitly. Restarting is cheap; two sessions are not.

    Raises: RuntimeError if the venv is missing, with the path in the message -
            that is by far the most likely first-run failure, and an ImportError
            from deep inside pyobjc does not say which path was wrong.
            Anything the engine raises (no camera permission, no matching
            device) propagates unchanged.
    """
    global _source
    stop()

    if not add_venv_to_path(site_packages):
        raise RuntimeError(
            "appletd: no site-packages at %s - create the venv with\n"
            "  ~/.pyenv/versions/3.11.9/bin/python3.11 -m venv ~/.venvs/appletd\n"
            "  ~/.venvs/appletd/bin/pip install -r requirements.txt"
            % site_packages)

    # Imported here, not at module scope, for two reasons. The path above has to
    # be in place first. And this module stays importable - so `stop()` can be
    # called on an unload path - even when the venv is missing entirely.
    from appletd.source import InProcessSource

    source = InProcessSource(camera_name=camera_name)
    source.start()
    _source = source
    return source


def stop() -> None:
    """Stop and release the source. Idempotent, and never raises.

    Never raises because this is what runs on TD's unload path, and an exception
    there would leave the camera running while looking like a failure to stop.
    The one thing this must not do is give up quietly on a live session, so a
    failure is printed to the Textport rather than swallowed silently
    (STANDARDS.md 2: recorded, never silently dropped).
    """
    global _source
    source = _source
    _source = None
    if source is None:
        return
    try:
        source.stop()                       # type: ignore[attr-defined]
    except Exception as exc:                # noqa: BLE001 - unload path, see docstring
        print("appletd: error stopping source: %r" % (exc,))


def source() -> object | None:
    """The running source, or None. This is what the Script CHOP reads."""
    return _source


def status() -> str:
    """A one-line summary for the Textport. Diagnostics, not a channel.

    Deliberately tolerant of every partial state, because the times you type
    this are exactly the times something is half-built.
    """
    current = _source
    if current is None:
        return "appletd: not started"
    try:
        frame = current.latest()            # type: ignore[attr-defined]
        return ("appletd: running=%s seq=%d age=%.0fms hands=%d errors=%s"
                % (current.running,         # type: ignore[attr-defined]
                   frame.seq,
                   current.age_ms(),        # type: ignore[attr-defined]
                   sum(1 for hand in frame.hands if hand.found),
                   current.errors or "none"))   # type: ignore[attr-defined]
    except Exception as exc:                # noqa: BLE001 - a status line must never throw
        return "appletd: status unavailable (%r)" % (exc,)
