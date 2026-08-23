"""Turning Depth Anything's relative output into metres, with points of known distance.

THE PROBLEM THIS SOLVES, and it is not a nicety. Depth Anything V2's Core ML graph
ends in a `reduce_max`: the output is divided by the frame's own maximum, and the
maximum inverse-depth pixel is the NEAREST thing in shot. So a hand coming toward the
lens darkens every other pixel in the frame. That is not the model changing its mind,
it is division - and it means nothing the model outputs is comparable between frames.
Anything driven off it has to be scale-invariant, or pinned.

Pinning works because the corruption is AFFINE. Give it points in the room whose real
distance you know and every frame you can solve

    1 / Z  =  alpha * d  +  beta            d = this frame's raw output

for alpha and beta, then read metres anywhere as `Z = 1 / (alpha*d + beta)`. Two
unknowns, so two pins minimum, at DIFFERENT distances.

RE-SOLVED EVERY FRAME, and caching it would defeat the whole thing: the quantity
being cancelled changes every frame, so a cached alpha/beta is exactly the stale-scale
problem the correction exists to remove, wearing the costume of a fix.

WHAT THIS MODULE IS NOT. It does not import Vision, Core ML, TouchDesigner or
`maskbuf`. It is arithmetic over a numpy array and a list of pins, which is why it can
be tested exhaustively with no model, no camera and no frameworks - and it is where
every number that reaches a user comes from. `appletd/depth.py` owns the model,
`appletd/sidecar.py` owns the transport.

numpy IS imported at module scope here, unlike everywhere else in the package. The
rule it breaks is real - `appletd/__init__.py` promises the core layer needs no
numpy - so this module is deliberately NOT part of that layer: nothing in the OSC or
CHOP path imports it, only the depth stream does, and the depth stream already needs
numpy to read a pixel buffer at all.

CAVEATS WORTH KNOWING BEFORE TRUSTING A NUMBER OFF THIS - all of them from the
reference implementation in apple-vision-examples, kept because they are the honest
limits rather than because they are tidy:

  * A pin someone walks in front of reports their chest and poisons the solve. With
    three or more, the worst-fitting pin is dropped when it disagrees by more than
    `drop_m`, which is why three is the number to use and not two.
  * The residual is the part of the drift that is NOT affine, and no amount of
    pinning removes it. It is the number that decides whether this is good enough for
    whatever you are driving.
  * The camera must not move. If it does, every pin is silently wrong.
  * Keep pins at middling distances rather than on the far wall. 0.6 m to 2 m already
    spans most of the useful inverse-depth range, and the far wall lands in very few
    levels once something near has set the frame maximum.

Ref: design/DESIGN.md 2.22, docs/DEPTH.md,
     apple-vision-examples/examples/depth/depth.py (where the maths came from).
"""

from __future__ import annotations

import struct
from typing import Any, Final, NamedTuple

import numpy
import numpy.typing

# Half-width of the patch sampled at each pin. The median of a patch rather than the
# pixel under the crosshair: the map is a ViT output upsampled to frame size, and one
# pixel of it twitches enough to move the fit for reasons that are not the scene.
PATCH: Final = 3

# Pins must span this fraction of the frame's OWN range to be worth fitting through.
# Conditioning is set by the SPREAD of the readings, not by how many there are:
# fitting a slope through values that all sit within a hair of each other amplifies
# their noise without limit. Expressed as a fraction so one threshold works whatever
# units the pipeline is in.
MIN_SPREAD_FRAC: Final = 0.05

# Where the reciprocal is clamped, in metres. Not cosmetic: `d` values outside the
# range the pins covered can drive `alpha*d + beta` through zero, and one infinity in
# the map poisons everything downstream that touches it.
NEAR_CLAMP_M: Final = 0.05
FAR_CLAMP_M: Final = 60.0

# Pins that let it run with no configuration. Placed at the edges and the bottom
# because that is where a person in front of a webcam usually is not, and the
# distances are a plausible room rather than a measurement - they are a starting
# point to dial in, and `docs/DEPTH.md` says so in those words.
# Defined AFTER `Pin` below, so these are real pins rather than bare tuples - which
# is not pedantry: `sample()` reads `.x`, and a tuple that quacks nearly-right fails
# at the first frame rather than at import.
DEFAULT_PIN_SPEC: Final = "0.06,0.30,2.5 0.94,0.30,2.4 0.50,0.96,1.1"

# The aux block `maskbuf` carries for us, packed here because this is the module that
# knows what the numbers mean. Little-endian and spelled out, like the header itself:
# the writer and the reader are different processes.
#
#   alpha, beta        the fit. 0, 0 means "no usable fit this frame"
#   used               how many pins the fit actually used
#   dropped            1-based index of the pin that was dropped, 0 for none
#   worst_residual_m   the largest disagreement among the pins that WERE used. Only
#                      meaningful when `used` >= 3 - see Solve.residual_is_a_check
#   frame_range        the spread of the raw map, for the conditioning note
_AUX: Final = struct.Struct("<ffIIff")
assert _AUX.size <= 32, "the aux block must fit maskbuf.AUX_BYTES"


class Pin(NamedTuple):
    """A point of known distance. `x`/`y` normalised over the frame, 0 to 1."""

    x: float
    y: float
    metres: float


class Solve(NamedTuple):
    """One frame's fit. `alpha is None` means there is no usable one.

    `note` is for a human: it says why there is no fit, which is the difference
    between "nothing is wrong, you gave me two pins at the same distance" and a bug.
    """

    alpha: float | None
    beta: float | None
    used: tuple[int, ...]               # indices of the pins the fit used
    dropped: int | None                 # index of the pin thrown out, if any
    residuals_m: tuple[float, ...]      # metres of error, per used pin, same order
    frame_range: float                  # spread of the raw map this frame
    note: str

    @property
    def ok(self) -> bool:
        return self.alpha is not None

    @property
    def worst_residual_m(self) -> float:
        """The largest disagreement among the pins used. 0.0 with no fit.

        THE number to gate on, BUT ONLY IF `residual_is_a_check` - see below.
        """
        return max((abs(r) for r in self.residuals_m), default=0.0)

    @property
    def residual_is_a_check(self) -> bool:
        """Whether the residual means anything. False with fewer than three pins.

        WHY THIS EXISTS, and it is not pedantry - it was caught on the first real run:
        two points ALWAYS fit a line exactly, so with two pins the residual is
        identically 0.000 and reads as a perfect fit when nothing has been checked at
        all. That is precisely the plausible-wrong-number this project keeps finding,
        and a `0.000 m` on a panel is the most convincing version of it.

        Three pins is the smallest number where the fit can disagree with itself, and
        it is also the number that survives one pin being walked in front of. So:
        three is the minimum for a residual, two is the minimum for a fit.
        """
        return len(self.used) >= 3

    def pack(self) -> bytes:
        """The aux block for `maskbuf`, so the fit travels WITH the frame it describes."""
        return _AUX.pack(
            0.0 if self.alpha is None else self.alpha,
            0.0 if self.beta is None else self.beta,
            len(self.used),
            0 if self.dropped is None else self.dropped + 1,
            self.worst_residual_m,
            self.frame_range)


def unpack(aux: bytes) -> dict[str, float]:
    """The aux block, read back. `{}` if it is absent or too short.

    A dict rather than a `Solve`, deliberately: the reader gets what was PUBLISHED,
    not a reconstructed object that might imply it also has the pin list and the
    residual per pin. It has six numbers, and pretending otherwise would be the kind
    of tidy lie this project keeps finding.
    """
    if len(aux) < _AUX.size:
        return {}
    alpha, beta, used, dropped, worst, frame_range = _AUX.unpack(aux[:_AUX.size])
    # `alpha == beta == 0` is the no-fit sentinel, NOT an all-zero block. A refused
    # solve still reports the frame's range, so the block is not all zeros - checking
    # for that instead handed back a dict claiming `alpha = 0`, which is a fit putting
    # every pixel at the same distance. Caught by a test, and it is exactly the class
    # of plausible-wrong-number this module exists to avoid.
    if alpha == 0.0 and beta == 0.0:
        return {}
    return {"alpha": alpha, "beta": beta, "used": float(used),
            "dropped": float(dropped), "worst_residual_m": worst,
            "frame_range": frame_range,
            # Derived, not transmitted, because it is a property of `used` and a
            # consumer that had to remember the rule would forget it. 0.0/1.0 rather
            # than a bool so every value in here is a number a CHOP can carry.
            "residual_is_a_check": 1.0 if used >= 3 else 0.0}


def parse_pins(text: str) -> tuple[Pin, ...]:
    """`"0.06,0.3,2.5 0.94,0.3,2.4"` -> pins. Raises with a readable complaint.

    Contract: validated HERE rather than at first use, because a typo in a pin is a
              wrong number on screen all session rather than a crash. It has to fail
              at startup or not at all.
    Why one string of triples rather than a repeated flag: this arrives from a
              TouchDesigner parameter, which is one text field. The same string works
              on the command line.
    """
    pins = []
    for spec in text.replace(";", " ").split():
        parts = spec.split(",")
        if len(parts) != 3:
            raise ValueError("a pin is X,Y,METRES - got %r" % spec)
        try:
            x, y, metres = (float(value) for value in parts)
        except ValueError:
            raise ValueError("pin %r has a value that is not a number" % spec) from None
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("pin %r: x and y are normalised, 0 to 1" % spec)
        if metres <= 0.0:
            raise ValueError("pin %r: metres must be positive" % spec)
        pins.append(Pin(x, y, metres))
    return tuple(pins)


def format_pins(pins: tuple[Pin, ...]) -> str:
    """The inverse of `parse_pins`, for building a command line or filling a field."""
    return " ".join("%g,%g,%g" % (p.x, p.y, p.metres) for p in pins)


def sample(depth: numpy.typing.NDArray[Any], pins: tuple[Pin, ...]) -> list[float]:
    """The median raw value at each pin, in the MODEL's own units.

    No division by 255 and no unit assumption: `depth` is whatever the pipeline
    produced - fp16 straight from the model - and the fit is affine, so it does not
    care about the scale. What it must NOT have is a unit assumption baked in here,
    because that is what silently halves the precision.
    """
    height, width = depth.shape[:2]
    out = []
    for pin in pins:
        cx = min(max(int(pin.x * (width - 1)), PATCH), width - 1 - PATCH)
        cy = min(max(int(pin.y * (height - 1)), PATCH), height - 1 - PATCH)
        patch = depth[cy - PATCH:cy + PATCH + 1, cx - PATCH:cx + PATCH + 1]
        out.append(float(numpy.median(patch)))
    return out


def solve(depth: numpy.typing.NDArray[Any], pins: tuple[Pin, ...],
          drop_m: float = 0.5) -> Solve:
    """Fit `1/Z = alpha*d + beta` on THIS frame.

    Contract: never raises and never returns a fit it does not believe. Every refusal
              carries a `note` saying which one it was, because "no metric depth" has
              several causes and they need different fixes.
    """
    if not depth.size:
        # BEFORE sampling. A 0x0 array makes `numpy.median` return nan, and a nan
        # reaches `polyfit` as an illegal LAPACK argument printed straight to stderr -
        # not an exception, a line of Fortran diagnostics in the sidecar's log.
        return Solve(None, None, (), None, (), 0.0, "the frame is empty")
    frame_range = _range(depth)
    if len(pins) < 2:
        return Solve(None, None, (), None, (), frame_range,
                     "needs 2 pins at different distances, have %d" % len(pins))

    readings = sample(depth, pins)
    indices = tuple(range(len(pins)))
    fit = _fit(readings, pins, indices, frame_range)
    dropped = None

    # With three or more we can afford to distrust one. A pin someone has walked in
    # front of reads their chest, which is a large residual and a badly wrong solve
    # for the WHOLE frame; dropping the worst offender is the cheapest defence that
    # does not need an occlusion detector.
    if fit is not None and len(indices) >= 3:
        alpha, beta, residuals = fit
        worst = max(range(len(indices)), key=lambda k: abs(residuals[k]))
        if abs(residuals[worst]) > drop_m:
            dropped = indices[worst]
            indices = tuple(i for i in indices if i != dropped)
            fit = _fit([readings[i] for i in indices], pins, indices, frame_range)

    if fit is None:
        return Solve(None, None, (), dropped, (), frame_range,
                     _refusal(readings, pins, frame_range))
    alpha, beta, residuals = fit
    note = "%d pins" % len(indices)
    if dropped is not None:
        note += ", dropped #%d" % (dropped + 1)
    if len(indices) < 3:
        # Said out loud on every such fit, because the residual next to it reads 0.000
        # and looks like the best possible news.
        note += " - residual is NOT a check, two points always fit a line exactly"
    return Solve(alpha, beta, indices, dropped, tuple(residuals), frame_range, note)


# How coarsely the frame's range is sampled. MEASURED, and it was 88% of the entire
# solve: numpy has no fast path for fp16 reductions, so `max()`/`min()` on a 518x392
# half-float map costs 0.5123 ms against 0.0170 for the same array as float32 - THIRTY
# times slower. Every 4th pixel in each direction is 0.0415 ms.
#
# Legitimate because of what the number is FOR. `frame_range` is the scale the
# conditioning test is expressed against - "do the pins span at least 5% of the range" -
# so it is a rough magnitude, not a measurement. And the model's output is a smooth
# upsampled ViT map where adjacent pixels are highly correlated: MEASURED at 0.00%
# difference in the range at every 4th pixel, and 0.02% at every 8th.
#
# The whole solve goes from 0.589 ms to about 0.12 on the capture thread, next to 23 ms
# of inference. Small in proportion and free to have.
RANGE_STEP: Final = 4
# Below this, sample every pixel. A 32x32 synthetic frame subsampled 4x is 8x8, where
# the correlation argument above stops holding - and the tests use small frames.
RANGE_MIN_SIDE: Final = 64


def _range(depth: numpy.typing.NDArray[Any]) -> float:
    """The spread of the map, from a subsample on anything big enough. See RANGE_STEP."""
    view = depth
    if min(depth.shape[:2]) >= RANGE_MIN_SIDE:
        view = depth[::RANGE_STEP, ::RANGE_STEP]
    return max(float(view.max()) - float(view.min()), 1e-9)


def _refusal(readings: list[float], pins: tuple[Pin, ...],
             frame_range: float) -> str:
    """Why `_fit` said no, in words. Recomputed rather than threaded out of `_fit`,
    because it is only ever wanted on the failure path and threading it would put a
    diagnostic string in the hot return value."""
    spread = max(readings) - min(readings) if readings else 0.0
    if spread < MIN_SPREAD_FRAC * frame_range:
        return ("pins too close in depth (%.1f%% of the frame's range, needs %.0f%%) "
                "- move one nearer or further"
                % (100.0 * spread / max(frame_range, 1e-9), 100.0 * MIN_SPREAD_FRAC))
    return "fit went non-physical - a pin ended up behind the camera"


def _fit(readings: list[float], pins: tuple[Pin, ...], indices: tuple[int, ...],
         frame_range: float) -> tuple[float, float, list[float]] | None:
    """Least squares over (d, 1/Z). -> `(alpha, beta, metre error per pin)` or None."""
    if len(readings) < 2:
        return None
    d = numpy.asarray(readings, dtype=numpy.float64)
    inverse = numpy.asarray([1.0 / pins[i].metres for i in indices],
                            dtype=numpy.float64)
    if float(d.max() - d.min()) < MIN_SPREAD_FRAC * frame_range:
        return None
    alpha, beta = numpy.polyfit(d, inverse, 1)
    predicted = alpha * d + beta
    if numpy.any(predicted <= 0.0):
        # A fit that puts a pin behind the camera. Refused rather than clamped: the
        # clamp would hide it and every reading off the frame would be wrong.
        return None
    return (float(alpha), float(beta),
            [1.0 / p - 1.0 / i for p, i in zip(predicted, inverse, strict=True)])


def metres(depth: numpy.typing.NDArray[Any],
           fit: Solve) -> numpy.typing.NDArray[Any] | None:
    """Whole-frame metric depth, or None with no usable fit.

    Clamped rather than left to explode - see NEAR_CLAMP_M. float32 out, because
    metres past a few tens of a metre stop being representable usefully in fp16 and
    this is the array somebody will do arithmetic on.
    """
    if fit.alpha is None or fit.beta is None:
        return None
    inverse = fit.alpha * depth.astype(numpy.float32) + fit.beta
    # float32 out, explicitly. `alpha` and `beta` are Python floats, so the
    # multiplication promotes to float64 and the reciprocal would hand back an array
    # twice the size of the one that went in - which for a 518x392 map is 800 KB
    # crossing a process boundary for no reason.
    clamped = numpy.clip(inverse, 1.0 / FAR_CLAMP_M, 1.0 / NEAR_CLAMP_M)
    return (1.0 / clamped).astype(numpy.float32)


# Parsed rather than written out as literals, so the default and a user's own string
# go through exactly the same validation. A default that skipped it would be the one
# pin list nobody had checked.
DEFAULT_PINS: Final[tuple[Pin, ...]] = parse_pins(DEFAULT_PIN_SPEC)
