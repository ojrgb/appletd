"""Embeds the runtime package into the .toe, as one Text DAT per module.

    RUN IT
        run("<repo>/tools/td_embed_package.py")

    or through the chain: tools/td_rebuild.py, layer "embed".

WHAT THIS IS FOR. The sidecar is a separate OS process, so its code has to exist as
FILES on disk - it cannot import from a Text DAT. But a `.toe` handed to somebody
else has no repository behind it. So the builders carry the package INTO the file, and
the Install button writes it back OUT. This is the carrying half.

THE FILES REMAIN THE SOURCE OF TRUTH. 567 tests, ruff and mypy all run against
`appletd/*.py`; source that lives inside a binary `.toe` cannot be diffed, tested or
linted. So this script overwrites every DAT unconditionally on every run, and editing
one is a change that the next run destroys without asking. That is the intended
behaviour and the reason the warning is where it is (below).

BYTE-IDENTICAL, and this is a DEVIATION from the plan in docs/BUILD_PLAN.md 21.3,
which said the DATs would "carry a header saying so". They do not. A header would
mean the file Install writes is not the file that was tested - different bytes,
different hash, and a version stamp that cannot be compared against anything. The
warning lives on each DAT's `comment` (visible in the network, right-click to read)
and in this container's `notes`, where it costs nothing.

THE VERSION is a hash of the embedded content, written to `Sourceversion` on the
Advanced page. `appletd.install.probe()` compares it against what an install recorded,
which is the only way to catch a newer `.toe` opened over an older install - that runs
the OLD `derive` on every cook, with no error and wrong channels.

Ref: docs/BUILD_PLAN.md step 21, appletd/install.py.
"""

import os
import sys

# Derived from this file's own location. A literal path here meant the project
# opened on exactly one machine; `__file__` is set by the shell, by
# TouchDesigner's run(), and by tools/td_rebuild.py before each exec.
# tools/td_paths.py has the full reasoning and why it is not imported from there.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MASTER_PATH = "/project1/vision"
# The container. One COMP so nineteen DATs are one node in the master network, and a
# name short enough to type in an expression.
GROUP = "src"
NOTES = "notes"

# What every embedded DAT says on its `comment`, so somebody who opens one and starts
# typing has been told. Deliberately not in the TEXT - see the module docstring.
WARNING = ("GENERATED from appletd/%s.py by tools/td_embed_package.py. "
           "Overwritten on every run - edit the FILE, not this.")

CONTAINER_NOTES = """SOURCE, carried into the .toe so the Install button can write it
back out to disk.

WHY IT HAS TO BE ON DISK AT ALL. The sidecar is a separate OS process and cannot
import from a Text DAT. Three of these are also imported by generated callback DATs on
every cook.

THESE DATS ARE A BUILD ARTEFACT. The files in appletd/ are the source of truth - they
are what the tests, ruff and mypy run against. tools/td_embed_package.py overwrites
every DAT here on every run, so an edit made in this container is destroyed by the
next build without warning. Edit the file.

The text is BYTE-IDENTICAL to the file on purpose: what Install writes out has to be
what was tested, or the version hash means nothing.

%(count)d modules, %(kb)d KB, version %(version)s
"""


def module_sources():
    """`(name, text)` for every embedded module, in `EMBEDDED_MODULES` order.

    Raises on a missing file rather than embedding a short list. A package that is
    missing a module installs cleanly and then fails at its first import, on somebody
    else's machine, which is the worst place to find out.
    """
    from appletd.install import EMBEDDED_MODULES

    out = []
    for name in EMBEDDED_MODULES:
        path = os.path.join(REPO_ROOT, "appletd", name + ".py")
        if not os.path.exists(path):
            raise RuntimeError(
                "appletd/%s.py is in EMBEDDED_MODULES and not on disk. Either the "
                "module was renamed and the list was not, or the list is wrong - "
                "appletd/tests/test_install.py is what normally catches this."
                % name)
        with open(path, encoding="utf-8") as handle:
            out.append((name, handle.read()))
    return out


def main():
    import td

    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    # TouchDesigner caches our modules for the life of the process. DESIGN.md 2.11.
    for stale in [n for n in list(sys.modules)
                  if n == "appletd" or n.startswith("appletd.")]:
        del sys.modules[stale]
    from appletd.install import content_version
    from appletd.td_layout import master_xy, placement

    master = op(MASTER_PATH)
    if master is None:
        print("FAIL no COMP at %s - run tools/td_build_vision.py first" % MASTER_PATH)
        return

    print("=" * 70)
    print("embed: carrying the runtime package into %s" % master.path)
    print("=" * 70)

    sources = module_sources()
    version = content_version(sources)

    group = master.op(GROUP)
    existed = group is not None
    if group is None:
        group = master.create(td.baseCOMP, GROUP)
    where = placement(master_xy(GROUP), bool(master.par.Keeplayout.eval()), existed)
    if where is not None:
        group.nodeX, group.nodeY = where
    group.color = (0.25, 0.35, 0.45)

    # REUSE the container and clear only what we own. Destroying it would orphan
    # anything somebody wired to it, and a dangling input cannot be told from one
    # that was never connected (DESIGN.md 2.11).
    wanted = {name for name, _text in sources} | {NOTES}
    for child in list(group.children):
        if child.name not in wanted:
            child.destroy()

    total = 0
    for index, (name, text) in enumerate(sources):
        dat = group.op(name)
        if dat is None:
            dat = group.create(td.textDAT, name)
        dat.text = text
        dat.comment = WARNING % name
        # A grid rather than a column: nineteen nodes in one line is a network nobody
        # can read at a glance.
        dat.nodeX = (index % 4) * 200
        dat.nodeY = -(index // 4) * 120
        total += len(text)

    notes = group.op(NOTES)
    if notes is None:
        notes = group.create(td.textDAT, NOTES)
    notes.text = CONTAINER_NOTES % {"count": len(sources),
                                    "kb": round(total / 1024),
                                    "version": version}
    notes.nodeX, notes.nodeY = -240, 0

    # The version, where the probe can read it. Appended here rather than in
    # td_build_vision.py because this script is what decides the value, and the rule
    # for a shared page is that each builder appends only its OWN names.
    page = None
    for existing in master.customPages:
        if existing.name == "Advanced":
            page = existing
            break
    if page is None:
        print("   (no Advanced page yet - run tools/td_build_vision.py first)")
    else:
        par = getattr(master.par, "Sourceversion", None)
        if par is None:
            par = page.appendStr("Sourceversion", label="Embedded Source Version")[0]
        par.val = version
        # READ-ONLY: it is a fact about what is embedded, not a preference. Somebody
        # typing over it would make `probe()` compare the install against a version
        # that was never built.
        par.readOnly = True

    print()
    print("   %d modules, %d KB, version %s" % (len(sources), total / 1024, version))
    print("   %s %s" % ("reused" if existed else "created", group.path))

    # -- verification ------------------------------------------------------
    # Read every DAT BACK and compare it to the file. TouchDesigner stores DAT text
    # its own way, and this script's whole promise is that what Install writes is
    # what was tested - so the promise is checked rather than assumed.
    failures = []
    for name, text in sources:
        dat = group.op(name)
        if dat is None:
            failures.append("%s: DAT missing after writing it" % name)
            continue
        if dat.text != text:
            failures.append(
                "%s: DAT text differs from the file (%d chars vs %d)%s"
                % (name, len(dat.text), len(text),
                   " - trailing newline" if dat.text.rstrip() == text.rstrip()
                   else ""))
    from appletd.install import EMBEDDED_MODULES
    if len(sources) != len(EMBEDDED_MODULES):
        failures.append("embedded %d modules, EMBEDDED_MODULES names %d"
                        % (len(sources), len(EMBEDDED_MODULES)))

    print()
    if failures:
        print("FAILURES (%d):" % len(failures))
        for failure in failures:
            print("   " + failure)
        print()
        print("   A DAT that does not match its file means Install would write "
              "something")
        print("   that was never tested. That is the one thing this script must not "
              "do.")
    else:
        print("   verified: every DAT is byte-identical to its file.")
        print("   `Sourceversion` = %s, which is what appletd.install.probe()"
              % version)
        print("   compares against an install's INSTALLED.json.")
    print()


main()
