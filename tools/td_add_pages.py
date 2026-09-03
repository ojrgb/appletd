#!/usr/bin/env python
"""Put every custom parameter on the page it belongs on.

    RUN IT
        run("<repo>/tools/td_add_pages.py")

    or through the chain: tools/td_rebuild.py, layer "pages".

RUNS LAST but one, after every builder has appended whatever it appends and before
`about`. It owns no parameters of its own: it moves what others created, adds the
section dividers, sorts each page and destroys the pages nothing lives on any more.

`appletd/td_pages.py` is the table. This file is the mechanism, and it is short
because the two operations it needs turned out to be cheap - MEASURED 2026-09-03,
`par.page = page` moves a parameter and KEEPS ITS VALUE, and `page.name = ...` renames
in place. So there is no snapshot and no restore here, and running it twice changes
nothing the second time.

A PARAMETER THE TABLE DOES NOT MENTION STOPS THE BUILD. Not a warning: a control that
lands wherever its builder happened to run is what this file exists to prevent, and a
warning in a 13-builder chain is a line nobody reads.

Ref: appletd/td_pages.py, docs/BUILD_PLAN.md step 27.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_PATH = "/project1/appletd"


def main():
    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    for stale in [n for n in list(sys.modules)
                  if n == "appletd" or n.startswith("appletd.")]:
        del sys.modules[stale]
    from appletd import td_pages

    comp = op(MASTER_PATH)
    if comp is None:
        print("no COMP at %s" % MASTER_PATH)
        return

    print("=" * 70)
    print("pages: the parameter layout on %s" % comp.path)
    print("=" * 70)

    # 1. RENAME BEFORE ANYTHING ELSE. `Vision` holds 22 parameters and renaming keeps
    #    every one of them; creating `General` and moving them is 22 chances to drop
    #    one, and the old page would have to be destroyed afterwards anyway.
    by_name = {page.name: page for page in comp.customPages}
    for old, new in td_pages.RENAMES:
        if old in by_name and new not in by_name:
            by_name[old].name = new
            print("   renamed page %-12s -> %s" % (old, new))
            by_name = {page.name: page for page in comp.customPages}

    # 2. Every page the table needs, in order.
    for name in td_pages.PAGE_ORDER:
        if name not in by_name:
            comp.appendCustomPage(name)
            print("   added page   %s" % name)
    by_name = {page.name: page for page in comp.customPages}

    # 3. Retired parameters, before the unplaced check can trip over them.
    for name in td_pages.RETIRED:
        par = getattr(comp.par, name, None)
        if par is not None:
            par.destroy()
            print("   retired      %s" % name)

    # 4. Anything the table does not know about. Checked BEFORE the moves, so the
    #    message names a page somebody can go and look at.
    present = [par.name for par in comp.customPars]
    stray = td_pages.unplaced(present)
    if stray:
        print("\n   FAILED - %d parameter(s) are on no page in appletd/td_pages.py:"
              % len(stray))
        for name in stray:
            par = getattr(comp.par, name)
            print("      %-18s currently on %s" % (name, par.page.name))
        print("\n   Add them to LAYOUT, or to RETIRED if they are going away.")
        return

    # 5. The dividers. A Header parameter with `startSection` on is what draws the
    #    labelled rule; the label is the text and the parameter itself has no value.
    for page_name, header, label, _above in td_pages.sections():
        if getattr(comp.par, header, None) is None:
            par = by_name[page_name].appendHeader(header, label=label)[0]
            par.startSection = True
        else:
            par = getattr(comp.par, header)
            par.label = label
            par.startSection = True

    # 6. The moves. `par.page` takes the Page object, not its name.
    moved = 0
    for name, (page_name, _index) in sorted(td_pages.placement().items()):
        par = getattr(comp.par, name, None)
        if par is None:
            continue                      # a builder that has not run yet
        if par.page.name != page_name:
            par.page = by_name[page_name]
            moved += 1
    print("   moved        %d parameter(s)" % moved)

    # 7. Order within each page: the dividers interleaved with what they head.
    for page_name in td_pages.PAGE_ORDER:
        order = []
        for label, names in td_pages.LAYOUT[page_name]:
            if label and names:
                order.append(td_pages.header_name(page_name, label))
            order.extend(n for n in names if getattr(comp.par, n, None) is not None)
        if order:
            by_name[page_name].sort(*order)

    # 8. Pages nothing lives on any more. An empty tab is a question somebody has to
    #    answer by clicking it.
    for page in list(comp.customPages):
        if page.name in td_pages.LEGACY_PAGES and not page.pars:
            print("   removed page %s" % page.name)
            page.destroy()

    # 9. And the page order itself.
    comp.sortCustomPages(*[n for n in td_pages.PAGE_ORDER
                           if n in {p.name for p in comp.customPages}])

    print()
    for page in comp.customPages:
        print("   %-18s %2d" % (page.name, len(page.pars)))
    print("\n   verified. Every parameter is where appletd/td_pages.py says.")


main()
