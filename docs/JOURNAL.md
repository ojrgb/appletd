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

## The face stream, and one number I refused to write down

Built `visionface` the same afternoon as pose, with the same shape: a contract
module, a request module, a synthesiser, a sender, a COMP builder. **Verified in
TouchDesigner: 23 channels on port 10002**, head pose sweeping, camera untouched.
320 tests.

**The interesting part is what is NOT in it.** A hand and a body both arrive as a
`VNRecognizedPointsObservation` - a flat dict of named joints, fixed count, known
before you start. A face does not. `VNFaceObservation.landmarks()` gives **12 named
regions**, each a `VNFaceLandmarkRegion2D` with its own `pointCount`, and nothing in
the framework will tell you those counts before a face has been observed. The
constellation pins the TOTAL at 76; the split is invisible until there is a face.

So the channel list needed twelve numbers I could not get. Three ways not to get
them, in the order I tried:

1. **Draw a face and detect it.** A procedurally shaded ellipse with eyes, brows,
   nose and mouth, through `VNDetectFaceRectanglesRequest`: **0 faces**. Vision is
   robust to illustration but not to that.
2. **Find a face in something already on this machine.** The only fixture here is
   `hand_clip.mp4`, which was gated to contain no identifiable person - so by
   construction it has no face to measure.
3. **Write the numbers down from memory.** This is the one worth naming, because it
   was tempting and it is exactly the failure the repo's standards are about. A
   wrong count does not raise: it lays a nose's points into an eyebrow's channels,
   and everything looks completely plausible until somebody wonders why the eyebrows
   never move. `docs/STANDARDS.md` says a guessed constant says so; a guessed
   constant in a CHANNEL LIST cannot say so, because the misalignment is downstream
   of the guess.

What shipped instead: `FACE_REGIONS` carries the 12 regions with
`point_count = None`, the landmark channels are not published while any is None, and
the stream publishes the part that IS knowable without a face - confidence, capture
quality, roll, yaw, pitch and the bounding box, all straight off the observation.
`tools/probe_face_regions.py` settles the rest from one camera frame: it prints
integers, writes no image and keeps nothing. Paste them in, re-run the COMP builder,
and the points appear with no other change - and `face_types.py`'s self-check
refuses a half-filled table and refuses one whose counts do not sum to 76, so a typo
fails at import rather than in a channel list.

There is a test asserting **no region carries a guessed count**, which is an unusual
thing to test and the right thing to test: it is the invariant that will be broken by
somebody in a hurry, and it skips itself once the counts are real.

**Two silent units traps on the face path**, both now pinned by tests:
`roll`/`yaw`/`pitch` are RADIANS and every angle in this system is degrees - a
missed conversion is a 57x that keeps every value small and plausible - and all
three are NSNumber-OR-NIL, where `float(None)` raises on the capture thread inside
an Objective-C callback, which is its own crash.

**One design detail worth keeping.** Faces sort left to right by bounding-box
CENTRE, not by `bbox_x`. A face close to the camera has a wide box, so sorting on
the left edge puts it left of a face that is genuinely further left. Same class of
bug as the body sort's confidence fallback: the obvious key is subtly wrong in the
case that actually happens.

**What the third stream cost the code.** Almost nothing, which is the point of
having done pose first: `streams.py` needed one name, the sidecar's third bundle
became a shared `_send_optional` rather than a third copy of the same twenty lines,
and the engine grew one more `_publish_*` method that cannot return out of the
callback. `IMPLEMENTED_STREAMS` went away entirely - with all three built it was
equal to `STREAM_NAMES`, and a distinction that is always true is dead code, so the
test that pinned it now pins the rule for the next stream instead.

## One COMP around the three streams, and two bugs that were waiting in it

The user's read, after face landed: the coordinate transforms and the filter live
inside the hands COMP and neither is hand-specific, so wrap the three streams in one
COMP, put the controls at the master level, and transform all three. Correct on
every count. Built it.

```
/project1/vision          every parameter, six pages, the sidecar control
  hands_osc 10000  ->  hands  ->  out1     499 channels
  pose_osc  10001  ->  pose   ->  out2     275   (was 123)
  face_osc  10002  ->  face   ->  out3      39   (was  23)
  status                                   the sidecar's own sc_* channels
```

**Checked the blast radius first**, and it was small: two Select CHOPs wired to the
old hands COMP and **zero** expression or DAT references to any of the three paths
outside them. That is what made a same-day restructure reasonable rather than
reckless - and the master builder rewires those two and inherits the old COMP's
tuned parameters, so Mincutoff 3.170 and Beta 10.000 came through untouched.

**`op.Vision.par.X` replaces `parent(2).par.X` everywhere.** A group now sits two
levels below the parameters instead of one, so every relative reference would have
had to be re-counted - and re-counted again the next time anything moved. Verified
the Global OP Shortcut resolves from a nested network BEFORE building on it: a
Constant CHOP two levels down read `op.VisionProbe.par.Testval` as 0.375. Depth
independent, survives a rename, and the only cost is that the shortcut name is
global to the project.

**The new module is `visionhands/spaces.py`, and it is the interesting part.** Both
builders used to pattern-match channel names; patterns have failed at this twice
already (`h?_*_x` matches two different groups, `^` does not exclude in a Select
CHOP). So the package now answers "what KIND of number is this channel" for every
stream, and the builders ask:

  * POSITION - offset by -0.5 into world space, because world space is centred;
  * EXTENT - NOT offset. A box is 0.22 wide wherever it sits, and subtracting 0.5
    from a width gives a negative size that draws as nothing;
  * ANGLE - smoothed like any continuous value, never transformed. There is no yaw
    in pixels;
  * SCALAR - confidences, counters, flags. Neither smoothed nor transformed: a
    smoothed confidence makes every gate reading it lag behind the thing it gates;
  * BOX_RELATIVE - and this is the one worth having written down before it bites.
    Face landmark points are normalised to the FACE'S BOUNDING BOX, not to the
    image. They look exactly like positions. Transformed with the image rules they
    would put every facial feature in one corner of the frame. They are not
    published yet, which is precisely when the rule needs to already be right.

Verified live, with synthetic senders and no camera: `f0_bbox_x` 0.39 -> tx -0.11,
px 499.2; `f0_bbox_w` 0.22 -> **tw 0.22**, pw 281.6; `f0_bbox_h` 0.30 -> th 0.1688
(the render aspect), ph 216.0. The third one is the whole point of the role
distinction.

### Two pre-existing bugs the restructure surfaced

**`derive_chop` was reading the RAW input.** Every derived attribute - every pinch
distance, every openness, every latch input - was computed on UNSMOOTHED landmarks
while the coordinate spaces used smoothed ones. Two consumers of the same positions
disagreeing about where the hand was, which is exactly what `td_add_coords.py`'s own
comment says must not happen.

The cause is a design mistake rather than a typo: `td_add_filter.py` asserted its
consumers, forcing a named list of five operators onto its output. Two of those
names had moved inside the `coords` group and matched nothing; and `derive_chop`
DOES NOT EXIST when the filter is built in the documented order, so it wired itself
to `in1` afterwards and stayed there. The list was written to be robust - "a list of
names cannot go stale" - and it went stale in both available ways at once. Each
consumer states its own input now, which is the only arrangement that cannot depend
on build order.

**`tools/td_verify_latches.py` never evaluated the h1 invariant.** Its state list
was hand-maintained and omitted `h1_pinching` and `h1_snapping`, so the loop over
END_COUNTERS raised KeyError on the second hand rather than checking it. The h1 rows
had been structurally present and behaviourally untested since the day they were
added - the same failure mode the release counters were added to fix, one level up.
The list is derived from END_COUNTERS now, so the two cannot drift. After the fix:
ramp +4 on `h0_pinch_count`, and fires - releases == engaged on all five rows.

### Four builders retired

`td_build_comp.py`, `td_build_pose_comp.py`, `td_build_face_comp.py` and
`td_setup_osc.py`. The first three are superseded by `td_build_vision.py`; the last
would have bound port 10000 a SECOND time, which is a stream arriving in a CHOP
nobody reads. Dead builders that still run are worse than missing ones, and one of
these would have quietly broken the thing it was written to set up.

342 tests, and the new ones are mostly about `spaces.py`: that the filter split
partitions every stream exactly, that an extent is never offset, and that nothing
box-relative or angular reaches a coordinate branch.

## The face landmark counts, and a datagram that was never sent

Ran `tools/probe_face_regions.py` against a real face - the user's, with their
say-so, at the machine. The 12 region counts came back immediately and the tool
refused them:

    sum of the regions: 87 points
    the constellation says: 76
    MISMATCH. Do not paste these in until it is understood.

**The check was right to fire and wrong about why.** It asserted that the regions
partition the constellation, which they do not. Extending the probe to count
DISTINCT coordinates settled it in one more run: 76 distinct points across 87 region
slots, with `medianLine` sharing points with `noseCrest` (4), `nose` (2), both lip
rings (2 each) and `faceContour` (1), plus `nose` and `noseCrest` sharing one more.
Twelve pair-overlaps for eleven duplicate points, which means nine points sit in two
regions and one sits in three - the nose tip.

Worth naming the shape of that mistake, because it is not the usual one: the guess
that got caught was in the CHECK, not in the data. The table was correct on the first
try; the invariant I wrote to protect it encoded an assumption I had not measured. A
self-check is code like any other and can be the thing that is wrong.

**Publishing per region means those eleven points appear twice**, under both of their
regions' names - 44 duplicate channels across two faces. That is the price of
`f0_nose_crest_00_x` over `f0_lm43_x`, and it is the same trade DESIGN.md 6.2 made
for hands: "which one is lm43" is exactly the question a name should answer. 371
channels, 387 after the coordinate spaces.

### And then nothing arrived

The suite went red the moment the counts landed: three face tests timed out waiting
for a datagram, and `n_sent` was 6 where 9 was expected. The face bundle is **12208
bytes**, and:

    net.inet.udp.maxdgram              9216
    a UDP socket's default SO_SNDBUF   9216
    sendto(12208)                      REFUSED

Not fragmented, not truncated - refused outright. `osc.py` had a note claiming the
bundles sat "comfortably inside the 16 KB loopback datagram limit", which was a guess
that had never been tested against anything bigger than 3480 bytes.

**The fix is one socket option, and the reason it works is the important part:**
raising `SO_SNDBUF` on the SENDER lifts the limit completely with no privileges
(65000 bytes sent and received on loopback), and the RECEIVER needs nothing - a
socket with the default buffer accepted 20000 bytes. That asymmetry is the only
reason this was fixable at all, because TouchDesigner owns the receiving end and we
cannot configure it. Every sender in the repo now goes through
`osc.datagram_socket()`.

**How it presented is the part to remember.** Two streams kept working perfectly and
the third one's channels never appeared in TouchDesigner. Nothing in the sidecar's
reporting distinguishes "the socket refused a datagram" from "nobody is sending", so
it reads as a broken stream rather than a wrong socket option. The tests caught it
before TouchDesigner did, which is the first time the socket-level tests have earned
their keep - a bound socket in a test refuses the same datagram a real one does.

Measured, for the next contract that grows: hands 4188 bytes (141 channels), pose
3720 (123), face 12208 (371). About 33 bytes per channel, so a default socket tops
out near 280 channels and `MAX_FACES = 2` is the practical ceiling for one face
bundle.

### Left undone

`visionhands/synth_face.py` still emits a box and three angles, so
`send_synthetic_face.py` leaves 304 of the 371 channels flat. The channels are real
and named; watching them move needs the camera until somebody writes a plausible
76-point synthetic face. `Face.landmarks` is the field to fill and
`face_channel_values` already pads what is missing, so it is additive.

## Two builders that broke their own network, and the cascade one of them caused

Verifying the face landmarks in TouchDesigner turned up something worse than a face
bug: the hands stream had quietly lost 133 channels. `derive_chop` was publishing
**0** and the COMP output read 366 instead of 499, with no error anywhere.

**Bug one: a rebuild unwired its consumer, and only one of them.** Re-running
`td_add_filter.py` left `coords` reading `filter` and disconnected `derive_chop`
from the same output connector. Reproduced deterministically - connect, verify 87
channels, re-run the builder, gone. The difference is what is doing the reading: a
group's In/Out CHOPs ARE its connectors, and destroying the Out CHOP breaks a plain
CHOP's wire while a COMP-to-COMP connection survives.

That is a new measured behaviour, and it retroactively explains why the old
`td_add_filter.py` had a "repoint every consumer" step that I deleted in the
restructure. Deleting it was right - each consumer stating its own input is the only
arrangement that survives a change of build order - but it was load-bearing for a
reason I had not identified: somebody had to re-establish the wires a rebuild broke.
The better fix is not to break them. Every group builder now PRESERVES `in1`/`out1`
and replaces only the working operators, so a rebuild is invisible downstream
whatever is reading.

**Bug two, and this one cascaded.** After fixing that, the output was 381 with every
latch channel missing and `derive_chop` still at 0 - but connected, and with no
errors. The toggles told the story: `Verbosity` read **"Minimal"**, whose preset is
exactly `(Landmarks, Coordstx)`, which is exactly the state observed.

`appendMenu` on an existing parameter resets it, and a MENU resets to INDEX 0 -
"Minimal" happens to be first in the dict. This is the recorded
`appendFloat`-clobbers-a-value trap in a different costume, and the guard for it was
already in the file one line away: the toggles are restored from a snapshot taken
before the appends, but the menu was restored only `if "Verbosity" not in was` -
i.e. only when it was NEW. Which is never, on a re-run.

What makes it worth a journal entry is the amplification. A Parameter Execute DAT was
watching `Verbosity`, so the reset did not stay a one-parameter problem: the callback
faithfully applied the Minimal preset, which disabled every derive group, which took
`derive()` to zero channels, which left the latch bank nothing to select. **One menu
reverting cost 118 channels and every latch, and every operator involved behaved
exactly as designed.** Callbacks turn a clobbered parameter into a clobbered network.

The rule that covers all of it, now written into DESIGN.md 2.11: read a stored value
before an append and write it back UNCONDITIONALLY. "Only when the parameter is new"
is a different question from "only when the COMP is new", and both are the wrong
question.

**A workflow finding, too.** Running six builders in one `exec` raised "Invalid OP
object. The node this python object referenced has likely been deleted" partway
through and left the temporal group with 5 operators instead of 73 - while the
surrounding script carried on. The same six, one per call, all succeed. They share a
Python namespace when chained and each destroys operators the previous one's
module-level state may still reference. One builder per call, as the docs always
said.

Verified after both fixes: 499 channels, every probe channel present, no operator
errors, re-running `td_add_filter` and `td_add_groups` changes nothing, and
`td_verify_latches` reports ramp **+4** with fires - releases == engaged on all five
rows.

## Making it cheaper, and finding out that gating is not free

*2026-08-21. The six-item pass the user asked for: bring the cook time down, gate
what is left, use `scope` instead of Select+Merge, give the face landmarks world and
pixel coordinates, add a screen-space-only toggle, and make the networks readable.*

### The first thing built was an instrument, and it earned that immediately

`cookTime` is a last-value gauge. This project has thrown away three separate sets
of figures because of it, and I was about to produce a fourth. So the first thing
this session built was `tools/td_profile.py`: samples every operator across 90
frames from a scheduled callback, reports the delta in `totalCooks` alongside the
median duration of the cooks that actually happened, and **discards the whole run if
`seq` did not change.**

It discarded its second run. The synthetic senders had expired — 200 cycles at 3
seconds is ten minutes and I had been going longer than that — and every number in
that run was the last value from whenever the stream stopped. Without the check I
would have written those numbers into `DESIGN.md`.

Building it also cost three of TouchDesigner's own traps: `totalCooks`, not
`cookCount`, which does not exist; `td.run`, not `run`, which the MCP bridge's exec
context does not have; and `op(path).module`, not `mod(path)`, for the same reason.
The last one is the nasty one — **a scheduled string that raises NameError does so
silently.** The schedule fired ninety times and the report saw zero samples.

### `scope` is everything the user suspected, with one exception

A CHOP's `scope` parameter does Select+op+Merge in one operator, and unscoped
channels pass through bit-exact — verified by lagging a scoped channel and comparing
an unscoped one for exact equality. Each `filter` group went from four working
operators to one: **0.4380 ms to 0.1527 across the three streams**, and the failure
mode that has cost this project the most time is now impossible rather than merely
tested for. There is no Select whose pattern can miss a channel.

The exception, found by trying fourteen operators rather than assuming: **`scope` on
a Logic CHOP DELETES the unscoped channels.** Three in, two named, two out. Logic is
what the latch bank and the presence debounce are built from, so that would have
been an expensive assumption.

And `^` does not exclude in `scope` any more than in `channames`, for the same
reason: the terms are a union. `^a` alone does mean "everything but a" — but
`^a ^b` is (all but a) OR (all but b), which is all of them. `* ^*_conf ^*_quality`
scaled every channel including the two it named.

### What replaced the long name lists

`coords` could not use `scope` — a coordinate branch produces new channels alongside
the originals, and `scope` modifies in place. But its Selects were carrying up to
362 literal channel names, and that one Select cost 0.1725 ms per cook, more than
the filter it fed.

So `spaces.compact_pattern()`: given the wanted names, the universe they will be
matched against, and some candidate patterns, it EXPANDS each candidate with
`fnmatchcase` and returns it only on an exact match, falling back to the literal
list. That is the part that makes a wildcard safe to attempt at all — `DESIGN.md`
2.11 records twice that a wildcard cannot partition these channels, and both times
the damage was silent. Now a pattern is checked before it is written.

It needed one more measurement to be trustworthy: **TouchDesigner's matcher agrees
with `fnmatchcase` on `*`, `?` and `[0-9]`**, verified by expanding the same
patterns both ways. Which is what lets `f0_*_[0-9][0-9]_x` separate 87 landmark
points from `f0_bbox_x` — every landmark name has a two-digit index and the box does
not.

### The face landmarks, and the operator that did the whole composition

A face's points are normalised to its bounding box, so `_tx` needs
`bbox_x + point_x * bbox_w` before the usual centre-and-scale. Written out that is
`point * (bbox_w * K) + (bbox_x - 0.5) * K` — and a Math CHOP computes
`(value + preoff) * gain + postoff`, measured. So `gain` and `postoff` as
expressions do the whole two-step composition in one operator, evaluated once per
cook. The alternative I nearly built was an Expression CHOP composing per channel:
608 Python evaluations a frame on the main thread.

I tried the obvious thing first — a Math CHOP multiplying the 87 landmark channels
by the one `bbox_w` channel — and **it does not broadcast.** Two inputs of 2 and 1
channels produced FOUR channels: the inputs concatenated, with the collision renamed
`f0_bbox_w1`. It matches by name and there is no parameter to change that.

Verifying it needed a synthetic face with real landmarks, and that turned out to be
necessary rather than nice. With every point at zero, `_tx` reduces to the bounding
box alone — a completely broken `point * bbox_w` term would have looked perfect. So
`synth_face.py` grew a 76-point constellation: a hand-laid caricature with the head
angles applied, which is enough to prove a channel is wired to the right name and
moves when the head moves, and is not enough to tune anything against. It says so.

### The number that did not go the way it was supposed to

**Like for like, the COMP got 4% SLOWER: 1.6686 ms to 1.7381.**

| change | before | after |
|---|---|---|
| the three filter groups | 0.4380 | 0.1527 |
| `derive_chop` | 0.2815 | 0.2037 |
| the three coords groups | 0.2941 | 0.6260 |

The filter and the Script CHOP gave back 0.36 ms. Splitting `coords` into gateable
halves cost 0.33 — eight extra COMP boundaries and an extra merge layer, paid
whether anything is gated or not. And a frozen half still costs its channels at the
merge, which is the point of it keeping them.

I could have reported the 1.1367 ms of the configuration a project actually runs and
called it a 32% improvement. It is a real number and it is the one that matters to a
user, but it is not a like-for-like comparison and presenting it as one would be a
lie by framing. **Gating is not free. It is right here anyway** — every
configuration that runs has something switched off, and with the attribute layer off
the COMP is 0.6031 ms with 29 of 287 operators cooking, which is the thing the
toggles were for.

One thing inside that regression was a straightforward mistake of mine. I had
collapsed Select+Rename into a single Select doing both, on the assumption that
fewer operators is cheaper. Measured in the live network: **0.0418 ms for a Select
doing `channames` and a rename, against 0.0138 for the same Select doing only
`channames`.** Moving the rename onto the Math CHOP that was already there took the
pair from 0.0515 to 0.0335. My scratch-network benchmark had said all the variants
were identical at 0.001 ms — because operators in a scratch COMP are not
time-sliced, so it was measuring a different thing entirely. The live network is the
only honest place to measure this.

### Then the tidy-up found four real bugs

Asked to lay the networks out properly, I put every coordinate in one table
(`visionhands/td_layout.py`) with a test that nothing collides, and wrote
`tools/td_verify_layout.py` to check the live network. It found, on the first run:

- **`td_add_filter.py` and `td_add_coords.py` had both been placing their group at
  (-400, -300)** — exactly on top of each other, for as long as both files existed.
- **`temporal` and `latches` were 58 and 49 columns wide** because a single running
  counter advanced for every operator whatever row it was on. One counter per row
  makes them 22 and 21.
- **Two operators called `tmp_motion_callbacks`**, one orphaned and one sitting
  exactly on top of `tmp_held_prev`. Creating a Script CHOP auto-creates a docked
  callbacks DAT, and the builder had been destroying TD's copy by guessing the name
  it would collide into — a guess that stopped being right when the Script CHOP moved
  into the group.
- And once that DAT lived inside the group, **every "destroy each child" loop
  started failing intermittently**, because destroying a Script CHOP destroys its
  docked DAT too and the snapshot list still holds a reference. I had written the
  first occurrence off as MCP flakiness. It was not. `child.valid` is the guard.

### The one that could have cost somebody real work

While re-running the chain I noticed the hands stream had **zero cooks** on `coords`
and on `out1`, while `temporal` and `latches` kept cooking off their own clocks. So
the COMP looked busy and a third of it was dead.

`td_build_vision.py` destroys and recreates everything at the master level that is
not a stream child — and that included `out1`, `out2` and `out3`, which ARE the
COMP's output connectors. Every run of it silently disconnected whatever the project
had wired to them. Three operators reading output 1 were orphaned, so nothing pulled
the hands stream and it stopped cooking. Nothing errored, in TouchDesigner or in the
builder.

This is exactly the failure `_clear_keeping_ports` exists for in every group
builder. The master had the same hole and nobody had looked. Fixed the same way —
the Out CHOPs are reused, never replaced — and verified by running the builder again
and checking all three external connections survive. I reconnected the three
orphaned operators from their patterns (`*_tx`, `*_ty`, `*trigger*`, all
unambiguously hands) and flagged it, because that repair is a judgement and the
user's project is the user's.

Two smaller ones came out of the same pass: `td_add_groups.py`'s frozen-channel
check was comparing every gated group against the HANDS stream's output, which was
correct until pose and face got gated groups and then reported four false failures;
and `send_synthetic.py` was sending 137 channels where the sidecar sends 141,
omitting the four `sc_*` channels — which are precisely the set that once vanished
silently between the OSC In CHOP and the COMP output. It sends them now.

### What I would want to know next

The screen-space toggle leaves 24 channels behind, and they are the interesting
ones: `h{i}_palm_x/y`, `h{i}_pinch_x/y` and `h{i}_point_x/y` are derived POSITIONS
with no companion — they should have one. `h{i}_vel_x/y` and `h{i}_dir_x/y` need a
different rule, because a rate and a unit vector do not transform like a point: a
direction in world space is not the image-space direction, since y is scaled by the
aspect. That is a real piece of design, not an oversight to sweep up.

And the review gate is now SEVEN milestones unpaid.

### Addendum, same day: the 24 channels, and what they turned out to be

The screen-space toggle left 24 channels behind because they had no companion, and I
wrote that up as a gap for next time. Then I looked at what they actually were, and
the answer was more interesting than "some missing branches".

`derive()` and `temporal` publish 100 channels ending in `_x` or `_y`, in four kinds:

- **12 POSITIONS** — `palm`, `pinch`, `hands_center`, `index_center`. Points in the
  image, exactly like a joint. Simply missed, and a project drawing the palm centre
  had to redo the transform by hand. They have companions now.
- **4 RATES** — `vel`. Normalised units per second, and the extent's rule fits it
  exactly: scale, no offset, because a velocity of 0.2 is 0.2 wherever the hand is.
  Companions now, and the world ones are world units per second.
- **8 UNIT VECTORS** — `point`, `dir`. **These cannot have companions, and that is a
  conclusion rather than a gap.** World space scales y by the render's aspect, so a
  transformed unit vector is neither unit length nor pointing where the image-space
  one pointed. Re-normalising it is a magnitude and a divide — a different branch.
- **84 HAND-LOCAL** — the `h{i}_d_<joint>_x/y` descriptor channels are joint offsets
  rotated into the hand's own frame and divided by hand size. A shape signature with
  no image position in it at all.

So 16 of the 24 got companions and 8 are correctly excluded, which makes the toggle
complete rather than approximately right. The arithmetic is the identical `Branch`
machinery — `derived_branches()` returns the same tuple type as
`transform_branches()` — so there is no second copy of the coordinate formula.

Two consequences worth knowing. `coords` now READS `derive_chop` and `temporal`, so
it moved to the END of the build order: a consumer must state its own input, and an
input has to exist to be named. And the delete list gained channels that can be
ABSENT — a disabled derive group emits nothing, unlike a frozen native group which
holds its channels — so the builder checks presence only for the wire-contract half.

The pleasant part: the user's own network reads `*_tx` and `*_ty` off the hands
output, and those two Selects went from 42 channels to **50** without anyone
touching them.

## Bankruptcy on the review debt, declared rather than carried

*2026-08-21. A decision, not a piece of work.*

The gate ran five times — `583ed73`, `2ecfaa1`, `cfaa8a5`, `33e5d12`, `98194f1` —
and then stopped for 32 commits and 8,565 insertions. I priced reviewing that as
one batch (three scoped reviewers, a few hundred thousand tokens) and offered the
alternative: write it off explicitly and gate from here. The user took the second.

That is the right call and worth recording as a call rather than an omission. What
made the debt unreviewable was not its size but its shape: 5,571 of those insertions
are TouchDesigner builders that run on the main thread and have no concurrency at
all, and reviewing them under a standard whose scope is "the lock-free handoff,
teardown ordering, anything that can block TD's main thread" would have spent most
of the budget on the least dangerous code in the repo.

**What is genuinely unreviewed, and I would rather write it down than let it be
discovered:** about 1,300 lines across `engine.py`, `source.py`, `sidecar.py`,
`pose.py` and `face.py`. `LatestBox` became generic over a protocol and there are
three of them where there was one. Two more Vision requests run on the capture
thread. The sidecar's send path was rewritten around per-stream bundles with
independent try/except so that no stream can `return` out of the callback — a path I
wrote deliberately and have never exercised. If a teardown crash or a torn frame
ever appears, that is where to look first.

The distinction that matters: **written off is not pending.** The handoff said
"seven milestones unpaid" for long enough that it had stopped meaning anything, and
a permanent entry on a to-do list is worse than an honest closed one. §3 now records
the write-off next to the rule it suspends, with the date and who decided it, and
the gate resumes at the next milestone. A standard skipped seven times running is
not a standard — so it either runs from here or it gets deleted, and the user chose
for it to run.

## The Script CHOP question, answered — and two traps that nearly reversed it

*2026-08-21. Asked whether a chain like `temporal` could be collapsed into one Script
CHOP for efficiency, and whether we would actually get any.*

**Yes, 2.9x on that group. No, do not ship it.** 74 native operators at ~0.225 ms
against 4 at ~0.078, alternating A/B on the same live stream so neither side carries
the other's machine conditions. The saving is 0.147 ms: 6% of the COMP, 0.9% of a
frame.

Not shipping it is not timidity. `docs/ATTRIBUTES.md` gave three reasons memory
belongs in native CHOPs and all three survived measurement, the third decisively: a
`TemporalState` in operator storage is hidden Python state that a project reload
treats unpredictably. And `temporal` is already freezable — the whole attribute layer
off is 0.6031 ms — so a project that does not want presence and velocity already pays
nothing. Trading 74 verified operators for 200 lines of Python to save 0.147 ms from
the case that does want them is a bad deal.

The prototype stays, frozen and attached to nothing. If the attribute layer ever
grows enough for 0.147 to become 1.5, both the measurement and the code are here.

### The part worth writing down

I reported the wrong answer twice on the way, and both times the numbers were
internally consistent.

**First: "the prototype is SLOWER, 0.3605 against 0.2069."** My group rollup summed
`proto_callbacks` at 0.2874 ms — a DAT reporting its own COMPILE, with **one** cook
against everything else's eighty-nine. The Script CHOP itself was 0.0689 all along. A
cook COUNT next to every duration is what caught it, and it is the same lesson as
`cookTime` being a last-value gauge, one level up: the count is not decoration, it is
what makes the duration mean anything.

**Second: "the whole COMP is 12.8 ms and the cook rate has collapsed."** It had — to
26 frames in 89. Not because of the prototype. A browser was using 60% of the machine,
`cookTime` is wall-clock, and every figure in that run was inflated together, in
proportion, with nothing anywhere to say so. I went looking for the bug in my own code
first and found a plausible suspect — a `store()` on every cook — fixed it, remeasured,
and got the same 12.8. Which is the only reason I looked outside.

`tools/td_profile.py` now reports the ACHIEVED FRAME RATE and warns under 45 fps, and
flags any operator whose cook count is a small fraction of the busiest as a one-off
not to be summed. **A performance figure in this repo without an fps beside it should
be treated as suspect**, including the ones I have already written down.

The `store()` suspicion was wrong about the symptom and right about the practice, so
the fix stayed: `absTime.stepSeconds` is how a Script CHOP should learn its own `dt`,
and operator storage is not a per-cook scratchpad. `derive_chop` gets away with it
only because it writes when the channel set changes, which is almost never.

## Depth and tilt from a projection that has neither, and a 62-degree dead zone

*2026-08-21. Asked for hand and finger Z from hand size, and for hand XY angles
alongside the existing Z angle, both behind toggles.*

The observation behind the second request was exactly right: every angle this system
published was in-plane. `h{i}_rotation`, `h{i}_point_angle`, `h{i}_pinch_angle`,
`hands_angle` — all `atan2` on two image coordinates, all rotations about the camera's
Z axis. `ATTRIBUTES.md` had even labelled one of them "In-plane roll".

What I could not do honestly was give back pitch and yaw. A projection destroys which
side of the image plane a point is on, so a palm tilted 30 degrees toward the camera
and one tilted 30 away project identically. Unsigned numbers published under those
names would be a lie about what they are, so the channels are `tilt` (magnitude) and
`tilt_axis` — which is what the projection actually determines. Same for the
per-finger depth: foreshortening is real and its sign is not recoverable, and there is
a test asserting the two cases produce the same number so nobody later "fixes" it.

### The dead zone

`Palmarea` is the face-on area of the palm triangle over size squared, and `tilt` is
`acos(measured / Palmarea)`, so it is the zero point of the whole channel. I guessed
**0.14**. Measured against `synth.py`: **0.3059**.

The guess put the ratio above 1.0, where the clamp I had written for safety pinned
it — so `tilt` read exactly **0 for the first 62 degrees of rotation** and then
started moving. Every test passed. `test_a_palm_facing_the_camera_has_zero_tilt`
passed *because* of the bug: a face-on hand read 0, which is what it asserted.

What caught it was noticing the value was **exactly** 0.0000 rather than nearly zero.
A clamp reaching its limit and a quantity genuinely at zero look identical in a
number, and only one of them should be exact. Then measuring the thing I had guessed
took thirty seconds.

The lesson I want to keep: **a guessed constant that a clamp can hide is worse than a
guessed constant that cannot**, because the clamp converts a wrong value into a dead
zone, and a dead zone in the middle of a channel's range is invisible from either
end. The test now asserts which failure mode `Palmarea` should have when it is wrong,
so the default cannot quietly drift to the side that reports tilt on a flat hand.

### And the gap underneath

Adding two tuning constants meant finding out where the others lived, and they did
not. `derive()`'s `_params()` reads eight thresholds by name with a getattr fallback,
and **nothing in the repo ever created them.** `Confthreshold`, `Curlmin`,
`Spreadmax`, the lot — stuck at their dataclass defaults since they were written,
with a panel that gave no sign and a helper whose fallback made it work perfectly.

Worse in a small way: the hand-written `_params()` listed eight of the nine fields
that existed, so `Overlapthreshold` was unreachable twice over. It is built from
`dataclasses.fields(Params)` now, which is the only arrangement where adding a field
cannot silently fail to arrive.

Eleven parameters on the Tuning page that a user would reasonably have assumed were
already there. Everything in both new groups is unmeasured on a real hand — the
arithmetic is exact and tested, the usefulness is not established, and `Zreference`
and `Palmarea` are the two to move.

### Addendum: the prototype would not let the project save

The user tried to save and got `PicklingError: Can't pickle <class
'visionhands.temporal.TemporalState'>: it's not the same object as
visionhands.temporal.TemporalState`.

TouchDesigner pickles operator storage into the .toe, and I had put the prototype's
recurrence state there. So the third reason `ATTRIBUTES.md` gives for keeping memory
in native CHOPs — "no hidden Python state for a project reload to treat
unpredictably" — turns out to be understated. The state does not reload
unpredictably. **The project does not save.**

The specific failure is one this repo manufactures for itself, which is the part I
should have seen coming: pickle compares an instance's class against the module's
current class by IDENTITY, and every builder here purges `visionhands.*` from
`sys.modules` so that edits are visible. After any rebuild, the stored object's class
is a different object with the same name and pickle refuses. I had written that
purging trap into `DESIGN.md` myself and did not connect it to storing an instance.

Two things worth carrying:

- **Store only primitives.** `derive_chop`'s channel-name cache is a tuple of strings
  and was never at risk. I verified that by pickling every storage entry in the
  project rather than reasoning about it — five entries, one bad, and it is worth
  having a way to check.
- **Storage outlives the code that wrote it.** Removing the `store()` call does not
  remove the value: the .toe kept failing to save against a callback that no longer
  wrote anything. The builder `unstore`s the stale keys explicitly now.

I verified the fix the way the save does — `pickle.dumps` over every operator's
storage — rather than by writing to the user's project file, and then confirmed the
prototype still advances its recurrence (held 8.58 → 19.7 over 667 cooks).

The verdict does not change. It gets firmer: a shipping version of that Script CHOP
would have to make a recurrence's state survive a save and reload in a form
TouchDesigner can serialise, which is most of what a Feedback CHOP hands you for
nothing.

## One output for beginners, and the operator that cost ten times what it should

The COMP had three output connectors and, with everything switched on, 1,863
channels across them. The user's reason for collapsing that was not performance:

> I'm going to be rolling out this component to beginners and don't want them to
> immediately get 1000 channels in the out.

So: three streams into one Merge, a trim, one Out CHOP. `Delete Empty Channels` on
the Vision page bypasses the trim, and the list is generated by `td_add_groups.py`
from the groups that are frozen — read off each group's own `out1`, because a
channel-to-group map still does not exist and would go stale the first time a
builder added a channel.

**I built it as a Delete CHOP, because "add a delete" is what was asked for, and it
was the wrong operator by an order of magnitude.** Same 1,863 channels in, same 306
out, median of 30 forced cooks:

| operator | list | ms |
|---|---|---|
| Delete | 35 drop patterns | 0.6697 |
| Delete | 306 literal keep names | 3.3618 |
| Select | 306 literal keep names | 0.0555 |

I had spent effort on the wrong thing first: a compaction pass that reduced 1,458
channel names to 8 wildcard patterns, verified exact against the live channel set in
seven configurations. It worked, and it was the wrong lever — the Delete's cost grows
with list length times input channels, so shortening the list from 306 names to 35
patterns bought 5×, where changing the operator bought 60×. The compaction is gone;
`trim_empty` names all 306 channels literally and costs less than the shortest
possible Delete.

The user asked the right question — "how come? should we replace it with a select?" —
before I had drawn that conclusion out loud.

Three things a keep list changes, and none of them were free:

- **it fails closed.** A Delete's drop list passes an unnamed channel through; a
  Select's keep list drops it. So every attribute toggle now has to rewrite the list,
  not just the ones that gate cooking. Without that, `Descriptor` on would compute 84
  channels and throw them away, and the toggle would look broken.
- **a Select emits in `channames` order**, so the list is generated in merge order.
- **an empty list is not free**, so the trim is bypassed whenever nothing is frozen.

### The thing I nearly shipped past: channels that had already vanished

Verifying the trim meant reading each frozen group's channels, and `hands/temporal`
reported **0** where it should have reported 27. `latches` 0 instead of 70. The hands
stream 406 channels instead of 503. `DESIGN.md` 6.2 is the rule that channels never
vanish, `ATTRIBUTES.md` promises frozen groups hold their last value, and neither was
true at that moment.

`allowCooking = False` does hold — until something asks the group for fresh data it
cannot produce. `cook(force=True)` on an ancestor does exactly that, and so does an
upstream SHAPE change: flipping `Descriptor` re-shapes `derive_chop`, dirties the
frozen groups downstream, and dirty-plus-frozen reads as empty. Nothing is logged.

The trigger was in the shipping builder: `td_build_vision.py` force-cooked the whole
master just to print channel counts, and every builder that ran afterwards then
reported false failures against the empty groups — `td_add_coords.py` produced seven,
which is what made me look. A plain `cook()` gives the same counts.

The repair is in `_apply_gating`: unfreeze, cook `out1`, freeze — for every group it
is about to freeze, not just one that has never cooked. `td_add_groups.py` now
provokes the failure with a forced master cook and then verifies the repair, in that
order, because a check that runs before the thing that breaks it proves nothing. That
is the second time this project has had a verification pass because the state it was
checking had not been disturbed yet.

**What I did not fix.** `td_add_latches.py`'s self-check compares against the full
latch table whatever is enabled, so with `Pose` off it reports nine failures for the
two `grab` latches it correctly dropped. Pre-existing, reproducible before any of
today's changes, and left alone rather than swept into an unrelated commit.

## The pace question, and the bug that answered it

Mid-session the user asked whether the builder infrastructure had become the
problem — "every tiny single simple change to this network takes you a long time."
Fair question. My accounting of the preceding task: ~15% the actual change, ~25%
measurement, ~20% a wrong turn of mine, ~40% doc passes and chain re-runs.

The infrastructure was not the answer. It is what let a 60× wrong operator choice
cost twenty minutes. Three other things were, and all three are fixed:

**The chain was all-or-nothing.** Eight builders to change three operators.
`tools/td_rebuild.py` now takes a layer and runs what that layer needs — `master` is
two scripts. It only became possible once `td_build_vision.py` stopped destroying
master-level operators other builders own; it had been removing the gating callback
and then relying on somebody re-running the whole chain to put it back.

**Two self-checks cried wolf every run.** `td_add_latches.py` reported nine failures
whenever `Pose` was off, `td_add_coords.py` seven whenever a stream was frozen. Both
correct networks. The cost is not the reading — it is that a real failure hides among
them, which is exactly what happened next.

**Five documents per change.** `STANDARDS.md` 4 now says which. DESIGN 2 and
ATTRIBUTES always; JOURNAL per milestone; BUILD_PLAN only for a new step; HANDOFF
only when handing off.

### And then the fix found the bug

`td_rebuild.py` runs its builders back to back in one frame, which the eight-script
chain also did. With `TARGET = "master"` — two builders, both individually verified
clean — the network quietly fell apart: `derive_chop` published 0 channels,
`temporal` went from 27 to 12, `latches` from 72 to 45, and the output from 505
channels to 366. No error anywhere.

`Verbosity` was sitting on `Minimal`. The mechanism, and it is worth carrying:

- `appendMenu` on an existing parameter RESETS it, and a menu resets to index 0,
  which in `PRESETS` is `Minimal`. This was already documented — `DESIGN.md` 2.11 —
  and the code knew it, and wrote the tuned value straight back on the next line.
- **Parameter Execute callbacks are deferred to the end of the frame.** So the reset
  and the restore both queue, the builder prints the correct value, and the queue
  resolves afterwards with the reset winning.
- `Minimal` is `Landmarks` plus `Coordstx`. Every derive group off. Everything
  downstream of `derive()` starved.

I chased this through four wrong hypotheses before running the two builders
separately, which took thirty seconds and showed each was clean alone. That is the
lesson I keep relearning: **when "A is fine and B is fine but A then B is broken",
stop theorising and bisect.** I had the tool to do it immediately.

The fix is to never re-append: `_page` appends only what is absent. Which is also
better behaviour — a rebuild no longer overwrites a label someone edited.

Two things the episode says about verification generally, both now in `DESIGN.md`
2.17: a check that runs before the thing that breaks it proves nothing, and "correct
alone" is not "correct composed" when callbacks are deferred.

The whole chain now runs eight builders with zero failures, which it had not managed
once all session.

### Also, at the user's request: what the output no longer carries

The 80 per-joint `*_conf` channels, `sc_*`, `seq` and `age_ms`. The three
housekeeping families go to a `housekeeping` Null beside the output — 10 channels,
0.0037 ms, one click away rather than gone. The output is 258 channels, and the trim
got cheaper doing it (0.0438 ms against 0.0669) because a Select's cost tracks its
list length.

One judgement call, flagged: `*_conf_median` STAYS on the output. "All conf channels"
would take it, but it is the summary rather than a per-joint confidence, and
`DESIGN.md` 6.2 tells people to gate on it. Removing the channel the documentation
recommends would be a trap.

## The segmentation transport, and a mask that is the wrong shape

Step 9 researched this in August and parked it with a decision: a file-backed `mmap`
with a seqlock, not TouchDesigner's shared memory. Built now, plus the Vision request
that feeds it, and then tested against Apple's person segmentation over the fixture.

The transport went as designed. **0.02 ms a read, zero torn frames across 56,591
accepted** from a writer in a separate process running flat out. The only test that
proves anything is that one — a single-process test shares an address space and
cannot expose a visibility problem — so it is the one in the suite, with a payload
where every byte of a frame carries that frame's own number, making a torn frame one
that holds two values.

I was honest about the fence rather than confident. It is an uncontended
`threading.Lock` acquire/release, which is a real acquire/release barrier and not a
C11 `atomic_thread_fence`. 56,591 clean frames is evidence, not proof, and the
residual risk is one bad mask frame on a stream that replaces it in 16 ms. That trade
is stated in the module and in `DESIGN.md` 2.19 rather than glossed.

### A harness found a bug that reasoning had not

Writing a measurement script for the miss rate, the second iteration crashed:
`magic 0x0 is not 0x56484d42`. My first instinct was that the harness had raced —
which it had. But the harness was doing exactly what a Script TOP does: opening the
buffer as soon as it exists. `ftruncate` gives a full-size file of ZEROS, so between
creating the buffer and stamping its header the writer leaves a window where the
magic is 0, and my reader raised on it. TouchDesigner starting at the same moment as
the sidecar lands in that window every time.

Fixed both ends: the writer stamps the magic on the descriptor before the mapping
exists, and an all-zero header now reads as "nothing published yet". A non-zero wrong
magic still raises, because that one really is a mistake. The distinction is the
whole fix — "not ready" and "wrong file" look identical if you only check for
inequality.

### And then the thing that would have shipped broken

Segmentation measured cleanly: `fast` 2.21 ms, `balanced` 8.54, `accurate` 30.73.
Vision's own default is `accurate`, which is very likely the user's C++ plugin
pinning the frame rate at 50 fps — a question step 9 had left open as "needs a
camera", answered from a fixture.

Then I looked at the mask sizes: 256x192 for a 1280x720 input. **16:9 in, 4:3 out.**

I nearly wrote that down as a resolution and moved on. What made me stop was that
`accurate` returned 2016x1512 — also 4:3, at a completely different scale. So I fed
the same frame at four aspect ratios, and the shape is chosen by ORIENTATION alone:
landscape gets 4:3, portrait gets 3:4, and a SQUARE input gets a portrait mask. The
image is stretched anisotropically to fit, with no letterboxing — the subject spans
the full height, which I checked by row occupancy rather than by eye.

Undoing that needs two different scale factors and nothing in the pixels says what
they are. A consumer scaling by one factor would be **exactly right on a 4:3 camera
and quietly wrong on every other one**. This project keeps meeting that failure: not
a crash, a plausible picture. Two hours earlier a Delete CHOP was costing 12x what a
Select does and looking fine.

So the header carries the source geometry beside the mask's, and `source_scale`
returns the pair. Zero means "not stated" and yields `(1.0, 1.0)` — as wrong as
having no field at all, and deliberately not a guess. It is a wire-format change I
would rather have made today than after something depended on it.

One more difference worth having: **`fast` is not a smaller `balanced`.** It returns
only 0 and 255 — a hard alpha with no soft edge, where `balanced` has real
intermediate values. The levels differ in kind, so a feathered matte costs 8.54 ms
and not 2.21.

### What I deliberately did not build

The TouchDesigner side. `maskbuf.py` moves bytes and does not reorient its payload,
because a transport that silently flips its contents cannot be tested against a known
pattern — so the y-flip and the un-stretch both belong in the Script TOP, and it is
not written yet. Segmentation is also not wired into the live capture queue:
`detect_sample_buffer` exists and is unexercised, because the fixture path uses
`detect_pixel_buffer` and the camera needs asking for.

## The mask into TouchDesigner, and a restart that exposed a third door

A force quit came first. My own probe hung TouchDesigner — I swept four array shapes
through `copyNumpyArray` on a locked Script TOP to learn what it accepted, when one
shape and one question would have done. The lesson is small and I keep needing it:
probe the narrowest thing that answers the question.

### The restart found a defect the two earlier fixes had not

The project reloaded with `merge_streams` at 302 channels instead of 1,863. Frozen
groups again — third door. `DESIGN.md` 2.16 had already recorded two ways a frozen
group loses its channels (a forced cook of an ancestor, an upstream shape change);
this was project LOAD. A `.toe` saved with `Temporal` off reopens with that group
frozen and never cooked, so its channels simply are not there.

Worse than the channels, it broke the BUILDERS. `td_add_temporal.py` rebuilt its group
and then verified it while it was still frozen, so all ten structural checks reported
`got ()` on a network that was fine. `td_add_coords.py` reported eight missing
channels because its velocity branch reads the same group. Ten failures and one, all
noise — and the noise is the danger, because the day a real failure appears it will be
sitting in the middle of them.

And the repair was incomplete: one cook brought `temporal` back to 23 channels of 27.
The four missing were the velocity family, which is a Feedback CHOP chain whose
template input has to have cooked once before it can name its own channels. So "cook
it once, then freeze it" is not always enough, and I would not have found that by
reading the code.

The rule now: **a builder unfreezes what it needs to verify, and `td_add_groups.py`
sets the final state because it runs last.** With that in place the chain runs nine
builders with zero failures — and `td_add_coords.py`'s "built, not verified" message
for pose and face is gone, because those spaces are now actually verified. A message
saying a check was skipped had been sitting there looking like diligence.

### Then the Script TOP

Three operators: the Script TOP reads the buffer and flips y, a Fit TOP undoes the
stretch on the GPU, an Out TOP publishes it. 0.0915 ms and 0.0312 ms — about 0.12 ms,
next to the 0.11 ms the CHOP output costs.

The flip is in the Script TOP and not in `maskbuf.py`, and the reason is the test:
**a transport that silently reorients its payload cannot be checked against a known
pattern.** So I checked the Script TOP against one. A mask looks plausible either way
up, which is exactly why a mask cannot be the test — I published a frame with a bright
band across the payload's first 24 rows and a grey block down its first 24 columns,
and TouchDesigner showed the band at the top and the block at the left. That is the
whole verification, and it took less time than reasoning about it would have.

Four TD facts came out of it. `copyNumpyArray` needs three dimensions and takes uint8
natively but refuses float16. It sets the TOP's resolution from the array, overriding
`outputresolution`. `format = mono8fixed` keeps a mask one byte per pixel instead of
four. And `allowCooking = False` **raises** on anything that is not a COMP — "This
flag can only be disabled for COMPs" — which killed my first design for the `Segment`
toggle. I could have wrapped the three operators in a base COMP to get the flag back;
I decided a COMP boundary and a second connector to explain was a worse trade than
checking the toggle inside `onCook`, for an operator that only cooks when something
is looking at it. The toggle still earns its place: off releases the mapping.

### The hardening I did before letting any of it near TouchDesigner

A writer restarting at a different quality re-creates its buffer at a different size.
Touching a page past the end of a file that shrank is a **SIGBUS** — not an exception,
a process death, and inside TouchDesigner that is the user's whole project with no
traceback. So the reader stats the file on every read and remaps when
`(st_ino, st_size)` changes. The inode matters as much as the size: a writer
recreating its buffer at the same geometry gets a new inode, and a reader still on the
old one would report "nothing published yet" for ever, which is indistinguishable from
a sidecar that never started.

A file that has GONE is deliberately left mapped, because POSIX keeps it valid and
that is what lets a sidecar restart without faulting the reader. Verified live: writer
killed, buffer unlinked, and the TOP kept showing its last frame with no error.

### What is not done

`sidecar.py` still does not run segmentation, so `Segment` ships off and there is
nothing to read until something writes. That is the next piece and it needs the
camera.

## Segmentation on the live queue, and the fourth stream that is not a stream

Wiring rather than machinery: the request, the transport and the Script TOP all
existed. What was missing was the sidecar running it.

The interesting decision was taxonomic. Everything in `STREAM_NAMES` sends CHANNELS
over its own UDP port, and a 197 KB mask has nowhere to go on a datagram - §2.13
measured that ceiling months ago. But segmentation shares everything else that makes
a stream a stream: one launch flag, the same camera, the same serial queue, the same
`seq` and `captured_at` as the hands frame from the same buffer, and it is exactly as
capable of being requested and failing to start.

So two tuples. `STREAM_NAMES` is what has a port; `REQUEST_NAMES` is that plus the
portless requests. `port_for("segment")` raises **by name** and says where the mask
actually goes, rather than returning a plausible number - a wrong port is silent at
the far end, which is the failure mode this project keeps meeting. And `sc_segment`
joins the status channels, because the argument for the other four applies unchanged.

Three decisions inside the sidecar that I would not have got right by guessing:

**The buffer is created lazily, from the first mask's own geometry.** I started to
write a table of sizes per quality level, then realised it would need a second axis
for orientation - a portrait frame gets a 3:4 mask - and would be wrong the day Vision
changes a number. The mask knows how big it is. Size the file from it.

**The source geometry is what the camera DELIVERED.** DESIGN.md 3 records that the
session preset silently reverts `setActiveFormat_`, and that two spike runs produced
1080p while the log said 720p. A mask scaled against the *requested* size would be off
by exactly that difference, invisibly. And when nothing has been delivered yet it
states `(0, 0)` rather than a plausible default, which `source_scale` turns into
`(1.0, 1.0)` - as wrong as having no field at all, and deliberately not misleading.

**`stop()` stops the source before closing the buffer**, and that ordering has its own
test because it is invisible in either method alone. `_write_mask` runs on the capture
queue; closing first would leave a frame in flight writing into a closed mmap from a
thread still running. It also `close`s rather than `unlink`s, so a reader inside
TouchDesigner keeps its last mask instead of going black the instant the sidecar exits.

### One toggle, because two would have been a trap I set myself

`td_add_segmentation.py` had a `Segment` toggle for the read side, and the sidecar
needed a `Streamsegment` launch flag. Two controls a letter apart, for a thing nobody
wants half of. I deleted mine and had the Script TOP read `Streamsegment`, and moved
`Segquality` and `Maskbuffer` to the builder that owns the command line. That also
retires a comment I had written the same day claiming there was deliberately no
quality parameter because "this side only reads" - true of the read side, and it
stopped being the whole picture the moment the sidecar owned the request.

### The check that cried wolf within a minute

Adding one status channel made the contract 142 channels while the OSC In CHOP still
held the previous session's 141, and `td_add_filter.py` reported "the filter's scope
covers 83 channels, expected 84" on a filter that was correct. A contract that has
grown but has not yet been SENT is the *normal* state between a code change and the
next Start - the sidecar is a separate process. Both sides of that check now count
from what actually arrived, and it says so out loud when the contract is ahead of the
wire.

Two fixed today, one yesterday. The pattern is always the same: a check comparing a
live reading against a compile-time constant, correct on the day it was written and
noisy the first time the two legitimately differ.

### What is NOT verified

The live path. 506 tests, a fake source, a stand-in mask, and the whole builder chain
green - but no real sidecar has published a real mask into TouchDesigner, because that
needs the camera and the camera needs asking for.

## Why the mask was black, and a finding I had already written down

Step 18 shipped the sidecar wiring with the live path untested - I said so in the
commit and asked for the camera. The user ran it and got black.

The sidecar was blameless. The buffer held frame 666, 16,599 of 49,152 pixels lit,
source geometry stated, hard alpha exactly as `fast` should give. Reading it by hand
from the Script TOP's own callback module worked first time and returned the mask.

**The Script TOP was not cooking.** `totalCooks` sat at 151 across two calls seconds
apart - hundreds of frames. An input-less Script TOP has no dependency that can dirty
it, and there is no Cook Type parameter on one to override that with; its whole
parameter list has nothing to set. So the single cook it had done - before the sidecar
had created the file - published the blank, and it served that texture for ever.

`docs/BUILD_PLAN.md` has said for weeks that an input-less Script **CHOP** "cooks once
and freezes". I wrote that note. It cost time again because it was filed under the
operator family it was first seen in rather than under the behaviour, and I went
looking for a bug in the read path instead. It now says both, in the traps list where
the next person will hit it.

The fix is a parameter whose value changes every frame:

    absTime.frame if op.Vision.par.Streamsegment else 0

Verified both directions rather than one: cooks climb with the toggle on (151 to 640
in seconds, `null5` 3 to 103, and the reader appears in the callback's cache), and
settle with it off. That last half is a bonus - `Streamsegment` now genuinely stops
the work, which is the gating `allowCooking` refused a bare operator two commits ago.

### The part that was my own fault twice over

The blank frame was 256x192. A `fast` mask is 256x192. So "never read the buffer" and
"read a real mask" were **identical in the operator's resolution**, and there was no
way to tell them apart without running the callback's logic by hand - which is exactly
what I ended up doing. A five-minute diagnosis became a long one because I had chosen
a placeholder that impersonates the real thing.

It is 16x16 now: a shape nothing else in this path produces. The lesson is cheap and I
would rather have it written down than remembered - **a placeholder should be
recognisable as a placeholder.** Zero-filled data that is also the right SHAPE is a
disguise.

### On the cost, honestly

Under a forced tight loop `seg_mask` was 0.0915 ms. Cooking naturally, its `cookTime`
gauge reads 0.2802. Both are in DESIGN.md 2.21 because neither is the honest number
alone: the loop isolates the work, the gauge is wall-clock and includes whatever else
the frame was doing. The mask path is ~0.3 ms in situ.

## Depth, and the residual that lies

Depth Anything V2 Small through Core ML, in the sidecar, with its own TOP output and
the pin-based metric correction ported from the examples repo. The user asked for the
pinning specifically, and it turned out to be the part worth the most care.

**23.00 ms a frame.** Against hands' 3.41 and a 30 fps frame interval of 33.3. That is
not a number to bury: depth at 30 fps and hands at 30 fps are not both available, and
`docs/DEPTH.md` says exactly that rather than leaving somebody to discover it as
"choppy hands". It runs last on the queue, behind everything a live project reads.

**7,302 distinct fp16 levels per frame, against 256 for an 8-bit read.** That
measurement is why `maskbuf` grew an fp16 dtype instead of the map being narrowed on
the way out - and it is also why the user asked about fp16 through `copyNumpyArray` two
hours before I wrote any of this. They were ahead of me: `copyNumpyArray` refuses
float16, so the Script TOP widens to float32, 406 KB in and 812 KB out, 0.2911 ms for
the whole read/flip/widen/copy.

### The thing I am most glad I caught

The first real run printed `2 pins, dropped #1` with a worst residual of **0.000 m**.

That looks like a perfect fit. It is not a fit at all: **two points always fit a line
exactly**, so with two pins the residual is identically zero and checks nothing. And
the fixture produces it every frame - it is a person filling the frame, so one of the
three default pins is occluded and the solve correctly falls back to two.

A `0.000 m` on a panel is the most convincing wrong number in this entire system. So
`Solve` carries `residual_is_a_check`, false below three pins; the note says so in
words on every such fit; `Depthfitchecked` publishes it to TouchDesigner; and
`docs/DEPTH.md` has a table telling people to gate on that flag before the residual. It
cost twenty minutes to build and it is the single most valuable thing in the change.

I only saw it because I ran the thing and read the output rather than checking that it
did not crash.

### Where I split the module, and why it paid

`pins.py` imports numpy and nothing else - no Vision, no Core ML, no `maskbuf`. That
made it testable against a synthetic frame built to satisfy a known alpha and beta, and
the solve recovers both to 1e-6 with `metres()` reading each pin's true distance back
to a centimetre. 31 tests, no model, no camera.

Three of those tests were wrong when I wrote them: I poisoned a pin at 1.6 m and
expected it to be dropped, and it was not - because occluding a 1.6 m pin only puts it
0.4 m out and the threshold is 0.5. **The code was right and my test was too gentle.**
Moving the pin to the far wall, where an occlusion is a 3.8 m disagreement, made it a
real test of the mechanism instead of a coincidence.

### Two bugs in code I had already written

**My own out2/out3 migration was running on every build.** Capture every consumer,
reconnect it to `outputConnectors[0]`. Harmless while the COMP had only CHOP outputs -
and a hard error the moment it grew a TOP one, because connector 0 is no longer
necessarily the CHOP and every TOP consumer got repointed at it. The entire chain
stopped on the first run after adding `outdepth`. Two lessons in one line: a migration
that has already run should not keep running, and code that indexes a connector list
has to say which family it means.

**A 0x0 frame printed Fortran.** `numpy.median` of an empty slice is nan, the nan
reaches `polyfit`, and LAPACK writes `On entry to DLASCL, parameter number 4 had an
illegal value` straight to stderr. Not an exception - a line of Fortran in the
sidecar's log. Refused before sampling now.

### Shipping something we do not own

47 MB of weights that Apple already hosts. `models/` is gitignored, one `curl` script
fetches it, and `resolve_model` raises naming that script and the docs rather than
letting Core ML report a missing file - "No such file or directory" is not an
instruction, and a missing model is the first thing a new clone hits.

`pyobjc-framework-CoreML` went in as a hard requirement rather than an extra. A
framework that only surfaces when somebody flips a toggle is the worst possible place
to find a dependency.

### What is not verified

The live camera. Everything is fixture replay plus a writer in a second process; the
TouchDesigner side was verified against real model output flowing through the real
buffer at 1280x720, and the readouts populate correctly - but no sidecar has run depth
off the camera yet, because the camera needs asking for.

## `--streams hands`, and the two lines that were missing

The user turned `Streamdepth` on, pressed Active, and got no depth. The answer was in
one line of a log that did not exist yet.

### First, the log

`start()` launched the sidecar with `stdout=DEVNULL, stderr=DEVNULL`. Everything a
separate process says about itself - which streams it started, which model it compiled,
why a request refused to build - was being thrown away. So "why did depth not start"
was a question nobody could answer, including me, and I had shipped it that way.

`/tmp/visionhands_sidecar.log` now, truncated per launch with the previous run kept as
`.prev`. Its first line is the streams it actually started. With that in place the
diagnosis took one `cat`:

    visionhands sidecar -> 127.0.0.1 at 60 Hz | streams: hands

### Then, the bug

`--streams hands`. Not `hands,depth`. And the panel said depth was on.

I had added `Streamdepth` to the parameter table, added its `--depth-path` and
`--depth-pins` arguments, and added its line to the startup report - and never added
`("depth", "Streamdepth")` to the one loop that turns toggles into `--streams`. Three
places out of four.

The reason it was silent is the part worth keeping: the argv block I *did* write is
guarded by `if "depth" in streams`, so it never ran. A missing entry in the list made
the code that depended on it quietly skip itself. Nothing errored, nothing warned, and
the panel showed a toggle that was on.

**The only thing that said anything was `sc_depth` reading 0** - the status channel
whose entire purpose is to report what the sidecar ACTUALLY started rather than what was
asked for. It was right, it was there from the moment I added the request, and I did not
read it until the third diagnostic step. That channel exists because of a note in
DESIGN.md 6.4 saying a panel showing a stream on after somebody flipped it without
restarting "is simply lying". It earned its place today.

### What I changed so it cannot recur

One table, `REQUEST_TOGGLES`, defined once in the builder and interpolated into the
launcher DAT - so the loop and the argv extras and the report all read the same thing.
Plus a check in the builder that RAISES if any `Stream*` parameter on the COMP is absent
from that table:

    these Stream* toggles are not in REQUEST_TOGGLES, so pressing Active would launch
    the sidecar WITHOUT them while the panel says they are on

That is the cheapest possible guard against adding a request to some of its places, and
it is the second time this week the fix has been "one table instead of three lists".

### Verified live, on the camera

`--streams hands,depth`, `sc_depth = 1`, and the depth map arriving in `outdepth` at
1280x720 from the actual camera. One number worth recording from it: the camera holds
30 fps but `age_ms` sits at **~35 ms** rather than the ~6 ms it reads without depth.
That is the 23 ms of inference showing up as latency, exactly as DESIGN.md 2.22 said it
would - the frames keep coming, they are just older. It is now in the DEPTH.md
troubleshooting table next to "hands got choppy", because that is what somebody will
notice first.

## The pins become a list, and a Sequence that would not build

Two asks: `Segquality` as a dropdown, and `Depthpins` as an extendable list defaulting
to the example's three pins. Then, mid-work, a pin on/off toggle and a red overlay.

**`Segquality` was already a dropdown** - `style='Menu'`, three entries with their
costs in the labels, on the Vision page. Nothing to do but say so. Worth checking
before building: the alternative was quietly rebuilding a menu that already existed and
reporting it as work.

### The Sequence

TouchDesigner has a native parameter Sequence and that is plainly what was being asked
for. It does not work from Python in this build. `Page.appendSequence` creates the
header parameter and the `Sequence` object, and then nothing joins its block:
`blockPars` stays empty and `numBlocks = 1` raises "Could not set numBlocks to
specified value. Please check for potential name conflicts."

I tried four orderings before accepting that: block parameters named `Pin0m` - 0-based,
matching the `point0weight` convention the docs themselves describe - then `Pin1m`,
then appending before the header instead of after, then `insertBlock(0)` instead of
setting `numBlocks`. All four fail identically. Sequences on operators that already
have one are fine; `td_add_latches.py` has been setting `seq.const.numBlocks` on a
Constant CHOP for weeks. A CUSTOM sequence seems to need the Component Editor.

So: `Depthpincount` plus eight fixed rows, with the rows past the count disabled by a
callback. Raise the count, another row becomes editable - the same behaviour through a
mechanism I could verify. I would rather ship that and say why than ship a half-working
Sequence, and DESIGN.md 2.22 records the four attempts so nobody repeats them.

### The overlay, and the honest caveat in it

Red needs somewhere to live. A mono depth map has one channel, so drawing on it is not
a matter of picking a colour - the output has to become RGB. `Depthpinsdraw` switches
the Script TOP's format to `rgba32float` from the parameter callback rather than from
inside `onCook`, because changing a parameter from within the cook it would affect is a
race.

The caveat is the part worth having: **the crosses show where the pins are CONFIGURED,
not where the solve used them.** The solve is a launch flag, so moving a pin without
restarting moves the cross and not the maths. `Depthfitpins` is what says how many the
solve actually used, and if the cross count and that number disagree you have not
restarted. That is in DEPTH.md, because a red marker sitting exactly where you put it
is extremely convincing evidence that the number next to it is about that spot.

The cross has a gap at its centre on purpose. The whole point of looking is to see what
the pin is sitting on, and a filled dot hides exactly the pixels the solve samples.

### One test bound I widened, and why that is not cheating

`test_inference_cost_is_still_what_was_measured[balanced]` failed in a full-suite run
and passed on its own. The cause is real: the suite now runs Depth Anything V2 as well,
so two Core ML models contend for the Neural Engine inside one pytest session, and a 2x
headroom is not enough. Widened to 3x with the reason written next to the numbers - a
bound that fails under contention is not a regression guard, it is a coin toss. The
authoritative figures still come from `tools/depth_probe.py` on a quiet machine, which
is what STANDARDS.md 3 requires anyway.

---

## 2026-08-22 — Step 21 planned: the shipping design, and the venv that turns out to be unnecessary

Plan only, no build. The user's decision: one `.toe`, the files stay the source of truth,
the builders embed them, an Install button writes them back out and fetches the model,
and the status text probes on load. `BUILD_PLAN.md` step 21 is the design.

### The question that produced it was better than the answer I expected

"Why do we need repo root? What does TouchDesigner use that's in the repo?" I had been
treating the hardcoded paths as untidiness. Answering the question properly showed they
are not: `derive_callbacks` imports `visionhands.derive` **every cook**, both Script TOPs
import `maskbuf` every cook, and the sidecar is launched as `-m visionhands.sidecar` with
`cwd` set. The `.toe` cannot run without the repo, so fifteen hardcoded paths mean the
project opens on exactly one machine on earth. That is a blocker, not a tidy-up.

### The finding that changed the shape of the step

Before planning I checked what TouchDesigner actually bundles:

    python3.11   3.11.15, Derivative build      pip 24.2      numpy 2.1.2
    pyobjc       absent - the only thing missing

`requirements.txt` has said since the spike that the cp311 ABI match with TD is
deliberate. That was an argument about loading pyobjc *inside* TD. It turns out to be
worth much more: **the sidecar can run on TD's own interpreter, so there is no venv to
create.** `pip install --dry-run` through TD's own pip resolves all nine packages to
prebuilt `cp311-macosx_10_9_universal2` wheels, pyobjc-core included, so no compiler
either. Install becomes: write 19 files, `pip install --target`, fetch 47 MB. One button.

Not into TD's own `site-packages` - the app is code-signed with the hardened runtime and
a TouchDesigner update would delete our packages. `--target` keeps them ours, and using
the same interpreter makes the ABI correct by construction instead of by a pin somebody
has to remember to check.

Two things I deliberately did not claim. Resolving a wheel is not importing one, and TD's
CPython is a Derivative patch - that needs a one-minute test. And changing the sidecar's
interpreter may reset the camera TCC grant, or may attribute it to TouchDesigner, which
would be better; that needs the camera, so it needs asking for. Both are written into
21.7 rather than assumed.

### Two traps found while computing what to ship, not while shipping it

**`__init__.py` is invisible to an import-closure walk.** Nothing imports *from* it, so
walking the graph from `sidecar`, `derive`, `maskbuf` and `pins` finds 18 modules and
misses the one that makes them a package. An installer generated from that walk produces
a folder that fails on the first cook. So the check has to be "does `import
visionhands.sidecar` succeed in a fresh interpreter", not "are the 18 files present".

**Two copies of the code is a silent-wrong-answer defect, not an inconvenience.** A user
opening a newer `.toe` over an older install runs the OLD `derive` every cook with no
error and wrong channels. Existence checks cannot see it; only a version stamp can, which
is why the status field grows an `Update needed` state. Same class as two benchmark
figures that both look measured - the problem is not that one is wrong, it is that
nothing tells you which.

The pleasing part of the design: during development `Installroot` points at the checkout,
so the dev path and the ship path are one code path with a different parameter value, and
there is no second copy on this machine to drift.

---

## 2026-08-23 — renamed to appletd, and the one list that must not be renamed

`visionhands` -> `appletd`, everywhere: the package, the venv, the repo directory,
the OS-level paths, 96 tracked files and 728 occurrences. The repo is
`~/Documents/GitHub/appletd` and the venv is `~/.venvs/appletd`. Tests: 539 passed.
Ruff clean, mypy clean on the package (59 files, strict).

### THIS FILE IS NOT RENAMED, and that is the point

Everything else describes the current state and should say `appletd`. A journal is
the one document that must not, because an entry dated 2026-08-12 saying
"`appletd_osc` produced no channels" describes an operator that never existed under
that name. Renaming history to match the present is how a record stops being
evidence. Entries before today refer to the package as `visionhands`; that is
correct and stays.

### The one thing a blind sed would have broken

`SUPERSEDED` in `tools/td_build_vision.py` lists operator names to FIND AND
DESTROY - `visionhands`, `visionhands_osc` and four others that existed before the
COMP did. Renaming those to `appletd*` would have left the cleanup hunting for
names that never existed, and the real orphans would have survived for ever. It is
annotated now with the reason, because an unexplained exception to a global rename
is a defect waiting to be "fixed".

Two live orphans from the pre-sidecar route are still at project root -
`/project1/visionhands_callbacks` and `/project1/visionhands_landmarks`, created by
`tools/td_setup_snippet.py` before the sidecar existed. They are not in
`SUPERSEDED` and nothing destroys them.

### Four builder FAILURES that are the same crying-wolf pattern, again

The full chain runs clean twice with no exceptions, and reports four FAILURES
blocks. Every one of them is a builder comparing a LIVE channel list against a
hardcoded expected tuple, with no account of the group being switched off:

    tmp_out_active   expected h0_active, got ()          Presence=0
    lat_valid        missing 'together'                  Contacts=0
    coords           expected h0_pinch_tx                Contacts=0
    groups           h0_vel_x not on out1                Motion=0

This is the third time this exact shape has appeared - DESIGN.md already names it,
"a live reading compared against a compile-time constant" - and it is still
generating four blocks of alarming text on every clean build. `derive_chop`'s 29
channels are exactly right for `Core` + `Twohands` + `Landmarks` with the rest off.
The checks need to read the toggles, or say "skipped, group off".

### Two things left in a state worth knowing

`Coordspx` came back from the rebuild switched off, which cost 96 channels on the
output until it was switched on again; `out1` is 222 now against 214 before, the
extra 8 being the derived positions in world space. And `hands/derive_chop` carries
a **cook dependency loop warning**. It produces correct channels, no operator in
the COMP is in error, and whether that warning predates today is not known - it was
not checked before the rebuild, which is the honest answer and also the lesson.

---

## 2026-08-23 — three output toggles, and why two of them cannot gate cooking

Asked for: a `Finger Tips Only` toggle, `h?_score`/`h?_conf_median`/`h?_chirality`
off the output, and `h?_bbox_*`/`h?_size` behind a toggle of their own. All three go
through the one Select in front of `out1`.

    baseline                        216 channels
    Finger Tips Only on              96
    Hand Box and Size off           200
    (the scalars, unconditional)    222 -> 216

### Neither new toggle gates any cooking, and that is forced

The obvious implementation is to gate the computation as well as the output. It is
not available. The fifteen non-tip joints feed every curl, spread and angle
`derive()` computes, and the bounding boxes feed `hands_overlap` at derive.py:359.
Gating either would break a two-hand attribute to tidy a channel list. So both are
output-only, and the comment says so, because "why didn't you gate this properly"
is the first thing a reader will think.

### The scalars went to `housekeeping` rather than being dropped

`td_add_groups.py` carried an explicit exemption for `*_conf_median`: it is the
SUMMARY confidence, DESIGN.md 6.2 tells people to gate on it rather than on `found`,
and taking the channel the documentation recommends off the output would be a trap.

That reasoning is still right, so the fix is not to overrule it - it is to keep the
channel reachable. All six hand scalars land in the `housekeeping` Null beside
`out1`, exactly as `seq` and `age_ms` did. The advice in 6.2 does not change; only
where you read the channel. Both the comment and ATTRIBUTES.md were rewritten,
because a comment asserting the opposite of the code is worse than no comment.

### A literal list, and the check that stops it drifting

`NON_TIP_JOINTS` has to be a literal: it lives inside the TRIM SCOPE markers, and
everything in there is copied verbatim into a generated DAT that may import nothing
but the standard library. A literal copy of a contract is a second source of truth,
and this one fails silently - a joint added to `types.JOINT_NAMES` would simply
never be trimmed, and `Finger Tips Only` would quietly leave it on the output.

So `_check_joint_split()` sits below the markers, imports the real contract, and
raises before anything is built. Same shape as `spaces.py`'s import-time check.

**The wrist is kept.** It is not an mcp, pip or dip, it is the hand's anchor, and
`hands_angle` is measured from it - so "fingertips only" is six points per hand.

### Both defaults preserve today's output

`Fingertipsonly` off, `Handbox` on. A new toggle that changes what an existing
project receives is a silent breakage, and the beginner-facing short list is one
flipped default away when that is wanted deliberately rather than as a side effect.

### And the deferred callback caught me out, again

Setting `Fingertipsonly` and reading `out1` in the same call showed no change at
all, and it looked like the toggle drove nothing. Parameter Execute callbacks are
deferred to end of frame - the same fact that reset `Verbosity` in August - so the
trim list had not been rewritten yet. Read on the next call it was 96 channels.
Worth writing down because the symptom is identical to a toggle that is not wired.

---

## 2026-08-23 — six small panel changes, and the silent failure they uncovered

`Handbox` moved to Attributes. "restart to apply" and "off freezes it" gone from
every label. `Renderw`, `Renderh` and `Orthowidth` are EXPRESSIONS now.
`Verbosity` reads "Level of Detail". The buffer paths were already
`/tmp/appletd_*.buf` - the rename did that on its own.

### Three one-shot reads became three expressions

The builder used to read `render1` and `cam1` once and copy the numbers in. Correct
at the moment it ran, silently stale the moment somebody resized the render - and
`_ty` is scaled by the render's aspect, so a wrong position looks exactly like a
right one. They are `op('../render1').width if op('../render1') is not None else
1280` and friends.

`op('../render1')`, not `op('render1')`. The expression evaluates on the vision
COMP, so the bare form searches the COMP's own children and finds nothing at all.

### Three failures on the way, each teaching the same thing

**`ParMode` is not reachable.** Not as a bare global, not off `td`. Two builds died
on it. Assigning `.expr` switches the parameter into expression mode by itself, and
a non-empty `.expr` is a better test for "driven by an expression" than any enum -
so `ParMode` is not named anywhere now.

**Deleting a variable left a dangling reference eight lines down.** Removing the
`render`/`camera` reads broke `if name in ("Orthowidth", "Renderw", "Renderh") and
(render is not None or camera is not None)` in the restore loop below. `main()`
raised there, halfway through. That check is now `if parameter.expr: continue` -
asking the parameter what it is cannot fall out of step with the block above, and a
hardcoded list of three names can, and did.

**A relabel has to be deliberate.** `_page` never touches an existing parameter's
label, for the good reason that a rebuild must not overwrite one somebody edited.
So renaming `Verbosity` did nothing at all until a `RELABELLED` table was added.
`Depthpinson` needed the same treatment in `td_add_depth.py`.

### And the real find: an empty keep list emptied the output in silence

Those two aborted builds left every parameter in `MOVED_PARS` at 0 - they are
destroyed and re-appended on every build, and the restore pass never ran. The next
build then faithfully restored the zeros. Every `Stream*` toggle read False, so
every group reported disabled, so the trim dropped all 1,786 channels and wrote an
empty keep list to a Select that was not bypassed.

`out1` went to **0 channels**. No error. No warning. No operator in the COMP
flagged. The builder printed "trim_empty  0 of 1786 channels kept" as though that
were a normal reading, and it took four queries to work out why.

`_apply_trim` now treats an empty keep list as a fault rather than an instruction:
bypass, and say "every group reports disabled - check the Stream toggles". A
component with everything switched off should show you everything frozen, not
nothing at all. That is the same principle as DESIGN.md 6.2 on vanishing channels,
one operator further downstream.

---

## 2026-08-23 — the delivered resolution, and a microbenchmark that lied

`Resw`/`Resh` were numbers somebody typed, and TouchDesigner converts to pixels with
`_px = x * Resw`. Nothing checked them against the camera. The capture session
silently reverts `setActiveFormat_` and two spike runs delivered 1080p while the log
said 720p (DESIGN.md 3), so the parameter and the truth could differ by a third with
every pixel coordinate wrong and nothing at all to see.

The sidecar knows the delivered size - `_source_px()` already returned it, honestly
zero when unknown - so it now sends `sc_src_w`/`sc_src_h`.

### `sc_` prefixed, and that did all the routing for free

Naming them `sc_*` made them housekeeping by construction. `sc_*` is already off the
output and already routed to the `housekeeping` Null, so **no trim list, no keep
pattern and no page had to change**. Picking the name that matched the existing
convention was worth more than any amount of new plumbing.

### Three implementations, and the first two were wrong

**A CHOP Execute on those two channels.** `onValueChange` fires when a channel
CHANGES, and `sc_src_w` is 1280 from the moment it appears and never moves again. The
callback never ran once. `Resw` sat on 999 while the camera actively contradicted it.
Wired correctly, scoped correctly - `channel` singular, a pattern, not `channels` -
and structurally incapable of doing the job.

**An expression on the parameter**, reading the channel. Always correct, no event to
miss, and I said it was free on the strength of a 0.575 us microbenchmark. It was
**+0.27 ms on the COMP**, about a fifth of it:

    hands/coords/pixels/math_px    0.0094 ms  ->  0.1431 ms
    whole COMP, 176 operators      1.3246 ms  ->  1.5984 ms

**A Math CHOP evaluates its gain expression once per CHANNEL, not once per cook.**
About 48 here, each walking `Math.gain` -> `Vision.par.Resw` -> the channel read. My
benchmark measured the right operation and the wrong count. A per-evaluation figure
says nothing until you know how many evaluations there are, and for a CHOP parameter
that number is channels.

**Written, reconciled on start.** `reconcile_source_size()` in the launcher, called
120 frames after the process comes up and once at build. Zero per-frame cost,
converges in about two seconds, and `math_px` is back to 0.0109 ms. Verified by
putting 999/555 in on purpose and watching it corrected: `Resw: 999 -> 1280, from the
camera`.

### `../render1` was wrong and every check agreed with it

The user caught this by resizing the render and watching nothing happen. A custom
parameter's expression resolves relative to the COMP's PARENT, so `op('../render1')`
found nothing and every expression fell to its `else` branch - 1280 and 720, which
are exactly what `render1` was already set to. **A fallback equal to the true value
hides a broken expression completely.** Same shape as the `_ty` render-aspect bug,
where the right and wrong versions agreed until the two resolutions differed.

`op('render1')`, as originally specified. Verified against a 1920x1080 render.

### And `Resw` got a floor of 1

Because 0 there takes every pixel coordinate in the component to zero with no error
anywhere, and it is reachable: the aborted builds left both at 0. `sc_src_w` reports 0
for "not stated" and the reconcile refuses to write it, so nothing legitimate wants a
zero. `clampMin` now makes it unreachable.

---

## 2026-08-23 — disabling `Presence` put the COMP in error

    TypeError: float() argument must be a string or a real number, not 'NoneType'
    (Parameter: const0value)  /project1/vision/hands/temporal/tmp_ready

`tmp_ready` is a Constant CHOP whose three values are expressions reading
`h0_active`, `h1_active` and `both_active`. Those are Presence-group channels, so
switching `Presence` off removes them from `derive_chop` - and **indexing a CHOP for
a channel that is not there returns `None`**, which `float()` will not take.

That is the same fact measured yesterday when deciding how `Resw` should read
`sc_src_w`: a missing channel is None, not an error and not zero. There it was the
convenient half of the behaviour; here it is the expensive half.

### Guarded through a named helper, not three inline `or 0`s

`chan_or_zero()` in `td_add_temporal.py`, used at the three sites that read a channel
by name. The name is the point - the docstring is where the reasoning lives, and the
reasoning is not "None is annoying" but **why zero is the right answer at these three
sites specifically**: every one is a flag or a counter where zero is the honest
conservative reading. Presence not computed IS not active; motion not computed IS not
moving; no `seq` yet IS not live. A latch that stays shut because nobody told it a
hand was there is behaving correctly.

### And where it is deliberately NOT used

`tools/td_add_coords.py` reads `bbox_*` through the identical construct, and it does
not get the guard. A zero gain there does not mean "unknown" - it collapses the
transform and puts every point at the origin. A plausible wrong position is worse
than an error, so that one is left to fail loudly if it ever can.

### Swept the rest rather than fixing the one that was reported

All 20 Attributes toggles, each flipped individually with gating applied
synchronously and the whole project force-cooked: no other toggle produces an error,
and none left residue behind. The two other guarded sites were found by grepping for
the construct rather than by waiting for someone to hit them.

---

## 2026-08-23 — removed the four toggles that could not do what they said

`Landmarks`, `Triggers`, `Motion`, `Events`. They sat on the Attributes page looking
like controls and could not remove their own channels from the output, because that
needs an exact channel-to-group map and a wildcard cannot separate the raw
`h0_wrist_x` from the derived `h0_palm_x`.

### They were not equally dead, and the difference mattered

`Landmarks` gated nothing anywhere - not one entry in `COOK_GATED`, `COOK_VETOED` or
`COOK_REQUIRES`. Genuinely inert.

The other three were **OR terms in `COOK_GATED`**: `("Presence", "Motion")` on
`hands/temporal` and `("Triggers", "Gestures", "Events")` on `hands/latches`. So each
did nothing on its own and something in combination - which is worse than dead,
because it is a control that works only when another control is in a particular
state.

### What removing `Motion` cost, stated rather than discovered later

`Motion` on with `Presence` off was the only way to express "cook `temporal` for its
velocity half, not its presence half". There is no way to say that now: `Presence` off
freezes the whole group, verified. `Presence` ships on so the common case is
unaffected, and putting the term back is one word in `COOK_GATED`.

The trade was taken because a toggle that does nothing alone and something in
company is harder to explain to a beginner than one capability fewer. That reasoning
is in `COOK_GATED` next to the change, not only here.

### Removing the code that creates a parameter does not remove the parameter

For the third time in this project. `td_add_groups.py` had no retire mechanism at
all, so it grew `RETIRED_PARS` and destroys those four before anything else in
`_page` - before the relabel pass and before `existing` is built, so a name being
retired cannot also be relabelled or counted as present.

### Five documents were asserting the opposite

The module docstring, the builder's own closing report, and three separate passages
of `ATTRIBUTES.md` - the channel-budget table, the parameter reference and the
"what the toggles gate" table - all described the four as advisory-and-known. Every
one now describes what is actually there, including the `Motion` trade. A comment
that survives the thing it describes is the defect this project keeps finding.

Verified: 16 toggles swept individually with gating applied synchronously, no errors,
no residue. 540 tests pass.

---

## 2026-08-23 — `Sidecar.run()` finally has tests, and they are mutation-checked

`run()` is the loop the whole process is, and it had no test. Everything around it
did - `send_once`, `stop`, the parent watch, all covered - which is exactly why the
gap was invisible: the pieces are individually correct and the ASSEMBLY is what
breaks. Four tests now, and each was verified by breaking the code on purpose:

    running set AFTER start            CAUGHT
    no stop() when start raises        CAUGHT
    no finally: stop()                 CAUGHT
    status line never printed          CAUGHT
    parent check never breaks          CAUGHT

### The mutation run changed the tests, twice

**Moving `running = True` below `start()` hung the suite** instead of failing it. The
test asked the source to stop during warm-up and then had no way out of the loop,
because it exits by giving `run()` a dead parent pid - so it depended on the very
behaviour it was checking. Fixed by giving that test a dead pid too.

**Deleting the parent-check `break` still hung**, for the same reason one level up:
every one of these tests ends the loop through that break. So `_bounded()` stops the
sidecar from outside after 3 s and RETURNS WHETHER IT HAD TO. The assertion is
`assert not overran` - the loop must end on its own - which turns "never exited" from
a frozen CI job into an ordinary failure with a readable message.

That is the general lesson and it is worth more than the tests: **a test that ends
the thing under test using the thing under test cannot fail, it can only hang.** The
bound has to come from outside.

### What each test pins

- datagrams actually leave and the loop actually returns 0. A mutation that stopped
  `send_once` being called left every other test in the file passing.
- `running = True` sits ABOVE `start()`, so a stop arriving during camera warm-up is
  honoured rather than thrown away.
- a raising `start()` still reaches `stop()`. It previously left the socket open and
  the source running.
- the status line carries `sends`, and `age`/`hands` only when hands is on - the
  distinction that made "why is depth not working" unanswerable in August.
