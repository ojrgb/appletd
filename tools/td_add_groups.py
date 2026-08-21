#!/usr/bin/env python
"""Add the Attributes page - the group toggles - to the visionhands COMP.

    Paste into a Text DAT, Run Script. Idempotent.

WHAT THE TOGGLES ACTUALLY DO, because it is less than docs/ATTRIBUTES.md's step 3
describes and the difference is deliberate.

**They gate the compute that costs something.** `visionhands/derive.py` takes the
set of enabled groups and computes only those, and `tools/td_add_derive.py`'s
`_groups()` reader has always read these parameters - it just fell back to "all
enabled" because they did not exist. So turning Descriptor off stops 84 channels
being computed in Python, and Contacts off stops 28, inside the one operator in
the whole COMP where per-channel cost is real: main-thread Python measured at
0.210 ms for 171 channels (DESIGN.md 2.7).

**AND THEY GATE COOKING on every group that has been wrapped in a COMP.**
`temporal` and `latches` since docs/BUILD_PLAN.md 7.2; `coords/world` and
`coords/pixels` in all three streams since 2026-08-21, which is what `Coordstx` and
`Coordspx` now do - they were "channels only" until the coords group was split into
two halves for them to freeze.

That change earned its keep on the face stream, where the landmark points gained
world and pixel companions: MEASURED 1.8628 ms per cook for `face/coords`, 696
output channels through two operators each. `Coordspx` ships off, so half of it is
frozen out of the box.

**A disabled DERIVE group's channels genuinely disappear** from the COMP's output,
because `derive()` simply does not emit them - measured, Verbosity = Minimal takes
`derive_chop` from 171 channels to 0 and the COMP output from 561 to 393. Nothing
is left stale there.

**A disabled NATIVE group's channels do NOT disappear.** They keep their last
value, whether the group is frozen by `allowCooking` or not gated at all. That is
deliberate rather than a gap: a channel that VANISHES breaks every reference to it
with no error anywhere (DESIGN.md 6.2), which is worse than a stale number on a
toggle somebody just turned off. The genuinely un-gated toggles are the remaining
`native` ones - `Landmarks`, `Triggers`, `Motion`, `Events` - and closing those needs
an exact channel-to-group map; see the note at the end of this file for why wildcard
patterns cannot provide one.

MEASURED, the `allowCooking` question step 3 could not proceed without: a Feedback
counter and a Cook Type = Always Null inside a base COMP, with `allowCooking` set
to False. The counter FROZE at 275 and its cook count stopped, and on re-enabling
it resumed at 276 rather than jumping. So:

  * disabling a group really does stop all of its cost, including its clock;
  * a group with memory PAUSES rather than resetting, so re-enabling one resumes
    with a `prev` from whenever it was switched off. For the latches that means the
    state is stale for one frame - self-correcting, because the recurrence is
    level-driven - and one edge pulse across the gap may be wrong. Bounded and
    acceptable, but it is why the clock must sit OUTSIDE any gated group if the
    native groups are ever wrapped.

Ref: docs/ATTRIBUTES.md (the group table and the budget), docs/BUILD_PLAN.md step
3, DESIGN.md 2.10 (why memory needs a clock).
"""

import sys

REPO_ROOT = "/Users/omer/Documents/GitHub/visionhands-touchdesigner"
# The master COMP holds every parameter; the hands STREAM holds this
# network. Both are named, because this script writes to both.
MASTER_PATH = "/project1/vision"
COMP_PATH = MASTER_PATH + "/hands"

# The groups, their defaults, and how each one's toggle reaches what it gates.
#
# `derive` means the group is computed by visionhands/derive.py, so the toggle
# reaches it through the existing `_groups()` reader and genuinely saves work.
# `native` means the group is built from CHOPs, where the toggle gates cost only if
# those CHOPs live in a COMP that `allowCooking` can freeze - which is what COOK_GATED
# below records. A `native` toggle that is NOT in COOK_GATED drives nothing, and its
# LABEL says so, so nobody switches one off expecting a saving that is not there.
GROUPS = (
    # name          default  gated
    ("Landmarks",   True,  "native"),
    ("Coordstx",    True,  "native"),
    # ON, and the default CHANGED on 2026-08-21 when this toggle stopped being
    # advisory. It used to ship off and gate nothing, so the `_px`/`_py` channels
    # were always live. Now off means FROZEN - the channels are still there, holding
    # whatever they last cooked, which is a plausible wrong number rather than a
    # visible failure. A toggle that can do that has to default to NOT doing it, and
    # turning it off has to be a deliberate saving. Same reasoning as
    # `Screenspaceonly`, which ships off because on deletes channels.
    ("Coordspx",    True,  "native"),
    # The FACE LANDMARK coordinate spaces, separate from the box's own because they
    # are two orders of magnitude bigger: 348 channels against 8, and MEASURED at
    # 1.2099 ms per cook against under a tenth of that. A project that wants a
    # face's bounding box in world space should not have to pay for 174 points.
    ("Lmcoordstx",  True,  "native"),
    # OFF, and unlike `Coordspx` that is safe: these 348 channels did not exist
    # before today, so nothing can be reading a value that is about to go stale.
    ("Lmcoordspx",  False, "native"),
    ("Core",        True,  "derive"),
    ("Presence",    True,  "derive"),
    ("Contacts",    True,  "derive"),
    ("Triggers",    True,  "native"),
    ("Pose",        True,  "derive"),
    ("Motion",      True,  "native"),
    ("Twohands",    True,  "derive"),
    ("Gestures",    True,  "derive"),
    ("Events",      True,  "native"),
    ("Descriptor",  False, "derive"),
)

# Which group COMP each toggle keeps alive, and therefore which toggles have to be
# OFF before that COMP can stop cooking.
#
# `allowCooking = False` on a base COMP stops everything inside it, including a
# Cook Type = Always clock - MEASURED, see the docstring above. So the saving is
# real, and so is the consequence: a group with memory PAUSES, and re-enabling it
# resumes with a `prev` from whenever it was switched off. One stale frame, and
# possibly one wrong edge pulse across the gap. Bounded, self-correcting (every
# recurrence here is level-driven), and written into docs/ATTRIBUTES.md beside the
# toggles rather than left to be discovered.
#
# `filter` is deliberately absent: it is in the DATA PATH, so disabling its cooking
# would freeze every position rather than bypass the filter. Its `Smoothing` toggle
# already gates its cost through the bypass flag (docs/BUILD_PLAN.md 7.2).
# Keyed by the path from the MASTER COMP, because the groups live inside a stream
# now: `hands/temporal`, not `temporal`. Written out rather than assembled, so the
# one place that has to change when a stream grows a gated group is this table.
COOK_GATED = {
    # THE WHOLE STREAM, on its own toggle. A disabled stream still receives a full
    # datagram of zeros every frame - deliberately, because a channel that stops
    # arriving VANISHES and breaks its consumers silently (DESIGN.md 6.2) - and
    # until now the network went on processing those zeros. Freezing the child COMP
    # stops all of it and keeps every channel, holding zero.
    #
    # MEASURED in the default configuration, where `Streampose` and `Streamface`
    # both ship OFF: pose cost 0.2456 ms and face 1.4525 ms per cook, computing
    # coordinate spaces for people who are not there.
    #
    # NOTE the one confusing consequence, and it is worth the trade: these toggles
    # are LAUNCH FLAGS for the sidecar and say "restart to apply", but freezing
    # takes effect immediately. Turning a stream on therefore stops the freeze at
    # once and starts the inference at the next restart. Freezing a stream nobody is
    # producing is right either way round.
    "hands": ("Streamhands",),
    "pose": ("Streampose",),
    "face": ("Streamface",),
    "hands/temporal": ("Presence", "Motion"),
    "hands/latches": ("Triggers", "Gestures", "Events"),
    # The coordinate spaces, split into a world half and a pixel half by
    # tools/td_add_coords.py precisely so these two toggles can freeze them. They
    # were labelled "channels only, gates no cost yet" from the day they were added;
    # they gate real cost now.
    #
    # MEASURED, and it is why this was worth doing: the FACE stream's coords group
    # cost 1.8628 ms per cook once the landmark points gained world and pixel
    # companions - 696 output channels through two operators each, at about 1.2
    # microseconds per channel per operator, which is more than everything else in
    # the COMP put together. `Coordspx` ships OFF, so a project that only wants
    # world coordinates does not pay for pixel ones.
    "hands/coords/world": ("Coordstx",),
    "hands/coords/pixels": ("Coordspx",),
    "pose/coords/world": ("Coordstx",),
    "pose/coords/pixels": ("Coordspx",),
    "face/coords/world": ("Coordstx",),
    "face/coords/pixels": ("Coordspx",),
    # Only the face has box-relative channels, so only the face has these two.
    "face/coords/lm_world": ("Lmcoordstx",),
    "face/coords/lm_pixels": ("Lmcoordspx",),
    # And only HANDS has an attribute layer, so only hands has coordinate branches
    # reading `derive_chop` and `temporal` - palm, pinch, the two centres, and the
    # velocity channels. Same toggles as the wire-contract halves, because they are
    # the same spaces; separate COMPs because they read a different input.
    "hands/coords/dv_world": ("Coordstx",),
    "hands/coords/dv_pixels": ("Coordspx",),
}

# `latches` READS `temporal`'s second output - the liveness/presence gate that
# arms every latch. So `temporal` must keep cooking whenever `latches` does, or the
# latches would gate on a FROZEN `ready`: frozen at 1 they keep firing with stale
# liveness, frozen at 0 they never fire again, and neither says anything. The
# dependency is a wire in the network (docs/BUILD_PLAN.md 7.3) and this is the same
# dependency expressed in the gating.
COOK_REQUIRES = {"hands/latches": ("hands/temporal",)}

# The presets from docs/ATTRIBUTES.md. Verbosity sets the toggles in one click; it
# is not a fourth state, and moving a toggle afterwards does not fight it.
PRESETS = {
    "Minimal": ("Landmarks", "Coordstx"),
    "Interaction": tuple(n for n, _d, _g in GROUPS
                         if n not in ("Coordspx", "Lmcoordspx", "Descriptor")),
    "Everything": tuple(n for n, _d, _g in GROUPS),
}

CALLBACK_SOURCE = '''# Generated by tools/td_add_groups.py
#
# Verbosity is a preset button, not a fourth state: it writes the toggles once and
# then leaves them alone, so moving one afterwards does not fight it and the
# toggles remain the single source of truth for what is enabled.
PRESETS = %(presets)r


COOK_GATED = %(cook_gated)r
COOK_REQUIRES = %(cook_requires)r


def onValueChange(par, prev):
    comp = par.owner
    if par.name == "Verbosity":
        wanted = PRESETS.get(par.eval())
        if wanted is None:
            return
        for name in %(all_groups)r:
            target = getattr(comp.par, name, None)
            if target is not None:
                target.val = name in wanted
        # Setting the toggles above fires this callback again, once per toggle, so
        # the gating below is applied by those calls too. Applied here as well
        # because a preset that changed nothing fires no further callbacks.
    _apply_gating(comp)


def _apply_gating(comp):
    """Write `allowCooking` on each gated group from the toggles.

    A copy of the same function in tools/td_add_groups.py, which is where the
    reasoning lives. It is duplicated rather than imported because this DAT has to
    keep working with no repo on sys.path - a project someone opens on another
    machine still has to be able to switch a group off.
    """
    wanted = {}
    for group_name, toggles in COOK_GATED.items():
        wanted[group_name] = any(
            bool(getattr(comp.par, toggle).eval())
            for toggle in toggles if hasattr(comp.par, toggle))
    for group_name, needs in COOK_REQUIRES.items():
        if wanted.get(group_name):
            for needed in needs:
                wanted[needed] = True
    for group_name, enabled in wanted.items():
        group = comp.op(group_name)
        if group is None:
            continue
        out = group.op("out1")
        if not enabled and out is not None and out.numChans == 0:
            group.allowCooking = True
            out.cook(force=True)
        group.allowCooking = enabled
'''


def _apply_gating(comp, verbose=False):
    """Set `allowCooking` on each gated group COMP from the toggles. Returns a report.

    `allowCooking` is an ATTRIBUTE, not a parameter, so nothing can bind an
    expression to it - it has to be WRITTEN, here at build and again from the
    Parameter Execute DAT whenever one of the toggles changes. That is the same
    pattern `filter_callbacks` uses for the filter's bypass flag.

    A group is left cooking if ANY of its toggles is on, and additionally if
    anything that reads it is cooking - see COOK_REQUIRES.

    Traps this handles rather than discovers:
      * a COMP that has never cooked has no channels, so disabling one before its
        first cook would take its channels out of the COMP's output rather than
        freezing them. Each group is cooked once before it can be disabled.
      * writing `allowCooking = True` on a group that is already cooking is free,
        so this is idempotent and can run on every parameter change.
    """
    report = []
    wanted = {}
    for group_name, toggles in COOK_GATED.items():
        wanted[group_name] = any(
            bool(getattr(comp.par, toggle).eval())
            for toggle in toggles if hasattr(comp.par, toggle))
    # Second pass: anything a cooking group reads has to keep cooking too.
    for group_name, needs in COOK_REQUIRES.items():
        if wanted.get(group_name):
            for needed in needs:
                wanted[needed] = True

    for group_name, enabled in sorted(wanted.items()):
        group = comp.op(group_name)
        if group is None:
            report.append("%s: MISSING" % group_name)
            continue
        out = group.op("out1")
        if not enabled and out is not None and out.numChans == 0:
            # Never cooked: freeze it AFTER it has produced its channels once, or
            # they leave the COMP's output entirely instead of holding their last
            # value.
            group.allowCooking = True
            out.cook(force=True)
        group.allowCooking = enabled
        report.append("%s: %s" % (group_name, "cooking" if enabled else "FROZEN"))
        if verbose:
            print("   %-15s %-7s from %s" % (group_name,
                                            "on" if enabled else "off",
                                            ", ".join(COOK_GATED[group_name])))
    return report


def _page(comp, name="Attributes"):
    """The Attributes page. Created once; a tuned toggle survives a rebuild.

    Traps: `appendCustomPage` with an existing name makes a SECOND page of that
           name. And `appendToggle`/`appendMenu` on an existing parameter reset it,
           so the current values are read first and written back.
    """
    page = None
    for existing in comp.customPages:
        if existing.name == name:
            page = existing
            break
    if page is None:
        page = comp.appendCustomPage(name)

    known = {n for n, _d, _g in GROUPS}
    was = {p.name: p.eval() for p in comp.customPars
           if p.name in known or p.name == "Verbosity"}

    menu = page.appendMenu("Verbosity", label="Verbosity")[0]
    menu.menuNames = list(PRESETS)
    menu.menuLabels = list(PRESETS)
    menu.default = "Interaction"
    # ALWAYS written back, not only when the parameter is new - and this line is a
    # bug fix. MEASURED: `appendMenu` on an EXISTING parameter resets it, and a menu
    # resets to INDEX 0, which here is "Minimal". That is the recorded
    # append-clobbers-a-value trap (DESIGN.md 2.11) wearing a different costume: for
    # a menu the reset lands on the first entry rather than on zero-as-false.
    #
    # And it does not stay a one-parameter problem, because a Parameter Execute DAT
    # is watching: the reset FIRED the preset callback, which correctly turned every
    # derive group off, which took `derive_chop` to 0 channels, which left the latch
    # bank nothing to select - the COMP output fell from 499 to 381 and every latch
    # channel disappeared. All of it downstream of one menu quietly reverting.
    #
    # The toggles below are restored from `was` after this, so a preset firing here
    # cannot have the last word.
    menu.val = was.get("Verbosity", "Interaction")

    cost_gating = {name for names in COOK_GATED.values() for name in names}
    for group, default, gated in GROUPS:
        if gated == "derive" or group in cost_gating:
            suffix = ""
        else:
            suffix = "  (channels only)"
        par = page.appendToggle(group, label=group + suffix)[0]
        par.default = default
        # A toggle has no invalid value, so a stored one always wins - unlike a
        # threshold, where zero had to be treated as untuned.
        par.val = was.get(group, default)
    return page


def main():
    import td

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    # TouchDesigner caches our modules for the life of the process. DESIGN.md 2.11.
    for stale in [n for n in list(sys.modules)
                  if n == "visionhands" or n.startswith("visionhands.")]:
        del sys.modules[stale]
    from visionhands.derive import ALL_GROUPS

    master = op(MASTER_PATH)
    comp = op(COMP_PATH)
    if master is None or comp is None:
        print("FAIL no COMP at %s - run tools/td_build_vision.py first"
              % (MASTER_PATH if master is None else COMP_PATH))
        return

    # The page and the gating both belong to the MASTER: the toggles are controls,
    # and `allowCooking` is written on groups that live inside a stream.
    _page(master)
    print("1. Attributes page on %s:" % master.path)
    # What each toggle actually reaches, from the same two tables the LABELS are
    # built from - not from the `gated` marker alone, which was still printing
    # "gates no cost yet" for Coordstx and Coordspx after they started freezing
    # `coords/world` and `coords/pixels`. A report that contradicts the label it
    # sits beside is worse than no report.
    freezes = {}
    for group_name, toggles in COOK_GATED.items():
        for toggle in toggles:
            freezes.setdefault(toggle, []).append(group_name)
    for group, _default, gated in GROUPS:
        if gated == "derive":
            reach = "gates derive()"
        elif group in freezes:
            reach = "freezes " + ", ".join(sorted(freezes[group]))
        else:
            reach = "channels only, gates no cost yet"
        print("   %-12s %-3s  %s"
              % (group, "on" if getattr(master.par, group).eval() else "off", reach))
    print("   Verbosity = %s" % master.par.Verbosity.eval())

    # The preset callback. A Parameter Execute DAT rather than an expression on
    # each toggle: an expression would make the toggles read-only, and the point of
    # a preset is to SET them and then get out of the way.
    callbacks = master.op("groups_callbacks") or master.create(
        td.parameterexecuteDAT, "groups_callbacks")
    callbacks.nodeX, callbacks.nodeY = -400, 700
    callbacks.text = CALLBACK_SOURCE % {
        "presets": {k: list(v) for k, v in PRESETS.items()},
        "all_groups": [n for n, _d, _g in GROUPS],
        "cook_gated": {k: list(v) for k, v in COOK_GATED.items()},
        "cook_requires": {k: list(v) for k, v in COOK_REQUIRES.items()},
    }
    callbacks.par.op = master.path
    # Verbosity AND every toggle that gates a group's cooking: `allowCooking` is an
    # attribute, so it cannot be bound to a parameter and has to be rewritten on
    # change.
    gating_toggles = sorted({name for names in COOK_GATED.values() for name in names})
    callbacks.par.pars = " ".join(["Verbosity", *gating_toggles])
    callbacks.par.valuechange = True
    print("2. Verbosity presets and cook gating wired through %s (%s)"
          % (callbacks.name, callbacks.par.pars.eval()))

    print("3. cook gating:")
    _apply_gating(master, verbose=True)

    # -- does the toggle actually reach derive()? --------------------------
    # ALL_GROUPS is derive.py's own list, so this catches a toggle whose name does
    # not match the group it is meant to gate - which would fail silently, because
    # `_groups()` treats a missing parameter as "enabled".
    failures = []
    derive_gated = {g for g, _d, kind in GROUPS if kind == "derive"}
    expected = {g.capitalize() for g in ALL_GROUPS}
    if derive_gated != expected:
        failures.append("the derive-gated toggles are %r but derive.ALL_GROUPS "
                        "wants %r" % (sorted(derive_gated), sorted(expected)))

    chop = comp.op("derive_chop")
    if chop is not None:
        before = chop.numChans
        was_descriptor = master.par.Descriptor.eval()
        master.par.Descriptor = not was_descriptor
        chop.cook(force=True)
        after = chop.numChans
        master.par.Descriptor = was_descriptor
        chop.cook(force=True)
        print("4. proof the toggles bite: Descriptor flipped changed derive_chop "
              "from %d channels to %d" % (before, after))
        if before == after:
            failures.append("flipping Descriptor did not change derive_chop's "
                            "channel count - the toggle is not reaching derive()")

    # -- does a frozen group keep its channels? -----------------------------
    # The one thing about `allowCooking` that could break a project rather than
    # just save time: a COMP that is not cooking holds its last output, so the
    # channels must still be there - reading their last value - rather than
    # vanishing from the COMP's output and silently breaking every reference
    # (DESIGN.md 6.2). Checked by NAME, which is a same-frame-safe question, unlike
    # anything about cook counts.
    print("5. stream outputs:")
    for stream_name in sorted({name.split("/")[0] for name in COOK_GATED}):
        stream_out = master.op(stream_name + "/out1")
        if stream_out is not None:
            stream_out.cook(force=True)
            print("   %-6s %d channels" % (stream_name, stream_out.numChans))
    for group_name in sorted(COOK_GATED):
        # Relative to the MASTER: `hands/temporal` is not a child of the stream.
        group = master.op(group_name)
        group_out = None if group is None else group.op("out1")
        # Against ITS OWN stream's output. This used to compare every gated group
        # against the HANDS stream's out1, which was correct while only hands had
        # gated groups and reported four false failures the moment pose and face
        # got them: 356 face channels "missing" from a hands output that never
        # carried them.
        stream_out = master.op(group_name.split("/")[0] + "/out1")
        if group is None or group_out is None or stream_out is None:
            continue
        mine = {c.name for c in group_out.chans()}
        lost = sorted(mine - {c.name for c in stream_out.chans()})
        if lost:
            failures.append("`%s` is %s and %d of its channels are NOT on %s: %r"
                            % (group_name,
                               "cooking" if group.allowCooking else "frozen",
                               len(lost), stream_out.path, lost[:5]))
        print("   %-22s %-7s %4d channels, all on the output: %s"
              % (group_name, "cooking" if group.allowCooking else "FROZEN",
                 len(mine), not lost))

    if failures:
        print("\nFAILURES (%d):" % len(failures))
        for failure in failures:
            print("   " + failure)
    else:
        print("\nverified: every derive group has a toggle, and flipping one "
              "changes what derive() computes.")

    print("\nNOT DONE, deliberately, and what each needs:")
    print("  Trimming the output channel list for the NATIVE groups. A disabled")
    print("  derive group really does vanish from out1; the latch, trigger and")
    print("  motion channels keep their last value, because nothing gates them.")
    print("  docs/ATTRIBUTES.md suggests a Select whose channames is assembled")
    print("  from the enabled groups - but wildcard patterns CANNOT partition")
    print("  these channels: `h?_*_x` matches both the raw `h0_wrist_x` (Landmarks)")
    print("  and the derived `h0_palm_x` (Core), and `h?_e_*` matches both the")
    print("  Events pulses and the Triggers pulses. It needs an exact channel-to-")
    print("  group map, and the sound way to get one is a registry in the package:")
    print("  derive() can already report a group's channels by being called with")
    print("  just that group, and the latch table would move from")
    print("  tools/td_add_latches.py into visionhands/ so both can import it.")
    print("  That refactor is worth doing carefully rather than quickly.")
    print("")
    print("  NOTE what IS done now: `temporal` and `latches` are real group COMPs,")
    print("  so their toggles gate COOKING through allowCooking - the restructuring")
    print("  this note used to say was needed happened in docs/BUILD_PLAN.md 7.3.")
    print("  `filter` is still not a candidate: it is in the data path, and its")
    print("  Smoothing toggle gates its cost through the bypass flag instead.")


main()
