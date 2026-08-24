#!/usr/bin/env python
"""Ask the camera how many points each face landmark region carries. Prints numbers only.

    RUN IT, from the repo root, with your own face in front of the camera:
        ~/.venvs/appletd/bin/python tools/probe_face_regions.py

    It opens the camera, waits for one face, prints the 12 regions and their point
    counts, and exits. **It writes no image, saves nothing, and keeps nothing** -
    the only thing that leaves this process is a table of integers and the numbers
    it prints.

WHY IT HAS TO EXIST. `appletd/face_types.py` needs one number per region to
publish the landmark channels, and **nothing in the Vision framework will tell you
before a face has been observed**: `VNFaceLandmarks2D` hands back 12 regions, each
with its own `pointCount`, and the counts only exist once there is a face. The
constellation pins the TOTAL at 76 (MEASURED, DESIGN.md 2.12) and says nothing
about the split.

The alternative was to write the counts down from memory or from a blog post. That
fails silently and expensively: a wrong count lays one region's points into
another's channels, and a nose in an eyebrow's channels looks entirely plausible
until somebody wonders why the eyebrows never move. So the numbers get measured on
the machine that will run them, or the channels do not ship.

`--keypoints` ADDS A SECOND REPORT, and it exists for a question a fixture cannot
answer: whether Vision's `leftPupil` is the SUBJECT's left or the IMAGE's left.
`appletd/face_types.py` publishes `f{i}_eye_left` as a faithful mirror of the region
name and says in as many words that the answer is unmeasured; this is what measures
it. It also prints which index the nose tip occupies in each of the three regions
that share it - a confirmation, not an input, because the tip is found by
intersecting those regions on every frame and no index is pinned anywhere.

WHAT TO DO WITH THE OUTPUT. Paste each count into the matching `FaceRegionSpec` in
`appletd/face_types.py`, then re-run **`tools/td_add_filter.py`**. That is the
script that matters: it splits each stream into smoothed and passthrough halves from
`appletd/spaces.py`, and a channel in neither half is dropped inside the filter
group with no error anywhere. The coordinate builder needs no re-run for these -
face landmarks are normalised to the BOUNDING BOX rather than to the image, so they
are deliberately not transformed into world or pixel space (`spaces.py` marks them
`box_relative`).

The import-time self-check refuses a partial table, and refuses one whose counts do
not sum to 76 - so a typo fails at import rather than in a channel list.

THE CAMERA. This is the only tool in the repo that needs it, and it needs it for a
few seconds. If the camera has another owner - a call, a recording - this will fail
to open it and say so rather than fighting for the device.

Ref: appletd/face_types.py (what the numbers are for), DESIGN.md 2.12,
     docs/STANDARDS.md 3 (why no frame is written anywhere).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from appletd.face_types import CONSTELLATION_DISTINCT_POINTS, FACE_REGIONS

# How long to wait for a face before giving up. Long enough to walk into frame,
# short enough that a forgotten process releases the camera on its own.
DEFAULT_TIMEOUT_S = 30.0

# The camera warm-up alone is ~1.5 s (MEASURED, DESIGN.md 2.7), so a poll interval
# below that just spins.
POLL_INTERVAL_S = 0.2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help="seconds to wait for a face (default: %(default)s)")
    parser.add_argument("--camera", default=None,
                        help="substring of the camera name; never an index")
    parser.add_argument("--keypoints", action="store_true",
                        help="also report where the four key points come from: "
                             "which index the nose tip occupies in each of its "
                             "three regions, and whether Vision's `leftPupil` is "
                             "on the left of the IMAGE or the subject's left")
    args = parser.parse_args(argv)

    # Imported here, not at module scope: this is the one tool that pulls in
    # pyobjc, and a `--help` should not.
    from appletd.face import keypoint_report, region_point_report
    from appletd.source import InProcessSource
    from appletd.streams import STREAM_FACE

    print(__doc__.splitlines()[0])
    print("opening the camera - nothing is written to disk, at any point.")
    source = InProcessSource(camera_name=args.camera, streams=(STREAM_FACE,))
    try:
        source.start()
    except Exception as exc:                        # noqa: BLE001
        # Deliberately broad and deliberately terminal: every failure here is
        # "the camera did not open", the reasons are numerous (permission,
        # another owner, no matching device), and none of them is retryable from
        # inside this script. Reported rather than raised, because a traceback
        # would bury the one line that matters.
        print("FAILED to open the camera: %s" % exc)
        print("If something else is using it - a call, a recording - close that "
              "first. Nothing was started.")
        return 1

    deadline = time.monotonic() + args.timeout
    report: dict[str, object] = {}
    points: dict[str, object] = {}
    try:
        while time.monotonic() < deadline and not report:
            time.sleep(POLL_INTERVAL_S)
            frame = source.latest_face()
            report = region_point_report(frame)
            # From the SAME frame as the counts, so the indices printed below
            # belong to the table printed above rather than to whatever the face
            # was doing a fifth of a second later.
            points = keypoint_report(frame) if report else {}
            faces = sum(1 for face in frame.faces if face.found)
            print("  waiting... faces in frame: %d" % faces, end="\r", flush=True)
    finally:
        # In a finally: a Ctrl-C must still release the capture session properly,
        # or the camera is reclaimed by process death instead (DESIGN.md 8).
        source.stop()
    print()

    if not report:
        print("no face was seen in %.0f s, so there is nothing to report."
              % args.timeout)
        for error in source.errors:
            print("  engine error: %s" % error)
        return 1

    counts = report["counts"]
    assert isinstance(counts, dict)
    print("MEASURED on this machine, %s:" % time.strftime("%Y-%m-%d"))
    print()
    for region in FACE_REGIONS:
        count = counts.get(region.name)
        print('    FaceRegionSpec("%s", "%s", %s),'
              % (region.name, region.accessor,
                 "None  # NOT REPORTED" if count is None else count))
    print()

    total, distinct = report["sum"], report["distinct_rounded"]
    assert isinstance(total, int) and isinstance(distinct, int)
    print("sum of the regions:     %d points" % total)
    print("DISTINCT coordinates:   %d points  (exact: %s)"
          % (distinct, report["distinct_exact"]))
    print("the constellation says: %d points" % CONSTELLATION_DISTINCT_POINTS)
    print()

    overlaps = report["overlaps"]
    assert isinstance(overlaps, dict)
    if overlaps:
        # The explanation the first run of this tool could not give: regions that
        # share points make the sum exceed the constellation's total without either
        # number being wrong.
        print("regions that SHARE points (%d pairs):" % len(overlaps))
        for pair, shared in sorted(overlaps.items(), key=lambda kv: -kv[1]):
            print("    %-34s %d shared" % (pair, shared))
        print()
    else:
        print("no region shares a point with another - the regions are DISJOINT.")
        print()

    if distinct == CONSTELLATION_DISTINCT_POINTS:
        print("So the constellation total is intact and the regions overlap: %d - %d"
              % (total, CONSTELLATION_DISTINCT_POINTS))
        print("= %d points carried under two names. Publishing per REGION means"
              % (total - CONSTELLATION_DISTINCT_POINTS))
        print("those %d appear twice, which is the price of every point having a"
              % (total - CONSTELLATION_DISTINCT_POINTS))
        print("meaningful name instead of an index.")
    elif total == distinct:
        print("The regions are disjoint and carry %d distinct points, which is NOT"
              % total)
        print("the %d the constellation constant implies. That assumption is what"
              % CONSTELLATION_DISTINCT_POINTS)
        print("has to change - not these numbers.")
    else:
        print("Neither figure matches the constellation. Do not paste anything in")
        print("until this is understood: the channel list is built from it.")
    print()
    unnamed = sorted(set(counts) - {r.name for r in FACE_REGIONS})
    if unnamed:
        print("this Vision also reported regions our table does not name: %s"
              % unnamed)
    print("Send this whole block back before pasting: the counts are the easy half,")
    print("and what they SUM to decides the shape of the contract.")
    if args.keypoints:
        _print_keypoints(points)
    return 0


def _print_keypoints(points: dict[str, object]) -> None:
    """The `--keypoints` half of the report. Nothing here changes any constant.

    Both numbers it prints are CONFIRMATIONS rather than inputs: the nose tip is
    found by intersecting three regions at runtime, so no index is pinned anywhere
    and none of this has to be pasted back into the package. What it is for is the
    one open question in `face_types.py` - which side of the IMAGE Vision's
    `leftPupil` is on - and the reassurance that the intersection lands on the point
    a person would have chosen.
    """
    print()
    print("-" * 70)
    if not points:
        print("no key points: this face reported no landmarks.")
        return
    print("THE FOUR KEY POINTS, box-normalised, origin bottom left:")
    values = points.get("points")
    assert isinstance(values, dict)
    for name, (x, y) in values.items():
        print("    %-10s %.4f  %.4f%s"
              % (name, x, y, "   <- NOT FOUND" if (x, y) == (0.0, 0.0) else ""))
    print()
    indices = points.get("nose_tip_indices")
    assert isinstance(indices, dict)
    if points.get("nose_tip") is None:
        print("THE NOSE TIP WAS NOT FOUND. `nose`, `nose_crest` and `median_line` do")
        print("not share exactly one point on this machine, which is the assumption")
        print("appletd/face_types.py is built on - send this whole block back.")
    else:
        print("the nose tip is the one point three regions share, and it sits at:")
        for region, index in indices.items():
            print("    %-12s index %s" % (region, index))
        print("Nothing depends on these indices - the point is found by")
        print("intersection on every frame - but they are worth having written down.")
    print()
    side = points.get("left_pupil_is_right_of_image_centre")
    if side is None:
        print("one of the pupils was missing, so which side `leftPupil` is on")
        print("cannot be answered from this frame.")
    elif side:
        print("`leftPupil` is at LARGER x than `rightPupil`, so Vision's `left` is")
        print("the SUBJECT's left and appears on the RIGHT of an unmirrored image.")
    else:
        print("`leftPupil` is at SMALLER x than `rightPupil`, so Vision's `left` is")
        print("the IMAGE's left.")
    print("Either way `f{i}_eye_left` mirrors the region name; this is the sentence")
    print("docs/ATTRIBUTES.md needs, and it is the one thing here worth pasting.")
    missing = points.get("missing")
    if missing:
        print()
        print("regions that reported NO points at all: %s" % missing)


if __name__ == "__main__":
    raise SystemExit(main())
