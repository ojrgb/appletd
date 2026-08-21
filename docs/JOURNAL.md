# Journal

One entry per milestone: what was built, what was measured, what surprised us,
what is still unknown. Kept because the numbers in `design/DESIGN.md` 2 need
provenance, and "we changed our mind about this in week two" is not recoverable
from a diff.

---

## Milestone 0 — standards, tooling, and the two things a human has to run
2026-08-20

**Built.** `docs/STANDARDS.md` (comment tiers, code rules, review gate),
`pyproject.toml`, dev venv at `~/.venvs/visionhands` (pyenv 3.11.9, pyobjc
12.2.2), `tools/probe_m1_in_td.py`, `tools/record_fixture.py`.

**Measured — new, and it contradicts the spike.** Running the probe standalone
delivered **48 AVFoundation frames in 2.24 s with no run loop turned by us at
all** — a plain `time.sleep()`, in a bare Python process. The spike believed
"AVFoundation delivers frames via the delegate only while a run loop turns"; that
is wrong. `AVCaptureVideoDataOutput` delivers on a GCD dispatch queue, which
needs no run loop. The 28 fps → 8.4 fps collapse recorded in `DESIGN.md` 3 was
therefore *entirely* GIL starvation from the Python polling loop, and nothing to
do with the run loop's presence. `DESIGN.md` 4.2's contingency thread looks
unnecessary. **Not yet confirmed inside TouchDesigner** — that is what the probe
run answers.

Also measured on the standalone control run, consistent with `DESIGN.md` 2:
Vision first call 4279 ms (model load), warm calls 1.70–1.90 ms on a synthetic
720p grey frame, 2.12 ms median on live camera buffers; 1280×720 requested and
1280×720 delivered.

**Surprised us.** `ruff format` cannot be used: it hardcodes two spaces before a
trailing comment, which collapses the aligned annotation column the comment
standard depends on. `ruff check` is the gate instead.

**Still unknown.** Everything the probe exists to answer, in TD's own process:
does pyobjc load next to TD's Objective-C runtime and its own numpy, and does
delivery work there. No fixture clip recorded yet, so no honest performance
number for this repo's own code exists.

---

## Milestone 2a — the pure core: data contract and coordinates
2026-08-20

**Built.** `visionhands/types.py` (Joint / Hand / LandmarkFrame, the 21-joint
table, the 135-name channel contract, an import-time self-check),
`visionhands/coords.py` (three conversions and one clamp, nothing else),
`visionhands/tests/test_coords.py` and `test_channel_contract.py`. Lint, mypy
strict and 12 tests green.

**Verified, not transcribed.** All 21 joint codes were read out of pyobjc 12.2.2
and compared against the table in `DESIGN.md` 2.3: 21 present, all distinct,
identical to the doc, including the `ThumbIP`=`VNHLKTIP` / `IndexTip`=
`VNHLKITIP` pair and the pinky's internal `P`. The codes are hardcoded in
`types.py` so the pure core stays free of pyobjc; `engine.py` will verify each
one against the framework at start-up rather than trusting the copy.

**Deviations from DESIGN.md 5/6.1, both deliberate.**
- `channel_names()` and `N_CHANNELS` live in `types.py`, not in
  `td/hands_chop.py`. The channel list is a contract between the CHOP and
  everything downstream, and the tests have to assert it without importing
  TouchDesigner.
- `LandmarkFrame.hands` is a FIXED-length tuple of `MAX_HANDS`, with a shared
  frozen `BLANK_HAND` in empty slots — not "0..MAX_HANDS hands" as 6.1 words it.
  A fixed length makes the fixed channel list in 6.2 unconditionally true rather
  than something the CHOP has to remember to pad, and the shared blank keeps the
  no-hand path allocation-free per cook.

**Surprised us.** mypy caught a real mistake in our own test on its first run: a
comparison between a bottom-left pixel value and a top-left one. That is exactly
the class of bug `DESIGN.md` 7 warns about, found by the type system before the
code ever ran. The NewType-per-coordinate-space decision has already paid for
itself.

**Still unknown.** Everything downstream of the pure core. No engine, no camera
path in this repo's own code, no fixture clip, and milestone 1 not yet confirmed
in TD.

---

## Review of M0 + M2a — findings and triage
2026-08-20

Reviewed by a fresh agent against `DESIGN.md` and `STANDARDS.md`, scope
correctness + concurrency. No critical defects. Nine findings, all resolved
below except where stated.

**MAJOR, and the reason the review gate is worth its cost: nothing pinned the
joint table.** The reviewer mutation-tested `types.py` and all 12 tests passed
while the table was corrupted three separate ways — the `VNHLKTIP`/`VNHLKITIP`
pair swapped, the wrist moved from index 0 to index 20, and the pinky's `P`
changed to an `L`. `_self_check()` only tested distinctness, which every one of
those mutations preserves. Worse, the start-up check this journal had promised
for `engine.py` — each row's code against the framework — *cannot* catch a
reorder, because a reordered row stays internally consistent, and order is what
`h0_lm00` means. Fixed by `visionhands/tests/test_joint_table.py`, which pins
both 21-tuples literally, checks the structure finger by finger, and
cross-checks every row against the live framework under `importorskip`. Verified
by re-running all three of the reviewer's mutations plus a subtler
within-finger swap (`index_dip`/`index_tip`): each now fails 3 tests.

**Fixed, correctness.** `startRunning()` moved inside the `try` in both tools —
a raise there skipped the whole teardown, and in the probe that left stage 6
opening a second session on the same device, which is precisely `DESIGN.md` 8's
two-sessions-fighting case. `REPO_ROOT` now really is derived from `__file__`
with a `NameError` fallback, as its comment had claimed while the code was an
unconditional hardcoded path.

**Fixed, honesty of the comments.** The `RecorderDelegate` write path credited
the wrong barrier: the reviewer demonstrated that the `stopping` re-check inside
the lock is what prevents a late callback truncating the fixture, and the
`writer is None and n_written > 0` guard is redundant today. Both are kept, with
the comment now naming the one that does the work. A dead `REPORT_CONF_GATE`
constant whose comment claimed it was in use was removed, and the unnamed
`time.sleep(0.2)` drain became `CALLBACK_DRAIN_S` with its provenance stated in
both tools.

**Fixed, tooling.** The mypy override for `tools/` never bound — `tools/` has no
`__init__.py`, so mypy names those files as top-level modules and the `tools.*`
pattern missed them, leaving them under full strict mode and effectively
unchecked at 88 errors. The bare module names are now listed, with
`disallow_untyped_defs`, `disallow_incomplete_defs` and `disallow_subclassing_any`
relaxed (pyobjc's `NSObject` is untyped, so every delegate subclasses `Any`).
Three real errors surfaced once it bound and all three are fixed:
`ensure_writer` now returns the writer, so the write holds a local that another
thread cannot clear underneath it, and `cv2.VideoWriter.fourcc` replaces the
legacy alias that opencv's stubs do not declare.

**Fixed, standards.** `STANDARDS.md` 2 forbade `except Exception` around native
calls with no exemption, while both capture callbacks legitimately need one — a
Python exception unwinding out of an Objective-C callback is its own crash. The
exemption is now written down, scoped to a callback body, with the obligation
that the recorded error must surface somewhere a human looks, and with the
limitation stated plainly: it cannot catch the native crash it sits beside. Also
added: generated reports are regenerated, never hand-patched.

**Fixed, and it was our own bug report.** `JOINT_INDEX_BY_CODE` is now a
`MappingProxyType`, so `types.py`'s promise that everything it defines is
immutable and thread-safe to read is literally true rather than merely intended.

**Not a review finding — found by the user running the probe.** The in-TD run
produced no report at all. The entry point was guarded by
`elif hasattr(builtins, "op")`, a guess about where TouchDesigner injects its
API, and neither branch fired. `main()` is now called unconditionally: a guard
exists to stop an *imported* module doing work, and nothing imports this file.
The standalone report was regenerated afterwards, which also re-verified the
probe end to end after the teardown edits.

**Deferred, recorded here rather than silently.**
- `STANDARDS.md` 2's module-boundary test (engine ↮ TD, td ↮ pyobjc, no cv2/PIL
  at module scope) has nothing to test yet. It lands with M2b/M3, which is when
  the boundaries first exist.
- Neither tool calls `output.setSampleBufferDelegate_queue_(None, None)` before
  dropping its delegate; both rely on the session's release ordering. Safe in a
  hand-run probe. `TODO(M6)`: `engine.py` must unregister explicitly rather than
  inherit this pattern.

**Also worth recording.** The regenerated standalone run delivered 18 frames in
its 2 s window against 48 the first time. That is camera cold-start latency, not
a throughput change — the first run followed a smoke test that had already
warmed the camera. It is a further illustration of `DESIGN.md` 3: live capture
cannot produce a comparable number, and nothing in section 2 may come from it.

---

## Milestone 1 — PASSED, in TouchDesigner
2026-08-20

Run by the user: `tools/probe_m1_in_td.py` pasted into a Text DAT in
TouchDesigner 099. Full console output captured in the session; the numbers are
promoted into `design/DESIGN.md` 2.5, which is now the citable record.

**The answer is yes, and the Python route stands.** pyobjc 12.2.2 loads inside
TD's own interpreter (3.11.15, cp311, matching the venv's 3.11.9), Vision runs
in-process at 2.06 ms median on live camera buffers, and 720p is delivered as
requested. Nothing about being inside TD made Vision slower than the standalone
spike.

**Two design consequences, both simplifications.**

1. **No run loop, no extra thread.** 48 frames arrived while the main thread sat
   in a plain `time.sleep()`. The spike's `CFRunLoopRun` was never what made
   delivery work — `AVCaptureVideoDataOutput` delivers on a GCD queue, which
   needs no run loop, and the same probe proves it standalone too. Section 4.2's
   contingency thread is deleted rather than deferred. The system owns exactly
   two threads of interest: TD's main thread and the capture queue.
2. **Append-not-insert is now measured rather than argued.** Inside TD, numpy
   resolved to the host's 2.1.2 and not the venv's 2.2.6. That is the outcome
   that keeps TD's own extension modules matched to the numpy they were compiled
   against, and it is the difference between a working install and a crash.
   `DESIGN.md` 11's venv question is closed on evidence.

**New trap, and it cost us a round trip.** Inside a Text DAT, `__file__` IS
defined, and it holds the DAT's *operator* path — `/project1/text1` — not a
filesystem path. The probe's derivation therefore produced `/`, tried to write
`/docs/m1_report.txt`, and got `Errno 30: read-only file system`. A
`except NameError` guard is useless against this, because nothing raises: the
value is simply a path to nowhere. `_repo_root()` now validates that the derived
path is a real file and that the directory above it contains `design/DESIGN.md`
before trusting it. Recorded in `DESIGN.md` 2.5 as a trap in its own right,
because anything else we ever run from a DAT will meet it.

Worth noting what did NOT fail: because `Report` treats an unwritable path as a
reason to fall back to the Textport rather than to abort, the run still produced
a complete, readable result on screen. That non-fatal choice is the only reason
this milestone was answered on the first successful run instead of the second.

**Not measured, deliberately.** The probe's delivered-fps line (21.6) is
dominated by camera start-up inside a 2 s window and is not a throughput figure
— a repeat standalone run gave 18 frames rather than 48 for that reason alone.
No hands were detected because nobody was waving; the probe is a liveness check.
Neither number may be cited as a measurement.

**Still outstanding.** No fixture clip, so this repo still has no deterministic
performance number of its own. Two-hand behaviour remains entirely unmeasured.

---

## Fixture attempt 1 — rejected as a benchmark, kept as a jitter input
2026-08-20

The user recorded a clip with Photo Booth/QuickTime rather than
`tools/record_fixture.py`, at `temp/Movie on 8-20-26 at 11.30<U+202F>AM.mov`.
Measured with a throwaway replay: **1620x1080**, 30 fps, 151 frames (5 s), hand
present in 150/150, max 1 hand, Vision median **4.86 ms** (p95 6.10).

**Rejected as the benchmark fixture, on resolution.** 1620x1080 costs 4.86 ms
against the 3.29 ms of `DESIGN.md` 2.1 at 720p, so nothing measured on it is
comparable to what we already have, and resizing it to 720p would mean the
benchmark measures a resize - which 3 rules out explicitly. Secondary reasons:
5 s rather than 10, and the wrist travels only 0.202 of the frame vertically,
so it exercises almost no pose variety.

**Kept for the jitter measurement that `DESIGN.md` 11 defers.** A near-static
hand at 100% presence is precisely the right input for per-joint jitter, and
jitter is what decides whether downstream smoothing is needed.

**Two side findings.** Chirality came back +1 (right) on all 150 consecutive
frames with no flicker - the first evidence of chirality *stability*, where 2.4
had only a single still. And whether the clip is horizontally mirrored is
**UNVERIFIED**: Photo Booth mirrors by default, the user does not recall which
hand was in shot, so the +1 cannot be checked against ground truth. Not treated
as evidence that chirality is *correct*, only that it is stable.

**New standard, from a check rather than a hunch.** Sampling every 15th frame
with `VNDetectFaceRectanglesRequest` found a face in 7 of 10. A fixture is
committed to the repo by design - it is the provenance for the numbers - so it
cannot be a video of a person. `STANDARDS.md` 3 now requires fixtures to be
framed hands-only, and a clip showing someone stays in `temp/` and cannot be
cited as a benchmark input.

**Also a small trap.** The filename contains U+202F, the narrow no-break space
macOS puts before "AM". Two shell attempts reported the file as missing, and
cv2's "couldn't read video stream" for a nonexistent path reads exactly like a
codec failure. Glob for fixture paths; never retype them.

---

## Fixture accepted, and two corrections to the design
2026-08-20

`fixtures/hand_clip.mp4` recorded with the tool: 1280x720, 30 fps, 284 frames
(9.5 s), hand present in 271/283, three dropout runs of 10/1/1 frames, wrist
travelling 0.930 of x and 0.704 of y, and 63 frames with two hands. Vision median
**3.41 ms**, against the 3.29 ms of `DESIGN.md` 2.1 at the same resolution: two
independent recordings and two toolchains agreeing, so 2.1's numbers are now
confirmed rather than inherited. Promoted into `DESIGN.md` 2.6.

**The fixture is gitignored, deliberately, and my privacy check was wrong the
first time.** I wrote a standard requiring hands-only fixtures and gated it on
`VNDetectFaceRectanglesRequest`. This clip passed that gate with zero faces —
because the head is cropped out of frame — while showing the user from the chin
down in their own home. `VNDetectHumanRectanglesRequest` finds a person in 29 of
29 sampled frames. The lesson is not "use a better detector", it is that a
detector answers "is a person in shot" and not "would the subject mind this being
public", so the standard now requires looking at frames as well. `fixtures/*.mp4`
and `*.mov` are gitignored so the mistake cannot be made by accident: putting a
video of someone into git history is not something to undo later. The user chose
to keep the clip local, so these numbers are reproducible here but not from a
clone, and `DESIGN.md` 2.6 says so.

**Correction 1: `VNHumanHandPoseObservation.confidence()` is a constant 1.0.**
Exactly 1.0 on all 336 observations. `DESIGN.md` 6.2 told everyone to gate
downstream work on `h<i>_score`, which would have been gating on a channel that
never moves — a defect that would have shipped and then looked like a
smoothing problem months later. On the user's decision, `h<i>_score` keeps
publishing Vision's raw value as a faithful mirror (so the day Apple starts
varying it, we see it) and a NEW channel `h<i>_conf_median` carries our own
aggregate. **The contract is now 137 channels, not 135.**

Median rather than mean, and that choice is itself measured: 11.0% of joint
confidences arrive at exactly 0.0, so a mean lets two occluded joints drag a
perfectly good hand down. `median_joint_confidence()` exploits N_JOINTS being
odd and fixed — a 21-element sort and one index, microseconds against a 3.4 ms
budget — and returns 0.0 for an empty tuple so `BLANK_HAND` needs no special
case. Three tests pin the behaviour, including the robustness-to-zeros property
that is the whole reason for the choice.

**Correction 2: per-joint confidence is the real signal, and it bottoms out at
zero.** Over 7056 samples: min 0.000, median 0.685, max 0.945, 40.8% below 0.5,
11.0% at exactly 0.0. `DESIGN.md` 2.4's "occluded joints come back with lower
confidence" is right; what it missed is that lower can mean *zero* while Vision
still returns coordinates that look entirely plausible on screen. 2.4's
0.77-0.92 range was one clean unoccluded palm and does not describe a moving
hand.

**Still open.** Whether the clip is horizontally mirrored is unresolved: both
chiralities appear in it (295 right, 41 left across 336 observations) because
both hands are in shot at times, so the statistics cannot settle it, and the one
frame I inspected was too motion-blurred to read handedness off. Deferred to the
two-hand fixture, where it can be checked against a known pose.

---

## Milestone 2b — engine.py, headless
2026-08-20

**Built.** `visionhands/engine.py`: joint-table verification against the live
framework, observation-to-`LandmarkFrame` conversion, `HandDetector` (shared by
the live and replay paths), `HandEngine` (session lifecycle, idempotent
start/stop, bounded deduplicated error recording, delivered-dimension
assertion). Plus `tests/test_engine_replay.py` (12 tests over the fixture) and
`tests/test_boundaries.py` (the module-boundary rules the M0 review left owed).
39 tests, ruff and mypy clean.

**Replay, over `fixtures/hand_clip.mp4`.** 284 frames, every frame carrying
exactly 2 hands x 21 joints, seq monotonic and gapless, dimensions read from the
buffer rather than from a constant, hand presence 96%, median inference within
budget, coordinates still normalised and unflipped, empty slots all the shared
`BLANK_HAND` object. The replay path shares everything downstream of
`performRequests` with the live path, so this tests the code the camera runs
rather than a parallel implementation.

**Live camera, verified by hand** (not a committed test - a suite that turns on a
webcam is a suite people stop running): 37 delivered, 37 published, 0 dropped,
1280x720 requested and delivered, no recorded errors, no pyobjc object escaping
the capture thread, `stop()` and `start()` both idempotent, a second engine
starting and stopping cleanly afterwards.

**A frame worth recording: `found=True`, `score=1.000`, `conf_median=0.000`.**
Vision returned an observation whose 21 joint confidences were all essentially
zero. `found` and `score` were simultaneously affirmative and worthless, which is
the case `DESIGN.md` 6.2's gating rule now exists for - and the strongest single
argument for having added `h<i>_conf_median` at all. It also sharpens 2.4's "no
false positives observed": observations with no real hand behind them do occur,
they simply carry near-zero joint confidence while the per-hand score stays at
1.0. Whether a hand was genuinely in view during that frame is unknown, so this
is recorded as "found can be affirmative and meaningless", not as a measured
false-positive rate.

**Deliberate gap, labelled in the code.** Slot assignment in
`frame_from_observations` is naive - hands land in whatever order Vision returned
them. Vision has no tracking IDs and no stable ordering, so `h0` will swap
between physical hands. `TODO(M5)` in the source, and two-hand output is
unusable for anything that cares which hand is which until milestone 5.

**Owed item cleared.** `stop()` now calls
`setSampleBufferDelegate_queue_(None, None)` explicitly before dropping
references, which the M0 review flagged as owed here rather than inherited from
the tools' reliance on session release ordering.

**Tooling, tightened as a side effect.** `BLE001` (blind except) is now enabled
in ruff, which turns `STANDARDS.md` 2's "one exemption, and only one" from an
honour-system rule into a tool-enforced one: every `except Exception` must carry
an explicit `# noqa: BLE001` next to its justification. Enabling it immediately
proved one of my own noqa markers unnecessary - the `startRunning()` handler
re-raises as `EngineError`, so it was never a blind except. The probe gets a
documented per-file exemption, since surviving a failing stage to report which
one failed is that file's design rather than an exception to it.

---

## Review of M2b — one critical defect, and a test suite that was not testing
2026-08-20

Twelve findings. The two that matter were both invisible to me and both
confirmed by the reviewer running code rather than reading it.

**CRITICAL: `time.sleep()` is not a barrier, and the consequence was a hand
silently vanishing.** `stop()` waited `CALLBACK_DRAIN_S` and declared the queue
drained. With a consumer slower than 200 ms, `stop()` returned while a delegate
callback was still executing — `_session` already `None`, `running` already
`False`. A following `start()`, which is exactly the TD project-reload sequence
`DESIGN.md` 8 is about, then created a second dispatch queue whose callbacks
entered the SAME `HandDetector`. The reviewer measured two threads inside it
concurrently, and then demonstrated the payload: a frame that detects a hand
single-threaded published `found=False` with all-zero joints, because the other
thread's request overwrote `self._request` between `performRequests` and
`results()`. No exception, no `n_dropped`, no error recorded. A hand that was in
shot simply disappeared.

`HandDetector`'s own docstring warned that sharing it across queues corrupts
Vision's state; the engine's own stop/start path was doing it. Fixed with
`dispatch_sync(self._queue, no-op)` after unregistering the delegate — the queue
is serial, so that is a true barrier rather than a guess, and it needs no
constant. Verified against the reviewer's own scenario: a callback in flight
when `stop()` returned is now False, and peak concurrency inside one detector
across three stop/start cycles is 1 (two distinct OS threads seen over time,
never overlapping). `CALLBACK_DRAIN_S` is kept as a cheap cover for the window
before the barrier, and is no longer load-bearing.

**MAJOR: the engine was immortal, so `__del__` could never fire.**
`AVCaptureVideoDataOutput` does not retain its delegate (measured: retainCount
unchanged), so the engine had to hold it — and the delegate held the engine
back, closing a cycle that the garbage collector cannot break, because pyobjc's
Objective-C subclass instances do not expose their Python attributes to it.
Measured: `gc.collect()` collected 0 and left the engine alive. So a caller who
forgot `stop()` leaked the engine, the session and the camera **permanently**,
which is worse than the "two sessions fighting" `DESIGN.md` 8 predicted, since
the old one could never be reclaimed. The `__del__` safety net was unreachable
in the only state it existed for. Fixed by making the delegate hold a weakref;
verified the engine is now collected when its owner drops it without stopping.

**MAJOR: `start()` reset nothing.** `delivered_px`, `first_frame` and `errors`
all survived a stop/start cycle, so: the delivered-versus-requested assertion —
`DESIGN.md` 3's trap, the one that silently lied twice in the spike — ran once
per ENGINE rather than once per session; `first_frame` gave an immediate false
liveness signal after a restart, on the very mechanism this class advertises as
the way to check liveness without polling; and a fault recurring in the new
session was suppressed as a duplicate of the old one. All three now reset in
`start()`, verified. `_seq` deliberately does not — a consumer should see a
restart as a gap, not as time running backwards.

**MAJOR: five mutants survived all 39 tests.** A y-flip, an x/y transpose, a
joint-index shift, a dropped `float()`, and dimensions read from a constant
instead of the buffer. Four of those are silent corruptions of exactly the kind
`DESIGN.md` 7 calls the most dangerous: the landmarks still track a hand, they
are just wrong. The replay test even claimed the y-flip was covered by
`test_coords.py`, which was false — that file tests `coords.py`'s functions, and
the engine never calls them. Six new tests added; four of the five mutants now
die (2, 3, 2 and 1 failures respectively).

The fifth, dropping `float()`, still survives — **and that is correct.** pyobjc
already returns exact `float` and `int` for these accessors (verified:
`type(point.x()) is float`), so the mutant is semantically equivalent on this
version. The `float()` calls are defensive against a future pyobjc that wraps
them, and the new test asserts the invariant that actually matters —
`type(v) is float`, not `isinstance` — which today holds either way. Recorded
rather than papered over with a test contrived to kill a harmless mutant.

**Fixed, smaller.** A Vision failure raised past the drop counter, so five
consecutive failed frames reported `n_dropped=0` while nothing was published —
now caught by type and counted. `verify_joint_table` checked our rows against
the framework but never the reverse, so a 22nd joint would have been silently
dropped; the added set comparison found a phantom on its first run, because
Vision also exports the bare prefix as a type alias. `max_hands=3` was accepted,
paid for, and silently truncated — now rejected, since MAX_HANDS is the width of
the channel contract and not a preference. `pixel_buffer_from_bgra` accepted an
HxWx3 array that Vision then read out of bounds on, returning `found=True` for
memory it did not own — now validated for shape, dtype and contiguity. The
boundary detector missed relative imports entirely and reported
`from visionhands.td import x` as `visionhands`; both fixed and both pinned by
its own self-test.

**Two comments were confidently wrong, which is worse than missing.** The
lifetime warning on `pixel_buffer_from_bgra` said callers must keep the array
alive or risk use-after-free; measured, pyobjc raises the array's refcount and
it outlives the `del`. True of the C function, false of this binding — and the
real hazard is the shape validation that was missing. And `video_settings`
claimed native 420v avoided a conversion: Vision converts internally either way,
and 420v measured **3.94–4.16 ms against BGRA's 3.26–3.39 ms**, about 20%
*slower*. 420v is kept, because it avoids a host-side conversion of a buffer
nothing reads and 0.6 ms is nothing against 33 ms — but the reason is now the
measured one, and requesting BGRA is recorded as a real option if latency ever
matters.

---

## Milestone 3 — the lock-free handoff
2026-08-20

**Built.** `visionhands/source.py`: `LatestFrameBox` (the single-slot lock-free
box), the `HandSource` protocol, `InProcessSource` (camera-backed) and
`FakeSource` (thread-backed, no hardware). Plus `tests/test_source.py` — 13
tests, all with real threads — and two additions to `test_boundaries.py`. 58
tests total, ruff and mypy clean.

**`source.py` imports no pyobjc at module scope**, and `InProcessSource` imports
the engine lazily inside `start()`. That is what keeps the `HandSource`
interface free of a hard pyobjc dependency, so `td/hands_chop.py` can be written
and tested against it with no camera in the process, and so an out-of-process or
C++ backend stays a transport change rather than a rewrite (`DESIGN.md` 9). The
`HandEngine` type still type-checks properly via a `TYPE_CHECKING` import, which
does not execute at runtime.

Proved rather than asserted: a subprocess test installs a meta-path finder that
raises on every pyobjc import, then imports `source.py`, constructs an
`InProcessSource`, reads from it and calls `stop()`. It also asserts that
`engine.py` FAILS to import under the same blocker — without that, the test
would pass forever if the blocker ever stopped blocking, which is the same
vacuous-test failure the M2b review found in the dimensions test.

**Measured, and promoted into `DESIGN.md` 2.7.**

- Reading the box: median **0.0047 ms**, worst **0.0591 ms** — the worst case
  about 58x cheaper than one Vision inference, which is what a lock held across
  inference would have cost.
- Delivered frame rate by what the main thread is doing: idle **30.0 fps**,
  reading at 60 Hz and yielding **30.0 fps (zero cost)**, tight Python spin with
  no sleep **18.5 fps (−38%)**. So a realistic cook loop is free, and only a
  main thread that never yields degrades delivery.
- Camera warm-up is **~1.5 s**, not the ~15 frames `DESIGN.md` 3 suggests. My
  first end-to-end run measured 6.5 fps and I nearly reported it as starvation;
  it was a 2 s window opened the instant `start()` returned. Re-measuring after
  discarding warm-up gave 30.0 fps. Worth recording as a near-miss: the wrong
  conclusion there would have been "the architecture starves the camera", which
  is both alarming and false.

**Design decision recorded: the age of a source that has never delivered.** If
`age_ms` returned 0 until the first frame, a camera that started and then never
produced anything would look perfectly healthy forever — the opposite of what a
staleness channel is for. So the box records when the producer started, and
`age_ms` measures from the last frame's capture time OR from that start time,
whichever applies. It returns 0.0 only when the producer was never started at
all, which is the one case where "no data" honestly means "none expected yet".

**Test design worth keeping.** The no-torn-reads test embeds a checksum in every
frame — every joint value is derived from `seq` — so a reader that ever observed
a frame assembled from two different writes would see the fields disagree. Zero
torn reads over ~1000s of reads under contention, which is what immutability
guarantees. The value is not today's result, it is that the day someone unfreezes
`Hand` for convenience, this is the test that notices.

---

## Review of M3 — the tests were not testing the milestone
2026-08-20

Eight findings. The lock-free reasoning itself was confirmed correct and
complete for CPython 3.11; what was wrong was everything around it.

**MAJOR, and the most embarrassing: the suite did not test this milestone's
central claim.** The reviewer mutated the code and all 60 tests stayed green
for: unfreezing every dataclass, adding a real `threading.Lock` around both
`publish()` and `latest()`, and deleting `FakeSource.errors` (which breaks the
interface milestone 4 will be written against) — the last one passing ruff and
`mypy --strict` too, because nothing in the repo ever bound a `HandSource`.

The journal entry for M3 claimed "the day someone unfreezes `Hand` for
convenience, this is the test that notices". It was not. Nothing in production
mutates a frame, so no behavioural test can detect frozenness being removed.
Three structural tests now do the work instead:

- `test_the_frame_types_are_actually_frozen` asserts `FrozenInstanceError`
  directly. Immutability is the reason no lock is needed, so it is a contract,
  not a style choice.
- `test_latest_calls_nothing_at_all` disassembles `latest()` and asserts it
  contains no CALL opcode of any kind, and that the box holds no
  synchronisation primitive. A timing test cannot tell an uncontended lock from
  no lock; bytecode can.
- `test_both_sources_satisfy_the_protocol_at_runtime` — `HandSource` is now
  `@runtime_checkable`, and the test binds both sources to it with annotations
  so mypy checks the signatures and `isinstance` checks the members exist.

**MAJOR: `seq` ran backwards across a reload.** `HandEngine` deliberately keeps
`_seq` monotonic across its own stop/start — but `InProcessSource` builds a NEW
engine, so the counter restarted at 1 while the box still served the old
session's frame. Measured by the reviewer on the real camera: **75 → 2**. Any
downstream `seq > last_seq` freshness test silently ignores every frame until the
new engine catches up, and `DESIGN.md` 6.1 promises monotonic. `HandEngine` now
takes `seq_start`, and `InProcessSource` passes the box's last seq. Their probe
initially missed this by comparing only endpoints — a warm camera reached a
higher number in session 2 — which is a good reminder that "the number went up"
is not the same as "the number never went down".

**MAJOR: `FakeSource.stop()` lied, and `start()` then resurrected the orphan.**
`stop()` called `join(timeout=2.0)` and ignored the result, so a slow
`frame_factory` left it returning cleanly with the thread still publishing while
`running` reported False. Then `start()` cleared the shared Event, bringing the
orphan back alongside a new thread: two writers into one box. `stop()` now
raises if the publisher will not stop, keeps `_thread` set so `start()` can see
it via `is_alive()`, and each run owns its own Event. Verified by reviving the
original bug in full — both new tests go red.

Worth recording precisely: the per-run Event is **defence-in-depth, not the
load-bearing half**. Mutating just that back to shared-and-cleared does not
reproduce the bug, because the `is_alive()` guard now catches it. Same shape as
the M0 review's finding about the fixture writer's two barriers, and the comment
says which one does the work.

**MINOR fixes.** `errors` returned `[]` the moment the engine was released,
which is exactly when a caller most wants to know why — measured: a failed start
gave a correctly rising `age_ms` and an empty error list, so a CHOP could say
"stale" but never "stale because there is no such camera". Errors are now
retained across stop and recorded on a failed start. `stop()` nulled `_engine`
before stopping it, so a raising `engine.stop()` left a live capture session
permanently unreachable; the reference is now dropped in a `finally`, after.
`FakeSource.start()` recorded the thread before starting it, so a failed
`Thread.start()` left `running` reporting True and `stop()` raising on the
teardown path. `start()`/`stop()` on `InProcessSource` are now guarded by a
lifecycle lock — never taken by `latest()`, so the read path stays lock-free.

**`age_ms` could go negative, and is now clamped.** It sampled the clock before
the frame, leaving a window where a newer frame appeared to come from the
future: 55 negatives in 7.5M reads against a fast synthetic producer, worst
−6.3 ms. It never happened against the real engine, because Vision's ~3.4 ms
covers the window — but `FakeSource` is what the CHOP will be developed against,
and a negative age reads as *fresher than fresh* to any staleness gate. Reading
the frame first narrows the window; clamping closes it. A negative age can only
ever be a sampling artifact, since every timestamp comes from one monotonic
clock in one process, so nothing real is being hidden.

**A correction to my own comment.** I had written that `+=` is not atomic and
that two writers "would silently lose counts". The reviewer could not lose a
single count on CPython 3.11 with six threads and a 1 microsecond switch
interval, because the eval breaker is not polled between those three bytecodes.
The single-writer rule stays as discipline against an implementation detail
changing, but the comment now says what was actually measured rather than
asserting a race nobody can produce.

**A measurement I nearly published as a defect.** My first version of the
latency test used a producer that published in a tight loop with no yield, and
measured a worst-case read of 6.3 ms against a median of 0.0001 ms. That is not
a lock and not a flaw in the box — it is the GIL switch interval, 5 ms by
default, with a thread that never gives it up. The real capture thread yields
constantly, because it sits inside AVFoundation and Vision with the GIL
released. The test now models a yielding producer and measures a worst case of
~0.03 ms against a 1 ms bound; the pathological number is recorded in
`DESIGN.md` 2.7 as a property of CPython rather than of this design.

Also worth noting how that near-miss happened: my edit to the test's docstring
applied while my edit to its body did not, so for two runs I was reading an
explanation that described code that was not there. Both halves of an edit have
to be verified, not just the one that shows up in a diff first.

---

## Milestone 4 — the Script CHOP
2026-08-20

**Built.** `visionhands/td/bootstrap.py` (sys.path wiring, source lifecycle,
`status()`), `visionhands/td/hands_chop.py` (the callbacks), and
`tools/td_setup_snippet.py` — a paste-into-a-Text-DAT script that creates both
operators, wires the callbacks parameter, starts the camera and reports what it
did. Plus `tests/test_hands_chop.py`: 22 tests against a `FakeScriptOp` double,
with no TouchDesigner in the process. 88 tests, ruff and mypy clean.

**Reading TouchDesigner's own install saved this milestone from a bug I would
not have found by testing.** The shipped stub `tdi/ops/chops/scriptCHOP.py`, the
callback template in `tdutils/DATScripts/`, and the offline help under
`Samples/Learn/OfflineHelp/` between them settled three things:

1. **`onGetCookLevel` matters, and the default is wrong for us.**
   `CookLevel.AUTOMATIC` means "inputs changed AND output being used". This CHOP
   has **no inputs** — landmarks arrive on a background thread — so under the
   default it would cook once and then serve a frozen frame forever while the
   camera ran perfectly. `CookLevel.WHEN_USED` is "every frame while the output
   is being used", which is exactly the semantics wanted: track when something
   needs the data, cost nothing when nothing does. This would have presented as
   "the engine works in tests but the CHOP is stuck", and I would have gone
   looking in the engine.
2. **Channels must be built inside `onCook`.** TD's own docs: calling
   `appendChan()` outside the callback "causes random behavior" unless the
   operator is locked. So the tempting optimisation — append 137 channels once,
   then only write values — is off the table, and the documented
   clear-then-append-every-cook pattern is what we do.
3. **The write idiom.** `scriptOp.clear()`, `numSamples = 1`,
   `appendChan(name)`, then `chan[0] = value`, which is what TD's own examples
   use and which avoids allocating a list per channel per cook.

**Testable without TouchDesigner, deliberately.** The cook logic is a pure
function over a four-method surface — `clear()`, `appendChan()`, `numSamples`,
`rate` — and `FakeScriptOp` implements exactly that, including rejecting a
duplicate channel name (TD would silently rename, which is worse). So the whole
137-channel contract is verified before it ever meets a camera: values aligned
one-for-one with names, every value a float, absent hands publishing zeros with
all channels present, the channel set byte-identical with and without a source,
and no growth across repeated cooks.

**A cook can never raise.** An exception inside `onCook` shows up as a broken
operator in the project. The useful behaviour when the source is missing or
misbehaving is a full set of zeros with a telling `age_ms`, so the channel
references survive and the failure is visible — tested with a source that raises
from every method.

**New sentinel: `AGE_NO_SOURCE_MS = -1.0`.** A negative age is impossible from a
real source (`source.py` clamps at zero), so it distinguishes "nothing is wired
up" from "the camera has gone quiet". Those need different responses — one is a
project mistake, the other a dead engine — and a downstream gate can now tell
them apart.

**The `td/` boundary rule stopped being vacuous.** It has existed since M2b with
nothing to check; it now asserts that all three files import no pyobjc, plus a
count check so a renamed directory cannot quietly make it vacuous again. The
subprocess blocker test now also imports the CHOP layer and cooks a full
137-value channel set with every pyobjc import raising.

**Still needs a human.** That TD calls these callbacks at all, that
`CookLevel.WHEN_USED` behaves as documented, and that 137 channels actually
appear in the operator. No amount of test-doubling substitutes for running it.

**Not related to the code: no TouchDesigner MCP is available to this session.**
Checked `claude mcp list` (39 servers, all claude.ai connectors) and the
`mcpServers` entries in `~/.claude.json` (`stripe`, `jira`, `atlassian`). A
newly added server also needs a session restart before it appears, and this
session cannot run an OAuth flow. So milestone 4's verification stays manual for
now.

---

## The TouchDesigner MCP, the frozen CHOP, and TOP input measured
2026-08-20

**We have direct TD access now.** The user had installed `touchdesigner-mcp`
(8beeeaaat, v1.4.4) as a Claude Desktop extension bundle — which is why
`claude mcp list` and `claude_desktop_config.json` both showed nothing: UI-installed
connectors live in `~/Library/Application Support/Claude/Claude Extensions/`.
TouchDesigner's own web server was already listening on 9981 (`lsof` found it;
a `/dev/tcp` probe had said otherwise, which was wrong — zsh has no `/dev/tcp`).
Registered the same server for Claude Code in `.mcp.json`, and verified the
handshake works: 14 tools including `execute_python_script`, `create_td_node`
and `get_top_image`. It is also drivable directly over stdio without waiting for
a session restart, which is how everything below was measured.

**The CHOP was frozen, and the cause was a real trap.** Diagnosed in one query:
137 correct channels, no errors, `age_ms` = 18 ms — but `totalCooks = 1`. It
cooked once and served a frozen frame while the camera ran perfectly.

`onGetCookLevel` lived in `hands_chop.py`, an imported module, and looked
`CookLevel` up on `builtins`. Measured in the live TD:

    import td; td.CookLevel   ->  MISSING
    builtins.CookLevel        ->  absent
    builtins.op               ->  absent
    td.op                     ->  present
    td.noiseTOP / td.textDAT  ->  present

So **TouchDesigner injects `CookLevel` into a DAT's globals only.** Not builtins,
not the `td` module — while OP *types* and `op` itself ARE on `td`. A module
cannot see it by any route, so the function returned None and TD fell back to
`AUTOMATIC`: "inputs changed and output being used". With no inputs, that is one
cook, ever. `onGetCookLevel` now lives in the callbacks DAT and calls
`cook_level()` in the module for the decision. ALWAYS rather than WHEN_USED,
because this is a device input and should track the camera whether or not a
consumer is wired up yet. Verified after the fix: **287 cooks in 4 s**, `seq`
advancing at ~25 fps.

The M4 reviewer's prediction was also correct: creating a Script CHOP
auto-creates a docked callbacks DAT, and the snippet pointed `par.callbacks` at
its own, orphaning it. Destroyed.

**End-to-end staleness, measured at the cook: `age_ms` 65–69 ms.** Not yet
investigated. At ~25 fps delivery that is roughly 1.6 frame intervals, of which
Vision is ~3.4 ms — so most of it is camera pipeline latency. Worth a look before
anyone calls this low-latency.

**TOP input: viable, and here are the numbers.** All measured in the live TD.

| operation, 720p | median | p95 | max |
|---|---|---|---|
| `TOP.numpyArray()` | 0.203 ms | 0.861 | 4.511 (GPU stall) |
| `TOP.numpyArray(delayed=True)` | 0.222 ms | 0.262 | 0.316 |
| float32 RGBA → uint8 BGRA | 4.794 ms | 5.170 | 7.012 |

At 1080p the readback is 0.551 / 0.495 ms respectively — still sub-millisecond.
`delayed=True` costs one frame of latency and removes the stall entirely, which
is the better trade for a realtime overlay.

**The readback is cheap; the conversion is not.** TD always returns **float32**
regardless of the texture's GPU format, so a conversion is unavoidable. The
cheapest variant measured is 3.65 ms (drop alpha, reverse to BGR). Vision does
accept float buffers directly — `128RGBAFloat` and `64RGBAHalf` both work — but
on real frames they are SLOWER than BGRA (6.00 and 7.76 ms against 4.35), so
converting is still the right move.

A first measurement said float was *faster* at 2.70 ms. That was wrong twice
over: random-noise input (no hand, so trivial work) and a single
`VNSequenceRequestHandler` reused across three formats. Which produced its own
finding: **a sequence handler rejects a change of input pixel format
mid-sequence** — it returned error -12905 for BGRA purely because float buffers
had gone through it first. Any code that switches between camera and TOP input
needs a fresh handler.

**So the architecture for TOP input**: the cook does the readback ONLY (~0.2 ms
on TD's main thread) and hands the array to the worker thread, which converts
and runs Vision off-thread, publishing into the same `LatestFrameBox`. That
keeps the main-thread cost at 0.2 ms against the current camera path's 0.005 ms,
and both stay far inside a frame budget. Doing the conversion and inference
inline in the cook instead would cost ~8 ms — half of a 60 fps frame — and
violate DESIGN.md 4.1.

UNMEASURED and worth checking before building it: whether the array returned by
`numpyArray()` stays valid once the cook returns, or whether TD reuses that
buffer on the next call. If it is reused, the main thread needs a copy
(~3.7 MB memcpy) before handing it over. The `delayed=True` semantics — "the
image that was current on the previous call" — hint at internal buffering, so
this needs settling rather than assuming.

---

## Why it was choppy: the GIL, and §2.2 was half wrong
2026-08-20

The user reported the TD output looking choppy and laggy against a smooth
standalone script. It was not the camera, not the GPU, not the cook, and not
TouchDesigner doing anything wrong. Full numbers are promoted into `DESIGN.md`
2.8; the short version is that a Python background thread inside TD gets starved
by 28x, and `DESIGN.md` 2.2's headline conclusion needed a correction rather than
a footnote.

**What the sampler showed.** Recording one row per frame inside TD: cooks at a
steady 60.2/s, but camera frames arriving at 21.4 fps and then 13.4 fps, with
inter-arrival gaps of median 48 ms swinging from 15 ms to 203 ms and `age_ms` at
the cook of 100 ms median. Jitter of that size is what "choppy" means - a 203 ms
gap is three missing frames.

**Four wrong hypotheses, each killed by a measurement**, which is the part worth
remembering:

1. *My own leftover Video Device In TOP was stealing camera capacity.* Removing
   it made delivery WORSE (21.4 → 13.4 fps), which killed the theory and
   revealed the trend was monotonic decay over the session.
2. *The camera was halving its rate for auto-exposure.* Plausible and half-true:
   the device really does advertise 15-30 fps with `activeVideoMaxFrameDuration`
   at 1/15 s. Pinned min = max = 1/30 s, read it back to confirm it took - and
   delivery did not change at all. Kept the pin anyway, since an adaptive rate is
   still wrong for tracking, and `TARGET_FPS` now makes it explicit.
3. *Our own cook was hogging the GIL with 137 appendChan calls.* Measured in real
   TD: the full rebuild costs **0.141 ms**, under 1% of a frame. Comprehensively
   wrong, and worth noting the M4 reviewer's 54.6 µs estimate against a fake op
   was the right order of magnitude.
4. *GPU/ANE contention with TD's rendering.* The same fixture in a separate
   process while TD rendered: 4.38 ms against 3.41 ms with TD closed. Real but
   trivial.

**What it actually is.** The MCP's `execute_python_script` runs on TD's main
thread, which handed me the decisive contrast: in the *same process at the same
moment*, `performRequests` costs 2.09 ms on the main thread and 58.90 ms on a
background thread. Adding the observation→Hand conversion takes the background
figure to 190.83 ms - 5.2 fps - because it makes ~126 pyobjc property calls per
frame and each one waits for the GIL again. Blocking TD's main thread for two
seconds made the live engine deliver 30.5 fps instantly.

Lowering `sys.setswitchinterval` from 5 ms to 0.5 ms changed nothing, so TD is
not holding the GIL at bytecode boundaries where a shorter quantum would help -
it holds it across long non-yielding stretches.

**Why §2.2 missed it.** That measurement used a 100%-Python spin loop as "a
deliberately pathological stand-in for TD's main thread" and found the worker
halved. The spinner was not pathological enough in the way that matters: it
yields constantly. TD's frame loop does not. The lesson is about the shape of a
stand-in rather than its intensity.

**Also disproved, usefully.** TD's Video Device In TOP and our engine read the
built-in camera *simultaneously*, both receiving live frames - so the user's
inability to open it was local to their node, and TD can display the camera
while we track it. That makes an out-of-process design cheaper than expected:
two independent readers, no pixel transport at all, and only 137 floats
(548 bytes) per frame to move.

**A dialog I caused.** A diagnostic worker of mine called `store()` on a DAT from
a background thread and TD raised THREAD CONFLICT - "objects cannot be
referenced from separate threads. Application may behave unpredictably or
terminate." My fault, in throwaway code, and the shipping engine is safe by
construction since it imports no TD at all. Re-ran the measurement writing to a
file instead. `DESIGN.md` 2.8 now carries this as a hard rule, because the next
person to write a diagnostic will reach for TD storage exactly as I did.

Cleaned up after myself: diagnostic nodes destroyed, switch interval restored.

---

## Out-of-process transport: OSC proven, with three gotchas
2026-08-20

Prototyped the transport in the live TouchDesigner before designing around it.

**OSC In CHOP works, and needs no Python in TD at all.** Created an
`oscinCHOP` on port 10000, hand-rolled an OSC bundle from the shell (the
encoding is 20 lines - padded address, `,f` typetag, big-endian float) and sent
all 137 contract names. TD received **137 channels with the exact contract names
in order**, correct values, no errors. No Script CHOP, no callbacks DAT, no cook
cost: the operator does it natively in C++. Bundle is 3480 bytes, so ~104 KB/s
at 30 fps, comfortably inside loopback's 16 KB datagram limit.

That is a better outcome than the current in-process design even ignoring the
GIL: the entire TD-side surface becomes one built-in operator.

Considered and rejected: `sharedmeminCHOP`. Lower latency in principle, but it
requires reproducing TouchDesigner's shared-memory header layout from Python -
undocumented and version-sensitive - to move 3.5 KB per frame that UDP loopback
already delivers in tens of microseconds.

**Three gotchas found by measuring rather than by shipping.**

1. **OSC floats are 32-bit, so raw timestamps are unusable.** `time.monotonic()`
   IS comparable across processes on macOS - verified, TD read 580737.438
   bracketed by shell readings either side, same boot epoch - which is a genuinely
   useful property. But at that magnitude float32 resolution is 1/16 s = 62 ms,
   so a raw timestamp cannot carry millisecond age. (A first test showed only
   0.32 ms error, which was luck: 580737.4375 happens to be exactly
   representable.) The sidecar therefore sends `age_ms` computed at send time,
   where float32 error is 0.000002 ms.

2. **`seq` is exact in float32 only to 2^24 = 16,777,216**, which is **6.5 days**
   at 30 fps. After that it silently stops incrementing - the worst kind of
   failure, since everything keeps working and nothing is wrong until a long
   installation quietly freezes its own staleness detection. The sidecar will
   send `seq` modulo 2^23.

3. **A dead sidecar leaves the channels frozen, not zeroed.** The OSC In CHOP
   holds its last received value indefinitely, so "the process died" looks
   identical to "nothing is moving" - including `age_ms`, which freezes at
   whatever it last was. Liveness has to be derived on the TD side: a Slope CHOP
   on `seq` reads zero when the sidecar stops, which is detectable with no Python
   at all. Worth stating in the README, because the in-process design got this
   for free and the out-of-process one does not.

**No pixel transport is needed.** Since TD's Video Device In and our engine read
the built-in camera simultaneously (measured earlier today), TD can display the
camera itself while the sidecar tracks it independently. That removes the whole
Route B frame-pushing problem: two independent readers, 3.5 KB per frame of
landmarks, and nothing else crossing the boundary.

---

## The sidecar, built and measured: 13 fps → 30 fps
2026-08-20

**Built.** `visionhands/osc.py` (minimal OSC encoding, no dependency),
`visionhands/sidecar.py` (the process), `tools/td_setup_osc.py` (the TD side, now
one operator), and tests: `test_osc.py` pins the wire format byte for byte,
`test_sidecar.py` binds a real UDP socket and decodes actual datagrams with an
independently-written decoder. 105 tests, ruff and mypy clean.

**Measured through the same sampler as the complaint, before and after:**

| | in-process | sidecar + OSC |
|---|---|---|
| camera | 13.4 fps | **29.8 fps** |
| inter-arrival median | 69.9 ms | **33.3 ms** |
| worst gap | 203.4 ms | **55.6 ms** |
| jitter (sigma) | 20.8 ms | **7.7 ms** |
| gaps over 60 ms | 70 of 89 | **0 of 198** |
| `age_ms` | 99.9 ms | **32.3 ms** |

33.3 ms median inter-arrival is exactly 30 fps, and zero gaps over 60 ms is what
"smooth" means. The engine, capture thread and lock-free box are unchanged - they
were never the problem, only their host was.

**The TD side shrank to one built-in operator.** No Script CHOP, no callbacks
DAT, nothing of ours in a cook. `channel_values()` moved from
`td/hands_chop.py` into `types.py` so names and values live together and the
sidecar does not import the TouchDesigner layer; `Sidecar` takes an injectable
`HandSource`, which is the first real payoff of that seam - the whole send path
is testable with no camera.

**Two defects the gates caught before the user could.**

- `os.kill(pid, 0)` raises `OverflowError`, not `ProcessLookupError`, for a pid
  outside the OS range. Unhandled, a typo'd `--parent-pid` would have killed the
  sidecar's main loop. A pid that cannot exist is not a live parent, so it now
  counts as gone.
- The Sidecar held its source as a `HandSource`, and mypy correctly refused
  `source.box` in the tests, since publishing is not part of the interface a
  consumer sees. The fixture now keeps the concrete `FakeSource`.

**`pin_frame_rate` reported its own failure honestly**, which is exactly why it
reads back rather than trusting: the camera accepts the minimum frame duration
and silently keeps its 1/15 s maximum. The sidecar prints
`frame rate not pinned: asked for 30 fps but the camera reports min 0.0333 s /
max 0.0667 s` every status line while running at a steady 30 fps. Left as-is:
the number is honest, the behaviour is fine, and a fix would be guessing at
AVFoundation's ordering rules.

**A TouchDesigner gotcha worth writing down.** `chop.chan('name')` returns a
Channel whose **truthiness is its value**, so `x if chop.chan('n_hands') else -1`
falls through whenever the channel legitimately reads 0.0. It cost me two
diagnostic round trips convincing myself a working system was broken. Use
`is not None`.

**Liveness is the one thing this route costs.** An OSC In CHOP holds its last
value forever, so a dead sidecar looks like a still hand. Both signals are
available natively: slope of `seq` is zero when the camera stops, slope of
`age_ms` is zero when the sidecar stops. Documented in `tools/td_setup_osc.py`
and printed by it.

Cleaned up after myself: prototype and diagnostic nodes destroyed, only
`visionhands_osc` and the older Script CHOP pair remain in the project.

---

## Readable channel names, and the coordinate COMP
2026-08-20

The user asked for a COMP after the OSC In CHOP that (a) turns `h0_lm08_x` into
readable names and (b) converts to TouchDesigner coordinates. Built (b);
moved (a) to the source instead, which is a better answer than the one requested
and worth explaining.

**Naming moved to the sidecar.** `channel_names()` now emits
`h0_index_tip_x` rather than `h0_lm08_x`. The original rationale - fixed-width
names, with JOINT_NAMES carrying the mapping - was a bad trade: every consumer,
human or operator network, had to do the translation, and "which one is lm08" is
exactly the question a name should answer. Cost: the bundle grew from 3480 to
4088 bytes. Worth it.

**The rename-in-TD route was built first, and it taught us three things before
being thrown away.**

1. TouchDesigner's Rename CHOP DOES support wildcard back-references:
   `*_lm08_*` -> `*_index_tip_*` renames h0 and h1, and `_x`, `_y` and `_conf`,
   from a single rule. Verified live.
2. It does NOT apply a space-separated From/To list as pairwise rules. The first
   replacement is applied to everything the whole From list matches, so 21 rules
   turned all 126 landmark channels into `h0_wrist_x`, `h0_wrist_x1`,
   `h0_wrist_x2`... - TD deduplicating by suffix. That failure is silent in the
   sense that it produces 137 plausible channels.
3. The working alternative is the Rename CHOP's second input, a name reference.
   One can be built with no cook-time Python from a Table DAT through a DAT to
   CHOP - and note the DAT arrives by PARAMETER, `par.dat`, not by a wire: a DAT
   to CHOP has no CHOP input connector, so `inputConnectors[0]` raises
   `IndexError: list index out of range`, which is what cost the debugging time.

That last route works, but it renames BY POSITION - the mapping would depend on
channel order matching, two hops from where the order is decided. Naming belongs
at the single source of truth, so the sidecar does it.

**What the COMP does now**, in 15 built-in operators with no Python in any cook:

| channel | space |
|---|---|
| `h0_index_tip_x` / `_y` | unchanged: normalised 0..1, origin bottom-left |
| `h0_index_tip_tx` / `_ty` | centred on zero, x scaled by aspect - drop into a Geometry COMP translate |
| `h0_index_tip_px` / `_py` | pixels, still bottom-left like TD's TOPs |

305 channels out: the 137 in, plus tx/ty/px/py for all 42 joints. Source
resolution is a COMP parameter (`Resw`/`Resh`, default 1280x720) rather than read
from the stream, because the wire contract deliberately does not carry width and
height and a parameter solves it without a contract change.

**Verified by injection, not by waving.** Stopped the sidecar, sent a bundle with
known coordinates - centre, both edges - and checked all twelve conversions:
0.5 -> tx 0.0 -> px 640; 1.0 -> tx +0.8889 -> px 1280; 0.0 -> tx -0.8889 -> px 0,
and the y equivalents. Zero failures. That is a deterministic check of the
arithmetic, which watching a hand move could never be.

---

## The 0.56 the user had to dial in: unit height versus unit width
2026-08-20

The user reported having to re-range both `tx` and `ty` by about 0.56 to make the
overlay land on the hand. Rather than theorise, inspected their network over MCP:

    cam1     projection = ortho, orthowidth = 1.0
    render1  1280 x 720
    geo1     instancing, tx = h0_wrist_tx, ty = h0_wrist_ty
    math1    fromrange -1..1  torange -0.56..0.56
    math2    fromrange -1..1  torange -0.55..0.55

**TouchDesigner's Ortho Width sets the visible WIDTH in world units.** At the
default 1.0 on a 16:9 render, the visible space is x in [-0.5, +0.5] and y in
[-0.28125, +0.28125] - the image normalised to unit WIDTH.

My `tx`/`ty` normalised it to unit HEIGHT: y in [-0.5, +0.5] and x out at
+/-0.8889. Both conventions are aspect-correct and only one matches TD's camera.
The conversion between them is exactly **1/aspect = 9/16 = 0.5625**, applied to
BOTH axes - which is why both needed the same fudge, and why 0.56 looked like a
magic number rather than a ratio.

Fixed by making the scale the camera's own number: an `Orthowidth` parameter,
defaulting to TD's own default of 1.0, with

    tx = (x - 0.5) * Orthowidth
    ty = (y - 0.5) * Orthowidth * Resh / Resw

Verified by injection with the sidecar stopped: 0.5 -> 0.0, 1.0 -> tx +0.5 and
ty +0.28125, 0.0 -> tx -0.5 and ty -0.28125. Zero failures. The builder also now
reads `cam1`'s actual Ortho Width when there is an orthographic camera to read,
so the common case needs no tuning at all.

**Worth telling the user: their y fudge was 2.2% out.** 0.55 against the correct
0.5625, which is invisible in the middle of frame and shows as ~1.5 px of drift
at the top and bottom edges - exactly the kind of error that hand-dialling
produces and that a derived number does not. Their x value of 0.56 was within
0.5% of right.

The lesson for the design: "screen space" is not one space. The COMP now takes
the parameter that disambiguates it rather than picking a convention and hoping.

---

## Sidecar lifecycle on the COMP, and a process-matching bug I shipped and caught
2026-08-20

The user asked whether the sidecar can be stopped from the components we built.
It could not - TouchDesigner did not own the process - so the COMP now has a
Sidecar parameter page: **Start Sidecar**, **Stop Sidecar**, **Print Status**,
and a read-only **Sidecar PID**. An Execute DAT inside the COMP also stops it on
project exit, which pairs with the sidecar's own parent-pid watch: that one
handles a TouchDesigner crash, this one handles a clean quit.

Stop deliberately finds ANY running sidecar rather than only one this COMP
started, because during development it gets started from a terminal, and that is
exactly when being able to stop it from the UI matters.

**The bug: my first version SIGTERMed the shell that was grepping for it.**
`pgrep -f visionhands.sidecar` matches the pattern anywhere in a full command
line, so it matched the very `pgrep` invocation I was using to check on it - and
`stop()` killed it. My own tool call died with exit code 144. On a user's machine
that could have been their terminal, their editor with the file open, or a `tail`
on the log.

Fixed with a two-stage match, and it took three attempts, each caught by testing
against deliberate decoys rather than by reasoning:

1. Confirm each candidate with `ps -p PID -o comm=,command=` - **wrong**, because
   macOS truncates `comm` to 16 characters. A venv interpreter comes back as
   `/Users/omer/.ven`, whose basename is `.ven`, so "python" matched nothing and
   Stop silently found no processes at all. That failure is invisible: a Stop
   button that never finds anything looks like a Stop button on an already-stopped
   process.
2. Use `command=` alone and substring-test for `-m visionhands.sidecar` -
   **still wrong**: a python running `-c "x='-m visionhands.sidecar'"` matches.
3. Require `-m` and the module name to be **adjacent argv tokens**, with argv[0]
   a python. Verified against three decoys - a shell echoing the string, a python
   with it inside a `-c`, and a `tail` on a path containing it - all rejected,
   the real sidecar found.

The lesson is not "be careful with pgrep". It is that a function whose job is to
send signals to processes it identified by string matching deserves adversarial
tests before it runs anywhere near a user's machine, and that I only found the
first bug because I ran it.

Also worth noting: `par.pulse()` is ASYNCHRONOUS. Reading `Sidecarpid`
immediately after pulsing Start returns the old value, because the callback has
not fired yet. That is not a bug, but it made the first verification look like a
failure when the process had in fact started correctly.

---

## Review of the sidecar: the same aspect mistake, one level deeper
2026-08-20

Fifteen findings. The wire format came out clean - cross-checked byte-for-byte
against `python-osc`'s independent encoder with zero mismatches across 40
address/value combinations, `channel_values()` aligned with `channel_names()` at
all 137 positions, the `seq` wrap exact at every boundary, and coordinate
precision worst-case 0.00004 px at 1280 wide. Sixteen mutations of the encoder
and contract were all caught. What was not clean was the process and the COMP.

**CRITICAL, and it is the 0.56 bug wearing a different hat.** `ty` divided by the
SOURCE aspect (`Resh/Resw`) where TouchDesigner uses the RENDER aspect. The
Camera COMP docs derive an ortho camera's vertical extent from the rendered
view's resolution (`aperture_y = resy/resx * aperture_x`), so the visible
half-height is `Orthowidth/2 * Renderh/Renderw`. The source image's resolution is
a different number that belongs only to the pixel branch.

They are equal in the setup this was built against - a 1280x720 camera rendered
to a 1280x720 TOP - which is exactly why using one for both looked right. Render
the same source into a square 1080x1080 and `ty` lands at 56% of where it should;
into portrait 720x1280, at 32%. Fixed with separate `Renderw`/`Renderh`
parameters, read from `render1` when it exists. Verified by driving all three
render shapes and checking `ty` at y=1.0: 0.28125, 0.5, 0.888889, all exact.

Twice now the same class of error: two different aspects collapsed into one
number, invisible because the test setup made them equal. Worth naming as a
pattern rather than fixing twice and moving on.

**MAJOR: `start()` threw away a shutdown request.** `running = True` was set
unconditionally after `source.start()`, so a SIGTERM arriving during the ~1.5 s
camera warm-up was discarded. With TouchDesigner's launcher that is not
cosmetic: TD calls `terminate()`, the sidecar ignores it, the 3 s wait expires,
TD sends SIGKILL - which skips `stop()` and leaves the capture session to process
death rather than releasing it. Exactly the DESIGN.md 8 hazard the flag-only
handler was built to avoid, reached through the front door on any fast reload.

**MAJOR: `start()` sat outside `run()`'s try**, so a failed camera open never
reached `stop()` - socket left open, source never stopped, despite HandSource
promising `stop()` is safe whether or not `start()` succeeded. The promise exists
so `run()` can lean on it and `run()` did not.

**MAJOR: `run()` has no tests at all.** Five mutations survive with 105/105
green, including `if False and self.send_once():` - the sidecar can be made to
send nothing whatsoever and the suite does not notice. Also survivable: deleting
the parent-pid check from the loop, removing the teardown `finally`, and dropping
`socket.close()`. Not yet fixed; it is the next piece of work.

**Fixed alongside:** `age_ms` was read from the box separately from the frame, so
a publish landing between the two lines shipped frame N's seq with frame N+1's
age - measured claiming 100 ms fresher than the frame it carried, which is the
mistake `LatestFrameBox.age_ms` was rewritten to avoid, reintroduced one layer
up. The encode now happens outside the `OSError` guard so a length mismatch
surfaces instead of being swallowed. `stop()` closes the socket in a `finally`.
The status and parent deadlines reset instead of catching up (a 600 s stall
produced 300 spurious log lines). `--parent-pid 0` is rejected, because
`os.kill(0, 0)` signals the whole process group and always succeeds, making the
watch unfireable.

**And the builder was resetting the user's tuning.** `appendInt`/`appendFloat`
replace a parameter's attributes on rebuild, so assigning `.val` unconditionally
snapped a tuned `Orthowidth` back to 1.0 - putting a correctly-aligned overlay
back off-target by re-running the tool meant to align it. Defaults are now set
always, values only on creation, and prior tuning is restored.

**The reviewer also caught the pgrep regex point independently**: `-f` takes a
regex, so the `.` in `visionhands.sidecar` matches a slash and would hit
`nvim .../visionhands/sidecar.py`. Already covered by the two-stage `ps` check
committed earlier - argv[0] must be a python and `-m` must be adjacent to the
module name - which rejects an editor. Two people finding the same hazard from
different directions is a good sign about the check that now guards it.

---

## Attributes, part one: synthetic hands and the stateless maths
2026-08-20

**Built.** `visionhands/synth.py` (parameterised synthetic hands),
`visionhands/derive.py` (the pure stateless half of docs/ATTRIBUTES.md),
`tests/test_derive.py` (36 tests), `tools/td_add_derive.py`. 141 tests total,
ruff and mypy clean.

**Verified live in TouchDesigner**, driven by injected synthetic hands with the
camera sidecar stopped: two open hands 0.30 apart at size 0.10 gave
`h0_size` 0.1, `h0_palm_x` 0.35, `h0_openness` 1.0, `h0_g_open` 1.0,
`hands_distance` 3.0, `hands_center_x` 0.5, `hands_symmetry` 1.0 - every value
exactly what the geometry says. 171 derived channels, COMP output 476.

**The open performance question is answered: 0.210 ms median** for the whole
Script CHOP (p95 0.327, max 0.335), which is 1.3% of a 60 fps frame. The
stateless-maths-in-Python decision holds, and main-thread Python was never the
problem - the 28x starvation in `DESIGN.md` 2.8 was a background thread.

**Four calibrations that came from measuring rather than assuming.** The
generator's "full curl" bent 75 degrees per joint, which left a generated fist
reporting curl 0.74 - so the generator and the derived channel disagreed and no
gesture threshold could be asserted. 108 degrees puts it at 1.0. The thumb needs
150, because it has one fewer segment and two bends cannot fold as tightly as
three; that same fact is why `g_fist` now tests only the four long fingers, since
requiring the thumb would have made the most basic gesture the least reliable.
`g_peace` needed a test that the V is actually open, or two extended fingers held
together reads as a peace sign. And three earlier corrections to the generator
itself - normalising by joint index 10 instead of 9, an x offset on the middle
finger tilting the rotation axis by 1.35 degrees, and a guessed palm centroid
landing 0.021 short - all found by asserting exactness rather than plausibility.

**The absent-hand trap is the first test in the file** and it would have shipped:
all joints at (0,0) means every distance reads 0, below every engage threshold,
and size reads 0 so every normalised distance is 0/0. Losing a hand would have
fired pinch, snap and clap simultaneously while pushing NaN into TouchDesigner,
where it spreads silently. Found by talking through the Schmitt trigger, not by
running anything.

**Recorded as a test rather than fixed:** gestures are not mutually exclusive. A
pointing hand with the thumb up genuinely is also a gun shape, and which reading
wins belongs to the project rather than to this layer.

**Still to build**, in order: the native temporal network (Slope for velocity,
Lag/Filter for smoothing, Feedback+Logic for the Schmitt latches and their edge
pulses, Count/Timer for debounce, refractory and dwell), the group COMPs with
`allowCooking` gating and the five parameter pages, and `tools/send_synthetic.py`
to drive gesture sequences into TD without a camera.

**Needs the user, once, at the end:** the threshold *feel*. `Snapon`,
`Togetheron`, `Swipespeed` and the peace-sign spread are calibrated against
synthetic geometry, which validates the arithmetic and says nothing about how a
real hand sits. That is one session, not a rebuild.

---

## The proximity latches: pinch, snap and clap, and the cook that never happened
2026-08-20

**Built.** `visionhands/sequences.py` (gesture sweeps as pure functions of a
phase), `tools/send_synthetic.py` (those sweeps over OSC, no camera),
`tools/td_add_latches.py` (the latch bank, 30 native operators),
`tools/td_verify_latches.py` (the verdict), `pinch_finger` on `HandPose` so the
snap channel can be driven at all, and `h{i}_pinch_count` / `h{i}_snap_count` /
`clap_count`. 164 tests, ruff and mypy clean. COMP output 476 → 497.

**One five-channel network, not three copies.** `docs/BUILD_PLAN.md` specified a
base COMP duplicated three times, one per gesture. Built instead as a single bank
five channels wide: every distance, every validity flag and both threshold sets
are renamed to the same five working names (`pinch0 pinch1 snap0 snap1
together`), so one set of operators serves all five latches and every Logic CHOP
pairs the right channels by name rather than by luck. Duplication is how two of
three copies end up subtly different.

That naming scheme also dissolves the alignment problem the build plan flagged.
Rather than gate `h0_pinch_index` with a channel called `h0_valid` and hope, both
are called `pinch0` by the time they meet.

**The whole recurrence is native, and needs no Expression CHOP.** The Logic
CHOP's `convert` parameter turns out to be a comparator — `lt` is "On When Less
Than Zero" — so `distance < threshold` is a Math CHOP subtract and one Logic CHOP,
with the threshold arriving as a *channel* from a Constant CHOP bound to the
Tuning page. Zero Python in the cook path, and retuning takes effect on the next
frame with nothing to rebuild.

**THE FINDING: TouchDesigner does not cook a branch nobody consumes, and for a
branch with memory that is fatal.** Promoted to `DESIGN.md` 2.10. The latch bank
was built, structurally verified, correct on inspection — and dead. Measured:

    merge_out    283,930 cooks
    out1         283,929 cooks
    derive_chop  178,976 cooks
    lat_state          2 cooks   <- both of them forced by the build script

This project's consumer of the COMP is two Select CHOPs taking `*_tx` and `*_ty`,
so nothing ever pulled the branch the latches live in. For the stateless
`derive_chop` that is a pure optimisation and entirely correct. For a recurrence
it means `prev` holds a value from an arbitrarily distant past and every edge
pulse is computed between two frames that were never adjacent. The state would
eventually self-correct, because the recurrence is level-driven; the pulses never
would.

The fix is one operator: a Null CHOP with Cook Type = `always` consuming the
deepest point of the bank. Every group with memory still to build — velocity
Slopes, smoothing Filters, debounce Counts, dwell Timers — needs the same, and
this is now the first thing to check when a temporal channel misbehaves.

**Three wrong answers on the way there, each of which looked right.**

*A Trail CHOP recorder.* The instinct was to record the whole trajectory and
check every property at once. It reported a rock-steady distance through a sweep
that had unquestionably swept — because a diagnostic branch with nothing
downstream does not cook, so its 1200-sample "history" was a record of when the
script forced it rather than of what the network did. Same root cause as the bug
it was built to find, which is why it hid it.

*`viewer = True`.* Measured: cooks did not advance. A node viewer cooks only when
actually visible in an open pane.

*A Null CHOP downstream.* It is itself unconsumed, so it does not cook either.
Only Cook Type = `always` breaks the regress.

**And two operators that lied quietly.** The Count CHOP's `offtoon` / `on` /
`ontooff` / `off` parameters look exactly like toggles and are menus of actions
(none / inc / dec / …); assigning `True` leaves them at `none`, with no error, and
the counter reads zero for ever. Replaced with Feedback plus a Math add — the
same construction the latch already uses for `prev`, so it adds no new unknowns
and what it does is legible in the network. And `appendFloat` on an *existing*
custom parameter resets its value to **zero**, not to its default; setting
`.default` afterwards does not bring it back. A second run of the builder left
every threshold at 0.000, which disabled every latch while the network still
looked correct and reported nothing. The rule inherited from
`tools/td_build_comp.py` — ".default always, .val only when creating" — is
necessary and not sufficient; the builder now saves and restores.

**A shipped bug found by re-running a tool.** `tools/td_add_derive.py` appended
`derive_chop` to `merge_out` unconditionally, so every run added a duplicate. The
COMP had been publishing 647 channels where 476 were meant since the previous
session — 171 duplicates, invisible because `chan()` returns the first match and
the channel count is the only symptom. Fixing it uncovered a second layer:
connecting to a Merge CHOP connector that reports itself FREE can *insert* rather
than fill, pushing the existing connection down and leaving it attached twice, so
even the careful "append only if missing" version made things worse. Both
builders now rebuild the whole input list. That also matters because the MCP
bridge has been observed executing a script TWICE per call, so idempotence has to
be structural rather than conventional.

**VERIFIED, over time, with the sidecar stopped and nobody in frame.** A latch
cannot be checked from one frame, so each property is a counter delta over a
generated sweep. All five passed:

| sweep | expected | measured |
|---|---|---|
| `ramp --cycles 4` | h0_pinch +4 | **+4** (and h0_snap +4) |
| `deadband --cycles 6` | h0_pinch **+1** | **+1** |
| `clap --cycles 3` | clap +3, pinch +0 | **+3, +0** |
| `both --cycles 3` | all four per-hand +3, clap +0 | **+3, +0** |
| `absent` | +0 everywhere | **+0** |

`ramp +4` rather than +8 is what proves each pulse is exactly one frame wide,
because the counter counts frames-high rather than transitions. `deadband +1` for
six dips is the hysteresis: six crossings of `on`, none rising past `off`, so a
correct latch engages once and holds — a comparator, or a parity counter that has
lost sync, gives +6. It was also observed directly, sitting engaged at
`pinch_index` 0.435 with `Pinchon` 0.35 and `Pinchoff` 0.50. `absent +0` is the
trap `docs/ATTRIBUTES.md` opens with, defused: every distance reads 0.000, below
every engage threshold, and only the validity gate stops all three firing at
once. `both` exists because every other sweep leaves slot 1 empty, so hand 1's
column had never been exercised.

**Recorded rather than fixed: a full index pinch also engages the snap latch.**
At `pinch` 1.0 the thumb sits 0.21 hand-sizes from the middle fingertip, below the
`Snapon` default of 0.25. That is what a real hand does — the arithmetic is not in
question — but it means `h0_snapping` does not discriminate a pinch from a snap at
these defaults. One for the threshold-feel session.

**Also fixed:** `derive_chop` was warning on every cook that editing `numSamples`
is unsupported in Time Slice mode. It publishes a snapshot of one frame, so it is
now explicitly not time-sliced, which also leaves the latch bank downstream a
plain 1-sample CHOP with no rate negotiation of its own to get wrong.

**Still to build:** the rest of the temporal channels (velocity, smoothing,
debounce, dwell, steadiness, motion events), the group COMPs with `allowCooking`
gating and the five parameter pages. **Still needs the user:** threshold feel, and
a two-hand fixture before slot assignment.

---

## Review triage: a stuck latch, a wrong explanation, and two tests that tested nothing
2026-08-20

The review gate earned its keep. Thirteen findings; the four that mattered were
all invisible to me, and one of them was that my headline measurement from the
previous entry was arithmetic nonsense.

**HIGH, real, and now fixed: a stopped sidecar latched every engaged gesture for
ever.** `DESIGN.md` 2.9 already recorded that a dead sidecar leaves TouchDesigner's
channels frozen rather than zeroed. The validity gate was `found AND conf_median
>= threshold AND size > floor` — every term read from the frozen last frame. So
pressing Stop Sidecar mid-pinch left `h0_valid = 1` and `h0_pinch_index ≈ 0.1`
indefinitely: the latch stayed engaged, no end pulse ever fired, and any project
gated on the state was stuck on with nothing reporting it.

The previous entry had already *observed* this and not recognised it — I wrote that
the latch was "sitting engaged at `pinch_index` 0.435" after the sender exited and
treated it as a nice demonstration of the dead-band hold. It was that, and it was
also the bug.

Fixed with a liveness term: the slope of `seq` is non-zero while frames arrive, a
Logic `ne` convert turns that into a boolean, and a Trail plus an Analyze maximum
answers "did it change at all in the last 0.5 s" exactly, with no filter constant
and no epsilon. Broadcast to the bank's five channels by a Constant CHOP of
expressions, the same way the thresholds already are. Verified: with the stream
stopped, `h0_pinch_index` frozen at 0.435 and `h0_valid` frozen at 1,
`h0_pinching` went to 0 and `h0_pinch_end_count` incremented.

**And the liveness detector exposed that my explanation for the clock was
wrong.** The previous entry claimed, and `DESIGN.md` 2.10 asserted, that
TouchDesigner does not cook a Merge branch whose channels nothing downstream
selects — citing `merge_out` at 283,930 cooks against `lat_state` at 2. The
reviewer pointed out that a Merge cannot emit output without cooking its inputs,
so the numbers could not both be right.

They were not comparable at all. `merge_out` had existed for hours; every build of
the bank destroys and recreates `lat_state`, restarting its counter. I compared a
lifetime total against a fresh one and read a mechanism into the ratio. Tested
properly — bank wired, clock detached — `lat_state` and `merge_out` advanced **402
to 402, in lockstep**.

What makes this worth a long entry is that the wrong diagnosis *worked*: adding
the clock did fix the symptom, so nothing pushed back. The clock is still there,
and it is still necessary — for a completely different reason, which the liveness
work then made obvious. When the sender dies, `in1` stops changing, so nothing
downstream cooks, **including the detector whose entire job is to notice that
nothing is arriving**. Measured: `lat_live` sat at 1 forever with nothing sending.
A branch with memory has to be clocked by time, not by data, because the event it
must react to is the absence of data. `DESIGN.md` 2.10 has been rewritten, with
the wrong version kept underneath it.

Two lessons banked: `totalCooks` is a lifetime counter and comparing it across
operators of different ages measures nothing; and a fix that works is not evidence
that its rationale is right.

**HIGH, latent: channel identity was positional.** Every rename used
`renamefrom = "*"`, which maps the To list onto whatever order channels arrive
in — so identity depended on order, and `derive_chop` publishes alphabetically,
which is not the bank's order. Correctness rested entirely on the Select CHOP
reordering to `channames`. Worse, `derive()` already takes an enabled-groups set:
the moment the Attributes page exists, unchecking Contacts drops the eight
`h*_pinch_*` channels, `hands_distance` becomes channel 0, and the clap latch is
published as `h0_pinching` — cleanly, with no error. Now every rename is keyed on
names. Verified live that a literal From/To list is pairwise, and that an absent
source keeps its own name while the rest still map correctly, so a dropped group
costs one latch instead of shifting four onto the wrong data.

**MEDIUM, deferred with a reason: the gate is raw `valid`, not the debounced
`active` the spec asks for.** One low-confidence frame mid-pinch drops the state,
fires a spurious end pulse and re-engages with a spurious start and a bumped
counter. The debounce is the Presence group's job; the shortfall is now marked
`TODO(M-presence)` at the gate and written into `docs/ATTRIBUTES.md` rather than
left implicit. No sequence perturbs confidence yet, so nothing would catch it — a
`glitch` sweep expecting +1 rather than +2 is the test to write with it.

**Nothing measured the falling edges.** All five sweeps passed while
`h{i}_e_pinch_end`, `h{i}_e_snap_end` and `e_apart` were structurally checked and
behaviourally untested. Added release counters, and with them a standing
invariant checked on every run: `fires - releases == 1 if engaged else 0`. It
catches a missing end pulse, a doubled one, and a missing start. Re-verified from
a fresh build: ramp +4 fires and +4 releases, and after clap and both, all five
pairs at 8/8, 3/3, 7/7, 3/3, 3/3 with the invariant holding.

**Two surviving mutations — tests that tested nothing.**

*`pinch_finger` could be ignored entirely.* Reverting the whole point of the
synth change — hard-coding the index tip — left all 164 tests green, because a
full index pinch already brings the thumb to 0.21 hand-sizes of the middle tip,
under `Snapon` 0.25. So the `snap` sequence was secretly sending a pinch and
TouchDesigner still reported `h0_snap_count +N`. The discriminating property is
which distance reaches zero, and it is now asserted.

*"Crosses the threshold" was not what the sweep test asserted.* Doubling the clap
separation left the minimum `hands_distance` at 0.5999999999999996 — four parts in
1e17 under `Togetheron` — and every Python assertion passed. But that value
reaches TouchDesigner as a **float32**, where it rounds to exactly 0.6 and the
clap latch never engages: green suite, `clap +0`, and the network blamed. Now
every sweep must clear its thresholds by a tenth of the dead band on at least four
frames.

Both mutations re-applied and confirmed to fail now.

**Thresholds had three homes.** `visionhands/tuning.py` is now the only one, with
an import-time check that every `off` exceeds its `on`. The failure this prevents
is specific: raising `Pinchon` to 0.45 in the builder alone left every Python test
passing and TouchDesigner still reporting `deadband +1` — while the sweep's crest,
at 0.44, was now *below* the engage threshold, so the distance never left the
engaged region and the hysteresis test had silently stopped testing hysteresis.
The test now asserts the crest sits strictly inside the band. Mutation confirmed
failing.

**Two writers on the port are now refused rather than documented.** The docstring
said the receiver could not tell the sidecar from the synthetic stream, so the
tool would not guess. Both halves were answerable, and the consequence justified
the check: interleaved streams alternate the pinch distance between two different
hands every other frame, driving the latch across the dead band repeatedly and
turning +4 into +37 — or into something plausible enough to accept. `ps`-based
detection, `--force` to override.

**Which promptly found a bug in itself.** The first matcher required `-m`
immediately followed by the module — and matched a decoy `python -c "... # -m
visionhands.sidecar"`. `ps -Ao args=` returns the command line as one string, so
splitting on spaces does not recover argv boundaries and the body of a `-c` script
splits into words too. Same class as the `pgrep -f` false positive that once had
the Stop button SIGTERM its own shell, one level subtler. Now the leading
interpreter options are walked the way CPython parses them, stopping at the first
`-c`, `-m` or script path. Six argv cases plus a live `-m` match and a live `-c`
decoy.

**Confirmed correct, so recorded as such:** initialisation. Both feedbacks take
`lat_zero` as their template, so a reload comes back released with the counters at
0, and the phantom-end-pulse-on-reload hazard `docs/ATTRIBUTES.md` warns about
cannot happen. The converse does — reloading mid-gesture fires one spurious start
— which is inherent to a level-driven latch and is now written down.

**And a hazard closed by measurement rather than by argument.** The reviewer
flagged that the Feedback CHOP's `previous` is time-based and asked what happens
when the timeline rewinds. This project's timeline plays and loops every 600
frames, so it had already rewound roughly a hundred times during the verification
runs, and `h0_pinch_count` held at 8 throughout. It does not reset. Every +4/+1/+3
delta in the previous entry was measured across multiple rewinds.

**Still open, and named rather than absorbed:** a `pinch0`/`snap0` swap is
invisible to both `ramp` and `both`, because both counters move together in each;
the only thing that currently discriminates the mapping is the incidental snap
engagement on a one-hand ramp, which the threshold-feel session may tune away.
And 39 operators is a lot to hold in the head — `docs/BUILD_PLAN.md` step 3 has to
decide how `allowCooking` group gating interacts with a Cook Type = Always clock
before the group COMPs are built, because a disabled group would freeze its
latches and resume with a stale `prev`.

502 channels, 168 tests, ruff and mypy clean.

---

## Presence, velocity, and two operators that were doing nothing
2026-08-20

**Built.** `tools/td_add_temporal.py`: `h{i}_active` with a real debounce,
`h{i}_entering`, `h{i}_exiting`, `h{i}_held`, `both_active`, `n_active`,
`h{i}_vel_x/_y`, `h{i}_speed`, `h{i}_accel`, `hands_approach`. The latch bank now
gates on the debounced `active`, closing the shortfall the review flagged. COMP
output 542 -> 561.

**The debounce is the same Schmitt trigger as the latches**, which was worth
noticing because it looks like a different problem:

    active = all_valid_over_last_A  OR  ( prev AND any_valid_over_last_D )

`min(valid)` over a Trail of `Activateframes` is the turn-on condition;
`max(valid)` over `Deactivateframes` is the term that holds. The asymmetry
`docs/ATTRIBUTES.md` asks for - slower to drop a hand than to acquire one - is
just two window lengths on one shape, and it inherits the same self-correcting
property. Several of the channels still to build are that shape too.

**THE FINDING, and it cost two operators: anything that consumes a sample axis is
inert here.** Every CHOP in this network carries one sample per frame - a
snapshot, not a time-sliced signal.

- The **Slope CHOP** differentiates along the sample axis, so it has no neighbour
  to difference against. It reported palm velocities of **12.5 and 32.5 units per
  second on coordinates that cannot leave 0..1**, and an acceleration of 1950.
  Absurd enough to catch immediately, which is the good kind of wrong.
- The **Filter CHOP** smooths along the same axis, and is a silent passthrough.
  **All nine of its filter types returned the identical value**, unchanged by
  width. That is the bad kind of wrong: a plausible number, a parameter that does
  nothing, and no error anywhere. Caught only by sweeping the type menu and
  noticing every answer was the same.

Operators that CREATE a sample axis from successive frames - the Trail CHOP - work,
which is why the debounce does. Promoted to `DESIGN.md` 2.11 as the rule to read
before building anything else temporal: differentiate with `(x - x_prev) * rate`,
smooth with Trail + Analyze or with Feedback + Math.

**And a derivative has to be AVERAGED, not smoothed, to be correct at all.**
Positions only change when a frame arrives - 30 fps into a 60 fps cook - so the
per-cook derivative alternates between a double-size step and zero. Measured
directly: a palm step of 0.0592 in one cook where a cook's worth of motion was
0.0292. Against a swipe of known speed 1.75 units/s the raw derivative peaked at
**4.43**; averaged over 0.15 s it reads **1.66, within 5%**, and the residual is
the window spanning part of the direction reversal. So `Velocityfilter` is a
window width rather than a smoothing constant, and its default moved 0.08 -> 0.15
because 0.08 spans only two camera frames and leaves the alternation in the output.

**A first-frame spike, fixed by seeding rather than by clamping.** The derivative's
Feedback initialised from zeros, so frame one reported `x * rate` - a palm at 0.208
gave 12.5 units/s, which propagated into acceleration as 1950 and took the average
window to shed. Seeded from the input instead, frame one reports 0, which is the
only defensible answer when there is no previous frame.

**The liveness bug had a second instance, in the group built to avoid it.**
`h{i}_valid` is computed from the last frame TouchDesigner received, and a dead
sidecar leaves that frozen - so `h0_active` read 1 and `h0_held` climbed to **54
seconds** with nothing sending. The latch gate was already immune because it ANDs
liveness separately; a project reading `h0_active` would not have been. Liveness is
now ANDed in before the debounce, so presence decays through the normal
`Deactivateframes` path rather than snapping. Recorded as a smell: liveness is a
property of the STREAM and is currently owned by the latch builder, which both
other builders now reach into. It should move somewhere shared.

**Two measurement traps worth more than the numbers.** `totalCooks` is a lifetime
counter, so comparing it across operators of different ages measures nothing -
that is what made 2.10 wrong twice. And forcing a cook does not advance the frame,
so every read inside one script sees one frame: a loop that samples twenty values
and reports a peak and a mean reports the same number twice, which is exactly what
happened before I noticed.

**Still to build**, with what each needs, in `docs/BUILD_PLAN.md` step 2: `dir`
(needs atan2, which no native operator has), `steadiness`, `dwell`, `hands_scale`,
`hands_twist` (needs sin/cos from `derive()` to survive the +/-180 wrap), grab and
release (two more latch rows, not new machinery), and the swipe/wave/cross/twist
events.

181 tests, ruff and mypy clean.

---

## Group toggles, and the allowCooking question settled by measurement
2026-08-21

**Built.** `tools/td_add_groups.py`: the Attributes page, a `Verbosity` menu with
the three presets from the spec, and a toggle per group.

**The toggles gate the compute that actually costs.** They reach `derive()` through
the `_groups()` reader that `tools/td_add_derive.py` has had all along - it fell
back to "all enabled" only because the parameters did not exist. Measured:
`Verbosity = Minimal` takes `derive_chop` from **171 channels to 0**, and the COMP
output from 561 to 393. That is the one operator in the COMP where per-channel cost
is real (main-thread Python, 0.210 ms for 171 channels), so the compute worth
saving is saved by a page of toggles and no restructuring at all.

**The `allowCooking` question, answered rather than argued.** A Feedback counter
plus a Cook Type = Always Null inside a base COMP, then `allowCooking = False`: the
counter **froze at 275** and its cook count stopped dead. Re-enabled, it resumed at
276 - it paused, it did not reset. So:

- disabling a group really does stop all of its cost, its clock included;
- a group with memory resumes with a `prev` from whenever it was switched off, so
  the latches would be stale for one frame (self-correcting, being level-driven)
  and one edge pulse across the gap could be wrong. Bounded, acceptable, and the
  reason the clock must live outside any gated group.

**Two things I did not do, and would rather say why than pretend otherwise.**
Wrapping the native groups in base COMPs is ~150 operators of restructuring to
save native CHOP cost on 1-sample inputs - negligible against the Python cook the
toggles already gate. And trimming the output list for the native groups needs an
exact channel-to-group map: wildcard patterns provably cannot partition these
channels, because `h?_*_x` matches the raw `h0_wrist_x` and the derived
`h0_palm_x`, and `h?_e_*` matches both the Events and the Triggers pulses. The
registry that would fix it is designed in `docs/BUILD_PLAN.md` step 3, and it also
resolves a smell: liveness is a property of the stream and currently lives in the
latch builder, which two other builders now reach into.

**The same-frame trap, twice more, on the same mechanism.** Setting `Verbosity` and
reading the toggles in one script showed the preset doing nothing three times in a
row - the Parameter Execute callback runs after the script. Read one call later,
`Minimal` correctly left exactly `Landmarks` and `Coordstx` on. This is the third
distinct thing that a same-frame read has lied about today, after the cook-count
comparison and the velocity distribution. Worth stating as a rule: nothing that
crosses a frame boundary can be verified inside one script.

**Not started: visionface, visionpose and person segmentation.** The two hours went
on the filter, the triggers, the temporal channels and the three bugs each turned
up. The plumbing is well-shaped for it - the sidecar takes any `HandSource` and the
channel contract is one table - but it is a real piece of work and starting it
badly would be worse than not starting.

---

## Slot assignment, and a left hand that was never a left hand
2026-08-21

**Built.** `visionhands/slots.py` plus 17 tests, wired through `engine.py`,
`source.py` and `sidecar.py`, with a `--slots` flag and a `Slotassign` toggle on
the COMP's Sidecar page. Milestone 11, closed - and it did not need the two-hand
fixture everyone including me kept saying it was blocked on.

**The algorithm collapsed to a partition.** Vision already reports which hand is
which: `engine.py` has read `observation.chirality()` since it was written and
DESIGN.md 2.3 records it verified against a real hand. So slot 0 is the right hand
and slot 1 the left, fixed by anatomy rather than by Vision's ordering, which makes
it stable across dropouts, occlusion and reordering with **no history at all**.
Proximity against the previous frame is still there, but only for the cases
chirality cannot decide - two hands both reported RIGHT, or UNKNOWN.

**The N-frame grace period specified in 6.3 is deliberately NOT built.** The
Presence debounce added yesterday already holds a hand through brief dropouts,
downstream and per hand - and in chirality mode the hazard cannot arise anyway,
because a left hand never lands in slot 0 however many frames the right one is
missing. Building it would have been a second mechanism for one problem.

**The measurement that shows what the defect actually was.** A left and a right
hand crossing over, with the hand order reversed every frame the way Vision
reorders (`sequences.py`'s `crossing`, sent with `--unstable`). The two hands were
generated at sizes 0.1200 and 0.1440.

| | assignment on | assignment off |
|---|---|---|
| `h0_chirality` | +1 throughout | alternates |
| `h0_size` | **0.1200** | **0.1322** |
| `h1_size` | 0.1440 | 0.1318 |

0.1320 is the mean of 0.1200 and 0.1440. So with assignment off, `h0` is **neither
hand** - the smoothing filter averages both into one slot, because the slot changes
identity every frame. That is the most concrete demonstration of "silent slot
swapping looks fine on screen" this project is likely to produce, and it only
became visible because the filter was added first.

**A toggle, because it changes behaviour.** With assignment on, a single LEFT hand
puts nothing in slot 0: every `h0_*` channel reads zero and `h1_*` carries the
hand. That is what "h0 is always the right hand" means, and it will surprise anyone
treating `h0` as "the hand". Read at launch, restart to apply - the same mechanism
settled for the stream toggles, and for the same reason: there is no control
channel into the sidecar.

**AND THE USER CAUGHT A BUG BY READING THE GENERATOR: the synthetic left hand was
never a left hand.** `_FINGERS` describes a right hand - thumb at x = -0.42, little
at +0.36 - and `synthetic_hand` built that geometry regardless of `chirality`,
storing the label and nothing else. Both hands came out with the thumb on the same
side.

That made `chirality` cosmetic in every synthetic test, and it means anything
handedness-sensitive had been silently testing a right hand twice: `rotation`,
`point_angle`, `pinch_angle`, `spread`, and the whole pose descriptor. Fixed by
reflecting local x before normalising, rotating or pinching, so everything
downstream inherits it - including the pinch, which reaches for a fingertip that
has already moved.

Two things make the fix safe, and both are now tests. `size` and `rotation` are
defined off the wrist-to-middle-MCP vector and middle_mcp sits at local x = 0, so
reflection cannot move it - every exactness assertion in `test_derive.py` holds for
either hand, verified. And a negative control asserts that `pinch_angle` DOES
change, because a mirror that changes nothing was not applied.

Worth noting what this says about the slot measurement above: it was taken before
the mirror fix, and it stands, because it turns on `size`, which reflection does not
change. Re-run afterwards with genuinely mirrored hands it also shows `thumb - little`
in x at -0.1101 for h0 and +0.1321 for h1 - opposite signs, which no amount of
relabelling could fake.

**Still unmeasured**, and now the only thing the fixture is for: whether real
Vision's chirality flickers frame to frame, and what it reports during occlusion or
edge-on. Both decide how often the proximity fallback is actually reached.

201 tests, ruff and mypy clean.

---

## 1.8 ms to 1.0 ms, and TouchDesigner already had the filter
2026-08-21

Cook time was raised as too high after the live session: 1.5-1.8 ms is 9-11% of a
frame for what is mostly arithmetic on 15 channels. Profiled per operator, then
three fixes. **1.828 ms -> 1.040 ms, 152 operators -> 144.**

**THE BIG ONE, and it is a correction rather than an optimisation: TouchDesigner
has a one-euro filter built in, and I built one by hand out of 21 operators.**

    hand-built chain   0.6949 ms across 21 operators
    Filter CHOP        0.0153 ms in 1

Functionally identical - within 0.17% under motion - and the native one converges
EXACTLY on a still hand where mine left a float32 residual of 9e-8. That was 38% of
the COMP's entire cook time, spent reimplementing a built-in.

**Why the wrong conclusion looked right, because that is the transferable part.** I
tested the Filter CHOP on `tmp_slope` - a Math CHOP fed by a Feedback - and all
nine of its filter types returned the identical value, unchanged by width. That
reading was correct. The generalisation I drew from it, and wrote into DESIGN.md
2.11 as a rule, was "sample-axis operators do not work here". The truth is narrower:
they are inert on a NON-TIME-SLICED one-sample CHOP and work perfectly on a
time-sliced one. `tmp_slope` is built from Math and Feedback and inherits no
time-slicing; `oef_in` descends from the OSC In CHOP and does.

So the question was never "is this CHOP one sample wide" but "is it time-sliced" -
and I never asked it, because one measurement had already settled the matter in my
head. Third correction in that area of 2.11 now. The rule I actually needed: test
the operator on the input you intend to use it on.

Worth noting the hand-built version was not wasted in one respect - every stage was
inspectable, which is where the measurement that the cutoff really does open up
2.3x on a real gesture came from. The native filter is a black box that gives the
same answer.

**Two cheaper wins from the same profile.**

`lat_on` and `lat_off` cost **0.110 ms a frame to deliver six numbers**, because
each of their 15 values was a Python expression on a Tuning parameter -
30 evaluations per cook for thresholds that change when somebody drags a slider.
Now static values, rewritten by a Parameter Execute DAT on change. Verified both
ways: they read the Tuning page at build, and setting `Pinchon` to 0.42 showed up
in `lat_on` on the next frame.

`lat_live5` and `lat_active13` were two 15-channel broadcasts of two booleans,
0.074 ms, ANDed into the same gate. The AND now happens per hand first, in one
native Logic CHOP, and only the result is broadcast.

**A measurement trap worth recording.** The first profile after the filter swap read
**2.946 ms** - worse than before - and the latch bank appeared to have jumped from
0.370 to 1.822. It was a forced full re-cook immediately after a rebuild. On a
clean frame it was 1.030, then 1.040 on the next. `cook(force=True)` right after
building anything makes every downstream cookTime meaningless; sample a frame that
nobody disturbed.

| group | before | after |
|---|---|---|
| filter | 0.612 | 0.110 |
| latches | 0.370 | 0.247 |
| `derive_chop` | 0.368 | 0.256 |
| temporal | 0.241 | 0.204 |
| DATs / other | 0.138 | 0.133 |
| coords | 0.099 | 0.090 |
| **total** | **1.828** | **1.040** |

`derive_chop` is the largest single item now at 25%, and it is already gated by the
group toggles - so the remaining headroom is mostly in what a project chooses to
enable rather than in making anything faster.

**Still open**, and recorded in docs/BUILD_PLAN.md step 7: the velocity chain uses
Trail + Analyze because it is not time-sliced, and might be replaceable by the
native Filter the same way - one measurement would tell. The two DATs cost 0.057 ms
between them and it is not obvious why they cook at all. And 7.3, grouping the
network into COMPs, is now the main remaining item.

## Body pose: a second stream, and four silent channel losses in one afternoon

Built `visionpose`: the 19-joint contract, the Vision request, the stream registry,
one bundle per stream on its own port, a synthetic body to test it with, and the
TouchDesigner side. 223 tests to 284. Verified live in TouchDesigner with nobody in
frame and the camera untouched: **123 pose channels on port 10001** with the right
names and moving values, and the hands port now carrying **141** (137 + four `sc_*`
status channels) with the COMP output at **499**.

**The design questions were settled before this session and they held.** Launch
flags rather than a control channel; one process and one camera with both requests
performed on the same sample buffer, so the two streams share a `seq`; a disabled
stream sends zeros rather than nothing. Two things I decided while building, both
now in DESIGN.md 6.4: **one port per stream** (sharing one would put pose channels
inside the hands COMP's output, and the alternative is the prefix Select that has
already failed to partition these channels once), and **the sidecar DATs stay where
they are** - moving them upstream would break the panel in a live project in
exchange for tidiness.

**What the framework gave me for free, and what it tried to take.**
`VNDetectHumanBodyPoseRequest` enumerates its own joints - `supportedJointNames`
returns all 19 - so the hardcoded table is checked against the request's own answer
rather than a `dir()` scrape. That is a stronger check than hands gets. But the
constant NAMES and their VALUES disagree about anatomy: `LeftElbow` is
`left_forearm_joint`, `LeftWrist` is `left_hand_joint`, `Nose` is `head_joint`.
Anyone building the table from the values maps an elbow to a forearm and it looks
entirely plausible on screen. Our table keys on the constant suffix and carries the
value as the dict key, which is the same three-name arrangement `types.py` uses for
hands and for the same reason.

**No chirality for a person, so no partition.** Slot assignment works for hands
because Vision labels them left and right; there is no equivalent for people, and
Vision's observation order is not stable. So bodies are sorted **left to right by
the root joint** - p0 is the leftmost person - which is stateless, stable while
people do not cross, and visible in the `two` sweep, which crosses them
deliberately. The fallback matters: an occluded root reads x = 0.0, which is the far
LEFT of frame, so a plain x sort would put a person we cannot locate in slot 0 and
push a visible one to slot 1.

### Four ways a channel disappeared without anything saying so

This is the part worth reading. Every one of them presented as "the number is
fine", and three were in code that already existed.

**1. A newly appended toggle reads False whatever its default says.** The stream
toggles came back off - `Streamhands` with `.default = True` and `val = False` - so
`start()` refused to launch anything. The builder set `.val` only when the COMP was
new, which is not the same question as whether the PARAMETER is new, and every
parameter added to an existing COMP therefore arrived switched off. This is the
sibling of the recorded `appendFloat`-resets-to-zero trap and the fix covers both:
set `.val` exactly when the parameter did not exist before, which the `previous`
dict already records. It was only noticed because my refusal-to-start check is loud;
a version that silently launched with no streams would have looked like a dead
camera.

**2. `merge.inputs` reports a group COMP as its inner `out1`.** So
`tools/td_add_derive.py`, which rebuilt the merge input list and deduped by NAME,
saw `filter` and `coords` as two operators both called `out1` and kept one. The
coordinate group was silently dropped: **392 channels to 224**, builder reporting
success. Nobody had hit it because the documented build order had not been run end
to end since the groups were introduced - I hit it on the first honest attempt.
Fixed by using the `_attach_once` helper the other three builders already carry,
which dedupes by the owner OBJECT. Third distinct way `.inputs` has lied in this
repo.

**3. The filter group ate the four new status channels.** It splits its input into
positions and everything-else from two NAMED lists, and `sc_*` was in neither, so
they vanished inside the group: OSC In CHOP 141 channels, COMP output unchanged at
495, no error anywhere. The comment above that split already said "the two halves
have to partition exactly - anything dropped from both vanishes from the COMP's
output". It was right and it was not enough, because nothing CHECKED it. The check
is now output-versus-INPUT rather than output-versus-contract, which is the only
form that works: with `send_synthetic.py` as the sender there are no `sc_*` to
reproduce, so a contract comparison would report a phantom failure.

**4. `^` does not exclude in a Select CHOP's `channames`.** Trying to write that
partition as a complement instead of a list: a 141-channel input, pattern
`* ^*_x ^*_y`, gave **423 channels**. The terms are additive and each matched the
whole lot again. `* ^h0_wrist_x` gave 282. So there is no complement pattern to be
had; compute it in Python and name the channels.

### Cook time, and a measurement I got wrong before I got it right

The task was to re-measure properly, since the 0.599 ms reported after grouping
counted only top-level operators. First attempt: **1.2 ms and 1.55 ms**, summing all
162 operators. Both figures were junk, and the reason is worth more than the
numbers. My synthetic feeder was launched as a FILE rather than on stdin, so
`sys.path[0]` was the script's directory instead of the repo root; it died on
`ModuleNotFoundError` into `/dev/null` and never sent a byte. Nothing downstream
cooked - and `cookTime` is a LAST-VALUE gauge, so every operator cheerfully reported
its previous cook. A dead sender and a live one look identical in that column.

Redone with data provably flowing (`seq` rising 730 -> 1160 across the two reads):
**0.997 ms and 1.187 ms** over 162 operators and 499 channels. Latches 0.28-0.31,
temporal 0.23-0.24, `derive_chop` 0.17-0.21, coords 0.11-0.24, filter 0.06-0.12.
Against the 1.040 ms measured flat, **grouping is cost-neutral** - which is the
answer the build plan asked for and did not assume either way.

The lesson generalises past this repo: `cookTime` cannot tell you whether anything
is happening, only what happened last time it did. Check the data is moving first,
by a channel that has to change.

### allowCooking gating, and proof by counter delta

`temporal` and `latches` now bind `allowCooking` to their toggles - the last
approved item. Presence or Motion keeps `temporal` cooking; Triggers, Gestures or
Events keeps `latches`; and `latches` cooking forces `temporal` to keep cooking too,
because it READS temporal's presence gate. Without that rule a frozen `temporal`
would leave every latch armed by a stale `ready`: frozen at 1 they keep firing on
stale liveness, frozen at 0 they never fire again, and neither says anything.

Proved the way anything with memory has to be proved here - counter deltas over an
interval, never a same-frame read. With the three latch toggles off: `latches`
cooked **0** times while `temporal` and `coords` each cooked **377**. With all five
off: latches 0, temporal 0, coords 567. And the frozen groups' channels are all
still on the COMP output, holding their last values, which is the property that
matters for a project that references them.

One measured detail with teeth: **a frozen group still reports its old `cookTime`**
- 0.715 ms for a `temporal` that had not cooked in hundreds of frames. So summing
that column while anything is frozen double-counts stale work, and the saving can
only be inferred from what those groups contribute when they ARE cooking: about
0.5 ms of the ~1.0-1.2 ms total.

### Left undone, deliberately

No attribute layer for pose - no derived channels, no filter, no temporal - because
the brief was plumbing plus basic testing. Face is next and does NOT fit this shape: its landmarks
are 12 named REGIONS of differing point counts rather than one joint table
(verified, DESIGN.md 2.12), so the channel list needs a level the pose table does
not have.
