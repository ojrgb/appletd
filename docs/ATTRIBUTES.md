# Derived attributes — the definitive list

What the `visionhands` COMP computes from the 137 channels the sidecar sends.
This is the spec the implementation follows; every formula here is the definition
of that channel, and a channel whose formula is not written down here does not
exist.

Conventions used throughout:

- `h{i}` is `h0` or `h1`. Everything per-hand exists twice.
- Positions are the normalised, bottom-left-origin `_x`/`_y` channels from the
  sidecar (`DESIGN.md` 7). Nothing here flips y.
- **Every distance is divided by `h{i}_size`.** This is the most important
  decision in the document — see below.
- Angles are degrees, counter-clockwise, 0 = +x.
- A *state* is 0 or 1 and holds. A *pulse* is 1 for exactly one frame.

## One pattern for every proximity gesture

Pinch, snap and clap are the same thing measured between different pairs of
points, and they all use one mechanism:

    a distance d, and two thresholds, on < off

        d < on   ->  state becomes true
        d > off  ->  state becomes false
        between  ->  state holds whatever it was

    start pulse = rising edge of state       end pulse = falling edge

That is a Schmitt trigger, and the gap between `on` and `off` does two jobs at
once. It stops the state chattering when a distance sits near the threshold, and
it **is** the re-arm rule: the start pulse cannot fire again until the points have
separated past `off`, which is how we know the gesture ended rather than jittered.
No timer is involved, and nothing has to be tuned against frame rate.

This replaces the refractory windows an earlier draft specified for these
gestures. Refractory timers are still used for the motion gestures - swipe, wave -
because a swipe has no "released" state to wait for.

**A note on what changed and why.** An earlier version of this document argued
that snap and clap were unreliable at 30 fps, on Nyquist grounds: a snap's
audible event lasts about 60 ms, which is two frames. That reasoning was sound
and answered the wrong question. Detecting the *instant of contact* needs that
resolution; detecting *that contact happened* does not, because the fingers stay
together for many frames afterwards. Defined as a proximity crossing, both
gestures are as reliable as pinch, which is to say completely.

What remains true, and is the only residual caveat: the pulse lands on the frame
where the crossing is first observed, so its timing is accurate to +/- one frame
(33 ms). That is irrelevant for triggering an event and would matter for
sample-accurate audio, which is not what this is for.

## Why every distance is normalised by hand size

A raw distance in normalised screen coordinates shrinks as the hand moves away
from the camera. A pinch threshold tuned at arm's length fires early when you
lean in and never fires when you sit back. Dividing by `h{i}_size` — the
wrist-to-middle-MCP distance — makes every distance a ratio of hand geometry,
which is invariant to depth.

The same number is also the only depth signal available from 2D landmarks:
apparent size is proportional to 1/distance. So `h{i}_size` is both the
normaliser and the z-axis.

---

## Group: Core — always computed

Everything else depends on these, so they are computed whether or not they are
published.

| channel | definition |
|---|---|
| `h{i}_palm_x`, `h{i}_palm_y` | mean of wrist and the five MCP joints. Steadier than the wrist alone, which pivots as the hand rotates |
| `h{i}_size` | distance(wrist, middle_mcp). The normaliser and the depth proxy |
| `h{i}_bbox_x1/_y1/_x2/_y2` | min/max over all 21 joints |
| `h{i}_bbox_w/_h/_area` | derived from the above |

20 channels.

## Group: Presence

| channel | definition |
|---|---|
| `h{i}_active` | `found AND conf_median > Confthreshold`, held for `Debounceframes` before flipping. Debounced because a single low-confidence frame should not drop an interaction |
| `h{i}_entering`, `h{i}_exiting` | pulses on the edges of `_active` |
| `h{i}_held` | seconds `_active` has been continuously true |
| `both_active`, `n_active` | `h0_active AND h1_active`; sum |

10 channels.

## Group: Contacts

Finger-to-thumb and hand-to-hand proximity. Named Contacts rather than Pinch
because pinch, snap and the two-hand "together" state are one family.

| channel | definition |
|---|---|
| `h{i}_pinch_index` | distance(index_tip, thumb_tip) / size |
| `h{i}_pinch_middle/_ring/_little` | same for the other fingertips |
| `h{i}_pinch_angle` | angle of the thumb_tip->index_tip vector |
| `h{i}_pinch_x`, `h{i}_pinch_y` | midpoint of thumb_tip and index_tip - the grab point, more stable than either tip |
| `h{i}_pinching` | state on `pinch_index`, thresholds `Pinchon` / `Pinchoff` |
| `h{i}_snapping` | state on `pinch_middle`, thresholds `Snapon` / `Snapoff`. Middle finger and thumb together |

18 channels. The edge pulses for these live in the Events group.

## Group: Pose

| channel | definition |
|---|---|
| `h{i}_curl_thumb/_index/_middle/_ring/_little` | 1 − (distance(mcp, tip) / summed bone length of that finger). 0 straight, 1 fully curled |
| `h{i}_openness` | mean of the four finger curls, inverted. 0 fist, 1 open |
| `h{i}_spread` | distance(index_tip, little_tip) / size |
| `h{i}_rotation` | angle of the wrist→middle_mcp vector. In-plane roll |
| `h{i}_point_x`, `h{i}_point_y` | unit vector index_mcp→index_tip |
| `h{i}_point_angle` | its angle. Cursors and pointing rays |

22 channels.

## Group: Motion

Everything here is temporal and therefore depends on frame delivery being
regular — see the jitter note at the end.

| channel | definition |
|---|---|
| `h{i}_vel_x`, `h{i}_vel_y` | d/dt of palm position, units per second |
| `h{i}_speed` | magnitude |
| `h{i}_dir` | direction angle, meaningful only when speed is above a floor |
| `h{i}_accel` | d/dt of speed |
| `h{i}_steadiness` | 1 − normalised variance of palm position over `Steadywindow`. "Hold still to confirm" |
| `h{i}_dwell` | seconds the palm has stayed within `Dwellradius` of where it settled |

14 channels.

## Group: Two hands

Symmetric attributes only — see the slot-assignment caveat at the end.

| channel | definition |
|---|---|
| `hands_together` | state on `hands_distance`, thresholds `Togetheron` / `Togetheroff`. The thing "clap" is the momentary version of |
| `hands_distance` | distance(h0_palm, h1_palm) / mean size |
| `hands_scale` | same, but expressed relative to its own value when both hands became active. A depth-invariant zoom control that starts at 1.0 |
| `hands_center_x`, `hands_center_y` | midpoint of the two palms |
| `hands_angle` | angle of the h0_palm→h1_palm vector |
| `hands_twist` | d/dt of `hands_angle`, unwrapped |
| `hands_approach` | d/dt of `hands_distance`. Negative closing, positive opening |
| `index_distance`, `index_center_x`, `index_center_y` | as above but for the index tips |
| `both_pinching` | `h0_pinching AND h1_pinching` |
| `hands_overlap` | bounding-box intersection area / smaller box area |
| `hands_symmetry` | 1 − abs(h0_openness − h1_openness) |

14 channels.

## Group: Gestures — held states

Combinations of the curls, each debounced by `Gestureframes`.

| channel | condition |
|---|---|
| `h{i}_g_fist` | all four fingers curled, thumb curled |
| `h{i}_g_open` | all five extended, spread above a floor |
| `h{i}_g_point` | index extended, middle/ring/little curled |
| `h{i}_g_peace` | index and middle extended and spread, ring/little curled |
| `h{i}_g_ok` | `pinch_index` closed AND middle/ring/little extended. This is what distinguishes an OK sign from a plain pinch, where the other fingers usually curl too |
| `h{i}_g_thumbsup`, `h{i}_g_thumbsdown` | thumb extended, others curled, thumb direction up or down relative to the palm |
| `h{i}_g_horns` | index and little extended, middle and ring curled |
| `h{i}_g_gun` | index extended, thumb extended, others curled |

18 channels.

## Group: Events — momentary pulses

Every pulse is 1 for exactly one frame. Proximity events re-arm by hysteresis;
motion events re-arm by a `Refractory` timer, because they have no released state.

**Proximity — one hand**

| channel | fires when |
|---|---|
| `h{i}_e_pinch_start`, `h{i}_e_pinch_end` | `pinching` rises / falls |
| `h{i}_e_pinch_click` | a start and end inside `Clickms` - a click rather than a drag |
| `h{i}_e_pinch_double` | two clicks inside `Doublems` |
| `h{i}_e_snap` | `snapping` rises: middle finger and thumb come within `Snapon`. Cannot fire again until they separate past `Snapoff` |
| `h{i}_e_snap_end` | `snapping` falls |

**Proximity — two hands**

| channel | fires when |
|---|---|
| `e_clap` | `hands_together` rises: palms come within `Togetheron`. Cannot fire again until they separate past `Togetheroff` |
| `e_apart` | `hands_together` falls |

**Motion and pose — one hand**

| channel | fires when | reliability |
|---|---|---|
| `h{i}_e_grab`, `h{i}_e_release` | `openness` crosses `Grabon` / `Graboff` | high |
| `h{i}_e_swipe_left/_right/_up/_down` | speed above `Swipespeed`, direction inside `Swipesector`, then deceleration | high |
| `h{i}_e_wave` | `Wavecrossings` sign changes of `vel_x` within `Wavewindow`, hand open | medium |

**Motion — two hands**

| channel | fires when | reliability |
|---|---|---|
| `e_cross` | sign change of (h0_palm_x - h1_palm_x) | high |
| `e_pull_apart`, `e_squeeze` | `hands_approach` beyond +/- `Approachrate`, sustained two frames | high |
| `e_twist_cw`, `e_twist_ccw` | `hands_twist` beyond +/- `Twistrate` | medium |
| `e_frame` | both hands in L shapes with adjacent bounding boxes | medium |

34 channels.

### Where a microphone still helps

Defined as proximity crossings, snap and clap need no audio - they are as
reliable as pinch. Audio remains the better sensor for one thing only: the
*attack timing* of a percussive gesture. If a snap is driving something musical
and needs to land inside 10 ms rather than inside 33 ms, gate an audio transient
with `h0_snapping` and trigger from the audio. For event triggering, which is
what these channels are for, the vision path is sufficient on its own.

## Group: Pose descriptor — off by default

All 21 joints per hand, expressed relative to the palm centre, divided by size,
and rotated so `rotation` is zero. A position-, scale- and rotation-invariant
signature of the pose, for gesture matching or as features for a model.

84 channels.

---

## Output groups and channel budget

| group | channels | default |
|---|---|---|
| `Landmarks` — the raw 137 from the sidecar | 137 | on |
| `Coordstx` — `_tx`/`_ty` for all 42 joints | 84 | on |
| `Coordspx` — `_px`/`_py` for all 42 joints | 84 | off |
| `Core` | 20 | on |
| `Presence` | 10 | on |
| `Contacts` | 18 | on |
| `Pose` | 22 | on |
| `Motion` | 14 | on |
| `Twohands` | 14 | on |
| `Gestures` — held states | 18 | on |
| `Events` — momentary pulses | 34 | on |
| `Descriptor` | 84 | off |

Presets on the `Verbosity` menu: **Minimal** = Landmarks + Coordstx (221),
**Interaction** = everything except Coordspx and Descriptor (371), **Everything**
= all of it (539).

## Parameters

Five pages. The split is by how often you touch them, not by which group they
belong to - a page you have to scroll is a page nobody tunes.

### Page: Attributes

| parameter | type | default | what it does |
|---|---|---|---|
| `Verbosity` | menu | Interaction | Minimal / Interaction / Everything. Sets the toggles below in one click |
| `Landmarks` | toggle | on | publish the raw 137 from the sidecar |
| `Coordstx` | toggle | on | `_tx`/`_ty` for all 42 joints |
| `Coordspx` | toggle | off | `_px`/`_py` for all 42 joints |
| `Core` | toggle | on | palm, size, bbox |
| `Presence` | toggle | on | |
| `Contacts` | toggle | on | |
| `Pose` | toggle | on | |
| `Motion` | toggle | on | |
| `Twohands` | toggle | on | |
| `Gestures` | toggle | on | held states |
| `Events` | toggle | on | momentary pulses |
| `Descriptor` | toggle | off | 84-channel pose signature |

A disabled group has `allowCooking` off, so it costs nothing, and its channels
are excluded from the output rather than left stale.

### Page: Tuning — the ones you will actually adjust

| parameter | type | default | what it does |
|---|---|---|---|
| `Confthreshold` | 0..1 | 0.30 | minimum `conf_median` for a hand to count as active |
| `Pinchon` | ratio | 0.35 | index-thumb distance, over hand size, below which pinch engages |
| `Pinchoff` | ratio | 0.50 | and above which it releases. Must exceed `Pinchon` |
| `Snapon` | ratio | 0.25 | middle-thumb distance below which `snapping` engages and `e_snap` fires |
| `Snapoff` | ratio | 0.45 | and above which it re-arms |
| `Togetheron` | ratio | 0.60 | palm distance, over mean hand size, below which `hands_together` engages and `e_clap` fires |
| `Togetheroff` | ratio | 0.95 | and above which it re-arms |
| `Grabon` | 0..1 | 0.30 | `openness` below which `e_grab` fires |
| `Graboff` | 0..1 | 0.55 | above which `e_release` fires |
| `Swipespeed` | /s | 1.50 | palm speed above which a swipe is considered |
| `Smoothing` | s | 0.05 | master position filter. 0 disables |

### Page: Advanced — presence and pose

| parameter | type | default | what it does |
|---|---|---|---|
| `Activateframes` | int | 3 | consecutive good frames before `active` turns on |
| `Deactivateframes` | int | 6 | consecutive bad frames before it turns off. Higher than activate on purpose: losing a hand mid-gesture is worse than gaining one late |
| `Sizejoint` | menu | middle_mcp | what defines `h{i}_size`: middle_mcp distance, or bbox diagonal |
| `Curlmin` | ratio | 0.35 | tip-to-mcp over finger length that maps to curl 1.0 |
| `Curlmax` | ratio | 0.95 | and to curl 0.0. Calibrates the curl range to a hand |
| `Extendedbelow` | 0..1 | 0.30 | curl below this counts as extended for gesture states |
| `Curledabove` | 0..1 | 0.60 | curl above this counts as curled. The gap debounces the held gestures |
| `Gestureframes` | int | 3 | frames a held gesture must persist before its state turns on |
| `Spreadmin`, `Spreadmax` | ratio | 0.4, 1.6 | normalise `spread` to 0..1 |

### Page: Advanced — motion and events

| parameter | type | default | what it does |
|---|---|---|---|
| `Velocityfilter` | s | 0.08 | filter on velocity before speed and direction. Raw landmark jitter makes unfiltered velocity useless |
| `Speedfloor` | /s | 0.05 | below this, `dir` holds its last value instead of spinning on noise |
| `Steadywindow` | s | 0.50 | window for `steadiness` |
| `Steadytolerance` | units | 0.02 | movement inside this counts as still |
| `Dwellradius` | units | 0.05 | radius for `dwell` |
| `Refractory` | s | 0.25 | re-arm time for MOTION events only. Proximity events use hysteresis instead |
| `Clickms` | ms | 300 | pinch down-and-up inside this is a click, not a drag |
| `Doublems` | ms | 400 | two clicks inside this is a double |
| `Swipesector` | deg | 45 | angular width of each swipe direction |
| `Wavewindow` | s | 1.00 | window for counting wave reversals |
| `Wavecrossings` | int | 3 | reversals needed inside that window |
| `Approachrate` | /s | 1.20 | `hands_approach` magnitude for `e_pull_apart` / `e_squeeze` |
| `Twistrate` | deg/s | 90 | `hands_twist` magnitude for the twist events |
| `Overlapthreshold` | 0..1 | 0.15 | bbox intersection fraction that counts as overlapping |
| `Scalereset` | pulse | - | sets `hands_scale` to 1.0 at the current spread |

### Page: Coords

| parameter | type | default | what it does |
|---|---|---|---|
| `Resw`, `Resh` | int | 1280, 720 | SOURCE resolution. Drives `_px`/`_py` only |
| `Renderw`, `Renderh` | int | from render1 | RENDER resolution. Drives `_ty`. Not the same number as the source - see DESIGN.md 2.9 |
| `Orthowidth` | float | from cam1 | match your ortho camera's Ortho Width |

### Page: Sidecar

`Startsidecar`, `Stopsidecar`, `Sidecarstatus` pulses and a read-only
`Sidecarpid`, as built.

### Two rules the defaults follow

**Every `off` threshold is looser than its `on` threshold.** That gap is the
hysteresis, and it is what makes a gesture end deliberately rather than by
jitter. A build that sets them equal has no re-arm rule and will chatter.

**Every threshold that is a distance is a RATIO of hand size**, never a raw
normalised distance. The units column says `ratio` for exactly those. This is
what makes a pinch tuned at arm's length still work when you lean in.

## Two caveats that are properties of the system, not of this list

**Per-hand identity is not yet trustworthy.** Slot assignment is milestone 5;
until it lands, `h0` can swap to the other physical hand between frames
(`DESIGN.md` 6.3). Symmetric two-hand attributes — `hands_distance`,
`hands_center`, `hands_angle`, `e_clap` — are unaffected. Anything that cares
which hand is which, including `hands_symmetry` and every per-hand channel over
time, is unreliable until then.

**Angles wrap, and filtering them naively produces spikes.** A hand crossing
±180° makes a smoothed `hands_angle` swing through zero, which reads as a violent
twist. Anywhere an angle is differentiated or filtered, it is unwrapped first, or
sin/cos are filtered and recombined.

**Per-joint jitter is still unmeasured** (`DESIGN.md` 11). It determines how much
smoothing the temporal groups need, and it is measurable from the fixture without
a person present. Worth doing before tuning any velocity threshold.
