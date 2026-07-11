# Working on Chalk

Read `docs/ARCHITECTURE.md`, `docs/CRAFT.md`, and `CLAUDE.md`. Preserve the AI-native boundary:
agents and authors edit Markdown/Python directly, while Chalk quietly handles
recording, timing, builds, feedback binding, and snapshots.

Before finishing a tool change, run `python3 -m unittest discover -v tests` and
exercise `./chalk new` in a temporary directory. Avoid introducing a new
author-facing schema or required command when the same state can be derived.
Render-time file dependencies belong under `assets/` or in project-local Python
helpers so the action key can identify everything a scene reads.
