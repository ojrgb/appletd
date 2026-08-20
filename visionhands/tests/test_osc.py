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

from visionhands.osc import encode_bundle, encode_channels, encode_message
from visionhands.types import N_CHANNELS, blank_frame, channel_names, channel_values

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
