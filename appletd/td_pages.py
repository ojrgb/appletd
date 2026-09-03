"""Where every custom parameter belongs, as one table.

WHY THIS EXISTS. Nine builders append parameters, each to a page of its own choosing,
and the page a control ends up on is therefore decided in nine places by whichever
script happened to create it. That produced a `Filter` page holding three parameters, a
`Tuning` page holding thresholds for a stream you may not have enabled, and stream
toggles for five different streams in one list on `Vision`.

This file says where things GO. The builders still create them wherever suits them; the
layout pass moves each one to its stated home, inserts the section dividers and sorts.

WHAT MAKES THAT CHEAP, and it was not obvious - MEASURED 2026-09-03:

    par.page = some_page     moves the parameter and KEEPS ITS VALUE
    page.name = "General"    renames in place, parameters intact

So none of this is the destroy-and-re-append that `MOVED_PARS` in
`tools/td_build_vision.py` has to do for a parameter changing STYLE. Nothing is
snapshotted, nothing is restored, and a rebuild that runs this twice is idempotent.

A PARAMETER NOT IN THIS TABLE IS AN ERROR, not a default. `unplaced()` returns them and
the builder prints them loudly: a new control that silently lands on whatever page its
builder felt like is exactly the drift this file exists to stop.
"""

from __future__ import annotations

from typing import Final

# The page order, top to bottom. `About` last because it is the only page that is not
# about running the thing.
PAGE_ORDER: Final = ("General", "Hands", "Body Pose", "Face", "Segmentation Mask",
                     "Depth", "Advanced", "About")

# Pages that existed before 2026-09-03 and no longer do. The layout pass moves their
# parameters out by the table below and then destroys the empty page - a page left
# behind is an empty tab somebody has to wonder about.
LEGACY_PAGES: Final = ("Vision", "Attributes", "Filter", "Tuning", "Segmentation",
                       "Coords")

# `Vision` became `General` rather than being replaced: renaming keeps every parameter
# and every tuned value, where creating a new page and moving 22 controls into it is
# 22 chances to lose one.
RENAMES: Final = (("Vision", "General"),)

# page -> the sections, in order, each a label and the parameters under it.
#
# The label is what the divider says. A section with a label of "" gets no divider,
# which is how a page starts with something at the top rather than under a heading.
LAYOUT: Final[dict[str, tuple[tuple[str, tuple[str, ...]], ...]]] = {
    "General": (
        ("Capture", ("Active", "Restartcapture", "Capturestate",
                     "Camera", "Listcameras")),
        ("Coordinate spaces", ("Coordstx", "Coordspx")),
        ("Smoothing", ("Smoothing", "Mincutoff", "Beta")),
        ("Install", ("Install", "Installstate")),
    ),
    "Hands": (
        ("", ("Streamhands",)),
        ("Output", ("Fingertipsonly", "Handbox")),
        ("Attributes", ("Core", "Presence", "Contacts", "Pose", "Twohands",
                        "Gestures", "Descriptor", "Depth", "Tilt")),
        ("Detection", ("Confthreshold", "Activateframes", "Deactivateframes",
                       "Sizefloor")),
        ("Contact thresholds", ("Pinchon", "Pinchoff", "Snapon", "Snapoff",
                                "Triggeron", "Triggeroff", "Togetheron",
                                "Togetheroff", "Grabon", "Graboff",
                                "Overlapthreshold")),
        ("Pose and motion", ("Curlmin", "Curlmax", "Extendedbelow", "Curledabove",
                             "Spreadmin", "Spreadmax", "Velocityfilter",
                             "Speedfloor")),
        ("Depth and tilt", ("Zreference", "Palmarea")),
    ),
    "Body Pose": (
        ("", ("Streampose",)),
    ),
    "Face": (
        ("", ("Streamface",)),
        ("Output", ("Facekeypoints", "Onefaceonly")),
    ),
    "Segmentation Mask": (
        ("", ("Streamsegment", "Segquality")),
        ("Output", ("Maskfit", "Masksourcew", "Masksourceh")),
    ),
    "Depth": (
        ("", ("Streamdepth",)),
        ("Pins", ("Depthpinson", "Depthpincount", "Depthpinsdraw",
                  *("Depthpin%d%s" % (row, axis)
                    for row in range(1, 9) for axis in ("x", "y", "m")))),
        ("Output", ("Depthfit", "Depthwindownear", "Depthwindowfar", "Depthunits",
                    "Depthsourcew", "Depthsourceh")),
        ("This frame's fit", ("Depthfitalpha", "Depthfitbeta", "Depthfitpins",
                              "Depthfitresidual", "Depthfitchecked")),
    ),
    "Advanced": (
        ("Output", ("Screenspaceonly", "Deleteempty", "Slotassign")),
        ("Geometry", ("Resw", "Resh", "Renderw", "Renderh", "Orthowidth")),
        ("Master switches", ("Temporal", "Latches")),
        ("Network", ("Oscport", "Maskbuffer", "Depthbuffer")),
        ("Install", ("Installroot", "Sidecarpython", "Pythonurl", "Sourceversion",
                     "Forceinstall")),
        ("Diagnostics", ("Printstatus", "Capturepid", "Keeplayout")),
    ),
    "About": (
        ("", ("Repository", "Openrepository")),
        ("Update", ("Updateurl", "Checkupdate", "Updatestate", "Applyupdate",
                    "Updateetag", "Updatefound")),
        ("", ("Notice1", "Notice2", "Notice3", "Openlicence")),
    ),
}

# Parameters that are deliberately not laid out because they are being removed.
# `Verbosity` was a preset menu over the Hands attribute toggles; it went on
# 2026-09-03 with the page it presided over.
RETIRED: Final = ("Verbosity",)


def header_name(page: str, section: str) -> str:
    """The parameter name for one section divider.

    Derived rather than listed, so a new section cannot collide with an existing one
    by hand. Page and section both go in because "Output" appears on four pages and a
    custom parameter name is unique across the whole COMP, not per page.
    """
    slug = "".join(ch for ch in page + section if ch.isalnum())
    return ("Hdr" + slug).capitalize()


def placement() -> dict[str, tuple[str, int]]:
    """name -> (page, index within the page), for every parameter in the table."""
    out: dict[str, tuple[str, int]] = {}
    for page in PAGE_ORDER:
        index = 0
        for _label, names in LAYOUT[page]:
            for name in names:
                out[name] = (page, index)
                index += 1
    return out


def sections() -> list[tuple[str, str, str, str]]:
    """(page, header parameter name, label, the parameter it sits above).

    A divider with nothing under it is not emitted - see `apply` - because a heading
    over an empty space says a control is missing when it is only switched off.
    """
    out = []
    for page in PAGE_ORDER:
        for label, names in LAYOUT[page]:
            if label and names:
                out.append((page, header_name(page, label), label, names[0]))
    return out


def unplaced(existing: list[str] | tuple[str, ...]) -> list[str]:
    """Parameters on the COMP that this table says nothing about.

    Excludes the dividers themselves and anything in `RETIRED`. The builder treats a
    non-empty result as a failure rather than a note: a control nobody placed is a
    control that lands wherever its builder ran, which is the drift this file exists
    to stop.
    """
    known = set(placement())
    known.update(name for _p, name, _l, _a in sections())
    known.update(RETIRED)
    return sorted(name for name in existing if name not in known)


def duplicates() -> list[str]:
    """Any parameter listed on two pages. A table typo, caught by the tests."""
    seen: dict[str, int] = {}
    for page in PAGE_ORDER:
        for _label, names in LAYOUT[page]:
            for name in names:
                seen[name] = seen.get(name, 0) + 1
    return sorted(name for name, count in seen.items() if count > 1)
