# Handoff — 2026-08-21

**396 tests**, `ruff check` and `mypy visionhands` clean. Working tree clean apart
from `tools/vision_landmarks.py` and `tools/vision_landmarks_live.py`, which are the
user's own untracked files — leave them alone. (`ruff check` at the repo root will
fail on one of those two; lint `visionhands/` and `tools/td_*.py tools/send_*.py
tools/probe_*.py tools/record_*.py` instead.)

The optimisation and tidy pass landed. `docs/BUILD_PLAN.md` step 10 is the full
account, `design/DESIGN.md` 2.14 is the cook-time table, and the journal's last
entry is what went wrong on the way.

## Read these, in this order

1. **`docs/BUILD_PLAN.md` step 10** — what this phase did and what it left. Then
   steps 1–9 for the rest.
2. **`design/DESIGN.md` §2** — measured fact, the only section that is reliably
   current. **§2.11 and §2.14 before touching TouchDesigner**: 2.11 is every native
   behaviour that has cost time (it grew by fifteen entries this session) and 2.14
   is what the COMP costs and why.
3. **`docs/ATTRIBUTES.md`** — the channel spec and every toggle.
4. **`docs/JOURNAL.md`**, last two entries.
5. **`docs/STANDARDS.md`** — binding.

## What is running

    /project1/vision            every parameter, six pages, the sidecar control
      hands_osc 10000  ->  hands  ->  out1      531 channels
      pose_osc  10001  ->  pose   ->  out2      275
      face_osc  10002  ->  face   ->  out3     1083
      status                                   the sidecar's own sc_* channels

Inside each stream, and the ROW a node is on says whether it is in the data path:

    in1 -> filter -> coords ----+
                               |
           derive_chop --------+-> merge_out -> screen_only -> out1
           temporal -----------+
           latches ------------+

`coords` now holds up to six gateable halves: `world` and `pixels` for the wire
contract, `dv_world`/`dv_pixels` for the positions and rates that `derive_chop` and
`temporal` compute (hands only), and `lm_world`/`lm_pixels` for the face's landmark
points.

Build order, run end to end and verified 2026-08-21 with zero failures. **One
builder per call.** Note where `td_add_coords.py` sits — it moved to the end,
because it now reads `derive_chop` and `temporal` as well as `filter`.

    td_build_vision.py -> td_add_filter.py -> td_add_derive.py -> td_add_temporal.py
    -> td_add_latches.py -> td_add_coords.py -> td_add_screenspace.py
    -> td_add_groups.py

Then, both read-only and both worth running after any build:

    td_profile.py        cook time and cook COUNT, sampled across frames
    td_verify_layout.py  overlaps, undocumented networks, ribbons, orphans

## The instruments, and why to use them rather than reading a number

**`tools/td_profile.py`.** `cookTime` is a LAST-VALUE gauge: an operator that has
stopped cooking reports exactly what a busy one reports. Four sets of figures have
been thrown away over this. The profiler samples across frames from a scheduled
callback, reports the delta in `totalCooks` beside the median duration, and
**discards the run if `seq` did not move**. It discarded its own second run this
session, because the synthetic senders had expired.

**`tools/td_verify_layout.py`.** Overlapping nodes, networks with no notes DAT,
networks laid out in a ribbon by a running counter, and operators nothing reads or
feeds. It found four real bugs on its first run, including two builders that had been
placing their group on the identical coordinate for as long as both existed.

**`tools/td_verify_latches.py`.** Run it, run a sweep, run it again. Nothing with
memory can be checked from one frame. Last run: ramp **+4**, fires − releases ==
engaged on all five rows.

## How to test anything, with no camera and nobody in frame

    ~/.venvs/visionhands/bin/python tools/send_synthetic.py      --list
    ~/.venvs/visionhands/bin/python tools/send_synthetic_pose.py --list
    ~/.venvs/visionhands/bin/python tools/send_synthetic_face.py --list

For a long session, `--period 3 --cycles 4000` is about three hours; the default
runs out in well under one, and an expired sender is indistinguishable from a
working COMP unless you check `seq`.

**The face sender now moves all 348 landmark channels** — `turn` sweeps them,
`tilt` rotates them, `speak` opens the mouth and must move only the twenty lip
channels. That last one is the only sequence that moves a landmark without moving
the head, so it separates "wired to the wrong point" from "just following the
bounding box".

Every sender refuses to run while the sidecar is up: two writers on one port
interleave into a plausible wrong answer rather than a visible fault.

## The state this project's TouchDesigner file is in

**Read this before changing a parameter.** The user's own network consumes all three
streams, and two things in it depend on toggles that now gate cooking:

| their operator | reads | needs |
|---|---|---|
| `select3` `select4` `select5` | `vision` out1, `*_tx` / `*_ty` / `*trigger*` | `Streamhands`, `Coordstx`, and the latch bank (`Triggers`) |
| | (these went 42 → 50 channels when the derived positions gained companions — palm, pinch, the two centres and velocity) | |
| `null2` → nothing yet | out2 (pose) | `Streampose` |
| `null3` → `select6` `select7`, `*f0*_x` / `*f0*_y` | out3 (face) | `Streamface`, and `Screenspaceonly` OFF — on, it deletes exactly those channels |

So the streams are all left ON and `Screenspaceonly` OFF. `select3`/`select4`/
`select5` were reconnected by hand this session after `td_build_vision.py` orphaned
them (see below); their patterns are unambiguously hands, but **the user should
confirm that is where they belong.**

## The things most likely to waste your time

All measured here. 1–10 are older and still true; 11 onwards are this session's.

1. **Nothing that crosses a frame boundary can be verified inside one TD script.**
   It lied five times now — the newest being a parameter set and the resulting
   channel count read in the same script, where the callback that applies the change
   has not run yet.
2. **`op.inputs` / `op.outputs` go stale; `inputConnectors[i].connections` is the
   truth.**
3. **A branch with memory needs its own clock** (a Null at Cook Type = Always),
   inside the group it clocks. And Cook Type = Always does NOT fire in a COMP that
   nothing pulls — a scratch benchmark built that way measured zero cooks.
4. **Sample-axis operators are inert on a NON-time-sliced one-sample CHOP.** This
   also means **a scratch COMP is the wrong place to benchmark a CHOP**: its
   operators are not time-sliced, and a benchmark there said five variants were
   identical when the live network showed a 3x spread.
5. **TouchDesigner caches imported modules for the process's life.** Purge
   `visionhands*` from `sys.modules` at the top of any builder.
6. **Re-running a builder can silently unwire its consumers.** An In/Out CHOP IS a
   connector; destroying one breaks a CHOP consumer's wire and leaves a COMP
   consumer's intact. Every builder now PRESERVES its ports — including
   `td_build_vision.py`, which did not until this session (item 15).
7. **`appendMenu` resets an existing menu to index 0, and a callback amplifies it.**
   Read stored values before an append and write them back unconditionally.
8. **Run builders ONE PER CALL.**
9. **A builder that repoints its CONSUMERS depends on build order, and lost.** Each
   consumer states its own input.
10. **A channel can disappear between the OSC In CHOP and the COMP output with
    nothing saying so.** Compare a data-path group's output against its INPUT, not
    against the contract.
11. **`scope` is the Select+op+Merge pattern in one operator — except on a LOGIC
    CHOP, where it DELETES the unscoped channels.** Logic is what the latch bank is
    built from.
12. **`^` does not exclude in `scope` either.** One `^a` term means "all but a";
    two union to everything.
13. **A Select doing its own rename costs 3x a Select that only selects.** 0.0418 ms
    against 0.0138 on 42 channels. Put the rename on the operator that was already
    going to be there.
14. **Creating a Script CHOP auto-creates a docked callbacks DAT, and destroying the
    CHOP destroys the DAT.** So a `for child in list(children): child.destroy()` loop
    hits an already-deleted reference. `child.valid` is the guard, and the symptom is
    an intermittent "Invalid OP object" that looks exactly like MCP flakiness.
15. **`scriptOp.numChans` raises inside `onCook`.** `len(scriptOp.chans())` works.
    And a Script CHOP's channels PERSIST between cooks, which is what makes caching
    them worth 0.057 ms a frame.
16. **`td.run`, not `run`; `op(path).module`, not `mod(path)`.** The MCP bridge's
    exec context has neither bare name, and a scheduled string that raises NameError
    does so SILENTLY.
17. **`totalCooks`, not `cookCount`** — the latter does not exist.
18. **Gating costs about 0.11 ms per COMP boundary crossing at these channel
    counts**, paid whether the gate is open or shut. Do not wrap something in a COMP
    to make it switchable unless it is worth more than that switched off.

## Next

### 1. The review gate, SEVEN milestones unpaid

`STANDARDS.md` §3 requires a fresh review agent per milestone, scoped to correctness
and concurrency. Pose, the gating, face, the restructure, the landmark counts, the
builder fixes and now this pass have all landed without one, and between them they
touched the engine's capture queue, three lock-free boxes and the sidecar's send
path. **Offer this to the user before starting anything new.** It is the oldest
outstanding item in the project and the only one with a real risk attached.

### 2. The unit vectors, and the channel-to-group registry

The derived POSITIONS and RATES got their companions (BUILD_PLAN step 10 item 7), so
`Screenspaceonly` is complete except for eight channels: `h{i}_point_x/y` and
`h{i}_dir_x/y` are UNIT VECTORS, and a transformed unit vector is neither unit
length nor pointing where the image-space one pointed, because world space scales y
by the render's aspect. Either publish a re-normalised world-space direction — a
magnitude and a divide, so a real branch — or leave them image-space and say so in
`ATTRIBUTES.md`. **It is a design decision, not a task.** My reading is that a
direction is most useful as an ANGLE anyway, and `h{i}_dir` already exists.

The bigger prize behind it is the **channel-to-group registry**.
`spaces.derived_roles()` now classifies 24 of the derived channels; extending that
to all 171 of `derive()`'s outputs plus the latch table (which still lives in
`tools/td_add_latches.py` and should move into the package) is what
`td_add_groups.py` has been asking for in a printed note since it was written.

**But read this before starting it, because the goal that note states is not
achievable.** It says the registry would let `Landmarks`, `Triggers`, `Motion` and
`Events` gate real cost. It would not, and §2.14's boundary measurement is why:

- **`Landmarks` cannot be a cooking gate at all.** It names the raw wire channels,
  which are in the DATA PATH — everything downstream reads them, so freezing them
  freezes the whole stream.
- **`Triggers`, `Gestures` and `Events` are three slices of ONE 53-operator group**,
  and `Motion` is a slice of a 75-operator one. To gate a slice you must give it its
  own COMP, and **a COMP boundary costs about 0.11 ms at these channel counts** while
  the whole of `latches` costs 0.18. Splitting it three ways to gate thirds of it
  would cost more than never gating it at all.

So the registry is worth building for what it WOULD give: an exact answer to "which
channels belong to which group", which is what a channel-trimming Select needs — and
what `Screenspaceonly` had to be a Delete CHOP to avoid needing. It is not worth
building to make those four toggles gate cost. Measured, not assumed.

### 3. Segmentation — PARKED, decision made

File-backed `mmap` plus a Script TOP with `numpy.memmap` and a seqlock. NOT
TouchDesigner's shared-memory protocol. `docs/BUILD_PLAN.md` step 9 has the compiled
header layouts and the reason TD's protocol is closed to a foreign owner. Do not
re-research it. The one thing still unmeasured needs a camera: the per-frame cost of
`VNGeneratePersonSegmentationRequest` at each quality level.

### 4. Smaller, and each is self-contained

- **`face/coords/lm_world` is 0.83–1.08 ms** and it is the most expensive thing in
  the COMP when it is on. The cost is 348 channels through two operators at about
  1.2 µs each; there is no cleverer network, only fewer channels. If it matters,
  the honest lever is publishing 76 distinct points instead of 87 region slots.
- **A `Coordspx`-style default hazard exists for `Lmcoordspx`** and does not matter
  yet, because nothing has read those channels. It will the moment something does.
- **Phase-2 temporal channels**: `hands_twist`, `steadiness`, `dwell`,
  `hands_scale`, and the swipe/wave/cross/twist events. BUILD_PLAN step 2 lists what
  each needs.
- **Per-joint jitter is still unmeasured**, and it is what should set `Mincutoff`,
  `Beta`, `Velocityfilter` and `Speedfloor`. All four are guesses.
- **`Sidecar.run()` has no tests** — five mutations survive a green suite, including
  one where the loop sends nothing.
- **README.**

## Waiting on the user

- **A two-hand fixture**, for one question: whether real Vision's chirality flickers
  or drops out edge-on, which decides how often slot assignment's proximity fallback
  is reached.
- **Confirmation that `select3`/`select4`/`select5` belong on the hands output.** They
  were reconnected on the evidence of their own patterns after a builder orphaned
  them; that is a judgement about their project, not about this code.
- **Threshold feel** generally. `Triggeron`/`Triggeroff` at 0.13/0.17 are the only
  measured thresholds in the system, and they replaced a guess that was three times
  too loose. Treat that as the cautionary case for every remaining one.

## Deferred by the user — decisions, not oversights

README; per-joint jitter measurement; `Sidecar.run()` tests; two-hand behaviour of
the tracker; the remaining phase-2 temporal channels.

## Standing constraints

- **Ask before using the camera** — it usually has another owner, and almost nothing
  needs one. `tools/probe_face_regions.py` is the only tool that does.
- **`reference/` is frozen.** It is the provenance of every measurement in
  `DESIGN.md` §2. Extend a spike by copying into `tools/`.
- **`git add` explicit paths, never `-A`** — it once swept the user's unrelated work
  into two commits.
- Fixtures must show hands only, checked with `VNDetectHumanRectanglesRequest` and by
  looking at frames.
- Nothing may touch a TouchDesigner object from any thread but the main one.
- Promote new measurements into `DESIGN.md` §2 with their figures; a stale comment is
  a defect; one commit and one journal entry per milestone.
