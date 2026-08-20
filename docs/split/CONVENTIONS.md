# The two repositories: names, split, and conventions

Proposal. Nothing here is applied yet.

## 1. Names and identity

| | repo A | repo B |
|---|---|---|
| GitHub | `apple-vision-landmarks` | `touchdesigner-apple-vision` |
| import / package | `vision_landmarks` | `visionhands` (keep — it is what the sidecar is called) |
| PyPI | `apple-vision-landmarks` | `touchdesigner-apple-vision` |
| one-line description | "Hand, body and face landmarks on macOS with Apple's Vision framework, from Python. No MediaPipe, no model files." | "Apple Vision hand tracking into TouchDesigner — 137 CHOP channels at 30 fps, no Python in your project." |
| topics | `macos` `apple` `vision-framework` `pyobjc` `hand-tracking` `pose-estimation` `face-detection` `computer-vision` `mediapipe-alternative` `apple-silicon` | `touchdesigner` `touchdesigner-components` `tox` `osc` `hand-tracking` `macos` `apple-vision` `creative-coding` `realtime` `mocap` |

Naming logic: both names contain the phrase people type into search
("apple vision"), repo B additionally contains "touchdesigner". Lowercase-hyphen
beats PascalCase for GitHub search. Say "Vision.framework, not Vision Pro" in the
first two lines of both READMEs — that collision will otherwise cost you every
ambiguous search.

Check both names on PyPI before announcing anything.

## 2. Which repo owns what

| today | goes to | note |
|---|---|---|
| `visionhands/types.py` | **A** | minus `channel_names()` / `channel_values()` |
| `visionhands/coords.py` | **A** | |
| `visionhands/engine.py` | **A** | plus `pose.py`, `face.py` when they exist |
| `visionhands/source.py` | **A** | `HandSource` is the seam both repos depend on |
| `visionhands/osc.py` | **B** | the wire is a TD concern |
| `visionhands/sidecar.py` | **B** | |
| `channel_names()` / `channel_values()` | **B**, as `contract.py` | the 137-channel flattening is TD-shaped, not landmark-shaped |
| `visionhands/td/*` | **B**, under `experiments/in_process/` | the measured-slower route, kept as provenance |
| `tools/td_*.py`, the `.tox`, the COMP | **B** | |
| `tools/record_fixture.py` | **A** | B depends on A for fixtures |
| `tools/probe_m1_in_td.py` | **B** | it is a TD probe |
| `reference/` spikes | **A** | frozen provenance, excluded from lint |
| `design/DESIGN.md` §1, 2.1–2.7, 3, 6.1, 7 | **A** | |
| `design/DESIGN.md` §2.8, 2.9, 4, 6.2, 8, 9 | **B** | |
| `docs/JOURNAL.md` | split by entry, both keep a `JOURNAL.md`, cross-linked | the journal is an asset, not clutter — see GROWTH.md |
| `docs/STANDARDS.md` | **both**, verbatim | it is the reason to trust either repo |

**Dependency direction is one-way: B depends on A, A knows nothing about TD.**
That is already true in the code (`engine.py` imports no TD, `td/*` imports no
pyobjc) and the boundary test that enforces it should ship in both repos.

B pins A as `apple-vision-landmarks>=1.2,<2` and states the exact version it was
measured against in its README table.

## 3. Versioning

- **SemVer on both packages.** A's public surface is `HandEngine`, `LandmarkFrame`,
  `HandSource`, the joint table and the coordinate helpers.
- **The OSC channel contract is versioned separately** in B's `docs/CONTRACT.md`,
  and published as a `contract_version` channel. Adding channels → minor.
  Renaming, reordering or removing → major. The COMP validates it and errors on
  itself when it does not recognise the value. Rationale: a landmark arriving in
  the wrong channel is plausible on screen and wrong, which is the worst failure
  class in the system.
- **TD compatibility is a documented matrix**, not an assumption: TD version →
  bundled Python → last release verified against it.
- Hand-maintained `CHANGELOG.md` in Keep-a-Changelog format. Do not automate it
  from commits; the interesting content is the measurement that changed.

## 4. Commits and reviews

Keep the current narrative subject line — `Match the camera: tx/ty now scale by
Ortho Width, not by a chosen convention` is better than
`fix(coords): ortho width`. Add two body conventions:

- any behaviour change states the measurement that justified it, with the machine;
- `Refs: DESIGN.md 2.8` so a reader can find the reasoning.

One commit per milestone stays the rule while you are the only committer; drop to
per-PR once anyone else contributes.

## 5. Licence and third-party

MIT on both. Add `THIRD_PARTY.md` listing pyobjc, numpy, opencv-python and pillow
with their licences — verify each before publishing rather than assuming. Note
explicitly that Apple's Vision framework is used through its public API and that
no Apple code or model is redistributed, because that is the first question a
cautious studio will ask.

`NOTICE`: TouchDesigner and Derivative are not affiliated with or endorsing repo B.

## 6. CI, and the fixture problem it exposes

GitHub Actions on `macos-latest` (Apple silicon runners), matrix `3.11` (TD's
version, mandatory) plus `3.12`/`3.13` for library users.

- `ruff check` and `mypy --strict` on the pure core, relaxed per-module for pyobjc
  and TD, exactly as configured today.
- **Tests must pass on a fresh clone with no camera and no video fixture.** They do
  not today, and this is the single most important change the split needs: mark
  camera tests `@pytest.mark.camera` and fixture-timing tests
  `@pytest.mark.fixture`, deselect both in CI, and commit a
  `fixtures/replay.jsonl` of *recorded Vision outputs* — landmark floats, no
  imagery, no privacy question — so contract, coordinate, slot-assignment and OSC
  tests run everywhere.
- A skipped test must print why it skipped. "105 passed" on a clone that silently
  skipped the interesting 30 is how a repo loses trust.
- Repo B additionally: an `osc.py` round-trip test against an independently
  written decoder (exists), and a `contract.py` golden file of all 137 names in
  order, so a reorder fails loudly.

Badges: tests, licence, platform, TD version, PyPI version. No badge for anything
you are not actually measuring.

## 7. Docs layout, identical in both

```
README.md            the whole story, in one screen plus scroll
CHANGELOG.md
CONTRIBUTING.md      the two rules from STANDARDS.md, restated for outsiders
CODE_OF_CONDUCT.md   Contributor Covenant, unmodified
SECURITY.md          camera privacy posture: loopback only, no telemetry, no network by default
THIRD_PARTY.md
docs/
  MEASUREMENTS.md    every number, with machine / OS / fixture / date
  TRAPS.md           the annotated trap list
  STANDARDS.md       how the code is written and why
  JOURNAL.md         dated entries: what was built, measured, surprised us
  CONTRACT.md        (B only) the 137 channels, authoritative
  WHY-A-SIDECAR.md   (B only) the 28x GIL write-up
  media/             demo.gif, screenshots, the diagram
examples/            (A) runnable scripts, one concept each
tools/               paste-into-TD scripts (B), fixture recorder (A)
experiments/         things that were measured and lost, kept honestly
```

Two conventions worth keeping from the current repo, because they are unusual
enough to be a selling point:

- **`# MEASURED:` / `# UNMEASURED:` / `# TRAP:` / `# INVARIANT:` tags** in code,
  greppable. Document them in CONTRIBUTING so contributors use them.
- **Proven vs Design separation** in the docs. Nothing moves from Design to Proven
  without a figure.

## 8. Issue and PR templates

Bug report requires the output of `python -m <pkg>.doctor` (build it — it is the
highest-leverage 60 lines in either repo) and, for B, the TD version and a
screenshot of the OSC In CHOP's channel list. Add a "did you check the liveness
channels?" checkbox; frozen channels will otherwise be your most common issue.

Three labels that will do real work: `needs-measurement`, `td-version`,
`macos-version`.

## 9. Release

- A: PyPI on tag, pure-Python wheel, `python -m build` in CI with a trusted publisher.
- B: PyPI *and* a GitHub Release carrying `visionhands-<version>.tox` plus a
  `.toe` demo project. The `.tox` is what the TouchDesigner audience actually
  wants; a pip command is not a distribution channel for them. Name the asset with
  the version and the TD build it was saved from — a `.tox` saved in a newer TD
  will not open in an older one, and that will be an issue on day one.
- Ship the Palette layout (`~/Documents/Derivative/Palette/visionhands/`) in the
  release notes as the one-line install.
