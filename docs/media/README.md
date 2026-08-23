# Media

Two clips, referenced from the top of `README.md`.

| file | what it should show |
|---|---|
| `demo-hands.mp4` | a hand driving instanced geometry, **with the CHOP viewer in the same frame** so the channels and the motion are visibly one event. A crop showing only the geometry loses the whole argument. ~12 s loop. |
| `demo-depth.mp4` | the person mask and the depth map side by side, somebody walking toward the camera so the depth actually changes. |

## Why .mp4 and not .gif

H.264 is roughly five to ten times smaller than the same clip as a GIF, with real
colour instead of 256 dithered ones. For a page that is mostly read on github.com
there is no reason to pay for a GIF.

**It has to be a `<video>` tag with an absolute raw URL.** GitHub's Markdown does not
turn an `.mp4` into a player from a relative `![](…)`:

```html
<video src="https://github.com/ojrgb/appletd/raw/main/docs/media/demo-hands.mp4"
       autoplay loop muted playsinline width="900"></video>
```

`muted` is not optional — a browser will not autoplay anything with sound. `playsinline`
stops iOS Safari opening it fullscreen.

**If that does not render**, the always-works fallback is a poster frame that links to
the video. Uglier, degrades everywhere, never breaks:

```markdown
[![hands](docs/media/demo-hands.png)](https://github.com/ojrgb/appletd/raw/main/docs/media/demo-hands.mp4)
```

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
