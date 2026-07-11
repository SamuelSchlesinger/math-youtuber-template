from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from _chalk.server import (
    MEDIA_SUFFIXES,
    ChalkRequestHandler,
    ProjectRoom,
    _is_loopback_host,
    _parse_range,
    compose_project_schema,
    make_server,
    serve,
)


@dataclass
class FakeConfig:
    title: str = "A Small Proof"
    slug: str = "small-proof"


@dataclass
class FakeState:
    selected_takes: dict[str, str] = field(default_factory=lambda: {"intro": "take-intro"})
    current_actions: dict[str, str] = field(
        default_factory=lambda: {
            "segment:intro:render": "sha256:render",
            "segment:intro:cut": "sha256:cut",
            "full": "sha256:full",
        }
    )


@dataclass
class FakeSegment:
    id: str = "intro"
    title: str = "Intro"
    order: int = 0
    source: Path = Path("scenes/intro.py")
    narration: str = "Here is the opening idea."
    narration_hash: str = "narration-hash"


@dataclass
class FakeProject:
    root: Path
    config: FakeConfig = field(default_factory=FakeConfig)
    state: FakeState = field(default_factory=FakeState)
    segments: tuple[FakeSegment, ...] = (FakeSegment(),)


class FakeProvider:
    def __init__(self, project: FakeProject) -> None:
        self.project = project
        self.note_values: list[dict[str, object]] = [
            {
                "id": "note-1",
                "text": "The title appears too early.",
                "resolved": False,
                "segment": "intro",
                "category": "timing",
                "severity": "bug",
                "timecode": "1.250s",
                "snapshot": "snapshot-1",
            }
        ]
        self.approved: set[str] = set()
        self.resolutions: list[tuple[str, str | None]] = []

    def load_project(self, project: object) -> FakeProject:
        del project
        return self.project

    def status(self, project: object) -> dict[str, object]:
        del project
        return {"next_action": "Review intro."}

    def notes(self, project: object) -> list[dict[str, object]]:
        del project
        return self.note_values

    def approval(self, project: object, segment_id: str) -> dict[str, object]:
        del project
        state = "current" if segment_id in self.approved else "missing"
        return {"state": state, "current": state == "current", "snapshot": None}

    def take(self, project: object, segment_id: str, take_id: str) -> dict[str, object]:
        del project
        return {
            "id": take_id,
            "segment_id": segment_id,
            "audio_sha256": "audio-hash",
            "narration_sha256": "narration-hash",
            "duration_us": 2_500_000,
            "metadata": {"label": "take 2"},
        }

    def take_path(self, project: FakeProject, segment_id: str, take_id: str) -> Path:
        del segment_id, take_id
        return project.root / "media/takes/intro.flac"

    def transcript_path(self, project: FakeProject, segment_id: str, take_id: str) -> Path:
        del segment_id, take_id
        return project.root / "transcripts/audio-hash.json"

    def build_state(self, project: FakeProject) -> dict[str, object]:
        return {
            "segments": {
                "intro": {
                    "render": {
                        "path": project.root / "output/proxy/intro-render.mp4",
                        "current": True,
                        "profile": "proxy",
                    },
                    "cut": {
                        "path": project.root / "output/proxy/intro.mp4",
                        "current": True,
                        "profile": "proxy",
                        "durationSeconds": 3.0,
                        "artifactId": "sha256:cut",
                    },
                }
            },
            "fullCut": {
                "path": project.root / "output/proxy/full.mp4",
                "current": True,
                "profile": "proxy",
                "durationSeconds": 3.0,
                "artifactId": "sha256:full",
            },
        }

    def add_note(self, project: object, payload: dict[str, object]) -> dict[str, object]:
        del project
        note = {
            "id": f"note-{len(self.note_values) + 1}",
            "text": payload["message"],
            "resolved": False,
            "segment": payload["segmentId"],
            "category": payload.get("category", "general"),
            "severity": payload.get("severity", "note"),
            "timecode": f"{float(payload.get('timeSeconds', 0)):.3f}s",
            "snapshot": "snapshot-2",
        }
        self.note_values.append(note)
        return note

    def resolve_note(
        self, project: object, note_id: str, resolution: str | None
    ) -> dict[str, object]:
        del project
        self.resolutions.append((note_id, resolution))
        for note in self.note_values:
            if note["id"] == note_id:
                note["resolved"] = True
                return note
        raise ValueError("unknown note")

    def approve(self, project: object, segment_id: str) -> dict[str, object]:
        del project
        self.approved.add(segment_id)
        return {"scope": f"segment:{segment_id}", "state": "current"}


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / "scenes").mkdir()
        (root / "scenes/intro.py").write_text("class Visual: pass\n", encoding="utf-8")
        (root / "media/takes").mkdir(parents=True)
        (root / "media/takes/intro.flac").write_bytes(b"fLaC take")
        (root / "transcripts").mkdir()
        (root / "transcripts/audio-hash.json").write_text(
            json.dumps({"audio_sha256": "audio-hash", "words": []}), encoding="utf-8"
        )
        (root / "output/proxy").mkdir(parents=True)
        (root / "output/proxy/intro-render.mp4").write_bytes(b"render")
        (root / "output/proxy/intro.mp4").write_bytes(b"0123456789")
        (root / "output/proxy/full.mp4").write_bytes(b"abcdefghij")
        (root / "secret.txt").write_text("not media", encoding="utf-8")
        self.project = FakeProject(root)
        self.provider = FakeProvider(self.project)
        patcher = patch("_chalk.server.LocalProjectData", return_value=self.provider)
        patcher.start()
        self.addCleanup(patcher.stop)
        try:
            self.handle = serve(self.project, port=0, open_browser=False)
        except PermissionError:
            self.skipTest("loopback bind denied by sandbox")
        self.addCleanup(self.handle.shutdown)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, path: str, *, method: str = "GET", value: object | None = None):
        data = None if value is None else json.dumps(value).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        return urlopen(Request(self.handle.url + path.lstrip("/"), data=data, headers=headers, method=method))

    def test_project_schema_matches_frontend_contract(self) -> None:
        schema = compose_project_schema(self.project, self.provider)
        self.assertEqual(
            set(schema),
            {"title", "nextAction", "readinessPercent", "openNoteCount", "segments", "fullCut"},
        )
        self.assertEqual(schema["title"], "A Small Proof")
        self.assertEqual(schema["openNoteCount"], 1)
        self.assertEqual(schema["readinessPercent"], 100)
        segment = schema["segments"][0]
        for key in (
            "narrationHash",
            "scene",
            "take",
            "transcript",
            "render",
            "cut",
            "approval",
            "openNotes",
        ):
            self.assertIn(key, segment)
        self.assertEqual(segment["cut"]["url"], "/media/output/proxy/intro.mp4")
        self.assertTrue(segment["transcript"]["current"])
        self.assertEqual(segment["openNotes"][0]["message"], "The title appears too early.")

        with self.request("/api/project") as response:
            payload = json.load(response)
            self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(payload["fullCut"]["url"], "/media/output/proxy/full.mp4")

    def test_review_actions_call_provider_and_refresh_state(self) -> None:
        with self.request(
            "/api/notes",
            method="POST",
            value={
                "segmentId": "intro",
                "message": "Hold the equation longer.",
                "category": "timing",
                "severity": "note",
                "timeSeconds": 2.5,
                "artifactId": "sha256:cut",
            },
        ) as response:
            self.assertTrue(json.load(response)["ok"])
        self.assertEqual(len(self.provider.note_values), 2)

        with self.request(
            "/api/notes/note-2/resolve",
            method="POST",
            value={"resolution": "Checked the revised cut."},
        ) as response:
            self.assertTrue(json.load(response)["ok"])
        self.assertEqual(
            self.provider.resolutions,
            [("note-2", "Checked the revised cut.")],
        )

        with self.request(
            "/api/segments/intro/approve", method="POST", value={}
        ) as response:
            self.assertTrue(json.load(response)["ok"])
        with self.request("/api/project") as response:
            segment = json.load(response)["segments"][0]
        self.assertTrue(segment["approval"]["current"])

    def test_static_media_range_and_traversal_protection(self) -> None:
        with self.request("/") as response:
            self.assertIn(b"Chalk project room", response.read())
            self.assertEqual(response.headers.get_content_type(), "text/html")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")

        request = Request(self.handle.url + "media/output/proxy/intro.mp4")
        request.add_header("Range", "bytes=2-5")
        with urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
            self.assertEqual(response.headers.get_content_type(), "video/mp4")
            self.assertEqual(response.read(), b"2345")

        for path in ("media/%2e%2e/secret.txt", "media/output/%2e%2e/%2e%2e/secret.txt"):
            with self.assertRaises(HTTPError) as raised:
                self.request(path)
            self.assertIn(raised.exception.code, {403, 404})

    def test_rejects_oversized_json_without_reading_it(self) -> None:
        request = Request(
            self.handle.url + "api/notes",
            data=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": str(70_000)},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 413)


class SchemaOnlyTests(unittest.TestCase):
    def test_schema_is_available_without_binding_a_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scenes").mkdir()
            (root / "scenes/intro.py").write_text("class Visual: pass\n", encoding="utf-8")
            (root / "media/takes").mkdir(parents=True)
            (root / "media/takes/intro.flac").write_bytes(b"fLaC")
            (root / "transcripts").mkdir()
            (root / "transcripts/audio-hash.json").write_text(
                json.dumps({"audio_sha256": "audio-hash"}), encoding="utf-8"
            )
            (root / "output/proxy").mkdir(parents=True)
            for name in ("intro-render.mp4", "intro.mp4", "full.mp4"):
                (root / "output/proxy" / name).write_bytes(b"media")
            project = FakeProject(root)
            schema = compose_project_schema(project, FakeProvider(project))
            self.assertEqual(schema["title"], "A Small Proof")
            self.assertEqual(schema["segments"][0]["id"], "intro")
            self.assertTrue(schema["segments"][0]["cut"]["current"])
            self.assertEqual(schema["openNoteCount"], 1)

    def test_range_parser_covers_open_suffix_and_invalid_ranges(self) -> None:
        self.assertIsNone(_parse_range(None, 10))
        self.assertEqual(_parse_range("bytes=2-5", 10), (2, 5))
        self.assertEqual(_parse_range("bytes=7-", 10), (7, 9))
        self.assertEqual(_parse_range("bytes=-4", 10), (6, 9))
        self.assertEqual(_parse_range("bytes=-40", 10), (0, 9))
        self.assertIs(_parse_range("bytes=10-12", 10), False)
        self.assertIs(_parse_range("bytes=5-2", 10), False)
        self.assertIs(_parse_range("bytes=0-1,4-5", 10), False)

    def test_media_resolver_rejects_traversal_and_symlink_escape_without_socket(self) -> None:
        class ProbeHandler(ChalkRequestHandler):
            def _error(self, status, message):
                self.observed = (int(status), message)

            def _serve_file(self, path, content_type, *, head, cache_control):
                self.observed = (path, content_type, head, cache_control)

        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary).resolve()
            (root / "output").mkdir()
            media = root / "output/cut.mp4"
            media.write_bytes(b"media")
            secret = Path(outside) / "secret.mp4"
            secret.write_bytes(b"secret")
            handler = object.__new__(ProbeHandler)
            handler.server = SimpleNamespace(room=SimpleNamespace(root=root))

            handler._serve_project_media("output/cut.mp4", head=False)
            self.assertEqual(handler.observed[0], media)
            self.assertEqual(handler.observed[1], "video/mp4")

            handler._serve_project_media("%2e%2e/secret.mp4", head=False)
            self.assertEqual(handler.observed[0], 403)

            link = root / "output/escape.mp4"
            try:
                link.symlink_to(secret)
            except OSError:
                return
            handler._serve_project_media("output/escape.mp4", head=False)
            self.assertEqual(handler.observed[0], 403)

            # A symlink under a media root that resolves to a media-suffixed file
            # elsewhere *inside* the project (not a media root) must also be
            # refused: relative_to(root) alone would allow it.
            (root / "scenes").mkdir()
            inside = root / "scenes/diagram.mp4"
            inside.write_bytes(b"not under a media root")
            sneak = root / "output/sneak.mp4"
            try:
                sneak.symlink_to(inside)
            except OSError:
                return
            handler._serve_project_media("output/sneak.mp4", head=False)
            self.assertEqual(handler.observed[0], 403)

    def test_local_host_boundary_and_active_media_types(self) -> None:
        for value in ("127.0.0.1", "127.0.0.1:8042", "localhost", "[::1]:443"):
            self.assertTrue(_is_loopback_host(value))
        for value in (None, "", "example.test", "example.test:8042", "127.0.0.1:99999"):
            self.assertFalse(_is_loopback_host(value))
        self.assertNotIn(".svg", MEDIA_SUFFIXES)
        with self.assertRaisesRegex(ValueError, "loopback"):
            make_server(SimpleNamespace(root=Path.cwd()), host="0.0.0.0")

    def test_readiness_marks_stale_takes_and_empty_scripts_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scenes").mkdir()
            (root / "scenes/intro.py").write_text("class Visual: pass\n", encoding="utf-8")
            (root / "media/takes").mkdir(parents=True)
            (root / "media/takes/intro.flac").write_bytes(b"fLaC")
            (root / "transcripts").mkdir()
            (root / "transcripts/audio-hash.json").write_text(
                json.dumps({"audio_sha256": "audio-hash"}), encoding="utf-8"
            )
            (root / "output/proxy").mkdir(parents=True)
            for name in ("intro-render.mp4", "intro.mp4", "full.mp4"):
                (root / "output/proxy" / name).write_bytes(b"media")

            stale_project = FakeProject(
                root,
                segments=(FakeSegment(narration_hash="new-narration-hash"),),
            )
            stale = compose_project_schema(stale_project, FakeProvider(stale_project))
            self.assertFalse(stale["segments"][0]["take"]["current"])
            self.assertEqual(stale["readinessPercent"], 83)

            empty_project = FakeProject(
                root,
                segments=(FakeSegment(narration="", narration_hash="narration-hash"),),
            )
            empty = compose_project_schema(empty_project, FakeProvider(empty_project))
            self.assertEqual(empty["segments"][0]["wordCount"], 0)
            self.assertEqual(empty["readinessPercent"], 83)

    def test_full_cut_notes_are_project_scoped_and_exposed_with_the_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scenes").mkdir()
            (root / "scenes/intro.py").write_text(
                "class Visual: pass\n", encoding="utf-8"
            )
            project = FakeProject(root)
            provider = FakeProvider(project)
            provider.note_values.append(
                {
                    "id": "note-full",
                    "text": "The transition breaks the argument.",
                    "resolved": False,
                    "segment": None,
                    "category": "transition",
                    "severity": "bug",
                    "timecode": "8.500s",
                    "snapshot": "snapshot-full",
                }
            )
            schema = compose_project_schema(project, provider)
            self.assertEqual(
                schema["fullCut"]["openNotes"][0]["id"], "note-full"
            )

            room = ProjectRoom(project, provider)
            room.add_note(
                {
                    "segmentId": None,
                    "scope": "project",
                    "message": "The ending lands too abruptly.",
                    "category": "transition",
                    "severity": "bug",
                    "timeSeconds": 12.25,
                }
            )
            self.assertIsNone(provider.note_values[-1]["segment"])


if __name__ == "__main__":
    unittest.main()
