# apple-vision-landmarks

**Hand, body and face landmarks on macOS with Apple's Vision framework, from Python.
No MediaPipe, no model files, no CUDA — 3.4 ms per 720p frame on an M4 Pro.**

[![tests](https://img.shields.io/badge/tests-105%20passing-brightgreen)](#)
[![macOS only](https://img.shields.io/badge/platform-macOS%2014%2B-lightgrey)](#)
[![python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](#)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> This is Apple's **Vision.framework** — the on-device vision API that has shipped
> in macOS since 10.13. Nothing to do with Vision Pro.

<!-- 12s loop: hand moving, 21 joints drawn, ms/frame counter in the corner -->
![demo](docs/media/demo.gif)

```bash
python3.11 -m venv ~/.venvs/vision && ~/.venvs/vision/bin/pip install apple-vision-landmarks
~/.venvs/vision/bin/python -m vision_landmarks.demo        # live camera, overlay window
```

That is the whole install. The detector is already on your Mac.

---

## Why this exists

Every "hand tracking in Python" answer is MediaPipe: a 30 MB wheel, a bundled
model, a pinned protobuf, and an install that breaks on Apple silicon roughly
once a year. macOS has shipped a hand-pose detector since Sonoma that is faster,
needs no model file, and is already signed and sandboxed by the OS. The only
thing missing was a Python path to it that documents the traps.

This repo is that path, plus **every measurement and every trap written down with
its provenance** — see [`docs/MEASUREMENTS.md`](docs/MEASUREMENTS.md).

## What you get

| | |
|---|---|
| **Hands** | 21 joints × (x, y, confidence), chirality, up to 2 hands. Shipping. |
| **Body pose** | 19 joints. Roadmap — API designed, **not yet measured**. |
| **Face** | Face rectangles + landmark constellation. Roadmap, **not yet measured**. |
| **Dependencies** | `pyobjc-framework-{Vision,Quartz,AVFoundation,libdispatch}`. That is all. |
| **Extras** | `numpy`, `opencv-python`, `pillow` — dev/demo only, never imported at module scope. |

## Measured, on the target machine

Deterministic replay of a fixed 720p clip (284 frames, real moving hand),
macOS 26.5.2 / Darwin 25.5.0, M4 Pro, pyobjc 12.2.2:

| input | hand present | no hand |
|---|---|---|
| 640×480 | 3.41 ms (~293 fps) | 1.99 ms |
| **1280×720** | **3.29 ms (~304 fps)** | **1.82 ms** |
| 1920×1080 | 5.08 ms (~197 fps) | 3.82 ms |

Two independent recordings on two toolchains agreed to within 0.12 ms.

- **Below 720p is not faster.** 640×480 is consistently *slower*. Vision
  normalises internally; downscaling costs accuracy and buys nothing.
- **`maximumProcessingDimensionOnTheLongSide` is a no-op** — flat ~3.1 ms from
  192 through unset. Don't spend an afternoon on that knob.
- **pyobjc releases the GIL during Vision calls.** A background Vision thread ran
  at 102% of a main thread's throughput while inferring concurrently. The
  inference thread absorbs the contention, not your app.
- **First call costs 68 ms–4.3 s** (model load). Warm up once before timing.

Every number here comes from fixture replay, never from a live camera — identical
live configs gave a **8× spread** (2.1–16.3 ms) because cost tracks what is in
shot. See [`docs/MEASUREMENTS.md`](docs/MEASUREMENTS.md) for the full table and
the machine each row was taken on.

## Use it

```python
from vision_landmarks import HandEngine

engine = HandEngine(width=1280, height=720)   # asserts delivered dims == requested
engine.start()                                 # AVFoundation delivers on a GCD queue
frame = engine.latest()                        # NEVER blocks; returns the newest frame
for hand in frame.hands:
    tip = hand.joint("index_tip")
    print(hand.chirality, tip.x, tip.y, tip.conf)   # normalised, origin BOTTOM-LEFT
engine.stop()                                  # mandatory — see Lifecycle
```

`LandmarkFrame` is a frozen dataclass of floats and tuples. **No pyobjc object
ever leaves the capture thread**, which is what makes the frame safe to read from
anywhere with no lock: the handoff is a single-slot latest-value box, and the read
measured **0.0047 ms median / 0.059 ms worst** over 103 reads taken during live
capture.

### Examples

| script | what it shows |
|---|---|
| `examples/still_image.py` | one PNG in, annotated PNG + joint table out |
| `examples/live_overlay.py` | live capture with a cv2 overlay, confidence-coloured |
| `examples/replay_benchmark.py` | deterministic timing against your own clip |
| `examples/raw_pyobjc.py` | the same thing in ~60 lines of bare pyobjc, no package |

`examples/raw_pyobjc.py` exists so you can lift the calls into your own code and
never depend on this repo.

## The traps, all of which cost us real time

Full annotated list in [`docs/TRAPS.md`](docs/TRAPS.md); the ones that bite first:

1. **Out-parameters.** Every `…error_` selector takes `None` as its last argument
   and returns a tuple: `ok, err = seq.performRequests_onCMSampleBuffer_error_([req], buf, None)`.
2. **Resolution control does not work the documented way.** `setActiveFormat_` is
   silently reverted by the session preset, and `AVCaptureSessionPresetInputPriority`
   raises on macOS. Use `kCVPixelBufferWidthKey`/`HeightKey` in the output's
   `videoSettings` — and **assert delivered dimensions against requested**, because
   two spike runs produced 1080p while the log claimed 720p.
3. **Vision returns coordinates for joints it cannot see.** Over 7056 joint
   samples: median confidence 0.685, **40.8% below 0.5, 11.0% at exactly 0.0**. A
   0.0-confidence joint still hands back a plausible-looking position. Gate on
   confidence, always.
4. **`VNHumanHandPoseObservation.confidence()` is a constant 1.0** — 336 of 336
   observations. Use the median of the 21 joint confidences instead.
5. **No tracking IDs.** Observations are independent per frame, so which hand is
   "hand 0" is *your* problem. This is the one real functional gap versus
   MediaPipe. `SlotAssigner` here matches on chirality, then wrist proximity, with
   an N-frame grace period.
6. **`CVPixelBufferCreateWithBytes` does not copy** — the backing array must
   outlive the buffer. And `CVPixelBufferGetBytesPerRow` is not `width * 4`.
7. **Pick the capture device explicitly.** A dev Mac enumerates Camo, OBS Virtual
   Camera and a Continuity iPhone next to the built-in camera; index 0 is a good
   way to debug black frames from OBS.
8. **Camera warm-up is ~1.5 s.** A 2 s measurement window opened at `start()` read
   6.5 fps; the same window opened 1.5 s later read 30.0 fps.

## Coordinates

Vision is **normalised, origin bottom-left, y up**. Everything in this package
stays in that space. The one flip lives in `coords.py` and is used only by the
cv2/PIL overlay, because those are top-left. The failure this guards against is
not a crash — a vertically mirrored hand looks entirely plausible.

Names carry the frame: `_norm`, `_px`, `_bl`, `_tl`.

## Lifecycle — read this before shipping

A Vision or AVFoundation call landing on a torn-down capture session is a
**native crash that takes the process with it**, and `except Exception` will not
catch it. `stop()` sets an atomic flag, stops the session, and joins with a
timeout; the delegate checks the flag before touching anything. `start()` is
idempotent and stops any existing session first, so two capture sessions never
fight over one device.

The camera TCC prompt attaches to the *host* app, not to this code.
`AVCaptureDevice.authorizationStatusForMediaType_` returns 3 when authorised.

## Diagnostics

```
$ python -m vision_landmarks.doctor
macOS 26.5.2 (Darwin 25.5.0) · arm64 · Python 3.11.9
pyobjc 12.2.2 · Vision request revision 1
camera authorisation: 3 (authorised)
devices: FaceTime HD Camera [built-in] · OBS Virtual Camera · iPhone (Continuity)
requested 1280x720 → delivered 1280x720 ✓
warm-up 1.48 s · first inference 68.4 ms · steady median 3.31 ms
```

Paste that into any issue and it answers most of the questions we would ask.

## Using this with TouchDesigner

Don't run it inside TouchDesigner's Python. Measured: a Python background thread
in TD's process is starved **28×**. Use
[**touchdesigner-apple-vision**](https://github.com/ORG/touchdesigner-apple-vision),
which runs this package in its own process and sends landmarks to TD over OSC at
a steady 30 fps.

## Compared to MediaPipe Hands

| | apple-vision-landmarks | MediaPipe |
|---|---|---|
| install | 4 pyobjc wheels | ~30 MB wheel + pinned protobuf |
| model file | none — it's in the OS | bundled |
| 720p latency | 3.3 ms (M4 Pro, measured) | comparable, not measured here |
| hand tracking IDs | none — you assign slots | yes |
| platforms | macOS only | everywhere |
| 3D / z depth | no | yes |

Pick MediaPipe if you need cross-platform, tracking IDs or depth. Pick this if
you are on a Mac and want no dependencies and no model files.

## Fixtures, and a privacy rule we recommend you copy

Video fixtures are **gitignored by default**. A clip of a person entering git
history is expensive to remove, and body plus clothing plus room identify someone
perfectly well without a face in shot — our first fixture passed a
`VNDetectFaceRectangles` gate with zero faces while showing the author from the
chin down in their home. `tools/record_fixture.py` gates on
`VNDetectHumanRectangles` and prints a frame montage to look at before you commit
anything.

For CI we ship `fixtures/replay.jsonl` instead: recorded *Vision outputs*, no
imagery. Contract, coordinate and slot-assignment tests run on a fresh clone;
timing tests need a clip you record yourself, and say so when skipped.

## Contributing

Two rules, both enforced by review:

1. **A number without provenance is not a result.** Every performance claim names
   the machine, OS, fixture and date. Anything unverified is tagged `# UNMEASURED:`
   in the code and labelled in prose. `# MEASURED:` carries its figure.
2. **Comments carry what the code cannot** — units, thread, invariants, and why the
   obvious alternative is wrong. A comment that restates its line is deleted.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`docs/STANDARDS.md`](docs/STANDARDS.md).

## Licence

MIT. Apple's Vision framework is part of macOS and is used through the public API.
