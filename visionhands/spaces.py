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

from typing import Final

from visionhands.face_types import FACE_REGION_NAMES, face_channel_names
from visionhands.pose_types import pose_channel_names
from visionhands.streams import (
    STREAM_FACE,
    STREAM_HANDS,
    STREAM_NAMES,
    STREAM_POSE,
    status_channel_names,
)
from visionhands.types import channel_names

# The roles. Strings rather than an enum because they end up in generated
# TouchDesigner code and in printed build reports, where an enum member's repr is
# noise.
ROLE_POSITION_X: Final = "position_x"
ROLE_POSITION_Y: Final = "position_y"
ROLE_EXTENT_X: Final = "extent_x"
ROLE_EXTENT_Y: Final = "extent_y"
ROLE_ANGLE: Final = "angle"
ROLE_BOX_RELATIVE: Final = "box_relative"
ROLE_SCALAR: Final = "scalar"

# Which roles the one-euro filter touches: everything continuous and physical.
# NOT scalars - a smoothed confidence makes every gate that reads it lag behind the
# thing it is gating, and `found` is a boolean whose midpoint means nothing.
SMOOTHED_ROLES: Final[frozenset[str]] = frozenset({
    ROLE_POSITION_X, ROLE_POSITION_Y, ROLE_EXTENT_X, ROLE_EXTENT_Y,
    ROLE_ANGLE, ROLE_BOX_RELATIVE,
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
                              ROLE_SCALAR)}
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


def transform_branches(stream: str) -> list[tuple[str, str, list[str], float]]:
    """The coordinate branches to build, as (label, output suffix, channels, offset).

    Contract: returns one entry per (role, target space) pair that has channels,
              in a stable order. `offset` is the PRE-offset a Math CHOP applies
              before its gain: -0.5 centres a position, and 0.0 leaves an extent
              alone - a width is 0.2 wide wherever it sits, and offsetting it
              produces a negative size that draws as nothing.
    The GAIN is not here: it is a TouchDesigner expression on the master COMP's
              parameters, so it belongs to the builder that writes expressions
              rather than to this pure module.
    """
    grouped = names_by_role(stream)
    branches: list[tuple[str, str, list[str], float]] = []
    for role in (ROLE_POSITION_X, ROLE_POSITION_Y, ROLE_EXTENT_X, ROLE_EXTENT_Y):
        names = grouped[role]
        if not names:
            continue
        _source, world, pixels = _SUFFIXES[role]
        offset = -0.5 if role in (ROLE_POSITION_X, ROLE_POSITION_Y) else 0.0
        branches.append((world.lstrip("_"), world, names, offset))
        branches.append((pixels.lstrip("_"), pixels, names, 0.0))
    return branches


def source_suffix(role: str) -> str:
    """The suffix a role's channels end with, for a Rename CHOP's From pattern."""
    return _SUFFIXES[role][0]


def _self_check() -> None:
    """Every stream classifies, and every stream partitions. Cheap, and it is the
    property both builders depend on."""
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


_self_check()
