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

**READ THIS BEFORE BUILDING STEP 2.** The bank was structurally perfect and
completely dead, because TouchDesigner does not cook a Merge branch that nothing
downstream consumes — measured, `merge_out` 283,930 cooks against `lat_state` 2.
Harmless for stateless maths; fatal for anything with memory, and it silently
corrupts every edge pulse rather than failing. `DESIGN.md` 2.10 has the numbers.
Every group below needs a Null CHOP with Cook Type = `always`, or needs to be
provably downstream of one. `viewer = True` does not work and neither does an
ordinary Null.

`DESIGN.md` 2.11 lists the seven other native-operator behaviours that cost time
here — the Logic CHOP's `convert` being a comparator, its `match` defaulting to
index, the Feedback CHOP emitting nothing without its second input, the Count
CHOP's action menus that look like toggles, Merge connectors that insert rather
than fill, `appendFloat` zeroing an existing value, and the MCP bridge running a
script twice. Do not rediscover them.

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
