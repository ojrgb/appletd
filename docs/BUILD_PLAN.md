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

### DEFERRED TO PHASE 2, by decision 2026-08-21

The remaining temporal channels are earmarked rather than next. What is built -
presence with a real debounce, the velocity family, heading, and the closing rate -
covers what the latches and the gesture work need.

### Still to build in phase 2, and what each needs

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

## Step 5 — visionpose and visionface — DONE 2026-08-21. Segmentation deferred

**Built and verified end to end with no camera:** `visionhands/pose_types.py` (the
19-joint table and the 123-channel contract), `visionhands/pose.py` (the Vision
request and the conversion), `visionhands/streams.py` (the stream registry, the
ports and the `sc_*` status channels), the engine carrying both requests on one
buffer, the sidecar sending one bundle per stream, `visionhands/synth_body.py` plus
`tools/send_synthetic_pose.py` for testing without anybody in frame, and
the TouchDesigner side (folded into `tools/td_build_vision.py` when the streams
were wrapped - step 8). 61 new tests.

**Measured in TouchDesigner:** 123 pose channels arriving on port 10001 with the
right names and moving values, and the hands port now carrying 141 (137 + four
`sc_*`) with the COMP output at 499. DESIGN.md 6.4 has the design and 2.12 the API
surface; the journal has the four things that went wrong.

**visionface followed the same afternoon**, and finished later the same day:
`visionhands/face_types.py`, `visionhands/face.py`, `visionhands/synth_face.py`,
`tools/send_synthetic_face.py`, `tools/td_build_face_comp.py`. **Verified in
TouchDesigner: 387 channels on port 10002** - 23 observation scalars, 348 landmark
slots, and 16 transformed box components.

**The landmark counts, MEASURED 2026-08-21** by `tools/probe_face_regions.py` with a
real face, and they were not what a guess would have produced:

```
sum of the 12 regions   87 slots
DISTINCT coordinates    76        <- what "the 76-point constellation" means
```

**The regions overlap.** `medianLine` shares points with `noseCrest` (4), `nose` (2),
`outerLips` (2), `innerLips` (2) and `faceContour` (1); `nose` shares one more with
`noseCrest`. Twelve pair-overlaps for eleven duplicate points - nine points in two
regions, the nose tip in three. The channel list is built from the SLOTS, so those
eleven are published twice under both names: the price of `f0_nose_crest_00_x` over
`f0_lm43_x`.

Two things fell out of it, both now in `DESIGN.md`:

- **the first self-check refused the correct table** (2.12), because it asserted the
  regions partition the constellation. The guess that got caught was in the CHECK,
  not in the data.
- **the 371-channel bundle is 12208 bytes and a default UDP socket refuses it**
  (2.13). Two streams kept working and face's channels simply never appeared.
  Raising `SO_SNDBUF` on the sender fixes it with no privileges, and the receiver
  needs nothing - which is the only reason it is fixable when TouchDesigner owns the
  receiving end.

**What is NOT done:** a synthetic face that moves its landmarks - the synthesiser
emits a box and three angles, so 304 of the 371 channels are flat under
`send_synthetic_face.py` - and any attribute layer for either new stream: no derived
channels, no temporal channels, by design. The brief was plumbing plus basic testing.


**The original request, for the record.**

**The shape of the request:** the same treatment as hands for
`VNDetectFaceLandmarksRequest`, `VNDetectHumanBodyPoseRequest` and person
segmentation, with **a toggle per stream so not every model runs at once**. The
brief was explicitly plumbing plus basic testing - not a full attribute layer per
stream, and not running vision models continuously to prove it.

**What already generalises.** The sidecar takes any `HandSource`, so a second
detector is a new source rather than a rewrite. `types.py` holds the channel
contract as one table with an import-time self-check. `synth.py` and
`sequences.py` mean a stream can be tested with no camera at all. The OSC encoder
is contract-agnostic.

**SETTLED 2026-08-21, so not open questions any more:**

- **The toggles are launch flags, not live control.** The sidecar is a separate
  process that TouchDesigner starts with `subprocess.Popen`, and the Start button
  already builds a command line - so a stream toggle is a flag appended in
  `start()`, read once at initialisation. Flipping a toggle takes effect on the
  next restart. That removes the whole hard part: no control channel into the
  sidecar, no live reconfiguration of a running capture session, no
  partial-reconfiguration state machine.
- **The channel contract stays CONSTANT whatever is enabled.** A disabled stream
  sends zeros; it does not stop sending. TouchDesigner's OSC In CHOP creates
  channels as they arrive, so omitting them makes them VANISH from the CHOP - and
  DESIGN.md 6.2 records why that is the dangerous failure: an operator referencing
  a missing channel silently stops receiving, with no error anywhere. The saving
  that matters is the inference, not the 4 bytes per channel.
- **The sidecar reports which streams it actually started**, as channels. Otherwise
  the panel lies after someone flips a toggle without restarting; with them, TD can
  say "toggle on, sidecar off - restart needed".
- **Segmentation goes down the C++ path**, and is deferred. It is a mask rather than
  floats, so no toggle scheme changes the fact that it breaks the "137 floats and no
  pixels" property (DESIGN.md 4.2). Datum worth having before starting: a C++
  segmentation plugin for TouchDesigner already exists and reportedly **pins the
  frame rate at 50 fps**. So the question is not whether it can be done but whether
  10 fps is an acceptable price, and that is worth measuring against our own
  requirements rather than inheriting someone else's implementation.

**DECIDED 2026-08-21 while building it**, and all four are written up in
DESIGN.md 6.4 with the reasoning:

- **One process, several requests.** One camera open, one capture queue, both
  requests performed on the same sample buffer - so the two streams share a `seq`
  and a consumer can align them. Fault isolation, which was the argument for
  several processes, is bought instead with a try/except per stream: a failing pose
  request costs the pose stream and nothing else.
- **One port PER STREAM**, hands 10000 and pose 10001, from one base constant in
  `visionhands/streams.py`. Sharing a port would put pose channels inside the hands
  COMP's output, and the alternative - a prefix Select per COMP - is the mechanism
  that has already failed to partition these channels once (step 3 above).
- **The toggles are launch flags**, appended to the command line in `start()`, and
  the sidecar reports what it actually started as `sc_*` channels so the panel
  cannot lie.
- **The sidecar DATs did NOT move upstream.** They still live in the `visionhands`
  COMP even though the process serves every stream now. Moving them would break the
  panel in a live project and every callback's parameter path in exchange for
  tidiness; the trigger for doing it is a second thing needing to control the
  process, not the current asymmetry.

**The face API surface**, VERIFIED against the live framework 2026-08-21
(DESIGN.md 2.12): `VNFaceObservation.landmarks()` returns a `VNFaceLandmarks2D` of
**12 named REGIONS** - `faceContour`, `medianLine`, `nose`, `noseCrest`, `leftEye`,
`rightEye`, `leftPupil`, `rightPupil`, `leftEyebrow`, `rightEyebrow`, `innerLips`,
`outerLips` - each a `VNFaceLandmarkRegion2D` with its own `pointCount` and
`normalizedPoints`, plus an `allPoints`. The constellation is selectable, 65 or 76
points, and the request's default is 76. The observation also carries
`boundingBox`, `confidence`, `faceCaptureQuality` and `roll`/`yaw`/`pitch` - the
last three in RADIANS and each NSNumber-or-nil. Everything except the per-region
point counts is publishable without a face in front of the camera, and that is
exactly what shipped. Everything else - the port,
the flag, the status channel, the box, the send path - is already built and
generic; `sc_face` is published, reading 0, and `parse_streams` refuses `face` with
"in the channel contract but not implemented yet" rather than silently starting
nothing.

**Segmentation is a mask, not landmarks**, so it is the one that breaks the
"137 floats and no pixels" property the whole architecture rests on (DESIGN.md
4.2). A mask has to reach TD as a TOP, which is either a shared-memory TOP or the
C++ path in DESIGN.md 9 - and it is the strongest trigger for that path yet.

**Suggested order:** pose first (closest to hands - the same
`VNRecognizedPointsObservation` shape, so `types.py`'s joint-table pattern and
`derive()`'s structure both carry over), then face, then segmentation separately
down the C++ path.

## Deferred by the user, so not oversights

Raised and explicitly set aside on 2026-08-20, listed so nobody re-raises them as
gaps:

- **README** - later.
- **Two-hand behaviour of the TRACKER** - later; needs a two-hand fixture to
  measure throughput, chirality stability and behaviour under occlusion. Note that
  this is NOT the same as slot assignment, which is not blocked - see below.
- **Per-joint jitter measurement** - not wanted for now. It is what should set
  `Beta` and `Velocityfilter`, so those defaults stay labelled guesses.
- **`Sidecar.run()` tests** - later. Five mutations still survive a green suite,
  including one where the loop sends nothing.

## Step 6 — slot assignment — DONE 2026-08-21

`visionhands/slots.py`, 17 tests, wired through the engine, the source and the
sidecar, with `--slots` and a `Slotassign` toggle on the Sidecar page. DESIGN.md
6.3 has the algorithm and the measurement; the journal has how it went.

Short version: it is a partition, not a tracking problem, because Vision already
reports handedness. Slot 0 is the right hand. The grace period the original spec
asked for is deliberately not built - the Presence debounce does that job
downstream, and in chirality mode the hazard cannot arise.

**Left over from it**, and worth doing next time this area is open:

- `h{i}_e_grab` / `h{i}_e_release` are latches on `openness` with `Grabon` /
  `Graboff`: two more rows in `tools/td_add_latches.py`, no new machinery. Intended
  to be bundled with this and was not.
- The fixture is now wanted for exactly one question: whether real Vision's
  chirality flickers, and what it reports during occlusion or edge-on. That decides
  how often the proximity fallback is reached, and it is the last thing standing
  between the fallback being tested and being merely written.

## Step 7 — cook time: 1.8 ms is too much, and here is where it goes

Raised after the first live session: the COMP's CPU cook time is about 1.5-1.8 ms,
which is 9-11% of a 16.7 ms frame for what is mostly arithmetic on 15 channels.
Three separate asks, and the profile below decides the order.

**MEASURED**, `cookTime` summed over the COMP's 152 operators, Smoothing on:

| group | ms | share |
|---|---|---|
| one-euro filter (`oef_*`) | 0.612 | 33.5% |
| latch bank (`lat_*`) | 0.370 | 20.3% |
| `derive_chop` (Python) | 0.368 | 20.1% |
| temporal (`tmp_*`) | 0.241 | 13.2% |
| other | 0.138 | 7.5% |
| coordinate branches | 0.099 | 5.4% |
| **total** | **1.828** | |

And the single most useful reading: **with `Smoothing` OFF the filter still costs
0.609 ms.** The toggle switches which channels reach the output; it does not stop
the chain cooking, because the Switch CHOP pulls both its inputs and the group
clock pulls the Switch. So the most expensive thing in the COMP is not gated by its
own toggle.

### RESULT so far: 1.828 ms -> 1.040 ms, and 152 operators -> 144

Done 2026-08-21, in this order:

- **Replaced the hand-built one-euro filter with TouchDesigner's own**, `Filter
  CHOP type = oneeuro`. **0.695 ms across 21 operators -> 0.015 ms in one.** The
  hand-built version existed because of a wrong conclusion that the Filter CHOP
  could not work on these channels; it can, on a time-sliced input. DESIGN.md 2.11
  has the correction. This was 38% of the COMP's entire cook time.
- **`lat_on` and `lat_off` are static values now**, rewritten by a Parameter
  Execute DAT when a threshold changes rather than being 30 Python expressions
  re-evaluated every frame.
- **One broadcast instead of two**: liveness and the debounced `active` are ANDed
  per hand first, then broadcast once.

| group | before | after |
|---|---|---|
| filter | 0.612 | 0.110 |
| latches | 0.370 | 0.247 |
| `derive_chop` | 0.368 | 0.256 |
| temporal | 0.241 | 0.204 |
| **total** | **1.828** | **1.040** |

`derive_chop` is now the largest single item at 25%, and it is already gated by the
group toggles. Measure on a clean frame: a `cook(force=True)` right after a rebuild
reads nearly 3 ms because it re-cooks everything.

**Still worth doing**, in order of value:

- **The velocity chain may be replaceable the same way.** `tmp_slope` is built from
  Feedback + Math and so is not time-sliced, which is why Trail + Analyze was
  needed. If that chain can be made time-sliced, the native Filter CHOP replaces
  the moving average - possibly the Slope CHOP too. Worth one measurement.
- 7.3, the COMP restructure, which is now the main remaining item.
- The DATs cost 0.057 ms between them (`sidecar_control` 0.040,
  `groups_callbacks` 0.017) and neither needs to be evaluated per frame. Worth a
  look at why they are cooking at all.

### 7.1 Non-destructive optimisations — done, and what is left

- **`lat_on` and `lat_off` cost 0.110 ms between them, for six numbers.** They are
  Constant CHOPs whose 15 values are each a Python EXPRESSION on a Tuning
  parameter, so that is 30 expression evaluations per frame to deliver thresholds
  that only change when somebody drags a slider. Replace with static values
  rewritten by a Parameter Execute DAT on change. Same channels, same values, no
  per-frame Python.
- **`lat_live5` and `lat_active13` are two 15-channel broadcasts of two booleans**,
  0.074 ms together, and they are ANDed into the same gate. Compute `live AND
  active` per hand first (one native Logic CHOP, 2 channels) and broadcast ONCE.
  Halves it.
- **The tail is 130 operators summing 0.628 ms**, about 0.005 ms each. That is the
  floor for this many operators and it is the strongest argument for 7.3: fewer,
  wider operators rather than more, narrower ones.
- Worth checking but probably not worth doing: the filter runs on all 84 position
  channels. Nothing needs the confidences filtered and few consumers need all 21
  joints smoothed, but narrowing it trades a real property - that everything
  downstream inherits one filter - for a fraction of 0.6 ms.

### 7.2 Put the expensive things behind toggles that actually gate them

The filter is the case in point: 0.6 ms that a project not using smoothing pays
anyway. A Switch CHOP cannot gate cost, because it pulls both inputs. Two ways:

- **`allowCooking` on a group COMP** — measured to work, and it stops the clock
  inside too (docs/BUILD_PLAN step 3). Needs 7.3 first.
- **A Select CHOP whose `chops` parameter is an expression** naming either the raw
  or the filtered source. Nothing references the branch that is not named, so it is
  not pulled. Cheaper to build than 7.3 and worth measuring against it.

Either way the consequence is the same and needs writing down: a filter that stops
cooking has a stale `x_hat_prev`, so re-enabling it has a settling transient. It
self-converges, and it is the same trade `allowCooking` makes everywhere else.

### 7.3 Group the network into COMPs — DONE 2026-08-21

**Top level went from 152 operators to 16**, with four group COMPs: `filter`,
`coords`, `temporal`, `latches`. `merge_out` has five inputs instead of thirteen.
Each builder owns exactly one group, reuses it and clears its children, and
attaches itself to `merge_out` exactly once.

Build order from scratch, and it matters because each group wires to the one
before:

    td_build_comp.py    the shell, the parameters, the sidecar control
    td_add_filter.py    `filter` - everything downstream reads through it
    td_add_coords.py    `coords`
    td_add_derive.py    the stateless attributes
    td_add_temporal.py  `temporal` - liveness, presence, velocity, heading
    td_add_latches.py   `latches` - gates on temporal's second output
    td_add_groups.py    the Attributes page

**Cross-group dependencies are wires now, not name lookups.** `temporal` has two
inputs (derived attributes, and the RAW stream for `seq`) and two outputs (its
channels, and `ready` for the latches). `latches` has two inputs. The latch bank
used to reach for `op('tmp_ready')` in an expression; the dependency is a line in
the network instead.

**Cook time, MEASURED 2026-08-21 and the 0.599 ms figure withdrawn.** Summing all
162 operators including the groups' children, with data provably arriving:
**0.997 ms and 1.187 ms** on two single-frame reads, against 1.040 ms measured flat.
Grouping is cost-neutral. The first attempt at this measurement read 1.2 and 1.55 ms
from a network that was not cooking at all, because the synthetic feeder had died on
an import error - `cookTime` is a last-value gauge and reports the previous cook
happily forever, so check that the data is moving before reading it.

**Still to do**, both now unblocked by the grouping:

- **`allowCooking` gating for `temporal` and `latches` — DONE 2026-08-21.**
  Built in `tools/td_add_groups.py`: `COOK_GATED` maps each group to the toggles
  that keep it alive, `COOK_REQUIRES` records that `latches` cooking forces
  `temporal` (it reads temporal's presence gate, and a frozen gate arms every latch
  forever or disarms them forever with nothing to show for it), and
  `groups_callbacks` writes the attribute on change. Verified by counter delta:
  three toggles off gave `latches` **0 cooks against 377** for temporal and coords
  over the same interval, with every frozen channel still on the COMP output. Note
  for anyone measuring it: a frozen group still REPORTS its last `cookTime`, so that
  column cannot show the saving - only a cook count can.

  Cook time, re-measured with data provably flowing: **0.997 and 1.187 ms** over 162
  operators and 499 channels, against 1.040 ms flat. Grouping is cost-neutral;
  gating saves about 0.5 ms of it.

  The original note, for the reasoning:
  Both are safe candidates: nothing outside reads their channels except through
  `merge_out`, and each has its clock INSIDE it, which is what makes disabling one
  genuinely free. `filter` is not a candidate - it is in the data path, and its
  `Smoothing` toggle already gates it via the bypass flag.

  Bind each group's `allowCooking` to the matching Attributes toggle. Two things to
  handle rather than discover: `allowCooking` is an ATTRIBUTE, so a Parameter
  Execute DAT applies it - the pattern `filter_callbacks` and
  `lat_threshold_callbacks` already use - and a group resuming after being disabled
  comes back with a `prev` from whenever it stopped, so the first frame after
  re-enabling can produce one wrong edge pulse. Measured, bounded, acceptable;
  write it into ATTRIBUTES.md beside the toggles rather than leaving it to be
  found.
- The channel-to-group registry for trimming disabled groups' channels out of the
  output. Now cheaper than it was: each group has one output, so the map is
  group COMP -> its own channel list, which each builder already knows.

### 7.3 notes from doing it — IN PROGRESS (superseded above)

**Done: `filter/`.** Five operators plus an In and an Out CHOP, inside a base COMP,
so the top-level network shows one node. The pattern the other groups follow:

- everything the builder owns is created INSIDE the group, and the group is
  destroyed and recreated whole, which is what keeps the builder idempotent;
- an In CHOP inside and a wired connector outside, so the group is a node in the
  parent network rather than a folder reaching out of itself;
- user parameters stay on the TOP-LEVEL COMP so all tuning is in one place, and
  expressions inside reach them through `parent(2).par.X` - relative, so it
  survives a rename.

**A design point that fell out, and it changes what gating means.** `allowCooking`
gating is only right for a group whose output is OPTIONAL. The filter sits in the
main DATA PATH - everything downstream reads landmarks through it - so disabling its
cooking would freeze every position rather than bypass the filter. Its toggle stays
a Switch, and grouping buys readability there rather than gating. The latch and
temporal groups are the real `allowCooking` candidates: nothing breaks if their
channels go stale, because nothing else reads them.

**Two traps this hit, both worth knowing before doing the next group:**

- `inputConnector.connect(op)` takes an operator only where that operator has one
  unambiguous output. A base COMP does not: pass `group.outputConnectors[0]` or it
  raises "Invalid number or type of arguments".
- **`source.outputs` goes stale the moment a downstream group is destroyed.** The
  first version repointed consumers by walking `in1.outputs`; after a second run of
  the builder nothing read the group at all, `derive_chop` and the coordinate
  branches were back on the raw input, and **the filter was silently bypassed while
  the builder reported success**. Walk the SIBLINGS and inspect their input
  connectors instead - the question asked that way round cannot go stale - and then
  assert it took, which the builder now does.

**Remaining:** `latches/`, `temporal/`, `coords/`. The first two take two inputs
each (derive's channels, and `tmp_ready` for the latches), which is what In CHOPs
are for and what makes the cross-group dependency visible in the network instead of
hidden in an expression.



152 operators in one flat network is spaghetti, and it is also why the tail costs
0.6 ms. One base COMP per concern - `filter`, `latches`, `temporal`, `coords` -
each with its inputs and outputs on connectors, gives:

- something a person can read;
- `allowCooking` gating per group, which is 7.2 done properly;
- and the clock question already answered: Cook Type = Always does NOT run inside
  a COMP with cooking disabled, so each group's clock must sit inside the group it
  clocks - which is what makes disabling a group actually free.

The channel-to-group registry that step 3 wants for output trimming is the same
refactor: once the latch table lives in `visionhands/` rather than in a builder
script, both the registry and the builders can import it.

## TouchDesigner facts that cost time to learn

- `CookLevel` exists ONLY in a DAT's globals. Not `builtins`, not the `td` module.
  A callback in an imported module cannot see it, returns None, and TD silently
  falls back to AUTOMATIC - which for an input-less CHOP means it cooks once and
  freezes. `td.noiseTOP` and friends ARE on the `td` module; `CookLevel` is not.
- `appendChan()` outside `onCook` "causes random behavior" unless the operator is
  locked. Same for `copyNumpyArray`.
- **Removing the code that creates a parameter does not remove the parameter.** It
  keeps its value, keeps its place on the page, and drives nothing. `Segment` sat on
  the Segmentation page reading True for two commits after `Streamsegment` took over
  both its jobs, and it took the user asking "what is the difference?" to find it. The
  same shape as operator storage outliving the code that wrote it (DESIGN.md 2.11):
  TouchDesigner keeps what you made until you unmake it. Every builder that has ever
  dropped a parameter needs a retire table - `td_build_vision.py` has `RETIRED_PARS`,
  and `td_add_segmentation.py` has one now too.
- **An operator with NO INPUT is never dirtied, so it cooks once and then serves its
  last result for ever.** True of a Script CHOP (below, via `CookLevel`) AND of a
  Script TOP - which has no Cook Type parameter at all to fix it with. The cure is a
  custom parameter whose value changes every frame, e.g. `absTime.frame`; gate the
  expression on your enable toggle and the operator stops cooking when it is off.
  Cost the project two separate afternoons because it was filed under CHOPs the first
  time. DESIGN.md 2.21.
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

## Step 8 — one COMP around the three streams — DONE 2026-08-21

Asked for after the face stream landed: *"visionhands also contains transformation
logic to transform coords to TouchDesigner screenspace. These, the filter, etc,
should be applicable for pose and face as well. Suggestion: wrap visionhands,
visionpose, visionface in one comp, have the filter + sidecar + etc controls at the
master comp level, and perform space transformations on all three."*

Built exactly that. `DESIGN.md` 6.5 has the structure and the reasoning; the short
version:

```
/project1/vision            every user parameter, six pages, the sidecar control
  hands_osc 10000 -> hands -> out1     499 channels
  pose_osc  10001 -> pose  -> out2     275   (was 123: +152 coordinate channels)
  face_osc  10002 -> face  -> out3      39   (was  23: + 16)
  status                               the sidecar's own sc_* channels
```

**What each stream gained.** A `filter` group of its own, reading the master's one
`Smoothing`/`Mincutoff`/`Beta`; and a `coords` group of its own, reading the
master's one `Orthowidth`/`Renderw`/`Renderh`/`Resw`/`Resh`. Verified live with
synthetic senders and no camera:

| | normalised | `_tx` world | `_px` pixels |
|---|---|---|---|
| `p0_root_x` | 0.5366 | 0.0366 | 686.83 |
| `p0_nose_y` | 0.8500 | 0.1969 | 612.00 |
| `f0_bbox_x` | 0.3900 | −0.1100 | 499.20 |
| `f0_bbox_w` | 0.2200 | **0.2200** | 281.60 |
| `f0_bbox_h` | 0.3000 | 0.1688 | 216.00 |

The fourth row is the point: **a width is not offset**. `_tw` = w × Orthowidth with
no −0.5, because a box is 0.2 wide wherever it sits and subtracting 0.5 gives a
negative size that draws as nothing. `visionhands/spaces.py` is what keeps positions
and extents apart, and it also marks face landmark points `box_relative` - they are
normalised to the bounding box rather than the image, so they are deliberately not
transformed (DESIGN.md 7).

**Every expression is `op.Vision.par.X` now**, via a Global OP Shortcut, replacing
`parent(2).par.X`. A group sits two levels below the parameters instead of one, so a
relative reference would have to be counted per operator; the shortcut is
depth-independent and VERIFIED to resolve from a nested network before anything was
built on it.

**Two bugs surfaced by doing it**, both pre-existing and both silent:

- **`derive_chop` was reading the RAW input**, so every derived attribute was
  computed on unsmoothed landmarks while the coordinate spaces used smoothed ones.
  Cause: the filter builder forced a named list of consumers onto its output, and
  in the documented build order `derive_chop` does not exist yet. Each consumer
  states its own input now. DESIGN.md 2.11.
- **`tools/td_verify_latches.py` never evaluated the h1 invariant.** Its state list
  was hand-maintained and omitted `h1_pinching`/`h1_snapping`, so the second hand's
  rows raised KeyError rather than being checked. The list is derived from
  `END_COUNTERS` now. After the fix: ramp +4 on `h0_pinch_count`, and
  fires − releases == engaged on all five rows.

**BUILD ORDER, and it is shorter than it was** - four builders retired
(`td_build_comp.py`, `td_build_pose_comp.py`, `td_build_face_comp.py`,
`td_setup_osc.py`; the last would have bound port 10000 a second time):

    td_build_vision.py  the master: parameters, OSC In CHOPs, three stream shells
    td_add_filter.py    `filter` in EVERY stream
    td_add_coords.py    `coords` in EVERY stream
    td_add_derive.py    the stateless hand attributes
    td_add_temporal.py  presence, liveness, velocity   (hands)
    td_add_latches.py   the proximity latches          (hands)
    td_add_groups.py    the Attributes page and the cook gating

**PATHS CHANGED.** `/project1/visionhands` is now `/project1/vision/hands`, and the
same for pose and face. The master builder rewires anything that was wired to the
old COMPs and inherits their tuned parameter values, so a rebuild does not reset
somebody's Ortho Width - verified: Mincutoff 3.170 and Beta 10.000 survived, both
tuned away from their defaults.

## Step 9 — segmentation: the transport, researched and PARKED 2026-08-21

Researched at the user's request, then parked with a decision made: **the next phase
uses a plain file-backed `mmap`, not TouchDesigner's shared-memory protocol.**

### What was proven about TD's shared memory

Measured on this machine, from the shipped headers in
`/Applications/TouchDesigner.app/Contents/Resources/tfs/Samples/SharedMem` rather
than from the documentation, which does not carry the detail:

- **The Shared Mem In TOP works on macOS** and reads a foreign segment: pointed at
  TD's own Shared Mem Out TOP it returned 64x32 with the right pixels and no
  warnings. `memtype` must be `local`.
- **Layouts, compiled from the headers.** `TOP_SharedMemHeader` is 56 bytes: magic
  `0xd95ef835` @0, version @4, width @8, height @12, aspectx/y @16/@20, pixelFormat
  @24, `dataSize` (int64) @32, `dataOffset` @40, gamutPrimaries @44, transfer @48.
  `UT_SharedMemInfo` is 44 bytes: magic `0x56ed34ba` @0, version @4, supported @8,
  `namePostFix[32]` @9, detach @41.
- **THREE segments per name**, found by probing what TD's own writer creates:

      TouchSHM<name>                    the data
      TouchSHM<name>Mutex               the data's lock
      TouchSHMTouchSHM<name>4jhd783h    the info - DOUBLY decorated, because the
                                        info segment is itself a UT_SharedMem whose
                                        short name is the already-decorated one

- **The blocker.** `UT_SharedMem::open` names its lock `myName + "Mutex"`, which for
  the INFO segment is **36 characters** against macOS's 31-character `shm_open`
  limit - so it must be registered in TD's own shared-memory directory
  (`TD1zzSharedNameDirectory`), and `ShmNameForName(name, create)` only registers
  when `create` is true, i.e. **only for the segment's owner**. A foreign owner must
  therefore implement that directory protocol AND initialise process-shared
  `pthread_mutex_t` + `pthread_cond_t` in shared memory. The receiver calls
  `tryLock(5000)`: a mistake stalls TouchDesigner for five seconds per frame rather
  than failing cleanly. Byte-identical data and info segments were still reported
  "not found" for exactly this reason.

### Two incidental findings, both measured

- **The shared-mem name must be 7 characters or fewer** to keep every segment inside
  the 31-character limit (the info name is 24 + len(name)).
- **`shm_open` is variadic**, and on Apple Silicon ctypes must be given only the two
  FIXED argtypes - with the mode in `argtypes` it arrives as 0, creating a segment
  that nobody can open and that `shm_unlink` cannot remove either, poisoning the
  name until reboot. Two names were burned this way.

### The decision: file-backed mmap

- `VNGeneratePersonSegmentationRequest` outputs **`L008` - one 8-bit component**
  (revision 1 only; quality levels Fast/Balanced/Accurate). A 256x144 mask is 37 KB.
- So: a plain file in `/tmp`, `mmap` on both sides, `numpy.memmap` and
  `copyNumpyArray` in a Script TOP, and a **seqlock** for coherence - write seq,
  write pixels, write seq again; the reader takes seq, pixels, seq and retries if
  the two seqs differ. No TD protocol, no pthreads in shared memory, no ABI
  coupling, and testable with no TouchDesigner running.
- Cost: one Script TOP cook on TD's main thread. `copyNumpyArray` outside `onCook`
  needs the operator locked (docs/BUILD_PLAN "TouchDesigner facts").

### MEASURED 2026-08-22, and it did not need a camera

That question is closed. `DESIGN.md` 2.18 has the table; the headline is that
`fast` is **2.21 ms** and `accurate` is **30.73 ms**, and Vision's own DEFAULT is
`accurate` - which is very likely the C++ plugin's 50 fps ceiling. `fast` sits
happily beside the hands stream inside a 16 ms frame. Measured over
`fixtures/hand_clip.mp4`, so the figures are reproducible and no camera was used.

Two things that measurement turned up which the research had not:

- **`fast` returns a HARD alpha** - only 0 and 255, no soft edge. It is not a
  smaller `balanced`, so a feathered matte costs 8.54 ms, not 2.21.
- **the mask does not share its input's aspect ratio.** Landscape in gives 4:3 out,
  portrait gives 3:4, chosen by orientation alone and stretched anisotropically to
  fit. A 1280x720 frame yields a 256x192 mask. This one would have shipped broken:
  a consumer scaling by a single factor is exactly right on a 4:3 camera.

## Step 10 — the optimisation and tidy pass — DONE 2026-08-21

The six things the user asked for on 2026-08-21, in their order, and what each
turned into. All figures from `tools/td_profile.py`, which is new and is the first
honest instrument this project has had for cook time — it samples across frames
from a scheduled callback and DISCARDS a run in which `seq` did not move.

### 1. Cook time — 1.6686 ms measured, and the result is mixed

`DESIGN.md` 2.14 has the whole table. The short version:

| configuration | ms |
|---|---|
| before this phase | 1.6686 |
| like for like now | 1.7381 |
| what a project actually runs (hands, world only) | 1.1367 |
| everything gated off except landmarks and coordinates | 0.6031 |

**The like-for-like number went up 4%**, because making the coordinate spaces
gateable cost 0.33 ms of COMP boundaries and merges while the filter and
`derive_chop` changes gave back 0.36. Gating is not free. It is right here anyway,
because the configurations a project runs all have something switched off — but
"we made it faster" would be the wrong summary.

### 2. `scope` instead of Select + Merge — DONE, and it is the one clear win

Each `filter` group went from `in1 -> Select -> Filter -> Merge <- Select -> out1`
to `in1 -> Filter(scope) -> out1`. **0.4380 ms -> 0.1527 across the three
streams**, and the class of bug that has cost the most time here is now
structurally impossible: there is no Select whose pattern can miss a channel.

`scope` did NOT go into `coords`, because a coordinate branch produces NEW channels
alongside the originals and `scope` modifies in place. What went in there instead
was verified PATTERNS in place of literal name lists — `spaces.compact_pattern()`
expands each candidate against the stream's own contract with `fnmatchcase` and
returns the literal list unless the match is exact. No network in this COMP now
carries a 362-name string.

### 3. Gating — every expensive thing is now behind a toggle

| toggle | freezes | measured saving |
|---|---|---|
| `Streamhands` / `Streampose` / `Streamface` | the whole stream COMP | pose 0.25, face 1.45 |
| `Coordstx` / `Coordspx` | `coords/world`, `coords/pixels` | ~0.1 per stream |
| `Lmcoordstx` / `Lmcoordspx` | `coords/lm_world`, `coords/lm_pixels` | **1.08** (face) |
| `Presence` / `Motion` | `temporal` | 0.32 |
| `Triggers` / `Gestures` / `Events` | `latches` | 0.26 |

Proved by cook COUNT, never by duration: 29 of 287 operators cooking in the
all-off configuration. `Coordspx`'s DEFAULT had to change from off to ON in the
same edit — it used to gate nothing, so the pixel channels were always live, and a
toggle that starts freezing them must default to not freezing.

`derive_chop` is not gated and should not be: it feeds the latch bank. It was made
cheaper instead — the channel list is a pure function of the enabled groups, so it
is cached on the operator and rebuilt only when it changes. **0.2815 -> 0.2037.**

### 4. Face landmarks get world and pixel coordinates — DONE

All 174 points per face, composed through that face's own bounding box:

    image_x = bbox_x + point_x * bbox_w    then the usual centre-and-scale

folded into ONE Math CHOP per branch with `gain` and `postoff` as expressions,
because a Math CHOP computes `(value + preoff) * gain + postoff` and an expression
on a scalar parameter is evaluated once per cook. The alternative — an Expression
CHOP composing per channel — would have been 608 Python evaluations a frame.

The face stream went **387 -> 1083 channels**. It costs 1.08 ms with the world half
on, which is why it has its own toggle rather than riding on `Coordstx`.

`visionhands/synth_face.py` grew a full 76-point constellation to verify it, and
that was necessary rather than nice: with landmarks at zero, `_tx` reduces to the
bounding box alone and a broken `point * bbox_w` term looks perfect.

### 5. `Screenspaceonly` — one toggle, one Delete CHOP per stream

Between `merge_out` and `out1`, holding the exact list of channels that HAVE a
screen-space companion (`spaces.companioned_names()`). On: hands 499 -> 415, pose
275 -> 199, face 1083 -> 727, and the removed set equals the delete list exactly
with zero collateral. Off: the operator is bypassed, bit-exact.

A Delete CHOP and not a Select, because a Select needs the KEEP list and the
stream's output carries 87 derived attributes, 27 temporal channels and 76 latch
channels named by tables in three different builders.

It keeps every boolean, counter, confidence and ANGLE — the rule is "remove the
second copy of a number, never the only copy".

That rule left 24 channels behind on the first pass, because they had no companion
to be redundant with. Sorting out which of them SHOULD have one is item 7.

### 7. The 24 channels the toggle could not remove — 16 of them now can be

The screen-space toggle made a gap visible that had been there since `derive()` was
written: 100 derived channels end in `_x` or `_y`, and they are not all the same
kind of number.

| kind | channels | rule | companions |
|---|---|---|---|
| positions — `palm`, `pinch`, `hands_center`, `index_center` | 12 | as a joint: scale, offset by −0.5 | **added** |
| rates — `vel` | 4 | as an extent: scale, NO offset. World units per second | **added** |
| unit vectors — `point`, `dir` | 8 | none | deliberately not |
| hand-local — the 84 `h{i}_d_<joint>_x/y` descriptor channels | 84 | none | never |

The positions were simply missed, and a project drawing the palm centre had to
redo the transform by hand. The rates take the extent rule, which is the same
arithmetic: a velocity of 0.2 is 0.2 wherever the hand is.

**The unit vectors are a conclusion, not an omission.** World space scales y by the
render's aspect, so a transformed unit vector is no longer unit length AND no longer
points where the image-space one pointed. Re-normalising it is a magnitude and a
divide — a different branch, not a companion. It is named in `spaces.py` beside the
table so nobody has to work it out twice.

**The 84 descriptor channels are hand-LOCAL**: joint offsets rotated into the hand's
own frame and divided by hand size, so they are a shape signature with no image
position in them. Transforming one would be meaningless.

Built as two more gateable halves — `coords/dv_world` and `coords/dv_pixels`, on the
same `Coordstx`/`Coordspx` toggles, because they are the same spaces. The
arithmetic is the identical `Branch` machinery: `spaces.derived_branches()` returns
the same tuple type as `transform_branches()`, so there is no second copy of the
coordinate formula anywhere.

**`coords` therefore moved to the END of the build order.** It now READS
`derive_chop` and `temporal` as well as `filter`, a consumer must state its own
input (2.11), and an input has to exist to be named. A missing source is reported
and its branches skipped, so a partial build still leaves a working group.

Hands went **499 → 531 channels**, and the delete list 84 → 100.

### 6. Text DATs and deliberate layout — DONE, and it found bugs

Every network has a `notes` DAT explaining it, and every generated coordinate comes
from `visionhands/td_layout.py` — one table, with a test asserting nothing collides.
`tools/td_verify_layout.py` checks the LIVE network for overlaps, undocumented
groups, ribbons and orphans, and it earned its keep on the first run:

- `td_add_filter.py` and `td_add_coords.py` had both been placing their group at
  **(-400, -300)**, exactly on top of each other, for as long as both existed.
- `temporal` and `latches` were laid out by a single running column counter, so
  every operator advanced one column whatever row it was on: **58 columns x 13 rows
  and 49 x 7**. One counter per row makes them 22 x 11 and 21 x 8.
- Two operators called `tmp_motion_callbacks` existed, one orphaned at the stream
  level and one sitting exactly on top of `tmp_held_prev`, because creating a Script
  CHOP auto-creates a docked callbacks DAT and the builder was destroying TD's copy
  by guessing the name it would collide into.

### And three bugs the pass turned up that were nothing to do with layout

- **`td_build_vision.py` was destroying and recreating `out1`/`out2`/`out3`**, which
  are the COMP's output connectors — so every rebuild silently disconnected whatever
  the PROJECT had wired to them. It happened during this session: three operators
  reading `vision` output 1 were orphaned, the hands stream stopped being pulled and
  stopped cooking, and the only reason it was visible at all is that `temporal` and
  `latches` kept cooking off their own clocks while `coords` and `out1` went to zero
  cooks. Nothing errored. Fixed the same way the group builders were: the Out CHOPs
  are reused, never replaced.
- **`td_add_groups.py`'s frozen-channel check compared every gated group against the
  HANDS stream's output**, which was correct while only hands had gated groups and
  reported four false failures the moment pose and face got them.
- **`tools/send_synthetic.py` was sending 137 channels where the sidecar sends 141**,
  omitting the four `sc_*` status channels — which are exactly the set that once
  vanished silently between the OSC In CHOP and the COMP output. It sends them now,
  so a synthetic session exercises the whole contract.

## Step 11 — the panel, the plumbing and two master switches — DONE 2026-08-21

The user's second list. Items are numbered as they asked them.

### The Sidecar page is gone, and so is the word (their item 4, 5a, 3)

It named an implementation detail. What was operational moved to **Vision**
(`Active`, `Camera`, `Listcameras`, the three stream toggles, `Slotassign`) and what
was diagnostic moved to **Advanced** (`Oscport`, `Printstatus`, `Capturepid`). Five
pages now, not six.

`Active` replaces the Start and Stop pulses. It is a COMMAND, not a reading — the
process can die and the toggle will still read on, which the label says and
`sc_uptime_s` contradicts truthfully.

`Camera` is a name SUBSTRING, straight into the `--camera` flag the sidecar already
had. A string and not a menu: MEASURED, this machine has four devices — the built-in
camera, an OBS virtual camera, a Camo camera and an iPhone — so a list baked at build
time goes wrong the moment one is unplugged. `Listcameras` shells out to
`python -m visionhands.sidecar --list-cameras` (new), a subprocess and not an import,
so pyobjc never enters TouchDesigner and the names come from the same code that
`--camera` matches against.

`Oscport` drives the three OSC In CHOPs through an EXPRESSION, so TD follows it
immediately while the running process keeps sending to the old port until restarted.
VERIFIED: 10000 → 10010 → 10000, ports following as 10010/10011/10012.

**Moving a parameter between pages is a destroy plus an append**, because TD will not
hold one custom parameter name on two pages and the append cannot run first. `_retire`
destroys before anything is appended, with `previous` already captured, so the four
tuned values survive. It refuses to destroy a page that still holds anything — which
is what caught the first attempt, where the four had not been listed as moving.

### `Keeplayout` (their item 2)

**Yes, the builders overwrite your positions on every run** — every one writes
`nodeX`/`nodeY` from `visionhands/td_layout.py`. `Keeplayout` (default off) makes them
leave an operator that already existed where it is.

**What it cannot hold, and the label says so:** anything INSIDE a group. `temporal`
destroys and regenerates all 73 of its operators every run, so there is nothing to
preserve. It holds the group COMPs, the stream shell, the OSC In CHOPs and the loose
operators at the top level — which is the layer a person actually rearranges. A NEW
node is always placed whatever the flag says, because TouchDesigner puts a freshly
created operator wherever it likes and "leave it alone" would mean piling every new
operator somewhere arbitrary. The rule is one function,
`visionhands.td_layout.placement`, with a test.

### `Temporal` and `Latches` master switches (their item 7)

The existing toggles could not express "off": `COOK_GATED` is an OR, so adding
`Temporal` to it would mean turning it ON keeps the group alive. Hence `COOK_VETOED`
and `cooking = any(COOK_GATED) and all(COOK_VETOED)`.

**The veto beats `COOK_REQUIRES`**, and that was a decision rather than a detail. The
latch bank gates on `temporal`'s presence signal, and `COOK_REQUIRES` exists to keep
`temporal` cooking whenever `latches` does — a frozen gate arms every latch for ever
or disarms them for ever, silently. So `Temporal` off has to take `latches` with it
rather than being overridden. `_apply_gating` prints when it does.

### Item 8: it already held, and here is the proof

Asked whether only `Lmcoordstx` being on leaves everything else frozen. It does, and
the answer needed a cook-COUNT check rather than an assertion — a group COMP's own
cook count is always 0 whatever its children do, which is the trap that nearly made
this look broken.

With `Coordstx`, `Coordspx` and `Lmcoordspx` off and `Lmcoordstx` on, plus both new
master switches off:

| gated group | cooking children |
|---|---|
| `face/coords/lm_world` | **11 / 12** |
| `hands/temporal` | 0 / 74 |
| `hands/latches` | 0 / 53 |
| `hands/coords/world` `pixels` `dv_world` `dv_pixels` | 0 / 8, 0 / 8, 0 / 13, 0 / 13 |
| `face/coords/world` `pixels` `lm_pixels` | 0 / 12 each |
| `pose/coords/world` `pixels` | 0 / 8 each |

**221 operators frozen, one half cooking, 1.5086 ms** over 42 of 317 operators — and
`face/coords/lm_world` is 0.8241 of that, which is the landmark transform doing the
only work asked of it. There was no hidden dependency to fix: `lm_world` composes
through the bounding box from `filter`, not from `coords/world`.

## Step 12 — one Script CHOP against 74 native operators — MEASURED, NOT SHIPPED

The user's question: *can a processing chain like `temporal` be reorganised into a
single Script CHOP for efficiency gains, and would we actually get them?*

**Yes, about 2.9x on that group. No, do not ship it.** Both halves are measured.

### The numbers

Alternating A/B on the same live stream, `temporal_proto` and `temporal` swapped one
at a time so neither run carries the other's machine conditions:

| | native `temporal` | `temporal_proto` | whole COMP |
|---|---|---|---|
| pair 1 | 0.2305 ms | 0.0780 ms | 2.335 / 2.279 |
| pair 2 | 0.2196 ms | 0.0775 ms | 2.252 / 2.279 |

**74 operators at ~0.225 ms against 4 at ~0.078.** The saving is **0.147 ms** — 6% of
the COMP and 0.9% of a 16.6 ms frame. The prototype's spread is also far tighter
(0.0775–0.0780 against 0.2196–0.2305), which is what one operator versus 74 should
look like.

### Why it is not shipped

The saving is real and small, and `docs/ATTRIBUTES.md` already gave three reasons
memory belongs in native CHOPs. All three survived being measured, and the third
decides it:

1. **A Trail CHOP's window is a window whatever the cook rate does.** The prototype
   counts its windows in COOKS (`ASSUMED_COOK_FPS`), which is the same thing only
   while nothing cooks twice in a frame or skips one.
2. **74 operators can be inspected** in the network with a hand in front of the
   camera. A `TemporalState` in operator storage cannot.
3. **Hidden Python state that a project reload treats unpredictably.** Every
   recurrence lives in an object a save/load knows nothing about, so a reloaded
   project resumes with whatever was there, or with nothing, and the difference is
   invisible. That is the failure this project has spent the most effort avoiding.

And the practical argument: `temporal` is already freezable. With the attribute layer
off the whole COMP is **0.6031 ms** (DESIGN.md 2.14), so a project that does not want
presence and velocity already pays nothing. Replacing 74 verified operators with 200
lines of Python to save 0.147 ms from the case that does want them is the wrong
trade.

**The prototype stays, frozen**, as `visionhands/temporal.py` (pure, 16 tests) plus
`tools/td_add_temporal_proto.py`. It is attached to nothing downstream. If the
attribute layer ever grows enough that 0.147 ms becomes 1.5, the measurement and the
code are both already here.

### Reason 3 stopped being an argument and became a save failure

The prototype held its `TemporalState` in operator storage, and the user hit this
trying to save:

    PicklingError: Can't pickle <class 'visionhands.temporal.TemporalState'>:
    it's not the same object as visionhands.temporal.TemporalState

**TouchDesigner pickles operator storage into the .toe.** So the third reason for
keeping memory native - "no hidden Python state for a project reload to treat
unpredictably" - is understated: the state does not reload unpredictably, **the
project does not save.** And the specific failure is one this repo manufactures for
itself: pickle compares the instance's class against the module's current class by
IDENTITY, and every builder purges `visionhands.*` from `sys.modules`, so after any
rebuild the stored object's class is a different object with the same name.

Fixed by moving the state into the callback DAT's module globals, which are not
pickled and are lost on a recompile - one self-correcting frame for a level-driven
recurrence. Two things learned that apply beyond the prototype:

- **store only primitives.** `derive_chop`'s channel-name cache is a tuple of strings
  and was never affected; verified by pickling every storage entry in the project.
- **storage outlives the code that wrote it.** Deleting the `store()` call does not
  delete the value, so the builder now `unstore`s the stale keys explicitly - without
  that, the save keeps failing against a callback that no longer writes anything.

If this were ever shipped, that is the problem to solve first, and it is not a small
one: a recurrence's state has to survive a save/load in a form TouchDesigner can
serialise, which is most of what a Feedback CHOP is giving you for free.

### Two measurement traps this turned up, and both nearly reversed the verdict

**A DAT that COMPILES during the window reports the compile as its `cookTime`.**
`proto_callbacks` showed **0.2874 ms with 1 cook** while everything else cooked 89
times, and summing it into the group total made the prototype look 74% SLOWER than
the native version instead of 2.9x faster. `tools/td_profile.py` now flags any
operator whose cook count is under a tenth of the busiest as "ONE-OFF, do not sum".

**`cookTime` is wall-clock, so external load inflates everything.** A set of readings
taken while a browser was using 60% of the machine put the whole COMP at **12.8 ms**
and the achieved rate at 26 frames in 89 — and nothing in the numbers said so. The
profiler now reports the ACHIEVED FRAME RATE and prints a warning below 45 fps. The
clean runs above are all at 60.0.

That second one is the more dangerous, because every figure in the run is
self-consistently wrong. Any figure in this repo not accompanied by an fps should be
treated as suspect.

## Step 13 — depth and out-of-plane angles — DONE 2026-08-21

The user's items 5b and 6: *hand and finger Z from hand size, and hand XY angles in
addition to the current Z angle,* both behind toggles. Two new `derive()` groups,
`Depth` and `Tilt`, both shipping **off**. 16 channels across two hands.

### They asked about the right thing

Every angle this system published was in-plane. `h{i}_rotation`,
`h{i}_point_angle`, `h{i}_pinch_angle`, `hands_angle` — all `atan2` on two image
coordinates, all rotations about the camera's Z axis. `ATTRIBUTES.md` even labelled
`h{i}_rotation` "In-plane roll". So the observation was exactly right.

### What could be built honestly, and what could not

| asked for | delivered | why |
|---|---|---|
| hand Z from hand size | `h{i}_z` | apparent size goes as 1/distance. Monotonic, uncalibrated, and not comparable between two different hands |
| finger Z | `h{i}_z_<finger>` | foreshortening: how much the finger points along the axis rather than across it. **Cannot tell toward from away** |
| hand XY angles | `h{i}_tilt`, `h{i}_tilt_axis` | NOT pitch and yaw — see below |

**Pitch and yaw cannot be signed from a 2D projection.** A palm tilted 30 degrees
toward the camera and one tilted 30 away project identically, because a projection
destroys which side of the image plane a point is on. Publishing unsigned values
under those names would misrepresent them, so the channels are magnitude plus axis:
`acos` of the palm triangle's apparent area over its face-on area, and the direction
of the longest surviving edge. Two honest numbers instead of two dishonest ones. The
ambiguity is asserted as a test so nobody later "fixes" it.

### The calibration constant that was a 62-degree dead zone

`Palmarea` is the face-on area of the palm triangle as a fraction of size squared,
and `tilt` is `acos` of the measured ratio over it — so it is the channel's zero
point. I first guessed **0.14**. MEASURED against `synth.py`: **0.3059**. The guess
put the ratio above 1.0, where the clamp pinned it, so `tilt` read exactly **0 for
the first 62 degrees of real rotation** — a channel that looked like it worked and
was useless.

It is now set at the measured value rather than above it, deliberately: too low gives
a small dead zone near zero, too high reports tilt on a hand that has none, and there
is no value of the channel that means "flat" in the second case. A test asserts which
failure mode it should have.

**The confound, measured and not corrected for:** the triangle's corners include two
MCP knuckles, and the area ratio moves with finger SPREAD — 0.1835 at spread 0.6,
0.4282 at 1.4. Spreading the fingers reads partly as un-tilting the palm. A real
hand's knuckles move far less than the synthesiser's, so the true confound is
smaller, but it is not zero.

### And a gap this uncovered: eleven parameters that never existed

`derive()`'s `_params()` has always read its thresholds by name with a getattr
fallback — and **nothing ever created them.** `Confthreshold`, `Sizefloor`,
`Curlmin`, `Curlmax`, `Extendedbelow`, `Curledabove`, `Spreadmin`, `Spreadmax` and
`Overlapthreshold` have been stuck at their dataclass defaults since the day they
were written, with the panel giving no sign. Every "adjustable" threshold in the
stateless attribute layer was a constant.

They are on the Tuning page now, built by iterating `dataclasses.fields(Params)` so
the page and the dataclass cannot drift. The hand-written `_params()` they replaced
had already drifted: it named eight of the nine fields that existed, so
`Overlapthreshold` was doubly unreachable.

**Everything here is unmeasured on a real hand.** The arithmetic is exact and tested
(17 tests); whether the numbers feel right at a real distance is not established, and
`Zreference` and `Palmarea` are what to move.

## Step 14 — one output, and a trim that follows the toggles — DONE 2026-08-22

The COMP had three output connectors, one per stream, and with everything enabled
they carried 1,863 channels between them. Asked for by the user, in their words:

> I'm going to be rolling out this component to beginners and don't want them to
> immediately get 1000 channels in the out. I want to make it so they can have a
> very limited list they can understand.

**The shape.** `hands`, `pose` and `face` now merge into `merge_streams`, that feeds
`trim_empty`, and that feeds the only `out1`. `Delete Empty Channels` on the Vision
page (on by default) bypasses the trim. Node positions are in
`visionhands/td_layout.py` like everything else.

**The trim's list is generated, never written by hand.** `tools/td_add_groups.py`
owns it, because it is the only thing that knows which groups are frozen, and it
reads the channel names off the NETWORK — each group's `out1` IS the set of channels
that group contributes. No channel-to-group map, which is the map that does not
exist and would go stale the first time a builder added a channel.

### The Delete CHOP had to become a Select

It was built as a Delete CHOP first, because "delete the empty channels" is what was
asked for. That cost **0.6697 ms** — 28% of the whole component's budget — and the
Select that replaces it costs **0.0555 ms** for the same reduction. `DESIGN.md` §2.15
has the table. The user's call, made on the numbers.

Three things the swap changed, all handled:

- a keep list **fails closed**, so `groups_callbacks` now watches every toggle in
  `GROUPS`, not just the ones that gate cooking. Otherwise turning `Descriptor` on
  would compute 84 channels and throw them away.
- a Select emits in `channames` order, so the list is generated in MERGE order and
  the output's channel order is unchanged.
- an empty list is not free, so the Select is bypassed whenever nothing is frozen.

### Two defects this found, neither of them in the new code

**A frozen group's channels vanish after a forced cook** (`DESIGN.md` §2.16).
`hands/temporal` was reporting 0 channels instead of 27, `hands/latches` 0 instead
of 70, and `hands` 406 instead of 503 — silently. `td_build_vision.py`'s
`comp.cook(force=True)` was the trigger, and every builder that ran after it
reported false failures against the empty groups. Fixed on both sides: the builder
does a plain `cook()`, and `_apply_gating` now cooks each group it is about to
freeze unconditionally rather than only when the group has never cooked.

**`td_add_latches.py`'s self-check compares against the full latch table** whatever
is enabled, so with `Pose` off it reports 9 failures for the two `grab` latches it
correctly dropped, plus packing order. Pre-existing, unfixed, and NOT the new code —
it fails the same way before this step.

### What existing patterns this breaks

Anything wired to output connector 1 or 2 was moved to 0 by the builder, which
captures every consumer before destroying `out2`/`out3` and repoints it. But a
pattern that matched one stream now sees all three:

| operator | before | after |
|---|---|---|
| `select3` `*_tx`, `select4` `*_ty` | hands only | hands, and pose and face too when those streams are on and the trim is bypassed |
| `null2`, `null3` | pose, face | the single merged output |
| `select5` `*trigger*` | 24 channels | 0 while `Latches` is off — the trim removes them, which is the point |
| `select6` `select7` `*f0*_x`/`_y` | face | 0 while `Streamface` is off |

## Step 15 — making the next change cheap — DONE 2026-08-22

Asked for after a three-operator change took a full session. The diagnosis was not
the builder infrastructure — it is what let a wrong operator choice be a
twenty-minute fix — but three things around it.

### 1. The chain was all-or-nothing: `tools/td_rebuild.py`

Eight builders, in order, every time. Now:

    TARGET = "master"          # or ("coords", "latches"), or "all"
    run(".../tools/td_rebuild.py")

`LAYERS` maps a layer to its script in chain order; `REQUIRES` says what a change
drags along, written out by hand per layer with a reason, because a builder's
dependencies are a fact about what it destroys and reads rather than something to
infer. `master` is 2 builders instead of 8. An unknown layer name RAISES — "I asked
for a rebuild and got no output" is the worst way to find a typo.

What made that possible: `td_build_vision.py` stopped destroying master-level
operators other builders own (`OTHER_BUILDERS_OWN` — the filter, gating, latch
threshold and screenspace callback DATs, and the profiler). It used to remove them
and leave the COMP with no gating callback, so the only safe move was to run
everything afterwards.

### 2. Two self-checks that cried wolf

Both reported failures on correct networks, every run, which meant triaging them
before every real failure could be seen.

- **`td_add_latches.py`** compared against the full 15-row table. `Pose` off drops
  `h?_openness` and with it the two grab latches — documented behaviour — so it
  reported 9 failures. The naive fix (compare against 13) reported 15, because the
  network really is built 15 wide: the constant CHOPs have a block per row and the
  validity fan reads `h?_valid`, which is always present. Only the operators
  downstream of the distance fan narrow to 13. So the check now compares against the
  full table with the dropped rows' names ALLOWED to be absent and nothing else
  tolerated, and it also catches a duplicate name, which the old equality could not.
  24 checks, 0 failures.
- **`td_add_coords.py`** reported "produced 0 channels, expected 152" for a frozen
  stream and four rename failures for a branch reading a frozen group. Both now say
  "built, not verified" — a rename of nothing produces nothing, and the map is still
  correct; it just cannot be demonstrated.

**The whole chain now runs with zero failures**, which it had not done all session.

### 3. The documentation obligation

`STANDARDS.md` asked for five documents per change. DESIGN 2 (measurements) and
ATTRIBUTES (user-facing behaviour) have both earned their place — every measurement
in 2 has changed a decision. BUILD_PLAN had become a second journal, and HANDOFF
does not need rewriting mid-session. See `STANDARDS.md` 4.

### And the defect all this uncovered

`append*` on an existing parameter resets it, the reset FIRES a deferred callback,
and a menu resets to index 0. `Verbosity` reverting to `Minimal` turned every derive
group off and took the output from 505 channels to 366, silently, only when two
builders ran in the same frame. `DESIGN.md` 2.17. Fixed by never re-appending.

## Step 16 — the segmentation transport, built and measured — DONE 2026-08-22

Step 9 researched the transport and parked it with a decision. This is that decision
built: `visionhands/maskbuf.py` and `visionhands/segmentation.py`, 54 tests, no
TouchDesigner side yet.

### `maskbuf.py` - a file-backed mmap with a seqlock

One slot, not a ring: a mask is only ever wanted at its newest, so a queue would add
latency to deliver frames nobody will draw. The file is sized ONCE for the largest
frame it will carry and the header says how much is live, so a quality change is a
header write rather than a remap - the reader is TouchDesigner's main thread and not
somewhere to discover that a file just got shorter.

Three layers of coherence, each earning its place: an ODD sequence number says a
write is in progress; a trailing counter catches a whole write that started and
finished inside the reader's copy; and a fence - an uncontended `threading.Lock`
acquire/release - because the CPU may otherwise make the tail visible before the
pixels. That last one is honest rather than proven: `DESIGN.md` 2.19 says what it is
and is not, and the residual risk is one bad mask frame on a stream that replaces it
in 16 ms.

Measured: **0.02 ms a read**, 0 to 4 retries per 108 reads at realistic writer rates,
and **zero torn frames across 56,591 accepted** from a writer in a separate process
running flat out. That last test is the only one that proves anything - a
single-process test shares an address space and cannot expose a visibility problem.

### `segmentation.py` - one Vision request, and bytes out

Same boundary rules as `face.py` and `pose.py`: no TouchDesigner import, and no
pyobjc object escapes the capture thread, which is why the mask is COPIED out of the
CVPixelBuffer here rather than passed along.

Two traps handled rather than discovered:

- **row padding.** `CVPixelBufferGetBytesPerRow` is not `width * bytesPerPixel`.
  Handing the padded bytes on as an image shears it progressively down the frame,
  which reads as a bad mask rather than as a bug. Un-padded here, asserted in tests.
- **construction.** `init` and `new` are both NS_UNAVAILABLE on this request class,
  alone among the Vision requests in this project. `initWithCompletionHandler_(None)`
  is the way in, and it took a probe to find.

### What is NOT done

The TouchDesigner side. A Script TOP with `numpy.memmap` and `copyNumpyArray`, and
the flip - Vision's mask is top-down and TD's TOP arrays are bottom-up. Deliberately
not started here: `maskbuf.py` moves bytes and does not reorient its payload, because
a transport that silently flips its contents cannot be tested against a known
pattern. The flip belongs in the Script TOP, and so does undoing the anisotropic
stretch, for which `MaskFrame.source_scale` hands over both factors.

Also not done: wiring segmentation into `sidecar.py` so it runs on the live capture
queue beside hands, pose and face. `detect_sample_buffer` is there and unexercised -
the fixture path uses `detect_pixel_buffer`.

    ~/.venvs/visionhands/bin/python tools/segmentation_probe.py --png /tmp/masks

runs the whole pipeline in two real processes and writes the mask both as published
and stretched back to its source, which is the shortest available explanation of why
the header carries the source geometry.

## Step 17 — the mask into TouchDesigner — DONE 2026-08-22

`tools/td_add_segmentation.py`, and `td_rebuild.py` gained a `segmentation` layer.

    seg_mask (Script TOP)  ->  seg_fit (Fit TOP)  ->  outmask (Out TOP)
       reads the mmap,            undoes the              a TOP output, and the
       flips y                    stretch, on the GPU     COMP's second family

**Three operators, each doing the one thing it is cheapest at.** The Script TOP does
what only Python can - read the shared buffer - and the y-flip, because it is the
operator that knows the convention. The Fit TOP undoes the anisotropic stretch on the
GPU, where a numpy resize would have cost a 1280x720 resample per frame on the main
thread. MEASURED: 0.0915 ms and 0.0312 ms, so about 0.12 ms for the whole path.

**The orientation is verified with a KNOWN PATTERN, not with a mask.** A mask looks
plausible either way up, which is precisely why it cannot be the test. A frame with a
255 band across the payload's first 24 rows and a 180 block down its first 24 columns
shows up in TouchDesigner as a band at the top and a block at the left - so the flip
is right and there is no accidental x-mirror.

### Four TouchDesigner facts this needed

- **`copyNumpyArray` wants three dimensions**, `(h, w, components)`. A plain `(h, w)`
  raises. `uint8` is accepted natively - no float32 widening for a mask - and
  `float16` is refused outright. It also SETS the TOP's resolution from the array.
- **`allowCooking = False` raises on anything that is not a COMP:** "This flag can
  only be disabled for COMPs". So the gating pattern every CHOP group here uses is
  unavailable to a bare operator, and `Segment` is checked inside `onCook` instead.
- **`format = mono8fixed`** keeps a mask at one byte per pixel on the GPU. The default
  would carry four.
- **`fit = fill`** is the non-uniform stretch. `fitbest` letterboxes, which would be
  right if Vision letterboxed - it does not (DESIGN.md 2.18).

### And one hardening the reader needed first

A writer restarting at a different quality re-creates its buffer at a different size,
and touching a page past the end of a file that SHRANK is a **SIGBUS** - inside
TouchDesigner that is the user's whole project with no traceback. `MaskReader.read`
now stats the file every read and remaps when `(st_ino, st_size)` changes. The inode
matters as much as the size: a writer that recreates its buffer at the same geometry
gets a new inode, and a reader still on the old one reports "nothing published yet"
for ever, which looks exactly like a sidecar that failed to start.

### Still not done

`visionhands/sidecar.py` does not run segmentation, so `Segment` ships OFF and there
is nothing to read until something writes. `detect_sample_buffer` is there and
unexercised. That is the next piece, and it needs the camera.

## Step 18 — segmentation on the live capture queue — DONE 2026-08-22

Wiring, not new machinery: `segmentation.py` and `maskbuf.py` already existed and the
Script TOP already read the buffer. What was missing was the sidecar actually running
the request.

### `segment` is a request, not a stream

`visionhands/streams.py` now has two tuples. `STREAM_NAMES` is what has a UDP port;
`REQUEST_NAMES` is that plus the portless requests, and it is what `--streams` accepts
and what the status channels are built from. A 197 KB mask has nowhere to go on a
datagram, so `port_for("segment")` **raises by name**, saying where the mask actually
goes rather than returning a plausible number.

`sc_segment` is the fifth status channel, for the reason the other four exist: a panel
showing segmentation on, after somebody flipped it without restarting, is lying. The
ORDER is the port order followed by the portless requests, and `_self_check` asserts
that `REQUEST_NAMES` starts with `STREAM_NAMES` - reordering it would rename live
channels.

### Where each piece lives

    engine.py     `segmentation_detector` + `on_mask`, and `_publish_mask` LAST on
                  the queue - behind hands, pose and face, because at 2.21-30.73 ms
                  it is the most expensive of the four (DESIGN.md 2.18)
    source.py     builds the detector when the flag is on AND there is somewhere to
                  publish. Both or neither: a detector with nowhere to go would burn
                  2.21 ms a frame and drop the result, and `errors` records the flag
                  that did nothing
    sidecar.py    owns the MaskWriter, the way it owns the socket. `_write_mask` runs
                  on the capture queue - which is what MaskWriter is written for

**The buffer is created lazily, from the first mask's own geometry.** Not from a
table: the size depends on the quality level AND the camera's orientation (a portrait
frame gets a 3:4 mask), so anything guessed in advance is a buffer that refuses every
write the day Vision changes a number.

**The source geometry is what the camera DELIVERED, not what was requested.** The
session preset silently reverts `setActiveFormat_` and two spike runs produced 1080p
while the log said 720p (DESIGN.md 3) - a mask scaled against the requested size would
be off by the difference, invisibly. Unknown is stated as `(0, 0)`, which
`source_scale` turns into `(1.0, 1.0)`: as wrong as having no field, and not misleading.

**`stop()` stops the source BEFORE closing the buffer**, and that order has its own
test. `_write_mask` runs on the capture queue; closing first would leave a frame in
flight writing into a closed mmap from a thread still running. It `close`s and does
NOT `unlink` - a reader inside TouchDesigner may still hold the mapping, and leaving
the file is what lets TD show the last mask instead of going black the moment the
sidecar exits.

### One toggle, not two

`Streamsegment` on the Vision page puts `segment` on the sidecar's command line AND
gates the Script TOP. `tools/td_add_segmentation.py` lost its own `Segment` toggle:
the read side and the write side are always wanted together, and two toggles a letter
apart would have been a trap of my own making. `Segquality` and `Maskbuffer` moved to
`td_build_vision.py` for the same reason - they are launch flags, and `start()` has to
be able to read the path before anything has read a mask.

### And one check that cried wolf immediately

Adding `sc_segment` made the contract 142 channels while the OSC In CHOP still held
the previous session's 141, so `td_add_filter.py` reported "the filter's scope covers
83 channels, expected 84" on a filter that was correct. A contract that has grown but
has not yet been SENT is the normal state between a code change and the next Start.
Both sides of that check now count from what actually ARRIVED, and it prints a note
when the contract is ahead of the wire.

### Not verified yet

The live path. Everything here is tested with a fake source and a stand-in mask -
506 tests, no camera - and the whole chain builds with zero failures. What has NOT
been run is a real sidecar publishing real masks into TouchDesigner, because that
needs the camera.

## Step 19 — why the mask was black — DONE 2026-08-22

Step 18 shipped the wiring untested on the live path, and the first thing the user
tried came back black. The sidecar was blameless: the buffer held frame 666 with
16,599 of 49,152 pixels lit and the source geometry stated, and reading it by hand
from the callback's own module worked first time.

**The Script TOP was not cooking at all.** `totalCooks` sat at 151 across many
frames. An input-less Script TOP has no dependency to dirty it and no Cook Type
parameter to override that with, so the single cook it had done - before the sidecar
created the file - published the blank and it served that texture for ever.

Fixed with a `Tick` parameter on the Script TOP:

    absTime.frame if op.Vision.par.Streamsegment else 0

Verified both directions: cooks climb with the toggle on, settle with it off. Which
also gives `Streamsegment` real gating, the thing `allowCooking` refused a bare
operator in step 17.

**The blank is 16x16 now, not 256x192.** It was exactly the size of a `fast` mask, so
"never read the buffer" and "read a real mask" were indistinguishable in the
operator's resolution - which is what turned a five-minute diagnosis into a long one.

The `CookLevel` note above already recorded this behaviour for an input-less Script
CHOP. It cost time again because it was filed under the operator family it was first
seen in rather than under the behaviour, so it now says both.
