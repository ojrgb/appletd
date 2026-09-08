"""Which streams exist, which port each uses, and what the sidecar reports.

The vocabulary shared by the sidecar, the TouchDesigner builders and the tests. It
lives here rather than in `sidecar.py` because the port numbers must match on both
sides of a UDP socket, and the two sides are written by different tools.

  * ONE PORT PER STREAM, computed from one base. Sharing a port would put pose
    channels inside the hands COMP's output, because TD's OSC In CHOP puts everything
    it receives into one CHOP.
  * The IMAGE requests - segment, depth, flow - have NO port. `port_for` refuses them
    by name rather than returning a plausible number; they publish through a shared
    buffer instead.
  * A status channel per request, so a panel showing a stream as on after somebody
    flipped it without restarting is not left lying.

Also holds the quality and accuracy names for those requests, because both the
sidecar's command line and the builders need them and neither can import a module that
imports Vision.

Pure stdlib. No pyobjc, no TouchDesigner.

Thread: pure data and pure functions. Safe anywhere.
Ref: DESIGN.md 6.4.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Sequence
from typing import Final

# ---------------------------------------------------------------------------
# The streams
# ---------------------------------------------------------------------------
STREAM_HANDS: Final = "hands"
STREAM_POSE: Final = "pose"
STREAM_FACE: Final = "face"

# Every stream, in a fixed order. The order is the status channel order below, and
# the offset order for the ports, so it is not cosmetic.
#
# All three are built now. When a fourth is DECLARED before it is BUILT - which is
# how face spent a day, so that `sc_face` existed from the start rather than
# appearing from nowhere later (DESIGN.md 6.2) - split this into two tuples again
# and have `parse_streams` refuse the unbuilt one by name. The distinction is worth
# reintroducing at that point and worth nothing while it is empty.
STREAM_NAMES: Final[tuple[str, ...]] = (STREAM_HANDS, STREAM_POSE, STREAM_FACE)

# SEGMENTATION is a request, not a stream, and the distinction is the whole reason it
# is a separate name here. Everything in STREAM_NAMES sends CHANNELS over a UDP port;
# a person-segmentation mask is 197 KB of pixels per frame and goes through a shared
# mmap instead (appletd/maskbuf.py, DESIGN.md 2.19). So it has:
#
#   * a launch flag, because it is one more Vision request on the same camera and the
#     same serial queue, and it costs 2.21 ms at `fast` (DESIGN.md 2.18);
#   * a STATUS channel, for exactly the reason the block below gives - a panel
#     showing segmentation on, after somebody flipped it without restarting, is
#     lying;
#   * NO port. `port_for` refuses it by name rather than returning a plausible
#     number, because a mask has nowhere to go on a UDP socket.
REQUEST_SEGMENT: Final = "segment"

# DEPTH, on the same terms and for the same reasons: a 518x392 fp16 map is 406 KB a
# frame, so it goes through its own shared mmap and has no port either.
#
# It is the most expensive thing this project runs by a wide margin - MEASURED at
# 23.00 ms a frame against hands' 3.41 (DESIGN.md 2.22) - so enabling it is a real
# decision rather than a preference, and the label says so.
REQUEST_DEPTH: Final = "depth"

# OPTICAL FLOW, on the same terms again and for the same reasons - an image, its own
# buffer, no port.
#
# The most BANDWIDTH-hungry of the three by a wide margin: the field comes back at FULL
# input resolution with two float32 components, so 720p is 7.2 MB a frame against the
# mask's 48 KB (docs/BENCHMARKS.md). It is also expensive to compute - MEASURED 16.2 ms
# at `low` and 30.1 at `high`, against hands' 3.41 - so like depth it is off by default
# and enabling it is a decision.
REQUEST_FLOW: Final = "flow"

# What `--streams` accepts. STREAM_NAMES plus the requests that have no port, in the
# order they RUN on the capture queue: the cheap ones a live project reads first.
REQUEST_NAMES: Final[tuple[str, ...]] = (*STREAM_NAMES, REQUEST_SEGMENT,
                                         REQUEST_DEPTH, REQUEST_FLOW)

# The optical flow accuracy levels, IN COST ORDER, and here for exactly the reason the
# segmentation qualities below are: `sidecar.py` needs them for its command line and
# must stay importable with no pyobjc, and `tools/td_add_flow.py` needs them for a menu
# and runs inside TouchDesigner, which CANNOT load pyobjc at all. Neither can import
# `appletd/flow.py`, which does `import Vision` at the top.
#
# MEASURED at 1280x720 (docs/BENCHMARKS.md): low 16.2 ms, medium 18.9, high 30.1 -
# against hands' 3.41 and a 33.3 ms frame, which is why the default is the cheapest.
FLOW_ACCURACIES: Final[tuple[str, ...]] = ("low", "medium", "high", "veryhigh")
DEFAULT_FLOW_ACCURACY: Final = "low"
# Two components a pixel - the x and y displacement. Here rather than in
# `flow.py` for the same boundary reason: `sidecar.py` sizes the shared buffer
# with it and must stay importable with no pyobjc.
FLOW_COMPONENTS: Final = 2

# The segmentation quality levels, IN COST ORDER, and they live here rather than in
# `segmentation.py` for the same reason the ports do: both sides of a boundary have
# to agree on them. `sidecar.py` needs the list for its command line and has to stay
# importable with no pyobjc present; `tools/td_build_vision.py` needs it for a menu
# and runs inside TouchDesigner. Neither can import a module that does `import
# Vision` at the top.
#
# MEASURED per frame over fixtures/hand_clip.mp4 (DESIGN.md 2.18), and the spread is
# why this is a control and not a constant:
#
#     fast       2.21 ms   256x192     a HARD alpha - only 0 and 255, no soft edge
#     balanced   8.54 ms   512x384     feathered
#     accurate  30.73 ms  2016x1512    cannot hold 30 fps on its own
#
# Vision's own default is `accurate`, which is almost certainly why an existing C++
# plugin pinned a frame rate at 50 fps. Ours is `fast`.
SEGMENT_QUALITIES: Final[tuple[str, ...]] = ("fast", "balanced", "accurate")
DEFAULT_SEGMENT_QUALITY: Final = "fast"

# THE MULTI-PERSON MASK'S COLOURS, indexed by Vision's instance number.
#
# `VNInstanceMaskObservation.instanceMask()` is natively index-encoded - 0 for
# background, 1..4 per person - so the buffer carries 1, 2, 3, 4 in a uint8 and reads
# as very nearly black. The indices are the right thing to SEND (one component,
# lossless, and the arithmetic stays integer); turning them into something visible is
# the consumer's job, and this is the table it uses.
#
# Red, green, blue, then WHITE last: a fourth person has to be visible against a light
# background as well as a dark one, and there is no fourth primary.
MASK_INSTANCE_COLOURS: Final[tuple[tuple[int, int, int], ...]] = (
    (0, 0, 0),          # 0 - background
    (255, 0, 0),        # 1
    (0, 255, 0),        # 2
    (0, 0, 255),        # 3
    (255, 255, 255),    # 4 - Vision separates at most four
)

# How many people the mask separated, carried in the buffer's 32 opaque aux bytes.
#
# WHY IT TRAVELS WITH THE FRAME rather than being read off `Multiperson`: the toggle
# is a LAUNCH FLAG, so between flipping it and restarting, the parameter and the
# running sidecar disagree - and a consumer that colourised a 0/255 binary mask as
# though it were indices would index a colour table with 255. The frame says what the
# frame is.
_MASK_AUX: Final = struct.Struct("<I")


def pack_mask_aux(people: int) -> bytes:
    """The mask's aux block: how many people it separates. 0 means a binary mask."""
    return _MASK_AUX.pack(max(0, int(people)))


def unpack_mask_people(aux: bytes) -> int:
    """People separated, or 0 for a single-person 0/255 mask or an older writer.

    Contract: never raises. An aux block too short - anything written before this
              existed - reads as 0, which is the single-person path and the safe
              answer.
    """
    if not aux or len(aux) < _MASK_AUX.size:
        return 0
    return int(_MASK_AUX.unpack_from(aux, 0)[0])

# CAMERA FLIP. Vision applies an orientation to the image before it does anything
# else, so mirroring costs NOTHING on our side: no pixel is touched in Python, and
# the flag is one more argument on a call already being made.
#
# PLAIN INTS rather than `Quartz.kCGImagePropertyOrientation*`, because this module
# is imported by the TouchDesigner builders and TouchDesigner's Python cannot load
# pyobjc (DESIGN.md 2.7). They are EXIF orientation values and have been these
# numbers since TIFF 6.0; `test_orientation.py` holds them against Quartz, where
# pyobjc is available.
# WHICH CAMERA "default" MEANS, as a SUBSTRING of the device name - the same thing
# `--camera` takes, so a name works identically on the command line and in the panel.
#
# HERE rather than in `engine.py`, which is where it used to live: the TouchDesigner
# builders need it to resolve `(default)` for their own Video Device In TOP, and they
# cannot import `engine` because it imports Vision (DESIGN.md 2.7). If the two sides
# disagreed, the PICTURE and the TRACKING would come from different cameras and the
# overlay would sit on the wrong one.
DEFAULT_CAMERA_NAME: Final = "MacBook"

ORIENTATION_UP: Final = 1
ORIENTATION_UP_MIRRORED: Final = 2


def orientation_for(flip: bool) -> int:
    """The EXIF orientation a `Camera Flip` setting asks Vision for.

    WHAT MOVES WITH IT, and it is everything: Vision reports landmark coordinates
    in the ORIENTED image, and returns its image outputs - the segmentation mask,
    the depth map, the flow field - oriented too. MEASURED on a fixture
    frame: the hand's mean x went 0.784 -> 0.228 and the depth map came back
    mirrored, 8.2x closer to the flip of the original than to the original.

    So a flipped camera needs NOTHING flipped downstream. The one thing that does
    is TouchDesigner's own `video_in`, which opens the camera separately and never
    goes near Vision.

    CHIRALITY IS THE CATCH. A mirrored left hand is a right hand, and Vision says
    so - `h?_chirality` reports what it sees, which is the mirrored world. That is
    the honest answer for an overlay drawn on a mirrored image, and the wrong one
    for asking which of the user's actual hands is raised.
    """
    return ORIENTATION_UP_MIRRORED if flip else ORIENTATION_UP

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
        if stream in (REQUEST_SEGMENT, REQUEST_DEPTH, REQUEST_FLOW):
            raise ValueError(
                "%r has no UDP port - it publishes an IMAGE through a shared buffer, "
                "not channels over OSC (appletd/maskbuf.py)" % (stream,))
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
                         % ", ".join(REQUEST_NAMES))
    unknown = sorted({name for name in wanted if name not in REQUEST_NAMES})
    if unknown:
        raise ValueError("unknown stream(s): %s. Available: %s"
                         % (", ".join(unknown), ", ".join(REQUEST_NAMES)))
    # REQUEST_NAMES order, so `segment` sorts last - which is also the order it runs
    # in on the capture queue, hands first (DESIGN.md 6.4).
    return tuple(name for name in REQUEST_NAMES if name in wanted)


def format_streams(streams: Iterable[str]) -> str:
    """The inverse of `parse_streams`, for building a command line."""
    return ",".join(name for name in REQUEST_NAMES if name in set(streams))


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

# What the camera ACTUALLY DELIVERED, in pixels. Added, and it closes a
# real hole rather than adding a convenience.
#
# TouchDesigner converts normalised coordinates to pixels with `_px = x * Resw`, and
# `Resw` was a PARAMETER somebody typed - nothing checked it against the camera. The
# session preset silently reverts `setActiveFormat_`, and two spike runs delivered
# 1080p while the log said 720p (DESIGN.md 3), so the parameter and the truth could
# differ by a third with nothing to see: every pixel coordinate wrong, no error.
#
# `sc_` prefixed on purpose. It makes these two channels housekeeping by
# construction - `sc_*` is already off the output and already routed to the
# `housekeeping` Null - so no trim list, no keep pattern and no page had to change.
#
# ZERO means "not stated", matching `Sidecar._source_px()`. A consumer must not read
# 0 as a resolution, and the TouchDesigner side ignores it rather than writing it.
STATUS_SRC_W: Final = STATUS_PREFIX + "src_w"
STATUS_SRC_H: Final = STATUS_PREFIX + "src_h"


def status_channel_names() -> tuple[str, ...]:
    """The status channel list. Fixed, like every other channel list here.

    REQUEST_NAMES and not STREAM_NAMES, so `sc_segment` is here too: it has no port
    but it is exactly as capable of being requested and failing to start, which is
    what these channels are for.
    """
    return (STATUS_UPTIME, *(STATUS_PREFIX + name for name in REQUEST_NAMES),
            STATUS_SRC_W, STATUS_SRC_H)


def status_channel_values(uptime_s: float,
                         started: Iterable[str],
                         source_px: tuple[int, int] = (0, 0)) -> list[float]:
    """Values in `status_channel_names()` order.

    Contract: `started` is what the sidecar ACTUALLY started, not what was asked
              for. That distinction is the entire reason these channels exist -
              a stream that was requested and failed to start must read 0 here,
              or the panel reports the request rather than the state.
              `source_px` is what the camera DELIVERED, or (0, 0) for "not
              stated" - never the requested size, which is the whole point.
    """
    live = set(started)
    return ([float(uptime_s)]
            + [1.0 if name in live else 0.0 for name in REQUEST_NAMES]
            + [float(source_px[0]), float(source_px[1])])


N_STATUS_CHANNELS: Final = 1 + len(REQUEST_NAMES) + 2


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
    for name in DEFAULT_STREAMS:
        if name not in REQUEST_NAMES:
            raise RuntimeError("%r is not in REQUEST_NAMES" % (name,))
    if (set(REQUEST_NAMES) - set(STREAM_NAMES)
            - {REQUEST_SEGMENT, REQUEST_DEPTH, REQUEST_FLOW}):
        raise RuntimeError("a request without a port was added to REQUEST_NAMES "
                           "without teaching `port_for` to refuse it by name")
    if DEFAULT_SEGMENT_QUALITY not in SEGMENT_QUALITIES:
        raise RuntimeError("the default segmentation quality %r is not one of %s"
                           % (DEFAULT_SEGMENT_QUALITY, SEGMENT_QUALITIES))
    if REQUEST_NAMES[:len(STREAM_NAMES)] != STREAM_NAMES:
        raise RuntimeError("REQUEST_NAMES must START with STREAM_NAMES: the status "
                           "channel order is the port order plus the portless "
                           "requests, and reordering it renames live channels")
    names: Sequence[str] = status_channel_names()
    if len(names) != N_STATUS_CHANNELS or len(set(names)) != N_STATUS_CHANNELS:
        raise RuntimeError("the status channel list is the wrong length or has "
                           "a duplicate: %s" % (names,))


_self_check()
