#!/usr/bin/env python
"""Drive TouchDesigner with a synthetic FACE over OSC. No camera, nobody in frame.

    RUN IT
        ~/.venvs/visionhands/bin/python tools/send_synthetic_face.py turn
        ~/.venvs/visionhands/bin/python tools/send_synthetic_face.py tilt --cycles 6
        ~/.venvs/visionhands/bin/python tools/send_synthetic_face.py speak
        ~/.venvs/visionhands/bin/python tools/send_synthetic_face.py two
        ~/.venvs/visionhands/bin/python tools/send_synthetic_face.py absent

    IT SENDS TO THE FACE PORT, the base + 2 (10002 by default). `--port` is the
    BASE, matching the sidecar's flag, so both tools take the same number.

    IT CHECKS FOR THE SIDECAR and refuses to run alongside it: two writers on one
    port interleave into one set of channels, which looks like data rather than a
    fault. `--force` overrides.

WHAT IT IS FOR. Proving the face plumbing with the camera left alone - all 371
channels, their names, the port, the left-to-right slot rule, the DEGREES convention
on roll/yaw/pitch, and since the constellation was measured, every one of the 348
landmark channels.

    THE LANDMARKS MOVE, which is the whole reason this is worth running. `turn`
    sweeps them across the box, `tilt` rotates them, and `speak` opens the mouth -
    so `f0_outer_lips_07_ty` is a channel you can watch rather than a channel that
    exists. It is also the only way to exercise the MULTIPLY term in the
    box-relative coordinate transform: with landmarks at zero, `_tx` reduces to the
    bounding box alone, and a broken `point * bbox_w` would look perfect.

WHAT IT IS NOT. The constellation is a hand-laid caricature, not a face model -
`visionhands/synth_face.py` says exactly what it does and does not reproduce, and
the region OVERLAP is the important omission. No threshold should be tuned against
this stream.

Ref: visionhands/synth_face.py, visionhands/face_types.py, DESIGN.md 6.4.
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

# `send_synthetic` is a sibling in tools/; its sidecar check is imported rather
# than reimplemented, as in send_synthetic_pose.py.
from send_synthetic import sidecar_pids

from visionhands.face_types import face_channel_names, face_channel_values
from visionhands.osc import datagram_socket, encode_channels
from visionhands.sidecar import DEFAULT_HOST, DEFAULT_PORT, SEQ_MODULUS
from visionhands.streams import STREAM_FACE, port_for
from visionhands.synth_face import FacePose, synthetic_face_frame

DEFAULT_FPS = 30.0
DEFAULT_PERIOD_S = 3.0
STATUS_INTERVAL_S = 1.0

Sequence = Callable[[float], tuple[FacePose, ...]]


def turn(phase: float) -> tuple[FacePose, ...]:
    """One face turning left and right: yaw sweeping +/-40 degrees.

    DEGREES, which is the convention worth exercising: Vision reports radians and
    the conversion happens once, in face.py. A stream that sent radians would make
    that conversion untested and every downstream rotation 57x too small.
    """
    return (FacePose(yaw=40.0 * math.sin(2.0 * math.pi * phase)),)


def tilt(phase: float) -> tuple[FacePose, ...]:
    """One face rolling and pitching together - a head nodding while it tips."""
    return (FacePose(roll=30.0 * math.sin(2.0 * math.pi * phase),
                     pitch=20.0 * math.cos(2.0 * math.pi * phase)),)


def approach(phase: float) -> tuple[FacePose, ...]:
    """One face moving toward the camera and away: the bounding box grows.

    Nothing in the face contract is depth-invariant, so this is the sweep that
    shows what a consumer reading raw box size is actually reading.
    """
    return (FacePose(size=0.6 + 0.7 * (0.5 - 0.5 * math.cos(2.0 * math.pi * phase))),)


def speak(phase: float) -> tuple[FacePose, ...]:
    """One face opening and closing its mouth.

    The only sequence that moves a landmark WITHOUT moving the head, so it separates
    two failures that otherwise look identical: a landmark channel wired to the wrong
    point, and a landmark branch that is really just following the bounding box. Only
    the twenty lip channels should change here.
    """
    return (FacePose(expression=0.5 - 0.5 * math.cos(2.0 * math.pi * phase)),)


def two(phase: float) -> tuple[FacePose, ...]:
    """Two faces that SWAP SIDES halfway through.

    `f0` is the leftmost face, so the two slots exchange people at the crossing -
    the known limit of a stateless spatial sort (DESIGN.md 6.4), made visible rather
    than hidden. Watch `f0_bbox_x`: it should rise to the middle and come back.
    """
    offset = 0.25 * math.cos(2.0 * math.pi * phase)
    return (FacePose(center_x=0.5 - offset, size=0.9, yaw=20.0),
            FacePose(center_x=0.5 + offset, size=1.0, yaw=-20.0))


def absent(phase: float) -> tuple[FacePose, ...]:
    """Nobody in frame. Every channel zero, all of them still present."""
    return ()


SEQUENCES: dict[str, Sequence] = {
    "turn": turn, "tilt": tilt, "approach": approach, "speak": speak,
    "two": two, "absent": absent,
}


def send(sequence_name: str, host: str, base_port: int, fps: float,
         period_s: float, cycles: float, quiet: bool = False) -> int:
    """Stream one sequence to the face port. Returns the number of frames sent.

    Thread: the calling thread, which it blocks for the duration.
    """
    sequence = SEQUENCES[sequence_name]
    names = face_channel_names()
    port = port_for(STREAM_FACE, base_port)
    udp = datagram_socket()
    total = max(1, round(fps * period_s * cycles))
    sent = 0
    started = time.monotonic()
    last_status = started
    try:
        for index in range(total):
            due_offset = index / fps
            phase = (due_offset / period_s) % 1.0
            # Paced against the START time, so the sweep rate is the rate asked for
            # rather than the rate that survived scheduling.
            delay = (started + due_offset) - time.monotonic()
            if delay > 0.0:
                time.sleep(delay)

            frame = synthetic_face_frame(sequence(phase), seq=index + 1,
                                         captured_at=started + due_offset)
            age_ms = max(0.0, (time.monotonic() - (started + due_offset)) * 1000.0)
            n_faces = sum(1 for face in frame.faces if face.found)
            values = face_channel_values(frame, age_ms, n_faces)
            # Index 1 is `face_seq`, wrapped as the sidecar wraps it (DESIGN.md 2.9).
            values[1] = float(frame.seq % SEQ_MODULUS)
            udp.sendto(encode_channels(names, values), (host, port))
            sent += 1

            now = time.monotonic()
            if not quiet and now - last_status >= STATUS_INTERVAL_S:
                print("  %s: %d frames, %.1f fps, faces=%d"
                      % (sequence_name, sent, sent / (now - started), n_faces))
                last_status = now
    finally:
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
                        help="the BASE port, as the sidecar takes it. Face is sent "
                             "to base + 2 (default: %(default)s)")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS)
    parser.add_argument("--period", type=float, default=DEFAULT_PERIOD_S,
                        help="seconds per cycle (default: %(default)s)")
    parser.add_argument("--cycles", type=float, default=4.0)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="send even though a sidecar is running")
    args = parser.parse_args(argv)

    if args.list or args.sequence is None:
        print("sequences:")
        for name in sorted(SEQUENCES):
            doc = (SEQUENCES[name].__doc__ or "").strip().splitlines()[0]
            print("  %-9s %s" % (name, doc))
        return 0 if args.list else 2

    running = sidecar_pids()
    if running and not args.force:
        print("REFUSING: the sidecar is running (pid %s)."
              % ", ".join(str(p) for p in running))
        print("It sends the face contract too - as zeros when the stream is off -")
        print("so both writers would land on port %d and interleave, which looks"
              % port_for(STREAM_FACE, args.port))
        print("like data rather than a fault. Stop it, or pass --force.")
        return 1

    print("sending %r to %s:%d - %.0f fps, %.1f s per cycle, %g cycles"
          % (args.sequence, args.host, port_for(STREAM_FACE, args.port),
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
