from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _chalk.audio import AudioStore
from _chalk.build import (
    BuildEngine,
    Builder,
    BuildError,
    FFprobeValidator,
    MediaRequirements,
    default_environment,
    project_artifacts,
)
from _chalk.model import load_project
from _chalk.store import Store, action_key, hash_file, hash_tree


MODEL_REVISION = "a" * 40


def accept_media(*_: object) -> None:
    pass


class StoreTests(unittest.TestCase):
    def test_default_environment_binds_lock_and_render_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "requirements.lock"
            lock.write_text("manim==0.20.1\n", encoding="utf-8")
            identity = {
                "available": True,
                "returncode": 0,
                "output_sha256": "sha256:" + "a" * 64,
                "output": "tool configuration\n",
            }
            with (
                patch(
                    "_chalk.build._python_distribution_inventory",
                    return_value=[{"name": "manim", "version": "0.20.1"}],
                ),
                patch("_chalk.build._tool_identity", return_value=identity),
                patch(
                    "_chalk.build._fontconfig_identity",
                    return_value={
                        "available": True,
                        "font_count": 3,
                        "inventory_sha256": "sha256:" + "b" * 64,
                    },
                ),
            ):
                environment = default_environment(root)
            self.assertEqual(environment["requirements_lock"], hash_file(lock))
            self.assertEqual(
                environment["python_distributions"],
                [{"name": "manim", "version": "0.20.1"}],
            )
            for key in (
                "ffmpeg",
                "ffprobe",
                "cairo",
                "pango",
                "pangocairo",
                "latex",
                "dvisvgm",
                "fontconfig",
            ):
                self.assertIn(key, environment)

    def test_action_invalidation_verified_hits_and_tamper_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(temporary)
            calls = 0

            def producer(workdir: Path):
                nonlocal calls
                calls += 1
                output = workdir / "value.bin"
                output.write_bytes(b"derived value")
                return {"value": output}

            first = store.run_action(
                "example",
                inputs={"source": "sha256:one"},
                recipe={"version": 1},
                environment={"tool": "test"},
                producer=producer,
            )
            second = store.run_action(
                "example",
                inputs={"source": "sha256:one"},
                recipe={"version": 1},
                environment={"tool": "test"},
                producer=producer,
            )
            self.assertEqual(calls, 1)
            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)

            changed = store.run_action(
                "example",
                inputs={"source": "sha256:two"},
                recipe={"version": 1},
                environment={"tool": "test"},
                producer=producer,
            )
            self.assertEqual(calls, 2)
            self.assertNotEqual(changed.record.key, first.record.key)

            blob = store.blob_path(first.record.outputs["value"])
            os.chmod(blob, 0o644)
            blob.write_bytes(b"tampered")
            repaired = store.run_action(
                "example",
                inputs={"source": "sha256:one"},
                recipe={"version": 1},
                environment={"tool": "test"},
                producer=producer,
            )
            self.assertEqual(calls, 3)
            self.assertFalse(repaired.cache_hit)
            self.assertEqual(store.read_blob(first.record.outputs["value"]), b"derived value")

    def test_failed_action_never_publishes_and_materialization_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root)
            key = action_key(
                "failure",
                inputs={"input": 1},
                recipe={"version": 1},
                environment={"tool": "test"},
            )

            def failing(workdir: Path):
                (workdir / "partial").write_bytes(b"partial")
                raise RuntimeError("producer failed")

            with self.assertRaisesRegex(RuntimeError, "producer failed"):
                store.run_action(
                    "failure",
                    inputs={"input": 1},
                    recipe={"version": 1},
                    environment={"tool": "test"},
                    producer=failing,
                )
            self.assertIsNone(store.lookup_action(key))

            blob = store.put_bytes(b"new complete value")
            destination = root / "output/value.bin"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old")
            store.materialize(blob, destination)
            self.assertEqual(destination.read_bytes(), b"new complete value")
            self.assertFalse(any(destination.parent.glob(".value.bin.*")))

    def test_tree_identity_ignores_root_and_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            left = Path(first)
            right = Path(second)
            (left / "nested").mkdir()
            (right / "nested").mkdir()
            (left / "nested/source.py").write_text("x = 1\n", encoding="utf-8")
            (right / "nested/source.py").write_text("x = 1\n", encoding="utf-8")
            os.utime(right / "nested/source.py", (1, 1))
            self.assertEqual(hash_tree(left), hash_tree(right))

    def test_single_flight_runs_one_producer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(temporary)
            calls = 0
            calls_lock = threading.Lock()

            def producer(workdir: Path):
                nonlocal calls
                with calls_lock:
                    calls += 1
                time.sleep(0.03)
                output = workdir / "result"
                output.write_bytes(b"one")
                return {"result": output}

            def run():
                return store.run_action(
                    "single-flight",
                    inputs={"x": 1},
                    recipe={"version": 1},
                    environment={"tool": "test"},
                    producer=producer,
                )

            threads = []
            results = []
            for _ in range(4):
                thread = threading.Thread(target=lambda: results.append(run()))
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
            self.assertEqual(calls, 1)
            self.assertEqual(sum(not result.cache_hit for result in results), 1)


@dataclass
class FakeConfig:
    root: Path
    raw: dict[str, object]
    style: Path = Path("style.py")
    slug: str = "test-video"


@dataclass
class FakeState:
    selected_takes: dict[str, str] = field(default_factory=dict)
    current_actions: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeSegment:
    id: str
    order: int
    source: Path
    narration: str


@dataclass
class FakeProject:
    root: Path
    config: FakeConfig
    segments: list[FakeSegment]
    state: FakeState = field(default_factory=FakeState)

    def segment(self, segment_id: str) -> FakeSegment:
        for segment in self.segments:
            if segment.id == segment_id:
                return segment
        raise KeyError(segment_id)

    def scene_path(self, segment: FakeSegment) -> Path:
        return self.root / segment.source


class FakeExecutor:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.lock = threading.Lock()
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    def run(
        self,
        command: list[str] | tuple[str, ...],
        *,
        cwd: Path,
        expected_output: Path,
        env: dict[str, str] | None = None,
    ) -> None:
        if command[0] == "manim":
            kind = "render"
        elif "-f" in command and "concat" in command:
            kind = "full"
        else:
            kind = "segment"
        with self.lock:
            self.counts[kind] += 1
            serial = self.counts[kind]
            self.calls.append((tuple(command), cwd, dict(env or {})))
        expected_output.parent.mkdir(parents=True, exist_ok=True)
        expected_output.write_bytes(f"{kind}-{serial}".encode())

    def capture(
        self,
        command: list[str] | tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> SimpleNamespace:
        del command, cwd, env
        measurements = {
            "input_i": "-18.25",
            "input_tp": "-2.10",
            "input_lra": "4.30",
            "input_thresh": "-28.50",
            "target_offset": "0.15",
        }
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr=json.dumps(measurements),
        )


class BuildTests(unittest.TestCase):
    def make_project(self, root: Path) -> FakeProject:
        (root / "scenes").mkdir()
        (root / "scenes/a.py").write_text("class Visual: pass\n# a1\n", encoding="utf-8")
        (root / "scenes/b.py").write_text("class Visual: pass\n# b1\n", encoding="utf-8")
        (root / "style.py").write_text("BG = '#000'\n", encoding="utf-8")
        (root / "chalk_runtime.py").write_text("class ChalkScene: pass\n", encoding="utf-8")
        raw = {
            "settings": {
                "default_profile": "draft",
                "speech_wpm": 150,
                "lead_in_seconds": 0.6,
                "tail_seconds": 0.35,
            },
            "profiles": {
                "draft": {
                    "manim_quality": "-ql",
                    "width": 854,
                    "height": 480,
                    "fps": 15,
                    "video_crf": 25,
                },
                "review": {
                    "manim_quality": "-qm",
                    "width": 1280,
                    "height": 720,
                    "fps": 30,
                    "video_crf": 21,
                }
            },
        }
        return FakeProject(
            root,
            FakeConfig(root, raw),
            [
                FakeSegment("a", 0, Path("scenes/a.py"), "alpha narration"),
                FakeSegment("b", 1, Path("scenes/b.py"), "beta narration"),
            ],
        )

    def test_configured_media_executables_enter_environment_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = self.make_project(Path(temporary))
            project.config.raw["tools"] = {
                "manim": "/opt/video/manim",
                "ffmpeg": "/opt/video/ffmpeg",
                "ffprobe": "/opt/video/ffprobe",
            }

            def identity(executable, _arguments):
                return {"executable": executable, "binary_sha256": "sha256:test"}

            with (
                patch(
                    "_chalk.build.default_environment",
                    return_value={"manim": {"package": "0.20.1"}},
                ),
                patch("_chalk.build._executable_identity", side_effect=identity),
            ):
                engine = BuildEngine(project)
            self.assertEqual(
                engine.environment["manim"]["command"]["executable"],
                "/opt/video/manim",
            )
            self.assertEqual(
                engine.environment["ffmpeg"]["executable"],
                "/opt/video/ffmpeg",
            )
            self.assertEqual(
                engine.environment["ffprobe"]["executable"],
                "/opt/video/ffprobe",
            )

    def test_scoped_change_builds_only_requested_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            executor = FakeExecutor()
            engine = BuildEngine(
                project,
                executor=executor,
                validator=accept_media,
                environment={
                    "python": "3.11",
                    "platform": "test",
                    "manim": "test",
                    "ffmpeg": "test",
                    "runtime": "test",
                },
                max_workers=2,
            )
            first = engine.build_full("draft")
            self.assertEqual(executor.counts, {"render": 2, "segment": 2, "full": 1})
            self.assertEqual(len(engine.trace(first.final)), 5)
            render_calls = [call for call in executor.calls if call[0][0] == "manim"]
            self.assertTrue(render_calls)
            for command, cwd, env in render_calls:
                self.assertEqual(command[command.index("-r") + 1], "854,480")
                self.assertEqual(cwd, root.resolve())
                self.assertIn("CHALK_TIMELINE_PATH", env)
            full_call = next(
                command
                for command, _, _ in executor.calls
                if "concat" in command
            )
            loudnorm = full_call[full_call.index("-af") + 1]
            self.assertIn("measured_I=-18.250000", loudnorm)
            self.assertIn("linear=true", loudnorm)
            full_record = engine.store.lookup_action(first.final.action_key)
            assert full_record is not None
            self.assertEqual(
                full_record.metadata["loudness"]["measurements"]["input_i"],
                "-18.250000",
            )

            (root / "scenes/a.py").write_text("class Visual: pass\n# a2\n", encoding="utf-8")
            rebuilt = engine.build_segment("a", "draft")
            self.assertFalse(rebuilt.cache_hit)
            self.assertEqual(executor.counts, {"render": 3, "segment": 3, "full": 1})

            # Segment B's exact action remains a verified hit and no unrelated
            # full-cut barrier ran for the scoped request.
            b = engine.build_segment("b", "draft")
            self.assertTrue(b.cache_hit)
            self.assertEqual(executor.counts, {"render": 3, "segment": 3, "full": 1})

    def test_reordering_invalidates_only_the_full_cut(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            executor = FakeExecutor()
            engine = BuildEngine(
                project,
                executor=executor,
                validator=accept_media,
                environment={
                    "python": "3.11",
                    "platform": "test",
                    "manim": "test",
                    "ffmpeg": "test",
                    "runtime": "test",
                },
                max_workers=2,
            )
            first = engine.build_full("draft")
            first_cut_keys = {
                artifact.segment_id: artifact.action_key
                for artifact in first.artifacts
            }
            project.segments[0].order = 1
            project.segments[1].order = 0
            second = engine.build_full("draft")
            second_cut_keys = {
                artifact.segment_id: artifact.action_key
                for artifact in second.artifacts
            }

            self.assertEqual(first_cut_keys, second_cut_keys)
            self.assertNotEqual(first.final.action_key, second.final.action_key)
            self.assertEqual(executor.counts, {"render": 2, "segment": 2, "full": 2})
            self.assertTrue(all(artifact.cache_hit for artifact in second.artifacts))

    def test_force_is_scoped_to_the_requested_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            executor = FakeExecutor()
            engine = BuildEngine(
                project,
                executor=executor,
                validator=accept_media,
                environment={
                    "python": "3.11",
                    "platform": "test",
                    "manim": "test",
                    "ffmpeg": "test",
                    "runtime": "test",
                },
            )
            engine.build_full("draft")
            forced_full = engine.build_full("draft", force=True)
            self.assertEqual(
                executor.counts,
                {"render": 2, "segment": 2, "full": 2},
            )
            assert forced_full.final is not None
            full_record = engine.store.lookup_action(forced_full.final.action_key)
            assert full_record is not None
            self.assertIn("force_nonce", full_record.inputs)

            forced_cut = engine.composite_segment("a", "draft", force=True)
            self.assertEqual(executor.counts["render"], 2)
            self.assertEqual(executor.counts["segment"], 3)
            cut_record = engine.store.lookup_action(forced_cut.action_key)
            assert cut_record is not None
            self.assertIn("force_nonce", cut_record.inputs)

            engine.render_segment("a", "draft", force=True)
            self.assertEqual(executor.counts["render"], 3)

    def test_tampered_materialized_cut_is_repaired_from_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            executor = FakeExecutor()
            engine = BuildEngine(
                project,
                executor=executor,
                validator=accept_media,
                environment={
                    "python": "3.11",
                    "platform": "test",
                    "manim": "test",
                    "ffmpeg": "test",
                    "runtime": "test",
                },
            )
            first = engine.build_segment("a")
            expected = first.path.read_bytes()
            first.path.write_bytes(b"tampered alias")
            second = engine.build_segment("a")
            self.assertTrue(second.cache_hit)
            self.assertEqual(second.path.read_bytes(), expected)
            self.assertEqual(hash_file(second.path), second.blob_id)
            self.assertEqual(executor.counts, {"render": 1, "segment": 1})

    def test_render_retries_if_sources_change_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)

            class MutatingExecutor(FakeExecutor):
                def run(self, command, *, cwd, expected_output, env=None):
                    if command[0] == "manim" and not self.counts["render"]:
                        (root / "scenes/a.py").write_text(
                            "class Visual: pass\n# changed during render\n",
                            encoding="utf-8",
                        )
                    return super().run(
                        command,
                        cwd=cwd,
                        expected_output=expected_output,
                        env=env,
                    )

            executor = MutatingExecutor()
            environment = {
                "python": "3.11",
                "platform": "test",
                "manim": "test",
                "ffmpeg": "test",
                "runtime": "test",
                "media_validator": {
                    "kind": "injected",
                    "type": (
                        f"{accept_media.__module__}."
                        f"{accept_media.__qualname__}"
                    ),
                },
            }
            engine = BuildEngine(
                project,
                executor=executor,
                validator=accept_media,
                environment=environment,
            )
            original_key = engine.desired_render_key("a", "draft")
            artifact = engine.render_segment("a", "draft")
            self.assertEqual(executor.counts["render"], 2)
            self.assertNotEqual(artifact.action_key, original_key)
            self.assertIsNone(engine.store.lookup_action(original_key))
            record = engine.store.lookup_action(artifact.action_key)
            assert record is not None
            self.assertEqual(record.inputs["scene"], hash_file(root / "scenes/a.py"))

    def test_ffprobe_validator_rejects_wrong_or_incomplete_media(self) -> None:
        requirements = MediaRequirements(
            kind="segment cut",
            width=854,
            height=480,
            fps=15,
            duration_us=2_000_000,
            require_audio=True,
        )
        payload = {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 854,
                    "height": 480,
                    "avg_frame_rate": "15/1",
                    "nb_read_frames": "30",
                    "duration": "2.0",
                },
                {"codec_type": "audio", "nb_read_frames": "94"},
            ],
            "format": {"duration": "2.0"},
        }

        def result(value: object) -> SimpleNamespace:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(value),
                stderr="",
            )

        validator = FFprobeValidator(runner=lambda *_, **__: result(payload))
        validator.validate(Path("cut.mp4"), requirements)

        missing_audio = dict(payload)
        missing_audio["streams"] = payload["streams"][:1]
        validator = FFprobeValidator(
            runner=lambda *_, **__: result(missing_audio)
        )
        with self.assertRaisesRegex(BuildError, "no decodable audio"):
            validator.validate(Path("cut.mp4"), requirements)

        wrong_dimensions = json.loads(json.dumps(payload))
        wrong_dimensions["streams"][0]["width"] = 1920
        validator = FFprobeValidator(
            runner=lambda *_, **__: result(wrong_dimensions)
        )
        with self.assertRaisesRegex(BuildError, "expected 854x480"):
            validator.validate(Path("cut.mp4"), requirements)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)

            def reject_media(*_: object) -> None:
                raise BuildError("probe rejected output")

            engine = BuildEngine(
                project,
                executor=FakeExecutor(),
                validator=reject_media,
                environment={
                    "python": "3.11",
                    "platform": "test",
                    "manim": "test",
                    "ffmpeg": "test",
                    "runtime": "test",
                },
            )
            key = engine.desired_render_key("a", "draft")
            with self.assertRaisesRegex(BuildError, "probe rejected"):
                engine.render_segment("a", "draft")
            self.assertIsNone(engine.store.lookup_action(key))

    def test_selected_transcript_changes_timeline_and_strict_mode_requires_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            executor = FakeExecutor()
            engine = BuildEngine(
                project,
                executor=executor,
                validator=accept_media,
                environment={
                    "python": "3.11",
                    "platform": "test",
                    "manim": "test",
                    "ffmpeg": "test",
                    "runtime": "test",
                },
            )
            estimated = engine.render_segment("a")

            def converter(source: Path, destination: Path) -> None:
                destination.write_bytes(b"fLaC" + source.read_bytes())

            def transcriber(path: Path, *_: object) -> dict[str, object]:
                final_end = 2.4 if b"two" in path.read_bytes() else 1.9
                return {
                    "words": [
                        {"word": "alpha", "start": 0.1, "end": 0.8},
                        {
                            "word": "narration",
                            "start": 0.8,
                            "end": final_end,
                        },
                    ]
                }

            audio = AudioStore(
                root,
                converter=converter,
                prober=lambda _: 2_000_000,
                transcriber=transcriber,
            )
            source = root / "take.wav"
            source.write_bytes(b"RIFF-one")
            take = audio.import_take("a", "alpha narration", source, select=True)
            with self.assertRaisesRegex(BuildError, "no current selected transcript"):
                engine.render_segment("a", draft=False)

            audio.transcribe_take(
                take,
                model="fake",
                revision=MODEL_REVISION,
                model_fingerprint="sha256:model",
            )
            recorded = engine.render_segment("a", draft=False)
            self.assertNotEqual(estimated.action_key, recorded.action_key)
            self.assertEqual(recorded.timing, "transcript")

            source.write_bytes(b"RIFF-two")
            retake = audio.import_take("a", "alpha narration", source, select=True)
            audio.transcribe_take(
                retake,
                model="fake",
                revision=MODEL_REVISION,
                model_fingerprint="sha256:model",
            )
            changed = engine.render_segment("a", draft=False)
            self.assertNotEqual(recorded.action_key, changed.action_key)

            project.segments[0].narration = "revised alpha narration"
            stale_cut = engine.build_segment("a", draft=True)
            stale_record = engine.store.lookup_action(stale_cut.action_key)
            assert stale_record is not None
            self.assertEqual(stale_cut.timing, "estimated")
            self.assertIn(
                "selected take is stale; using silence", stale_cut.warnings
            )
            self.assertEqual(stale_record.inputs["audio"]["kind"], "generated-silence")
            with self.assertRaisesRegex(BuildError, "selected take is stale"):
                engine.build_segment("a", draft=False)

    def test_project_artifacts_marks_only_impacted_lineage_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_project(root)
            executor = FakeExecutor()
            environment = {
                "python": "3.11",
                "platform": "test",
                "manim": "test",
                "ffmpeg": "test",
                "runtime": "test",
                "media_validator": {
                    "kind": "injected",
                    "type": (
                        f"{accept_media.__module__}."
                        f"{accept_media.__qualname__}"
                    ),
                },
            }
            with patch("_chalk.build.default_environment", return_value=environment):
                engine = BuildEngine(
                    project,
                    executor=executor,
                    validator=accept_media,
                    environment=environment,
                )
                engine.build_full("draft")
                before = project_artifacts(project)
                self.assertTrue(before["segments"]["a"]["render"]["current"])
                self.assertTrue(before["segments"]["a"]["cut"]["current"])
                self.assertTrue(before["segments"]["b"]["render"]["current"])
                self.assertTrue(before["fullCut"]["current"])

                engine.build_full("review")
                review = project_artifacts(project)
                self.assertEqual(review["segments"]["a"]["render"]["profile"], "review")
                self.assertTrue(review["segments"]["a"]["render"]["current"])
                self.assertTrue(review["segments"]["a"]["cut"]["current"])
                self.assertTrue(review["segments"]["b"]["render"]["current"])
                self.assertTrue(review["fullCut"]["current"])

                (root / "scenes/a.py").write_text(
                    "class Visual: pass\n# revised\n", encoding="utf-8"
                )
                after = project_artifacts(project)
            self.assertFalse(after["segments"]["a"]["render"]["current"])
            self.assertFalse(after["segments"]["a"]["cut"]["current"])
            self.assertTrue(after["segments"]["b"]["render"]["current"])
            self.assertTrue(after["segments"]["b"]["cut"]["current"])
            self.assertFalse(after["fullCut"]["current"])


class BuilderReleaseTests(unittest.TestCase):
    def make_project(self, root: Path) -> None:
        (root / "scenes").mkdir(parents=True)
        (root / "chalk.toml").write_text(
            f"""schema = 1
project_id = "release-project"
title = "Release project"
slug = "release-project"

[settings]
default_profile = "draft"
speech_wpm = 150
lead_in_seconds = 0.6
tail_seconds = 0.35

[voice]
model = "fake"
revision = "{MODEL_REVISION}"

[profiles.draft]
manim_quality = "-ql"
width = 854
height = 480
fps = 15
video_crf = 25

[profiles.final]
manim_quality = "-qh"
width = 1920
height = 1080
fps = 60
video_crf = 18
""",
            encoding="utf-8",
        )
        (root / "brief.md").write_text("# Brief\n", encoding="utf-8")
        (root / "outline.md").write_text("# Outline\n", encoding="utf-8")
        (root / "feedback.md").write_text(
            "# Feedback\n\n## Open\n\n## Resolved\n", encoding="utf-8"
        )
        (root / "script.md").write_text(
            """# Release

## Intro
<!-- chalk:segment intro -->

Hello release.
""",
            encoding="utf-8",
        )
        (root / "style.py").write_text("BG = '#000'\n", encoding="utf-8")
        (root / "chalk_runtime.py").write_text(
            "class ChalkScene: pass\n", encoding="utf-8"
        )
        (root / "scenes/intro.py").write_text(
            """class Visual:
    def construct(self):
        self.play_on("hello release", object())
""",
            encoding="utf-8",
        )

    def retain_audio(self, root: Path) -> None:
        def converter(source: Path, destination: Path) -> None:
            destination.write_bytes(b"fLaC" + source.read_bytes())

        audio = AudioStore(
            root,
            converter=converter,
            prober=lambda _: 1_500_000,
            transcriber=lambda *_: {
                "words": [
                    {"word": "hello", "start": 0.1, "end": 0.7},
                    {"word": "release", "start": 0.7, "end": 1.4},
                ]
            },
        )
        source = root / "voice.wav"
        source.write_bytes(b"RIFF-release")
        take = audio.import_take("intro", "Hello release.", source, select=True)
        audio.transcribe_take(
            take,
            model="fake",
            revision=MODEL_REVISION,
            model_fingerprint="sha256:model",
        )

    def test_builder_materializes_and_named_release_is_strict_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_project(root)
            self.retain_audio(root)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "chalk-tests@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Chalk Tests"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial project"],
                cwd=root,
                check=True,
            )
            counts: Counter[str] = Counter()

            def runner(
                command: list[str], *, cwd: Path, env: dict[str, str]
            ) -> SimpleNamespace:
                del cwd
                if any("print_format=json" in argument for argument in command):
                    return SimpleNamespace(
                        returncode=0,
                        stdout="",
                        stderr=json.dumps(
                            {
                                "input_i": "-18.25",
                                "input_tp": "-2.10",
                                "input_lra": "4.30",
                                "input_thresh": "-28.50",
                                "target_offset": "0.15",
                            }
                        ),
                    )
                if command[0] == "ffplay":
                    counts["play"] += 1
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[0] == "manim":
                    kind = "render"
                elif "concat" in command:
                    kind = "full"
                else:
                    kind = "cut"
                counts[kind] += 1
                output = Path(env["CHALK_OUTPUT_PATH"])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(f"deterministic-{kind}-{counts[kind]}".encode())
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            environment = {
                "python": "3.11",
                "platform": "test",
                "manim": "test",
                "ffmpeg": "test",
                "runtime": "test",
            }
            builder = Builder(
                root,
                runner=runner,
                validator=accept_media,
                environment=environment,
                jobs=2,
            )
            rendered = builder.render(segment_ids=["intro"], profile="draft")
            self.assertEqual(
                rendered[0].path,
                root.resolve() / "output" / "draft" / "renders" / "intro.mp4",
            )
            cuts = builder.composite(segment_ids=["intro"], profile="draft")
            self.assertEqual(
                cuts[0].path,
                root.resolve() / "output" / "draft" / "segments" / "intro.mp4",
            )
            final = builder.full("draft")
            self.assertEqual(
                final.path, root.resolve() / "output" / "draft" / "full.mp4"
            )
            self.assertTrue(final.to_dict()["current"])
            self.assertTrue(
                load_project(root).state.current_actions["full"].startswith("sha256:")
            )

            release = builder.release("v1", profile="final")
            manifest = root / release.manifest_path
            media = root / release.media_path
            self.assertTrue(manifest.is_file())
            self.assertTrue(media.is_file())
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertNotIn(str(root), manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["root_action"], release.artifact.action_key)
            root_action = next(
                action
                for action in payload["actions"]
                if action["key"] == payload["root_action"]
            )
            self.assertEqual(
                root_action["metadata"]["loudness"]["recipe"]["mode"],
                "two-pass-linear",
            )
            self.assertIn(
                "measured_I=", root_action["metadata"]["loudness"]["normalization_filter"]
            )
            snapshot = json.loads(
                (
                    root
                    / ".chalk"
                    / "snapshots"
                    / f"{release.snapshot}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                snapshot["components"]["current_actions"]["full"],
                release.artifact.action_key,
            )

            identical = builder.release("v1", profile="final")
            self.assertEqual(identical.artifact.action_key, release.artifact.action_key)
            self.assertEqual(identical.snapshot, release.snapshot)
            retained = media.read_bytes()
            retained_manifest = manifest.read_bytes()
            self.assertEqual(hash_file(media), payload["output_blob"])

            audio = AudioStore(root)
            take = audio.selected_take("intro")
            assert take is not None
            transcript_path = audio.transcript_path(take.audio_sha256)
            transcript_bytes = transcript_path.read_bytes()
            transcript_path.unlink()
            with self.assertRaisesRegex(BuildError, "transcript-missing"):
                builder.release("missing-transcript", profile="final")
            transcript_path.write_bytes(b"{")
            with self.assertRaisesRegex(BuildError, "transcript-invalid"):
                builder.release("invalid-transcript", profile="final")
            transcript_path.write_bytes(transcript_bytes)

            config_path = root / "chalk.toml"
            config_text = config_path.read_text(encoding="utf-8")
            config_path.write_text(
                config_text.replace('model = "fake"', 'model = "different"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BuildError, "transcript-config-stale"):
                builder.release("stale-transcript", profile="final")
            config_path.write_text(config_text, encoding="utf-8")

            (root / "scenes/intro.py").write_text(
                """class Visual:
    def construct(self):
        self.play_on("hello release", object())
        # changed visual
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BuildError, "already identifies different content"):
                builder.release("v1", profile="final")
            self.assertEqual(media.read_bytes(), retained)
            self.assertEqual(manifest.read_bytes(), retained_manifest)
            self.assertEqual(hash_file(media), payload["output_blob"])


if __name__ == "__main__":
    unittest.main()
