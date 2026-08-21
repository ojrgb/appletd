#!/usr/bin/env python
"""Spike: hand landmarks out of Apple's Vision framework via pyobjc.

Throwaway interface validation - one image in, one annotated PNG out, plus a
joint table and a timing figure on stdout. No realtime loop, no other ML stack.

Run with the spike venv (NOT the repo .venv):
    ~/.venvs/vision-spike/bin/python tools/spike_vision_hands.py temp/hand.jpg

Built against pyobjc 12.2.2 / macOS 26.5.2 on Apple silicon.
"""

import argparse
import statistics
import time
from pathlib import Path

import Quartz
import Vision
from Foundation import NSURL
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Joint identifiers.
#
# The public constants are long; their *values* are short internal codes
# ('VNHLKWRI' etc) and it is those codes that come back as the keys of the
# recognised-points dict. Two of them are a hazard: ThumbIP is 'VNHLKTIP' and
# IndexTip is 'VNHLKITIP' - one character apart - and the little finger is
# internally 'P' for pinky. Always go through the constants, never hand-type
# the codes.
# ---------------------------------------------------------------------------
V = Vision
JOINTS = [
    ("wrist", V.VNHumanHandPoseObservationJointNameWrist),
    ("thumb_cmc", V.VNHumanHandPoseObservationJointNameThumbCMC),
    ("thumb_mp", V.VNHumanHandPoseObservationJointNameThumbMP),
    ("thumb_ip", V.VNHumanHandPoseObservationJointNameThumbIP),
    ("thumb_tip", V.VNHumanHandPoseObservationJointNameThumbTip),
    ("index_mcp", V.VNHumanHandPoseObservationJointNameIndexMCP),
    ("index_pip", V.VNHumanHandPoseObservationJointNameIndexPIP),
    ("index_dip", V.VNHumanHandPoseObservationJointNameIndexDIP),
    ("index_tip", V.VNHumanHandPoseObservationJointNameIndexTip),
    ("middle_mcp", V.VNHumanHandPoseObservationJointNameMiddleMCP),
    ("middle_pip", V.VNHumanHandPoseObservationJointNameMiddlePIP),
    ("middle_dip", V.VNHumanHandPoseObservationJointNameMiddleDIP),
    ("middle_tip", V.VNHumanHandPoseObservationJointNameMiddleTip),
    ("ring_mcp", V.VNHumanHandPoseObservationJointNameRingMCP),
    ("ring_pip", V.VNHumanHandPoseObservationJointNameRingPIP),
    ("ring_dip", V.VNHumanHandPoseObservationJointNameRingDIP),
    ("ring_tip", V.VNHumanHandPoseObservationJointNameRingTip),
    ("little_mcp", V.VNHumanHandPoseObservationJointNameLittleMCP),
    ("little_pip", V.VNHumanHandPoseObservationJointNameLittlePIP),
    ("little_dip", V.VNHumanHandPoseObservationJointNameLittleDIP),
    ("little_tip", V.VNHumanHandPoseObservationJointNameLittleTip),
]

# (label, colour) per finger, then the bone chains by label name.
FINGERS = [
    ("thumb", (255, 96, 96), ["wrist", "thumb_cmc", "thumb_mp", "thumb_ip", "thumb_tip"]),
    ("index", (255, 196, 64), ["wrist", "index_mcp", "index_pip", "index_dip", "index_tip"]),
    ("middle", (96, 230, 120), ["wrist", "middle_mcp", "middle_pip", "middle_dip", "middle_tip"]),
    ("ring", (96, 176, 255), ["wrist", "ring_mcp", "ring_pip", "ring_dip", "ring_tip"]),
    ("little", (208, 128, 255), ["wrist", "little_mcp", "little_pip", "little_dip", "little_tip"]),
]
# Knuckle line across the palm, so a vertical flip is obvious at a glance.
PALM = ["index_mcp", "middle_mcp", "ring_mcp", "little_mcp"]

CHIRALITY = {
    V.VNChiralityLeft: "left",
    V.VNChiralityRight: "right",
    V.VNChiralityUnknown: "unknown",
}


def load_cgimage(path):
    """Decode to a CGImage. No EXIF transform is applied - the CGImage is the
    raw stored pixel buffer, which is what we also hand to PIL for drawing, so
    the two stay in the same frame of reference."""
    url = NSURL.fileURLWithPath_(str(path))
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        raise SystemExit("could not open image source: %s" % path)
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if cg is None:
        raise SystemExit("could not decode image: %s" % path)
    return cg


def detect(cg, max_hands):
    """One detection pass. Returns (observations, elapsed_seconds).

    Both error-taking selectors below are pyobjc out-parameter methods: you
    pass None for the trailing error: argument and get a tuple back.
    """
    req = Vision.VNDetectHumanHandPoseRequest.alloc().init()
    req.setMaximumHandCount_(max_hands)
    if hasattr(req, "setAllowsCachingOfResults_"):
        # Otherwise a repeated identical request can be served from cache and
        # the timing loop measures nothing.
        req.setAllowsCachingOfResults_(False)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)

    t0 = time.perf_counter()
    ok, err = handler.performRequests_error_([req], None)   # <- out-param
    dt = time.perf_counter() - t0

    if not ok:
        raise SystemExit("performRequests failed: %s" % err)
    return req.results(), dt


def points_for(obs):
    """All 21 joints in one call via the group accessor. Also an out-param."""
    pts, err = obs.recognizedPointsForJointsGroupName_error_(
        Vision.VNHumanHandPoseObservationJointsGroupNameAll, None)   # <- out-param
    if err is not None:
        raise SystemExit("recognizedPointsForJointsGroupName failed: %s" % err)
    return pts


def to_pixels(x, y, w, h):
    """Vision normalises to [0,1] with the origin at the BOTTOM-left, so the y
    axis runs opposite to image row order. Flip it to get a row index."""
    return x * w, (1.0 - y) * h


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image", nargs="?", default="temp/hand.jpg")
    ap.add_argument("-o", "--out", default=None, help="annotated PNG path")
    ap.add_argument("-n", "--runs", type=int, default=20, help="timed runs")
    ap.add_argument("--max-hands", type=int, default=2)
    ap.add_argument("--no-flip", action="store_true",
                    help="skip the y flip, to show what getting it wrong looks like")
    args = ap.parse_args()

    src_path = Path(args.image)
    if not src_path.exists():
        raise SystemExit("no such image: %s" % src_path)
    out_path = Path(args.out) if args.out else src_path.with_name(
        src_path.stem + "_vision_hands.png")

    cg = load_cgimage(src_path)
    w, h = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
    print("image      : %s  (%dx%d)" % (src_path, w, h))

    # ---- detect -----------------------------------------------------------
    observations, first_dt = detect(cg, args.max_hands)
    print("hands found: %d   (first call %.1f ms, includes model load)"
          % (len(observations), first_dt * 1e3))
    if not observations:
        raise SystemExit("no hands detected - nothing to draw")

    # ---- table ------------------------------------------------------------
    per_hand = []
    for hi, obs in enumerate(observations):
        pts = points_for(obs)
        chir = CHIRALITY.get(obs.chirality(), "?(%s)" % obs.chirality())
        print("\nhand %d  chirality=%s  observation_confidence=%.3f  joints=%d"
              % (hi, chir, obs.confidence(), len(pts)))
        print("  %-11s %-10s %8s %8s %8s %8s  %s"
              % ("joint", "code", "norm_x", "norm_y", "px_x", "px_y", "conf"))
        coords = {}
        for label, key in JOINTS:
            p = pts.get(key)
            if p is None:
                print("  %-11s %-10s %s" % (label, key, "-- not returned --"))
                continue
            nx, ny = p.x(), p.y()
            px, py = (nx * w, ny * h) if args.no_flip else to_pixels(nx, ny, w, h)
            coords[label] = (px, py, p.confidence())
            print("  %-11s %-10s %8.4f %8.4f %8.1f %8.1f  %.3f"
                  % (label, key, nx, ny, px, py, p.confidence()))
        per_hand.append(coords)

    # ---- timing -----------------------------------------------------------
    times = []
    for _ in range(args.runs):
        _, dt = detect(cg, args.max_hands)
        times.append(dt * 1e3)
    print("\nVision request only, %d runs (excludes decode + draw):" % args.runs)
    print("  mean %.2f ms   median %.2f ms   min %.2f ms   max %.2f ms   stdev %.2f ms"
          % (statistics.mean(times), statistics.median(times),
             min(times), max(times),
             statistics.stdev(times) if len(times) > 1 else 0.0))

    # ---- draw -------------------------------------------------------------
    im = Image.open(src_path).convert("RGB")
    if im.size != (w, h):
        # Would mean PIL applied an orientation the CGImage did not.
        print("WARNING: PIL size %s != CGImage size %s" % (im.size, (w, h)))
    d = ImageDraw.Draw(im)
    r = max(3, int(min(w, h) * 0.008))

    for coords in per_hand:
        for _, colour, chain in FINGERS:
            for a, b in zip(chain, chain[1:]):
                if a in coords and b in coords:
                    d.line([coords[a][:2], coords[b][:2]], fill=colour, width=max(2, r // 2))
        for a, b in zip(PALM, PALM[1:]):
            if a in coords and b in coords:
                d.line([coords[a][:2], coords[b][:2]], fill=(220, 220, 220), width=max(2, r // 2))
        for label, (px, py, conf) in coords.items():
            colour = next((c for _, c, ch in FINGERS if label in ch and label != "wrist"),
                          (255, 255, 255))
            d.ellipse([px - r, py - r, px + r, py + r], fill=colour, outline=(20, 20, 20))
        if "wrist" in coords:
            px, py, _ = coords["wrist"]
            d.ellipse([px - r * 1.6, py - r * 1.6, px + r * 1.6, py + r * 1.6],
                      fill=(255, 255, 255), outline=(20, 20, 20), width=2)

    im.save(out_path)
    print("\nwrote %s" % out_path)


if __name__ == "__main__":
    main()
