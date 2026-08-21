"""Heading from velocity. Small surface, but every sign convention in it can be
wrong in a way that looks plausible.

The convention under test is the one docs/ATTRIBUTES.md states for every angle in
the system: degrees, 0 = +x, counter-clockwise, y UP. A y-flip here would put every
swipe in the wrong vertical direction and look entirely reasonable on screen, which
is the same class of failure DESIGN.md 7 is about.
"""

from __future__ import annotations

import pytest

from visionhands.motion import MotionParams, directions


def _values(vel_x: float, vel_y: float, speed: float | None = None,
            hand: int = 0) -> dict[str, float]:
    if speed is None:
        speed = (vel_x ** 2 + vel_y ** 2) ** 0.5
    return {"h%d_vel_x" % hand: vel_x, "h%d_vel_y" % hand: vel_y,
            "h%d_speed" % hand: speed}


# ---------------------------------------------------------------------------
# The four cardinal directions, which pin the convention
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("vel_x", "vel_y", "degrees"), [
    (1.0, 0.0, 0.0),        # right
    (0.0, 1.0, 90.0),       # UP, because y is up (DESIGN.md 7)
    (-1.0, 0.0, 180.0),     # left
    (0.0, -1.0, -90.0),     # down
])
def test_the_cardinal_directions(vel_x: float, vel_y: float,
                                 degrees: float) -> None:
    out = directions(_values(vel_x, vel_y))
    assert out["h0_dir"] == pytest.approx(degrees)


def test_a_diagonal_is_forty_five_degrees() -> None:
    out = directions(_values(1.0, 1.0))
    assert out["h0_dir"] == pytest.approx(45.0)


def test_moving_up_is_POSITIVE_not_negative() -> None:
    """Stated separately because it is the one that would look fine while wrong.

    Vision, and everything downstream of it here, uses y UP (DESIGN.md 7). A y-flip
    would send every upward swipe downward, and nothing about the number 90 versus
    -90 looks suspicious in isolation.
    """
    assert directions(_values(0.0, 0.5))["h0_dir"] > 0.0
    assert directions(_values(0.0, -0.5))["h0_dir"] < 0.0


# ---------------------------------------------------------------------------
# The unit vector agrees with the angle
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("vel_x", "vel_y"), [
    (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0), (3.0, 4.0), (-2.0, 0.5),
])
def test_dir_x_and_dir_y_are_the_normalised_velocity(vel_x: float,
                                                     vel_y: float) -> None:
    """The pair must be the velocity's own direction, not merely a unit vector."""
    out = directions(_values(vel_x, vel_y))
    magnitude = (vel_x ** 2 + vel_y ** 2) ** 0.5
    assert out["h0_dir_x"] == pytest.approx(vel_x / magnitude, abs=1e-9)
    assert out["h0_dir_y"] == pytest.approx(vel_y / magnitude, abs=1e-9)


@pytest.mark.parametrize(("vel_x", "vel_y"), [(1.0, 0.0), (3.0, 4.0), (-2.0, 0.5)])
def test_the_unit_vector_really_is_unit_length(vel_x: float, vel_y: float) -> None:
    out = directions(_values(vel_x, vel_y))
    length = (out["h0_dir_x"] ** 2 + out["h0_dir_y"] ** 2) ** 0.5
    assert length == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# The speed floor
# ---------------------------------------------------------------------------
def test_moving_is_gated_on_the_speed_floor() -> None:
    params = MotionParams(speedfloor=0.5)
    assert directions(_values(1.0, 0.0), params)["h0_moving"] == 1.0
    assert directions(_values(0.1, 0.0), params)["h0_moving"] == 0.0


def test_moving_reads_the_published_speed_not_the_components() -> None:
    """One definition of speed in the system, not two.

    `speed` downstream is an AVERAGED magnitude (DESIGN.md 2.11), so recomputing a
    hypotenuse here could disagree with the channel a consumer sees. Passing a
    speed that contradicts the components proves which one is used.
    """
    params = MotionParams(speedfloor=0.5)
    # Big components, but the published speed says slow: slow wins.
    assert directions(_values(10.0, 10.0, speed=0.01), params)["h0_moving"] == 0.0
    # And the reverse.
    assert directions(_values(0.001, 0.0, speed=9.0), params)["h0_moving"] == 1.0


def test_a_hand_at_rest_gives_zero_rather_than_a_nan() -> None:
    """atan2(0, 0) is 0, and `moving` is what says not to believe it.

    A NaN here would spread silently through TouchDesigner maths, which is the
    trap docs/ATTRIBUTES.md opens with.
    """
    out = directions(_values(0.0, 0.0))
    assert out["h0_dir"] == 0.0
    assert out["h0_moving"] == 0.0
    for name, value in out.items():
        assert value == value, name          # not NaN


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
def test_both_hands_are_computed_independently() -> None:
    values = {**_values(1.0, 0.0, hand=0), **_values(0.0, -1.0, hand=1)}
    out = directions(values)
    assert out["h0_dir"] == pytest.approx(0.0)
    assert out["h1_dir"] == pytest.approx(-90.0)


def test_missing_channels_read_as_zero_rather_than_raising() -> None:
    """A caller publishing a subset still works - same contract as derive()."""
    out = directions({})
    assert out["h0_dir"] == 0.0
    assert out["h0_moving"] == 0.0


def test_the_channel_set_is_the_same_whatever_the_input() -> None:
    """A channel list that varies breaks every downstream reference silently."""
    at_rest = set(directions({}))
    moving = set(directions(_values(1.0, 1.0)))
    assert at_rest == moving
    assert at_rest == {"h%d_%s" % (hand, name)
                       for hand in (0, 1)
                       for name in ("dir", "dir_x", "dir_y", "moving")}
