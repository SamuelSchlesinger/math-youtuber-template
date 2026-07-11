from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import types
import unittest
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from _chalk import cli


@contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def invoke(arguments: list[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    context = working_directory(cwd) if cwd is not None else nullcontext()
    with context, redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class CLITestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.project = self.root / "video"
        self.git_identity = patch.dict(
            os.environ,
            {
                "GIT_AUTHOR_NAME": "CLI Test",
                "GIT_AUTHOR_EMAIL": "cli@example.invalid",
                "GIT_COMMITTER_NAME": "CLI Test",
                "GIT_COMMITTER_EMAIL": "cli@example.invalid",
            },
        )
        self.git_identity.start()
        code, output, error = invoke(
            ["new", str(self.project), "--title", "A Test Video", "--json"]
        )
        self.assertEqual((code, error), (0, ""), output)
        self.new_record = json.loads(output)

    def tearDown(self) -> None:
        self.git_identity.stop()
        self.temporary_directory.cleanup()

    def test_new_starts_fresh_git_history_and_copies_project_local_tool(self) -> None:
        self.assertRegex(
            self.new_record["project_id"], r"^project-[0-9a-f]{32}$"
        )
        self.assertRegex(self.new_record["source_tree"], r"^[0-9a-f]{64}$")
        self.assertTrue((self.project / "_chalk" / "cli.py").is_file())
        self.assertTrue((self.project / "_chalk" / "ui" / "index.html").is_file())
        self.assertTrue(os.access(self.project / "chalk", os.X_OK))
        self.assertTrue(os.access(self.project / "setup.sh", os.X_OK))
        self.assertTrue((self.project / ".chalk" / "state.json").is_file())
        attributes = (self.project / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("media/takes/sha256/** filter=lfs", attributes)
        self.assertNotIn("media/takes/** filter=lfs", attributes)
        self.assertIn(".chalk/scratch/", (self.project / ".gitignore").read_text())

        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=self.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        remotes = subprocess.run(
            ["git", "remote"],
            cwd=self.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        author = subprocess.run(
            ["git", "show", "-s", "--format=%an <%ae>", "HEAD"],
            cwd=self.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(count, "1")
        self.assertEqual(remotes, "")
        self.assertEqual(author, "CLI Test <cli@example.invalid>")
        self.assertNotIn("__TITLE__", (self.project / "script.md").read_text())

    def test_new_escapes_hostile_titles_without_rewriting_the_copied_tool(self) -> None:
        destination = self.root / "quoted-video"
        title = 'Why "e" and \\ Matter'
        code, output, error = invoke(
            ["new", str(destination), "--title", title, "--json"]
        )
        self.assertEqual((code, error), (0, ""), output)
        with (destination / "chalk.toml").open("rb") as stream:
            self.assertEqual(tomllib.load(stream)["project"]["title"], title)
        subprocess.run(
            [sys.executable, "-m", "py_compile", "_chalk/cli.py", "scenes/intro.py"],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            [sys.executable, "chalk", "status", "--json"],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout)["title"], title)

    def test_checkpoint_captures_new_assets_and_project_helpers(self) -> None:
        asset = self.project / "assets" / "diagram.txt"
        asset.parent.mkdir()
        asset.write_text("diagram source\n", encoding="utf-8")
        (self.project / "visual_helpers.py").write_text(
            "GRID_GAP = 0.4\n", encoding="utf-8"
        )
        code, output, error = invoke(["checkpoint", "with-assets"], cwd=self.project)
        self.assertEqual((code, error), (0, ""), output)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(status, "")
        tracked = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=self.project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertIn("assets/diagram.txt", tracked)
        self.assertIn("visual_helpers.py", tracked)

    def test_status_and_scoped_check_are_human_first_with_json_available(self) -> None:
        code, output, error = invoke([], cwd=self.project)
        self.assertEqual((code, error), (0, ""))
        self.assertIn("A Test Video", output)
        self.assertIn("next:", output)
        self.assertIn("chalk watch intro", output)

        code, output, error = invoke(["check", "intro", "--json"], cwd=self.project)
        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reports"][0]["scope"], "intro")
        self.assertEqual(result["warnings"], 1)

        code, output, _ = invoke(["check", "missing"], cwd=self.project)
        self.assertEqual(code, 1)
        self.assertIn("unknown segment", output)

    def test_segment_add_inserts_stable_section_and_independent_scene(self) -> None:
        code, output, error = invoke(
            ["segment", "add", "Middle Thought", "--after", "intro", "--json"],
            cwd=self.project,
        )
        self.assertEqual((code, error), (0, ""), output)
        record = json.loads(output)
        self.assertEqual(record["id"], "middle-thought")
        scene = self.project / record["scene"]
        self.assertTrue(scene.is_file())
        self.assertIn("class Visual(ChalkScene)", scene.read_text())

        script = (self.project / "script.md").read_text()
        intro = script.index("chalk:segment intro")
        middle = script.index("chalk:segment middle-thought")
        closing = script.index("chalk:segment closing")
        self.assertLess(intro, middle)
        self.assertLess(middle, closing)

        code, output, _ = invoke(
            ["check", "middle-thought", "--json"], cwd=self.project
        )
        self.assertEqual(code, 0, output)

    def test_note_context_and_snapshot_adapters_leave_readable_source(self) -> None:
        reference = self.root / "reference"
        reference.mkdir()
        (reference / "outline.md").write_text("# A useful arc\n", encoding="utf-8")

        code, output, error = invoke(
            [
                "context",
                "add",
                str(reference),
                "--files",
                "outline.md",
                "--label",
                "earlier-video",
                "--note",
                "borrow the pacing",
                "--json",
            ],
            cwd=self.project,
        )
        self.assertEqual((code, error), (0, ""), output)
        context_record = json.loads(output)
        self.assertTrue(Path(context_record["path"]).is_file())
        self.assertIn(
            "borrow the pacing",
            (self.project / "context" / "index.md").read_text(),
        )

        code, output, error = invoke(
            [
                "note",
                "add",
                "intro",
                "the title arrives too late",
                "--at",
                "1.25",
                "--category",
                "timing",
                "--json",
            ],
            cwd=self.project,
        )
        self.assertEqual((code, error), (0, ""), output)
        note = json.loads(output)
        self.assertEqual(note["segment"], "intro")
        self.assertEqual(note["timecode"], "1.25s")

        code, _, error = invoke(
            [
                "note",
                "resolve",
                note["id"],
                "--resolution",
                "moved the title earlier and reviewed it",
            ],
            cwd=self.project,
        )
        self.assertEqual((code, error), (0, ""))
        feedback = (self.project / "feedback.md").read_text()
        self.assertIn(f"- [x] **{note['id']}**", feedback)
        self.assertIn("Resolution: moved the title earlier", feedback)

        code, output, error = invoke(
            ["snapshot", "rough-cut", "--json"], cwd=self.project
        )
        self.assertEqual((code, error), (0, ""), output)
        snapshot = json.loads(output)
        self.assertRegex(snapshot["digest"], r"^[0-9a-f]{64}$")
        self.assertIn("feedback.md", snapshot["provenance"]["git"]["dirty_paths"])
        self.assertTrue(Path(snapshot["path"]).is_file())
        self.assertTrue((self.project / ".chalk" / "refs" / "rough-cut.json").is_file())

    def test_build_and_server_are_delayed_mockable_integrations(self) -> None:
        calls: list[tuple[str, object]] = []

        class BuildEngine:
            def __init__(self, project):
                calls.append(("init", project.root))

            def render(self, segment_ids, profile=None, *, draft=True):
                calls.append(("render", (segment_ids, profile, draft)))
                return ({"path": "render.mp4"},)

            def build_segment(self, segment_id, profile=None, *, draft=True):
                calls.append(("segment", (segment_id, profile, draft)))
                return {"path": "cut.mp4"}

        build = types.ModuleType("_chalk.build")
        build.BuildEngine = BuildEngine
        with patch.dict(sys.modules, {"_chalk.build": build}):
            code, output, error = invoke(
                ["watch", "intro", "--no-play", "--json"], cwd=self.project
            )
        self.assertEqual((code, error), (0, ""), output)
        self.assertEqual(json.loads(output)["path"], "cut.mp4")
        self.assertEqual(calls[-1], ("segment", ("intro", None, True)))

        with patch.dict(sys.modules, {"_chalk.build": build}):
            code, output, error = invoke(
                ["render", "intro", "--json"], cwd=self.project
            )
        self.assertEqual((code, error), (0, ""), output)
        self.assertEqual(json.loads(output)[0]["path"], "render.mp4")
        self.assertEqual(calls[-1], ("render", (["intro"], None, True)))

        waited: list[bool] = []

        class Handle:
            url = "http://127.0.0.1:4321/"

            def wait(self) -> None:
                waited.append(True)

        server = types.ModuleType("_chalk.server")

        def serve(project, **options):
            del project, options
            print(Handle.url)
            return Handle()

        server.serve = serve
        with patch.dict(sys.modules, {"_chalk.server": server}):
            code, output, error = invoke(
                ["open", "--no-browser", "--port", "4321"], cwd=self.project
            )
        self.assertEqual((code, error), (0, ""))
        self.assertIn("http://127.0.0.1:4321/", output)
        self.assertEqual(waited, [True])

    def test_cli_prefers_the_fresh_state_builder_facade(self) -> None:
        chosen: list[str] = []

        class BuildEngine:
            def __init__(self, _project):
                chosen.append("engine")

        class Builder:
            def __init__(self, _project):
                chosen.append("builder")

        build = types.ModuleType("_chalk.build")
        build.BuildEngine = BuildEngine
        build.Builder = Builder
        with patch.dict(sys.modules, {"_chalk.build": build}):
            instance = cli._builder(cli._load_project(self.project))
        self.assertIsInstance(instance, Builder)
        self.assertEqual(chosen, ["builder"])

    def test_watch_dispatches_through_facade_state_merge(self) -> None:
        calls: list[tuple[object, ...]] = []

        class Builder:
            def __init__(self, _project):
                pass

            def watch(self, segment_ids, profile=None, *, play=True, full=False):
                calls.append((tuple(segment_ids), profile, play, full))
                return {"path": "cut.mp4"}

            def build_segment(self, *_args, **_kwargs):
                raise AssertionError("low-level deferred method must not be called")

        build = types.ModuleType("_chalk.build")
        build.Builder = Builder
        with patch.dict(sys.modules, {"_chalk.build": build}):
            code, output, error = invoke(
                ["watch", "intro", "--no-play", "--json"], cwd=self.project
            )
        self.assertEqual((code, error), (0, ""), output)
        self.assertEqual(calls, [(('intro',), None, False, False)])

    def test_trace_uses_the_current_action_closure_without_mutating_a_snapshot(self) -> None:
        from _chalk.model import load_project

        project = load_project(self.project)
        project.state.current_actions["full"] = "a" * 64
        project.save_state()

        class Record:
            kind = "full-composite"
            key = "sha256:" + "a" * 64
            dependencies = ("sha256:" + "b" * 64,)

            def to_dict(self):
                return {
                    "kind": self.kind,
                    "key": self.key,
                    "dependencies": list(self.dependencies),
                }

        class Builder:
            def trace(self, artifact):
                self.artifact = artifact
                return (Record(),)

        builder = Builder()
        with patch("_chalk.cli._builder", return_value=builder):
            code, output, error = invoke(["trace", "--json"], cwd=self.project)

        self.assertEqual((code, error), (0, ""), output)
        result = json.loads(output)
        self.assertEqual(result["rootAction"], "a" * 64)
        self.assertEqual(result["actions"][0]["kind"], "full-composite")
        self.assertEqual(builder.artifact, "a" * 64)

    def test_take_import_can_be_injected_without_media_dependencies(self) -> None:
        source = self.root / "take.wav"
        source.write_bytes(b"fake wav")
        calls: list[tuple[str, str, Path, bool]] = []

        class Take:
            id = "a" * 64

            def to_record(self):
                return {"id": self.id}

        class FakeStore:
            def __init__(self, root):
                self.root = root

            def import_take(self, segment, narration, path, *, select=False):
                calls.append((segment, narration, Path(path), select))
                return Take()

        with patch("_chalk.audio.AudioStore", FakeStore):
            code, output, error = invoke(
                ["take", "import", "intro", str(source), "--select", "--json"],
                cwd=self.project,
            )
        self.assertEqual((code, error), (0, ""), output)
        self.assertEqual(json.loads(output)["id"], "a" * 64)
        self.assertEqual(calls[0][0], "intro")
        self.assertTrue(calls[0][3])

    def test_transcription_uses_pinned_voice_configuration_by_default(self) -> None:
        calls: list[tuple[str, str, str | None, object]] = []

        class FakeStore:
            def __init__(self, root):
                self.root = root

            def transcribe_selected(self, segment, *, model, revision, options):
                calls.append((segment, model, revision, options))
                return {"segment": segment, "revision": revision}

        with patch("_chalk.audio.AudioStore", FakeStore):
            code, output, error = invoke(
                ["transcribe", "intro", "--json"], cwd=self.project
            )

        self.assertEqual((code, error), (0, ""), output)
        self.assertEqual(len(calls), 1)
        segment, model, revision, options = calls[0]
        self.assertEqual(segment, "intro")
        self.assertEqual(model, "mlx-community/whisper-large-v3-turbo")
        self.assertRegex(revision or "", r"^[0-9a-f]{40}$")
        self.assertEqual(options, {"language": "en"})

    def test_production_commands_reexec_into_project_venv_when_present(self) -> None:
        python = self.project / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n", encoding="utf-8")
        python.chmod(0o755)

        with working_directory(self.project), patch("os.execve") as execute:
            cli._maybe_reexec_in_project_venv(["record", "intro"], "record")

        self.assertEqual(execute.call_count, 1)
        executable, arguments, environment = execute.call_args.args
        self.assertEqual(Path(executable).resolve(), python.resolve())
        self.assertEqual(arguments[-2:], ["record", "intro"])
        self.assertEqual(environment["CHALK_VENV_REEXEC"], "1")
        self.assertEqual(
            Path(environment["VIRTUAL_ENV"]).resolve(),
            (self.project / ".venv").resolve(),
        )
        self.assertEqual(
            Path(environment["PATH"].split(os.pathsep)[0]).resolve(),
            (self.project / ".venv" / "bin").resolve(),
        )


class ParserTests(unittest.TestCase):
    def test_help_and_short_human_commands_are_present(self) -> None:
        help_text = cli.build_parser().format_help()
        for command in (
            "watch",
            "record",
            "review",
            "release",
            "note",
            "context",
            "doctor",
        ):
            self.assertRegex(help_text, rf"\b{re.escape(command)}\b")

        parsed = cli.build_parser().parse_args(
            ["note", "add", "intro", "make it earlier", "--at", "2.5"]
        )
        self.assertEqual(parsed.handler, cli._cmd_note_add)
        self.assertEqual(parsed.at, 2.5)


if __name__ == "__main__":
    unittest.main()
