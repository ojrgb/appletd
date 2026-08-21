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
`tools/td_build_pose_comp.py` for the TouchDesigner side. 61 new tests.

**Measured in TouchDesigner:** 123 pose channels arriving on port 10001 with the
right names and moving values, and the hands port now carrying 141 (137 + four
`sc_*`) with the COMP output at 499. DESIGN.md 6.4 has the design and 2.12 the API
surface; the journal has the four things that went wrong.

**visionface followed the same afternoon**, and one number short of complete:
`visionhands/face_types.py`, `visionhands/face.py`, `visionhands/synth_face.py`,
`tools/send_synthetic_face.py`, `tools/td_build_face_comp.py`. **Verified in
TouchDesigner: 23 channels on port 10002**, head pose sweeping, with no camera.

**The missing number, and why it is missing rather than guessed.** A face is not a
joint table: `VNFaceObservation.landmarks()` gives **12 named REGIONS**, each with
its own `pointCount`, and nothing in the framework publishes those counts before a
face has been observed. The constellation pins the TOTAL at 76 and says nothing
about the split. A guessed split lays a nose's points into an eyebrow's channels and
looks entirely plausible, so the table carries `point_count = None`, no landmark
channels are published, and `tools/probe_face_regions.py` settles it from one
camera frame - printing integers, writing no image. Paste the counts in, re-run
`tools/td_build_face_comp.py`, and the points appear with no other change.

**What is NOT done:** the face landmark points (above), and any attribute layer for
either new stream - no derived channels, no filtering, no temporal channels, by
design. The brief was plumbing plus basic testing.

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
