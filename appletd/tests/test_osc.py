"""The OSC wire format, pinned byte for byte.

This is the one piece of the system that crosses a process boundary, so its
encoding is a contract with something we do not control. The tests are
deliberately about BYTES rather than about round-tripping through our own
decoder - a decoder of ours agreeing with an encoder of ours proves nothing
about what TouchDesigner will accept.

The format has been verified against a real OSC In CHOP: 137 named floats arrive
as 137 correctly-named channels. These tests pin what was sent that day, so a
change to the encoder that TD would reject fails here instead.
"""

from __future__ import annotations

import struct

import pytest

from appletd.osc import (
    decode_messages,
    encode_bundle,
    encode_bundle_of,
    encode_channels,
    encode_message,
    encode_typed_message,
)
from appletd.types import N_CHANNELS, blank_frame, channel_names, channel_values

# MEASURED: the exact size of our 137-channel bundle, and the reason it matters.
# A UDP datagram on macOS loopback tops out around 16 KB; past that this silently
# stops arriving. Pinned so that growing the contract has to notice - it grew
# from 3480 to 4088 bytes when channel names went from `h0_lm08_x` to
# `h0_index_tip_x`, which is the cost of readable names and is worth it.
BUNDLE_BYTES_137 = 4088
LOOPBACK_DATAGRAM_LIMIT = 16384


def test_message_bytes_are_exactly_the_osc_format() -> None:
    """One float message, byte for byte.

    '/h0_lm00_x' is 10 characters, so it pads to 12 with two NULs. ',f' pads to
    4. Then a big-endian float32: 0.5 is 0x3F000000.
    """
    assert encode_message("/h0_lm00_x", 0.5) == (
        b"/h0_lm00_x\x00\x00" + b",f\x00\x00" + b"\x3f\x00\x00\x00")


def test_a_string_already_aligned_still_gains_four_nuls() -> None:
    """The rule that trips people: OSC needs at least one terminating NUL, so a
    4-aligned string pads by a full 4 bytes rather than by zero.

    Getting this wrong produces a stream some receivers accept and others
    truncate - the worst kind of wire bug, because it works on your machine.
    """
    encoded = encode_message("/abc", 0.0)          # '/abc' is exactly 4 bytes
    assert encoded.startswith(b"/abc\x00\x00\x00\x00")
    assert len(encoded) % 4 == 0


def test_every_encoding_is_four_byte_aligned() -> None:
    for address in ("/a", "/ab", "/abc", "/abcd", "/abcde", "/h1_lm20_conf"):
        assert len(encode_message(address, 1.0)) % 4 == 0


def test_address_must_start_with_a_slash() -> None:
    """TD uses the address as the channel name; a missing slash is not something
    to paper over silently."""
    with pytest.raises(ValueError, match="must start with"):
        encode_message("h0_found", 1.0)


def test_bundle_has_the_header_timetag_and_sized_elements() -> None:
    bundle = encode_bundle([("/a", 1.0), ("/b", 2.0)])
    assert bundle.startswith(b"#bundle\x00")
    # Immediate-execution timetag: 63 zero bits then a 1.
    assert bundle[8:16] == struct.pack(">Q", 1)
    # Then [size][message] pairs. Both our messages are 12 bytes.
    first_size = struct.unpack(">i", bundle[16:20])[0]
    assert first_size == 12
    assert bundle[20:32] == encode_message("/a", 1.0)
    second_size = struct.unpack(">i", bundle[32:36])[0]
    assert second_size == 12


def test_the_full_contract_fits_in_one_datagram() -> None:
    """Pins the measured size AND the headroom that makes it safe.

    If the channel contract grows past the datagram limit, the failure mode is
    packets that stop arriving rather than an error - so the check belongs here,
    not in a comment.
    """
    names = channel_names()
    values = channel_values(blank_frame(), 0.0, 0)
    bundle = encode_channels(names, values)
    assert len(names) == N_CHANNELS
    assert len(bundle) == BUNDLE_BYTES_137
    assert len(bundle) < LOOPBACK_DATAGRAM_LIMIT


def test_length_mismatch_raises_rather_than_truncating() -> None:
    """The failure this prevents is a joint's y arriving in another channel:
    plausible on screen, and wrong (DESIGN.md 7)."""
    with pytest.raises(ValueError, match="mismatch"):
        encode_channels(["a", "b"], [1.0])


def test_values_survive_the_encoding_at_the_precision_we_rely_on() -> None:
    """float32 is exact enough for normalised coordinates and ages, and NOT for
    absolute timestamps - which is why the sidecar sends elapsed values
    (DESIGN.md 2.9). This pins the half that has to work.
    """
    for value in (0.0, 1.0, -1.0, 0.5, 0.123456, 41.372, 8388607.0):
        encoded = encode_message("/x", value)
        back = struct.unpack(">f", encoded[-4:])[0]
        assert abs(back - value) <= abs(value) * 1e-6 + 1e-6, value


# ---------------------------------------------------------------------------
# The socket, and the ceiling it removes
# ---------------------------------------------------------------------------
def test_the_largest_bundle_we_send_does_not_fit_a_default_socket() -> None:
    """The measurement behind `datagram_socket`, pinned so it cannot rot quietly.

    macOS: `net.inet.udp.maxdgram` is 9216 and a UDP socket's default SO_SNDBUF is
    9216 too, so `sendto` REFUSES anything larger - it does not fragment and does
    not truncate. The face bundle is 12208 bytes. The failure presents as one
    stream's channels never appearing in TouchDesigner while the other two work,
    which is a very confusing way to find a socket option.
    """
    import socket as socket_module

    from appletd.face_types import (
        blank_face_frame,
        face_channel_names,
        face_channel_values,
    )

    payload = encode_channels(face_channel_names(),
                              face_channel_values(blank_face_frame(), 0.0, 0))
    assert len(payload) > 9216, (
        "the face bundle no longer exceeds the default send buffer, so this test "
        "has stopped testing anything - check whether the contract shrank")

    receiver = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(0.5)
    plain = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
    try:
        with pytest.raises(OSError):
            plain.sendto(payload, receiver.getsockname())
    finally:
        plain.close()
        receiver.close()


def test_datagram_socket_sends_that_bundle_and_a_default_receiver_gets_it() -> None:
    """Both halves of the fix in one test: the SENDER needs the bigger buffer and the
    RECEIVER needs nothing - which is what makes this fixable at all, since
    TouchDesigner owns the receiving end and we cannot configure it.
    """
    import socket as socket_module

    from appletd.face_types import (
        blank_face_frame,
        face_channel_names,
        face_channel_values,
    )
    from appletd.osc import datagram_socket

    payload = encode_channels(face_channel_names(),
                              face_channel_values(blank_face_frame(), 0.0, 0))
    receiver = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))          # DEFAULT receive buffer, deliberately
    receiver.settimeout(1.0)
    sender = datagram_socket()
    try:
        sender.sendto(payload, receiver.getsockname())
        assert len(receiver.recv(1 << 17)) == len(payload)
    finally:
        sender.close()
        receiver.close()


# ---------------------------------------------------------------------------
# The typed message the text request sends
# ---------------------------------------------------------------------------
def test_typed_message_bytes_are_exactly_the_osc_format() -> None:
    """Address, then a type-tag string with one letter per argument, then each
    argument in turn - strings NUL-padded to 4, floats as big-endian float32."""
    raw = encode_typed_message("/ocr", ["ab", 1.0])
    assert raw == (b"/ocr\x00\x00\x00\x00" + b",sf\x00" + b"ab\x00\x00"
                   + struct.pack(">f", 1.0))


def test_typed_strings_go_through_as_utf8_and_stay_aligned() -> None:
    """Recognised text is not ASCII. The receiver that matters passes the bytes
    through (MEASURED in TouchDesigner: `£` and `ü` arrived intact)."""
    raw = encode_typed_message("/ocr", ["£5 für", 0.5, "", "abcd"])
    assert len(raw) % 4 == 0
    assert "£5 für".encode() in raw
    # An empty string still takes four bytes - OSC requires the terminating NUL.
    assert raw.count(b"\x00\x00\x00\x00") >= 1
    # Four characters need FOUR more bytes of padding, not zero.
    assert b"abcd\x00\x00\x00\x00" in raw


def test_typed_message_refuses_anything_but_float_and_str() -> None:
    """An int or a bool silently sent as a float is a type the receiver cannot tell
    from a measurement, so it is refused rather than coerced."""
    with pytest.raises(TypeError):
        encode_typed_message("/ocr", [1])
    with pytest.raises(TypeError):
        encode_typed_message("/ocr", [True])
    with pytest.raises(ValueError, match="must start with"):
        encode_typed_message("ocr", [])


def test_bundle_of_wraps_messages_the_same_way_encode_bundle_does() -> None:
    message = encode_message("/a", 2.0)
    assert encode_bundle_of([message]) == encode_bundle([("/a", 2.0)])
    typed = encode_typed_message("/ocr", ["x", 0.0])
    wrapped = encode_bundle_of([typed])
    assert wrapped[:8] == b"#bundle\x00"
    assert struct.unpack(">i", wrapped[16:20])[0] == len(typed)
    assert wrapped[20:] == typed


# ---------------------------------------------------------------------------
# Decoding, for the control port
# ---------------------------------------------------------------------------
def test_decode_round_trips_a_typed_message_and_a_bundle() -> None:
    message = encode_typed_message("/ocr/mode", ["manual", 0.5, ""])
    assert decode_messages(message) == [("/ocr/mode", ["manual", 0.5, ""])]
    bundle = encode_bundle_of([encode_typed_message("/a", []),
                               encode_typed_message("/b", ["x y", 2.0])])
    assert decode_messages(bundle) == [("/a", []), ("/b", ["x y", 2.0])]
    # The channel encoder's bundles decode too - the same wire, read back.
    assert decode_messages(encode_bundle([("/seq", 3.0)])) == [("/seq", [3.0])]


def test_decode_widens_an_int_and_refuses_what_it_does_not_know() -> None:
    raw = b"/x\x00\x00,i\x00\x00" + struct.pack(">i", 7)
    assert decode_messages(raw) == [("/x", [7.0])]
    with pytest.raises(ValueError, match="type tag"):
        decode_messages(b"/x\x00\x00,b\x00\x00" + struct.pack(">i", 0))
    with pytest.raises(ValueError, match="must start with"):
        decode_messages(b"x\x00\x00\x00,\x00\x00\x00")
    with pytest.raises((ValueError, IndexError, struct.error)):
        decode_messages(encode_typed_message("/ocr/mode", ["manual"])[:6])
