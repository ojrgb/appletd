# V2 — where things stand

**Read this first if you are resuming after a context compaction.** The previous
version of this file was a snapshot of one batch of work and went stale within days,
naming "next steps" that were already finished. This one records only what is
UNFINISHED, so it cannot rot the same way.

Current as of 2026-09-08, 52 commits on `V2`.

## Done, verified, and committed

Everything in `v2-next.md` — the eight items from Omer's testing — plus the two
batches before it. `v2-to-test.md` says what has been confirmed on real hardware and
by whom. The journal is the account of how; don't re-derive it from the code.

## Not built

**YOLOv3 object detection.** Asked for, licence confirmed MIT (`v2-open-questions.md`
Q5.3), not started. The architectural question in Q5.0 — whether each new model is its
own stream with its own toggle, or one "extras" stream — was never answered and decides
the shape of the work.

**Text recognition as a DAT.** `VNRecognizeTextRequest` returns STRINGS, and nothing in
the current transport carries a string: OSC feeds CHOPs and the shared buffers carry
images. It needs a third channel — most likely a small JSON file the sidecar writes and
a DAT reads, the way the image buffers work. **That is a decision, not just work**, and
Q5.1 has the options.

## Open questions nobody has answered

`v2-open-questions.md` — the live ones are Q1.1 (should auto-refresh restart when a
stream goes OFF), Q2.2 (publish a people count for the mask), Q3.2 (what the sidecar
should do when a TOP input stops updating), and Q5.0/Q5.1 above. Each currently has an
assumption recorded in that file; none has been confirmed.

## Loose end worth an hour

**An OSC In CHOP never forgets a channel name.** After the `roll`/`yaw`/`pitch` to
`angle_[xyz]` rename, `face_osc` still held the old three: 393 channels against a 387
contract. Harmless, but every project that saw the old names keeps them until the
operator is recreated, and a renamed channel is therefore not really gone.

## Rules that bite here

  * **Never push without asking.** Never commit on `main`.
  * **Run `tools/td_rebuild.py`, not individual builders** — all 18 layers, in order.
  * **Install is ASYNCHRONOUS.** Pulse it, confirm the files on disk, THEN restart.
    Doing it in one breath starts the sidecar on the old package, three times so far.
  * **The sidecar runs the INSTALLED package**, not the checkout:
    `checkout -> td_embed_package.py -> DATs -> Install -> disk`.
  * `pytest`, `ruff check appletd/ tools/` and `mypy appletd` before every commit.
  * Ask before using the camera. It usually already has an owner.
