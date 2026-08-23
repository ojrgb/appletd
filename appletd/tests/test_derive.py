"""Every formula in docs/ATTRIBUTES.md, asserted against a hand we generated.

This is the payoff of the synthetic generator: a real hand cannot tell you what
its openness ought to be, so a test against one can only check that a number came
out. A generated hand knows - ask for a fist and `openness` must be 0, ask for
30 degrees of rotation and `rotation` must be 30 - which turns the spec into
assertions rather than prose.

No TouchDesigner, no camera, no pyobjc.
"""

from __future__ import annotations

import math

import pytest

from appletd.derive import ALL_GROUPS, Params, derive
from appletd.synth import (
    HandPose,
    fist,
    open_hand,
    peace,
    pinching,
    pointing,
    synthetic_frame,
)
from appletd.types import channel_names, channel_values

SIZE = 0.12


def channels(*poses: HandPose | None) -> dict[str, float]:
    """Synthetic hands -> the channel dict the sidecar would have sent."""
    frame = synthetic_frame(list(poses))
    n_hands = sum(1 for hand in frame.hands if hand.found)
    return dict(zip(channel_names(), channel_values(frame, 0.0, n_hands), strict=True))


def d(*poses: HandPose | None, params: Params | None = None) -> dict[str, float]:
    return derive(channels(*poses), params)


# ---------------------------------------------------------------------------
# The absent-hand trap. First, because it is the one that would have shipped.
# ---------------------------------------------------------------------------
def test_an_absent_hand_produces_zeros_and_never_nan() -> None:
    """An absent hand publishes every joint at (0,0).

    Every distance would then read 0 - below every engage threshold - so losing a
    hand would fire pinch, snap and clap at once; and size would be 0, making
    every normalised distance 0/0. NaN spreads silently through TouchDesigner
    maths, so this is the most important test in the file.
    """
    out = d(None, None)
    assert out["h0_valid"] == 0.0
    assert out["h1_valid"] == 0.0
    assert out["n_valid"] == 0.0
    assert out["both_valid"] == 0.0
    assert all(value == value for value in out.values()), "a NaN escaped"
    assert all(abs(value) != math.inf for value in out.values())
    # Specifically: the proximity distances must NOT read as closed.
    for name in ("h0_pinch_index", "h0_pinch_middle", "hands_distance"):
        assert out[name] == 0.0
    # And nothing derived is left at a stale or invented value.
    assert out["h0_size"] == 0.0
    assert out["h0_openness"] == 0.0


def test_a_low_confidence_hand_is_invalid() -> None:
    """Confidence gates validity, so a barely-seen hand cannot drive gestures."""
    seen = d(open_hand(confidence=0.9))
    unseen = d(open_hand(confidence=0.05))
    assert seen["h0_valid"] == 1.0
    assert unseen["h0_valid"] == 0.0
    assert unseen["h0_openness"] == 0.0


def test_a_collapsed_hand_is_invalid_rather_than_dividing_by_zero() -> None:
    """A degenerate hand - every joint on top of another - has size 0."""
    out = d(HandPose(size=0.0, confidence=0.9))
    assert out["h0_valid"] == 0.0
    assert all(value == value for value in out.values())


# ---------------------------------------------------------------------------
# Core geometry: exact, because the generator makes it exact
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("size", [0.05, 0.12, 0.30])
def test_size_is_the_size_that_was_generated(size: float) -> None:
    """`h{i}_size` is the depth proxy AND the normaliser for every distance, so
    an error here scales everything else."""
    assert d(open_hand(size=size))["h0_size"] == pytest.approx(size, abs=1e-6)


@pytest.mark.parametrize(("px", "py"), [(0.5, 0.5), (0.2, 0.8), (0.9, 0.1)])
def test_palm_is_where_the_hand_was_placed(px: float, py: float) -> None:
    out = d(open_hand(palm_x=px, palm_y=py))
    assert out["h0_palm_x"] == pytest.approx(px, abs=1e-6)
    assert out["h0_palm_y"] == pytest.approx(py, abs=1e-6)


@pytest.mark.parametrize("rotation", [0.0, 30.0, 90.0, 179.0, -90.0])
def test_rotation_is_the_rotation_that_was_generated(rotation: float) -> None:
    got = d(open_hand(rotation=rotation))["h0_rotation"]
    assert math.cos(math.radians(got - rotation)) == pytest.approx(1.0, abs=1e-6)


def test_y_is_not_flipped_anywhere_in_the_derivation() -> None:
    """Fingers up means fingertips ABOVE the palm (DESIGN.md 7).

    The whole pipeline is bottom-left, y-up, and a flip anywhere here would
    produce plausible-looking attributes for an upside-down hand.
    """
    out = d(open_hand(rotation=90.0))
    assert out["h0_bbox_y2"] > out["h0_palm_y"]
    assert out["h0_point_y"] > 0.5, "index should point UP at rotation 90"


def test_bbox_contains_the_hand_and_its_area_is_consistent() -> None:
    out = d(open_hand(palm_x=0.5, palm_y=0.5, size=0.12))
    assert out["h0_bbox_x1"] < out["h0_palm_x"] < out["h0_bbox_x2"]
    assert out["h0_bbox_y1"] < out["h0_palm_y"] < out["h0_bbox_y2"]
    assert out["h0_bbox_w"] == pytest.approx(out["h0_bbox_x2"] - out["h0_bbox_x1"])
    assert out["h0_bbox_area"] == pytest.approx(out["h0_bbox_w"] * out["h0_bbox_h"])


def test_every_distance_is_depth_invariant() -> None:
    """The decision the whole spec rests on.

    The same pose at two sizes must give the same ratios. If it does not, a pinch
    threshold tuned at arm's length breaks when the hand comes closer - which is
    the failure normalising by hand size exists to prevent.
    """
    near = d(pinching(0.5, size=0.25))
    far = d(pinching(0.5, size=0.06))
    for name in ("h0_pinch_index", "h0_pinch_middle", "h0_spread", "h0_openness"):
        assert near[name] == pytest.approx(far[name], abs=1e-6), name


# ---------------------------------------------------------------------------
# Pose
# ---------------------------------------------------------------------------
def test_curl_and_openness_agree_with_what_was_generated() -> None:
    """The generator's curl convention and the derived one are the same scale."""
    closed = d(fist())
    opened = d(open_hand())
    assert closed["h0_curl_index"] == pytest.approx(1.0, abs=0.02)
    assert opened["h0_curl_index"] == pytest.approx(0.0, abs=0.02)
    assert closed["h0_openness"] == pytest.approx(0.0, abs=0.02)
    assert opened["h0_openness"] == pytest.approx(1.0, abs=0.02)


def test_openness_is_monotonic_in_curl() -> None:
    previous = 1.1
    for amount in (0.0, 0.25, 0.5, 0.75, 1.0):
        pose = HandPose(curl={f: amount for f in
                              ("thumb", "index", "middle", "ring", "little")})
        current = d(pose)["h0_openness"]
        assert current < previous, "openness must fall as curl rises"
        previous = current


def test_pinch_falls_to_zero_as_the_tips_meet() -> None:
    values = [d(pinching(a))["h0_pinch_index"] for a in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert values == sorted(values, reverse=True), values
    assert values[-1] == pytest.approx(0.0, abs=1e-6)


def test_pinch_point_is_between_the_two_tips() -> None:
    out = d(pinching(1.0, palm_x=0.4, palm_y=0.6))
    # At a full pinch the tips coincide, so the midpoint is that point - and it
    # must be somewhere sane relative to the palm rather than at the origin.
    assert 0.0 < out["h0_pinch_x"] < 1.0
    assert 0.0 < out["h0_pinch_y"] < 1.0


def test_point_vector_is_a_unit_vector_matching_its_angle() -> None:
    out = d(pointing(rotation=45.0))
    assert math.hypot(out["h0_point_x"], out["h0_point_y"]) == pytest.approx(1.0, abs=1e-6)
    assert math.degrees(math.atan2(out["h0_point_y"], out["h0_point_x"])) == \
        pytest.approx(out["h0_point_angle"], abs=1e-6)


# ---------------------------------------------------------------------------
# Gesture conditions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("pose", "expected"), [
    (open_hand(), "g_open"),
    (fist(), "g_fist"),
    (pointing(), "g_point"),
    (peace(), "g_peace"),
])
def test_each_named_pose_fires_its_own_gesture(pose: HandPose, expected: str) -> None:
    out = d(pose)
    assert out["h0_" + expected] == 1.0, expected


def test_a_closed_pinch_with_other_fingers_extended_is_an_ok_sign() -> None:
    """What separates an OK sign from a plain pinch: the other three fingers.

    Without that test both gestures fire on the same pose and you cannot have
    both in the set.
    """
    ok = d(pinching(1.0))
    assert ok["h0_g_ok"] == 1.0
    # A fist has a closed hand but no extended fingers, so it is not an OK sign.
    assert d(fist())["h0_g_ok"] == 0.0


def test_gestures_are_not_mutually_exclusive_and_that_is_intended() -> None:
    """A pointing hand with the thumb up genuinely is also a gun shape.

    Recorded as a test so nobody later "fixes" it into an exclusive set: which
    interpretation wins is a decision for the project, not for this layer.
    """
    out = d(pointing())
    assert out["h0_g_point"] == 1.0
    assert out["h0_g_gun"] == 1.0


def test_an_invalid_hand_fires_no_gesture() -> None:
    out = d(fist(confidence=0.01))
    assert not any(value for name, value in out.items() if "_g_" in name)


# ---------------------------------------------------------------------------
# Two hands
# ---------------------------------------------------------------------------
def test_hands_distance_is_the_separation_over_mean_size() -> None:
    out = d(open_hand(palm_x=0.35, palm_y=0.5, size=0.1),
            open_hand(palm_x=0.65, palm_y=0.5, size=0.1))
    assert out["hands_distance"] == pytest.approx(0.30 / 0.10, abs=1e-6)
    assert out["hands_center_x"] == pytest.approx(0.5, abs=1e-6)
    assert out["hands_angle"] == pytest.approx(0.0, abs=1e-6)


def test_two_hand_attributes_need_both_hands() -> None:
    """One hand present must not produce a distance to nowhere."""
    out = d(open_hand(palm_x=0.35), None)
    assert out["both_valid"] == 0.0
    assert out["hands_distance"] == 0.0
    assert out["hands_center_x"] == 0.0


def test_hands_symmetry_measures_matching_openness() -> None:
    same = d(open_hand(palm_x=0.3), open_hand(palm_x=0.7))
    different = d(open_hand(palm_x=0.3), fist(palm_x=0.7))
    assert same["hands_symmetry"] == pytest.approx(1.0, abs=0.02)
    assert different["hands_symmetry"] == pytest.approx(0.0, abs=0.05)


def test_overlap_is_zero_apart_and_rises_monotonically_as_they_meet() -> None:
    """Asserted as a monotonic relationship rather than against a chosen number.

    An earlier version demanded overlap > 0.5 for hands half a hand-size apart
    and got 0.455 - the test was wrong, not the code. The property that matters is
    that overlap is zero when separated, complete when coincident, and rises in
    between; any particular value depends on the synthetic hand's proportions.
    """
    separations = (0.70, 0.20, 0.10, 0.04, 0.00)
    overlaps = [d(open_hand(palm_x=0.5 - gap / 2, size=0.08),
                  open_hand(palm_x=0.5 + gap / 2, size=0.08))["hands_overlap"]
                for gap in separations]
    assert overlaps[0] == 0.0, "well separated hands must not overlap"
    assert overlaps[-1] == pytest.approx(1.0, abs=1e-6), "coincident hands overlap fully"
    assert overlaps == sorted(overlaps), "overlap must rise as the hands approach"


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
def test_a_disabled_group_computes_nothing() -> None:
    """The toggles have to save work inside this function too, not only outside."""
    everything = derive(channels(open_hand()), None, frozenset(ALL_GROUPS))
    only_core = derive(channels(open_hand()), None, frozenset({"core"}))
    assert len(only_core) < len(everything)
    assert "h0_size" in only_core
    assert not any("_g_" in name for name in only_core)
    assert not any(name.startswith("hands_") for name in only_core)


def test_the_descriptor_is_position_scale_and_rotation_invariant() -> None:
    """The point of the descriptor: the same pose gives the same numbers wherever
    it is, however big, whichever way up."""
    a = derive(channels(fist(palm_x=0.2, palm_y=0.3, size=0.08, rotation=10.0)),
               None, frozenset({"descriptor"}))
    b = derive(channels(fist(palm_x=0.8, palm_y=0.7, size=0.22, rotation=200.0)),
               None, frozenset({"descriptor"}))
    for name, value in a.items():
        assert b[name] == pytest.approx(value, abs=0.02), name


def test_params_are_honoured() -> None:
    """A threshold that does nothing is worse than no threshold."""
    strict = derive(channels(open_hand(confidence=0.5)), Params(confthreshold=0.9))
    loose = derive(channels(open_hand(confidence=0.5)), Params(confthreshold=0.1))
    assert strict["h0_valid"] == 0.0
    assert loose["h0_valid"] == 1.0


# ---------------------------------------------------------------------------
# Handedness: a left hand is the mirror image, not a relabelled right one
# ---------------------------------------------------------------------------
def test_a_left_hand_is_mirrored_and_a_right_one_is_not() -> None:
    """Caught by the user reading the generator: both thumbs pointed the same way.

    `_FINGERS` describes a right hand, so before this a left hand came out with
    identical geometry and only a different `chirality` label. That made the label
    cosmetic, and it meant anything handedness-sensitive - `rotation`,
    `point_angle`, `pinch_angle`, `spread`, the pose descriptor - was silently
    testing a right hand twice.

    The property asserted is the one that defines handedness: which side of the
    little finger the thumb is on.
    """
    from appletd.types import CHIRALITY_LEFT, CHIRALITY_RIGHT, JOINT_NAMES

    thumb, little = JOINT_NAMES.index("thumb_tip"), JOINT_NAMES.index("little_tip")
    sides = {}
    for chirality in (CHIRALITY_RIGHT, CHIRALITY_LEFT):
        hand = synthetic_frame([open_hand(palm_x=0.5, palm_y=0.5, size=0.12,
                                          rotation=90.0, chirality=chirality),
                                None]).hands[0]
        sides[chirality] = hand.joints[thumb].x - hand.joints[little].x

    assert sides[CHIRALITY_RIGHT] < 0.0, "a right hand's thumb sits left of its little finger"
    assert sides[CHIRALITY_LEFT] > 0.0, "a left hand's thumb must sit on the OTHER side"
    # And a true reflection, not merely a different sign.
    assert sides[CHIRALITY_LEFT] == pytest.approx(-sides[CHIRALITY_RIGHT])


def test_mirroring_leaves_every_scalar_attribute_alone() -> None:
    """A reflection must not change size, rotation, curl, openness or spread.

    `size` and `rotation` are both defined off the wrist-to-middle-MCP vector, and
    middle_mcp sits at local x = 0, so reflecting x cannot move it - which is what
    keeps every exactness assertion in this file true for either hand. If this ever
    fails, the mirror has been applied somewhere that also moves middle_mcp.
    """
    from appletd.types import CHIRALITY_LEFT, CHIRALITY_RIGHT

    def attributes(chirality: int) -> dict[str, float]:
        return derive(channels(peace(palm_x=0.4, palm_y=0.6, size=0.14,
                                     rotation=35.0, chirality=chirality)))

    right, left = attributes(CHIRALITY_RIGHT), attributes(CHIRALITY_LEFT)
    for name in ("h0_size", "h0_rotation", "h0_openness", "h0_spread",
                 "h0_curl_index", "h0_curl_middle", "h0_curl_ring",
                 "h0_curl_little", "h0_curl_thumb", "h0_g_peace"):
        assert left[name] == pytest.approx(right[name], abs=1e-6), name


def test_mirroring_DOES_flip_the_directional_attributes() -> None:
    """The negative control: a reflection has to change something, or it did nothing.

    `pinch_angle` is the thumb->index vector's angle, which is exactly the kind of
    quantity a reflection must change. Without this, the test above would pass on a
    mirror that was not applied at all.
    """
    from appletd.types import CHIRALITY_LEFT, CHIRALITY_RIGHT

    def angle(chirality: int) -> float:
        return derive(channels(open_hand(palm_x=0.5, palm_y=0.5, size=0.12,
                                         rotation=90.0,
                                         chirality=chirality)))["h0_pinch_angle"]

    assert angle(CHIRALITY_LEFT) != pytest.approx(angle(CHIRALITY_RIGHT), abs=1.0)
