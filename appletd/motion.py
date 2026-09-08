"""Pure functions of the TEMPORAL channels, where a native CHOP cannot do the job.

`derive.py` is a pure function of one frame's raw channels, and keeping that contract
exact is what makes every formula in docs/ATTRIBUTES.md a unit test. Velocity is not
among those channels - it is computed downstream by native CHOPs - so anything that is
a pure function OF velocity has nowhere to live there without muddying its input.

Why not native: direction is `atan2(vel_y, vel_x)` and no CHOP does atan2. The Math
CHOP's unary menu covers negate, absolute value, square, root and reciprocal, which
between them built the whole one-euro filter, but not an inverse tangent.

Thread: pure and stateless. Safe anywhere.
Ref: docs/ATTRIBUTES.md, DESIGN.md 2.11.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from appletd.types import MAX_HANDS


@dataclass(frozen=True)
class MotionParams:
    """Thresholds this module needs. Frozen, like `derive.Params`.

    `speedfloor` is in normalised units per second and is the speed below which a
    direction is noise rather than a heading - see `directions`.
    """

    speedfloor: float = 0.05


def directions(values: dict[str, float],
               params: MotionParams | None = None) -> dict[str, float]:
    """Velocity channels in, heading channels out. Pure.

    Contract: reads `h{i}_vel_x`, `h{i}_vel_y` and `h{i}_speed`; a missing key reads
              as 0.0 rather than raising. Emits per hand:

                h{i}_dir        degrees, 0 = +x, counter-clockwise
                h{i}_dir_x      the unit heading vector, free once the angle is known
                h{i}_dir_y
                h{i}_moving     1 when speed is above `speedfloor`

    All three headings HOLD together below the floor. Holding only the angle once left
    the vector following live velocity, so a stationary hand reported `dir` = 180
    while `dir_x` read +1 - two channels disagreeing is worse than either alone.
    """
    params = params or MotionParams()
    out: dict[str, float] = {}
    for hand in range(MAX_HANDS):
        prefix = "h%d_" % hand
        vel_x = values.get(prefix + "vel_x", 0.0)
        vel_y = values.get(prefix + "vel_y", 0.0)
        # The published `speed` rather than recomputing the hypotenuse: it is
        # already the AVERAGED magnitude (DESIGN.md 2.11 - a per-cook derivative
        # alternates between double-size and zero, so only the mean is true), and
        # recomputing from the same averaged components would agree anyway. Reading
        # it keeps one definition of speed in the system rather than two.
        speed = values.get(prefix + "speed", 0.0)

        angle = math.degrees(math.atan2(vel_y, vel_x))
        out[prefix + "dir"] = angle
        radians = math.radians(angle)
        out[prefix + "dir_x"] = math.cos(radians)
        out[prefix + "dir_y"] = math.sin(radians)
        out[prefix + "moving"] = 1.0 if speed > params.speedfloor else 0.0
    return out
