"""The pin solve: parsing, the fit, the refusals, and the residual that lies.

WHY THIS FILE IS THOROUGH. Every metric number that reaches a user comes out of
`pins.solve`, and the failure mode is not a crash - it is a confident wrong distance.
So the tests are built around a frame whose TRUE affine relationship is known, and they
check that the solve recovers it exactly, refuses when it should, and says why.

No model, no camera, no frameworks: `pins.py` is arithmetic over a numpy array, which
is precisely why it is a separate module from `depth.py`.

Ref: visionhands/pins.py, docs/DEPTH.md, design/DESIGN.md 2.22.
"""

from __future__ import annotations

import numpy
import numpy.typing
import pytest

from visionhands.pins import (
    DEFAULT_PINS,
    MIN_SPREAD_FRAC,
    Pin,
    format_pins,
    metres,
    parse_pins,
    sample,
    solve,
    unpack,
)

# The affine relationship every synthetic frame below is built to satisfy:
#     1/Z = ALPHA * d + BETA
# Chosen so that a 1.1 m and a 2.5 m pin land at readings that are far apart, which is
# the condition the solve needs and the thing a real room can fail to provide.
ALPHA = 0.8
BETA = 0.05


def _frame(pins: tuple[Pin, ...], width: int = 160, height: int = 100,
           noise: float = 0.0) -> numpy.typing.NDArray[numpy.float32]:
    """A frame where each pin sits on the reading its true distance implies.

    The two corner pixels give the frame a real min/max: the conditioning test is
    expressed as a fraction of the frame's own range, so a frame that is flat except
    at the pins would make every spread look enormous.
    """
    depth = numpy.zeros((height, width), dtype=numpy.float32)
    depth[0, 0] = 0.0
    depth[0, 1] = 1.0
    generator = numpy.random.default_rng(7)
    for pin in pins:
        cx = min(max(int(pin.x * (width - 1)), 4), width - 5)
        cy = min(max(int(pin.y * (height - 1)), 4), height - 5)
        value = (1.0 / pin.metres - BETA) / ALPHA
        patch = numpy.full((9, 9), value, dtype=numpy.float32)
        if noise:
            patch += generator.normal(0.0, noise, patch.shape).astype(numpy.float32)
        depth[cy - 4:cy + 5, cx - 4:cx + 5] = patch
    return depth


# -- parsing -------------------------------------------------------------


def test_a_pin_list_round_trips() -> None:
    text = "0.06,0.3,2.5 0.94,0.3,2.4 0.5,0.96,1.1"
    assert format_pins(parse_pins(text)) == text


def test_semicolons_and_extra_spaces_are_accepted() -> None:
    """It arrives from a TouchDesigner text field, where somebody will type either."""
    assert parse_pins("0.1,0.2,1.5; 0.8,0.9,3.0") == (Pin(0.1, 0.2, 1.5),
                                                      Pin(0.8, 0.9, 3.0))
    assert parse_pins("  0.1,0.2,1.5   ") == (Pin(0.1, 0.2, 1.5),)


def test_an_empty_string_is_no_pins_and_not_an_error() -> None:
    """"No metric claim" is a legitimate configuration and the shipping default."""
    assert parse_pins("") == ()
    assert parse_pins("   ") == ()


@pytest.mark.parametrize(("text", "complaint"), [
    ("0.1,0.2", "a pin is X,Y,METRES"),
    ("0.1,0.2,1.0,4", "a pin is X,Y,METRES"),
    ("a,0.2,1.0", "not a number"),
    ("1.4,0.2,1.0", "normalised"),
    ("0.1,-0.2,1.0", "normalised"),
    ("0.1,0.2,0", "must be positive"),
    ("0.1,0.2,-3", "must be positive"),
])
def test_a_bad_pin_is_refused_at_parse_time(text: str, complaint: str) -> None:
    """AT PARSE TIME, which is startup. A typo in a pin is not a crash - it is a wrong
    number on screen all session - so it has to fail here or not at all."""
    with pytest.raises(ValueError, match=complaint):
        parse_pins(text)


def test_the_defaults_go_through_the_same_validation() -> None:
    """Parsed rather than written as literals, so the default is not the one pin list
    nobody ever checked."""
    assert len(DEFAULT_PINS) == 3
    assert all(isinstance(pin, Pin) for pin in DEFAULT_PINS)
    assert format_pins(parse_pins(format_pins(DEFAULT_PINS))) == format_pins(DEFAULT_PINS)


# -- the fit -------------------------------------------------------------


def test_the_solve_recovers_a_known_affine_relationship_exactly() -> None:
    """The whole point, and the reason the synthetic frame exists: if the maths is
    right, alpha and beta come back to floating-point precision."""
    pins = parse_pins("0.2,0.2,1.1 0.8,0.2,2.5 0.5,0.9,1.6")
    fit = solve(_frame(pins), pins)
    assert fit.ok
    assert fit.alpha == pytest.approx(ALPHA, abs=1e-6)
    assert fit.beta == pytest.approx(BETA, abs=1e-6)
    assert fit.used == (0, 1, 2)
    assert fit.dropped is None
    assert fit.worst_residual_m == pytest.approx(0.0, abs=1e-6)


def test_metres_reads_the_true_distance_back_at_each_pin() -> None:
    pins = parse_pins("0.2,0.2,1.1 0.8,0.2,2.5 0.5,0.9,1.6")
    depth = _frame(pins)
    fit = solve(depth, pins)
    metric = metres(depth, fit)
    assert metric is not None
    assert metric.dtype == numpy.float32
    for pin in pins:
        cx = min(max(int(pin.x * 159), 4), 155)
        cy = min(max(int(pin.y * 99), 4), 95)
        assert metric[cy, cx] == pytest.approx(pin.metres, abs=0.01)


def test_two_pins_are_enough_for_a_fit() -> None:
    pins = parse_pins("0.2,0.2,1.1 0.8,0.8,2.5")
    fit = solve(_frame(pins), pins)
    assert fit.ok
    assert fit.alpha == pytest.approx(ALPHA, abs=1e-6)


def test_a_noisy_frame_still_fits_within_centimetres() -> None:
    """The patch median is what makes this work - the map is a ViT output upsampled to
    frame size and one pixel of it twitches enough to move the fit."""
    pins = parse_pins("0.2,0.2,1.1 0.8,0.2,2.5 0.5,0.9,1.6")
    fit = solve(_frame(pins, noise=0.01), pins)
    assert fit.ok
    assert fit.worst_residual_m < 0.10


# -- the residual that lies ----------------------------------------------


def test_with_two_pins_the_residual_is_zero_and_says_it_is_not_a_check() -> None:
    """THE trap this module guards. Two points always fit a line exactly, so the
    residual is identically 0.000 and reads as a perfect fit while checking nothing.
    A `0.000 m` on a panel is the most convincing wrong number here."""
    pins = parse_pins("0.2,0.2,1.1 0.8,0.8,2.5")
    fit = solve(_frame(pins), pins)
    assert fit.ok
    assert fit.worst_residual_m == pytest.approx(0.0, abs=1e-9)
    assert fit.residual_is_a_check is False
    assert "residual is NOT a check" in fit.note


def test_with_three_pins_the_residual_is_a_check() -> None:
    pins = parse_pins("0.2,0.2,1.1 0.8,0.2,2.5 0.5,0.9,1.6")
    fit = solve(_frame(pins), pins)
    assert fit.residual_is_a_check is True
    assert "residual is NOT a check" not in fit.note


def test_dropping_a_pin_can_take_the_residual_back_below_three() -> None:
    """And when it does, the flag has to follow. A three-pin setup that drops one is a
    two-pin fit, with a two-pin fit's meaningless residual - which is exactly the
    situation the fixture clip produces every frame."""
    # The third pin is on the far wall at 5 m, which is what makes an occlusion
    # obvious: read at 0.99 it implies 1.19 m, which is 3.8 m of disagreement. A pin
    # at 1.6 m occluded the same way is only 0.4 m out and is CORRECTLY not dropped -
    # the threshold is 0.5 m by default and it is doing its job.
    pins = parse_pins("0.2,0.2,1.1 0.8,0.2,2.5 0.5,0.9,5.0")
    depth = _frame(pins)
    cx, cy = int(0.5 * 159), int(0.9 * 99)
    depth[cy - 4:cy + 5, cx - 4:cx + 5] = 0.99      # a chest at arm's length
    fit = solve(depth, pins)
    assert fit.ok
    assert fit.dropped == 2
    assert fit.used == (0, 1)
    assert fit.residual_is_a_check is False


# -- occlusion -----------------------------------------------------------


def test_a_pin_somebody_walks_in_front_of_is_dropped() -> None:
    """The cheapest defence that needs no occlusion detector: with three or more, throw
    out the worst offender."""
    pins = parse_pins("0.2,0.2,1.1 0.8,0.2,2.5 0.5,0.9,5.0 0.5,0.5,2.0")
    depth = _frame(pins)
    cx, cy = int(0.5 * 159), int(0.9 * 99)
    depth[cy - 4:cy + 5, cx - 4:cx + 5] = 0.99      # a chest at arm's length
    fit = solve(depth, pins)
    assert fit.ok
    assert fit.dropped == 2
    assert 2 not in fit.used
    assert "dropped #3" in fit.note


def test_only_one_pin_is_ever_dropped() -> None:
    """One, not "as many as it takes". Dropping until the residual looks good would fit
    a line through whichever two pins happened to agree, which is a confident answer
    from no evidence."""
    pins = parse_pins("0.2,0.2,1.1 0.8,0.2,2.5 0.5,0.9,5.0 0.3,0.7,6.0")
    depth = _frame(pins)
    for x, y in ((0.5, 0.9), (0.3, 0.7)):
        cx, cy = int(x * 159), int(y * 99)
        depth[cy - 4:cy + 5, cx - 4:cx + 5] = 0.97
    fit = solve(depth, pins)
    assert fit.dropped is not None
    assert len(fit.used) == 3           # one dropped, not two


def test_two_pins_are_never_dropped_however_badly_they_disagree() -> None:
    """With two there is nothing to disagree WITH. Dropping one would leave a single
    pin and no fit at all, which is worse than a fit somebody can see the residual of -
    except that with two there is no residual either, which is what the flag is for."""
    pins = parse_pins("0.2,0.2,1.1 0.8,0.8,2.5")
    depth = _frame(pins)
    depth[75:85, 120:130] = 0.99
    fit = solve(depth, pins)
    assert fit.dropped is None


# -- refusals ------------------------------------------------------------


def test_fewer_than_two_pins_is_refused_with_a_count() -> None:
    for pins in ((), parse_pins("0.5,0.5,1.0")):
        fit = solve(_frame(()), pins)
        assert not fit.ok
        assert "needs 2 pins" in fit.note
        assert metres(_frame(()), fit) is None


def test_pins_too_close_in_depth_are_refused_with_the_percentage() -> None:
    """Conditioning is set by the SPREAD of the readings, not by how many there are.
    The note carries the number so somebody can see how far off they are."""
    pins = parse_pins("0.2,0.2,2.00 0.8,0.8,2.01")
    fit = solve(_frame(pins), pins)
    assert not fit.ok
    assert "too close in depth" in fit.note
    assert "%.0f%%" % (100.0 * MIN_SPREAD_FRAC) in fit.note


def test_an_empty_frame_is_refused_before_anything_samples_it() -> None:
    """Not just "does not crash". A 0x0 array makes the patch median nan, and a nan
    reaches polyfit as an illegal LAPACK argument printed straight to stderr - a line
    of Fortran diagnostics in the sidecar's log rather than an exception. So it has to
    be refused BEFORE sampling."""
    fit = solve(numpy.zeros((0, 0), dtype=numpy.float32), DEFAULT_PINS)
    assert not fit.ok
    assert fit.note == "the frame is empty"


def test_a_flat_frame_is_refused_rather_than_fitted() -> None:
    """Every pin reads the same value, so there is no slope to find. It has to be a
    refusal and not a fit through noise."""
    fit = solve(numpy.full((100, 160), 0.5, dtype=numpy.float32), DEFAULT_PINS)
    assert not fit.ok


def test_metres_is_clamped_rather_than_allowed_to_explode() -> None:
    """`alpha*d + beta` can pass through zero for `d` outside the pinned range, and one
    infinity in the map poisons everything downstream that touches it."""
    pins = parse_pins("0.2,0.2,1.1 0.8,0.2,2.5 0.5,0.9,1.6")
    depth = _frame(pins)
    # SOLVED FIRST, then poisoned: a -50 in the frame blows up its range, which the
    # conditioning test would then reject - and this test is about the clamp, not
    # about the refusal.
    fit = solve(depth, pins)
    depth[50, 50] = -50.0               # far outside anything the pins covered
    metric = metres(depth, fit)
    assert metric is not None
    assert numpy.all(numpy.isfinite(metric))
    assert metric.max() <= 60.0 + 1e-3
    assert metric.min() >= 0.05 - 1e-3


# -- sampling ------------------------------------------------------------


def test_a_pin_at_the_very_edge_is_pulled_inside_the_patch() -> None:
    """A pin at 0,0 would sample a patch that runs off the array. Clamped rather than
    refused: the edge is a sensible place to put a pin precisely because nobody stands
    there."""
    depth = numpy.arange(100 * 160, dtype=numpy.float32).reshape(100, 160)
    readings = sample(depth, parse_pins("0,0,1.0 1,1,2.0"))
    assert len(readings) == 2
    assert all(numpy.isfinite(readings))


def test_sampling_takes_a_median_and_not_the_centre_pixel() -> None:
    """One pixel of an upsampled ViT output twitches enough to move the fit. A single
    wild pixel in the middle of the patch must not survive."""
    depth = numpy.full((100, 160), 0.5, dtype=numpy.float32)
    cx, cy = int(0.5 * 159), int(0.5 * 99)
    depth[cy, cx] = 99.0
    assert sample(depth, parse_pins("0.5,0.5,1.0")) == [pytest.approx(0.5)]


# -- the aux block -------------------------------------------------------


def test_the_fit_round_trips_through_the_aux_block() -> None:
    """It travels WITH the pixels it describes - a fit paired with a neighbouring
    frame's map is a plausible wrong metric depth."""
    pins = parse_pins("0.2,0.2,1.1 0.8,0.2,2.5 0.5,0.9,1.6")
    fit = solve(_frame(pins), pins)
    packed = fit.pack()
    assert len(packed) <= 32            # must fit maskbuf.AUX_BYTES
    got = unpack(packed)
    assert got["alpha"] == pytest.approx(fit.alpha, rel=1e-6)
    assert got["beta"] == pytest.approx(fit.beta, rel=1e-6)
    assert got["used"] == 3.0
    assert got["dropped"] == 0.0
    assert got["residual_is_a_check"] == 1.0


def test_a_refused_fit_packs_as_zeros_and_unpacks_as_nothing() -> None:
    """An all-zero aux block reads as "no fit", not as `alpha = 0` - which would be a
    fit that puts everything at the same distance."""
    fit = solve(_frame(()), ())
    assert unpack(fit.pack()) == {}
    assert unpack(b"") == {}
    assert unpack(b"\x00" * 32) == {}


def test_the_dropped_index_in_the_aux_block_is_one_based() -> None:
    """Because 0 has to mean "none". A zero-based index would make pin #1 being dropped
    indistinguishable from nothing being dropped."""
    pins = parse_pins("0.2,0.2,5.0 0.8,0.2,2.5 0.5,0.9,1.1 0.3,0.7,2.0")
    depth = _frame(pins)
    cx, cy = int(0.2 * 159), int(0.2 * 99)
    depth[cy - 4:cy + 5, cx - 4:cx + 5] = 0.99
    fit = solve(depth, pins)
    assert fit.dropped == 0
    assert unpack(fit.pack())["dropped"] == 1.0
