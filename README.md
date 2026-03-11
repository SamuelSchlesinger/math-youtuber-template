# chalk

compose scripted math explainer videos entirely from the command line on macOS.
no GUI editors, no drag-and-drop timelines — just python, ffmpeg, and a microphone.

## quick start

```bash
# clone chalk
git clone <your-chalk-repo-url> my-video
cd my-video

# initialize the project
./init.sh "sqrt 2 is irrational"

# activate venv and start working
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

`init.sh` creates a `.venv` and installs the python dependencies for you.

## project structure

```
my-video/
├── init.sh                    # run once to set up the project
├── render.sh                  # render all scenes (handles quality + shorts)
├── record-reference.sh        # record your voice reference
├── README.md                  # you are here
├── script.md                  # single source of truth
├── generate_narration.py      # f5-tts batch generation with explicit durations
├── timed_scenes.py            # manim scenes, one class per segment (landscape)
├── timed_scenes_shorts.py     # same scenes adapted for 9:16 vertical
├── voiceover.sh               # record yourself, composite, check durations
├── clips/                     # audio
│   ├── my_voice_ref.wav           # raw mic recording
│   ├── my_voice_ref_24k.wav       # resampled for f5-tts
│   ├── 01_intro.wav               # generated narration segments
│   ├── vo_01_intro.wav            # voiceover recordings (optional)
│   └── ...
├── output/                    # final product
│   ├── segments/                  # individual composited segments
│   ├── final.mp4                  # the final video
│   └── shorts/                    # shorts version (1080x1920)
│       ├── segments/
│       └── final_shorts.mp4
├── media/                     # manim output (auto-generated)
└── .venv/                     # python virtual environment
```

## end-to-end pipeline

### step 0: plan the video

**target length:** 3-5 minutes. longer than 5 and you lose people; shorter than 3 and you can't develop ideas.

**segment count:** 10-25 segments of 4-20 seconds each.

**duration estimation:** word count / 2.5 ≈ speech duration in seconds (for TTS). if you are recording voiceover, this will vary — use actual recorded durations as feedback and adjust timing accordingly.

| words | duration | good for |
|-------|----------|----------|
| 10-15 | 4-6s | titles, transitions, one-liners |
| 20-30 | 8-12s | single concept + animation |
| 30-40 | 12-16s | multi-step explanation |
| 40-50 | 16-20s | algebraic walkthrough, complex diagrams |

### step 1: write the script

write everything in `script.md`. interleave three kinds of content:

- **spoken lines** — plain text, lowercase, precise
- **`> [MANIM:]` cues** — what animation plays during this section
- **`> [DIRECTOR:]` notes** — performance direction

here's an example from a proof that sqrt(2) is irrational:

```markdown
## square both sides

> **[DIRECTOR: walk through the algebra one step at a time.]**

square both sides. two equals alpha squared over beta squared. multiply through: alpha squared equals two beta squared.

> **[CUT TO MANIM: S03_Square scene]** show the algebra step by step:
> sqrt(2) = α/β -> 2 = α²/β² -> α² = 2β²
```

#### voice and tone

the goal is **clarity with flair** — not hype. the best math communication lets the ideas do the work.

- **explain, don't sell.** if a result is beautiful, say why — don't just say "and here's where it gets wild."
- **be precise.** use correct terminology and define it. vagueness isn't accessible, it's confusing.
- **earn the wonder.** set up a concept carefully and the moment it clicks is naturally exciting. no hype needed.
- **conversational, not performative.** talk to the viewer like you're explaining to a smart friend, not presenting to an audience.
- **cut the filler.** "let's", "so basically", "here's the thing" — these pad runtime without adding understanding.
- **lowercase is fine, but don't force casualness.** "this gives us injectivity" is better than "boom — injectivity."

bad: "both alpha and beta are even. boom. contradiction."
good: "both alpha and beta are even. they share a factor of two. but we assumed they had no common factors. contradiction."

bad: "congratulations, you just proved irrationality."
good: "the square root of two is irrational."

#### LLM-isms to avoid

since these scripts are written with AI assistance, they tend to pick up identifiable AI writing tics. watch for these and rewrite when you spot them. (see [wikipedia's field guide](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing) for the full taxonomy.)

**significance inflation.** LLMs love to tell the audience how important something is rather than showing why. words like "remarkable", "profound", "elegant", "pivotal", "crucial", "key" are flags. if something is remarkable, the explanation should make the viewer feel that — you shouldn't need the adjective.

bad: "this is the remarkable thing: every algorithm can be expressed as a turing machine."
good: "every algorithm ever written can be expressed as a turing machine."

bad: "turing's most profound insight wasn't just the machine."
good: "turing went further. he described a specific machine."

**"not just X — Y" and other negative parallelisms.** a staple of LLM rhetoric: "not just X, but Y", "it wasn't X — it was Y", "not because X — because Y". occasionally fine, but if your script has three of them, two need to go. rewrite as direct statements.

bad: "not because we lack the hardware — because no machine could solve them in principle."
good: "no machine could solve them, regardless of speed or memory."

**rule of three.** LLMs default to tripling: "every search engine, every compiler, every neural network." once in a script is fine. twice is a pattern. three times and the audience can hear the prompt.

**em dash overuse.** LLMs reach for em dashes where commas, colons, periods, or parentheses would be more natural. one or two per segment is fine. if every sentence has one, restructure.

**recap closings.** LLMs love to end by restating every point made in the piece ("X drew the boundary. Y showed universality. Z proved the limits."). a closing should leave the viewer with one thought, not a bulleted summary masquerading as prose.

bad: "the church-turing thesis draws the boundary. the universal turing machine shows one machine can simulate them all. quantum computers push efficiency. the halting problem proves the boundary is real."
good: end on a single image or idea that the video has earned.

**rhetorical question pairs.** "but is there X? are there Y?" — LLMs use paired rhetorical questions as transitions. one question is fine. a pair, where the second rephrases the first, is filler.

**AI vocabulary.** these words appear at far higher rates in LLM output than in human writing: delve, intricate, tapestry, testament, landscape (figurative), meticulous, underscore, showcase, foster, vibrant, enduring, bolster, garner, pivotal, crucial. not banned, but if you see a cluster of them, rewrite.

**superficial participle tails.** sentences ending with "...representing X", "...highlighting Y", "...underscoring Z". these tack on a shallow interpretation instead of letting the fact speak. cut the participle phrase or make it its own sentence with actual content.

bad: "their states become correlated, representing a departure from classical physics."
good: "their states become correlated in ways that classical probability cannot describe."

#### conventions

**heading = segment boundary.** each `##` heading maps to one narration segment, one manim scene class, and one composited video file.

**spoken lines are lowercase, precise.** write like you're explaining to a smart friend, then trim. these go verbatim into `generate_narration.py`.

**naming consistency:**

| file | pattern | example |
|------|---------|---------|
| `script.md` | `## heading` | `## square both sides` |
| `generate_narration.py` | `"03_square"` | filename stem |
| `timed_scenes.py` | `class S03_Square(Scene)` + `DUR["square"]` | class + dict key |
| `voiceover.sh` | `"03:S03_Square:03_square:description"` | id:class:audio:desc |

#### keeping files in sync

the script is the **single source of truth**:
```
script.md
    ├── generate_narration.py   — spoken text + durations
    ├── timed_scenes.py         — animation content + durations
    └── voiceover.sh            — segment mapping + script text
```

**when you change a spoken line:** update script.md, update SEGMENTS in generate_narration.py, `rm clips/XX_name.wav`, re-run `python generate_narration.py`, update DUR in timed_scenes.py if duration changed.

**when you add a segment:** add to all four files. use "b" suffixes for insertions (e.g. `03b_square`).

### step 2: record reference audio

record 8-12 seconds of yourself reading a line from the script:

```bash
# use the helper script
./record-reference.sh clips

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
- scene naming: `S01_Intro`, `S03_Square` — number prefix for order, name for content
- **sync visuals to narration** — use the class docstring to write out the spoken text with `|` delimiters between phrases. estimate when each phrase lands (~2.5 words/sec for TTS) and place `self.wait()` calls so animations fire at the right moment. the goal is that each visual appears as the viewer hears it described, not before or after. if you are recording your own voiceover, speaking pace will vary — treat 2.5 w/s as a starting estimate and adjust based on actual recorded durations

### step 5: render animations

```bash
# all scenes, fast iteration
./render.sh

# all scenes, final quality
./render.sh -qh

# one scene
./render.sh -ql S03_Square

# quality flags:
#   -ql  480p/15fps   fast iteration
#   -qm  720p/30fps   review drafts
#   -qh  1080p/60fps  final render
#   -qk  4K/60fps     4K final
```

`render.sh` auto-discovers scene classes from `timed_scenes.py`, activates the venv, and loops through them. no need to maintain a separate scene list.

### step 5b: render shorts animations (optional)

`timed_scenes_shorts.py` is adapted for the 9:16 vertical frame. `init.sh` scaffolds it for you — adapt each scene from `timed_scenes.py` with layout changes for the narrow vertical frame.

```bash
# all shorts scenes
./render.sh --shorts -qh

# one shorts scene
./render.sh --shorts -ql S03_Square
```

`render.sh --shorts` handles the `-r 1080,1920 --fps 60` flags automatically.

**key layout differences from landscape:**

| property | landscape (16:9) | shorts (9:16) |
|----------|-----------------|---------------|
| frame width | ~14.2 units | ~4.5 units |
| frame height | 8 units | 8 units |
| font sizes | 48-96 | 72-192 (~2x) |
| horizontal layout | side-by-side ok | stack vertically |
| vertical spacing | 0.6-0.8 buff | 0.8-1.0 buff (large text needs room) |

**common pitfalls:**
- `-r` flag is **height,width** not width,height — `-r 1080,1920` gives 1080w x 1920h
- wide equations (e.g. `\gcd(\alpha, \beta) = 1`) may overflow — split across lines or reduce font size
- elements placed with `LEFT * 2` / `RIGHT * 2` are near the frame edge (frame is only ±2.25 wide)
- test with `-ql` first — vertical rendering is the same speed as landscape

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
# composite landscape with whatever audio exists (prefers voiceover > TTS)
./voiceover.sh composite

# composite shorts (uses same audio, different video from timed_scenes_shorts)
./voiceover.sh composite-shorts
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
- **max output length is ~31 seconds** — the model has an effective context limit of ~43s total (reference + generated audio). if your segment needs more than ~30s of speech, split it into sub-segments of 10-15s each and concatenate with ffmpeg. all sub-segments hitting the exact same output length (e.g. 31.48s) is a sign you've hit this limit
- **keep segments under ~60 words** — segments with 70+ words tend to hit the output length cap. for standalone shorts (30-60s), always split into 2-5 sub-segments
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
- **manim caches aggressively** — if you edit a scene file and re-render, manim may serve the old cached video. delete the output `.mp4` file before re-rendering to force a fresh build. `--flush_cache` alone may not be enough
- **`-r` flag changes the output directory** — `-r 1080,1920 -ql` renders to `1920p15/` not `480p15/`. the custom resolution overrides the quality preset's resolution but keeps its fps. always check the actual output path after rendering with `-r`
- **fade out elements before replacing them** — when transitioning between scene phases, explicitly `FadeOut` text and labels that will be replaced. leaving them on screen (even if partially obscured) causes visual clutter, especially in the narrow 9:16 frame

### ffmpeg / compositing
- **never use `seq`/`printf` with 08, 09** — bash interprets as invalid octal. hardcode the list
- **re-encode at both stages** — per-segment and final concat. `-c copy` causes playback freezing
- **tpad for audio > video** — `tpad=stop_mode=clone:stop_duration=N` freezes last frame. do NOT use `-stream_loop`
- **2-pass loudnorm for YouTube** — measure first, then encode with measured values + `linear=true`

### shorts (9:16 vertical)
- **`-r` is height,width** — `manim render -r 1080,1920` gives 1080w x 1920h. getting this backwards gives landscape at a weird resolution
- **double your font sizes** — phone screens are small. text that's readable at 48pt on a laptop needs ~96pt for shorts
- **stack, don't spread** — the frame is only ~4.5 units wide. anything side-by-side in landscape should be stacked vertically
- **increase vertical spacing** — larger text takes more room. use `buff=0.8-1.0` instead of `0.6-0.8`
- **watch for overlap** — fractions (`\frac{}{}`) are tall. increase `UP/DOWN` shifts between equations
- **same audio, different video** — `composite-shorts` reuses the same TTS/voiceover clips with the shorts-rendered video

### workflow
- **iterate at 8 steps / `-ql`** — 5-10x faster than production. check timing before committing
- **script is the single source of truth** — all other files derive from it
- **voice reference recording matters** — the AI clones cadence and energy, not just timbre
- **line up visuals with narration** — animations should appear in sync with the words describing them. if you say "square both sides" while the equation is already on screen, it feels disconnected. time `self.play()` calls to land with the spoken cues
- **abrupt audio cuts between segments are jarring** — each TTS segment starts cold, so back-to-back segments can sound choppy. use brief pauses (0.3-0.5s silence) between segments in concatenation, and keep vocal energy consistent across segments by using the same reference audio and cfg_strength
