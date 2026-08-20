#!/usr/bin/env python
"""Spike: Apple Vision hand, body and face landmarks on a LIVE camera feed, via pyobjc.

Follow-on from spike_vision_hands.py (single still). Same Vision calls, new
frame source. Purpose here is THROUGHPUT only - latency, dropout, jitter and
chirality stability are explicitly out of scope for this pass.

    ~/.venvs/vision-spike/bin/python reference/spike_vision_hands_live.py --seconds 10

Detectors are switched WITH THE KEYBOARD while the camera runs, because the
question this spike answers - what does adding face landmarks cost - is answered
by toggling one detector against an otherwise identical live scene, not by
comparing two runs of a room that moved in between:

    h hands   b body   3 body3d   f face   o overlay   q quit

Every timing sample is filed under the set that was live when the frame was
captured, so one run prints a table of hands / hands+face / hands+body+face and
the fps each of them sustained. The same flags exist for the starting state
(--no-hands, --no-body, --no-face, --body-3d), and for --source file, which has
no keyboard because a fixed clip is a measurement, not a session.

Capture is AVFoundation -> CMSampleBuffer -> VNSequenceRequestHandler, so the
buffer the camera produces goes into Vision with no conversion at all. The only
numpy/OpenCV work is drawing the annotated MP4, and that is timed separately so
it never contaminates the Vision figure.

MEASURED on the 284-frame fixture clip at 1280x720, replay path, all three 2D
detectors: median 11.1 ms a frame, a ~90 fps ceiling, hands found in 96% of
frames and a body in 47% (the clip is framed from the chin down, so half of it
has too little body to pose).

--body-3d is a different animal, and the key is here mostly to show you why.
It costs ~90 ms a frame, and on the buffer paths this file uses it appears to
detect NOTHING. Measured on one frame, same pixels, four containers:

    VNImageRequestHandler(CGImage)         1 observation, bodyHeight 1.80 m
    VNImageRequestHandler(data)            1 observation
    VNImageRequestHandler(CVPixelBuffer)   0 observations
    VNSequenceRequestHandler(CMSampleBuffer, hand-built)  0 observations

The face request returned its observation through every one of those, so this is
not a broken buffer. UNVERIFIED against a real camera buffer - that needs a run
with the camera free - but expect '3' to cost 90 ms a frame and draw nothing.

Built against pyobjc 12.2.2 / macOS 26.5.2 on Apple silicon.
"""

import argparse
import select
import statistics
import sys
import termios
import threading
import time
import tty
from pathlib import Path

import AVFoundation as AVF
import objc
import CoreFoundation as CF
import CoreMedia
import Quartz
import Vision
import cv2
import numpy as np
from Foundation import NSObject
from libdispatch import dispatch_queue_attr_make_with_qos_class, dispatch_queue_create

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spike_vision_hands import (BODY3D_BONES, BODY_BONES, BODY_JOINTS,  # noqa: E402
                               DETECTOR_ORDER, FACE_REGIONS, FINGERS, JOINTS,
                               PALM, body3d_points, body_points_for,
                               face_landmarks, make_requests, to_pixels)

PIXEL_FORMATS = {
    "bgra": Quartz.kCVPixelFormatType_32BGRA,
    "420v": Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
}

# key -> what it flips. Deliberately not configurable: a spike with a settings
# file for its keybindings has lost the plot.
KEYMAP = (("h", "hands"), ("b", "body"), ("3", "body3d"), ("f", "face"),
          ("o", "overlay"))


# ---------------------------------------------------------------------------
# camera
# ---------------------------------------------------------------------------
def discover(include_virtual=True):
    types = [AVF.AVCaptureDeviceTypeBuiltInWideAngleCamera]
    for extra in ("AVCaptureDeviceTypeExternal", "AVCaptureDeviceTypeContinuityCamera"):
        if hasattr(AVF, extra):
            types.append(getattr(AVF, extra))
    s = AVF.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
        types, AVF.AVMediaTypeVideo, AVF.AVCaptureDevicePositionUnspecified)
    return list(s.devices())


def pick_device(name_substr):
    devs = discover()
    if not devs:
        raise SystemExit("no capture devices found")
    for d in devs:
        if name_substr.lower() in d.localizedName().lower():
            return d
    raise SystemExit("no device matching %r. available: %s"
                     % (name_substr, [d.localizedName() for d in devs]))


def format_dims(fmt):
    dims = CoreMedia.CMVideoFormatDescriptionGetDimensions(fmt.formatDescription())
    return int(dims.width), int(dims.height)


def set_active_format(dev, want):
    """Best-effort: pin the DEVICE to a native WxH sensor mode.

    Unreliable as the sole mechanism. The session's sessionPreset outranks
    activeFormat and the default preset is High, so this gets silently reverted
    by addInput_/addOutput_/commitConfiguration depending on ordering, and
    AVCaptureSessionPresetInputPriority (the iOS escape hatch) does not exist on
    macOS. The guaranteed control is the output-side scale in video_settings();
    this just tries to get the sensor mode near the target so that scale is a
    downsample rather than an upsample.
    """
    chosen = next((f for f in dev.formats() if format_dims(f) == want), None)
    if chosen is None:
        return None
    ok, err = dev.lockForConfiguration_(None)          # <- out-param
    if not ok:
        return None
    try:
        dev.setActiveFormat_(chosen)
    finally:
        dev.unlockForConfiguration()
    return chosen


def video_settings(w, h):
    """Output-side scale. This is what ACTUALLY determines the buffer size Vision
    receives, regardless of what the device's activeFormat ended up as."""
    return {
        Quartz.kCVPixelBufferPixelFormatTypeKey: PIXEL_FORMATS["bgra"],
        Quartz.kCVPixelBufferWidthKey: w,
        Quartz.kCVPixelBufferHeightKey: h,
    }


def buffer_to_bgr(pb):
    """CVPixelBuffer (BGRA) -> contiguous HxWx3 BGR uint8.

    bytesPerRow is NOT always width*4 - the row stride can be padded, so slice
    to width after reshaping or every row shears.
    """
    w = Quartz.CVPixelBufferGetWidth(pb)
    h = Quartz.CVPixelBufferGetHeight(pb)
    bpr = Quartz.CVPixelBufferGetBytesPerRow(pb)
    Quartz.CVPixelBufferLockBaseAddress(pb, 1)          # 1 = kCVPixelBufferLock_ReadOnly
    try:
        base = Quartz.CVPixelBufferGetBaseAddress(pb)   # objc.varlist
        arr = np.frombuffer(base.as_buffer(bpr * h), dtype=np.uint8)
        return np.ascontiguousarray(arr.reshape(h, bpr // 4, 4)[:, :w, :3])
    finally:
        Quartz.CVPixelBufferUnlockBaseAddress(pb, 1)


# ---------------------------------------------------------------------------
# which detectors are live
# ---------------------------------------------------------------------------
class Toggles:
    """Detector on/off state: written by the keyboard thread, read per frame by
    the capture queue.

    No lock, on purpose. The state is one flat dict of bools and a read is a
    dict.copy(), which is a single bytecode and therefore atomic under the GIL,
    so a keypress landing mid-frame takes effect on the next frame rather than
    tearing. A lock here would have to be taken on the capture queue every
    frame, and that queue is the one place this spike must not add contention -
    starving it is exactly the failure that cost 3x the delivered fps when an
    earlier version spun the run loop from Python.
    """

    def __init__(self, state):
        self._state = dict(state)
        self.quit = threading.Event()
        self.changes = 0

    def snapshot(self):
        return self._state.copy()

    def flip(self, name):
        self._state[name] = not self._state[name]
        self.changes += 1
        return self._state[name]

    def line(self):
        st = self._state
        return "  ".join("%s %s" % (name, "ON " if st[name] else "off")
                         for _key, name in KEYMAP)


def key_reader(toggles):
    """Read single keypresses until quit. Runs in a daemon thread.

    stdin is already in cbreak mode (see raw_keys) so this gets one character at
    a time with no echo and no Enter. select() rather than a bare read() so the
    thread notices quit and exits instead of blocking on a dead terminal.

    Note what this thread does NOT do: touch CoreFoundation. Waking the main run
    loop is the timer's job in main(), so every CF call in this program stays on
    the thread that owns the run loop.
    """
    while not toggles.quit.is_set():
        if not select.select([sys.stdin], [], [], 0.2)[0]:
            continue
        ch = sys.stdin.read(1)
        if ch in ("q", "\x03", "\x04"):        # q, ctrl-C, ctrl-D
            print("\n  keys> quit")
            toggles.quit.set()
            return
        for key, name in KEYMAP:
            if ch == key:
                now = toggles.flip(name)
                print("  keys> %s -> %s   [%s]"
                      % (name, "ON" if now else "off", toggles.line()))
                if name == "body3d" and now:
                    print("  keys> body3d costs ~90 ms a frame AND detected nothing "
                          "on every buffer path measured - see the module docstring")
                break
        else:
            print("  keys> %r does nothing. %s"
                  % (ch, "  ".join("%s %s" % (k, n) for k, n in KEYMAP)))


class raw_keys:
    """Put the terminal in cbreak mode for the life of a block, and put it back.

    The restore lives here, on the MAIN thread, rather than in the reader
    thread: a daemon thread's finally: does not run at interpreter exit, and
    leaving a terminal in cbreak mode means the user's next shell prompt has no
    echo and no line editing. Not a tty (piped stdin, a cron run) is not an
    error, it just means no keys.
    """

    def __init__(self, enabled=True):
        self.enabled = enabled and sys.stdin.isatty()
        self.fd = None
        self.saved = None

    def __enter__(self):
        if self.enabled:
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)             # char at a time, ECHO and ICANON off
        return self.enabled

    def __exit__(self, *exc):
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        return False


def apply_max_dim(requests, max_dim):
    """maximumProcessingDimensionOnTheLongSide, where the request has it.

    It is a VNImageBasedRequest property, so most requests take it, but do not
    assume: hasattr rather than a try/except, so a request that lacks it is
    skipped rather than raising mid-frame.
    """
    if not max_dim:
        return
    for req in requests:
        if hasattr(req, "setMaximumProcessingDimensionOnTheLongSide_"):
            req.setMaximumProcessingDimensionOnTheLongSide_(max_dim)


def build_detectors(max_hands, face_points, max_dim=0):
    """{detector name: [request, ...]} - built ONCE, for the whole session.

    Toggling includes or excludes these request objects; it never rebuilds them.
    A VNSequenceRequestHandler carries Vision's inter-frame state per request,
    and handing it a freshly allocated request every frame throws that state
    away (DESIGN.md 2.3) - which is also why the objects outlive being switched
    off.

    MEASURED, because the keyboard depends on it: handing ONE sequence handler a
    DIFFERENT request array on different frames is fine. Cycling five sets
    (hands / hands+body / body / hands+body+face / nothing) every 10 frames over
    a 284-frame clip gave 0 failures and kept detecting throughout.

    Also measured, and worth knowing before crediting the sequence handler for
    anything: over that same clip the 2D body request found a body in 132 of 284
    frames with a shared handler and in exactly 132 with a fresh handler per
    frame. Its inter-frame state buys nothing for body pose here.
    UNVERIFIED: whether a request that sits out a few hundred frames and comes
    back keeps or resets that state.
    """
    reqs = {k: make_requests(k, max_hands, face_points) for k in DETECTOR_ORDER}
    for group in reqs.values():
        apply_max_dim(group, max_dim)
    return reqs


# ---------------------------------------------------------------------------
# vision -> pixels
# ---------------------------------------------------------------------------
def landmarks(obs, w, h):
    pts, err = obs.recognizedPointsForJointsGroupName_error_(
        Vision.VNHumanHandPoseObservationJointsGroupNameAll, None)   # <- out-param
    if err is not None or pts is None:
        return {}
    out = {}
    for label, key in JOINTS:
        p = pts.get(key)
        if p is not None:
            px, py = to_pixels(p.x(), p.y(), w, h)
            out[label] = (px, py, p.confidence())
    return out


def body_landmarks(obs, w, h):
    pts = body_points_for(obs)
    out = {}
    for label, key in BODY_JOINTS:
        p = pts.get(key)
        if p is not None:
            px, py = to_pixels(p.x(), p.y(), w, h)
            out[label] = (px, py, p.confidence())
    return out


# ---------------------------------------------------------------------------
# drawing (cv2 - BGR, hence every colour[::-1])
# ---------------------------------------------------------------------------
def _pt(xy):
    return (int(xy[0]), int(xy[1]))


def draw_chain(frame, coords, chain, colour_bgr, width):
    for a, b in zip(chain, chain[1:]):
        if a in coords and b in coords:
            cv2.line(frame, _pt(coords[a]), _pt(coords[b]), colour_bgr, width, cv2.LINE_AA)


def draw(frame, hands, conf_gate):
    r = max(3, int(min(frame.shape[:2]) * 0.008))
    for coords in hands:
        for _, colour, chain in FINGERS:
            draw_chain(frame, coords, chain, colour[::-1], max(2, r // 2))
        draw_chain(frame, coords, PALM, (220, 220, 220), max(2, r // 2))
        for label, (px, py, conf) in coords.items():
            if conf < conf_gate:
                continue
            colour = next((c for _, c, ch in FINGERS if label in ch and label != "wrist"),
                          (255, 255, 255))
            cv2.circle(frame, (int(px), int(py)), r, colour[::-1], -1, cv2.LINE_AA)
            cv2.circle(frame, (int(px), int(py)), r, (20, 20, 20), 1, cv2.LINE_AA)
        if "wrist" in coords:
            px, py, _ = coords["wrist"]
            cv2.circle(frame, (int(px), int(py)), int(r * 1.6), (255, 255, 255), -1, cv2.LINE_AA)
            cv2.circle(frame, (int(px), int(py)), int(r * 1.6), (20, 20, 20), 2, cv2.LINE_AA)


def draw_bodies(frame, bodies, conf_gate, bones=BODY_BONES):
    r = max(3, int(min(frame.shape[:2]) * 0.008))
    for coords in bodies:
        for colour, chain in bones:
            draw_chain(frame, coords, chain, colour[::-1], max(2, r // 2))
        for _label, xy in coords.items():
            # A 2D joint carries a confidence; a projected 3D joint does not, so
            # the gate is only applied where there is something to gate on.
            if len(xy) > 2 and xy[2] < conf_gate:
                continue
            cv2.circle(frame, _pt(xy), max(2, int(r * 0.8)), (255, 255, 255), -1, cv2.LINE_AA)
            cv2.circle(frame, _pt(xy), max(2, int(r * 0.8)), (20, 20, 20), 1, cv2.LINE_AA)


def draw_faces(frame, faces):
    """faces: [(observation, {region: [(px, py), ...]}), ...]."""
    h, w = frame.shape[:2]
    r = max(3, int(min(h, w) * 0.008))
    for obs, regions in faces:
        box = obs.boundingBox()
        x0, x1 = int(box.origin.x * w), int((box.origin.x + box.size.width) * w)
        y0 = int((1.0 - box.origin.y - box.size.height) * h)
        y1 = int((1.0 - box.origin.y) * h)
        cv2.rectangle(frame, (x0, y0), (x1, y1), (160, 220, 120), max(1, r // 3), cv2.LINE_AA)
        for name, colour, closed in FACE_REGIONS:
            pts = regions.get(name)
            if not pts:
                continue
            bgr = colour[::-1]
            if len(pts) > 1:
                cv2.polylines(frame, [np.array([_pt(p) for p in pts], np.int32)],
                              bool(closed), bgr, max(1, r // 3), cv2.LINE_AA)
            for p in pts:
                cv2.circle(frame, _pt(p), max(1, int(r * 0.35)), bgr, -1, cv2.LINE_AA)


def hud(frame, lines):
    """Text twice, black then white: a HUD that is unreadable against a bright
    wall is a HUD that gets ignored."""
    for i, text in enumerate(lines):
        y = 30 + i * 30
        for colour, thick in (((0, 0, 0), 4), ((255, 255, 255), 2)):
            cv2.putText(frame, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        colour, thick, cv2.LINE_AA)


# ---------------------------------------------------------------------------
# capture delegate
# ---------------------------------------------------------------------------
class HandDelegate(NSObject):
    """Runs on the capture dispatch queue, never the main thread."""

    def initWithConfig_toggles_(self, cfg, toggles):
        self = objc.super(HandDelegate, self).init()
        self.cfg = cfg
        self.toggles = toggles
        self.seq = Vision.VNSequenceRequestHandler.alloc().init()   # once, not per frame
        self.reqs = build_detectors(cfg["max_hands"], cfg["face_points"], cfg["max_dim"])
        # Timings are filed under the detector set that was live for that frame.
        # One dict rather than one list, because the whole point of the keyboard
        # is that a run contains several configurations.
        self.vision_ms = {}
        self.draw_ms = []
        self.n_frames = 0
        self.n_seen = {}                 # detector -> frames with >=1 observation
        self.n_counted = {}              # detector -> frames it actually ran on
        self.writer = None
        self.want_video = False
        self.out_path = None
        self.fps = 30.0
        self.delivered = None            # actual buffer dims, may differ from requested
        self.lock = threading.Lock()
        self.stop = False
        return self

    def ensure_writer(self, w, h):
        """Open the writer against the size the camera ACTUALLY delivers. If the
        writer size and frame size disagree, cv2 drops every frame silently and
        you get a 0-byte MP4 with no error."""
        if self.writer is not None or not self.want_video:
            return
        self.writer = cv2.VideoWriter(
            self.out_path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
        if not self.writer.isOpened():
            raise SystemExit("could not open VideoWriter for %s" % self.out_path)

    def captureOutput_didOutputSampleBuffer_fromConnection_(self, out, sbuf, conn):
        if self.stop:
            return
        pb = CoreMedia.CMSampleBufferGetImageBuffer(sbuf)
        if pb is None:
            return
        w = Quartz.CVPixelBufferGetWidth(pb)
        h = Quartz.CVPixelBufferGetHeight(pb)
        if self.delivered is None:
            self.delivered = (w, h)
            self.ensure_writer(w, h)

        # Read the switches ONCE per frame. Reading them again further down could
        # draw a detector that did not run, or file a timing under a set that was
        # not the set that produced it.
        state = self.toggles.snapshot()
        active = [k for k in DETECTOR_ORDER if state.get(k)]
        requests = [r for k in active for r in self.reqs[k]]

        self.n_frames += 1
        # Skip the exposure ramp: the first frames off a cold camera are black
        # and detect nothing, which would drag the throughput stats.
        counted = self.n_frames > self.cfg["warmup"]

        dt = 0.0
        if requests:
            t0 = time.perf_counter()
            ok, err = self.seq.performRequests_onCMSampleBuffer_error_(   # <- out-param
                requests, sbuf, None)
            dt = (time.perf_counter() - t0) * 1e3
            if not ok:
                if counted:
                    print("  performRequests failed (%s): %s" % ("+".join(active), err))
                return
            if counted:
                self.vision_ms.setdefault("+".join(active), []).append(dt)

        obs = {k: list(self.reqs[k][0].results() or []) for k in active}
        if counted:
            for k in active:
                self.n_counted[k] = self.n_counted.get(k, 0) + 1
                if obs[k]:
                    self.n_seen[k] = self.n_seen.get(k, 0) + 1

        if self.writer is not None:
            t1 = time.perf_counter()
            frame = buffer_to_bgr(pb)
            if state["overlay"]:
                if "body" in obs:
                    draw_bodies(frame, [body_landmarks(o, w, h) for o in obs["body"]],
                                self.cfg["conf_gate"])
                if "body3d" in obs:
                    # metres=False: the overlay only needs the projection, and
                    # the metric read is another selector call per joint.
                    draw_bodies(frame, [body3d_points(o, w, h, metres=False)
                                        for o in obs["body3d"]],
                                self.cfg["conf_gate"], bones=BODY3D_BONES)
                if "face" in obs:
                    draw_faces(frame, [(o, face_landmarks(o, w, h)) for o in obs["face"]])
                if "hands" in obs:
                    draw(frame, [landmarks(o, w, h) for o in obs["hands"]],
                         self.cfg["conf_gate"])
                hud(frame, ["%5.1f ms  %s" % (dt, "+".join(active) or "nothing running"),
                            "  ".join("%s:%d" % (k, len(v)) for k, v in obs.items())
                            or "no detectors"])
            with self.lock:
                if self.writer is not None:
                    self.writer.write(frame)
            if counted:
                self.draw_ms.append((time.perf_counter() - t1) * 1e3)


# ---------------------------------------------------------------------------
def replay(path, size, active, max_hands, face_points, max_dim, reps):
    """Run the identical Vision path over a FIXED clip.

    The live camera cannot give a comparable throughput number: the cost per
    frame depends on what is in shot, and a moving person makes every run a
    different workload. Replaying one recorded clip holds the content constant
    so a resolution, detector or max-dim change is the only variable. No
    keyboard here for the same reason - a set that changes halfway through is a
    session, not a measurement.
    """
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise SystemExit("could not open clip: %s" % path)
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        if size:
            f = cv2.resize(f, size, interpolation=cv2.INTER_AREA)
        # BGR -> BGRA, contiguous, and kept alive: CVPixelBufferCreateWithBytes
        # does NOT copy, so letting the array be collected corrupts the buffer.
        frames.append(np.ascontiguousarray(
            cv2.cvtColor(f, cv2.COLOR_BGR2BGRA)))
    cap.release()
    if not frames:
        raise SystemExit("clip decoded to 0 frames: %s" % path)

    h, w = frames[0].shape[:2]
    seq = Vision.VNSequenceRequestHandler.alloc().init()
    built = build_detectors(max_hands, face_points, max_dim)
    requests = [r for k in active for r in built[k]]

    print("clip   : %s" % path)
    print("frames : %d at %dx%d, %d rep(s)" % (len(frames), w, h, reps))
    print("running: %s" % "+".join(active))
    print("maxProcessingDim: %d (0 = unset)"
          % requests[0].maximumProcessingDimensionOnTheLongSide())

    ms, seen, n_seen = [], 0, {}
    for _rep in range(reps):
        for arr in frames:
            seen += 1
            r, pb = Quartz.CVPixelBufferCreateWithBytes(
                None, w, h, Quartz.kCVPixelFormatType_32BGRA,
                arr, arr.strides[0], None, None, None, None)
            if r != 0:
                raise SystemExit("CVPixelBufferCreateWithBytes failed: %d" % r)
            t0 = time.perf_counter()
            ok, err = seq.performRequests_onCVPixelBuffer_error_(requests, pb, None)  # <- out-param
            dt = (time.perf_counter() - t0) * 1e3
            if not ok:
                raise SystemExit("performRequests failed: %s" % err)
            if seen == 1:
                continue          # first call carries the model load
            ms.append(dt)
            for k in active:
                if built[k][0].results():
                    n_seen[k] = n_seen.get(k, 0) + 1
    print("\n%dx%d replay, %s" % (w, h, "+".join(active)))
    print(stats("vision", ms))
    print("  vision-only ceiling ~%.0f fps (median)" % (1000.0 / statistics.median(ms)))
    for k in active:
        print("  frames with >=1 %-7s %d/%d (%.0f%%)"
              % (k, n_seen.get(k, 0), len(ms), 100.0 * n_seen.get(k, 0) / len(ms)))
    return ms


def stats(name, xs, unit="ms"):
    if not xs:
        return "  %-16s (no samples)" % name
    xs = sorted(xs)
    p95 = xs[min(len(xs) - 1, int(len(xs) * 0.95))]
    return ("  %-16s mean %6.2f  median %6.2f  p95 %6.2f  min %6.2f  max %6.2f %s  n=%d"
            % (name, statistics.mean(xs), statistics.median(xs), p95, xs[0], xs[-1],
               unit, len(xs)))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--device", default="MacBook", help="substring of device name")
    ap.add_argument("--size", default="1280x720", help="WxH, must be a native device format")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--max-hands", type=int, default=2)
    ap.add_argument("--warmup", type=int, default=15, help="frames to skip before counting")
    ap.add_argument("--conf-gate", type=float, default=0.3, help="min joint confidence to draw")
    ap.add_argument("--max-dim", type=int, default=0,
                    help="maximumProcessingDimensionOnTheLongSide (0 = leave unset)")
    ap.add_argument("--no-hands", action="store_true", help="start with hands off")
    ap.add_argument("--no-body", action="store_true", help="start with 2D body pose off")
    ap.add_argument("--no-face", action="store_true", help="start with face landmarks off")
    ap.add_argument("--body-3d", action="store_true",
                    help="start with the 3D body request ON (~90 ms a frame, and it "
                         "detected nothing on any buffer path - see the docstring)")
    ap.add_argument("--face-points", type=int, choices=(65, 76), default=76,
                    help="face landmark constellation")
    ap.add_argument("--no-keys", action="store_true",
                    help="do not touch the terminal; detectors stay as the flags left them")
    ap.add_argument("--no-video", action="store_true", help="pure throughput, no MP4")
    ap.add_argument("--no-overlay", action="store_true",
                    help="write the MP4 WITHOUT landmarks - use this to record a clean "
                         "clip for --source file")
    ap.add_argument("--source", choices=("camera", "file"), default="camera")
    ap.add_argument("--input", default=None, help="clip path for --source file")
    ap.add_argument("--reps", type=int, default=1, help="replay passes over the clip")
    ap.add_argument("-o", "--out", default="temp/hand_live.mp4")
    args = ap.parse_args()

    start_state = {"hands": not args.no_hands, "body": not args.no_body,
                   "body3d": args.body_3d, "face": not args.no_face,
                   "overlay": not args.no_overlay}
    active = [k for k in DETECTOR_ORDER if start_state[k]]
    if not active:
        raise SystemExit("every detector starts switched off - nothing to measure")

    if args.source == "file":
        if not args.input:
            raise SystemExit("--source file needs --input PATH")
        size = tuple(int(v) for v in args.size.lower().split("x")) if args.size else None
        replay(args.input, size, active, args.max_hands, args.face_points,
               args.max_dim, args.reps)
        return

    if args.list_devices:
        for d in discover():
            dims = sorted({format_dims(f) for f in d.formats()})
            print("%-28s %s" % (d.localizedName(), dims))
        return

    auth = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeVideo)
    if auth != 3:
        raise SystemExit("camera not authorized (status %d). Grant camera access to the "
                         "host app (Terminal/VSCode) in System Settings > Privacy." % auth)

    w, h = (int(v) for v in args.size.lower().split("x"))
    dev = pick_device(args.device)
    print("device : %s" % dev.localizedName())

    sess = AVF.AVCaptureSession.alloc().init()
    sess.beginConfiguration()

    inp, err = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(dev, None)  # <- out-param
    if inp is None:
        raise SystemExit("deviceInputWithDevice failed: %s" % err)
    sess.addInput_(inp)

    # ORDER MATTERS. activeFormat must be set AFTER addInput_ - adding an input
    # resets the device to whatever the sessionPreset says, and the default
    # preset is High, so an activeFormat set beforehand is silently discarded
    # and you get 1080p. Setting it here flips the session to input-priority
    # implicitly; AVCaptureSessionPresetInputPriority itself is iOS-only and
    # raises NSInvalidArgumentException on macOS.
    fmt = set_active_format(dev, (w, h))
    print("format : requested %dx%d  (device native mode: %s)"
          % (w, h, "%dx%d" % format_dims(fmt) if fmt else "no exact match, will scale"))

    out = AVF.AVCaptureVideoDataOutput.alloc().init()
    out.setAlwaysDiscardsLateVideoFrames_(True)
    out.setVideoSettings_(video_settings(w, h))

    cfg = {"max_hands": args.max_hands, "warmup": args.warmup,
           "conf_gate": args.conf_gate, "face_points": args.face_points,
           "max_dim": args.max_dim}
    toggles = Toggles(start_state)
    delegate = HandDelegate.alloc().initWithConfig_toggles_(cfg, toggles)
    print("maxProcessingDim: %d (0 = unset)"
          % delegate.reqs["hands"][0].maximumProcessingDimensionOnTheLongSide())

    if not args.no_video:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        nominal = 1.0 / CoreMedia.CMTimeGetSeconds(dev.activeVideoMinFrameDuration())
        delegate.want_video = True
        delegate.out_path = args.out
        delegate.fps = min(nominal, 30.0)   # writer opens on frame 1, at real dims

    # Pin the capture queue to a high QoS. A default-QoS queue can be scheduled
    # onto efficiency cores, which is a large chunk of the run-to-run variance in
    # the per-frame timings. 25 = QOS_CLASS_USER_INITIATED.
    attr = dispatch_queue_attr_make_with_qos_class(None, 25, 0)
    queue = dispatch_queue_create(b"vision.hands.capture", attr)
    out.setSampleBufferDelegate_queue_(delegate, queue)
    sess.addOutput_(out)
    sess.commitConfiguration()

    print("capturing %.1f s ... (camera is live)" % args.seconds)
    print("start  : [%s]" % toggles.line())
    with raw_keys(not args.no_keys) as keys_live:
        if keys_live:
            print("keys   : %s   q quit"
                  % "   ".join("%s %s" % (k, n) for k, n in KEYMAP))
            threading.Thread(target=key_reader, args=(toggles,), daemon=True).start()
        else:
            print("keys   : disabled (%s)"
                  % ("--no-keys" if args.no_keys else "stdin is not a tty"))

        sess.startRunning()
        t0 = time.perf_counter()

        # A timer on THIS run loop, rather than a short CFRunLoopRunInMode from
        # Python, is how the keyboard's q gets to interrupt the wait. The one
        # long call below is load-bearing: AVFoundation delivers frames via the
        # delegate only while a run loop turns, and CFRunLoopRunInMode releases
        # the GIL while it waits, so spinning it at 0.05 s from Python holds the
        # GIL and starves the capture queue - that alone cut delivered fps by
        # ~3x. A 4 Hz timer callback costs a few microseconds of GIL instead,
        # and keeps every CF call on the thread that owns the loop.
        def tick(timer, info):
            if toggles.quit.is_set():
                CF.CFRunLoopStop(CF.CFRunLoopGetCurrent())

        timer = CF.CFRunLoopTimerCreate(
            None, CF.CFAbsoluteTimeGetCurrent() + 0.25, 0.25, 0, 0, tick, None)
        CF.CFRunLoopAddTimer(CF.CFRunLoopGetCurrent(), timer, CF.kCFRunLoopDefaultMode)
        try:
            while not toggles.quit.is_set():
                remaining = args.seconds - (time.perf_counter() - t0)
                if remaining <= 0:
                    break
                CF.CFRunLoopRunInMode(CF.kCFRunLoopDefaultMode, remaining, False)
        finally:
            CF.CFRunLoopRemoveTimer(CF.CFRunLoopGetCurrent(), timer,
                                    CF.kCFRunLoopDefaultMode)
            toggles.quit.set()          # release the key thread's select loop
        elapsed = time.perf_counter() - t0

    delegate.stop = True
    sess.stopRunning()
    time.sleep(0.2)                      # let any in-flight callback finish
    with delegate.lock:
        if delegate.writer is not None:
            delegate.writer.release()
            delegate.writer = None

    counted = sum(len(v) for v in delegate.vision_ms.values())
    got = delegate.delivered or (0, 0)
    if got != (w, h):
        print("\nWARNING: requested %dx%d but camera delivered %dx%d" % (w, h, got[0], got[1]))
    print("\n%dx%d delivered, %.2f s wall" % (got[0], got[1], elapsed))
    print("  frames delivered %d  (%.1f fps delivered, %d counted after warmup, "
          "%d detector changes)"
          % (delegate.n_frames, delegate.n_frames / elapsed, counted, toggles.changes))
    print("  ended with [%s]" % toggles.line())

    # One row per detector set that was live at some point, biggest sample first,
    # because with the keyboard in play a run is several measurements.
    print("\nvision, by detector set:")
    for label, xs in sorted(delegate.vision_ms.items(), key=lambda kv: -len(kv[1])):
        print(stats(label, xs))
        print("      -> ~%.0f fps ceiling  (%.0f%% of a 33.3 ms/30fps budget)"
              % (1000.0 / statistics.mean(xs), 100.0 * statistics.mean(xs) / 33.3))
    if delegate.draw_ms:
        print(stats("draw+encode", delegate.draw_ms))
    if delegate.n_counted:
        print("\ndetection rate, over the frames each one actually ran on:")
        for k in DETECTOR_ORDER:
            ran = delegate.n_counted.get(k, 0)
            if ran:
                print("  %-7s %d/%d frames with >=1 observation (%.0f%%)"
                      % (k, delegate.n_seen.get(k, 0), ran,
                         100.0 * delegate.n_seen.get(k, 0) / ran))
    if not args.no_video:
        print("\nwrote %s" % args.out)


if __name__ == "__main__":
    main()
