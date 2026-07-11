from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from _chalk.model import (
    ProjectState,
    find_project_root,
    load_project,
    parse_script,
    save_state,
    validate_project,
)
from _chalk.review import (
    append_note,
    approval_status,
    approve,
    context_add,
    create_snapshot,
    list_notes,
    resolve_note,
    resolve_snapshot,
)
from _chalk.store import Store


SCRIPT = """# Example

## Intro title
<!-- chalk:segment intro -->

> **[DIRECTOR: calm and direct.]**

This is the spoken intro.

> **[VISUAL: intro]**
> Bring in the title on spoken intro.

## Second idea
<!-- chalk:segment second -->

Now explain the second idea.
"""


INTRO_SCENE = """class Visual:
    def construct(self):
        self.play_on("spoken intro", object())
"""


def write(path: Path, contents: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(contents, bytes):
        path.write_bytes(contents)
    else:
        path.write_text(contents, encoding="utf-8")


def make_project(root: Path, *, second_scene: bool = True) -> Path:
    write(
        root / "chalk.toml",
        """schema = 1
project_id = "project-test"
title = "Test project"
slug = "test-project"
""",
    )
    write(root / "brief.md", "# Brief\n\nExplain the thing.\n")
    write(root / "outline.md", "# Outline\n\nIntro, then second idea.\n")
    write(root / "script.md", SCRIPT)
    write(root / "style.py", "BACKGROUND = '#111827'\n")
    write(root / "scenes" / "intro.py", INTRO_SCENE)
    if second_scene:
        write(
            root / "scenes" / "second.py",
            (
                "class Visual:\n"
                "    def construct(self):\n"
                "        self.land_on('second idea', object())\n"
            ),
        )
    return root


class ScriptModelTests(unittest.TestCase):
    def test_root_discovery_and_facet_hashes_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            nested = root / "scenes" / "nested"
            nested.mkdir()
            self.assertEqual(find_project_root(nested), root.resolve())

            parsed = parse_script(root / "script.md")
            self.assertEqual([segment.id for segment in parsed], ["intro", "second"])
            intro = parsed[0]
            self.assertEqual(intro.title, "Intro title")
            self.assertEqual(intro.order, 0)
            self.assertEqual(intro.source, Path("scenes/intro.py"))
            self.assertEqual(intro.source_line, 3)
            self.assertEqual(intro.narration, "This is the spoken intro.")
            self.assertEqual(intro.director_notes, ("[DIRECTOR: calm and direct.]",))
            self.assertIn("[VISUAL: intro]", intro.visual_notes[0])

            original_narration = intro.narration_hash
            original_director = intro.director_hash
            original_visual = intro.visual_hash
            changed = SCRIPT.replace("calm and direct", "quick and amused")
            write(root / "script.md", changed)
            revised = parse_script(root / "script.md")[0]
            self.assertEqual(revised.narration_hash, original_narration)
            self.assertNotEqual(revised.director_hash, original_director)
            self.assertEqual(revised.visual_hash, original_visual)

    def test_missing_duplicate_markers_and_literal_cues_are_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            write(
                root / "script.md",
                """# Example
## A
<!-- chalk:segment repeated -->
Say alpha.
## B
<!-- chalk:segment repeated -->
Say beta.
## C
No marker here.
""",
            )
            write(
                root / "scenes" / "repeated.py",
                (
                    "class Visual:\n"
                    "    def construct(self):\n"
                    "        phrase = 'alpha'\n"
                    "        self.play_on(phrase, object())\n"
                ),
            )
            project = load_project(root)
            report = validate_project(project)
            codes = {item.code for item in report.diagnostics}
            self.assertIn("segment-id-duplicate", codes)
            self.assertIn("segment-id-missing", codes)
            self.assertIn("scene-cue-not-literal", codes)

    def test_validation_is_scoped_and_audio_absence_is_only_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary), second_scene=False)
            write(root / "scenes" / "orphan.py", "class Visual:\n    pass\n")
            project = load_project(root)

            scoped = validate_project(project, "intro")
            self.assertTrue(scoped.ok)
            self.assertIn("take-unselected", {item.code for item in scoped.warnings})
            self.assertNotIn("scene-orphan", {item.code for item in scoped.diagnostics})

            full = validate_project(project)
            codes = {item.code for item in full.errors}
            self.assertIn("scene-missing", codes)
            self.assertIn("scene-orphan", codes)

            take_id = "1" * 64
            audio_hash = "2" * 64
            save_state(root, ProjectState(selected_takes={"intro": take_id}))
            write(
                root / "media" / "takes" / "takes.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "takes": [
                            {
                                "id": take_id,
                                "segment_id": "intro",
                                "narration_sha256": project.segment("intro").narration_hash,
                                "audio_sha256": audio_hash,
                                "path": f"media/takes/sha256/{audio_hash}.flac",
                                "duration_us": 1_000_000,
                                "created_at": "2026-01-01T00:00:00Z",
                                "metadata": {},
                            }
                        ],
                    }
                ),
            )
            selected = validate_project(load_project(root), "intro")
            self.assertTrue(selected.ok)
            selected_codes = {item.code for item in selected.warnings}
            self.assertIn("take-missing", selected_codes)
            self.assertIn("transcript-missing", selected_codes)

            save_state(root, ProjectState(selected_takes={"intro": "unknown"}))
            dangling = validate_project(load_project(root), "intro")
            self.assertIn("take-selection-unknown", {item.code for item in dangling.errors})

    def test_scoped_check_ignores_an_unrelated_unregistered_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            write(
                root / "script.md",
                SCRIPT.replace("<!-- chalk:segment second -->\n", ""),
            )
            scoped = validate_project(load_project(root), "intro")
            self.assertTrue(scoped.ok)
            self.assertNotIn(
                "segment-id-missing", {item.code for item in scoped.diagnostics}
            )
            self.assertIn(
                "segment-id-missing",
                {item.code for item in validate_project(load_project(root)).errors},
            )

    def test_fenced_markdown_examples_do_not_create_segments_or_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            write(
                root / "script.md",
                """# Example

## Real intro
<!-- chalk:segment intro -->

This is spoken.

```markdown
## Not a segment
<!-- chalk:segment fenced-fake -->
This example is not narration.
```

## Real second
<!-- chalk:segment second -->

The second idea is spoken.
""",
            )
            parsed = parse_script(root / "script.md")
            self.assertEqual([segment.id for segment in parsed], ["intro", "second"])
            self.assertEqual(parsed[0].narration, "This is spoken.")
            self.assertFalse(parsed.diagnostics)

    def test_scene_validation_uses_the_runtime_phrase_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            write(
                root / "script.md",
                """# Example

## Intro
<!-- chalk:segment intro -->

Can't stop. Alpha beta, then alpha beta. Foobar follows.
""",
            )
            write(
                root / "scenes" / "intro.py",
                """class Visual:
    def construct(self):
        self.play_on("cant", object())
        self.play_on("alpha beta", object())
        self.play_on("foo", object())
""",
            )
            codes = {
                item.code
                for item in validate_project(load_project(root), "intro").diagnostics
            }
            self.assertIn("scene-cue-ambiguous", codes)
            self.assertIn("scene-cue-not-in-narration", codes)
            # Punctuation normalization makes "cant" resolve to "Can't".
            self.assertEqual(
                sum(
                    item.code == "scene-cue-not-in-narration"
                    for item in validate_project(load_project(root), "intro").diagnostics
                ),
                1,
            )

            source = (root / "scenes" / "intro.py").read_text(encoding="utf-8")
            write(
                root / "scenes" / "intro.py",
                source.replace(
                    'self.play_on("alpha beta", object())',
                    'self.play_on("alpha beta", object(), occurrence=2)',
                ).replace('        self.play_on("foo", object())\n', ""),
            )
            self.assertTrue(validate_project(load_project(root), "intro").ok)


class ReviewTests(unittest.TestCase):
    def test_snapshots_are_deterministic_and_refs_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            project = load_project(root)
            first = create_snapshot(project, "draft")
            second = create_snapshot(load_project(root))
            self.assertEqual(first.digest, second.digest)
            self.assertEqual(resolve_snapshot(project, "draft").digest, first.digest)
            self.assertTrue(first.path.is_file())

            write(root / "outline.md", "# Outline\n\nA different intellectual arc.\n")
            changed = create_snapshot(load_project(root))
            self.assertNotEqual(changed.digest, first.digest)

    def test_snapshot_binds_assets_feedback_and_project_local_tool_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            write(root / "assets" / "diagram.svg", "<svg><!-- v1 --></svg>\n")
            write(root / "feedback.md", "# Feedback\n\n## Open\n")
            write(root / "chalk", "#!/usr/bin/env python3\n")
            write(root / "chalk_runtime.py", "RUNTIME_VERSION = 1\n")
            write(root / "_chalk" / "build.py", "BUILD_VERSION = 1\n")
            write(root / "visual_helpers.py", "GRID_GAP = 0.4\n")

            first = create_snapshot(load_project(root))
            components = first.data["components"]
            self.assertEqual(components["assets"][0]["path"], "assets/diagram.svg")
            self.assertEqual(components["feedback"]["path"], "feedback.md")
            self.assertEqual(
                [entry["path"] for entry in components["tool_sources"]],
                ["chalk", "chalk_runtime.py", "_chalk/build.py"],
            )
            self.assertEqual(
                [entry["path"] for entry in components["helpers"]],
                ["visual_helpers.py"],
            )

            write(root / "_chalk" / "build.py", "BUILD_VERSION = 2\n")
            second = create_snapshot(load_project(root))
            self.assertNotEqual(second.digest, first.digest)

    def test_snapshot_persists_action_records_outside_the_ignored_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            store = Store(root)
            leaf = store.run_action(
                "render",
                inputs={"scene": "sha256:scene"},
                recipe={"version": 1},
                environment={"manim": "test"},
                producer=lambda _workdir: {"video": b"rendered"},
            ).record
            root_action = store.run_action(
                "mux",
                inputs={"render": leaf.key, "audio": "sha256:audio"},
                recipe={"version": 1},
                environment={"ffmpeg": "test"},
                dependencies=(leaf.key,),
                producer=lambda _workdir: {"video": b"muxed"},
            ).record
            save_state(
                root,
                ProjectState(current_actions={"segment:intro:cut": root_action.key}),
            )

            snapshot = create_snapshot(load_project(root))
            actions = snapshot.data["components"]["actions"]
            self.assertEqual({item["kind"] for item in actions}, {"render", "mux"})
            mux = next(item for item in actions if item["kind"] == "mux")
            self.assertEqual(mux["dependencies"], [leaf.key])
            self.assertEqual(snapshot.data["components"]["unresolved_action_refs"], [])

    def test_snapshot_binds_selected_take_record_blob_and_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            project = load_project(root)
            audio = b"immutable flac bytes"
            audio_hash = hashlib.sha256(audio).hexdigest()
            take_id = "3" * 64
            blob = root / "media" / "takes" / "sha256" / f"{audio_hash}.flac"
            write(blob, audio)
            write(
                root / "media" / "takes" / "takes.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "takes": [
                            {
                                "id": take_id,
                                "segment_id": "intro",
                                "narration_sha256": project.segment("intro").narration_hash,
                                "audio_sha256": audio_hash,
                                "path": blob.relative_to(root).as_posix(),
                                "duration_us": 1_000_000,
                                "created_at": "2026-01-01T00:00:00Z",
                                "metadata": {"room": "desk"},
                            }
                        ],
                    }
                ),
            )
            transcript = root / "transcripts" / f"{audio_hash}.json"
            model_fingerprint = "sha256:model-test"
            options = {"language": "en", "word_timestamps": True}
            from _chalk.audio import AudioStore

            cache_key = AudioStore.transcript_cache_key(
                audio_hash, model_fingerprint, options
            )
            transcript_record = {
                "schema_version": 1,
                "audio_sha256": audio_hash,
                "cache_key": cache_key,
                "model": "test/whisper",
                "model_revision": "a" * 40,
                "model_fingerprint": model_fingerprint,
                "options": options,
                "words": [],
                "created_at": "2026-01-01T00:00:00Z",
                "provenance": {},
            }
            write(
                transcript,
                json.dumps(transcript_record),
            )
            write(
                root / "transcripts" / "sha256" / f"{cache_key}.json",
                json.dumps(transcript_record),
            )
            save_state(root, ProjectState(selected_takes={"intro": take_id}))
            project = load_project(root)
            self.assertTrue(validate_project(project, "intro").ok)
            selection = create_snapshot(project).data["components"]["selections"][0]
            self.assertEqual(selection["take"]["selected_take_id"], take_id)
            self.assertEqual(selection["take"]["file"]["sha256"], audio_hash)
            self.assertEqual(selection["transcript"]["path"], f"transcripts/{audio_hash}.json")
            self.assertEqual(selection["transcript"]["cache_key"], cache_key)
            self.assertEqual(
                selection["transcript"]["immutable"]["path"],
                f"transcripts/sha256/{cache_key}.json",
            )

            config = (root / "chalk.toml").read_text(encoding="utf-8")
            write(
                root / "chalk.toml",
                config
                + "\n[voice]\n"
                + 'model = "test/whisper"\n'
                + f'revision = "{"a" * 40}"\n'
                + 'language = "fr"\n',
            )
            diagnostics = validate_project(load_project(root), "intro").diagnostics
            self.assertIn(
                "transcript-config-stale", {item.code for item in diagnostics}
            )

    def test_notes_are_readable_resolvable_and_snapshot_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            project = load_project(root)
            artifact = root / "output" / "intro.mp4"
            write(artifact, b"not really a video")
            note = append_note(
                project,
                "The title arrives before the phrase.",
                segment="intro",
                category="timing",
                severity="major",
                timecode="00:01.250",
                artifact=artifact,
            )
            self.assertEqual(len(list_notes(project)), 1)
            self.assertEqual(list_notes(project)[0].snapshot, note.snapshot)
            feedback = (root / "feedback.md").read_text(encoding="utf-8")
            self.assertIn("- [ ]", feedback)
            self.assertIn("chalk:note", feedback)
            self.assertEqual(resolve_note(project, note.id).resolved, True)
            self.assertEqual(list_notes(project), [])
            self.assertEqual(len(list_notes(project, include_resolved=True)), 1)

    def test_visible_feedback_is_authoritative_and_manual_blockers_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            project = load_project(root)
            generated = append_note(project, "Original visible wording", segment="intro")
            path = root / "feedback.md"
            contents = path.read_text(encoding="utf-8").replace(
                "Original visible wording", "Author corrected the visible wording", 1
            )
            contents = contents.replace(
                "## Open\n",
                "## Open\n\n- [ ] **manual-block** — [blocker] equation is false\n",
                1,
            )
            write(path, contents)

            notes = {note.id: note for note in list_notes(project)}
            self.assertEqual(
                notes[generated.id].text, "Author corrected the visible wording"
            )
            self.assertEqual(notes["manual-block"].severity, "blocker")
            self.assertEqual(
                notes["manual-block"].text, "[blocker] equation is false"
            )
            self.assertTrue(
                resolve_note(project, "manual-block", resolution="corrected").resolved
            )

    def test_approval_becomes_stale_after_relevant_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = make_project(Path(temporary))
            project = load_project(root)
            approved = approve(project, "intro")
            self.assertEqual(approved.state, "current")
            self.assertTrue(approval_status(load_project(root), "intro").current)

            write(root / "scenes" / "intro.py", INTRO_SCENE + "\n# visual revision\n")
            self.assertEqual(approval_status(load_project(root), "intro").state, "stale")

            write(root / "scenes" / "intro.py", INTRO_SCENE)
            approved_again = approve(load_project(root), "intro")
            self.assertTrue(approved_again.current)
            write(root / "assets" / "equation.svg", "<svg><!-- revised --></svg>\n")
            self.assertEqual(approval_status(load_project(root), "intro").state, "stale")

    def test_context_pack_copies_exact_text_and_enters_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = make_project(workspace / "video")
            reference = workspace / "sibling" / "script.md"
            write(reference, "# Reference\n\nA pinned explanation.\n")
            project = load_project(root)
            before = create_snapshot(project).digest
            pack = context_add(project, [reference], name="sibling script")
            self.assertTrue(pack.path.is_file())
            self.assertTrue(pack.index_path.is_file())
            contents = pack.path.read_text(encoding="utf-8")
            self.assertIn(str(reference), contents)
            self.assertIn(hashlib.sha256(reference.read_bytes()).hexdigest(), contents)
            self.assertIn("A pinned explanation", contents)
            self.assertEqual(
                context_add(project, [reference], name="sibling script").digest,
                pack.digest,
            )
            after = create_snapshot(load_project(root)).digest
            self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
