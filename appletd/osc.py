"""Minimal OSC encoding. No dependencies, no pyobjc, no TouchDesigner.

Just enough OSC to send named floats to TouchDesigner's OSC In CHOP, and one typed
message of floats and strings to its OSC In DAT, which is the whole transport.
Hand-rolled rather than taking a dependency: the encoding is twenty lines, and this
sits between two processes - a version skew in a third-party library would present as
landmarks quietly arriving in the wrong channels.

    message   address (padded to 4)  ",f" (padded to 4)  float32 big-endian
    typed     address (padded to 4)  ",fs..." (padded)   each argument in turn
    bundle    "#bundle" (padded)     timetag (8 bytes)   [size][message]...

Big-endian throughout, every part padded to a multiple of 4 bytes with NULs, addresses
starting with "/".

Thread: pure functions over bytes. Safe anywhere.
Ref: DESIGN.md 2.9.
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
# The two argument types the typed message carries. Ints are deliberately absent:
# every number on this wire is a float32 so the receiver has one numeric type, and a
# sequence number is wrapped by the sender rather than widened here.
_TAG_FLOAT = b"f"
_TAG_STRING = b"s"


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


def encode_typed_message(address: str, args: Sequence[float | str]) -> bytes:
    """One OSC message carrying floats and strings, in the order given.

    Contract: floats go as float32, strings as UTF-8 padded to 4 bytes. Anything else
              raises rather than being coerced - a bool or an int silently sent as
              a float is a type the receiver cannot tell from a real measurement.
    Why UTF-8: the OSC 1.0 text says ASCII; every receiver that matters, including
              TouchDesigner's OSC In DAT (MEASURED: `£` and `ü` arrived intact),
              passes the bytes through, and recognised text is not ASCII.
    """
    if not address.startswith("/"):
        raise ValueError("OSC address must start with '/': %r" % (address,))
    tags = bytearray(b",")
    body = bytearray()
    for value in args:
        if isinstance(value, str):
            tags += _TAG_STRING
            body += _pad(value.encode("utf-8"))
        elif isinstance(value, float):
            tags += _TAG_FLOAT
            body += struct.pack(">f", value)
        else:
            raise TypeError("typed message arguments are float or str, not %s"
                            % type(value).__name__)
    return _pad(address.encode("ascii")) + _pad(bytes(tags)) + bytes(body)


def encode_bundle_of(messages: Iterable[bytes]) -> bytes:
    """One OSC bundle around already-encoded messages."""
    parts = [_BUNDLE_PREFIX_PADDED, _TIMETAG_IMMEDIATE]
    for message in messages:
        parts.append(struct.pack(">i", len(message)))
        parts.append(message)
    return b"".join(parts)


def encode_bundle(pairs: Iterable[tuple[str, float]]) -> bytes:
    """One OSC bundle carrying many messages.

    Why a bundle rather than N datagrams: all 137 values belong to the same
    frame, and a bundle keeps them together so the receiver cannot show half of
    one frame and half of the next. 137 separate datagrams would also be 137
    syscalls per frame.
    Size: MEASURED per stream - hands 4188 bytes (141 channels), pose 3720 (123),
          face 12640 (387). The face bundle is the one to watch, and the limit is
          NOT 16 KB, which is the usual guess: see `datagram_socket`.
    """
    return encode_bundle_of(encode_message(address, value) for address, value in pairs)


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


def _string_at(message: bytes, at: int) -> tuple[str, int]:
    """A NUL-terminated string at `at`, and the 4-aligned offset after it."""
    end = message.index(b"\x00", at)
    return message[at:end].decode("utf-8"), (end // 4 + 1) * 4


def decode_messages(datagram: bytes) -> list[tuple[str, list[float | str]]]:
    """Every message in a datagram - bare or bundled - as (address, args).

    The RECEIVING half, and the sidecar's only one: it exists for the control port,
    where TouchDesigner sends a mode or a trigger. Floats, ints (widened to float)
    and strings; anything else raises ValueError, as does a truncated or misaligned
    datagram, so a malformed packet is refused rather than half-read.
    """
    if datagram.startswith(_BUNDLE_PREFIX):
        out: list[tuple[str, list[float | str]]] = []
        offset = len(_BUNDLE_PREFIX_PADDED) + len(_TIMETAG_IMMEDIATE)
        while offset < len(datagram):
            if offset + 4 > len(datagram):
                raise ValueError("bundle element size truncated")
            size = struct.unpack(">i", datagram[offset:offset + 4])[0]
            element = datagram[offset + 4:offset + 4 + size]
            if len(element) != size:
                raise ValueError("bundle element truncated")
            out.extend(decode_messages(element))
            offset += 4 + size
        return out
    address, cursor = _string_at(datagram, 0)
    if not address.startswith("/"):
        raise ValueError("OSC address must start with '/': %r" % (address,))
    tags, cursor = _string_at(datagram, cursor)
    if not tags.startswith(","):
        raise ValueError("type tag string must start with ',': %r" % (tags,))
    args: list[float | str] = []
    for tag in tags[1:]:
        if tag == "f":
            args.append(struct.unpack(">f", datagram[cursor:cursor + 4])[0])
            cursor += 4
        elif tag == "i":
            args.append(float(struct.unpack(">i", datagram[cursor:cursor + 4])[0]))
            cursor += 4
        elif tag == "s":
            value, cursor = _string_at(datagram, cursor)
            args.append(value)
        else:
            raise ValueError("unsupported OSC type tag %r" % tag)
    return [(address, args)]

