# math-youtuber-skill

compose scripted math explainer videos entirely from the command line on macOS.
no GUI editors, no drag-and-drop timelines — just python, ffmpeg, and a microphone.

## quick start

```bash
# scaffold a new project
./new-project.sh ../03-11-2026 "my video title"

# cd in, activate venv, and start working
cd ../03-11-2026
source .venv/bin/activate
```

then follow the pipeline below.

## toolchain

| tool | purpose | install |
|------|---------|---------|
| manim CE | math/diagram animations | `pip install manim` + `brew install ffmpeg cairo pango` + LaTeX |
| f5-tts-mlx | voice cloning/synthesis on apple silicon | `pip install f5-tts-mlx` |
| ffmpeg | audio/video compositing, format conversion | `brew install ffmpeg` |
| sox | audio recording from terminal | `brew install sox` |

use a python venv to keep dependencies isolated:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install manim f5-tts-mlx soundfile
```

## project structure

```
project/
├── script.md              # single source of truth
├── generate_narration.py  # f5-tts batch generation with explicit durations
├── timed_scenes.py        # manim scenes, one class per segment
├── voiceover.sh           # record yourself, composite, check durations
├── clips/                 # audio
│   ├── my_voice_ref.wav       # raw mic recording
│   ├── my_voice_ref_24k.wav   # resampled for f5-tts
│   ├── 01_intro.wav           # generated narration segments
│   ├── vo_01_intro.wav        # voiceover recordings (optional)
│   └── ...
├── output/                # final product
│   ├── segments/              # individual composited segments
│   └── final.mp4              # the final video
├── media/                 # manim output (auto-generated)
└── .venv/                 # python virtual environment
```

## end-to-end pipeline

### step 0: plan the video

**target length:** 3-5 minutes. longer than 5 and you lose people; shorter than 3 and you can't develop ideas.

**segment count:** 10-25 segments of 4-20 seconds each.

**duration estimation:** word count / 2.5 ≈ speech duration in seconds (for TTS). if the user is recording voiceover, this will vary — use actual recorded durations as feedback and adjust timing accordingly.

| words | duration | good for |
|-------|----------|----------|
| 10-15 | 4-6s | titles, transitions, one-liners |
| 20-30 | 8-12s | single concept + animation |
| 30-40 | 12-16s | multi-step explanation |
| 40-50 | 16-20s | algebraic walkthrough, complex diagrams |

### step 1: write the script

write everything in `script.md`. interleave three kinds of content:

- **spoken lines** — plain text, lowercase, pithy
- **`> [MANIM:]` cues** — what animation plays during this section
- **`> [DIRECTOR:]` notes** — performance direction

```markdown
## the three-move dance

> **[DIRECTOR: straighten up. hold up three fingers.]**

sigma protocols are elegant. three messages. that is it.

> **[CUT TO MANIM: ThreeMoveDance scene]** prover/verifier diagram,
> arrows appear one at a time.
```

#### voice and tone

the goal is **clarity with flair** — not hype. the best math communication lets the ideas do the work.

- **explain, don't sell.** if a result is beautiful, say why — don't just say "and here's where it gets wild."
- **be precise.** use correct terminology and define it. vagueness isn't accessible, it's confusing.
- **earn the wonder.** set up a concept carefully and the moment it clicks is naturally exciting. no hype needed.
- **conversational, not performative.** talk to the viewer like you're explaining to a smart friend, not presenting to an audience.
- **cut the filler.** "let's", "so basically", "here's the thing" — these pad runtime without adding understanding.
- **lowercase is fine, but don't force casualness.** "this gives us injectivity" is better than "boom — injectivity."

bad: "three messages. that's it. elegant."
good: "the protocol has three messages: a commitment, a challenge, and a response."

bad: "congratulations, you just invented schnorr signatures."
good: "this is exactly a schnorr signature — a non-interactive proof of knowledge bound to a message."

#### conventions

**heading = segment boundary.** each `##` heading maps to one narration segment, one manim scene class, and one composited video file.

**spoken lines are lowercase, precise.** write like you're explaining to a smart friend, then trim. these go verbatim into `generate_narration.py`.

**naming consistency:**

| file | pattern | example |
|------|---------|---------|
| `script.md` | `## heading` | `## public key` |
| `generate_narration.py` | `"03b_public_key"` | filename stem |
| `timed_scenes.py` | `class S03b_PublicKey(Scene)` + `DUR["public_key"]` | class + dict key |
| `voiceover.sh` | `"03b:S03b_PublicKey:03b_public_key:description"` | id:class:audio:desc |

#### keeping files in sync

the script is the **single source of truth**:
```
script.md
    ├── generate_narration.py   — spoken text + durations
    ├── timed_scenes.py         — animation content + durations
    └── voiceover.sh            — segment mapping + script text
```

**when you change a spoken line:** update script.md, update SEGMENTS in generate_narration.py, `rm clips/XX_name.wav`, re-run `python generate_narration.py`, update DUR in timed_scenes.py if duration changed.

**when you add a segment:** add to all four files. use "b" suffixes for insertions (e.g. `03b_public_key`).

### step 2: record reference audio

record 8-12 seconds of yourself reading a line from the script:

```bash
# use the helper script
../math-youtuber-skill/record-reference.sh

# or manually
rec clips/my_voice_ref.wav
# read your line, then Ctrl+C after ~1s silence
ffmpeg -y -i clips/my_voice_ref.wav -ac 1 -ar 24000 -sample_fmt s16 clips/my_voice_ref_24k.wav
```

**rules:**
- 8-12 seconds (model clips beyond ~12s internally)
- ~1 second silence at the end
- speak in the tone/pace you want — the model clones style, not just timbre
- clean audio, no background noise, no clipping
- transcription must be exact — model uses text/audio ratio for timing

### step 3: generate narration

edit `generate_narration.py` — update `REF_AUDIO`, `REF_TEXT`, `REF_DUR`, and `SEGMENTS`.

```bash
python generate_narration.py
```

**critical: use explicit `duration`, never `estimate_duration=True`.** the estimator frequently overshoots — we've seen 103 seconds of audio for an 8-second line.

to regenerate one segment: `rm clips/09_name.wav && python generate_narration.py`

**quality/speed tradeoffs (M4 48GB):**

| steps | quality | time per ~10s |
|-------|---------|---------------|
| 8 | draft | ~10s |
| 32 | high | ~1 min |
| 64 | max | ~2 min (diminishing returns) |

### step 4: build timed animations

edit `timed_scenes.py` — one scene class per segment, durations from DUR dict.

**key patterns:**
- `DUR` dict at top — durations defined once, referenced by descriptive key
- elapsed time tracking in comments — `self.wait(max(d - elapsed, 0.1))` at end
- scene naming: `S01_Intro`, `S03b_PublicKey` — number prefix for order, name for content
- **sync visuals to narration** — use the class docstring to write out the spoken text with `|` delimiters between phrases. estimate when each phrase lands (~2.5 words/sec for TTS) and place `self.wait()` calls so animations fire at the right moment. the goal is that each visual appears as the viewer hears it described, not before or after. note: if the user is recording their own voiceover, speaking pace will vary — treat 2.5 w/s as a starting estimate and adjust based on actual recorded durations

### step 5: render animations

```bash
source .venv/bin/activate

# single scene (iteration)
manim render -ql timed_scenes.py S05_Elegant

# all scenes — must specify each by name (omitting the scene name
# causes an interactive prompt that breaks automation)
for scene in S01_Intro S02_Concept S03_Detail; do
    manim render -ql timed_scenes.py "$scene"
done

# quality flags:
#   -ql  480p/15fps   fast iteration
#   -qm  720p/30fps   review drafts
#   -qh  1080p/60fps  final render
#   -qk  4K/60fps     4K final
```

### step 6: record voiceover (optional)

```bash
# record all segments (video autoplays while you speak)
./voiceover.sh record

# record just one segment
./voiceover.sh record 05

# check duration mismatches
./voiceover.sh durations

# preview a segment's animation
./voiceover.sh play 05
```

voiceover files (`vo_*.wav`) take priority over TTS during compositing. only re-record segments you want to replace.

### step 7: composite and concatenate

```bash
# composite with whatever audio exists (prefers voiceover > TTS)
./voiceover.sh composite

# or for TTS-only compositing without loudnorm:
# bash composite.sh
```

the composite step:
1. pairs each animation + audio into a segment video
2. handles audio longer than video (freezes last frame via tpad)
3. concatenates all segments
4. applies 2-pass YouTube loudnorm (-14 LUFS, -1 dBTP)

## manim reference

### useful primitives

- `MathTex(r"...")` — LaTeX math. `{{ }}` double braces for sub-part morphing
- `Text("...", font="Courier New")` — plain text
- `Arrow(start, end)` — animate with `GrowArrow()`
- `Graph(vertices, edges, layout=...)` — network graphs
- `SurroundingRectangle(obj)` — highlight box
- `VGroup(a, b, c)` — group for collective animation

### animation patterns

```python
self.play(FadeIn(obj), run_time=0.5)
self.play(obj.animate.move_to(RIGHT * 3), run_time=0.8)
self.play(TransformMatchingTex(eq1, eq2))
self.play(Flash(obj, color=RED, flash_radius=0.5))
self.play(FadeIn(a), GrowArrow(b), run_time=0.6)           # simultaneous
self.play(obj.animate.set_fill(GREEN, opacity=0.8))         # color change
```

### rendering quality flags

| flag | resolution | fps | use case |
|------|-----------|-----|----------|
| `-ql` | 854x480 | 15 | fast iteration |
| `-qm` | 1280x720 | 30 | review drafts |
| `-qh` | 1920x1080 | 60 | final render |
| `-qk` | 3840x2160 | 60 | 4K final |

## f5-tts-mlx parameter reference

| parameter | default | recommended | notes |
|-----------|---------|-------------|-------|
| `steps` | 8 | 32 | 8=draft, 32=production, 64=diminishing returns |
| `cfg_strength` | 2.0 | 3.5 | voice adherence. higher = more like reference |
| `speed` | 1.0 | 1.0 | speech rate |
| `method` | "rk4" | "rk4" | solver. rk4 is best quality |
| `seed` | None | set for reproducibility | |

## lessons learned

### f5-tts-mlx
- **never use `estimate_duration=True`** — causes massive overshooting and hallucinated speech
- **transcription accuracy is critical** — wrong text = garbled output
- **spell out variable names phonetically** — TTS can't pronounce single letters well. "a" sounds like filler, "b" is ambiguous. use greek letters in spoken text: "alpha", "beta", "kappa". the manim visuals can show the corresponding symbols (α, β, κ)
- **cfg_strength=3.5** — sweet spot for cloning. below 2.0 sounds generic, above 4.0 sounds overfit
- **reference audio must be 24kHz mono** — model hard-errors on anything else
- **8-12 seconds of clean reference** — more isn't better (model clips at 12s)

### manim
- **always specify scene names when rendering** — `manim render -ql timed_scenes.py` without a scene name triggers an interactive prompt that breaks automation. render each scene explicitly in a loop
- **one scene class per segment** — much easier to time than monolithic scenes
- **networkx layouts are 2d, manim wants 3d** — convert with `{k: [v[0], v[1], 0] for k, v in layout.items()}`
- **`Text` submobjects are SVG paths, not characters** — use `save_state()` + `Restore()` for scatter-assemble
- **track elapsed time in comments** — `self.wait(max(target - elapsed, 0.1))` ensures exact duration

### ffmpeg / compositing
- **never use `seq`/`printf` with 08, 09** — bash interprets as invalid octal. hardcode the list
- **re-encode at both stages** — per-segment and final concat. `-c copy` causes playback freezing
- **tpad for audio > video** — `tpad=stop_mode=clone:stop_duration=N` freezes last frame. do NOT use `-stream_loop`
- **2-pass loudnorm for YouTube** — measure first, then encode with measured values + `linear=true`

### workflow
- **iterate at 8 steps / `-ql`** — 5-10x faster than production. check timing before committing
- **script is the single source of truth** — all other files derive from it
- **voice reference recording matters** — the AI clones cadence and energy, not just timbre
- **line up visuals with narration** — animations should appear in sync with the words describing them. if you say "square both sides" while the equation is already on screen, it feels disconnected. time `self.play()` calls to land with the spoken cues
- **abrupt audio cuts between segments are jarring** — each TTS segment starts cold, so back-to-back segments can sound choppy. use brief pauses (0.3-0.5s silence) between segments in concatenation, and keep vocal energy consistent across segments by using the same reference audio and cfg_strength
