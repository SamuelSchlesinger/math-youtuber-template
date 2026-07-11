# Chalk

Chalk is an AI-native workspace for scripted math and technical explainer
videos. You shape the explanation in conversation with coding agents; everyone
works in ordinary Markdown and Python; Chalk quietly remembers which script,
scene, recording, transcript, and settings produced each cut.

It uses the tools that already work well:

- Manim for expressive visual code;
- SoX for human voice recording;
- MLX Whisper for word timing;
- ffmpeg for composition and delivery;
- Git for human history;
- SHA-256 for exact artifact lineage and incremental reuse.

Chalk does not introduce a visual DSL, editor database, or required UI. Its job
is to shorten the distance from an idea to a watched A/V cut without losing the
history of how that cut came to be.

## Start a video

Install or clone Chalk once, then create each video as its own repository:

```bash
git clone https://github.com/SamuelSchlesinger/chalk.git
cd chalk
./chalk new ../my-video --title "My video"
cd ../my-video
./setup.sh
```

The first setup resolves transitive Python dependencies into
`requirements.lock`; commit that file. Later setups install the lock. MathTex
also needs a local LaTeX distribution with `dvisvgm`.

`chalk new` copies a pinned project-local tool, initializes a fresh Git history,
and records the Chalk version used. It does not carry Chalk's origin or commit
history into the video project.

Run `./chalk` at any time. It reports what exists, what is stale, open feedback,
and the most useful next action.

Run `./chalk open` for the local project room: the current full cut and segment
cuts, production readiness, take/timing freshness, approvals, exact revisions,
and timecoded review notes in one video-centered view. Segment notes bind to a
segment cut; notes made while the full cut is playing bind to the assembled
artifact and its global timecode.

## The files you author

```text
brief.md                 audience, promise, constraints, target length
outline.md               intellectual arc
script.md                cohesive read-through, direction, visual notes
scenes/<segment-id>.py   ordinary Manim, one independent file per segment
style.py                 genuinely shared palette and helpers
feedback.md              durable review observations and decisions
context/                 pinned excerpts from reference videos
```

The rest is supporting memory:

```text
chalk.toml               small project/render configuration
media/takes/             immutable accepted recordings, addressed by hash
transcripts/             word timings tied to exact recordings and model
.chalk/state.json        one selected take and optional approval per segment
.chalk/snapshots/        content-addressed project closures
.chalk/cache/            ignored local render/mux action cache
output/                  ignored convenient aliases to current cuts
releases/                named release manifests and optional masters
```

### Stable segments without a registry

Every `##` script section has one unobtrusive stable ID:

```markdown
## Square both sides
<!-- chalk:segment square-both-sides -->

Square both sides. Two equals alpha squared over beta squared.

> **[VISUAL]** Transform the equation one step at a time.
```

The heading and order can change. The ID stays put and joins that prose to
`scenes/square-both-sides.py`, its recordings, feedback, and artifacts. There
is no Bash array, numbered class, duration dictionary, or audio stem to keep in
sync.

## The normal loop

### 1. Brief, outline, and context

Write `brief.md` and shape the arc in `outline.md`. If an agent should learn
from earlier videos, pin the exact material instead of relying on whatever a
sibling directory happens to contain later:

```bash
./chalk context add ../category-theory \
  --files outline.md script.md \
  --label category-theory \
  --note "voice and abstraction-to-example pacing"
```

The generated Markdown pack records source hashes and Git state and can be
committed with the project.

### 2. Script and visuals in parallel

The script stays in one file so it can be read and edited as a whole. Visual
work is split by stable segment, so agents can work independently:

```text
script.md
scenes/intro.py
scenes/square-both-sides.py
scenes/closing.py
```

Use `./chalk segment add "Square both sides"` when convenient, or edit the
Markdown and Python directly. `./chalk check` explains missing IDs/scenes and
literal cue phrases that no longer occur in the narration.

### 3. Watch before recording

Scene code uses phrases rather than copied timestamps:

```python
from manim import *
from chalk_runtime import ChalkScene


class Visual(ChalkScene):
    def construct(self):
        equation = MathTex(r"\sqrt{2} = \alpha / \beta")
        squared = MathTex(r"2 = \alpha^2 / \beta^2")

        self.play_on("square both sides", FadeIn(equation), lead=0.2)
        self.land_on("two equals alpha squared", Transform(equation, squared))
        self.finish()
```

Before a recording exists, Chalk estimates word timing from the narration. This
makes the early animatic useful while script and visual work are still happening:

```bash
./chalk watch square-both-sides
```

Only the requested segment must be healthy. Incomplete later scenes do not block
local iteration.

### 4. Record, align, and immediately review A/V

```bash
./chalk record square-both-sides
```

A kept performance is converted to lossless audio, stored under its SHA-256,
bound to the exact narration revision, transcribed, used to resolve phrase cues,
and played back with the newly built visual. Rerecording never overwrites the
previous take. Rejected scratch recordings never change the selected take.

You can also import an existing recording:

```bash
./chalk take import square-both-sides path/to/take.wav --select
./chalk transcribe square-both-sides
./chalk review square-both-sides
```

Natural performance differences are expected. Chalk aligns script tokens to
Whisper words rather than requiring an exact transcript. A spoken-line edit
marks the old take stale; a director-note edit does not.

### 5. Make feedback durable

During review, capture the actual observation and timecode:

```bash
./chalk note add square-both-sides \
  --at 12.4 --category visual --severity bug \
  "the denominator clips the frame before the transform"
```

The note is appended to readable `feedback.md` and bound to the exact snapshot
and cut under discussion. Agents may edit the task list directly. Resolve it
only after checking the repaired A/V:

```bash
./chalk note resolve n_ab12cd --resolution "reflowed the fraction and checked draft cut"
```

This captures the useful delta from an agent conversation without archiving the
whole conversation.

### 6. Review locally, then as a whole

```bash
./chalk review square-both-sides
./chalk review --full --profile review
```

Segment review catches timing and visual defects early. Full review is for
transitions, global pacing, and whether the intellectual arc actually knits
together.

### 7. Snapshot, checkpoint, and release

```bash
./chalk snapshot recording-candidate
./chalk checkpoint rough-cut-3
./chalk release v1
```

A snapshot is a content-addressed closure of the exact brief, outline, script
facets, scenes, style/assets, context, selected takes, transcripts, build
recipes, and environment. A checkpoint additionally makes an intentional Git
commit. A release performs stricter completeness checks and writes a portable
release manifest beside the master.

Git explains *why and when* authored work changed. Content hashes explain
*exactly which versions* were joined into a particular cut.

## Commands

The human loop is deliberately small:

```text
./chalk                         status and suggested next action
./chalk watch [segment]         rebuild changed proxy and play it
./chalk record [segment]        immutable take through immediate A/V review
./chalk review [segment]        review a segment or --full video
./chalk open                    open the local video/project room
./chalk release <name>          strict final build and release record
```

Supporting commands:

```text
./chalk new PATH                create an independent project
./chalk check [segment...]      validate only the requested source closure
./chalk segment add TITLE       add stable script/scene pair
./chalk render [segment...]     render without opening a player
./chalk take ...                import, select, and inspect immutable takes
./chalk transcribe [segment...] cache word timings for selected takes
./chalk note ...                add/list/resolve timecoded feedback
./chalk approve [segment...]    bind approval to current content
./chalk context ...             pin exact reference material
./chalk snapshot [name]         write a content-addressed closure
./chalk trace [ref]             inspect lineage when needed
./chalk doctor                  inspect local tools and locks
```

Human-readable output is the default. Commands that support automation also
offer JSON output.

## Speed and cache correctness

Each derived operation has an immutable action key. The render key includes the
segment scene, shared helpers/assets, resolved timing, explicit profile, recipe,
and relevant tool versions. The mux key includes that render and exact selected
audio. The full-cut key includes the ordered segment cuts.

Consequences:

- changing one scene rebuilds one scene and its downstream cut;
- changing shared style invalidates every scene that conservatively depends on it;
- a retake rebuilds that transcript/timeline/scene/mux and the full cut;
- reordering segments rebuilds only the full cut;
- warm no-op commands verify and reuse their result;
- output paths never determine freshness;
- Manim's own cache cannot return a render from a different Chalk action.

Draft profiles are optimistic and allow estimated timing or silence. Release is
strict about missing source media, structural errors, blocking feedback, hashes,
and environment locks. Provenance remains ambient until `trace` or release.

## Design rationale

The previous Chalk template proved that agents are already good at Markdown,
Python, and Manim. Chalk2 proved that stable IDs, immutable takes, explicit
targets, phrase cues, and incremental composition are useful—but also that a
custom renderer, verbose visual interchange format, and user-visible provenance
machinery can displace the creative work.

This version keeps the invariants and removes the ceremony. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the precise boundary and
[`docs/CRAFT.md`](docs/CRAFT.md) for reusable writing, visual, audio, and review
lessons carried forward from actual production.
