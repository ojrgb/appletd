# visionhands — coding and working standards

Agreed before implementation started. Binding on every file in this repo and on
every review agent. `design/DESIGN.md` is the source of truth for *what* we
build; this document is the source of truth for *how*.

---

## 1. Comment standard

The goal is that the logic can be understood from the code files alone, without
`DESIGN.md` open beside them. The rule that keeps that from becoming noise:
**comments carry information the code cannot** — intent, invariants, units,
thread context, native-API behaviour, and why the obvious alternative is wrong.
A comment that restates the line above it is deleted, not tolerated.

### 1.1 Four tiers, in every file

**Tier 1 — module docstring.** What this module owns; what it must *never*
import or touch; which thread its code runs on; the `DESIGN.md` section it
implements. The negative statements matter as much as the positive ones —
`engine.py` never imports TD, `td/*` never imports pyobjc, and the docstring is
where that is declared.

**Tier 2 — section banners.** `# ---- 75-col rule ----` around a named group of
concerns, as `reference/spike_vision_hands_live.py` already does. One concern
per section.

**Tier 3 — function and class docstrings.** A one-line summary, then whichever
of these labelled blocks apply:

```
Thread:    which thread/queue this runs on, and which it must never run on
Contract:  inputs and outputs with units and coordinate origin stated
Why:       the design rationale, including why the obvious alternative is wrong
Traps:     native-API behaviour that has already cost us time
Ref:       DESIGN.md section number
```

Omit a block rather than pad it. `Thread:` is mandatory on anything that can run
off the main thread. `Contract:` is mandatory anywhere a coordinate or a unit
crosses a function boundary.

**Tier 4 — inline comments.** On every non-obvious line: why it is written that
way, what invariant it preserves, what the native call actually does. Every
pyobjc gotcha is annotated at its own call site, not once at the top of the
file — the out-param pattern in particular:

```python
ok, err = seq.performRequests_onCMSampleBuffer_error_([req], sbuf, None)
#                                                                 ^ TRAP:
# out-param selector: pass None, get (ok, err) back as a tuple. DESIGN.md 2.3.
```

### 1.2 Greppable tags

| tag | meaning |
|---|---|
| `# TRAP:` | native-framework behaviour that has already burned us |
| `# INVARIANT:` | a property the surrounding code depends on and must preserve |
| `# MEASURED:` | a number with provenance — cite the figure and where it came from |
| `# UNMEASURED:` | a belief we have not verified; never phrased as fact |
| `# TODO(M4):` | deferred work, tagged with the milestone that closes it |

### 1.3 Names carry the contract

Units and frames of reference go in the identifier, always:

- `_norm` normalised 0..1 · `_px` pixels · `_ms` / `_s` time
- `_bl` origin bottom-left (Vision, TD) · `_tl` origin top-left (cv2, PIL)

The design's worst silent failure is a vertical flip that looks plausible. If
the names carry the frame, a wrong call *reads* wrong at the call site.

### 1.4 Constants and provenance

No bare magic numbers. Every threshold, dimension, timeout and grace count is a
named module-level constant with a comment stating where the value came from:
measured on the target machine, documented by Apple, or guessed. A guessed
constant says so.

### 1.5 Staleness is a defect

A comment that describes behaviour the code no longer has is treated as a bug of
equal severity. Comments are updated in the same edit as the code they describe
— never in a follow-up pass.

---

## 2. Code standard

**Typing.** Full annotations everywhere. `NewType` for coordinate and unit
distinctions in the core (`NormX`/`NormY` vs `PixelX`/`PixelY`) so the checker
catches the flip class of bug.

**Tooling.** `ruff check` on everything (`ruff format` is deliberately not used:
it hardcodes two spaces before a trailing comment and so destroys the aligned
annotation column that 1.1 tier 4 depends on. Formatting consistency is a review
concern instead). `reference/` is excluded from linting entirely - it is frozen
provenance, and its value is in being exactly what produced the measurements. `mypy --strict` on the pure
Python core (`types.py`, `coords.py`, `source.py`, slot assignment); relaxed per
module on `engine.py` and `td/`, where pyobjc and TD's API carry no type
information. Both are dev-only and never ship into TouchDesigner.

**Module boundaries, enforced by a test rather than by discipline.**

- `engine.py` imports nothing from TD.
- `td/*` imports no pyobjc.
- Nothing in the package imports `cv2` or `PIL` at module scope — TD's bundled
  Python will not have them. Debug-overlay code imports them lazily, inside the
  function that draws.
- No pyobjc object escapes the capture thread. `LandmarkFrame` is floats and
  tuples only.

**Error handling.** No bare `except Exception` around a native call. A Vision or
AVFoundation call landing on a torn-down session is a process-killing crash, not
a retryable error, and swallowing it hides the teardown bug that caused it.
Failures on the worker thread are recorded on the shared state so the CHOP can
publish them — never silently dropped.

**Assertions for the known traps.** Every trap in `DESIGN.md` 3 that can be
checked at runtime, is. Delivered capture dimensions are asserted against
requested dimensions first, because that one silently lied twice in the spike.

---

## 3. Working standards

**Milestone gate.** At each milestone in `DESIGN.md` 10, the diff goes to a
fresh review agent before any further work starts. Review scope is
**correctness and concurrency**: bugs, the lock-free handoff, teardown ordering,
anything that can block TD's main thread, anything that can call into a dead
capture session. The agent is handed `DESIGN.md`, this document, and the diff.
Findings are triaged and resolved or explicitly deferred with a reason before
the next milestone opens.

**Bankruptcy declared 2026-08-21, by the user, on everything up to `b06484b`.**
The gate ran five times and then stopped: the last triage was `98194f1`, and the
32 commits after it — pose, the `allowCooking` gating, face, the one-COMP
restructure, the landmark counts, the builder fixes and the optimisation pass —
accumulated 8,565 insertions without one. Reviewing that as a single batch was
priced and declined. It is **written off, not pending**: nobody should carry it in
a handoff as outstanding work, and nobody should go looking for the review that
covers `dabe41d`, because there is not one and there will not be one.

What that costs, stated so it is not discovered later: roughly 1,300 lines of the
written-off diff are concurrency-critical — three lock-free boxes where there was
one, `LatestBox` made generic over a protocol, two new capture requests and a
rewritten sidecar send path. Those are the lines §3 exists for, and they are
unreviewed. If a teardown crash or a torn frame ever shows up, that diff is the
first place to look.

**The gate resumes from `b06484b`** on the terms above, and the lesson from the
five that did run is why it is worth resuming rather than dropping: the last one
returned thirteen findings, four of which were invisible to their author, and one
of those was that the previous entry's headline measurement was arithmetic
nonsense. A gate that is skipped seven times in a row is not a standard, so
either it runs at each milestone from here or §3 should be deleted honestly.

**Version control.** One commit per milestone, so every review has a clean diff
and a bad milestone is revertible.

**Journal.** `docs/JOURNAL.md` gets a short entry per milestone: what was built,
what was measured with numbers, what surprised us, what is still unknown.

**The design doc stays true.** New measurements are promoted into `DESIGN.md` 2
with their figures. Questions in 11 are closed explicitly with the answer, not
left to rot. A design change is written into the doc *before* the code changes.


**Fixtures are hands-only, and the gate is people, not faces.** A committed
fixture is a video file that enters git history and stays there, so no fixture
containing an identifiable person may be committed. The first version of this
rule checked `VNDetectFaceRectanglesRequest` and that was the wrong gate: the
first real fixture passed it with zero faces while showing the user from the chin
down, in their own home. A body, clothing and a room identify someone perfectly
well. Use `VNDetectHumanRectanglesRequest` over every 10th frame, and look at a
montage of frames yourself before committing - the detector answers "is a person
in shot", not "would the subject mind this being public".

Fixture media is gitignored by default (`fixtures/*.mp4`, `*.mov`) so the mistake
cannot be made accidentally; un-ignoring a specific file is a deliberate act
taken after checking it. A fixture that stays out of git still works locally as a
benchmark input - it just cannot be cited as reproducible provenance, and the
journal has to say so.

**Measurement honesty.** Performance claims come only from deterministic fixture
replay. Live-camera timings are never quoted as results — the spike saw an 8x
spread on identical configs. Anything unmeasured is labelled unmeasured, in code
and in prose.

**And every figure goes into `docs/BENCHMARKS.md`.** Enrich it whenever a measurement
is taken — not at the end of a milestone, at the point the number exists, because that
is when its provenance is still known. Three things have to travel with it or it is
worth less than nothing:

  * **the machine.** Everything in this project rides on the Neural Engine, so a
    figure without hardware attached is unfalsifiable. The file states its own
    machine at the top; if a measurement was taken elsewhere, say so beside it.
  * **how it was taken** — fixture replay, a forced-cook loop, or TouchDesigner's own
    wall-clock `cookTime` gauge. These are not interchangeable and the gauge always
    reads higher. Where both exist, record both; picking the flattering one makes the
    file useless.
  * **what it is compared against.** "23 ms" means nothing; "23 ms against a 33.3 ms
    frame interval" is a decision. A bare number invites the reader to invent the
    comparison, and they will invent a kind one.

Delete a figure when it stops being true rather than leaving two. A benchmarks file
with a superseded number in it is the same defect as a stale comment (1.5), and it is
harder to spot because both numbers look measured.

**Human-in-the-loop points** are flagged as they arrive rather than worked
around: milestone 1 needs TouchDesigner booted by hand, the fixture clip needs
recording on the target machine, and the camera TCC prompt attaches to
TouchDesigner.app.

## 4. What each change owes the documentation — narrowed 2026-08-22

Five documents per change was the standing obligation, and it stopped being
proportionate: on 2026-08-22 a three-operator change to the master COMP updated
DESIGN, ATTRIBUTES, BUILD_PLAN, JOURNAL and HANDOFF, and the doc pass was a bigger
share of the work than the change, the measurement and a real bug put together. So,
per change:

| document | when | why it earns it |
|---|---|---|
| `DESIGN.md` 2 | **always**, if anything was measured | every figure in 2 has changed a decision. It is the reason a wrong operator choice costs twenty minutes |
| `docs/BENCHMARKS.md` | **always**, if anything was measured — the same trigger as DESIGN 2 | 2 is where a number is ARGUED; this is where it is looked up. A figure that exists only inside a paragraph of prose is a figure nobody finds when they need to compare against it |
| `docs/ATTRIBUTES.md` | **always**, if user-visible behaviour moved | it is the contract. A parameter whose documented behaviour is wrong is worse than one that is undocumented |
| `docs/JOURNAL.md` | **per milestone**, not per change | what surprised us and what it cost. Keep it short; it is not a changelog |
| `docs/BUILD_PLAN.md` | **only for a new step** — a new capability or a structural change | it had become a second journal, one numbered step per change. A change that fits inside an existing step does not need a new one |
| `docs/HANDOFF.md` | **when handing off**, not mid-session | it is a snapshot for the next session. Rewriting it while still working means writing it twice |

Unchanged: a stale comment is a defect (1.5), and anything unmeasured is labelled
unmeasured. Narrowing what gets written does not narrow what has to be true.
