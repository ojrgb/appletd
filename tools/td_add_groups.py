#!/usr/bin/env python
"""Add the Attributes page - the group toggles - to the appletd COMP.

    Paste into a Text DAT, Run Script. Idempotent.

WHAT THE TOGGLES ACTUALLY DO, because it is less than docs/ATTRIBUTES.md's step 3
describes and the difference is deliberate.

**They gate the compute that costs something.** `appletd/derive.py` takes the
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
toggle somebody just turned off. Four toggles that could NOT be closed this way -
`Landmarks`, `Triggers`, `Motion`, `Events` - were removed on 2026-08-23 rather than
left on the page pretending: closing them needs an exact channel-to-group map, and
wildcards cannot provide one because `h?_*_x` matches both the raw `h0_wrist_x` and
the derived `h0_palm_x`. See RETIRED_PARS.

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
from fnmatch import fnmatchcase

REPO_ROOT = "/Users/omer/Documents/GitHub/appletd"
# The master COMP holds every parameter; the hands STREAM holds this
# network. Both are named, because this script writes to both.
MASTER_PATH = "/project1/vision"
COMP_PATH = MASTER_PATH + "/hands"

# The groups, their defaults, and how each one's toggle reaches what it gates.
#
# `derive` means the group is computed by appletd/derive.py, so the toggle
# reaches it through the existing `_groups()` reader and genuinely saves work.
# `native` means the group is built from CHOPs, where the toggle gates cost only if
# those CHOPs live in a COMP that `allowCooking` can freeze - which is what COOK_GATED
# below records. A `native` toggle that is NOT in COOK_GATED drives nothing, and its
# LABEL says so, so nobody switches one off expecting a saving that is not there.
GROUPS = (
    # name          default  gated
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
    # The two MASTER switches for the hands attribute layer, added 2026-08-21
    # because the existing toggles could not express "off". See COOK_VETOED.
    ("Temporal",    True,  "native"),
    ("Latches",     True,  "native"),
    ("Pose",        True,  "derive"),
    ("Twohands",    True,  "derive"),
    ("Gestures",    True,  "derive"),
    ("Descriptor",  False, "derive"),
    # 3D inferred from a 2D projection, added 2026-08-21. Both OFF: the numbers are
    # honest but uncalibrated, and `Palmarea`/`Zreference` on the Tuning page are what
    # calibrate them. See docs/ATTRIBUTES.md for what each one can and cannot claim -
    # in particular that neither can tell toward from away.
    ("Depth",       False, "derive"),
    ("Tilt",        False, "derive"),
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
    # are LAUNCH FLAGS for the sidecar, but freezing takes effect immediately.
    # Turning a stream on therefore stops the freeze at once and starts the
    # inference at the next restart. Freezing a stream nobody is producing is right
    # either way round. The labels no longer say "restart to apply" - `Capturestate`
    # says "Requires Restart" instead, which is one place rather than five.
    "hands": ("Streamhands",),
    "pose": ("Streampose",),
    "face": ("Streamface",),
    # `Motion` was a second term here and `Triggers`/`Events` on the line below,
    # removed 2026-08-23 with the rest of the advisory toggles. WHAT THAT COST, said
    # plainly: `Motion` on with `Presence` off was the only way to express "cook
    # temporal for its velocity half, not its presence half", and there is now no way
    # to say that - `Presence` off freezes the whole group. `Presence` ships on, so
    # the common case is unaffected, and putting the term back is one word here.
    #
    # The trade was made because a toggle that does nothing on its own and something
    # in combination is harder to explain than one capability fewer.
    "hands/temporal": ("Presence",),
    "hands/latches": ("Gestures",),
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

# A VETO: if any listed toggle is OFF, the group is frozen whatever COOK_GATED says.
#
# WHY a second mechanism rather than another entry in COOK_GATED. That table is an
# OR - "any of these toggles being on keeps the group alive" - which is right for
# `Presence` and `Motion`, because either one needs `temporal` cooking. It cannot
# express "off": adding `Temporal` to that list would make turning it ON keep the
# group alive, which is the opposite of a master switch. So:
#
#     cooking = any(COOK_GATED) and all(COOK_VETOED)
#
# THE INTERACTION THAT NEEDED A DECISION. `latches` reads `temporal`'s presence
# gate, and COOK_REQUIRES exists to force `temporal` to keep cooking whenever
# `latches` does - a frozen gate either arms every latch for ever or disarms them
# for ever, with nothing to say which. A veto on `Temporal` fights that. The veto
# WINS, and `latches` is frozen with it: the alternative is a latch bank running on
# a gate that stopped updating, which is the exact silent failure COOK_REQUIRES was
# written to prevent. `_apply_gating` prints when it does this.
COOK_VETOED = {
    "hands/temporal": ("Temporal",),
    "hands/latches": ("Latches", "Temporal"),
}


# >>> TRIM SCOPE - everything between these markers is copied VERBATIM into the
# generated Parameter Execute DAT (see _trim_source), so that the DAT and this
# module cannot drift. Keep it dependent on nothing but the standard library.
#
# The one Select CHOP in front of the master's single output, and the toggle that
# bypasses it. Both live on the MASTER COMP - tools/td_build_vision.py builds them,
# this script is the only thing that knows what to put IN the list.
TRIM_CHOP = "trim_empty"
TRIM_TOGGLE = "Deleteempty"
MERGE_CHOP = "merge_streams"

# Channels the output never carries, whatever the toggles say. Asked for by the user
# 2026-08-22, and the reason is the same one the trim exists for: a beginner reading
# the channel list should see the things they came for, not the housekeeping.
#
# `HOUSEKEEPING` goes to a Null CHOP inside the component instead - `housekeeping`,
# next to the output - so it is one click away rather than gone.
#
# `*_conf_median` USED TO BE EXEMPT, on the grounds that DESIGN.md 6.2 tells people
# to "gate on `h<i>_conf_median`, never on `found` alone", so taking the channel the
# documentation recommends off the output would be a trap. That reasoning stands and
# is why the hand scalars go to `housekeeping` rather than being dropped: the channel
# is still there to gate on, one click inside the COMP. It is off the OUTPUT because
# a beginner reading the list should not meet three quality scalars per hand before
# they meet a fingertip. The 80 per-joint `*_conf` channels are dropped outright -
# those nobody inspects.
PER_JOINT_CONF = ("*_conf",)
HOUSEKEEPING = ("sc_*", "seq", "*_seq", "age_ms", "*_age_ms")

# Per-hand identity and quality scalars. Off the OUTPUT 2026-08-23 by request, and
# routed to `housekeeping` rather than dropped, which is the point: DESIGN.md 6.2
# tells people to "gate on `h<i>_conf_median`, never on `found` alone", so the
# channel that documentation names has to stay reachable. One click inside the COMP
# instead of a line in the output list.
#
# `h?_` and not `*_`: this is a HANDS decision. Pose and face keep their own score.
HAND_SCALARS = ("h?_score", "h?_conf_median", "h?_chirality")

NEVER_ON_OUTPUT = PER_JOINT_CONF + HOUSEKEEPING + HAND_SCALARS
# What the `housekeeping` Null CHOP carries: everything the output does not, except
# the per-joint confidences - 80 channels of them, which is a list nobody inspects.
INSPECTABLE = HOUSEKEEPING + HAND_SCALARS

# Two OUTPUT-ONLY toggles, asked for 2026-08-23. Neither gates any cooking, and that
# is forced rather than lazy: the fifteen non-tip joints feed every curl, spread and
# angle `derive()` computes, and the bounding boxes feed `hands_overlap`. The work
# happens either way, so these remove second copies from the output - which is what
# somebody reading the channel list for the first time actually wants.
FINGERTIPS_TOGGLE = "Fingertipsonly"
HANDBOX_TOGGLE = "Handbox"

# The fifteen joints that are neither a fingertip nor the wrist. A LITERAL list,
# because everything between the TRIM SCOPE markers is copied verbatim into a DAT
# and may import nothing but the standard library. `_check_joint_split()` below the
# markers holds it against `appletd.types.JOINT_NAMES`, so it cannot drift.
#
# THE WRIST IS KEPT. It is not an mcp, pip or dip, it is the hand's anchor, and
# `hands_angle` is measured from it - so "fingertips only" leaves six points per
# hand rather than five.
NON_TIP_JOINTS = ("thumb_cmc", "thumb_mp", "thumb_ip",
                  "index_mcp", "index_pip", "index_dip",
                  "middle_mcp", "middle_pip", "middle_dip",
                  "ring_mcp", "ring_pip", "ring_dip",
                  "little_mcp", "little_pip", "little_dip")
HANDBOX_CHANNELS = ("h?_bbox_*", "h?_size")


def _trim_keep(comp, wanted):
    """The channels to KEEP on the single output, in merge order. Returns a list.

    Which channels to drop is read off the NETWORK - each group's `out1` is exactly
    the set of channels that group contributes - rather than from a channel-to-group
    map, which does not exist and would go stale the first time a builder added a
    channel. A frozen group still reports its channels, holding their last value,
    which is the whole reason this trim is needed: `Coordspx` off used to leave 100
    `_px`/`_py` channels on the output carrying a plausible wrong number.

    A KEEP list rather than the drop list this started as, because the operator is a
    Select and not a Delete CHOP - see tools/td_build_vision.py for the measurement.
    Two consequences:

      * it FAILS CLOSED. A channel nobody names is gone, so every attribute toggle
        has to rewrite this list, not just the ones that gate cooking. That is why
        `callbacks.par.pars` lists all of GROUPS and not just the gating subset.
      * a Select emits in `channames` ORDER, so this returns MERGE order and not
        sorted order, leaving the output identical to what the Delete produced.

    Literal names, not patterns: MEASURED, a Select naming 306 channels costs
    0.0555 ms where one pattern costs 0.0313, and compaction cannot beat that by
    enough to be worth a wildcard that could match a channel added later.

    NOT covered, and the labels already say so: `Landmarks`, `Triggers`, `Motion`
    and `Events` are advisory. Their channels come out of a group that IS cooking -
    `Motion` shares `temporal` with `Presence`, the other three share `latches` -
    so separating them needs the channel-to-group map this deliberately avoids.
    """
    drop = set()
    for group_name, enabled in wanted.items():
        if enabled:
            continue
        group = comp.op(group_name)
        out = None if group is None else group.op("out1")
        if out is None:
            continue
        drop.update(chan.name for chan in out.chans())
    merged = comp.op(MERGE_CHOP)
    if merged is None:
        return None
    names = [chan.name for chan in merged.chans()]
    # The per-joint confidences and the housekeeping, dropped whatever is frozen.
    # `*_conf_median` survives `*_conf` because fnmatch anchors at both ends.
    drop.update(name for name in names
                if any(fnmatchcase(name, pattern) for pattern in NEVER_ON_OUTPUT))
    # The two output-only toggles, read off the COMP the way `_apply_trim` reads
    # `Deleteempty`: a parameter that is not there yet means "leave it alone"
    # rather than an exception on a half-built network.
    optional = []
    tips = getattr(comp.par, FINGERTIPS_TOGGLE, None)
    if tips is not None and tips.eval():
        optional += ["h?_%s_*" % joint for joint in NON_TIP_JOINTS]
    box = getattr(comp.par, HANDBOX_TOGGLE, None)
    if box is not None and not box.eval():
        optional += list(HANDBOX_CHANNELS)
    if optional:
        drop.update(name for name in names
                    if any(fnmatchcase(name, pattern) for pattern in optional))
    if not drop.intersection(names):
        # Nothing to drop: BYPASS rather than name all 1,861 channels. An empty list
        # is free for a Delete CHOP but not for a Select.
        return None
    return [name for name in names if name not in drop]


def _housekeeping_names(comp):
    """The channels the output does not carry but somebody may want to look at.

    Read off the merge, so it is whatever is actually there rather than a list to
    keep in step. Returns [] when the merge is missing.
    """
    merged = comp.op(MERGE_CHOP)
    if merged is None:
        return []
    return [chan.name for chan in merged.chans()
            if any(fnmatchcase(chan.name, pattern) for pattern in INSPECTABLE)]
# <<< TRIM SCOPE


def _check_joint_split():
    """`NON_TIP_JOINTS` must be exactly the joints that are neither a tip nor the
    wrist.

    Why this exists: that list is a LITERAL because it lives inside the TRIM SCOPE
    markers, and everything in there is copied verbatim into a generated DAT that
    may import nothing but the standard library. A literal copy of a contract is a
    second source of truth, and the failure mode is silent - a joint added to
    `types.JOINT_NAMES` would simply never be trimmed, and `Fingertipsonly` would
    quietly leave it on the output. So the contract is checked here, at build, where
    it can raise.
    """
    from appletd.types import JOINT_NAMES
    expected = tuple(name for name in JOINT_NAMES
                     if name != "wrist" and not name.endswith("_tip"))
    if tuple(NON_TIP_JOINTS) != expected:
        raise RuntimeError(
            "NON_TIP_JOINTS has drifted from types.JOINT_NAMES.\n"
            "  literal:  %s\n  contract: %s"
            % (" ".join(NON_TIP_JOINTS), " ".join(expected)))


def _trim_source():
    """The TRIM SCOPE block of this file, for pasting into the generated DAT.

    Read off DISK rather than duplicated by hand, because the previous duplication in
    this file - `_apply_gating` - is a copy somebody has to remember to update, and
    this block is the part where a divergence would be silent: the DAT would write a
    delete list that disagreed with the one the builder wrote.
    """
    text = open(__file__ if "__file__" in globals()
                else REPO_ROOT + "/tools/td_add_groups.py").read()
    start = text.index("# >>> TRIM SCOPE")
    end = text.index("# <<< TRIM SCOPE")
    return text[start:end].rstrip() + "\n"

# The presets from docs/ATTRIBUTES.md. Verbosity sets the toggles in one click; it
# is not a fourth state, and moving a toggle afterwards does not fight it.
PRESETS = {
    # `Landmarks` was here until 2026-08-23. It gated nothing anywhere - not one
    # entry in COOK_GATED, COOK_VETOED or COOK_REQUIRES - so the raw landmark
    # channels were always on and "Minimal" is what it always actually was.
    "Minimal": ("Coordstx",),
    # Excluded from Interaction for the same reason each ships OFF: `Coordspx` and
    # `Lmcoordspx` are channel volume nobody asked for, `Descriptor` is 84 channels,
    # and `Depth`/`Tilt` are UNCALIBRATED - `Palmarea` and `Zreference` are still set
    # against synthetic geometry (BUILD_PLAN step 13).
    #
    # A preset must not contradict the defaults. `Verbosity` itself defaults to
    # "Interaction", so a preset that included these would turn them on the moment
    # anyone pressed the button their panel already claimed to be set to.
    "Interaction": tuple(n for n, _d, _g in GROUPS
                         if n not in ("Coordspx", "Lmcoordspx", "Descriptor",
                                      "Depth", "Tilt")),
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
COOK_VETOED = %(cook_vetoed)r

from fnmatch import fnmatchcase  # noqa: E402  - stdlib, no repo on sys.path needed

%(trim_source)s


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
    # A veto beats both passes above, COOK_REQUIRES included. tools/td_add_groups.py
    # says why: a latch bank gating on a frozen presence gate is the silent failure
    # COOK_REQUIRES exists to prevent, so `Temporal` off has to take `latches` with
    # it rather than being overridden.
    for group_name, vetoes in COOK_VETOED.items():
        if any(hasattr(comp.par, v) and not bool(getattr(comp.par, v).eval())
               for v in vetoes):
            wanted[group_name] = False
    for group_name, enabled in wanted.items():
        group = comp.op(group_name)
        if group is None:
            continue
        out = group.op("out1")
        if not enabled and out is not None:
            # Cook it, THEN freeze it - unconditionally. A frozen Out CHOP holds its
            # channels only until something upstream changes shape; after that it is
            # dirty, cannot cook, and reports ZERO, so the channels vanish instead of
            # holding. tools/td_add_groups.py has the measurement.
            group.allowCooking = True
            out.cook(force=True)
        group.allowCooking = enabled
    # LAST, after every allowCooking above: `_trim_keep` reads the groups' channels,
    # and a group cooked for the first time only has them once that has happened.
    trim = comp.op(TRIM_CHOP)
    if trim is not None:
        toggle = getattr(comp.par, TRIM_TOGGLE, None)
        on = True if toggle is None else bool(toggle.eval())
        keep = _trim_keep(comp, wanted)
        if keep is not None:
            trim.par.channames = " ".join(keep)
        trim.bypass = keep is None or not on
    house = comp.op("housekeeping_sel")
    if house is not None:
        house.par.channames = " ".join(_housekeeping_names(comp))
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
    # LAST pass, so a veto beats both of the above - including COOK_REQUIRES, which
    # would otherwise force a vetoed group back on. See the COOK_VETOED comment for
    # why the veto has to win.
    for group_name, vetoes in COOK_VETOED.items():
        blocked = [v for v in vetoes
                   if hasattr(comp.par, v) and not bool(getattr(comp.par, v).eval())]
        if blocked and wanted.get(group_name):
            report.append("%s: FROZEN by %s" % (group_name, ", ".join(blocked)))
        if blocked:
            wanted[group_name] = False

    for group_name, enabled in sorted(wanted.items()):
        group = comp.op(group_name)
        if group is None:
            report.append("%s: MISSING" % group_name)
            continue
        out = group.op("out1")
        if not enabled and out is not None:
            # Cook it, THEN freeze it. Unconditionally, not just when it has never
            # cooked - MEASURED 2026-08-22, and this was a silent hole: a frozen
            # group's Out CHOP holds its channels only until something upstream
            # changes SHAPE. After that it is dirty, it cannot cook because it is
            # frozen, and it reports zero channels - so the channels VANISH from the
            # component's output instead of holding their last value, which is
            # exactly the failure DESIGN.md 6.2 forbids. Reproduced by flipping
            # `Descriptor`, which changes derive_chop's channel count: `temporal`
            # went from 27 channels to 0 and `latches` from 70 to 0.
            #
            # The cost is one cook per frozen group per parameter change - the same
            # one cook re-enabling it would take, at human speed, not per frame.
            group.allowCooking = True
            out.cook(force=True)
        group.allowCooking = enabled
        report.append("%s: %s" % (group_name, "cooking" if enabled else "FROZEN"))
        if verbose:
            print("   %-15s %-7s from %s" % (group_name,
                                            "on" if enabled else "off",
                                            ", ".join(COOK_GATED[group_name])))

    # LAST, and after every `allowCooking` above, because `_trim_keep` reads the
    # groups' channels and a group that was just cooked for the first time only has
    # them once that has happened.
    report.extend(_apply_trim(comp, wanted, verbose=verbose))
    return report


def _apply_trim(comp, wanted, verbose=False):
    """Write the Select CHOP's keep list and bypass from the same `wanted` map.

    The list is a PARAMETER, so this could in principle be an expression - but the
    expression would have to walk every group on every cook, which is exactly the
    cost the gating exists to avoid. Written instead, from here at build and from the
    Parameter Execute DAT on every change, the same pattern `allowCooking` uses.
    """
    report = []
    trim = comp.op(TRIM_CHOP)
    if trim is None:
        return ["%s: MISSING" % TRIM_CHOP]
    toggle = getattr(comp.par, TRIM_TOGGLE, None)
    on = True if toggle is None else bool(toggle.eval())
    keep = _trim_keep(comp, wanted)
    # AN EMPTY KEEP LIST IS NOT "KEEP NOTHING". It means every group reported
    # disabled - which is what happens when all the `Stream*` toggles are off - and
    # writing "" to a Select that is not bypassed empties the output in total
    # silence: no error, no warning, `out1` at 0 channels and every consumer
    # downstream reading nothing.
    #
    # MEASURED on 2026-08-23, and it took four queries to find: two builder runs
    # aborted midway, which left every parameter in MOVED_PARS at 0 because those
    # are destroyed and re-appended on every build and the restore pass never ran.
    # The next build then faithfully restored the zeros, every stream read disabled,
    # and the trim dropped all 1,786 channels while reporting "0 of 1786 kept" as
    # though that were a normal result.
    #
    # So: bypass, and say why. A component with everything switched off should show
    # you everything frozen, not nothing at all.
    if keep is not None and not keep:
        report.append("%s: every group reports disabled - BYPASSED rather than "
                      "emitting nothing. Check the Stream toggles." % TRIM_CHOP)
        keep = None
    if keep is not None:
        trim.par.channames = " ".join(keep)
    trim.bypass = keep is None or not on
    house = comp.op("housekeeping_sel")
    if house is not None:
        house.par.channames = " ".join(_housekeeping_names(comp))
    merged = comp.op(MERGE_CHOP)
    total = 0 if merged is None else len(merged.chans())
    if keep is None:
        report.append("%s: BYPASSED, nothing to trim (%d channels out)"
                      % (TRIM_CHOP, total))
    elif not on:
        report.append("%s: BYPASSED by %s, %d of %d channels would be trimmed"
                      % (TRIM_CHOP, TRIM_TOGGLE, total - len(keep), total))
    else:
        report.append("%s: %d of %d channels kept"
                      % (TRIM_CHOP, len(keep), total))
    if house is not None:
        report.append("housekeeping: %d channels off the output, in `housekeeping`"
                      % len(house.par.channames.eval().split()))
    if verbose:
        # Every line, each labelled by its own operator - printing `report[-1]`
        # under the trim's name put the housekeeping count beside the wrong node.
        for line in report:
            name, _, detail = line.partition(": ")
            print("   %-15s %s" % (name, detail))
    return report


# Parameters this script used to create and must now DESTROY. Removing the code that
# creates a parameter does not remove the parameter - it survives in the .toe for ever,
# looking exactly like a working control (DESIGN.md 2.11). Same mechanism as
# RETIRED_PARS in tools/td_build_vision.py.
#
# All four were advisory: they appeared on the Attributes page and could not do what
# their names promised, because removing a group's channels from the output needs an
# exact channel-to-group map and wildcards cannot provide one - `h?_*_x` matches both
# the raw `h0_wrist_x` and the derived `h0_palm_x`.
#
# `Landmarks` gated nothing at all, anywhere. The other three were OR terms in
# COOK_GATED, so each did something in combination and nothing on its own, which is
# harder to explain to a beginner than one capability fewer. See COOK_GATED for what
# removing `Motion` specifically cost.
RETIRED_PARS = ("Landmarks", "Triggers", "Motion", "Events")

# name -> the label it should now have. See the note in `_page`: labels on existing
# parameters are left alone by default, so a rename has to be listed here or it only
# ever applies to somebody building the network from scratch.
RELABELLED = {
    # "Verbosity" is a word about the CODE. What the menu actually chooses is how
    # much the component computes, which is a level of detail.
    "Verbosity": "Level of Detail",
}


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

    # NEVER re-append a parameter that already exists. MEASURED: `appendMenu` on an
    # existing parameter RESETS it, and a menu resets to INDEX 0, which here is
    # "Minimal" - the append-clobbers-a-value trap (DESIGN.md 2.11) wearing a
    # different costume. Writing the value straight back afterwards is not enough,
    # and that is the 2026-08-22 fix: a Parameter Execute DAT is watching this menu,
    # its callbacks are DEFERRED to the end of the frame, and when two builders run
    # in the same frame the queued reset outlives the restore. The observed result
    # was `Verbosity` sitting on "Minimal" with every derive group off, so
    # `derive_chop` published 0 channels, `temporal` fell to 12 and `latches` to 45,
    # and the COMP output went from 505 channels to 366 - silently, from re-running
    # two builders that are each correct on their own.
    #
    # So: append only what is absent. Nothing that exists is touched except its
    # `default`, which is not a value. Labels on existing parameters are left alone
    # too - a rebuild must not overwrite a label somebody edited.
    # RETIRED FIRST, so a name that is being retired cannot also be relabelled or
    # counted as existing further down.
    for name in RETIRED_PARS:
        for par in list(comp.customPars):
            if par.name == name:
                print("   retired %s (was on the %s page)" % (name, par.page.name))
                par.destroy()

    existing = {par.name: par for par in comp.customPars}

    # DELIBERATE RELABELS. `_page` never touches an existing parameter's label,
    # for the good reason in the block above - a rebuild must not overwrite a label
    # somebody edited. That also means renaming one is impossible without saying so
    # explicitly, which is what this table is: name -> the label it should now have.
    # Applied to parameters that already exist, and only to these names.
    for name, label in RELABELLED.items():
        par = existing.get(name)
        if par is not None and par.label != label:
            print("   relabelled %s: %r -> %r" % (name, par.label, label))
            par.label = label

    # `Handbox` lives here rather than on Vision, where it was born on 2026-08-23:
    # it decides which attribute channels reach the output, which is exactly what
    # every other toggle on this page does. `td_build_vision.py` lists it in
    # MOVED_PARS and destroys the old one first, because TouchDesigner will not hold
    # one custom parameter name on two pages.
    if HANDBOX_TOGGLE not in existing:
        box = page.appendToggle(HANDBOX_TOGGLE, label="Hand Box and Size")[0]
        box.default = True
        box.val = True

    menu = existing.get("Verbosity")
    if menu is None:
        menu = page.appendMenu("Verbosity", label="Level of Detail")[0]
        menu.val = "Interaction"
    menu.menuNames = list(PRESETS)
    menu.menuLabels = list(PRESETS)
    menu.default = "Interaction"

    cost_gating = {name for names in COOK_GATED.values() for name in names}
    for group, default, gated in GROUPS:
        par = existing.get(group)
        if par is None:
            if gated == "derive" or group in cost_gating:
                suffix = ""
            else:
                suffix = "  (channels only)"
            par = page.appendToggle(group, label=group + suffix)[0]
            par.val = default
        par.default = default
    return page


def main():
    import td

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    # TouchDesigner caches our modules for the life of the process. DESIGN.md 2.11.
    for stale in [n for n in list(sys.modules)
                  if n == "appletd" or n.startswith("appletd.")]:
        del sys.modules[stale]
    from appletd.derive import ALL_GROUPS

    # BEFORE anything is built. A drifted joint list would silently leave a joint on
    # the output that `Fingertipsonly` claims to remove, and there is no point
    # building a network around a trim list that is already wrong.
    _check_joint_split()

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
    for group_name, vetoes in COOK_VETOED.items():
        for toggle in vetoes:
            freezes.setdefault(toggle, []).append(group_name + " (veto)")
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
        "cook_vetoed": {k: list(v) for k, v in COOK_VETOED.items()},
        "trim_source": _trim_source(),
    }
    callbacks.par.op = master.path
    # Verbosity AND every toggle that gates a group's cooking: `allowCooking` is an
    # attribute, so it cannot be bound to a parameter and has to be rewritten on
    # change.
    gating_toggles = sorted(
        {name for names in COOK_GATED.values() for name in names}
        | {name for names in COOK_VETOED.values() for name in names}
        # `Deleteempty` gates nothing's cooking - it bypasses the trim - but it goes
        # through the same callback, so it is in the same list.
        | {TRIM_TOGGLE}
        # The two output-only toggles. Same reason as the attribute toggles below:
        # the trim is a Select with a KEEP list, so nothing rewriting the list means
        # a toggle that appears to do nothing at all.
        | {FINGERTIPS_TOGGLE, HANDBOX_TOGGLE}
        # And EVERY attribute toggle, not just the ones that gate cooking. The trim
        # is a Select with a keep list, so it fails CLOSED: a channel the list does
        # not name is gone. `Descriptor`, `Depth` and `Tilt` gate no group's cooking,
        # but turning one on adds channels, and if nothing rewrote the list those
        # channels would be computed and then thrown away - a toggle that appeared
        # to do nothing.
        | {name for name, _default, _kind in GROUPS})
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
    #
    # And it is checked AFTER a forced cook of the whole master, because that is what
    # breaks it. MEASURED 2026-08-22: `master.cook(force=True)` - which
    # tools/td_build_vision.py does at its own report step - asks every child for
    # fresh data, a frozen group cannot answer, and its Out CHOP drops to ZERO
    # channels. `hands/temporal` went from 27 to 0 and `latches` from 70 to 0, and
    # the hands stream from 503 channels to 406. Nothing said so; the channels were
    # simply gone.
    #
    # `_apply_gating` repairs it - it unfreezes each group, cooks its Out CHOP, and
    # freezes it again - so it is called once more here, after the forced cook. The
    # order is deliberate: provoke, repair, then verify.
    master.cook(force=True)
    _apply_gating(master)
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

    print("\nEVERY TOGGLE ON THIS PAGE NOW CHANGES SOMETHING.")
    print("  `Landmarks`, `Triggers`, `Motion` and `Events` were removed on")
    print("  2026-08-23 - they could not remove their own channels from the output,")
    print("  because that needs an exact channel-to-group map and a wildcard cannot")
    print("  partition `h?_*_x` into the raw `h0_wrist_x` and the derived")
    print("  `h0_palm_x`. If they are ever wanted back, the sound way to get that")
    print("  map is a registry in the package: derive() can already report a group's")
    print("  channels by being called with just that group, and the latch table")
    print("  would move out of tools/td_add_latches.py into appletd/ so both can")
    print("  import it. RETIRED_PARS destroys the old parameters.")
    print("")
    print("  `filter` is still not a gating candidate: it is in the data path, and")
    print("  its Smoothing toggle gates its cost through the bypass flag instead.")

main()
