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

`.mp4`, not `.gif` — five to ten times smaller for the same thing, in real colour.
`docs/media/README.md` has the ffmpeg recipe and the `<video>` tag that renders it.
Under about 3 MB.

**The fixture privacy rule applies here too.** Body, clothing and room identify
somebody perfectly well without a face in shot, and a video entering git history is
expensive to remove later. Hands only for anything committed, and look at the frames
before you commit them.

## First run, in every example

The component needs its Python installed once — press **Install** on the Vision page
and wait for `Installstate` to read `Installed`. It downloads nothing if you already
have a working interpreter. See the root [README](../README.md#install).
