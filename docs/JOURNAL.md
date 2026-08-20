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
