# V2 — questions, and what I assumed to keep moving

Raised 2026-09-07, while Omer was away. Each has an assumption in **bold** that I built
against, so nothing is blocked; anything he decides differently is a small change now
rather than a rewrite.

Availability probe, macOS 26.6.2 — all present: `VNGeneratePersonInstanceMaskRequest`,
`VNRecognizeTextRequest`, `VNTrackObjectRequest`, `VNTrackOpticalFlowRequest`,
`VNDetectHumanRectanglesRequest`, `VNDetectTrajectoriesRequest`, `VNCoreMLRequest`.

---

## 1. Auto-refresh

No blocking question. The 3 s debounce is a constant rather than a parameter — one
number nobody needs to tune, and a parameter for it would need a home on a page.

**Q1.1** Should auto-refresh restart when a stream toggle is switched OFF? Turning a
stream off already freezes its COMP immediately; the restart only stops the sidecar
computing it. **Assumed yes** — otherwise "Requires Restart" can sit on the panel
indefinitely with auto-refresh on, which is worse than a restart nobody asked for.

## 2. Multi-Person mask

**Q2.1 — what does the mask TOP contain when there are several people?** This is the
real question, because it changes what the mask MEANS.

  * (a) the union, as today: 0 or 255.
  * (b) one grey level per instance: person 1 = 1, person 2 = 2, and so on.

**Assumed (b)**, because (a) is recoverable from it with a `> 0` threshold and (b) is
not recoverable from (a). It does change the mask's meaning when `Multi-Person` is on,
so the existing 0/255 behaviour is exactly what you get with it off.

**Q2.2** `VNGeneratePersonInstanceMaskRequest` has a documented ceiling of four people.
**Assumed** we publish a `sc_seg_people` count so a project can tell "nobody" from
"five people, one of them dropped".

## 3. Input Mode: TOP Input

**Q3.1 — ANSWERED, and the answer is that it is affordable.** MEASURED 2026-09-07:
the CPU readback costs **0.26 ms median at 720p**, 0.57 at 1080p, against a 33.3 ms
frame. Full table in `docs/BENCHMARKS.md`. The maxima matter more than the medians -
`delayed=False` forces a GPU sync and spikes to 3.3 ms at 720p - so the writer uses
`delayed=True`, which returns the previous frame and does not stall. One frame of
latency on a path that is already a frame behind.

**Q3.2** What should the sidecar do when the TOP stops updating - hold the last frame,
or report the stream as dead? **Assumed hold**, matching how a mask read handles a
missed write.

**Q3.3** Colour: TOPs are RGBA, Vision wants BGRA. **Assumed** we convert on the TD
side in the Script TOP, since it is one GPU operation there and a per-pixel loop in
the sidecar.

## 4. Hide unused outputs

**Q4.1 — ANSWERED 2026-09-07, by hitting it.** With `Output Video` on, turning depth
and the mask off left both their outputs in place - because the first version removed
only from the END inwards, to stop a live connector changing meaning.

Omer chose REMOVAL. So any unwanted output now goes, in any order, and the consequence
is real: `connectorder` fixes the ORDER of connectors but does not reserve a SLOT, so
removing one slides everything after it down. A wire drawn from connector 3 can end up
reading connector 2's image.

The mitigation is a printed line rather than a restriction - every change names what
went and what the connectors now are, so a renumber is visible while it happens.

## 5. Additional models

**Q5.0 — THE ONE THAT DECIDES THE WORK, and it is architectural.** Every capability
this component has today is a STREAM: its own UDP port, its own COMP inside the master,
its own entry in `spaces.py`, its own coords branches, its own page. That is roughly a
day of work per stream and a permanent widening of the channel contract.

Six new models is therefore not one decision but six, and some of them do not want to
be streams at all:

  * `VNDetectHumanRectanglesRequest` is genuinely stream-shaped - boxes per person,
    exactly like faces. But POSE already detects people, at the cost of 19 joints. Is
    this a new stream, or extra channels on the pose stream for projects that want the
    box without the skeleton?
  * `VNTrackOpticalFlowRequest` produces an IMAGE. It is a TOP output, like the mask
    and the depth map, and shares nothing with the CHOP contract.
  * `VNRecognizeTextRequest` produces STRINGS. It is a DAT output, which this component
    has never had.
  * `VNTrackObjectRequest` and `VNDetectTrajectoriesRequest` are stateful across frames
    and need something to track - see Q5.2.
  * YOLOv3 is boxes plus class indices: stream-shaped, and the closest to what exists.

**I have not started any of them**, because guessing wrong here means a day of work in
the wrong shape rather than a parameter in the wrong place. What would help most is
which two or three actually matter to you, and I will build those properly rather than
six half-built ones.

My own ranking, for what it is worth: YOLOv3 first (MIT, stream-shaped, obviously
useful), then optical flow (a TOP, self-contained, no contract change), then human
rectangles as pose-stream channels rather than a new stream. Text and the two trackers
last, because each needs a new output KIND or a seeding story.

**Q5.1 — text has no home.** `VNRecognizeTextRequest` returns STRINGS, and a CHOP
cannot carry one. This component has never had a DAT output. **Assumed** a Table DAT
output (`outtext`), one row per observation: text, confidence, and the four bbox
numbers.

**Q5.2 — what seeds `VNTrackObjectRequest`?** Tracking needs a starting rectangle.
**Assumed** four parameters (x, y, w, h) plus a `Track` pulse to arm it, so it works
with no other model enabled. The alternative — seeding from a YOLO or human-rectangle
detection — is better but needs a way to say WHICH detection.

**Q5.3 — ANSWERED. YOLOv3 is MIT.** Apple's `coreml-YOLOv3` card states `license: mit`:
commercial use, redistribution and modification all permitted with the notice kept. No
obstacle, and no change to anyone's terms - unlike Depth Anything's Base and Large
siblings. Three variants ship (FP32, FP16, Int8LUT); like the depth model it should be
FETCHED rather than committed, and `NOTICE.md` needs an entry when it lands.

**Q5.4 — class labels.** YOLOv3 gives 80 COCO classes, and a class name is a string.
**Assumed** channels carry the class INDEX and the name list ships as a Table DAT
beside it.

**Q5.5 — optical flow is a TOP, not channels.** `VNTrackOpticalFlowRequest` returns a
two-channel float image. **Assumed** a new `outflow` TOP, which interacts with Q4.1.

## 6. Freeze

**ANSWERED by re-reading the request** — "then turn Sidecar back on, releasing the
cache". A pulse works because `Active` is what releases it, so no second control is
needed and nothing depends on invisible state. Built as you described:

  * `Freeze` (pulse) locks `out1`, `outmask` and `outdepth`, then sets `Active` off.
  * Turning `Active` on releases the lock and starts capturing.

Built on TouchDesigner's own `lock`, which holds the data an operator last cooked and
stops it cooking. Locked BEFORE `Active` goes off, so what is held is the frame on
screen rather than whatever one more cook produces with a dead stream behind it.

## 7. angle_[xyz]

**Q7.1 — the rename breaks every patch that reads a face angle.** `f0_roll` becomes
`f0_angle_z`. **Assumed** you want it anyway; it is the reason for a V2.

Mapping assumed: **`angle_x` = pitch** (nodding), **`angle_y` = yaw** (turning),
**`angle_z` = roll** (tilting). Right-handed about the camera axes.

**Q7.2 — hands cannot have a signed pitch and yaw from 2D landmarks, and that is
geometry rather than effort.** A palm tilted 30° toward the camera and one tilted 30°
away project identically, so the sign is not in the data. What I can publish honestly:

  * `h{i}_angle_z` — EXACT. It is the in-plane roll, already computed as
    `h{i}_rotation`.
  * `h{i}_angle_x`, `h{i}_angle_y` — the tilt magnitude resolved onto two axes, with
    the sign UNKNOWN. Currently published honestly as `h{i}_tilt` and
    `h{i}_tilt_axis`.

**There is one way to recover the sign, and it is worth your call:** with the depth
stream on, sampling the depth map at the wrist against the middle MCP says which end of
the hand is nearer, which disambiguates the tilt direction. That makes `angle_x`/`_y`
genuinely signed — but only while `Streamdepth` is on, which costs 23 ms a frame.

**Assumed** for now: publish all three, with `angle_x`/`_y` unsigned and labelled, and
a `Handangles` toggle gating the cost. The depth-assisted sign is a follow-on if you
want it.
