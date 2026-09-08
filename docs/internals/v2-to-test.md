# V2 — for Omer to test

Things I could not verify myself, and exactly what to look for. Kept as a running list;
I add to it as I build. Tick them off and tell me which failed.

**Nothing here is a known bug.** These are the checks that need a camera, a person, or a
judgement about how something looks — none of which I can do from a script.

Current as of 2026-09-08, after the housekeeping pass over the code review.

---

## Do this first

**Press `Install`.** `~/Library/Application Support/appletd` is what TouchDesigner
actually imports — it sits ahead of the checkout on `sys.path` — and as of 2026-09-08 it
was **9 files short and 21 stale**. None of V2's package work is live in the project
until you press it. The sidecar is unaffected, because it runs from a different
interpreter, which is why nothing looked wrong.

Everything below assumes this has been done.

---

## Confirmed by Omer, 2026-09-08

  * **Multi-Person mask colours** — red/green/blue/white, checked on one person.
  * **The face skeleton** draws from the four key points.
  * **`hands_angle_x`/`_y`** move sensibly with one hand nearer than the other.
  * **Camera Flip**, **Freeze**, **the pose skeleton's shape**, **hand angles**, and
    **optical flow alone**.

## Worth one look, from today's work

**The face angles are ours now, not Vision's.** Turn and tilt your head: `f0_angle_y`
and `f0_angle_z` should move CONTINUOUSLY, not in 45° and 30° steps. `f0_angle_x`
(pitch) is RELATIVE — it may read a degree or two off zero looking straight ahead, and
what matters is that it moves the right way when you nod.

**`Camera` = `(default)`** should give a picture, not a blank. It resolves to the same
camera the sidecar picks on its own.

**TOP Input mode** should keep capture RUNNING rather than going to Stopped, and the
sidecar log should say `waiting for TouchDesigner to publish a frame` until something
is wired to the COMP's image input. With `Output Video` on, the picture should be that
input rather than the camera — and the camera should not be opened at all.

**The status counts down.** A launch-flag change with `Auto Refresh` on reads
`Restarting in 3`, `2`, `1`. `Freeze` with a timer set reads `Freezing in N`. Neither
should ever be left on screen after the action happens or is declined.

**`Active` off should stop the overlays cooking** while leaving the last picture up.

## Already verified — no need to retest

  * **Freeze** — you confirmed it, 2026-09-08.
  * **The output contract under a hands overlay.** `out1` stays at 46 channels with zero
    non-tip joints whether `Show Overlay` is on or off, while `overlay/in1` goes from 13
    hand `_tx` channels to 43. Checked by toggling.
  * **The overlay gating logic.** Every render flag reads False when `Streamhands` is
    off, at all three levels. Checked by toggling.
  * **Camera Flip's effect on Vision.** Landmarks and depth both come back mirrored —
    measured on a fixture frame, not inferred (`BENCHMARKS.md`).

---

## Needs a person in front of the camera

**Multi-Person mask.** `Streamsegment` on, `Multi-Person` on, restart. With two people in
frame the mask should carry **1 for one person and 2 for the other**, not 255 for both.
One person reads 1. Nobody reads all zeros. Vision separates at most four.

  * Why I could not: the fixtures are hands-only by policy, and Vision needs a real
    person. The API says `instanceMask` is index-encoded; I have not seen it happen.
  * If it comes back 0/255, the request is falling back to the single-person path and I
    have the wrong observation.

**Face and hand angles.** `f0_angle_x/y/z` replaced `f0_pitch/yaw/roll`. Nod, turn and
tilt your head and check each moves the axis you would expect: **x nods, y turns, z
tilts.** If two are swapped, the mapping in `face_types.py` needs reordering, not the
maths.

  * **The quantisation is FIXED.** Vision's own angles came in 45° and 30° steps; all
    three are computed from the landmarks now, so they are continuous. Pitch is
    relative — see "Worth one look" above.

**Hand `angle_x` / `angle_y`.** Tilt a palm toward the camera, then the same amount away.
They will read the SAME - that is the documented sign ambiguity, not a bug. What matters
is that the RATIO tracks which way you lean. `angle_z` should follow an in-plane roll
exactly.

**The pose skeleton.** `Streampose` on, `Show Overlay` on. 18 segments off the 19 joints:
head to neck, neck to both shoulders and down the spine, a chain per limb. The bones are
my reading of the joint table — Vision publishes no skeleton — so tell me if any of them
look wrong rather than merely ugly.

---

## The overlays

**The hand skeleton.** `Streamhands` on, `Show Overlay` on, `Overlay Mode` = Finger
Skeleton, `Output Video` on. This is the network you built, generated: 21 segments per
hand, red lines, green points.

  * What to look for: the skeleton **lands on the hand in the image**, and no stray line
    runs from a fingertip back to the wrist or between the two hands. A stray line means
    the delete list is wrong.
  * It may lag the picture by a frame. The landmarks come from the sidecar's frames and
    `video_in` is a separate camera client — a display artefact, not a tracking error.

**`Overlay Mode` switches cleanly.** Change to `Index Line`: the full skeleton should go
and a four-segment index chain (wrist → mcp → pip → dip → tip) should appear. Exactly one
mode draws at a time.

**`Show Overlay` is inert while its stream is off.** Turn `Hands` off with `Show Overlay`
still on — the skeleton should disappear entirely rather than freeze on screen.

**The face skeleton draws the four KEY POINTS**, not the landmarks: the eye line, both
eyes to the nose tip, nose to mouth. `Facekeypoints` strips the 348 landmark channels at
the stream, so a landmark skeleton would draw nothing in a default project — that is why
this is the default mode. `All Landmarks` is the other mode and needs `Face Key Points`
turned OFF to have anything to draw.

---

## Camera Flip

**The picture and the tracking mirror together.** `Camera Flip` on, restart. The image
should mirror AND the overlay should still sit on the hand. If the picture flips but the
skeleton does not follow, `video_flip` and the sidecar disagree.

**Nothing else needs flipping.** The mask, depth and flow TOPs come back already mirrored
— Vision applies the orientation to its image outputs, measured. If any of those looks
mirrored the wrong way with the flip on, that is a real finding and I want it.

**Chirality follows the mirror, by design.** A mirrored left hand IS a right hand and
Vision says so, so `h?_chirality` reports the mirrored world. Correct for drawing on a
mirrored image, wrong for "which of my actual hands is raised" — say if you want that
compensated instead.

---

## Needs the camera, no person

**Auto Refresh.** `Active` on, `Auto Refresh` on. Change a stream toggle. Nothing should
happen for three seconds, then one restart. Flip four toggles quickly: still **one**
restart, three seconds after the last.

**TOP Input through Vision.** `Input Mode` = TOP Input, wire a movie or a render into the
COMP's image input, restart. Hands should be found in the TOP's content. **No camera
permission prompt should appear and no camera light should come on** - that is the point
of the mode.

  * The transport is proven byte for byte (a constant TOP came out as the right BGRA),
    but the sidecar has never actually run in this mode.

**Output Video follows the Camera choice.** Pick a device in `Camera` and the picture
should change with it. The `device` parameter stores an opaque token
(`V1|||<uuid>|||0|||0|||MacBook Pro Camera`), not the name, so the expression looks the
name up in the menu - `(default)` and any name not in the list both give an empty
device, which lets the engine pick.

**Output Video is a second client on the camera.** The sidecar already has the device
open; `video_in` opens it again. macOS has allowed that since Ventura, but it is worth
confirming on your machine.

**Freeze Timer Seconds.** Set it to 5, press `Freeze`, and get both hands into frame. It
should fire five seconds later, once, even if you press twice.

**Optical flow.** `Optical Flow` page, `Streamflow` on, restart. `outflow` carries
R = x displacement in pixels, G = y. It is **backward** flow - the vector points back to
where the content came from - and the values are in input pixels, not normalised. Expect
16-30 ms a frame depending on accuracy, so the camera will drop buffers with it and depth
both on.

  * Its callbacks DAT was broken until 2026-09-08 - a missing `import os` - so this has
    never run.

---

## Judgement calls, not pass/fail

**Hide Unused Outputs.** Off by default. On, it removes ANY output nothing is feeding -
and the outputs after it renumber, because a connector slot cannot be reserved. Watch the
textport: every change prints what went and what the connectors now are. If you have
wires drawn from this COMP, settle which outputs you want before drawing them.

**Overlay colours.** Hands keep your red lines and green points. Pose is blue with yellow
points and face is amber with orange, so two overlays at once can be told apart. Say if
you would rather they all matched — it is one table in `tools/td_add_overlay.py`.

**The overlay camera follows `Orthowidth`.** `cam1`'s ortho width is an expression on it
rather than the 1.0 you had, so the two cannot drift. If you change `Orthowidth` the
overlay should stay the right size against the image.

---

## Known and not worth reporting

  * A **closed lid** disables the built-in camera silently: the session starts, no error
    appears, and no frame ever arrives. Diagnosed 2026-09-07. Pick another device or
    open the lid.
  * `tools/vision_landmarks_live.py` trips `ruff` (RUF059). It is your file and
    untracked, so I leave it alone.
  * Rebuilding with `tools/td_build_vision.py` alone puts new parameters back on the
    General page. `tools/td_rebuild.py` runs the pages layer after it; run that instead.


---

## After the housekeeping pass (chore/v2-housekeeping)

Everything in this section is already verified by the suite or against the running
project; these are the parts that need a camera, a person, or your judgement about how
something behaves. **The full chain was run and is idempotent** - 18 builders, 174
parameters in and out, nothing added, nothing lost, nothing changed.

**Restart capture before testing any of this.** The sidecar imports the installed
package once, at launch, and yours has been running since before this pass. The panel
will now tell you this itself after a future install - `Installed - ... - restart
capture to run it` - but it cannot know about the one already running.

- [ ] **The About page's four buttons.** They were doing nothing at all - a master
      rebuild had eaten `about_control` and `about_callbacks`, and a pulse with no
      Parameter Execute behind it fails silently. Check For Update, Open In Browser,
      Licence and Apply Update should all now do something.
- [ ] **`Keep Layout`.** Rearrange the master network however you like, switch it on,
      rebuild. Everything should stay where you put it - including `sidecar_control`
      and the other DATs the master destroys and recreates, which never honoured it
      before. Verified here with three operators; your arrangement is a better test.
- [ ] **`Depthpinson`.** Turn Use Pins OFF, rebuild anything, and check it is still
      off. It used to turn itself back on and switch `outdepth` from relative to
      metric with nothing said.
- [ ] **Turning every stream off.** `out1` should keep its channels frozen rather than
      emptying. And with hands off specifically, `clap_count` and `apart_count` should
      leave the output rather than sitting there holding their last value.
- [ ] **Two TouchDesigner instances**, or a sidecar started from a terminal. The panel
      should say `Not Ours - pid N` rather than `Running`. Pressing `Active` takes
      over; `Stop` still reaches it.
- [ ] **Restart capture twice quickly**, and flip `Camera Flip` during the camera
      warm-up. Neither should ever leave two sidecars running - the log will say
      `NOT starting` if one refuses to die.
- [ ] **A multi-person mask.** `MaskImage.coverage` used to read 0.0 for every
      instance mask; nothing on the panel shows it, so this is only worth checking if
      you use it from a script.
