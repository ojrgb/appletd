# Benchmarks

Every measured figure in this project, in one place.

## The machine, and why that matters more than usual

    MacBook Pro, Apple M4 Pro
    12 cores (8 performance, 4 efficiency), 48 GB
    macOS 26.5.2
    TouchDesigner, via the MCP bridge
    Python 3.11.9 (pyenv, in ~/.venvs/appletd), pyobjc 12.2.2, numpy 2.2.6

**WHICH INTERPRETER, and why it is named.** Every figure below was taken on that
pyenv 3.11.9 venv. From 2026-08-23 the Install button can instead download a
relocatable CPython 3.11.16, so the interpreter a user gets is not necessarily the one
these numbers came from.

Checked on 2026-08-23, both interpreters back to back over the same 60 fixture frames:
the difference is **0.00 to 0.16 ms** across `fast`, `balanced`, `accurate` and depth -
under half a percent, and inside run-to-run noise. Which is what the architecture
predicts: the hot path is Apple framework code on the Neural Engine, the pyobjc C
extension is the same wheel in both, and numpy is pinned to the same version.

**Those comparison numbers are deliberately not recorded here.** They were taken while
the machine was under other load (load average 3.0 to 4.6), so they are evidence that
the two interpreters MATCH and not evidence of what either one costs. Recording them
would put a figure in this file that `docs/STANDARDS.md` 3 would not accept, next to
figures that it would.

**Every inference figure here depends on the Neural Engine.** An M1, an Intel Mac or a
machine where Core ML has quietly fallen back to CPU will not produce these numbers, and
the depth model is several times slower on CPU. Treat the whole file as *what this class
of machine does*, not as a specification.

## How these were taken, because it decides what they mean

Three kinds of number appear below and they are not interchangeable:

- **fixture replay** — `fixtures/hand_clip.mp4`, deterministic, repeatable.
  `docs/STANDARDS.md` §3 only accepts performance claims from this. Every inference
  figure is one of these.
- **forced cook** — a tight loop of `op.cook(force=True)` with the first cooks
  discarded. Isolates one operator's work with nothing else competing.
- **natural gauge** — TouchDesigner's own `cookTime`, read after normal frames. It is
  wall-clock and a *last-value* reading, so it includes whatever else the frame was
  doing. Higher than the forced figure, and closer to what you actually pay.

Where both exist, both are given. Picking the flattering one would make this file
useless.

**Nothing here is a live-camera timing.** Live figures track what is in shot and the
spike measured an 8× spread on identical configurations, so they are not quoted as
results. The one live observation that *is* recorded is a latency, at the bottom.

---

## Vision and Core ML inference — fixture replay

| request | cost/frame | output | notes |
|---|---|---|---|
| hands, `VNDetectHumanHandPoseRequest` | **3.41 ms** | 21 joints × 2 hands | the baseline everything else is compared against |
| segmentation, `fast` | **2.21 ms** | 256 × 192 | HARD alpha — only 0 and 255, no soft edge |
| segmentation, `balanced` | **8.54 ms** | 512 × 384 | feathered |
| segmentation, `accurate` | **30.73 ms** | 2016 × 1512 | cannot hold 30 fps on its own |
| depth, Depth Anything V2 Small | **23.00 ms** | 518 × 392 fp16 | p95 23.27, max 23.33 |

A 30 fps frame interval is **33.3 ms**. Depth alone is 69% of it; depth plus hands is
79%; depth plus `accurate` segmentation is ~162% and cannot work.

Requests run in this order on the one capture queue — cheapest and most-read first, so
whatever is dropped is dropped from the end: `hands`, `pose`, `face`, `segment`,
`depth`.

### Two facts about the depth output worth having

| | |
|---|---|
| distinct fp16 levels per frame | **~7,302** |
| distinct levels an 8-bit read would give | 256 |
| payload | 406,112 bytes (518 × 392 × 2) |

Reading that buffer as 8-bit loses precision twice — once quantising to 256 levels, and
once applying a second per-frame normalisation on top of the `reduce_max` already in the
model's graph. That measurement is why the transport carries fp16.

---

## The pin solve — fixture replay

| | cost/frame |
|---|---|
| solve, 3 pins | **0.111 ms** |
| solve, 8 pins | 0.13 ms |
| solve, 0 pins | 0.0 — skipped entirely |

It was **0.589 ms** before 2026-08-22, and 88% of that was one line. The breakdown is
worth keeping because it is so lopsided:

| part of the solve | cost |
|---|---|
| `max()`/`min()` on the fp16 map, full | 0.5123 ms |
| the same, on a float32 array | **0.0170 ms** |
| the same, every 4th pixel (what ships) | **0.0415 ms** |
| sampling 3 patch medians | 0.0224 ms |
| `polyfit` on 3 points | 0.0115 ms |

**numpy has no fast path for fp16 reductions** — thirty times slower than float32 on the
same data. The frame range is only the scale the conditioning test is expressed against,
so it is taken from every 4th pixel: measured at **0.00% difference** in the resulting
range (0.02% at every 8th), for a twelfth of the cost.

---

## The shared-memory transport

| | |
|---|---|
| reader cost, 256 × 144 u8, writer at 30 fps | median **0.0204 ms**, p95 0.0278, max 0.0487 |
| the same, writer at 60 fps | median 0.0158 ms |
| reader cost, 518 × 392 fp16 (depth) | median **0.0517 ms** |
| retries, 108 reads against a 30 fps writer | 4 |
| retries, 108 reads against a 60 fps writer | 0 |
| **torn frames, two processes, 56,591 accepted** | **0** |

The tearing figure comes from a writer in a separate process running flat out — 1.28 M
frames in 1.5 s — with every frame filled with a byte value derived from its own frame
number, so a torn frame is one holding two values. A single-process test shares an
address space and cannot expose a visibility problem, which is why that shape is the one
in the suite.

It is evidence, not proof: the fence is an uncontended lock acquire/release, not a C11
`atomic_thread_fence`. The residual risk is one bad frame on a stream that replaces it in
16 ms.

---

## TouchDesigner operator costs

### The channel output path

| operator | cost | note |
|---|---|---|
| `merge_streams`, 3 → 1,863 channels | 0.0590 ms | |
| `trim_empty`, 1,863 → 258 (258 literal names) | **0.0438 ms** | |
| `housekeeping_sel`, 10 channels | 0.0037 ms | |

**The Select-versus-Delete measurement**, on the same 1,863-channel input and the same
reduction, is the largest single margin found in this project:

| operator | list | cost |
|---|---|---|
| Delete CHOP | 35 drop patterns | **0.6697 ms** |
| Delete CHOP | 306 literal keep names | **3.3618 ms** |
| Delete CHOP | 6 drop patterns | 0.0772 ms |
| Select CHOP | 306 literal keep names | **0.0555 ms** |
| Select CHOP | one pattern | 0.0313 ms |

The Delete CHOP's cost grows with list length × input channels; the Select's barely
moves. 306 names cost the Select 1.8× what one pattern does, and cost the Delete 43× what
a six-pattern list does.

### The mask path

| operator | forced cook | natural gauge |
|---|---|---|
| `seg_mask` — stat, read, y-flip, copy | 0.0915 ms | **0.2802 ms** |
| `seg_fit` — 512 × 384 → 1280 × 720, GPU | 0.0312 ms | 0.0371 ms |
| `outmask` | — | 0.0042 ms |

### The depth path

| state | forced cook | p95 |
|---|---|---|
| pins OFF — raw map | **0.3709 ms** | 0.4602 |
| pins ON — metres on a fixed window | **0.4835 ms** | 0.6392 |
| pins ON + Visualize — RGB with crosses | **0.8995 ms** | 1.1324 |
| `depth_fit` — 518 × 392 → 1280 × 720, GPU | 0.0040 ms | |

So the pin correction costs **+0.11 ms** and the overlay another **+0.42 ms**. The
overlay was 1.4176 ms until 2026-08-22, when it stopped correcting the map twice and
calling `copyNumpyArray` twice — one correction and one upload per cook.

### The cost of a parameter expression in the pixel path

Measured 2026-08-23 while deciding how `Resw`/`Resh` should learn the camera's
delivered resolution. Same network, same 89-frame window, input moving.

| `Resw`/`Resh` as | `math_px` | `math_py` | whole COMP |
|---|---|---|---|
| a written constant | **0.0094 ms** | 0.0092 | 1.3246 ms |
| an expression reading `sc_src_w` | **0.1431 ms** | 0.1347 | 1.5984 ms |

**+0.27 ms, about a fifth of the COMP, for a number that changes once.** A single
read of that channel is 0.575 us, which is exactly what made the expression look
free. A Math CHOP evaluates its gain expression **once per channel** - about 48 here
- and each evaluation walks a chained indirection, `Math.gain` -> `Vision.par.Resw`
-> the channel read. 48 x that is a tenth of a millisecond, per operator, per frame.

The lesson generalises past this parameter: a per-evaluation microbenchmark says
nothing until you know the evaluation COUNT, and for a CHOP parameter the count is
channels, not cooks.

So the value is written instead, by `reconcile_source_size()` in the launcher, when
the sidecar starts. Back to 0.0109 ms after the revert.

### What each toggle costs, and what it costs to do nothing

Measured 2026-08-24 in a fresh project, and these are the figures the Attributes page
now shows in its own labels.

**Doing nothing was the expensive part.** With the sidecar switched OFF:

| | operators cooking | cost |
|---|---|---|
| before | 230 of 365 | **5.8208 ms** |
| after `face/screen_only` became a pattern | 230 of 365 | 3.8795 ms |
| after `Active` became a cook veto | **0 of 365** | **0.0000 ms** |

An OSC In CHOP cooks whether or not a datagram arrives, so the whole chain ran at
60 fps deriving attributes from channels nothing had touched.

**Per toggle**, chain unfrozen, 259 operators, 4.0929 ms in total:

| toggle | ms | how it was measured |
|---|---|---|
| `Coordstx` (`Screen Space Coords`) | **1.08** | every world half in all three streams, the face's landmark half included |
| `Coordspx` (`Pixel Coords`) | **0.90** | the pixel halves, the same |
| `Presence` | 0.24 | 71 under `temporal`, plus its slice of `derive()` |
| `Gestures` | 0.18 | 49 under `latches`, plus `derive()` |
| `Core` `Contacts` `Pose` `Twohands` `Tilt` | 0.01 each | `derive()` only |
| `Descriptor` | 0.03 | `derive()` only |
| `Depth` | 0.02 | `derive()` only |

Two instruments, because these are two kinds of toggle. A NATIVE group gates a COMP,
so its cost is the operators inside it, from `tools/td_profile.py` across 89 frames. A
DERIVE group changes what `derive()` computes, so it was measured in Python with no
TouchDesigner at all - every group against every-group-but-one, 500 calls, median.

**The face answer**: the face's landmark halves are 1.46 ms of the 1.98, which is
174 points converted into two coordinate spaces. Switch off the space you are not
reading, or switch on `Face Key Points` and keep neither.

**RE-MEASURED 2026-08-24**, after `Lmcoordstx`/`Lmcoordspx` were collapsed into the
two toggles above. What each is worth now, 89 frames, everything cooking:

```
Coordstx  1.0789 = face/lm_world 0.8015 + face/world 0.1330 + hands/dv_world 0.0364
                 + hands/world 0.0579 + pose/world 0.0500
Coordspx  0.9037 = 0.6631 + 0.1158 + 0.0306 + 0.0510 + 0.0432
```

The old pair read 0.82 / 0.68 for the landmark halves and 0.20 / 0.18 for everything
else. Measured again rather than added: the panel's figures come from a run.

### `Face Key Points` — what the swap is worth

Measured 2026-08-24, `tools/td_profile.py` across 89 frames at 60.0 fps, with the
synthetic face and hands senders running and the face stream unfrozen. The toggle
swaps the 348 landmark points for four points per face.

| | `Facekeypoints` off | on |
|---|---|---|
| `face/coords/lm_world` | 0.7751 ms | **frozen** |
| `face/coords/lm_pixels` | 0.6523 ms | **frozen** |
| `face/coords/world` | 0.1306 ms | 0.1813 ms |
| `face/coords/pixels` | 0.1103 ms | 0.1538 ms |
| **the face stream** | **1.9644 ms** | **0.7872 ms** |
| everything cooking | 3.4405 ms | 2.3027 ms |
| operators cooking | 87 | 65 |
| channels on `out1` | 1,082 | 418 |

**1.18 ms saved, and that is the number on the panel.** It is a REVERSE gate — the
only toggle in the component that freezes a group by being switched ON — so its label
says "saves" rather than a cost, because putting `1.18 ms` beside a switch that gives
you 1.18 ms back reads as exactly the opposite of what it does.

**What it costs when it is OFF: 0.1012 ms.** The four points are composed beside the
bounding box rather than in the landmark halves, which is
what lets the cheap half survive when the expensive one freezes - and it means they
are computed whether the toggle is on or not. That is 2.9% of the component with
everything enabled, and it is the price of `f0_eye_left_tx` being there without a
toggle to find first.

| | ms | channels |
|---|---|---|
| the 8 key point branches, world half | 0.0577 | 16 |
| the 8 key point branches, pixel half | 0.0435 | 16 |
| the 16 landmark branches, both halves | 1.3763 | 696 |

Per channel the key points are worse - 3.2 µs against 2.0 - because a 4-channel
operator is mostly fixed overhead. Per FACE they are 43× cheaper, which is the trade
the toggle exists to offer.

**One figure here is unexplained and left as measured**: the two cheap halves cost
about 0.09 ms MORE with the toggle on, on identical work. Fewer operators cooking
changes how TouchDesigner attributes time to the ones that remain; it does not change
the total, which fell by 1.14 ms.

### Where the time actually went, measured 2026-08-24 — it was the filtering

The question was whether the component computes a great deal and throws it away late.
It does, and the throwing away was the expensive part. Everything cooking, 89 frames,
**4.1350 ms total**:

| operator | cost | what it is |
|---|---|---|
| `trim_empty` | **0.5075 ms** | a Select keeping 1,110 of 1,581 channels |
| `hands/screen_only` | **0.4885 ms** | a Delete CHOP with **100 literal names** |
| `hands/derive_chop` | 0.2182 ms | the whole attribute layer |
| `face/coords/lm_world/box_f1_ty` | 0.1918 ms | one landmark branch of eight |
| `face/screen_only` | 0.1319 ms | the same job as hands', as a PATTERN |

Two of the top three remove channels rather than computing them.

**`hands/screen_only`: 0.4885 → 0.2170 ms**, by replacing 100 literal names with 24
patterns. A Delete CHOP's cost is list length × input channels, and face had already
had this fix (2.1595 → 0.1319); hands had not, because the obvious pattern swallowed
`h?_point_x/y`. The list is now one term per leading letter — `h?_w*_x h?_t*_x
h?_i*_x` … — which excludes `d` (the 84 hand-local descriptors) and `point` without a
character class.

**A character class was tried first and TouchDesigner's matcher selected NOTHING with
it.** `h?_[a-ce-oq-z]*_x` — every letter but `d` and `p` — expands correctly under
`fnmatchcase`, so `compact_pattern` chose it, and the build reported 88 channels that
should have been deleted and were not. Three ranges in one class is outside what has
been verified of TD's matcher (`*`, `?`, `[0-9]`). The verifier caught it; nothing
before the live network did.

**`trim_empty` is the other half, and it scales with the KEEP list**, which is the
finding that makes an output-only toggle worth having:

| keep list | input | cost |
|---|---|---|
| 306 names | 1,863 | 0.0555 ms |
| 748 names | 1,581 | 0.3169 ms |
| 1,110 names | 1,581 | 0.5312 ms |

So `One Face Only` — which gates no cooking at all — **saves 0.39 ms** simply by
making the keep list 362 names shorter. Removing a channel anywhere upstream of the
trim is paid back at the trim.

### The whole COMP, by configuration

Sampled over 90 render frames from a scheduled callback, medians of cooks where
`totalCooks` actually advanced (`tools/td_profile.py`).

| configuration | cost | operators cooking |
|---|---|---|
| three streams, every space, nothing gated | 1.6686 ms | 238 / 238 |
| like-for-like after the gating work | 1.7381 ms | 207 / 287 |
| three streams + face landmark world coords | 2.3854 ms | 193 / 287 |
| hands only, world only | 1.1367 ms | 142 / 277 |
| **hands only, both spaces, attribute layer off** | **0.6031 ms** | 29 / 287 |

The last row is the one that says gating works: 287 operators, 29 cooking.

**Gating is not free.** Making the coordinate spaces switchable cost 0.33 ms of extra
COMP boundaries and merges, paid whether anything is gated or not — about **0.11 ms per
COMP boundary** at these channel counts. It pays here only because something almost
always is off.

---

## Channel counts

| | |
|---|---|
| all three streams, every space enabled | **2,041** (635 hands, 275 pose, 1,131 face) |
| the same, with `Screenspaceonly` on | 1,493 |
| shipping configuration, on `out1` | **258** |
| `housekeeping` (`sc_*`, `seq`, `age_ms`) | 10 |
| never on the output (per-joint `*_conf`) | 80 |

---

## The one live observation

With `hands` and `depth` both running off the camera, the camera holds **30 fps** but
`age_ms` sits at **~35 ms** against ~6 ms with hands alone. The frames keep coming; they
are just older. That is the 23 ms of depth inference appearing as latency, and it is what
"depth at 30 fps and hands at 30 fps are not both available" means in practice.

Not a benchmark — a single observation, recorded because it is the thing somebody
notices first and it is easy to mistake for a fault.

---

## Reproducing these

```sh
# inference and the transport, no camera, no TouchDesigner
~/.venvs/appletd/bin/python tools/depth_probe.py
~/.venvs/appletd/bin/python tools/segmentation_probe.py

# the COMP, from inside TouchDesigner
tools/td_profile.py
```

The operator costs above were taken by forced-cook loops through the MCP bridge rather
than by a committed script; `tools/td_profile.py` is the instrument for the cross-frame
figures.

**Keeping this file current is a standing obligation, not a tidy-up.**
`docs/STANDARDS.md` §3 and §4 are the rule: enrich it at the point a number exists,
carry the machine, how it was taken and what it is compared against, and DELETE a figure
that stops being true rather than leaving two. Two numbers that both look measured is
worse than one that is wrong, because there is no way to tell which is which.

Ref: `design/DESIGN.md` §2 is where each measurement is argued rather than listed —
2.14 (COMP cost by configuration), 2.15 (the one output), 2.18 (segmentation), 2.19 (the
transport), 2.20 (the mask into TD), 2.21 (input-less Script TOPs), 2.22 (depth).
