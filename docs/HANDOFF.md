# Handoff — 2026-08-21

State at `469f251`. 223 tests, ruff and mypy clean. Working tree clean apart from
`tools/vision_landmarks.py` and `tools/vision_landmarks_live.py`, which are the
user's own untracked files — leave them alone.

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

Camera → sidecar (own process) → OSC 10000 → `/project1/visionhands` → **495
channels**. Top level is 16 operators: four group COMPs (`filter`, `coords`,
`temporal`, `latches`), `derive_chop`, `merge_out`, `out1`, the sidecar DATs.

Build order from scratch — each group wires to the one before:

    td_build_comp.py → td_add_filter.py → td_add_coords.py → td_add_derive.py
    → td_add_temporal.py → td_add_latches.py → td_add_groups.py

Every builder is idempotent and is run by pasting into a Text DAT, or via the MCP
with `exec(open(path).read())`.

Six parameter pages on the COMP: Visionhands, Sidecar (Start/Stop, `Slotassign`),
Tuning (latch thresholds), Filter (`Smoothing`, `Mincutoff`, `Beta`), Advanced
(debounce frames, `Velocityfilter`, `Speedfloor`), Attributes (group toggles,
`Verbosity`).

## How to test anything

No camera and nobody in frame:

    ~/.venvs/visionhands/bin/python tools/send_synthetic.py --list
    ~/.venvs/visionhands/bin/python tools/send_synthetic.py ramp --cycles 4

Sequences: `ramp` `snap` `ring` `little` (per-finger contact), `curl` (grab),
`clap`, `both`, `crossing` (slot assignment — use `--unstable`), `deadband`
(hysteresis), `swipe` (sustained motion), `open`, `absent`.

**Anything with memory cannot be verified from one frame.** The pattern that works
is counter deltas over a generated sweep — `tools/td_verify_latches.py` does the
before/after bookkeeping. It refuses to run alongside the sidecar, because
interleaved streams give a plausible wrong answer rather than a visible fault.

## The five things most likely to waste your time

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

And: `cook(force=True)` right after building anything makes every downstream
`cookTime` meaningless — one profile read 2.946 ms and looked like a regression
when the real number was 1.030.

## Next — decided 2026-08-21

**1. The additional vision streams. This is the next task.** Every design question
is settled, so it is implementation:

- **Toggles are LAUNCH FLAGS**, read once at startup. The sidecar is a separate
  process TouchDesigner starts with `Popen`, and `start()` in
  `tools/td_build_comp.py` already builds the command line — a stream toggle is a
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

**2. `allowCooking` gating for `temporal` and `latches` — approved.** Small; see
`docs/BUILD_PLAN.md` 7.2, which names the two things to handle.

**3. Re-measure cook time properly.** The 0.599 ms after grouping counts only
top-level operators and excludes the groups' children. Flat it was 1.040 ms.

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
