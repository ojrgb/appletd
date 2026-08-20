#!/usr/bin/env python
"""Record the benchmark fixture clip - the only honest performance input we have.

    ~/.venvs/visionhands/bin/python tools/record_fixture.py --seconds 10
    ~/.venvs/visionhands/bin/python tools/record_fixture.py --list-devices
    ~/.venvs/visionhands/bin/python tools/record_fixture.py --name two_hands --seconds 12

WHY THIS EXISTS. A live camera cannot produce a comparable performance number:
identical 720p configs gave medians from 2.12 to 16.30 ms in the spike, an 8x
spread, because Vision's cost tracks what is in shot and a moving person makes
every run a different workload (MEASURED, DESIGN.md 3). Replaying one recorded
clip holds the content constant, so a code change is the only variable. Every
performance claim in this repo has to come from replaying this file.

WHAT TO RECORD.
  hand_clip.mp4  (default)  ONE hand, moving continuously through the frame for
                            the whole take: open palm, fist, rotation, fingers
                            spread, near the edges as well as the middle. Vary
                            distance. An unmoving hand makes a fixture that
                            flatters every smoothing bug we might write later.
  two_hands.mp4             BOTH hands, crossing over each other at least twice
                            and leaving frame separately. This is the fixture
                            that milestone 5's slot assignment is tested
                            against, and crossing hands is the case that breaks
                            wrist-proximity matching (DESIGN.md 6.3).

The clip is written WITHOUT any landmark overlay - it is an input, not a
demonstration. Drawing on it would feed our own annotations back into Vision.

This is a dev tool: it imports cv2, which TouchDesigner's bundled Python does
not have, and nothing in visionhands/ imports it.

Built against pyobjc 12.2.2 / macOS 26.5.2 (Darwin 25.5.0), Apple silicon.
"""

import argparse
import sys
import threading
import time
from pathlib import Path

import AVFoundation as AVF
import CoreMedia
import cv2
import numpy as np
import objc
import Quartz
from Foundation import NSObject
from libdispatch import dispatch_queue_attr_make_with_qos_class, dispatch_queue_create

# ---------------------------------------------------------------------------
# Constants. Every value here has a reason; none of them are taste.
# ---------------------------------------------------------------------------

# 1280x720. MEASURED (DESIGN.md 2.1): ~3.3 ms per frame, and 640x480 is
# consistently *slower* because Vision normalises internally, so this is both
# the accurate and the cheap choice. The fixture must be recorded at the
# resolution we intend to run at, or the replay benchmark measures a resize.
DEFAULT_W_PX = 1280
DEFAULT_H_PX = 720

# Frames discarded before recording starts. MEASURED (DESIGN.md 3): the first
# frames off a cold camera are near-black while exposure ramps, and they detect
# nothing. Leaving them in the fixture would put a stretch of guaranteed
# no-hand frames at the head of every benchmark.
WARMUP_FRAMES = 15

# QOS_CLASS_USER_INITIATED. A default-QoS dispatch queue can be scheduled onto
# efficiency cores, which was a large part of the run-to-run variance in the
# spike's timings. Recording does not need the speed, but it does need the
# camera to keep up with its own nominal frame rate.
QOS_CLASS_USER_INITIATED = 25

# Cap on the writer's declared frame rate. The camera reports a nominal rate
# that can exceed what it sustains; declaring 30 keeps playback duration honest.
MAX_WRITER_FPS = 30.0

# Seconds to let in-flight delegate callbacks finish after stopRunning() before
# the writer is released. Same value and same reasoning as the milestone 1 probe:
# carried over from the spike, not independently measured.
CALLBACK_DRAIN_S = 0.2

# mp4v, not avc1. mp4v is always present in the opencv-python wheel; avc1
# depends on how the wheel was built and fails at runtime with a warning and a
# zero-byte file rather than an exception.
FOURCC = "mp4v"


# ---------------------------------------------------------------------------
# camera enumeration
# ---------------------------------------------------------------------------
def discover_devices():
    """All video capture devices, built-in and external.

    Traps: the optional device-type constants come and go between macOS
           releases, so each is probed with hasattr rather than assumed.
    """
    device_types = [AVF.AVCaptureDeviceTypeBuiltInWideAngleCamera]
    for optional in ("AVCaptureDeviceTypeExternal", "AVCaptureDeviceTypeContinuityCamera"):
        if hasattr(AVF, optional):
            device_types.append(getattr(AVF, optional))
    session = (AVF.AVCaptureDeviceDiscoverySession
               .discoverySessionWithDeviceTypes_mediaType_position_(
                   device_types, AVF.AVMediaTypeVideo,
                   AVF.AVCaptureDevicePositionUnspecified))
    return list(session.devices())


def format_dims_px(fmt):
    """Native sensor dimensions of one AVCaptureDeviceFormat, in pixels."""
    dims = CoreMedia.CMVideoFormatDescriptionGetDimensions(fmt.formatDescription())
    return int(dims.width), int(dims.height)


def pick_device(name_substr):
    """Select the camera by NAME, never by index.

    Why: this machine enumerates Camo, OBS Virtual Camera and a Continuity
         iPhone alongside the built-in camera (MEASURED, DESIGN.md 3).
         Index 0 is not stable across reboots and is a good way to record ten
         seconds of black frames from OBS and not notice until replay.
    """
    devices = discover_devices()
    if not devices:
        raise SystemExit("no capture devices found")
    for device in devices:
        if name_substr.lower() in device.localizedName().lower():
            return device
    raise SystemExit("no device name contains %r. available: %s"
                     % (name_substr, [d.localizedName() for d in devices]))


def video_settings(w_px, h_px):
    """The output-side scale - the ONLY resolution control that actually works.

    Traps: setActiveFormat_ is silently reverted by the session preset (default
           High = 1080p) whether set before or after addInput_, and
           AVCaptureSessionPresetInputPriority, the iOS escape hatch, raises
           NSInvalidArgumentException on macOS. Two spike runs silently produced
           1080p while the log claimed 720p, which is why record_and_report
           asserts the delivered size rather than trusting this. DESIGN.md 3.
    """
    return {
        # BGRA because cv2.VideoWriter wants packed 8-bit colour on the host.
        # The engine leaves the camera in its native 420v, which Vision consumes
        # directly - but a recorder has to touch pixels, so it pays for BGRA.
        Quartz.kCVPixelBufferPixelFormatTypeKey: Quartz.kCVPixelFormatType_32BGRA,
        Quartz.kCVPixelBufferWidthKey: w_px,
        Quartz.kCVPixelBufferHeightKey: h_px,
    }


def buffer_to_bgr(pixel_buffer):
    """CVPixelBuffer (BGRA) -> contiguous HxWx3 BGR uint8 for cv2.

    Traps: CVPixelBufferGetBytesPerRow is NOT width*4 in general - the row
           stride is padded to a hardware-friendly multiple. Reshape to
           stride//4 and then slice to width, or every row shears progressively
           and the result looks like a skewed mirror. DESIGN.md 3.
    """
    w_px = Quartz.CVPixelBufferGetWidth(pixel_buffer)
    h_px = Quartz.CVPixelBufferGetHeight(pixel_buffer)
    stride_bytes = Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer)
    Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, 1)   # 1 = kCVPixelBufferLock_ReadOnly
    try:
        base = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
        flat = np.frombuffer(base.as_buffer(stride_bytes * h_px), dtype=np.uint8)
        # ascontiguousarray because the slice below is a view with gaps, and
        # cv2 needs contiguous memory.
        return np.ascontiguousarray(
            flat.reshape(h_px, stride_bytes // 4, 4)[:, :w_px, :3])
    finally:
        Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, 1)


# ---------------------------------------------------------------------------
# capture delegate
# ---------------------------------------------------------------------------
class RecorderDelegate(NSObject):
    """Writes delivered frames to an MP4. Runs on the capture queue, never main.

    Thread: every method here runs on the dispatch queue passed to
            setSampleBufferDelegate_queue_. The main thread only reads the
            counters after the session has stopped, and only after the 0.2 s
            drain in record_and_report, so no lock is needed for those.
            `writer_lock` exists for a different reason - see release_writer.
    Why a class and not a closure: pyobjc needs a real NSObject subclass to
            register the delegate selector with the Objective-C runtime.
    """

    def initWithPath_fps_(self, out_path, fps):
        self = objc.super(RecorderDelegate, self).init()
        if self is None:
            return None
        self.out_path = str(out_path)
        self.fps = fps
        self.writer = None
        self.n_delivered = 0            # every frame the camera handed us
        self.n_written = 0              # frames that made it into the file
        self.delivered_dims_px = None   # what we ACTUALLY got, not what we asked for
        self.first_error = None
        self.stopping = False
        # Guards writer access between this thread and the main thread's
        # release_writer(). A cv2.VideoWriter released underneath an in-flight
        # write() is a native crash, not an exception.
        self.writer_lock = threading.Lock()
        return self

    def ensure_writer(self, w_px, h_px):
        """Open the writer against the size the camera ACTUALLY delivers.

        Contract: RETURNS the writer, rather than only assigning it. The caller
               then holds a local that is known non-None for the duration of the
               write, which is both what mypy needs and what makes the write
               immune to another thread clearing self.writer underneath it.
        Traps: if the writer's declared size and the frames it is given
               disagree, cv2 silently drops every single frame and leaves a
               ~0-byte MP4 with no error anywhere. Opening lazily on the first
               real frame is what makes that impossible.
        """
        if self.writer is not None:
            return self.writer
        # cv2.VideoWriter.fourcc, not the legacy cv2.VideoWriter_fourcc alias:
        # identical value, but the alias is absent from opencv's type stubs.
        writer = cv2.VideoWriter(
            self.out_path, cv2.VideoWriter.fourcc(*FOURCC), self.fps, (w_px, h_px))
        if not writer.isOpened():
            raise RuntimeError("could not open VideoWriter for %s" % self.out_path)
        self.writer = writer
        return writer

    def captureOutput_didOutputSampleBuffer_fromConnection_(self, output, sbuf, conn):
        # INVARIANT: once stopping is set, touch nothing native. A call landing
        # on a torn-down session or a released writer is a process kill that no
        # `except` can catch. DESIGN.md 8.
        if self.stopping:
            return
        try:
            pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sbuf)
            if pixel_buffer is None:
                return
            self.n_delivered += 1

            # Discard the exposure ramp before the writer even opens, so the
            # fixture starts on a properly exposed frame.
            if self.n_delivered <= WARMUP_FRAMES:
                return

            w_px = int(Quartz.CVPixelBufferGetWidth(pixel_buffer))
            h_px = int(Quartz.CVPixelBufferGetHeight(pixel_buffer))
            if self.delivered_dims_px is None:
                self.delivered_dims_px = (w_px, h_px)

            frame_bgr = buffer_to_bgr(pixel_buffer)
            with self.writer_lock:
                # THIS is the barrier that protects the fixture, and it has to
                # be re-checked inside the lock: `stopping` is set before
                # stopRunning(), so a callback that passed the check at the top
                # of this method and then blocked on the lock while teardown ran
                # is stopped here instead. Without it, a late callback reopens
                # the writer after release_writer() and truncates the clip -
                # demonstrated in the M0 review.
                if self.stopping:
                    return
                # Belt and braces for the same race, from the other side: the
                # writer is released exactly once, on the main thread, after the
                # session has stopped, so a None writer when frames have already
                # been written means teardown got there first. Redundant with
                # the check above today; kept because it is the invariant a
                # reader is most likely to reason about, and it costs nothing.
                if self.writer is None and self.n_written > 0:
                    return
                writer = self.ensure_writer(w_px, h_px)
                writer.write(frame_bgr)
                self.n_written += 1
        except Exception as exc:            # noqa: BLE001 - see STANDARDS.md 2
            # Swallow-and-record: an exception propagating out of an
            # Objective-C callback unwinds into native frames, which is its own
            # crash. This is the one place swallowing is correct, and the error
            # is still reported at the end.
            if self.first_error is None:
                self.first_error = "%r" % (exc,)

    def release_writer(self):
        """Close the file. Called from the main thread AFTER the session stops.

        Thread: main only. The lock is what stops it landing mid-write().
        """
        with self.writer_lock:
            if self.writer is not None:
                self.writer.release()
                self.writer = None


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------
def record(device, out_path, w_px, h_px, seconds):
    """Capture `seconds` of video to out_path. Returns the delegate for stats.

    Thread: blocks the calling thread for the whole take, in ONE call.
    Traps: do NOT poll while waiting. A tight CFRunLoopRunInMode(..., 0.05)
           loop in Python holds the GIL and starved the capture queue from
           28 fps to 8.4 fps (MEASURED, DESIGN.md 3). time.sleep() releases the
           GIL for its whole duration, and - per the milestone 1 probe - frames
           arrive under it with no run loop turning at all, so a plain sleep is
           both sufficient and the least intrusive option.
    """
    session = AVF.AVCaptureSession.alloc().init()
    session.beginConfiguration()

    device_input, err = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(
        device, None)                                      # TRAP: out-param
    if device_input is None:
        raise SystemExit("deviceInputWithDevice failed: %s" % err)
    session.addInput_(device_input)

    output = AVF.AVCaptureVideoDataOutput.alloc().init()
    # Drop late frames rather than queue them. A recorder that falls behind
    # should lose frames, not accumulate a latency backlog that shows up as a
    # clip whose motion no longer matches real time. DESIGN.md 3.
    output.setAlwaysDiscardsLateVideoFrames_(True)
    output.setVideoSettings_(video_settings(w_px, h_px))

    # Nominal camera rate, capped. Used only as the writer's declared fps, so
    # the clip plays back at roughly the speed it was shot.
    min_frame_duration_s = CoreMedia.CMTimeGetSeconds(device.activeVideoMinFrameDuration())
    nominal_fps = 1.0 / min_frame_duration_s if min_frame_duration_s > 0 else MAX_WRITER_FPS
    writer_fps = min(nominal_fps, MAX_WRITER_FPS)

    delegate = RecorderDelegate.alloc().initWithPath_fps_(out_path, writer_fps)
    queue = dispatch_queue_create(
        b"visionhands.fixture.capture",
        dispatch_queue_attr_make_with_qos_class(None, QOS_CLASS_USER_INITIATED, 0))
    output.setSampleBufferDelegate_queue_(delegate, queue)
    session.addOutput_(output)
    session.commitConfiguration()

    print("recording %.1f s at %dx%d, writer fps %.1f" % (seconds, w_px, h_px, writer_fps))
    print("MOVE YOUR HAND for the whole take - see this file's docstring for what to do.")
    t0_s = time.perf_counter()
    try:
        # INSIDE the try: a raise from startRunning() must still reach the
        # finally below, or the session is left running and the writer is never
        # released. DESIGN.md 8.
        session.startRunning()
        # Warm-up frames are discarded inside the delegate, so add their cost
        # back or the clip comes out short.
        time.sleep(seconds + WARMUP_FRAMES / max(writer_fps, 1.0))
    finally:
        # Teardown order is load-bearing and must hold even on Ctrl-C:
        # flag, stop the session, let in-flight callbacks drain, THEN release
        # the writer. Releasing first would free the writer under a live
        # write(). DESIGN.md 8.
        delegate.stopping = True
        session.stopRunning()
        time.sleep(CALLBACK_DRAIN_S)                       # drain in-flight callbacks
        delegate.release_writer()
    delegate.elapsed_s = time.perf_counter() - t0_s
    return delegate


def verify(out_path, expect_w_px, expect_h_px, n_written):
    """Re-open the file and prove it is actually usable as a fixture.

    Why: every silent failure mode in this tool - the writer size mismatch, a
         codec that is not really available, a delivered resolution that is not
         the requested one - produces a file that exists and is wrong. Reading
         it back is the only check that catches all three.
    Contract: returns True if the clip is usable; prints why if not.
    """
    path = Path(out_path)
    if not path.exists():
        print("FAIL no file at %s" % path)
        return False
    size_mb = path.stat().st_size / 1e6
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        print("FAIL wrote %s (%.1f MB) but cv2 cannot open it" % (path, size_mb))
        return False
    n_decoded = 0
    dims_px = None
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        n_decoded += 1
        if dims_px is None:
            dims_px = (frame.shape[1], frame.shape[0])
    cap.release()

    print()
    print("clip        : %s (%.1f MB)" % (path, size_mb))
    print("decoded     : %d frames (%d written)" % (n_decoded, n_written))
    print("dimensions  : %s" % ("%dx%d" % dims_px if dims_px else "unknown"))
    if n_decoded == 0:
        print("FAIL clip decoded to 0 frames - almost always a writer/frame size")
        print("     mismatch, or a fourcc the wheel does not really support.")
        return False
    if dims_px != (expect_w_px, expect_h_px):
        print("FAIL expected %dx%d. The camera delivered something else and the"
              % (expect_w_px, expect_h_px))
        print("     benchmark would be measuring a resize. DESIGN.md 3.")
        return False
    print("OK this clip is usable as a benchmark fixture")
    return True


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list-devices", action="store_true",
                        help="print device names and their native formats, then exit")
    parser.add_argument("--device", default="MacBook",
                        help="substring of the camera's name (never an index)")
    parser.add_argument("--name", default="hand_clip",
                        help="fixture basename, written to fixtures/<name>.mp4")
    parser.add_argument("--seconds", type=float, default=10.0,
                        help="length of the take, excluding camera warm-up")
    parser.add_argument("--size", default="%dx%d" % (DEFAULT_W_PX, DEFAULT_H_PX),
                        help="WxH; changing this invalidates comparison with DESIGN.md 2.1")
    args = parser.parse_args()

    if args.list_devices:
        for device in discover_devices():
            native = sorted({format_dims_px(f) for f in device.formats()})
            print("%-28s %s" % (device.localizedName(), native))
        return 0

    # Status 3 == authorized. Checked first because an unauthorised host
    # delivers no frames rather than an error, and DESIGN.md 8 says to rule
    # this out before diagnosing anything else. The prompt attaches to the host
    # app - here that is the terminal or VS Code, not TouchDesigner.
    auth_status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeVideo)
    if auth_status != 3:
        print("camera not authorised (status %d). Grant camera access to this"
              % auth_status)
        print("terminal in System Settings > Privacy & Security > Camera, then re-run.")
        return 1

    w_px, h_px = (int(v) for v in args.size.lower().split("x"))
    fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    out_path = fixtures_dir / ("%s.mp4" % args.name)
    if out_path.exists():
        # Never silently overwrite: a fixture is the provenance for every
        # performance number in the repo, and replacing one invalidates them.
        print("refusing to overwrite existing fixture %s" % out_path)
        print("delete it first, or pass a different --name.")
        return 1

    device = pick_device(args.device)
    print("device      : %s" % device.localizedName())

    delegate = record(device, out_path, w_px, h_px, args.seconds)

    got_dims_px = delegate.delivered_dims_px or (0, 0)
    print()
    # The fps here includes the ~0.5 s before the camera delivers anything, so
    # it reads low on a short take. It is a sanity check, not a measurement -
    # nothing in DESIGN.md 2 may be sourced from it (STANDARDS.md 3).
    print("delivered   : %d frames in %.2f s (%.1f fps incl. start-up), %d written"
          % (delegate.n_delivered, delegate.elapsed_s,
             delegate.n_delivered / max(delegate.elapsed_s, 1e-9), delegate.n_written))
    if got_dims_px != (w_px, h_px):
        # TRAP: assert delivered against requested, always. DESIGN.md 3.
        print("WARNING requested %dx%d but the camera delivered %dx%d"
              % (w_px, h_px, got_dims_px[0], got_dims_px[1]))
    if delegate.first_error:
        print("first error : %s" % delegate.first_error)

    return 0 if verify(out_path, w_px, h_px, delegate.n_written) else 1


if __name__ == "__main__":
    sys.exit(main())
