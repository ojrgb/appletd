# Next, from Omer's testing — 2026-09-08

Eight items, in his words then mine. Nothing here is started; the comment sweep took
this session. Ordered as given, not by priority.

## 1. Multi-Person mask colours are too dark

Switching `Multi-Person` on makes the mask values too low to see. With support for four
people the values should be **Red, Green, Blue and White** rather than a near-black
index ramp.

  * Today `instanceMask` is natively index-encoded — 0 background, 1..4 per person — so
    the mask carries 1/255, 2/255 … which reads as black.
  * So this is a mapping decision, not a detection one: index → colour, in the Script
    TOP that publishes the mask (`seg_mask`), or in the sidecar before it writes.
  * Four people, four colours, and white last so a fourth person is not invisible on a
    light background.

## 2. Face angles — treat the quantisation as an exception

`f0_angle_y` snaps to 45° steps and `angle_z` to 30°. I reported that we already
request revision 3, the highest `VNDetectFaceLandmarksRequest` offers, which was an
answer about the API and not about what we should do.

Omer: "Not sure what you mean that the revision is as high as it can go - is this our
standards? If so let's just treat this as an exception."

  * So: find a way to get continuous angles, rather than documenting the steps.
  * Worth trying: derive the angles from the LANDMARKS instead of from the
    observation's own `roll`/`yaw`/`pitch`. We already publish 76 points; the eye line
    gives roll directly and the nose-to-midline offset gives yaw. That is our own
    maths on continuous inputs, so it cannot be quantised.
  * Keep Vision's values too, or replace them? Ask. Replacing changes the contract.

## 3. `hands_angle` split into x/y/z

Hand per-joint angles were renamed to `angle_[xyz]` and are fine. The DERIVED
`hands_angle` — the whole-hand angle from `derive.py` — was not, and should get the
same treatment.

## 4. Pose skeleton — Omer is adjusting it tomorrow

The 18 bones I read off the joint table are acceptable. He wants to change some. The
table is `BODY_CONNECTIONS` in `appletd/skeletons.py`; editing it regenerates the
Select channels and the Delete list together.

## 5. Face skeleton should use the STRIPPED landmarks

Not all 76 points. Use what `in1` actually carries — the key points: mouth, left eye,
nose tip and so on.

  * This REPLACES the plan to hold `Facekeypoints` back for the overlay. Much better:
    no 348 channels, no 1.21 ms, and nothing to guard.
  * So `face_connections()` should be built from `FACE_KEYPOINTS`, not `FACE_REGIONS`.
  * Those are box-relative and composed through the box by `coords/world`, so the
    `_tx`/`_ty` the overlay reads already exist.

## 6. Status should count down

**Restart:** when a restart is pending, `Capturestate` should read
`Restarting in 3`, `2`, `1` rather than `Requires Restart`.

**Freeze:** when `Freeze Timer Seconds` is set, the countdown to the freeze should show
in the status the same way.

  * `schedule_refresh` already carries a debounce token; the countdown can hang off the
    same mechanism rather than a second timer.
  * One thing to settle: `Capturestate` is also where startup failures now appear.
    A countdown must not overwrite `Stopped - <reason>`.

## 7. TOP Input mode should route Output Video from the TOP

With `Output Video` on and `Input Mode` = TOP Input, the video output should be the
COMP's TOP INPUT, not the camera — and `video_in` should be bypassed so nothing opens
the camera at all.

  * `video_in.active` is already gated on `Outputvideo`; it needs `Inputmode` too.
  * The composite's background becomes `in_frames` in that mode. A Switch TOP on
    `Inputmode`, feeding `video_flip`.
  * And with the camera never opened, the flip still applies — `video_flip` stays where
    it is.

## 8. Changing Input Mode must not report Stopped

Switching `Input Mode` from Camera to TOP Input flipped the status to `Stopped` while
the sidecar was still running happily on the camera.

  * A launch flag changing should give `Requires Restart`, which is what the state
    machine is for. `Stopped` means "no process", and there was one.
  * My first suspicion — that `running_pids()` matches on a command line rebuilt from
    the panel — is WRONG. `MATCH` is the fixed string `"appletd.sidecar"`, so a launch
    flag cannot affect it. Checked before writing this down.
  * `Stopped` therefore means `running_pids()` really was empty, so the process really
    had gone. With `Auto Refresh` on, changing `Inputmode` schedules a restart; the
    likeliest story is that the restart RAN and the sidecar then failed to start in
    TOP Input mode — no frames buffer, nothing feeding the TOP.
  * Which is now easy to confirm: `Capturestate` carries the reason since today.
    Switch the mode and read what it says before changing anything.

---

**Verified working by Omer, 2026-09-08:** Freeze, hand angles, the pose skeleton's
shape, Camera Flip, and optical flow alone.
