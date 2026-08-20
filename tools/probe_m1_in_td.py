#!/usr/bin/env python
"""Milestone 1 probe: does the pyobjc + Vision approach work INSIDE TouchDesigner?

Run this by hand, once, in a Text DAT. It is a diagnostic, not part of the
package, and nothing in `visionhands/` imports it.

    HOW TO RUN
      1. In TouchDesigner, create a Text DAT.
      2. Paste this whole file into it.
      3. Right-click the DAT > "Run Script". Watch the Textport (Alt+T).
      4. Tell the agent it has run - the report is written to
         docs/m1_report.txt in the repo, so it can be read directly.

    It also runs standalone, which is useful as a control:
      ~/.venvs/visionhands/bin/python tools/probe_m1_in_td.py

Why this lives in tools/ and not td/: DESIGN.md 5 and STANDARDS.md 2 forbid
`td/*` from importing pyobjc, and importing pyobjc is this file's entire job.

WHAT IT ANSWERS - the two open questions in DESIGN.md 11 that gate everything:

  Q1  Does pyobjc load and run cleanly inside TD's bundled Python 3.11?
      Already half-answered offline: TD ships cp311 (Python.framework/
      Versions/3.11, libboost_python311.dylib) and the venv is cp311/pyobjc
      12.2.2, so the wheel ABI matches. What is untested is the *in-process*
      load, next to TD's own Objective-C runtime and its own numpy.

  Q2  Does TD's existing run loop serve AVFoundation delegate callbacks, so we
      do not need to own a run-loop thread?
      Stage 5 blocks in a plain time.sleep() - no run loop turned by us at all
      - and counts delivered frames. If frames arrive, TD's main run loop (or
      GCD, which needs no run loop) is sufficient and the design loses a
      thread. If none arrive, stage 6 retries with CFRunLoopRunInMode to prove
      the camera itself is fine and a run loop is the missing ingredient.

WHAT IT CANNOT DO. A native crash - a Vision call landing on a torn-down
session, an ABI mismatch - kills the host process and takes TouchDesigner with
it. `except` cannot catch that. The report is therefore flushed to disk line by
line, so if TD dies the last line written is the stage that killed it.

Built against pyobjc 12.2.2 / macOS 26.5.2 (Darwin 25.5.0), Apple silicon.
"""

import os
import sys
import time
import traceback

# ---------------------------------------------------------------------------
# Configuration. Edit these two if the paths or the camera differ.
# ---------------------------------------------------------------------------

# The dev venv's site-packages, APPENDED to sys.path (see stage 2 for why
# appending rather than inserting is load-bearing).
VENV_SITE_PACKAGES = os.path.expanduser(
    "~/.venvs/visionhands/lib/python3.11/site-packages")

# Repo root, used only to place the report. Derived from this file when run
# from disk; when pasted into a Text DAT __file__ does not exist, so fall back.
REPO_ROOT = os.path.expanduser("~/Documents/GitHub/visionhands-touchdesigner")

# Matched case-insensitively against AVCaptureDevice.localizedName(). NOT an
# index: DESIGN.md 3 records that this machine enumerates Camo, OBS Virtual
# Camera and a Continuity iPhone alongside the built-in camera, and index 0 is
# a good way to spend an hour debugging black frames from OBS.
CAMERA_NAME_SUBSTR = "MacBook"

# 1280x720. MEASURED (DESIGN.md 2.1): Vision costs ~3.3 ms here, and 640x480 is
# consistently *slower*, so this is the cheap resolution as well as the accurate
# one. Requested via the output's videoSettings, which is the only control that
# actually works - see stage 5.
CAPTURE_W_PX = 1280
CAPTURE_H_PX = 720

# How long to block waiting for frames. At ~28-30 fps delivered (MEASURED,
# DESIGN.md 2.1) 2 s is ~60 frames: enough to be unambiguous, short enough that
# nobody minds TD's UI freezing for it. This probe deliberately blocks TD's main
# thread; the shipping code never does.
DELIVERY_WINDOW_S = 2.0

# QOS_CLASS_USER_INITIATED. A default-QoS dispatch queue can be scheduled onto
# efficiency cores, which was a large chunk of the run-to-run variance in the
# spike's per-frame timings.
QOS_CLASS_USER_INITIATED = 25

# Vision joint-confidence floor used only for the "did we see a plausible hand"
# line in the report. Nothing downstream depends on it.
REPORT_CONF_GATE = 0.3


# ---------------------------------------------------------------------------
# Reporting
#
# Two audiences: the Textport (live, so a human sees progress) and a file (so
# the agent can read the result without the human transcribing it). Every line
# is flushed immediately - if a native crash takes TD down, the last line on
# disk names the stage that did it, which is the only diagnostic we get.
# ---------------------------------------------------------------------------
class Report:
    def __init__(self, path):
        self.path = path
        self.fh = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.fh = open(path, "w", buffering=1)     # buffering=1 = line buffered
        except OSError as exc:
            # A missing repo path must not stop the probe: the Textport output
            # is still useful, it just has to be copied by hand.
            print("probe: could not open report %s (%s); Textport only" % (path, exc))

    def line(self, text=""):
        print(text)
        if self.fh is not None:
            self.fh.write(text + "\n")
            self.fh.flush()
            os.fsync(self.fh.fileno())    # survive a native crash, not just an exit

    def stage(self, n, title):
        self.line()
        self.line("--- stage %d: %s" % (n, title))

    def fail(self, what):
        """Record an exception with its traceback and keep going where safe."""
        self.line("  FAIL %s" % what)
        for raw in traceback.format_exc().rstrip().splitlines():
            self.line("    %s" % raw)

    def close(self):
        if self.fh is not None:
            self.fh.close()
            self.fh = None


# ---------------------------------------------------------------------------
# stage 1 - where are we running?
# ---------------------------------------------------------------------------
def host_is_touchdesigner():
    """True if this is running inside TouchDesigner's interpreter.

    Why: TD injects the `td` module, and importing it anywhere else raises, so
         this is the cheapest reliable discriminator. Called before the report
         file is opened, because the control run and the real in-TD run must not
         overwrite each other's report.
    """
    try:
        import td  # noqa: F401  (probe only)
        return True
    except ImportError:
        return False


def stage_1_environment(rep, in_td):
    """Describe the interpreter this is running in.

    Contract: returns in_td unchanged, for readability at the call site.
    Why: every later result means something different in TD's process than in a
         bare python, so the report has to say which one produced it.
    """
    rep.stage(1, "environment")
    rep.line("  python      : %s" % sys.version.split()[0])
    rep.line("  executable  : %s" % sys.executable)
    rep.line("  platform    : %s" % sys.platform)

    if in_td:
        rep.line("  host        : TouchDesigner")
        try:
            # app.version exists in TD; guarded because the attribute name has
            # moved between releases and a probe must not die on cosmetics.
            import td as _td
            rep.line("  TD version  : %s" % _td.app.version)
        except Exception:
            rep.line("  TD version  : (unavailable)")
    else:
        rep.line("  host        : standalone python (control run, NOT inside TD)")

    # cp311 is the whole reason the wheels are expected to load. Say so loudly
    # if it ever stops being true after a TD update.
    if sys.version_info[:2] != (3, 11):
        rep.line("  WARNING: interpreter is not 3.11 - the pinned pyobjc wheels "
                 "are cp311 and will not match this ABI")
    return in_td


# ---------------------------------------------------------------------------
# stage 2 - can we import pyobjc in this process?
# ---------------------------------------------------------------------------
def stage_2_import(rep):
    """Append the venv to sys.path and import the four frameworks we need.

    Contract: returns a dict of modules on success, None on failure.
    Why APPEND and never insert(0): TD ships its own numpy, compiled against
         TD's own extension modules. Putting our site-packages *first* would
         shadow TD's numpy with the venv's, which is an ABI mismatch inside
         TD's process - i.e. a crash, not an ImportError. Appending means our
         path is only consulted for names TD does not already provide, which is
         exactly the pyobjc frameworks.
    Traps: `import Vision` is not a pure-Python import - it loads the Vision
         framework and registers Objective-C classes into a runtime TD is
         already using. This is the specific risk milestone 1 exists to retire.
    Ref: DESIGN.md 11 (venv strategy), STANDARDS.md 2.
    """
    rep.stage(2, "pyobjc import")

    if not os.path.isdir(VENV_SITE_PACKAGES):
        rep.line("  FAIL venv site-packages not found: %s" % VENV_SITE_PACKAGES)
        rep.line("       create it with:  ~/.pyenv/versions/3.11.9/bin/python3.11 "
                 "-m venv ~/.venvs/visionhands")
        return None

    if VENV_SITE_PACKAGES not in sys.path:
        sys.path.append(VENV_SITE_PACKAGES)           # INVARIANT: append, never insert
        rep.line("  appended    : %s" % VENV_SITE_PACKAGES)
    else:
        rep.line("  already on sys.path: %s" % VENV_SITE_PACKAGES)

    try:
        import AVFoundation
        import CoreMedia
        import objc
        import Quartz
        import Vision
        from Foundation import NSObject
        from libdispatch import dispatch_queue_attr_make_with_qos_class, dispatch_queue_create
    except Exception:
        rep.fail("could not import pyobjc frameworks")
        return None

    rep.line("  pyobjc      : %s" % objc.__version__)

    # Which numpy won matters: if it resolved to the venv's copy inside TD, the
    # append ordering is not doing what we think and that is worth knowing
    # before a crash tells us the hard way.
    try:
        import numpy
        origin = "venv" if numpy.__file__.startswith(VENV_SITE_PACKAGES) else "host"
        rep.line("  numpy       : %s  (from %s: %s)"
                 % (numpy.__version__, origin, numpy.__file__))
        # Only a problem inside a host that ships its own numpy. Standalone,
        # the venv's copy is the only one there is and resolving to it is right.
        if origin == "venv" and host_is_touchdesigner():
            rep.line("  WARNING: numpy resolved to the venv inside the host process. "
                     "Expected the host's own numpy to win. Check sys.path order.")
    except Exception:
        rep.fail("numpy import")
        return None

    # Only revision 1 of the request exists (MEASURED, DESIGN.md 2.3). If a
    # future macOS adds one, the report will show it and the design gets to
    # decide, rather than silently changing behaviour.
    try:
        req = Vision.VNDetectHumanHandPoseRequest.alloc().init()
        rep.line("  request rev : %d  (supported: %s)"
                 % (req.revision(),
                    list(Vision.VNDetectHumanHandPoseRequest.supportedRevisions())))
    except Exception:
        rep.fail("instantiating VNDetectHumanHandPoseRequest")
        return None

    rep.line("  OK pyobjc + Vision loaded in this process")
    return {
        "objc": objc, "AVF": AVFoundation, "CoreMedia": CoreMedia,
        "Quartz": Quartz, "Vision": Vision, "NSObject": NSObject, "numpy": numpy,
        "qattr": dispatch_queue_attr_make_with_qos_class,
        "qcreate": dispatch_queue_create,
    }


# ---------------------------------------------------------------------------
# stage 3 - does inference actually execute here?
# ---------------------------------------------------------------------------
def stage_3_synthetic_inference(rep, mod):
    """Run Vision on a synthetic buffer - no camera, no fixture, no hand.

    Contract: returns True if the request executed. Zero hands is the expected
              and correct result; we are testing that the call path works, not
              that Vision can find a hand in noise.
    Why: separates "Vision runs in TD's process" from "the camera works in TD's
         process". If stage 5 fails, this stage says which half broke.
    Traps: - `performRequests...error_` is an out-param selector: pass None as
             the trailing argument, get (ok, err) back as a tuple. DESIGN.md
             2.3 calls this the single most common place these scripts fail.
           - CVPixelBufferCreateWithBytes does NOT copy. `frame_bgra` must
             outlive the pixel buffer or Vision reads freed memory.
           - The first call carries model load: MEASURED at ~100 ms to 4.3 s
             (DESIGN.md 2.4). Always warm up before quoting any timing.
    Ref: DESIGN.md 2.3, 2.4.
    """
    rep.stage(3, "synthetic inference (no camera)")
    Quartz, Vision, np = mod["Quartz"], mod["Vision"], mod["numpy"]

    try:
        # Mid-grey with a little structure. Content is irrelevant - a flat frame
        # and a noisy one both cost the same, because Vision normalises
        # internally (MEASURED: maximumProcessingDimensionOnTheLongSide is a
        # no-op, DESIGN.md 2.1).
        frame_bgra = np.full((CAPTURE_H_PX, CAPTURE_W_PX, 4), 128, dtype=np.uint8)
        frame_bgra[::16, :, :] = 64                   # faint stripes, purely cosmetic

        rc, pixel_buffer = Quartz.CVPixelBufferCreateWithBytes(
            None, CAPTURE_W_PX, CAPTURE_H_PX, Quartz.kCVPixelFormatType_32BGRA,
            frame_bgra,                # INVARIANT: not copied - must stay alive
            frame_bgra.strides[0],     # row stride, NOT width*4 in general
            None, None, None, None)
        if rc != 0:
            rep.line("  FAIL CVPixelBufferCreateWithBytes returned %d" % rc)
            return False

        seq_handler = Vision.VNSequenceRequestHandler.alloc().init()
        request = Vision.VNDetectHumanHandPoseRequest.alloc().init()
        request.setMaximumHandCount_(2)

        timings_ms = []
        for attempt in range(4):                      # 1 warm-up + 3 measured
            t0_s = time.perf_counter()
            ok, err = seq_handler.performRequests_onCVPixelBuffer_error_(
                [request], pixel_buffer, None)        # TRAP: trailing None = out-param
            elapsed_ms = (time.perf_counter() - t0_s) * 1e3
            if not ok:
                rep.line("  FAIL performRequests failed on attempt %d: %s" % (attempt, err))
                return False
            timings_ms.append(elapsed_ms)

        rep.line("  first call  : %8.2f ms  (includes model load)" % timings_ms[0])
        rep.line("  warm calls  : %s ms" % ", ".join("%.2f" % v for v in timings_ms[1:]))
        rep.line("  observations: %d  (0 is the expected answer for a grey frame)"
                 % len(request.results() or []))
        rep.line("  OK Vision inference executes in this process")
        return True
    except Exception:
        rep.fail("synthetic inference")
        return False


# ---------------------------------------------------------------------------
# stage 4 - camera permission
# ---------------------------------------------------------------------------
def stage_4_authorization(rep, mod):
    """Report the TCC camera authorisation status for the HOST app.

    Why: the prompt attaches to TouchDesigner.app, not to our code, and an
         unauthorised host delivers frames that never arrive rather than an
         error you can catch. DESIGN.md 8 says check this before diagnosing
         anything else, so the probe checks it before it tries to capture.
    Contract: returns the raw status int. 3 == authorised.
    """
    rep.stage(4, "camera authorisation")
    AVF = mod["AVF"]
    # AVAuthorizationStatus: 0 notDetermined, 1 restricted, 2 denied, 3 authorized.
    names = {0: "notDetermined", 1: "restricted", 2: "denied", 3: "authorized"}
    status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeVideo)
    rep.line("  status      : %d (%s)" % (status, names.get(status, "unknown")))
    if status != 3:
        rep.line("  NOTE: the host app is not authorised yet. Starting the session")
        rep.line("        below will raise the system prompt; click Allow, then RUN")
        rep.line("        THIS PROBE AGAIN - the first run usually delivers 0 frames")
        rep.line("        because the prompt eats the whole delivery window.")
    return status


# ---------------------------------------------------------------------------
# stage 5 / 6 - frame delivery
# ---------------------------------------------------------------------------
def _make_delegate_class(mod):
    """Build the AVCaptureVideoDataOutput delegate class fresh, under a unique name.

    Thread: every method defined here runs on the capture dispatch queue, never
            on TD's main thread.
    Traps: registering an Objective-C class name that already exists raises
           `objc.error: <name> is overriding existing Objective-C class`. In TD
           a Text DAT gets re-run constantly, so a fixed class name fails on the
           second run - and reusing the already-registered class instead would
           silently run the *previous* edit's code. A unique name per run avoids
           both. The classes leak for the life of the process; acceptable for a
           probe, and the reason the shipping engine defines its delegate once
           at import time instead.
    """
    NSObject, Vision, CoreMedia, Quartz = (
        mod["NSObject"], mod["Vision"], mod["CoreMedia"], mod["Quartz"])

    def init_probe(self):
        # pyobjc initialisers must go through objc.super and return self.
        self = mod["objc"].super(type(self), self).init()
        if self is None:
            return None
        # One sequence handler for the whole session, NOT one per frame - a
        # per-frame handler throws away Vision's inter-frame state. DESIGN.md 2.3.
        self.seq_handler = Vision.VNSequenceRequestHandler.alloc().init()
        self.request = Vision.VNDetectHumanHandPoseRequest.alloc().init()
        self.request.setMaximumHandCount_(2)
        self.n_delivered = 0
        self.n_vision_ok = 0
        self.n_with_hand = 0
        self.best_conf = 0.0
        self.delivered_dims_px = None       # what the camera ACTUALLY gave us
        self.vision_ms = []
        self.first_error = None
        self.stopping = False               # checked before touching anything native
        return self

    def capture_output(self, output, sample_buffer, connection):
        # INVARIANT: once `stopping` is set, this method touches nothing native.
        # A Vision call landing on a torn-down session is a process kill, and
        # `except` does not catch it. DESIGN.md 8.
        if self.stopping:
            return
        try:
            pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
            if pixel_buffer is None:
                return
            self.n_delivered += 1
            if self.delivered_dims_px is None:
                # TRAP: assert delivered dimensions against requested. The
                # session preset silently reverts setActiveFormat_, and two
                # spike runs produced 1080p while the log claimed 720p.
                self.delivered_dims_px = (
                    int(Quartz.CVPixelBufferGetWidth(pixel_buffer)),
                    int(Quartz.CVPixelBufferGetHeight(pixel_buffer)))

            t0_s = time.perf_counter()
            ok, err = self.seq_handler.performRequests_onCMSampleBuffer_error_(
                [self.request], sample_buffer, None)   # TRAP: out-param
            self.vision_ms.append((time.perf_counter() - t0_s) * 1e3)
            if not ok:
                if self.first_error is None:
                    self.first_error = str(err)
                return
            self.n_vision_ok += 1

            observations = self.request.results() or []
            if observations:
                self.n_with_hand += 1
                for obs in observations:
                    self.best_conf = max(self.best_conf, float(obs.confidence()))
        except Exception as exc:
            # Record and swallow: an exception raised out of an Objective-C
            # callback unwinds into native frames, which is its own crash. This
            # is the one place swallowing is correct, and it still gets reported.
            if self.first_error is None:
                self.first_error = "python exception: %r" % (exc,)

    return type("VisionHandsProbeDelegate_%d" % time.time_ns(), (NSObject,), {
        "init": init_probe,
        "captureOutput_didOutputSampleBuffer_fromConnection_": capture_output,
    })


def _video_settings(mod):
    """The output-side scale, which is the ONLY resolution control that works.

    Traps: setActiveFormat_ is silently reverted by the session preset (default
           High = 1080p) whether set before or after addInput_, and
           AVCaptureSessionPresetInputPriority - the iOS escape hatch - raises
           NSInvalidArgumentException on macOS. DESIGN.md 3.
    """
    Quartz = mod["Quartz"]
    return {
        # Native 420v would be marginally cheaper (Vision consumes it directly),
        # but BGRA keeps this probe's buffer readable if it ever needs to dump a
        # frame for a human to look at. The engine uses the native format.
        Quartz.kCVPixelBufferPixelFormatTypeKey: Quartz.kCVPixelFormatType_32BGRA,
        Quartz.kCVPixelBufferWidthKey: CAPTURE_W_PX,
        Quartz.kCVPixelBufferHeightKey: CAPTURE_H_PX,
    }


def _pick_device(rep, mod):
    """Find the capture device by NAME, never by index. DESIGN.md 3."""
    AVF = mod["AVF"]
    device_types = [AVF.AVCaptureDeviceTypeBuiltInWideAngleCamera]
    for optional in ("AVCaptureDeviceTypeExternal", "AVCaptureDeviceTypeContinuityCamera"):
        # Guarded: these constants come and go between macOS versions, and a
        # missing one must not take the probe down.
        if hasattr(AVF, optional):
            device_types.append(getattr(AVF, optional))
    session = (AVF.AVCaptureDeviceDiscoverySession
               .discoverySessionWithDeviceTypes_mediaType_position_(
                   device_types, AVF.AVMediaTypeVideo,
                   AVF.AVCaptureDevicePositionUnspecified))
    devices = list(session.devices())
    rep.line("  devices     : %s" % [d.localizedName() for d in devices])
    for device in devices:
        if CAMERA_NAME_SUBSTR.lower() in device.localizedName().lower():
            rep.line("  using       : %s" % device.localizedName())
            return device
    rep.line("  FAIL no device name contains %r" % CAMERA_NAME_SUBSTR)
    return None


def _run_delivery_test(rep, mod, wait_style):
    """Start a capture session, wait in ONE blocking call, count what arrived.

    Contract: returns (n_delivered, delegate) - delegate is None if setup failed.
    Thread:   this function runs on TD's main thread and deliberately blocks it.
              The shipping code never does; a probe may.
    Why two styles: `sleep` turns no run loop at all, so frames arriving under
              it prove TD (or GCD) already serves delivery and DESIGN.md 4.2's
              extra run-loop thread is unnecessary. `runloop` is the spike's
              known-good path, used only as a control if `sleep` gets nothing.
    Traps:  do NOT poll in Python while waiting. A tight
              CFRunLoopRunInMode(..., 0.05, False) loop holds the GIL and
              starved the capture thread from 28 fps down to 8.4 fps (MEASURED,
              DESIGN.md 3). One long blocking call, always. time.sleep() and a
              long CFRunLoopRunInMode both release the GIL; a Python loop
              around either does not.
    """
    AVF = mod["AVF"]

    device = _pick_device(rep, mod)
    if device is None:
        return 0, None

    session = AVF.AVCaptureSession.alloc().init()
    session.beginConfiguration()

    device_input, err = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(
        device, None)                                 # TRAP: out-param
    if device_input is None:
        rep.line("  FAIL deviceInputWithDevice: %s" % err)
        return 0, None
    session.addInput_(device_input)

    output = AVF.AVCaptureVideoDataOutput.alloc().init()
    # Drop late frames rather than queue them: an overloaded consumer should
    # lose frames, not accumulate latency. DESIGN.md 3.
    output.setAlwaysDiscardsLateVideoFrames_(True)
    output.setVideoSettings_(_video_settings(mod))

    delegate = _make_delegate_class(mod).alloc().init()
    queue = mod["qcreate"](
        b"visionhands.probe.capture",
        mod["qattr"](None, QOS_CLASS_USER_INITIATED, 0))
    output.setSampleBufferDelegate_queue_(delegate, queue)
    session.addOutput_(output)
    session.commitConfiguration()

    rep.line("  waiting     : %.1f s via %s (TD's UI will freeze - expected)"
             % (DELIVERY_WINDOW_S, wait_style))
    session.startRunning()
    t0_s = time.perf_counter()
    try:
        if wait_style == "sleep":
            time.sleep(DELIVERY_WINDOW_S)             # no run loop turned by us
        else:
            import CoreFoundation as CF
            CF.CFRunLoopRunInMode(CF.kCFRunLoopDefaultMode, DELIVERY_WINDOW_S, False)
    finally:
        # Teardown order is load-bearing: flag first so the delegate stops
        # touching native objects, then stop the session, then let any callback
        # already in flight finish before anything gets released. DESIGN.md 8.
        delegate.stopping = True
        session.stopRunning()
        time.sleep(0.2)                               # MEASURED-adjacent: spike used 0.2
    elapsed_s = time.perf_counter() - t0_s

    fps = delegate.n_delivered / elapsed_s if elapsed_s > 0 else 0.0
    rep.line("  delivered   : %d frames in %.2f s (%.1f fps)"
             % (delegate.n_delivered, elapsed_s, fps))
    if delegate.delivered_dims_px:
        got_w, got_h = delegate.delivered_dims_px
        verdict = "OK" if (got_w, got_h) == (CAPTURE_W_PX, CAPTURE_H_PX) else "MISMATCH"
        rep.line("  dimensions  : requested %dx%d, delivered %dx%d  <- %s"
                 % (CAPTURE_W_PX, CAPTURE_H_PX, got_w, got_h, verdict))
    if delegate.vision_ms:
        ordered = sorted(delegate.vision_ms)
        rep.line("  vision      : median %.2f ms, min %.2f, max %.2f  (%d ok of %d)"
                 % (ordered[len(ordered) // 2], ordered[0], ordered[-1],
                    delegate.n_vision_ok, delegate.n_delivered))
        rep.line("  hands       : %d frames with >=1 hand, best obs confidence %.2f"
                 % (delegate.n_with_hand, delegate.best_conf))
        rep.line("                (0 is fine if no hand was in shot - this probe")
        rep.line("                 is not a detection test. Wave at the camera to")
        rep.line("                 make this line interesting.)")
    if delegate.first_error:
        rep.line("  first error : %s" % delegate.first_error)
    return delegate.n_delivered, delegate


def stage_5_delivery_without_runloop(rep, mod):
    rep.stage(5, "frame delivery with NO run loop of our own (the Q2 answer)")
    try:
        n_delivered, _ = _run_delivery_test(rep, mod, "sleep")
        return n_delivered
    except Exception:
        rep.fail("delivery test (sleep)")
        return 0


def stage_6_delivery_with_runloop(rep, mod):
    rep.stage(6, "control: frame delivery WITH CFRunLoopRunInMode")
    try:
        n_delivered, _ = _run_delivery_test(rep, mod, "runloop")
        return n_delivered
    except Exception:
        rep.fail("delivery test (runloop)")
        return 0


# ---------------------------------------------------------------------------
def main():
    # Separate files per host: a standalone control run must never clobber the
    # in-TD result, which is the one that actually answers milestone 1.
    in_td = host_is_touchdesigner()
    report_path = os.path.join(
        REPO_ROOT, "docs",
        "m1_report.txt" if in_td else "m1_report_standalone.txt")
    rep = Report(report_path)
    rep.line("=" * 72)
    rep.line("visionhands milestone 1 probe")
    rep.line("=" * 72)

    try:
        stage_1_environment(rep, in_td)
        mod = stage_2_import(rep)
        if mod is None:
            rep.line("\nVERDICT: pyobjc did not load. Nothing else can be tested.")
            return

        vision_ok = stage_3_synthetic_inference(rep, mod)
        auth_status = stage_4_authorization(rep, mod)

        n_sleep = stage_5_delivery_without_runloop(rep, mod)
        n_runloop = 0
        if n_sleep == 0:
            # Only worth running as a control - if sleep already delivered, the
            # question is answered and there is no reason to freeze TD again.
            n_runloop = stage_6_delivery_with_runloop(rep, mod)
        else:
            rep.line()
            rep.line("--- stage 6: skipped (stage 5 already delivered frames)")

        # -------------------------------------------------------------------
        # Verdict. These lines are what get promoted into DESIGN.md 2 and used
        # to close the open questions in DESIGN.md 11, so they are written to be
        # unambiguous rather than short.
        # -------------------------------------------------------------------
        rep.line()
        rep.line("=" * 72)
        rep.line("VERDICT")
        rep.line("  host                 : %s" % ("TouchDesigner" if in_td else "standalone"))
        rep.line("  Q1 pyobjc in-process : %s" % ("PASS" if vision_ok else "FAIL"))
        rep.line("  camera authorised    : %s" % ("yes" if auth_status == 3 else
                                                  "NO (status %d)" % auth_status))
        if n_sleep > 0:
            rep.line("  Q2 run loop needed   : NO - %d frames arrived with no run loop of"
                     % n_sleep)
            rep.line("                         our own. DESIGN.md 4.2 loses a thread.")
        elif n_runloop > 0:
            rep.line("  Q2 run loop needed   : YES - 0 frames under sleep, %d under"
                     % n_runloop)
            rep.line("                         CFRunLoopRunInMode. The engine must own a")
            rep.line("                         run-loop thread (DESIGN.md 4.2).")
        else:
            rep.line("  Q2 run loop needed   : UNKNOWN - no frames arrived either way.")
            rep.line("                         Check authorisation and the camera name")
            rep.line("                         above before reading anything into this.")
        rep.line("=" * 72)
        rep.line("report written to %s" % report_path)
    except Exception:
        rep.fail("probe aborted")
    finally:
        rep.close()


# Run from the shell, __name__ is "__main__". Pasted into a Text DAT it is not,
# so fall back to detecting TD's injected `op` builtin. The elif keeps main()
# from running twice in the case where TD does set __name__ to "__main__".
if __name__ == "__main__":
    main()
elif hasattr(__import__("builtins"), "op"):
    main()
