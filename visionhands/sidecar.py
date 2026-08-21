"""The sidecar: camera and Vision in their OWN process, landmarks out over OSC.

    RUN IT
        ~/.venvs/visionhands/bin/python -m visionhands.sidecar
        ~/.venvs/visionhands/bin/python -m visionhands.sidecar --fps 60 --port 10000
        ~/.venvs/visionhands/bin/python -m visionhands.sidecar --slots off
        ~/.venvs/visionhands/bin/python -m visionhands.sidecar --streams hands,pose

    STREAMS are launch flags, one Vision request each, and `--port` is the BASE
    of a block: hands go to 10000 and body pose to 10001, so no stream's channels
    can land in another's OSC In CHOP (DESIGN.md 6.4). A stream that is switched
    off costs no inference but STILL SENDS its channels, as zeros - because TD's
    OSC In CHOP creates channels as they arrive, so omitting them makes them
    vanish, and a vanished channel breaks its consumers silently.

    SLOT ASSIGNMENT is on by default: h0 is the right hand, h1 the left, whatever
    order Vision hands them over in (visionhands/slots.py). `--slots off` passes
    Vision's order straight through. It is read once at startup, so the toggle in
    TouchDesigner takes effect on the next Start rather than immediately - which is
    deliberate: there is no control channel INTO this process, and adding one to
    change a partition would be a lot of machinery for something nobody changes
    mid-session.

    RECEIVE IT
        One OSC In CHOP in TouchDesigner per stream, on that stream's port. That
        is the entire TD side: no Script CHOP, no callbacks DAT, no Python
        running in TD at all.

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

WHAT CROSSES THE BOUNDARY: 137 hand floats (3480 bytes) plus 4 status channels on
the base port, and 123 pose floats on the next one, per tick. No pixels. TD can
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
    here TD has to derive it. Three signals are available with no Python: the
    slope of `seq` is zero when the camera stops, the slope of `age_ms` is zero
    when this process stops, and `sc_uptime_s` rises for as long as the process
    lives - which is what tells "sidecar dead" apart from "stream switched off",
    since a disabled stream's own seq is frozen too.

Ref: DESIGN.md 2.8 (why), 2.9 (the transport), 6.2 (the hands channel contract),
     6.4 (several streams: the ports, the flags, and the status channels).
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
from visionhands.pose_types import blank_pose_frame, pose_channel_names, pose_channel_values
from visionhands.slots import SLOT_MODE_CHIRALITY, SLOT_MODES
from visionhands.source import (
    SEQ_NEVER_PUBLISHED,
    HandSource,
    InProcessSource,
    PoseSource,
)
from visionhands.streams import (
    DEFAULT_STREAMS,
    IMPLEMENTED_STREAMS,
    STREAM_HANDS,
    STREAM_POSE,
    format_streams,
    parse_streams,
    port_for,
    status_channel_names,
    status_channel_values,
)
from visionhands.types import channel_names, channel_values

# Where TouchDesigner's OSC In CHOP listens. Loopback only: this stream is
# landmarks from a camera in someone's room, and it has no business on a network
# interface unless somebody deliberately asks for that.
DEFAULT_HOST = "127.0.0.1"
# The BASE port. Each stream gets its own, computed by `streams.port_for` - hands
# 10000, pose 10001 - so a stream's channels cannot land in another stream's OSC
# In CHOP (DESIGN.md 6.4). `--port` moves the whole block.
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
                 source: HandSource | None = None,
                 slot_mode: str = SLOT_MODE_CHIRALITY,
                 streams: tuple[str, ...] = DEFAULT_STREAMS) -> None:
        """`source` is injectable so the send path can be tested with no camera.

        Defaulting to InProcessSource keeps the production path a one-liner; a
        test passes a FakeSource and gets to assert on real datagrams. This is
        the payoff for the HandSource seam in DESIGN.md 5.

        `streams` is which Vision requests to run. It is passed to the source,
        which is where the inference is either paid for or not; what it does NOT
        change is what goes on the wire (DESIGN.md 6.4).
        """
        self.host = host
        self.port = port
        self.streams = streams
        self.send_interval_s = 1.0 / send_fps
        self.parent_pid = parent_pid
        self.source = source if source is not None else InProcessSource(
            camera_name=camera_name, slot_mode=slot_mode, streams=streams)
        # A source can implement one stream and not the other. Asked once, here,
        # rather than per send: with no pose support the pose bundle is sent from
        # a blank frame, which is the same zeros a disabled stream sends.
        self.pose_source: PoseSource | None = (
            self.source if isinstance(self.source, PoseSource) else None)
        # UDP: this is a stream of the most recent state, and a retransmitted
        # stale frame is worth less than the next fresh one. Loss on loopback is
        # not a practical concern, and `seq` makes any loss visible downstream.
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.names = channel_names()
        # Sent on the BASE port, with the hands bundle - a status channel that
        # travelled only on an optional stream's port would vanish exactly when
        # it is needed (DESIGN.md 6.4).
        self.status_names = status_channel_names()
        self.pose_names = pose_channel_names()
        # Where `sc_uptime_s` counts from. Construction rather than the top of
        # run(), so the camera warm-up (~1.5 s, DESIGN.md 2.7) is included: the
        # channel exists to say "this process is alive", and it is alive during
        # the warm-up. What matters downstream is that it KEEPS RISING; a frozen
        # value is a dead sidecar.
        self._started_at = time.monotonic()
        self.running = False
        self.n_sent = 0
        self.n_send_errors = 0
        self._last_seq_sent = -1
        self._last_pose_seq_sent = -1

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Open the camera. Does NOT set `running` - see run().

        `running` used to be set here, unconditionally, AFTER source.start().
        That silently discarded a SIGTERM arriving during start-up, and start-up
        is where a signal is most likely to land: opening the camera takes ~1.5 s
        of warm-up (MEASURED, DESIGN.md 2.7). The consequence with TouchDesigner's
        launcher is not cosmetic - TD calls terminate(), the sidecar ignores it,
        the 3 s wait expires and TD sends SIGKILL, which skips stop() entirely
        and leaves the capture session to be reclaimed by process death rather
        than released (DESIGN.md 8).
        """
        self.source.start()

    def stop(self) -> None:
        """Idempotent, and safe from a signal handler.

        Stopping the source is the part that matters: it stops the capture
        session and drains the queue with a real barrier before releasing
        anything native (DESIGN.md 8). Leaving that undone is a native crash
        waiting for the process to exit at the wrong moment.
        """
        self.running = False
        try:
            self.source.stop()
        finally:
            # In a finally: if stopping the camera raises, the socket must still
            # be closed, and the original exception must still be the one that
            # propagates rather than being masked by a second failure here.
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
        """Send one bundle per stream. Returns True if a NEW frame went out.

        Never raises: a send failure is counted and reported, because the useful
        behaviour when the socket misbehaves is to keep tracking and keep trying,
        not to take down a process that is otherwise working.

        EVERY STREAM IS SENT EVERY TICK, enabled or not (DESIGN.md 6.4). A
        disabled stream's box has never been published to, so its bundle is the
        blank frame's zeros - which keeps its channels present in TouchDesigner
        rather than making them vanish, and a vanished channel fails silently
        downstream (DESIGN.md 6.2).
        """
        hands_is_new = self._send_hands()
        pose_is_new = self._send_pose()
        # Either stream counts as news. With hands disabled, testing only the
        # hands seq would report 0 fps in the status line while pose frames were
        # arriving perfectly well.
        return hands_is_new or pose_is_new

    def _send_hands(self) -> bool:
        """The hands bundle plus the sidecar status, to the base port.

        Returns True if this is a frame we have not sent before.
        """
        frame = self.source.latest()
        # Age derived from the frame IN HAND, not from a second read of the box.
        # Reading both separately lets a publish land between them, so the
        # payload carries frame N's seq with frame N+1's age - measured claiming
        # 100 ms fresher than the frame it carried. That is the same mistake
        # LatestFrameBox.age_ms was rewritten to avoid ("frame first, clock
        # second"), one layer up.
        if frame.seq != SEQ_NEVER_PUBLISHED:
            age_ms = max(0.0, (time.monotonic() - frame.captured_at) * 1e3)
        else:
            # Nothing published yet: the box measures from when the producer
            # started, which is the only thing that can be meaningful here.
            age_ms = self.source.age_ms()
        n_hands = sum(1 for hand in frame.hands if hand.found)

        values = channel_values(frame, age_ms, n_hands)
        # Wrap seq for float32 exactness. Index 1 is `seq` in the contract, and
        # it is asserted as such by a test rather than trusted here.
        values[1] = float(frame.seq % SEQ_MODULUS)

        # The status channels ride along in the SAME bundle, so what the sidecar
        # is doing and what it found arrive together and cannot disagree about
        # which tick they describe.
        names = self.names + self.status_names
        values = values + status_channel_values(
            time.monotonic() - self._started_at, self.streams_started())
        self._send(names, values, port_for(STREAM_HANDS, self.port))

        is_new = frame.seq != self._last_seq_sent
        self._last_seq_sent = frame.seq
        return is_new

    def _send_pose(self) -> bool:
        """The body-pose bundle, to the pose port. Zeros when the stream is off."""
        source = self.pose_source
        if source is None:
            # No pose support in this source at all - a hands-only test double,
            # or a future source that does not implement it. Send the contract
            # anyway, as zeros: the channels must exist in TouchDesigner whether
            # or not anything is filling them.
            frame = blank_pose_frame()
            age_ms = 0.0
        else:
            frame = source.latest_pose()
            # Frame first, clock second - see _send_hands.
            if frame.seq != SEQ_NEVER_PUBLISHED:
                age_ms = max(0.0, (time.monotonic() - frame.captured_at) * 1e3)
            else:
                age_ms = source.pose_age_ms()
        n_bodies = sum(1 for body in frame.bodies if body.found)

        values = pose_channel_values(frame, age_ms, n_bodies)
        # Index 1 is `pose_seq`. Wrapped for the same float32 reason as hands, and
        # pinned by a test rather than trusted here.
        values[1] = float(frame.seq % SEQ_MODULUS)
        self._send(self.pose_names, values, port_for(STREAM_POSE, self.port))

        is_new = frame.seq != self._last_pose_seq_sent
        self._last_pose_seq_sent = frame.seq
        return is_new

    def _send(self, names: tuple[str, ...], values: list[float],
              port: int) -> None:
        """Encode one bundle and put it on the wire. Never raises.

        Encoding happens OUTSIDE the try: a length mismatch is a ValueError from
        encode_channels, and that guard exists to stop a joint's y landing in
        another joint's channel (DESIGN.md 7). Catching it alongside socket errors
        would swallow exactly the bug it was written to surface.
        """
        payload = encode_channels(names, values)
        try:
            self.socket.sendto(payload, (self.host, port))
            self.n_sent += 1
        except OSError as exc:
            self.n_send_errors += 1
            if self.n_send_errors in (1, 100, 1000):
                # Not every failure: a broken socket at 60 Hz would otherwise
                # produce 60 identical lines a second and bury everything else.
                print("send failed (%d so far): %r" % (self.n_send_errors, exc),
                      file=sys.stderr, flush=True)

    def streams_started(self) -> tuple[str, ...]:
        """Which streams are actually running, for the `sc_*` channels.

        Asked of the SOURCE, not of `self.streams`: the difference between what
        was requested and what is running is the whole reason those channels
        exist (DESIGN.md 6.4). A source that cannot answer - a hands-only test
        double - is taken at its word about the streams it was given, which is
        the best available answer and is never the production path.
        """
        source = self.pose_source
        if source is None:
            return self.streams if self.source.running else ()
        return source.streams_started

    def run(self) -> int:
        """Send until interrupted. Returns a process exit code."""
        # Set BEFORE start(), so a signal arriving during camera warm-up clears
        # it and the loop below never runs. Setting it after start() threw the
        # request away.
        self.running = True
        print("visionhands sidecar -> %s at %.0f Hz | streams: %s"
              % (self.host, 1.0 / self.send_interval_s,
                 format_streams(self.streams)), flush=True)
        # One line per stream, with its port and its channel count, because
        # "which port is pose on" is the first question anyone wiring up the TD
        # side asks - and getting it wrong shows up as an OSC In CHOP with no
        # channels and no error (DESIGN.md 6.4).
        print("  hands  port %d  %d channels (+%d status)"
              % (port_for(STREAM_HANDS, self.port), len(self.names),
                 len(self.status_names)), flush=True)
        print("  pose   port %d  %d channels%s"
              % (port_for(STREAM_POSE, self.port), len(self.pose_names),
                 "" if STREAM_POSE in self.streams else "  (DISABLED - zeros)"),
              flush=True)
        if self.parent_pid:
            print("watching parent pid %d; will exit when it does" % self.parent_pid,
                  flush=True)

        try:
            # INSIDE the try, so a failure to open the camera still reaches the
            # finally and still calls stop(). HandSource promises stop() is safe
            # whether or not start() succeeded; that promise exists precisely so
            # this path can lean on it, and it previously did not - a raising
            # start() left the socket open and the source never stopped.
            self.start()
        except BaseException:
            self.stop()
            raise

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
                    if next_send < time.monotonic():
                        # We fell behind by more than an interval - skip rather
                        # than burst-send to catch up, which would only make the
                        # backlog worse.
                        next_send = now + self.send_interval_s

                if now >= next_status:
                    delivered = new_frames - frames_at_last_status
                    pose = self.pose_source
                    print("camera %5.1f fps | sent %d | age %6.1f ms | hands %d%s%s"
                          % (delivered / STATUS_INTERVAL_S, self.n_sent,
                             self.source.age_ms(),
                             sum(1 for h in self.source.latest().hands if h.found),
                             "" if pose is None or STREAM_POSE not in self.streams
                             else " | bodies %d" % sum(
                                 1 for b in pose.latest_pose().bodies if b.found),
                             "" if not self.source.errors
                             else " | ERRORS: %s" % self.source.errors),
                          flush=True)
                    frames_at_last_status = new_frames
                    next_status += STATUS_INTERVAL_S
                    if next_status < now:
                        # Reset rather than catch up. After a long stall - a
                        # SIGSTOP, a swap storm - incrementing would emit one
                        # status line per missed interval in a burst, all reading
                        # 0.0 fps, making the log unreadable at exactly the
                        # moment it matters. Measured: 300 spurious lines after a
                        # 600 s stall.
                        next_status = now + STATUS_INTERVAL_S

                if now >= next_parent_check:
                    if self._parent_is_gone():
                        print("parent process is gone; shutting down", flush=True)
                        break
                    next_parent_check += PARENT_CHECK_INTERVAL_S
                    if next_parent_check < now:
                        next_parent_check = now + PARENT_CHECK_INTERVAL_S

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
    parser.add_argument("--slots", default=SLOT_MODE_CHIRALITY, choices=SLOT_MODES,
                        help="which physical hand goes in which slot. "
                             "'chirality' puts the right hand in h0 and the left "
                             "in h1, so a slot means the same hand every frame; "
                             "'off' passes Vision's own order through, which is "
                             "not stable between frames. Read once at startup - "
                             "restart to change it.")
    parser.add_argument("--streams", default=format_streams(DEFAULT_STREAMS),
                        help="comma-separated list of the Vision requests to "
                             "run: %s. Read once at startup, so a toggle in "
                             "TouchDesigner takes effect on the next Start. Each "
                             "stream is sent to its OWN port (--port is the base: "
                             "hands +0, pose +1), and a stream that is off still "
                             "sends its channels as zeros so nothing downstream "
                             "loses a reference. (default: %%(default)s)"
                             % ", ".join(IMPLEMENTED_STREAMS))
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="exit when this pid does. Pass os.getpid() from a "
                             "TouchDesigner launcher so a TD crash cannot leave "
                             "an orphan holding the camera.")
    args = parser.parse_args(argv)

    if args.parent_pid is not None and args.parent_pid <= 0:
        # os.kill(0, 0) signals the entire PROCESS GROUP and always succeeds, so
        # a pid of 0 would make the parent look alive forever - a watch that runs
        # and can never fire. Negative pids are process groups too.
        parser.error("--parent-pid must be a positive pid, got %d" % args.parent_pid)

    try:
        streams = parse_streams(args.streams)
    except ValueError as exc:
        # parser.error, not a raise: a bad --streams is a command-line mistake
        # and deserves usage plus exit 2, not a traceback.
        parser.error(str(exc))

    sidecar = Sidecar(host=args.host, port=args.port, send_fps=args.fps,
                      camera_name=args.camera, parent_pid=args.parent_pid,
                      slot_mode=args.slots, streams=streams)

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
