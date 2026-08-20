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
