"""Script CHOP callbacks: publish the latest landmarks as 137 channels.

    HOW TO USE IT. Create a Script CHOP, point its `callbacks` parameter at a
    Text DAT containing:

        from visionhands.td.hands_chop import cook_level, onCook   # noqa

        def onGetCookLevel(scriptOp):
            return cook_level(CookLevel)

    ...having run `visionhands.td.bootstrap.start()` once beforehand.
    `onGetCookLevel` has to be defined in the DAT rather than imported - see
    point 2 below, and `cook_level()`.

THREE THINGS ABOUT THIS FILE, all of which come from TouchDesigner rather than
from us.

1. IT MUST NEVER BLOCK. `onCook` runs on TD's main cooking thread, and
   everything on that thread is blocking - a Script CHOP that waits freezes the
   whole application (DESIGN.md 4.1). So this reads an already-computed frame
   from a lock-free box and writes numbers. It never waits for the camera, never
   takes a lock, and never calls into Vision. Reading the box costs ~0.005 ms
   (MEASURED, DESIGN.md 2.7).

2. THE COOK LEVEL IS NOT THE DEFAULT, AND `onGetCookLevel` CANNOT LIVE HERE.
   `CookLevel.AUTOMATIC` means "inputs changed and output being used". This CHOP
   has NO inputs - its data arrives on a background thread - so under the
   default it cooks ONCE and then serves a frozen frame forever while the camera
   runs perfectly. Measured in TD: `totalCooks = 1`.
   The trap is that `CookLevel` is injected into a DAT's globals only - it is
   not on `builtins` and not on the `td` module (verified live). So
   `onGetCookLevel` is defined in the callbacks DAT, which can see the name, and
   it calls `cook_level()` here for the decision. See that function.

3. CHANNELS ARE BUILT INSIDE `onCook` AND NOWHERE ELSE. TouchDesigner's own
   documentation is explicit that calling `appendChan()` from outside `onCook`
   "causes random behavior" unless the CHOP is locked. So the tempting
   optimisation - build 137 channels once, then only write values - is not
   available without locking the operator, and is not worth it: TD does this
   routinely, and the alternative is undefined behaviour.

THE CHANNEL CONTRACT IS FIXED AND NEVER VARIES. All 137 channels are present on
every cook, including before the camera has produced anything and including
after it dies. A CHOP whose channels appear and disappear breaks every
downstream reference in the project the moment they vanish, and breaks it
silently - the operator that referenced `h1_lm08_x` simply stops receiving
(DESIGN.md 6.2).

Ref: DESIGN.md 6.2 (the contract), 4.1, 2.7.
"""

from __future__ import annotations

from typing import Any

from visionhands.types import LandmarkFrame, blank_frame, channel_names

# What to publish when the engine has never started, or has been stopped. A
# module-level constant so the failure path allocates nothing.
_NO_DATA_FRAME = blank_frame()

# Sentinel age for "there is no source at all". Distinct from a large age, which
# means "there is a source and it has gone quiet" - a downstream gate wants to
# tell those apart, because one is a wiring mistake and the other is a dead
# camera. Negative is safe here precisely because a real age can never be
# (source.py clamps at zero).
AGE_NO_SOURCE_MS = -1.0


def _source() -> Any:
    """The running source, or None.

    Imported lazily so this module can be imported - and its channel list
    inspected - with no venv, no camera and no bootstrap having run.
    """
    from visionhands.td import bootstrap
    return bootstrap.source()


def channel_values(frame: LandmarkFrame, age_ms: float,
                   n_hands: int) -> list[float]:
    """The 137 values, in `channel_names()` order. Pure, and therefore testable.

    Contract: returns exactly len(channel_names()) floats in the same order as
              that list. The two are kept in step by a test, not by inspection
              (`test_hands_chop.py`), because a silent misalignment here would
              put a joint's y into another joint's x channel and still look
              plausible on screen.
    Why floats for everything: a CHOP channel is a float. `found` becomes 1.0 or
              0.0 and chirality becomes -1.0 / 0.0 / 1.0 rather than being
              encoded some cleverer way, because the far end is TD arithmetic.
    """
    values: list[float] = [float(n_hands), float(frame.seq), float(age_ms)]
    for hand in frame.hands:
        values.append(1.0 if hand.found else 0.0)
        # Vision's own confidence: a MEASURED constant 1.0, published as a
        # faithful mirror and never to be gated on (DESIGN.md 2.6).
        values.append(float(hand.confidence))
        # Ours, and the one to gate on.
        values.append(float(hand.conf_median))
        values.append(float(hand.chirality))
        for joint in hand.joints:
            # Normalised, bottom-left origin, exactly as Vision produced them.
            # No flip happens here or anywhere else (DESIGN.md 7).
            values.append(float(joint.x))
            values.append(float(joint.y))
            values.append(float(joint.conf))
    return values


def read_state() -> tuple[LandmarkFrame, float, int]:
    """Fetch what to publish. NEVER blocks, never raises.

    Returns (frame, age_ms, n_hands).

    Why it cannot raise: this runs inside a cook. An exception here shows up as
    a broken operator in the project, and the useful behaviour when the source
    is missing or misbehaving is to publish a full set of zeros with a telling
    `age_ms` - so downstream keeps its channel references and can see that
    something is wrong - rather than to break the CHOP.
    """
    try:
        source = _source()
    except Exception:                       # noqa: BLE001 - see docstring
        # bootstrap itself failed to import. Nothing to publish but zeros.
        return _NO_DATA_FRAME, AGE_NO_SOURCE_MS, 0
    if source is None:
        return _NO_DATA_FRAME, AGE_NO_SOURCE_MS, 0
    try:
        frame = source.latest()
        age_ms = source.age_ms()
    except Exception:                       # noqa: BLE001 - see docstring
        return _NO_DATA_FRAME, AGE_NO_SOURCE_MS, 0
    n_hands = sum(1 for hand in frame.hands if hand.found)
    return frame, age_ms, n_hands


def write_channels(script_op: Any) -> int:
    """Write the fixed channel set onto a Script CHOP. Returns the channel count.

    Split out from `onCook` so it can be driven by a test double with no
    TouchDesigner present - see `test_hands_chop.py`. The only TD API used is
    `clear()`, `appendChan(name)`, `numSamples` and `rate`, all of which the
    double implements.

    Thread: TD's main thread, inside a cook, 60 times a second.
    """
    frame, age_ms, n_hands = read_state()
    names = channel_names()
    values = channel_values(frame, age_ms, n_hands)

    script_op.clear()
    # One sample per channel: this is a snapshot of the most recent frame, not a
    # time-sliced signal. TD's own examples set numSamples before appending.
    script_op.numSamples = 1
    for name, value in zip(names, values, strict=True):
        # strict=True on the zip: if the names and the values ever drift out of
        # step, fail loudly here rather than silently writing joint 5's y into
        # joint 5's conf and looking almost right.
        channel = script_op.appendChan(name)
        # chan[0] = v, which is the idiom TD's shipped examples use, and which
        # avoids allocating a one-element list per channel per cook.
        channel[0] = value
    return len(names)


def cook_level(cook_level_enum: Any) -> Any:
    """Which cook level this CHOP needs. The DAT passes TD's CookLevel enum in.

    WHY THE ENUM IS AN ARGUMENT rather than something this module looks up.
    VERIFIED LIVE against the running TouchDesigner (099, Python 3.11.15):

        import td; td.CookLevel   ->  MISSING
        builtins.CookLevel        ->  absent
        builtins.op               ->  absent
        td.op                     ->  present

    TouchDesigner injects `CookLevel` into a DAT's globals ONLY. A function
    living in an imported module cannot see it by any route. The first version
    of this file looked it up on `builtins`, found nothing, and returned None -
    at which point TD fell back to `AUTOMATIC`, and since this CHOP has no
    inputs to change, it cooked exactly ONCE and then served a frozen frame
    forever while the camera ran perfectly. Measured in TD: `totalCooks = 1`.

    So the lookup happens in the callbacks DAT, where the name exists, and the
    DECISION lives here, where it is version-controlled and testable.

    ALWAYS, not WHEN_USED: this is a device input, like a camera or MIDI
    operator, and it should track the camera whether or not anything is
    connected downstream yet. WHEN_USED means "every frame while the output is
    being used", which leaves the CHOP frozen until a consumer exists - correct
    in production, actively confusing while building, and indistinguishable from
    the bug above. A cook costs ~55 microseconds (MEASURED), so cooking
    unwatched is not worth optimising.
    """
    return cook_level_enum.ALWAYS


def onCook(scriptOp: Any) -> None:
    """Called by TouchDesigner every frame, per cook_level()."""
    write_channels(scriptOp)
