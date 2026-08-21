# Handoff — 2026-08-21

**342 tests**, `ruff check` and `mypy visionhands` clean. Working tree clean apart
from `tools/vision_landmarks.py` and `tools/vision_landmarks_live.py`, which are
the user's own untracked files — leave them alone.

Body pose AND face landed this session — both end to end, both verified in
TouchDesigner with no camera. See `docs/BUILD_PLAN.md` step 5 and the last two
journal entries. **Four separate silent channel losses** came out of the pose work,
three in code that already existed; they are all in `DESIGN.md` 2.11 now.

**One thing is deliberately unfinished and it needs ten seconds of camera:** the
face stream publishes head pose and the bounding box but NOT the 76 landmark points,
because their split across the 12 regions cannot be known before a face has been
observed and a guessed split misaligns a region invisibly.
`tools/probe_face_regions.py` prints the counts from one frame, writes no image, and
keeps nothing. That is the only outstanding piece of the streams work.

## Read these, in this order

1. **`docs/BUILD_PLAN.md`** — the task list. Steps 1, 4, 6 and 7 are done; step 2
   is partly done with a table of what remains and what each piece needs; steps 3,
   5 and 7.2 are open.
2. **`design/DESIGN.md` §2** — measured fact, and the only section that is
   reliably current. **§2.10 and §2.11 are the ones to read before touching
   TouchDesigner**: every rule in them cost real time, several cost it twice.
3. **`docs/ATTRIBUTES.md`** — the channel spec. Every formula there is the
   definition of that channel.
4. **`docs/JOURNAL.md`**, last four entries — what was built and what went wrong.
5. **`docs/STANDARDS.md`** — binding. Comment style, the review gate, `ruff check`
   not `ruff format`.

## What is running

One sidecar process, one camera, **one Vision request per enabled stream**, one UDP
port each, and - since 2026-08-21 - **one COMP around all three** (`DESIGN.md` 6.4
and 6.5):

    /project1/vision          every parameter, six pages, the sidecar control
      hands_osc 10000  ->  hands  ->  out1     499 channels
      pose_osc  10001  ->  pose   ->  out2     275
      face_osc  10002  ->  face   ->  out3      39
      status                                   the sidecar's own sc_* channels

**Paths changed in that restructure:** `/project1/visionhands` is now
`/project1/vision/hands`. Every expression inside reads `op.Vision.par.X` through a
Global OP Shortcut rather than `parent(2).par.X`.

Each stream has its own `filter` and `coords` group, reading the master's single set
of controls - so one `Smoothing` toggle and one `Orthowidth` serve all three.
`hands` adds `derive_chop`, `temporal` and `latches`; pose and face have no
attribute layer, by design.

Build order from scratch. Run end to end and verified on 2026-08-21:

    td_build_vision.py -> td_add_filter.py -> td_add_coords.py -> td_add_derive.py
    -> td_add_temporal.py -> td_add_latches.py -> td_add_groups.py

Every builder is idempotent and is run by pasting into a Text DAT, or via the MCP
with `exec(open(path).read())`. **Four builders were retired** in the restructure -
`td_build_comp.py`, `td_build_pose_comp.py`, `td_build_face_comp.py` and
`td_setup_osc.py`; the last would bind port 10000 a second time.

Six parameter pages, all on the master: Vision (resolutions, `Orthowidth`), Sidecar
(Start/Stop, `Slotassign`, `Streamhands`, `Streampose`, `Streamface`), Filter
(`Smoothing`, `Mincutoff`, `Beta`), Advanced (debounce frames, `Velocityfilter`,
`Speedfloor`), Tuning (latch thresholds), Attributes (group toggles, `Verbosity`).


## How to test anything

No camera and nobody in frame:

    ~/.venvs/visionhands/bin/python tools/send_synthetic.py --list
    ~/.venvs/visionhands/bin/python tools/send_synthetic.py ramp --cycles 4

Sequences: `ramp` `snap` `ring` `little` (per-finger contact), `curl` (grab),
`clap`, `both`, `crossing` (slot assignment — use `--unstable`), `deadband`
(hysteresis), `swipe` (sustained motion), `open`, `absent`.

And for the pose stream, which needs nobody in frame either:

    ~/.venvs/visionhands/bin/python tools/send_synthetic_pose.py --list
    ~/.venvs/visionhands/bin/python tools/send_synthetic_pose.py two --cycles 4

`walk` `wave` `two` (two people crossing — the slot rule) `depth` `absent`. And for
face:

    ~/.venvs/visionhands/bin/python tools/send_synthetic_face.py turn

`turn` `tilt` `approach` `two` `absent`. Every sender refuses to run while the
sidecar is up, because two writers on one port interleave into a plausible wrong
answer rather than a visible fault.

**Anything with memory cannot be verified from one frame.** The pattern that works
is counter deltas over a generated sweep — `tools/td_verify_latches.py` does the
before/after bookkeeping. It refuses to run alongside the sidecar, because
interleaved streams give a plausible wrong answer rather than a visible fault.

## The things most likely to waste your time

All measured here, all in `DESIGN.md` 2.10/2.11, restated because they recur:

1. **Nothing that crosses a frame boundary can be verified inside one TD script.**
   It lied four times: a cook-count comparison, a velocity "distribution" that was
   one value repeated, a Verbosity preset that appeared to do nothing three runs in
   a row, and `time.sleep()` in a script blocking the frame loop it was measuring.
2. **`op.inputs` / `op.outputs` go stale; `inputConnectors[i].connections` is the
   truth.** Cost time three times, once reporting an input list that implied a
   loop.
3. **A branch with memory needs its own clock** (a Null CHOP at Cook Type =
   Always), and it must sit inside the group it clocks. Symptoms are silent: the
   state looks right and the edge pulses are computed between frames that were
   never adjacent.
4. **Sample-axis operators (Slope, Filter) are inert on a NON-time-sliced
   one-sample CHOP and work fine on a time-sliced one.** The over-broad version of
   this rule cost 21 operators reimplementing a filter TouchDesigner already has.
5. **TouchDesigner caches imported modules for the process's life.** Purge
   `visionhands*` from `sys.modules` at the top of any builder, or your edits are
   invisible.
6. **A builder that repoints its CONSUMERS depends on build order, and lost.**
   `td_add_filter.py` forced a named list onto its output; two of the five names
   had moved into a group, and `derive_chop` does not exist yet when the filter is
   built - so it wired itself to the RAW input and stayed there. Every derived
   attribute was computed on unsmoothed landmarks while the coordinate spaces used
   smoothed ones. **Each consumer states its own input**, which is the only
   arrangement that cannot depend on build order.
7. **A channel can disappear between the OSC In CHOP and the COMP output with
   nothing saying so.** Four separate instances in one afternoon (2026-08-21):
   `merge.inputs` reports a group COMP as its inner `out1`, so a name-keyed dedup
   collapsed two groups into one and dropped 168 channels; the filter group ate
   four channels that were in neither of its two named Selects; `^` does not
   exclude in a Select CHOP's `channames` and the terms are ADDITIVE, so
   `* ^*_x ^*_y` selected 423 channels from a 141-channel input; and a newly
   appended parameter reads 0 whatever `.default` says. **Compare a data-path
   group's output against its INPUT**, not against the contract — the contract is
   not what arrived.

And: `cook(force=True)` right after building anything makes every downstream
`cookTime` meaningless — one profile read 2.946 ms and looked like a regression
when the real number was 1.030.

## Next

**1. The face landmark points — ten seconds of camera, then one paste.** Everything
else about the face stream is built and verified. Run, with a face in frame:

    ~/.venvs/visionhands/bin/python tools/probe_face_regions.py

It prints a `FaceRegionSpec` block, writes no image and keeps nothing. Paste it over
`FACE_REGIONS` in `visionhands/face_types.py`, re-run `tools/td_add_filter.py` -
which is what classifies the new channels, and anything it does not classify is
dropped silently - and the 76 points appear as channels. The coordinate builder
needs no re-run: those points are box-relative, so they are not transformed. The self-check refuses a half-filled table and
one that does not sum to 76, so a typo fails at import. **Ask before running it** —
it is the only tool in the repo that opens the camera.

**2. An attribute layer for pose or face, if either is wanted.** There is none: no
derived channels, no filtering, no temporal channels, and nothing depth-invariant.
That was the brief (plumbing plus basic testing), not an oversight.
`send_synthetic_pose.py depth` and `send_synthetic_face.py approach` both
demonstrate what a naive consumer gets wrong.

**3. A cook-time DISTRIBUTION, if the number ever matters.** Two single-frame
reads with data provably flowing gave **0.997 ms and 1.187 ms** over 162 operators
and 499 channels, against 1.040 ms measured flat — so grouping is cost-neutral and
`allowCooking` now saves about 0.5 ms of it. Anything finer needs a Trail on
`cookTime` across frames, because reads inside one script all return the same
frame's value.

### Done 2026-08-21: one COMP around the three streams

The restructure asked for after face landed: shared filter, shared coordinate
spaces, shared sidecar control, all at the master level, transformations on all
three streams. `docs/BUILD_PLAN.md` step 8 and `DESIGN.md` 6.5 have it. Pose went
123 channels to **275** and face 23 to **39**, both gaining world and pixel space.

The piece worth knowing before touching either builder: **`visionhands/spaces.py`
decides which channels get smoothed and transformed, and with what rule** -
positions are offset by -0.5, extents are not, angles are smoothed but never
transformed, and face landmark points are `box_relative` (normalised to the
bounding box, so image-space rules would put every feature in one corner). A
pattern cannot make those distinctions and has failed at it twice.

### Done 2026-08-21: `allowCooking` gating

`temporal` and `latches` bind `allowCooking` to their Attributes toggles, applied by
`groups_callbacks` — an attribute cannot take an expression, so it is written on
change, the same pattern `filter_callbacks` uses. Presence or Motion keeps
`temporal`; Triggers, Gestures or Events keeps `latches`; and `latches` cooking
forces `temporal`, because it reads temporal's presence gate and a frozen gate
either arms every latch forever or disarms them forever, silently.

Proved by counter delta, which is the only honest way here: three toggles off gave
`latches` **0 cooks against 377** for temporal and coords over the same interval.
Frozen groups keep their channels on the COMP output, holding their last values.

**`filter` is not a candidate** and should not be made one: it is in the data path,
so freezing it would freeze every position rather than bypass the filter.

### Done 2026-08-21: the pose and face streams

Left here because the reasoning is what generalises to whatever comes next:

- **Toggles are LAUNCH FLAGS**, read once at startup. The sidecar is a separate
  process TouchDesigner starts with `Popen`, and `start()` in
  `tools/td_build_vision.py` already builds the command line — a stream toggle is a
  flag appended there, exactly as `--slots` is. Restart to apply. No control
  channel into the sidecar, no live reconfiguration of a running capture session.
- **The channel contract stays CONSTANT whatever is enabled.** A disabled stream
  sends zeros; it does not stop sending. TD's OSC In CHOP creates channels as they
  arrive, so omitting them makes them VANISH — and `DESIGN.md` 6.2 records that a
  downstream reference to a vanished channel fails silently, with no error
  anywhere. The saving worth having is the inference, not the bytes.
- **The sidecar reports which streams it actually started**, as channels, so the
  panel cannot lie after someone flips a toggle without restarting.
- **Pose first.** `VNDetectHumanBodyPoseRequest` returns the same
  `VNRecognizedPointsObservation` shape as hands, so `types.py`'s joint-table
  pattern and `derive()`'s structure both carry over. Then face.
- **Segmentation is deferred to the C++ path.** It is a mask, not floats, so it
  breaks the "137 floats and no pixels" property (`DESIGN.md` 4.2) whatever the
  launch flags say. Datum from the user: an existing TouchDesigner segmentation
  plugin **pins the frame rate at 50 fps**, so the question is whether 10 fps is an
  acceptable price, not whether it is possible.

**Deferred to phase 2 by decision:** the remaining temporal channels —
`hands_twist`, `steadiness`, `dwell`, `hands_scale`, and the
swipe/wave/cross/twist events. Step 2 of the build plan lists what each needs.

## Waiting on the user

- **A two-hand fixture**, for one question only now: whether real Vision's
  chirality flickers or drops out edge-on, which decides how often slot
  assignment's proximity fallback is reached.
- **Threshold feel** generally. Every threshold not listed above is a guess.

`Triggeron`/`Triggeroff` are now **0.13 / 0.17, measured and confirmed** — the only
measured thresholds in the system, replacing a guess that was three times too loose
and fired while the fingers were still visibly open. Treat that as the cautionary
case for every remaining threshold: synthetic geometry validates arithmetic and
says nothing about how a hand sits.

## Deferred by the user — decisions, not oversights

README; per-joint jitter measurement; `Sidecar.run()` tests (five mutations survive
a green suite, including one where the loop sends nothing); two-hand behaviour of
the tracker.

## Standing constraints

- **Ask before using the camera** — it usually has another owner, and almost
  nothing needs it.
- **`reference/` is frozen.** It is the provenance of every measurement in
  `DESIGN.md` §2.
- **`git add` explicit paths, never `-A`** — it once swept the user's unrelated
  work into two commits.
- Fixtures must show hands only, checked with `VNDetectHumanRectanglesRequest` and
  by looking at frames.
- Nothing may touch a TouchDesigner object from any thread but the main one.
- Promote new measurements into `DESIGN.md` §2 with their figures; a stale comment
  is a defect; one commit and one journal entry per milestone.
