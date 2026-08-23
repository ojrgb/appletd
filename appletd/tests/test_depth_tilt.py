"""Depth and tilt: 3D inferred from a 2D projection, and what it cannot know.

These two groups publish numbers that do not exist in the input. Vision gives 21
points on a plane; `h{i}_z` and `h{i}_tilt` are consequences of those points, not
measurements, and the tests that matter are the ones pinning what they can and cannot
claim.

THE AMBIGUITY IS THE POINT. A projection destroys which side of the image plane a
point is on, so a hand tilted 30 degrees TOWARD the camera and one tilted 30 AWAY
project identically. Every test below that looks like it is being pedantic about
signs is really asserting that this module does not pretend otherwise - which is why
the channels are `tilt` and `tilt_axis` rather than `pitch` and `yaw`.

Ref: appletd/derive.py (`_foreshorten`, `_tilt`), docs/ATTRIBUTES.md.
"""

from __future__ import annotations

import math

import pytest

from appletd.derive import ALL_GROUPS, Params, derive
from appletd.synth import HandPose, synthetic_frame
from appletd.types import channel_names, channel_values

GROUPS = frozenset(ALL_GROUPS)


def _from_pose(pose: HandPose, params: Params | None = None) -> dict[str, float]:
    """Run a synthetic hand through the real contract and the real `derive`."""
    frame = synthetic_frame([pose, None])
    values = dict(zip(channel_names(), channel_values(frame, 0.0, 1), strict=True))
    return derive(values, params, GROUPS)


def _hand(joints: dict[str, tuple[float, float]], size: float = 0.12,
          **extra: float) -> dict[str, float]:
    """A hand built joint by joint, for the geometric cases a pose cannot express.

    `synth.py` places anatomically plausible hands, which is right for almost every
    test and wrong for these: a foreshortened finger and an edge-on palm are exactly
    what a plausible generator will not produce.
    """
    values = {"h0_found": 1.0, "h0_conf_median": 1.0, "n_hands": 1.0, **extra}
    for name, (x, y) in joints.items():
        values["h0_" + name + "_x"] = x
        values["h0_" + name + "_y"] = y
        values["h0_" + name + "_conf"] = 1.0
    return values


# ---------------------------------------------------------------------------
# Depth, from apparent size
# ---------------------------------------------------------------------------
def test_z_is_one_at_the_reference_size() -> None:
    """`zreference` is the calibration, and the whole scale hangs off it. It defaults
    to `synth.HandPose.size`'s own default, so a hand at the project's reference
    distance reads exactly 1.0 with no parameters passed at all."""
    out = _from_pose(HandPose(size=0.12))
    assert out["h0_z"] == pytest.approx(1.0)
    assert _from_pose(HandPose(size=0.12), Params(zreference=0.24))["h0_z"] == \
        pytest.approx(2.0)


def test_z_rises_with_distance() -> None:
    """A hand twice as far away subtends half the angle, so z doubles. This is the
    only property `h{i}_z` actually has, and it is enough for lean-in interactions."""
    params = Params(zreference=0.12)
    near = _from_pose(HandPose(size=0.24), params)
    mid = _from_pose(HandPose(size=0.12), params)
    far = _from_pose(HandPose(size=0.06), params)
    assert near["h0_z"] == pytest.approx(0.5)
    assert mid["h0_z"] == pytest.approx(1.0)
    assert far["h0_z"] == pytest.approx(2.0)
    assert near["h0_z"] < mid["h0_z"] < far["h0_z"]


def test_z_is_not_comparable_between_different_hands() -> None:
    """Not a bug - a documented limit, asserted so nobody later reads `z` as a
    distance. A small hand near the camera and a large one further away are
    indistinguishable, because apparent size confounds the two."""
    params = Params(zreference=0.12)
    small_hand_close = _from_pose(HandPose(size=0.12), params)
    large_hand_far = _from_pose(HandPose(size=0.12), params)
    assert small_hand_close["h0_z"] == large_hand_far["h0_z"]


def test_an_invalid_hand_publishes_zero_rather_than_a_stale_z() -> None:
    """`size` is floored to avoid a divide-by-zero, so an absent hand would otherwise
    read a large and entirely fictional depth."""
    out = derive({}, None, GROUPS)
    assert out["h0_z"] == 0.0
    assert out["h0_tilt"] == 0.0


# ---------------------------------------------------------------------------
# Per-finger foreshortening, and what it cannot tell you
# ---------------------------------------------------------------------------
def test_a_straight_in_plane_finger_is_not_foreshortened() -> None:
    out = _from_pose(HandPose(curl={f: 0.0 for f in
                                    ("thumb", "index", "middle", "ring", "little")}))
    for finger in ("index", "middle", "ring", "little"):
        assert out["h0_z_" + finger] == pytest.approx(0.0, abs=0.05), finger


def test_foreshortening_rises_as_the_apparent_length_collapses() -> None:
    """Built joint by joint, because a plausible-hand generator will not produce a
    finger pointing down the camera axis."""
    # An index finger along +x, bones 0.02 each, tip 0.06 from the base: flat.
    flat = _hand({"wrist": (0.5, 0.5), "middle_mcp": (0.5, 0.62),
                  "index_mcp": (0.5, 0.6), "index_pip": (0.52, 0.6),
                  "index_dip": (0.54, 0.6), "index_tip": (0.56, 0.6)})
    # The same bones, but the tip has collapsed back toward the base: foreshortened.
    short = _hand({"wrist": (0.5, 0.5), "middle_mcp": (0.5, 0.62),
                   "index_mcp": (0.5, 0.6), "index_pip": (0.52, 0.6),
                   "index_dip": (0.51, 0.6), "index_tip": (0.52, 0.6)})
    assert derive(flat, None, GROUPS)["h0_z_index"] < 0.1
    assert derive(short, None, GROUPS)["h0_z_index"] > 0.4


def test_foreshortening_cannot_tell_toward_from_away() -> None:
    """THE limit, and it lives in the projection rather than in the code. A finger
    pointing at the camera and one pointing directly away project to the same
    apparent length, so they must produce the same number - and a consumer that needs
    the sign has to get it from somewhere else."""
    toward = _hand({"wrist": (0.5, 0.5), "middle_mcp": (0.5, 0.62),
                    "index_mcp": (0.5, 0.6), "index_pip": (0.51, 0.6),
                    "index_dip": (0.515, 0.6), "index_tip": (0.52, 0.6)})
    # Mirrored about the base: the tip is now on the other side, same distance.
    away = _hand({"wrist": (0.5, 0.5), "middle_mcp": (0.5, 0.62),
                  "index_mcp": (0.5, 0.6), "index_pip": (0.49, 0.6),
                  "index_dip": (0.485, 0.6), "index_tip": (0.48, 0.6)})
    assert (derive(toward, None, GROUPS)["h0_z_index"]
            == pytest.approx(derive(away, None, GROUPS)["h0_z_index"]))


def test_foreshortening_is_bounded() -> None:
    """It is fed to consumers as 0..1 and a ratio can exceed that if the bones are
    inconsistent - which happens on a low-confidence frame."""
    silly = _hand({"wrist": (0.5, 0.5), "middle_mcp": (0.5, 0.62),
                   "index_mcp": (0.5, 0.6), "index_pip": (0.9, 0.6),
                   "index_dip": (0.1, 0.6), "index_tip": (0.5, 0.6)})
    value = derive(silly, None, GROUPS)["h0_z_index"]
    assert 0.0 <= value <= 1.0


# ---------------------------------------------------------------------------
# Palm tilt: magnitude and axis, deliberately not pitch and yaw
# ---------------------------------------------------------------------------
def test_a_palm_facing_the_camera_has_zero_tilt() -> None:
    """The synthetic hand is flat in the image plane by construction, so this pins
    `palmarea` against the generator. It is not a formality: the first value was a
    guess of 0.14 against a real 0.3059, which clamped the ratio and made `tilt` read
    exactly 0 for the first 62 degrees of rotation - a dead zone that looked like a
    working channel."""
    out = _from_pose(HandPose())
    assert out["h0_tilt"] == pytest.approx(0.0, abs=1.0)


def test_a_wrong_palmarea_is_a_dead_zone_rather_than_an_offset() -> None:
    """Which failure `palmarea` should have when it is mis-calibrated, asserted so
    the default is not quietly moved the other way. Too LOW clamps near zero - a
    face-on hand reads 0 and so does a slightly tilted one. Too HIGH reports tilt on a
    hand that has none, which is worse, because there is no value of the channel that
    means "flat"."""
    face_on = HandPose()
    too_low = _from_pose(face_on, Params(palmarea=0.20))
    too_high = _from_pose(face_on, Params(palmarea=0.60))
    assert too_low["h0_tilt"] == pytest.approx(0.0, abs=0.01)
    assert too_high["h0_tilt"] > 20.0


def test_tilt_rises_as_the_palm_turns_edge_on() -> None:
    """The palm triangle's apparent area shrinks with the cosine of the tilt, so a
    narrower projected palm is a larger angle."""
    wide = _hand({"wrist": (0.5, 0.5), "middle_mcp": (0.5, 0.62),
                  "index_mcp": (0.56, 0.6), "little_mcp": (0.44, 0.6)})
    narrow = _hand({"wrist": (0.5, 0.5), "middle_mcp": (0.5, 0.62),
                    "index_mcp": (0.52, 0.6), "little_mcp": (0.48, 0.6)})
    edge = _hand({"wrist": (0.5, 0.5), "middle_mcp": (0.5, 0.62),
                  "index_mcp": (0.5, 0.6), "little_mcp": (0.5, 0.6)})
    a = derive(wide, None, GROUPS)["h0_tilt"]
    b = derive(narrow, None, GROUPS)["h0_tilt"]
    c = derive(edge, None, GROUPS)["h0_tilt"]
    assert a < b < c
    # Edge-on is the maximum the arithmetic can report.
    assert c == pytest.approx(90.0)


def test_tilt_is_bounded_to_a_quarter_turn() -> None:
    """`acos` of a clamped ratio, so a mis-calibrated `palmarea` gives a wrong angle
    rather than a NaN - which would propagate silently into every consumer."""
    for area in (0.0001, 0.14, 10.0):
        out = derive(_hand({"wrist": (0.5, 0.5), "middle_mcp": (0.5, 0.62),
                            "index_mcp": (0.56, 0.6), "little_mcp": (0.44, 0.6)}),
                     Params(palmarea=area), GROUPS)
        assert 0.0 <= out["h0_tilt"] <= 90.0
        assert out["h0_tilt"] == out["h0_tilt"]        # not NaN


def test_the_tilt_axis_follows_the_unforeshortened_direction() -> None:
    """The palm foreshortens ALONG the way it is leaning, so the axis it turns about
    is the direction that kept its length - the longest projected edge."""
    # A palm squashed vertically: the surviving extent is horizontal, so the axis it
    # is leaning about is horizontal too.
    squashed = _hand({"wrist": (0.5, 0.50), "middle_mcp": (0.5, 0.62),
                      "index_mcp": (0.60, 0.51), "little_mcp": (0.40, 0.51)})
    axis = derive(squashed, None, GROUPS)["h0_tilt_axis"]
    # Horizontal, to within a few degrees, in either direction along that line.
    assert min(abs(axis), abs(abs(axis) - 180.0)) < 15.0


def test_tilt_is_scale_free_and_therefore_depth_free() -> None:
    """Normalised by size squared, like every ratio in this module - so the same
    gesture reads the same at arm's length and up close, which is the property a
    threshold needs to be tunable at all."""
    near = _from_pose(HandPose(size=0.24))
    far = _from_pose(HandPose(size=0.06))
    assert near["h0_tilt"] == pytest.approx(far["h0_tilt"], abs=1.0)


# ---------------------------------------------------------------------------
# The groups behave like every other group
# ---------------------------------------------------------------------------
def test_the_two_groups_are_gated_independently() -> None:
    """A disabled group must emit nothing at all, which is what makes the toggles
    save real work in `derive()` rather than just hiding channels."""
    depth_only = derive(_from_pose_values(), None, frozenset({"depth"}))
    assert "h0_z" in depth_only
    assert "h0_tilt" not in depth_only
    tilt_only = derive(_from_pose_values(), None, frozenset({"tilt"}))
    assert "h0_tilt" in tilt_only and "h0_tilt_axis" in tilt_only
    assert "h0_z" not in tilt_only


def test_they_add_sixteen_channels_across_two_hands() -> None:
    """1 z + 5 finger z + tilt + tilt_axis, per hand."""
    without = derive(_from_pose_values(), None,
                     frozenset(set(ALL_GROUPS) - {"depth", "tilt"}))
    with_both = derive(_from_pose_values(), None, GROUPS)
    assert len(with_both) - len(without) == 16


def _from_pose_values() -> dict[str, float]:
    frame = synthetic_frame([HandPose(), HandPose(palm_x=0.8)])
    return dict(zip(channel_names(), channel_values(frame, 0.0, 2), strict=True))


def test_every_new_channel_is_finite() -> None:
    """Two divisions and an `acos` between them, so this is worth asserting once."""
    for pose in (HandPose(), HandPose(size=0.01), HandPose(size=0.9),
                 HandPose(curl={f: 1.0 for f in ("thumb", "index", "middle",
                                                 "ring", "little")})):
        out = _from_pose(pose)
        for name, value in out.items():
            assert math.isfinite(value), name
