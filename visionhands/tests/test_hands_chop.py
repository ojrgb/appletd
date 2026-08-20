"""The Script CHOP, tested with no TouchDesigner present.

A Script CHOP that can only be exercised by dropping it into a project and
looking at it is a Script CHOP nobody tests. So the cook logic is a pure
function over a small TD surface - `clear()`, `appendChan(name)`, `numSamples`,
`rate` - and `FakeScriptOp` below implements exactly that surface, mimicking the
real API as documented in TouchDesigner's own shipped stubs
(`tdi/ops/chops/scriptCHOP.py`) and examples.

What this CANNOT test, and what still needs a human in TouchDesigner: that TD
calls these callbacks at all, that `CookLevel.WHEN_USED` behaves as documented,
and that 137 channels actually appear in the operator. Those are milestone 4's
manual verification, and no amount of test-doubling substitutes for them.

Ref: DESIGN.md 6.2 (the fixed channel contract), 4.1 (never block).
"""

from __future__ import annotations

from typing import Any

import pytest

from visionhands.source import FakeSource, LatestFrameBox
from visionhands.td import bootstrap, hands_chop
from visionhands.types import (
    CHIRALITY_LEFT,
    MAX_HANDS,
    N_CHANNELS,
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


class FakeChannel:
    """One channel. Supports `chan[0] = value`, the idiom TD's examples use."""

    def __init__(self, name: str, n_samples: int) -> None:
        self.name = name
        self.vals = [0.0] * n_samples

    def __setitem__(self, index: int, value: float) -> None:
        self.vals[index] = value

    def __getitem__(self, index: int) -> float:
        return self.vals[index]


class FakeScriptOp:
    """The smallest thing that behaves like a Script CHOP.

    Mirrors the real API's important behaviours rather than just accepting calls:
    `clear()` really does drop every channel, `appendChan` really does return
    something writable, and appending a duplicate name is rejected - TD would
    silently rename it, which is worse, and a duplicate in our channel list
    would mean a downstream reference resolving to the wrong one.
    """

    def __init__(self) -> None:
        self.channels: list[FakeChannel] = []
        self.numSamples = 0
        self.rate = 0.0
        self.n_clears = 0

    def clear(self) -> None:
        self.channels = []
        self.n_clears += 1

    def appendChan(self, name: str) -> FakeChannel:
        if any(c.name == name for c in self.channels):
            raise AssertionError("duplicate channel name: %s" % name)
        channel = FakeChannel(name, max(self.numSamples, 1))
        self.channels.append(channel)
        return channel

    # -- test helpers, not part of TD's API --
    def as_dict(self) -> dict[str, float]:
        return {c.name: c[0] for c in self.channels}

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.channels]


def _hand(value: float, *, found: bool = True, chirality: int = 1) -> Hand:
    joints = tuple(
        Joint(NormX(value), NormY(value + 0.1), Confidence(value))
        for _ in range(N_JOINTS)
    )
    return Hand(chirality=chirality, confidence=Confidence(1.0),
                conf_median=Confidence(value), joints=joints, found=found)


def _frame(seq: int = 42, n_found: int = 1) -> LandmarkFrame:
    hands = [_hand(0.5) for _ in range(n_found)]
    hands += [blank_frame().hands[0] for _ in range(MAX_HANDS - n_found)]
    return LandmarkFrame(seq=seq, captured_at=0.0, width=1280, height=720,
                         hands=tuple(hands))


@pytest.fixture(autouse=True)
def _no_leaked_source() -> Any:
    """Every test starts and ends with no source bootstrapped.

    bootstrap holds a module global on purpose - a cook cannot afford to
    construct anything - which means tests must clean up after each other or one
    test's source silently answers another's cook.
    """
    bootstrap.stop()
    yield
    bootstrap.stop()


# ---------------------------------------------------------------------------
# The value list, on its own
# ---------------------------------------------------------------------------
def test_values_line_up_with_names_one_for_one() -> None:
    """The alignment that everything downstream depends on.

    A drift between `channel_names()` and `channel_values()` would put a joint's
    y into another channel and still look plausible on screen - the worst kind of
    defect in this system. The zip in write_channels is strict for the same
    reason; this asserts it before it can ever be reached.
    """
    values = hands_chop.channel_values(_frame(), age_ms=12.0, n_hands=1)
    assert len(values) == len(channel_names()) == N_CHANNELS


def test_frame_scalars_come_first_and_carry_the_right_numbers() -> None:
    values = hands_chop.channel_values(_frame(seq=99), age_ms=7.5, n_hands=1)
    assert values[0] == 1.0         # n_hands
    assert values[1] == 99.0        # seq
    assert values[2] == 7.5         # age_ms


def test_everything_published_is_a_float() -> None:
    """A CHOP channel is a float. `found` is 1.0/0.0 and chirality is -1.0/0.0/1.0
    rather than anything cleverer, because the far end is TD arithmetic."""
    values = hands_chop.channel_values(_frame(), age_ms=0.0, n_hands=1)
    assert all(type(v) is float for v in values)


def test_a_hand_is_published_in_the_documented_order() -> None:
    named = dict(zip(channel_names(),
                     hands_chop.channel_values(_frame(), 0.0, 1), strict=True))
    assert named["h0_found"] == 1.0
    assert named["h0_score"] == 1.0          # Vision's constant, mirrored
    assert named["h0_conf_median"] == 0.5    # ours, the one to gate on
    assert named["h0_chirality"] == 1.0
    assert named["h0_lm00_x"] == 0.5
    assert named["h0_lm00_y"] == 0.6         # y is NOT flipped (DESIGN.md 7)
    assert named["h0_lm00_conf"] == 0.5


def test_an_absent_hand_publishes_zeros_with_every_channel_present() -> None:
    """The heart of the fixed-contract rule (DESIGN.md 6.2)."""
    named = dict(zip(channel_names(),
                     hands_chop.channel_values(_frame(n_found=1), 0.0, 1), strict=True))
    assert named["h1_found"] == 0.0
    assert named["h1_score"] == 0.0           # NOT 1.0 - an empty slot is not confident
    assert named["h1_conf_median"] == 0.0
    for joint_index in range(N_JOINTS):
        for suffix in ("x", "y", "conf"):
            assert named["h1_lm%02d_%s" % (joint_index, suffix)] == 0.0


def test_negative_chirality_survives_as_a_float() -> None:
    frame = LandmarkFrame(seq=1, captured_at=0.0, width=1280, height=720,
                          hands=(_hand(0.5, chirality=CHIRALITY_LEFT),
                                 blank_frame().hands[0]))
    named = dict(zip(channel_names(),
                     hands_chop.channel_values(frame, 0.0, 1), strict=True))
    assert named["h0_chirality"] == -1.0


# ---------------------------------------------------------------------------
# Writing onto a Script CHOP
# ---------------------------------------------------------------------------
def test_write_channels_produces_exactly_the_contract() -> None:
    script_op = FakeScriptOp()
    count = hands_chop.write_channels(script_op)
    assert count == N_CHANNELS == 137
    assert script_op.names == list(channel_names())
    assert script_op.numSamples == 1
    assert script_op.n_clears == 1


def test_the_channel_set_is_identical_with_and_without_a_source() -> None:
    """The property that stops downstream references breaking.

    Before the camera starts, after it dies, and while it is running, the channel
    list must be byte-identical. Anything else and the operator referencing
    h1_lm08_x silently stops receiving at the worst possible moment.
    """
    without = FakeScriptOp()
    hands_chop.write_channels(without)

    source = FakeSource(lambda seq: _frame(seq=seq))
    source.start()
    try:
        bootstrap._source = source
        with_source = FakeScriptOp()
        hands_chop.write_channels(with_source)
    finally:
        source.stop()
        bootstrap._source = None

    assert without.names == with_source.names
    assert len(without.names) == len(with_source.names) == N_CHANNELS


def test_no_source_publishes_the_sentinel_age_not_a_lie() -> None:
    """With nothing bootstrapped, age must not read as fresh.

    AGE_NO_SOURCE_MS is negative, which a real age can never be (source.py
    clamps at zero), so downstream can distinguish "nothing is wired up" from
    "the camera has gone quiet" - one is a project mistake, the other is a dead
    engine, and they need different responses.
    """
    script_op = FakeScriptOp()
    hands_chop.write_channels(script_op)
    published = script_op.as_dict()
    assert published["age_ms"] == hands_chop.AGE_NO_SOURCE_MS < 0.0
    assert published["n_hands"] == 0.0
    assert published["seq"] == 0.0
    assert published["h0_found"] == 0.0


def test_a_cook_survives_a_source_that_raises() -> None:
    """An exception inside a cook shows up as a broken operator in the project.

    The useful behaviour when the source misbehaves is a full set of zeros with a
    telling age, so the channel references survive and the failure is visible.
    """
    class ExplodingSource:
        def latest(self) -> LandmarkFrame:
            raise RuntimeError("boom")

        def age_ms(self) -> float:
            raise RuntimeError("boom")

    bootstrap._source = ExplodingSource()
    try:
        script_op = FakeScriptOp()
        hands_chop.write_channels(script_op)      # must not raise
    finally:
        bootstrap._source = None
    assert len(script_op.names) == N_CHANNELS
    assert script_op.as_dict()["age_ms"] == hands_chop.AGE_NO_SOURCE_MS


def test_a_real_frame_reaches_the_channels() -> None:
    """End to end through the source, with a fake camera."""
    source = FakeSource(lambda seq: _frame(seq=seq), interval_s=0.002)
    source.start()
    try:
        deadline_reads = 0
        while source.box.n_published < 3 and deadline_reads < 500:
            deadline_reads += 1
        bootstrap._source = source
        script_op = FakeScriptOp()
        hands_chop.write_channels(script_op)
    finally:
        source.stop()
        bootstrap._source = None

    published = script_op.as_dict()
    assert published["seq"] > 0.0
    assert published["n_hands"] == 1.0
    assert published["h0_found"] == 1.0
    assert published["h0_lm00_x"] == 0.5
    assert published["age_ms"] >= 0.0


def test_cook_writes_no_channels_twice() -> None:
    """clear() then append, every cook: no duplicates, no growth.

    FakeScriptOp rejects a duplicate name, so a missing clear() would fail here
    rather than producing 274 channels on the second cook.
    """
    script_op = FakeScriptOp()
    for _ in range(3):
        hands_chop.write_channels(script_op)
    assert len(script_op.names) == N_CHANNELS
    assert script_op.n_clears == 3


# ---------------------------------------------------------------------------
# The cook level - the detail that would have made this whole CHOP look frozen
# ---------------------------------------------------------------------------
def test_cook_level_is_when_used_not_the_default() -> None:
    """With TD's default AUTOMATIC ("inputs changed and output being used") this
    CHOP would cook once and then serve a frozen frame forever, because it has
    no inputs - the camera delivers on a background thread.

    TD injects CookLevel into builtins, so the test injects a stand-in the same
    way rather than mocking the function under test.
    """
    class FakeCookLevel:
        AUTOMATIC = "AUTOMATIC"
        ON_CHANGE = "ON_CHANGE"
        WHEN_USED = "WHEN_USED"
        ALWAYS = "ALWAYS"

    import builtins
    builtins.CookLevel = FakeCookLevel      # type: ignore[attr-defined]
    try:
        assert hands_chop.onGetCookLevel(FakeScriptOp()) == "WHEN_USED"
    finally:
        del builtins.CookLevel              # type: ignore[attr-defined]


def test_cook_level_outside_touchdesigner_returns_none() -> None:
    """No CookLevel means we are not in TD. Returning None lets TD's default
    apply rather than raising inside a callback."""
    assert hands_chop.onGetCookLevel(FakeScriptOp()) is None


def test_oncook_is_the_documented_entry_point() -> None:
    """TD calls these by name, so the names and arity are part of the contract."""
    import inspect
    for name in ("onCook", "onGetCookLevel"):
        function = getattr(hands_chop, name)
        assert callable(function), name
        assert list(inspect.signature(function).parameters) == ["scriptOp"], name

    script_op = FakeScriptOp()
    hands_chop.onCook(script_op)
    assert len(script_op.names) == N_CHANNELS


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------
def test_bootstrap_appends_never_inserts() -> None:
    """MEASURED (DESIGN.md 2.5): appending let numpy resolve to TD's own copy.

    Inserting at position 0 would shadow TD's numpy with the venv's, which is an
    ABI mismatch inside TD's process - a crash, not an ImportError.
    """
    import sys
    before = list(sys.path)
    try:
        ok = bootstrap.add_venv_to_path(bootstrap.DEFAULT_VENV_SITE_PACKAGES)
        if not ok:
            pytest.skip("dev venv not present")
        assert sys.path[0] == before[0], "something was inserted at the front"
        assert bootstrap.DEFAULT_VENV_SITE_PACKAGES in sys.path
    finally:
        sys.path[:] = before


def test_bootstrap_stop_is_safe_before_any_start() -> None:
    bootstrap.stop()
    bootstrap.stop()
    assert bootstrap.source() is None


def test_bootstrap_start_reports_a_missing_venv_usefully() -> None:
    """The most likely first-run failure, and the error has to name the path."""
    with pytest.raises(RuntimeError, match="no site-packages"):
        bootstrap.start(site_packages="/nonexistent/site-packages")


def test_bootstrap_stop_swallows_a_failing_source_but_says_so(
        capsys: pytest.CaptureFixture[str]) -> None:
    """stop() runs on TD's unload path and must never raise there - but it must
    not go quiet about a session it could not stop either."""
    class StubbornSource:
        def stop(self) -> None:
            raise RuntimeError("will not stop")

    bootstrap._source = StubbornSource()
    bootstrap.stop()                        # must not raise
    assert bootstrap.source() is None
    assert "error stopping source" in capsys.readouterr().out


def test_status_never_raises() -> None:
    """It is typed into the Textport precisely when things are half-built."""
    assert "not started" in bootstrap.status()

    class BrokenSource:
        running = True

        def latest(self) -> LandmarkFrame:
            raise RuntimeError("boom")

        def age_ms(self) -> float:
            return 0.0

    bootstrap._source = BrokenSource()
    try:
        assert "unavailable" in bootstrap.status()
    finally:
        bootstrap._source = None


def test_the_box_and_the_chop_agree_on_an_empty_start() -> None:
    """A freshly-made box and a sourceless cook publish the same shape."""
    frame = LatestFrameBox().latest()
    values = hands_chop.channel_values(frame, hands_chop.AGE_NO_SOURCE_MS, 0)
    assert len(values) == N_CHANNELS
    assert values[0] == 0.0 and values[1] == 0.0
