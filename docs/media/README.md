# Media

Two clips, referenced from the top of `README.md`.

| file | what it should show |
|---|---|
| `demo-hands.mp4` | a hand driving instanced geometry, **with the CHOP viewer in the same frame** so the channels and the motion are visibly one event. A crop showing only the geometry loses the whole argument. ~12 s loop. |
| `demo-depth.mp4` | the person mask and the depth map side by side, somebody walking toward the camera so the depth actually changes. |

## What renders inline, and what it costs — MEASURED

**A `.mp4` in this repository cannot autoplay inline.** Every URL form serves the
wrong content type, checked 2026-08-23:

    github.com/ojrgb/appletd/raw/main/…      200  application/octet-stream
    raw.githubusercontent.com/…              200  application/octet-stream
    github.com/…/blob/main/…?raw=1           200  application/octet-stream

No browser will put `application/octet-stream` in a `<video>` element, so the tag
renders nothing whatever the `src`. GitHub does play video inline, but only for files
dragged into an issue, a PR or a release, which returns a
`github.com/user-attachments/assets/<uuid>` URL served as `video/mp4` — and that asset
is not in a clone.

**So a GIF is the only repo-tracked format that plays inline.** The cost, from this
same 5.5 s clip of a CHOP viewer beside moving geometry — which is the worst case for
a palette, being full-frame detail that changes every frame:

| | size |
|---|---|
| the `.mp4`, 1280×720, 30 fps, crf 24 | **1.6 MB** |
| GIF 900 wide, 15 fps, 256 colours | 13 MB |
| GIF 720 wide, 12 fps, 256 colours | 7.4 MB |
| GIF 720 wide, 12 fps, 64 colours | 4.6 MB |
| GIF 720 wide, 12 fps, 32 colours | 3.4 MB |
| **GIF 720 wide, 12 fps, 32 colours, trimmed to 3.5 s** | **2.2 MB** |

**Length is the lever, not resolution.** Dropping 900→640 saved 1 MB; trimming 5.5 s
to 3.5 s saved 1.2 MB on top of that and costs almost nothing, because a loop only has
to show the idea once. Colours are the second lever and screen UI is mostly flat, so
32 bands far less than it would on video of a room.

### What this repo does

An inline GIF at 2.2 MB, **linked to the `.mp4`**. Motion on the page, and one click
for the full length in real colour. The `.mp4` stays in the repository as the
artefact — it is a fifth of the size and the thing worth keeping.

## Encoding the GIF, in two passes

One pass to build a palette from what CHANGES between frames, one to apply it:

```sh
ffmpeg -y -t 3.5 -i demo-hands.mp4 \
  -vf "fps=12,scale=720:-1:flags=lanczos,palettegen=max_colors=32:stats_mode=diff" p.png

ffmpeg -y -t 3.5 -i demo-hands.mp4 -i p.png \
  -lavfi "fps=12,scale=720:-1:flags=lanczos [x]; \
          [x][1:v] paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" demo-hands.gif
```

`stats_mode=diff` matters here: the background is static and the hand is not, so a
palette built from what MOVES spends its 32 entries where they show. `dither=bayer`
with a low `bayer_scale` stops flat UI areas crawling between frames — the default
`sierra2_4a` looks better on photographs and worse on screen recordings.

## Encoding the mp4

```sh
ffmpeg -i in.mov \
  -vf "fps=30,scale=900:-1:flags=lanczos" \
  -c:v libx264 -profile:v high -pix_fmt yuv420p \
  -crf 24 -preset slow -movflags +faststart -an \
  demo-hands.mp4
```

- **`-an`** drops the audio track. There is nothing to hear and it is bytes.
- **`-crf`** is the size knob: 20 is nearly lossless, 24 is good, 28 starts to smear
  a moving hand against a busy background. Change this before you change resolution.
- **`+faststart`** puts the index at the front so it begins playing before it has
  finished downloading.
- **`yuv420p`** because anything else will not play in some browsers.

Aim under about 3 MB each. Check with `du -h`, and remember every re-export of a
tracked binary adds a full copy to the repository's history — so get it right before
committing rather than iterating in public.

## The privacy rule applies here too

Body, clothing and room identify somebody perfectly well without a face in shot, and a
video entering git history is expensive to remove later. **Hands only**, and look at
the frames before you commit them. `fixtures/*.mp4` is gitignored for exactly this
reason; `docs/media/*.mp4` is deliberately not, so the judgement is yours each time.
