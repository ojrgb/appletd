# Attributes — why the numbers are what they are

Reasoning cut from `docs/ATTRIBUTES.md` on 2026-09-03, when that file became a
user-facing reference. Nothing here is a channel or a parameter definition; it is the
argument behind them, kept for whoever changes the implementation.

`DESIGN.md` holds the TouchDesigner-level traps. This holds the attribute-level ones.

---

## The proximity mechanism

Pinch, snap, clap, the finger triggers and grab are one mechanism: a distance `d` and
two thresholds, `on < off`.

    d < on   -> state true
    d > off  -> state false
    between  -> state holds

    start pulse = rising edge      end pulse = falling edge

The dead band is both the debounce and the re-arm rule: a start cannot fire again until
the points separate past `off`. No timer, nothing tuned against frame rate.

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

## Why distances are normalised by hand size

A raw normalised distance shrinks with camera distance, so a threshold tuned at arm's
length fires early when you lean in. Dividing by `h{i}_size` — the wrist-to-middle-MCP
distance — makes every distance a ratio of hand geometry, invariant to depth.

The same number is the only depth signal available from 2D landmarks: apparent size goes
as 1/distance. `h{i}_size` is both the normaliser and the z proxy.

---

---

## The trigger grid overlaps the named gestures

`h{i}_index_trigger` watches the same distance as `h{i}_pinching`, and
`h{i}_middle_trigger` the same as `h{i}_snapping`. That is not redundancy to tidy away:
pinch and snap are named gestures tuned by feel, each with their own thresholds; the
triggers are a uniform grid to map eight things to. Either can be retuned without
disturbing the other.

One shared threshold pair, not one per finger. Sixteen sliders for a grid whose entire
value is that all eight behave identically would defeat the purpose. The resting distance
differing per finger changes how far the thumb has to travel, not whether the contact
fires.

---

## Why tilt is magnitude and axis, not pitch and yaw

Recovering pitch and yaw from a 2D projection needs the one thing a projection destroys:
which side of the image plane a point is on. A palm tilted 30 degrees toward the camera
and one tilted 30 degrees away project *identically*, so pitch and yaw cannot be signed —
and publishing them unsigned under those names would misrepresent what they are.
Magnitude plus axis is what the projection actually determines: two honest numbers
instead of two dishonest ones.

`Palmarea` is deliberately set at the measured value rather than above it, because a
small dead zone near zero is a better failure than reporting tilt on a hand that has
none. The first value was a guess of 0.14, which put the ratio over 1.0 and clamped it:
`tilt` read exactly 0 for the first 62 degrees of real rotation — a dead zone big enough
to make the channel useless while looking like it worked.

---

## Channel budget

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

Always on, with no toggle: the 137 raw landmark channels, the 40 finger triggers, the 16
motion channels and the event pulses. Removing a group's channels needs an exact
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

## Output path

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

## Gating semantics

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

---

## Implementation

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

