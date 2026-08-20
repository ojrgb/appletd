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

---

## 3. Traps — all of these cost real time in the spike

**GIL starvation from a polling main thread.** A tight
`CFRunLoopRunInMode(..., 0.05, False)` loop in Python holds the GIL and starves
the capture thread: delivered frame rate went from 28 fps to **8.4 fps**. One
long blocking call fixed it. Anything that busy-waits in Python next to a pyobjc
callback thread will do this.

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
exposure ramps and detect nothing. Skip ~15 frames before counting anything.

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

**Run loop.** My standalone spike needed `CFRunLoopRun` for AVFoundation to
deliver anything. **Inside TD this is probably already satisfied** — TD is a
Cocoa app whose main run loop is turning, which is the condition a bare Python
process lacks. Verify at milestone 1. If confirmed, the design loses a thread;
if not, add a dedicated run-loop thread. Do not assume either way.

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
h<i>_found, h<i>_score, h<i>_chirality           for i in 0..MAX_HANDS-1
h<i>_lm<jj>_x, h<i>_lm<jj>_y, h<i>_lm<jj>_conf   for jj in 00..20
```

`MAX_HANDS = 2` gives 3 + 2×(3 + 63) = **135 channels**.

Gate downstream work on `h<i>_score` and per-joint `conf`, never on `found`
alone. Publish `age_ms` and treat a rising value as engine death.

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

1. **Go/no-go: pyobjc inside TD.** Boot TD, import `Vision` and run one request
   from a Text DAT. Also settle whether TD's run loop already serves
   AVFoundation (§4.2). Cheap, and it gates everything.
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

- Does TD's existing run loop serve AVFoundation delegate callbacks? (M1)
- Does pyobjc load cleanly inside TD's bundled Python 3.11? Both are cp311, so
  the wheels are ABI-compatible, but this is untested inside TD's process. (M1)
- Best venv strategy: append a dedicated venv's `site-packages` to `sys.path`
  (preferred — survives TD updates) versus installing into the app bundle
  (pollutes it, does not survive updates).
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
