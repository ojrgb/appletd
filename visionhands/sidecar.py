"""The sidecar: camera and Vision in their OWN process, landmarks out over OSC.

    RUN IT
        ~/.venvs/visionhands/bin/python -m visionhands.sidecar
        ~/.venvs/visionhands/bin/python -m visionhands.sidecar --fps 60 --port 10000

    RECEIVE IT
        An OSC In CHOP in TouchDesigner, port 10000. That is the entire TD side:
        no Script CHOP, no callbacks DAT, no Python running in TD at all.

WHY THIS EXISTS, and it is not a preference. MEASURED inside TouchDesigner
(DESIGN.md 2.8): the same Vision call costs 2.09 ms on TD's main thread and
58.90 ms on a background thread in TD's process, and the observation-to-Hand
conversion pushes that to 190.83 ms because each of its ~126 pyobjc property
calls waits for the GIL again. Delivery decayed to 13 fps with inter-frame gaps
swinging from 15 to 203 ms - visibly choppy. TouchDesigner's frame loop holds the
GIL in long non-yielding stretches, and lowering the switch interval tenfold
changed nothing.

In a separate process there is no TD frame loop to compete with, and the same
work measures 4.38 ms even while TD renders at 60 fps. So the engine, the capture
thread and the lock-free box all stay exactly as they were - they were never the
problem - and only their *host* changes.

WHAT CROSSES THE BOUNDARY: 137 floats, 3480 bytes, per frame. No pixels. TD can
open the same camera itself for display - measured, both processes receive live
frames simultaneously - so there is no image to transport.

THREE THINGS THE OSC BOUNDARY COSTS US, all measured, all handled here:

  * OSC floats are 32-bit, so `age_ms` is computed at send time rather than
    shipping a raw timestamp. `time.monotonic()` is genuinely comparable across
    processes on macOS, but float32 resolution at typical uptime magnitudes is
    1/16 s, which would make a millisecond age meaningless.
  * `seq` is exact in float32 only to 2**24, which is 6.5 days at 30 fps, after
    which it silently stops incrementing. It is sent modulo SEQ_MODULUS.
  * A dead sidecar leaves TD's channels FROZEN, not zeroed - the OSC In CHOP
    holds its last value forever. The in-process design got liveness for free;
    here TD has to derive it. Both signals are available with no Python: the
    slope of `seq` is zero when the camera stops, and the slope of `age_ms` is
    zero when this process stops.

Ref: DESIGN.md 2.8 (why), 2.9 (the transport), 6.2 (the channel contract).
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time
from types import FrameType

from visionhands.osc import encode_channels
from visionhands.source import HandSource, InProcessSource
from visionhands.types import channel_names, channel_values

# Where TouchDesigner's OSC In CHOP listens. Loopback only: this stream is
# landmarks from a camera in someone's room, and it has no business on a network
# interface unless somebody deliberately asks for that.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10000

# How often to send, independent of camera delivery.
#
# Sending on a fixed clock rather than only on new frames is what makes `age_ms`
# a live signal: if the camera stalls, age keeps climbing and TD can see that the
# sidecar is alive but the camera is not. Send-on-new-frame-only would freeze
# both, making a dead camera indistinguishable from a dead sidecar.
#
# 60 Hz against a 30 fps camera means every frame is sent about twice. At 3480
# bytes that is 209 KB/s on loopback - irrelevant - and it keeps TD's channels
# fresh regardless of its own cook rate.
DEFAULT_SEND_FPS = 60

# seq is wrapped here so it stays exactly representable as a float32 all the way
# to TouchDesigner. 2**23 = 8388608: at 60 Hz that wraps every 38 hours, and a
# wrap shows up as one negative slope rather than as a counter that quietly stops
# counting. A consumer watching for "seq changed" is unaffected.
SEQ_MODULUS = 2 ** 23

# How often to print a status line. Frequent enough to see a problem, rare
# enough to leave a scrollback worth reading.
STATUS_INTERVAL_S = 2.0

# How often to check that the parent process is still alive, when we were given
# one to watch.
PARENT_CHECK_INTERVAL_S = 1.0


class Sidecar:
    """Owns the camera, the engine and the socket. One instance per process.

    Thread: the send loop runs on the main thread. The engine's capture thread
            publishes into the box exactly as it always did; this loop only ever
            reads it, lock-free, which is the same contract the Script CHOP had.
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 send_fps: int = DEFAULT_SEND_FPS,
                 camera_name: str | None = None,
                 parent_pid: int | None = None,
                 source: HandSource | None = None) -> None:
        """`source` is injectable so the send path can be tested with no camera.

        Defaulting to InProcessSource keeps the production path a one-liner; a
        test passes a FakeSource and gets to assert on real datagrams. This is
        the payoff for the HandSource seam in DESIGN.md 5.
        """
        self.host = host
        self.port = port
        self.send_interval_s = 1.0 / send_fps
        self.parent_pid = parent_pid
        self.source = source if source is not None else InProcessSource(
            camera_name=camera_name)
        # UDP: this is a stream of the most recent state, and a retransmitted
        # stale frame is worth less than the next fresh one. Loss on loopback is
        # not a practical concern, and `seq` makes any loss visible downstream.
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.names = channel_names()
        self.running = False
        self.n_sent = 0
        self.n_send_errors = 0
        self._last_seq_sent = -1

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.source.start()
        self.running = True

    def stop(self) -> None:
        """Idempotent, and safe from a signal handler.

        Stopping the source is the part that matters: it stops the capture
        session and drains the queue with a real barrier before releasing
        anything native (DESIGN.md 8). Leaving that undone is a native crash
        waiting for the process to exit at the wrong moment.
        """
        self.running = False
        self.source.stop()
        self.socket.close()

    def _parent_is_gone(self) -> bool:
        """True if the process that launched us has exited.

        Why this matters: if TouchDesigner spawns this sidecar and then crashes,
        nothing else would ever stop it - and an orphan holding the camera means
        the next launch fights it for the device, which is exactly the failure
        DESIGN.md 8 is about, merely relocated to another process. Signal 0 is
        the standard existence check: it validates the pid without delivering
        anything.
        """
        if self.parent_pid is None:
            return False
        try:
            os.kill(self.parent_pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # It exists but belongs to someone else - so it is alive, which is
            # all this question asks.
            return False
        except (OverflowError, ValueError):
            # A pid outside the OS's range, which os.kill rejects before it even
            # looks. Found by a test passing 4_000_000_000: unhandled, this
            # raised OverflowError out of the main loop and killed the sidecar
            # on a typo'd --parent-pid. A pid that cannot exist is not a live
            # parent, so the honest answer is "gone".
            return True
        return False

    # -- the loop ----------------------------------------------------------
    def send_once(self) -> bool:
        """Read the latest frame and send it. Returns True if a NEW frame went out.

        Never raises: a send failure is counted and reported, because the useful
        behaviour when the socket misbehaves is to keep tracking and keep trying,
        not to take down a process that is otherwise working.
        """
        frame = self.source.latest()
        age_ms = self.source.age_ms()
        n_hands = sum(1 for hand in frame.hands if hand.found)

        values = channel_values(frame, age_ms, n_hands)
        # Wrap seq for float32 exactness. Index 1 is `seq` in the contract, and
        # it is asserted as such by a test rather than trusted here.
        values[1] = float(frame.seq % SEQ_MODULUS)

        try:
            self.socket.sendto(encode_channels(self.names, values), (self.host, self.port))
            self.n_sent += 1
        except OSError as exc:
            self.n_send_errors += 1
            if self.n_send_errors in (1, 100, 1000):
                # Not every failure: a broken socket at 60 Hz would otherwise
                # produce 60 identical lines a second and bury everything else.
                print("send failed (%d so far): %r" % (self.n_send_errors, exc),
                      file=sys.stderr, flush=True)

        is_new = frame.seq != self._last_seq_sent
        self._last_seq_sent = frame.seq
        return is_new

    def run(self) -> int:
        """Send until interrupted. Returns a process exit code."""
        self.start()
        print("visionhands sidecar -> %s:%d at %.0f Hz | %d channels"
              % (self.host, self.port, 1.0 / self.send_interval_s, len(self.names)),
              flush=True)
        if self.parent_pid:
            print("watching parent pid %d; will exit when it does" % self.parent_pid,
                  flush=True)

        next_send = time.monotonic()
        next_status = time.monotonic() + STATUS_INTERVAL_S
        next_parent_check = time.monotonic() + PARENT_CHECK_INTERVAL_S
        frames_at_last_status = 0
        new_frames = 0

        try:
            while self.running:
                now = time.monotonic()

                if now >= next_send:
                    if self.send_once():
                        new_frames += 1
                    # Advance on a schedule rather than by adding the interval to
                    # `now`, so the send rate does not drift with the time each
                    # iteration takes.
                    next_send += self.send_interval_s
                    if next_send < now:
                        # We fell behind by more than an interval - skip rather
                        # than burst-send to catch up, which would only make the
                        # backlog worse.
                        next_send = now + self.send_interval_s

                if now >= next_status:
                    delivered = new_frames - frames_at_last_status
                    print("camera %5.1f fps | sent %d | age %6.1f ms | hands %d%s"
                          % (delivered / STATUS_INTERVAL_S, self.n_sent,
                             self.source.age_ms(),
                             sum(1 for h in self.source.latest().hands if h.found),
                             "" if not self.source.errors
                             else " | ERRORS: %s" % self.source.errors),
                          flush=True)
                    frames_at_last_status = new_frames
                    next_status += STATUS_INTERVAL_S

                if now >= next_parent_check:
                    if self._parent_is_gone():
                        print("parent process is gone; shutting down", flush=True)
                        break
                    next_parent_check += PARENT_CHECK_INTERVAL_S

                # Sleep to the next deadline instead of spinning. This releases
                # the GIL and yields the core; a spin here would starve our own
                # capture thread, which is the whole mistake this process exists
                # to avoid (DESIGN.md 2.8).
                sleep_for = min(next_send, next_status, next_parent_check) - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
        finally:
            self.stop()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="where the OSC In CHOP listens (default loopback)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--fps", type=int, default=DEFAULT_SEND_FPS,
                        help="send rate, independent of the camera's rate")
    parser.add_argument("--camera", default=None,
                        help="substring of the camera name; never an index")
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="exit when this pid does. Pass os.getpid() from a "
                             "TouchDesigner launcher so a TD crash cannot leave "
                             "an orphan holding the camera.")
    args = parser.parse_args(argv)

    sidecar = Sidecar(host=args.host, port=args.port, send_fps=args.fps,
                      camera_name=args.camera, parent_pid=args.parent_pid)

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        # Only sets a flag. Doing the teardown here would run camera shutdown
        # inside a signal handler, which is a good way to deadlock on a lock the
        # interrupted code was already holding.
        print("\nsignal %d; stopping" % signum, flush=True)
        sidecar.running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    return sidecar.run()


if __name__ == "__main__":
    sys.exit(main())
