#!/usr/bin/env python
"""Spike: Apple Vision hand landmarks on a LIVE camera feed, via pyobjc.

Follow-on from spike_vision_hands.py (single still). Same Vision calls, new
frame source. Purpose here is THROUGHPUT only - latency, dropout, jitter and
chirality stability are explicitly out of scope for this pass.

    ~/.venvs/vision-spike/bin/python tools/spike_vision_hands_live.py --seconds 10

Capture is AVFoundation -> CMSampleBuffer -> VNSequenceRequestHandler, so the
buffer the camera produces goes into Vision with no conversion at all. The only
numpy/OpenCV work is drawing the annotated MP4, and that is timed separately so
it never contaminates the Vision figure.

Built against pyobjc 12.2.2 / macOS 26.5.2 on Apple silicon.
"""

import argparse
import statistics
import sys
import threading
import time
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
from spike_vision_hands import CHIRALITY, FINGERS, JOINTS, PALM, to_pixels  # noqa: E402

PIXEL_FORMATS = {
    "bgra": Quartz.kCVPixelFormatType_32BGRA,
    "420v": Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
}


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
# vision
# ---------------------------------------------------------------------------
def make_request(max_hands):
    req = Vision.VNDetectHumanHandPoseRequest.alloc().init()
    req.setMaximumHandCount_(max_hands)
    return req


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


def draw(frame, hands, conf_gate):
    r = max(3, int(min(frame.shape[:2]) * 0.008))
    for coords in hands:
        for _, colour, chain in FINGERS:
            bgr = colour[::-1]
            for a, b in zip(chain, chain[1:]):
                if a in coords and b in coords:
                    cv2.line(frame, tuple(map(int, coords[a][:2])),
                             tuple(map(int, coords[b][:2])), bgr, max(2, r // 2), cv2.LINE_AA)
        for a, b in zip(PALM, PALM[1:]):
            if a in coords and b in coords:
                cv2.line(frame, tuple(map(int, coords[a][:2])),
                         tuple(map(int, coords[b][:2])), (220, 220, 220), max(2, r // 2), cv2.LINE_AA)
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


# ---------------------------------------------------------------------------
# capture delegate
# ---------------------------------------------------------------------------
class HandDelegate(NSObject):
    """Runs on the capture dispatch queue, never the main thread."""

    def initWithConfig_(self, cfg):
        self = objc.super(HandDelegate, self).init()
        self.cfg = cfg
        self.seq = Vision.VNSequenceRequestHandler.alloc().init()   # once, not per frame
        self.req = make_request(cfg["max_hands"])
        self.vision_ms = []
        self.draw_ms = []
        self.n_frames = 0
        self.n_with_hand = 0
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

        t0 = time.perf_counter()
        ok, err = self.seq.performRequests_onCMSampleBuffer_error_(   # <- out-param
            [self.req], sbuf, None)
        dt = (time.perf_counter() - t0) * 1e3

        self.n_frames += 1
        # Skip the exposure ramp: the first frames off a cold camera are black
        # and detect nothing, which would drag the throughput stats.
        counted = self.n_frames > self.cfg["warmup"]
        if not ok:
            if counted:
                print("  performRequests failed: %s" % err)
            return
        if counted:
            self.vision_ms.append(dt)

        obs = self.req.results() or []
        if obs and counted:
            self.n_with_hand += 1

        if self.writer is not None:
            t1 = time.perf_counter()
            frame = buffer_to_bgr(pb)
            if self.cfg["overlay"]:
                hands = [landmarks(o, w, h) for o in obs]
                draw(frame, hands, self.cfg["conf_gate"])
                cv2.putText(frame, "%5.1f ms  hands:%d" % (dt, len(obs)), (12, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4, cv2.LINE_AA)
                cv2.putText(frame, "%5.1f ms  hands:%d" % (dt, len(obs)), (12, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
            with self.lock:
                if self.writer is not None:
                    self.writer.write(frame)
            if counted:
                self.draw_ms.append((time.perf_counter() - t1) * 1e3)


# ---------------------------------------------------------------------------
def replay(path, size, max_hands, max_dim, reps):
    """Run the identical Vision path over a FIXED clip.

    The live camera cannot give a comparable throughput number: the cost per
    frame depends on what is in shot, and a moving person makes every run a
    different workload. Replaying one recorded clip holds the content constant
    so a resolution or max-dim change is the only variable.
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
    req = make_request(max_hands)
    if max_dim:
        req.setMaximumProcessingDimensionOnTheLongSide_(max_dim)

    print("clip   : %s" % path)
    print("frames : %d at %dx%d, %d rep(s)" % (len(frames), w, h, reps))
    print("maxProcessingDim: %d (0 = unset)" % req.maximumProcessingDimensionOnTheLongSide())

    ms, n_hand, seen = [], 0, 0
    for rep in range(reps):
        for arr in frames:
            seen += 1
            r, pb = Quartz.CVPixelBufferCreateWithBytes(
                None, w, h, Quartz.kCVPixelFormatType_32BGRA,
                arr, arr.strides[0], None, None, None, None)
            if r != 0:
                raise SystemExit("CVPixelBufferCreateWithBytes failed: %d" % r)
            t0 = time.perf_counter()
            ok, err = seq.performRequests_onCVPixelBuffer_error_([req], pb, None)  # <- out-param
            dt = (time.perf_counter() - t0) * 1e3
            if not ok:
                raise SystemExit("performRequests failed: %s" % err)
            if seen == 1:
                continue          # first call carries the ~100 ms model load
            ms.append(dt)
            if req.results():
                n_hand += 1
    print("\n%dx%d replay" % (w, h))
    print(stats("vision", ms))
    print("  vision-only ceiling ~%.0f fps (median)  |  frames with >=1 hand: %d/%d (%.0f%%)"
          % (1000.0 / statistics.median(ms), n_hand, len(ms), 100.0 * n_hand / len(ms)))
    return ms


def stats(name, xs, unit="ms"):
    if not xs:
        return "  %-14s (no samples)" % name
    xs = sorted(xs)
    p95 = xs[min(len(xs) - 1, int(len(xs) * 0.95))]
    return ("  %-14s mean %6.2f  median %6.2f  p95 %6.2f  min %6.2f  max %6.2f %s"
            % (name, statistics.mean(xs), statistics.median(xs), p95, xs[0], xs[-1], unit))


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
    ap.add_argument("--no-video", action="store_true", help="pure throughput, no MP4")
    ap.add_argument("--no-overlay", action="store_true",
                    help="write the MP4 WITHOUT landmarks - use this to record a clean "
                         "clip for --source file")
    ap.add_argument("--source", choices=("camera", "file"), default="camera")
    ap.add_argument("--input", default=None, help="clip path for --source file")
    ap.add_argument("--reps", type=int, default=1, help="replay passes over the clip")
    ap.add_argument("-o", "--out", default="temp/hand_live.mp4")
    args = ap.parse_args()

    if args.source == "file":
        if not args.input:
            raise SystemExit("--source file needs --input PATH")
        size = tuple(int(v) for v in args.size.lower().split("x")) if args.size else None
        replay(args.input, size, args.max_hands, args.max_dim, args.reps)
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
           "conf_gate": args.conf_gate, "overlay": not args.no_overlay}
    delegate = HandDelegate.alloc().initWithConfig_(cfg)
    if args.max_dim:
        delegate.req.setMaximumProcessingDimensionOnTheLongSide_(args.max_dim)
    print("maxProcessingDim: %d (0 = unset)"
          % delegate.req.maximumProcessingDimensionOnTheLongSide())

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
    sess.startRunning()
    t0 = time.perf_counter()
    # AVFoundation delivers frames via the delegate only while a run loop turns.
    # ONE long call, not a tight poll loop. CFRunLoopRunInMode releases the GIL
    # while it waits; spinning it at 0.05 s from Python holds the GIL and starves
    # the capture queue - that alone cut delivered fps by ~3x.
    while True:
        remaining = args.seconds - (time.perf_counter() - t0)
        if remaining <= 0:
            break
        CF.CFRunLoopRunInMode(CF.kCFRunLoopDefaultMode, remaining, False)
    elapsed = time.perf_counter() - t0

    delegate.stop = True
    sess.stopRunning()
    time.sleep(0.2)                      # let any in-flight callback finish
    with delegate.lock:
        if delegate.writer is not None:
            delegate.writer.release()
            delegate.writer = None

    counted = len(delegate.vision_ms)
    got = delegate.delivered or (0, 0)
    if got != (w, h):
        print("\nWARNING: requested %dx%d but camera delivered %dx%d" % (w, h, got[0], got[1]))
    print("\n%dx%d delivered, %.2f s wall" % (got[0], got[1], elapsed))
    print("  frames delivered %d  (%.1f fps delivered, %d counted after warmup)"
          % (delegate.n_frames, delegate.n_frames / elapsed, counted))
    print(stats("vision", delegate.vision_ms))
    if delegate.draw_ms:
        print(stats("draw+encode", delegate.draw_ms))
    if counted:
        mean_ms = statistics.mean(delegate.vision_ms)
        print("  vision-only ceiling ~%.0f fps  (%.1f%% of a 33.3 ms/30fps budget)"
              % (1000.0 / mean_ms, 100.0 * mean_ms / 33.3))
        print("  frames with >=1 hand: %d / %d (%.0f%%)"
              % (delegate.n_with_hand, counted, 100.0 * delegate.n_with_hand / counted))
    if not args.no_video:
        print("\nwrote %s" % args.out)


if __name__ == "__main__":
    main()
