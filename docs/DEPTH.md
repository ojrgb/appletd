# Depth — first run, and what the numbers mean

Monocular depth from one camera, using Apple's Core ML conversion of **Depth Anything
V2 Small**. It runs in the sidecar beside hands, pose and face, and publishes a
518 × 392 fp16 map into TouchDesigner through shared memory.

Read the first two sections before you switch it on, and the third before you believe
a number.

---

## 1. First run — the model is not in this repository

The weights are **47 MB** and Apple already hosts them. A repository meant to be
cloned has no business carrying them: they would be in every clone, every fork and
every diff. So `models/` is gitignored and there is one command:

```sh
./tools/fetch_models.sh
```

It fetches `DepthAnythingV2SmallF16.mlpackage` from
`huggingface.co/apple/coreml-depth-anything-v2-small` with `curl`. **No Hugging Face
account, no token, no `huggingface_hub` dependency** — the files are public. Re-run it
any time; anything already present and complete is skipped, so it is also the repair
path for an interrupted download.

An `.mlpackage` is a **directory**, not a file. The script builds the exact tree Core
ML expects (`Manifest.json` plus `Data/com.apple.CoreML/…`), which is why it fetches
three files rather than one archive.

**Then check it, with no camera and no TouchDesigner:**

```sh
~/.venvs/visionhands/bin/python tools/depth_probe.py
```

That runs the model over `fixtures/hand_clip.mp4`, publishes the maps through the real
shared buffer, reads them back in a second process and reports. It is the fastest way
to find out whether this machine can do depth at all.

### What you should see, and what each number is for

```
writer: inference median 23.00 ms, p95 23.27, max 23.33
writer: that is 69% of a 30 fps frame interval
writer: 7302 distinct fp16 levels per frame (an 8-bit read caps at 256)
reader: read cost median 0.0517 ms
```

- **~23 ms inference** is what this model costs on an M-series Mac. If yours is much
  worse, `--compute cpu` is several times slower than the default `all` — a machine
  that has silently fallen back to CPU is the usual explanation.
- **~7,300 distinct levels.** If you ever see 256, something has quantised the fp16
  map to 8 bits and the pin solve is running on a sixth of the precision it should
  have.
- **The first run compiles the model** — `.mlpackage` is a distribution format and the
  runtime wants a compiled `.mlmodelc`. Core ML does that with no Xcode involved, it
  takes a few seconds, it happens once, and it prints that it is happening. Delete the
  `.mlmodelc` beside the package to force a rebuild.

### Dependencies

`pyobjc-framework-CoreML` is in `requirements.txt`. It is a hard requirement rather
than an extra, because a missing framework that only surfaces when somebody flips a
toggle is the worst possible place to discover a dependency.

---

## 2. Turning it on

| where | parameter | what it does |
|---|---|---|
| Vision | `Streamdepth` — "Depth Map (23 ms/frame)" | run the model, and read its map into `outdepth` |
| Advanced | `Depthbuffer` | the shared file. Must match on both sides |
| Advanced | `Depthpins` | the pins. Empty means no metric claim — see below |
| Depth | `Depthfit` | stretch the map back to the camera's aspect |
| Depth | `Depthfit*` (read-only) | the fit this frame, and whether to trust it |

`Streamdepth` is a **launch flag**: the model is built when the sidecar starts, so it
says "restart to apply" like the other stream toggles. It is one control for both
halves — it puts `depth` on the sidecar's command line *and* drives the Script TOP that
reads the buffer.

**The output is `outdepth`, a TOP.** The COMP has three outputs now: `out1` (channels),
`outmask` (the segmentation mask) and `outdepth`.

### It costs more than everything else put together

| | per frame |
|---|---|
| depth inference, in the sidecar | **23.00 ms** |
| hands inference, for comparison | 3.41 ms |
| a 30 fps frame interval | 33.3 ms |

Depth runs **last** on the capture queue, behind everything a live project reads. With
it on, the camera's frame interval cannot always cover the total and AVFoundation will
drop buffers — visible as the engine's `n_delivered` falling, not as anything failing.
That is the honest trade, not a fault. **Depth at 30 fps and hands at 30 fps are not
both available.** If you need both, expect the hands stream to keep up and depth to
arrive at whatever rate is left.

On the TouchDesigner side it is cheap: `depth_map` **0.2911 ms** (read 406 KB, flip,
widen fp16 → float32, copy) and `depth_fit` **0.0040 ms** on the GPU.

---

## 3. What the map actually is — read this before trusting a number

**It is INVERSE depth, and bigger means NEARER.**

**It is not comparable between frames.** The model's graph ends in a `reduce_max`: the
output is divided by the frame's own maximum, and the maximum inverse-depth pixel is
the nearest thing in shot. So a hand coming toward the lens **darkens every other
pixel in the frame**. That is not the model changing its mind, it is division.

Anything you build on the raw map has to be scale-invariant — or pinned.

### Pinning: how to get metres

The corruption is *affine*, and affine corruptions can be solved away. Give it points
in the room whose real distance you know, and every frame it solves

```
1 / Z  =  alpha * d  +  beta          d = this frame's raw output
```

then metres anywhere is `Z = 1 / (alpha*d + beta)`. Two unknowns, so **two pins
minimum, at different distances**.

Set them in `Depthpins` on the Advanced page, as `X,Y,METRES` triples separated by
spaces, with x and y normalised over the frame:

```
0.06,0.30,2.5  0.94,0.30,2.4  0.50,0.96,1.1
```

or on the command line, `--depth-pins "0.06,0.30,2.5 0.94,0.30,2.4 0.50,0.96,1.1"`.

The solve is **re-run every frame**, in the sidecar, against the raw fp16 map. Caching
it would be exactly the stale-scale problem the correction exists to remove, wearing
the costume of a fix.

`alpha` and `beta` arrive in TouchDesigner as `Depthfitalpha` and `Depthfitbeta`.
Converting the map to metres is a reciprocal and two numbers — a GLSL TOP or an
expression on whatever is reading it. **This project deliberately does not do that
conversion for you**: it would mean choosing a clamp and a colour window on your
behalf, on the main thread, for every project whether it wants metres or not.

### Use THREE pins, not two

**With two pins the residual is always exactly 0.000, and it checks nothing.** Two
points always fit a line. A residual of `0.000 m` on a panel is the most convincing
wrong number in this whole system, so there is a flag for it:

| readout | meaning |
|---|---|
| `Depthfitpins` | how many pins the fit actually used |
| `Depthfitresidual` | worst disagreement, in metres |
| `Depthfitchecked` | **1 only when the residual means something** — i.e. 3+ pins |

Gate on `Depthfitchecked`, then on `Depthfitresidual`. With three or more pins, one
that disagrees by more than `--depth-drop` metres (default 0.5) is thrown out and
`Depthfitpins` drops to reflect it — which is how a pin somebody has walked in front of
stops poisoning the whole frame.

### Where to put pins

- **Somewhere a person will not be.** The defaults in `tools/depth_probe.py` are at the
  left edge, the right edge and the bottom for exactly that reason. On the fixture clip,
  which is a person filling the frame, one of the three is dropped every frame — which
  is the mechanism working, not failing.
- **At middling distances, not on the far wall.** 0.6 m to 2 m already spans most of
  the useful inverse-depth range. Once something near sets the frame maximum, the far
  wall lands in very few levels and rescaling cannot un-quantise it.
- **At clearly different distances from each other.** Pins that span less than 5% of
  the frame's own range are refused, with a note saying so — fitting a slope through
  readings that sit within a hair of each other amplifies their noise without limit.

### The four caveats, plainly

1. **The camera must not move.** If it does, every pin is silently wrong. Nothing here
   can detect it.
2. **The residual is the part of the drift that is not affine**, and no amount of
   pinning removes it. It is the number that decides whether this is good enough for
   whatever you are driving.
3. **`Depthpins` is a launch flag.** The solve runs in the sidecar, so moving a pin
   needs a restart.
4. **Empty pins is a legitimate setting, and it is the default.** It means "publish the
   raw map, make no metric claim". A default pin list would be measurements of somebody
   else's room producing confident metres for yours, which is the most convincing wrong
   number this project could ship.

---

## 4. If something is wrong

| what you see | what it is |
|---|---|
| `no depth model at …` | run `./tools/fetch_models.sh` |
| `depth_map` is 16 × 16 | it has never read the buffer. Is the sidecar running with `depth`? |
| `depth_map` cooks are not climbing | the `Tick` expression; check `Streamdepth` is on (`DESIGN.md` 2.21) |
| `buffer version N, this reader speaks M` | a stale buffer from an older build. The message names the file — delete it |
| the map is there but `Depthfitpins` is 0 | no pins set, or they were refused. The sidecar prints why at startup |
| `Depthfitresidual` is 0.000 and looks perfect | check `Depthfitchecked`. With two pins it is meaningless |
| depth arrives but hands got choppy | expected. 23 ms of inference on a 33 ms frame interval — see §2 |

Ref: `design/DESIGN.md` 2.22 (the measurements), `visionhands/depth.py` (the model),
`visionhands/pins.py` (the solve and its limits), `tools/depth_probe.py` (the check).
