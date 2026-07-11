# Chalk tool repository

Read `docs/ARCHITECTURE.md` and `docs/CRAFT.md` before changing Chalk. The governing constraint is
that conversation remains the control plane and plain Markdown/Python remain
the creative interface. Tool state may support authoring; it may not replace it.

The high-value invariants are:

- `script.md` is a cohesive read-through with stable segment IDs.
- Visuals are ordinary, independent Manim files.
- Drafts are optimistic and scoped; one incomplete segment does not block work
  on another.
- Accepted takes are immutable and one tracked pointer selects each segment's
  take.
- Phrase timing works from estimates before recording and aligned Whisper words
  afterward.
- Cache keys use exact derivation inputs and verified output hashes.
- Feedback is readable, version-bound project memory.
- Provenance is derived from snapshots/action records and stays out of the
  normal creative loop.
- A local UI is a projection of files and artifacts, never a second source of
  truth.

Do not add a custom renderer, visual interchange format, database, daemon,
mandatory graph workflow, or full-project gate for a scoped draft. Prefer a
small adapter around Manim/ffmpeg/MLX and a clear diagnostic over a new subsystem.

Run the stdlib suite with:

```bash
python3 -m unittest discover -v tests
```

Generated projects include their own `AGENTS.md`; that file governs video work.
