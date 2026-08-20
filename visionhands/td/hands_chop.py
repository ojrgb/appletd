"""Script CHOP callbacks: publish the latest landmarks as 137 channels.

    HOW TO USE IT. Create a Script CHOP, point its `callbacks` parameter at a
    Text DAT containing:

        from visionhands.td.hands_chop import onCook, onGetCookLevel   # noqa

    ...having run `visionhands.td.bootstrap.start()` once beforehand.

THREE THINGS ABOUT THIS FILE, all of which come from TouchDesigner rather than
from us.

1. IT MUST NEVER BLOCK. `onCook` runs on TD's main cooking thread, and
   everything on that thread is blocking - a Script CHOP that waits freezes the
   whole application (DESIGN.md 4.1). So this reads an already-computed frame
   from a lock-free box and writes numbers. It never waits for the camera, never
   takes a lock, and never calls into Vision. Reading the box costs ~0.005 ms
   (MEASURED, DESIGN.md 2.7).

2. THE COOK LEVEL IS NOT THE DEFAULT, AND THAT IS LOAD-BEARING.
   `CookLevel.AUTOMATIC` means "inputs changed and output being used". This CHOP
   has NO inputs - its data arrives on a background thread - so under the
   default it would cook once and then sit there serving a stale frame forever
   while the camera ran. `WHEN_USED` means "every frame while the output is
   being used", which is exactly right: track the camera when something
   downstream needs the landmarks, and cost nothing when nothing does.

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


def _td_builtins() -> Any:
    """Where TouchDesigner injects its API.

    Indirected through a function so a test can substitute it, and so the lookup
    happens at call time - this module has to stay importable outside TD, where
    none of these names exist.
    """
    import builtins
    return builtins


# ---------------------------------------------------------------------------
# The TouchDesigner callbacks.
#
# camelCase, including the argument names: TouchDesigner calls these by name and
# passes `scriptOp` positionally. Renaming either to satisfy a naming linter
# would silently stop TD finding them - pep8-naming is not among our enabled
# ruff rules, and if anyone adds it, this file wants an exemption rather than a
# fix.
# ---------------------------------------------------------------------------
def onCook(scriptOp: Any) -> None:
    """Called by TouchDesigner every frame the output is used."""
    write_channels(scriptOp)


def onGetCookLevel(scriptOp: Any) -> Any:
    """Cook every frame the output is used, not only when inputs change.

    See this module's docstring, point 2: with the default `AUTOMATIC` this CHOP
    would cook once and then serve a frozen frame forever, because it has no
    inputs to change - the camera delivers on a background thread.

    `CookLevel` is injected by TouchDesigner, so it is resolved at call time
    rather than imported. Outside TD it does not exist, and returning None lets
    TD's own default apply rather than raising inside a callback, which would
    leave the operator broken with an error pointing at the wrong thing.
    """
    cook_level = getattr(_td_builtins(), "CookLevel", None)
    if cook_level is None:
        return None
    return cook_level.WHEN_USED
