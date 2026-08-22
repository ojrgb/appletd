"""The sidecar: camera and Vision in their OWN process, landmarks out over OSC.

    RUN IT
        ~/.venvs/visionhands/bin/python -m visionhands.sidecar
        ~/.venvs/visionhands/bin/python -m visionhands.sidecar --fps 60 --port 10000
        ~/.venvs/visionhands/bin/python -m visionhands.sidecar --slots off
        ~/.venvs/visionhands/bin/python -m visionhands.sidecar --streams hands,pose

    STREAMS are launch flags, one Vision request each, and `--port` is the BASE
    of a block: hands 10000, body pose 10001, face 10002, so no stream's channels
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

WHAT CROSSES THE BOUNDARY, per tick: 137 hand floats (3480 bytes) plus 4 status
channels on the base port, 123 pose floats on the next, and 23 face floats on the
one after. No pixels. TD can
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
import sys
import time
from collections.abc import Callable
from types import FrameType

from visionhands.face_types import (
    blank_face_frame,
    face_channel_names,
    face_channel_values,
)
from visionhands.osc import datagram_socket, encode_channels
from visionhands.pose_types import blank_pose_frame, pose_channel_names, pose_channel_values
from visionhands.slots import SLOT_MODE_CHIRALITY, SLOT_MODES
from visionhands.source import (
    SEQ_NEVER_PUBLISHED,
    FaceSource,
    HandSource,
    InProcessSource,
    PoseSource,
)
from visionhands.streams import (
    DEFAULT_STREAMS,
    STREAM_FACE,
    STREAM_HANDS,
    STREAM_NAMES,
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
        self.face_source: FaceSource | None = (
            self.source if isinstance(self.source, FaceSource) else None)
        # UDP: this is a stream of the most recent state, and a retransmitted
        # stale frame is worth less than the next fresh one. Loss on loopback is
        # not a practical concern, and `seq` makes any loss visible downstream.
        # Through the helper, which raises SO_SNDBUF: the default 9216-byte send
        # buffer is a hard ceiling on the DATAGRAM, and the face bundle is 12208
        # (MEASURED, osc.py). Without it that stream's channels never appear in
        # TouchDesigner while the other two work perfectly.
        self.socket = datagram_socket()
        self.names = channel_names()
        # Sent on the BASE port, with the hands bundle - a status channel that
        # travelled only on an optional stream's port would vanish exactly when
        # it is needed (DESIGN.md 6.4).
        self.status_names = status_channel_names()
        self.pose_names = pose_channel_names()
        self.face_names = face_channel_names()
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
        # One per optional stream, keyed by name: "did anything new go out" is
        # per stream, and with hands disabled the hands seq never moves.
        self._last_optional_seq: dict[str, int] = {}

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
        # Every stream, every tick, and `any()` rather than `or` so none of them
        # is short-circuited away: a stream that stopped being SENT would make its
        # channels vanish from TouchDesigner, which is the whole point of
        # DESIGN.md 6.4.
        fresh = [self._send_hands(), self._send_pose(), self._send_face()]
        # Any stream counts as news. With hands disabled, testing only the hands
        # seq would report 0 fps in the status line while pose frames were arriving
        # perfectly well.
        return any(fresh)

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
        frame = blank_pose_frame() if source is None else source.latest_pose()
        age_ms = self._age_of(frame, None if source is None else source.pose_age_ms)
        n_bodies = sum(1 for body in frame.bodies if body.found)
        return self._send_optional(
            STREAM_POSE, self.pose_names,
            pose_channel_values(frame, age_ms, n_bodies), frame.seq)

    def _send_face(self) -> bool:
        """The face bundle, to the face port. Zeros when the stream is off."""
        source = self.face_source
        frame = blank_face_frame() if source is None else source.latest_face()
        age_ms = self._age_of(frame, None if source is None else source.face_age_ms)
        n_faces = sum(1 for face in frame.faces if face.found)
        return self._send_optional(
            STREAM_FACE, self.face_names,
            face_channel_values(frame, age_ms, n_faces), frame.seq)

    def _age_of(self, frame: object,
                fallback: Callable[[], float] | None) -> float:
        """How stale a frame is, in ms. Frame first, clock second.

        Sampling the clock first leaves a window in which a newer frame lands with
        a later `captured_at`, and the age comes out NEGATIVE - measured 55 times in
        7.5M against a fast synthetic producer (source.py has the detail). Shared by
        both optional streams so there is one copy of that ordering rather than
        three.

        `fallback` is the box's own age, for a stream that has never published: the
        box measures from when its producer started, which is the only meaningful
        answer. None means there is no producer at all - a source that does not
        implement this stream - and the honest age is then 0, not a rising number,
        because nothing was ever expected.
        """
        seq = getattr(frame, "seq", SEQ_NEVER_PUBLISHED)
        captured_at = getattr(frame, "captured_at", 0.0)
        if seq != SEQ_NEVER_PUBLISHED:
            return max(0.0, (time.monotonic() - float(captured_at)) * 1e3)
        return 0.0 if fallback is None else fallback()

    def _send_optional(self, stream: str, names: tuple[str, ...],
                       values: list[float], seq: int) -> bool:
        """One optional stream's bundle, to its own port. Returns True if new.

        Every stream is sent every tick whether or not it is enabled: a disabled
        one carries the blank frame's zeros. TouchDesigner's OSC In CHOP creates
        channels as they arrive, so a stream that stopped sending would make its
        channels VANISH and break its consumers silently (DESIGN.md 6.2).
        """
        # Index 1 is that stream's `seq`, wrapped for float32 exactness as hands'
        # is (DESIGN.md 2.9). Both contracts put it there and both are pinned by a
        # test rather than trusted here.
        values[1] = float(seq % SEQ_MODULUS)
        self._send(names, values, port_for(stream, self.port))
        is_new = self._last_optional_seq.get(stream, -1) != seq
        self._last_optional_seq[stream] = seq
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
        # channels and no error (DESIGN.md 6.4). Every stream is listed, including
        # the disabled ones, because a disabled stream is still SENDING - zeros -
        # and somebody watching that port needs to know why it is flat.
        for stream, names in ((STREAM_HANDS, self.names),
                              (STREAM_POSE, self.pose_names),
                              (STREAM_FACE, self.face_names)):
            print("  %-6s port %d  %d channels%s%s"
                  % (stream, port_for(stream, self.port), len(names),
                     " (+%d status)" % len(self.status_names)
                     if stream == STREAM_HANDS else "",
                     "" if stream in self.streams else "  (DISABLED - zeros)"),
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
                    pose, face = self.pose_source, self.face_source
                    print("camera %5.1f fps | sent %d | age %6.1f ms | hands %d%s%s"
                          % (delivered / STATUS_INTERVAL_S, self.n_sent,
                             self.source.age_ms(),
                             sum(1 for h in self.source.latest().hands if h.found),
                             "".join(x for x in (
                                 "" if pose is None or STREAM_POSE not in self.streams
                                 else " | bodies %d" % sum(
                                     1 for b in pose.latest_pose().bodies if b.found),
                                 "" if face is None or STREAM_FACE not in self.streams
                                 else " | faces %d" % sum(
                                     1 for f in face.latest_face().faces if f.found),
                             )),
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


def _print_cameras() -> int:
    """Print every capture device, one per line, as `name<TAB>uniqueID`.

    Why it lives in the SIDECAR rather than in a tool of its own: this is the
    process that already imports pyobjc, and `--camera` takes a substring of the
    name, so the list and the thing that consumes it come from one place and cannot
    disagree. TouchDesigner shells out to it (`Listcameras` on the Vision page)
    precisely so that no pyobjc import ever happens inside TD.

    Contract: stdout is machine-readable - `name`, a tab, `uniqueID` - so the caller
              can build a menu from it. Returns 0 even when the list is empty; no
              cameras is a fact about the machine, not a failure of this command.
    Traps:    enumeration does NOT open a device or start a session, so it raises no
              TCC prompt and does not take the camera from whatever already has it.
              VERIFIED on this machine: four devices listed with the camera in use
              by another application.
              The import is INSIDE the function. `engine` pulls in AVFoundation and
              Vision, and `--list-cameras` must not be the reason a machine without
              them cannot even print a usage message.
    """
    from visionhands.engine import discover_devices

    devices = discover_devices()
    for device in devices:
        print("%s\t%s" % (device.localizedName(), device.uniqueID()))
    if not devices:
        print("(no video capture devices found)")
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
    parser.add_argument("--list-cameras", action="store_true",
                        help="print the capture devices and exit. Enumeration "
                             "only - it opens no device, starts no session and "
                             "triggers no permission prompt, which is what makes "
                             "it safe to call from a button in TouchDesigner")
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
                             "hands +0, pose +1, face +2), and a stream that is off still "
                             "sends its channels as zeros so nothing downstream "
                             "loses a reference. (default: %%(default)s)"
                             % ", ".join(STREAM_NAMES))
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="exit when this pid does. Pass os.getpid() from a "
                             "TouchDesigner launcher so a TD crash cannot leave "
                             "an orphan holding the camera.")
    args = parser.parse_args(argv)

    if args.list_cameras:
        return _print_cameras()

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
