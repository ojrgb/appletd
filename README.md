# appletd

**Apple's on-device neural networks in TouchDesigner. Hands, body, face, person
segmentation, depth, and more.**

It is 28x faster than running inference inline. [Why it is built this way](#why-it-is-built-this-way).

Maintained with ❤️ by **Omer Jacoby** —
[x](https://x.com/jacodesby) ·
[instagram](https://www.instagram.com/ojrgb/) ·
[linkedin](https://www.linkedin.com/in/omer-jacoby-12a12886/)

Please read the [disclaimer](#disclaimer) before using this component — using it means
you accept it.

[![a hand driving instanced geometry, with the CHOP viewer alongside](docs/media/demo-hands.gif)](https://github.com/ojrgb/appletd/blob/main/docs/media/demo-hands.mp4)

*Click for the full clip in real colour.*

---

## Install

Download [**`appletd.tox`**](https://github.com/ojrgb/appletd/raw/main/appletd.tox),
drop it into your project, press **Install** on its General page, wait for
`Installstate` to read `Installed`, switch `Active` on.

Prefer your own interpreter? Any Python 3.11+ that is not hardened will do; point
`Sidecarpython` at it and nothing is downloaded.

```bash
python3.11 -m venv ~/.venvs/appletd
~/.venvs/appletd/bin/pip install -r requirements.txt
```

> **Do not reach for TouchDesigner's bundled Python.** It is the obvious thing to try
> and macOS refuses to load pyobjc into it — `TouchDesigner.app` carries
> `disable-library-validation` and the interpreter inside the bundle does not, so as a
> subprocess it is an ordinary hardened binary. Nothing to be done from this side.

---

## Compatibility

**Apple Silicon only.** Intel has no Neural Engine, so every figure below is
meaningless there; the installer refuses rather than disappointing you.

| | tested | expected |
|---|---|---|
| chip | M4 Pro | any M-series, slower than these numbers |
| macOS | 26.5 | 12+ for segmentation, 11+ for hands, body, face |
| TouchDesigner | 2025.33070 | 2025+ — the overlays are built from POPs |
| sidecar Python | 3.11.9, 3.11.16 | any 3.11+ that is not hardened |

---

## Usage

Switch `Active` on — the COMP launches the sidecar itself. Working projects to open
instead of building your own: [`examples/`](examples/).

| parameter | does |
|---|---|
| `Active` | starts and stops the sidecar |
| `Camera` | refreshable dropdown |
| `Restart` | needed after changing anything read at launch |
| `Camera Flip` | mirrors the image before Vision sees it |
| `Streamhands` `Streampose` `Streamface` | one Vision request each |
| `Streamsegment` + `Segquality` | person mask: `fast`, `balanced`, `accurate` |
| `Streamflow` + `Flowaccuracy` | optical flow, as a TOP |
| `Streamdepth` + `Depthpins` | depth, and the pins that make it metric |
| `Coordstx` `Coordspx` | world-space and pixel-space channel sets |
| `Facekeypoints` `Onefaceonly` `Fingertipsonly` | shorten the channel list |
| `Show Overlay` per stream | draws the skeleton over the camera image |
| `Output Video` | the camera image, with any overlays on it |

Separate outputs, because `out1` cannot be both a CHOP and a TOP:

    out1        CHOP   every channel
    outmask     TOP    the person segmentation mask
    outdepth    TOP    the depth map, raw or in metres
    outflow     TOP    optical flow, x and y displacement in pixels
    outvideo    TOP    the camera image, with any overlays composited on

The sidecar is also a normal CLI, with no TouchDesigner involved:

```bash
python -m appletd.sidecar --list-cameras
python -m appletd.sidecar --streams hands,depth
python tools/send_synthetic.py clap --period 4    # no camera needed
```

---

## What you get

```
h0_wrist_x          h0_wrist_y          h0_wrist_conf
h0_index_tip_x      h0_index_tip_y      h0_index_tip_conf
h0_pinch_index      h0_curl_middle      h0_spread
hands_distance      hands_center_x      hands_angle_z
```

    hands    21 joints × 2, plus pinches, curls, spreads and two-hand geometry
    pose     19 body joints
    face     76 landmarks across 12 regions, plus four key points and a box
    mask     person segmentation, three qualities
    depth    Depth Anything V2 Small via Core ML, in metres if you pin it
    flow     optical flow, per pixel, in input pixels

An absent hand reports **zeros with all channels still present** — a CHOP whose
channels vanish breaks every downstream reference silently.

| suffix | space |
|---|---|
| `_x` `_y` | normalised, as Vision gives it |
| `_tx` `_ty` | TouchDesigner world — `(x − 0.5) × Orthowidth`, y by the **render's** aspect |
| `_px` `_py` | pixels — `x × Resw` |

Full channel reference: [`docs/ATTRIBUTES.md`](docs/ATTRIBUTES.md).

---

## Why it is built this way

Vision is fast. Running it inside TouchDesigner's Python is not:

| where the work runs | Vision call | + conversion | throughput |
|---|---|---|---|
| a separate process | 4.38 ms | — | **30 fps** |
| TD's main thread | 2.09 ms | 5.65 ms | cook-rate bound |
| a TD background thread | **58.90 ms** | **190.83 ms** | **5.2 fps** |

A Python background thread in TD's process is starved **28×**, purely from reacquiring
the GIL after every pyobjc call. Everything else follows from that.

- **A sidecar process**, so the GIL is not in the way.
- **Landmarks over OSC, images over shared memory** — a 7 MB frame does not fit a datagram.
- **Metric depth by pinning** — the model is relative; two known distances scale it.


---

## Benchmarks

M4 Pro, macOS 26.5. Every figure depends on the Neural Engine.

| request | per frame | output |
|---|---|---|
| hands | **3.41 ms** | 21 joints × 2 |
| segmentation `fast` | 2.21 ms | 256 × 192 |
| segmentation `accurate` | 30.73 ms | 2016 × 1512 |
| depth | **23.00 ms** | 518 × 392 fp16 |


Method and every other figure: [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md). MediaPipe
and transport comparisons are **projections, not measurements**, and are labelled so.

---

## Repo

| | |
|---|---|
| `appletd/` | the package — pure core, engine, sidecar. 729 tests |
| `tools/td_*.py` | builders that generate the TouchDesigner network |
| `docs/` | [ATTRIBUTES](docs/ATTRIBUTES.md) · [ARCHITECTURE](docs/ARCHITECTURE.md) · [BENCHMARKS](docs/BENCHMARKS.md) · [BUILD_PLAN](docs/BUILD_PLAN.md) · [JOURNAL](docs/JOURNAL.md) |

The network is generated, never hand-edited: every builder verifies what it built and
prints what it found. `pytest`, `ruff` and `mypy --strict` on the core.

---

## Disclaimer

**Provided as is, without warranty of any kind**, express or implied. You use it at your
own risk, and the author is not liable for any claim, damage or loss arising from it. The
binding text is in [LICENSE](LICENSE).

**Not for safety-critical use.** Research and creative work. Not designed, tested or
suitable for medical, automotive, security, access-control or any other use where a wrong
answer causes harm.

**It detects people.** Whether you may point a camera at someone, what you have to tell
them, and what you may keep depends on where you are and who they are — none of which
this repository knows. That is yours to answer for, and if the answer matters, it is a
question for a lawyer rather than for a README.

---

## Licence

MIT — see [LICENSE](LICENSE).

**Independent project.** Not affiliated with, endorsed by or connected to Apple Inc. or
Derivative Inc. Apple, Vision, Core ML and macOS are trademarks of Apple Inc.;
TouchDesigner is a trademark of Derivative Inc. [NOTICE.md](NOTICE.md) has the full
statement.

**Depth Anything V2 is not in this repository** — it is fetched at run time. The Small
model is **Apache-2.0**; its Base and Large siblings are **CC-BY-NC-4.0**, so swapping
one in changes your terms. [NOTICE.md](NOTICE.md) has the detail.
