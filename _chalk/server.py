"""Zero-build local project room for reviewing Chalk artifacts.

The server is intentionally a view over project files.  It serves three bundled
UI assets, read-only project media, and three explicit review mutations.  It
never evaluates source files or exposes a command-execution endpoint.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import inspect
import json
import mimetypes
from pathlib import Path
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import quote, unquote, urlsplit
import webbrowser


MAX_REQUEST_BODY = 64 * 1024
UI_ROOT = Path(__file__).with_name("ui")
UI_ASSETS = {
    "/": "index.html",
    "/index.html": "index.html",
    "/assets/app.js": "app.js",
    "/assets/styles.css": "styles.css",
}
MEDIA_SUFFIXES = {
    ".aac",
    ".aif",
    ".aiff",
    ".flac",
    ".gif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".png",
    ".wav",
    ".webm",
}
MEDIA_ROOTS = {"assets", "media", "output", "releases"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class ServerError(RuntimeError):
    """The project room could not read or update its project."""


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _record(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    for method_name in ("to_dict", "to_record"):
        method = getattr(value, method_name, None)
        if callable(method):
            result = method()
            if isinstance(result, Mapping):
                return result
    if is_dataclass(value):
        result = asdict(value)
        return result if isinstance(result, Mapping) else {}
    return {}


def _call(provider: Any, name: str, *arguments: Any, default: Any = None, **keywords: Any) -> Any:
    function = getattr(provider, name, None)
    if not callable(function):
        return default
    return function(*arguments, **keywords)


def _seconds_from_timecode(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value).strip().removesuffix("s")
    if not text:
        return None
    try:
        parts = [float(part) for part in text.split(":")]
    except ValueError:
        return None
    if len(parts) == 1:
        return max(0.0, parts[0])
    if len(parts) == 2:
        return max(0.0, parts[0] * 60 + parts[1])
    if len(parts) == 3:
        return max(0.0, parts[0] * 3600 + parts[1] * 60 + parts[2])
    return None


def _timecode_from_seconds(value: Any) -> str | None:
    if value is None:
        return None
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        raise ValueError("timeSeconds must be a non-negative number") from None
    return f"{seconds:.3f}s"


def _relative_media_url(root: Path, path: Any) -> str | None:
    if not path:
        return None
    candidate = Path(str(path))
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        relative = resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if not resolved.is_file() or resolved.suffix.lower() not in MEDIA_SUFFIXES:
        return None
    return "/media/" + quote(relative.as_posix(), safe="/")


def _artifact(
    root: Path,
    value: Any,
    *,
    action_id: str | None = None,
    default_profile: str | None = None,
) -> dict[str, Any]:
    data = dict(_record(value))
    path = data.get("path") or data.get("outputPath") or data.get("output_path")
    url = data.get("url") or _relative_media_url(root, path)
    exists = bool(data.get("exists", url is not None))
    current = bool(data.get("current", exists and bool(action_id or data.get("artifactId"))))
    duration = data.get("durationSeconds", data.get("duration_seconds"))
    if duration is None and data.get("duration_us") is not None:
        duration = int(data["duration_us"]) / 1_000_000
    try:
        duration_seconds = max(0.0, float(duration or 0))
    except (TypeError, ValueError):
        duration_seconds = 0.0
    artifact_id = (
        data.get("artifactId")
        or data.get("artifact_id")
        or data.get("actionKey")
        or data.get("action_key")
        or action_id
    )
    return {
        "exists": exists,
        "current": current,
        "url": url,
        "path": str(path) if path else None,
        "durationSeconds": duration_seconds,
        "profile": data.get("profile", default_profile),
        "timing": data.get("timing"),
        "artifactId": artifact_id,
        "hash": data.get("hash") or data.get("sha256") or data.get("blobId"),
    }


def _note_schema(note: Any) -> dict[str, Any]:
    data = _record(note)
    artifact = data.get("artifact") if isinstance(data.get("artifact"), Mapping) else {}
    return {
        "id": str(data.get("id", "")),
        "message": str(data.get("message", data.get("text", ""))),
        "category": str(data.get("category", "general")),
        "severity": str(data.get("severity", "note")),
        "timeSeconds": _seconds_from_timecode(data.get("timeSeconds", data.get("timecode"))),
        "artifactId": data.get("artifactId") or artifact.get("sha256") or artifact.get("ref"),
        "segmentId": data.get("segmentId", data.get("segment")),
        "snapshot": data.get("snapshot"),
    }


class LocalProjectData:
    """Delayed adapter over model, audio, review, and optional build modules."""

    def load_project(self, project: Any) -> Any:
        from .model import load_project

        root = _value(project, "root", project)
        return load_project(root)

    def status(self, project: Any) -> Any:
        from .model import project_status

        return project_status(project)

    def notes(self, project: Any) -> list[Any]:
        from .review import list_notes

        return list_notes(project)

    def approval(self, project: Any, segment_id: str) -> Any:
        from .review import approval_status

        return approval_status(project, segment_id)

    def take(self, project: Any, segment_id: str, take_id: str) -> Any:
        del segment_id
        from .model import take_record

        return take_record(project, take_id)

    def take_path(self, project: Any, segment_id: str, take_id: str) -> Path | None:
        from .model import take_path

        return take_path(project, segment_id, take_id)

    def transcript_path(self, project: Any, segment_id: str, take_id: str) -> Path | None:
        from .model import transcript_path

        return transcript_path(project, segment_id, take_id)

    def build_state(self, project: Any) -> Mapping[str, Any]:
        # Build remains optional while a project is being authored.  If its
        # adapter exists, prefer it; otherwise discover only conventional aliases.
        try:
            from . import build

            function = getattr(build, "project_artifacts", None)
            if callable(function):
                value = function(project)
                if isinstance(value, Mapping):
                    return value
        except (ImportError, AttributeError, OSError, ValueError):
            pass
        return _discover_build_state(project)

    def add_note(self, project: Any, payload: Mapping[str, Any]) -> Any:
        from .review import append_note

        return append_note(
            project,
            str(payload["message"]),
            segment=(
                str(payload["segmentId"])
                if isinstance(payload.get("segmentId"), str)
                else None
            ),
            category=str(payload.get("category", "general")),
            severity=str(payload.get("severity", "note")),
            timecode=_timecode_from_seconds(payload.get("timeSeconds")),
            artifact=payload.get("artifactId"),
        )

    def resolve_note(self, project: Any, note_id: str, resolution: str | None) -> Any:
        from .review import resolve_note

        # A future review backend may persist resolution text.  Pass it only
        # when supported without making the HTTP layer depend on that revision.
        parameters = inspect.signature(resolve_note).parameters
        if "resolution" in parameters:
            return resolve_note(project, note_id, resolution=resolution)
        return resolve_note(project, note_id)

    def approve(self, project: Any, segment_id: str) -> Any:
        from .review import approve

        return approve(project, segment_id)


def _action_ref(actions: Mapping[str, Any], segment_id: str, kind: str) -> str | None:
    for key in (
        f"segment:{segment_id}:{kind}",
        f"{segment_id}:{kind}",
        f"{kind}:{segment_id}",
    ):
        value = actions.get(key)
        if isinstance(value, str):
            return value
    return None


def _first_file(candidates: list[Path]) -> Path | None:
    return next((path for path in candidates if path.is_file()), None)


def _discover_build_state(project: Any) -> Mapping[str, Any]:
    root = Path(_value(project, "root")).resolve()
    state = _value(project, "state", {})
    actions = _value(state, "current_actions", {})
    if not isinstance(actions, Mapping):
        actions = {}
    segments: dict[str, Any] = {}
    output = root / "output"
    for segment in _value(project, "segments", ()):
        segment_id = str(_value(segment, "id", ""))
        render = _first_file(
            sorted(output.glob(f"*/renders/{segment_id}.mp4"))
            + [output / "renders" / f"{segment_id}.mp4"]
        )
        cut = _first_file(
            sorted(output.glob(f"*/segments/{segment_id}.mp4"))
            + [output / "segments" / f"{segment_id}.mp4", output / f"{segment_id}.mp4"]
        )
        segments[segment_id] = {
            "render": {"path": render, "current": bool(render and _action_ref(actions, segment_id, "render"))},
            "cut": {"path": cut, "current": bool(cut and _action_ref(actions, segment_id, "cut"))},
        }
    full = _first_file(
        sorted(output.glob("*/full.mp4"))
        + [output / "full.mp4", output / f"{_value(_value(project, 'config', {}), 'slug', 'final')}.mp4"]
    )
    full_ref = next(
        (actions[key] for key in ("full", "project:full", "full:cut") if isinstance(actions.get(key), str)),
        None,
    )
    return {"segments": segments, "fullCut": {"path": full, "current": bool(full and full_ref), "artifactId": full_ref}}


def compose_project_schema(project: Any, data_provider: Any | None = None) -> dict[str, Any]:
    """Compose the exact frontend read model from duck-typed project providers."""

    provider = data_provider or LocalProjectData()
    root = Path(_value(project, "root", Path.cwd())).resolve()
    status = _call(provider, "status", project, default={})
    status_data = _record(status)
    status_segments_value = status_data.get("segments", ())
    status_segments = {
        str(_value(item, "id", "")): _record(item)
        for item in status_segments_value
        if isinstance(status_segments_value, (list, tuple))
    }
    notes = list(_call(provider, "notes", project, default=[]) or [])
    open_notes = [_note_schema(note) for note in notes if not bool(_value(note, "resolved", False))]
    notes_by_segment: dict[str, list[dict[str, Any]]] = {}
    project_notes: list[dict[str, Any]] = []
    for note in open_notes:
        segment_id = note.get("segmentId")
        if isinstance(segment_id, str):
            notes_by_segment.setdefault(segment_id, []).append(note)
        else:
            project_notes.append(note)

    build_state = _call(provider, "build_state", project, default={}) or {}
    build_segments = _value(build_state, "segments", {})
    if not isinstance(build_segments, Mapping):
        build_segments = {}
    project_state = _value(project, "state", {})
    selected_takes = _value(project_state, "selected_takes", {})
    if not isinstance(selected_takes, Mapping):
        selected_takes = {}
    current_actions = _value(project_state, "current_actions", {})
    if not isinstance(current_actions, Mapping):
        current_actions = {}

    segments: list[dict[str, Any]] = []
    completed_stages = 0
    total_stages = 0
    for segment in _value(project, "segments", ()):
        segment_id = str(_value(segment, "id", ""))
        title = str(_value(segment, "title", segment_id))
        narration = str(_value(segment, "narration", ""))
        narration_hash = str(_value(segment, "narration_hash", "")) or None

        scene_path_value = _value(segment, "source", None)
        scene_path = Path(scene_path_value) if scene_path_value is not None else Path("scenes") / f"{segment_id}.py"
        if not scene_path.is_absolute():
            scene_path = root / scene_path
        scene_exists = scene_path.is_file()
        scene_hash = None
        if scene_exists:
            try:
                from .model import hash_file

                scene_hash = hash_file(scene_path)
            except (ImportError, OSError):
                scene_hash = None
        scene_status = status_segments.get(segment_id, {}).get("scene")
        scene_ready = scene_exists and (
            scene_status is None or scene_status == "ready"
        )

        take_id = selected_takes.get(segment_id)
        take = _call(provider, "take", project, segment_id, take_id, default=None) if isinstance(take_id, str) else None
        take_data = _record(take)
        take_file = (
            _call(provider, "take_path", project, segment_id, take_id, default=None)
            if isinstance(take_id, str)
            else None
        )
        audio_hash = take_data.get("audio_sha256", take_data.get("audioHash"))
        take_current = bool(
            take
            and (take_file is None or Path(take_file).is_file())
            and take_data.get("narration_sha256", take_data.get("narrationHash")) == narration_hash
        )
        duration_us = take_data.get("duration_us", take_data.get("durationUs", 0))
        try:
            take_duration = max(0.0, int(duration_us) / 1_000_000)
        except (TypeError, ValueError):
            take_duration = 0.0

        transcript_file = (
            _call(provider, "transcript_path", project, segment_id, take_id, default=None)
            if isinstance(take_id, str)
            else None
        )
        transcript_data: Mapping[str, Any] = {}
        transcript_hash = None
        if transcript_file and Path(transcript_file).is_file():
            try:
                transcript_data = json.loads(Path(transcript_file).read_text(encoding="utf-8"))
                from .model import hash_file

                transcript_hash = hash_file(Path(transcript_file))
            except (ImportError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                transcript_data = {}
        transcript_exists = bool(transcript_file and Path(transcript_file).is_file())
        transcript_audio = transcript_data.get("audio_sha256", transcript_data.get("audioHash"))
        transcript_current = bool(transcript_exists and audio_hash and transcript_audio == audio_hash)
        transcript_status = status_segments.get(segment_id, {}).get("transcript")
        if transcript_status is not None:
            transcript_current = transcript_current and transcript_status == "current"

        segment_build = build_segments.get(segment_id, {})
        render_ref = _action_ref(current_actions, segment_id, "render")
        cut_ref = _action_ref(current_actions, segment_id, "cut")
        render = _artifact(root, _value(segment_build, "render", {}), action_id=render_ref)
        cut = _artifact(root, _value(segment_build, "cut", {}), action_id=cut_ref)
        approval = _call(provider, "approval", project, segment_id, default={})
        approval_data = _record(approval)
        approval_state = str(approval_data.get("state", "missing"))

        stages = (
            bool(narration.strip()),
            scene_ready,
            take_current,
            transcript_current,
            render["current"],
            cut["current"],
        )
        completed_stages += sum(stages)
        total_stages += len(stages)
        segments.append(
            {
                "id": segment_id,
                "title": title,
                "order": int(_value(segment, "order", len(segments))),
                "narration": narration,
                "narrationHash": narration_hash,
                "wordCount": len(narration.split()),
                "scene": {
                    "exists": scene_exists,
                    "valid": scene_ready,
                    "state": scene_status or ("ready" if scene_exists else "missing"),
                    "path": scene_path.relative_to(root).as_posix() if scene_path.is_relative_to(root) else None,
                    "hash": scene_hash,
                },
                "take": {
                    "selected": bool(take_id),
                    "id": take_id,
                    "label": take_data.get("metadata", {}).get("label") if isinstance(take_data.get("metadata"), Mapping) else None,
                    "audioHash": audio_hash,
                    "durationSeconds": take_duration,
                    "current": take_current,
                },
                "transcript": {
                    "exists": transcript_exists,
                    "current": transcript_current,
                    "hash": transcript_hash,
                },
                "render": render,
                "cut": cut,
                "approval": {
                    "exists": approval_state != "missing",
                    "current": bool(approval_data.get("current", approval_state == "current")),
                    "state": approval_state,
                    "snapshot": approval_data.get("snapshot"),
                },
                "openNotes": notes_by_segment.get(segment_id, []),
            }
        )

    full_ref = next(
        (current_actions[key] for key in ("full", "project:full", "full:cut") if isinstance(current_actions.get(key), str)),
        None,
    )
    full_cut = _artifact(root, _value(build_state, "fullCut", _value(build_state, "full_cut", {})), action_id=full_ref)
    full_cut["openNotes"] = project_notes
    readiness = round(100 * completed_stages / total_stages) if total_stages else 0
    title = _value(_value(project, "config", {}), "title", status_data.get("title", "Untitled video"))
    next_action = status_data.get("next_action", status_data.get("nextAction", "Review the next changed segment."))
    return {
        "title": str(title),
        "nextAction": str(next_action),
        "readinessPercent": readiness,
        "openNoteCount": len(open_notes),
        "segments": segments,
        "fullCut": full_cut,
    }


class ProjectRoom:
    """Reloading project/data facade shared by HTTP request threads."""

    def __init__(self, project: Any, data_provider: Any | None = None) -> None:
        self.source = project
        self.provider = data_provider or LocalProjectData()
        root_value = _value(project, "root", project)
        self.root = Path(root_value).expanduser().resolve()
        self._mutation_lock = threading.Lock()

    def project(self) -> Any:
        loaded = _call(self.provider, "load_project", self.source, default=None)
        return loaded if loaded is not None else self.source

    def schema(self) -> dict[str, Any]:
        return compose_project_schema(self.project(), self.provider)

    def add_note(self, payload: Mapping[str, Any]) -> Any:
        message = payload.get("message")
        segment_id = payload.get("segmentId")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if segment_id is not None:
            _validate_id(segment_id, "segmentId")
        category = payload.get("category", "general")
        severity = payload.get("severity", "note")
        if not isinstance(category, str) or not category or len(category) > 40:
            raise ValueError("category must be a short string")
        if not isinstance(severity, str) or not severity or len(severity) > 40:
            raise ValueError("severity must be a short string")
        if len(message) > 20_000:
            raise ValueError("message is too long")
        with self._mutation_lock:
            result = _call(self.provider, "add_note", self.project(), payload, default=None)
        if result is None:
            raise ServerError("review provider does not support adding notes")
        return result

    def resolve_note(self, note_id: str, payload: Mapping[str, Any]) -> Any:
        _validate_id(note_id, "note id")
        resolution = payload.get("resolution")
        if resolution is not None and (not isinstance(resolution, str) or len(resolution) > 20_000):
            raise ValueError("resolution must be a short string")
        with self._mutation_lock:
            result = _call(
                self.provider,
                "resolve_note",
                self.project(),
                note_id,
                resolution,
                default=None,
            )
        if result is None:
            raise ServerError("review provider does not support resolving notes")
        return result

    def approve(self, segment_id: str) -> Any:
        _validate_id(segment_id, "segment id")
        with self._mutation_lock:
            result = _call(self.provider, "approve", self.project(), segment_id, default=None)
        if result is None:
            raise ServerError("review provider does not support approvals")
        return result


def _validate_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _is_loopback_host(value: str | None) -> bool:
    """Reject DNS-rebinding Host headers before exposing local mutations."""

    if not value:
        return False
    match = re.fullmatch(
        r"(?:127\.0\.0\.1|localhost|\[::1\])(?::([0-9]{1,5}))?",
        value.strip().lower(),
    )
    if match is None:
        return False
    return match.group(1) is None or 0 < int(match.group(1)) <= 65_535


class ChalkHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], room: ProjectRoom) -> None:
        self.room = room
        super().__init__(address, ChalkRequestHandler)


class ChalkRequestHandler(BaseHTTPRequestHandler):
    server: ChalkHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._accept_local_host():
            return
        self._dispatch(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._accept_local_host():
            return
        self._dispatch(head=True)

    def do_POST(self) -> None:  # noqa: N802
        if not self._accept_local_host():
            return
        path = urlsplit(self.path).path
        if path == "/api/notes":
            self._post_json(lambda payload: self.server.room.add_note(payload))
            return

        match = re.fullmatch(r"/api/notes/([^/]+)/resolve", path)
        if match:
            note_id = unquote(match.group(1))
            self._post_json(lambda payload: self.server.room.resolve_note(note_id, payload))
            return

        match = re.fullmatch(r"/api/segments/([^/]+)/approve", path)
        if match:
            segment_id = unquote(match.group(1))
            self._post_json(lambda payload: self.server.room.approve(segment_id))
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_OPTIONS(self) -> None:  # noqa: N802
        # No CORS opt-in: another web origin must not mutate the local project.
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")

    def _accept_local_host(self) -> bool:
        if _is_loopback_host(self.headers.get("Host")):
            return True
        self._error(HTTPStatus.FORBIDDEN, "invalid Host header")
        return False

    def _dispatch(self, *, head: bool) -> None:
        path = urlsplit(self.path).path
        if path == "/api/project":
            if head:
                self._send_json({}, head=True)
                return
            try:
                self._send_json(self.server.room.schema())
            except Exception as error:  # API boundary: never expose a traceback
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"could not read project: {error}")
            return
        if path in UI_ASSETS:
            self._serve_ui(UI_ASSETS[path], head=head)
            return
        if path.startswith("/media/"):
            self._serve_project_media(path.removeprefix("/media/"), head=head)
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def _post_json(self, operation: Any) -> None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
            return
        if self.headers.get("Transfer-Encoding"):
            self._error(HTTPStatus.BAD_REQUEST, "streamed request bodies are not supported")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            return
        if length < 0:
            self._error(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            return
        if length > MAX_REQUEST_BODY:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
            return
        try:
            body = self.rfile.read(length)
            payload = json.loads(body or b"{}")
            if not isinstance(payload, Mapping):
                raise ValueError("JSON body must be an object")
            result = operation(payload)
            self._send_json({"ok": True, "result": _record(result)})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, KeyError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            # ReviewError/ModelError are intentionally delayed imports, so map
            # their human messages here without importing their modules eagerly.
            if error.__class__.__name__ in {"ReviewError", "ModelError"}:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
            else:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "project update failed")

    def _serve_ui(self, name: str, *, head: bool) -> None:
        path = UI_ROOT / name
        if not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "UI asset not found")
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        self._serve_file(path, content_type, head=head, cache_control="no-cache")

    def _serve_project_media(self, encoded_path: str, *, head: bool) -> None:
        try:
            decoded = unquote(encoded_path, errors="strict")
        except UnicodeDecodeError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid media path")
            return
        relative = Path(decoded)
        if (
            not decoded
            or "\x00" in decoded
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or not relative.parts
            or relative.parts[0] not in MEDIA_ROOTS
        ):
            self._error(HTTPStatus.FORBIDDEN, "invalid media path")
            return
        root = self.server.room.root
        try:
            path = (root / relative).resolve()
            path.relative_to(root)
        except (OSError, ValueError):
            self._error(HTTPStatus.FORBIDDEN, "media path escapes project")
            return
        if not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES:
            self._error(HTTPStatus.NOT_FOUND, "media not found")
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._serve_file(path, content_type, head=head, cache_control="private, no-cache")

    def _serve_file(
        self,
        path: Path,
        content_type: str,
        *,
        head: bool,
        cache_control: str,
    ) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "file not found")
            return
        parsed_range = _parse_range(self.headers.get("Range"), size)
        if parsed_range is False:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            return
        if parsed_range is None:
            start, end = 0, max(0, size - 1)
            status = HTTPStatus.OK
        else:
            start, end = parsed_range
            status = HTTPStatus.PARTIAL_CONTENT
        length = 0 if size == 0 else end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; object-src 'none'; "
                "frame-ancestors 'none'; form-action 'self'",
            )
            self.send_header("X-Frame-Options", "DENY")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head or length == 0:
            return
        try:
            with path.open("rb") as source:
                source.seek(start)
                remaining = length
                while remaining:
                    chunk = source.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, value: Any, *, status: HTTPStatus = HTTPStatus.OK, head: bool = False) -> None:
        body = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    def log_message(self, format: str, *arguments: Any) -> None:
        # A project-room refresh should not flood the author's terminal.  Errors
        # remain visible through the API response and caller-level diagnostics.
        del format, arguments


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None | bool:
    """Return an inclusive range, None for full content, or False if invalid."""

    if header is None:
        return None
    if not header.startswith("bytes=") or "," in header or size <= 0:
        return False
    value = header[6:].strip()
    if "-" not in value:
        return False
    first, last = value.split("-", 1)
    try:
        if not first:
            suffix = int(last)
            if suffix <= 0:
                return False
            start = max(0, size - suffix)
            return start, size - 1
        start = int(first)
        if start < 0 or start >= size:
            return False
        end = size - 1 if not last else min(int(last), size - 1)
        if end < start:
            return False
        return start, end
    except ValueError:
        return False


def make_server(
    project: Any,
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    data_provider: Any | None = None,
) -> tuple[ChalkHTTPServer, str]:
    """Create, but do not start, a project-room server (useful for tests)."""

    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("the project room only binds to the local loopback interface")
    room = ProjectRoom(project, data_provider)
    server = ChalkHTTPServer((host, port), room)
    bound_host, bound_port = server.server_address[:2]
    display_host = "127.0.0.1" if bound_host in {"0.0.0.0", ""} else str(bound_host)
    return server, f"http://{display_host}:{bound_port}/"


class ServerHandle(str):
    """A URL string carrying lifecycle controls for its background server."""

    server: ChalkHTTPServer
    thread: threading.Thread
    url: str

    def __new__(
        cls,
        url: str,
        server: ChalkHTTPServer,
        thread: threading.Thread,
    ) -> "ServerHandle":
        instance = str.__new__(cls, url)
        instance.url = url
        instance.server = server
        instance.thread = thread
        return instance

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def wait(self) -> None:
        try:
            self.thread.join()
        except KeyboardInterrupt:
            self.shutdown()


def serve(
    project: Any,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    background: bool = True,
) -> ServerHandle:
    """Start the local project room, print its URL, and return a URL handle."""

    server, url = make_server(project, host, port)
    thread = threading.Thread(target=server.serve_forever, name="chalk-project-room", daemon=True)
    thread.start()
    handle = ServerHandle(url, server, thread)
    print(url, flush=True)
    if open_browser:
        webbrowser.open(url)
    if not background:
        handle.wait()
    return handle


__all__ = [
    "LocalProjectData",
    "ProjectRoom",
    "ServerError",
    "ServerHandle",
    "compose_project_schema",
    "make_server",
    "serve",
]
