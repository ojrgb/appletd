# Finishing the attribute layer

State at handoff: `derive()` is built, tested (36 tests) and running in
TouchDesigner at 0.210 ms for 171 channels. What remains is the temporal half,
the gating, and a dev tool. The design is settled in `docs/ATTRIBUTES.md`; this
file is the implementation recipe plus the TouchDesigner facts that cost real time
to learn.

## Step 1 — the proximity latches — DONE 2026-08-20

`tools/td_add_latches.py` builds it, `tools/td_verify_latches.py` judges it, and
`docs/JOURNAL.md` records how. Delivered `h{i}_pinching`, `h{i}_snapping`,
`hands_together`, every edge pulse (so `e_clap` and `e_snap`), `both_pinching` and
three fire counters. COMP output 476 → 497.

Built as ONE five-channel-wide bank rather than the three duplicated base COMPs
this file originally specified — every distance, validity flag and threshold is
renamed to the same five working names, so one set of operators serves all five
latches and the name-alignment problem disappears at the source rather than being
worked around downstream.

Verified by counter deltas over generated sweeps, with the sidecar stopped and
nobody in frame: `ramp` +4 for four sweeps, `deadband` **+1** for six dips (the
hysteresis), `clap` +3, `both` +3 on all four per-hand counters, `absent` +0
everywhere.

**READ THIS BEFORE BUILDING STEP 2.** A branch with memory must be clocked by
TIME, not by data: when the sender stops, nothing downstream cooks, so anything
that should react to the absence of data never gets the chance — including the
liveness detector whose whole job that is. Measured, and `DESIGN.md` 2.10 has it,
including the wrong explanation I believed first and why the wrong one still
"worked". Every group below needs a Null CHOP with Cook Type = `always`, or needs
to be provably downstream of one. `viewer = True` does not work and neither does
an ordinary Null.

**Two things step 2 owes the latches**, both marked `TODO` in
`tools/td_add_latches.py`:

- `h{i}_active` — `valid` debounced over `Activateframes`/`Deactivateframes` — is
  what `docs/ATTRIBUTES.md` says should gate the latches. They currently gate on
  raw per-frame validity, so one low-confidence frame mid-gesture fires a spurious
  end-then-start pair and bumps the counter. AND it into `lat_valid`, where
  liveness already is. Write a `glitch` sequence with it (a held pinch with one
  low-confidence frame, expecting +1 rather than +2) - no existing sweep perturbs
  confidence, so nothing would catch a regression.
- Step 3's `allowCooking` group gating does not run a Cook Type = Always operator
  inside a disabled COMP, so a disabled group would freeze its latches and resume
  with a stale `prev`. Decide where the clock lives before building the group
  COMPs; it probably has to sit outside any gated group.

`DESIGN.md` 2.11 lists the seven other native-operator behaviours that cost time
here — the Logic CHOP's `convert` being a comparator, its `match` defaulting to
index, the Feedback CHOP emitting nothing without its second input, the Count
CHOP's action menus that look like toggles, Merge connectors that insert rather
than fill, `appendFloat` zeroing an existing value, and the MCP bridge running a
script twice. Do not rediscover them.

## Step 2 — the temporal channels — PARTLY DONE 2026-08-20

`tools/td_add_temporal.py` builds them. **Done:** `h{i}_active` (a real debounce),
`h{i}_entering`, `h{i}_exiting`, `h{i}_held`, `both_active`, `n_active`,
`h{i}_vel_x/_y`, `h{i}_speed`, `h{i}_accel`, `hands_approach`. The latch bank now
gates on the debounced `active` as `docs/ATTRIBUTES.md` always specified.

The debounce turned out to be the same Schmitt trigger as the latches, with two
window statistics in place of two thresholds - `min(valid)` over `Activateframes`
to turn on, `max(valid)` over `Deactivateframes` to hold. Worth knowing before
building the rest, because several of the remaining channels are also that shape.

**READ `DESIGN.md` 2.11 BEFORE BUILDING ANY MORE OF THIS.** Two operators that
look exactly right are inert here: every CHOP in this network carries one sample
per frame, so the **Slope CHOP** (velocities of 12.5 on a 0..1 coordinate) and the
**Filter CHOP** (all nine types returning the identical value) do nothing.
Differentiate with `(x - x_prev) * rate`; smooth with Trail + Analyze. And a
derivative must be *averaged* rather than smoothed to be correct at all, because
positions update at the camera rate and are differentiated at the cook rate.

### Still to build, and what each needs

| channel | what it needs |
|---|---|
| `h{i}_dir` | atan2 of the velocity pair. No native operator does atan2, so either an Expression CHOP per hand or a small stateless Script CHOP - and then a Hold gated on `Speedfloor`, so it stops spinning on noise |
| `h{i}_steadiness` | Trail over `Steadywindow` into Analyze, inverted. The Trail+Analyze pattern the debounce already uses |
| `h{i}_dwell` | a Hold of the settled position, a radius test against it, and an accumulator gated on the result - the `held` accumulator is the model |
| `hands_scale` | a Hold latched by the `Scalereset` pulse, as the reference distance |
| `hands_twist` | the derivative of an ANGLE, which wraps at +/-180 and spikes if differentiated naively. Publish `sin`/`cos` of `hands_angle` from `derive()` - a stateless addition - and use `cos*d(sin) - sin*d(cos)`, which is wrap-free |
| `h{i}_e_grab`, `h{i}_e_release` | these are LATCHES on `openness` with `Grabon`/`Graboff`. Two more rows in `tools/td_add_latches.py`, not new machinery |
| swipe / wave / cross / twist events | thresholds plus a Count-based refractory. The sequences to test them with: `swipe` exists; wave and cross need writing |

Add a sequence per group as you go. `sequences.py`'s `swipe` was written for the
velocity work and immediately earned it: the pinch ramps oscillate a fingertip,
and a smoothed signed velocity of an oscillation averages toward zero - 0.037
units/s against 1.43 for a traverse - so the motion thresholds would have had
nothing to fire on.

## Step 3 — groups and parameters — PARTLY DONE 2026-08-21

`tools/td_add_groups.py` builds the Attributes page: a `Verbosity` menu with the
three presets and a toggle per group.

**What works, and it is the part that matters for compute.** The toggles reach
`derive()` through the `_groups()` reader that has been in
`tools/td_add_derive.py` all along - it just fell back to "all enabled" because the
parameters did not exist. Measured: `Verbosity = Minimal` takes `derive_chop` from
**171 channels to 0** and the COMP output from 561 to 393. That is the one operator
in the COMP where per-channel cost is real, so the compute worth saving is saved.

**The `allowCooking` question is answered by measurement.** A Feedback counter and
a Cook Type = Always Null inside a base COMP, `allowCooking` set False: the counter
**froze** and its cook count stopped, and on re-enabling it resumed at the next
value rather than jumping. So the mechanism works, disabling a group stops all of
its cost including its clock, and a group with memory PAUSES rather than resetting
- which means re-enabling one resumes with a `prev` from whenever it was switched
off. For the latches that is a stale state for one frame (self-correcting, since
the recurrence is level-driven) and possibly one wrong edge pulse across the gap.
Bounded and acceptable - and it is why the clock must sit OUTSIDE any gated group.

**Two things deliberately not done, with the reasons.**

*Wrapping the native groups in base COMPs.* The mechanism is proven, but it is
~150 operators of restructuring to save native CHOP cost on 1-sample inputs, which
is negligible next to the Python cook the toggles already gate. Worth doing when
the groups need to be relocatable for some other reason, not for the cost.

*Trimming the output channel list for the native groups.* A disabled DERIVE group's
channels genuinely vanish; the latch, trigger and motion channels keep their last
value, because nothing gates them. `docs/ATTRIBUTES.md` suggests a Select whose
`channames` is assembled from the enabled groups, and **wildcard patterns cannot
partition these channels**: `h?_*_x` matches both the raw `h0_wrist_x` (Landmarks)
and the derived `h0_palm_x` (Core), and `h?_e_*` matches both the Events pulses and
the Triggers pulses. It needs an exact channel-to-group map.

The sound way to get one, when it is time: a registry in the package. `derive()`
can already report a group's channels by being called with just that group, so the
derive side is free; the latch table moves from `tools/td_add_latches.py` into
`visionhands/` so both the builder and the registry import it. That also fixes a
smell noted in the journal - liveness is a property of the stream and currently
lives in the latch builder, which two other builders now reach into.

## Step 4 — `tools/send_synthetic.py` — DONE 2026-08-20

`visionhands/sequences.py` holds the sweeps as pure functions of a phase (tested,
23 tests, no sockets); `tools/send_synthetic.py` is the CLI that puts them on the
wire. Sequences: `ramp`, `snap`, `clap`, `both`, `deadband`, `open`, `absent`.

`deadband` is the one worth understanding: it dips below `on` and rises only into
the dead band, never past `off`, so a correct latch engages ONCE however many
cycles are sent and a comparator engages every time. One number separates them.

Add a sequence per temporal group as step 2 proceeds — a swipe needs a palm
crossing frame at a known speed, a dwell needs a hand held still. The pattern is
established; the instrument is the cheap part and it is what makes each channel
assertable rather than plausible.

## TouchDesigner facts that cost time to learn

- `CookLevel` exists ONLY in a DAT's globals. Not `builtins`, not the `td` module.
  A callback in an imported module cannot see it, returns None, and TD silently
  falls back to AUTOMATIC - which for an input-less CHOP means it cooks once and
  freezes. `td.noiseTOP` and friends ARE on the `td` module; `CookLevel` is not.
- `appendChan()` outside `onCook` "causes random behavior" unless the operator is
  locked. Same for `copyNumpyArray`.
- A DAT to CHOP takes its DAT by PARAMETER (`par.dat`), not by a wire - it has no
  CHOP input connector, so `inputConnectors[0]` raises IndexError.
- Rename CHOP supports wildcard back-references (`*_lm08_*` to `*_index_tip_*`)
  but a space-separated From/To list is NOT pairwise rules: the first replacement
  is applied to everything the whole From list matches.
- Creating a Script CHOP auto-creates a docked callbacks DAT. Destroy it if you
  point `par.callbacks` elsewhere.
- `par.pulse()` is ASYNCHRONOUS. Reading a parameter the callback sets, on the
  next line, returns the old value.
- `chop.chan('name')` returns a Channel whose TRUTHINESS IS ITS VALUE, so
  `x if chop.chan('n_hands') else -1` falls through whenever the channel reads 0.
  Use `is not None`.
- `appendInt`/`appendFloat` replace a parameter's attributes on rebuild. Set
  `.default` always, `.val` only when creating, or a rebuild resets the user's
  tuning.
- Ortho Width is the visible WIDTH, and the vertical extent comes from the
  RENDER's resolution, not the source image's. Two different aspects.
- Nothing may touch a TD object from any thread but the main one. TD raises
  THREAD CONFLICT and warns it may terminate. Diagnostics write to a file.
- `import td` works from an imported module and gives `op`, OP types, and most of
  the API - but not the injected-only names.

## What still needs the user

Threshold *feel*: `Snapon`, `Togetheron`, `Swipespeed`, `Grabon/off`, and the
peace-sign spread test are calibrated against synthetic geometry. That validates
the arithmetic and says nothing about how a real hand sits. One session with a
person in frame, adjusting numbers on the Tuning page - not a rebuild.

Also outstanding from earlier milestones: slot assignment (M5, needs a two-hand
fixture), tests for `Sidecar.run()` (five mutations survive a green suite), and
per-joint jitter is still unmeasured, which is what should set `Velocityfilter`.
