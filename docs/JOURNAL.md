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
