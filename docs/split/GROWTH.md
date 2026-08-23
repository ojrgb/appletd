# Getting these two repos used

Blunt version first: **you have one genuinely novel thing and one genuinely
useful thing, and they spread through completely different channels.**

- Novel: *"A Python thread inside TouchDesigner runs Vision 28× slower, and here
  is the measurement."* That is original research. Nobody else has published it.
  It travels through developers, Hacker News, and search.
- Useful: *"Drag in a .tox, wave your hand, get 21 named joints per hand at
  30 fps."* That travels through TouchDesigner artists, video clips, and Discord.

Do not run one launch for both. Two audiences, two artefacts, two cadences.

## 1. The thing that actually spreads is not the repo

For repo B it is a **12-second video**: hand on the left, instanced geometry
responding on the right, CHOP viewer visible so people see it is real data. No
music, no intro, no talking. Loop it. That clip is what gets reposted; the repo is
where the 3% who clicked go.

For repo A it is a **paragraph with a table in it** — the 28× table, or the
"640×480 is slower than 720p" finding. Counter-intuitive, specific, falsifiable.

Budget more time on those two assets than on the READMEs. GIF above the fold,
under 5 MB, autoplaying, showing input and output in the same frame.

## 2. Time-to-first-wow under 60 seconds, measured on someone else's Mac

Test it on a machine that has never seen the project, with a stopwatch, watching
someone else type. Every second before the hand moves on screen is where you lose
people. Concretely:

- one `pip install`, one `python -m`, one drag-and-drop `.tox`;
- a `.toe` demo project in the release so there is nothing to wire;
- `python -m appletd.doctor` so the five common failures (camera permission,
  wrong port, wrong venv, frozen channels, camera taken by OBS) diagnose
  themselves;
- an FAQ of exactly those five, in the README, in the order people hit them.

A tool that fails silently for a stranger on day one does not get a second try,
and TouchDesigner's failure mode — channels frozen at their last value — looks
exactly like "this doesn't work."

## 3. Where to put them, specifically

**TouchDesigner audience (repo B), in this order:**
1. Derivative forum — a Showcase thread *and* a Components/Sharing post. Include
   the video, the 28× table, and the .tox link. Reply to everything for two weeks.
2. Email Derivative directly. They regularly feature community components in their
   blog and newsletter, and a measured macOS-native tracker with no dependencies is
   exactly the kind of thing they promote. This is the highest-leverage single email
   you will send.
3. The official TouchDesigner Discord/Slack, r/TouchDesigner, and the
   Interactive & Immersive HQ community — they cover tooling actively and a good
   tool pitch to them reaches a lot of professional users.
4. Instagram Reels and TikTok with `#touchdesigner`. That tag is enormous and the
   audience is exactly your users. One clip per interesting use, not one launch post.
5. Pitch a demo to one or two TD creators who make tutorials. One creator video
   featuring it is worth more than a thousand stars.
6. PR it into `awesome-touchdesigner`.

**Developer audience (repo A):**
1. Write the 28× piece as a blog post with the full method, then post *the post* to
   Hacker News — not the repo. "Show HN: repo" underperforms a titled writeup like
   *"Why a Python thread inside TouchDesigner is 28× slower at the same work."*
2. Second post, aimed at search rather than a spike: *"Apple's Vision framework
   from Python: hand landmarks in 60 lines, no MediaPipe."* Long-tail search
   traffic will out-earn every launch spike within three months.
3. r/Python, lobste.rs, macOS developer communities, and a link from the Apple
   Developer Forums thread where someone is asking how to do this from Python —
   there is always one.
4. PR into pyobjc-adjacent and macOS-dev awesome lists.

## 4. Measure MediaPipe on your own Mac before you say a word about it

"MediaPipe alternative" is your highest-traffic search phrase and your biggest
risk. If you claim or imply faster without having run both on the same machine
against the same clip, the top comment will be someone who did, and you lose the
credibility that is otherwise this project's whole differentiator.

So: run it. Same fixture, same Mac, publish the table with both, and let it say
whatever it says. Even if MediaPipe wins on latency you still win on
"no model file, no wheel, four pyobjc packages, and it ships in the OS" — and a
comparison you published against yourself is the single most citable artefact you
could produce here.

## 5. Your documentation habits are the differentiator — say so out loud

Most repos in this space are a script and a screenshot. Yours has a journal with
dated entries, a Proven/Design separation, `# MEASURED:` and `# UNMEASURED:` tags,
and a written record of six TouchDesigner gotchas with the debugging cost
attached. That reads as trustworthy in a way features do not, and engineers share
repos that read like a well-kept lab notebook.

Put a one-line pointer to `docs/JOURNAL.md` in the README and describe it as what
it is: every measurement, including the ones that killed a design.

The "things we ruled out first, each with a measurement" list is doing more
persuasive work than any feature list you could write. Keep it.

## 6. Close the credit loop

- Ask for a credit line and give people the exact text.
- Publish a `#` tag and repost what people make with it. In the TD community this
  loop *is* the growth mechanism: artists adopt tools they have seen produce work
  they liked, by people who amplified them.
- Answer every issue within 24 hours for the first fortnight. An unanswered issue
  in week one is read as abandonment and costs more adoption than a missing feature.

## 7. Don't do these

- Don't rename either repo after launch. Every link, every forum post, every video
  description breaks. Decide the names now.
- Don't launch without the video and the `.toe`.
- Don't ship a `.tox` without saying which TD build saved it — a newer build will
  not open in an older TD, and that will be your first issue.
- Don't claim two-hand stability, latency, or jitter numbers you have not measured.
  Your roadmap already lists them as unmeasured; keep it that way in public.
- Don't put a face or a home interior in the demo footage. You already have this
  discipline in the fixture rule — the launch video is where it would slip, and it
  is the one asset thousands of strangers will see.

## 8. What success looks like, honestly

A TouchDesigner component repo rarely passes a few hundred stars, and stars are
not the goal — usage is. Realistic good outcomes in three months:

- a forum thread with 20+ replies and people posting their own work;
- one creator video;
- `.tox` release downloads in the hundreds;
- five issues from people who got it *working* and want more;
- and the 28× post being the thing people link when this question comes up again.

The 28× measurement has a long shelf life: it is the kind of finding that gets
cited for years because nobody wants to re-measure it. That is the most likely
route to whatever "viral" ends up meaning here.
