#!/usr/bin/env python
"""Drive TouchDesigner with synthetic hands over OSC. No camera, nobody in frame.

    RUN IT
        ~/.venvs/visionhands/bin/python tools/send_synthetic.py --list
        ~/.venvs/visionhands/bin/python tools/send_synthetic.py ramp --cycles 4
        ~/.venvs/visionhands/bin/python tools/send_synthetic.py clap --period 3
        ~/.venvs/visionhands/bin/python tools/send_synthetic.py absent --cycles 1

    IT CHECKS FOR THE SIDECAR ITSELF and refuses to run alongside it; `--force`
    overrides. Both write to the same port, and TouchDesigner's OSC In CHOP
    interleaves the two streams into one set of channels - a hand that alternates
    between the camera's and this one's every other frame. That is not merely
    untidy: it drives the pinch distance back and forth across the dead band, so
    a four-sweep ramp can report +37, or - if the real hand happens to be open and
    still - something plausible enough to believe. The failure mode is a FALSE
    VERDICT on the measurement everything else rests on, which is worth a check
    rather than a warning in a docstring.

WHAT IT IS FOR. The latches have memory, so verifying them needs a controlled
input over time - see the module docstring of `visionhands/sequences.py`. This is
the transmitter for those sequences: the same 137 channels the sidecar sends, the
same encoder, the same port, so TouchDesigner cannot tell the difference and
every operator downstream is exercised for real.

WHAT IT IS NOT. It is not a benchmark. Nothing timed against this stream may be
quoted as a performance result: the frames are free to generate, so the numbers
would flatter everything downstream (STANDARDS.md, measurement honesty).

Ref: visionhands/sequences.py (the sequences), visionhands/osc.py (the encoder),
DESIGN.md 6.2 (the channel contract), docs/ATTRIBUTES.md (the thresholds swept).
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

# tools/ is not on the path when this is run as a script from the repo root.
# Insert the repo root so `visionhands` imports, exactly as the other tools do.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from visionhands.osc import encode_channels
from visionhands.sequences import SEQUENCES, frames
from visionhands.sidecar import DEFAULT_HOST, DEFAULT_PORT, SEQ_MODULUS
from visionhands.types import channel_names, channel_values

# Frames per second to send at, and seconds per sweep. 30 fps matches what the
# camera delivers (DESIGN.md 2.1), so the latches see the same frame spacing they
# will see in production; a 2 second sweep gives ~60 frames per cycle, which is
# enough that the dead band is many frames wide rather than one.
DEFAULT_FPS = 30.0
DEFAULT_PERIOD_S = 2.0

# How the sidecar is recognised. The same shape of check tools/td_build_comp.py
# uses for the COMP's Start/Stop buttons: argv[0] must be a python, and `-m` must
# sit immediately before the module, so a `python -c "...visionhands.sidecar..."`
# decoy and this process itself are both excluded.
SIDECAR_MODULE = "visionhands.sidecar"

# How often to print progress. Often enough to see it working, rare enough not to
# be the reason a frame is late.
STATUS_INTERVAL_S = 1.0


def sidecar_pids() -> list[int]:
    """PIDs of any running sidecar. Empty if none, and empty if we cannot tell.

    Why it parses `ps` rather than trusting `pgrep -f`: a `pgrep -f` pattern
    matches the grep's own command line, which is how an earlier version of the
    COMP's Stop button came to send SIGTERM to the shell that ran it. And macOS
    `ps -o comm=` truncates to 16 characters, so the interpreter cannot be
    identified that way either. This checks the full argv: the first word must be
    a python, and `-m` must be immediately followed by the module.
    """
    try:
        listing = subprocess.run(["ps", "-Ao", "pid=,args="], capture_output=True,
                                 text=True, timeout=5.0, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        # Cannot tell, so do not block on a guess - this is a convenience check,
        # not a safety interlock.
        return []
    found = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid, words = int(parts[0]), parts[1:]
        if not words[0].rsplit("/", 1)[-1].startswith("python"):
            continue
        if _runs_module(words, SIDECAR_MODULE):
            found.append(pid)
    return found


def _runs_module(words: list[str], module: str) -> bool:
    """Does this command line run `python -m <module>` as its MAIN program?

    Traps: `ps` hands back the whole command line as one string, so splitting it
           on spaces does NOT recover the argv boundaries - the body of a
           `python -c "..."` script is split into words too. An "is there a `-m`
           immediately followed by the module anywhere?" test therefore matches
           `python -c "# -m visionhands.sidecar"`, which is the same class of
           false positive that once made the COMP's Stop button SIGTERM the shell
           that launched it. Caught here by a decoy test rather than in anger.

    So the leading interpreter options are walked the way CPython itself parses
    them, and the scan stops at the first thing that decides what runs:

        -c   the program is a string; whatever follows is script text, not argv
        -m   the next word is the module - this is the only match
        else a script path; there is no -m
    """
    index = 1
    while index < len(words):
        word = words[index]
        if word == "-m":
            return index + 1 < len(words) and words[index + 1] == module
        if word == "-c":
            return False
        if not word.startswith("-"):
            return False            # a script path: not a -m invocation
        index += 1                  # another interpreter option, e.g. -u
    return False


def send(sequence_name: str, host: str, port: int, fps: float,
         period_s: float, cycles: float, quiet: bool = False) -> int:
    """Stream one sequence. Returns the number of frames sent.

    Thread: the calling thread, which it blocks for the duration. This is a
            command-line tool; there is nothing to be concurrent with.
    """
    sequence = SEQUENCES[sequence_name]
    names = channel_names()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent = 0
    started = time.monotonic()
    last_status = started
    try:
        for frame in frames(sequence, fps, period_s, cycles):
            # Pace against the START time, not against the previous frame: a
            # sleep-per-frame loop accumulates every scheduling overrun, so a
            # 2 second sweep quietly becomes 2.3 and the sweep rate - which is
            # what decides how many frames land in the dead band - stops being
            # the rate that was asked for.
            due = started + frame.captured_at
            delay = due - time.monotonic()
            if delay > 0.0:
                time.sleep(delay)

            # age_ms measured the same way the sidecar measures it: how stale
            # this frame is by the time it goes on the wire. Small and honest
            # rather than zero, so a downstream staleness gate behaves as it
            # would in production instead of seeing an impossibly fresh frame.
            age_ms = max(0.0, (time.monotonic() - due) * 1000.0)
            n_hands = sum(1 for hand in frame.hands if hand.found)
            values = list(channel_values(frame, age_ms, n_hands))
            # seq wrapped exactly as the sidecar wraps it, so float32 carries it
            # exactly all the way to TouchDesigner. Index 1 is seq - the frame
            # scalars lead the channel list (types.py).
            values[1] = float(frame.seq % SEQ_MODULUS)
            udp.sendto(encode_channels(names, values), (host, port))
            sent += 1

            now = time.monotonic()
            if not quiet and now - last_status >= STATUS_INTERVAL_S:
                print("  %s: %d frames, %.1f fps, hands=%d"
                      % (sequence_name, sent, sent / (now - started), n_hands))
                last_status = now
    finally:
        # In a finally: a Ctrl-C mid-sweep must not leave the socket open, and
        # the frame count printed by the caller should be the real one.
        udp.close()
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sequence", nargs="?", choices=sorted(SEQUENCES),
                        help="which sequence to send")
    parser.add_argument("--list", action="store_true",
                        help="describe the sequences and exit")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS)
    parser.add_argument("--period", type=float, default=DEFAULT_PERIOD_S,
                        help="seconds per sweep")
    parser.add_argument("--cycles", type=float, default=4.0,
                        help="how many sweeps; fractional is allowed")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="send even if the sidecar is running (interleaves)")
    args = parser.parse_args(argv)

    if args.list or args.sequence is None:
        print("sequences (visionhands/sequences.py):\n")
        for name in sorted(SEQUENCES):
            doc = (SEQUENCES[name].__doc__ or "").strip().splitlines()[0]
            print("  %-8s %s" % (name, doc))
        return 0 if args.list else 2

    running = sidecar_pids()
    if running and not args.force:
        print("REFUSING: the sidecar is running (pid %s)."
              % ", ".join(str(p) for p in running))
        print("Two writers on port %d interleave into one set of channels, and that"
              % args.port)
        print("does not look like a fault - it looks like a plausible wrong answer.")
        print("Press Stop Sidecar on the COMP, or pass --force if you mean it.")
        return 1

    print("sending %r to %s:%d - %.0f fps, %.1f s per sweep, %g sweeps"
          % (args.sequence, args.host, args.port, args.fps, args.period, args.cycles))
    if running:
        print("--force: the sidecar IS running, so these channels have two writers")
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
