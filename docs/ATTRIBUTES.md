# Derived attributes — the definitive list

What the `appletd` COMP computes from the 137 channels the sidecar sends. Every formula
here is the definition of that channel. A channel whose formula is not written down here
does not exist.

Conventions:

- `h{i}` is `h0` or `h1`; everything per-hand exists twice.
- Positions are the normalised, bottom-left-origin `_x`/`_y` channels (`DESIGN.md` 7).
  Nothing here flips y.
- Every distance is divided by `h{i}_size`.
- Angles are degrees, counter-clockwise, 0 = +x.
- A *state* is 0 or 1 and holds. A *pulse* is 1 for exactly one frame.

---

## 1. The proximity mechanism

Pinch, snap, clap, the finger triggers and grab are one mechanism: a distance `d` and
two thresholds, `on < off`.

    d < on   -> state true
    d > off  -> state false
    between  -> state holds

    start pulse = rising edge      end pulse = falling edge

The dead band is both the debounce and the re-arm rule: a start cannot fire again until
the points separate past `off`. No timer, nothing tuned against frame rate. Motion
events (swipe, wave) use a `Refractory` timer instead, having no released state.

Three gate terms, all required:

    state = h{i}_active AND live AND ( below_on OR (prev AND NOT above_off) )
    size  = max(raw_size, Sizefloor)

- `h{i}_active` — an absent hand publishes every joint at (0, 0), so every distance is 0
  and below every `on` threshold.
- `live` — a dead sidecar leaves channels **frozen, not zeroed** (`DESIGN.md` 2.9), so a
  latch would stay engaged indefinitely. Derived from the slope of `seq`; that detector
  needs its own clock (`DESIGN.md` 2.10).
- `Sizefloor` — guards 0/0 when a hand is absent or degenerate.

The gate is on the STATE, not only the pulses: gating pulses alone leaves the latch
engaged while the hand is gone, then emits a spurious `end` on return. Channels for an
inactive hand are forced to 0, not left stale.

Known shortfall: the gate uses raw per-frame validity rather than the debounced
`active`, so one low-confidence frame mid-gesture drops the state and fires a spurious
end pulse. `Activateframes`/`Deactivateframes` exist for this.

The state is level-driven rather than an accumulated parity, so a missed edge, a double
crossing or an odd startup state corrects itself on the next unambiguous frame. Counters
are separate channels.

Pulse timing is accurate to ±1 frame (33 ms at 30 fps).

## 2. Why distances are normalised by hand size

A raw normalised distance shrinks with camera distance, so a threshold tuned at arm's
length fires early when you lean in. Dividing by `h{i}_size` — the wrist-to-middle-MCP
distance — makes every distance a ratio of hand geometry, invariant to depth.

The same number is the only depth signal available from 2D landmarks: apparent size goes
as 1/distance. `h{i}_size` is both the normaliser and the z proxy.

---

## 3. Channel groups

### Core — 20 channels

Computed whether or not published; everything else depends on them.

| channel | definition |
|---|---|
| `h{i}_palm_x`, `h{i}_palm_y` | mean of wrist and the five MCP joints |
| `h{i}_size` | distance(wrist, middle_mcp) |
| `h{i}_bbox_x1/_y1/_x2/_y2` | min/max over all 21 joints |
| `h{i}_bbox_w/_h/_area` | derived from the above |

### Presence — 10 channels

| channel | definition |
|---|---|
| `h{i}_active` | `found AND conf_median > Confthreshold AND live`, held `Activateframes` before on, `Deactivateframes` before off |
| `h{i}_entering`, `h{i}_exiting` | pulses on the edges of `_active` |
| `h{i}_held` | seconds `_active` has been continuously true |
| `both_active`, `n_active` | `h0_active AND h1_active`; sum |

### Contacts — 28 channels

Edge pulses live in Events.

| channel | definition |
|---|---|
| `h{i}_pinch_index` | distance(index_tip, thumb_tip) / size |
| `h{i}_pinch_middle/_ring/_little` | same for the other fingertips |
| `h{i}_pinch_angle` | angle of thumb_tip→index_tip |
| `h{i}_pinch_x`, `h{i}_pinch_y` | midpoint of thumb_tip and index_tip |
| `h{i}_pinching` | state on `pinch_index`, `Pinchon` / `Pinchoff` |
| `h{i}_snapping` | state on `pinch_middle`, `Snapon` / `Snapoff` |
| `h{i}_pinch_count`, `h{i}_snap_count`, `clap_count` | times fired |
| `h{i}_pinch_end_count`, `h{i}_snap_end_count`, `apart_count` | times released |

### Finger triggers — 40 channels

Thumb-to-fingertip contact for all four long fingers on both hands, sharing one
`Triggeron`/`Triggeroff` pair.

| channel | definition |
|---|---|
| `h{i}_{finger}_trigger` | state on `pinch_{finger}`, `Triggeron` / `Triggeroff` |
| `h{i}_e_{finger}_trigger`, `h{i}_e_{finger}_release` | edge pulses |
| `h{i}_{finger}_trigger_count`, `h{i}_{finger}_release_count` | counters |

`{finger}` is `index`, `middle`, `ring`, `little`. No thumb trigger: every distance is
measured *to* the thumb tip, so the thumb cannot contact itself.

The triggers deliberately overlap `pinching` and `snapping` — the named gestures are
tuned by feel with their own thresholds, the triggers are a uniform grid. Either retunes
without disturbing the other.

Standing invariant, at every instant:

    fires - releases == 1 if engaged, else 0

Counters count every FRAME the start pulse is high, not every transition. Both give the
same answer when correct, so a pulse that ever lingered two frames shows as a doubled
count rather than passing silently.

### Pose — 22 channels

| channel | definition |
|---|---|
| `h{i}_curl_thumb/_index/_middle/_ring/_little` | 1 − (distance(mcp, tip) / summed bone length). 0 straight, 1 curled |
| `h{i}_openness` | mean of the four finger curls, inverted |
| `h{i}_spread` | distance(index_tip, little_tip) / size |
| `h{i}_rotation` | angle of wrist→middle_mcp. In-plane roll |
| `h{i}_point_x`, `h{i}_point_y` | unit vector index_mcp→index_tip |
| `h{i}_point_angle` | its angle |

### Motion — 20 channels

Temporal; depends on regular frame delivery.

| channel | definition |
|---|---|
| `h{i}_vel_x`, `h{i}_vel_y` | d/dt of palm position, units/s, averaged over `Velocityfilter` |
| `h{i}_speed` | magnitude |
| `h{i}_dir` | heading in degrees, HELD while speed < `Speedfloor` |
| `h{i}_dir_x`, `h{i}_dir_y` | the same heading as a unit vector, held with it |
| `h{i}_moving` | 1 when speed > `Speedfloor` |
| `h{i}_accel` | d/dt of speed |
| `h{i}_steadiness` | 1 − normalised variance of palm position over `Steadywindow`. **NOT BUILT** |
| `h{i}_dwell` | seconds the palm has stayed within `Dwellradius`. **NOT BUILT** |

All three heading channels hold together. Holding only the angle left the unit vector
following live velocity, so a stationary hand reported `dir` = 180 while `dir_x` read +1.

`dir` uses `atan2`, which no CHOP provides, so it is computed in `appletd/motion.py`.
The holding is native.

### Two hands — 14 channels

Symmetric attributes only; see §9.

| channel | definition |
|---|---|
| `hands_distance` | distance(h0_palm, h1_palm) / mean size |
| `hands_together` | state on `hands_distance`, `Togetheron` / `Togetheroff` |
| `hands_scale` | `hands_distance` relative to its value when both hands became active; starts at 1.0. **NOT BUILT** — needs a Hold latched by `Scalereset` |
| `hands_center_x`, `hands_center_y` | midpoint of the two palms |
| `hands_angle` | angle of h0_palm→h1_palm |
| `hands_twist` | d/dt of `hands_angle`, unwrapped. **NOT BUILT** — the derivative of an angle wraps at ±180 and spikes if differentiated naively; publish sin/cos of `hands_angle` and use `cos*d(sin) − sin*d(cos)` |
| `hands_approach` | d/dt of `hands_distance`. Negative closing |
| `index_distance`, `index_center_x`, `index_center_y` | as above, for the index tips |
| `both_pinching` | `h0_pinching AND h1_pinching` |
| `hands_overlap` | bbox intersection area / smaller box area |
| `hands_symmetry` | 1 − abs(h0_openness − h1_openness) |

### Gestures — 18 channels

Held states, each debounced by `Gestureframes`.

| channel | condition |
|---|---|
| `h{i}_g_fist` | all four fingers curled, thumb curled |
| `h{i}_g_open` | all five extended, spread above a floor |
| `h{i}_g_point` | index extended, middle/ring/little curled |
| `h{i}_g_peace` | index and middle extended and spread, ring/little curled |
| `h{i}_g_ok` | `pinch_index` closed AND middle/ring/little extended |
| `h{i}_g_thumbsup`, `h{i}_g_thumbsdown` | thumb extended, others curled, thumb up/down relative to palm |
| `h{i}_g_horns` | index and little extended, middle and ring curled |
| `h{i}_g_gun` | index and thumb extended, others curled |

### Events

Every pulse is 1 for exactly one frame. Built in `tools/td_add_latches.py` as Feedback +
Logic.

| channel | fires when |
|---|---|
| `h{i}_e_pinch_start`, `h{i}_e_pinch_end` | `pinching` rises / falls |
| `h{i}_e_snap`, `h{i}_e_snap_end` | `snapping` rises / falls |
| `e_clap`, `e_apart` | `hands_together` rises / falls |
| `h{i}_e_grab`, `h{i}_e_release` | `openness` crosses `Grabon` / `Graboff`. Also publishes `h{i}_grabbing` and two counters |
| `h{i}_e_{finger}_trigger`, `h{i}_e_{finger}_release` | the trigger grid, above |
| `h{i}_entering`, `h{i}_exiting` | edges of `_active` (Presence) |

Snap and clap need no audio. Audio is the better sensor only for *attack timing* inside
10 ms; for event triggering the vision path is sufficient.

#### Specified, not built

`tools/td_add_temporal.py` prints these as NOT YET BUILT on every run. They are a
specification, not a contract — nothing publishes them today.

| channel | intended definition | what it needs |
|---|---|---|
| `h{i}_e_pinch_click` | a start and end inside `Clickms` | a timed latch |
| `h{i}_e_pinch_double` | two clicks inside `Doublems` | the above, plus a second timer |
| `h{i}_e_swipe_left/_right/_up/_down` | speed above `Swipespeed`, direction inside `Swipesector`, then deceleration | thresholds plus a refractory Count |
| `h{i}_e_wave` | `Wavecrossings` sign changes of `vel_x` within `Wavewindow`, hand open | as above |
| `e_cross` | sign change of (h0_palm_x − h1_palm_x) | as above |
| `e_pull_apart`, `e_squeeze` | `hands_approach` beyond ±`Approachrate`, sustained two frames | as above |
| `e_twist_cw`, `e_twist_ccw` | `hands_twist` beyond ±`Twistrate` | `hands_twist` first |
| `e_frame` | both hands in L shapes with adjacent bounding boxes | — |

### Depth — 12 channels

Vision returns 21 points on a plane. These are consequences of the projection, not
measurements.

| channel | definition |
|---|---|
| `h{i}_z` | `Zreference / h{i}_size`. 1.0 at the reference distance, 2.0 at twice it |
| `h{i}_z_<finger>` | 0..1, how much the finger points along the camera axis |

- `h{i}_z` is monotonic in distance and nothing else. Not metres, and **not comparable
  between two hands** — apparent size confounds hand size with distance. `Zreference`
  defaults to 0.12; to recalibrate, hold a hand where you want z = 1 and read `h0_size`.
- `h{i}_z_<finger>` **cannot distinguish toward from away** — a finger pointing at the
  camera and one pointing away project identically. Asserted as a test. It is meaningful
  only for a straight finger (`h{i}_curl_<finger>` distinguishes those) and is UNMEASURED
  on a real hand.

### Tilt — 4 channels

| channel | definition |
|---|---|
| `h{i}_tilt` | 0..90°. 0 = palm square to camera, 90 = edge-on |
| `h{i}_tilt_axis` | degrees, which way it leans. Meaningless when `tilt` ≈ 0 |

Every other angle in the system is in-plane (rotation about camera Z). These are the
out-of-plane part.

Not pitch and yaw: recovering those needs which side of the image plane a point is on,
which the projection destroys. A palm tilted 30° toward the camera and one tilted 30°
away project identically, so they cannot be signed. Magnitude plus axis is what the
projection determines.

Method: the palm triangle (wrist, index MCP, little MCP) has an apparent area that
shrinks with the cosine of tilt, so `acos(area / Palmarea)` is the magnitude; the axis is
the longest surviving edge.

`Palmarea` is the channel's zero point. MEASURED at **0.3059** against `synth.py`
geometry, not a real hand. If a face-on hand reads non-zero `tilt`, raise it.

Confound: the triangle includes two MCP knuckles, so the area ratio moves with finger
spread — MEASURED 0.1835 at spread 0.6, 0.4282 at 1.4. Nothing corrects for it.

### Pose descriptor — 84 channels

`h{i}_d_<joint>_x`, `h{i}_d_<joint>_y`: all 21 joints per hand relative to the palm
centre, divided by size, rotated so `rotation` is zero. Position-, scale- and
rotation-invariant, for gesture matching or as model features. Hand-LOCAL, so these have
no screen-space companion.

---

## 4. Channel budget

| group | channels |
|---|---|
| `Coordstx` — `_tx`/`_ty` for all 42 joints | 84 |
| `Coordspx` — `_px`/`_py` for all 42 joints | 84 |
| `Core` | 20 |
| `Presence` | 10 |
| `Contacts` | 28 |
| `Pose` | 22 |
| `Twohands` | 14 |
| `Gestures` | 18 |
| `Descriptor` | 84 |
| `Depth` | 12 |
| `Tilt` | 4 |

Always on, with no toggle: the 137 raw landmark channels, the 40 finger triggers, the 20
motion channels and the 34 event pulses. Removing a group's channels needs an exact
channel-to-group map, which a wildcard cannot provide for these. Use `Finger Tips Only`
to cut the landmark channels, and `Temporal`/`Latches` to freeze the subsystems.

`Level of Detail` (`Verbosity`) presets: **Minimal** = `Coordstx` + `Twohands`; **Interaction** =
everything except `Coordspx`, `Descriptor`, `Depth`, `Tilt`; **Everything** = all.

The preset tuple and the shipping defaults describe the same component. A preset that
contradicts the defaults would change the panel the moment somebody pressed the setting
it already claimed to be in.

### Face-stream groups

`Coordstx`/`Coordspx` serve all three streams. The face's landmark halves are separate
because they are two orders of magnitude larger: the box is 8 channels, its landmark
points 348, MEASURED **1.08 ms per cook** for the world half alone.

---

## 5. Output path

    merge_streams -> early_trim -> coords -> screen_only -> trim_empty -> out1

- `early_trim` applies the toggles that REMOVE channels (`Finger Tips Only`, `Hand Box
  and Size`, `One Face Only`, `Face Key Points`) before `coords` converts anything.
- `coords` is one group for all three streams.
- `trim_empty` is last, because a frozen group holds its channels rather than dropping
  them.

With every stream and space enabled the merge is 2,041 channels (MEASURED 2026-08-24:
635 hands, 275 pose, 1,131 face). `Delete Empty Channels` keeps only what is switched
on. Switch it off to see everything, frozen values included.

This is the one place the component deliberately reverses `DESIGN.md` 6.2's "channels
never vanish" rule.

### Never on the output

| channels | where | why |
|---|---|---|
| the 80 per-joint `*_conf` | nowhere — use `h?_conf_median` | 80 channels nobody reads one at a time |
| `h?_score`, `h?_conf_median`, `h?_chirality` | `housekeeping` | quality and identity scalars |
| every `sc_*`, incl. `sc_src_w`/`sc_src_h` | `housekeeping` | the capture process's state, not the hands' |
| `seq`, `pose_seq`, `face_seq` | `housekeeping` | frame counters |
| `age_ms`, `pose_age_ms`, `face_age_ms` | `housekeeping` | datagram staleness |

`housekeeping` is a Null CHOP beside `out1`, costing 0.0037 ms. Gate on
`h?_conf_median`, read as
`op('/project1/appletd/housekeeping')['h0_conf_median']`.

---

## 6. Gating semantics

Three kinds of toggle:

| toggles | effect when off |
|---|---|
| `Core` `Presence` `Contacts` `Pose` `Twohands` `Gestures` `Descriptor` | `derive()` does not compute the group; channels leave the output |
| `Presence` (→`temporal`), `Gestures` (→`latches`), `Coordstx` `Coordspx` (→ `coords/world`, `coords/pixels`, `coords/dv_*` and `coords/lm_*` in all three streams) | the group COMP stops cooking via `allowCooking`; channels stay, holding their last value |
| `Temporal` `Latches` | a VETO: off freezes the group whatever the toggles above say |
| `Finger Tips Only` `Hand Box and Size` `One Face Only` | OUTPUT only; none can gate cooking |
| `Face Key Points` | a reverse veto: ON freezes `face/coords/lm_world` and `lm_pixels` |

    cooking = any(COOK_GATED) and all(COOK_VETOED)

The OR expresses "any of these keeps the group alive", which cannot express *off*; hence
the veto.

**Off freezes, it does not remove.** Turning `Coordspx` off stops `coords/pixels`
cooking; the `_px`/`_py` channels stay, holding their last value. VERIFIED: 0 cooks
across all eight operators over 403 frames of moving data. What it saves is compute,
~0.1 ms per stream, not channel count. A frozen channel often reads **0**, because a
freshly built group is frozen right after a cook with no data in it.

**`Temporal` off takes `latches` with it.** The latch bank gates on `temporal`'s presence
signal, and a frozen gate either arms every latch forever or disarms them forever with
nothing to say which. VERIFIED by cook count: `Temporal` off froze all 74 operators in
`temporal` and all 53 in `latches`.

**A memory group pauses rather than resets.** Re-enabling one resumes with a `prev` from
when it was switched off: the state is stale for one frame and one edge pulse across the
gap may be wrong. Cumulative counters are off by up to one per gap; deltas stay correct.
Self-correcting, because every recurrence is level-driven.

**Output-only is not free.** `trim_empty` is a Select whose cost scales with the keep
list: naming 1,110 channels costs 0.5312 ms, 748 costs 0.3169. `One Face Only` saves
0.39 ms without gating a cook.

**`Face Key Points` also saves cook time.** MEASURED 2026-08-24: the face stream costs
1.9644 ms per cook with it off, 0.7872 ms with it on. It costs 0.10 ms when off, which
is what composing the four points alongside the box costs.

**`filter` is not gated this way.** It sits in the data path, so freezing it would freeze
every position rather than bypass it. Its `Smoothing` toggle uses the bypass flag.

**Stream toggles gate cooking too.** `Streamhands`/`Streampose`/`Streamface` are launch
flags for the sidecar AND `allowCooking` on the stream's COMP. A disabled stream was
otherwise processing a datagram of zeros every frame — MEASURED 0.25 ms for pose, 1.45 ms
for face. The flag reaches the sidecar only on restart; the freeze is immediate.

A disabled stream keeps its channels, reading zero: the OSC In CHOP creates channels as
they arrive, so a stopped stream would make them vanish.

---

## 7. Parameters

Eight pages: Vision, Filter, Advanced, Tuning, Attributes, Segmentation, Depth, About.
Every one belongs to `/project1/appletd`. Verified against the live panel 2026-09-03 —
page, value and default.

### Page: Attributes

| parameter | type | default | effect |
|---|---|---|---|
| `Level of Detail` | menu | Minimal | sets the toggles below in one click |
| `Screen Space Coords` (`Coordstx`) | toggle | on | `_tx`/`_ty`, every stream, including the face's landmark points. **1.08 ms** |
| `Pixel Coords` (`Coordspx`) | toggle | off | `_px`/`_py`, the same. **0.90 ms** |
| `Core` | toggle | off | palm, size, bbox |
| `Presence` | toggle | off | |
| `Contacts` | toggle | off | |
| `Pose` | toggle | off | |
| `Twohands` | toggle | on | |
| `Gestures` | toggle | off | held states; also gates `latches` |
| `Descriptor` | toggle | off | 84-channel pose signature |
| `Hand Box and Size` (`Handbox`) | toggle | on | off drops `h?_bbox_*` and `h?_size` |
| `Face Key Points` (`Facekeypoints`) | toggle | on | swaps the 664 face landmark channels for 32: two pupils, a nose tip and a mouth centre per face |
| `One Face Only` (`Onefaceonly`) | toggle | on | drops every `f1_*` channel, keeping the leftmost face |
| `Temporal` | toggle | off | veto on `temporal` |
| `Latches` | toggle | off | veto on `latches` |

`Finger Tips Only` keeps the wrist: it is the hand's anchor and `hands_angle` is measured
from it.

### Page: Tuning

| parameter | type | default | effect |
|---|---|---|---|
| `Confthreshold` | 0..1 | 0.30 | minimum `conf_median` for a hand to be active |
| `Pinchon` | ratio | 0.35 | index-thumb distance below which pinch engages |
| `Pinchoff` | ratio | 0.50 | and above which it releases |
| `Snapon` | ratio | 0.25 | middle-thumb distance below which `snapping` engages |
| `Snapoff` | ratio | 0.45 | and above which it re-arms |
| `Togetheron` | ratio | 0.60 | palm distance below which `hands_together` engages |
| `Togetheroff` | ratio | 0.95 | and above which it re-arms |
| `Triggeron` | ratio | **0.13** | thumb-to-fingertip distance below which any trigger engages. MEASURED on a real hand |
| `Triggeroff` | ratio | **0.17** | and above which it re-arms |
| `Grabon` | 0..1 | 0.30 | `openness` below which `e_grab` fires. Not a ratio — `openness` is already 0..1 |
| `Graboff` | 0..1 | 0.55 | above which `e_release` fires |
| `Sizefloor` | units | 0.01 | minimum divisor for `h{i}_size` |
| `Curlmin` | ratio | 0.35 | tip-to-mcp over finger length mapping to curl 1.0 |
| `Curlmax` | ratio | 0.95 | and to curl 0.0 |
| `Extendedbelow` | 0..1 | 0.30 | curl below this counts as extended |
| `Curledabove` | 0..1 | 0.60 | curl above this counts as curled |
| `Spreadmin`, `Spreadmax` | ratio | 0.4, 1.6 | normalise `spread` to 0..1 |
| `Overlapthreshold` | 0..1 | 0.15 | bbox intersection fraction counting as overlap |
| `Zreference` | units | 0.12 | hand size at which `h{i}_z` reads 1.0 |
| `Palmarea` | units | 0.3059 | the zero point of `h{i}_tilt` |

The Tuning page is built by iterating `dataclasses.fields(Params)` in
`appletd/derive.py`, so the page and the dataclass cannot disagree and a new field
arrives on the panel without a second edit.

### Page: Filter

| parameter | type | default | effect |
|---|---|---|---|
| `Smoothing` | toggle | on | the one-euro filter. Off is a bit-exact passthrough |
| `Mincutoff` | Hz | 1.5 | how heavily a slow hand is smoothed |
| `Beta` | 1/units | 2.0 | how far a fast hand is allowed through |

`Triggeron`/`Triggeroff` are the only MEASURED pair. They were guessed at 0.40 / 0.55
from synthetic geometry and came out about three times tighter against a real hand.
Every other threshold here is a guess of the same kind: synthetic geometry validates
arithmetic and says nothing about how a hand sits.

### Page: Advanced

| parameter | type | default | effect |
|---|---|---|---|
| `Activateframes` | int | 3 | good frames before `active` turns on |
| `Deactivateframes` | int | 6 | bad frames before it turns off |
| `Velocityfilter` | s | 0.15 | window a velocity is AVERAGED over. Positions update at the camera rate and are differentiated at the cook rate, so the per-cook derivative alternates between double-size and zero; the mean is what makes the number true |
| `Speedfloor` | /s | 0.05 | below this, `dir` holds and `h{i}_moving` reads 0. GUESSED |
| `Oscport` | int | 10000 | BASE port; streams take base + 0/1/2 (`DESIGN.md` 6.4) |
| `Printstatus` | pulse | — | is a capture process running, and its pid |
| `Capturepid` | int | 0 | what was last started; not proof it is alive |
| `Maskbuffer` | str | `/tmp/appletd_mask.buf` | shared file, must match on both sides |
| `Depthbuffer` | str | `/tmp/appletd_depth.buf` | as above |
| `Installroot` | str | — | where the sidecar's Python and packages go |
| `Sidecarpython` | str | — | interpreter override; empty means the installed one |
| `Pythonurl` | str | python-build-standalone | the interpreter the installer fetches |
| `Sourceversion` | str | read-only | the build this component was made from |
| `Forceinstall` | pulse | — | reinstall regardless of the stamp |
| `Keeplayout` | toggle | off | builders leave existing nodes where you put them |

### Specified, not built

These appear in no builder and on no page. They tune the events in §3 that are likewise
unbuilt, and are listed so the specification stays complete.

| parameter | intended default | for |
|---|---|---|
| `Sizejoint` | middle_mcp | what defines `h{i}_size` |
| `Gestureframes` | 3 | frames a held gesture must persist |
| `Steadywindow`, `Steadytolerance` | 0.50 s, 0.02 | `steadiness` |
| `Dwellradius` | 0.05 | `dwell` |
| `Refractory` | 0.25 s | re-arm time for motion events |
| `Clickms`, `Doublems` | 300, 400 ms | click and double-click |
| `Swipespeed`, `Swipesector` | 1.50 /s, 45° | swipe |
| `Wavewindow`, `Wavecrossings` | 1.00 s, 3 | wave |
| `Approachrate` | 1.20 /s | `e_pull_apart` / `e_squeeze` |
| `Twistrate` | 90 deg/s | the twist events |
| `Scalereset` | pulse | sets `hands_scale` to 1.0 |

`Oscport` splits in two directions: the OSC In CHOPs bind it by expression and listen
immediately, while the running process keeps sending to the old port until restarted. The
symptom of that gap is `sc_uptime_s` freezing — the same signal as a dead process.

### Page: Vision

| parameter | default | effect |
|---|---|---|
| `Active` | off | is the capture process running |
| `Restartcapture` | pulse | stop and start in one press |
| `Capturestate` | read-only | `Running` / `Stopped` / `Requires Restart` |
| `Camera` | `(default)` | a menu of real devices; `(default)` lets the engine pick |
| `Listcameras` | pulse | re-enumerate and repopulate the menu |
| `Streamhands` | on | `VNDetectHumanHandPoseRequest` |
| `Streampose` | off | `VNDetectHumanBodyPoseRequest` |
| `Streamface` | off | `VNDetectFaceLandmarksRequest` |
| `Streamsegment` | off | `VNGeneratePersonSegmentationRequest` → `outmask` |
| `Segquality` | fast | mask quality, size and cost. Launch flag |
| `Streamdepth` | off | Depth Anything V2 → `outdepth`. **23 ms a frame** |
| `Slotassign` | on | h0 is the right hand, h1 the left (`DESIGN.md` 6.3) |
| `Resw`, `Resh` | 1280×720 | SOURCE image, for pixel space |
| `Renderw`, `Renderh` | from `render1` | RENDER resolution, for the world-space aspect |
| `Orthowidth` | from `cam1` | the camera's ortho width |
| `Screenspaceonly` | on | drop the raw normalised coords |
| `Deleteempty` | on | keep only channels actually being computed |
| `Fingertipsonly` | on | drop the 15 non-tip joints, leaving `wrist` and the five `*_tip`s |
| `Install` | pulse | install the sidecar's Python, packages and depth model |
| `Installstate` | read-only | what is installed against what this build wants |

Camera enumeration opens no device and raises no permission prompt.

Turning every stream off starts nothing, and says so in the Textport rather than
launching a process that opens the camera and runs nothing.

### The status light

| state | when |
|---|---|
| **Running** (green) | a sidecar is running AND its launch flags match the panel |
| **Stopped** (red) | no matching process, checked with `pgrep` |
| **Requires Restart** (yellow) | a process is running, but a launch flag has changed since it started |

It updates on changes and button presses, not on a timer: `pgrep` costs milliseconds,
which is nothing on a click and rude every frame. A process that dies on its own reads
`Running` until something asks; `sc_uptime_s` freezing is what catches that.

`Active` is a COMMAND, not a reading — the process can die independently. `sc_uptime_s`
is the truth, because it arrives from the process.

`Active` off reads `Stopped` immediately: `stop()` sends SIGTERM and WAITS, bounded at
1 s, because the sidecar releases the capture session before exiting.

### Page: Coords

| parameter | default | effect |
|---|---|---|
| `Resw`, `Resh` | 1280, 720 | SOURCE resolution. Drives `_px`/`_py` only |
| `Renderw`, `Renderh` | from `render1` | RENDER resolution. Drives `_ty` |
| `Orthowidth` | from `cam1` | match your ortho camera's Ortho Width |

### Screen Space Only

Removes every RAW normalised channel that has a screen-space companion — `h0_wrist_x`
goes, `h0_wrist_tx`/`_px` stay. MEASURED: hands 499 → 415, pose 275 → 199, face
1083 → 727, with the removed set equal to the delete list exactly.

It keeps every boolean, pulse, counter, confidence and **angle**. The rule is *remove the
second copy of a number, never the only copy*.

It leaves eight channels that have no companion by design: `h{i}_point_x/y` and
`h{i}_dir_x/y` are UNIT VECTORS, and world space scales y by the render's aspect, so a
transformed unit vector is neither unit length nor pointing where the image-space one
pointed. The 84 descriptor channels are hand-LOCAL and contain no image position.

Everything else derived has a companion and is removed: `h{i}_palm_x/y`,
`h{i}_pinch_x/y`, `hands_center_x/y` and `index_center_x/y` take the position rule;
`h{i}_vel_x/y` takes the extent rule — scaled into world units per second with no
centring, because a velocity of 0.2 is 0.2 wherever the hand is.

It is one Delete CHOP per stream after `merge_out`, where nothing inside the COMP reads
through it.

---

## 8. Streams

All three streams merge into one output. Channel names say which stream they came from.
Every parameter belongs to `/project1/appletd`.

```
/project1/appletd
  hands_osc 10000  ->  hands  --+
  pose_osc  10001  ->  pose   --+->  merge_streams -> ... -> trim_empty -> out1
  face_osc  10002  ->  face   --+          |
  status                          the sc_* +-> housekeeping_sel -> housekeeping

  /tmp/...mask.buf  -> seg_mask   -> seg_fit   -> outmask     TOP
  /tmp/...depth.buf -> depth_map  -> depth_fit -> outdepth    TOP
```

Three outputs of two families: `out1` is the CHOP output this document describes;
`outmask` and `outdepth` are TOPs arriving by shared memory rather than OSC.

Reference a channel as `op('/project1/appletd')['h0_index_tip_tx']`, or
`op('/project1/appletd/hands')['h0_index_tip_tx']` to reach past the trim. An operator
outside the COMP cannot wire to a nested one.

### Body pose — 275 channels

Plumbing only: no derived attributes, no filtering, no temporal channels.

```
pose_n_bodies, pose_seq, pose_age_ms
p<i>_found, p<i>_score, p<i>_conf_median              for i in 0..1
p<i>_<joint>_x, p<i>_<joint>_y, p<i>_<joint>_conf     19 joints
```

123 channels from the wire plus 152 transformed. Every joint also arrives in world and
pixel space.

Joints, in channel order: `nose` `left_eye` `right_eye` `left_ear` `right_ear` `neck`
`left_shoulder` `right_shoulder` `left_elbow` `right_elbow` `left_wrist` `right_wrist`
`root` `left_hip` `right_hip` `left_knee` `right_knee` `left_ankle` `right_ankle`.

- **`p0` is the LEFTMOST person**, by a stateless sort on the root joint (the neck if the
  root is not confident). There is no chirality for a person, so two people who cross
  over exchange slots.
- **Gate on `p<i>_conf_median`, never `p<i>_score`.** The body observation confidence is
  UNMEASURED.
- **Nothing here is depth-invariant.** No pose channel divides through by a size, so a
  raw distance between two pose joints measures how far away the person is standing.

### Face — 387 channels on the wire

```
face_n, face_seq, face_age_ms
f<i>_found, f<i>_score, f<i>_quality                  for i in 0..1
f<i>_roll, f<i>_yaw, f<i>_pitch                       DEGREES
f<i>_bbox_x, f<i>_bbox_y, f<i>_bbox_w, f<i>_bbox_h    normalised
```

The bounding box also arrives in world and pixel space, and extents follow a different
rule from positions:

    f0_bbox_tx = (bbox_x - 0.5) * Orthowidth       a position: centred, so offset
    f0_bbox_tw =  bbox_w        * Orthowidth       an EXTENT: not offset
    f0_bbox_th =  bbox_h        * Orthowidth * Renderh / Renderw
    f0_bbox_pw =  bbox_w        * Resw             pixels

A width is 0.22 wide wherever the box sits, so subtracting 0.5 would give a negative
size. 39 channels: 23 from the wire plus 16 transformed. `roll`/`yaw`/`pitch` are not
transformed — there is no yaw in pixels — and are smoothed like any continuous value.

- **`roll`, `yaw`, `pitch` are DEGREES.** Vision reports radians; the conversion happens
  once, in `appletd/face.py`. Values under 2 for a clearly turned head mean something
  bypassed it.
- **`bbox_y` is the BOTTOM edge**, the box being a CGRect with a bottom-left origin. The
  top edge is `bbox_y + bbox_h`.
- **Gate on `f<i>_quality`, not `f<i>_score`.** Both are UNMEASURED (`DESIGN.md` 2.12);
  `quality` is `faceCaptureQuality`, documented as a comparison metric for the same
  subject.
- **`f0` is the LEFTMOST face**, by bounding-box CENTRE — not the left edge, because a
  near face has a wide box and would sort left of one genuinely further left.

### The 76 landmark points

`f<i>_<region>_<nn>_x` / `_y`, over 12 regions:

```
face_contour  17   median_line   10   left_eyebrow  6   right_eyebrow  6
left_eye       6   right_eye      6   left_pupil    1   right_pupil    1
nose           8   nose_crest     6   outer_lips   14   inner_lips     6
```

**87 slots over 76 distinct points**, because regions overlap: `median_line` shares
points with `nose_crest` (4), `nose` (2), both lip rings (2 each) and `face_contour` (1);
`nose` shares one more with `nose_crest`. Eleven points are published twice, under both
region names. Summing two regions double-counts those eleven.

**These points are normalised to the FACE'S BOUNDING BOX, not the image**
(`normalizedPoints`, `DESIGN.md` 2.12), which is why
they have no `_tx`/`_px` companions — transforming them with the image rules would put
every feature in one corner of the frame. Compose through the box:

    image_x = f0_bbox_x + point_x * f0_bbox_w
    image_y = f0_bbox_y + point_y * f0_bbox_h

Written out, a landmark's world coordinate is

    _tx = point_x * (bbox_w * K) + (bbox_x - 0.5) * K

which is one Math CHOP with `gain` and `postoff` as expressions. The raw box-relative
`_x`/`_y` stay — the right space for anything about a face's SHAPE rather than position.

The bundle is 12,640 bytes, more than a default UDP socket will send; `appletd/osc.py`
raises the send buffer, and `MAX_FACES = 2` is the practical ceiling for one datagram.

### The four key points

Computed in the sidecar, published beside the regions:

```
f<i>_eye_left_x,  f<i>_eye_left_y       leftPupil, unchanged
f<i>_eye_right_x, f<i>_eye_right_y      rightPupil, unchanged
f<i>_nose_tip_x,  f<i>_nose_tip_y       the point three regions share
f<i>_mouth_x,     f<i>_mouth_y          the mean of inner_lips
```

Box-normalised like every landmark, but unlike them they DO have `_tx`/`_ty`/`_px`/`_py`
companions.

- **Two of the four are not points Vision publishes.** Every region except the pupils is
  a RING — `left_eye_00`..`_05` trace around the eye, none is its centre — so the mouth
  centre is the mean of the six `inner_lips` points and the nose tip is identified rather
  than indexed.
- **The nose tip belongs to three regions.** `appletd/face_types.nose_tip_point`
  intersects `median_line`, `nose_crest` and `nose` every frame rather than pinning an
  index, because an index measured once is unverifiable afterwards. If the intersection
  is not exactly one point the channel reads 0.
- **There are no ears.** Vision has twelve regions and none is an ear.
- **`eye_left` is `leftPupil`**, whatever side of the frame that is. Which side it
  appears on in an unmirrored image is UNMEASURED;
  `tools/probe_face_regions.py --keypoints` answers it.

`Screenspaceonly` deletes the raw `f<i>_eye_left_x` and keeps `f<i>_eye_left_tx`, like
any other channel with a companion.

### Sidecar status channels

Four channels on the HANDS port, so they arrive whatever else is enabled.

| channel | meaning |
|---|---|
| `sc_uptime_s` | seconds since the sidecar started sending. **FROZEN means the process is gone** |
| `sc_hands`, `sc_pose`, `sc_face` | 1 when the sidecar really started that stream |

`sc_uptime_s` distinguishes "sidecar dead" from "stream switched off", because a disabled
stream's own `seq` is frozen too. The `sc_*` channels report what the process is RUNNING,
not what the toggles say.

---

## 9. Smoothing

The 84 landmark positions pass through a **one-euro filter** before anything reads them,
so every distance, angle, curl, velocity and bounding box inherits it from one place.
Anything that is not a position — confidences, `found`, `seq`, `age_ms` — passes through
untouched.

TouchDesigner's own Filter CHOP, `type = oneeuro`: one operator, 0.015 ms.

    dx      = (x - x_prev) * rate
    dx_hat  = exp_smooth(dx, alpha(1.0 Hz, fixed))
    cutoff  = Mincutoff + Beta * |dx_hat|
    x_hat   = x_hat_prev + alpha(cutoff) * (x - x_hat_prev)

Adaptive rather than fixed, because jitter and hand motion occupy the same frequency
band: a fixed cutoff must choose between stilling a resting hand and tracking a fast one.
MEASURED: at rest the cutoff sits at `Mincutoff` 1.5 Hz with alpha 0.136; a hand crossing
the frame in 0.8 s lifts it to 4.35 Hz with alpha 0.313 — **2.3× more responsive on a
real gesture than at rest**.

**The published `beta` of 0.007 is wrong for these channels.** Every one-euro reference
filters PIXELS, where speeds reach hundreds of units per second. These channels are
normalised 0..1, so the same motion measures about a thousand times smaller and
`1.5 + 0.007 * 1.4` is no adaptation at all. The default here is scaled for normalised
units, and is a guess.

`dx_hat` is a smoothed *signed* velocity, so an oscillating fingertip largely cancels
itself out — MEASURED, a fingertip pinching on 0.6 s cycles peaked at 0.037 units/s and
the filter barely opened. Sustained motion in one direction drives the adaptation.

---

## 10. Caveats

**Per-hand identity depends on `Slotassign`.** With it on, `h0` is the right hand and
`h1` the left every frame. With it off, `h0` can swap physical hands between frames and
anything with memory measures a hand that changed identity: velocity, `held`, `dwell`,
every latch and counter. MEASURED (`DESIGN.md` 6.3): with assignment off and two hands
crossing, `h0_size` reads the MEAN of both hands' sizes.

Consequence of it being on: a single LEFT hand puts nothing in `h0`. Symmetric two-hand
attributes are unaffected either way.

**Angles wrap.** A hand crossing ±180° makes a naively smoothed `hands_angle` swing
through zero, reading as a violent twist. Anywhere an angle is differentiated or
filtered, it is unwrapped first, or sin/cos are filtered and recombined.

**Per-joint jitter is unmeasured** (`DESIGN.md` 11). It determines how much smoothing the
temporal groups need and should set `Speedfloor` and `Beta`. Measurable from the fixture
with nobody present.

---

## 11. Implementation

**Stateless maths in one Script CHOP; everything temporal in native CHOPs.**

Every distance, angle, curl, palm centre, bounding box, openness and two-hand geometry is
a pure function of one frame's 137 channels. The Script CHOP's cook is a thin wrapper
around a pure function:

    derive(values: dict[str, float], params, groups) -> dict[str, float]

which needs no TouchDesigner. Feed it a synthetic fist and assert `h0_openness == 0`;
every formula above becomes an assertion, checked with no camera.

Everything with memory stays native: TouchDesigner does it properly, it is inspectable in
the network, and it keeps no Python state for a project reload to mishandle. A prototype
holding recurrences in a Python object made the project fail to SAVE — TouchDesigner
pickles operator storage into the `.toe`, and a class from a re-imported module fails
pickle's identity check (`DESIGN.md` 2.11).

| need | operator |
|---|---|
| velocity, acceleration | Slope |
| smoothing | Filter, `type = oneeuro` |
| Schmitt latches and edge pulses | Feedback + Logic |
| debounce | Count or Trail + Logic |
| refractory windows, dwell | Timer, Count |
| steadiness | Trail + Analyze |
| group gating | `allowCooking` per group COMP, plus a Select on the output |

The Script CHOP runs main-thread Python, which was never the problem — the 28× starvation
in `DESIGN.md` 2.8 was a BACKGROUND thread. Comparable work measured 0.141 ms for 137
channels. `derive()` takes the set of enabled groups, so a disabled group costs nothing
inside the Script CHOP as well as outside it.

### Page: About

| parameter | default | effect |
|---|---|---|
| `Repository` | the project URL | read-only |
| `Openrepository` | pulse | open it in a browser |
| `Updateurl` | `.../raw/main/appletd.tox` | what `Update` fetches. Editable, so a fork or branch works. Reset to the default on every rebuild |
| `Checkupdate` | pulse | HEAD the URL and compare its ETag with `Updateetag` |
| `Updatestate` | read-only | the result of the last check or update |
| `Applyupdate` | pulse | download and replace this component |
| `Updateetag` | — | the ETag this component was last updated FROM. Clear it to force the next check to report an update |
| `Updatefound` | read-only | the ETag the server last reported |
| `Notice1..3` | headers | the disclaimer |
| `Openlicence` | pulse | open the LICENSE of whatever `Repository` points at |

`Update` replaces the COMPONENT; `Install` on the Vision page writes the SIDECAR's
files. An update restores every parameter by name, with its mode, and lands with
`Active` off and the status light recomputed.

---

## 12. The segmentation mask — a TOP

`Streamsegment` puts `segment` on the sidecar's command line AND gates the Script TOP
that reads the buffer; switching it off releases the mapping. `Segquality` is a launch
flag.

| level | per frame | mask | edge |
|---|---|---|---|
| `fast` | **2.21 ms** | 256 × 192 | HARD — only 0 and 255 |
| `balanced` | 8.54 ms | 512 × 384 | feathered |
| `accurate` | 30.73 ms | 2016 × 1512 | feathered; cannot hold 30 fps |

All of it lands on the same serial queue as hands' 3.41 ms, which is why the default is
`fast` rather than Vision's own `accurate`. `fast` is not a smaller `balanced`: it has no
soft edge at all.

### Page: Segmentation

| parameter | default | effect |
|---|---|---|
| `Maskfit` | on | undo the anisotropic stretch so the mask lines up with the camera frame |
| `Masksourcew`, `Masksourceh` | 1280 × 720 | READ-ONLY, read from the buffer header |

`Depthsourcew`/`Depthsourceh` are the same thing on the Depth page.

Vision's mask does not share its input's aspect ratio: a 1280×720 frame gives a 256×192
mask, the shape chosen by orientation alone — landscape 4:3, portrait 3:4 — and the image
is stretched anisotropically with no letterboxing. `Maskfit` applies the inverse on the
GPU. Turn it off for the native resolution.

Cost: ~0.12 ms (`seg_mask` 0.0915, `seg_fit` 0.0312). A read during a write holds the
previous frame rather than blocking or blanking (`DESIGN.md` 2.19, 2.20).

**If the mask is black**, check `op('/project1/appletd/seg_mask').totalCooks` is
CLIMBING. An input-less Script TOP is never dirtied on its own, so it is driven by a
`Tick` expression gated on `Streamsegment`. A `seg_mask` that is 16×16 has never read the
buffer; a real mask is 256×192 or larger.

To see the mask with no camera:

    ~/.venvs/appletd/bin/python tools/segmentation_probe.py --write-only

---

## 13. The depth map — a TOP

`docs/DEPTH.md` is the documentation: first run, model download, pins, caveats. This is
the parameter table.

| page | parameter | default | effect |
|---|---|---|---|
| Vision | `Streamdepth` | off | run the model and read its map |
| Advanced | `Depthbuffer` | `/tmp/appletd_depth.buf` | shared file, must match both sides |
| Depth | `Depthpinson` | on | apply the fit to the pixels. Off = the model's own numbers |
| Depth | `Depthwindownear`, `Depthwindowfar` | 0.4, 3.0 | the fixed metre window the corrected map is drawn on |
| Depth | `Depthunits` | read-only | which of the two you are looking at |
| Depth | `Depthpincount` | 3 | pin rows in use, 0 to 8 |
| Depth | `Depthpin1x` … `Depthpin8m` | the example's | the pins |
| Depth | `Depthpinsdraw` | off | draw the pins in red. Output becomes RGB |
| Depth | `Depthfit` | on | stretch the map back to the camera's aspect |
| Depth | `Depthfitalpha`, `Depthfitbeta` | read-only | this frame's fit: `Z = 1/(alpha*d + beta)` |
| Depth | `Depthfitpins` | read-only | pins the fit used |
| Depth | `Depthfitresidual` | read-only | worst disagreement, in metres |
| Depth | `Depthfitchecked` | read-only | **1 only when the residual means anything** |

- **It costs 23 ms a frame** — MEASURED, against hands' 3.41 and a 33.3 ms frame
  interval. It runs last on the capture queue, and with it on the camera drops buffers.
  Depth at 30 fps and hands at 30 fps are not both available.
- **The map is INVERSE depth and bigger means NEARER**, and it is not comparable between
  frames: the model divides by the frame's own maximum, so a hand approaching the lens
  darkens everything else. Build nothing on the raw numbers without pins.
- **`Depthfitresidual` lies when `Depthfitchecked` is 0.** Two pins always fit a line
  exactly, so the residual is 0.000 and checks nothing. Gate on `Depthfitchecked` first.
  Use three pins.
- **Moving a pin needs a restart** (the solve is a launch flag); the drawing updates at
  once, which is the discrepancy to watch for.
- **Converting to metres is yours to do**: `Z = 1/(alpha*d + beta)` in a GLSL TOP or an
  expression. Doing it here would mean choosing a clamp and a colour window for every
  project whether it wants metres or not.
- **The model is not in this repository.** 47 MB: `./tools/fetch_models.sh`, once.
