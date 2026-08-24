# appletd — design plan

Apple Vision hand landmarks, on macOS, delivered into TouchDesigner as CHOP
channels without stalling TD's main thread.

This document is **self-contained on purpose**. It is written to be handed to an
agent starting fresh in a new repository, so it carries its own measurements and
its own reasoning. Nothing here depends on the repo it was drafted in.

Everything under "Proven" was measured on the target machine. Everything under
"Design" is a proposal. The two are kept separate deliberately — do not blur
them, and when you measure something new, move it up.

---

## 1. Goal and scope

**In scope.** A macOS-only Python package that runs Apple's Vision hand-pose
detector on a live camera feed on a background thread, and exposes the most
recent 21-joint result to TouchDesigner through a Script CHOP that never blocks.

**Out of scope for v1.** Video frames into TD (landmarks only — TD sources its
own image if it needs one), gesture recognition, recording, multi-camera, and
any non-Apple ML framework.

**Target.** macOS 26.5.2 (Darwin 25.5.0), Apple silicon (M4 Pro),
TouchDesigner with bundled Python 3.11.

---

## 2. Proven — measured, not assumed

**`docs/BENCHMARKS.md` is the same numbers as a table**, with the machine they were
taken on (MacBook Pro, Apple M4 Pro, macOS 26.5.2) and a note on which are fixture
replays, which are forced cooks and which are TouchDesigner's own wall-clock gauges.
This section is where each one is ARGUED; that file is where they are looked up.

A working spike exists in [`reference/`](reference/). Both scripts run today.

- [`reference/spike_vision_hands.py`](reference/spike_vision_hands.py) — one
  still image in, annotated PNG plus a joint table out.
- [`reference/spike_vision_hands_live.py`](reference/spike_vision_hands_live.py)
  — live AVFoundation capture, or deterministic replay of a recorded clip.

Built against pyobjc 12.2.2. Venv used: `~/.venvs/vision-spike`
(`pyobjc-framework-Vision`, `-Quartz`, `-AVFoundation`, `libdispatch`,
`opencv-python`, `numpy`, `pillow`).

### 2.1 Throughput

Deterministic replay of a fixed 10 s 720p clip with a real moving hand
(286 frames, hand present in 285). Medians:

| input | hand present | no hand |
|---|---|---|
| 640×480 | 3.41 ms (~293 fps) | 1.99 ms (~503 fps) |
| **1280×720** | **3.29 ms (~304 fps)** | **1.82 ms (~551 fps)** |
| 1920×1080 | 5.08 ms (~197 fps) | 3.82 ms (~262 fps) |

**At 720p Vision costs ~3.3 ms, about 10% of a 33 ms frame budget.** The camera
supplies ~28–30 fps and is the real limit; Vision has roughly 10× headroom.

Two findings worth keeping:

- **Below 720p is not faster.** 640×480 is consistently *slower* than 1280×720
  in both columns. Vision normalises internally; downscaling below 720p costs
  accuracy and buys nothing.
- **`maximumProcessingDimensionOnTheLongSide` is a no-op.** Flat ~3.1 ms from
  192 through unset. Do not spend time on that knob.

### 2.2 The GIL is not a problem — this is the load-bearing measurement

A background thread running Vision, against a main thread running a 100%-Python
spin loop (a deliberately pathological stand-in for TD's main thread):

| | alone | concurrent |
|---|---|---|
| Python spin (proxy for TD main thread) | 14.7 M it/s | **15.1 M it/s (102%)** |
| Vision inference | 4.58 ms | 8.86 ms (52%) |

**pyobjc releases the GIL during Vision calls.** The main thread is entirely
unaffected — a background Vision thread cannot stall TD. The Vision thread
absorbs the contention instead, halving under a pathological spinner. TD's main
thread is mostly C++ render time that releases the GIL, so real-world
degradation will be much smaller, and even this worst case leaves ~113 fps
against a 30 fps requirement.

This is why the Python route is viable and why a C++ plugin is not needed to
solve the threading problem. If you change nothing else, keep this measurement.

> **SUPERSEDED IN ONE DIRECTION — read §2.8 before relying on this.** The
> conclusion above is correct as far as it goes: a background Vision thread
> cannot stall TD's main thread. The *reverse* is false, and badly so. Measured
> inside TouchDesigner, TD's main thread starves the background thread by
> **28×**, not the 2× a Python spinner suggested. The spinner was a poor
> stand-in: it yields at bytecode boundaries, and TD's frame loop does not.

### 2.3 Verified Vision API surface

```python
import Quartz, Vision
from Foundation import NSURL

# still image
url = NSURL.fileURLWithPath_(path)
cg  = Quartz.CGImageSourceCreateImageAtIndex(
          Quartz.CGImageSourceCreateWithURL(url, None), 0, None)
handler  = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)

# video — one handler for the whole session, NOT one per frame
seq = Vision.VNSequenceRequestHandler.alloc().init()

req = Vision.VNDetectHumanHandPoseRequest.alloc().init()
req.setMaximumHandCount_(2)

ok, err = seq.performRequests_onCMSampleBuffer_error_([req], sbuf, None)
# also: performRequests_onCVPixelBuffer_error_ / _onCGImage_error_ / _onCIImage_error_
#       plus _orientation_ variants of each

for obs in req.results():                       # VNHumanHandPoseObservation
    obs.chirality()      # int: -1 left, 0 unknown, +1 right
    obs.confidence()
    pts, err = obs.recognizedPointsForJointsGroupName_error_(
        Vision.VNHumanHandPoseObservationJointsGroupNameAll, None)
    p = pts[Vision.VNHumanHandPoseObservationJointNameWrist]
    p.x(); p.y(); p.confidence(); p.identifier()   # or p.location() -> CGPoint
```

Only revision 1 exists (`req.revision()` == 1).

**Out-parameters.** Every `…error_` selector takes `None` as the trailing
argument and returns a tuple. This is the single most common place these
scripts fail. The ones used here:
`performRequests…error_`, `recognizedPointsForJointsGroupName_error_`,
`recognizedPointForJointName_error_`, `lockForConfiguration_`,
`deviceInputWithDevice_error_`.

**The 21 joints.** Constants are long; their *values* are short codes, and the
codes are the dict keys. Always go through the constants — two of them differ by
one character, and "Little" is internally `P` for pinky.

| constant suffix | code |
|---|---|
| `Wrist` | `VNHLKWRI` |
| `ThumbCMC` / `ThumbMP` / `ThumbIP` / `ThumbTip` | `VNHLKTCMC` / `VNHLKTMP` / **`VNHLKTIP`** / `VNHLKTTIP` |
| `IndexMCP` / `IndexPIP` / `IndexDIP` / `IndexTip` | `VNHLKIMCP` / `VNHLKIPIP` / `VNHLKIDIP` / **`VNHLKITIP`** |
| `MiddleMCP` / `PIP` / `DIP` / `Tip` | `VNHLKMMCP` / `VNHLKMPIP` / `VNHLKMDIP` / `VNHLKMTIP` |
| `RingMCP` / `PIP` / `DIP` / `Tip` | `VNHLKRMCP` / `VNHLKRPIP` / `VNHLKRDIP` / `VNHLKRTIP` |
| `LittleMCP` / `PIP` / `DIP` / `Tip` | `VNHLKPMCP` / `VNHLKPPIP` / `VNHLKPDIP` / `VNHLKPTIP` |

Groups: `All` = `VNIPOAll`, `Thumb` = `VNHLRKT`, `IndexFinger` = `VNHLRKI`,
`MiddleFinger` = `VNHLRKM`, `RingFinger` = `VNHLRKR`, `LittleFinger` =
`VNHLRKP`.

Use the `All` group — one call returns all 21. The per-finger groups return
**4 points each** (20 total); the wrist belongs to no finger group.

### 2.4 Behaviour worth knowing

- **Confidence, not presence.** Vision returns points for occluded joints with
  lower confidence rather than omitting them. A missing dict key is not how an
  occluded joint presents. Gate on confidence. On a clean unoccluded palm all
  21 came back at 0.77–0.92, with the *wrist* lowest at 0.770.
- **No tracking IDs.** Observations are independent per frame. There is no
  persistent hand identity, so slot assignment across frames is **our** problem
  (see §6.3). This is the single biggest functional gap versus MediaPipe-style
  trackers.
- **Chirality works.** Returned `right` correctly for a right hand, palm to
  camera.
- **No false positives observed** across 149 frames of a cluttered room with no
  hand in view. Not a rigorous FP measurement, but a good sign.
- **First call costs ~100 ms–4.3 s** (model load). Always warm up once before
  timing or before going live.

### 2.5 Inside TouchDesigner — milestone 1, answered

TouchDesigner 099, bundled Python **3.11.15**, on the target machine.
`tools/probe_m1_in_td.py` pasted into a Text DAT. This section replaces the
guesses that were in §4.2 and §11.

- **pyobjc loads and runs inside TD's process.** The venv's site-packages
  appended to `sys.path`; pyobjc 12.2.2 imported; `VNDetectHumanHandPoseRequest`
  instantiated (revision 1, still the only one). TD's interpreter is 3.11.15 and
  the venv's is 3.11.9 — both cp311, so the wheels load. **No C++ plugin is
  needed to make this work at all.**
- **Appending to `sys.path` rather than inserting is validated, not just
  argued.** numpy resolved to TD's own **2.1.2**, not the venv's 2.2.6. Had it
  resolved the other way, TD's extension modules would be running against a
  numpy they were not built for. Append. Never insert.
- **Vision inference in TD's process:** first call **68.47 ms** (model load,
  warm OS-side), then **2.80 / 2.86 / 2.80 ms** on a synthetic 720p frame. On
  live camera buffers, **median 2.06 ms** (min 1.92, max 2.52) over 48 frames.
  Consistent with the 3.29 ms of §2.1 and no worse for being inside TD.
- **No run loop of our own is needed. This is the load-bearing answer.** 48
  frames arrived in 2.23 s while the main thread sat in a plain `time.sleep()`,
  with nothing turning a run loop on our behalf. The same probe delivered 48
  frames standalone, in a bare Python process, under the same plain sleep — so
  the spike's belief that "AVFoundation delivers only while a run loop turns"
  was simply **wrong**. `AVCaptureVideoDataOutput` delivers on a GCD dispatch
  queue, which needs no run loop at all. The 28 → 8.4 fps collapse in §3 was
  *entirely* GIL starvation from the Python polling loop, and had nothing to do
  with the run loop's presence. §4.2 therefore loses its contingency thread.
- **Camera authorisation** was already granted to TouchDesigner.app: status 3.
  The TCC prompt attaches to the host app, as expected.
- **720p requested, 720p delivered**, via the output's `videoSettings`.

Two things that are *not* measurements. The delivered-fps figure the probe
prints (21.6 fps) is dominated by camera start-up inside a 2 s window — a
repeat run showed 18 frames rather than 48 for exactly that reason. And the
probe detects no hands, because nobody was waving; it is a liveness check, not
a detection test.

**New trap, found here.** Inside a TouchDesigner Text DAT, `__file__` **is**
defined — and it is the DAT's *operator* path (`/project1/text1`), not a
filesystem path. Deriving a directory from it silently produces `/`, so the
probe tried to write to `/docs` and got `Errno 30, read-only file system`. A
`except NameError` guard does not help, because nothing raises. Validate that
the derived path actually exists before using it.

### 2.6 This repo's own fixture — measured, and two corrections

`fixtures/hand_clip.mp4`, recorded with `tools/record_fixture.py`: 1280×720,
30 fps, **284 frames (9.5 s)**, one hand mostly, both hands sometimes.

| | |
|---|---|
| Vision, median | **3.41 ms** (p95 4.87, min 1.70, max 6.82) |
| vision-only ceiling | ~293 fps |
| hand present | 271 / 283 frames (96%) |
| dropout runs | 3, of 10, 1 and 1 frames |
| two-hand frames | 63 (max 2 hands) |
| wrist travel | x 0.930, y 0.704 of the frame |

**3.41 ms against §2.1's 3.29 ms at the same resolution.** Two independent
recordings, two toolchains, the same number. §2.1's throughput figures are
confirmed rather than merely inherited.

**The fixture is NOT in git, and that is a deliberate limitation.** It shows the
person who recorded it, from the chin down, in their home. `VNDetectFaceRectangles`
found no faces — the head is cropped — and that is exactly why face detection is
the wrong gate: `VNDetectHumanRectangles` finds a person in 29 of 29 sampled
frames. So `fixtures/*.mp4` is gitignored, and these numbers are reproducible
locally but not from a clone. Anyone rebuilding this repo must record their own
clip, hands only.

**Correction 1 — `VNHumanHandPoseObservation.confidence()` is a constant.** It
returned exactly **1.0 on all 336 observations** in the fixture. §6.2 originally
said to gate downstream work on `h<i>_score`; that instruction was useless,
because the channel never moves. `h<i>_score` still publishes the raw value, as a
faithful mirror that will show us the day Apple starts varying it, but a new
channel `h<i>_conf_median` carries the median of the hand's 21 joint
confidences, and that is the one to gate on. The channel count is therefore
**137, not 135**.

**Correction 2 — per-joint confidence is where the signal is, and it reaches
zero.** Over 7056 joint samples: min **0.000**, median **0.685**, max **0.945**,
with **40.8%** below 0.5 and **11.0%** at exactly 0.0. §2.4 said Vision returns
occluded joints with *lower* confidence rather than omitting them, which is true;
this sharpens it. A joint at exactly 0.0 is Vision saying it has no idea, while
still handing back coordinates that will look perfectly plausible on screen. The
§2.4 figure of 0.77–0.92 was one clean unoccluded palm and is not
representative of a moving hand.

### 2.7 The handoff, measured — the reader really is free

Milestone 3. `InProcessSource` driving the real camera, with the main thread
reading the single-slot box.

**Reading the latest frame costs nothing.** Over 103 reads taken while capture
and Vision ran behind them: median **0.0047 ms**, worst **0.0591 ms**. That
worst case is ~58× cheaper than a single Vision inference, which is roughly what
a lock held across inference would have cost. There is no code path from reader
to writer, and the numbers agree.

**A realistic TD cook loop does not starve the camera.** Delivered frame rate in
steady state, measured after discarding camera warm-up:

| main thread is doing | delivered |
|---|---|
| nothing (one blocking sleep) | **30.0 fps** |
| reading the box at 60 Hz, yielding between reads | **30.0 fps** (0% cost) |
| a tight Python spin with no yield at all | 18.5 fps (−38%) |

This is §2.2's GIL finding from the reader's side. A cook loop that reads and
then yields is free; only a main thread that never yields degrades delivery, and
even that pathological case still delivers more than the 30 fps we need.

**Camera warm-up is ~1.5 s**, which refines §3's "skip ~15 frames". A 2 s window
opened the instant `start()` returned measured 6.5 fps; the same window opened
1.5 s later measured 30.0 fps. Any figure taken from the moment of start is
measuring the camera waking up, and reads as a catastrophe that is not there.


### 2.8 The GIL, measured inside TouchDesigner — §2.2's conclusion reversed

Everything below was measured in TD 099 while it rendered a real project at
60 fps, with the same fixture frames throughout.

| where the work runs | `performRequests` | + observation→Hand | throughput |
|---|---|---|---|
| separate process | 4.38 ms | — | 30 fps |
| **TD's main thread** | **2.09 ms** | 5.65 ms (p95 18.3) | cook-rate bound |
| **TD background thread** | **58.90 ms** | **190.83 ms** | **5.2 fps** |

**A Python background thread inside TouchDesigner cannot do this work.** Vision
itself pays a 28× penalty there (2.09 → 58.90 ms) purely from reacquiring the
GIL after each call, and the conversion adds a further 132 ms because it makes
~126 individual pyobjc property calls per frame — three per joint, 21 joints,
two hands — and every one of them waits again.

The live engine showed the same thing more gently: inference reported 35–48 ms
against 3.4 ms standalone, delivery decaying to 13.4 fps, inter-frame gaps
swinging 15–203 ms (visibly choppy), and `age_ms` at the cook of 100 ms. With
TD's main thread deliberately blocked, the same engine immediately delivered
**30.5 fps** and inference fell to 10.8 ms. The camera was never the problem.

Things ruled out along the way, each with a measurement:

- **Not the camera's adaptive frame rate.** The device does advertise 15–30 fps
  and its `activeVideoMaxFrameDuration` does sit at 1/15 s, so auto-exposure is
  free to halve the rate in dim light. Pinning min = max = 1/30 s worked (read
  back and confirmed) and changed delivery not at all.
- **Not GPU contention.** The same fixture in a separate process while TD
  rendered: 4.38 ms median against 3.41 ms with TD closed.
- **Not the cook.** `clear()` plus 137 `appendChan` plus 137 writes measures
  **0.141 ms** in real TD — under 1% of a 60 fps frame. Writing values without
  rebuilding is 0.078 ms.
- **Not the GIL switch interval.** Lowering it from 5 ms to 0.5 ms changed
  nothing (13.7 fps), so TD holds the lock in long non-yielding stretches rather
  than releasing it at bytecode boundaries.
- **Not two camera consumers.** TD's Video Device In TOP and our engine read the
  built-in camera **simultaneously**, both getting live frames. macOS does not
  lock us out of each other, which also means TD can display the camera while we
  track it.

**Consequences for the design.** §9 says "the usual reason to go C++ — escaping
the GIL — does not apply, per §2.2". That is now wrong: escaping the GIL is
exactly the problem, and there are two cheaper answers than C++ before it.
Inference must run either on TD's main thread inside the cook (5.65 ms median,
p95 18.3 ms — comfortable at a 30 fps cook rate, tight at 60) or in a separate
process (4.38 ms, unaffected by TD). The `HandSource` seam in §5 was written for
exactly this, and `IpcSource` is named there already.

**New hard rule, learned the loud way.** Nothing may touch a TouchDesigner
object from any thread but the main one. A diagnostic of mine called `store()`
on a DAT from a worker thread and TD put up a THREAD CONFLICT dialog:
"TouchDesigner objects cannot be referenced from separate threads. Application
may behave unpredictably or terminate." The shipping code is already safe by
construction — `engine.py` imports no TD — but any diagnostic must write to a
file, not to TD storage.

### 2.9 The OSC transport, measured — what the process boundary costs

Prototyped in the live TouchDesigner before anything was designed around it. An
`oscinCHOP` on port 10000 received all **137 channels with the exact contract
names, in order**, from a hand-rolled bundle — no Script CHOP, no callbacks DAT,
no Python in TD at all. The bundle is 3480 bytes, about 104 KB/s at 30 fps,
comfortably inside loopback's 16 KB datagram limit. `sharedmeminCHOP` was
considered and rejected: lower latency in principle, but it needs
TouchDesigner's undocumented and version-sensitive shared-memory header layout
reproduced in Python, to move 3.5 KB that UDP loopback already delivers in tens
of microseconds.

**Three costs, all measured rather than discovered in production.**

1. **OSC floats are 32-bit, so raw timestamps are unusable.** `time.monotonic()`
   IS comparable across processes on macOS — verified, TD read 580737.438
   bracketed by shell readings either side, same boot epoch. But at that
   magnitude float32 resolution is 1/16 s = 62 ms, so a raw timestamp cannot
   carry a millisecond age. (A first test showed only 0.32 ms error, which was
   luck: 580737.4375 is exactly representable.) So `age_ms` is computed at send
   time, where the float32 error is 0.000002 ms.
2. **`seq` is exact in float32 only to 2²⁴ = 16,777,216** — 6.5 days at 30 fps,
   after which it silently stops incrementing. The worst kind of failure:
   everything keeps working until a long installation quietly freezes its own
   staleness detection. Sent modulo 2²³ instead, so a wrap reads as one negative
   slope rather than as a counter that stopped counting.
3. **A dead sidecar leaves the channels FROZEN, not zeroed.** The OSC In CHOP
   holds its last value indefinitely, so "the process died" looks identical to
   "nothing is moving" — including `age_ms`. Liveness has to be derived on the TD
   side, which needs no Python: the slope of `seq` is zero when the camera stops,
   and the slope of `age_ms` is zero when the process stops. The in-process
   design got this for free; this one does not.

**No pixel transport is needed.** TD's Video Device In TOP and the sidecar read
the built-in camera simultaneously (§2.8), so TD can display the camera while the
sidecar tracks it. Two independent readers, 3.5 KB of landmarks per frame, and
nothing else crosses the boundary.

### 2.10 A branch with memory needs its own clock — two reasons, measured

**This section has been wrong twice. What follows is what is measured, and a
design that does not depend on which explanation is right.** The history is kept
at the end because it is the useful part.

**Reason one: TouchDesigner does not cook a Merge input whose channels nothing
downstream asks for.** MEASURED, six operators at once: every terminal branch of
the latch bank — `lat_out_state`, `lat_out_start`, `lat_out_end`, `lat_out_count`,
`lat_out_count_end`, `lat_both_pinching` — had cooked **4** times, all four forced
by the build script, while the `merge_out` they all feed had cooked **420,613**.
This project's consumers of the COMP select `*_tx` and `*_ty`; nothing else was
pulled.

For a stateless branch that is a pure optimisation and entirely correct — an
uncooked distance is recomputed the moment anyone asks. For a recurrence it is
fatal and it fails **silently**: the state self-corrects, because the recurrence is
level-driven, but every edge pulse is computed between two frames that were never
adjacent. Observed as exactly that: release counters stuck at zero while the fire
counters climbed, because the clock pulled only the start counter and `lat_end`
had cooked 4 times against `lat_start`'s 4,845.

**Reason two, which holds even where consumption is guaranteed: the bank must
react to data STOPPING.** When the sidecar dies the OSC In CHOP freezes rather
than zeroing (§2.9), so `in1` stops changing and nothing downstream of it cooks —
including a liveness detector whose entire job is to notice that. MEASURED: with a
`seq`-slope detector and no independent clock, `lat_live` sat at **1**
indefinitely, and a latch engaged at that moment stayed engaged for ever with no
end pulse. After the fix, with the stream stopped: `h0_pinch_index` frozen at
0.435 inside the dead band and `h0_valid` frozen at 1, yet `h0_pinching` went to 0
and `h0_pinch_end_count` incremented.

**The design, which sidesteps the argument.** One Null CHOP at Cook Type =
`always`, consuming a Merge of **every terminal branch** of the bank — not one of
them, which is the mistake that left the release counters dead. Nothing then
depends on what any downstream project selects. Two things that look like they
should work and do not: `viewer = True` (measured: cooks did not advance) and an
ordinary Null downstream (itself unconsumed, so it does not cook either).

**Cost:** the clock pulls `derive_chop`, main-thread Python at 0.210 ms for 171
channels (§2.7), so it cooks on 100% of frames rather than only when a frame
arrives — about 1.3% of a 60 fps frame, paid always.

**The history, which is the lesson.** This section first claimed reason one, citing
`merge_out` 283,930 against `lat_state` 2. A review pointed out that a Merge cannot
emit output without cooking its inputs, and that the citation was invalid: those
two counters had different ages, because every build recreates `lat_state`. Both
objections were right about the citation. I then ran what I believed was a
controlled A/B — clock detached, bank wired — and read `lat_state` and `merge_out`
advancing **402 to 402**, and rewrote this section to say the pruning was not real.

That single reading has never reproduced. The six-operator measurement above
contradicts it directly and repeatedly. I do not know what pulled `lat_state` that
once; the most likely explanation is a leftover diagnostic operator from the same
session.

Three things worth keeping from that:

- `totalCooks` is a **lifetime** counter. Comparing it across operators of
  different ages measures nothing. The only valid use is a delta on one operator
  across a known interval — which is what the six-operator reading is.
- A fix that works is not evidence that its rationale is right. The clock fixed the
  symptom under both explanations, so nothing pushed back for two rewrites.
- Where a mechanism is this slippery, stop arguing and make the design not care.
  The clock now pulls every terminal branch explicitly, and
  `tools/td_verify_latches.py` compares `lat_start`'s cook count against
  `lat_end`'s on every run, so the specific failure that hid here is now checked
  rather than reasoned about.

### 2.11 Native CHOP behaviours that cost time, all verified live

Against the running instance (099), not read anywhere:

- **The Logic CHOP's `convert` parameter is a comparator.** `lt` is "On When Less
  Than Zero", `gt` is "On When Greater Than Zero", plus `ge`/`le`/`eq`/`ne`. So
  `distance < threshold` is a Math CHOP subtract and one Logic CHOP — no
  Expression CHOP and no Python, and the threshold arrives as a channel.
- **The Logic CHOP defaults to `match = index`.** It pairs channels by POSITION
  and keeps the first input's names. ANDing a channel called `aaa` with one called
  `zzz` produced one channel called `aaa`, with no error and no warning. Any
  multi-input Logic or Math CHOP whose alignment matters must set `match = name`.
- **The Feedback CHOP has no target parameter.** Input 0 is the operator whose
  previous output it emits; input 1 supplies the channel template and the initial
  values. With input 1 unconnected it produces ZERO channels, silently, and
  everything downstream then evaluates on nothing. It also does not advance on a
  second cook within the same frame (`output = previous` is time-based), which is
  what makes it safe to force-cook while debugging.
- **The Count CHOP's `offtoon` / `on` / `ontooff` / `off` parameters look like
  toggles and are menus of actions** — none / inc / dec / inctime / dectime /
  reset. Assigning `True` leaves them at `none` with no error, and the counter
  then reads zero for ever while everything around it works.
- **Connecting to a Merge CHOP input connector that reports itself FREE can
  INSERT rather than fill**, pushing the existing connection down and leaving it
  attached at both positions. Five nodes connected that way produced five
  duplicate `derive_chop` connections — 1710 published channels where 171 were
  meant, invisible downstream because `chan()` returns the first match. A builder
  must rebuild the whole input list, not append to it.
- **`appendFloat` on an EXISTING custom parameter resets its value to zero** —
  not to its default, to zero — and setting `.default` afterwards does not bring
  it back. The known rule ".default always, .val only when creating" is necessary
  and not sufficient: a builder has to save the tuned values and restore them.
- **Select CHOP `channames` order determines output order**, and its built-in
  `renamefrom = *` / `renameto = <list>` maps the list positionally onto that
  order — so one operator can pick, reorder and rename in a single step.
- **Sample-axis operators are inert on a NON-TIME-SLICED one-sample CHOP, and
  work fine on a TIME-SLICED one.** Get this right before building anything
  temporal. The first version of this entry was too broad and cost 21 operators.
    * the **Slope CHOP** differentiates along the sample axis. On a non-time-sliced
      one-sample CHOP it has no neighbour to difference against: measured, it
      reported palm velocities of 12.5 and 32.5 units/second on coordinates that
      cannot leave 0..1, and an acceleration of 1950.
    * the **Filter CHOP** smooths along the same axis. On `tmp_slope` - a Math CHOP
      fed by a Feedback, so NOT time-sliced - all nine of its filter types returned
      the identical value, unchanged by width. A silent passthrough.
    * **But on a time-sliced input the Filter CHOP works perfectly**, including its
      built-in `oneeuro` type. `oef_in` descends from the OSC In CHOP, which is
      time-sliced, so the filter has real slices to carry state across. Measured
      against a hand-built equivalent on the same 84 channels: functionally
      identical output, converging EXACTLY on a still hand where the hand-built
      version left a 9e-8 float32 residual, at **0.0153 ms against 0.6949 ms**.
  So the question is never "is this CHOP one sample wide" but **"is it
  time-sliced"**. A chain built from Math and Feedback operators is not and
  inherits nothing; anything descending from the OSC In CHOP is. Where a chain is
  not time-sliced, the tools are Trail + Analyze - which CREATES a sample axis from
  successive frames - and Feedback + Math. Differentiate with `(x - x_prev) * rate`.

  **The lesson, this being the third correction in this area.** The measurement was
  right; the generalisation drawn from it was wrong. One operator tested on one
  input became "sample-axis operators do not work here", and a 21-operator
  hand-built filter was built on the strength of it. Test the operator on the input
  you actually intend to use.
- **A frame-to-frame derivative must be AVERAGED to be correct, not merely
  smoothed.** Positions only change when a frame arrives - 30 fps into a 60 fps
  cook rate is every other frame - so the per-cook derivative alternates between a
  double-size step and zero. Measured: a palm step of 0.0592 in one cook where a
  cook's worth of motion was 0.0292, giving a peak velocity 2.5x the truth. The
  mean over a window is `(x_now - x_then) / elapsed`, which is exact regardless of
  how updates land: with a 0.15 s window a swipe of known speed 1.75 units/s
  measured **1.66, within 5%**, against 4.43 unaveraged.
- **`op.inputs` and `op.outputs` go STALE; the connectors are the truth.** This
  has cost time three separate times, so it is a rule rather than an anecdote:
    * a Merge CHOP's `.inputs` reported the same operator repeatedly right after a
      destroy, which sent a "dedupe" pass at connections that were fine;
    * `in1.outputs` did not list consumers that had been wired to a group COMP
      which was then destroyed, so a repoint pass found nothing to repoint and a
      filter was silently bypassed while its builder reported success;
    * and inside the same script that rewired it, `merge_out.inputs` reported an
      input list containing the COMP's own `out1` - which would have been a loop -
      while `inputConnectors[i].connections` showed the correct thirteen.
  Read `inputConnectors[i].connections` and `outputConnectors[i].connections`, and
  where the end state is knowable, ASSERT it by name rather than discovering what
  to change - discovery is what goes stale.
- **Destroying a group COMP orphans everything wired to its output**, and a
  dangling input is indistinguishable from one that was never connected, so a
  builder cannot tell it needs to reconnect them. Measured: the COMP output fell
  from 495 channels to 57 and the builder reported success. Reuse the group and
  destroy its CHILDREN; the external wiring then survives untouched.
- **`inputConnector.connect(op)` accepts an operator only where that operator has
  one unambiguous output.** A base COMP does not: pass
  `group.outputConnectors[0]`, or it raises "Invalid number or type of arguments".
- **`bypass` is an attribute, not a parameter**, so nothing can bind an expression
  to it - a Parameter Execute DAT has to set it. Worth the trouble where it
  replaces a Switch: bypass is a bit-exact passthrough that also stops the operator
  computing, measured 0.0110 ms down to 0.0010 ms, where a Switch pulls both its
  inputs and gates only the output.
- **`totalCooks` is a LIFETIME counter**, so comparing it between operators of
  different ages measures nothing - see 2.10 for what that cost. And forcing a
  cook does not advance the frame: every read inside one script sees one frame, so
  a "distribution" sampled in a loop there is one value repeated.
- **The MCP bridge has been observed executing a script TWICE per call.** Every
  builder must be idempotent by construction rather than by convention.
- **TouchDesigner caches imported modules for the life of the process.** A builder
  that imports from the repo gets whatever was imported hours ago, so editing a
  module does nothing until the process restarts. Purge `sys.modules` of the
  package first. Measured: adding parameters to a shared defaults table and
  re-running a builder produced a network referencing parameters the page did not
  have, because the page was built from the stale table.
- **`appendCustomPage` with an existing name creates a SECOND page of that name.**
  Look the page up first.
- **A literal space-separated Rename From/To list IS pairwise by name**, and a
  source that is absent simply keeps its own name while the rest still map
  correctly. So keying a rename on names rather than on `*` makes a missing
  channel cost one channel instead of shifting every later one onto the wrong
  data. (The recorded trap about non-pairwise lists applies to WILDCARD patterns,
  which is a different thing.)
- **`cookTime` is a LAST-VALUE gauge, not a live one.** It reports the most recent
  cook forever after, so an operator that has stopped cooking entirely still quotes
  its old cost. Measured twice in one afternoon: a whole network read 1.2-1.55 ms
  while nothing was arriving at all (the sender had died), and a `temporal` group
  frozen by `allowCooking` for hundreds of frames still reported 0.715 ms. So a
  duration cannot demonstrate that work is happening or has stopped - a cook COUNT
  delta over an interval can, and it is the same instrument §2.10 needed.
- **`^` DOES NOT EXCLUDE in a Select CHOP's `channames`, and the terms are
  ADDITIVE.** MEASURED against a 141-channel input: `*` gave 141, `* ^*_x ^*_y`
  gave **423** - each term matching the whole lot again and the results
  concatenated - and `* ^h0_wrist_x` gave 282. So a complement cannot be written
  as a pattern at all; compute it in Python and name the channels. This is what
  makes the "the two halves must partition exactly" rule in
  `tools/td_add_filter.py` a rule about lists rather than about patterns.
- **A NEWLY APPENDED parameter takes val = 0, whatever `.default` says.**
  MEASURED: `appendToggle("Streamhands")` with `.default = True` came back
  reading **False**. This is the sibling of the recorded `appendInt`/`appendFloat`
  trap, which is about an existing parameter being reset - and the rule that
  covers both is to set `.val` exactly when the PARAMETER is new. That is NOT the
  same question as whether the COMP is new, which is what the builder asked, so
  every parameter added to an existing COMP arrived switched off.
- **`op.inputs` reports a group COMP as its inner `out1` CHOP.** So dedup keyed on
  NAME collapses every group into one entry, because each group has an `out1`.
  Measured: `tools/td_add_derive.py` rebuilt `merge_out`'s input list from
  `merge.inputs`, deduped by name, and silently dropped the `coords` group - the
  COMP output went from 392 channels to 224 while the builder printed success.
  Dedup by the OWNER OBJECT (`inputConnectors[i].connections[0].owner`), which is
  the COMP itself. This is the third distinct way `.inputs` has lied.
- **A Global OP Shortcut resolves from a nested network**, VERIFIED: a COMP with
  `par.opshortcut = 'VisionProbe'` and a custom parameter was read by a Constant
  CHOP two levels down through `op.VisionProbe.par.Testval`, returning 0.375 with no
  error. That is what makes shared parameters possible without `parent(N)`, which
  has to be counted per operator and breaks the next time anything moves. The
  shortcut namespace is global to the project.
- **`scope` replaces a Select + operator + Merge chain**, and it is available on
  Math, Filter, Logic, Rename and Trail CHOPs (VERIFIED live). It restricts which
  channels the operator processes and passes the rest through untouched, so the
  three-operator pattern used throughout `tools/td_add_coords.py` and
  `tools/td_add_filter.py` collapses to one. Worth more than the operator count
  suggests: a Select whose pattern misses a channel DROPS it silently, and with
  `scope` there is no Select to get wrong.
- **A group's In/Out CHOPs ARE its connectors, and destroying the Out CHOP breaks a
  CHOP consumer's wire while leaving a COMP consumer's intact.** Reproduced
  deterministically: re-running the filter builder left `coords` (a base COMP)
  reading `filter` and silently disconnected `derive_chop` (a Script CHOP) from the
  same output connector. Every derived attribute went to 0 channels, the stream
  output fell from 499 to 366, and the builder reported success. So a rebuild must
  PRESERVE the ports and replace only the working operators - which is a better
  invariant than repointing consumers afterwards, because it makes a rebuild
  invisible downstream whatever kind of operator is reading.
- **`appendMenu` on an EXISTING parameter resets it to INDEX 0**, which is the same
  clobbering trap as `appendFloat` wearing a different costume - for a menu the
  reset lands on the first entry rather than on zero-as-false. And a Parameter
  Execute DAT AMPLIFIES it: `Verbosity` reverted to "Minimal", whose callback
  correctly disabled every derive group, which took `derive_chop` to 0 channels,
  which left the latch bank nothing to select. One menu quietly reverting cost the
  COMP output 118 channels and every latch. The rule for all of them: read the
  stored value before the append and write it back after, unconditionally - "only
  when the parameter is new" is not the same question.
- **Do not chain builders in one `exec`.** Six of them in a single call raised
  "Invalid OP object. The node this python object referenced has likely been
  deleted" partway through and left a half-built group; the same six run one per
  call all succeed. They share one Python namespace when chained, and each destroys
  operators the previous one's module-level state may still reference.
- **A builder that repoints its CONSUMERS depends on build order, and lost.**
  `td_add_filter.py` forced a named list of operators onto its output -
  `derive_chop`, `sel_tx`, `sel_ty`, `sel_px`, `sel_py`. Two of those failed at once:
  the four `sel_*` had moved inside a group and matched nothing, and `derive_chop`
  does not EXIST when the filter is built in the documented order, so it wired
  itself to the raw input afterwards and stayed there. Measured on the live network:
  `derive_chop` read `in1`, so every derived attribute - every pinch distance, every
  latch input - was computed on UNSMOOTHED landmarks while the coordinate spaces used
  smoothed ones. Two consumers of the same positions disagreeing about where the
  hand was. Each consumer states its own input now, which is the only arrangement
  that cannot depend on build order.
- **A pass-through group silently eats channels that are in neither of its
  Selects.** The filter group splits its input into positions and everything else,
  both from named lists; when the sidecar started sending four `sc_*` status
  channels they were in neither list and simply vanished - OSC In CHOP 141
  channels, COMP output unchanged at 495, no error anywhere. Any group in the DATA
  PATH needs a check that everything entering it leaves it, which is not the same
  check as "does the output match the contract" (the contract is not what arrived).
- **A CHOP's `scope` parameter is the Select+op+Merge pattern in one operator,
  and unscoped channels pass through BIT-EXACT.** Verified on Math, Filter, Lag,
  Limit, Slope, Speed, Trail, Rename, Null, Expression, Hold, Count, Delay and
  Reorder: all channels appear on the output, the scoped ones processed and the
  rest untouched to the last bit (checked by lagging a scoped channel and
  comparing an unscoped one for exact equality). It removes the whole class of
  failure above - there is no Select to get wrong and no Merge to drop half of.
- **`scope` on a LOGIC CHOP DELETES the unscoped channels instead.** Three
  channels in, `scope` naming two, two out. It was the only operator of fourteen
  tried that behaves this way, and Logic is exactly what the latch bank and the
  presence debounce are built from - so `scope` is safe for the filter and is not
  a general rule.
- **`^` does not exclude in `scope` either, and for the same reason as
  `channames`: the terms are a UNION.** A single `^a` term does mean "everything
  except a" - measured - but `^a ^b` is (all but a) OR (all but b), which is ALL.
  So `* ^*_conf ^*_quality` scaled every channel including the two it names.
  Exclusion is usable with exactly one excluded term and never with two.
- **TouchDesigner's channel matcher agrees with Python's `fnmatchcase` on `*`,
  `?` and `[0-9]` character classes.** Verified by expanding the same patterns
  both ways against the same channel set and comparing the lists. That is what
  makes a generated pattern checkable BEFORE it is written to an operator:
  `appletd/spaces.py` expands each candidate against the stream's own contract
  and falls back to the literal name list unless the match is exact. `fnmatchcase`
  and not `fnmatch` - the latter is case-insensitive on macOS.
- **`renamefrom = '*'` APPENDS rather than substitutes.** Reproduced deliberately
  this session to prove the builder's check has teeth: with `renameto = '*_tx'` the
  42 channels came out as `h0_wrist_x_tx`, `h0_thumb_cmc_x_tx`, ... The recorded
  form of this trap (a literal `renameto`, giving `h0_wrist_x`, `h0_wrist_x1`,
  `h0_wrist_x2`) is the same mistake with a different symptom. `renamefrom` must
  match the part being REPLACED - `'*_x'` -> `'*_tx'` - and a builder that generates
  a rename should check the resulting names against the list it expected.
- **A Select CHOP can do its own rename, and it costs more than a separate Rename
  CHOP.** `renamefrom`/`renameto` exist on every CHOP's Common page, and
  `renamefrom = '*_x'` with `renameto = '*_tx'` substitutes the matched part
  correctly - `f0_left_eye_00_x` becomes `f0_left_eye_00_tx`. That is NOT the
  wildcard form recorded below as a disaster, which was `renamefrom = '*'` with a
  literal target. But MEASURED in the live network on 42 channels: a Select doing
  `channames` AND a rename cost 0.0418 ms against 0.0138 for the same Select doing
  only `channames`. Moving the rename onto the following Math CHOP - which was
  already there - took the pair from 0.0515 to 0.0335.
- **A Math CHOP evaluates `(value + preoff) * gain + postoff`.** Measured: 2 in,
  preoff 10, gain 3, postoff 100, out 136. So a two-step composition folds into
  ONE operator when both terms can be written as expressions, and `gain` and
  `postoff` are evaluated once per cook rather than once per channel. That is what
  makes a face landmark's `bbox_x + point * bbox_w` then centre-and-scale a single
  Math CHOP instead of an Expression CHOP evaluating Python 608 times a frame.
- **A Math CHOP does NOT broadcast a one-channel input across a many-channel
  one.** With `chopop = multiply`, 2 channels against 1 produced FOUR channels -
  the two inputs concatenated, with the collision renamed `f0_bbox_w1`. It matches
  by NAME and there is no `matchbynames` parameter to change that. So "scale these
  87 channels by that one channel" has no two-input form; it is the expression on
  a scalar parameter above.
- **`scriptOp.numChans` RAISES inside a Script CHOP's `onCook`** - "numChans is
  unavailable for this CHOP while it is cooking". `len(scriptOp.chans())` gives the
  same number and works. And the property that makes a channel cache possible at
  all: **a Script CHOP's channels PERSIST between cooks.** Only the first cook
  after a rebuild starts with none, so `clear()` and re-`appendChan` every frame is
  optional - MEASURED at 0.0681 ms for 87 channels against 0.0070 to write the
  values into channels that already exist, plus 0.0041 for the guard that decides.
- **Creating a Script CHOP AUTO-CREATES a callbacks DAT docked to it**, named
  `<chop>_callbacks`, in the same network - and **destroying the Script CHOP
  destroys that DAT with it.** Both halves cost time. The first produced two
  operators called `tmp_motion_callbacks`, one orphaned at the stream level and one
  sitting exactly on top of another operator, because the builder was destroying
  TD's copy by guessing the name it would collide into. The second broke every
  "destroy each child" loop that had snapshotted `children` first: the DAT is
  already gone when the loop reaches it, and touching it raises "Invalid OP object.
  The node this python object referenced has likely been deleted." Guard with
  `child.valid`.
- **`allowCooking = False` on a COMP freezes its channels at their LAST VALUE, and
  a builder that cooks it once before freezing bakes in a plausible wrong number.**
  Not a new behaviour - it is what makes a frozen group safer than a vanished one
  (6.2) - but it changes what a default may be. A toggle that was advisory and
  becomes a real gate must default to NOT freezing, or an existing project's
  channels silently stop updating. `Coordspx` shipped off and gated nothing; when
  it started freezing the pixel spaces its default had to become ON.
- **A frozen half still costs its channels at the merge.** A gated sub-group's
  output stays on the group's Merge CHOP - that is the point - so the Merge's cost
  scales with the FULL channel count whether the sources are cooking or not.
  Measured on the face stream: `coords/out` merging 728 channels cost 0.1073 ms
  with 712 of them coming from frozen halves.
- **Node sizes, for anything that generates a layout:** a CHOP or DAT node is
  130 x 90 and a base COMP is 160 x 130, read from `nodeWidth`/`nodeHeight`.
- **Cook cost in this COMP is about 1.2 microseconds per channel per operator**,
  which is the number that decides whether a feature is affordable. It is why 696
  face-landmark coordinate channels through two operators each cost 1.2 ms, and why
  the answer was a toggle rather than a cleverer network.
- **A DAT that COMPILES during a profiling window reports the compile as its
  `cookTime`.** `proto_callbacks` read 0.2874 ms with ONE cook while every other
  operator cooked 89 times. Summed into a group total it made a Script CHOP that is
  2.9x faster than the CHOP chain it replaces look 74% slower. Any operator whose
  cook COUNT is a small fraction of the busiest is a one-off, not a cost.
- **`cookTime` is wall-clock, so anything else using the machine inflates every
  figure at once - self-consistently, with nothing in the numbers to say so.** A run
  taken while a browser was using 60% of the CPU put this COMP at 12.8 ms against a
  real 2.26, and the achieved cook rate at 26 frames in 89. `tools/td_profile.py`
  reports the ACHIEVED FRAME RATE for exactly this reason and warns below 45 fps. A
  performance figure without an fps beside it is not a measurement.
- **TouchDesigner PICKLES operator storage into the `.toe` on save, so anything
  `store()`d must be picklable - and a class from a module the builders purge from
  `sys.modules` is NOT.** Measured, as a save failure the user saw:
  `PicklingError: Can't pickle <class 'appletd.temporal.TemporalState'>: it's not
  the same object as appletd.temporal.TemporalState`. Pickle compares the
  instance's class against the module's current class BY IDENTITY, and every builder
  here re-imports `appletd.*` (see above), so after any rebuild the stored
  object's class is a different object with the same name. The project then saves
  "with errors" and the storage is silently dropped.
  Two consequences. Store only primitives - a tuple of strings is fine, which is why
  `derive_chop`'s channel-name cache was never affected. And **storage outlives the
  code that wrote it**: removing the `store()` call does not remove the value, so a
  builder that ever stored something unpicklable has to `unstore` it explicitly or the
  save keeps failing.
- **`absTime.stepSeconds` is how a Script CHOP learns its own `dt`.** Keeping a
  clock in operator storage and diffing it works and is the wrong instinct: `store()`
  is not a per-cook scratchpad, and `derive_chop` only gets away with storage because
  it writes when the channel SET changes, which is almost never.
- **`td.run`, not the bare `run`, and `op(path).module`, not `mod(path)`.** The MCP
  bridge's exec context has neither `run` nor `mod` in it - "name 'run' is not
  defined" - while a Text DAT's does. A builder that has to work both ways uses the
  `td.`-qualified scheduler, and a scheduled string that raises NameError does so
  SILENTLY: a 90-frame sampling schedule fired every time and reported zero samples.
- **`ps -Ao args=` is not tokenised by argv**, so splitting it on spaces cannot
  recover argument boundaries: the body of a `python -c "..."` script splits into
  words too, and a test for "`-m` immediately followed by the module" matches
  `python -c "# -m appletd.sidecar"`. This is the same class of false positive
  that once had the COMP's Stop button SIGTERM the shell that launched it, one
  level subtler. Walk the leading interpreter options the way CPython does and
  stop at the first `-c`, `-m`, or script path.

### 2.12 The body-pose API surface, verified 2026-08-21

Asked of the live framework (pyobjc 12.2.2 / macOS 26.5.2), not read from
documentation. No camera involved: every one of these answers comes from
constructing the request and asking it what it supports.

```python
r = Vision.VNDetectHumanBodyPoseRequest.alloc().init()
r.revision()                                  # 1, and supportedRevisions() = {1}
names, err = r.supportedJointNamesAndReturnError_(None)     # 19 names
groups, err = r.supportedJointsGroupNamesAndReturnError_(None)
# ('VNIPOAll', 'VNBLKFACE', 'VNBLKTORSO', 'VNBLKLARM', 'VNBLKRARM',
#  'VNBLKLLEG', 'VNBLKRLEG')
```

**The request enumerates its own joints.** `supportedJointNamesAndReturnError_`
returns all 19, so the hardcoded table in `pose_types.py` is checked against the
framework's own answer rather than against a scrape of `dir(Vision)` — a strictly
better check than the one hands gets, and it costs one call at construction.

**`VNIPOAll` is the same group constant hands uses**, so one call returns all 19
points exactly as it returns all 21 for a hand.

**TRAP, and it is a real one: the constant NAME and its VALUE disagree.** The
values are ARKit skeleton names, and four of them describe a different body part
from the constant that holds them:

| constant suffix | value |
|---|---|
| `LeftElbow` | `left_forearm_joint` |
| `LeftWrist` | `left_hand_joint` |
| `LeftKnee` | `left_leg_joint` |
| `LeftHip` | `left_upLeg_joint` |
| `Nose` | `head_joint` |

Anyone building the table from the values would map the elbow to a forearm and the
wrist to a hand. Our table keys on the CONSTANT suffix, publishes our own name, and
carries the value as the dict key Vision actually hands back — the same three-name
arrangement `types.py` uses for hands, and for the same reason.

**There is no maximum-person setting.** `VNDetectHumanHandPoseRequest` has
`setMaximumHandCount_`; the body-pose request has nothing equivalent, so Vision
finds every person in shot and the cost is whatever that is. Publishing
`MAX_BODIES = 2` therefore caps the CHANNEL LIST, not the inference — unlike
`maximumHandCount`, which caps the work. `maximumProcessingDimensionOnTheLongSide`
exists (default 0 = unlimited) and is the lever if downscaling is ever needed.

**The face request, verified at the same time, because it is next.**
`VNFaceObservation.landmarks()` returns a `VNFaceLandmarks2D` whose landmarks are
**12 named REGIONS**, not one flat table: `faceContour`, `medianLine`, `nose`,
`noseCrest`, `leftEye`, `rightEye`, `leftPupil`, `rightPupil`, `leftEyebrow`,
`rightEyebrow`, `innerLips`, `outerLips`, plus `allPoints`. Each is a
`VNFaceLandmarkRegion2D` with its own `pointCount`, `normalizedPoints` and
`precisionEstimatesPerPoint`. `VNDetectFaceLandmarksRequest` supports revisions
1-3 (default 3) and a selectable constellation -
`VNRequestFaceLandmarksConstellation65Points` or `76Points`, defaulting to 76. So
face does NOT fit the joint-table shape hands and pose share: the channel list
needs region -> point count -> names, and the per-region counts have to be read
from the framework rather than assumed.

**UNMEASURED:** the per-frame cost of the request, and whether
`VNHumanBodyPoseObservation.confidence()` is a real signal or the constant 1.0 that
hands' is (§2.6). Both need a camera or a fixture with a person in it, and a
fixture with a person in it cannot be committed (STANDARDS.md 3). `p<i>_score` is
published as a faithful mirror and must not be gated on until measured;
`p<i>_conf_median` is ours and is the one to gate on.

---

### 2.13 The UDP datagram ceiling is the SEND buffer, measured

Found by the face contract growing to 371 channels and simply not arriving.

```
net.inet.udp.maxdgram              9216 bytes
a UDP socket's default SO_SNDBUF   9216 bytes
sendto() of 12208 bytes            REFUSED - not fragmented, not truncated
```

**Raising `SO_SNDBUF` on the SENDER removes the limit entirely, with no
privileges:** 65000 bytes sent and received on loopback. And the RECEIVER needs
nothing — a socket with the default buffer accepted 20000 bytes — which is the only
reason this is fixable at all, since TouchDesigner owns the receiving end and we
cannot configure it. `osc.datagram_socket()` is what every sender uses now.

**How it presented, which is the part worth remembering.** Two streams kept working
perfectly and the third one's channels never appeared in TouchDesigner. Nothing in
the sidecar's own reporting distinguishes "the socket refused a datagram" from
"nobody is sending", so it read as a broken stream rather than a wrong socket
option. The test suite caught it before TouchDesigner did, because a bound socket in
a test refuses the same datagram.

Measured bundle sizes, for the next time a contract grows: hands 4188 bytes (141
channels), pose 3720 (123), face **12640** (387). At roughly 33 bytes per channel, a
default socket tops out near 280 channels.

---


### 2.14 The COMP's cook cost, by configuration — measured 2026-08-21

All of it with a synthetic 30 fps stream on every port, sampled over 90 render
frames from a scheduled callback, medians of the cooks where the operator's
`totalCooks` actually advanced. `tools/td_profile.py` is what took them, and it
DISCARDS a run where `seq` did not change — which is the check that would have
caught the three figures this project has had to throw away.

| configuration | ms | operators cooking |
|---|---|---|
| **before this phase**: three streams, every space, nothing gated | **1.6686** | 238 / 238 |
| like for like now: three streams, world + pixel, no landmark coords | 1.7381 | 207 / 287 |
| three streams + face landmark WORLD coords (its own toggle then) | 2.3854 | 193 / 287 |
| hands only, world only | 1.1367 | 142 / 277 |
| hands only, both spaces, attribute layer off | **0.6031** | 29 / 287 |

**The like-for-like number went UP, by 4%, and that is the honest headline.** Three
changes made things faster and one made them slower:

| change | before | after |
|---|---|---|
| the three `filter` groups, Select+Filter+Merge → one scoped Filter | 0.4380 | 0.1527 |
| `derive_chop`, caching its channel names instead of rebuilding them | 0.2815 | 0.2037 |
| the three `coords` groups, split into gateable halves | 0.2941 | 0.6260 |

So the filter and the Script CHOP gave back 0.36 ms, and making the coordinate
spaces switchable cost 0.33 ms — eight extra COMP boundaries and an extra merge
layer, paid whether anything is gated or not. **Gating is not free, and it only
pays when something is actually off.** It is worth it here because something almost
always is: `Coordspx` and the pose and face streams all ship off, and the
configuration a project actually runs is the fourth row, not the second.

The last row is the one that says the gating works: 287 operators, 29 of them
cooking, 0.60 ms. Every expensive thing in the COMP is now behind a toggle, and a
frozen group keeps its channels rather than dropping them (6.2) - with the one
exception §2.16 measured.

**What is left, and it is not the network's fault.** `hands/derive_chop` at 0.20 ms
is the single most expensive operator, and 0.06 of that is reading 141 input
channels into a dict and 0.04 is `derive()` itself. The rest is Script CHOP
framework overhead. There is no arrangement of native CHOPs that computes atan2.

### 2.15 One output, and what trimming it costs — measured 2026-08-22

The three stream outputs became ONE: `hands`, `pose` and `face` merge, a Select
keeps only the channels that are actually being computed, and that feeds the only
Out CHOP. A project wires the component once, and a beginner opening it meets 306
channels in the shipping configuration rather than 1,863.

**The trim started as a Delete CHOP and had to stop being one.** Same input (the
1,863-channel merge), same reduction, same 306 channels out, median of 30–40 forced
cooks. Read the LIST column, not the operator: a Delete carrying patterns is the
cheap one, and §2.24's 2026-08-24 measurement of the three `screen_only` operators
against literal-keep Selects confirms it - Delete won on two streams of three and by
1.8× on face:

| operator | list | ms |
|---|---|---|
| Delete CHOP | 35 drop patterns | **0.6697** |
| Delete CHOP | 6 drop patterns | 0.0772 |
| Delete CHOP | 306 literal keep names | **3.3618** |
| Select CHOP | 306 literal keep names | **0.0555** |
| Select CHOP | 258 literal keep names (shipping) | 0.0438 |
| Select CHOP | one pattern (`h0_*`) | 0.0313 |
| Delete CHOP | bypassed | 0.0003 |

The Delete's cost grows with list length × input channels; the Select's barely
moves — 306 names cost it 1.8× what one pattern does, where the same 306 names cost
the Delete 43× a six-pattern list. **So the trim is a Select**, and the shipping
path costs `merge_streams` 0.0590 + `trim_empty` 0.0438 + `housekeeping_sel` 0.0037
= **0.11 ms** for 1,863 → 258 channels.

Three consequences of a Select, all handled rather than discovered:

- **A keep list fails CLOSED.** A channel nobody names is gone, where a Delete's
  drop list let an unnamed channel through. So EVERY attribute toggle has to rewrite
  the list, not just the ones that gate cooking — `groups_callbacks` now watches all
  of `GROUPS`. Without that, turning `Descriptor` on would compute 84 channels and
  then throw them away, and the toggle would look broken. VERIFIED: `Descriptor` on
  takes the output from 306 channels to 390, `Temporal`+`Latches` on to 487.
- **A Select emits in `channames` order**, so the list is generated in MERGE order.
  Verified: the output's channel order is a subsequence of the merge's.
- **An empty list is not free**, where an empty drop list was. So the Select is
  BYPASSED whenever there is nothing to drop. That path is now mostly theoretical —
  see the exclusions below, which always match something.

**Four things never reach the output at all**, whatever the toggles say, added
2026-08-22 at the user's request for the same reason the trim exists: the 80
per-joint `*_conf` channels, and `sc_*`, `seq` and `age_ms`. The three housekeeping
families are not thrown away — a `housekeeping_sel` Select picks them off the merge
into a `housekeeping` Null beside the output, 10 channels, 0.0037 ms, one click away
for anyone who needs them. The output is **258 channels** in the shipping
configuration, and the trim got CHEAPER doing it: 0.0438 ms against 0.0669, because a
Select's cost tracks its list length.

`*_conf_median` is deliberately kept. "All conf channels" would take it, but it is
the SUMMARY rather than a per-joint confidence, and §6.2 tells people to gate on it —
removing the channel the documentation recommends would be a trap.

The list itself is read off the network — each group's `out1` IS the set of channels
that group contributes — rather than from a channel-to-group map, which does not
exist. Four toggles are therefore still advisory: `Landmarks`, `Triggers`, `Motion`
and `Events` name channels that come out of a COMP which is still cooking (`Motion`
shares `temporal` with `Presence`; the other three share `latches`), and wildcards
cannot partition them.

### 2.16 A frozen group's channels VANISH after a forced cook — 2026-08-22

§6.2 says a frozen group holds its last output rather than dropping its channels,
and `allowCooking = False` does behave that way. What it does not survive is
**`cook(force=True)` on an ancestor**: that asks every child for fresh data, a
frozen group cannot answer, and its Out CHOP reports ZERO channels. Nothing is
logged. `hands/temporal` went from 27 channels to 0, `hands/latches` from 70 to 0,
and the hands stream from 503 to 406 — and every builder that ran afterwards
reported false failures against the empty groups (`td_add_coords.py` produced 7).

An upstream SHAPE change does it too, for the same reason: flipping `Descriptor`
changes `derive_chop`'s channel count, which dirties the frozen groups downstream,
and dirty plus frozen reads as empty.

Two fixes, both in place:

- `tools/td_build_vision.py` no longer force-cooks the master for its report. A
  plain `cook()` gives the same channel counts without asking frozen children.
- `_apply_gating` now cooks each group it is about to freeze **unconditionally** —
  unfreeze, cook `out1`, freeze — where it used to do that only for a group that had
  never cooked. One cook per frozen group per parameter change, at human speed, and
  it repairs the vanish wherever it came from. `tools/td_add_groups.py` provokes it
  with a forced master cook and then verifies the repair, in that order.

**A THIRD door, found 2026-08-22 after a TouchDesigner restart: project LOAD.** A
`.toe` saved with `Temporal` off reloads with that group frozen and never cooked, so
its channels are absent — `merge_streams` came back at 302 channels instead of 1,863.
Worse, it broke the BUILDERS: `td_add_temporal.py` rebuilt the group and then
verified it while it was still frozen, so all ten of its structural checks reported
`got ()` on a network that was fine, and `td_add_coords.py` reported eight missing
channels because its velocity branch was reading the same frozen group. And the
repair was incomplete — one cook brought `temporal` back to 23 channels of 27,
because the velocity family is a Feedback CHOP chain whose template input has to have
cooked once before it can name its own channels.

So the rule now is: **a builder unfreezes what it needs to verify, and
`td_add_groups.py` sets the final state because it runs last.**
`td_add_temporal.py`, `td_add_latches.py` and `td_add_coords.py` each unfreeze their
own group or stream and say so. A builder cannot verify a network that is not allowed
to cook, and the alternative — `td_add_coords.py`'s old "built, not verified" — is an
admission rather than a check. With the rule in place the whole chain runs nine
builders with zero failures, and pose and face coordinate spaces are genuinely
verified instead of skipped.


### 2.17 `append*` on an existing parameter, and the deferred callback — 2026-08-22

§2.11 already records that appending a custom parameter that exists RESETS it, and
that a menu resets to index 0. What it did not record is why writing the value
straight back afterwards is not a fix, and this one cost real time to find.

**A Parameter Execute DAT's callbacks are DEFERRED to the end of the frame.** So:

1. `appendMenu("Verbosity")` resets it to index 0, which here is `Minimal`.
2. The next line writes the tuned value back. Both changes are now queued.
3. The builder finishes and reports the correct value. Everything looks right.
4. Two builders run in the same frame — which is exactly what a rebuild does — and
   the queue resolves with `Minimal` winning.

`Minimal` is a preset of `Landmarks` and `Coordstx` only, so it turned **every derive
group off**. `derive_chop` published 0 channels; `temporal` fell from 27 to 12,
`latches` from 72 to 45, `coords/dv_world` to 0, and the COMP output from 505
channels to 366. No error, no warning, and each builder individually correct — a
`master`-only rebuild was clean, a `groups`-only rebuild was clean, and running the
two together broke it.

**The fix is to never re-append.** `_page` now appends only what is absent and
touches nothing that exists except its `default`, which is not a value. That also
means a rebuild no longer overwrites a label somebody edited. `tools/td_build_vision.py`
still uses capture-and-restore for its own twenty parameters; the values survive
because nothing watching them applies a preset, but the same rule should reach it.

Two general lessons, both cheap to state and expensive to learn:

- **A check that runs before the thing that breaks it proves nothing.** The builder
  verified the parameters right after writing them, one step before the queue ran.
- **"Correct alone" is not "correct composed"** when callbacks are deferred. Any
  builder that writes a parameter another builder's callback reads is order- and
  frame-dependent.

### 2.18 Person segmentation, measured — and the mask is the wrong shape

Measured 2026-08-22 over `fixtures/hand_clip.mp4`, 120 frames per level, frame 0
excluded because it carries Vision's model load. **No camera** — the question §9's
research left open as "needs a camera" did not need one.

| quality | mask size | inference median | p95 | max | payload |
|---|---|---|---|---|---|
| `fast` | 256 × 192 | **2.21 ms** | 2.78 | 13.65 | 49 KB |
| `balanced` | 512 × 384 | **8.54 ms** | 9.41 | 9.96 | 197 KB |
| `accurate` | 2016 × 1512 | **30.73 ms** | 32.29 | 34.06 | 3.0 MB |

**`fast` is affordable and `accurate` is not.** 2.21 ms sits beside the hands
stream's 3.41 ms inside a 16 ms frame; 30.73 ms is a 32 fps ceiling for segmentation
alone. And Vision's OWN default is `accurate` — the constant is 0 and a fresh request
reports it — which is the most likely explanation for the user's datum of a C++
plugin pinning the frame rate at 50 fps. `SegmentationDetector` defaults to
`balanced` instead, deliberately, and that is now a choice made on numbers.

**`fast` is not a smaller `balanced`.** It returns ONLY 0 and 255 — a hard alpha with
no soft edge. `balanced` has real intermediate values (measured: 14,923 pixels in
1..127 and 15,579 in 128..254 on one frame). So the levels differ in kind, not just
in resolution, and a compositor that wants a feathered edge cannot have the cheap one.

**THE MASK DOES NOT SHARE ITS SOURCE'S ASPECT RATIO**, and this is the finding that
would have shipped broken:

| input | mask | input aspect | mask aspect |
|---|---|---|---|
| 1280 × 720 | 256 × 192 | 1.778 | 1.333 |
| 720 × 720 | 192 × 256 | 1.000 | 0.750 |
| 640 × 480 | 256 × 192 | 1.333 | 1.333 |
| 480 × 640 | 192 × 256 | 0.750 | 0.750 |

The shape is chosen by ORIENTATION alone — landscape gets 4:3, portrait gets 3:4 —
and the image is anisotropically stretched to fit it. There is no letterboxing: the
subject spans the full height, verified by row occupancy. So undoing the stretch
needs two different scale factors, and nothing in the pixels says what they are.

A consumer scaling by one factor would be **exactly right on a 4:3 camera and quietly
wrong on every other one**, which is the failure mode this project keeps meeting: not
a crash, a plausible picture. So the transport's header carries the SOURCE geometry
alongside the mask's, and `MaskFrame.source_scale` returns the pair. Zero means "not
stated" and yields `(1.0, 1.0)` — as wrong as having no field, and not misleading.

**Two API facts worth having.** `init` and `new` are both **NS_UNAVAILABLE** on
`VNGeneratePersonSegmentationRequest`, unlike every other Vision request here; the
designated initialiser is `initWithCompletionHandler_`, and nil is right because a
`VNSequenceRequestHandler` puts the results on the request synchronously. And
`outputPixelFormat` is readable — it reports `0x4c303038`, `L008` — so the
one-byte-per-pixel claim the transport depends on is checked at construction rather
than commented. `supportedRevisions()` is `[1]`.

### 2.19 The transport, measured — 0.02 ms a read and no tearing in 56,591 frames

`appletd/maskbuf.py`: a file-backed `mmap` with a seqlock, chosen over
TouchDesigner's shared memory for the reasons in `docs/BUILD_PLAN.md` step 9.

| what | figure |
|---|---|
| reader cost, 256 × 144, writer at 30 fps | median **0.0204 ms**, p95 0.0278, max 0.0487 |
| reader cost, writer at 60 fps | median 0.0158 ms, p95 0.0321, max 0.0801 |
| retries, 108 reads against a 30 fps writer | 4 |
| retries, 108 reads against a 60 fps writer | 0 |
| torn frames, two processes, 56,591 accepted | **0** |

The tearing figure comes from a writer in a SEPARATE PROCESS running flat out —
1.28 M frames in 1.5 s — while the reader accepted 56,591 of them, each filled with
a single byte value derived from its own frame number so that a torn frame is one
holding two values. A single-process test shares an address space and proves nothing
about visibility, which is why that shape is the one in the suite.

What it does not prove: that the fence is sufficient under the ARM64 memory model.
The fence is an uncontended `threading.Lock` acquire/release, which is a real
acquire/release barrier and not a C11 `atomic_thread_fence`. Absence of tearing over
56,591 frames is evidence, not proof, and the residual risk is one bad mask frame on
a stream that replaces it in 16 ms.

**One real bug, found by a measurement harness rather than by reasoning.**
`ftruncate` gives a full-size file of zeros, so between creating the buffer and
stamping its header the writer leaves a window where the magic is 0 — and a reader
opening in it got an exception. A Script TOP starting at the same moment as the
sidecar lands in exactly that window. Fixed on both sides: the writer stamps the
magic on the descriptor before the mapping exists, and an all-zero header now reads
as "nothing published yet" rather than as a wiring mistake. A NON-zero wrong magic
still raises, because that one really is a mistake.

### 2.20 The mask into TouchDesigner — the Script TOP, measured

`tools/td_add_segmentation.py` builds `seg_mask` (Script TOP) → `seg_fit` (Fit TOP) →
`outmask` (Out TOP) inside `/project1/appletd`. VERIFIED live against a writer in
another process publishing real Vision masks.

| operator | what it does | cost |
|---|---|---|
| `seg_mask` | stat, coherent read, y-flip, `copyNumpyArray` | **0.0915 ms** median |
| `seg_fit` | 512 × 384 → 1280 × 720, non-uniform, on the GPU | **0.0312 ms** |

About 0.12 ms for the whole mask path measured under a forced loop; **~0.3 ms in
situ**, once it was cooking every frame - see §2.21, which is also where the reason it
was not cooking at all lives. A numpy resize on the main thread instead of the Fit TOP would have been a
1280 × 720 resample per frame, which is why the stretch is undone on the GPU.

**`copyNumpyArray`, verified rather than assumed:**

- it needs **three dimensions**, `(h, w, components)`. A plain `(h, w)` raises
  *"NumPy array must be 3 dimensions"* — so a mono mask still needs its trailing 1.
- `uint8` is accepted **natively**. No float32 conversion, which for a mask would
  have been a 4× widening for nothing. `float16` is refused outright: *"Arrays with
  float data must be float32."*
- it **sets the TOP's resolution** from the array, overriding `outputresolution`. So
  the Script TOP is whatever size the mask is, and the Fit TOP is where a fixed
  output resolution belongs.
- `format = mono8fixed` keeps it one byte per pixel on the GPU. The default
  `rgba8fixed` would carry four and copy three of them for nothing.

**`allowCooking = False` raises on anything that is not a COMP:** *"This flag can
only be disabled for COMPs."* So the gating pattern every CHOP group in this project
uses is simply unavailable to a bare operator, and the `Segment` toggle is checked
inside `onCook` instead. Wrapping three operators in a base COMP purely to get the
flag would buy a COMP boundary and a second connector to explain, for an operator
that only cooks when something is looking at it.

**The orientation is verified with a known pattern, not with a mask.** A mask looks
plausible either way up, which is exactly why it cannot be the test. Publishing a
frame with a 255 band across the payload's FIRST 24 rows and a 180 block down its
first 24 columns, TouchDesigner shows the band at the top and the block at the left —
so the y-flip is right and there is no accidental x-mirror. `maskbuf.py` deliberately
does not flip; the Script TOP does, because a transport that silently reorients its
payload cannot be tested against a known pattern at all.

`fit = fill` is the non-uniform stretch (the menu is
`fill / fithorz / fitvert / fitbest / fitoutside / nativeres`). `fitbest` would
letterbox, which would be right if Vision had letterboxed — it does not, and §2.18
has the row-occupancy check that says so.

**One hardening the reader needed before going anywhere near TD.** A writer restarting
at a different quality level re-creates its buffer at a different size, and touching
a page past the end of a file that SHRANK is a **SIGBUS** — inside TouchDesigner that
is the user's whole project, with no traceback. So `MaskReader.read` stats the file
first, every read, and remaps when `(st_ino, st_size)` has changed. The inode matters
as much as the size: a writer that recreates its buffer at the same geometry gets a
new inode, and a reader still on the old one would report "nothing published yet" for
ever, which looks exactly like a sidecar that failed to start. A file that has GONE is
left mapped on purpose — POSIX keeps it valid, and that is what lets a sidecar restart
without faulting the reader. VERIFIED live: the writer killed and its buffer unlinked,
the TOP kept showing its last frame with no error.

### 2.21 An input-less Script TOP cooks ONCE — measured 2026-08-22

The mask was black in TouchDesigner while the shared buffer held a perfectly good
one: frame 666, 16,599 of 49,152 pixels lit, source geometry stated. Reading the
buffer by hand from the callback's own module worked first time. The Script TOP was
simply **not cooking**: `totalCooks` sat at **151 across many frames**, and the one
cook it had done happened before the sidecar had created the file, so it published
the blank and served that texture for ever.

**A Script TOP with no input is never dirtied.** Nothing about the file changing is
visible to TouchDesigner, so there is no dependency to invalidate it — and there is
no Cook Type parameter on a Script TOP to override that with. Its whole parameter
list is `aspect1 aspect2 callbacks chanmask fillmode filtertype format
inputfiltertype modoutsidecook npasses outputaspect outputresolution parmcolorspace
parmreferencewhite resmult resolutionh resolutionw setuppars` — there is nothing
there to set.

**This project had already recorded the same trap for a CHOP** — `docs/BUILD_PLAN.md`
says an input-less Script CHOP "cooks once and freezes" when `CookLevel` is missing —
and I did not connect it to the TOP. That is the second time in two days a finding
already in these documents cost time again because it was filed under the operator
family it was first seen in rather than under the behaviour.

**The fix is a custom parameter whose value changes every frame**, which is what
dirties an operator:

    seg_mask.par.Tick.expr = "absTime.frame if op.Appletd.par.Streamsegment else 0"

VERIFIED both ways. With `Streamsegment` on, `totalCooks` climbs — 151 to 640 in a
few seconds, `null5` from 3 to 103, and the reader appears in the callback's cache.
With it off the expression goes constant, the operator stops being dirtied, and
`totalCooks` settles. So the toggle genuinely stops the work, which is the gating
`allowCooking` refused to give a bare operator (§2.20).

**Cost, now that it cooks naturally rather than under a forced loop:**

| operator | forced, tight loop | natural, per frame |
|---|---|---|
| `seg_mask` | 0.0915 ms | **0.2802 ms** |
| `seg_fit` | 0.0312 ms | 0.0371 ms |
| `outmask` | — | 0.0042 ms |

Both are given because they measure different things and neither is the honest number
on its own: the tight loop isolates the work, and the natural figure is a last-value
wall-clock gauge that includes whatever else the frame was doing (§2.14 records that
`cookTime` is wall-clock). Call the mask path **~0.3 ms in situ**.

**And a diagnostic flaw of my own, worth the note.** The blank frame was 256x192 —
which is exactly the size of a `fast` mask. So "the buffer has never been read" and
"a real mask arrived" looked identical in the operator's resolution, and telling them
apart meant running the callback's logic by hand. The blank is 16x16 now: a shape
nothing else produces, so the question is answered at a glance.

### 2.22 Depth Anything V2 through Core ML, measured — 2026-08-22

Apple's Core ML conversion of Depth Anything V2 Small, run as a `VNCoreMLRequest` on
the same sample buffer as the other requests. Measured over
`fixtures/hand_clip.mp4`, no camera.

| | |
|---|---|
| model input / output | **518 × 392** in, 518 × 392 out |
| inference, median | **23.00 ms** (p95 23.27, max 23.33) |
| that against a 30 fps frame interval | **69%** |
| hands inference, for comparison | 3.41 ms |
| payload | 406,112 bytes — fp16, one component |
| distinct levels per frame | **~7,300** |

**It costs more than everything else this project runs, put together.** So it goes
LAST on the capture queue, behind everything a live project reads, and with it on the
camera's frame interval cannot always cover the total — AVFoundation drops buffers,
visible as `n_delivered` falling rather than as anything failing. Depth at 30 fps and
hands at 30 fps are not both available, and `docs/DEPTH.md` says so in those words
rather than leaving it to be discovered.

**The output is `OneComponent16Half` ('L00h'), and reading it as 8-bit loses precision
TWICE** — once by quantising 7,300 levels to 256, and once by applying a second
per-frame normalisation on top of the `reduce_max` already in the graph. That is why
`maskbuf` gained an fp16 dtype rather than the map being converted on the way out. The
reference implementation this was ported from records making that mistake, and the
measurement above is what settles it: 7,302 against 256 is not a rounding difference.

**BIGGER MEANS NEARER, and nothing is comparable between frames.** The graph ends in a
`reduce_max`: the output is divided by the frame's own maximum, and the maximum
inverse-depth pixel is the nearest thing in shot — so a hand approaching the lens
darkens every other pixel. Not the model changing its mind; division.

#### The pin solve, and the residual that lies

The corruption is AFFINE, so it can be solved away: given points of known distance,
fit `1/Z = alpha*d + beta` each frame and read metres anywhere. Two unknowns, two pins
minimum. Re-solved EVERY frame — a cached alpha/beta is the stale-scale problem the
correction exists to remove, wearing the costume of a fix.

Verified against a synthetic frame built to satisfy a known alpha and beta: the solve
recovers both to 1e-6, and `metres()` reads each pin's true distance back to a
centimetre.

**WITH TWO PINS THE RESIDUAL IS IDENTICALLY 0.000, AND IT CHECKS NOTHING.** Two points
always fit a line. That reads as a perfect fit on a panel, which makes it the most
convincing wrong number in this system — and it appeared on the very first real run,
where the fixture's own geometry drops one of three pins every frame. So `Solve` carries
`residual_is_a_check`, false below three pins, and both the note and the published
readout say which it is. Three pins is the smallest number where a fit can disagree
with itself, and it is also the number that survives one pin being occluded.

Occlusion handling, ported deliberately: with three or more, the worst-fitting pin is
dropped when it disagrees by more than 0.5 m. VERIFIED that only ONE is ever dropped —
dropping until the residual looks good would fit a line through whichever two pins
happened to agree, which is a confident answer from no evidence.

#### A TouchDesigner Sequence could not be built from Python

`Depthpins` started as one text field and became a list, which is what a panel wants.
The intended mechanism was TD's own **parameter Sequence**, and it does not work from
Python in this build: `Page.appendSequence` creates the header parameter and the
`Sequence` object, but nothing appended afterwards joins its block. `blockPars` stays
empty and `numBlocks = 1` raises *"Could not set numBlocks to specified value. Please
check for potential name conflicts."*

Four orderings were tried, all failing identically: block parameters named `Pin0m`
(0-based, matching the `point0weight` convention the docs describe), `Pin1m` (1-based),
appended before the header rather than after, and `insertBlock(0)` instead of setting
`numBlocks`. So a sequence is readable and mutable on operators that already have one -
`td_add_latches.py` sets `seq.const.numBlocks` on a Constant CHOP and that works fine -
but a CUSTOM sequence appears to need the Component Editor.

What shipped instead: `Depthpincount` plus eight fixed rows, with rows past the count
disabled by a parameter callback. Same behaviour - raise the count, another row becomes
editable - through a mechanism that demonstrably works. Worth revisiting if a later
build fixes `appendSequence`.

#### Two API facts and one Fortran diagnostic

`resolve_model` raises naming `tools/fetch_models.sh` rather than letting Core ML
report a missing file: the model is 47 MB from Hugging Face and is NOT in this
repository, so a missing one is the first thing a new clone hits, and "No such file or
directory" is not an instruction.

An `.mlpackage` compiles to a `.mlmodelc` at load time with no Xcode involved. Cached
beside the package, and the compile PRINTS that it is happening — a silent multi-second
stall on first run looks exactly like a hang.

And a 0×0 frame made `numpy.median` return nan, which reached `polyfit` as an illegal
LAPACK argument printed **straight to stderr as Fortran diagnostics** — not an
exception, a line of `On entry to DLASCL, parameter number 4 had an illegal value` in
the sidecar's log. Refused before sampling now.

#### The TouchDesigner side

`depth_map` (Script TOP) → `depth_fit` → `outdepth`, and `copyNumpyArray` **refuses
float16** ("Arrays with float data must be float32"), so the Script TOP widens: 406 KB
in, 812 KB out, one `astype` per frame. Measured **0.2911 ms** for the read, flip,
widen and copy, and **0.0040 ms** for the 518 × 392 → 1280 × 720 stretch on the GPU.

The fit travels in the buffer's 32-byte AUX block, not on a parameter of its own,
because it is solved from ONE frame and is meaningless against any other. Sending it
separately would let a consumer pair a map with a neighbouring frame's scale.

**`Depthpinson` switches the PIXELS, which is the port of the example's `p`** — and it
did not, for one commit. Off, or with no usable fit, `outdepth` carries the model's own
numbers. On, it carries metres mapped onto a fixed window (`Depthwindownear` → 1.0,
`Depthwindowfar` → 0.0, defaults 0.4/3.0 from the example's own `--window`). Both keep
NEAR BRIGHT, deliberately: that is the only way toggling shows a change in stability
rather than a change in palette, which is the example's argument.

The first version published the raw map in both states and only exported alpha/beta.
The reasoning was that a colour window is the user's choice - true, and it missed that
the *observable consequence* of pinning is the image ceasing to breathe, which cannot be
seen at all without a fixed window. A control whose only effect is on numbers nobody is
reading looks broken, and the user reported it as exactly that. `Depthfitalpha` and
`Depthfitbeta` are still published for anyone wanting true metres.

**And a measurement that shows what the three readouts are for.** On the fixture with
the default pins: `Depthfitpins` was 2 in every one of 40 frames (pin 1 dropped 34
times), `Depthfitchecked` was 0 throughout, and `alpha` swung 0.526 to 1.763 - a 3.3x
range - with `beta` going negative. So the windowed map was no more stable than the raw
one: 0.617 of swing on a stationary patch against 0.653. That is not the correction
failing; it is the correction faithfully tracking a fit solved from two points that are
partly a moving torso. The arithmetic is exact, the inputs were wrong, and the readouts
said so before anybody looked at the picture.

### 2.23 The face's four key points — measured 2026-08-24

348 landmark channels is the right answer for a project that wants a face mesh and
the wrong one for a project that wants to know where somebody is looking. So four
points per face are published beside the regions — two pupils, a nose tip and a
mouth centre — and `Facekeypoints` on the Attributes page swaps one for the other.

**Two of the four are not points Vision publishes**, and that is the finding worth
recording. Every region except the pupils is a RING: `leftEye` gives six points
tracing the lid and none of them is the eye's centre. `leftPupil` and `rightPupil`
are the only SINGLE-POINT regions in the constellation, which is why they cost
nothing to use and why the mouth centre has to be a mean of `innerLips`.

**The nose tip is identified structurally, not by index.** §2.12's overlap
arithmetic pins exactly one point in three regions at once — nine points sit in two
regions and one sits in three, which is the eleven duplicate slots and twelve
overlapping pairs measured on 2026-08-21 — and that point is the tip, where
`medianLine` running down the face meets `noseCrest` down the bridge and `nose`
around the base. `face_types.nose_tip_point` intersects those three regions on every
frame and publishes zero if the answer is not exactly one point.

**Why not a pinned index**, which was the obvious alternative and the one the plan
had: an index is one number measured once against one face on one macOS, with
nothing able to check it afterwards. On a Vision that renumbered a region it would
publish a nostril and call it a tip — the same shape of silent failure that made the
point COUNTS worth measuring rather than assuming in the first place. The
intersection is checked against the data every frame and costs 24 points of set
arithmetic per face.

**The cost, and where it is paid.** MEASURED across 89 frames with `td_profile.py`:

```
                          Facekeypoints off      on
face/coords/lm_world             0.7751 ms    frozen
face/coords/lm_pixels            0.6523 ms    frozen
the whole face stream            1.9644 ms    0.7872 ms
channels on out1                     1,082       418
```

The four points ride in `coords/world` and `coords/pixels`, beside the bounding box,
rather than in the landmark halves — which is what lets the expensive halves freeze
while the points survive, and is why `spaces.py` gives them their own role. They cost
**0.1012 ms** when the toggle is OFF, which is the price of their being available
without a toggle to find first.

**A third gating mechanism, and it is the first REVERSE gate here.** `COOK_GATED` is
an OR and `COOK_VETOED` is an AND, and both read "more on means more cooking" — every
toggle in this component until now added capability. This one removes it, so
`COOK_SUPPRESSED` freezes a group when a toggle is ON. It is also why `Facekeypoints`
is in none of the `Verbosity` presets: `Everything` would otherwise switch off the
348 landmarks it has just promised.

**No ears.** Asked for, and refused rather than approximated: Vision has twelve
landmark regions and none of them is an ear.

**One thing still UNMEASURED**: whether `leftPupil` is the SUBJECT's left or the
IMAGE's left. `f{i}_eye_left` mirrors the region name and says so;
`tools/probe_face_regions.py --keypoints` is what answers it, and needs a face.

---

### 2.24 The filtering was the expensive part — measured 2026-08-24

The suspicion was that the component computes a great deal and throws it away late.
It does, and throwing it away cost more than most of the computing. Everything
cooking, 89 frames, 4.1350 ms total: `trim_empty` **0.5075 ms**, `hands/screen_only`
**0.4885 ms**, `derive_chop` 0.2182 ms. Two of the top three remove channels.

**`hands/screen_only` fell to 0.2170 ms** when its 100 literal names became 24
patterns — a Delete CHOP's cost is list length × input channels (§2.15), and face had
already had this fix while hands had not.

**TOUCHDESIGNER'S MATCHER REJECTS A THREE-RANGE CHARACTER CLASS**, and this belongs
with the other native behaviours in §2.11. `h?_[a-ce-oq-z]*_x` — every letter but `d`
and `p` — expands correctly under `fnmatchcase`, so `compact_pattern` accepted it and
the builder applied it, and TD selected **nothing** with it: 88 channels that should
have been deleted were not. What §2.11 records as verified is `*`, `?` and `[0-9]`,
and that is now the limit rather than a starting point. The pattern that shipped uses
only `?` and `*`.

Worth recognising as a SHAPE: the offline expansion agreed with itself all the way to
the live network, exactly as the interpreter probe did in the install work. A check
that never leaves the machine it is checking cannot fail.

**`trim_empty`'s cost scales with the KEEP list**, not just with the input:

```
  306 names of 1,863   0.0555 ms
  748 names of 1,581   0.3169 ms
1,110 names of 1,581   0.5312 ms
```

Which is why an OUTPUT-ONLY toggle is not free money in the other direction: `One
Face Only` gates no cooking whatsoever and still saves **0.39 ms**, because it makes
the keep list 362 names shorter. Anything removed anywhere upstream of the trim is
paid back at the trim — a better argument for moving trims earlier than the coordinate
saving that was expected to be the reason.

---

### 2.25 Two ways a builder can move a parameter it never wrote — 2026-08-24

Collapsing `Lmcoordstx`/`Lmcoordspx` into `Coordstx`/`Coordspx` cost two rebuilds,
and neither failure was in the change.

**Assigning `menuNames` to an EXISTING menu resets it, and the reset is deferred.**
Same mechanism as §2.17's `appendMenu`, a different call. `menu.menuNames = list(...)`
put `Verbosity` on index 0 - "Minimal" - which fired the preset callback, and that
callback runs at the END of the frame. The parameter's own value read back as
"Everything" before anything could see otherwise; the queued preset ran anyway and
switched off all nine derive groups, `Coordspx`, `Temporal` and `Latches`.
`derive_chop` published 0 channels and the only visible symptom was a builder
reporting `coords` 24 channels short.

Writing the value back does not help - §2.17 says so and this confirmed it, because
the queued callback outlives the restore. **Only assign when the list actually
differs.** It almost never does.

**Destroying a custom parameter disturbed three others on the same page.** After
`RETIRED_PARS` destroyed the two collapsed toggles, `Handbox` had switched OFF and
`Facekeypoints` and `Onefaceonly` had switched ON - three toggles no code in the
builder writes, in no preset, on the same page as the two destroyed. **The mechanism
is NOT established** and nothing here claims one; it is recorded because it happened
and because the failure is silent - a wrong toggle looks exactly like a deliberate
setting.

`td_add_groups._page` now snapshots every custom parameter value before a retirement
and writes back anything that changed, printing what it restored. A retirement is
rare and this is cheap; it turns an invisible failure into a line in the build report.

**The shape both share**: neither parameter was written by the code that moved it.
Everything else in these builders is idempotent and says what it did, and these two
were the exception because a TouchDesigner side effect is not a return value.

---

## 3. Traps — all of these cost real time in the spike

**GIL starvation from a polling main thread.** A tight
`CFRunLoopRunInMode(..., 0.05, False)` loop in Python holds the GIL and starves
the capture thread: delivered frame rate went from 28 fps to **8.4 fps**. One
long blocking call fixed it. Anything that busy-waits in Python next to a pyobjc
callback thread will do this.

Refined by §2.7: the trap is *never yielding*, not *reading often*. A main
thread reading the frame box at 60 Hz and yielding between reads costs **zero**
delivered frames. A tight spin with no yield at all costs 38%. So a Script CHOP
cooking normally is free, and this trap is about poll loops specifically.

**Resolution control does not work the documented way.** `setActiveFormat_` is
silently reverted by the session preset (default `High` = 1080p) regardless of
whether you set it before or after `addInput_`, and
`AVCaptureSessionPresetInputPriority` — the iOS escape hatch — raises
`NSInvalidArgumentException` on macOS. The reliable control is
`kCVPixelBufferWidthKey` / `kCVPixelBufferHeightKey` in the output's
`videoSettings`. Two spike runs silently produced 1080p while the log claimed
720p. **Always assert delivered dimensions against requested.**

**Live camera cannot produce comparable benchmark numbers.** Identical 720p
configs gave medians from 2.12 to 16.30 ms — an 8× spread — because cost tracks
what is in shot. Any performance claim must come from replaying a fixed clip.
Keep a recorded fixture in the repo and benchmark against it.

**`CVPixelBufferCreateWithBytes` does not copy.** The backing numpy array must
outlive the pixel buffer or it corrupts.

**Row stride.** `CVPixelBufferGetBytesPerRow` is not `width * 4` in general.
Reshape to `bytesPerRow // 4` then slice to width, or every row shears.

**Pick the capture device explicitly.** The dev machine enumerates Camo, OBS
Virtual Camera and a Continuity iPhone alongside the built-in camera. Defaulting
to index 0 is a good way to debug black frames from OBS. Match on `uniqueID`,
or on `localizedName` with an assertion.

**Camera warm-up.** The first frames off a cold camera are near-black while
exposure ramps and detect nothing. Skip ~15 frames before counting anything —
and note §2.7 measured the ramp at **~1.5 s**, so a short window opened at
`start()` reports a frame rate several times too low. A 2 s window measured
6.5 fps starting immediately and 30.0 fps starting 1.5 s in.

**Pixel format.** Leave `AVCaptureVideoDataOutput` at native `420v` unless you
need RGB on the host; Vision consumes it directly. Set
`setAlwaysDiscardsLateVideoFrames_(True)` so overload drops frames instead of
building a latency backlog.

---

## 4. Architecture

**Rewritten after §2.8.** The original version of this section described Vision
running on a background thread inside TouchDesigner's process. That is measured to
be 28x slower there than on the main thread, and it is the reason the whole
pipeline moved out of process. What survives unchanged is the reasoning about
blocking and about the handoff — it just applies one process to the left.

### 4.1 The constraint

TouchDesigner cooks on a single main thread and everything on it is blocking, so
Vision inference must not happen during a cook. The original conclusion was "the
Script CHOP must only ever read an already-computed result". The measured
conclusion is stronger: inference must not happen in TD's **process** at all,
because a background thread there is starved by TD's frame loop (§2.8).

So there is no Script CHOP in the data path, and no Python of ours running in
TouchDesigner during a cook. The TD-side surface is an OSC In CHOP, which is
built-in C++.

### 4.2 Two processes, and what crosses between them

**The sidecar** (`appletd/sidecar.py`, run as `python -m
appletd.sidecar`) owns the camera, the `AVCaptureSession`, Vision, and a UDP
socket. Two threads of interest, exactly as before: the GCD capture queue
AVFoundation delivers on, and the sidecar's own send loop. No run loop is turned
by us — measured, §2.5.

**TouchDesigner** owns its main thread and nothing else of ours. It receives 137
floats per frame into an `oscinCHOP`. Everything after that is native CHOPs, plus
one Script CHOP for the stateless derived maths, which runs on the main thread by
design and measures 0.210 ms (§2.7).

**What crosses:** 137 floats, 3480 bytes, per frame. No pixels — TD's Video Device
In TOP and the sidecar read the same camera simultaneously (§2.8), so there is no
image to transport. See §2.9 for what the OSC boundary costs and how liveness is
recovered on the TD side.

### 4.3 The handoff is lock-free — still true, one process to the left

The single-slot latest-value box is unchanged and still load-bearing. It now sits
**inside the sidecar**, between the capture queue and the send loop, rather than
between a worker thread and a TD cook:

```python
# capture queue: self._latest = frame     # atomic reference rebind in CPython
# send loop:     frame = self._latest     # never blocks, never waits
```

`LandmarkFrame` is frozen, so a reader can never observe a half-constructed
object and a bare reference swap needs no lock. Measured at 0.0047 ms median to
read (§2.7). The structural tests that pin this — frozenness asserted directly,
and `latest()` disassembled to prove it contains no CALL opcode — are in
`tests/test_source.py`.

A queue would still be wrong, for the original reason: the consumer runs at its
own rate, so a queue either grows unbounded or needs draining logic. "Most recent
result, discard what nobody saw" is correct here.

One thing the process boundary changed: the sidecar sends on a **fixed clock**
rather than only when a new frame arrives, so `age_ms` stays live and a stalled
camera is distinguishable from a stalled sidecar.

### 4.4 What has memory, and where it lives

Settled in `docs/ATTRIBUTES.md` and worth stating here because it decides where
new work goes:

- **Stateless maths** — every distance, angle, curl, bounding box, two-hand
  geometry — is a pure function of one frame, and lives in `appletd/derive.py`
  called from one Script CHOP. Pure Python, no TD, so every formula in the spec is
  a unit test against a synthetic hand.
- **Anything with memory** — latches, edge pulses, filters, debounce, dwell,
  refractory windows — lives in native CHOPs, where TouchDesigner manages the
  state, it is visible in the network, and no hidden Python state survives a
  project reload unpredictably.
- **A branch with memory must be clocked by time, not by data** (§2.10). This is
  the rule most easily forgotten and the one whose violation is least visible.

---

## 5. Module layout

Actual, as built. **`engine.py`, `pose.py` and `face.py` are the three modules that touch
Vision**; neither imports TD, `td/` imports no pyobjc, and nothing in the package
imports `cv2` or `PIL` at module scope. A test enforces all of it rather than
trusting discipline (`tests/test_boundaries.py`). The dependency runs one way -
the request modules import `engine.py`, never the reverse - so the camera, the queue
and the teardown stay in one place and each new stream is a plug-in rather than a
rewrite.

```
appletd/
  types.py        # LandmarkFrame, Hand, the joint table, the 137-channel contract
  pose_types.py   # Body, PoseFrame, the 19-joint table, the 123-channel contract
  face_types.py   # Face, FaceFrame, the 12 regions, the 23-channel contract
  streams.py      # which streams exist, which port each uses, the sc_* status
  spaces.py       # what KIND each channel is: position, extent, angle, scalar
  coords.py       # every coordinate conversion, and the single legal y-flip
  engine.py       # TD-free: AVFoundation + Vision -> LandmarkFrame, and the camera
  pose.py         # TD-free: the body-pose request, a plug-in on top of engine.py
  face.py         # TD-free: the face request, the same shape as pose.py
  source.py       # HandSource/PoseSource; LatestBox; InProcessSource; FakeSource
  osc.py          # the OSC encoder, hand-rolled, byte-checked against python-osc
  sidecar.py      # the process: camera + Vision + socket, one bundle per stream
  derive.py       # the stateless half of docs/ATTRIBUTES.md, as one pure function
  synth.py        # parameterised synthetic hands, for testing without a camera
  synth_body.py   # the same for a body, so the pose plumbing needs no person
  synth_face.py   # and for a face: a box and three angles, no landmarks yet
  sequences.py    # gesture sweeps over time, for testing anything with memory
  tuning.py       # threshold defaults; one table, shared by builders and tests
  td/
    bootstrap.py  # sys.path wiring and engine lifecycle (in-process path, legacy)
    hands_chop.py # Script CHOP callbacks for the in-process path (legacy)
  tests/          # 342 tests, none of which need a camera or TouchDesigner

tools/            # scripts pasted into a TD Text DAT, plus dev utilities
  td_build_vision.py   # the MASTER COMP: every parameter, the three streams
  td_add_filter.py     # the one-euro filter, in EVERY stream
  td_add_coords.py     # the world and pixel spaces, in EVERY stream
  td_add_derive.py     # the derived-attributes Script CHOP  (hands)
  td_add_latches.py    # the proximity latch bank            (hands)
  td_verify_latches.py # judges the latches by counter deltas over a sweep
  send_synthetic.py    # drives sequences.py into TD over OSC, no camera
  send_synthetic_pose.py # the same for a synthetic body, on the pose port
  send_synthetic_face.py # and for a face: head pose sweeps, on the face port
  probe_face_regions.py # the ONE tool that needs the camera: region point counts
  record_fixture.py    # records a hands-only fixture, gated on people detection
  probe_m1_in_td.py    # the milestone-1 probe, kept

design/DESIGN.md   docs/{STANDARDS,ATTRIBUTES,BUILD_PLAN,JOURNAL}.md
fixtures/          # gitignored media; the only honest benchmark input
reference/         # the frozen spike that produced every measurement in §2
```

**`td/bootstrap.py` and `td/hands_chop.py` are the in-process path and are no
longer how this runs.** They are correct, tested, and superseded by the sidecar
(§2.8). Kept because `hands_chop.py` is where the channel contract was first
pinned and because the in-process path remains the fallback if the OSC boundary
ever becomes the problem. Anything new goes in the sidecar path.

`HandSource` is still the insulation layer, and it is what made the out-of-process
move a transport change rather than a rewrite — `sidecar.py` takes any
`HandSource`, which is also how it is tested with no camera.

---

## 6. Data contracts

### 6.1 `LandmarkFrame`

```python
@dataclass(frozen=True)
class Joint:
    x: float          # normalised, origin BOTTOM-LEFT (Vision native)
    y: float
    conf: float

@dataclass(frozen=True)
class Hand:
    chirality: int            # -1 left, 0 unknown, +1 right
    confidence: float
    joints: tuple             # 21 Joint, in JOINT_NAMES order

@dataclass(frozen=True)
class LandmarkFrame:
    seq: int
    captured_at: float        # time.monotonic() at capture
    width: int
    height: int
    hands: tuple              # 0..MAX_HANDS Hand, slot-assigned
```

### 6.2 Script CHOP channel contract

**The channel list is fixed and never varies.** A CHOP whose channels appear and
disappear breaks every downstream reference the moment they vanish. An absent
hand reports zeros with all its channels still present.

Two later qualifications, both measured and both deliberate: a group frozen by
`allowCooking` keeps its channels, but NOT across a forced cook of an ancestor
(§2.16), and the single output's `trim_empty` REMOVES the channels of every group
that is not computing on purpose, because the alternative was handing a beginner
1,863 channels of which 1,557 held a plausible wrong number (§2.15).

```
n_hands, seq, age_ms
h<i>_found, h<i>_score, h<i>_conf_median, h<i>_chirality    for i in 0..MAX_HANDS-1
h<i>_lm<jj>_x, h<i>_lm<jj>_y, h<i>_lm<jj>_conf              for jj in 00..20
```

`MAX_HANDS = 2` gives 3 + 2×(4 + 63) = **137 channels**.

**Gate on `h<i>_conf_median` and per-joint `conf`. Never on `found` alone, and
never on `h<i>_score`** — that channel is a measured constant 1.0 (§2.6). It is
published only so that a future macOS starting to vary it becomes visible rather
than silent.

Publish `age_ms` and treat a rising value as engine death.

### 6.3 Slot assignment — DONE, and simpler than planned

Vision gives no tracking ID and its observation order is not stable, so `h0` swaps
to the other physical hand between frames unless we prevent it.
`appletd/slots.py` prevents it, and the algorithm collapsed to almost nothing
because Vision already tells us which hand is which.

**The assignment is a partition, not a tracking problem.**
`VNHumanHandPoseObservation.chirality` reports left or right; `engine.py` has read
it since it was written and §2.3 records it verified against a real hand. So:

    slot 0 = the RIGHT hand,   slot 1 = the LEFT hand

fixed by anatomy rather than by Vision's whim, which makes it stable across
dropouts, occlusion and reordering with no history at all. Vision reports the
ANATOMICAL hand, not which side of the frame it appeared on, so this is unaffected
by mirroring — which is a separate question about the x coordinate.

The fallbacks the original plan specified are still there and still needed, but
only for the cases chirality cannot decide:

1. **Chirality**, when both hands are confidently and distinctly handed. `all`
   rather than `any`: one confident hand and one UNKNOWN is not enough, because
   placing one and guessing the other produces a guess that looks like an
   assignment.
2. **Wrist proximity** to the previous frame's slots otherwise — two hands both
   reported RIGHT, or UNKNOWN. With two slots there are exactly two pairings, so it
   enumerates both and takes the cheaper.
3. **The N-frame grace period is NOT implemented, and should not be.** The original
   plan wanted a slot held through brief dropouts. The Presence group's debounce
   (`Activateframes` / `Deactivateframes`, docs/ATTRIBUTES.md) already does exactly
   that job, downstream and per hand — and in chirality mode the hazard does not
   arise anyway, because a left hand can never land in slot 0 no matter how many
   frames the right hand is missing. Building it here would have been a second
   mechanism for one problem.

**MEASURED end to end, with an unstable synthetic stream** — a left and a right
hand crossing over, with the hand order reversed every frame the way Vision
reorders (`sequences.py`'s `crossing`, sent with `--unstable`):

| | assignment on | assignment off |
|---|---|---|
| `h0_chirality` | +1 throughout | alternates |
| `h0_size` | **0.1200** | **0.1322** |
| `h1_size` | 0.1440 | 0.1318 |

The two hands were generated at sizes 0.1200 and 0.1440, whose mean is 0.1320. So
with assignment off, `h0` is **neither hand** — the smoothing filter averages both
into one slot, because the slot changes identity every frame. That is what "silent
slot swapping looks fine on screen" means concretely, and it is the clearest
demonstration of the defect this section was written about.

**It is a toggle** (`--slots off`, or `Slotassign` on the COMP's Sidecar page,
read at launch) because it has a real behavioural consequence: with assignment on,
a single LEFT hand puts nothing in slot 0. Every `h0_*` channel reads zero and
`h1_*` carries the hand. That is the correct reading of "h0 is always the right
hand" and it will surprise anyone who has been treating `h0` as "the hand".

**What is still unmeasured** is how real Vision behaves: whether chirality flickers
frame to frame, and what it reports during occlusion or when a hand is edge-on.
Both determine how often the proximity fallback is actually reached. That needs a
two-hand fixture, and it validates the assumptions rather than gating the code.

### 6.4 Several streams — one process, one port each, and one with no port

Hands were the only stream until 2026-08-21. Pose and face are the same shape of
problem — a Vision request over the same camera frames — and this section is the
contract that makes adding one a table plus a converter rather than a redesign.

**One process, several requests.** The sidecar opens the camera once, runs one
capture queue, and performs every enabled request on the same sample buffer. The
alternative — a process per stream — buys fault isolation and costs a camera open
per stream (§2.8 measured two processes on one camera, never three), a warm-up
each (§2.7: ~1.5 s), and three Start buttons. Frames also stop being comparable
across streams, because each process would see a different one. Fault isolation is
recovered cheaply instead: a request that fails is counted and recorded per stream,
and one failing stream does not stop the others or the loop.

**A FOURTH request, and it is not a stream.** Segmentation joined 2026-08-22, and it
breaks the "one port each" symmetry deliberately: a mask is 197 KB of pixels per frame
at `balanced`, which no UDP datagram will carry (§2.13 measured the send-buffer
ceiling), so it goes through a shared `mmap` instead (§2.19). What it DOES share is
everything else that makes a stream a stream — one launch flag, the same camera, the
same serial queue, the same `seq` and `captured_at` as the hands frame from the same
buffer, and a status channel.

So `appletd/streams.py` now has two tuples. `STREAM_NAMES` is what has a port;
`REQUEST_NAMES` is `STREAM_NAMES` plus the portless requests, and it is what
`--streams` accepts and what the status channels are built from. `port_for("segment")`
raises **by name**, saying where the mask actually goes, rather than returning a
plausible number — a wrong port is silent at the far end.

`sc_segment` is therefore the fifth status channel, and the ORDER is the port order
followed by the portless requests. Reordering it would rename live channels, so
`_self_check` asserts that `REQUEST_NAMES` starts with `STREAM_NAMES`.

It runs LAST on the queue, behind hands, pose and face, because it is the most
expensive of the four by some distance: 2.21 ms at `fast` and 30.73 at `accurate`
against hands' 3.41 (§2.18). The consequence to know is the same one pose has — with
`accurate` selected the camera's frame interval cannot cover the total and
AVFoundation starts dropping buffers, which shows up as `n_delivered` falling rather
than as anything failing.

**One UDP port per stream.** Hands 10000, pose 10001, face 10002 — `BASE_PORT` plus
a fixed offset, in `appletd/streams.py`, which both the sidecar and the TD
builders import so the two cannot drift. All three are built. One port carrying everything was the
obvious alternative and is worse here:

  * TouchDesigner's OSC In CHOP puts every channel it receives into one CHOP, and
    the `appletd` COMP passes its whole input through to `merge_out`. Sharing
    the port would put pose channels inside the hands COMP's output, so each COMP
    would need a prefix Select at its input — and a prefix Select is exactly the
    mechanism that already failed to partition channels once (step 3 of
    docs/BUILD_PLAN.md: `h?_*_x` matches two different groups).
  * A port per stream means the existing hands path is untouched: same port, same
    137 channels, nothing inserted in front of it. Pose is purely additive, which
    is the property that makes it safe to build in one sitting.
  * The datagram-size question disappears. One bundle per stream stays far inside
    the ~16 KB loopback limit noted in `osc.py`; hands plus pose plus face in one
    bundle would be around 11 KB and would need watching as the contract grows.

The cost is three OSC In CHOPs instead of one, and three port numbers to keep in
step — which is why they are computed from one constant rather than typed twice.

**Streams are launch flags.** `--streams hands,pose`, parsed once at startup. There
is no control channel into the sidecar and there is not going to be one: the
process is started by a `Popen` from TouchDesigner (§8.2), so a toggle is an
argument on that command line and takes effect on the next Start. The TD toggles
say "restart to apply" for that reason.

**The channel contract per stream is CONSTANT, whatever is enabled.** A disabled
stream still sends its bundle, all zeros, at the send rate. This is not laziness
about bytes — §6.2's rule is that channels which appear and disappear break every
downstream reference *silently*, and the OSC In CHOP creates channels as they
arrive, so omitting a stream makes its channels vanish from the CHOP entirely. The
compute saving that matters is the inference, and that is saved whether or not the
zeros go out.

**The sidecar says what it actually started**, on the base port, as channels:

```
sc_uptime_s   seconds since the send loop started. FROZEN means the process is
              gone; a disabled stream's own seq is also frozen, so this is what
              tells the two apart
sc_hands      1 when the sidecar really started that stream, 0 when it did not
sc_pose
sc_face
```

Without these, a panel showing `Streampose` on after somebody flipped it without
restarting would be lying. With them TD can say "toggle on, sidecar off — restart
needed", which is the only honest thing to show.

They ride the BASE port rather than each stream's own port, because a status
channel that travels only on an optional stream's port vanishes exactly when it is
needed.

**Pose contract.** 19 joints (§2.12), `MAX_BODIES = 2`, prefix `p{i}_`:

```
pose_n_bodies, pose_seq, pose_age_ms
p<i>_found, p<i>_score, p<i>_conf_median
p<i>_<joint>_x, p<i>_<joint>_y, p<i>_<joint>_conf     for the 19 joints
```

3 + 2×(3 + 57) = **123 channels**. The frame scalars are prefixed where hands'
equivalents (`n_hands`, `seq`, `age_ms`) are not — hands went first and those names
are referenced in a live project, so they stay. Nothing new goes unprefixed.

**No chirality for a body, and therefore no slot assignment.** §6.3's partition
works because Vision labels each hand left or right; it has no equivalent for
people. Vision's observation order is not stable, so `p0` would swap between two
people frame to frame — the same defect §6.3 exists to fix. What is built instead
is a stable spatial sort: **bodies are ordered left to right by the x of the
reference joint**, so `p0` is the leftmost person. That is stable while people do
not cross over, costs one sort, and holds no state. Real tracking through a
crossing is the same proximity fallback `slots.py` already has and is phase 2 —
with one person, which is the case this was built for, the question does not arise.

**Face contract.** **371 channels**, prefix `f{i}_`, `MAX_FACES = 2`:

```
face_n, face_seq, face_age_ms
f<i>_found, f<i>_score, f<i>_quality
f<i>_roll, f<i>_yaw, f<i>_pitch                  DEGREES, converted from radians
f<i>_bbox_x, f<i>_bbox_y, f<i>_bbox_w, f<i>_bbox_h    normalised, y = BOTTOM edge
f<i>_<region>_<nn>_x, _y                         87 slots per face, 12 regions
```

**The landmark counts had to be MEASURED, and they were not what a guess would
give.** A face does not come back as a joint table: `VNFaceObservation.landmarks()`
gives 12 named REGIONS, each with its own `pointCount`, and nothing in the framework
publishes those counts before a face has been observed (§2.12). So `face_types.py`
carried them as `None` and published no landmark channels until
`tools/probe_face_regions.py` asked a real face. MEASURED 2026-08-21:

```
sum of the 12 regions   87 slots
DISTINCT coordinates    76           <- what "the 76-point constellation" means
```

**The regions OVERLAP.** `medianLine` shares points with `noseCrest` (4), `nose`
(2), `outerLips` (2), `innerLips` (2) and `faceContour` (1), and `nose` shares one
more with `noseCrest` — twelve pair-overlaps for eleven duplicate points, so nine
points sit in two regions and one, the nose tip, sits in three. The channel list is
built from the 87 SLOTS, so those 11 points are published twice under both region
names: the price of `f0_nose_crest_00_x` over `f0_lm43_x`, and the same trade §6.2
made for hands.

The first self-check written for this REFUSED the correctly-measured table, because
it asserted that the regions partition the constellation. They do not. Worth
remembering as a shape of mistake: the guess that got caught was in the check, not
in the data.

**12640 bytes on the wire, which is over a default UDP send buffer** (§2.13). That
makes face the size-constrained stream and `MAX_FACES = 2` the practical ceiling for
one bundle. It was 12208 for 371 channels until the four key points were added on
2026-08-24 — see §2.23.

**Two units traps on the face path**, both silent, both handled in `face.py`:
`roll`/`yaw`/`pitch` arrive in RADIANS and every angle in this system is degrees
(a missed conversion is a 57x that keeps every value plausible), and all three are
NSNumber-OR-NIL, where `float(None)` raises on the capture thread.

**No chirality for a face either**, so the same spatial rule as bodies: `f0` is the
leftmost face, by bounding-box CENTRE rather than left edge — a near face has a wide
box, and sorting on the edge would put it left of a face that is actually further
left.

**Segmentation is not in this scheme.** It is a mask rather than floats, so it
breaks the "137 floats and no pixels" property in §4.2 whatever the flags say, and
it goes down the §9 C++ path. Datum from the user, worth having before starting: a
C++ segmentation plugin for TouchDesigner already exists and pins the frame rate
at 50 fps. The question is whether 10 fps is an acceptable price, not whether it
can be done.

**Where the sidecar control lives, and why it did not move.** The Start/Stop DATs
and the stream toggles are still inside the `appletd` COMP, even though the
process they control now serves every stream. Moving them up to a shared COMP was
considered and deferred: it would break the panel in a live project, the parameter
paths every callback names, and the `--slots` plumbing, in exchange for tidiness.
The trigger for doing it is a second thing needing to control the process — a
`visionpose` COMP wanting its own Start — not the current asymmetry.

---

### 6.5 The TouchDesigner side — one COMP, three streams, one output

Restructured 2026-08-21, after the third stream landed and made the shape obvious.
Collapsed to a single output 2026-08-22 (§2.15).

```
/project1/appletd                    every user parameter; Global OP Shortcut "Vision"
  hands_osc  10000  ->  hands  --+                              505 channels
  pose_osc   10001  ->  pose   --+-> merge_streams -> trim_empty  -> out1
  face_osc   10002  ->  face   --+   1,863 chans     306 kept
  status                                       the sidecar's own sc_* channels
  sidecar_control / _callbacks / _exit         Active, Print Status
```

**Why the wrap.** The filter and the coordinate spaces were built inside the hands
COMP because hands came first, and neither is remotely hand-specific: a body's
joints and a face's bounding box want the same smoothing and the same conversion
into world and pixel space. Three Filter pages, three Ortho Widths to keep in step
and three Start buttons for one process would all have been wrong. So every
parameter lives on the master, once, and every stream reads it.

**`op.Appletd.par.X`, not `parent(2).par.X`.** A group is now two levels below the
parameters instead of one, so a relative reference would have to be counted per
operator and would break the next time anything moved. A Global OP Shortcut is
depth-independent and survives a rename; VERIFIED to resolve from a nested network
before the restructure was built on it. The shortcut name is global to the project,
which is the one cost.

**Each stream is exposed on its own output connector**, because a TouchDesigner
connection joins siblings and an operator outside the master cannot wire to a
nested one. Expressions can still reach inside:
`op('/project1/appletd/hands')['h0_index_tip_tx']`.

**One filter group per stream, not one shared operator.** Each stream arrives on its
own port with its own time-slicing; merging them would make every stream cook when
any one arrived, and reintroduce exactly the cross-stream name coupling that
separate ports were chosen to avoid.

**WHICH channels get filtered and transformed is `appletd/spaces.py`**, not a
pattern in a builder. Patterns have failed at this twice (§2.11): `h?_*_x` matches
two different groups, `^` does not exclude in a Select CHOP, and anything a pattern
misses vanishes from the COMP output with no error. `spaces.py` classifies every
channel of every stream as a position, an extent, an angle, a box-relative point or
a scalar, and both builders ask it.

---

## 7. Coordinate contract

**Vision is normalised with the origin at BOTTOM-LEFT, y up.** TouchDesigner's
TOP numpy arrays are bottom-up (row 0 = bottom) and TD UVs are bottom-left.

**They already agree. Do not flip.**

The spike flips with `(1 - y)` only because PIL and cv2 are top-left-origin. So:
everything stays Vision-native normalised bottom-left through the entire engine
and across the CHOP boundary, and the flip lives in exactly one place —
`coords.py`, used only by the cv2 debug overlay. Nothing else may flip.

Getting this wrong produces landmarks that look plausible and are vertically
mirrored. Verify visually with an asymmetric pose: the wrist must be at the
wrist. An open palm with fingers up is a good discriminator, since wrist and
fingertips then sit at opposite ends of the y range.

There is **no letterbox remap** here — Vision consumes the full frame at its
native aspect. If you are porting constants from a letterboxed detector
pipeline, discard them.

**A POSITION and an EXTENT do not transform the same way**, and this is the second
coordinate rule the system needs now that a face publishes a bounding box:

```
position:  _tx = (x - 0.5) * Orthowidth              centred, so it is offset
extent:    _tw =  w        * Orthowidth              a width is a width wherever
                                                     it sits, so it is NOT
_ty and _th both carry the RENDER's aspect: * Renderh / Renderw
```

Subtracting 0.5 from a width gives a negative size, which draws as nothing — a
silent failure that looks like a missing operator rather than wrong arithmetic.
`appletd/spaces.py` is what keeps the two apart, and it is the only place that
decides.

**Face landmark points are normalised to the FACE'S BOUNDING BOX, not to the
image** (`normalizedPoints`, §2.12). They look exactly like positions and they are
in a different space, so transforming them with the image rules would put every
facial feature in one corner of the frame. They are classified `box_relative`:
smoothed, never transformed, and composing them through the box is a job for
whoever publishes them.

---

## 8. Lifecycle

**Rewritten after the sidecar.** The original hazard was that a TouchDesigner
project reload could leave an old `AVCaptureSession` fighting a new one over the
camera, inside TD's process, with a native crash as the failure mode. That hazard
is gone: nothing of ours runs in TD's process any more. What replaced it is a
process lifecycle, which is a different and more tractable problem.

### 8.1 Inside the sidecar — unchanged and still necessary

A background thread calling into native frameworks is a liveness hazard, not a
tidiness problem: a call landing on a torn-down capture session is a native crash
that `except Exception` does not catch and that takes the process down.

- `stop()` order is flag → `stopRunning()` → brief sleep → **drain the capture
  queue** → teardown. The drain is `dispatch_sync` of a no-op onto the serial
  capture queue, which is a real barrier. It replaced a `time.sleep()` that was
  not one: with a consumer slower than the grace period, `stop()` returned while a
  delegate callback was still executing, and a following `start()` put two threads
  inside one `HandDetector` — which published `found=False` for a hand that was in
  shot, silently. See the M2b review in `docs/JOURNAL.md`.
- The delegate holds the engine **weakly**. `AVCaptureVideoDataOutput` does not
  retain its delegate, so the engine must hold it; the delegate holding the engine
  back closed a cycle the garbage collector cannot break, because pyobjc's
  Objective-C subclass instances do not expose their Python attributes to it.
  Measured: `gc.collect()` collected 0 and left the engine alive, leaking the
  camera permanently.
- `start()` resets `delivered_px`, `first_frame` and `errors`, so the
  delivered-versus-requested assertion runs once per session rather than once per
  engine. `_seq` deliberately does not reset — a consumer should see a restart as a
  gap, not as time running backwards.

### 8.2 The process, and how TouchDesigner controls it

- The COMP's **Sidecar** page has Start, Stop and Status pulses and a read-only
  PID. Start launches `python -m appletd.sidecar` detached; Stop matches the
  process by argv and signals it.
- **Process matching is by full argv, not by pattern.** `pgrep -f` matches the
  grep's own command line, which is how an early version of the Stop button
  SIGTERMed the shell that ran it; macOS `ps -o comm=` truncates to 16 characters
  so the interpreter cannot be identified that way; and `ps -Ao args=` is not
  tokenised by argv, so the body of a `python -c "..."` script splits into words
  and can impersonate a `-m` invocation. The check walks the leading interpreter
  options the way CPython parses them. §2.11.
- **The sidecar watches its parent.** Given a parent PID it exits when that
  process disappears, so closing TouchDesigner does not leave an orphan holding
  the camera.
- **A dead sidecar leaves TD's channels frozen, not zeroed** (§2.9), so liveness
  is derived on the TD side from the slope of `seq`. Every latch is gated on it,
  because without that a gesture engaged when the sidecar died stays engaged for
  ever (§2.10).
- The camera TCC prompt attaches to the process that opens the camera — now the
  sidecar's own interpreter rather than TouchDesigner.app. Verify the permission
  before diagnosing anything else; `AVCaptureDevice.authorizationStatusForMediaType_`
  returns 3 when authorised.

### 8.3 A project reload

No engine, no camera and no Python of ours lives in TD's process, so a reload
cannot produce two sessions fighting. What a reload does affect is the native
CHOP state: every Feedback CHOP in the latch bank is initialised from an all-zero
Constant, so the bank comes back **released** with its counters at zero. That is
the safe direction — the phantom end-pulse-on-reload that `docs/ATTRIBUTES.md`
warns about cannot happen. The converse does: reloading while a gesture is
physically ongoing fires one spurious start pulse. That is inherent to a
level-driven latch and is accepted.

---

## 9. C++ upgrade path — deliberately not v1, and less attractive than it was

TouchDesigner supports C++ custom operators; the SDK ships in the install at
`Contents/Resources/tfs/Samples/CPlusPlus` (headers `CPlusPlus_Common.h`,
`CHOP_CPlusPlusBase.h`, `TOP_CPlusPlusBase.h`, plus Xcode projects; macOS custom
ops are `BNDL` bundles).

**Two of the three original arguments have since been settled the other way.**

- The original text said "the usual reason to go C++ — escaping the GIL — does not
  apply, per §2.2". §2.2 was **reversed by §2.8**: escaping the GIL is exactly the
  problem. But a separate process escapes it too, for none of the cost, and
  measured at 4.38 ms while TD renders at 60 fps. So the GIL argues for *getting
  out of TD's process*, which is done, not specifically for C++.
- The strongest original trigger was "TD needs the camera image as well as
  landmarks, and `TOP_Buffer` upload is where Python gets awkward". Measured
  (§2.8): TD's Video Device In TOP and our engine read the same camera
  **simultaneously**, both getting live frames. TD can display the camera itself,
  so there is no image to transport and the trigger is moot.

**A C++ plugin still does not solve the threading problem** on its own —
`execute()` is called on TD's cooking thread, so blocking there blocks TD exactly
as much as Python would. Derivative's own `CPUMemoryTOP` sample demonstrates the
required pattern: `std::thread`, `std::atomic<bool>`, `join()` on destruct, and a
queue that `execute()` drains. That is structurally identical to §4.3, which is a
good sign the design is sound.

What would still justify it: the pyobjc bridge or the venv bootstrap becoming a
deployment liability, or a need to run Vision on frames TD itself produced (a
rendered TOP rather than a camera), where the process boundary would mean pushing
pixels rather than pulling them.

Costs unchanged: an Xcode project per TD version, bundle signing, far slower
iteration than editing a Python file live, and harder debugging.

---

## 10. Milestones

Rewritten to match what exists. The original list stopped at "Script CHOP" and
predates both the sidecar and the attribute layer.

1. ~~**Go/no-go: pyobjc inside TD.**~~ **DONE.** §2.5. Probe kept at
   `tools/probe_m1_in_td.py`.
2. ~~**`engine.py` headless.**~~ **DONE**, reviewed, and hardened — see §8.1 for
   the two defects that review found.
3. ~~**`HandSource` + lock-free slot.**~~ **DONE**, reviewed. §2.7, §4.3.
4. ~~**Script CHOP.**~~ **DONE**, and then superseded: the GIL measurement (§2.8)
   moved everything out of process. `td/hands_chop.py` is kept as the in-process
   fallback (§5).
5. ~~**The sidecar.**~~ **DONE.** Camera and Vision in their own process,
   landmarks over OSC. 13.4 → 29.8 fps, jitter 20.8 → 7.7 ms (§2.8, §2.9).
6. ~~**The COMP.**~~ **DONE.** Readable channel names, three coordinate spaces
   matched to the render aspect, Start/Stop on the Sidecar page.
7. ~~**Derived attributes, stateless half.**~~ **DONE.** `derive.py`, 171 channels
   at 0.210 ms, every formula a unit test against a synthetic hand.
8. ~~**Proximity latches.**~~ **DONE**, reviewed. Pinch, snap, clap; hysteresis,
   edge pulses and counters verified by counter deltas over generated sweeps.
9. **The rest of the temporal channels** — velocity, smoothing, debounce, dwell,
   steadiness, motion events. `docs/BUILD_PLAN.md` step 2.
10. **Groups and parameter pages**, gated by `allowCooking`. Step 3.
11. ~~**Slot assignment.**~~ **DONE** — see §6.3. It did not need the fixture:
    chirality was already in the stream, and the algorithm is testable with
    synthetic hands. The fixture is still wanted, for how real Vision behaves.
12. **README**, and fold anything newly measured back into §2.

---

## 11. Open questions

- ~~Does TD's existing run loop serve AVFoundation delegate callbacks?~~
  **CLOSED (§2.5): the question was malformed.** No run loop is involved at all
  — delivery is on a GCD queue and works with none turning, inside TD or out.
- ~~Does pyobjc load cleanly inside TD's bundled Python 3.11?~~ **CLOSED
  (§2.5): yes.** TD 099 ships 3.11.15; pyobjc 12.2.2 imports and runs Vision in
  TD's process.
- ~~Best venv strategy~~ **CLOSED (§2.5): a dedicated venv at
  `~/.venvs/appletd`, its `site-packages` APPENDED to `sys.path`.** Measured
  inside TD: numpy correctly resolved to TD's own 2.1.2 rather than the venv's
  2.2.6, which is the outcome that keeps TD's extension modules matched to the
  numpy they were built against. Inserting at position 0 would invert that.
- Two-hand behaviour is **partly** answered. The attribute layer is exercised
  two-handed with synthetic hands (`sequences.py`'s `both` and `clap`), so the
  arithmetic and the channel wiring are verified. What remains unmeasured is
  two-hand behaviour of the *tracker*: throughput with two hands in shot,
  chirality stability, and slot assignment. All three need a two-hand fixture.
- Deferred by explicit decision: end-to-end latency (photons → channels), dropout
  rate under motion and occlusion, and per-joint jitter with a still hand. Jitter
  is the one that matters most — it should set the default strength of the
  smoothing filter, and it is the usual reason a demo that looks fine on stills
  feels bad live. Measurable from the existing fixture with nobody present.
- **How much does the adaptive smoothing filter actually help, and at what
  latency cost?** A one-euro filter trades jitter against lag through `beta`, and
  the honest way to set the defaults is to measure jitter first and then measure
  the residual. Currently the defaults are guessed, and labelled as such.
- **What is the cost of the always-cook clock in a real project?** It pulls the
  stateless Script CHOP every frame, measured at 0.210 ms for 171 channels, but
  that figure predates the temporal groups. Worth re-measuring once they exist.
- **Does `allowCooking` group gating interact safely with the clock?** A disabled
  group cannot run a Cook Type = Always operator inside it, so its latches would
  freeze and resume with a stale `prev`. The intended answer is that the clock
  lives outside any gated group; unverified until the groups are built.

---

## 12. Provenance

Everything in §2 and §3 was measured on the target machine during the spike that
produced [`reference/`](reference/). Section 9's claims about the TD C++ SDK come
from reading the headers and samples in the local TouchDesigner install.
Sections 4–8 and 10 are design proposals and have not been built.
