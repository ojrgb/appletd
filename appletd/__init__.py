"""appletd - Apple Vision hand landmarks into TouchDesigner, on macOS.

Layout and the boundaries that hold it together (DESIGN.md 5, STANDARDS.md 2):

    types.py    the data contract. Pure Python: no pyobjc, no TouchDesigner,
                no numpy. Both of the other layers import it, which is exactly
                why it may import neither of them.
    coords.py   every coordinate conversion, and nothing else.
    engine.py   AVFoundation + Vision -> LandmarkFrame. Imports pyobjc, never TD.
    source.py   the HandSource interface and the lock-free handoff.
    td/         Script CHOP callbacks. Imports TD, never pyobjc.

Nothing here imports anything outside this package except the standard library
and, in engine.py only, pyobjc. `cv2` and `PIL` are dev-only and must never be
imported at module scope anywhere in the package - TouchDesigner's bundled
Python does not have them, so a stray top-level import breaks the CHOP at cook
time rather than at development time.

TRAP: put the *parent* of this directory on sys.path, never this directory
itself. This package contains a module called `types`, and putting the package
directory directly on sys.path would shadow the standard library's `types`
module for the whole process - which, inside TouchDesigner, means breaking TD.
"""
