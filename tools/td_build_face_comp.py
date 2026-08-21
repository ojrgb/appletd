#!/usr/bin/env python
"""Build the `visionface` COMP and its OSC In CHOP. Paste, Run Script. Idempotent.

    WHAT IT BUILDS
        /project1/visionface_osc    OSC In CHOP on the FACE port (10002)
        /project1/visionface        the COMP: in1 -> merge_out -> out1, 23 channels

    RUN IT AFTER tools/td_build_comp.py, and drive it with either:
        the sidecar, with the face stream enabled:
            Sidecar page -> Face Stream on -> Start Sidecar
        or no camera at all:
            ~/.venvs/visionhands/bin/python tools/send_synthetic_face.py turn

    23 CHANNELS, NOT 327, AND THAT IS ON PURPOSE. What is published is the
    observation's own numbers - found, confidence, capture quality, roll, yaw,
    pitch and the bounding box. The 76 LANDMARK POINTS are not, because their split
    across the 12 regions cannot be known before a face has been seen, and this repo
    does not ship guessed constants. Run tools/probe_face_regions.py once, paste the
    counts into visionhands/face_types.py, re-run this, and the points appear.

WHY A THIRD PORT AND A THIRD COMP rather than more channels on an existing one.
TouchDesigner's OSC In CHOP puts every channel it receives into ONE CHOP, and the
`visionhands` COMP passes its whole input through to `merge_out` - so a shared port
would put the face channels inside another COMP's output. The alternative is a
prefix Select at each COMP's input, and prefix patterns have already failed to
partition these channels once (`h?_*_x` matches both a raw landmark and a derived
one, docs/BUILD_PLAN.md step 3). One port per stream costs one extra operator and
cannot go wrong that way. DESIGN.md 6.4.

WHAT IS DELIBERATELY NOT HERE. No derived attributes, no filtering, no temporal
channels: this is the plumbing, which is what was asked for. The shell is built
the same shape as `visionhands` - one Merge feeding one Out - so a future group
builder attaches itself to `merge_out` exactly as the hand groups do, rather than
having to restructure anything first.

THE PORT AND THE CHANNEL LIST COME FROM THE PACKAGE, not from constants typed
again here. `visionhands/streams.py` owns the port and `visionhands/face_types.py`
owns the channel names, so the receiver cannot end up listening on a port the sender
does not use - which fails silently, as an OSC In CHOP with no channels and no
error.

Ref: DESIGN.md 6.4 (the stream contract), 2.12 (the face API surface, measured),
     docs/BUILD_PLAN.md step 5.
"""

import sys

REPO_ROOT = "/Users/omer/Documents/GitHub/visionhands-touchdesigner"
PARENT_PATH = "/project1"
COMP_NAME = "visionface"
OSC_CHOP_NAME = "visionface_osc"

# The BASE port, matching tools/td_build_comp.py's OSC_PORT. The face port is
# derived from it by the package, not by adding 2 here.
BASE_PORT = 10000


def main():
    import td

    print("=" * 70)
    print("visionface: building the face COMP")
    print("=" * 70)

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    # TouchDesigner caches imported modules for the life of the process, so a
    # builder run after an edit would otherwise use the previous version -
    # silently (DESIGN.md 2.11). Purge before importing.
    for name in [n for n in sys.modules if n.startswith("visionhands")]:
        del sys.modules[name]
    from visionhands.face_types import (
        LANDMARKS_PUBLISHED,
        N_FACE_CHANNELS,
        face_channel_names,
    )
    from visionhands.streams import STREAM_FACE, port_for

    face_port = port_for(STREAM_FACE, BASE_PORT)

    parent = op(PARENT_PATH)
    if parent is None:
        print("FAIL no operator at %s" % PARENT_PATH)
        return

    # -- the OSC In CHOP ---------------------------------------------------
    osc = parent.op(OSC_CHOP_NAME)
    if osc is None:
        osc = parent.create(td.oscinCHOP, OSC_CHOP_NAME)
        print("1. created %s" % osc.path)
    else:
        print("1. reusing %s" % osc.path)
    osc.par.port = face_port
    osc.par.active = True
    osc.nodeX, osc.nodeY = 0, -700
    print("   listening on port %d (base %d + %d)"
          % (face_port, BASE_PORT, face_port - BASE_PORT))

    # -- the COMP ----------------------------------------------------------
    comp = parent.op(COMP_NAME)
    if comp is None:
        comp = parent.create(td.baseCOMP, COMP_NAME)
        print("2. created %s" % comp.path)
    else:
        # Contents cleared rather than the COMP destroyed: destroying it would
        # orphan anything a project has already wired to its output, and a
        # dangling input cannot be told apart from one that was never connected
        # (DESIGN.md 2.11).
        for child in list(comp.children):
            child.destroy()
        print("2. reusing %s (contents cleared)" % comp.path)
    comp.nodeX, comp.nodeY = 250, -700
    comp.color = (0.45, 0.35, 0.45)

    def make(kind, name, x, y):
        node = comp.create(kind, name)
        node.nodeX, node.nodeY = x, y
        return node

    chop_in = make(td.inCHOP, "in1", -400, 0)
    # A Merge with one input today, so that whatever is added later attaches to
    # it instead of restructuring the COMP - the same shape as `visionhands`.
    merge = make(td.mergeCHOP, "merge_out", -150, 0)
    merge.inputConnectors[0].connect(chop_in)
    chop_out = make(td.outCHOP, "out1", 100, 0)
    chop_out.inputConnectors[0].connect(merge)

    comp.inputConnectors[0].connect(osc)
    print("3. wired %s -> %s" % (osc.path, comp.path))

    comp.cook(force=True)
    print()
    print("4. %s: %d channels (contract says %d)"
          % (chop_out.path, chop_out.numChans, N_FACE_CHANNELS))

    names = [c.name for c in chop_out.chans()]
    if not names:
        print("   ZERO channels, which means nothing is SENDING yet. The OSC In")
        print("   CHOP creates channels as they arrive, so this is what an idle")
        print("   port looks like - not a fault. Start a sender:")
        print("     Sidecar page -> Face Stream on -> Start Sidecar")
        print("     or: ~/.venvs/visionhands/bin/python tools/send_synthetic_face.py turn")
    else:
        missing = [n for n in face_channel_names() if n not in names]
        extra = [n for n in names if n not in face_channel_names()]
        for probe in ("face_n", "face_seq", "f0_yaw", "f0_roll", "f0_pitch",
                      "f0_bbox_x", "f0_bbox_w", "f0_quality", "f1_found"):
            channel = chop_out.chan(probe)
            print("   %-16s %s" % (probe, "MISSING" if channel is None
                                   else round(channel[0], 4)))
        if missing:
            print("   MISSING %d contract channels: %s" % (len(missing), missing[:6]))
        if extra:
            # Almost certainly another sender on this port, which interleaves
            # into one set of channels and looks like data rather than a fault.
            print("   UNEXPECTED %d channels not in the contract: %s"
                  % (len(extra), extra[:6]))
        if not missing and not extra:
            print()
            print("   verified: all %d face channels present and named."
                  % N_FACE_CHANNELS)
            print("   Reference the head's turn as:  op('%s')['f0_yaw']" % comp.path)

    print()
    print("   ROLL, YAW and PITCH are DEGREES here. Vision reports radians;")
    print("   the conversion happens once, in visionhands/face.py.")
    print("   bbox_y is the BOTTOM edge of the box, not the top (DESIGN.md 7).")
    print("   GATE ON f<i>_quality rather than f<i>_score: both are UNMEASURED for")
    print("   faces (DESIGN.md 2.12), and quality is the one Apple documents as a")
    print("   comparison metric.")
    print("   f0 is the LEFTMOST face, by a stateless spatial sort - two faces")
    print("   that cross over exchange slots. DESIGN.md 6.4.")
    if not LANDMARKS_PUBLISHED:
        print("")
        print("   LANDMARK POINTS ARE NOT IN THIS CONTRACT YET. The 12 regions'")
        print("   point counts are unmeasured, and a guessed count lays one")
        print("   region's points into another's channels while looking fine.")
        print("   Settle it with, from the repo root:")
        print("     ~/.venvs/visionhands/bin/python tools/probe_face_regions.py")
    print("=" * 70)


main()
