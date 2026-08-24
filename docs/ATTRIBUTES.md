# Derived attributes — the definitive list

What the `appletd` COMP computes from the 137 channels the sidecar sends.
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

**Two rules that are not optional.** An absent hand publishes every joint at
(0, 0), so every distance is 0 - below every `on` threshold - and `size` is 0 too,
making every normalised distance 0/0. Without these, losing a hand fires pinch,
snap and clap simultaneously and floods the output with NaN, which spreads
silently through TouchDesigner maths:

    state = h{i}_active AND live AND ( below_on OR (prev AND NOT above_off) )
    size  = max(raw_size, Sizefloor)

`live` is a THIRD condition, and it is not optional either. A dead sidecar leaves
TouchDesigner's channels **frozen, not zeroed** (`DESIGN.md` 2.9), so `found`,
`conf_median` and every distance keep reporting the last good frame for ever. Stop
the sidecar mid-pinch without this term and the latch stays engaged permanently,
no end pulse ever fires, and anything gated on the state is stuck on with nothing
reporting it. Derived with no Python from the slope of `seq`: non-zero means
frames are still arriving. See `DESIGN.md` 2.10 for why that detector needs its own
clock — the absence of data is otherwise exactly what stops it noticing.

**Currently the gate is raw per-frame validity, not the debounced `active`.** That
is a known shortfall, not a design change: one low-confidence frame mid-pinch
drops the state, fires a spurious end pulse, and re-engages with a spurious start
and a bumped counter. `Activateframes` and `Deactivateframes` exist for exactly
this, the debounce belongs to the Presence group, and when it is built it is ANDed
into the same gate — one place, alongside `live`.

The `active` gate is on the STATE, not just on the pulses. Gate only the pulses
and the latch sits quietly engaged while the hand is gone, then emits a spurious
`end` pulse when it comes back. Derived channels for an inactive hand are forced
to 0 rather than left at their last value, so nothing downstream mistakes a stale
reading for a live one.

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

### Why a latch and not a pulse counter

The obvious alternative - and a common one - is to accumulate edges: a Count CHOP
looping 0..1, incremented by the engage crossing and again by the release
crossing, with the input gated to prevent double-counting. TouchDesigner's Logic
CHOP has the same thing built in as `preop = Toggle`.

It works, and it has one structural weakness: it stores the PARITY of the pulses
it has seen. Miss a pulse - a dropped frame, or a distance that crosses both
thresholds inside a single frame - and the state is inverted permanently, until
some later pulse happens to flip it back. Nothing in the topology can detect
that it is wrong, which is why such designs end up needing a reset path.

The recurrence above is level-driven instead. `d < on` forces the state true and
`d > off` forces it false, both regardless of history; history is consulted only
inside the dead band, where the input genuinely is ambiguous. So every kind of
desynchronisation - a missed edge, a double crossing, an odd startup state -
corrects itself on the next frame where the distance is unambiguous. There is no
accumulated state to get out of step, and no reset to wire.

A counter is still worth having alongside it, for what a counter is actually good
at: `h{i}_pinch_count`, how many times the gesture has fired. Just not as the
state.

Built as a Feedback plus an add rather than as a Count CHOP — same accumulation,
one fewer operator whose parameters have to be interpreted correctly. The Count
CHOP's condition parameters are menus of actions rather than the toggles they
resemble, and it reads zero through a sweep if its Time Slice setting disagrees
with its input's. The accumulator is the same construction the latch already uses
for `prev`, so it introduces nothing new.

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
| `h{i}_active` | `found AND conf_median > Confthreshold AND the stream is live`, held for `Activateframes` before turning on and `Deactivateframes` before turning off. Debounced because a single low-confidence frame should not drop an interaction; gated on liveness because a dead sidecar leaves the channels frozen rather than zeroed, and without it `active` stays true for ever — measured, `h0_held` reached 54 s with nothing sending |
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
| `h{i}_pinch_count`, `h{i}_snap_count`, `clap_count` | how many times each has fired since the project opened. A counter doing the one job a counter is good at — see above |
| `h{i}_pinch_end_count`, `h{i}_snap_end_count`, `apart_count` | and how many times each has released |

28 channels. The edge pulses for these live in the Events group.

### Finger triggers — the same mechanism, as a uniform grid

| channel | definition |
|---|---|
| `h{i}_index_trigger` | state on `pinch_index`, thresholds `Triggeron` / `Triggeroff` |
| `h{i}_middle_trigger`, `h{i}_ring_trigger`, `h{i}_little_trigger` | the same for the other three fingertips |
| `h{i}_e_{finger}_trigger`, `h{i}_e_{finger}_release` | the edge pulses |
| `h{i}_{finger}_trigger_count`, `h{i}_{finger}_release_count` | and the counters |

40 channels. Thumb-to-fingertip contact for all four long fingers on both hands,
sharing **one** `Triggeron` / `Triggeroff` pair.

**One shared threshold pair, not one per finger.** Sixteen sliders for a grid
whose entire value is that all eight behave identically would defeat the purpose,
and the resting distance differing per finger changes how far the thumb has to
travel, not whether the contact fires. The default is a little looser than
`Pinchon` because the ring and little fingers meet the thumb less squarely than the
index does, and a threshold tuned on the index makes them feel dead.

**They deliberately overlap the named gestures.** `h{i}_index_trigger` watches the
same distance as `h{i}_pinching`, and `h{i}_middle_trigger` the same as
`h{i}_snapping`. That is not redundancy to tidy away — the two serve different
jobs. Pinch and snap are named gestures tuned by feel, each with their own
thresholds; the triggers are a uniform grid to map eight things to. Either can be
retuned without disturbing the other.

**No thumb trigger.** Every distance here is measured *to* the thumb tip, so the
thumb is the common reference and cannot contact itself. A thumb gesture would have
to be a curl threshold rather than a distance — a different mechanism, and not what
this group is.

**The two counters are a standing invariant, not just a convenience:**

    fires - releases  ==  1 if the gesture is engaged right now, else 0

That holds at every instant, and it is what makes the falling edges testable at
all — without the release counters, every verification sweep passed with
`h{i}_e_pinch_end`, `h{i}_e_snap_end` and `e_apart` structurally present and
behaviourally unmeasured. A missing end pulse, a doubled one, or a missing start
all break it.

The counters are deliberately configured to count every FRAME the start pulse is
high rather than every off-to-on transition. Both give the same answer when the
network is correct, because `start = state AND NOT prev` is one frame wide by
construction — so counting frames measures the pulse width as well as the fire
count, and a pulse that ever lingered two frames would show up as a doubled
count instead of passing silently.

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
| `h{i}_vel_x`, `h{i}_vel_y` | d/dt of palm position, units per second, averaged over `Velocityfilter` |
| `h{i}_speed` | magnitude |
| `h{i}_dir` | heading in degrees, HELD at its last value while speed is below `Speedfloor` rather than spinning on noise |
| `h{i}_dir_x`, `h{i}_dir_y` | the same heading as a unit vector, held with it. Most consumers want this rather than the angle: an angle has to be unwrapped before it can be interpolated, and this pair cannot wrap |
| `h{i}_moving` | 1 when speed is above `Speedfloor`. What tells a consumer whether the held heading is current |
| `h{i}_accel` | d/dt of speed |
| `h{i}_steadiness` | 1 − normalised variance of palm position over `Steadywindow`. "Hold still to confirm" |
| `h{i}_dwell` | seconds the palm has stayed within `Dwellradius` of where it settled |

20 channels.

**All three heading channels are held together**, which is not a detail. Holding
only the angle left the unit vector following the live velocity, so a stationary
hand reported `dir` = 180 while `dir_x` read +1 — one saying left, the other
right, with no way for a consumer to know which to believe. Two channels
disagreeing is worse than either alone.

`dir` comes from `atan2`, which **no CHOP provides** — the Math CHOP's unary menu
covers negate, absolute value, square, root and reciprocal and stops there. So it
is the second pure-Python operator in the COMP, `appletd/motion.py`, wrapped
the same way `derive()` is. The *holding* is native, because holding is memory:
the module publishes `moving` and lets the network decide what to remember, which
is the same division of labour as the latches.

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
| `h{i}_e_grab`, `h{i}_e_release` | `openness` crosses `Grabon` / `Graboff`. Built as two more rows in the latch bank — a fist is a hand whose openness has crossed a threshold, which is the same shape of problem as a pinch, so it needed no new operators. Also publishes `h{i}_grabbing` and the two counters | high |
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

## Group: Depth — off by default, and inferred rather than measured

Vision returns 21 points on a PLANE. There is no depth in the input, so everything
here is a consequence of the projection rather than a measurement, and the channels
are named to admit it.

| channel | what it is |
|---|---|
| `h{i}_z` | `Zreference / h{i}_size`. 1.0 at the reference distance, 2.0 at twice it, 0.5 at half |
| `h{i}_z_<finger>` | 0..1, how much the finger points ALONG the camera axis rather than across it |

**`h{i}_z` is monotonic in distance and nothing else.** Apparent size goes as
1/distance, so this rises as the hand retreats — which is all a "lean in to zoom"
interaction needs. It is **not** metres, and it is **not comparable between two
different hands**: a child's hand at 40 cm and an adult's at 60 read alike, because
apparent size confounds hand size with distance. `Zreference` defaults to 0.12,
which is `synth.HandPose.size`'s own default and the closest thing this project has
to a reference distance. To recalibrate, hold a hand where you want z = 1 and read
`h0_size`.

**`h{i}_z_<finger>` CANNOT TELL TOWARD FROM AWAY.** A finger pointing at the camera
and one pointing directly away project to the same foreshortened length, and that
ambiguity is in the projection, not in the arithmetic. It is asserted as a test, so
nobody later "fixes" it. Two more things about it:

- it is only meaningful for a STRAIGHT finger, because a curled one foreshortens too.
  `h{i}_curl_<finger>` is how a consumer tells those apart;
- it is UNMEASURED on a real hand. The arithmetic is exact and the usefulness is not
  established.

## Group: Tilt — off by default, and deliberately not pitch and yaw

| channel | what it is |
|---|---|
| `h{i}_tilt` | 0..90 degrees. 0 is the palm square on to the camera, 90 edge-on |
| `h{i}_tilt_axis` | degrees, which way it is leaning. Meaningless when `tilt` is near 0 |

**Every other angle in this system is in-plane.** `h{i}_rotation`,
`h{i}_point_angle`, `h{i}_pinch_angle` and `hands_angle` all come from `atan2` on two
image coordinates, so all of them are rotations about the camera's Z axis. These two
are the out-of-plane part.

**Why not pitch and yaw, which is the natural request.** Recovering those from a 2D
projection needs the one thing a projection destroys: which side of the image plane a
point is on. A palm tilted 30 degrees toward the camera and one tilted 30 degrees
away project *identically*, so pitch and yaw cannot be signed — and publishing them
unsigned under those names would misrepresent what they are. Magnitude plus axis is
what the projection actually determines: two honest numbers instead of two dishonest
ones.

**How.** The palm triangle — wrist, index MCP, little MCP — has an apparent area that
shrinks with the cosine of the tilt, so `acos(area / Palmarea)` is the magnitude. The
palm foreshortens *along* the direction it leans, so the axis it turns about is the
longest surviving edge.

**`Palmarea` is the zero point of the channel and it needs calibrating.** MEASURED at
**0.3059** against `synth.py`'s geometry — not against a real hand. The first value
was a guess of 0.14, which put the ratio over 1.0 and clamped it: `tilt` read exactly
0 for the first **62 degrees** of real rotation, a dead zone big enough to make the
channel useless while looking like it worked. It is deliberately set at the measured
value rather than above it, because a small dead zone near zero is a better failure
than reporting tilt on a hand that has none. **If a real face-on hand reads non-zero
`tilt`, raise `Palmarea`.**

**The confound:** the triangle's corners include two MCP knuckles, and MEASURED on
`synth.py` the area ratio moves with finger SPREAD — 0.1835 at spread 0.6, 0.4282 at
1.4. So spreading the fingers reads partly as un-tilting the palm. A real hand's
knuckles move far less than the synthesiser's, so the true confound is smaller than
that range suggests, but it is not zero and nothing here corrects for it.

## Group: Pose descriptor — off by default

All 21 joints per hand, expressed relative to the palm centre, divided by size,
and rotated so `rotation` is zero. A position-, scale- and rotation-invariant
signature of the pose, for gesture matching or as features for a model.

84 channels.

---

## Output groups and channel budget

| group | channels | default |
|---|---|---|
| `Coordstx` — `_tx`/`_ty` for all 42 joints | 84 | on |
| `Coordspx` — `_px`/`_py` for all 42 joints | 84 | **on** |
| `Core` | 20 | on |
| `Presence` | 10 | on |
| `Contacts` | 28 | on |
| `Pose` | 22 | on |
| `Twohands` | 14 | on |
| `Gestures` — held states | 18 | on |
| `Descriptor` | 84 | off |
| `Depth` — `h{i}_z` and five per-finger | 12 | **off** |
| `Tilt` — `h{i}_tilt` and `h{i}_tilt_axis` | 4 | **off** |

**Always on, with no toggle of their own** — the raw 137 landmark channels from the
sidecar (137), the thumb-to-fingertip trigger grid (40), the motion channels (14) and
the momentary event pulses (34). Each had a toggle until 2026-08-23 and none of them
worked: removing a group's channels from the output needs an exact channel-to-group
map, and a wildcard cannot separate the raw `h0_wrist_x` from the derived
`h0_palm_x`. They were removed rather than left on the page pretending. Use
`Finger Tips Only` to cut the landmark channels down, and `Temporal`/`Latches` to
freeze the subsystems those channels come from.

Presets on the `Level of Detail` menu: **Minimal** = Coordstx only,
**Interaction** = everything except the pixel spaces and Descriptor,
**Everything** = all of it.

**OFF FREEZES, IT DOES NOT REMOVE — and the label says so now.** Turning `Coordspx`
off stops `coords/pixels` cooking; the `_px`/`_py` channels stay on the COMP output
holding their last value. VERIFIED when a user asked why they were still there: 0
cooks across all eight operators in `coords/pixels` over 403 frames of moving data.
What the toggle saves is the COMPUTE, about 0.1 ms per stream, not the channel count.

Why that way round: `DESIGN.md` 6.2 — a channel that VANISHES breaks every reference
to it with no error anywhere, so stale beats absent. If you actually want the
channels gone from the output, that is `Screenspaceonly`'s job, and it currently
removes the RAW normalised ones rather than the pixel ones.

A frozen channel is likely to read **0**, not a plausible last value, because a
freshly built group is frozen right after a cook that had no data in it yet.

**`Coordspx` DEFAULTS ON as of 2026-08-21, and that is a change.** It used to
default off and gate nothing at all, so the `_px`/`_py` channels were always live.
It now freezes `coords/pixels` — the channels stay, holding their last value — and a
toggle that can silently stop channels updating has to default to not doing that.
Turning it off is a deliberate saving of about 0.1 ms per stream.

### And on the other two streams

`Coordstx` and `Coordspx` serve all three streams, not just hands, and the FACE
stream has two more of its own:

| group | channels | default |
|---|---|---|
| `Lmcoordstx` — `_tx`/`_ty` for all 174 face landmark points | 348 | on |
| `Lmcoordspx` — `_px`/`_py` for the same | 348 | off |

Separate from `Coordstx`/`Coordspx` because they are two orders of magnitude
bigger: the face's own bounding box is 8 channels and its landmark points are 348,
MEASURED at **1.08 ms per cook** for the world half alone. A project that wants a
face's box in world space should not have to pay for 174 points, and before this
split it did.

## Parameters

Five pages. The split is by how often you touch them, not by which group they
belong to - a page you have to scroll is a page nobody tunes.

### Page: Attributes

| parameter | type | default | what it does |
|---|---|---|---|
| `Level of Detail` | menu | Interaction | Minimal / Interaction / Everything. Sets the toggles below in one click |
| `Coordstx` | toggle | on | `_tx`/`_ty`, EVERY stream, wire contract AND derived. Freezes `coords/world` and `coords/dv_world` |
| `Coordspx` | toggle | on | `_px`/`_py`, the same. Freezes `coords/pixels` and `coords/dv_pixels` |
| `Lmcoordstx` | toggle | on | `_tx`/`_ty` for the 174 FACE landmark points |
| `Lmcoordspx` | toggle | off | `_px`/`_py` for the same. 348 channels, 1.08 ms |
| `Core` | toggle | on | palm, size, bbox |
| `Presence` | toggle | on | |
| `Contacts` | toggle | on | |
| `Pose` | toggle | on | |
| `Twohands` | toggle | on | |
| `Gestures` | toggle | on | held states. Also what gates `latches` cooking |
| `Descriptor` | toggle | off | 84-channel pose signature |
| `Hand Box and Size` | toggle | on | off drops `h?_bbox_*` and `h?_size` from the output |
| `Face Key Points` | toggle | off | on swaps the 664 face landmark channels for 32: two pupils, a nose tip and a mouth centre per face. Freezes `face/coords/lm_world` and `lm_pixels`, and **saves 1.18 ms** |

A disabled `derive` group's channels are excluded from the output. A disabled
NATIVE group has `allowCooking` off, so it costs nothing and its channels are left
at their last value — which is deliberate, and 6.2 of `DESIGN.md` is why.

**And then they are trimmed off the output.** The component has ONE output, and a
Select in front of it (`trim_empty`) keeps only the channels that are actually being
computed. So a group you switch off does not just stop costing anything — its
channels leave the list. In the shipping configuration that is 258 channels on the
output rather than the 2,041 every stream and space produce (MEASURED 2026-08-24).
`Delete Empty Channels` on the Vision page turns the trim off if you would rather
see everything, frozen values included.

**Five families never reach the output at all**, whatever the toggles say:

| never on the output | where it went | why |
|---|---|---|
| the 80 per-joint `*_conf` | nowhere — use `h?_conf_median` | one confidence per joint is 80 channels nobody reads one at a time |
| `h?_score` `h?_conf_median` `h?_chirality` | the `housekeeping` Null | quality and identity scalars; you should not meet three of them per hand before you meet a fingertip |
| every `sc_*` — uptime, one per request, and `sc_src_w`/`sc_src_h` | the `housekeeping` Null | the capture process's own state, not the hands'. `sc_src_w`/`sc_src_h` are what the camera ACTUALLY delivered, which is where `Resw`/`Resh` come from |
| `seq` `pose_seq` `face_seq` | the `housekeeping` Null | frame counters |
| `age_ms` `pose_age_ms` `face_age_ms` | the `housekeeping` Null | how stale the last datagram is |

`housekeeping` is a Null CHOP inside the component, beside `out1`. Open the COMP and
look at it, and it costs 0.0037 ms because a Null only cooks when something is
watching.

**`h?_conf_median` moved to `housekeeping` on 2026-08-23 and is still what to gate
on** (`DESIGN.md` 6.2). That is exactly why it was routed there rather than dropped:
the advice has not changed, only where you read the channel. Reference it as
`op('/project1/appletd/housekeeping')['h0_conf_median']`, or switch
`Delete Empty Channels` off to get everything back on `out1`.

#### Three toggles that shorten the list

| toggle | default | does |
|---|---|---|
| `Finger Tips Only` | **off** | drops the 15 non-tip joints, leaving `wrist` and the five `*_tip`s — six points per hand instead of 21 |
| `Hand Box and Size` | **on** | off drops `h?_bbox_*` and `h?_size` |
| `Face Key Points` | **off** | on swaps 664 face landmark channels for 32 |

All three defaults leave the output exactly as it was before they existed. `Finger
Tips Only` keeps the **wrist**: it is not an mcp, pip or dip, it is the hand's
anchor, and `hands_angle` is measured from it.

**The first two change only the output, and that is forced rather than lazy** — the
fifteen non-tip joints feed every curl, spread and angle `derive()` computes, and the
bounding boxes feed `hands_overlap`. The work happens either way; they remove second
copies from the list you read.

**`Face Key Points` is the one that also saves time**, because the four points it
leaves are composed somewhere else. MEASURED 2026-08-24: the face stream costs
1.9644 ms per cook with it off and 0.7872 ms with it on — the landmark halves stop
cooking entirely. It costs 0.10 ms when it is OFF, which is what the four points
cost to compose alongside the bounding box, and is the price of their being there
whether or not the toggle is.

**Every toggle on the page now changes something.** Four that could not —
`Landmarks`, `Triggers`, `Motion` and `Events` — were removed on 2026-08-23 rather
than left there pretending. What removing `Motion` specifically cost: it was an OR
term alongside `Presence` on `temporal`, so `Motion` on with `Presence` off used to
mean "cook temporal for its velocity half, not its presence half". There is no way to
say that now — `Presence` off freezes the whole group. `Presence` ships on, so the
common case is unaffected.

#### What the toggles gate, and the one thing to know about switching one off

Three kinds of toggle, and the label says which:

| toggles | what switching it off saves |
|---|---|
| `Core` `Presence` `Contacts` `Pose` `Twohands` `Gestures` `Descriptor` | `derive()` does not compute the group, and its channels leave the output |
| `Presence` (→ `temporal`), `Gestures` (→ `latches`), `Coordstx` `Coordspx` (→ `coords/world`, `coords/pixels` in all three streams), `Lmcoordstx` `Lmcoordspx` (→ the face's landmark halves) | the group COMP stops cooking entirely, via `allowCooking`. Its channels stay, holding their last value — which is why `trim_empty` removes them from the output |
| `Temporal` `Latches` | a VETO: off freezes the group whatever the toggles above say. See below |
| `Finger Tips Only` `Hand Box and Size` | the OUTPUT only. Neither gates cooking and neither can: the 15 non-tip joints feed every curl and spread `derive()` computes, and the bounding boxes feed `hands_overlap` |
| `Face Key Points` | a REVERSE veto: switching it ON freezes `face/coords/lm_world` and `lm_pixels`. The only toggle here that removes capability rather than adding it, which is why it is in none of the `Level of Detail` presets — `Everything` would otherwise switch off the 348 landmarks it has just promised |

**`Temporal` and `Latches` are master switches, and they needed a second
mechanism.** The table above is an OR — "any of these toggles being on keeps the
group alive" — which is right for `Presence` and `Motion`, because either one needs
`temporal` cooking. It cannot express *off*: adding `Temporal` to that list would
mean turning it ON keeps the group alive, the opposite of a switch. So a veto:

    cooking = any(COOK_GATED) and all(COOK_VETOED)

**Turning `Temporal` off takes `latches` with it.** The latch bank gates on
`temporal`'s presence signal, and a frozen gate either arms every latch for ever or
disarms them for ever with nothing to say which — which is the silent failure the
whole dependency exists to prevent. So the veto wins over that dependency rather
than being overridden by it, and the builder prints when it does. VERIFIED by cook
count: `Temporal` off froze all 74 operators in `temporal` and all 53 in `latches`.

**The stream toggles on the Sidecar page gate cooking too**, since 2026-08-21:
`Streamhands`/`Streampose`/`Streamface` are launch flags for the sidecar AND
`allowCooking` on the stream's child COMP. A disabled stream was still processing a
full datagram of zeros every frame — measured at 0.25 ms for pose and 1.45 for face.
The flag reaches the sidecar only on a restart; the freeze takes effect at once.

A group COMP stops cooking when ALL of its toggles are off. **`latches` cooking
keeps `temporal` cooking** whatever Presence and Motion say, because the latches are
armed by temporal's presence gate — a frozen gate would either arm every latch
forever or disarm them forever, with nothing to indicate which.

**OBSERVED 2026-08-21, and it is the documented cost arriving in practice.** Freezing
`latches` with the new `Temporal` veto and then re-enabling it left the counters one
release ahead of one fire — `tools/td_verify_latches.py` reported the standing
invariant BAD, with 20 fires against 21 releases. That is exactly the "one edge pulse
across the gap may be wrong" this section predicts, and rebuilding the latch bank
(which resets the Count CHOPs) then gave a clean ramp +4 with all five invariants ok.

So the invariant check is what makes the cost VISIBLE rather than silent, and the
practical rule is: after freezing and unfreezing a memory group, the cumulative
counters are off by up to one per gap. The deltas stay correct, which is what a
project reads.

**A frozen group keeps its channels**, holding their last values. They do not vanish
from the COMP output, so nothing downstream loses a reference (DESIGN.md 6.2).

**The one behavioural consequence.** A group with memory PAUSES rather than resets,
so re-enabling one resumes with a `prev` from whenever it was switched off: the state
is stale for one frame and one edge pulse across the gap may be wrong. It is
self-correcting, because every recurrence here is level-driven rather than counted —
but if you are switching a group on and off during a performance, expect that one
frame rather than being surprised by it.

**`filter` is deliberately not gated this way.** It sits in the data path, so
freezing it would freeze every position rather than bypass the filter. Its
`Smoothing` toggle gates its cost through the bypass flag instead.

### Page: Tuning — the ones you will actually adjust

**Eleven of these did not exist until 2026-08-21, and that was a real gap rather
than a new feature.** `derive()`'s `_params()` has always read them by name with a
getattr fallback, and nothing ever created them — so every threshold in `derive()`
was stuck at its dataclass default and the panel gave no sign. `Confthreshold`,
`Sizefloor`, `Curlmin`, `Curlmax`, `Extendedbelow`, `Curledabove`, `Spreadmin`,
`Spreadmax` and `Overlapthreshold` are all reachable now, along with `Zreference`
and `Palmarea` for the two new groups.

They are built by iterating `dataclasses.fields(Params)`, so the page and the
dataclass cannot disagree and a new field arrives on the panel without a second
edit. The hand-written version they replaced had already drifted: it listed eight of
the nine fields that existed, so `Overlapthreshold` was silently unreachable even
where the others would have worked.

| parameter | type | default | what it does |
|---|---|---|---|
| `Confthreshold` | 0..1 | 0.30 | minimum `conf_median` for a hand to count as active |
| `Pinchon` | ratio | 0.35 | index-thumb distance, over hand size, below which pinch engages |
| `Pinchoff` | ratio | 0.50 | and above which it releases. Must exceed `Pinchon` |
| `Snapon` | ratio | 0.25 | middle-thumb distance below which `snapping` engages and `e_snap` fires |
| `Snapoff` | ratio | 0.45 | and above which it re-arms |
| `Togetheron` | ratio | 0.60 | palm distance, over mean hand size, below which `hands_together` engages and `e_clap` fires |
| `Togetheroff` | ratio | 0.95 | and above which it re-arms |
| `Triggeron` | ratio | **0.13** | thumb-to-fingertip distance below which any finger trigger engages. MEASURED on a real hand |
| `Triggeroff` | ratio | **0.17** | and above which it re-arms. One pair for all eight |

**The only measured pair in this table, and the cautionary tale for the rest.**
These were guessed at 0.40 / 0.55 from synthetic geometry, where a "contact" pose
puts the fingertips exactly coincident. Tuned against a real hand they came out
about **three times tighter**, and 0.40 fired while the fingers were still visibly
open. Every other threshold here is still a guess of the same kind: synthetic
geometry validates arithmetic and says nothing about how a hand sits.
| `Grabon` | 0..1 | 0.30 | `openness` below which `e_grab` fires. The one threshold here that is NOT a ratio of hand size — `openness` is already 0..1 |
| `Graboff` | 0..1 | 0.55 | above which `e_release` fires |
| `Swipespeed` | /s | 1.50 | palm speed above which a swipe is considered |
| `Smoothing` | toggle | on | the one-euro position filter. Off is a bit-exact passthrough, not a filter set to zero |
| `Mincutoff` | Hz | 1.5 | how heavily a SLOW-moving hand is smoothed |
| `Beta` | 1/units | 2.0 | how far a FAST-moving hand is allowed through |

### Page: Advanced — the internals

| parameter | default | what it does |
|---|---|---|
| `Oscport` | 10000 | the BASE port. Streams take base + 0/1/2 (DESIGN.md 6.4) |
| `Printstatus` | pulse | is a capture process running, and its pid |
| `Capturepid` | read-only | what was last started. Not proof it is still alive |

**`Oscport` splits in two directions and the label says restart.** The three OSC In
CHOPs bind it through an EXPRESSION, so TouchDesigner starts listening on the new
port immediately; the running process keeps sending to the old one until it is
restarted. The visible symptom of that gap is `sc_uptime_s` freezing — the same
signal as a dead process, which is worth knowing before you chase it.

**This page is shared by two builders.** `tools/td_build_vision.py` owns the three
above and `tools/td_add_temporal.py` owns the four below. That is safe only because
each appends its own names and each restores its own stored values; the hazard is two
scripts appending the SAME parameter, where whichever runs second resets it.

### Page: Advanced — presence and pose

| parameter | type | default | what it does |
|---|---|---|---|
| `Activateframes` | int | 3 | consecutive good frames before `active` turns on |
| `Deactivateframes` | int | 6 | consecutive bad frames before it turns off. Higher than activate on purpose: losing a hand mid-gesture is worse than gaining one late |
| `Sizejoint` | menu | middle_mcp | what defines `h{i}_size`: middle_mcp distance, or bbox diagonal |
| `Sizefloor` | units | 0.01 | minimum divisor for `h{i}_size`. Guards 0/0 when a hand is absent or degenerate |
| `Curlmin` | ratio | 0.35 | tip-to-mcp over finger length that maps to curl 1.0 |
| `Curlmax` | ratio | 0.95 | and to curl 0.0. Calibrates the curl range to a hand |
| `Extendedbelow` | 0..1 | 0.30 | curl below this counts as extended for gesture states |
| `Curledabove` | 0..1 | 0.60 | curl above this counts as curled. The gap debounces the held gestures |
| `Gestureframes` | int | 3 | frames a held gesture must persist before its state turns on |
| `Spreadmin`, `Spreadmax` | ratio | 0.4, 1.6 | normalise `spread` to 0..1 |

### Page: Advanced — motion and events

| parameter | type | default | what it does |
|---|---|---|---|
| `Velocityfilter` | s | 0.15 | the window a velocity is AVERAGED over. Not merely smoothing: positions update at the camera rate and are differentiated at the cook rate, so the per-cook derivative alternates between double-size and zero, and the mean over a window is what makes the number true. 0.15 s spans four or five camera frames — see `DESIGN.md` 2.11 |
| `Speedfloor` | /s | 0.05 | below this, `dir` and its unit vector hold their last value instead of spinning on noise, and `h{i}_moving` reads 0. GUESSED — a floor below the actual noise floor achieves nothing, so the jitter measurement is what should set it |
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

### Screen space only — one toggle on the Vision page

| parameter | type | default | what it does |
|---|---|---|---|
| `Screenspaceonly` | toggle | off | removes every RAW normalised channel that has a screen-space companion |

On, each stream's output loses exactly the channels whose information is fully
available in TouchDesigner's own spaces — `h0_wrist_x` goes and `h0_wrist_tx`/`_px`
stay. Measured: hands 499 → 415, pose 275 → 199, face 1083 → 727, with the removed
set equal to the delete list exactly and nothing else touched.

It KEEPS every boolean, pulse, counter, confidence and **angle**. The rule is
*remove the second copy of a number, never the only copy* — a yaw has no
screen-space form and a confidence has no meaning in pixels, so dropping those
would lose information rather than a duplicate.

It ships OFF, and it is one Delete CHOP per stream sitting after `merge_out`, where
nothing inside the COMP reads through it. Deleting channels is normally the silent
failure this whole system is arranged to avoid (`DESIGN.md` 6.2); this is the one
place it is the point.

**What it leaves, and why.** Eight channels — `h{i}_point_x/y` and `h{i}_dir_x/y` —
are UNIT VECTORS, and they have no companion on purpose. World space scales y by the
render's aspect, so a transformed unit vector is neither unit length nor pointing
where the image-space one pointed; re-normalising it is a magnitude and a divide,
which is a different branch rather than a companion.

And the 84 `h{i}_d_<joint>_x/y` descriptor channels are hand-LOCAL — joint offsets
in the hand's own frame, divided by hand size. A shape signature, with no image
position in it.

Everything else derived now HAS a companion and is removed: `h{i}_palm_x/y`,
`h{i}_pinch_x/y`, `hands_center_x/y` and `index_center_x/y` take the position rule,
and `h{i}_vel_x/y` takes the extent rule — scaled into world units per second, with
no centring, because a velocity of 0.2 is 0.2 wherever the hand is.

### Face landmark coordinates — composed through the bounding box

A face's landmark points are normalised to that face's BOUNDING BOX, not to the
image (`normalizedPoints`, `DESIGN.md` 2.12), so they cannot take the image-space
rule. They compose first:

    image_x = bbox_x + point_x * bbox_w      image_y = bbox_y + point_y * bbox_h

and then centre and scale like any other position. Written out, a landmark's world
coordinate is

    _tx = point_x * (bbox_w * K)  +  (bbox_x - 0.5) * K

which is one Math CHOP with `gain` and `postoff` as expressions. Every point gets
`_tx`/`_ty` and, with `Lmcoordspx` on, `_px`/`_py`. The RAW box-relative `_x`/`_y`
stay — they are the right space for anything about a face's SHAPE rather than its
position, and the box channels are on the wire so either form recovers the other.

### Page: Coords

| parameter | type | default | what it does |
|---|---|---|---|
| `Resw`, `Resh` | int | 1280, 720 | SOURCE resolution. Drives `_px`/`_py` only |
| `Renderw`, `Renderh` | int | from render1 | RENDER resolution. Drives `_ty`. Not the same number as the source - see DESIGN.md 7 |
| `Orthowidth` | float | from cam1 | match your ortho camera's Ortho Width |

## Where the parameters live: one COMP, three streams

Restructured 2026-08-21. **Every parameter on this page and the ones below belongs
to `/project1/appletd`**, the master COMP, and every stream reads it:

```
/project1/appletd   Vision, Filter, Advanced, Tuning, Attributes, Segmentation
  hands_osc 10000  ->  hands  --+
  pose_osc  10001  ->  pose   --+->  merge_streams  ->  trim_empty  ->  out1
  face_osc  10002  ->  face   --+          |                             258 chans
  status                          the     +->  housekeeping_sel  ->  housekeeping
                                  sc_* channels                       10 chans

  /tmp/...mask.buf ->  seg_mask  ->  seg_fit  ->  outmask     a TOP, 1280x720
  /tmp/...depth.buf -> depth_map -> depth_fit -> outdepth    a TOP, 1280x720
```

**THREE outputs, of two families.** `out1` is the single CHOP output and is what
everything in this document is about. `outmask` and `outdepth` are TOPs - the person
segmentation mask and the monocular depth map - and both arrive by shared memory rather
than over OSC, a different transport for a different kind of data. Nothing about the
channel contract touches either.

**ONE output as of 2026-08-22**, not three. The three streams merge, `trim_empty`
keeps only what the Attributes page has switched on, and that is the COMP's single
output connector. Channel names still say which stream they came from — `h0_`, `p0_`,
`f0_`, `pose_seq`, `face_seq` — so nothing is ambiguous.

One `Smoothing` toggle drives all three filters; one `Orthowidth` drives all three
sets of coordinate spaces. Reference a channel as
`op('/project1/appletd')['h0_index_tip_tx']` off the merged output, or
`op('/project1/appletd/hands')['h0_index_tip_tx']` to reach past the trim into a
stream, or wire from the master's one output connector — an operator outside the COMP
cannot wire to a nested one.

**Paths changed:** `/project1/appletd` is now `/project1/appletd/hands`.

### Page: Vision — everything operational

**The Sidecar page is gone as of 2026-08-21**, and so is the word. It named an
implementation detail: a consumer of this COMP does not care that a separate process
holds the camera, and the page made them read about it before they could pick a
stream. What was operational moved here; the two genuinely diagnostic parameters
moved to Advanced.

| parameter | default | what it does |
|---|---|---|
| `Active` | off | is the capture process running. Replaces the Start and Stop pulses |
| `Restartcapture` | pulse | **Restart** — stop and start in one press |
| `Capturestate` | READ-ONLY | `Running` / `Stopped` / `Requires Restart` |
| `Camera` | `(default)` | a MENU of the real devices. `(default)` lets the engine pick |
| `Listcameras` | pulse | **Refresh Camera List** — re-enumerate and repopulate the menu |
| `Streamhands` | on | run `VNDetectHumanHandPoseRequest` |
| `Streampose` | **off** | run `VNDetectHumanBodyPoseRequest` |
| `Streamface` | **off** | run `VNDetectFaceLandmarksRequest` |
| `Streamsegment` | **off** | run `VNGeneratePersonSegmentationRequest`, and read its mask into `outmask` |
| `Segquality` | `fast` | the mask's quality, and therefore its size and its cost |
| `Streamdepth` | **off** | run Depth Anything V2 and read its map into `outdepth`. **23 ms a frame** - see `docs/DEPTH.md` |
| `Slotassign` | on | h0 is the right hand, h1 the left (DESIGN.md 6.3) |
| `Resw` `Resh` | 1280x720 | the SOURCE image, for pixel space |
| `Renderw` `Renderh` | 1280x720 | the RENDER, for the world-space aspect |
| `Orthowidth` | 1.0 | the camera's ortho width |
| `Screenspaceonly` | off | drop the raw normalised coords (above) |
| `Deleteempty` | **on** | keep only the channels that are actually being computed. `sc_*`, `seq`, `age_ms` and the per-joint `*_conf` are off the output either way — see `housekeeping` |
| `Keeplayout` | off | builders leave existing nodes where you put them |

**`Delete Empty Channels` is why the output is short.** The COMP has ONE output —
`hands`, `pose` and `face` merge into it — and with every stream and space enabled
that is 2,041 channels (MEASURED 2026-08-24: 635 hands, 275 pose, 1,131 face).
Nobody should meet a component that way. So a Select in front of the Out CHOP keeps
only what the Attributes page has switched on: 258 channels in the shipping
configuration. Switch a group on later and its channels
appear; switch it off and they go. That is the intended behaviour, not a
regression — a channel holding a frozen value is worse than a channel that is
honestly absent, because nothing about the number says it stopped moving.

Turn it OFF to see everything, frozen values and all. That is the right setting if
you have a patch that already references a channel from a group you have since
disabled and you want it back without re-enabling the group.

This toggle is the ONE place in the component that deliberately reverses
`DESIGN.md` 6.2's "channels never vanish" rule, and it does so because the audience
is people meeting a hand tracker for the first time.

### The status light, and what each colour actually means

| | when |
|---|---|
| **Running** (green) | a sidecar process is running AND its launch flags still match the panel |
| **Stopped** (red) | no matching process. Checked with `pgrep`, so this is the real thing rather than the toggle's opinion |
| **Requires Restart** (yellow) | a process IS running, but a launch flag has changed since it started |

**The yellow one is why this exists.** Nine labels on this page say "restart to
apply", and until now nothing told you when you had actually triggered that. It cost
a real twenty minutes: `Streamdepth` was switched on while the sidecar was already
running, so the panel said depth was on and the process had never been asked for it.

There was an RGB swatch beside it for about ten minutes, to get real colour into a
parameter page — a TouchDesigner parameter carries `style`, `enable` and `readOnly` and
no colour of its own, so an RGB par rendered as a swatch is the only way. It went
because the cost was three visible float fields and a meaningless label sitting next to
a word that already said the thing.

**It updates on changes and on button presses, not on a timer.** `pgrep` costs
milliseconds — nothing on a click, rude every frame. So a process that dies on its own
reads `Running` until something asks; `sc_uptime_s` freezing is the signal that catches
that, and it is why that channel exists.

**`Active` off reads `Stopped` immediately**, and getting that right needed `stop()` to
WAIT for the process to go. SIGTERM is a request: the sidecar handles it by releasing
the capture session properly, which takes a moment, so anything checking straight
afterwards still sees the pid — and the light read `Running` in green with the toggle
off. It is a bounded wait of up to 1 s on a button press, which is invisible, and it
says so rather than assuming if a process outlives it.

**`Active` is a COMMAND, not a reading.** The process can die on its own — a camera
unplugged, a crash, a kill from a terminal — and the toggle would still read on.
`sc_uptime_s` is the truth, because it arrives FROM the process; `Capturepid` on
Advanced is only what was last started. Reconciling the toggle would mean `pgrep` on
a timer, which costs milliseconds — fine on a button press, rude every frame.

**`Camera` is a substring, and a string rather than a menu on purpose.** MEASURED on
this machine, `Listcameras` returns four devices — the built-in camera, an OBS
virtual camera, a Camo camera and an iPhone. A menu baked at build time goes wrong
the moment something is unplugged, and a menu whose entries no longer exist is worse
than a field that simply does not match. Enumeration opens no device and raises no
permission prompt, so the button is safe to press at any time.

The three stream toggles are LAUNCH FLAGS — read once when the process starts, so
each says "restart to apply" — **and since 2026-08-21 they also gate cooking**, which
takes effect at once. See the Attributes page notes.

**What a stream toggle saves is INFERENCE, not channels.** A disabled stream keeps
every one of its channels, reading zero: TouchDesigner's OSC In CHOP creates
channels as they arrive, so a stream that stopped sending would make its channels
VANISH, and a vanished channel breaks its consumers silently (DESIGN.md 6.2).

**Turning both off does not start anything.** The COMP says so in the Textport
rather than launching a process that opens the camera and runs nothing - which
looks exactly like a working sidecar with nobody in frame.

## The body-pose stream — a separate COMP on a separate port

Built 2026-08-21. `visionpose` is plumbing only: **123 channels, no derived
attributes, no filtering, no temporal channels.** Build it with
`tools/td_build_vision.py`; drive it with the Body Pose Stream toggle, or with
`tools/send_synthetic_pose.py` and no camera at all.

```
pose_n_bodies, pose_seq, pose_age_ms
p<i>_found, p<i>_score, p<i>_conf_median                       for i in 0..1
p<i>_<joint>_x, p<i>_<joint>_y, p<i>_<joint>_conf              19 joints
```

**Every joint also arrives in world and pixel space**, exactly as a hand landmark
does: `p0_root_tx`, `p0_root_px`, `p0_root_ty`, `p0_root_py`. 275 channels in total -
123 from the wire plus 152 transformed - built by `tools/td_add_coords.py` from the
master's one `Orthowidth` and render resolution.

The 19 joints, in channel order: `nose` `left_eye` `right_eye` `left_ear`
`right_ear` `neck` `left_shoulder` `right_shoulder` `left_elbow` `right_elbow`
`left_wrist` `right_wrist` `root` `left_hip` `right_hip` `left_knee` `right_knee`
`left_ankle` `right_ankle`.

**Three things to know before using them.**

**`p0` is the LEFTMOST person**, by a stateless sort on the root joint (the neck if
the root is not confident). There is no chirality for a person, so the partition
that makes `h0` mean one hand has no equivalent here - two people who cross over
exchange slots, and the `two` sweep crosses them deliberately so you can see it.

**Gate on `p<i>_conf_median`, never on `p<i>_score`.** The body observation
confidence is UNMEASURED - hands' equivalent is a measured constant 1.0
(DESIGN.md 2.6) and this one has never been seen, because seeing it needs a person
in frame. It is published as a faithful mirror of the API, nothing more.

**Nothing here is depth-invariant.** Every hand attribute divides through hand size
for exactly that reason; no pose channel does, so a consumer measuring a raw
distance between two pose joints is measuring how far away the person is standing.
`tools/send_synthetic_pose.py depth` is the sweep that shows it.

## The face stream — head pose and a box, on a third port

Built 2026-08-21. The face stream is plumbing plus four computed points: **387
channels on the wire**, no attribute layer. Build it with `tools/td_build_vision.py`;
drive it with the Face Stream toggle, or with `tools/send_synthetic_face.py` and no
camera.

```
face_n, face_seq, face_age_ms
f<i>_found, f<i>_score, f<i>_quality                          for i in 0..1
f<i>_roll, f<i>_yaw, f<i>_pitch                               DEGREES
f<i>_bbox_x, f<i>_bbox_y, f<i>_bbox_w, f<i>_bbox_h            normalised
```

**The bounding box arrives in world and pixel space too**, and the extents follow a
different rule from the positions:

```
f0_bbox_tx = (bbox_x - 0.5) * Orthowidth        a position: centred, so offset
f0_bbox_tw =  bbox_w        * Orthowidth        an EXTENT: not offset
f0_bbox_th =  bbox_h        * Orthowidth * Renderh / Renderw
f0_bbox_pw =  bbox_w        * Resw              pixels
```

A width is 0.22 wide wherever the box sits, so subtracting 0.5 from it would give a
negative size that draws as nothing. 39 channels in total: 23 from the wire plus 16
transformed. **`roll`/`yaw`/`pitch` are not transformed** - there is no yaw in
pixels - and they are smoothed like any other continuous value.

**`roll`, `yaw` and `pitch` are DEGREES.** Vision reports radians; the conversion
happens once, in `appletd/face.py`. If you see values under 2 for a head that
is clearly turned, something has bypassed that conversion.

**`bbox_y` is the BOTTOM edge of the box**, because the box is a CGRect with a
bottom-left origin, like every other coordinate here (DESIGN.md 7). The top edge is
`bbox_y + bbox_h`.

**Gate on `f<i>_quality`, not `f<i>_score`.** Both are UNMEASURED for faces
(DESIGN.md 2.12); `quality` is `faceCaptureQuality`, which Apple documents as a
comparison metric for the same subject, and `score` is the observation confidence
that its hand equivalent turned out to be a constant 1.0.

**`f0` is the LEFTMOST face**, by bounding-box CENTRE - not the left edge, because a
near face has a wide box and would sort left of a face that is genuinely further
left. Two faces that cross over exchange slots; `send_synthetic_face.py two` does
exactly that so the limit is visible rather than surprising.

### The 76 landmark points

MEASURED 2026-08-21 and published: `f<i>_<region>_<nn>_x` / `_y`, over 12 regions.

```
face_contour  17   median_line   10   left_eyebrow  6   right_eyebrow  6
left_eye       6   right_eye      6   left_pupil    1   right_pupil    1
nose           8   nose_crest     6   outer_lips   14   inner_lips     6
```

**87 slots over 76 distinct points, because the regions overlap.** `median_line`
shares points with `nose_crest` (4), `nose` (2), both lip rings (2 each) and
`face_contour` (1); `nose` shares one more with `nose_crest`. Eleven points are
therefore published TWICE, under both of their regions' names - which is the price
of `f0_nose_crest_00_x` instead of `f0_lm43_x`, and the same trade the hand contract
made. If you sum two regions expecting disjoint sets, you will double-count those
eleven.

**These points are normalised to the FACE'S BOUNDING BOX, not to the image.** That
is what `normalizedPoints` means, and it is why they have no `_tx`/`_px` companions:
transforming them with the image rules would put every facial feature in one corner
of the frame. To place one in the image, compose it through the box:

```
image_x = f0_bbox_x + point_x * f0_bbox_w
image_y = f0_bbox_y + point_y * f0_bbox_h
```

The bundle is 12640 bytes, which is more than a default UDP socket will send;
`appletd/osc.py` raises the send buffer, and `MAX_FACES = 2` is the practical
ceiling for one datagram.

`tools/send_synthetic_face.py` MOVES every landmark: `turn` sweeps them across the
box, `tilt` rotates them and `speak` opens the mouth. `tools/probe_face_regions.py`
re-measures the counts on another machine or after a Vision update, and
`--keypoints` adds the report below.

### The four key points

Added 2026-08-24, published beside the regions and computed in the sidecar:

```
f<i>_eye_left_x,  f<i>_eye_left_y            leftPupil, unchanged
f<i>_eye_right_x, f<i>_eye_right_y           rightPupil, unchanged
f<i>_nose_tip_x,  f<i>_nose_tip_y            the point three regions share
f<i>_mouth_x,     f<i>_mouth_y               the mean of inner_lips
```

Box-normalised like every landmark, so they compose through the box the same way -
and unlike the landmarks they DO have `_tx`/`_ty`/`_px`/`_py` companions, computed
beside the bounding box rather than behind `Lmcoordstx`. `Face Key Points` on the
Attributes page swaps them for the 348.

**Two of the four are not points Vision publishes.** Every region except the pupils
is a RING - `left_eye_00`..`_05` trace around the eye and none of them is its centre
- so the mouth centre is the MEAN of the six `inner_lips` points, and the nose tip is
identified rather than indexed.

**The nose tip is the one point that belongs to three regions.** `median_line` runs
down the face, `nose_crest` down the bridge and `nose` around the base, and the
arithmetic of the overlaps above pins exactly one point in all three (nine points sit
in two regions, one sits in three). `appletd/face_types.nose_tip_point` intersects
those three regions on every frame rather than pinning an index, because an index
measured once is unverifiable afterwards and would publish a nostril without saying
so on a Vision that renumbered a region. If the intersection is not exactly one
point, the channel reads 0 and `--keypoints` says why.

**There are no ears.** Vision has twelve landmark regions and none of them is an ear,
so there is nothing to select. This was asked for and refused rather than
approximated.

**`eye_left` is `leftPupil`, whatever side of the frame that turns out to be.** The
name mirrors Vision's; which side of an unmirrored image it appears on is
**UNMEASURED** and `tools/probe_face_regions.py --keypoints` is what answers it.

`Screenspaceonly` deletes the raw `f<i>_eye_left_x` and keeps `f<i>_eye_left_tx`,
like any other channel with a companion.

### The sidecar's own status channels

Four channels on the HANDS port, so they arrive whatever else is enabled:

| channel | meaning |
|---|---|
| `sc_uptime_s` | seconds since the sidecar started sending. **FROZEN means the process is gone** |
| `sc_hands` | 1 when the sidecar really started that stream |
| `sc_pose` | as above |
| `sc_face` | as above. It existed and read 0 before the face stream was built, so the channel list did not have to grow when it landed |

`sc_uptime_s` is the channel that tells "sidecar dead" apart from "stream switched
off", because a disabled stream's own `seq` is frozen as well. And `sc_*` reports
what the process is RUNNING, not what the toggles say: after somebody flips a
toggle without restarting, the toggle is a request and this is the state.

### Two rules the defaults follow

**Every `off` threshold is looser than its `on` threshold.** That gap is the
hysteresis, and it is what makes a gesture end deliberately rather than by
jitter. A build that sets them equal has no re-arm rule and will chatter.

**Every threshold that is a distance is a RATIO of hand size**, never a raw
normalised distance. The units column says `ratio` for exactly those. This is
what makes a pinch tuned at arm's length still work when you lean in.

## Smoothing: one adaptive filter, upstream of everything

The 84 landmark positions pass through a **one-euro filter** before anything else
reads them, so every distance, angle, curl, velocity and bounding box inherits the
smoothing from one place and there is nothing per-channel to tune. Everything that
is not a position — confidences, `found`, `seq`, `age_ms` — passes through
untouched.

**It is TouchDesigner's own Filter CHOP, `type = oneeuro`** — one operator, 0.015
ms. It was first built by hand from a Feedback and twelve Math CHOPs, on a wrong
conclusion that the Filter CHOP could not work on these channels; the hand-built
version cost **0.695 ms across 21 operators** for functionally identical output.
`DESIGN.md` 2.11 has the correction and why the wrong conclusion looked right.
`Dcutoff` is gone because the native filter does not expose it — no loss, since
1.0 Hz is the published default and there was never a reason to move it.

**Why adaptive rather than a fixed cutoff.** Landmark jitter and hand motion occupy
the same frequency band, so a fixed cutoff has to choose: smooth enough to still a
resting hand, and a fast hand lags visibly; responsive enough to track a fast hand,
and a resting one shimmers. The one-euro filter makes the cutoff a function of
estimated speed:

    dx      = (x - x_prev) * rate
    dx_hat  = exp_smooth(dx, alpha(1.0 Hz, fixed))
    cutoff  = Mincutoff + Beta * |dx_hat|
    x_hat   = x_hat_prev + alpha(cutoff) * (x - x_hat_prev)

MEASURED on the hand-built version, where every stage was inspectable: at rest the
cutoff sat at `Mincutoff` 1.5 Hz with alpha 0.136, and a hand crossing the frame in
0.8 s lifted it to 4.35 Hz with alpha 0.313 — **2.3x more responsive on a real
gesture than at rest**, which is exactly the trade a constant cutoff cannot make.
The native filter converges on a still hand **exactly**, where the hand-built one
left a float32 residual of 9e-8.

**The published `beta` of 0.007 is wrong for these channels, and copying it makes
the filter useless.** Every one-euro reference filters PIXELS, where speeds reach
hundreds or thousands of units per second. These channels are normalised 0..1, so
the same motion measures about a thousand times smaller and `1.5 + 0.007 * 1.4`
is no adaptation at all. The default here is scaled for normalised units. It is a
guess, and per-joint jitter is what should really set it (`DESIGN.md` 11).

**One thing to know when tuning:** `dx_hat` is a smoothed *signed* velocity, so an
oscillating fingertip largely cancels itself out — measured, a fingertip pinching
back and forth on 0.6 s cycles peaked at only 0.037 units/s and the filter barely
opened. Sustained motion in one direction is what drives the adaptation, which is
the right behaviour, and it is why the smoothing feels different on a swipe than on
a rapid tap.

## Two caveats that are properties of the system, not of this list

**Per-hand identity depends on the `Slotassign` toggle.** With it ON — the default
— `h0` is the right hand and `h1` the left, every frame, so every per-hand channel
means what it says. With it OFF, `h0` can swap to the other physical hand between
frames and anything with memory measures a hand that changed identity: velocity,
`held`, `dwell`, and every per-hand latch and counter. Measured (`DESIGN.md` 6.3):
with assignment off and two hands crossing, `h0_size` reads the MEAN of the two
hands' sizes, because the smoothing filter averages both into one slot.

Note the consequence of it being on: a single LEFT hand puts nothing in `h0`. Every
`h0_*` channel reads zero and `h1_*` carries the hand. Symmetric two-hand
attributes — `hands_distance`, `hands_center`, `hands_angle`, `e_clap` — are
unaffected either way.

**Angles wrap, and filtering them naively produces spikes.** A hand crossing
±180° makes a smoothed `hands_angle` swing through zero, which reads as a violent
twist. Anywhere an angle is differentiated or filtered, it is unwrapped first, or
sin/cos are filtered and recombined.

**Per-joint jitter is still unmeasured** (`DESIGN.md` 11). It determines how much
smoothing the temporal groups need, and it is measurable from the fixture without
a person present. Worth doing before tuning any velocity threshold.

---

## Implementation

**Stateless maths in one Script CHOP; everything temporal in native CHOPs.**

Every distance, angle, curl, palm centre, bounding box, openness and two-hand
geometry is a pure function of one frame's 137 channels. Built from native CHOPs
that would be hundreds of operators of Shuffle and Rename gymnastics purely to
align channel names before subtracting them, and testable only by eye. Written as
Python it is one operator whose code reads like this document.

The decisive reason is testability. The Script CHOP's cook is a thin wrapper
around a pure function in `appletd/derive.py`:

    derive(values: dict[str, float], params, groups) -> dict[str, float]

which needs no TouchDesigner at all. Feed it a synthetic fist and assert
`h0_openness == 0`; feed it a hand rotated 30 degrees and assert `h0_rotation`
is 30. Every formula above becomes an assertion, checked with no camera and
nobody in frame.

Everything with memory stays native, because TouchDesigner does it properly, it
is inspectable in the network, and it keeps no hidden Python state for a project
reload to treat unpredictably.

**That third reason was tested in 2026-08-21 and is stronger than it reads.** A
prototype holding one group's recurrences in a Python object made the project fail to
SAVE - TouchDesigner pickles operator storage into the .toe, and a class from a module
the builders re-import fails pickle's identity check. Not "reloads unpredictably":
will not save. `DESIGN.md` 2.11 and `BUILD_PLAN.md` step 12.

| need | operator |
|---|---|
| velocity, acceleration | Slope |
| smoothing | a one-euro filter, built from Feedback + Math — see below |
| Schmitt latches and their edge pulses | Feedback + Logic |
| debounce (`Activateframes`, `Gestureframes`) | Count or Trail + Logic |
| refractory windows, dwell | Timer, Count |
| steadiness | Trail + Analyze |
| group gating | `allowCooking` per group COMP, plus a Select on the output |

Cost: the Script CHOP runs main-thread Python, which was never the problem - the
28x starvation measured in `DESIGN.md` 2.8 was a BACKGROUND thread. Comparable
work measured 0.141 ms for 137 channels, and numpy arithmetic over ~40 arrays adds
microseconds. To be measured rather than assumed once built.

`derive()` takes the set of enabled groups so a disabled group costs nothing
inside the Script CHOP as well as outside it.


## The segmentation mask - a TOP, not channels

**`Streamsegment` is the fourth stream toggle, on the Vision page with the other
three.** It is one control, not two: it puts `segment` on the sidecar's command line
AND gates the Script TOP that reads the buffer. The read side and the write side are
always wanted together, and two toggles a letter apart would be a trap. Switching it
off releases the mapping, so a project not using the mask holds no file handle open.

`Segquality` is beside it, and it is a **launch flag** - the request is built when the
sidecar starts, so it says "restart to apply" like the stream toggles do:

| level | per frame | mask | edge |
|---|---|---|---|
| `fast` | **2.21 ms** | 256 x 192 | HARD - only 0 and 255, no soft edge |
| `balanced` | 8.54 ms | 512 x 384 | feathered |
| `accurate` | 30.73 ms | 2016 x 1512 | feathered, and it cannot hold 30 fps |

All of that lands on the same serial queue as hands' 3.41 ms, which is why the default
is `fast` and not Vision's own default of `accurate`. `fast` is not simply a smaller
`balanced`: it has no soft edge at all, so a feathered matte costs 8.54 ms.

### Page: Segmentation - the read side

| parameter | default | what it does |
|---|---|---|
| `Maskfit` | on | undo the anisotropic stretch, so the mask lines up with the camera frame |
| `Masksourcew` `Masksourceh` | 1280 x 720 | READ-ONLY. The source geometry, read out of the buffer's header |

`Maskbuffer` is on **Advanced**, with the other internals: it is a file path that has
to match on both sides, not something a consumer of this COMP chooses. The sidecar
writes there and the Script TOP reads there - the same arrangement `Oscport` has.

**If the mask is black**, the first thing to check is that `seg_mask` is cooking:
`op('/project1/appletd/seg_mask').totalCooks` has to be CLIMBING. An input-less Script
TOP is never dirtied on its own, so it is driven by a `Tick` parameter whose
expression changes every frame - and that expression is gated on `Streamsegment`, so
with the toggle off the operator correctly stops cooking altogether (`DESIGN.md` 2.21).
A `seg_mask` that is 16x16 has never read the buffer; a real mask is 256x192 or larger.

**To see the mask with no camera**, which is also how it was verified:

    ~/.venvs/appletd/bin/python tools/segmentation_probe.py --write-only

then switch `Streamsegment` on. That is exactly the shape the sidecar has.

**Why `Maskfit` exists at all.** Vision's mask does NOT share its input's aspect
ratio: a 1280x720 frame gives a 256x192 mask, and the shape is chosen by orientation
alone - landscape gets 4:3, portrait gets 3:4. The image is stretched anisotropically
to fit, with no letterboxing. So lining the mask up with the camera frame is a
non-uniform scale, and `Maskfit` is what applies it, on the GPU, from the source
geometry the buffer's header carries. Turn it off to get the mask at its native
resolution - which is the right choice if you are going to composite it against
something else that is also mask-shaped.

**What it costs.** About 0.12 ms: `seg_mask` 0.0915 and `seg_fit` 0.0312. A miss - the
writer publishing while the Script TOP is reading - holds the previous frame rather
than blocking or blanking, which for a mask is the right answer and is why the
transport never takes a lock (`DESIGN.md` 2.19, 2.20).


## The depth map - a TOP, and metres if you pin it

`docs/DEPTH.md` is the real documentation: first run, the model download, the pins and
every caveat. This is the parameter table.

| where | parameter | default | what it does |
|---|---|---|---|
| Vision | `Streamdepth` | **off** | run the model and read its map |
| Advanced | `Depthbuffer` | `/tmp/appletd_depth.buf` | the shared file. Must match on both sides |
| Depth | `Depthpinson` | on | apply the fit to the PIXELS - the port of the example's `p`. Off = the model's own numbers |
| Depth | `Depthwindownear` `Depthwindowfar` | 0.4, 3.0 | the fixed metre window the corrected map is drawn on |
| Depth | `Depthunits` | READ-ONLY | which of the two you are looking at |
| Depth | `Depthpincount` | 3 | how many pin rows are in use, 0 to 8. Rows past it are greyed out |
| Depth | `Depthpin1x` … `Depthpin8m` | the example's | the pins. First three from `apple-vision-examples` |
| Depth | `Depthpinsdraw` | off | draw the pins in red on the map. Output becomes RGB |
| Depth | `Depthfit` | on | stretch the map back to the camera's aspect |
| Depth | `Depthfitalpha` `Depthfitbeta` | READ-ONLY | this frame's fit: `Z = 1/(alpha*d + beta)` |
| Depth | `Depthfitpins` | READ-ONLY | how many pins the fit used |
| Depth | `Depthfitresidual` | READ-ONLY | worst disagreement, in metres |
| Depth | `Depthfitchecked` | READ-ONLY | **1 only when the residual means anything** |

**Three things to know before using it.**

**It costs 23 ms a frame** - MEASURED, against hands' 3.41 and a 30 fps frame interval
of 33.3. It runs last on the capture queue, and with it on the camera will drop
buffers. Depth at 30 fps and hands at 30 fps are not both available.

**The map is INVERSE depth and bigger means NEARER**, and it is not comparable between
frames: the model divides by the frame's own maximum, so a hand coming toward the lens
darkens everything else. Build nothing on the raw numbers without pins.

**`Depthfitresidual` lies when `Depthfitchecked` is 0.** Two pins always fit a line
exactly, so with two the residual is 0.000 and checks nothing - which reads as the best
possible news. Gate on `Depthfitchecked` first, then on the residual. Use three pins.

**The pins are a list, not a text field.** `Depthpincount` says how many rows are in
use and the rest are greyed out. The first three default to the values from
`apple-vision-examples/examples/depth/depth.py` - a plausible room rather than a
measurement, and `Depthpinsdraw` is how you see where they landed before dialling the
metres in. Moving a pin needs a restart (the solve is a launch flag); the drawing
updates at once, which is the discrepancy to watch for.

**Converting to metres is yours to do**, and on purpose: `Z = 1/(alpha*d + beta)` from
the two published numbers, in a GLSL TOP or an expression. Doing it here would mean
choosing a clamp and a colour window for every project whether it wants metres or not.

**The model is not in this repository.** 47 MB from Apple's Hugging Face account:
`./tools/fetch_models.sh`, once. `docs/DEPTH.md` has the first-run walkthrough and the
troubleshooting table.
