#!/usr/bin/env python
"""Build the `visionpose` COMP and its OSC In CHOP. Paste, Run Script. Idempotent.

    WHAT IT BUILDS
        /project1/visionpose_osc    OSC In CHOP on the POSE port (10001)
        /project1/visionpose        the COMP: in1 -> merge_out -> out1, 123 channels

    RUN IT AFTER tools/td_build_comp.py, and drive it with either:
        the sidecar, with body pose enabled:
            Sidecar page -> Body Pose Stream on -> Start Sidecar
        or no camera at all:
            ~/.venvs/visionhands/bin/python tools/send_synthetic_pose.py wave

WHY A SECOND PORT AND A SECOND COMP rather than more channels on the hands one.
TouchDesigner's OSC In CHOP puts every channel it receives into ONE CHOP, and the
`visionhands` COMP passes its whole input through to `merge_out` - so a shared port
would put 123 pose channels inside the hands COMP's output. The alternative is a
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
again here. `visionhands/streams.py` owns the port and `visionhands/pose_types.py`
owns the 123 names, so the receiver cannot end up listening on a port the sender
does not use - which fails silently, as an OSC In CHOP with no channels and no
error.

Ref: DESIGN.md 6.4 (the stream contract), 2.12 (the body-pose API surface),
     docs/BUILD_PLAN.md step 5.
"""

import sys

REPO_ROOT = "/Users/omer/Documents/GitHub/visionhands-touchdesigner"
PARENT_PATH = "/project1"
COMP_NAME = "visionpose"
OSC_CHOP_NAME = "visionpose_osc"

# The BASE port, matching tools/td_build_comp.py's OSC_PORT. The pose port is
# derived from it by the package, not by adding 1 here.
BASE_PORT = 10000


def main():
    import td

    print("=" * 70)
    print("visionpose: building the body-pose COMP")
    print("=" * 70)

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    # TouchDesigner caches imported modules for the life of the process, so a
    # builder run after an edit would otherwise use the previous version -
    # silently (DESIGN.md 2.11). Purge before importing.
    for name in [n for n in sys.modules if n.startswith("visionhands")]:
        del sys.modules[name]
    from visionhands.pose_types import N_POSE_CHANNELS, pose_channel_names
    from visionhands.streams import STREAM_POSE, port_for

    pose_port = port_for(STREAM_POSE, BASE_PORT)

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
    osc.par.port = pose_port
    osc.par.active = True
    osc.nodeX, osc.nodeY = 0, -400
    print("   listening on port %d (base %d + %d)"
          % (pose_port, BASE_PORT, pose_port - BASE_PORT))

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
    comp.nodeX, comp.nodeY = 250, -400
    comp.color = (0.3, 0.4, 0.5)

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
          % (chop_out.path, chop_out.numChans, N_POSE_CHANNELS))

    names = [c.name for c in chop_out.chans()]
    if not names:
        print("   ZERO channels, which means nothing is SENDING yet. The OSC In")
        print("   CHOP creates channels as they arrive, so this is what an idle")
        print("   port looks like - not a fault. Start a sender:")
        print("     Sidecar page -> Body Pose Stream on -> Start Sidecar")
        print("     or: ~/.venvs/visionhands/bin/python tools/send_synthetic_pose.py wave")
    else:
        missing = [n for n in pose_channel_names() if n not in names]
        extra = [n for n in names if n not in pose_channel_names()]
        for probe in ("pose_n_bodies", "pose_seq", "p0_nose_x", "p0_nose_y",
                      "p0_left_wrist_y", "p0_conf_median", "p1_found"):
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
            print("   verified: all %d pose channels present and named."
                  % N_POSE_CHANNELS)
            print("   Reference a joint as:  op('%s')['p0_left_wrist_y']" % comp.path)

    print()
    print("   GATE ON p<i>_conf_median, never on p<i>_score: the body")
    print("   observation confidence is UNMEASURED (DESIGN.md 2.12) and hands'")
    print("   equivalent is a measured constant 1.0.")
    print("   p0 is the LEFTMOST person, by a stateless spatial sort - two people")
    print("   who cross over exchange slots. DESIGN.md 6.4.")
    print("=" * 70)


main()
