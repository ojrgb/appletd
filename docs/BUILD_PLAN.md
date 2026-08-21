# Finishing the attribute layer

State at handoff: `derive()` is built, tested (36 tests) and running in
TouchDesigner at 0.210 ms for 171 channels. What remains is the temporal half,
the gating, and a dev tool. The design is settled in `docs/ATTRIBUTES.md`; this
file is the implementation recipe plus the TouchDesigner facts that cost real time
to learn.

## Step 1 — the proximity latches (highest value; do first)

Delivers `pinching`, `snapping`, `hands_together` and their edge pulses, which is
`clap` and `snap`. Three instances of one sub-network, differing only in the
channel they watch and the two thresholds.

The recurrence, from `docs/ATTRIBUTES.md`:

    state = valid AND ( below_on OR (prev AND NOT above_off) )

Operator chain per instance, built inside a small base COMP so it can be
duplicated:

| op | type | notes |
|---|---|---|
| `sel_d` | Select | the distance channel(s), e.g. `h*_pinch_index` |
| `below_on` | Expression | `1 if me.inputVal < parent().par.Pinchon else 0` |
| `above_off` | Expression | `1 if me.inputVal > parent().par.Pinchoff else 0` |
| `fb` | Feedback | Target CHOP = `state`. This is what breaks the loop |
| `held` | Logic | inputs `fb` + `above_off`; chopop AND, with `above_off` inverted via `preop` |
| `state` | Logic | inputs `below_on` + `held`; chopop OR, then AND with validity |
| `start` | Logic | inputs `state` + `fb`; state AND NOT prev |
| `end` | Logic | inputs `fb` + `state`; prev AND NOT state |

**The one risk to watch.** Logic CHOP combines multiple input CHOPs *pairwise by
channel*, so the channel NAMES must line up across its inputs. `below_on`,
`above_off` and `fb` all derive from `sel_d`, so they match by construction - but
the validity channel does not (`h0_valid` against `h0_pinch_index`). Either
rename validity to match, or apply it with a Math CHOP multiply after `state`.
Check `Logic CHOP`'s `align` and `match` parameters before assuming.

**Verify it like this**, because a latch cannot be verified from one frame: send
a descending then ascending ramp of `h0_pinch_index` over OSC across ~20 frames
and assert the state engages only below `on`, releases only above `off`, holds in
the dead band, and that `start` fires exactly once per crossing. A synthetic ramp
is the only way to test the dead band deliberately.

## Step 2 — the rest of the temporal channels

| channels | operators |
|---|---|
| `vel_x/y`, `speed`, `dir`, `accel` | Slope on the palm channels, then Filter (`Velocityfilter`); `dir` gated by `Speedfloor` via a Logic + Hold |
| smoothing | Lag or Filter on positions, before derivation |
| `active` (debounced `valid`) | Count with limits, or Trail + Analyze, then Logic |
| `held` seconds | Count on `active`, scaled by the frame rate |
| `dwell` | Count gated by a radius test against a Hold of the settled position |
| `steadiness` | Trail (`Steadywindow`) into Analyze (deviation), inverted |
| `hands_scale`, `hands_twist`, `hands_approach` | Slope on the matching stateless channels; `hands_scale` needs a Hold latched by `Scalereset` |
| motion events | Logic on the thresholds, then a Count-based refractory of `Refractory` |

Angles: unwrap before differentiating or filtering, or filter sin/cos and
recombine. A hand crossing +/-180 otherwise produces a spike that reads as a
violent twist.

## Step 3 — groups and parameters

- One base COMP per group; `allowCooking` bound to its toggle so a disabled group
  costs nothing.
- `select_out.channames` as an expression assembling the enabled groups' patterns,
  so a disabled group's stale channels are excluded rather than published.
- Both are needed: cooking gated by one, channel list by the other.
- Five pages exactly as tabulated in `docs/ATTRIBUTES.md`.
- Pass the parameters into `derive()` via the existing `_params()` reader in
  `tools/td_add_derive.py`; it already falls back to spec defaults with `getattr`,
  so adding a parameter needs no code change there.

## Step 4 — `tools/send_synthetic.py`

Stream generated sequences over OSC so gestures can be exercised with no camera:
`--gesture pinch|clap|snap|swipe|ramp`, using `visionhands.synth` and
`visionhands.osc`. The ramp mode is what step 1's verification needs.

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
