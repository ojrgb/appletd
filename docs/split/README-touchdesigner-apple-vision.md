# touchdesigner-apple-vision

**Apple Vision hand tracking into TouchDesigner. 137 CHOP channels at a steady
30 fps, through one built-in operator, with no Python running in your project.**

[![TouchDesigner 099](https://img.shields.io/badge/TouchDesigner-099%20(2023.11xxx)-informational)](#)
[![macOS only](https://img.shields.io/badge/platform-macOS%2014%2B-lightgrey)](#)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

<!-- 12s loop: hand driving instanced geometry, CHOP viewer visible alongside -->
![demo](docs/media/demo.gif)

```bash
# once
python3.11 -m venv ~/.venvs/visionhands
~/.venvs/visionhands/bin/pip install touchdesigner-apple-vision

# every session
~/.venvs/visionhands/bin/python -m visionhands.sidecar
```

Then drop [`visionhands.tox`](https://github.com/ORG/touchdesigner-apple-vision/releases/latest)
into your network. Wave. You have 21 named joints per hand as channels.

---

## What lands in TouchDesigner

```
n_hands  seq  age_ms
h0_found  h0_conf_median  h0_chirality  h0_score
h0_wrist_x  h0_wrist_y  h0_wrist_conf
h0_index_tip_x  h0_index_tip_y  h0_index_tip_conf     … 21 joints × 2 hands
```

**137 channels, readable names, and the list never changes.** An absent hand
reports zeros with all of its channels still present, because a CHOP whose
channels appear and disappear breaks every downstream reference the moment they
vanish.

The included COMP adds TouchDesigner-native coordinate spaces — 305 channels out,
built from 15 stock operators with no Python in any cook:

| channel | space |
|---|---|
| `h0_index_tip_x` / `_y` | normalised 0..1, origin **bottom-left** — same as TD's UVs, drop straight onto a TOP |
| `h0_index_tip_tx` / `_ty` | world units matching your camera's **Ortho Width** — feed a Geometry COMP's instance translate |
| `h0_index_tip_px` / `_py` | pixels, still bottom-left like TD's TOPs |

## Measured, in a real project rendering at 60 fps

| | in-process Script CHOP | **this (sidecar + OSC)** |
|---|---|---|
| delivered | 13.4 fps | **29.8 fps** |
| inter-arrival median | 69.9 ms | **33.3 ms** |
| worst gap | 203.4 ms | **55.6 ms** |
| jitter (σ) | 20.8 ms | **7.7 ms** |
| gaps over 60 ms | 70 of 89 | **0 of 198** |
| `age_ms` at cook | 99.9 ms | **32.3 ms** |

Cost to your cook: **0.141 ms** to rebuild all 137 channels, 0.078 ms to write
values — under 1% of a 60 fps frame. Wire: 4088 bytes per frame on loopback. No
pixels cross the boundary.

## Why a separate process — the number that decided the architecture

The obvious build is a Script CHOP running Vision on a background thread inside
TouchDesigner. We built it, and measured it:

| where the work runs | `performRequests` | + observation → landmarks | throughput |
|---|---|---|---|
| separate process | 4.38 ms | — | **30 fps** |
| TD's main thread | 2.09 ms | 5.65 ms (p95 18.3) | cook-rate bound |
| **TD background thread** | **58.90 ms** | **190.83 ms** | **5.2 fps** |

**A Python background thread inside TouchDesigner cannot do this work.** Vision
pays a **28× penalty** there purely from reacquiring the GIL after each call, and
the conversion adds 132 ms more because it makes ~126 individual pyobjc property
calls per frame and every one of them waits again. TD's frame loop holds the GIL
in long non-yielding stretches; lowering the switch interval tenfold changed
nothing.

Things we ruled out first, each with a measurement: the camera's adaptive frame
rate (pinned 30 fps, no change), GPU contention (4.38 vs 3.41 ms with TD closed),
the cook itself (0.141 ms), the GIL switch interval, and two camera consumers —
TD's Video Device In TOP and the sidecar read the built-in camera **simultaneously**,
both getting live frames, which is why TD can display the camera while this tracks
it and no image transport is needed.

Full write-up: [`docs/WHY-A-SIDECAR.md`](docs/WHY-A-SIDECAR.md). The losing
in-process build is kept in `experiments/in_process/` as provenance, clearly
marked.

## Install

**1. The sidecar** (its own venv — never TouchDesigner's Python):

```bash
python3.11 -m venv ~/.venvs/visionhands
~/.venvs/visionhands/bin/pip install touchdesigner-apple-vision
~/.venvs/visionhands/bin/python -m visionhands.doctor     # verify before debugging anything
```

**2. The TouchDesigner side**, either way:

- **Drop-in:** `visionhands.tox` from
  [Releases](https://github.com/ORG/touchdesigner-apple-vision/releases). Copy it
  to `~/Documents/Derivative/Palette/visionhands/` to get it in your Palette
  permanently.
- **Build it:** paste [`tools/td_build_comp.py`](tools/td_build_comp.py) into a
  Text DAT → right-click → Run Script. Idempotent; run it as often as you like.
  It creates the OSC In CHOP and the COMP, and reports each step.

**3. Grant the camera.** The macOS prompt attaches to the app that opens the
device — here, your terminal or Python, **not** TouchDesigner.app. If you see no
frames, check System Settings → Privacy & Security → Camera first.

Optional: `LAUNCH_SIDECAR = True` in the setup script starts the sidecar as a
child process and stops it when the project exits.

```
--host 127.0.0.1   loopback only by default, deliberately
--port 10000       must match the OSC In CHOP
--fps 60           send rate, independent of camera rate
--camera "name"    match on localizedName; pick explicitly
--parent-pid N     exit when TouchDesigner does
```

## Liveness — the one thing this route costs you

**An OSC In CHOP holds its last received value forever.** A dead sidecar looks
exactly like a still hand, `age_ms` included. Two native signals, no Python:

```
slope of seq     == 0  →  the CAMERA has stopped
slope of age_ms  == 0  →  the SIDECAR has stopped
```

A Slope CHOP on either into a Logic CHOP gives you a usable alarm. The COMP ships
with both wired to a `alive` channel.

## Gate on the right channel

```
h0_conf_median      ← gate on this
h0_lm*_conf         ← and on per-joint confidence
h0_score            ← NEVER. It is a measured constant 1.0 (336/336 observations).
h0_found alone      ← not enough
```

Per-joint confidence reaches **exactly 0.0** on 11% of samples, and Vision still
hands back a plausible-looking position for those joints. 40.8% of samples sit
below 0.5.

## Known limitations, stated up front

- **macOS only.** Apple Vision is the detector; there is no Windows path.
- **No tracking IDs from Vision.** `h0` versus `h1` is assigned by us — chirality
  first, then wrist proximity, with a grace period through dropouts. Two-hand
  stability is **not yet fully measured**.
- **Two hands maximum**, by contract (`MAX_HANDS = 2`).
- **Landmarks only.** No image comes over the wire; open the camera in TD's own
  Video Device In TOP if you want to see it (measured: both readers work at once).
- **`seq` wraps** every 38 hours at 60 Hz, by design — OSC floats are 32-bit, and a
  wrap shows as one negative slope instead of a counter that quietly stops.
- Tested against **TouchDesigner 099, bundled Python 3.11.15**, macOS 26.5.2, M4 Pro.

## TouchDesigner gotchas we hit so you don't have to

1. **`chop.chan('name')` truthiness is its value.** `x if chop.chan('n_hands') else -1`
   falls through whenever the channel legitimately reads 0.0. Use `is not None`.
2. **Nothing may touch a TD object from a non-main thread.** A diagnostic calling
   `store()` from a worker put up TD's THREAD CONFLICT dialog. Write to a file
   instead.
3. **`__file__` inside a Text DAT is the operator path** (`/project1/text1`), not a
   filesystem path. Deriving a directory from it silently yields `/` and then
   `Errno 30, read-only file system`. Nothing raises; validate the path exists.
4. **Append to `sys.path`, never insert.** Inserting a venv at position 0 shadows
   TD's own numpy 2.1.2 with a different build, against which TD's extension
   modules were not compiled.
5. **The Rename CHOP does not apply a From/To list pairwise.** 21 rules turned all
   126 landmark channels into `h0_wrist_x`, `h0_wrist_x1`, `h0_wrist_x2`… — 137
   plausible channels, all wrong. Wildcard back-references (`*_lm08_*` → `*_index_tip_*`)
   *do* work. We name at the source instead.
6. **A DAT to CHOP takes its DAT by parameter** (`par.dat`), not by a wire — it has
   no CHOP input connector, so `inputConnectors[0]` raises `IndexError`.

## The Ortho Width parameter, and the 0.56 nobody should have to dial

TouchDesigner's orthographic camera has an **Ortho Width** that sets the visible
*width* in world units — at the default 1.0 on a 16:9 render, x spans −0.5..+0.5
and y spans −0.28125..+0.28125. An earlier build normalised to unit *height*
instead. Both are aspect-correct; only one matches TD's camera, and the conversion
between them is exactly `1/aspect = 0.5625` on **both** axes — which is why a user
hand-dialling it landed on "0.56 and 0.55" and wondered where the magic number
came from.

So the COMP takes the camera's own number:

```
tx = (x - 0.5) * Orthowidth
ty = (y - 0.5) * Orthowidth * Resh / Resw
```

The builder reads `cam1`'s actual Ortho Width when there is an orthographic camera
to read, so the common case needs no tuning. Verified by injecting known
coordinates with the sidecar stopped: 0.5 → 0.0; 1.0 → +0.5 / +0.28125; 0.0 →
−0.5 / −0.28125. Zero failures.

## The wire contract

`docs/CONTRACT.md` is the authority: 137 channel names, in order, with units and
origin, plus a `contract_version` channel. Adding channels is a minor version;
renaming or removing one is major. The COMP checks the version and puts an error
on itself if it does not recognise it — a landmark arriving in the wrong channel
looks perfectly plausible on screen, which makes it the most dangerous failure in
this system.

The transport is hand-rolled OSC (~20 lines, no dependency): a bundle of named
float32s, immediate timetag, so 137 values from one frame arrive together and you
can never see half of one frame and half of the next.

## Roadmap

- Body pose and face landmarks (same transport, more channels behind a contract bump)
- Measured two-hand slot stability
- End-to-end latency, photons → channels
- Per-joint jitter with a still hand, which decides whether smoothing belongs here or downstream

## Credits

The detection layer is [**apple-vision-landmarks**](https://github.com/ORG/apple-vision-landmarks)
— use that directly if you are not in TouchDesigner. MIT.
