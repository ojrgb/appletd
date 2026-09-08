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

import os
import sys

# Derived from this file's own location. A literal path here meant the project
# opened on exactly one machine; `__file__` is set by the shell, by
# TouchDesigner's run(), and by tools/td_rebuild.py before each exec.
# tools/td_paths.py has the full reasoning and why it is not imported from there.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    ("depth", "td_add_depth.py"),
    ("flow", "td_add_flow.py"),
    # After the master exists and before the page layout: it adds two parameters and
    # an image input, and touches no operator any other builder owns.
    ("topinput", "td_add_topinput.py"),
    # The camera image as a TOP, and the composite the overlays land on.
    ("video", "td_add_video.py"),
    # AFTER video: it connects into `video_over`, which that layer creates.
    ("overlay", "td_add_overlay.py"),
    # LAST, and independent of every operator above: it carries the package into the
    # file and touches nothing in the CHOP or TOP networks. Last also means the
    # version it stamps reflects the sources as they are at the end of a chain run.
    ("embed", "td_embed_package.py"),
    # AFTER embed: the button writes out what embed put in, and its own probe
    # reads the version embed stamped.
    ("install", "td_add_install.py"),
    # LAST, so the About page is the last tab. It touches no operator any other
    # builder owns - one page and two DATs.
    ("about", "td_add_about.py"),
    # AFTER EVERYTHING, because it moves what everything else appended. Nine builders
    # each create parameters on a page of their own choosing; this puts them where
    # appletd/td_pages.py says they go, adds the section dividers and drops the pages
    # nothing is left on. It owns no parameters, so it has nothing to be before.
    ("pages", "td_add_pages.py"),
)

# What each layer drags along, and why. Anything listed here is added to the run
# and then the whole run is sorted back into CHAIN ORDER, so listing a layer that
# comes earlier is safe.
#
# EVERY LAYER HAS AN ENTRY, checked below. Six were simply absent, and `.get(name, ())`
# made a missing entry indistinguishable from a considered empty one - which is the
# opposite of what the docstring above promises. An empty tuple is now a statement.
#
# WHY SO MANY NAME `pages`. Thirteen of these builders append custom parameters, and
# `td_add_pages.py` is what puts a parameter on the page and under the heading it
# belongs to. Run one of them alone and its parameters stay wherever they were
# appended - which is what "I rebuilt one layer and my parameters moved to General"
# is. The layers that append nothing say so instead.
REQUIRES = {
    # `td_build_vision.py` no longer destroys the callback DATs other builders own
    # (OTHER_BUILDERS_OWN), so a master rebuild is genuinely standalone.
    # It does re-derive the Attributes page's parameters though, and `groups` is what
    # writes the gating and the trim list from them.
    "master": ("groups", "segmentation", "depth", "pages"),
    # These four each rebuild a group whose channels the trim list is generated
    # from, so the list has to be rewritten or the new channels are invisible - a
    # keep list fails closed (DESIGN.md 2.15).
    "derive": ("groups", "pages"),
    "temporal": ("groups", "pages"),
    "latches": ("groups", "pages"),
    "coords": ("groups",),
    "screenspace": ("groups",),
    # `filter` is in the data path and gates through a bypass flag, not through
    # `allowCooking`, so it changes no channel NAMES and the trim list still holds.
    "filter": (),
    "groups": ("pages",),
    # `embed` writes Text DATs into a container nothing is wired to - it is the only
    # layer here that cannot change a channel - but it does append `Sourceversion`,
    # which the Install section of the General page has a place for.
    "embed": ("pages",),
    # `install` reads `Sourceversion`, which `embed` writes, and appends four
    # parameters of its own.
    "install": ("embed", "pages"),
    # Nothing. It owns its own page, its own three operators and its own callbacks,
    # and `td_build_vision.py` no longer destroys any of them (OTHER_BUILDERS_OWN).
    "segmentation": ("pages",),
    # Same as segmentation: its own page, its own three operators, its own callbacks,
    # and td_build_vision.py destroys none of them.
    "depth": ("pages",),
    # Its own page, its own three operators, its own callbacks - and four parameters.
    "flow": ("pages",),
    # Two parameters and an image input, and no operator any other builder owns.
    "topinput": ("pages",),
    # NOTHING. `video` appends no parameter and owns five TOPs nothing else touches.
    # It does own `video_over`, which `overlay` connects into - but recreating a
    # connection is `overlay`'s job on its own next run, and `video` rebuilt alone
    # leaves the overlay COMP itself intact.
    "video": (),
    # Two parameters per stream, appended to pages `pages` then sorts.
    "overlay": ("pages",),
    # Ten parameters on a page of its own, and `pages` is what puts the About page
    # last and drops the headings in.
    "about": ("pages",),
    # NOTHING, and it must stay that way: it is what everything else requires, so a
    # requirement of its own would be a cycle waiting to be written.
    "pages": (),
}

_UNLISTED = [name for name in tuple(n for n, _ in LAYERS) if name not in REQUIRES]
if _UNLISTED:
    raise AssertionError(
        "these layers have no REQUIRES entry, so `plan()` would treat them as having "
        "no dependencies without anyone having decided that: %s"
        % ", ".join(_UNLISTED))

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
        queue.extend(REQUIRES[name])
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
