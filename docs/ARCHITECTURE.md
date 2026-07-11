# Chalk: an AI-native video workspace

## Product boundary

Chalk is a thin memory and build layer around a conversation between an author
and coding agents. The conversation is the control plane. Markdown and Python
are the shared substrate. Chalk handles the physical operations that benefit
from determinism: project creation, recording, transcription, rendering,
composition, review playback, and release snapshots.

Chalk is deliberately not a renderer, visual DSL, provenance database, task
manager, or nonlinear editor. Manim remains the visual language. Git remains
the source history. Chalk adds stable identity and content-addressed lineage
without making either one the creative interface.

## The authoring surface

A new project should feel this small:

```text
brief.md                 audience, promise, constraints, target length
outline.md               intellectual arc
script.md                complete read-through, direction, and visual notes
scenes/<segment-id>.py   one ordinary Manim scene per segment
style.py                 shared palette and visual helpers
feedback.md              open observations and resolved decisions
context/                 exact, pinned excerpts from reference videos
```

Everything else is tool state or generated output.

`script.md` remains the cohesive writing surface. Each `##` section contains a
stable logical ID in an HTML comment:

```markdown
## Square both sides
<!-- chalk:segment square-both-sides -->
```

The heading and order may change; the ID does not. Chalk derives the scene path,
take selection, transcript, output name, and review target from that ID. There
is no editable segment registry.

Each scene file exports the same class name, because files already provide the
namespace:

```python
from manim import *
from chalk_runtime import ChalkScene

class Visual(ChalkScene):
    def construct(self):
        equation = MathTex(r"x = 2")
        squared = MathTex(r"x^2 = 4")
        self.play_on("square both sides", FadeIn(equation))
        self.land_on("multiply through", Transform(equation, squared))
        self.finish()
```

Agents edit these files directly. Structured commands never replace ordinary
editing.

The render dependency boundary is explicit: runtime data belongs under
`assets/`, and imported author helpers remain inside the project. Reference
packs and sibling repositories inform authoring but are not read by a scene at
render time. This keeps action keys complete without hashing unrelated prose on
every preview.

## Timing

Scene code names phrases, not seconds. Before recording, Chalk estimates a word
timeline from the narration and configured speaking rate. After recording, it
aligns canonical script tokens to Whisper word timestamps. This lets a natural
performance differ slightly from the script while keeping phrase cues useful.

`play_on(phrase, animation)` starts an animation at a phrase. `land_on` makes an
animation finish at a phrase. `finish()` pads the scene to the estimated or
recorded narration duration. Lead-in, current renderer time, animation runtime,
and tail padding are centralized; scene files do not maintain `DUR`, `CUE_*`, or
elapsed-time arithmetic.

Missing recordings and transcripts are normal during drafting. They are status,
not structural errors. Only the requested segment is required to render.

## Identity and lineage

Logical identity and revision identity are different:

- a stable segment ID survives edits, renames, and reordering;
- SHA-256 identifies exact source, media, action, and result revisions;
- Git commits explain human history;
- a snapshot binds the exact outline, script facets, scenes, context, selected
  takes, immutable transcript revision, build action closure, project helpers,
  assets, and project-local tool sources into one root digest.

Narration, director notes, and visual notes are hashed separately. Changing a
director note does not make a recording stale. Changing spoken text does.

Accepted recordings never overwrite earlier recordings. A take has an immutable
audio blob, the narration revision it was recorded against, duration, and useful
observational metadata. Exactly one tracked map selects a take for a segment.
Selected/retained audio is stored by content hash and is intended for Git LFS.

## Quiet content-addressed build

The local cache uses immutable blobs and action records:

```text
.chalk/cache/blobs/sha256/...
.chalk/cache/actions/sha256/...
```

An action key hashes only the inputs that affect that operation, plus its recipe
and environment identity:

```text
scene source + shared helpers + assets + timeline + render profile -> render
render + selected audio + mux recipe                            -> segment cut
ordered segment cuts + release recipe                           -> full cut
```

Independent segments render in parallel. An unchanged action reuses its verified
output. Human-facing paths under `output/` are materialized aliases, never cache
identity. Actions write into a temporary directory and publish their record only
after the result has been validated, hashed, and atomically installed.

There is no separate provenance graph. Lineage is the transitive closure of
immutable action records. `chalk trace` exposes it when useful; normal creative
work does not mention it. Snapshots embed the small action records so lineage
survives cleanup of the ignored working cache; derived video blobs remain
rebuildable rather than becoming tracked project state.

## Feedback and approval

Feedback is readable project source, not a chat transcript or database. A note
records the author's words, segment, category, optional timecode, and the exact
snapshot/cut being discussed in `feedback.md`. Agents may edit the Markdown
directly or use `chalk note`; visible task text is authoritative. Full-cut notes
bind to the assembled action and a global timecode, while segment notes bind to
their corresponding cut.

An approval records the current script, scene, take, timing, and cut hashes. Any
relevant change makes that approval stale automatically. Draft review proceeds
with warnings. Release fails only on genuine structural problems, missing final
inputs, or explicitly blocking feedback.

Agent instructions require two durable actions: capture unresolved user feedback
before a session ends, and resolve a note only after checking the resulting cut.

## Reference context

`chalk context add` copies the selected files or excerpts from sibling projects
into a content-addressed Markdown pack under `context/`. The pack records source
path, Git commit when available, dirty state, and file hashes. Agents therefore
receive the exact reference version used to shape an outline or script, even if
the sibling directory later changes.

## Human loop

The normal commands stay few:

```text
chalk new <path>         create an independent Git project
chalk                    show current state and the most useful next action
chalk watch [segment]    rebuild the changed proxy and play it
chalk record [segment]   preserve a take, transcribe, rebuild, and play A/V
chalk review [segment]   play a cut and capture/resolve feedback
chalk review --full      inspect transitions and the whole arc
chalk release <name>     perform final checks and write a release manifest
```

Advanced commands (`check`, `note`, `context`, `take`, `snapshot`, `trace`, and
`doctor`) exist for agents, automation, and diagnosis. Human-readable output is
the default; machine-readable output is opt-in.

## Version-control boundary

The Chalk tool repository is not cloned to make a video. `chalk new` copies a
pinned project-local tool/runtime into a new directory, initializes a fresh Git
repository, and records the Chalk version that created it. Old videos remain
self-contained on their old tool versions. New tool versions do not silently
migrate active projects.

Git tracks authored text/code, feedback, context packs, take selection, retained
audio, transcript revisions, dependency locks, snapshots, and release manifests.
It ignores caches, scratch recordings, previews, and ordinary derived output. A checkpoint command
may combine validation, a content snapshot, and an intentional Git commit, but
Git remains visible and usable directly.

## Explicit non-goals

- no custom renderer or scene interchange format;
- no YAML/JSON visual authoring;
- no SQLite provenance graph;
- no daemon, actor system, or required UI;
- no full-project validation gate for a single-segment draft;
- no remote CAS or distributed build in the first version;
- no promise of cross-machine bit-identical video in the first version;
- no migration layer for Chalk1 or Chalk2 projects.

The first version promises direct authoring, recoverable human work, correct
cache reuse, exact lineage, and a shorter path from an idea to a watched A/V cut.
