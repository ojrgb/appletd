#!/usr/bin/env python3
"""Run Apple's person segmentation into the shared buffer, and read it back out.

WHAT THIS IS FOR. Seeing the whole pipeline work, end to end, in two real processes,
with no TouchDesigner running and no camera. It is the thing to run before believing
anything about the transport, and the thing to run again when a macOS update lands.

    # the default: replay the fixture, write masks, read them in this process
    ~/.venvs/visionhands/bin/python tools/segmentation_probe.py

    # one quality level, and dump PNGs to look at
    ... tools/segmentation_probe.py --quality fast --png /tmp/masks

    # the two halves separately, in two terminals - this is the shape the
    # sidecar and TouchDesigner will have
    ... tools/segmentation_probe.py --write-only --quality balanced
    ... tools/segmentation_probe.py --read-only

WHY THE FIXTURE AND NOT THE CAMERA. Two reasons and both are standing rules on this
project: the camera usually has another owner and is asked for first, and a live
camera cannot produce a comparable number - `docs/STANDARDS.md` 3 only accepts
timings from deterministic fixture replay. `--camera` is deliberately NOT an option
here; if you want live, that is `visionhands/sidecar.py`'s job.

WHAT THE FIXTURE CONTAINS, since this file is about segmenting people: a torso, an
arm and a hand in a room, framed below the neck. It is gitignored and stays that way,
and `--png` writes wherever you point it, which should not be inside the repo.

Ref: visionhands/segmentation.py, visionhands/maskbuf.py,
     design/DESIGN.md 2.18, docs/BUILD_PLAN.md step 9.
"""

from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from visionhands.maskbuf import MaskReader, MaskWriter, capacity_for  # noqa: E402

FIXTURE = os.path.join(REPO_ROOT, "fixtures", "hand_clip.mp4")
DEFAULT_PATH = "/tmp/visionhands_mask.buf"

# MEASURED 2026-08-22 over the fixture. The buffer is sized for the level being
# written, and `accurate` is 3 MB where `fast` is 49 KB - a 60x spread, which is why
# the capacity is chosen from the quality level rather than being one generous number.
MASK_SIZE = {"fast": (256, 192), "balanced": (512, 384), "accurate": (2016, 1512)}


def decode(limit: int) -> list:
    """The fixture as contiguous BGRA frames, kept alive by the caller.

    cv2 and numpy are imported HERE. TouchDesigner's bundled Python has neither cv2
    nor a use for it, and STANDARDS.md 2 forbids a module-scope import of it anywhere
    that could reach a cook.
    """
    import cv2
    import numpy

    if not os.path.isfile(FIXTURE):
        raise SystemExit("no fixture at %s - record one with tools/record_fixture.py"
                         % FIXTURE)
    capture = cv2.VideoCapture(FIXTURE)
    if not capture.isOpened():
        raise SystemExit("cv2 could not open %s" % FIXTURE)
    frames = []
    while len(frames) < limit:
        ok, bgr = capture.read()
        if not ok:
            break
        # Contiguous AND retained: the pixel buffer built from each of these does not
        # copy, so letting one be collected while Vision reads it corrupts the frame.
        frames.append(numpy.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)))
    capture.release()
    if not frames:
        raise SystemExit("%s decoded zero frames" % FIXTURE)
    return frames


def write(path: str, quality: str, frames_wanted: int, fps: float,
          loop: bool) -> None:
    """Segment the fixture and publish each mask. Runs until the frames run out."""
    from visionhands.engine import pixel_buffer_from_bgra
    from visionhands.segmentation import SegmentationDetector

    frames = decode(frames_wanted)
    height, width = frames[0].shape[:2]
    mask_w, mask_h = MASK_SIZE[quality]
    detector = SegmentationDetector(quality)

    print("writer: %s, %d frames of %dx%d -> mask %dx%d, buffer %d bytes at %s"
          % (quality, len(frames), width, height, mask_w, mask_h,
             capacity_for(mask_w, mask_h), path))
    period = 0.0 if fps <= 0 else 1.0 / fps
    timings, published = [], 0
    with MaskWriter(path, mask_w, mask_h) as writer:
        started = time.monotonic()
        index = 0
        while True:
            bgra = frames[index % len(frames)]
            mask = detector.detect_pixel_buffer(
                pixel_buffer_from_bgra(bgra), seq=index + 1,
                captured_at=time.monotonic())
            if mask is not None:
                writer.write(mask.pixels, width=mask.width, height=mask.height,
                             timestamp=mask.captured_at,
                             source=(width, height))
                published += 1
                if index:               # frame 0 carries Vision's model load
                    timings.append(mask.inference_ms)
            index += 1
            if not loop and index >= len(frames):
                break
            if period:
                while time.monotonic() < started + index * period:
                    pass
    if timings:
        print("writer: %d published, inference median %.2f ms, p95 %.2f, max %.2f"
              % (published, statistics.median(timings),
                 sorted(timings)[int(len(timings) * 0.95)], max(timings)))
    print("writer: %d frames Vision found nobody in" % detector.failures)


def read(path: str, seconds: float, png_dir: str | None) -> int:
    """Poll the buffer at 60 Hz and report. Returns a process exit code."""
    deadline = time.monotonic() + seconds
    for _ in range(int(seconds * 200)):
        if os.path.exists(path):
            break
        time.sleep(0.005)
    if not os.path.exists(path):
        print("reader: nothing at %s" % path)
        return 1

    durations: list[float] = []
    seen: set[int] = set()
    last = None
    with MaskReader(path) as reader:
        # 60 Hz, because that is the rate a Script TOP would ask at.
        nxt = time.monotonic()
        while time.monotonic() < deadline:
            started = time.perf_counter()
            frame = reader.read()
            durations.append((time.perf_counter() - started) * 1e3)
            if frame is not None:
                seen.add(frame.frame)
                last = frame
            nxt += 1.0 / 60.0
            while time.monotonic() < nxt:
                pass
        misses = reader.misses

    if last is None:
        print("reader: %d reads, nothing published" % len(durations))
        return 1
    scale_x, scale_y = last.source_scale
    print("reader: %d reads, %d distinct frames, %d retries"
          % (len(durations), len(seen), misses))
    print("reader: read cost median %.4f ms, p95 %.4f, max %.4f"
          % (statistics.median(durations),
             sorted(durations)[int(len(durations) * 0.95)], max(durations)))
    print("reader: last frame #%d, mask %dx%d, source %dx%d, scale %.3f x %.3f"
          % (last.frame, last.width, last.height, last.source_width,
             last.source_height, scale_x, scale_y))
    if abs(scale_x - scale_y) > 0.01:
        print("        the two scales DIFFER, which is the measured finding: the "
              "mask does not")
        print("        share its source's aspect ratio, so undoing the stretch "
              "needs both (DESIGN.md 2.18)")
    values = set(last.pixels)
    print("reader: %d distinct pixel values%s"
          % (len(values), " - hard alpha, no soft edge" if values <= {0, 255} else ""))

    if png_dir:
        _write_png(last, png_dir)
    return 0


def _write_png(frame, png_dir: str) -> None:
    """Two PNGs: the mask as it arrives, and the mask undistorted to its source.

    Both, because the pair is the argument. Side by side it is obvious that the raw
    mask is the wrong shape and that the source geometry in the header is what fixes
    it - which is a much shorter explanation than any comment.
    """
    import cv2

    os.makedirs(png_dir, exist_ok=True)
    raw = frame.as_numpy()
    cv2.imwrite(os.path.join(png_dir, "mask_raw.png"), raw)
    written = ["mask_raw.png (%dx%d, as published)" % (frame.width, frame.height)]
    if frame.source_width and frame.source_height:
        fitted = cv2.resize(raw, (frame.source_width, frame.source_height),
                            interpolation=cv2.INTER_LINEAR)
        cv2.imwrite(os.path.join(png_dir, "mask_fitted.png"), fitted)
        written.append("mask_fitted.png (%dx%d, stretched back to the source)"
                       % (frame.source_width, frame.source_height))
    print("reader: wrote %s to %s" % (", ".join(written), png_dir))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--quality", default="balanced", choices=sorted(MASK_SIZE))
    parser.add_argument("--frames", type=int, default=60,
                        help="fixture frames to decode (default 60)")
    parser.add_argument("--fps", type=float, default=30.0,
                        help="writer pacing; 0 for flat out (default 30)")
    parser.add_argument("--seconds", type=float, default=3.0,
                        help="how long the reader polls (default 3)")
    parser.add_argument("--png", metavar="DIR",
                        help="write the last mask as PNGs. Not inside the repo")
    parser.add_argument("--write-only", action="store_true",
                        help="publish and exit; loops the fixture")
    parser.add_argument("--read-only", action="store_true",
                        help="read an existing buffer somebody else is writing")
    args = parser.parse_args()

    if args.write_only and args.read_only:
        parser.error("--write-only and --read-only are opposites")

    if args.write_only:
        write(args.path, args.quality, args.frames, args.fps, loop=True)
        return 0
    if args.read_only:
        return read(args.path, args.seconds, args.png)

    # Both halves, the writer in a REAL subprocess. Not a thread: two threads share
    # an address space and an interpreter, so they cannot demonstrate anything about
    # a shared-memory protocol - which is the whole claim being tested.
    print("=" * 70)
    print("segmentation -> mmap -> reader, two processes, %s quality" % args.quality)
    print("=" * 70)
    child = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--write-only",
         "--path", args.path, "--quality", args.quality,
         "--frames", str(args.frames), "--fps", str(args.fps)])
    try:
        code = read(args.path, args.seconds, args.png)
    finally:
        child.terminate()
        child.wait(timeout=10)
    try:
        os.unlink(args.path)
    except FileNotFoundError:
        pass
    print("=" * 70)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
