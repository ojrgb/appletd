# Media

Two clips, referenced from the top of `README.md`.

| file | what it should show |
|---|---|
| `demo-hands.mp4` | a hand driving instanced geometry, **with the CHOP viewer in the same frame** so the channels and the motion are visibly one event. A crop showing only the geometry loses the whole argument. ~12 s loop. |
| `demo-depth.mp4` | the person mask and the depth map side by side, somebody walking toward the camera so the depth actually changes. |

## How video actually renders on GitHub — MEASURED, because the obvious answer is wrong

**A `.mp4` in this repository cannot autoplay inline.** Every URL form serves the
wrong content type, checked on 2026-08-23:

    github.com/ojrgb/appletd/raw/main/docs/media/demo-hands.mp4
    raw.githubusercontent.com/ojrgb/appletd/main/docs/media/demo-hands.mp4
    github.com/ojrgb/appletd/blob/main/docs/media/demo-hands.mp4?raw=1

        all three: 200, content-type: application/octet-stream

No browser will put `application/octet-stream` in a `<video>` element, so the tag
renders as nothing whatever the `src`. GitHub DOES play video inline - but only for
files uploaded by dragging them into an issue, a PR or a release, which returns a
`github.com/user-attachments/assets/<uuid>` URL served as `video/mp4`.

So there are three options and each costs something:

| | inline autoplay | in the repo | notes |
|---|---|---|---|
| **poster `.jpg` linked to the blob** | no, one click | **yes** | what this repo does. Self-contained, never breaks |
| `user-attachments` URL from an issue upload | **yes** | no | the asset lives in GitHub's store, not in a clone |
| `.gif` | yes | yes | 5-10x the bytes, 256 dithered colours |

**Both is reasonable**: keep the `.mp4` in the repo as the artefact, and additionally
paste a `user-attachments` URL into the README for the inline player. That is the only
way to get autoplay without a GIF, and the repo still has the file when GitHub does not.

### The poster

```sh
ffmpeg -ss 2.2 -i demo-hands.mp4 -frames:v 1 \
  -vf "scale=900:-1:flags=lanczos" -q:v 3 demo-hands.jpg
```

JPEG, not PNG: the same frame was 478 KB as a PNG and 56 KB as a JPEG at `-q:v 3`,
and nobody can tell. Pick a moment where the hand is actually in shot - a poster of
an empty frame advertises an empty demo.

## Encoding

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
