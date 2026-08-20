"""The sidecar's send path, tested against real UDP datagrams and no camera.

Everything here binds a real socket on an ephemeral port and reads what the
sidecar actually put on the wire, then decodes it independently. A test that
asserted on the values passed *into* the encoder would miss precisely the class
of bug that matters here - something going wrong between our frame and the bytes
TouchDesigner receives.

The camera never runs: `Sidecar` takes an injectable source, which is what the
HandSource seam in DESIGN.md 5 was for.
"""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
from collections.abc import Callable, Iterator

import pytest

from visionhands.sidecar import SEQ_MODULUS, Sidecar
from visionhands.source import FakeSource
from visionhands.types import (
    MAX_HANDS,
    N_JOINTS,
    Confidence,
    Hand,
    Joint,
    LandmarkFrame,
    NormX,
    NormY,
    blank_frame,
    channel_names,
)


def decode_bundle(payload: bytes) -> dict[str, float]:
    """An INDEPENDENT decoder, written from the OSC spec rather than from our
    encoder, so agreement means something."""
    assert payload[:8] == b"#bundle\x00"
    assert payload[8:16] == struct.pack(">Q", 1)
    out: dict[str, float] = {}
    offset = 16
    while offset < len(payload):
        size = struct.unpack(">i", payload[offset:offset + 4])[0]
        message = payload[offset + 4:offset + 4 + size]
        offset += 4 + size
        end_of_address = message.index(b"\x00")
        address = message[:end_of_address].decode("ascii")
        # Skip the padded address and the padded ",f" typetag.
        address_len = (end_of_address // 4 + 1) * 4
        value = struct.unpack(">f", message[address_len + 4:address_len + 8])[0]
        out[address.lstrip("/")] = value
    return out


def _frame(seq: int, *, found: int = 1, value: float = 0.25) -> LandmarkFrame:
    joints = tuple(
        Joint(NormX(value), NormY(value + 0.5), Confidence(0.8)) for _ in range(N_JOINTS)
    )
    hand = Hand(chirality=1, confidence=Confidence(1.0),
                conf_median=Confidence(0.8), joints=joints, found=True)
    hands = [hand] * found + [blank_frame().hands[0]] * (MAX_HANDS - found)
    return LandmarkFrame(seq=seq, captured_at=0.0, width=1280, height=720,
                         hands=tuple(hands))


@pytest.fixture
def receiver() -> Iterator[socket.socket]:
    """A real UDP socket on an ephemeral port, standing in for TD's OSC In CHOP."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    yield sock
    sock.close()


def _sidecar_for(receiver: socket.socket,
                 frames: Callable[[int], LandmarkFrame]) -> tuple[Sidecar, FakeSource]:
    """Returns the sidecar AND the concrete source.

    The Sidecar holds its source as a `HandSource`, which deliberately does not
    expose `box` - publishing is not part of the interface a consumer sees. Tests
    need to publish, so they keep the concrete object rather than reaching
    through the protocol.
    """
    source = FakeSource(frames, interval_s=0.001)
    return Sidecar(host="127.0.0.1", port=receiver.getsockname()[1], source=source), source


def test_a_frame_arrives_as_the_full_named_contract(receiver: socket.socket) -> None:
    """The end-to-end property: our frame, on the wire, named correctly."""
    sidecar, source = _sidecar_for(receiver, lambda seq: _frame(seq))
    source.box.publish(_frame(seq=7))
    sidecar.send_once()

    received = decode_bundle(receiver.recv(65535))
    assert set(received) == set(channel_names())
    assert received["seq"] == 7.0
    assert received["n_hands"] == 1.0
    assert received["h0_found"] == 1.0
    assert received["h0_conf_median"] == pytest.approx(0.8, abs=1e-6)
    assert received["h0_lm00_x"] == pytest.approx(0.25, abs=1e-6)
    # y is NOT flipped anywhere in the pipeline (DESIGN.md 7).
    assert received["h0_lm00_y"] == pytest.approx(0.75, abs=1e-6)
    # An absent hand still publishes every channel, as zeros.
    assert received["h1_found"] == 0.0
    assert received["h1_lm20_conf"] == 0.0


def test_channel_order_on_the_wire_matches_the_contract(receiver: socket.socket) -> None:
    """OSC In CHOP creates channels in arrival order, so order is worth pinning
    even though downstream references by name."""
    sidecar, source = _sidecar_for(receiver, lambda seq: _frame(seq))
    sidecar.send_once()
    payload = receiver.recv(65535)
    # Addresses appear in the payload in send order; check the first few.
    for name in ("n_hands", "seq", "age_ms", "h0_found"):
        assert ("/" + name).encode() in payload
    assert payload.index(b"/n_hands") < payload.index(b"/seq") < payload.index(b"/age_ms")


def test_seq_is_wrapped_so_it_stays_exact_in_float32(receiver: socket.socket) -> None:
    """float32 holds integers exactly only to 2**24 - 6.5 days at 30 fps - after
    which an unwrapped counter silently stops incrementing and freezes the
    staleness detection that depends on it (DESIGN.md 2.9)."""
    sidecar, source = _sidecar_for(receiver, lambda seq: _frame(seq))
    source.box.publish(_frame(seq=SEQ_MODULUS + 5))
    sidecar.send_once()
    received = decode_bundle(receiver.recv(65535))
    assert received["seq"] == 5.0
    # And the wrapped value is exactly representable, which is the whole point.
    assert struct.unpack(">f", struct.pack(">f", received["seq"]))[0] == received["seq"]


def test_seq_index_really_is_one() -> None:
    """send_once() overwrites values[1] to wrap seq. If the contract ever
    reorders, that would silently overwrite something else - so the assumption
    is asserted rather than commented."""
    assert channel_names()[1] == "seq"


def test_send_once_reports_whether_the_frame_was_new(receiver: socket.socket) -> None:
    """The status line counts real camera frames, not sends - at 60 Hz against a
    30 fps camera most sends are repeats, and reporting those as fps would be a
    lie."""
    sidecar, source = _sidecar_for(receiver, lambda seq: _frame(seq))
    source.box.publish(_frame(seq=1))
    assert sidecar.send_once() is True          # first sight of seq 1
    assert sidecar.send_once() is False         # same frame again
    source.box.publish(_frame(seq=2))
    assert sidecar.send_once() is True
    for _ in range(3):
        receiver.recv(65535)


def test_sending_keeps_working_when_the_socket_fails(receiver: socket.socket) -> None:
    """A broken socket must not take down a process that is otherwise tracking.

    Sends to a closed socket, which raises OSError, and checks the error is
    counted rather than raised.
    """
    sidecar, source = _sidecar_for(receiver, lambda seq: _frame(seq))
    sidecar.socket.close()
    sidecar.send_once()                          # must not raise
    assert sidecar.n_send_errors == 1
    assert sidecar.n_sent == 0


def test_no_source_still_sends_a_full_channel_set(receiver: socket.socket) -> None:
    """Before the camera produces anything, TD must still receive all 137
    channels - the fixed contract holds across the process boundary too."""
    sidecar, source = _sidecar_for(receiver, lambda seq: _frame(seq))
    sidecar.send_once()                          # nothing published yet
    received = decode_bundle(receiver.recv(65535))
    assert set(received) == set(channel_names())
    assert received["seq"] == 0.0
    assert received["n_hands"] == 0.0


def test_parent_watch_detects_a_dead_parent(receiver: socket.socket) -> None:
    """Without this, a TouchDesigner crash orphans a process still holding the
    camera, and the next launch fights it for the device - DESIGN.md 8's failure
    relocated rather than solved.
    """
    sidecar, source = _sidecar_for(receiver, lambda seq: _frame(seq))
    assert sidecar._parent_is_gone() is False    # no parent to watch

    sidecar.parent_pid = 1                       # launchd: always alive
    assert sidecar._parent_is_gone() is False

    # A genuinely dead pid: spawn a process, reap it, reuse its pid. More
    # faithful than a made-up number, since this is the case that actually
    # happens when TouchDesigner exits.
    child = subprocess.Popen([sys.executable, "-c", ""])
    dead_pid = child.pid
    child.wait()                       # reaped, so the pid is genuinely gone
    sidecar.parent_pid = dead_pid
    assert sidecar._parent_is_gone() is True

    # And a pid outside the OS's range, which os.kill rejects with OverflowError
    # before it even looks. Unhandled, that escaped the main loop and killed the
    # sidecar; a pid that cannot exist is not a live parent.
    sidecar.parent_pid = 4_000_000_000
    assert sidecar._parent_is_gone() is True
    assert os.getpid() != sidecar.parent_pid


def test_stop_is_safe_and_stops_the_source(receiver: socket.socket) -> None:
    """Leaving the capture session running at process exit is a native crash
    waiting for the wrong moment (DESIGN.md 8)."""
    sidecar, source = _sidecar_for(receiver, lambda seq: _frame(seq))
    source.start()
    sidecar.stop()
    assert sidecar.running is False
    assert source.running is False
