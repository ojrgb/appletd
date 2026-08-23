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
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from appletd.sidecar import SEQ_MODULUS, Sidecar
from appletd.source import FakeSource
from appletd.streams import STREAM_NAMES, status_channel_names
from appletd.types import (
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
    """The end-to-end property: our frame, on the wire, named correctly.

    The base port carries the hands contract AND the sidecar status channels, in
    one bundle, so that what the sidecar found and what it is running cannot
    disagree about which tick they describe (DESIGN.md 6.4).
    """
    sidecar, source = _sidecar_for(receiver, lambda seq: _frame(seq))
    source.box.publish(_frame(seq=7))
    sidecar.send_once()

    received = decode_bundle(receiver.recv(65535))
    assert set(received) == set(channel_names()) | set(status_channel_names())
    assert received["seq"] == 7.0
    assert received["n_hands"] == 1.0
    assert received["h0_found"] == 1.0
    assert received["h0_conf_median"] == pytest.approx(0.8, abs=1e-6)
    assert received["h0_wrist_x"] == pytest.approx(0.25, abs=1e-6)
    # y is NOT flipped anywhere in the pipeline (DESIGN.md 7).
    assert received["h0_wrist_y"] == pytest.approx(0.75, abs=1e-6)
    # An absent hand still publishes every channel, as zeros.
    assert received["h1_found"] == 0.0
    assert received["h1_little_tip_conf"] == 0.0


def test_channel_order_on_the_wire_matches_the_contract(receiver: socket.socket) -> None:
    """OSC In CHOP creates channels in arrival order, so order is worth pinning
    even though downstream references by name."""
    sidecar, _ = _sidecar_for(receiver, lambda seq: _frame(seq))
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
    sidecar, _ = _sidecar_for(receiver, lambda seq: _frame(seq))
    sidecar.socket.close()
    sidecar.send_once()                          # must not raise
    # One per STREAM, not one per tick: send_once() puts a bundle on the wire for
    # each of hands, pose and face, and all three fail on a closed socket.
    # Counting sends rather than ticks is what makes "the socket is broken"
    # visible per bundle (DESIGN.md 6.4).
    assert sidecar.n_send_errors == len(STREAM_NAMES)
    assert sidecar.n_sent == 0


def test_no_source_still_sends_a_full_channel_set(receiver: socket.socket) -> None:
    """Before the camera produces anything, TD must still receive all 137
    channels - the fixed contract holds across the process boundary too."""
    sidecar, _ = _sidecar_for(receiver, lambda seq: _frame(seq))
    sidecar.send_once()                          # nothing published yet
    received = decode_bundle(receiver.recv(65535))
    assert set(received) == set(channel_names()) | set(status_channel_names())
    assert received["seq"] == 0.0
    assert received["n_hands"] == 0.0


def test_parent_watch_detects_a_dead_parent(receiver: socket.socket) -> None:
    """Without this, a TouchDesigner crash orphans a process still holding the
    camera, and the next launch fights it for the device - DESIGN.md 8's failure
    relocated rather than solved.
    """
    sidecar, _ = _sidecar_for(receiver, lambda seq: _frame(seq))
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


# ---------------------------------------------------------------------------
# run() - the main loop
#
# WHY THIS SECTION EXISTS. Everything above tests `send_once`, `stop` and the
# parent watch as separate pieces, and they all passed while `run()` itself had no
# test at all - which is the loop the whole process is. Five mutations survived a
# green suite, including one where the loop sent nothing whatsoever, and that is
# not a gap you can see by reading: the pieces are individually correct and the
# assembly is what breaks.
#
# The loop is driven to a real exit rather than cut off with a timer: a dead
# parent pid is a condition `run()` genuinely checks, so the test exercises the
# same break the shipping code takes when TouchDesigner quits.
# ---------------------------------------------------------------------------
def _bounded(sidecar: Sidecar, seconds: float = 3.0) -> list[str]:
    """Stop `sidecar` from outside after `seconds`. Returns a list that is non-empty
    only if it had to intervene.

    WHY EVERY run() TEST NEEDS THIS. These tests end the loop by giving it a dead
    parent pid, so they all depend on the very `break` they are meant to be
    checking. Deleting that break was tried as a mutation: the suite HUNG rather
    than failed, and a test whose failure mode is a hang is worse than no test at
    all in CI.

    This bound is independent of the code under test, so the loop always ends - and
    asserting the list is empty turns "it never exited on its own" into an ordinary
    failure with a readable message.
    """
    fired: list[str] = []

    def guillotine() -> None:
        time.sleep(seconds)
        if sidecar.running:
            fired.append("the loop was still running after %.1fs" % seconds)
            sidecar.running = False

    threading.Thread(target=guillotine, daemon=True).start()
    return fired


def _dead_pid() -> int:
    """A pid that genuinely existed and genuinely does not now.

    Spawned and reaped rather than invented, for the reason the parent-watch test
    above gives: this is the case that actually happens when TD exits.
    """
    child = subprocess.Popen([sys.executable, "-c", ""])
    pid = child.pid
    child.wait()
    return pid


def test_run_sends_frames_and_returns_when_the_parent_goes(
        receiver: socket.socket, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE test the loop did not have. A mutation that stopped `send_once` being
    called at all left every other test in this file passing.

    Both halves matter: datagrams have to arrive, and the loop has to end by
    itself. A loop that sends and never exits is the orphan holding the camera.
    """
    monkeypatch.setattr("appletd.sidecar.PARENT_CHECK_INTERVAL_S", 0.02)
    sidecar, source = _sidecar_for(receiver, lambda seq: _frame(seq))
    sidecar.send_interval_s = 0.002
    sidecar.parent_pid = _dead_pid()
    overran = _bounded(sidecar)

    assert sidecar.run() == 0
    assert not overran, overran

    receiver.settimeout(0.5)
    payload, _ = receiver.recvfrom(65535)
    assert decode_bundle(payload)                 # a real, decodable bundle
    assert sidecar.n_sent > 0
    assert source.running is False                # the finally ran


def test_running_is_set_before_start_so_a_stop_during_warm_up_is_not_lost(
        receiver: socket.socket, monkeypatch: pytest.MonkeyPatch) -> None:
    """`self.running = True` sits ABOVE `self.start()`, and the order is the
    behaviour: a signal arriving while the camera warms up sets `running` False,
    and the loop must then never run. Setting the flag after `start()` threw that
    request away and the sidecar carried on into the loop.

    Reproduced here by a source that stops the sidecar from inside its own
    `start()`, which is exactly the shape of a signal landing during warm-up.

    THE DEAD PARENT PID IS NOT DECORATION. Without it, moving `running = True`
    below `start()` makes this loop run for ever and the test HANGS instead of
    failing - verified by making that exact mutation, which froze the suite rather
    than reporting anything. A test whose failure mode is a hang is worse than no
    test in CI, so the loop is given a way out and the assertion is on `n_sent`:
    zero if the request was honoured, non-zero if it was thrown away.
    """
    monkeypatch.setattr("appletd.sidecar.PARENT_CHECK_INTERVAL_S", 0.02)
    sidecar, source = _sidecar_for(receiver, lambda seq: _frame(seq))
    sidecar.send_interval_s = 0.002
    sidecar.parent_pid = _dead_pid()
    real_start = source.start

    def start_then_ask_to_stop() -> None:
        real_start()
        sidecar.running = False

    source.start = start_then_ask_to_stop         # type: ignore[method-assign]
    overran = _bounded(sidecar)

    assert sidecar.run() == 0
    assert not overran, overran
    assert sidecar.n_sent == 0, "the loop ran despite being asked to stop"
    assert source.running is False


def test_a_start_that_raises_still_stops_everything(
        receiver: socket.socket) -> None:
    """`start()` is inside a try whose except calls `stop()` and re-raises. Without
    it a raising start left the socket open and the source never stopped - and
    `HandSource` promises `stop()` is safe whether or not `start()` succeeded
    precisely so this path can lean on it.
    """
    sidecar, source = _sidecar_for(receiver, lambda seq: _frame(seq))
    stopped: list[bool] = []
    real_stop = source.stop

    def boom() -> None:
        raise RuntimeError("camera did not open")

    def record_stop() -> None:
        stopped.append(True)
        real_stop()

    source.start = boom                           # type: ignore[method-assign]
    source.stop = record_stop                     # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="camera did not open"):
        sidecar.run()
    assert stopped, "a raising start() left the source running"
    assert sidecar.running is False


def test_the_status_line_names_the_streams_that_are_on(
        receiver: socket.socket, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The status line is the only window into a running sidecar, and it has been
    wrong twice: it reported hands' `age` and a hands-derived rate while hands was
    disabled, which read as a catastrophe and measured nothing (2026-08-22).

    So: `sends` is always there, and `age`/`hands` only when hands is actually on.
    """
    monkeypatch.setattr("appletd.sidecar.STATUS_INTERVAL_S", 0.02)
    monkeypatch.setattr("appletd.sidecar.PARENT_CHECK_INTERVAL_S", 0.08)
    sidecar, _ = _sidecar_for(receiver, lambda seq: _frame(seq))
    sidecar.send_interval_s = 0.002
    sidecar.parent_pid = _dead_pid()
    overran = _bounded(sidecar)

    assert sidecar.run() == 0
    assert not overran, overran
    out = capsys.readouterr().out
    status = [line for line in out.splitlines() if line.startswith("sends ")]
    assert status, "no status line was printed at all"
    assert "age" in status[0] and "hands" in status[0]    # hands is on by default
