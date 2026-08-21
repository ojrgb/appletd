"""Derived attributes: the stateless half, as one pure function.

`derive()` maps one frame's channel values to the derived channels specified in
docs/ATTRIBUTES.md. It is pure - same input, same output, no clock, no memory -
which is the whole point: every formula in that document becomes an assertion
against a synthetic hand, with no TouchDesigner and no camera involved.

WHAT IS NOT HERE, deliberately. Anything with memory: velocity, filters, the
Schmitt latches, edge pulses, debounce, dwell, refractory windows. Those live in
native CHOPs, where TouchDesigner does them properly, they are visible in the
network, and they keep no hidden Python state for a project reload to mishandle.
This module emits the *levels* those operators act on - a distance, a condition -
and never the state derived from them over time.

TWO RULES THAT ARE NOT OPTIONAL (docs/ATTRIBUTES.md). An absent hand publishes
every joint at (0, 0):

  * every distance would read 0, which is below every engage threshold, so losing
    a hand would fire pinch, snap and clap at once;
  * `size` would be 0, so every normalised distance would be 0/0 - NaN, which
    spreads silently through TouchDesigner maths and is very hard to trace.

So `h{i}_valid` gates everything, `size` has a floor, and an invalid hand emits
zeros rather than stale or undefined values.

Ref: docs/ATTRIBUTES.md (the definitions), DESIGN.md 6.2 (the input contract),
7 (normalised, bottom-left, never flipped).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from visionhands.types import JOINT_NAMES, MAX_HANDS

# The six joints whose mean is the palm centre. Wrist plus thumb CMC plus the
# four finger MCPs - steadier than the wrist alone, which swings as the hand
# rotates.
PALM_JOINTS: Final = ("wrist", "thumb_cmc", "index_mcp", "middle_mcp",
                      "ring_mcp", "little_mcp")

# Each finger as (base joint, chain root-to-tip). The thumb's base is its MP
# rather than an MCP it does not have.
FINGERS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "thumb": ("thumb_mp", ("thumb_mp", "thumb_ip", "thumb_tip")),
    "index": ("index_mcp", ("index_mcp", "index_pip", "index_dip", "index_tip")),
    "middle": ("middle_mcp", ("middle_mcp", "middle_pip", "middle_dip", "middle_tip")),
    "ring": ("ring_mcp", ("ring_mcp", "ring_pip", "ring_dip", "ring_tip")),
    "little": ("little_mcp", ("little_mcp", "little_pip", "little_dip", "little_tip")),
}
FINGER_NAMES: Final = tuple(FINGERS)
# The four fingers that are not the thumb - openness averages these, because a
# thumb's curl says more about grip than about whether a hand is open.
LONG_FINGERS: Final = ("index", "middle", "ring", "little")


@dataclass(frozen=True)
class Params:
    """Thresholds and calibration. Defaults are the ones in docs/ATTRIBUTES.md.

    Frozen: these arrive from TouchDesigner parameters once per cook and nothing
    downstream should be rewriting them mid-computation.
    """

    confthreshold: float = 0.30
    sizefloor: float = 0.01
    curlmin: float = 0.35
    curlmax: float = 0.95
    extendedbelow: float = 0.30
    curledabove: float = 0.60
    spreadmin: float = 0.40
    spreadmax: float = 1.60
    overlapthreshold: float = 0.15


# Group names, matching the toggles in docs/ATTRIBUTES.md. `derive` computes only
# what is asked for, so a disabled group costs nothing here either.
ALL_GROUPS: Final = ("core", "presence", "contacts", "pose", "twohands",
                     "gestures", "descriptor")


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _normalise(value: float, low: float, high: float) -> float:
    """Map low..high onto 0..1, clamped. Guards a zero-width range."""
    if high <= low:
        return 0.0
    return _clamp01((value - low) / (high - low))


class _HandView:
    """One hand's joints, addressable by name, with the geometry cached once.

    A view rather than a copy: `derive` is called every cook, and computing the
    palm and size once and reusing them is the difference between one pass over
    21 joints and a dozen.
    """

    __slots__ = ("conf", "palm_x", "palm_y", "size", "valid", "x", "y")

    def __init__(self, values: dict[str, float], hand: int, params: Params) -> None:
        prefix = "h%d_" % hand
        self.x = {name: values.get(prefix + name + "_x", 0.0) for name in JOINT_NAMES}
        self.y = {name: values.get(prefix + name + "_y", 0.0) for name in JOINT_NAMES}
        self.conf = {name: values.get(prefix + name + "_conf", 0.0) for name in JOINT_NAMES}

        found = values.get(prefix + "found", 0.0) > 0.5
        conf_median = values.get(prefix + "conf_median", 0.0)
        raw_size = self._raw_distance("wrist", "middle_mcp")
        # Validity is the gate for everything else. A hand that is not found, or
        # whose confidence is below the threshold, or whose geometry has
        # collapsed, produces zeros - never a stale or undefined value.
        self.valid = found and conf_median >= params.confthreshold \
            and raw_size > params.sizefloor
        self.size = max(raw_size, params.sizefloor)
        self.palm_x = sum(self.x[n] for n in PALM_JOINTS) / len(PALM_JOINTS)
        self.palm_y = sum(self.y[n] for n in PALM_JOINTS) / len(PALM_JOINTS)

    def _raw_distance(self, a: str, b: str) -> float:
        return math.hypot(self.x[a] - self.x[b], self.y[a] - self.y[b])

    def ratio(self, a: str, b: str) -> float:
        """Distance between two joints, as a fraction of hand size.

        Every distance published by this module goes through here. Raw normalised
        distances shrink with depth, so a threshold tuned at arm's length would
        fire early when the hand comes closer; dividing by hand size removes the
        depth term entirely (docs/ATTRIBUTES.md).
        """
        return self._raw_distance(a, b) / self.size

    def angle(self, a: str, b: str) -> float:
        """Degrees of the a->b vector, 0 = +x, counter-clockwise."""
        return math.degrees(math.atan2(self.y[b] - self.y[a], self.x[b] - self.x[a]))

    def curl(self, finger: str, params: Params) -> float:
        """0 straight, 1 fully curled.

        A straight finger has tip-to-base distance equal to the sum of its bone
        lengths; a curled one much less. That ratio is scale-free and rotation-
        free by construction, so it needs no normalising by hand size - the
        finger's own bones are the ruler.
        """
        base, chain = FINGERS[finger]
        bone_length = sum(self._raw_distance(chain[i], chain[i + 1])
                          for i in range(len(chain) - 1))
        if bone_length <= 0.0:
            return 0.0
        straightness = self._raw_distance(base, chain[-1]) / bone_length
        # curlmax maps to 0 (straight), curlmin to 1 (curled), hence the invert.
        return 1.0 - _normalise(straightness, params.curlmin, params.curlmax)

    def bbox(self) -> tuple[float, float, float, float]:
        xs = self.x.values()
        ys = self.y.values()
        return min(xs), min(ys), max(xs), max(ys)


def _hand_channels(view: _HandView, hand: int, params: Params,
                   groups: frozenset[str], out: dict[str, float]) -> dict[str, float]:
    """Everything for one hand. Returns the curls, which two-hand code reuses."""
    prefix = "h%d_" % hand
    valid = view.valid
    curls = {name: (view.curl(name, params) if valid else 0.0) for name in FINGER_NAMES}

    def put(name: str, value: float) -> None:
        out[prefix + name] = float(value) if valid else 0.0

    if "core" in groups:
        put("palm_x", view.palm_x)
        put("palm_y", view.palm_y)
        put("size", view.size)
        x1, y1, x2, y2 = view.bbox()
        put("bbox_x1", x1)
        put("bbox_y1", y1)
        put("bbox_x2", x2)
        put("bbox_y2", y2)
        put("bbox_w", x2 - x1)
        put("bbox_h", y2 - y1)
        put("bbox_area", (x2 - x1) * (y2 - y1))

    if "presence" in groups:
        # The stateless half of presence. The published `active` is this
        # debounced by a native Count/Logic pair, because debounce is memory.
        out[prefix + "valid"] = 1.0 if valid else 0.0

    if "contacts" in groups:
        for finger in LONG_FINGERS:
            tip = FINGERS[finger][1][-1]
            put("pinch_" + finger, view.ratio(tip, "thumb_tip"))
        put("pinch_angle", view.angle("thumb_tip", "index_tip"))
        put("pinch_x", (view.x["thumb_tip"] + view.x["index_tip"]) / 2.0)
        put("pinch_y", (view.y["thumb_tip"] + view.y["index_tip"]) / 2.0)

    if "pose" in groups:
        for finger in FINGER_NAMES:
            put("curl_" + finger, curls[finger])
        put("openness", 1.0 - sum(curls[f] for f in LONG_FINGERS) / len(LONG_FINGERS))
        put("spread", _normalise(view.ratio("index_tip", "little_tip"),
                                 params.spreadmin, params.spreadmax))
        put("rotation", view.angle("wrist", "middle_mcp"))
        point_angle = view.angle("index_mcp", "index_tip")
        put("point_x", math.cos(math.radians(point_angle)))
        put("point_y", math.sin(math.radians(point_angle)))
        put("point_angle", point_angle)

    if "gestures" in groups:
        extended = {f: curls[f] < params.extendedbelow for f in FINGER_NAMES}
        curled = {f: curls[f] > params.curledabove for f in FINGER_NAMES}
        long_curled = all(curled[f] for f in LONG_FINGERS)
        spread_now = view.ratio("index_tip", "little_tip")
        # A thumb points "up" or "down" relative to the palm, not to the frame,
        # so that a tilted hand still reads correctly.
        thumb_above = view.y["thumb_tip"] > view.palm_y

        states = {
            # The four long fingers only. A fist with the thumb outside the
            # fingers is still a fist, and the thumb's curl range is genuinely
            # different: it has one fewer segment, so two bends cannot fold as
            # tightly as three and a tucked thumb measures far less curled than a
            # tucked finger. Requiring it would make the most basic gesture in
            # the set the least reliable.
            "g_fist": long_curled,
            "g_open": all(extended[f] for f in FINGER_NAMES)
            and spread_now > params.spreadmin,
            "g_point": extended["index"]
            and all(curled[f] for f in ("middle", "ring", "little")),
            "g_peace": extended["index"] and extended["middle"]
            and curled["ring"] and curled["little"]
            # The V has to be open. Two extended fingers held together is not a
            # peace sign, and without this test it reads as one. UNMEASURED on a
            # real hand: calibrated against synthetic geometry, so this threshold
            # is the most likely of the gesture constants to need adjusting.
            and view.ratio("index_tip", "middle_tip") > 0.25,
            # An OK sign is a closed pinch with the OTHER fingers extended -
            # which is exactly what distinguishes it from a plain pinch, where
            # they usually curl too.
            "g_ok": view.ratio("index_tip", "thumb_tip") < 0.35
            and all(extended[f] for f in ("middle", "ring", "little")),
            "g_thumbsup": extended["thumb"] and long_curled and thumb_above,
            "g_thumbsdown": extended["thumb"] and long_curled and not thumb_above,
            "g_horns": extended["index"] and extended["little"]
            and curled["middle"] and curled["ring"],
            "g_gun": extended["index"] and extended["thumb"]
            and all(curled[f] for f in ("middle", "ring", "little")),
        }
        for name, condition in states.items():
            put(name, 1.0 if condition else 0.0)

    if "descriptor" in groups:
        # Position-, scale- and rotation-invariant pose signature: every joint
        # relative to the palm, over hand size, rotated so `rotation` is zero.
        rotation = math.radians(view.angle("wrist", "middle_mcp"))
        cos_a, sin_a = math.cos(-rotation), math.sin(-rotation)
        for name in JOINT_NAMES:
            dx = (view.x[name] - view.palm_x) / view.size
            dy = (view.y[name] - view.palm_y) / view.size
            put("d_%s_x" % name, dx * cos_a - dy * sin_a)
            put("d_%s_y" % name, dx * sin_a + dy * cos_a)

    return curls


def _twohand_channels(views: list[_HandView], curls: list[dict[str, float]],
                      params: Params, out: dict[str, float]) -> None:
    """Symmetric two-hand geometry. Zeros unless BOTH hands are valid.

    Symmetric on purpose: these are the only two-hand attributes trustworthy
    before slot assignment lands, because they do not care which hand is which
    (DESIGN.md 6.3).
    """
    a, b = views[0], views[1]
    both = a.valid and b.valid
    mean_size = max((a.size + b.size) / 2.0, params.sizefloor)

    def put(name: str, value: float) -> None:
        out[name] = float(value) if both else 0.0

    put("hands_distance", math.hypot(a.palm_x - b.palm_x, a.palm_y - b.palm_y) / mean_size)
    put("hands_center_x", (a.palm_x + b.palm_x) / 2.0)
    put("hands_center_y", (a.palm_y + b.palm_y) / 2.0)
    put("hands_angle", math.degrees(math.atan2(b.palm_y - a.palm_y, b.palm_x - a.palm_x)))
    put("index_distance", math.hypot(a.x["index_tip"] - b.x["index_tip"],
                                     a.y["index_tip"] - b.y["index_tip"]) / mean_size)
    put("index_center_x", (a.x["index_tip"] + b.x["index_tip"]) / 2.0)
    put("index_center_y", (a.y["index_tip"] + b.y["index_tip"]) / 2.0)

    # Bounding-box overlap as a fraction of the smaller box - a scale-free
    # measure of how much the hands occupy the same space, which is also the
    # signal that says "these two are crossing" and will be useful to slot
    # assignment at milestone 5.
    ax1, ay1, ax2, ay2 = a.bbox()
    bx1, by1, bx2, by2 = b.bbox()
    overlap_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    overlap_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    smaller = min((ax2 - ax1) * (ay2 - ay1), (bx2 - bx1) * (by2 - by1))
    put("hands_overlap", (overlap_w * overlap_h) / smaller if smaller > 0.0 else 0.0)

    open_a = 1.0 - sum(curls[0][f] for f in LONG_FINGERS) / len(LONG_FINGERS)
    open_b = 1.0 - sum(curls[1][f] for f in LONG_FINGERS) / len(LONG_FINGERS)
    put("hands_symmetry", 1.0 - abs(open_a - open_b))


def derive(values: dict[str, float], params: Params | None = None,
           groups: frozenset[str] | None = None) -> dict[str, float]:
    """One frame's channels in, the derived channels out. Pure.

    Contract: `values` is keyed by the sidecar's channel names (DESIGN.md 6.2);
              a missing key reads as 0.0 rather than raising, so a caller that
              publishes a subset still works.
    Thread:   pure and stateless, so safe anywhere. In production it runs on
              TouchDesigner's main thread inside a Script CHOP cook.
    """
    params = params or Params()
    groups = groups if groups is not None else frozenset(ALL_GROUPS)

    out: dict[str, float] = {}
    views = [_HandView(values, hand, params) for hand in range(MAX_HANDS)]
    curls = [_hand_channels(views[hand], hand, params, groups, out)
             for hand in range(MAX_HANDS)]

    if "presence" in groups:
        out["n_valid"] = float(sum(1 for view in views if view.valid))
        out["both_valid"] = 1.0 if all(view.valid for view in views) else 0.0

    if "twohands" in groups:
        _twohand_channels(views, curls, params, out)

    return out
