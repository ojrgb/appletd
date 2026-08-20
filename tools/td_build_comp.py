#!/usr/bin/env python
"""Build the visionhands COMP: TD-ready coordinate spaces from the OSC channels.

    HOW TO USE
      Paste into a Text DAT in TouchDesigner, right-click > "Run Script".
      It builds /project1/visionhands and wires the OSC In CHOP into it.
      Idempotent: run it as often as you like.

WHAT IT PRODUCES, and what it deliberately does NOT.

Joint NAMING is not done here. The sidecar sends `h0_index_tip_x` directly,
because naming belongs at the single source of truth rather than in a lookup two
hops downstream. An earlier version of this COMP renamed `h0_lm08_x` with a
Rename CHOP; that works - including the name-reference trick of feeding it a
Table DAT through a DAT to CHOP, which is verified - but it renames BY POSITION,
so the mapping would silently depend on channel order matching, decided
somewhere else. So the wire carries readable names and this COMP does only the
thing that genuinely belongs in TouchDesigner: coordinate spaces.

    in                    out
    h0_index_tip_x   ->   h0_index_tip_x     unchanged: normalised 0..1, bottom-left
                          h0_index_tip_tx    centred, aspect-corrected
                          h0_index_tip_px    pixels

Everything else - the frame scalars, the per-hand scalars, every `_conf` -
passes through untouched.

WHY THREE SPACES rather than picking one. They are each a one-operator branch and
cost nothing, and which one you want depends entirely on what you hook up:

    _x, _y    normalised 0..1 with the origin BOTTOM-LEFT. This is Vision's own
              space and it is already what TouchDesigner's UVs use, so it maps
              straight onto a TOP without conversion (DESIGN.md 7). Use it for
              texture lookups and for anything expecting UVs.
    _tx, _ty  centred on zero, x scaled by the aspect ratio. Drop these into a
              Geometry COMP's translate or an instancing setup and a fingertip
              lands where the fingertip is, without stretching - which plain
              normalised coordinates would do on a 16:9 image.
    _px, _py  pixels, for hit-testing against a TOP's resolution or for anything
              that thinks in pixels. Still bottom-left, like TD's TOPs.

NO PYTHON RUNS IN A COOK. Every operator here is a built-in doing built-in work;
the joint-name mapping is generated once, at build time, into a Rename CHOP's
parameters. The only Python is in two parameter expressions that read the COMP's
own resolution parameters, which TouchDesigner evaluates itself.

Built against TouchDesigner 099 / Python 3.11.15 on macOS.
"""

PARENT_PATH = "/project1"
COMP_NAME = "visionhands"
SOURCE_CHOP = "visionhands_osc"

# Defaults for the aspect and pixel conversions. MEASURED: the sidecar requests
# and receives 1280x720 (DESIGN.md 2.6), and Vision is fastest there.
#
# These are COMP parameters rather than baked-in numbers so a different camera
# does not need a rebuild. They are NOT read from the incoming channels because
# the wire contract deliberately does not carry width and height - adding them
# would be a contract change to solve something a parameter already solves.
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720


# The rename approach that is no longer needed, recorded because the knowledge
# cost something to get: TouchDesigner's Rename CHOP DOES support wildcard
# back-references, so `*_lm08_*` -> `*_index_tip_*` renames h0 and h1 and _x,
# _y and _conf from one rule (verified live). What it does NOT do is apply 21
# such rules pairwise - a space-separated From/To list applies the first
# replacement to everything the whole From list matches, which turned all 126
# landmark channels into `h0_wrist_x`, `h0_wrist_x1`, `h0_wrist_x2`... The
# working alternative is the second input, a name reference built from a Table
# DAT through a DAT to CHOP (its DAT arrives by PARAMETER, not by a wire - a
# DAT to CHOP has no CHOP input connector), but that renames by position.


def main():
    import td

    print("=" * 70)
    print("visionhands: building the translation COMP")
    print("=" * 70)

    parent = op(PARENT_PATH)
    if parent is None:
        print("FAIL no operator at %s" % PARENT_PATH)
        return

    comp = parent.op(COMP_NAME)
    if comp is None:
        comp = parent.create(td.baseCOMP, COMP_NAME)
        print("1. created %s" % comp.path)
    else:
        # Rebuild the insides from scratch rather than patching them, so running
        # this twice cannot leave a half-old network behind.
        for child in list(comp.children):
            child.destroy()
        print("1. reusing %s (contents cleared)" % comp.path)
    comp.nodeX, comp.nodeY = 250, 0

    # -- custom parameters -------------------------------------------------
    page = comp.appendCustomPage("Visionhands")
    par_w = page.appendInt("Resw", label="Source Width")[0]
    par_h = page.appendInt("Resh", label="Source Height")[0]
    par_w.default = par_w.val = DEFAULT_WIDTH
    par_h.default = par_h.val = DEFAULT_HEIGHT
    print("2. parameters: Resw=%d Resh=%d" % (par_w.eval(), par_h.eval()))

    def make(kind, name, x, y):
        node = comp.create(kind, name)
        node.nodeX, node.nodeY = x, y
        return node

    # -- the network -------------------------------------------------------
    chop_in = make(td.inCHOP, "in1", -600, 0)

    print("3. no rename stage: the sidecar already sends readable joint names")

    # Branches. Each is Select -> Math -> Rename, which is the whole trick:
    # pick the channels, do the arithmetic, give the result its own suffix.
    def branch(label, select_pattern, suffix, pre_offset, mult_expr, y):
        select = make(td.selectCHOP, "sel_%s" % label, -200, y)
        select.inputConnectors[0].connect(chop_in)
        select.par.channames = select_pattern

        math = make(td.mathCHOP, "math_%s" % label, 0, y)
        math.inputConnectors[0].connect(select)
        # Math CHOP applies pre-offset, then multiply. So (v - 0.5) * aspect is
        # exactly preoff=-0.5, mult=aspect - no expression gymnastics needed.
        math.par.preoff = pre_offset
        math.par.gain.expr = mult_expr

        rename = make(td.renameCHOP, "ren_%s" % label, 200, y)
        rename.inputConnectors[0].connect(math)
        rename.par.renamefrom = select_pattern
        rename.par.renameto = select_pattern.replace("_x", "_%s" % suffix).replace(
            "_y", "_%s" % suffix)
        return rename

    # Centred and aspect-corrected: ready for a Geometry COMP translate.
    tx = branch("tx", "*_x", "tx", -0.5, "me.parent().par.Resw / me.parent().par.Resh", -150)
    ty = branch("ty", "*_y", "ty", -0.5, "1.0", -300)
    # Pixels.
    px = branch("px", "*_x", "px", 0.0, "me.parent().par.Resw", -450)
    py = branch("py", "*_y", "py", 0.0, "me.parent().par.Resh", -600)

    merge = make(td.mergeCHOP, "merge_out", 450, 0)
    for index, source in enumerate((chop_in, tx, ty, px, py)):
        merge.inputConnectors[index].connect(source)

    chop_out = make(td.outCHOP, "out1", 650, 0)
    chop_out.inputConnectors[0].connect(merge)
    print("4. built %d operators" % len(comp.children))

    # -- wire the OSC CHOP in ---------------------------------------------
    source = parent.op(SOURCE_CHOP)
    if source is None:
        print("5. no %s to wire up - run tools/td_setup_osc.py first" % SOURCE_CHOP)
    else:
        comp.inputConnectors[0].connect(source)
        print("5. wired %s -> %s" % (source.path, comp.path))

    comp.cook(force=True)
    out_chop = comp.op("out1")
    print()
    print("6. output: %d channels" % out_chop.numChans)
    names = [c.name for c in out_chop.chans()]
    for probe in ("h0_wrist_x", "h0_index_tip_x", "h0_index_tip_tx",
                  "h0_index_tip_px", "h0_index_tip_conf", "n_hands", "age_ms"):
        mark = "ok " if probe in names else "MISSING"
        value = ""
        channel = out_chop.chan(probe)
        if channel is not None:
            value = " = %.4f" % channel[0]
        print("   %-8s %s%s" % (mark, probe, value))
    print()
    print("   Reference a fingertip as:  op('%s')['h0_index_tip_tx']" % comp.path)
    print("=" * 70)


main()
