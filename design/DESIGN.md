# visionhands — design plan

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

### 2.10 A branch with memory must be clocked by TIME, not by data — measured

**This section replaces an earlier version that got the reason wrong.** The wrong
version is kept below, because the way it was wrong is the useful part.

**What is true.** A CHOP branch containing a recurrence has to cook every frame,
and the reason is not efficiency — it is that *the event it must react to is the
absence of data*. When the sidecar stops, the OSC In CHOP freezes rather than
zeroing (§2.9), so `in1` stops changing, so nothing downstream of it cooks. That
includes any detector whose job is to notice that nothing is arriving. MEASURED:
with a `seq`-slope liveness detector wired from `in1`, nothing sending, and no
independent clock, `lat_live` sat at **1** indefinitely — the absence of data was
exactly what stopped the detector from being recomputed. A latch engaged at that
moment stayed engaged for ever and never emitted its end pulse.

The fix is a Null CHOP with Cook Type = `always` consuming the deepest point of
the chain, so everything upstream cooks on the clock whether or not a frame
arrived. Two things that look like they should work and do not: `viewer = True`
(measured: cooks did not advance) and an ordinary Null CHOP downstream (itself
unconsumed, so it does not cook either).

**MEASURED, after the fix.** With `deadband` sent until the latch was engaged and
then the sender stopped: `h0_pinch_index` frozen at 0.435 — inside the dead band,
below the release threshold — and `h0_valid` frozen at 1, yet `h0_pinching` went
to 0 and `h0_pinch_end_count` incremented. The gesture ended because the *stream*
ended, which is the only correct answer.

**What it costs, stated plainly.** The clock pulls `derive_chop`, which is
main-thread Python at 0.210 ms (§2.7), so that cooks on 100% of frames rather than
only when a frame arrives — about 1.3% of a 60 fps frame, paid always. It buys a
latch bank that releases when the camera goes away.

**THE WRONG VERSION, and why it was believed.** This section first claimed that
*TouchDesigner does not cook a Merge branch whose channels nothing downstream
selects*, citing `merge_out` at 283,930 cooks against `lat_state` at 2. That claim
is **false**, and the citation was an invalid comparison: `merge_out` is an
operator that had existed for hours, while every build of the latch bank destroys
and recreates `lat_state`, so its counter had restarted. The two numbers measured
different spans and were never comparable.

Tested properly, under control — bank connected to `merge_out`, clock fully
detached — `lat_state` and `merge_out` advanced **402 cooks to 402, in lockstep**.
A Merge CHOP pulls all of its inputs, and there is no channel-level pruning.

Two lessons worth more than the conclusion. First, `totalCooks` is a lifetime
counter: comparing it across operators of different ages measures nothing, and
the only valid use is a delta on one operator across a known interval. Second,
the original diagnosis was reached from a single dramatic ratio and it *worked* —
adding the clock did fix the symptom — which is precisely how a wrong explanation
survives. It took a review challenging the arithmetic to notice, and the
controlled A/B took thirty seconds.

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
- **The MCP bridge has been observed executing a script TWICE per call.** Every
  builder must be idempotent by construction rather than by convention.
- **`appendCustomPage` with an existing name creates a SECOND page of that name.**
  Look the page up first.
- **A literal space-separated Rename From/To list IS pairwise by name**, and a
  source that is absent simply keeps its own name while the rest still map
  correctly. So keying a rename on names rather than on `*` makes a missing
  channel cost one channel instead of shifting every later one onto the wrong
  data. (The recorded trap about non-pairwise lists applies to WILDCARD patterns,
  which is a different thing.)
- **`ps -Ao args=` is not tokenised by argv**, so splitting it on spaces cannot
  recover argument boundaries: the body of a `python -c "..."` script splits into
  words too, and a test for "`-m` immediately followed by the module" matches
  `python -c "# -m visionhands.sidecar"`. This is the same class of false positive
  that once had the COMP's Stop button SIGTERM the shell that launched it, one
  level subtler. Walk the leading interpreter options the way CPython does and
  stop at the first `-c`, `-m`, or script path.

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

### 4.1 The constraint

TouchDesigner cooks on a single main thread and everything on it is blocking.
Vision inference must therefore not happen during a cook. The Script CHOP must
only ever *read an already-computed result*, and that read must never wait.

### 4.2 Threads

**Capture / Vision thread.** AVFoundation delivers `CMSampleBuffer` on a GCD
dispatch queue; Vision runs there (~3.3 ms at 720p). Converts results to a
plain-Python immutable `LandmarkFrame` — floats and tuples only, **no pyobjc
objects escape this thread**. Never touches the TD API.

**TD main thread.** Script CHOP cooks, reads the latest frame, writes a fixed
channel set. Never blocks, never waits, never allocates a lock it could contend
on.

**Run loop — settled, no thread needed.** MEASURED (§2.5): frames arrive with
no run loop turned by us, both inside TD and in a bare Python process. The
spike's `CFRunLoopRun` was never the thing making delivery work;
`AVCaptureVideoDataOutput` delivers on a GCD dispatch queue, which needs no run
loop. **This design owns exactly two threads of interest: TD's main thread, and
the capture queue AVFoundation gives us.** What the spike actually needed was
for its main thread to *block* rather than poll — see §3.

### 4.3 The handoff is lock-free

A **single-slot latest-value box**, not a queue:

```python
# worker thread:  self._latest = frame     # atomic reference rebind in CPython
# TD main thread: frame = self._latest     # never blocks, never waits
```

`LandmarkFrame` is frozen/immutable, so a reader can never observe a
half-constructed object, and a bare reference swap needs no lock at all. The
result is that **the main thread has no code path that can block on the
worker** — which is the property TD actually requires.

A queue would be wrong. TD cooks at its own rate, unrelated to the camera's.
A queue either grows unbounded (latency creep) or needs draining logic. "Most
recent result, discard what the renderer never saw" is correct for a realtime
overlay. Each frame carries a monotonic `seq` and a capture timestamp so the
CHOP can publish `age_ms` and downstream can distinguish fresh from stale — a
stalled engine must be *visible*, not silently serving old landmarks.

---

## 5. Module layout

Nothing imports anything outside this package.

```
visionhands/
  DESIGN.md            # this document
  README.md            # how to run it
  requirements.txt
  types.py             # LandmarkFrame, Hand, JOINT_NAMES, JOINT_CODES
  coords.py            # every coordinate conversion, and nothing else
  engine.py            # TD-free: AVFoundation + Vision -> LandmarkFrame
  source.py            # HandSource interface; InProcessSource (+ IpcSource later)
  td/
    bootstrap.py       # sys.path wiring, engine start/stop lifecycle
    hands_chop.py      # Script CHOP callbacks (main thread)
  tests/
    test_coords.py
    test_engine_replay.py      # against the fixture, deterministic
    test_channel_contract.py
    test_slot_assignment.py
  fixtures/
    hand_clip.mp4      # recorded take; the only honest benchmark input
  reference/           # the working spike, kept for provenance
```

`HandSource` is the insulation layer. `engine.py` never imports TD; `td/` never
imports pyobjc. That boundary is what makes both the C++ swap (§9) and any
future out-of-process move a transport change rather than a rewrite.

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

### 6.3 Slot assignment — new work, not inherited from the spike

Vision gives no tracking ID, so `h0` can swap to the other physical hand between
frames unless we prevent it. Algorithm:

1. Match by `chirality` when both hands have confident and distinct handedness.
2. Otherwise match by wrist proximity to the previous frame's slots.
3. Hold a slot through brief dropouts (N-frame grace) before releasing it.

This needs its own test with a two-hand fixture. Silent slot-swapping looks
fine on screen and corrupts everything downstream, which makes it the most
dangerous defect in the system.

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

---

## 8. Lifecycle

A background thread calling into native frameworks is a liveness hazard, not
just a tidiness problem — a call landing on a torn-down capture session is a
native crash that `except Exception` will not catch and that takes the whole
process, i.e. TouchDesigner, down with it.

- `stop()` sets an atomic flag, stops the `AVCaptureSession`, stops the run loop
  if we own one, and joins with a timeout.
- The capture delegate checks the flag before touching anything.
- **A TD reload must stop the old engine before starting a new one**, or two
  sessions fight over the camera. State surviving a project reload is the most
  likely way this misbehaves in practice — wire an explicit teardown into the
  project's unload path and make start idempotent.
- The camera TCC prompt attaches to TouchDesigner.app, not to our code. Verify
  the permission is granted before diagnosing anything else;
  `AVCaptureDevice.authorizationStatusForMediaType_` returns 3 when authorised.

---

## 9. C++ upgrade path — deliberately not v1

TouchDesigner supports C++ custom operators; the SDK ships in the install at
`Contents/Resources/tfs/Samples/CPlusPlus` (headers `CPlusPlus_Common.h`,
`CHOP_CPlusPlusBase.h`, `TOP_CPlusPlusBase.h`, plus Xcode projects; macOS
custom ops are `BNDL` bundles).

**A C++ plugin does not solve the threading problem.** `execute()` is called on
TD's cooking thread — blocking there blocks TD exactly as much as Python would.
Derivative's own `CPUMemoryTOP` sample demonstrates the required pattern:
`std::thread`, `std::atomic<bool> myThreadShouldExit`, `join()` on destruct, and
a `FrameQueue` guarded by `std::mutex` that `execute()` drains. That is
structurally identical to §4.3, which is a good sign the design is sound.

The usual reason to go C++ — escaping the GIL — **does not apply**, per §2.2.

Go C++/Objective-C++ if and when:

- TD needs the camera **image** as well as landmarks. `TOP_Buffer` upload is the
  correct mechanism and is where Python gets genuinely awkward. This is the
  strongest trigger.
- pyobjc inside TD's Python proves unstable (milestone 1 answers this).
- The pyobjc bridge or venv bootstrap becomes a deployment liability. Obj-C++
  calls `VNDetectHumanHandPoseRequest` natively; pyobjc is a bridge we are
  choosing to keep.

Costs: an Xcode project per TD version, bundle signing, far slower iteration
than editing a Python DAT live, and harder in-process debugging.

---

## 10. Milestones

1. ~~**Go/no-go: pyobjc inside TD.**~~ **DONE — passed.** See §2.5: pyobjc
   loads in TD's process, Vision runs there at 2.06 ms median on live camera
   buffers, and no run loop of our own is needed. Probe kept at
   `tools/probe_m1_in_td.py`.
2. **`engine.py` headless.** Camera → `LandmarkFrame`, no TD. Largely a port of
   `reference/spike_vision_hands_live.py`.
3. **`HandSource` + lock-free slot.** Tested headless against a fake source, so
   the concurrency contract is verified without a camera.
4. **Script CHOP.** Fixed channel contract, verified against the fixture clip.
5. **Slot assignment** with a two-hand fixture.
6. **Teardown and reload hardening** (§8).
7. **README**, and fold anything newly measured back into §2.

Milestone 1 needs TD running. Everything from 2 onward is headless.

---

## 11. Open questions

- ~~Does TD's existing run loop serve AVFoundation delegate callbacks?~~
  **CLOSED (§2.5): the question was malformed.** No run loop is involved at all
  — delivery is on a GCD queue and works with none turning, inside TD or out.
- ~~Does pyobjc load cleanly inside TD's bundled Python 3.11?~~ **CLOSED
  (§2.5): yes.** TD 099 ships 3.11.15; pyobjc 12.2.2 imports and runs Vision in
  TD's process.
- ~~Best venv strategy~~ **CLOSED (§2.5): a dedicated venv at
  `~/.venvs/visionhands`, its `site-packages` APPENDED to `sys.path`.** Measured
  inside TD: numpy correctly resolved to TD's own 2.1.2 rather than the venv's
  2.2.6, which is the outcome that keeps TD's extension modules matched to the
  numpy they were built against. Inserting at position 0 would invert that.
- Two-hand behaviour is entirely unmeasured — throughput, chirality stability
  and slot assignment were all validated single-hand only.
- Deferred by explicit decision, all cheap to measure once the fixture-replay
  path exists: end-to-end latency (photons → channels), dropout rate under
  motion and occlusion, and per-joint jitter with a still hand. Jitter in
  particular determines whether smoothing is needed downstream, and it is the
  usual reason a demo that looks fine on stills feels bad live.

---

## 12. Provenance

Everything in §2 and §3 was measured on the target machine during the spike that
produced [`reference/`](reference/). Section 9's claims about the TD C++ SDK come
from reading the headers and samples in the local TouchDesigner install.
Sections 4–8 and 10 are design proposals and have not been built.
