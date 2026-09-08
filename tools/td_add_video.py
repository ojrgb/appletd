#!/usr/bin/env python
"""The camera image as a TOP, and the overlays composited onto it.

    RUN IT
        run("<repo>/tools/td_add_video.py")

    or through the chain: tools/td_rebuild.py, layer "video".

WHAT IT ADDS
    video_in     a Video Device In TOP, following the `Camera` choice
    video_over   an Over TOP - the overlay render over the camera image
    outvideo     the COMP's fourth output

WHY A SECOND CAMERA CLIENT. The sidecar already has the device open; this opens it
again, in TouchDesigner. macOS has allowed several clients on one camera since Ventura,
and it is the only way to SEE the image - the sidecar sends landmarks, never pixels,
and shipping frames over the wire would cost more than the whole attribute layer.

It is gated on `Output Video` precisely because it is a second client: off, nothing is
opened, and a project that only wants channels pays nothing.

ONLY ONE OF THE TWO IS THE MEASUREMENT. The landmarks come from the SIDECAR's frames,
not from `video_in`, so the two are not synchronised and the overlay can lag the image
by a frame. That is a display artefact and not a tracking error - anything that needs
them aligned should read the channels, not the picture.

Ref: docs/BUILD_PLAN.md step 29.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_PATH = "/project1/appletd"

# The menu token `Camera` holds for "whatever the sidecar would pick on its own", and
# the substring that actually means. `CAMERA_DEFAULT` is duplicated from
# tools/td_build_vision.py, which owns the menu.
CAMERA_DEFAULT = "__default__"

VIDEO_IN = "video_in"
SOURCE = "video_source"
FLIP = "video_flip"
OVER = "video_over"
OUT = "outvideo"


def main():
    import td

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    for stale in [n for n in list(sys.modules)
                  if n == "appletd" or n.startswith("appletd.")]:
        del sys.modules[stale]
    from appletd.streams import DEFAULT_CAMERA_NAME
    from appletd.td_layout import OUTPUT_ORDER, master_xy

    comp = op(MASTER_PATH)
    if comp is None:
        print("no COMP at %s - run tools/td_build_vision.py first" % MASTER_PATH)
        return

    print("=" * 70)
    print("video: the camera image on %s" % comp.path)
    print("=" * 70)

    # -- the device ---------------------------------------------------------
    video = comp.op(VIDEO_IN) or comp.create(td.videodeviceinTOP, VIDEO_IN)
    video.nodeX, video.nodeY = master_xy(VIDEO_IN)
    # OPENED ONLY WHEN ASKED. This is a second client on the camera, so a project
    # that wants channels and no picture must not pay for it - and must not hold a
    # device open that something else might want.
    # OPENED ONLY WHEN IT IS THE SOURCE. `Output Video` on its own is not enough:
    # in TOP Input mode the picture comes from the component's image input and this
    # device would be held open for nothing - a second client on a camera nobody is
    # looking at, and a permission prompt on a machine that never wanted one.
    video.par.active.expr = ("op.Appletd.par.Outputvideo.eval() and "
                             "op.Appletd.par.Inputmode.eval() == 'camera'")
    # The same device the sidecar was told to use - but NOT by name, and this is the
    # trap. A Video Device In TOP's `device` menu shows the device NAME and stores an
    # opaque token:
    #
    #     V1|||6C707041-05AC-0010-0008-000000000001|||0|||0|||MacBook Pro Camera
    #
    # so writing "MacBook Pro Camera" into it selects nothing and the TOP reports
    # "Device not found". The label is only the last field, and the UUID in the middle
    # is what actually identifies the device.
    #
    # So the expression LOOKS THE NAME UP: the first menu entry whose LABEL contains
    # the wanted name, and its token from `menuNames`. An expression rather than a
    # callback because the device list repopulates when something is plugged in, and
    # an expression re-evaluates then.
    #
    # A SUBSTRING, matching what `--camera` has always taken, and that is what makes
    # `(default)` work: it resolves to `DEFAULT_CAMERA_NAME`, the same substring the
    # sidecar falls back to with no `--camera`. Both sides therefore pick the same
    # device, which is the property that matters - the overlay is drawn on this
    # picture, so a picture from a different camera than the tracking is worse than
    # no picture.
    #
    # `''` ONLY when nothing matches. An empty device opens nothing: MEASURED, the
    # TOP goes to a blank 128x128 and reports no error at all. It does NOT mean "let
    # the engine pick", which is what this comment claimed until `(default)` was
    # tried and the video output went empty.
    video.par.device.expr = (
        "next((token for token, label in zip(me.par.device.menuNames,"
        " me.par.device.menuLabels)"
        " if (op.Appletd.par.Camera.eval() if op.Appletd.par.Camera.eval() != %r"
        " else %r) in label), '')" % (CAMERA_DEFAULT, DEFAULT_CAMERA_NAME))

    # -- which picture ------------------------------------------------------
    # `Input Mode` decides, and the Switch is what makes `Output Video` mean the same
    # thing in both modes: the image the SIDECAR IS LOOKING AT. In TOP Input mode
    # that is the component's own image input, which is also what an overlay has to
    # be drawn over - landmarks found in a TOP composited onto a camera frame would
    # sit wherever the two happened to disagree.
    #
    # Input 0 is the camera and 1 the frames, so the index is simply the mode.
    source = comp.op(SOURCE) or comp.create(td.switchTOP, SOURCE)
    source.nodeX, source.nodeY = master_xy(SOURCE)
    source.par.index.expr = "op.Appletd.par.Inputmode.eval() != 'camera'"
    source.inputConnectors[0].connect(video)
    frames = comp.op("in_frames")
    if frames is not None:
        source.inputConnectors[1].connect(frames)

    # -- the mirror ---------------------------------------------------------
    # THE ONLY IMAGE THAT NEEDS ONE, and that is worth stating because the obvious
    # reading is that every output does.
    #
    # `Camera Flip` reaches Vision as an image ORIENTATION on the request, and
    # Vision returns everything in the oriented space - the landmarks, the
    # segmentation mask, the depth map, the flow field. MEASURED on a
    # fixture frame: the hand's mean x went 0.784 -> 0.228, and the depth map came
    # back 8.2x closer to the mirror of the original than to the original. So a
    # Flip TOP on any of THOSE would undo the flip rather than apply it.
    #
    # This picture is different: TouchDesigner opens the camera itself, as a second
    # client, and this frame never goes near Vision. It is the one thing left to
    # mirror, and without it the overlay would sit on an unmirrored image.
    flip = comp.op(FLIP) or comp.create(td.flipTOP, FLIP)
    flip.nodeX, flip.nodeY = master_xy(FLIP)
    flip.inputConnectors[0].connect(source)
    flip.par.flipx.expr = "op.Appletd.par.Cameraflip"

    # -- the composite ------------------------------------------------------
    # Input 1 is the FOREGROUND. `tools/td_add_overlay.py` connects the overlay
    # render there; with nothing connected this passes the camera through, which is
    # what `Output Video` on its own should do.
    over = comp.op(OVER) or comp.create(td.overTOP, OVER)
    over.nodeX, over.nodeY = master_xy(OVER)
    over.inputConnectors[1].connect(flip)

    # -- the output ---------------------------------------------------------
    out = comp.op(OUT) or comp.create(td.outTOP, OUT)
    out.nodeX, out.nodeY = master_xy(OUT)
    out.inputConnectors[0].connect(over)
    # LAST connector, and `apply_output_visibility` removes from the end inwards, so
    # this being 3 is what keeps the other three from renumbering when it goes.
    out.par.connectorder = OUTPUT_ORDER[OUT]

    print("   %-11s active <- Output Video and Input Mode = Camera" % VIDEO_IN)
    print("   %-11s index <- Input Mode (camera, or the COMP's image input)" % SOURCE)
    print("   %-11s flipx <- Camera Flip (the ONE image Vision did not mirror)"
          % FLIP)
    print("   %-11s overlay over the image (foreground added by the overlay layer)"
          % OVER)
    print("   %-11s connector %d" % (OUT, int(out.par.connectorder.eval())))
    print()
    print("   verified. Nothing opens the camera until `Output Video` is on.")


main()
