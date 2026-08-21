#!/usr/bin/env python
"""Ask the camera how many points each face landmark region carries. Prints numbers only.

    RUN IT, from the repo root, with your own face in front of the camera:
        ~/.venvs/visionhands/bin/python tools/probe_face_regions.py

    It opens the camera, waits for one face, prints the 12 regions and their point
    counts, and exits. **It writes no image, saves nothing, and keeps nothing** -
    the only thing that leaves this process is a table of integers and the numbers
    it prints.

WHY IT HAS TO EXIST. `visionhands/face_types.py` needs one number per region to
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

WHAT TO DO WITH THE OUTPUT. Paste each count into the matching `FaceRegionSpec` in
`visionhands/face_types.py`, then re-run **`tools/td_add_filter.py`**. That is the
script that matters: it splits each stream into smoothed and passthrough halves from
`visionhands/spaces.py`, and a channel in neither half is dropped inside the filter
group with no error anywhere. The coordinate builder needs no re-run for these -
face landmarks are normalised to the BOUNDING BOX rather than to the image, so they
are deliberately not transformed into world or pixel space (`spaces.py` marks them
`box_relative`).

The import-time self-check refuses a partial table, and refuses one whose counts do
not sum to 76 - so a typo fails at import rather than in a channel list.

THE CAMERA. This is the only tool in the repo that needs it, and it needs it for a
few seconds. If the camera has another owner - a call, a recording - this will fail
to open it and say so rather than fighting for the device.

Ref: visionhands/face_types.py (what the numbers are for), DESIGN.md 2.12,
     docs/STANDARDS.md 3 (why no frame is written anywhere).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from visionhands.face_types import EXPECTED_TOTAL_POINTS, FACE_REGIONS

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
    args = parser.parse_args(argv)

    # Imported here, not at module scope: this is the one tool that pulls in
    # pyobjc, and a `--help` should not.
    from visionhands.face import region_point_counts
    from visionhands.source import InProcessSource
    from visionhands.streams import STREAM_FACE

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
    counts: dict[str, int] = {}
    try:
        while time.monotonic() < deadline and not counts:
            time.sleep(POLL_INTERVAL_S)
            counts = region_point_counts(source.latest_face())
            faces = sum(1 for face in source.latest_face().faces if face.found)
            print("  waiting... faces in frame: %d" % faces, end="\r", flush=True)
    finally:
        # In a finally: a Ctrl-C must still release the capture session properly,
        # or the camera is reclaimed by process death instead (DESIGN.md 8).
        source.stop()
    print()

    if not counts:
        print("no face was seen in %.0f s, so there is nothing to report."
              % args.timeout)
        for error in source.errors:
            print("  engine error: %s" % error)
        return 1

    print("MEASURED on this machine, %s:" % time.strftime("%Y-%m-%d"))
    print()
    total = 0
    for region in FACE_REGIONS:
        count = counts.get(region.name)
        total += count or 0
        print('    FaceRegionSpec("%s", "%s", %s),'
              % (region.name, region.accessor,
                 "None  # NOT REPORTED" if count is None else count))
    print()
    print("total: %d points (the 76-point constellation expects %d)"
          % (total, EXPECTED_TOTAL_POINTS))
    if total != EXPECTED_TOTAL_POINTS:
        print("MISMATCH. Do not paste these in until it is understood: the channel")
        print("list is built from them, and face_types.py's self-check will refuse")
        print("a table that does not sum to the constellation's total.")
        return 1
    unnamed = sorted(set(counts) - {r.name for r in FACE_REGIONS})
    if unnamed:
        print("this Vision also reported regions our table does not name: %s"
              % unnamed)
    print()
    print("Paste the block above over FACE_REGIONS in visionhands/face_types.py,")
    print("then re-run tools/td_add_filter.py - that is what classifies the new")
    print("channels, and anything it does not classify is dropped silently.")
    print("The coordinate builder needs no re-run: face landmarks are normalised")
    print("to the BOUNDING BOX, not the image, so they are not transformed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
