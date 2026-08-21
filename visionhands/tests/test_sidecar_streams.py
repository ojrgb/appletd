"""Several streams on the wire: three ports, three bundles, and the status channels.

The same approach as `test_sidecar.py` - bind real sockets, read the actual
datagrams, decode them independently - extended to the property that matters once
there is more than one stream: **each bundle goes to its OWN port and is sent
whether or not the stream is enabled** (DESIGN.md 6.4).

That second half is the one worth a test rather than an inspection. "A disabled
stream sends zeros" is a decision that looks like an oversight from the outside,
and the failure it prevents is silent: TouchDesigner's OSC In CHOP creates
channels as they arrive, so a stream that stops sending makes its channels VANISH
from the CHOP, and an operator referencing a vanished channel stops receiving
with no error anywhere (DESIGN.md 6.2).

No camera: the source is a fake, as it is for every other test of this path.

Ref: DESIGN.md 6.4; visionhands/streams.py; visionhands/sidecar.py.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from visionhands.face_types import FaceFrame, blank_face_frame, face_channel_names
from visionhands.pose_types import PoseFrame, blank_pose_frame, pose_channel_names
from visionhands.sidecar import SEQ_MODULUS, Sidecar
from visionhands.source import (
    FaceSource,
    HandSource,
    LatestFaceBox,
    LatestFrameBox,
    LatestPoseBox,
    PoseSource,
)
from visionhands.streams import (
    STREAM_FACE,
    STREAM_HANDS,
    STREAM_NAMES,
    STREAM_POSE,
    port_for,
    status_channel_names,
)
from visionhands.tests.test_sidecar import decode_bundle
from visionhands.types import LandmarkFrame, blank_frame

# The ports have to be ADJACENT, because that is what the port scheme means: hands
# on the base, pose on base + 1, face on base + 2. So the fixture cannot just take
# three ephemeral ports - it has to find a base whose two neighbours are free.
MAX_PORT_ATTEMPTS = 40
N_PORTS = len(STREAM_NAMES)


class FakeStreamSource:
    """A source that implements all three halves: hands, pose and face. No camera.

    Deliberately NOT reusing `FakeSource`: that one publishes from a background
    thread on a timer, which is right for testing the age and liveness behaviour
    and wrong here, where every test wants to control exactly what is in the box
    when `send_once()` reads it. Publishing by hand makes each assertion about one
    known frame.
    """

    def __init__(self, streams: tuple[str, ...] = STREAM_NAMES) -> None:
        self.box = LatestFrameBox()
        self.pose_box = LatestPoseBox()
        self.face_box = LatestFaceBox()
        self._streams = streams
        self._running = False

    # -- HandSource --------------------------------------------------------
    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def latest(self) -> LandmarkFrame:
        return self.box.latest()

    def age_ms(self) -> float:
        return self.box.age_ms()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def errors(self) -> list[str]:
        return []

    # -- PoseSource --------------------------------------------------------
    def latest_pose(self) -> PoseFrame:
        return self.pose_box.latest()

    def pose_age_ms(self) -> float:
        return self.pose_box.age_ms()

    @property
    def streams_started(self) -> tuple[str, ...]:
        return self._streams if self._running else ()

    # -- FaceSource --------------------------------------------------------
    def latest_face(self) -> FaceFrame:
        return self.face_box.latest()

    def face_age_ms(self) -> float:
        return self.face_box.age_ms()


@pytest.fixture
def ports() -> Iterator[tuple[socket.socket, ...]]:
    """One socket per stream, on ADJACENT ports, in STREAM_NAMES order."""
    for _attempt in range(MAX_PORT_ATTEMPTS):
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        first.bind(("127.0.0.1", 0))
        base = first.getsockname()[1]
        socks = [first]
        for offset in range(1, N_PORTS):
            extra = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                extra.bind(("127.0.0.1", base + offset))
            except OSError:
                # A neighbour is taken by something else on this machine. Try
                # another base rather than skipping the test, which would leave
                # the whole port scheme unverified on a busy machine.
                extra.close()
                break
            socks.append(extra)
        if len(socks) != N_PORTS:
            for sock in socks:
                sock.close()
            continue
        for sock in socks:
            sock.settimeout(2.0)
        yield tuple(socks)
        for sock in socks:
            sock.close()
        return
    pytest.fail("could not find %d adjacent free UDP ports in %d attempts"
                % (N_PORTS, MAX_PORT_ATTEMPTS))


def _sidecar(ports: tuple[socket.socket, ...],
             streams: tuple[str, ...] = STREAM_NAMES,
             ) -> tuple[Sidecar, FakeStreamSource]:
    source = FakeStreamSource(streams)
    source.start()
    sidecar = Sidecar(host="127.0.0.1", port=ports[0].getsockname()[1],
                      source=source, streams=streams)
    return sidecar, source


# ---------------------------------------------------------------------------
# One bundle per stream, on its own port
# ---------------------------------------------------------------------------
def test_the_fake_source_really_satisfies_all_three_protocols() -> None:
    """Asserted rather than assumed: the sidecar picks its senders with
    `isinstance`, so a fake that quietly stopped matching one protocol would make
    that stream fall back to blank frames and every assertion about it would pass
    for the wrong reason."""
    source = FakeStreamSource()
    assert isinstance(source, HandSource)
    assert isinstance(source, PoseSource)
    assert isinstance(source, FaceSource)


def test_each_stream_lands_on_its_own_port(
        ports: tuple[socket.socket, ...]) -> None:
    """The property separate ports were chosen for: no stream's channels can
    appear in another stream's OSC In CHOP (DESIGN.md 6.4)."""
    hands_sock, pose_sock = ports[0], ports[1]
    sidecar, _ = _sidecar(ports)
    sidecar.send_once()

    hands = decode_bundle(hands_sock.recv(65535))
    pose = decode_bundle(pose_sock.recv(65535))

    assert set(pose) == set(pose_channel_names())
    # Neither bundle carries a channel belonging to the other.
    assert not set(pose) & set(hands)
    assert "h0_wrist_x" in hands and "h0_wrist_x" not in pose
    assert "p0_nose_x" in pose and "p0_nose_x" not in hands


def test_the_face_bundle_lands_on_its_own_port_too(
        ports: tuple[socket.socket, ...]) -> None:
    """Three streams, three ports, no overlap. The face contract is head pose and
    the bounding box for now - the 76 landmark points wait on one measurement
    (visionhands/face_types.py) - and this is what says the plumbing does not."""
    sidecar, _ = _sidecar(ports)
    sidecar.send_once()
    face = decode_bundle(ports[2].recv(65535))
    assert set(face) == set(face_channel_names())
    assert "f0_yaw" in face
    hands = decode_bundle(ports[0].recv(65535))
    pose = decode_bundle(ports[1].recv(65535))
    assert not set(face) & (set(hands) | set(pose))


def test_a_published_face_frame_arrives_with_its_angles_in_degrees(
        ports: tuple[socket.socket, ...]) -> None:
    """End to end for the unit that matters: Vision reports radians, face.py
    converts once, and a stream that sent radians would leave that conversion
    untested."""
    import dataclasses

    from visionhands.face_types import BLANK_FACE, Face
    from visionhands.types import Confidence, NormX, NormY

    sidecar, source = _sidecar(ports)
    face = Face(confidence=Confidence(1.0), quality=Confidence(0.6),
                roll_deg=45.0, yaw_deg=-30.0, pitch_deg=15.0,
                bbox_x=NormX(0.3), bbox_y=NormY(0.4), bbox_w=0.2, bbox_h=0.3,
                found=True)
    source.face_box.publish(dataclasses.replace(
        blank_face_frame(seq=4), faces=(face, BLANK_FACE)))
    sidecar.send_once()

    received = decode_bundle(ports[2].recv(65535))
    assert received["face_seq"] == 4.0
    assert received["face_n"] == 1.0
    assert received["f0_roll"] == pytest.approx(45.0, abs=1e-4)
    assert received["f0_yaw"] == pytest.approx(-30.0, abs=1e-4)
    assert received["f0_quality"] == pytest.approx(0.6, abs=1e-6)
    assert received["f0_bbox_y"] == pytest.approx(0.4, abs=1e-6)


def test_a_disabled_face_stream_still_sends_its_channels(
        ports: tuple[socket.socket, ...]) -> None:
    """Face ships OFF by default, so this is the case every project starts in: the
    channels have to be there, reading zero, or a project that switches the stream
    on later has nothing to reference until it restarts."""
    sidecar, _ = _sidecar(ports, streams=(STREAM_HANDS,))
    sidecar.send_once()
    received = decode_bundle(ports[2].recv(65535))
    assert set(received) == set(face_channel_names())
    assert all(value == 0.0 for value in received.values())


def test_the_pose_port_is_the_base_plus_one(
        ports: tuple[socket.socket, ...]) -> None:
    """Stated as a test because getting it wrong is silent: TouchDesigner shows an
    OSC In CHOP with no channels and no error."""
    sidecar, _ = _sidecar(ports)
    assert port_for(STREAM_POSE, sidecar.port) == ports[1].getsockname()[1]


def test_a_published_pose_frame_arrives_with_its_values(
        ports: tuple[socket.socket, ...]) -> None:
    pose_sock = ports[1]
    sidecar, source = _sidecar(ports)
    frame = blank_pose_frame(seq=9, captured_at=0.0, width=1280, height=720)
    source.pose_box.publish(frame)
    sidecar.send_once()

    received = decode_bundle(pose_sock.recv(65535))
    assert received["pose_seq"] == 9.0
    assert received["pose_n_bodies"] == 0.0


def test_pose_seq_wraps_for_float32_exactness(
        ports: tuple[socket.socket, ...]) -> None:
    """The same trap as the hands `seq` (DESIGN.md 2.9): past 2**24 a float32
    stops incrementing, and a counter that silently stops counting defeats every
    freshness test downstream."""
    pose_sock = ports[1]
    sidecar, source = _sidecar(ports)
    source.pose_box.publish(blank_pose_frame(seq=SEQ_MODULUS + 5))
    sidecar.send_once()
    assert decode_bundle(pose_sock.recv(65535))["pose_seq"] == 5.0


# ---------------------------------------------------------------------------
# A disabled stream keeps its channels
# ---------------------------------------------------------------------------
def test_a_disabled_stream_still_sends_its_channels_as_zeros(
        ports: tuple[socket.socket, ...]) -> None:
    """The decision that looks like an oversight from outside, pinned. Omitting
    the bundle would make these channels vanish from the OSC In CHOP, and a
    vanished channel breaks its consumers silently (DESIGN.md 6.2)."""
    pose_sock = ports[1]
    sidecar, _ = _sidecar(ports, streams=(STREAM_HANDS,))
    sidecar.send_once()

    received = decode_bundle(pose_sock.recv(65535))
    assert set(received) == set(pose_channel_names())
    assert all(value == 0.0 for value in received.values())


def test_a_source_with_no_pose_support_still_sends_the_pose_contract(
        ports: tuple[socket.socket, ...]) -> None:
    """A hands-only source - a test double, or a future transport that implements
    one stream - must not make the pose channels disappear."""
    from visionhands.source import FakeSource

    hands_only = FakeSource(lambda seq: blank_frame(seq=seq), interval_s=0.01)
    assert not isinstance(hands_only, PoseSource)
    sidecar = Sidecar(host="127.0.0.1", port=ports[0].getsockname()[1],
                      source=hands_only)
    sidecar.send_once()
    assert set(decode_bundle(ports[1].recv(65535))) == set(pose_channel_names())


def test_a_disabled_streams_age_stays_zero_rather_than_climbing(
        ports: tuple[socket.socket, ...]) -> None:
    """A rising age means "data was expected and never came" (DESIGN.md 6.2).
    Nothing is expected from a stream nobody asked for, so its age must read 0 -
    otherwise a disabled stream looks like a broken one."""
    pose_sock = ports[1]
    sidecar, _ = _sidecar(ports, streams=(STREAM_HANDS,))
    sidecar.send_once()
    assert decode_bundle(pose_sock.recv(65535))["pose_age_ms"] == 0.0


# ---------------------------------------------------------------------------
# The status channels
# ---------------------------------------------------------------------------
def test_the_status_channels_ride_with_the_hands_bundle(
        ports: tuple[socket.socket, ...]) -> None:
    """On the base port, so they survive every stream being off (DESIGN.md 6.4)."""
    hands_sock = ports[0]
    sidecar, _ = _sidecar(ports)
    sidecar.send_once()
    received = decode_bundle(hands_sock.recv(65535))
    assert set(status_channel_names()) <= set(received)


def test_the_status_channels_report_what_is_RUNNING(
        ports: tuple[socket.socket, ...]) -> None:
    """Not what was requested. This is the whole reason they exist: after somebody
    flips a toggle without restarting, a panel reading the toggle lies."""
    hands_sock = ports[0]
    sidecar, source = _sidecar(ports, streams=(STREAM_HANDS,))
    sidecar.send_once()
    reading = decode_bundle(hands_sock.recv(65535))
    assert reading["sc_hands"] == 1.0
    assert reading["sc_pose"] == 0.0
    assert reading["sc_face"] == 0.0

    # The source stops: every stream must read 0 immediately, even though
    # `sidecar.streams` still says hands was asked for.
    source.stop()
    sidecar.send_once()
    assert decode_bundle(hands_sock.recv(65535))["sc_hands"] == 0.0
    assert sidecar.streams == (STREAM_HANDS,)


def test_the_face_status_channel_follows_the_face_stream(
        ports: tuple[socket.socket, ...]) -> None:
    """`sc_face` existed and read 0 before the stream was built, which is why the
    status channel list did not have to grow when it landed (DESIGN.md 6.2). Now it
    has to actually track it."""
    hands_sock = ports[0]
    sidecar, _ = _sidecar(ports, streams=(STREAM_HANDS, STREAM_FACE))
    sidecar.send_once()
    reading = decode_bundle(hands_sock.recv(65535))
    assert reading["sc_face"] == 1.0
    assert reading["sc_pose"] == 0.0


def test_uptime_rises_so_a_frozen_channel_means_a_dead_process(
        ports: tuple[socket.socket, ...]) -> None:
    """The signal that tells "sidecar dead" apart from "stream switched off": a
    disabled stream's own seq is frozen too, so seq alone cannot answer it."""
    hands_sock = ports[0]
    sidecar, _ = _sidecar(ports)
    sidecar.send_once()
    first = decode_bundle(hands_sock.recv(65535))["sc_uptime_s"]
    # Busy-wait on the clock rather than sleeping: this asserts the value moves,
    # and a sleep long enough to be safe against float32 resolution would be
    # slower than spinning for a fraction of a millisecond.
    import time
    target = time.monotonic() + 0.002
    while time.monotonic() < target:
        pass
    sidecar.send_once()
    assert decode_bundle(hands_sock.recv(65535))["sc_uptime_s"] > first


# ---------------------------------------------------------------------------
# Newness, which drives the status line's fps figure
# ---------------------------------------------------------------------------
def test_a_new_pose_frame_counts_as_news_on_its_own(
        ports: tuple[socket.socket, ...]) -> None:
    """With hands disabled, testing only the hands seq would report 0 fps while
    pose frames were arriving perfectly well."""
    sidecar, source = _sidecar(ports, streams=(STREAM_POSE,))
    assert sidecar.send_once() is True           # first sight of both blanks
    assert sidecar.send_once() is False          # nothing changed
    source.pose_box.publish(blank_pose_frame(seq=3))
    assert sidecar.send_once() is True
    assert sidecar.send_once() is False


def test_every_stream_goes_out_on_every_tick(
        ports: tuple[socket.socket, ...]) -> None:
    """Not only when something changed, and not only the enabled ones. The send
    clock is what makes `age_ms` a live signal (see the send-rate comment in
    sidecar.py); a stream that went quiet when nothing moved would freeze its own
    staleness indicator, and one that went quiet when DISABLED would take its
    channels out of TouchDesigner altogether."""
    sidecar, _ = _sidecar(ports)
    for _ in range(3):
        sidecar.send_once()
    assert sidecar.n_sent == 3 * len(STREAM_NAMES)
