"""Where this checkout is, without anybody having to type it.

WHY THIS FILE EXISTS. Until 2026-08-23 fifteen files carried

    REPO_ROOT = "/Users/omer/Documents/GitHub/appletd"

as a literal, which meant the project opened on exactly one machine on earth. Not
untidiness - the `.toe` cannot run without the repo, because the generated DATs
import `appletd.derive` on every cook and the sidecar is launched as
`-m appletd.sidecar`, so a wrong path is a project that does not work at all.

THE ANSWER IS ALWAYS THE SAME and it needs no configuration: a file inside the
checkout knows where the checkout is, because it knows where IT is.

    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

That line is written out in each builder rather than imported from here, and that
is deliberate. A builder needs the repo root BEFORE it can put the repo on
`sys.path`, so it cannot import anything from the repo to find out - including this
module. The one-liner is the whole mechanism; this file is where the reasoning
lives, and `repo_root()` below is for the things that CAN import it.

WHERE `__file__` COMES FROM, in each of the three ways a builder runs:

  * from the shell - Python sets it.
  * from TouchDesigner's `run()` on a path - TouchDesigner sets it.
  * from `tools/td_rebuild.py`, which execs each builder in a fresh namespace and
    sets `namespace["__file__"] = path` itself, precisely so this works.

If it is ever missing, something is exec'ing a builder's TEXT without saying where
that text came from, and the fix is at the caller.

WHAT IS STILL A LITERAL, and is a different problem: `SIDECAR_PYTHON` in
tools/td_build_vision.py. An interpreter path cannot be derived from anything -
there is no relationship between where this code lives and which Python has pyobjc
installed. It is a parameter on the Advanced page, and docs/BUILD_PLAN.md step 21
is where making it choose itself is designed.

Ref: docs/BUILD_PLAN.md step 21, docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import os


def repo_root() -> str:
    """The checkout root, derived from this file's location.

    For code that can already import from the repo - the probes, the tests, tools
    run from a shell. A builder cannot use this (see the module docstring) and
    writes the one-liner out instead.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

