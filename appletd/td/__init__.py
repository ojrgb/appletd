"""The TouchDesigner layer. Imports TD; NEVER imports pyobjc.

Two modules, and the split matters:

    bootstrap.py    sys.path wiring and the engine's start/stop lifecycle.
                    Run from a Text DAT, by hand or from a Startup DAT.
    hands_chop.py   the Script CHOP callbacks. Runs on TD's main thread, every
                    frame, and must never block.

Neither imports `Vision`, `AVFoundation`, `objc` or anything else from pyobjc -
that boundary is enforced by `tests/test_boundaries.py`, not by good intentions.
They reach the camera through `appletd.source`, which is itself pyobjc-free
at module scope and pulls the engine in lazily. So this layer can be read,
reasoned about and tested with no AVFoundation in the process at all
(DESIGN.md 5).
"""
