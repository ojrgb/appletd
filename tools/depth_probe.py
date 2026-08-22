#!/usr/bin/env python3
"""Run Depth Anything V2 into the shared buffer, and read it back out.

THE FIRST THING TO RUN after `./tools/fetch_models.sh`. It proves the model
downloaded, compiles, loads and produces a map, and that the map survives the shared
buffer - with no camera, no TouchDesigner, and nothing to set up.

    # the default: replay the fixture, publish maps, read them in this process
    ~/.venvs/visionhands/bin/python tools/depth_probe.py

    # with pins, so the numbers come out in metres
    ... tools/depth_probe.py --pins "0.06,0.30,2.5 0.94,0.30,2.4 0.50,0.96,1.1"

    # dump PNGs to look at
    ... tools/depth_probe.py --png /tmp/depth

    # the two halves separately, in two terminals - the shape the sidecar has
    ... tools/depth_probe.py --write-only
    ... tools/depth_probe.py --read-only

WHAT IT WILL TELL YOU, and why each number is here:

  * INFERENCE, per frame. MEASURED at 23.00 ms on this machine, which is most of a
    30 fps frame interval on its own. If yours is much worse, `--compute` is the first
    thing to try - `cpu` is several times slower than `all`, and a machine that has
    fallen back to it silently is the usual explanation.
  * DISTINCT LEVELS in the model's own output. ~7,300 in a typical frame. If you ever
    see 256, something has quantised the fp16 map to 8-bit and the pin solve is
    running on a sixth of the precision it should have.
  * THE FIT, if you gave it pins: alpha, beta, and the residual. Read the residual
    warning below before trusting it.

WHY THE FIXTURE AND NOT THE CAMERA. The camera usually has another owner and has to be
asked for, and `docs/STANDARDS.md` 3 only accepts timings from deterministic fixture
replay. For live depth, enable it in the sidecar - that is what it is for.

Ref: docs/DEPTH.md, visionhands/depth.py, visionhands/pins.py, DESIGN.md 2.22.
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

from visionhands.maskbuf import (  # noqa: E402
    DTYPE_F16,
    MaskReader,
    MaskWriter,
    capacity_for,
)
from visionhands.pins import format_pins, parse_pins, unpack  # noqa: E402

FIXTURE = os.path.join(REPO_ROOT, "fixtures", "hand_clip.mp4")
DEFAULT_PATH = "/tmp/visionhands_depth.buf"


def decode(limit: int) -> list:
    """The fixture as contiguous BGRA frames, kept alive by the caller.

    cv2 and numpy are imported HERE, not at module scope: TouchDesigner's bundled
    Python has neither cv2 nor a use for it, and STANDARDS.md 2 forbids a module-scope
    import of it anywhere that could reach a cook.
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
        # copy, so letting one be collected while Core ML reads it corrupts the frame.
        frames.append(numpy.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)))
    capture.release()
    if not frames:
        raise SystemExit("%s decoded zero frames" % FIXTURE)
    return frames


def write(path: str, pin_text: str, frames_wanted: int, fps: float,
          loop: bool, compute: str) -> None:
    """Run the model over the fixture and publish each map."""
    import numpy

    from visionhands.depth import DepthDetector
    from visionhands.engine import pixel_buffer_from_bgra

    pins = parse_pins(pin_text) if pin_text.strip() else ()
    frames = decode(frames_wanted)
    height, width = frames[0].shape[:2]
    detector = DepthDetector(pins=pins, compute=compute)

    print("writer: %s, input %dx%d, model %dx%d, compute=%s, %d pin%s"
          % (detector.model_path.name, width, height,
             detector.input_size[0], detector.input_size[1], compute,
             len(pins), "" if len(pins) == 1 else "s"))
    if pins:
        print("writer: pins %s" % format_pins(pins))

    period = 0.0 if fps <= 0 else 1.0 / fps
    timings: list[float] = []
    levels: list[int] = []
    writer = None
    started = time.monotonic()
    index = 0
    try:
        while True:
            bgra = frames[index % len(frames)]
            frame = detector.detect_pixel_buffer(
                pixel_buffer_from_bgra(bgra), seq=index + 1,
                captured_at=time.monotonic())
            if frame is not None:
                if writer is None:
                    writer = MaskWriter(path, frame.width, frame.height,
                                        dtype=DTYPE_F16)
                    print("writer: buffer %s, %dx%d fp16, %d bytes"
                          % (path, frame.width, frame.height, writer.capacity))
                writer.write(frame.pixels, width=frame.width, height=frame.height,
                             timestamp=frame.captured_at, source=(width, height),
                             aux=frame.fit.pack())
                if index:               # frame 0 carries the model load
                    timings.append(frame.inference_ms)
                if index % 10 == 0:
                    array = numpy.frombuffer(frame.pixels, dtype=numpy.float16)
                    levels.append(int(numpy.unique(array).size))
            index += 1
            if not loop and index >= len(frames):
                break
            if period:
                while time.monotonic() < started + index * period:
                    pass
    finally:
        if writer is not None:
            writer.close()

    if timings:
        print("writer: inference median %.2f ms, p95 %.2f, max %.2f"
              % (statistics.median(timings),
                 sorted(timings)[int(len(timings) * 0.95)], max(timings)))
        # 30 fps is 33.3 ms. Said as a comparison rather than a number, because "23 ms"
        # only means something next to the interval it has to fit inside.
        median = statistics.median(timings)
        print("writer: that is %.0f%% of a 30 fps frame interval%s"
              % (100.0 * median / 33.3,
                 " - depth alone cannot hold 30 fps here" if median > 33.3 else ""))
    if levels:
        print("writer: %d distinct fp16 levels per frame (an 8-bit read caps at 256)"
              % statistics.median(levels))
    print("writer: %d frames Core ML returned nothing for" % detector.failures)


def read(path: str, seconds: float, png_dir: str | None) -> int:
    """Poll the buffer at 60 Hz and report. Returns a process exit code."""
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
    deadline = time.monotonic() + seconds
    with MaskReader(path) as reader:
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
    print("reader: %d reads, %d distinct frames, %d retries"
          % (len(durations), len(seen), misses))
    print("reader: read cost median %.4f ms, p95 %.4f, max %.4f"
          % (statistics.median(durations),
             sorted(durations)[int(len(durations) * 0.95)], max(durations)))
    scale_x, scale_y = last.source_scale
    print("reader: last frame #%d, map %dx%d fp16, source %dx%d, scale %.3f x %.3f"
          % (last.frame, last.width, last.height, last.source_width,
             last.source_height, scale_x, scale_y))

    fit = unpack(last.aux)
    if not fit:
        print("reader: no fit in the aux block - the writer had no pins, so the map "
              "is RELATIVE")
        print("        (bigger = nearer, and the scale changes every frame - see "
              "docs/DEPTH.md)")
    else:
        print("reader: fit alpha=%.4f beta=%.4f from %d pins%s"
              % (fit["alpha"], fit["beta"], int(fit["used"]),
                 "" if not fit["dropped"] else ", dropped #%d" % int(fit["dropped"])))
        if fit["residual_is_a_check"]:
            print("        worst residual %.3f m - THIS is the number that says "
                  "whether to trust it" % fit["worst_residual_m"])
        else:
            print("        residual %.3f m means NOTHING with %d pins: two points "
                  "always fit a line exactly."
                  % (fit["worst_residual_m"], int(fit["used"])))
            print("        Add a third pin at a different distance to get a real "
                  "check.")
    if png_dir:
        _write_png(last, fit, png_dir)
    return 0


def _write_png(frame, fit: dict, png_dir: str) -> None:
    """The raw map, and the metric one if there is a fit. Both, because the pair is
    the argument: the raw one breathes as things move and the metric one should not."""
    import cv2
    import numpy

    from visionhands.pins import Solve, metres

    os.makedirs(png_dir, exist_ok=True)
    raw = frame.as_numpy().astype(numpy.float32)
    # Stretched for DISPLAY ONLY, and only here. Every number upstream is the model's
    # own; a PNG needs 0..255 and cannot be given anything else, so this is the last
    # possible place to spend the precision.
    span = max(float(raw.max() - raw.min()), 1e-9)
    eight = ((raw - raw.min()) / span * 255.0).astype(numpy.uint8)
    cv2.imwrite(os.path.join(png_dir, "depth_raw.png"),
                cv2.applyColorMap(eight, cv2.COLORMAP_TURBO))
    written = ["depth_raw.png (per-frame range, near = red)"]
    if fit:
        solve = Solve(fit["alpha"], fit["beta"], tuple(range(int(fit["used"]))),
                      None, (), fit["frame_range"], "")
        metric = metres(raw, solve)
        if metric is not None:
            # A FIXED metre window, which is the whole point: the raw panel breathes
            # as things move because of the reduce_max, and a fixed window is where
            # you can see that the corrected one does not.
            lo, hi = 0.4, 3.0
            t = numpy.clip((metric - lo) / (hi - lo), 0.0, 1.0)
            cv2.imwrite(os.path.join(png_dir, "depth_metres.png"),
                        cv2.applyColorMap(((1.0 - t) * 255).astype(numpy.uint8),
                                          cv2.COLORMAP_TURBO))
            written.append("depth_metres.png (fixed %.1f-%.1f m window)" % (lo, hi))
    print("reader: wrote %s to %s" % (", ".join(written), png_dir))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--pins", default="", metavar="X,Y,M X,Y,M ...",
                        help="points of known distance. Two minimum, three for a "
                             "residual that means anything")
    parser.add_argument("--frames", type=int, default=40,
                        help="fixture frames to decode (default 40)")
    parser.add_argument("--fps", type=float, default=0.0,
                        help="writer pacing; 0 for flat out (default 0)")
    parser.add_argument("--seconds", type=float, default=3.0,
                        help="how long the reader polls (default 3)")
    parser.add_argument("--compute", default="all",
                        choices=("all", "cpu", "gpu", "ane"))
    parser.add_argument("--png", metavar="DIR",
                        help="write the last map as PNGs. Not inside the repo")
    parser.add_argument("--write-only", action="store_true",
                        help="publish and exit; loops the fixture")
    parser.add_argument("--read-only", action="store_true",
                        help="read an existing buffer somebody else is writing")
    args = parser.parse_args()

    if args.write_only and args.read_only:
        parser.error("--write-only and --read-only are opposites")

    if args.write_only:
        write(args.path, args.pins, args.frames, args.fps, True, args.compute)
        return 0
    if args.read_only:
        return read(args.path, args.seconds, args.png)

    print("=" * 70)
    print("Depth Anything V2 -> mmap -> reader, two processes")
    print("=" * 70)
    child = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--write-only",
         "--path", args.path, "--frames", str(args.frames), "--fps", str(args.fps),
         "--compute", args.compute] + (["--pins", args.pins] if args.pins else []))
    try:
        code = read(args.path, args.seconds, args.png)
    finally:
        child.terminate()
        child.wait(timeout=30)
    try:
        os.unlink(args.path)
    except FileNotFoundError:
        pass
    print("=" * 70)
    print("Buffer for a %dx%d fp16 map: %d bytes."
          % (518, 392, capacity_for(518, 392, dtype=DTYPE_F16)))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
