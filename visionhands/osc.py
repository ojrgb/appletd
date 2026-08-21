"""Minimal OSC encoding. No dependencies, no pyobjc, no TouchDesigner.

Just enough OSC to send named floats to TouchDesigner's OSC In CHOP, which is
the whole transport (DESIGN.md 2.9). Hand-rolled rather than taking a dependency
because the encoding is about twenty lines and this is the one piece that sits
between two processes - a version skew in a third-party OSC library would
present as landmarks quietly arriving in the wrong channels.

THE FORMAT, which is all we need of it:

    message   address (padded to 4)  ",f" (padded to 4)  float32 big-endian
    bundle    "#bundle" (padded)     timetag (8 bytes)   [size][message]...

Everything is big-endian and every part is padded to a multiple of 4 bytes with
NULs. Addresses start with "/".

VERIFIED against a real TouchDesigner OSC In CHOP: 137 named floats arrive as
137 correctly-named channels with correct values, with no Python running in TD at
all.

TRAP, measured: a UDP socket's DEFAULT SEND BUFFER caps the datagram at 9216 bytes
on macOS, and our face bundle is 12208. `sendto` refuses it - nothing arrives, and
the other streams keep working, so it looks like one stream is broken rather than
one socket option being wrong. `datagram_socket()` below is what every sender uses.

TRAP, measured: OSC floats are 32-BIT. That is fine for normalised coordinates,
confidences and ages, and NOT fine for a raw `time.monotonic()` timestamp, where
float32 resolution at typical uptime magnitudes is 1/16 of a second. Send
elapsed values, not absolute ones (DESIGN.md 2.9).

Thread: pure functions, no state, safe anywhere.
"""

from __future__ import annotations

import socket
import struct
from collections.abc import Iterable, Sequence

# OSC pads every string and blob to a 4-byte boundary with NULs.
_ALIGNMENT = 4

# The immediate-execution timetag: 63 zero bits then a 1. Servers treat it as
# "now" rather than scheduling, which is what a realtime stream wants.
_TIMETAG_IMMEDIATE = struct.pack(">Q", 1)

_BUNDLE_PREFIX = b"#bundle"

# A single float argument. The comma is part of the type-tag string, not a
# separator we chose.
_TYPETAG_FLOAT = b",f"


# The send buffer every sender sets, and the number is the whole point of the
# function below. MEASURED on macOS 26.5.2:
#
#   net.inet.udp.maxdgram = 9216, and a UDP socket's DEFAULT SO_SNDBUF is 9216 too.
#   sendto() of anything larger fails outright - not fragmented, not truncated,
#   REFUSED - and the face bundle is 12208 bytes.
#
# Raising SO_SNDBUF on the SENDER defeats it completely and needs no privileges:
# 65000 bytes sent and received on loopback. The RECEIVER needs nothing - a socket
# with the default buffer took 20000 bytes - which is what makes this fixable at
# all, since TouchDesigner owns the receiving end and we cannot configure it.
#
# 256 KB rather than exactly enough: it costs nothing, it is kernel-clamped if the
# system disagrees, and the alternative is a constant that has to be revisited every
# time a contract grows.
SEND_BUFFER_BYTES = 1 << 18


def datagram_socket() -> socket.socket:
    """A UDP socket that can actually send our largest bundle.

    Every sender in this repo goes through here rather than calling
    `socket.socket()` directly, because the default send buffer silently sets a
    9216-byte ceiling on the CONTRACT - and it presents as one stream's channels
    never appearing in TouchDesigner at all, with the other two working perfectly.
    That is a very confusing way to discover a socket option.

    Not raised for performance: a bigger buffer buys nothing here. It raises the
    maximum DATAGRAM SIZE, which on macOS is bounded by the send buffer.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER_BYTES)
    return sock


def _pad(raw: bytes) -> bytes:
    """NUL-pad to the next 4-byte boundary.

    Note the `or _ALIGNMENT`: OSC requires at least one terminating NUL, so a
    string whose length is already a multiple of 4 still gains a full 4 bytes.
    Getting this wrong produces a stream that some receivers accept and others
    silently truncate.
    """
    padding = _ALIGNMENT - (len(raw) % _ALIGNMENT)
    return raw + b"\x00" * padding


# Padded once at import: it is a constant, and it is prepended to every bundle.
_BUNDLE_PREFIX_PADDED = _pad(_BUNDLE_PREFIX)


def encode_message(address: str, value: float) -> bytes:
    """One OSC message carrying one float.

    Contract: `address` must start with "/" - the receiver uses it as the
              channel name, and TouchDesigner strips the leading slash.
    """
    if not address.startswith("/"):
        raise ValueError("OSC address must start with '/': %r" % (address,))
    return _pad(address.encode("ascii")) + _pad(_TYPETAG_FLOAT) + struct.pack(">f", value)


def encode_bundle(pairs: Iterable[tuple[str, float]]) -> bytes:
    """One OSC bundle carrying many messages.

    Why a bundle rather than N datagrams: all 137 values belong to the same
    frame, and a bundle keeps them together so the receiver cannot show half of
    one frame and half of the next. 137 separate datagrams would also be 137
    syscalls per frame.
    Size: MEASURED per stream - hands 4188 bytes (141 channels), pose 3720 (123),
          face 12208 (371). The face bundle is the one to watch, and the limit is
          NOT the 16 KB this note used to guess: see `datagram_socket`.
    """
    parts = [_BUNDLE_PREFIX_PADDED, _TIMETAG_IMMEDIATE]
    for address, value in pairs:
        message = encode_message(address, value)
        parts.append(struct.pack(">i", len(message)))
        parts.append(message)
    return b"".join(parts)


def encode_channels(names: Sequence[str], values: Sequence[float]) -> bytes:
    """Encode a full channel set as one bundle.

    Contract: names and values must be the same length; a mismatch raises rather
              than silently truncating, because the failure it prevents is a
              joint's y arriving in another joint's channel - plausible on
              screen and wrong (DESIGN.md 7).
    """
    if len(names) != len(values):
        raise ValueError("names/values length mismatch: %d vs %d"
                         % (len(names), len(values)))
    return encode_bundle(
        ("/" + name, value) for name, value in zip(names, values, strict=True))
