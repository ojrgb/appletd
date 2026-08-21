"""Which streams exist, which port each one uses, and what the sidecar reports.

The vocabulary shared by the sidecar, the TouchDesigner builders and the tests.
It is here rather than in `sidecar.py` because the port numbers have to be the
same on both sides of a UDP socket, and the two sides are written in different
files by different tools: `tools/td_build_comp.py` and `tools/td_build_pose_comp.py`
import this module for the port, so a stream cannot be listening on one number
while the sender uses another.

Pure stdlib, no pyobjc, no TouchDesigner - the same rule `types.py` follows and
for the same reason (DESIGN.md 5).

THREE THINGS THIS MODULE ENCODES, all settled in DESIGN.md 6.4:

  * ONE PORT PER STREAM, computed from one base. Sharing a port would put pose
    channels inside the hands COMP's output, because TD's OSC In CHOP puts
    everything it receives into one CHOP and the COMP passes its whole input
    through. A prefix Select at each COMP's input is the alternative, and prefix
    patterns have already failed to partition these channels once
    (docs/BUILD_PLAN.md step 3).
  * STREAMS ARE LAUNCH FLAGS. `parse_streams` is called once, on the sidecar's
    command line. There is no control channel into the sidecar and there is not
    going to be one - flipping a toggle in TouchDesigner takes effect on the
    next Start.
  * THE STATUS CHANNELS. `sc_*`, sent on the BASE port, saying what the sidecar
    actually started. Without them a panel showing `Streampose` on, after
    somebody flipped it without restarting, is simply lying.

Thread: pure functions over immutable values. Safe anywhere.
Ref: DESIGN.md 6.4 (the whole scheme), 2.12 (the body-pose API surface).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final

# ---------------------------------------------------------------------------
# The streams
# ---------------------------------------------------------------------------
STREAM_HANDS: Final = "hands"
STREAM_POSE: Final = "pose"
STREAM_FACE: Final = "face"

# Every stream the CONTRACT knows about, in a fixed order. Face is here while
# being unimplemented on purpose: the status channel list below is built from
# this tuple, and a channel list that grows when a feature lands is a channel
# list that appears from nowhere in an existing project (DESIGN.md 6.2). Better
# to publish `sc_face` reading 0 from the first day than to add it later.
STREAM_NAMES: Final[tuple[str, ...]] = (STREAM_HANDS, STREAM_POSE, STREAM_FACE)

# What can actually be asked for today. `parse_streams` refuses anything else,
# so asking for face fails on the command line with a clear message rather than
# starting a sidecar that quietly sends zeros forever.
IMPLEMENTED_STREAMS: Final[tuple[str, ...]] = (STREAM_HANDS, STREAM_POSE)

# What runs when nobody says otherwise: exactly what ran before there was a
# choice. A default that turned pose on would make every existing project pay
# for an inference it does not read.
DEFAULT_STREAMS: Final[tuple[str, ...]] = (STREAM_HANDS,)

# ---------------------------------------------------------------------------
# Ports
#
# BASE_PORT is the one number both sides agree on; everything else is an offset
# from it, so a machine that needs a different range moves one constant. 10000
# is where the hands stream has always been - the offset for hands is 0 so that
# an existing TouchDesigner project, and every note in this repo that says
# "port 10000", stay correct.
# ---------------------------------------------------------------------------
BASE_PORT: Final = 10000
PORT_OFFSETS: Final[dict[str, int]] = {
    STREAM_HANDS: 0,
    STREAM_POSE: 1,
    STREAM_FACE: 2,
}


def port_for(stream: str, base_port: int = BASE_PORT) -> int:
    """The UDP port a stream is sent to.

    Contract: `stream` must be one of STREAM_NAMES; anything else raises rather
              than returning a plausible number, because a wrong port is a
              silent failure at the far end - TouchDesigner shows an OSC In CHOP
              with no channels and no error.
    """
    if stream not in PORT_OFFSETS:
        raise ValueError("unknown stream %r; known: %s"
                         % (stream, ", ".join(STREAM_NAMES)))
    return base_port + PORT_OFFSETS[stream]


# ---------------------------------------------------------------------------
# The launch flag
# ---------------------------------------------------------------------------
def parse_streams(text: str) -> tuple[str, ...]:
    """`"hands,pose"` -> `("hands", "pose")`, in STREAM_NAMES order.

    Contract: returns the streams in canonical order with duplicates removed, so
              the caller can compare two parses for equality and so the order a
              user typed cannot change which port anything lands on. Raises
              ValueError, with the list of valid names, on anything unknown or
              on an empty selection.
    Why a comma list rather than a flag per stream: one argument that names
              everything is one thing for the TouchDesigner Start button to
              build and one thing to read in a process listing. `--pose` plus
              `--no-hands` plus `--face` is three flags whose combinations have
              to be reasoned about.
    Why an empty selection is an error: a sidecar with no streams opens the
              camera, runs no inference and sends zeros. That is never what
              anybody meant, and it looks exactly like a working sidecar with
              nobody in frame.
    """
    wanted = [part.strip().lower() for part in text.split(",") if part.strip()]
    if not wanted:
        raise ValueError("no streams selected; ask for at least one of: %s"
                         % ", ".join(IMPLEMENTED_STREAMS))
    unknown = [name for name in wanted if name not in IMPLEMENTED_STREAMS]
    if unknown:
        # Distinguish "not a stream" from "a stream that is not built yet": the
        # second is a roadmap question and the first is a typo, and conflating
        # them sends someone looking in the wrong place.
        not_built = [name for name in unknown if name in STREAM_NAMES]
        detail = "unknown stream(s): %s" % ", ".join(sorted(set(unknown) - set(not_built)))
        if not_built:
            detail = ("%s is in the channel contract but not implemented yet "
                      "(DESIGN.md 6.4)" % ", ".join(sorted(set(not_built))))
        raise ValueError("%s. Available: %s" % (detail, ", ".join(IMPLEMENTED_STREAMS)))
    return tuple(name for name in STREAM_NAMES if name in wanted)


def format_streams(streams: Iterable[str]) -> str:
    """The inverse of `parse_streams`, for building a command line."""
    return ",".join(name for name in STREAM_NAMES if name in set(streams))


# ---------------------------------------------------------------------------
# The status contract
#
# Sent on the BASE port, with the hands bundle. On the base port rather than
# each stream's own port because a status channel that travels only on an
# optional stream's port vanishes exactly when it is needed (DESIGN.md 6.4).
# ---------------------------------------------------------------------------
# `sc_` is "sidecar". Prefixed, unlike hands' `n_hands`/`seq`/`age_ms`, because
# those names predate there being more than one stream and are referenced in a
# live project; nothing new goes unprefixed.
STATUS_PREFIX: Final = "sc_"

# Seconds since the send loop started. The point of it is liveness: a FROZEN
# value means the process is gone. A disabled stream's own `seq` is also frozen,
# so without this there is no way to tell "sidecar dead" from "stream off".
STATUS_UPTIME: Final = STATUS_PREFIX + "uptime_s"


def status_channel_names() -> tuple[str, ...]:
    """The status channel list. Fixed, like every other channel list here."""
    return (STATUS_UPTIME, *(STATUS_PREFIX + name for name in STREAM_NAMES))


def status_channel_values(uptime_s: float,
                         started: Iterable[str]) -> list[float]:
    """Values in `status_channel_names()` order.

    Contract: `started` is what the sidecar ACTUALLY started, not what was asked
              for. That distinction is the entire reason these channels exist -
              a stream that was requested and failed to start must read 0 here,
              or the panel reports the request rather than the state.
    """
    live = set(started)
    return [float(uptime_s)] + [1.0 if name in live else 0.0 for name in STREAM_NAMES]


N_STATUS_CHANNELS: Final = 1 + len(STREAM_NAMES)


# ---------------------------------------------------------------------------
# Import-time self-check. `raise`, not `assert`: assertions vanish under
# python -O and these are contract checks (the same reasoning as types.py).
# ---------------------------------------------------------------------------
def _self_check() -> None:
    if set(PORT_OFFSETS) != set(STREAM_NAMES):
        raise RuntimeError("PORT_OFFSETS and STREAM_NAMES disagree: %s vs %s"
                           % (sorted(PORT_OFFSETS), sorted(STREAM_NAMES)))
    if len(set(PORT_OFFSETS.values())) != len(PORT_OFFSETS):
        raise RuntimeError("two streams share a port offset - they would collide "
                           "on one socket and interleave silently")
    if PORT_OFFSETS[STREAM_HANDS] != 0:
        raise RuntimeError("hands must stay on the base port: an existing project "
                           "and every note in this repo says 10000")
    for name in IMPLEMENTED_STREAMS + DEFAULT_STREAMS:
        if name not in STREAM_NAMES:
            raise RuntimeError("%r is not in STREAM_NAMES" % (name,))
    names: Sequence[str] = status_channel_names()
    if len(names) != N_STATUS_CHANNELS or len(set(names)) != N_STATUS_CHANNELS:
        raise RuntimeError("the status channel list is the wrong length or has "
                           "a duplicate: %s" % (names,))


_self_check()
