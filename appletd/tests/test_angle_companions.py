"""No angle ever gets a coordinate companion.

`coords` builds `_tx`/`_ty` for every channel it believes is a normalised POSITION,
and it finds them with patterns. An angle that happens to end in `_x` looks exactly
like a position to a pattern — and there is no such thing as a yaw in world units, so
the companion is meaningless and `Screenspaceonly` may delete the real channel in its
place.

This has now bitten twice. `f0_angle_x` was caught before it shipped, by naming the
face angles explicitly in `_role_of`. `hands_angle_x` was NOT: `hands_*_x` in
`_MERGED_CANDIDATES` had been written when the only per-frame positions were the two
centres, and it silently produced `hands_angle_tx` the day the angle was added.

So this checks the property rather than the two instances: whatever ends in `_angle_x`,
`_angle_y` or `_angle_z` must never appear in a transform branch, for any stream.

Ref: appletd/spaces.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import pytest

from appletd.face_types import face_channel_names
from appletd.pose_types import pose_channel_names
from appletd.spaces import (
    DERIVED_SOURCES,
    STREAM_MERGED,
    box_branches,
    derived_branches,
    transform_branches,
)
from appletd.streams import STREAM_FACE, STREAM_HANDS, STREAM_NAMES, STREAM_POSE
from appletd.types import channel_names

ANGLE_SUFFIXES = ("_angle_x", "_angle_y", "_angle_z")


def _every_branch_name() -> set[str]:
    """Every channel any coordinate branch would transform, across every stream."""
    names: set[str] = set()
    for stream in (*STREAM_NAMES, STREAM_MERGED):
        for transformed in transform_branches(stream):
            names.update(transformed.names)
        try:
            for boxed in box_branches(stream):
                names.update(boxed.names)
        except (KeyError, ValueError):
            pass                        # not every stream has box-relative points
    for source in DERIVED_SOURCES:
        for derived in derived_branches(source):
            names.update(derived.names)
    return names


# WHY THERE IS NO PATTERN-LEVEL TEST HERE, since it is the obvious next one to write:
# a branch's pattern is verified against the universe it is APPLIED to, and a
# single-stream branch's universe is the WIRE contract, which contains no angle ending
# in `_x` - the angles are derived and merge in further downstream. So `*_x` is exact
# there, and asserting it never matches `h0_angle_x` fails on a channel that pattern
# never sees. The resolved NAMES below are the property that actually matters.
def test_no_angle_is_ever_transformed() -> None:
    caught = sorted(n for n in _every_branch_name()
                    if n.endswith(ANGLE_SUFFIXES))
    assert caught == [], "these angles would get a world/pixel companion: %s" % caught


@pytest.mark.parametrize("stream,names", [
    (STREAM_HANDS, channel_names),
    (STREAM_POSE, pose_channel_names),
    (STREAM_FACE, face_channel_names),
])
def test_the_wire_contract_has_no_angle_companions(
        stream: str, names: Callable[[], Iterable[str]]) -> None:
    """Belt and braces: no `*_angle_tx` style name is in any contract either."""
    for name in names():
        assert "_angle_t" not in name and "_angle_p" not in name, name
