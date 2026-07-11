# Working in this video

The conversation with the author is the control plane. The shared source is
plain Markdown and Python. Read `brief.md`, `outline.md`, `script.md`, and the
open items in `feedback.md` before changing the video. Read `docs/CRAFT.md`
before authoring prose, visuals, or a final mix.

Edit source files directly:

- `brief.md` defines audience, promise, and constraints.
- `outline.md` owns the intellectual arc.
- `script.md` is the cohesive read-through. Every `##` segment keeps its
  `<!-- chalk:segment stable-id -->` comment even if its heading or order moves.
- `scenes/<stable-id>.py` is ordinary Manim code for that segment.
- `style.py` contains genuinely shared visual language.
- `feedback.md` contains durable observations and decisions.

Use phrase timing through `ChalkScene.play_on`, `land_on`, and `finish`. Do not
copy timestamps into scene code or recreate duration dictionaries.
Put every file a scene opens at render time under `assets/`, and keep imported
author helpers inside the project. Do not make rendering depend on a sibling
repository, `context/`, or another undeclared external path; pin the reference,
then turn it into project source or an asset.

Draft work is allowed to be incomplete. Run `./chalk check <segment>` and
`./chalk watch <segment>` on the part you changed; do not make an unrelated
broken segment block local iteration.

Before ending an agent session:

1. Put every unresolved author observation into `feedback.md` (or use
   `./chalk note add`). Preserve the author's wording and timecode when known.
   A direct unbound item uses `- [ ] **short-id** — [blocker] observation`;
   visible Markdown is authoritative and the severity tag is optional.
2. Resolve a note only after the corresponding A/V cut has been checked.
3. Run `./chalk` so the next collaborator can see what is current or stale.

Hashes, takes, transcripts, caches, and snapshots are supporting memory. Do not
hand-edit `.chalk/` or generated output unless diagnosing the tool itself.
