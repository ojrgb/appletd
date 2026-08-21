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

## Group: Pinch

| channel | definition |
|---|---|
| `h{i}_pinch_index` | distance(index_tip, thumb_tip) / size |
| `h{i}_pinch_middle/_ring/_little` | same for the other fingertips |
| `h{i}_pinch_angle` | angle of the thumb_tip→index_tip vector |
| `h{i}_pinch_x`, `h{i}_pinch_y` | midpoint of thumb_tip and index_tip — the grab point, more stable than either tip |
| `h{i}_pinching` | state, with **hysteresis**: on below `Pinchon`, off above `Pinchoff`. A single threshold chatters at the boundary |
| `h{i}_pinch_start`, `h{i}_pinch_end` | pulses on the edges |

20 channels.

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

13 channels.

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

## Group: Gestures — momentary pulses

Each fires for one frame and then cannot fire again for `Refractory` seconds.
Without a refractory window a single physical gesture fires on several
consecutive frames.

**One hand**

| channel | trigger | reliability |
|---|---|---|
| `h{i}_e_pinch_click` | `pinch_start` then `pinch_end` within `Clickms` | high |
| `h{i}_e_pinch_double` | two clicks within `Doublems` | high |
| `h{i}_e_grab`, `h{i}_e_release` | `openness` crossing down/up through thresholds | high |
| `h{i}_e_swipe_left/_right/_up/_down` | speed above `Swipespeed` with direction inside a 45° sector, then deceleration | high |
| `h{i}_e_wave` | three or more sign changes in `vel_x` within `Wavewindow`, hand open | medium |
| `h{i}_e_snap` | `pinch_middle` closed, then d/dt above `Snapspeed` | **low at 30 fps — see below** |

**Two hands**

| channel | trigger | reliability |
|---|---|---|
| `e_clap` | `hands_approach` strongly negative, then `hands_distance` below `Clapdistance` | medium |
| `e_cross` | sign change of (h0_palm_x − h1_palm_x) | high |
| `e_pull_apart`, `e_squeeze` | `hands_approach` beyond ± threshold, sustained two frames | high |
| `e_twist_cw`, `e_twist_ccw` | `hands_twist` beyond ± threshold | medium |
| `e_frame` | both hands in `g_gun`-like L shapes, bounding boxes adjacent | medium |

27 channels.

### Snap and clap deserve a warning, and a better answer

A snap lasts roughly 60 ms. At 30 fps that is **two frames** — right at the
Nyquist limit, so a velocity threshold will miss snaps and occasionally invent
them. A clap is a little slower but has the same problem: the informative part is
the impact, and the impact happens between frames.

The camera cannot help: this device advertises 15–30 fps and nothing higher
(MEASURED, `DESIGN.md` 2.8).

**The right sensor for a snap or a clap is a microphone.** Both are sharp audio
transients, trivially detectable with an Audio Device In CHOP and an envelope or
a high-band spectrum, at audio rate rather than frame rate. The strong pattern is
to combine them: the landmarks say *the hands are together* or *the fingers are
in snap position*, and the audio transient says *now*. That gives a gesture that
is both correctly timed and not triggered by an unrelated noise.

The vision-only versions are implemented because they are asked for and because
they work well enough to prototype with, but they are labelled medium and low
confidence here on purpose, and the audio route is the one to reach for if either
gesture matters.

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
| `Pinch` | 20 | on |
| `Pose` | 22 | on |
| `Motion` | 14 | on |
| `Twohands` | 13 | on |
| `Gestures` — held states | 18 | on |
| `Events` — momentary pulses | 27 | on |
| `Descriptor` | 84 | off |

Presets on the `Verbosity` menu: **Minimal** = Landmarks + Coordstx (221),
**Interaction** = everything except Coordspx and Descriptor (365), **Everything**
= all of it (533).

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
