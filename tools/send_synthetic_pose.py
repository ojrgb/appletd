#!/usr/bin/env python
"""Drive TouchDesigner with a synthetic BODY over OSC. No camera, nobody in frame.

    RUN IT
        ~/.venvs/visionhands/bin/python tools/send_synthetic_pose.py walk
        ~/.venvs/visionhands/bin/python tools/send_synthetic_pose.py wave --cycles 6
        ~/.venvs/visionhands/bin/python tools/send_synthetic_pose.py two --period 4
        ~/.venvs/visionhands/bin/python tools/send_synthetic_pose.py absent

    IT SENDS TO THE POSE PORT, which is the base port + 1 (10001 by default), for
    the same reason the sidecar does: one stream per port, so no stream's channels
    can land in another's OSC In CHOP (DESIGN.md 6.4). `--port` is the BASE, to
    match the sidecar's flag - so both tools take the same number and neither
    needs the offset spelling out.

    IT CHECKS FOR THE SIDECAR and refuses to run alongside it, exactly as
    tools/send_synthetic.py does: two writers on one port interleave into one set
    of channels, which does not look like a fault - it looks like a plausible
    wrong answer. `--force` overrides.

WHAT IT IS FOR. Proving the pose plumbing end to end - the 123 channels, their
names, the port, and the left-to-right slot rule - with nobody in frame and the
camera left alone. It is the pose half of the same argument
`tools/send_synthetic.py` makes for hands.

WHAT IT IS NOT. Not a benchmark (the frames are free to generate) and not
evidence about Vision. The skeleton is proportions from a diagram, so a threshold
tuned against it is a guess - see the module docstring of
`visionhands/synth_body.py`.

Ref: visionhands/synth_body.py (the figure), visionhands/streams.py (the ports),
DESIGN.md 6.4 (the stream contract).
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Callable
from pathlib import Path

# tools/ is not on the path when this is run as a script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# `send_synthetic` is a sibling in tools/, on the path because this file's own
# directory is. Its sidecar check is imported rather than reimplemented: the same
# question with the same two-stage answer (argv[0] must be a python, and `-m` must
# sit immediately before the module), and two copies of it would drift.
from send_synthetic import sidecar_pids

from visionhands.osc import datagram_socket, encode_channels
from visionhands.pose_types import pose_channel_names, pose_channel_values
from visionhands.sidecar import DEFAULT_HOST, DEFAULT_PORT, SEQ_MODULUS
from visionhands.streams import STREAM_POSE, port_for
from visionhands.synth_body import BodyPose, synthetic_pose_frame

DEFAULT_FPS = 30.0
DEFAULT_PERIOD_S = 3.0
STATUS_INTERVAL_S = 1.0

# A phase is 0..1 over one cycle. Every sequence below is a pure function of it,
# the same shape as `visionhands/sequences.py` - so a sweep is reproducible and
# the frame rate is a delivery detail rather than part of the definition.
Sequence = Callable[[float], tuple[BodyPose, ...]]


def walk(phase: float) -> tuple[BodyPose, ...]:
    """One person crossing the frame left to right and back, arms down.

    For velocity, heading and anything else that reads motion. The traverse is a
    cosine rather than a sawtooth so there is no discontinuity at the wrap, which
    would show up downstream as one enormous velocity spike.
    """
    x = 0.5 - 0.35 * math.cos(2.0 * math.pi * phase)
    return (BodyPose(center_x=x),)


def wave(phase: float) -> tuple[BodyPose, ...]:
    """One person standing still, right arm sweeping down-up-down.

    The arm is what moves, so this separates "the person moved" from "a limb
    moved" - two things a project usually wants to treat differently.
    """
    return (BodyPose(center_x=0.5, wave=0.5 - 0.5 * math.cos(2.0 * math.pi * phase)),)


def two(phase: float) -> tuple[BodyPose, ...]:
    """Two people who SWAP SIDES halfway through.

    The sequence that exercises the slot rule, and the one worth watching. p0 is
    the leftmost person, so at the crossing the two slots exchange people - which
    is the known limit of a stateless spatial sort (DESIGN.md 6.4) and is meant to
    be visible rather than hidden. Watch `p0_root_x`: it should rise to the middle
    and come back down, never jump across.
    """
    offset = 0.3 * math.cos(2.0 * math.pi * phase)
    return (BodyPose(center_x=0.5 - offset, height=0.9),
            BodyPose(center_x=0.5 + offset, height=1.0, wave=0.5))


def depth(phase: float) -> tuple[BodyPose, ...]:
    """One person walking toward the camera and away, i.e. changing scale.

    Nothing in the pose contract is depth-invariant yet - that is what the hand
    attributes do by dividing through hand size - so this is the sweep that shows
    what a naive consumer gets wrong.
    """
    return (BodyPose(center_x=0.5,
                     height=0.55 + 0.45 * (0.5 - 0.5 * math.cos(2.0 * math.pi * phase))),)


def absent(phase: float) -> tuple[BodyPose, ...]:
    """Nobody in frame. Every channel zero, all 123 still present.

    The control case: whatever a project does on presence must do nothing here,
    and the channels must not vanish (DESIGN.md 6.2).
    """
    return ()


SEQUENCES: dict[str, Sequence] = {
    "walk": walk, "wave": wave, "two": two, "depth": depth, "absent": absent,
}


def send(sequence_name: str, host: str, base_port: int, fps: float,
         period_s: float, cycles: float, quiet: bool = False) -> int:
    """Stream one sequence to the pose port. Returns the number of frames sent.

    Thread: the calling thread, which it blocks for the duration. This is a
            command-line tool; there is nothing to be concurrent with.
    """
    sequence = SEQUENCES[sequence_name]
    names = pose_channel_names()
    port = port_for(STREAM_POSE, base_port)
    udp = datagram_socket()
    total = max(1, round(fps * period_s * cycles))
    sent = 0
    started = time.monotonic()
    last_status = started
    try:
        for index in range(total):
            due_offset = index / fps
            phase = (due_offset / period_s) % 1.0
            # Pace against the START time, not against the previous frame: a
            # sleep-per-frame loop accumulates every scheduling overrun, so a
            # 3 second sweep quietly becomes 3.4 and the sweep rate stops being
            # the rate that was asked for.
            delay = (started + due_offset) - time.monotonic()
            if delay > 0.0:
                time.sleep(delay)

            frame = synthetic_pose_frame(sequence(phase), seq=index + 1,
                                         captured_at=started + due_offset)
            # age_ms measured the way the sidecar measures it - how stale this
            # frame is by the time it goes on the wire. Small and honest rather
            # than zero, so a staleness gate downstream behaves as it would in
            # production instead of seeing an impossibly fresh frame.
            age_ms = max(0.0, (time.monotonic() - (started + due_offset)) * 1000.0)
            n_bodies = sum(1 for body in frame.bodies if body.found)
            values = pose_channel_values(frame, age_ms, n_bodies)
            # Index 1 is `pose_seq`, wrapped exactly as the sidecar wraps it so
            # float32 carries it to TouchDesigner exactly (DESIGN.md 2.9).
            values[1] = float(frame.seq % SEQ_MODULUS)
            udp.sendto(encode_channels(names, values), (host, port))
            sent += 1

            now = time.monotonic()
            if not quiet and now - last_status >= STATUS_INTERVAL_S:
                print("  %s: %d frames, %.1f fps, bodies=%d"
                      % (sequence_name, sent, sent / (now - started), n_bodies))
                last_status = now
    finally:
        # In a finally: a Ctrl-C mid-sweep must not leave the socket open.
        udp.close()
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sequence", nargs="?", choices=sorted(SEQUENCES),
                        help="which sweep to send")
    parser.add_argument("--list", action="store_true",
                        help="list the sequences and what each is for")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="the BASE port, as the sidecar takes it. Pose is "
                             "sent to base + 1 (default: %(default)s)")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS)
    parser.add_argument("--period", type=float, default=DEFAULT_PERIOD_S,
                        help="seconds per cycle (default: %(default)s)")
    parser.add_argument("--cycles", type=float, default=4.0)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="send even though a sidecar is running. Two writers "
                             "on one port interleave into one set of channels")
    args = parser.parse_args(argv)

    if args.list or args.sequence is None:
        print("sequences:")
        for name in sorted(SEQUENCES):
            doc = (SEQUENCES[name].__doc__ or "").strip().splitlines()[0]
            print("  %-7s %s" % (name, doc))
        return 0 if args.list else 2

    running = sidecar_pids()
    if running and not args.force:
        print("REFUSING: the sidecar is running (pid %s)."
              % ", ".join(str(p) for p in running))
        print("It sends the pose contract too - as zeros when the stream is off -")
        print("so both writers would land on port %d and interleave. That does not"
              % port_for(STREAM_POSE, args.port))
        print("look like a fault; it looks like a plausible wrong answer.")
        print("Press Stop Sidecar on the COMP, or pass --force if you mean it.")
        return 1

    print("sending %r to %s:%d - %.0f fps, %.1f s per cycle, %g cycles"
          % (args.sequence, args.host, port_for(STREAM_POSE, args.port),
             args.fps, args.period, args.cycles))
    started = time.monotonic()
    try:
        sent = send(args.sequence, args.host, args.port, args.fps,
                    args.period, args.cycles, args.quiet)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    elapsed = time.monotonic() - started
    print("done: %d frames in %.2f s (%.1f fps)"
          % (sent, elapsed, sent / elapsed if elapsed > 0 else 0.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
