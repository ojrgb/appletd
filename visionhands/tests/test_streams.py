"""The stream registry: ports, the launch flag, and the status contract.

Small surface, and every part of it is a place where being silently wrong is the
failure mode. A wrong port is an OSC In CHOP with no channels and no error. A
status channel that reports what was REQUESTED rather than what is RUNNING makes
the panel lie in exactly the situation it exists for.

Ref: DESIGN.md 6.4, visionhands/streams.py.
"""

from __future__ import annotations

import pytest

from visionhands.streams import (
    BASE_PORT,
    DEFAULT_STREAMS,
    STREAM_FACE,
    STREAM_HANDS,
    STREAM_NAMES,
    STREAM_POSE,
    format_streams,
    parse_streams,
    port_for,
    status_channel_names,
    status_channel_values,
)


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------
def test_hands_stays_on_the_base_port() -> None:
    """Not a preference: an existing TouchDesigner project, and every note in
    this repo that says port 10000, depend on it."""
    assert port_for(STREAM_HANDS) == BASE_PORT == 10000


def test_every_stream_gets_a_distinct_port() -> None:
    """Two streams on one port would interleave into one OSC In CHOP, which is
    the whole thing DESIGN.md 6.4 chose separate ports to prevent."""
    ports = [port_for(name) for name in STREAM_NAMES]
    assert len(set(ports)) == len(STREAM_NAMES)


def test_the_base_port_moves_the_whole_block() -> None:
    """`--port` relocates every stream together, so the offsets stay meaningful
    on a machine where 10000 is taken."""
    assert port_for(STREAM_POSE, 20000) - port_for(STREAM_HANDS, 20000) == \
        port_for(STREAM_POSE) - port_for(STREAM_HANDS)


def test_an_unknown_stream_raises_rather_than_returning_a_number() -> None:
    """A plausible-looking wrong port fails silently at the far end."""
    with pytest.raises(ValueError, match="unknown stream"):
        port_for("elbows")


# ---------------------------------------------------------------------------
# The launch flag
# ---------------------------------------------------------------------------
def test_a_comma_list_parses_in_canonical_order() -> None:
    """The order somebody types must not change which port anything lands on."""
    assert parse_streams("pose,hands") == (STREAM_HANDS, STREAM_POSE)
    assert parse_streams("hands,pose") == (STREAM_HANDS, STREAM_POSE)


@pytest.mark.parametrize("text", ["hands", " hands ", "HANDS", "hands,hands"])
def test_whitespace_case_and_duplicates_are_tolerated(text: str) -> None:
    """This value is assembled by a TouchDesigner callback from toggles, so it is
    machine-written and can pick up all three."""
    assert parse_streams(text) == (STREAM_HANDS,)


def test_an_empty_selection_is_an_error() -> None:
    """A sidecar with no streams opens the camera, runs nothing, and looks exactly
    like a working sidecar with nobody in frame."""
    for text in ("", "   ", ",", ",,"):
        with pytest.raises(ValueError, match="no streams selected"):
            parse_streams(text)


def test_an_unknown_stream_names_the_ones_that_exist() -> None:
    """The error has to be actionable on the command line where it was typed."""
    with pytest.raises(ValueError, match="unknown stream"):
        parse_streams("hands,elbow")


def test_every_declared_stream_can_actually_be_asked_for() -> None:
    """They were not always the same set: `face` sat in STREAM_NAMES for a day
    before it was built, so that `sc_face` existed from the start rather than
    appearing from nowhere later (DESIGN.md 6.2). All three are built now, and this
    is what says so - if a fourth is ever declared early, this test is where the
    two sets part company again."""
    assert parse_streams(",".join(STREAM_NAMES)) == STREAM_NAMES
    assert STREAM_FACE in STREAM_NAMES


def test_the_default_is_what_ran_before_there_was_a_choice() -> None:
    """A default that turned pose on would make every existing project pay for an
    inference it does not read."""
    assert DEFAULT_STREAMS == (STREAM_HANDS,)


def test_format_and_parse_round_trip() -> None:
    """The TouchDesigner side formats, the sidecar parses. If these disagree, the
    process starts with streams nobody asked for."""
    for streams in ((STREAM_HANDS,), (STREAM_POSE,), (STREAM_FACE,),
                    (STREAM_HANDS, STREAM_POSE), STREAM_NAMES):
        assert parse_streams(format_streams(streams)) == streams


# ---------------------------------------------------------------------------
# The status contract
# ---------------------------------------------------------------------------
def test_the_status_channels_are_named_and_ordered() -> None:
    assert status_channel_names() == ("sc_uptime_s", "sc_hands", "sc_pose", "sc_face")


def test_status_values_line_up_with_status_names() -> None:
    """Names and values are one contract; a misalignment here would report pose's
    state in the face channel."""
    names = status_channel_names()
    values = status_channel_values(12.5, (STREAM_POSE,))
    assert len(names) == len(values)
    reading = dict(zip(names, values, strict=True))
    assert reading["sc_uptime_s"] == 12.5
    assert reading["sc_pose"] == 1.0
    assert reading["sc_hands"] == 0.0
    assert reading["sc_face"] == 0.0


def test_a_stream_that_did_not_start_reads_zero() -> None:
    """The distinction these channels exist for: `started` is what the sidecar is
    RUNNING, not what somebody asked for. A requested stream that failed to start
    must read 0, or the panel reports the request rather than the state."""
    reading = dict(zip(status_channel_names(),
                       status_channel_values(0.0, ()), strict=True))
    assert reading["sc_hands"] == 0.0
    assert reading["sc_pose"] == 0.0


def test_status_values_ignore_a_stream_nobody_has_heard_of() -> None:
    """Robustness where it costs nothing: an unknown name in `started` must not
    shift the values of the channels that do exist."""
    values = status_channel_values(1.0, ("hands", "elbows"))
    assert len(values) == len(status_channel_names())
    assert values[1] == 1.0                      # sc_hands, still in place
