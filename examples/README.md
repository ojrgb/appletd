# Examples

One folder per example. Each is self-contained and each says what it is for.

    examples/<name>/
        README.md      what it shows, what to press, what to look at
        <name>.toe     the project, openable on its own
        demo.mp4       a short loop of it working

`appletd.tox` in the repository root is the component itself — drop it into your own
project. These are here to show it doing something.

## What every example README should say

- **what it shows**, in one line
- **what to press**, because `Install` and then `Active` is not obvious from looking
- **which streams it needs**, since depth costs 23 ms and `accurate` segmentation 31,
  and an example that quietly turns both on is an example that drops frames
- **anything it assumes**, like a `render1` at a particular resolution

## The clip

`.mp4` plus a `.jpg` poster frame, and the README links the poster to the video.
A repo-hosted `.mp4` cannot autoplay inline — GitHub serves it as
`application/octet-stream` — so a `<video>` tag pointed at it renders nothing.
`docs/media/README.md` has the measurement, the three options and the ffmpeg recipes.
Budget about 5 MB for a GIF that does not band — see the measured table there;
cutting the palette to save bytes was tried and rejected on sight.

**The fixture privacy rule applies here too.** Body, clothing and room identify
somebody perfectly well without a face in shot, and a video entering git history is
expensive to remove later. Hands only for anything committed, and look at the frames
before you commit them.

## First run, in every example

The component needs its Python installed once — press **Install** on the General page
and wait for `Installstate` to read `Installed`. It downloads nothing if you already
have a working interpreter. See the root [README](../README.md#install).


## Minimal camera and hands setup

On an Apple Silicon Mac, drop `appletd.tox` into a project, press **Install**, and
wait for **Installed – ready**. Select **Camera** input, pick your device, turn
**Hands** and **Output Video** on, then enable **Active**.

Connect **outvideo → Null TOP** for the image and **out1 → Null CHOP** for landmarks.
Use output names rather than connector numbers: hiding unused outputs renumbers them.
For the index fingertip, inspect `h0_index_tip_x` and `h0_index_tip_y`; check `h0_found`
before using those values. Missing hands keep their channels and publish zeros.

If the picture or data is absent, press **Advanced → Setup Diagnostics** and inspect
the Textport report. It does not turn the camera on.
