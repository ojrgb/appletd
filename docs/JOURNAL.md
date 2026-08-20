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
