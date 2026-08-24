"""What KIND of number each channel is, so the filter and the coordinate spaces
can be built for every stream from one table instead of three.

WHY THIS EXISTS. Hands got a one-euro filter and four coordinate spaces because
hands came first, and both were built from a hand-shaped list of channel names.
Neither is remotely hand-specific: a body's joints and a face's bounding box want
exactly the same smoothing and exactly the same conversion into TouchDesigner's
world and pixel spaces. What was missing was a machine-readable answer to "is this
channel a position, an extent, an angle, or a number that must be left alone",
which is what this module is.

The builders (`tools/td_add_filter.py`, `tools/td_add_coords.py`) ask this module
rather than pattern-matching names, for the reason DESIGN.md 2.11 records twice: a
wildcard cannot partition these channels. `h?_*_x` matches both a raw landmark and
a derived one, `^` does not exclude in a Select CHOP, and anything a pattern misses
DISAPPEARS from the COMP's output with no error anywhere.

THE FOUR THINGS A CHANNEL CAN BE, and the distinctions are all load-bearing:

  * POSITION - a point in the image, normalised, origin bottom-left. Gets the
    -0.5 offset when it becomes a world coordinate, because world space is centred.
  * EXTENT - a width or a height, normalised. Gets NO offset: the box is 0.2 wide
    whether it sits on the left or the right, and subtracting 0.5 from a width
    produces a negative size that draws as nothing.
  * ANGLE - degrees. Smoothed like a position and NEVER transformed into a
    coordinate space, because there is no such thing as a yaw in pixels.
  * SCALAR - everything else: confidences, counters, ages, flags, chirality.
    Neither smoothed nor transformed. Smoothing a confidence would make a gate lag
    behind the thing it gates, and `found` is a boolean.

AND ONE MORE, which is the trap this module exists to record. Face LANDMARK points
are normalised to the FACE's bounding box, not to the image (`normalizedPoints`,
DESIGN.md 2.12). They look exactly like positions and they are in a different
space, so transforming them with the image rules puts every facial feature in the
top-left corner of the frame. They are `ROLE_BOX_RELATIVE`: smoothed, never
transformed, and they need composing through the bounding box first - which is a
job for whoever publishes them, not for the coordinate builder.

Pure stdlib. No pyobjc, no TouchDesigner - the same rule `types.py` follows.

Thread: pure functions over immutable values. Safe anywhere.
Ref: DESIGN.md 7 (the coordinate contract), 6.4 (the streams), docs/ATTRIBUTES.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase
from typing import Final, NamedTuple

from appletd.face_types import FACE_KEYPOINTS, FACE_REGION_NAMES, face_channel_names
from appletd.pose_types import pose_channel_names
from appletd.streams import (
    STREAM_FACE,
    STREAM_HANDS,
    STREAM_NAMES,
    STREAM_POSE,
    status_channel_names,
)
from appletd.types import channel_names

# The roles. Strings rather than an enum because they end up in generated
# TouchDesigner code and in printed build reports, where an enum member's repr is
# noise.
ROLE_POSITION_X: Final = "position_x"
ROLE_POSITION_Y: Final = "position_y"
ROLE_EXTENT_X: Final = "extent_x"
ROLE_EXTENT_Y: Final = "extent_y"
ROLE_ANGLE: Final = "angle"
ROLE_BOX_RELATIVE: Final = "box_relative"
# The four key points, which are box-relative in exactly the same way and get a
# SEPARATE role for one reason: cost. The 348 landmark channels are the most
# expensive thing in the component and live in a COMP `Facekeypoints` can freeze on
# its own; the 16 key point channels are a fiftieth of that and have to survive that
# freeze. A separate role is what lets the two land in different COMPs.
ROLE_BOX_KEYPOINT: Final = "box_keypoint"
ROLE_SCALAR: Final = "scalar"

# Which roles the one-euro filter touches: everything continuous and physical.
# NOT scalars - a smoothed confidence makes every gate that reads it lag behind the
# thing it is gating, and `found` is a boolean whose midpoint means nothing.
SMOOTHED_ROLES: Final[frozenset[str]] = frozenset({
    ROLE_POSITION_X, ROLE_POSITION_Y, ROLE_EXTENT_X, ROLE_EXTENT_Y,
    ROLE_ANGLE, ROLE_BOX_RELATIVE, ROLE_BOX_KEYPOINT,
})

# Which roles get world and pixel companions. Angles and box-relative points are
# deliberately absent - see the module docstring.
TRANSFORMED_ROLES: Final[frozenset[str]] = frozenset({
    ROLE_POSITION_X, ROLE_POSITION_Y, ROLE_EXTENT_X, ROLE_EXTENT_Y,
})

# The suffix each role's transformed companions take, as (world, pixels). The
# existing hands contract set the convention - `h0_wrist_x` -> `h0_wrist_tx` and
# `h0_wrist_px` - and the extents follow it: `f0_bbox_w` -> `f0_bbox_tw`, `_pw`.
_SUFFIXES: Final[dict[str, tuple[str, str, str]]] = {
    # role: (source suffix, world suffix, pixel suffix)
    ROLE_POSITION_X: ("_x", "_tx", "_px"),
    ROLE_POSITION_Y: ("_y", "_ty", "_py"),
    ROLE_EXTENT_X: ("_w", "_tw", "_pw"),
    ROLE_EXTENT_Y: ("_h", "_th", "_ph"),
}

# Face channels that are not landmarks and not the box. Named explicitly, because
# `f0_roll` gives a suffix-matcher nothing to go on.
_FACE_ANGLES: Final = ("_roll", "_yaw", "_pitch")

# The key point channel endings, built from the contract rather than written out -
# `face_types.py` owns which four points there are.
_FACE_KEYPOINT_SUFFIXES: Final[tuple[str, ...]] = tuple(
    "_%s_%s" % (point, axis)
    for point in FACE_KEYPOINTS for axis in ("x", "y"))


def _role_of(stream: str, name: str) -> str:
    """One channel's role. The single place any of this is decided."""
    if stream == STREAM_FACE:
        # Order matters: the bounding box's `_x` must be caught before the generic
        # landmark rule, and the landmark rule must not swallow the box.
        if name.endswith(("_bbox_x", "_bbox_y", "_bbox_w", "_bbox_h")):
            return {"x": ROLE_POSITION_X, "y": ROLE_POSITION_Y,
                    "w": ROLE_EXTENT_X, "h": ROLE_EXTENT_Y}[name[-1]]
        if name.endswith(_FACE_ANGLES):
            return ROLE_ANGLE
        # Before the landmark rule, and it cannot collide with it: every landmark
        # name carries a two-digit point index (`f0_nose_00_x`) and no key point
        # does (`f0_nose_tip_x`).
        if name.endswith(_FACE_KEYPOINT_SUFFIXES):
            return ROLE_BOX_KEYPOINT
        # A landmark point: `f0_left_eye_03_x`. Normalised to the BOX, not the
        # image (DESIGN.md 2.12), so it is emphatically not a position.
        if any("_%s_" % region in name for region in FACE_REGION_NAMES):
            return ROLE_BOX_RELATIVE
        return ROLE_SCALAR

    # Hands and pose: a joint's `_x`/`_y` are image positions; `_conf` and every
    # per-hand or per-frame scalar is a scalar. Nothing in either contract is an
    # extent or an angle - `hands_angle` and friends are DERIVED downstream by
    # derive.py and never arrive on the wire.
    if name.endswith("_x"):
        return ROLE_POSITION_X
    if name.endswith("_y"):
        return ROLE_POSITION_Y
    return ROLE_SCALAR


def channel_roles(stream: str) -> dict[str, str]:
    """Every channel a stream carries, mapped to its role.

    Contract: covers exactly the stream's own contract channels, PLUS the `sc_*`
              status channels on the hands stream, because those arrive on the same
              port and anything a builder does not classify vanishes from the COMP
              output (DESIGN.md 2.11). Raises on an unknown stream rather than
              returning an empty map, which would silently build a network that
              transforms nothing.
    """
    if stream not in STREAM_NAMES:
        raise ValueError("unknown stream %r; known: %s"
                         % (stream, ", ".join(STREAM_NAMES)))
    if stream == STREAM_HANDS:
        names = list(channel_names()) + list(status_channel_names())
    elif stream == STREAM_POSE:
        names = list(pose_channel_names())
    else:
        names = list(face_channel_names())
    return {name: _role_of(stream, name) for name in names}


def names_by_role(stream: str) -> dict[str, list[str]]:
    """The inverse view: role -> the channels with it, in contract order.

    What the builders actually want, because a Select CHOP takes a list of names.
    Every role is present as a key even when empty, so a caller can index it
    without a `get` and cannot mistake "no such role" for "no channels".
    """
    grouped: dict[str, list[str]] = {
        role: [] for role in (ROLE_POSITION_X, ROLE_POSITION_Y, ROLE_EXTENT_X,
                              ROLE_EXTENT_Y, ROLE_ANGLE, ROLE_BOX_RELATIVE,
                              ROLE_BOX_KEYPOINT, ROLE_SCALAR)}
    for name, role in channel_roles(stream).items():
        grouped[role].append(name)
    return grouped


def smoothed_names(stream: str) -> list[str]:
    """The channels the one-euro filter should touch, in contract order."""
    return [name for name, role in channel_roles(stream).items()
            if role in SMOOTHED_ROLES]


def passthrough_names(stream: str) -> list[str]:
    """The rest. `smoothed_names` and this must PARTITION the stream exactly.

    The filter group merges the two halves back together, so a channel missing from
    both leaves the COMP's output entirely - measured, and with no error anywhere
    (DESIGN.md 2.11). `test_spaces.py` asserts the partition rather than trusting
    that both functions were edited together.
    """
    return [name for name, role in channel_roles(stream).items()
            if role not in SMOOTHED_ROLES]


# ---- patterns, and why every one of them is verified before it is used -----

def compact_pattern(wanted: Sequence[str], universe: Iterable[str],
                    candidates: Sequence[str]) -> str:
    """The first candidate pattern that selects EXACTLY `wanted` out of `universe`,
    or the explicit space-joined list of `wanted` when none of them does.

    Contract: the return value is a TouchDesigner channel-pattern string. Whatever
              it is, feeding it to a Select CHOP's `channames` or any CHOP's `scope`
              selects precisely `wanted` - never a superset, never a subset.
    Why:      a builder's alternative is a literal list of every channel name, and
              those lists got long enough to cost real time. MEASURED: the face
              filter's Select carried 362 names and cost 0.1725 ms per cook, more
              than the filter it fed and 10% of the whole COMP.
    Why verified rather than trusted: DESIGN.md 2.11 records twice that a wildcard
              cannot partition these channels, and both times the damage was
              silent - `h?_*_x` matches a raw landmark and a derived one, and
              `* ^*_x` selected 423 channels out of a 141-channel input because the
              terms are ADDITIVE and `^` does not exclude. So no pattern is used on
              the strength of looking right. Each candidate is EXPANDED against the
              universe it will be applied to and accepted only on an exact match.
    Traps:    `fnmatchcase`, not `fnmatch` - the latter is case-insensitive on
              macOS, which would accept a pattern TouchDesigner rejects.
              VERIFIED that TouchDesigner's own matcher agrees with `fnmatchcase`
              on the syntax used here: `*`, `?` and `[0-9]` character classes all
              selected the identical channel set in a live network. Nothing else is
              used, deliberately - `^` in particular does NOT mean what it looks
              like (see above).
    Ref:      DESIGN.md 2.11, docs/BUILD_PLAN.md step 10.
    """
    target = list(wanted)
    ordered = list(universe)
    for candidate in candidates:
        tokens = candidate.split()
        selected = [name for name in ordered
                    if any(fnmatchcase(name, token) for token in tokens)]
        if selected == target:
            return candidate
    return " ".join(target)


def scope_pattern(stream: str) -> str:
    """A `scope` string selecting exactly the channels the filter should smooth.

    Contract: as `compact_pattern` - verified against the stream's own contract, so
              it is a pattern or a literal list and never a guess.
    Why:      this replaces the filter group's Select -> Filter -> Merge with ONE
              Filter CHOP whose `scope` names what to touch. MEASURED that scope
              passes every unscoped channel through BIT-EXACT (a lagged channel
              differed, an unscoped one matched to the last bit), so the partition
              the old three-operator form had to be checked for is now structural:
              there is no Select to get wrong and no Merge to drop half of.
    Traps:    `scope` on a LOGIC CHOP does not behave this way - it DELETES the
              unscoped channels. Measured: three channels in, `scope` naming two,
              two out. Every other CHOP tried passed all three. So this is for the
              filter, not a rule about `scope` everywhere.
    Ref:      DESIGN.md 2.11.
    """
    smoothed = smoothed_names(stream)
    # One `*_suffix` token per distinct final name component, in first-appearance
    # order so a rebuild writes the same string and does not show as a change.
    tokens: list[str] = []
    for name in smoothed:
        token = "*_" + name.rsplit("_", 1)[-1]
        if token not in tokens:
            tokens.append(token)
    return compact_pattern(smoothed, channel_roles(stream), [" ".join(tokens)])


# ---- the coordinate branches a builder builds ------------------------------

class Branch(NamedTuple):
    """One coordinate branch: a set of channels, and how to turn them into one
    output space.

    label    a network-safe name for the operators, e.g. `tx`
    source   the suffix the input channels END with, e.g. `_x`. Carried rather than
             re-derived, because a builder that peels it off the pattern gets it
             right for `*_x` and wrong the moment the pattern falls back to a
             literal name list.
    suffix   the output suffix, e.g. `_tx`
    names    the source channels, in contract order. The count a builder asserts.
    pattern  a VERIFIED selection pattern for exactly `names` (see compact_pattern)
    offset   the Math CHOP pre-offset: -0.5 centres a position on the camera, 0.0
             leaves an extent alone. A width is 0.2 wide wherever it sits, and
             offsetting it gives a negative size that draws as nothing.
    """

    label: str
    source: str
    suffix: str
    names: list[str]
    pattern: str
    offset: float


class BoxBranch(NamedTuple):
    """One coordinate branch for BOX-RELATIVE channels - a face's landmark points.

    Same shape as `Branch` plus the two bounding-box channels the points have to be
    composed through, and the arithmetic is one operator rather than two:

        out = point * extent * K  +  (origin + offset) * K

    which is exactly a Math CHOP's `gain` and `postoff` as EXPRESSIONS, with
    `preoff` left at zero - MEASURED, the Math CHOP evaluates
    `(value + preoff) * gain + postoff`, and an expression on a scalar parameter is
    evaluated once per cook rather than once per channel. The alternative was an
    Expression CHOP doing the composition per landmark: 608 Python evaluations per
    frame on the main thread, against 16 here.

    origin   the box's LEFT edge for an x, its BOTTOM edge for a y - `bbox_y` is the
             bottom, because that is what `VNFaceObservation` reports (DESIGN.md 7)
    extent   the box's width for an x, its height for a y
    `label`, `source`, `suffix`, `names`, `pattern` and `offset` mean what they
    mean on `Branch`.
    """

    label: str
    source: str
    suffix: str
    names: list[str]
    pattern: str
    origin: str
    extent: str
    offset: float


def transform_branches(stream: str) -> list[Branch]:
    """Every coordinate branch for the channels that are already in IMAGE space.

    Contract: one `Branch` per (role, target space) pair that has channels, in a
              stable order. Positions and extents only - an angle is never
              transformed, because there is no such thing as a yaw in pixels, and a
              box-relative point needs `box_branches` instead.
    The GAIN is not here: it is a TouchDesigner expression on the master COMP's
              parameters, so it belongs to the builder that writes expressions
              rather than to this pure module.
    """
    grouped = names_by_role(stream)
    universe = list(channel_roles(stream))
    branches: list[Branch] = []
    for role in (ROLE_POSITION_X, ROLE_POSITION_Y, ROLE_EXTENT_X, ROLE_EXTENT_Y):
        names = grouped[role]
        if not names:
            continue
        source, world, pixels = _SUFFIXES[role]
        # Candidates from most to least specific. `*_x` covers hands and pose,
        # where every position is a joint; `*_bbox_x` is the face, where `*_x`
        # would also sweep in 176 landmark points.
        pattern = compact_pattern(
            names, universe, ["*" + source, "*_bbox" + source])
        offset = -0.5 if role in (ROLE_POSITION_X, ROLE_POSITION_Y) else 0.0
        branches.append(
            Branch(world.lstrip("_"), source, world, names, pattern, offset))
        branches.append(
            Branch(pixels.lstrip("_"), source, pixels, names, pattern, 0.0))
    return branches


def box_branches(stream: str) -> list[BoxBranch]:
    """Every coordinate branch for the 348 LANDMARK channels, one per (subject,
    axis, space). Empty for hands and pose, so a caller needs no special case.

    Why per subject: each point needs ITS OWN face's box, and a Math CHOP does NOT
              broadcast a one-channel input across many - MEASURED, it concatenated
              the two inputs and renamed the collision `f0_bbox_w1`. Which is what
              makes the expression-on-a-scalar-parameter form above the right one:
              a per-face branch needs no per-channel second operand at all.
    Ref:      DESIGN.md 2.12 (`normalizedPoints`), 7, docs/ATTRIBUTES.md.
    """
    return _box_branches(stream, ROLE_BOX_RELATIVE)


def keypoint_branches(stream: str) -> list[BoxBranch]:
    """The same thing for the four KEY POINTS. Empty for hands and pose.

    Contract: identical arithmetic to `box_branches` - a key point is normalised to
              the face's box exactly as a landmark is - and a separate function only
              so the two can land in different COMPs. 4 channels per branch against
              87, which is the whole reason for the split: `Facekeypoints` freezes
              1.5 ms of landmark composition, and a project that wants two pupils and
              a mouth has to keep them when it does.
    """
    return _box_branches(stream, ROLE_BOX_KEYPOINT)


def _box_branches(stream: str, role: str) -> list[BoxBranch]:
    """`box_branches` and `keypoint_branches` share every line of this."""
    grouped = names_by_role(stream)
    relative = grouped[role]
    if not relative:
        return []
    universe = list(channel_roles(stream))

    # Bucket by subject prefix (`f0`, `f1`) and axis, keeping contract order inside
    # each bucket so a printed build report reads the same way twice.
    buckets: dict[tuple[str, str], list[str]] = {}
    for name in relative:
        subject = name.split("_", 1)[0]
        buckets.setdefault((subject, name.rsplit("_", 1)[-1]), []).append(name)

    # Which box channels an axis composes through, and which suffixes it produces.
    axis_rule = {"x": ("_bbox_x", "_bbox_w", ("_tx", "_px")),
                 "y": ("_bbox_y", "_bbox_h", ("_ty", "_py"))}

    branches: list[BoxBranch] = []
    for (subject, axis), names in buckets.items():
        rule = axis_rule.get(axis)
        if rule is None:
            # A box-relative channel on an axis nothing knows how to compose. Raised
            # rather than skipped: the alternative is 87 channels of plausible
            # nonsense in the output and nothing saying why.
            raise ValueError("box-relative channel with unknown axis %r: %r"
                             % (axis, names[0]))
        origin_suffix, extent_suffix, suffixes = rule
        # Every landmark name is `<subject>_<region>_NN_<axis>` - a two-digit index
        # before the suffix, which `<subject>_bbox_<axis>` does not have. That is
        # what lets one pattern separate the points from the box, and it is checked
        # against the contract rather than believed.
        # Candidates most specific first. The landmarks all carry a two-digit point
        # index, which is what separates them from `<subject>_bbox_<axis>`; the key
        # points carry none, so their only honest candidate is the names themselves
        # - four of them, and `compact_pattern` still EXPANDS it and checks, which
        # is what catches a contract reordering.
        pattern = compact_pattern(
            names, universe,
            ["%s_*_[0-9][0-9]_%s" % (subject, axis), " ".join(names),
             "%s_*_%s" % (subject, axis)])
        for suffix in suffixes:
            offset = -0.5 if suffix in ("_tx", "_ty") else 0.0
            # The label becomes an operator name, and the two roles produce a
            # branch per (subject, axis, space) each - so without the prefix the
            # key point branch and the landmark branch would both want to be
            # `f0_tx`, in the same stream, and one would silently rename itself.
            label = ("kp_" if role == ROLE_BOX_KEYPOINT else "") + subject + suffix
            branches.append(BoxBranch(
                label, "_" + axis, suffix, names, pattern,
                subject + origin_suffix, subject + extent_suffix, offset))
    return branches


def box_expressions(branch: BoxBranch, gain: str,
                    channel: str = "op('in1')['%s']") -> tuple[str, str]:
    """The two TouchDesigner expressions that compose one `BoxBranch`, as
    (gain expression, post-offset expression) for a Math CHOP with `preoff` = 0.

    Contract: with the returned strings on `gain` and `postoff`, a Math CHOP
              evaluating `(value + 0) * gain + postoff` produces

                  ((origin + point * extent) + offset) * K

              which is the box-relative point composed into image space and then
              into the target space, in one operator. `gain` is the target space's
              own factor K as an expression - `op.Appletd.par.Orthowidth` and
              friends - and `channel` is the accessor template for a channel on the
              group's input.
    Why here: the arithmetic is the part of this that has been wrong twice, both
              times invisibly - a `_ty` that used the source image's aspect instead
              of the render's agreed with the correct version for as long as the two
              resolutions matched. Returning STRINGS the builder uses verbatim is
              what lets `test_spaces.py` evaluate them against hand-computed numbers
              instead of trusting that a builder assembled them correctly.
    Ref:      DESIGN.md 7, docs/ATTRIBUTES.md.
    """
    return ("(%s) * (%s)" % (channel % branch.extent, gain),
            "((%s) + (%r)) * (%s)" % (channel % branch.origin, branch.offset, gain))


def companioned_names(stream: str) -> list[str]:
    """Every RAW channel that gets a screen-space companion, in contract order.

    Contract: exactly the channels for which `transform_branches` or `box_branches`
              produces a `_tx`/`_ty`/`_px`/`_py`/`_tw`/... twin. So every name here
              is one whose information survives in full in TouchDesigner's own
              spaces.
    Why:      this is the list the "screen space only" toggle deletes. Framing it as
              "channels that have a companion" rather than "channels that are not
              screen-space adjusted" is deliberate and narrower: an ANGLE has no
              screen-space form and a confidence has no meaning in pixels, so
              dropping those would lose information rather than remove a duplicate.
              The toggle removes second copies, never only copies.
    Ref:      docs/ATTRIBUTES.md, DESIGN.md 6.2 - which is where a vanished channel
              is recorded as a SILENT failure, and this is the one place it is the
              intended behaviour rather than the bug.
    """
    with_companion: set[str] = set()
    for branch in transform_branches(stream):
        with_companion.update(branch.names)
    for box in box_branches(stream) + keypoint_branches(stream):
        with_companion.update(box.names)
    return [name for name in channel_roles(stream) if name in with_companion]


# ---------------------------------------------------------------------------
# Channels that never arrive on the wire
#
# `derive_chop` and `temporal` compute another 100 channels ending in `_x` or `_y`
# INSIDE TouchDesigner, and they are not all the same kind of number. The screen
# space toggle made that visible: it left 24 channels behind because they had no
# companion, and sorting out which of them SHOULD have one is what this section is.
#
#   POSITIONS       palm, pinch, hands_center, index_center. Points in the image,
#                   normalised, exactly like a joint. 12 channels, and they were
#                   simply missed - a project drawing the palm centre has to
#                   recompute the transform by hand.
#   RATES           vel. Normalised units per SECOND. It scales into world units
#                   per second and takes NO offset, which is the extent rule - a
#                   velocity of 0.2 is 0.2 wherever the hand is.
#   UNIT VECTORS    point, dir. NOT transformable, and this is a real conclusion
#                   rather than an omission: world space scales y by the render's
#                   aspect, so a transformed unit vector is no longer unit length,
#                   and the DIRECTION it points is not the direction the image-space
#                   one pointed. Re-normalising it is a magnitude and a divide - a
#                   different branch, not a companion. Left alone, and named here so
#                   the next person does not have to work it out again.
#   HAND-LOCAL      the 84 `h{i}_d_<joint>_x/y` descriptor channels. Joint offsets
#                   rotated into the HAND's own frame and divided by hand size, so
#                   they are a shape signature and there is no image position in
#                   them at all. Transforming one would be meaningless.
#
# Keyed by the operator that publishes them, because a coordinate branch has to
# read that operator and the two are built at different times.
DERIVE_CHOP: Final = "derive_chop"
TEMPORAL: Final = "temporal"
DERIVED_SOURCES: Final[tuple[str, ...]] = (DERIVE_CHOP, TEMPORAL)

# Per hand, and MAX_HANDS is 2 everywhere in this system.
_MAX_HANDS: Final = 2
_DERIVED_PER_HAND: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    DERIVE_CHOP: (("palm", "position"), ("pinch", "position"),
                  ("point", "direction")),
    TEMPORAL: (("vel", "rate"), ("dir", "direction")),
}
# Per frame rather than per hand: one point between the two hands.
_DERIVED_PER_FRAME: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    DERIVE_CHOP: (("hands_center", "position"), ("index_center", "position")),
    TEMPORAL: (),
}
_KIND_ROLES: Final[dict[str, tuple[str, str]]] = {
    "position": (ROLE_POSITION_X, ROLE_POSITION_Y),
    # An extent's rule: scaled, never offset.
    "rate": (ROLE_EXTENT_X, ROLE_EXTENT_Y),
    # Deliberately a scalar, so nothing transforms it. See the note above.
    "direction": (ROLE_SCALAR, ROLE_SCALAR),
}


def derived_roles(source: str) -> dict[str, str]:
    """The channels `source` publishes that a coordinate branch cares about.

    Contract: names in publication order, mapped to the same roles the wire
              contract uses - so `transform_branches`' arithmetic applies unchanged
              and there is no second copy of the coordinate formula anywhere.
              A `direction` is deliberately ROLE_SCALAR: it is not transformable and
              marking it so is what keeps it out of every branch.
    Raises:   on an unknown source, rather than returning an empty map - which would
              silently build a group that transforms nothing.
    """
    if source not in DERIVED_SOURCES:
        raise ValueError("unknown derived source %r; known: %s"
                         % (source, ", ".join(DERIVED_SOURCES)))
    roles: dict[str, str] = {}
    for stem, kind in _DERIVED_PER_HAND[source]:
        role_x, role_y = _KIND_ROLES[kind]
        for hand in range(_MAX_HANDS):
            roles["h%d_%s_x" % (hand, stem)] = role_x
            roles["h%d_%s_y" % (hand, stem)] = role_y
    for stem, kind in _DERIVED_PER_FRAME[source]:
        role_x, role_y = _KIND_ROLES[kind]
        roles["%s_x" % stem] = role_x
        roles["%s_y" % stem] = role_y
    return roles


def derived_branches(source: str) -> list[Branch]:
    """The coordinate branches for one derived source, as `Branch` tuples.

    Contract: same shape and same arithmetic as `transform_branches`, so
              `tools/td_add_coords.py` builds them with the identical code. The
              patterns are LITERAL NAME LISTS rather than wildcards, and that is not
              laziness: these branches read `derive_chop`, whose output contains
              `h0_palm_x` AND all 84 `h0_d_<joint>_x` descriptor channels, and
              DESIGN.md 2.11 records that `h?_*_x` cannot tell those apart. There are
              at most twelve names in a list here, so the cost is nothing.
    """
    roles = derived_roles(source)
    branches: list[Branch] = []
    for role in (ROLE_POSITION_X, ROLE_POSITION_Y, ROLE_EXTENT_X, ROLE_EXTENT_Y):
        names = [name for name, got in roles.items() if got == role]
        if not names:
            continue
        # An x-axis rate is still an `_x` channel on the wire; only its RULE is the
        # extent's. So the source suffix comes from the axis, not from the role.
        axis = "_x" if role in (ROLE_POSITION_X, ROLE_EXTENT_X) else "_y"
        world = "_t" + axis[1]
        pixels = "_p" + axis[1]
        pattern = " ".join(names)
        offset = -0.5 if role in (ROLE_POSITION_X, ROLE_POSITION_Y) else 0.0
        branches.append(Branch(source + world, axis, world, names, pattern, offset))
        branches.append(Branch(source + pixels, axis, pixels, names, pattern, 0.0))
    return branches


def derived_companioned_names() -> list[str]:
    """Every DERIVED channel that gets a screen-space companion.

    Kept separate from `companioned_names` for one reason: these channels can be
    ABSENT. A disabled derive group does not emit its channels at all - unlike a
    frozen native group, which holds them - so a builder must not treat a missing
    one as a fault. The wire-contract half is always present and is checked.
    """
    names: list[str] = []
    for source in DERIVED_SOURCES:
        for branch in derived_branches(source):
            names.extend(n for n in branch.names if n not in names)
    return names


def source_suffix(role: str) -> str:
    """The suffix a role's channels end with, for a Rename CHOP's From pattern."""
    return _SUFFIXES[role][0]


def _self_check() -> None:
    """Every stream classifies, every stream partitions, and every pattern this
    module hands out selects exactly what it claims. Cheap, and all three are
    properties the two TouchDesigner builders depend on."""
    for stream in STREAM_NAMES:
        roles = channel_roles(stream)
        if not roles:
            raise RuntimeError("%s classified no channels at all" % stream)
        smoothed = set(smoothed_names(stream))
        passthrough = set(passthrough_names(stream))
        if smoothed & passthrough:
            raise RuntimeError("%s has channels in both halves of the filter split"
                               % stream)
        if smoothed | passthrough != set(roles):
            raise RuntimeError("the filter split does not cover %s" % stream)
        # A pattern that has drifted from the contract is the failure mode this
        # module exists to prevent, so it is checked at import rather than left to
        # a builder to discover against a live network.
        for pattern, wanted in ([(scope_pattern(stream), smoothed_names(stream))]
                                + [(b.pattern, b.names)
                                   for b in transform_branches(stream)]
                                + [(b.pattern, b.names)
                                   for b in box_branches(stream)]
                                + [(b.pattern, b.names)
                                   for b in keypoint_branches(stream)]):
            tokens = pattern.split()
            got = [name for name in roles
                   if any(fnmatchcase(name, token) for token in tokens)]
            if got != list(wanted):
                raise RuntimeError(
                    "%s: pattern %r selects %d channels, wanted %d"
                    % (stream, pattern[:60], len(got), len(wanted)))


_self_check()
