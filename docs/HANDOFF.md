# Handoff

**843 tests**, `ruff check appletd/ tools/` and `mypy appletd` clean. Branch
**feat/ocr-text-dat**, off `main` — nothing pushed; a push has to be asked for.

`tools/vision_landmarks.py` and `tools/vision_landmarks_live.py` are the user's own
untracked files. Leave them alone; `ruff` at the repo root fails on one of them, so
lint `appletd/ tools/` instead.

**Just built: text recognition as a Table DAT** (docs/BUILD_PLAN.md step 32). The
chain has been run against the open project, so the live `.toe` has the Text page and
`outtext` on connector 5 — but the file has NOT been saved or re-exported; that is
Omer's step. The installed package under `~/Library/Application Support/appletd` is
two modules short (`ocr`, `text_types`) until Install is pressed, and the panel says so.
Verified in Omer's project: TOP Input with a Text TOP reads into `outtext`. TOP Input
now ships float32 untouched and Vision is told the rows are upside down (BENCHMARKS,
"TOP Input at 1080p") - the writer must never convert pixels on TD's main thread again. That run
found and fixed a TOP Input defect - nothing pulled `frames_write`, so it never cooked
headless; `frames_pull` (an Execute DAT) now asks for it each frame while capturing.
Unverified: the live camera path, and real signage rather than rendered text.

## Read these, in this order

1. **`docs/internals/v2-progress.md`** — what is unfinished. `v2-next.md` is the
   2026-09-08 batch, all done.
2. **`docs/internals/v2-to-test.md`** — what needs a camera, a person, or an opinion,
   and what he has already verified.
3. **`docs/JOURNAL.md`**, last few entries — what went wrong recently and why the
   code is the shape it is.
4. **`docs/ATTRIBUTES.md`** — the channel and parameter contract. User-facing; keep it
   accurate.

`docs/internals/handoff-2026-08-24.md` is the previous snapshot, kept for its
reasoning. It predates the page reorganisation, so treat any parameter or page name in
it as historical.

## The shape of the thing

A TouchDesigner COMP that talks to a **sidecar process** over OSC, with images passing
through a file-backed `mmap` and text arriving as one typed OSC message a frame into an
OSC In DAT's callback. The sidecar runs Apple's Vision and Core ML; TouchDesigner's
own Python cannot load pyobjc, which is why the process is separate at all.

    appletd/     the runtime package. Pure Python + pyobjc, no TouchDesigner imports.
    tools/       the builders. They CONSTRUCT the COMP; run tools/td_rebuild.py.
    docs/        contract and rationale. internals/ is for agents, the rest is public.

## Things that will bite you

**Run `tools/td_rebuild.py`, not individual builders.** It runs all 18 layers in
order. Running `td_build_vision.py` alone puts new parameters back on the General page
and destroys operators later layers own.

**The sidecar runs the INSTALLED package, not the checkout.** The chain is
`checkout → td_embed_package.py → DATs in the .toe → Install → disk`. Change the
package and you must re-embed and press Install, or the sidecar keeps running the old
code — silently, because the install is internally consistent.

**A new stream must be added to every list that names streams.** This has been missed
three times: `RUNTIME_MODULES`, `STREAM_CHANNELS`, and the engine's no-streams guard.
`test_engine_streams.py` now covers the last of those. Text added two more places a
request lives: `PORT_OFFSETS` (a port with no channels) and `OUTPUT_CHAIN` in the
control module, which has to know an output's FAMILY to recreate it.

**Never commit on `main`, and ask before every push.**

Ref: `docs/STANDARDS.md` for how to write here, `docs/ARCHITECTURE.md` for why the
sidecar exists.
