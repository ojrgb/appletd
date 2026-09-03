# Channels and parameters

What comes out of the `appletd` COMP, and what every control does.

Conventions:

- `h{i}` is `h0` or `h1`; everything per-hand exists twice.
- Positions are normalised 0..1 with a bottom-left origin. Nothing here flips y.
- Every distance is divided by `h{i}_size`, so it is a ratio of hand geometry rather than
  of the frame, and does not change as you move toward the camera.
- Angles are degrees, counter-clockwise, 0 = +x.
- A *state* is 0 or 1 and holds. A *pulse* is 1 for exactly one frame.
- A group switched off leaves the output. Switch `Delete Empty Channels` off to see
  everything.

Reasoning behind the definitions: `docs/internals/attributes-rationale.md`.

---

## Hands

### Core — 20 channels

| channel | definition |
|---|---|
| `h{i}_palm_x`, `h{i}_palm_y` | mean of wrist and the five MCP joints |
| `h{i}_size` | distance(wrist, middle_mcp) |
| `h{i}_bbox_x1/_y1/_x2/_y2` | min/max over all 21 joints |
| `h{i}_bbox_w/_h/_area` | derived from the above |

### Presence — 10 channels

| channel | definition |
|---|---|
| `h{i}_active` | `found AND conf_median > Confthreshold AND the stream is live`, held `Activateframes` before on, `Deactivateframes` before off |
| `h{i}_entering`, `h{i}_exiting` | pulses on the edges of `_active` |
| `h{i}_held` | seconds `_active` has been continuously true |
| `both_active`, `n_active` | `h0_active AND h1_active`; sum |

### Contacts — 28 channels

| channel | definition |
|---|---|
| `h{i}_pinch_index` | distance(index_tip, thumb_tip) / size |
| `h{i}_pinch_middle/_ring/_little` | same for the other fingertips |
| `h{i}_pinch_angle` | angle of thumb_tip to index_tip |
| `h{i}_pinch_x`, `h{i}_pinch_y` | midpoint of thumb_tip and index_tip |
| `h{i}_pinching` | state on `pinch_index`, `Pinchon` / `Pinchoff` |
| `h{i}_snapping` | state on `pinch_middle`, `Snapon` / `Snapoff` |
| `h{i}_pinch_count`, `h{i}_snap_count`, `clap_count` | times fired |
| `h{i}_pinch_end_count`, `h{i}_snap_end_count`, `apart_count` | times released |

### Finger triggers — 40 channels

Thumb-to-fingertip contact for all four long fingers on both hands, sharing one
`Triggeron` / `Triggeroff` pair. `{finger}` is `index`, `middle`, `ring`, `little`.

| channel | definition |
|---|---|
| `h{i}_{finger}_trigger` | state on `pinch_{finger}`, `Triggeron` / `Triggeroff` |
| `h{i}_e_{finger}_trigger`, `h{i}_e_{finger}_release` | edge pulses |
| `h{i}_{finger}_trigger_count`, `h{i}_{finger}_release_count` | counters |

There is no thumb trigger: every distance is measured *to* the thumb tip.

### Pose — 22 channels

| channel | definition |
|---|---|
| `h{i}_curl_thumb/_index/_middle/_ring/_little` | 1 minus (distance(mcp, tip) / summed bone length). 0 straight, 1 curled |
| `h{i}_openness` | mean of the four finger curls, inverted. 0 fist, 1 open |
| `h{i}_spread` | distance(index_tip, little_tip) / size |
| `h{i}_rotation` | angle of wrist to middle_mcp. In-plane roll |
| `h{i}_point_x`, `h{i}_point_y` | unit vector index_mcp to index_tip |
| `h{i}_point_angle` | its angle |

### Motion — 16 channels

| channel | definition |
|---|---|
| `h{i}_vel_x`, `h{i}_vel_y` | d/dt of palm position, units/s, averaged over `Velocityfilter` |
| `h{i}_speed` | magnitude |
| `h{i}_dir` | heading in degrees, HELD at its last value while speed is below `Speedfloor` |
| `h{i}_dir_x`, `h{i}_dir_y` | the same heading as a unit vector, held with it |
| `h{i}_moving` | 1 when speed is above `Speedfloor`, so 0 means the held heading is stale |
| `h{i}_accel` | d/dt of speed |

### Two hands — 12 channels

| channel | definition |
|---|---|
| `hands_distance` | distance(h0_palm, h1_palm) / mean size |
| `hands_together` | state on `hands_distance`, `Togetheron` / `Togetheroff` |
| `hands_center_x`, `hands_center_y` | midpoint of the two palms |
| `hands_angle` | angle of h0_palm to h1_palm |
| `hands_approach` | d/dt of `hands_distance`. Negative closing, positive opening |
| `index_distance`, `index_center_x`, `index_center_y` | as above, for the index tips |
| `both_pinching` | `h0_pinching AND h1_pinching` |
| `hands_overlap` | bbox intersection area / smaller box area |
| `hands_symmetry` | 1 minus abs(h0_openness minus h1_openness) |

### Gestures — 18 channels

Held states, from combinations of the curls.

| channel | condition |
|---|---|
| `h{i}_g_fist` | all four fingers curled, thumb curled |
| `h{i}_g_open` | all five extended, spread above a floor |
| `h{i}_g_point` | index extended, middle/ring/little curled |
| `h{i}_g_peace` | index and middle extended and spread, ring/little curled |
| `h{i}_g_ok` | `pinch_index` closed AND middle/ring/little extended |
| `h{i}_g_thumbsup`, `h{i}_g_thumbsdown` | thumb extended, others curled, thumb up or down relative to the palm |
| `h{i}_g_horns` | index and little extended, middle and ring curled |
| `h{i}_g_gun` | index and thumb extended, others curled |

### Events

Every pulse is 1 for exactly one frame.

| channel | fires when |
|---|---|
| `h{i}_e_pinch_start`, `h{i}_e_pinch_end` | `pinching` rises / falls |
| `h{i}_e_snap`, `h{i}_e_snap_end` | `snapping` rises / falls |
| `e_clap`, `e_apart` | `hands_together` rises / falls |
| `h{i}_e_grab`, `h{i}_e_release` | `openness` crosses `Grabon` / `Graboff`. Also publishes `h{i}_grabbing` and two counters |

A gesture cannot fire again until it has released past its `off` threshold.

### Depth — 12 channels

Inferred from apparent size, not measured.

| channel | definition |
|---|---|
| `h{i}_z` | `Zreference / h{i}_size`. 1.0 at the reference distance, 2.0 at twice it |
| `h{i}_z_<finger>` | 0..1, how much the finger points along the camera axis |

- `h{i}_z` rises as the hand retreats. It is **not metres** and **not comparable between
  two different hands** — a child's hand at 40 cm and an adult's at 60 read alike. To
  recalibrate, hold a hand where you want z = 1 and read `h0_size` into `Zreference`.
- `h{i}_z_<finger>` **cannot tell toward from away** — a finger pointing at the camera
  and one pointing away project identically. Meaningful only for a straight finger; use
  `h{i}_curl_<finger>` to tell those apart. UNMEASURED on a real hand.

### Tilt — 4 channels

| channel | definition |
|---|---|
| `h{i}_tilt` | 0..90 degrees. 0 is the palm square on to the camera, 90 edge-on |
| `h{i}_tilt_axis` | degrees, which way it is leaning. Meaningless when `tilt` is near 0 |

Every other angle in the system is in-plane; these two are the out-of-plane part, and are
a magnitude and an axis rather than pitch and yaw, which a 2D projection cannot sign.

`Palmarea` is the zero point and needs calibrating: **if a face-on hand reads non-zero
`tilt`, raise it.** Spreading the fingers reads partly as un-tilting the palm — MEASURED
0.1835 at spread 0.6 against 0.4282 at 1.4 — and nothing corrects for it.

### Pose descriptor — 84 channels

`h{i}_d_<joint>_x`, `h{i}_d_<joint>_y`: all 21 joints relative to the palm centre,
divided by size, rotated so `rotation` is zero. Position-, scale- and rotation-invariant,
for gesture matching or as model features. Hand-local, so no screen-space companion.

### Coordinate spaces — 84 channels each

Every joint also arrives in TouchDesigner's spaces: `_tx`/`_ty` in world units
(`Screen Space Coords`) and `_px`/`_py` in pixels (`Pixel Coords`).

---

## Body pose — 275 channels

Plumbing only: no derived attributes, no filtering, no temporal channels.

```
pose_n_bodies, pose_seq, pose_age_ms
p<i>_found, p<i>_score, p<i>_conf_median              for i in 0..1
p<i>_<joint>_x, p<i>_<joint>_y, p<i>_<joint>_conf     19 joints
```

123 channels from the wire plus 152 transformed: every joint also arrives in world and
pixel space.

Joints, in channel order: `nose` `left_eye` `right_eye` `left_ear` `right_ear` `neck`
`left_shoulder` `right_shoulder` `left_elbow` `right_elbow` `left_wrist` `right_wrist`
`root` `left_hip` `right_hip` `left_knee` `right_knee` `left_ankle` `right_ankle`.

- **`p0` is the LEFTMOST person.** There is no chirality for a person, so two people who
  cross over exchange slots.
- **Gate on `p<i>_conf_median`, never `p<i>_score`.** The body observation confidence is
  UNMEASURED.
- **Nothing here is depth-invariant.** No pose channel divides by a size, so a raw
  distance between two pose joints measures how far away the person is standing.

---

## Face — 387 channels on the wire

```
face_n, face_seq, face_age_ms
f<i>_found, f<i>_score, f<i>_quality                  for i in 0..1
f<i>_roll, f<i>_yaw, f<i>_pitch                       DEGREES
f<i>_bbox_x, f<i>_bbox_y, f<i>_bbox_w, f<i>_bbox_h    normalised
```

The bounding box also arrives in world and pixel space. Extents follow a different rule
from positions — a width is the same width wherever the box sits, so it is scaled but not
centred:

    f0_bbox_tx = (bbox_x - 0.5) * Orthowidth       a position: centred
    f0_bbox_tw =  bbox_w        * Orthowidth       an extent: not centred
    f0_bbox_th =  bbox_h        * Orthowidth * Renderh / Renderw
    f0_bbox_pw =  bbox_w        * Resw             pixels

`roll`/`yaw`/`pitch` are not transformed — there is no yaw in pixels.

- **`roll`, `yaw`, `pitch` are DEGREES.** Values under 2 for a clearly turned head mean
  something bypassed the conversion.
- **`bbox_y` is the BOTTOM edge.** The top edge is `bbox_y + bbox_h`.
- **Gate on `f<i>_quality`, not `f<i>_score`.** Both are UNMEASURED; `quality` is
  `faceCaptureQuality`, which Apple documents as a comparison metric for the same
  subject.
- **`f0` is the LEFTMOST face**, by bounding-box centre. Two faces that cross over
  exchange slots.

### The 76 landmark points

`f<i>_<region>_<nn>_x` / `_y`, over 12 regions:

```
face_contour  17   median_line   10   left_eyebrow  6   right_eyebrow  6
left_eye       6   right_eye      6   left_pupil    1   right_pupil    1
nose           8   nose_crest     6   outer_lips   14   inner_lips     6
```

**87 slots over 76 distinct points**, because the regions overlap: eleven points are
published twice, under both region names. Summing two regions double-counts those eleven.

**These points are normalised to the FACE'S BOUNDING BOX, not the image**, which is why
they have no `_tx`/`_px` companions. To place one in the image:

    image_x = f0_bbox_x + point_x * f0_bbox_w
    image_y = f0_bbox_y + point_y * f0_bbox_h

`MAX_FACES = 2` is the practical ceiling for one datagram.

### The four key points

| channel | what it is |
|---|---|
| `f<i>_eye_left_x`, `f<i>_eye_left_y` | `leftPupil`, unchanged |
| `f<i>_eye_right_x`, `f<i>_eye_right_y` | `rightPupil`, unchanged |
| `f<i>_nose_tip_x`, `f<i>_nose_tip_y` | the point three regions share |
| `f<i>_mouth_x`, `f<i>_mouth_y` | the mean of `inner_lips` |

Box-normalised like every landmark, but unlike them these DO have `_tx`/`_ty`/`_px`/`_py`
companions. `Face Key Points` publishes these instead of the full landmark set.

Two of the four are computed rather than reported: every region except the pupils is a
ring, so the mouth centre is the mean of the six `inner_lips` points and the nose tip is
identified by intersecting three regions. If that intersection is not exactly one point,
the channel reads 0.

**There are no ears** — Vision has twelve regions and none is an ear.

`eye_left` is `leftPupil`, whatever side of the frame that turns out to be. Which side it
appears on in an unmirrored image is UNMEASURED.

---

## Sidecar status — 4 channels

On the hands port, so they arrive whatever else is enabled. Read them off the
`housekeeping` Null CHOP beside `out1`.

| channel | meaning |
|---|---|
| `sc_uptime_s` | seconds since the sidecar started sending. **FROZEN means the process is gone** |
| `sc_hands`, `sc_pose`, `sc_face` | 1 when the sidecar really started that stream |

`sc_uptime_s` is what tells "sidecar dead" apart from "stream switched off". The `sc_*`
channels report what the process is RUNNING, not what the toggles say.

Also on `housekeeping` rather than the main output: the 80 per-joint `*_conf`,
`h?_score`, `h?_conf_median`, `h?_chirality`, `seq`, `pose_seq`, `face_seq`, `age_ms`,
`pose_age_ms`, `face_age_ms`, `sc_src_w` and `sc_src_h`.

    op('/project1/appletd/housekeeping')['h0_conf_median']

---

## Hands page

The stream toggle, what it computes, what reaches the output, and its tuning. Every
stream has a page of its own; `General` holds what applies to all of them.

### What is computed

| toggle | default | effect |
|---|---|---|
| `Screen Space Coords` (`Coordstx`) | on | `_tx`/`_ty`, every stream. **1.08 ms** |
| `Pixel Coords` (`Coordspx`) | off | `_px`/`_py`, every stream. **0.90 ms** |
| `Core` | off | palm, size, bounding box |
| `Presence` | off | active, entering, exiting, held |
| `Contacts` | off | pinch, snap, clap and their counters |
| `Pose` | off | curls, openness, spread, rotation, pointing |
| `Twohands` | on | the two-hand geometry |
| `Gestures` | off | the held gesture states |
| `Descriptor` | off | the 84-channel pose signature |
| `Depth` | off | `h{i}_z` and the per-finger axis channels |
| `Tilt` | off | `h{i}_tilt` and `h{i}_tilt_axis` |
| `Temporal` | off | master switch: off freezes everything with memory |
| `Latches` | off | master switch: off freezes the gesture latches |

### What reaches the output

| toggle | default | effect |
|---|---|---|
| `Hand Box and Size` (`Handbox`) | on | off drops `h?_bbox_*` and `h?_size` |
| `Finger Tips Only` (`Fingertipsonly`) | on | drops the 15 non-tip joints, leaving the wrist and the five tips — six points per hand instead of 21 |
| `Face Key Points` (`Facekeypoints`) | on | publishes the four key points instead of the 76 landmarks. **Saves 1.18 ms** |
| `One Face Only` (`Onefaceonly`) | on | drops every `f1_*` channel, keeping the leftmost face. **Saves 0.39 ms** |
| `Screen Space Only` (`Screenspaceonly`) | on | drops every raw normalised channel that has a `_tx`/`_px` companion |
| `Delete Empty Channels` (`Deleteempty`) | on | keeps only the channels actually being computed |

`Screen Space Only` removes the second copy of a number, never the only copy: it keeps
every boolean, pulse, counter, confidence and angle, the unit vectors `h{i}_point_x/y`
and `h{i}_dir_x/y`, and the hand-local descriptor channels.

---

### Tuning

Under `Detection`, `Contact thresholds`, `Pose and motion` and `Depth and tilt` on the
same page.

| parameter | default | what it does |
|---|---|---|
| `Confthreshold` | 0.30 | minimum `conf_median` for a hand to count as active |
| `Pinchon` | 0.35 | index-thumb distance, over hand size, below which pinch engages |
| `Pinchoff` | 0.50 | and above which it releases. Must exceed `Pinchon` |
| `Snapon` | 0.25 | middle-thumb distance below which `snapping` engages |
| `Snapoff` | 0.45 | and above which it re-arms |
| `Togetheron` | 0.60 | palm distance below which `hands_together` engages |
| `Togetheroff` | 0.95 | and above which it re-arms |
| `Triggeron` | **0.13** | thumb-to-fingertip distance below which any trigger engages |
| `Triggeroff` | **0.17** | and above which it re-arms. One pair for all eight |
| `Grabon` | 0.30 | `openness` below which `e_grab` fires |
| `Graboff` | 0.55 | and above which `e_release` fires |
| `Sizefloor` | 0.01 | minimum divisor for `h{i}_size` |
| `Curlmin` | 0.35 | tip-to-mcp over finger length that maps to curl 1.0 |
| `Curlmax` | 0.95 | and to curl 0.0 |
| `Extendedbelow` | 0.30 | curl below this counts as extended |
| `Curledabove` | 0.60 | curl above this counts as curled |
| `Spreadmin`, `Spreadmax` | 0.4, 1.6 | normalise `spread` to 0..1 |
| `Overlapthreshold` | 0.15 | bbox intersection fraction that counts as overlapping |
| `Zreference` | 0.12 | hand size at which `h{i}_z` reads 1.0 |
| `Palmarea` | 0.3059 | the zero point of `h{i}_tilt` |

Every `off` threshold is looser than its `on` threshold. Setting them equal removes the
re-arm rule and the gesture will chatter.

`Triggeron` / `Triggeroff` are the only pair MEASURED on a real hand. Every other
threshold here is a guess from synthetic geometry, which validates arithmetic and says
nothing about how a hand sits.

## General page

### Capture, coordinate spaces and output

On `General`, above the smoothing controls.

| parameter | default | what it does |
|---|---|---|
| `Install` | pulse | install the sidecar's Python, packages and depth model |
| `Installstate` | read-only | what is installed against what this build wants |
| `Active` | off | is the capture process running |
| `Restartcapture` | pulse | stop and start in one press |
| `Capturestate` | read-only | `Running` / `Stopped` / `Requires Restart` |
| `Camera` | `(default)` | a menu of the real devices |
| `Listcameras` | pulse | re-enumerate and repopulate the menu |
| `Streamhands` | on | hand pose |
| `Streampose` | off | body pose |
| `Streamface` | off | face landmarks |
| `Streamsegment` | off | person segmentation, into `outmask` |
| `Segquality` | fast | the mask's quality, size and cost |
| `Streamdepth` | off | depth, into `outdepth`. **23 ms a frame** |
| `Resw`, `Resh` | 1280x720 | SOURCE resolution. Drives `_px`/`_py` |
| `Renderw`, `Renderh` | from `render1` | RENDER resolution. Drives `_ty` |
| `Orthowidth` | from `cam1` | match your ortho camera's Ortho Width |

The stream toggles are launch flags: they reach the sidecar only on a restart, though
they stop the stream cooking at once.

Camera enumeration opens no device and raises no permission prompt, so `Listcameras` is
safe to press at any time.

### The status light

| state | when |
|---|---|
| **Running** | a sidecar is running AND its launch flags still match the panel |
| **Stopped** | no matching process |
| **Requires Restart** | a process is running, but a launch flag has changed since it started |

It updates on changes and button presses, not on a timer, so a process that dies on its
own reads `Running` until something asks. `sc_uptime_s` freezing is what catches that.
`Active` is a command, not a reading.

**A project saved with `Active` on starts capturing when it is opened**, and so does a
`.tox` dropped into a running project. The camera opens without anyone pressing
anything, which is the point - but it is worth knowing before you save.

---

### Smoothing

| parameter | default | what it does |
|---|---|---|
| `Smoothing` | on | the one-euro position filter. Off is a bit-exact passthrough |
| `Mincutoff` | 1.5 Hz | how heavily a SLOW-moving hand is smoothed |
| `Beta` | 2.0 | how far a FAST-moving hand is allowed through |

All 84 landmark positions pass through the filter before anything reads them, so every
distance, angle, curl, velocity and bounding box inherits the smoothing from one place.
Confidences, `found`, `seq` and `age_ms` pass through untouched.

The cutoff rises with speed, so a resting hand is stilled and a fast one is not lagged.
**Do not copy the published `beta` of 0.007** — that is for pixel coordinates, and these
channels are normalised 0..1, about a thousand times smaller.

## Advanced page

| parameter | default | what it does |
|---|---|---|
| `Screenspaceonly` | on | drops every raw normalised channel that has a `_tx`/`_px` companion |
| `Deleteempty` | on | keeps only the channels actually being computed |
| `Slotassign` | on | h0 is the right hand, h1 the left |
| `Activateframes` | 3 | consecutive good frames before `active` turns on |
| `Deactivateframes` | 6 | consecutive bad frames before it turns off |
| `Velocityfilter` | 0.15 s | window a velocity is averaged over |
| `Speedfloor` | 0.05 /s | below this, `dir` holds and `h{i}_moving` reads 0 |
| `Oscport` | 10000 | BASE port; the streams take base + 0/1/2 |
| `Printstatus` | pulse | is a capture process running, and its pid |
| `Capturepid` | read-only | what was last started. Not proof it is still alive |
| `Maskbuffer`, `Depthbuffer` | `/tmp/appletd_*.buf` | shared files; must match on both sides |
| `Installroot` | — | where the sidecar's Python and packages go |
| `Sidecarpython` | — | interpreter override; empty means the installed one |
| `Pythonurl` | python-build-standalone | the interpreter the installer fetches |
| `Sourceversion` | read-only | the build this component was made from |
| `Forceinstall` | pulse | reinstall regardless of the stamp |
| `Keeplayout` | off | builders leave existing nodes where you put them |

Changing `Oscport` takes effect on this side immediately and on the sidecar only after a
restart. `sc_uptime_s` freezing is the symptom of that gap.

---

## About page

| parameter | default | what it does |
|---|---|---|
| `Repository` | the project URL | read-only |
| `Openrepository` | pulse | open it in a browser |
| `Updateurl` | `.../raw/main/appletd.tox` | what `Update` fetches. Editable, so a fork or a branch works |
| `Checkupdate` | pulse | ask the server whether the file has changed |
| `Updatestate` | read-only | the result of the last check or update |
| `Applyupdate` | pulse | download and replace this component |
| `Updateetag` | — | what this component was last updated from. Clear it to force the next check to report an update |
| `Updatefound` | read-only | what the server last reported |
| `Openlicence` | pulse | open the LICENSE |

`Update` replaces the COMPONENT; `Install` on the Vision page writes the SIDECAR's files.
An update restores every parameter by name and lands with `Active` off.

---

## Segmentation Mask page

`outmask` is a TOP, not channels.

| parameter | default | what it does |
|---|---|---|
| `Maskfit` | on | undo the anisotropic stretch, so the mask lines up with the camera frame |
| `Masksourcew`, `Masksourceh` | 1280x720 | read-only, from the buffer header |

`Segquality` sits with the stream toggle and is a launch flag:

| level | per frame | mask | edge |
|---|---|---|---|
| `fast` | **2.21 ms** | 256 x 192 | HARD — only 0 and 255 |
| `balanced` | 8.54 ms | 512 x 384 | feathered |
| `accurate` | 30.73 ms | 2016 x 1512 | feathered; cannot hold 30 fps |

`fast` is not simply a smaller `balanced`: it has no soft edge at all, so a feathered
matte costs 8.54 ms.

Vision's mask does not share its input's aspect ratio — a 1280x720 frame gives a 256x192
mask — so `Maskfit` applies the inverse stretch on the GPU. Turn it off to work at the
mask's native resolution.

**If the mask is black**, check that `op('/project1/appletd/seg_mask').totalCooks` is
climbing. A `seg_mask` that is 16x16 has never read the buffer.

---

## Depth page

`outdepth` is a TOP. `docs/DEPTH.md` is the full guide.

| parameter | default | what it does |
|---|---|---|
| `Depthpinson` | on | apply the fit to the pixels. Off = the model's own numbers |
| `Depthwindownear`, `Depthwindowfar` | 0.4, 3.0 | the metre window the corrected map is drawn on |
| `Depthunits` | read-only | which of the two you are looking at |
| `Depthpincount` | 3 | how many pin rows are in use, 0 to 8 |
| `Depthpin1x` … `Depthpin8m` | the example's | the pins: a position and its true distance |
| `Depthpinsdraw` | off | draw the pins in red. Output becomes RGB |
| `Depthfit` | on | stretch the map back to the camera's aspect |
| `Depthsourcew`, `Depthsourceh` | 1280x720 | read-only, from the buffer header |
| `Depthfitalpha`, `Depthfitbeta` | read-only | this frame's fit: `Z = 1/(alpha*d + beta)` |
| `Depthfitpins` | read-only | how many pins the fit used |
| `Depthfitresidual` | read-only | worst disagreement, in metres |
| `Depthfitchecked` | read-only | **1 only when the residual means anything** |

- **It costs 23 ms a frame**, against a 33.3 ms frame interval. With it on the camera
  drops buffers; depth and hands are not both available at 30 fps.
- **The map is INVERSE depth and bigger means NEARER**, and it is not comparable between
  frames — the model divides by each frame's own maximum, so a hand coming toward the
  lens darkens everything else. Build nothing on the raw numbers without pins.
- **`Depthfitresidual` lies when `Depthfitchecked` is 0.** Two pins always fit a line
  exactly, so the residual reads 0.000 and checks nothing. Use three.
- **Moving a pin needs a restart**; the drawing updates at once.
- **Converting to metres is yours to do**: `Z = 1/(alpha*d + beta)`.
- **The model is not in this repository.** 47 MB: `./tools/fetch_models.sh`, once.

---

## Caveats

**`h0` is the right hand only while `Slotassign` is on.** With it off, `h0` can swap to
the other physical hand between frames, and anything with memory — velocity, `held`,
every latch and counter — then measures a hand that changed identity. With it on, a
single LEFT hand puts nothing in `h0`: every `h0_*` channel reads zero and `h1_*` carries
the hand. Symmetric two-hand channels are unaffected either way.

**A disabled stream keeps its channels, reading zero.** What a stream toggle saves is
inference, not channels.

**Angles wrap.** Anywhere you differentiate or filter one yourself, unwrap it first, or
filter sin/cos and recombine.

**Per-joint jitter is unmeasured.** It is what should really set `Speedfloor` and `Beta`.
