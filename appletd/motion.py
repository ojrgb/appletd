"""Pure functions of the TEMPORAL channels, where a native CHOP cannot do the job.

WHY THIS EXISTS AND `derive.py` DOES NOT COVER IT. `derive.py` is a pure function
of one frame's 137 raw channels, and that contract is worth keeping exact - it is
what makes every formula in docs/ATTRIBUTES.md a unit test against a synthetic
hand. Velocity is not in those 137: it is computed downstream by native CHOPs. So
anything that is a pure function *of velocity* has nowhere to live in `derive.py`
without muddying what its input means.

WHY NOT NATIVE. Direction is `atan2(vel_y, vel_x)` and **no CHOP does atan2**. The
Math CHOP's unary menu covers negate, absolute value, square, square root and
reciprocal (DESIGN.md 2.11) - which between them built the whole one-euro filter -
but not an inverse tangent. The alternatives were an Expression CHOP holding one
hand-written expression per hand, which does not extend to the swipe sectors, or
this: a thin Script CHOP wrapping a tested pure function, exactly as `derive.py` is
wrapped.

STATELESS, and that is the whole reason it is allowed to be Python. The rule in
docs/ATTRIBUTES.md is that anything with MEMORY lives in native CHOPs, where
TouchDesigner manages the state and a project reload cannot leave it stale. This
module has no memory: same velocity in, same direction out. The holding of `dir`
below `Speedfloor` - which IS memory - is done in CHOPs, not here.

Thread: pure, so safe anywhere. In production it runs on TouchDesigner's main
        thread inside a Script CHOP cook, alongside `derive()`.
Ref: docs/ATTRIBUTES.md (the Motion group), DESIGN.md 2.11 (why not native),
     appletd/derive.py (the pattern this follows).
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

    Contract: reads `h{i}_vel_x`, `h{i}_vel_y` and `h{i}_speed`; a missing key
              reads as 0.0 rather than raising, so a caller publishing a subset
              still works. Emits, per hand:

                h{i}_dir        degrees, 0 = +x, counter-clockwise, matching every
                                other angle in this system (docs/ATTRIBUTES.md)
                h{i}_dir_x      the unit heading vector - what most consumers
                h{i}_dir_y      actually want, and free once the angle is known
                h{i}_moving     1 when speed is above `speedfloor`

    Why `dir_x`/`dir_y` as well as the angle: an angle has to be un-wrapped before
    it can be interpolated or filtered, and every consumer that just wants to push
    something in a direction would otherwise have to convert it back. The pair
    costs two cosines and cannot wrap.

    Why `moving` rather than holding `dir` here: holding a value is MEMORY, and
    memory belongs in native CHOPs. This publishes the CONDITION and lets a CHOP do
    the holding, which is the same division of labour as the latches - this module
    emits the level, the network decides what to remember.

    At rest `dir` is 0 rather than undefined. atan2(0, 0) is 0 in Python, which is
    as good an answer as any and better than a NaN - and `moving` is what tells a
    consumer to disbelieve it.
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
