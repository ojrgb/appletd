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
    # The hand size that reads z = 0. Apparent size goes as 1/distance, so
    # `zreference / size` is a depth PROXY: 1.0 at the reference distance, 2.0 at
    # twice it, 0.5 at half. UNCALIBRATED and unitless by construction - there is no
    # depth in a 2D constellation, only its consequence.
    #
    # 0.12, which is `synth.HandPose.size`'s own default - documented there as a
    # hand at conversational distance from a 1280x720 camera, and the closest thing
    # this project has to a reference distance. So z reads 1.0 there, 2.0 at twice the
    # distance, 0.5 at half. To recalibrate: hold a hand where you want z = 1 and read
    # `h0_size`.
    zreference: float = 0.12
    # The palm triangle's area as a fraction of size squared, with the palm face on.
    # `tilt` is `acos` of the measured ratio over this, so it is the zero point of
    # the whole channel.
    #
    # MEASURED at 0.3059 against `synth.py`'s hand geometry - not against a real
    # hand, and the difference matters. The first value here was a guess of 0.14,
    # which put the ratio over 1.0 and clamped it: `tilt` read exactly 0 for the
    # first 62 degrees of real rotation, a dead zone big enough to make the channel
    # useless while looking like it worked.
    #
    # SET IT SLIGHTLY LOW ON PURPOSE. Too high and a face-on hand reads a few degrees
    # of tilt it does not have; too low and there is a small dead zone near zero. The
    # dead zone is the better failure, so this sits at the measured value rather than
    # above it. If a real face-on hand reads non-zero `tilt`, raise this.
    #
    # THE CONFOUND, and it is real: the triangle's corners are the wrist and two MCP
    # knuckles, and MEASURED on `synth.py` the ratio moves with finger SPREAD -
    # 0.1835 at spread 0.6, 0.4282 at 1.4. So spreading the fingers reads partly as
    # un-tilting the palm. A real hand's knuckles move far less than the
    # synthesiser's, so the true confound is smaller than that range suggests, but it
    # is not zero and nothing here corrects for it.
    palmarea: float = 0.3059


# Group names, matching the toggles in docs/ATTRIBUTES.md. `derive` computes only
# what is asked for, so a disabled group costs nothing here either.
ALL_GROUPS: Final = ("core", "presence", "contacts", "pose", "twohands",
                     "gestures", "descriptor", "depth", "tilt")

# The three joints whose triangle is the palm's plane. Wrist, index MCP and little
# MCP: the widest spread available, so its apparent area is the most sensitive
# measure of how far the palm has turned away from the camera.
PALM_TRIANGLE: Final = ("wrist", "index_mcp", "little_mcp")


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

    if "depth" in groups:
        # z from apparent SIZE, and that is the whole mechanism: a hand twice as far
        # away subtends half the angle, so `reference / size` rises with distance.
        #
        # WHAT IT IS NOT. Not metres, not calibrated, and not comparable between two
        # different hands - a child's hand at 40 cm and an adult's at 60 read alike.
        # It is monotonic in distance for ONE hand, which is what a "lean in to zoom"
        # interaction actually needs.
        put("z", params.zreference / view.size)
        # Per FINGER, and read the docstring on `_foreshorten` before using it: this
        # is how much a finger is pointing ALONG the camera axis rather than across
        # it, and it CANNOT tell toward from away.
        for finger in FINGER_NAMES:
            put("z_" + finger, _foreshorten(view, finger))

    if "tilt" in groups:
        # How far the palm has turned out of the image plane, and about which axis.
        # NOT pitch and yaw: a 2D projection cannot give those, because it cannot
        # tell a palm tilted toward the camera from one tilted away. Magnitude plus
        # axis is what the projection does determine, and it is two honest numbers
        # instead of two dishonest ones. See `_tilt`.
        magnitude, axis = _tilt(view, params)
        put("tilt", magnitude)
        put("tilt_axis", axis)

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


def _foreshorten(view: _HandView, finger: str) -> float:
    """How much `finger` points ALONG the camera axis rather than across it. 0..1.

    Contract: 0 means the finger lies in the image plane - fully across the camera,
              its apparent length equal to its true length. 1 means it points
              straight down the axis, its apparent length collapsed to nothing.
    THE AMBIGUITY, and it is in the projection rather than in this code: it CANNOT
              tell toward from away. A finger pointing at the camera and one pointing
              directly away project to the same foreshortened length. Anything gated
              on this has to accept both, or disambiguate some other way.
    Why:      a straight finger's tip-to-base distance divided by the sum of its bone
              lengths is 1 when it lies flat in the image and less when it turns out
              of the plane - the same ratio `curl` uses. So a CURLED finger reads
              foreshortened too, which is the second thing to know: this is only
              meaningful for a finger that is straight, and `curl_<finger>` is how a
              consumer tells the difference.
    UNMEASURED. The arithmetic is exact; whether the number is useful on a real hand
              at a real distance is not established, and nothing here is calibrated.
    """
    base, chain = FINGERS[finger]
    bones = sum(view._raw_distance(chain[i], chain[i + 1])
                for i in range(len(chain) - 1))
    if bones <= 0.0:
        return 0.0
    apparent = view._raw_distance(base, chain[-1])
    # 1 - (apparent / true) rises as the finger turns away from the image plane.
    return _clamp01(1.0 - apparent / bones)


def _tilt(view: _HandView, params: Params) -> tuple[float, float]:
    """The palm's rotation OUT of the image plane, as (degrees, axis degrees).

    Contract: the first value is 0 when the palm faces the camera square on and
              approaches 90 as it turns edge-on. The second is the direction of the
              axis it is leaning about, in the same convention as every other angle
              here - degrees, 0 = +x, counter-clockwise - and is meaningless when the
              magnitude is near zero, exactly as a direction is meaningless when speed
              is near zero.
    WHY NOT PITCH AND YAW, which is what was asked for. Every existing angle in this
              module comes from `atan2` on two image coordinates, so all of them are
              rotations about the camera's Z axis - in-plane. Recovering the other two
              from a 2D projection needs the one thing a projection destroys: which
              side of the image plane a point is on. A palm tilted 30 degrees toward
              the camera and one tilted 30 away project IDENTICALLY. So pitch and yaw
              cannot be signed, and publishing them unsigned under those names would
              be a lie about what they are. Magnitude and axis is what the projection
              actually determines.
    HOW. The palm triangle's apparent AREA shrinks as the cosine of the tilt, so
              `acos(area / area_face_on)` is the magnitude. The axis it is leaning
              about is the triangle's LONGEST remaining extent - the direction that
              did not foreshorten - so the axis is perpendicular to the direction that
              did.
    UNMEASURED, and `params.palmarea` is a GUESS. What should set it is one hand held
              square on to the camera with `h0_size` and the raw triangle area read
              off together. Until then `tilt` is monotonic in the right direction with
              an arbitrary zero.
    """
    a, b, c = PALM_TRIANGLE
    # Twice the signed triangle area, by the shoelace formula. Normalised by size
    # squared so it is scale-free and therefore depth-free, like every ratio here.
    area = abs((view.x[b] - view.x[a]) * (view.y[c] - view.y[a])
               - (view.x[c] - view.x[a]) * (view.y[b] - view.y[a])) / 2.0
    reference = params.palmarea * view.size * view.size
    if reference <= 0.0:
        return (0.0, 0.0)
    # acos of the area ratio. Clamped: a palm larger than the reference means the
    # reference is mis-calibrated, not that the tilt is imaginary.
    magnitude = math.degrees(math.acos(_clamp01(area / reference)))
    # The axis. The palm foreshortens ALONG the direction it is tilting, so the axis
    # it rotates about is the perpendicular - and the longest edge of the projected
    # triangle is the direction that did not foreshorten, which IS that axis.
    edges = ((a, b), (b, c), (a, c))
    longest = max(edges, key=lambda pair: view._raw_distance(*pair))
    return (magnitude, view.angle(*longest))


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
