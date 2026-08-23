# Why it is built this way

The long version of the README's "Why" section. Every decision here was made
against a measurement, and the ones that were reversed are still here with the
reason, because a design document that only records the winning answer is a
worse guide than one that shows the turn.

**Where numbers live.** `docs/BENCHMARKS.md` owns every figure and how it was
taken; `design/DESIGN.md` §2 is where each one is argued in raw form. This file
carries the *argument* and quotes only the load-bearing few. If a number here
disagrees with BENCHMARKS, BENCHMARKS is right and this file is stale.

---

## 1. The measurement that set the shape

Vision is fast. Running it inside TouchDesigner's Python is not. The same fixture
frames, measured three ways while TD rendered a real project at 60 fps:

| where the work runs | `performRequests` | + landmark conversion | throughput |
|---|---|---|---|
| a separate process | 4.38 ms | — | **30 fps** |
| TD's main thread | 2.09 ms | 5.65 ms (p95 18.3) | cook-rate bound |
| a TD background thread | **58.90 ms** | **190.83 ms** | **5.2 fps** |

A Python background thread inside TouchDesigner is starved **28×**. Vision itself
pays that penalty purely from reacquiring the GIL after each call, and the
observation-to-landmark conversion adds a further 132 ms because it makes about
126 individual pyobjc property calls per frame — three per joint, 21 joints, two
hands — and every one waits again.

The live engine showed it more gently: inference reporting 35–48 ms against 3.4 ms
standalone, delivery decaying to 13.4 fps, inter-frame gaps swinging between 15
and 203 ms, which is visibly choppy. With TD's main thread deliberately blocked,
the same engine immediately delivered 30.5 fps.

### What it was not

Each of these was a plausible culprit, and each was eliminated with a measurement
rather than an argument:

- **Not the camera's adaptive frame rate.** The device does advertise 15–30 fps and
  its `activeVideoMaxFrameDuration` does sit at 1/15 s, so auto-exposure is free to
  halve the rate in dim light. Pinning min = max = 1/30 s worked — read back and
  confirmed — and changed delivery not at all.
- **Not GPU contention.** The same fixture in a separate process while TD rendered:
  4.38 ms median, against 3.41 ms with TD closed.
- **Not the cook.** Clearing and rebuilding 137 channels then writing them costs
  **0.141 ms** in real TD, under 1% of a 60 fps frame.
- **Not the GIL switch interval.** Lowering it from 5 ms to 0.5 ms changed nothing,
  so TD holds the lock in long non-yielding stretches rather than releasing it at
  bytecode boundaries.
- **Not two camera consumers.** TD's Video Device In TOP and our engine read the
  built-in camera *simultaneously*, both getting live frames. macOS does not lock
  them out of each other — which also means TD can display the camera while we
  track on it.

### The rule that came out of it

> Nothing may touch a TouchDesigner object from any thread but the main one.

Learned loudly: a diagnostic called `store()` on a DAT from a worker thread and TD
put up a THREAD CONFLICT dialog — *"TouchDesigner objects cannot be referenced from
separate threads. Application may behave unpredictably or terminate."*

---

## 2. A sidecar process

The sidecar owns the camera, runs each Vision request on one serial queue, and
sends channels over OSC on UDP loopback. TouchDesigner never blocks on it, and if
it dies TD keeps rendering with the last values while the status light goes red.

Channels land through a stock **OSC In CHOP** — a built-in operator with no Python
in the cook at all. That is the entire TD-side receive path for landmarks.

### Absent things send zeros

A stream that is switched off still sends its channels, as zeros. TD's OSC In CHOP
creates channels as they arrive, so omitting them makes them *vanish*, and a
vanished channel breaks every downstream reference silently. A zero is a wrong
value you can see; a missing channel is an error you find three sessions later.

### The two ways it does not become an orphan

A `Popen` child on macOS is **not** killed when its parent exits — it is reparented
to launchd and keeps running, holding the camera. So there are two mechanisms:

1. an `onExit` hook in the project, which SIGTERMs the sidecar and waits up to 1 s;
2. the sidecar's own `--parent-pid` watch, checking `os.kill(pid, 0)` every second.

One handles a clean quit, the other handles a crash. Worst case after a hard crash
is about a second of the camera light staying on.

### Requests run cheapest-first

All requests share one capture queue, in this order: `hands`, `pose`, `face`,
`segment`, `depth`. Cheapest and most-read first, so whatever has to be dropped is
dropped from the end. Depth is 23 ms of a 33.3 ms frame; putting it anywhere but
last would starve things that cost 3 ms.

---

## 3. Images move through shared memory, not the socket

OSC is right for a few hundred floats and wrong for a 406 KB depth map.

Images go through a **file-backed `mmap` with a seqlock**. The writer bumps a
sequence number, writes the frame, bumps it again. The reader takes the number,
copies, and checks it has not changed — odd means a write is in progress. No
locks, no blocking, and a reader that is late simply gets the next frame.

Measured: **0.0204 ms** per read at 256×144, and **zero torn frames in 56,591
accepted reads** against a writer in a separate process running flat out.

That is evidence and not proof. The fence is an uncontended lock acquire and
release rather than a C11 `atomic_thread_fence`, and the residual risk is one bad
frame on a stream that replaces itself in 16 ms. The tearing test forks a real
second process on purpose: a single-process test shares an address space and
cannot expose a visibility problem at all.

### Why not TouchDesigner's own shared memory

TD ships a shared-memory protocol, and it is closed to a foreign writer. Two
reasons, both found by reading the compiled headers rather than by guessing:

- `UT_SharedMem`'s lock name is **36 characters** and macOS's `shm_open` limit is
  **31**;
- only the segment's *owner* registers in TD's `TD1zzSharedNameDirectory`, so a
  process that did not create the segment cannot be found.

### fp16 all the way through

Depth Anything V2 outputs `OneComponent16Half` with about **7,300 distinct values
per frame**. An 8-bit transport would give 256.

So the buffer carries fp16 and the Script TOP widens to float32 at the last
moment, because `copyNumpyArray` refuses float16 outright. This mattered more than
it looks: the model has a `reduce_max` at the tail of its graph, so nothing is
comparable between frames, and a second per-frame normalisation on top of it would
have quantised the metric solve to a sixth of its precision.

---

## 4. Metric depth by pinning

The model gives *relative inverse* depth. To get metres, name a few points whose
distance you have measured and solve one affine relationship:

    1/Z = alpha × d + beta

Three tape-measured points, two parameters, `polyfit`. It costs 0.111 ms.

### The residual that lies

**A two-pin fit has a residual of exactly 0.000 and checks nothing.** Two points
always fit a line. That is the most convincing wrong number in the whole system,
and the very first real run produced it — the fixture is a person filling the
frame, so one of three default pins is occluded every frame and the fit falls back
to two.

So the solve carries a `residual_is_a_check` flag, says so in words in its note,
and publishes the number of pins it actually used. If that disagrees with the
number of crosses drawn on the overlay, you have not restarted.

### The 30× fp16 tax

The solve cost 0.589 ms until we found that 88% of it was one line. **numpy has no
fast path for fp16 reductions** — `min`/`max` on the half map was thirty times
slower than the same call on a float32 array. The frame range is only the scale the
conditioning test is expressed against, so it is now taken from every 4th pixel:
measured at 0.00% difference in the resulting range, for a twelfth of the cost.

---

## 5. Things about the platform worth knowing before you hit them

### Vision's mask does not share your camera's aspect ratio

The mask and depth outputs pick their shape from **orientation alone** — landscape
gives 4:3, portrait 3:4 — and stretch anisotropically with no letterboxing. A Fit
TOP sorts it out. Assuming it matches the source does not.

### The render's aspect, not the image's

World-space `_ty` is scaled by the *render's* aspect ratio. Those are the same
number when camera and render match, which is exactly why getting it wrong
survives a casual check — and then a 1280×720 camera in a 1920×1080 render puts
every point in the wrong place.

### An input-less Script TOP cooks once and freezes

It is never dirtied, so it never cooks again, and there is no Cook Type parameter
on a Script TOP to fix it with. The segmentation mask was black for a day because
of this. Every Script TOP here has a custom parameter bound to `absTime.frame`.

The diagnosis was slower than it should have been because the placeholder frame
was 256×192 — byte-identical in shape to a real `fast` mask. It is 16×16 now, so a
frozen TOP looks obviously wrong.

### Three parameter facts that cost a day each

- **Removing the code that creates a parameter does not remove the parameter.**
  A name has to be actively deleted or it haunts the file for ever. Hence
  `RETIRED_PARS` tables in the builders.
- **`append*` on an existing parameter resets it**, and the reset fires a callback
  deferred to end of frame, so writing the value back loses the race. The fix is
  to never re-append.
- **TD pickles operator storage into the `.toe`.** Store primitives only.

---

## 6. Choices we did not make

### C++ custom operator

TouchDesigner ships the SDK, and this is the route people assume is obviously
better. **Two of the three original arguments for it were settled the other way by
measurement.**

The first was "escaping the GIL". That is real — but a separate process escapes it
too, for none of the cost, measured at 4.38 ms while TD renders at 60 fps. The GIL
argues for leaving TD's *process*, not specifically for C++.

The second, and originally the strongest, was "TD needs the camera image as well as
the landmarks, and `TOP_Buffer` upload is where Python gets awkward". That
evaporated: TD's Video Device In TOP and our engine read the same camera
simultaneously, so TD can display the image itself and there is nothing to
transport.

What remains is that **a C++ plugin does not solve the threading problem either**.
`execute()` is called on TD's cooking thread, so blocking there blocks TD exactly
as much as Python would. Derivative's own `CPUMemoryTOP` sample demonstrates the
required pattern — `std::thread`, `std::atomic<bool>`, `join()` on destruct, a
queue that `execute()` drains — which is structurally identical to what the
sidecar already does. The costs are unchanged: an Xcode project per TD version,
bundle signing, slower iteration than editing a Python file live, and a crash that
takes TouchDesigner down with it.

What would still justify it: pyobjc or the venv becoming a deployment liability, or
a need to run Vision on frames **TD itself produced** — a rendered TOP rather than
a camera — where the process boundary would mean pushing pixels rather than pulling
them.

### WebSocket instead of OSC

Loses on two counts, neither of them really about speed.

It is **TCP**, so a late frame blocks the frames behind it. That is the wrong trade
for data which is worthless once it is old; UDP's willingness to drop a frame is a
feature here.

And TD's WebSocket DAT hands you a **main-thread Python callback**. You would be
parsing a few hundred channels per frame in exactly the interpreter §1 is about.
The OSC In CHOP is native and runs no Python at all.

### A metric depth model

Considered and rejected in favour of relative depth plus pinning. A metric model
carries its own scale, which sounds better until the failure mode: it is confidently
wrong in a room it was not trained for, and there is nothing in the output that
tells you so. Pinning is worse on paper and better in practice, because the pins
are yours, the residual is checkable, and a bad fit says it is bad.

---

## 7. Projections, clearly labelled

This project's rule is that a performance claim comes from a fixture replay or it
does not get quoted (`docs/STANDARDS.md` §3). **Nothing in this section clears that
bar.** It is here because these are the first questions anyone asks, and refusing
to speculate is not the same as being honest about what we expect.

### Against MediaPipe Hands

The axes we are certain about are the unexciting ones:

| | this repo | MediaPipe Hands |
|---|---|---|
| install | 4 pyobjc wheels | ~30 MB wheel + pinned protobuf |
| model file | none — Vision ships in the OS | bundled TFLite |
| 3D / z per joint | **no** | **yes** |
| tracking IDs across frames | no — slots assigned by chirality | yes |
| platforms | macOS only | ~everywhere |
| 720p hand latency | 3.41 ms, **measured** | *estimated* 5–15 ms on CPU |

That latency estimate is a guess from the shape of the pipeline — MediaPipe runs
palm detection *and* a landmark regressor through TFLite where Vision is one
request — and should be treated as a hypothesis. **MediaPipe may well win**,
especially with a Metal delegate. If it does, the honest framing is still "no model
file, no wheel, four pyobjc packages, and it ships in the OS".

### A Core ML-adapted MediaPipe

The interesting case, and our expectation is that the win is **smaller than it
looks**. The landmark model is small and the Neural Engine should eat it. But the
palm detector's anchor decode and NMS do not convert cleanly through
`coremltools`, so in practice you move the backbone and leave the decode on the
CPU — and the per-frame Python around a two-stage pipeline becomes the floor
rather than the inference.

### How to settle any of this

Run both on one machine against one clip and publish the table whatever it says. A
comparison published against yourself is the most citable artefact this project
could produce. It is an open task, not a claim.

---

## 8. The generated network

287 operators, and none of them placed by hand. Eighteen scripts build the whole
TouchDesigner network in dependency order, and every one of them **verifies what it
built and prints what it found**.

That habit has caught more than the test suite has:

- a COMP output silently falling from 495 channels to 57 while the builder reported
  success;
- a migration step that ran unconditionally and broke the whole chain the moment
  the COMP grew a TOP output, because `outputConnectors[0]` was no longer the CHOP;
- a toggle that drove nothing for two commits, visible only as one status channel
  reading zero.

The failure mode a generated network protects against is the one where a change
*looks* applied. A builder that prints "207 of 287 cooking, 258 channels out" is
making a claim you can check.

### A Select, not a Delete

The single largest margin found in this project, on the same 1,863-channel input
and the same reduction:

| operator | list | cost |
|---|---|---|
| Delete CHOP | 306 literal keep names | **3.3618 ms** |
| Select CHOP | 306 literal keep names | **0.0555 ms** |

The Delete CHOP's cost grows with list length × input channels; the Select's barely
moves. 60× for choosing the other operator.

### Gating is not free

Making the coordinate spaces switchable cost 0.33 ms of extra COMP boundaries and
merges — about 0.11 ms per boundary — paid whether anything is gated or not. It
pays here only because something almost always is off. With hands alone and the
attribute layer down, 29 of 287 operators cook.
