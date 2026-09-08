# Why the filter is one Filter CHOP

Moved out of `tools/td_add_filter.py`, which had grown a 101-line docstring that was
mostly the history of a wrong turn. Kept because the reasoning is real; moved because
a user opening the builder does not need it.

## It is TouchDesigner's own one-euro filter

An earlier version built the filter by hand from a Feedback CHOP and twelve Math
CHOPs, on the conclusion that the Filter CHOP could not work here. That was wrong.

| | cook time | operators |
|---|---|---|
| hand-built chain | 0.6949 ms | 21 |
| Filter CHOP | **0.0153 ms** | **1** |

A 45x reduction, about 38% of the whole COMP's cook time. Output is functionally
identical — within 0.17% under motion — and on a still hand the native filter
converges exactly where the hand-built chain left a float32 residual of 9e-8.

**Why the wrong conclusion looked right.** The Filter CHOP was tested on `tmp_slope`,
a Math CHOP fed by a Feedback — non-time-sliced, one sample per frame — where all nine
filter types returned the same value whatever the width. That reading was correct; the
generalisation was not. The Filter CHOP is inert on a non-time-sliced one-sample CHOP
and works normally on a time-sliced one. `oef_in` derives from the OSC In CHOP, which
is time-sliced. DESIGN.md 2.11.

**What the native filter does not expose:** `dcutoff`, the smoothing on the speed
estimate. It takes `cutoff` and `speedcoeff` only. 1.0 Hz is the published default and
there was never a reason to move it, so `Dcutoff` was removed rather than kept as a
slider that does nothing.

## One scoped operator, not four

The group used to be:

    in1 -> Select(smoothed) -> Filter -> Merge <- Select(everything else) -> out1

Measured before the change, per cook, with data flowing:

    face   Select 0.1725 + Filter 0.0346 + Select 0.0105 + Merge 0.0299 = 0.2475 ms
    hands  0.1008 ms over 5 operators
    pose   0.0897 ms over 5 operators

The Select was the expensive one, for a poor reason: its `channames` carried 362
literal names matched against every channel every frame.

The silent failure went with it. Two Selects merged back together had to partition the
stream exactly, and four `sc_*` channels once fell into neither list and left the
output with no error anywhere (DESIGN.md 2.11). A scoped operator cannot drop a
channel it was not asked about.

## Why one-euro at all

Landmark jitter and hand motion occupy the same frequency band, so a fixed cutoff must
choose: smooth enough to still a resting hand and a fast hand lags; responsive enough
for a fast hand and a resting one shimmers. One-euro makes the cutoff a function of
estimated speed.

Measured on the hand-built version: at rest the cutoff sat at 1.5 Hz, and a hand
crossing the frame in 0.8 s lifted it to 4.35 Hz — 2.3x more responsive on a real
gesture than at rest.

## Beta, and why the published default is wrong here

Every one-euro reference sets `beta` near 0.007, and every one of them is filtering
PIXELS, where speeds run to hundreds of units per second. These channels are
normalised 0..1, so the same motion measures about a thousand times smaller and
`1.5 + 0.007 * 1.4` is no adaptation at all. Copying the published constant gives a
filter that behaves like a fixed one, and invites the conclusion that one-euro does
not help.

Our default is scaled for normalised units and is a GUESS. Measuring per-joint jitter
is what should set it (DESIGN.md 11).
