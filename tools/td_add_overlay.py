#!/usr/bin/env python
"""The overlay renders: a skeleton per stream, drawn over the camera image.

    RUN IT
        run("<repo>/tools/td_add_overlay.py")

    or through the chain: tools/td_rebuild.py, layer "overlay".

WHAT IT BUILDS

    overlay/                     one COMP, so everything renders together
      in1                        every channel, in world space, before the trim
      hands  pose  face          a Geo COMP each, rendered while its toggle is on
        <mode>                   a Geo COMP per Overlay Mode, one rendered at a time
          in1 -> select1 -> shuffle1 -> chopto1 -> delete1 -> null1 -> geo1
      cam1                       ortho, matched to `Orthowidth`
      render1  out1              -> video_over, over the camera image

HOW A SKELETON IS DRAWN. `appletd/skeletons.py` owns which points are joined; every
list in here is derived from it, so the Select CHOP's channels, the CHOP to SOP's
scope and the Delete SOP's primitives cannot drift apart. The short version: the
Select names each segment's two endpoints, X first then Y; the Shuffle folds those
two halves into 2 channels x N samples; `CHOP to SOP` joins consecutive points, which
draws twice as many lines as wanted; the Delete removes the ones that jump between
chains. That module's docstring has the rest.

WHAT DECIDES WHAT RENDERS. Two expressions, no callback:

    hands.render          op.Appletd.par.Handsoverlay
    hands/<mode>.render   op.Appletd.par.Handsoverlaymode.eval() == '<mode>'

so `Show Overlay` turns a stream's geometry on and `Overlay Mode` picks which one of
its children draws. `render1` renders `*`, every Geo COMP in here, which is why an
unselected mode has to say no for itself. `allowCooking` still follows the toggle -
see `apply_overlay_gating` - so an overlay that is off costs nothing either.

THIS BUILDER OWNS THESE OPERATORS and rewrites their parameters on every run. The
network came from Omer's own `hands_skeleton` and was generalised from it; a colour
or a camera changed by hand will not survive a rebuild, so change it here.

Ref: docs/BUILD_PLAN.md step 30.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_PATH = "/project1/appletd"

OVERLAY = "overlay"

# The camera the overlays render through. Orthographic and pulled back along +Z, so
# the world coordinates land where `coords` put them.
CAM = "cam1"
RENDER = "render1"
# `_tx`/`_ty` are `(normalised - 0.5) * Orthowidth`, so a camera showing exactly that
# range has to BE `Orthowidth` wide. It happens to be 1.0 today, and the two would
# drift silently the moment somebody changed it - an overlay quietly the wrong size
# against the image it is drawn on.
CAM_ORTHO_EXPR = "op.Appletd.par.Orthowidth"
# The render matches the IMAGE it is composited over, not the component's
# `Renderw`/`Renderh` - those read `render1` back, and using them here would be a loop.
#
# `video_source` and NOT `video_in`: in TOP Input mode the camera is not opened at
# all, so its width is 0 and a render sized from it is 0x0. The Switch is whichever
# picture is live.
RENDER_W_EXPR = "op('../video_source').width"
RENDER_H_EXPR = "op('../video_source').height"


def _overlays():
    """The table, built here because the connection lists come from the package.

    (stream COMP, toggle, stream toggle, mode menu, page, modes), where each mode is
    (menu name, menu label, COMP name, channel prefixes, connections, line colour,
     point colour).

    COLOURS ARE PER STREAM so two overlays on at once can be told apart. Hands keep
    the red lines and green points they were drawn with.
    """
    from appletd.skeletons import (
        BODY_CONNECTIONS,
        FACE_KEYPOINT_CONNECTIONS,
        HAND_CONNECTIONS,
        INDEX_CONNECTIONS,
        face_landmark_connections,
    )
    hands = ("h0", "h1")
    return (
        ("hands", "Handsoverlay", "Streamhands", "Handsoverlaymode", "Hands", (
            ("fingers", "Finger Skeleton", "hands_skeleton", hands,
             HAND_CONNECTIONS, (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            ("indexline", "Index Line", "hands_index", hands,
             INDEX_CONNECTIONS, (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        )),
        ("pose", "Poseoverlay", "Streampose", "Poseoverlaymode", "Body Pose", (
            ("skeleton", "Pose Skeleton", "pose_skeleton", ("p0", "p1"),
             BODY_CONNECTIONS, (0.0, 0.7, 1.0), (1.0, 1.0, 0.0)),
        )),
        # TWO MODES, and the DEFAULT is the four key points rather than the 76
        # landmarks. `Facekeypoints` ships ON and strips the landmark channels at the
        # stream, so a landmark overlay selects nothing in a default project - which
        # is what it did. The landmark mode is still here for a project that has
        # turned that toggle off and wants all of them.
        ("face", "Faceoverlay", "Streamface", "Faceoverlaymode", "Face", (
            ("keypoints", "Key Points", "face_keypoints", ("f0", "f1"),
             FACE_KEYPOINT_CONNECTIONS, (1.0, 0.9, 0.2), (1.0, 0.4, 0.0)),
            ("landmarks", "All Landmarks  (needs Face Key Points off)",
             "face_landmarks", ("f0", "f1"),
             face_landmark_connections(), (1.0, 0.9, 0.2), (1.0, 0.4, 0.0)),
        )),
    )


# Where each operator goes inside a mode COMP. Omer's layout, kept.
SKELETON_XY = {
    "in1": (-600, 275), "select1": (-400, 275), "shuffle1": (-250, 275),
    "chopto1": (-100, 275), "delete1": (50, 275), "null1": (225, 275),
    "line1": (225, 150), "geo1": (450, 275),
}


# What this builder cannot be built without. Checked by name at the top of `main()`
# rather than assumed - see the gate there for why it is not a version comparison.
REQUIRED_OP_TYPES = ("choptoPOP", "deletePOP", "nullPOP", "inPOP", "outPOP",
                     "lineMAT")


def main():
    import td

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    for stale in [n for n in list(sys.modules)
                  if n == "appletd" or n.startswith("appletd.")]:
        del sys.modules[stale]
    from appletd.td_layout import ensure, keep_layout, master_xy
    from appletd.td_pages import LAYOUT

    comp = op(MASTER_PATH)
    if comp is None:
        print("no COMP at %s - run tools/td_build_vision.py first" % MASTER_PATH)
        return

    # ONE call, so `Keeplayout` reaches every operator this builder places.
    # Twenty-odd of them wrote nodeX/nodeY straight from the table, so
    # tidying the master network and switching the parameter on lasted
    # exactly until the next rebuild.
    keep = keep_layout(comp)

    # THE BUILD GATE, and it asks the real question rather than reading a version
    # string. The overlay is drawn with POPs, which arrived in TouchDesigner 2025, and
    # on anything older every `td.choptoPOP` here is an AttributeError halfway through
    # a build - a half-made overlay COMP and a traceback that names a missing
    # attribute rather than a missing feature.
    #
    # `app.version` is "099" on the build this was written against, so it answers
    # nothing. `app.build` is a string to parse, and a parse is a second thing to get
    # wrong. Whether the operator type EXISTS is the actual question.
    missing = [name for name in REQUIRED_OP_TYPES if not hasattr(td, name)]
    if missing:
        print("FAIL this TouchDesigner has no %s." % ", ".join(missing))
        print("     The overlay is drawn with POPs, which need TouchDesigner 2025 "
              "or newer. Everything else in appletd works without them - the "
              "channels, the mask, depth and flow are all unaffected.")
        return

    print("=" * 70)
    print("overlay: skeletons and controls on %s" % comp.path)
    print("=" * 70)

    overlays = _overlays()
    pages = {page.name: page for page in comp.customPages}
    existing = {par.name: par for par in comp.customPars}

    for _inner, toggle, _stream, mode_name, page_name, modes in overlays:
        page = pages.get(page_name)
        if page is None:
            # CREATED, not refused. "Body Pose" and "Face" do not exist until
            # `td_add_pages.py` moves `Streampose` and `Streamface` onto them, and
            # that has to run AFTER this one or the two parameters below are appended
            # after the sort and land at the bottom of the page. So on a first-ever
            # chain run this printed one FAIL, returned, and built no overlay at all -
            # while `td_rebuild.py` reported every builder ran. A second run fixed it,
            # which is why it was never noticed from a checkout that had already been
            # built once.
            #
            # The name is still owned in one place: `td_pages.LAYOUT` is the authority
            # on which pages exist, and `test_overlay_pages_are_real_pages` holds this
            # table against it.
            if page_name not in LAYOUT:
                print("   FAIL %r is not a page in appletd/td_pages.py LAYOUT, so "
                      "creating it here would make a page nothing ever sorts"
                      % page_name)
                return
            print("   %s page did not exist yet - created" % page_name)
            page = comp.appendCustomPage(page_name)
            pages[page_name] = page
        if toggle not in existing:
            par = page.appendToggle(toggle, label="Show Overlay")[0]
            par.default = False
        if mode_name not in existing:
            menu = page.appendMenu(mode_name, label="Overlay Mode")[0]
        else:
            menu = existing[mode_name]
        # THE OPTIONS ARE REWRITTEN EVERY RUN, and only the options. They are derived
        # from the table above, so a mode added or renamed there has to reach a
        # parameter that ALREADY EXISTS - appending only on creation left the Face
        # page offering one stale entry after two modes were defined, and a menu that
        # cannot name a mode is a control that cannot select it.
        menu.menuNames = [mode[0] for mode in modes]
        menu.menuLabels = [mode[1] for mode in modes]
        menu.default = modes[0][0]
        # And if the stored value names a mode that no longer exists, it selects
        # nothing: fall back to the default rather than leave the panel on a dead
        # entry that renders no overlay at all.
        if menu.eval() not in menu.menuNames:
            print("   %-22s was %r, no longer a mode - reset to %r"
                  % (mode_name, menu.eval(), modes[0][0]))
            menu.val = modes[0][0]

    # -- the COMP everything renders in -------------------------------------
    group = ensure(comp, td.baseCOMP, OVERLAY, master_xy(OVERLAY), keep)
    group.color = (0.35, 0.45, 0.5)

    # `coords`, and the choice is the whole reason the overlay works in world units.
    #
    # It is DOWNSTREAM of `coords`, so `h0_index_tip_tx` exists here - the world
    # coordinate, already scaled by `Orthowidth` and centred, which is what a render
    # wants. `merge_streams` was the obvious tap and is the wrong one: it sits
    # UPSTREAM of `coords`, carries only the raw normalised `_x`/`_y`, and would make
    # every overlay redo the transform the component already does once.
    #
    # And it is UPSTREAM of `screen_only` and `trim_empty`, so what the user's output
    # toggles remove is still here: `Screen Space Only` cannot delete the raw
    # channels out from under an overlay, and `Fingertipsonly` cannot take the joints
    # - tools/td_add_groups.py holds that toggle back at `early_trim` while a hand
    # overlay is on, precisely so this input carries all 21.
    feed = comp.op("coords") or comp.op("merge_streams")
    inside = group.op("in1") or group.create(td.inCHOP, "in1")
    inside.nodeX, inside.nodeY = (-400, 0)
    if feed is not None:
        # A COMP is connected by its output CONNECTOR - `coords` is a baseCOMP, where
        # `merge_streams` was a CHOP and could be passed directly.
        group.inputConnectors[0].connect(
            feed.outputConnectors[0] if feed.isCOMP else feed)

    # -- one Geo per stream, one Geo per mode inside it ----------------------
    built = []
    for index, (inner, toggle, stream_toggle,
                 mode_name, _page, modes) in enumerate(overlays):
        stream = _ensure_geo(td, group, inner, (-150, 200 - index * 200))
        if stream is None:
            print("   SKIP %-14s exists, is not a Geo, and has things inside it"
                  % inner)
            continue
        stream.color = (0.5, 0.45, 0.35)
        # The stream's own toggle AND the stream itself. `Show Overlay` on with
        # `Hands` off is not an overlay: the channels are frozen at their last
        # value, so it would draw a hand that is no longer being tracked.
        #
        # `.eval()` on both, explicitly. `parA and parB` returns the second PAR
        # OBJECT rather than a truth value, and an object is always true - the veto
        # would silently never fire.
        shown = ("op.Appletd.par.%s.eval() and op.Appletd.par.%s.eval()"
                 % (toggle, stream_toggle))
        stream.par.render.expr = shown
        stream.par.display.expr = shown

        # The In CHOP FIRST: a COMP has no input connector until something inside it
        # asks for one, so creating this is what makes the next line possible.
        feed_in = stream.op("in1") or stream.create(td.inCHOP, "in1")
        feed_in.nodeX, feed_in.nodeY = (50, 75)
        stream.inputConnectors[0].connect(inside)

        # RETIRE a mode that has left the table. Removing an entry above does not
        # remove the COMP it built: it stays inside the stream, `render1` still finds
        # it by `*`, and its render expression tests a mode name the menu no longer
        # offers - so it is invisible, cooking, and impossible to select. Same
        # mechanism as RETIRED_PARS for parameters.
        wanted = {mode[2] for mode in modes} | {"in1"}
        for child in list(stream.children):
            if child.name not in wanted:
                print("   %-22s retired (no longer a mode)"
                      % (inner + "/" + child.name))
                child.destroy()

        for mode_index, mode in enumerate(modes):
            name, _label, comp_name, prefixes, connections, line_rgb, point_rgb = mode
            child = _ensure_geo(td, stream, comp_name,
                                (250, 50 - mode_index * 200))
            if child is None:
                print("   SKIP %-14s exists and is not a Geo" % comp_name)
                continue
            # ONE mode draws, and EVERY renderable says so for itself.
            #
            # `render1`'s geometry is `*`, and that pattern resolves RECURSIVELY -
            # it names `hands/hands_skeleton/geo1` directly, not only through its
            # parents. So a geo cannot rely on an ancestor's render flag to hide it;
            # the full condition goes on each one that actually holds geometry.
            test = "%s and op.Appletd.par.%s.eval() == %r" % (shown, mode_name, name)
            child.par.render.expr = test
            child.par.display.expr = test
            # BUILD FIRST, then connect: the skeleton's own `in1` is what gives this
            # COMP an input connector to attach to.
            segments = _build_skeleton(td, child, prefixes, connections,
                                       line_rgb, point_rgb, test)
            child.inputConnectors[0].connect(feed_in)
            built.append((inner, comp_name, segments))

    # -- the camera and the render ------------------------------------------
    cam = group.op(CAM) or group.create(td.cameraCOMP, CAM)
    cam.nodeX, cam.nodeY = (400, 175)
    cam.par.tx, cam.par.ty, cam.par.tz = 0.0, 0.0, 5.0
    cam.par.projection = "ortho"
    cam.par.orthowidth.expr = CAM_ORTHO_EXPR

    render = group.op(RENDER) or group.create(td.renderTOP, RENDER)
    render.nodeX, render.nodeY = (0, 0)
    render.par.camera = CAM
    render.par.resolutionw.expr = RENDER_W_EXPR
    render.par.resolutionh.expr = RENDER_H_EXPR

    # -- the composite ------------------------------------------------------
    out = group.op("out1") or group.create(td.outTOP, "out1")
    out.nodeX, out.nodeY = (250, 0)
    out.inputConnectors[0].connect(render)
    over = comp.op("video_over")
    if over is not None:
        # `inputConnectors[0]` is the FOREGROUND of an Over TOP - the overlay above
        # the image. `tools/td_add_video.py` puts the camera on `[1]`, the
        # background. Indices rather than "Input 1", which reads as either depending
        # on whether you are looking at Python or at the parameter dialog.
        #
        # `group.outputConnectors[0]` and NOT `group`: a COMP is connected by its
        # output CONNECTOR, and passing the COMP itself raises "Invalid number or
        # type of arguments". Third time in this project (DESIGN.md 2.11).
        over.inputConnectors[0].connect(group.outputConnectors[0])

    apply_overlay_gating(comp)

    print("   %-22s <- %s (world space, untrimmed)"
          % (OVERLAY + "/in1", "coords" if feed is None else feed.name))
    for inner, comp_name, segments in built:
        print("   %-22s %3d segments" % (inner + "/" + comp_name, segments))
    print("   %-22s ortho, width <- Orthowidth" % (OVERLAY + "/" + CAM))
    print("   %-22s -> out1 -> video_over foreground" % (OVERLAY + "/" + RENDER))
    print()
    print("   Show Overlay renders a stream; Overlay Mode picks which child draws.")


def _ensure_geo(td, parent, name, xy):
    """A Geo COMP called `name` inside `parent`. Returns it, or None to leave alone.

    A placeholder base COMP from an earlier build is REPLACED, because a base cannot
    render and `render1` would simply not see it. One that has things inside it is
    somebody's work and is left where it is, with the caller reporting it - a builder
    that silently destroys a network is worse than one that refuses.
    """
    existing = parent.op(name)
    if existing is not None and existing.OPType != "geometryCOMP":
        if [child for child in existing.children if child.name != "in1"]:
            return None
        existing.destroy()
        existing = None
    node = existing or parent.create(td.geometryCOMP, name)
    node.nodeX, node.nodeY = xy
    # A fresh Geo COMP arrives with a torus in it.
    torus = node.op("torus1")
    if torus is not None:
        torus.destroy()
    return node


def _build_skeleton(td, comp, prefixes, connections, line_rgb, point_rgb, shown):
    """The seven operators that turn channels into a drawn skeleton. Returns segments.

    Every list here comes from `appletd.skeletons`, which is the point: the Select's
    channels, the CHOP to SOP's scope and the Delete's primitives are three views of
    one table and are generated together.
    """
    from appletd.skeletons import channel_names, jump_primitives

    names = channel_names(prefixes, connections)
    jumps = jump_primitives(prefixes, connections)

    def place(kind, name):
        # THE TYPE IS CHECKED, not just the name. These are POPs - TouchDesigner's
        # point-operator family - and `choptoSOP` is a DIFFERENT operator with a
        # different parameter set that happens to answer to the same `.type`. A
        # leftover of the wrong family would take the name and then reject every
        # parameter set on it.
        node = comp.op(name)
        if node is not None and node.OPType != kind.OPType:
            node.destroy()
            node = None
        node = node or comp.create(kind, name)
        node.nodeX, node.nodeY = SKELETON_XY[name]
        return node

    feed = place(td.inCHOP, "in1")

    # THE CHANNELS, in draw order. A Select emits in the order its list names and
    # RENAMES repeats - `h0_wrist_tx`, `h0_wrist_tx1` - which is harmless: everything
    # downstream works by position, and a skeleton shares points on purpose.
    select = place(td.selectCHOP, "select1")
    select.par.channames = " ".join(names)
    select.inputConnectors[0].connect(feed)

    # X and Y into two channels of N samples. `numChans/2` rather than a literal, so
    # a changed connection table cannot leave this behind.
    shuffle = place(td.shuffleCHOP, "shuffle1")
    shuffle.par.method = "seqn"
    shuffle.par.nval.expr = "me.inputs[0].numChans/2"
    shuffle.par.firstsample = True
    shuffle.inputConnectors[0].connect(select)

    # The two channels become P.x and P.y. Named by the FIRST channel of each half,
    # which is what the Shuffle calls its outputs.
    half = len(names) // 2
    chopto = place(td.choptoPOP, "chopto1")
    chopto.par.chop = shuffle.name
    chopto.par.surftype = "lines"
    chopto.par.chanscope = "%s %s" % (names[0], names[half])
    chopto.par.attrscope = "P.x P.y"

    # The lines the skeleton did not ask for - see appletd/skeletons.py.
    delete = place(td.deletePOP, "delete1")
    delete.par.entity = "primitive"
    delete.par.pattern0pattern = "[%s]" % ",".join(str(n) for n in jumps)
    delete.inputConnectors[0].connect(chopto)

    null = place(td.nullPOP, "null1")
    null.inputConnectors[0].connect(delete)

    material = place(td.lineMAT, "line1")
    material.par.linenearcolorr = line_rgb[0]
    material.par.linenearcolorg = line_rgb[1]
    material.par.linenearcolorb = line_rgb[2]
    material.par.specifylinefarcolor = False
    material.par.drawpoints = True
    material.par.pointsizemultiplier = 3.0
    material.par.pointnearcolorr = point_rgb[0]
    material.par.pointnearcolorg = point_rgb[1]
    material.par.pointnearcolorb = point_rgb[2]
    material.par.specifypointfarcolor = False

    # The renderable object: the geometry from `null1`, wearing the line material.
    geo = comp.op("geo1")
    if geo is not None and geo.OPType != "geometryCOMP":
        geo.destroy()
        geo = None
    geo = geo or comp.create(td.geometryCOMP, "geo1")
    geo.nodeX, geo.nodeY = SKELETON_XY["geo1"]
    torus = geo.op("torus1")
    if torus is not None:
        torus.destroy()
    if geo.op("in1") is None:
        inner_in = geo.create(td.inPOP, "in1")
        inner_in.nodeX, inner_in.nodeY = (-200, 0)
        inner_in.render = True
        inner_in.display = True
        inner_out = geo.create(td.outPOP, "out1")
        inner_out.nodeX, inner_out.nodeY = (0, 0)
        inner_out.inputConnectors[0].connect(inner_in)
    geo.par.material = material.name
    # The same condition as the COMP above it - see the note at the call site. This
    # is the operator that actually carries geometry, so this is the one that has to
    # be right if `render1`'s `*` reaches past its parents.
    geo.par.render.expr = shown
    geo.par.display.expr = shown
    geo.inputConnectors[0].connect(null)
    return len(connections) * len(prefixes)


def apply_overlay_gating(comp):
    """`allowCooking` for the group and each inner COMP.

    An attribute rather than a parameter, so it cannot be bound to an expression and
    has to be rewritten when a toggle moves - the same arrangement the attribute
    groups use (tools/td_add_groups.py). The render FLAGS are expressions and need
    none of this; only cooking does.
    """
    group = comp.op(OVERLAY)
    if group is None:
        return
    # `Active` vetoes every overlay, like every attribute group: with capture off the
    # channels hold their last values, so a cooking overlay re-renders the same
    # skeleton for ever. See `apply_overlay` in tools/td_build_vision.py.
    live = getattr(comp.par, "Active", None)
    capturing = live is None or bool(live.eval())
    wanted = False
    for inner, toggle, stream_toggle, _mode, _page, _modes in _overlays():
        par = getattr(comp.par, toggle, None)
        stream = getattr(comp.par, stream_toggle, None)
        # A missing parameter means "leave it alone", so an absent stream toggle
        # does not veto - the same rule the rest of the component uses.
        on = bool(capturing and par is not None and par.eval()
                  and (stream is None or stream.eval()))
        wanted = wanted or on
        child = group.op(inner)
        if child is not None:
            child.allowCooking = on
    group.allowCooking = wanted


main()
