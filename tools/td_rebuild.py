"""Run only the builders a change actually needs.

WHY THIS EXISTS. There are eight builders and they have to run in order, so the
safe move has always been to run all eight - about thirty seconds of
TouchDesigner, and more to the point eight self-checks to read, any of which can
report something unrelated to what you changed. Today a three-operator change to
the master COMP cost a full chain run three times over.

Most changes touch ONE layer. This is the table that says which script owns which
layer, and what a change to one drags along behind it.

USE IT LIKE THIS, from the textport or an Execute DAT:

    TARGET = "master"                    # or a tuple: ("coords", "groups")
    run("<repo>/tools/td_rebuild.py")

or from a script that has already exec'd this file's globals, set `TARGET` before
the exec. With no TARGET it runs everything, which is what the chain always did.

WHAT IT DOES NOT DO. It does not guess. `REQUIRES` is written out by hand, per
layer, because a builder's dependencies are a fact about what that builder
destroys and reads - not something to infer from a filename. If a layer's entry is
wrong the symptom is a missing callback DAT, which is exactly the failure this
exists to stop, so each entry says WHY it is there.

Ref: docs/BUILD_PLAN.md step 14, docs/JOURNAL.md.
"""

import sys

REPO_ROOT = "/Users/omer/Documents/GitHub/visionhands-touchdesigner"

# layer -> the script that owns it, in CHAIN ORDER. The order is the dependency
# order: `derive` reads `filter`'s output, `latches` reads `temporal`'s, `groups`
# writes gating over all of them and must be last.
LAYERS = (
    ("master",      "td_build_vision.py"),
    ("filter",      "td_add_filter.py"),
    ("derive",      "td_add_derive.py"),
    ("temporal",    "td_add_temporal.py"),
    ("latches",     "td_add_latches.py"),
    ("coords",      "td_add_coords.py"),
    ("screenspace", "td_add_screenspace.py"),
    ("groups",      "td_add_groups.py"),
    # LAST, and independent of everything above: the mask arrives by shared memory
    # rather than over OSC, so it shares no operator with the CHOP network. It is
    # after `groups` only because `groups` must not be the last word on a page it
    # does not own.
    ("segmentation", "td_add_segmentation.py"),
)

# What each layer drags along, and why. Anything listed here is added to the run
# and then the whole run is sorted back into CHAIN ORDER, so listing a layer that
# comes earlier is safe.
REQUIRES = {
    # `td_build_vision.py` no longer destroys the callback DATs other builders own
    # (OTHER_BUILDERS_OWN, 2026-08-22), so a master rebuild is genuinely standalone.
    # It does re-derive the Attributes page's parameters though, and `groups` is what
    # writes the gating and the trim list from them.
    "master": ("groups", "segmentation"),
    # These four each rebuild a group whose channels the trim list is generated
    # from, so the list has to be rewritten or the new channels are invisible - a
    # keep list fails closed (DESIGN.md 2.15).
    "derive": ("groups",),
    "temporal": ("groups",),
    "latches": ("groups",),
    "coords": ("groups",),
    "screenspace": ("groups",),
    # `filter` is in the data path and gates through a bypass flag, not through
    # `allowCooking`, so it changes no channel NAMES and the trim list still holds.
    "filter": (),
    "groups": (),
    # Nothing. It owns its own page, its own three operators and its own callbacks,
    # and `td_build_vision.py` no longer destroys any of them (OTHER_BUILDERS_OWN).
    "segmentation": (),
}

ALL = tuple(name for name, _script in LAYERS)


def plan(targets):
    """The scripts to run, in chain order, with every requirement pulled in.

    Contract: raises on an unknown layer name rather than silently running nothing,
              because "I asked for a rebuild and got no output" is the worst way to
              find out you typed `coord`.
    """
    if isinstance(targets, str):
        targets = (targets,)
    wanted = set()
    queue = list(targets)
    while queue:
        name = queue.pop()
        if name == "all":
            wanted.update(ALL)
            continue
        if name not in ALL:
            raise KeyError("no layer called %r; known: all, %s"
                           % (name, ", ".join(ALL)))
        if name in wanted:
            continue
        wanted.add(name)
        queue.extend(REQUIRES.get(name, ()))
    return [(name, script) for name, script in LAYERS if name in wanted]


def main(targets="all"):
    import td  # noqa: F401  - proves we are inside TouchDesigner before any work

    steps = plan(targets)
    print("=" * 70)
    print("rebuild: %s -> %d of %d builders"
          % (targets if isinstance(targets, str) else " ".join(targets),
             len(steps), len(LAYERS)))
    for name, script in steps:
        print("   %-12s %s" % (name, script))
    print("=" * 70)

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    failed = []
    for name, script in steps:
        path = REPO_ROOT + "/tools/" + script
        print("\n" + "-" * 70)
        print("### %s (%s)" % (name, script))
        print("-" * 70)
        # Each builder gets its OWN globals with `__name__ == "__main__"`, because
        # every one of them calls `main()` at import and would otherwise inherit
        # this module's names. TouchDesigner's builtins come from this module's
        # globals, which is why they are copied in rather than starting empty.
        namespace = dict(globals())
        namespace["__name__"] = "__main__"
        namespace["__file__"] = path
        try:
            exec(compile(open(path).read(), script, "exec"), namespace)
        except Exception as problem:            # noqa: BLE001 - reported, not hidden
            failed.append((script, problem))
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 70)
    if failed:
        print("%d of %d builders FAILED:" % (len(failed), len(steps)))
        for script, problem in failed:
            print("   %-24s %r" % (script, problem))
    else:
        print("%d builders ran, none raised." % len(steps))
    print("=" * 70)


main(globals().get("TARGET", "all"))
