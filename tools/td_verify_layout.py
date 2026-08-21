#!/usr/bin/env python
"""Is the network READABLE? Overlapping nodes, undocumented groups, stray operators.

    Paste into a Text DAT, Run Script. Reads only - it changes nothing.

WHAT IT CHECKS, and each one is a thing that actually happened here:

  1. **Overlapping operators.** `tools/td_add_filter.py` and `tools/td_add_coords.py`
     both placed their group at (-400, -300) - exactly on top of each other - and
     nothing in TouchDesigner or in either builder could notice. The node boxes are
     read from the live operators (`nodeWidth`/`nodeHeight`), not assumed.

  2. **A network with no Text DAT explaining it.** Every group that holds working
     operators should say what it does, beside the operators it describes.

  3. **A network laid out by a running counter.** The symptom is a very wide, very
     shallow network: `temporal` was 11,590 units across and 13 rows deep, because a
     single column counter advanced for every operator whatever row it was on. The
     check is the ASPECT of the bounding box against the operator count - a network
     with 73 operators in one long ribbon is not a network anyone can read.

  4. **Operators nothing reads and nothing feeds.** Usually a leftover from an
     older shape. This found a real one immediately: `tmp_motion_callbacks` existed
     TWICE - once at the stream level, orphaned, and once inside `temporal` sitting
     directly on top of `tmp_held_prev` - because creating a Script CHOP
     auto-creates a docked callbacks DAT and the builder was destroying TD's copy by
     guessing the name it would collide into.

     Names that legitimately have no wires are listed in EXPECTED_ISLANDS: a Text
     DAT, and a Script CHOP's callbacks DAT, which is attached by a PARAMETER.

It is a REPORT, not a gate. Some findings are fine - a Text DAT has no inputs or
outputs by design - so it prints what it sees and says which ones are expected.

Ref: visionhands/td_layout.py (where the coordinates come from),
     docs/BUILD_PLAN.md step 10.
"""

import sys

REPO_ROOT = "/Users/omer/Documents/GitHub/visionhands-touchdesigner"
MASTER_PATH = "/project1/vision"

# A network wider than this many columns per operator is a ribbon rather than a
# layout. 73 operators over 13 rows should be about 6 columns wide on average; the
# running-counter version was 58. Generous, so only a real ribbon trips it.
MAX_COLUMNS_PER_OPERATOR = 1.6

# Operator names that legitimately have no inputs and no outputs.
EXPECTED_ISLANDS = ("notes", "screen_only_notes", "profiler", "sidecar_control",
                    "sidecar_callbacks", "sidecar_exit", "filter_callbacks",
                    "groups_callbacks", "lat_threshold_callbacks",
                    "screenspace_callbacks", "derive_callbacks",
                    # A Script CHOP's callbacks DAT is docked to it by a PARAMETER,
                    # not by a wire, so it has no connections by design.
                    "tmp_motion_callbacks",
                    # tools/td_verify_latches.py's stored counter baseline.
                    "ver_baseline")


def main():
    if REPO_ROOT not in sys.path:
        sys.path.append(REPO_ROOT)
    for stale in [n for n in list(sys.modules)
                  if n == "visionhands" or n.startswith("visionhands.")]:
        del sys.modules[stale]
    from visionhands.td_layout import COL_W

    master = op(MASTER_PATH)
    if master is None:
        print("FAIL no COMP at %s" % MASTER_PATH)
        return

    findings = []
    print("=" * 72)
    print("layout report for %s" % master.path)
    print("=" * 72)

    for network in _networks(master):
        children = list(network.children)
        if not children:
            continue
        working = [c for c in children if c.name not in EXPECTED_ISLANDS]
        clashes = _overlaps(children)
        spread = _spread(children, COL_W)
        documented = any(c.name == "notes" or c.name.endswith("_notes")
                         for c in children)
        islands = [c.name for c in children
                   if c.name not in EXPECTED_ISLANDS
                   and not any(conn.connections for conn in c.inputConnectors)
                   and not _has_consumer(c)]

        print()
        print("%-38s %3d operators  %2d cols x %2d rows"
              % (_relative(network, master), len(children), spread[0], spread[1]))
        if clashes:
            findings.append("%s: %d overlapping pair(s), first %s"
                            % (_relative(network, master), len(clashes), clashes[0]))
            print("   OVERLAP  %d pair(s): %s" % (len(clashes), clashes[:3]))
        if working and not documented:
            findings.append("%s: %d working operators and no notes DAT"
                            % (_relative(network, master), len(working)))
            print("   NO NOTES  %d working operators and nothing explaining them"
                  % len(working))
        if len(children) > 6 and spread[0] > len(children) * MAX_COLUMNS_PER_OPERATOR:
            findings.append("%s: %d operators spread over %d columns - a ribbon"
                            % (_relative(network, master), len(children), spread[0]))
            print("   RIBBON    %d operators over %d columns; a running column "
                  "counter looks exactly like this" % (len(children), spread[0]))
        if islands:
            findings.append("%s: %d operator(s) with no input and no consumer: %s"
                            % (_relative(network, master), len(islands), islands[:4]))
            print("   ORPHANED  %s" % ", ".join(islands[:6]))

    print()
    print("=" * 72)
    if findings:
        print("%d finding(s):" % len(findings))
        for finding in findings:
            print("   " + finding)
    else:
        print("no overlaps, every network documented, nothing orphaned.")
    print("=" * 72)


def _networks(master):
    """The master and every COMP inside it that holds operators, depth first."""
    found = [master]

    def walk(parent):
        for child in parent.children:
            if child.isCOMP:
                found.append(child)
                walk(child)

    walk(master)
    return found


def _relative(node, master):
    return node.path[len(master.path) + 1:] or "<master>"


def _overlaps(children):
    """Every pair whose node boxes intersect, using the LIVE node sizes.

    Read rather than assumed: a CHOP is 130 x 90 and a base COMP 160 x 130, and a
    check that assumed one size would either miss a real overlap or invent one.
    """
    boxes = [(c.name, c.nodeX, c.nodeY, c.nodeWidth, c.nodeHeight) for c in children]
    clashes = []
    for index, (name, x, y, w, h) in enumerate(boxes):
        for other, ox, oy, ow, oh in boxes[index + 1:]:
            if abs(x - ox) < (w + ow) / 2 and abs(y - oy) < (h + oh) / 2:
                clashes.append((name, other))
    return clashes


def _spread(children, col_w):
    """The bounding box in COLUMNS and ROWS, which is what makes a ribbon visible."""
    xs = [c.nodeX for c in children]
    ys = [c.nodeY for c in children]
    columns = int((max(xs) - min(xs)) / col_w) + 1
    rows = len({round(y / 50) for y in ys})
    return (columns, rows)


def _has_consumer(node):
    """Does anything in the same network read this operator's output?

    `outputConnectors[i].connections` rather than `.outputs`, which goes stale
    inside a script that rewired anything (DESIGN.md 2.11) - and this script rewires
    nothing, but the same habit costs nothing and cannot be wrong.
    """
    return any(conn.connections for conn in node.outputConnectors)


main()
