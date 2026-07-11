"""Project model, script facets, state, validation, and human status.

The model intentionally derives the segment registry from ``script.md``.  The
only durable join key is the ``chalk:segment`` marker under each level-two
heading; headings, order, scene paths, recordings, and review state all derive
from that stable id.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
import hashlib
import json
import os
import re
import tempfile
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

try:  # Chalk's production platform is macOS; fallback keeps imports portable.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


STATE_SCHEMA_VERSION = 1
SEGMENT_MARKER_RE = re.compile(
    r"^\s*<!--\s*chalk:segment\s+([a-z0-9][a-z0-9._-]*)\s*-->\s*$"
)
ANY_SEGMENT_MARKER_RE = re.compile(r"<!--\s*chalk:segment\b(.*?)-->")
HEADING_RE = re.compile(r"^##(?!#)\s+(.+?)\s*$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_LOCK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_THREAD_LOCKS: dict[Path, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class ModelError(RuntimeError):
    """A project could not be loaded or persisted safely."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical JSON representation used for local hashes."""

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def hash_bytes(data: bytes) -> str:
    """Hash bytes, preferring the content-store implementation when present."""

    try:
        from . import store  # type: ignore

        function = getattr(store, "hash_bytes", None)
        if callable(function):
            value = function(data)
            if isinstance(value, str):
                return value.removeprefix("sha256:")
    except (ImportError, AttributeError):
        pass
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    return hash_bytes(_normalize_text(text).encode("utf-8"))


def hash_file(path: Path) -> str:
    """Hash a file without loading large media into memory."""

    try:
        from . import store  # type: ignore

        function = getattr(store, "hash_file", None)
        if callable(function):
            value = function(path)
            if isinstance(value, str):
                return value.removeprefix("sha256:")
    except (ImportError, AttributeError):
        pass

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace a UTF-8 text file in its destination directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
    )


@contextmanager
def project_lock(
    root: str | os.PathLike[str], name: str = "project"
) -> Iterator[None]:
    """Serialize a short project read-modify-write across agents/processes."""

    if not _LOCK_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid project lock name: {name!r}")
    path = Path(root).resolve() / ".chalk" / "locks" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(path, threading.RLock())
    with thread_lock, path.open("a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    message: str
    path: str | None = None
    line: int | None = None
    segment_id: str | None = None

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "severity": self.severity,
                "code": self.code,
                "message": self.message,
                "path": self.path,
                "line": self.line,
                "segment_id": self.segment_id,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class Segment:
    id: str
    title: str
    order: int
    source: Path
    source_line: int
    narration: str
    director_notes: tuple[str, ...] = ()
    visual_notes: tuple[str, ...] = ()
    quoted_notes: tuple[str, ...] = ()
    narration_hash: str = ""
    director_hash: str = ""
    visual_hash: str = ""

    def __post_init__(self) -> None:
        if not self.narration_hash:
            object.__setattr__(self, "narration_hash", hash_text(self.narration))
        if not self.director_hash:
            object.__setattr__(
                self, "director_hash", hash_text("\n\n".join(self.director_notes))
            )
        if not self.visual_hash:
            visual = (*self.visual_notes, *self.quoted_notes)
            object.__setattr__(self, "visual_hash", hash_text("\n\n".join(visual)))

    @property
    def scene_source(self) -> Path:
        return self.source

    def facet_hashes(self) -> dict[str, str]:
        return {
            "narration": self.narration_hash,
            "director": self.director_hash,
            "visual": self.visual_hash,
        }


@dataclass(frozen=True)
class ParsedScript(Sequence[Segment]):
    path: Path
    segments: tuple[Segment, ...]
    diagnostics: tuple[Diagnostic, ...]
    source_hash: str

    def __len__(self) -> int:
        return len(self.segments)

    def __getitem__(self, index: int | slice) -> Segment | tuple[Segment, ...]:
        return self.segments[index]

    def __iter__(self) -> Iterator[Segment]:
        return iter(self.segments)


@dataclass(frozen=True)
class ProjectConfig:
    root: Path
    path: Path
    raw: Mapping[str, Any]
    schema_version: int
    project_id: str
    title: str
    slug: str
    brief: Path = Path("brief.md")
    outline: Path = Path("outline.md")
    script: Path = Path("script.md")
    scenes: Path = Path("scenes")
    style: Path = Path("style.py")
    context: Path = Path("context")

    @property
    def id(self) -> str:
        return self.project_id

    @property
    def content_hash(self) -> str:
        return hash_file(self.path)


@dataclass
class ProjectState:
    """Small tracked state; take selection has exactly one canonical map."""

    selected_takes: dict[str, str] = field(default_factory=dict)
    approvals: dict[str, dict[str, Any]] = field(default_factory=dict)
    current_actions: dict[str, str] = field(default_factory=dict)
    schema_version: int = STATE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "selected_takes": dict(sorted(self.selected_takes.items())),
            "approvals": {key: self.approvals[key] for key in sorted(self.approvals)},
            "current_actions": dict(sorted(self.current_actions.items())),
        }


@dataclass
class Project:
    root: Path
    config: ProjectConfig
    script: ParsedScript
    state: ProjectState

    @property
    def segments(self) -> tuple[Segment, ...]:
        return self.script.segments

    @property
    def state_path(self) -> Path:
        return self.root / ".chalk" / "state.json"

    def segment(self, segment_id: str) -> Segment:
        matches = [segment for segment in self.segments if segment.id == segment_id]
        if not matches:
            raise ModelError(f"unknown segment '{segment_id}'")
        if len(matches) > 1:
            raise ModelError(f"segment id '{segment_id}' is duplicated")
        return matches[0]

    def scene_path(self, segment: Segment | str) -> Path:
        value = self.segment(segment) if isinstance(segment, str) else segment
        return self.root / value.source

    def selected_take(self, segment: Segment | str) -> str | None:
        segment_id = segment if isinstance(segment, str) else segment.id
        return self.state.selected_takes.get(segment_id)

    def save_state(self) -> None:
        save_state(self.root, self.state)

    def reload(self) -> "Project":
        return load_project(self.root)

    @classmethod
    def load(cls, start: str | os.PathLike[str] | None = None) -> "Project":
        return load_project(start)


def find_project_root(start: str | os.PathLike[str] | None = None) -> Path:
    """Walk upward from ``start`` until a ``chalk.toml`` is found."""

    current = Path(start or Path.cwd()).expanduser()
    if current.is_file():
        current = current.parent
    try:
        current = current.resolve()
    except OSError as error:
        raise ModelError(f"cannot resolve project path {current}: {error}") from error

    for candidate in (current, *current.parents):
        if (candidate / "chalk.toml").is_file():
            return candidate
    raise ModelError(f"no chalk.toml found at or above {current}")


def _config_path(table: Mapping[str, Any], key: str, default: str) -> Path:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ModelError(f"chalk.toml paths.{key} must be a non-empty string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ModelError(f"chalk.toml paths.{key} must stay inside the project")
    return path


def load_config(root: str | os.PathLike[str]) -> ProjectConfig:
    root_path = Path(root).resolve()
    path = root_path / "chalk.toml"
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError as error:
        raise ModelError(f"missing project config: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ModelError(f"invalid {path.name}: {error}") from error

    project = raw.get("project", {})
    paths = raw.get("paths", {})
    if not isinstance(project, Mapping):
        raise ModelError("chalk.toml [project] must be a table")
    if not isinstance(paths, Mapping):
        raise ModelError("chalk.toml [paths] must be a table")

    schema = raw.get("schema", raw.get("schema_version", raw.get("version", 1)))
    if not isinstance(schema, int):
        raise ModelError("chalk.toml schema_version must be an integer")

    project_id = project.get("id", raw.get("project_id", raw.get("id", "")))
    title = project.get("title", raw.get("title", root_path.name))
    slug = project.get("slug", raw.get("slug", root_path.name))
    for key, value in (
        ("project.id", project_id),
        ("project.title", title),
        ("project.slug", slug),
    ):
        if not isinstance(value, str):
            raise ModelError(f"chalk.toml {key} must be a string")

    return ProjectConfig(
        root=root_path,
        path=path,
        raw=raw,
        schema_version=schema,
        project_id=project_id,
        title=title,
        slug=slug,
        brief=_config_path(paths, "brief", "brief.md"),
        outline=_config_path(paths, "outline", "outline.md"),
        script=_config_path(paths, "script", "script.md"),
        scenes=_config_path(paths, "scenes", "scenes"),
        style=_config_path(paths, "style", "style.py"),
        context=_config_path(paths, "context", "context"),
    )


def _strip_note_decoration(text: str) -> str:
    value = _normalize_text(text)
    if value.startswith("**") and value.endswith("**") and len(value) >= 4:
        value = value[2:-2]
    return value.strip()


def _note_kind(text: str) -> str:
    probe = text.lstrip("*_ ").upper()
    if probe.startswith("[DIRECTOR:") or probe.startswith("[DIRECTOR]"):
        return "director"
    if (
        probe.startswith("[VISUAL:")
        or probe.startswith("[VISUAL]")
        or probe.startswith("[MANIM:")
        or probe.startswith("[MANIM]")
        or probe.startswith("[CUT TO MANIM")
    ):
        return "visual"
    return "quoted"


def _narration_from_lines(lines: Sequence[str]) -> str:
    output: list[str] = []
    pending_blank = False
    in_fence = False
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if not stripped:
            pending_blank = bool(output)
            continue
        if not in_fence and (stripped.startswith("#") or stripped == "---"):
            continue
        if pending_blank and output and output[-1] != "":
            output.append("")
        output.append(stripped)
        pending_blank = False
    return _normalize_text("\n".join(output))


def _fenced_lines(lines: Sequence[str]) -> tuple[bool, ...]:
    """Mark Markdown fence delimiters and contents, honoring fence length."""

    masked: list[bool] = []
    fence_character: str | None = None
    fence_length = 0
    for line in lines:
        match = FENCE_RE.match(line)
        if fence_character is None:
            if match:
                token = match.group(1)
                fence_character = token[0]
                fence_length = len(token)
                masked.append(True)
            else:
                masked.append(False)
            continue
        masked.append(True)
        if match:
            token = match.group(1)
            if (
                token[0] == fence_character
                and len(token) >= fence_length
                and not match.group(2).strip()
            ):
                fence_character = None
                fence_length = 0
    return tuple(masked)


def parse_script(
    path: str | os.PathLike[str], *, scenes_dir: str | Path = "scenes"
) -> ParsedScript:
    """Parse level-two script sections and their separately hashed facets."""

    script_path = Path(path)
    try:
        source = script_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ModelError(f"missing script: {script_path}") from error
    except UnicodeDecodeError as error:
        raise ModelError(f"script is not UTF-8: {script_path}") from error

    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    fenced = _fenced_lines(lines)
    headings: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if fenced[index]:
            continue
        match = HEADING_RE.match(line)
        if match:
            headings.append((index, match.group(1).strip()))

    diagnostics: list[Diagnostic] = []
    segments: list[Segment] = []
    for order, (heading_index, title) in enumerate(headings):
        end = headings[order + 1][0] if order + 1 < len(headings) else len(lines)
        body = lines[heading_index + 1 : end]
        markers: list[tuple[int, str]] = []
        malformed_markers: list[int] = []
        for offset, line in enumerate(body, start=heading_index + 2):
            if fenced[offset - 1]:
                continue
            marker = SEGMENT_MARKER_RE.match(line)
            if marker:
                markers.append((offset, marker.group(1)))
            elif ANY_SEGMENT_MARKER_RE.search(line):
                malformed_markers.append(offset)

        segment_id = markers[0][1] if markers else ""
        if not markers:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "segment-id-missing",
                    f"section '{title}' needs <!-- chalk:segment stable-id -->",
                    str(script_path),
                    heading_index + 1,
                )
            )
        if len(markers) > 1:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "segment-id-multiple",
                    f"section '{title}' contains more than one segment marker",
                    str(script_path),
                    markers[1][0],
                    segment_id or None,
                )
            )
        for line_number in malformed_markers:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "segment-id-invalid",
                    "segment ids use lowercase letters, digits, '.', '_' or '-'",
                    str(script_path),
                    line_number,
                    segment_id or None,
                )
            )

        narration_lines: list[str] = []
        director: list[str] = []
        visual: list[str] = []
        quoted: list[str] = []
        cursor = 0
        while cursor < len(body):
            line = body[cursor]
            source_index = heading_index + 1 + cursor
            if fenced[source_index]:
                cursor += 1
                continue
            if SEGMENT_MARKER_RE.match(line) or ANY_SEGMENT_MARKER_RE.search(line):
                cursor += 1
                continue
            quote = QUOTE_RE.match(line)
            if quote:
                block = [quote.group(1)]
                cursor += 1
                while cursor < len(body):
                    if fenced[heading_index + 1 + cursor]:
                        break
                    continuation = QUOTE_RE.match(body[cursor])
                    if not continuation:
                        break
                    block.append(continuation.group(1))
                    cursor += 1
                note = _strip_note_decoration("\n".join(block))
                kind = _note_kind(note)
                if kind == "director":
                    director.append(note)
                elif kind == "visual":
                    visual.append(note)
                else:
                    quoted.append(note)
                continue
            narration_lines.append(line)
            cursor += 1

        narration = _narration_from_lines(narration_lines)
        source_path = Path(scenes_dir) / f"{segment_id}.py" if segment_id else Path(scenes_dir)
        segments.append(
            Segment(
                id=segment_id,
                title=title,
                order=order,
                source=source_path,
                source_line=heading_index + 1,
                narration=narration,
                director_notes=tuple(director),
                visual_notes=tuple(visual),
                quoted_notes=tuple(quoted),
            )
        )

    seen: dict[str, Segment] = {}
    for segment in segments:
        if not segment.id:
            continue
        if segment.id in seen:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "segment-id-duplicate",
                    (
                        f"segment id '{segment.id}' is also used on line "
                        f"{seen[segment.id].source_line}"
                    ),
                    str(script_path),
                    segment.source_line,
                    segment.id,
                )
            )
        else:
            seen[segment.id] = segment

    return ParsedScript(
        path=script_path,
        segments=tuple(segments),
        diagnostics=tuple(diagnostics),
        source_hash=hash_bytes(source.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")),
    )


def load_state(root: str | os.PathLike[str]) -> ProjectState:
    path = Path(root) / ".chalk" / "state.json"
    if not path.exists():
        return ProjectState()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelError(f"invalid project state {path}: {error}") from error
    if not isinstance(value, dict):
        raise ModelError(f"invalid project state {path}: root must be an object")
    schema = value.get("schema_version", STATE_SCHEMA_VERSION)
    selected = value.get("selected_takes", {})
    approvals = value.get("approvals", {})
    actions = value.get("current_actions", {})
    if not isinstance(schema, int):
        raise ModelError("state schema_version must be an integer")
    if not isinstance(selected, dict):
        raise ModelError("state selected_takes must be one object map")
    if not isinstance(approvals, dict):
        raise ModelError("state approvals must be an object map")
    if not isinstance(actions, dict):
        raise ModelError("state current_actions must be an object map")
    return ProjectState(
        selected_takes=dict(selected),
        approvals=dict(approvals),
        current_actions=dict(actions),
        schema_version=schema,
    )


def save_state(root: str | os.PathLike[str], state: ProjectState) -> None:
    atomic_write_json(Path(root) / ".chalk" / "state.json", state.to_dict())


def load_project(start: str | os.PathLike[str] | None = None) -> Project:
    root = find_project_root(start)
    config = load_config(root)
    script = parse_script(root / config.script, scenes_dir=config.scenes)
    state = load_state(root)
    return Project(root=root, config=config, script=script, state=state)


@dataclass(frozen=True)
class ValidationReport:
    diagnostics: tuple[Diagnostic, ...]
    scope: str | None = None

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "error")

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def structural_ok(self) -> bool:
        return self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "scope": self.scope,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


def _relative(project: Project, path: Path) -> str:
    try:
        return path.relative_to(project.root).as_posix()
    except ValueError:
        return str(path)


def _validate_scene(project: Project, segment: Segment) -> list[Diagnostic]:
    path = project.scene_path(segment)
    relative = _relative(project, path)
    if not path.is_file():
        return [
            Diagnostic(
                "error",
                "scene-missing",
                f"missing scene source for '{segment.id}': {relative}",
                relative,
                segment_id=segment.id,
            )
        ]
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return [
            Diagnostic(
                "error", "scene-unreadable", str(error), relative, segment_id=segment.id
            )
        ]
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError as error:
        return [
            Diagnostic(
                "error",
                "scene-syntax",
                error.msg,
                relative,
                error.lineno,
                segment.id,
            )
        ]

    diagnostics: list[Diagnostic] = []
    if not any(isinstance(node, ast.ClassDef) and node.name == "Visual" for node in tree.body):
        diagnostics.append(
            Diagnostic(
                "error",
                "scene-visual-class-missing",
                "scene must export class Visual",
                relative,
                segment_id=segment.id,
            )
        )

    from .timeline import (
        AmbiguousPhraseError,
        CueNotFoundError,
        resolve_timeline,
    )

    timeline = resolve_timeline(segment.id, segment.narration)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_name: str | None = None
        if isinstance(node.func, ast.Attribute):
            function_name = node.func.attr
        elif isinstance(node.func, ast.Name):
            function_name = node.func.id
        if function_name not in {"play_on", "land_on"}:
            continue
        if (
            not node.args
            or not isinstance(node.args[0], ast.Constant)
            or not isinstance(node.args[0].value, str)
        ):
            diagnostics.append(
                Diagnostic(
                    "error",
                    "scene-cue-not-literal",
                    f"{function_name} phrase must be a literal string",
                    relative,
                    getattr(node, "lineno", None),
                    segment.id,
                )
            )
            continue
        phrase = node.args[0].value
        occurrence: int | None = None
        occurrence_node = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "occurrence"),
            None,
        )
        if occurrence_node is not None:
            if (
                not isinstance(occurrence_node, ast.Constant)
                or not isinstance(occurrence_node.value, int)
                or isinstance(occurrence_node.value, bool)
            ):
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "scene-cue-occurrence-not-literal",
                        f"{function_name} occurrence must be a literal integer",
                        relative,
                        getattr(node, "lineno", None),
                        segment.id,
                    )
                )
                continue
            occurrence = occurrence_node.value
        try:
            timeline.cue(phrase, occurrence=occurrence)
        except AmbiguousPhraseError as error:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "scene-cue-ambiguous",
                    str(error),
                    relative,
                    getattr(node, "lineno", None),
                    segment.id,
                )
            )
        except CueNotFoundError as error:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "scene-cue-not-in-narration",
                    str(error),
                    relative,
                    getattr(node, "lineno", None),
                    segment.id,
                )
            )
    return diagnostics


def take_record(project: Project, take_id: str) -> Any | None:
    """Resolve a take id through ``AudioStore`` without an import cycle."""

    try:
        from .audio import AudioStore

        return AudioStore(project.root).get_take(take_id)
    except (ImportError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def take_path(project: Project, segment_id: str, take_id: str) -> Path | None:
    """Resolve the blob for a selected take id, never treating the id as audio."""

    record = take_record(project, take_id)
    if record is None or getattr(record, "segment_id", None) != segment_id:
        return None
    try:
        from .audio import AudioStore

        path = AudioStore(project.root).take_path(record)
    except (ImportError, OSError, TypeError, ValueError):
        return None
    return path if path.is_file() else None


def transcript_path(project: Project, segment_id: str, take_id: str) -> Path | None:
    """Resolve the transcript by the selected take's audio revision."""

    record = take_record(project, take_id)
    if record is None or getattr(record, "segment_id", None) != segment_id:
        return None
    try:
        from .audio import AudioStore

        path = AudioStore(project.root).transcript_path(record.audio_sha256)
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        return None
    return path if path.is_file() else None


def _expected_transcript_configuration(
    project: Project,
) -> tuple[str | None, str | None, dict[str, Any]]:
    voice = project.config.raw.get("voice", {})
    if not isinstance(voice, Mapping):
        return None, None, {"word_timestamps": True}
    model_value = voice.get("model")
    model = str(model_value) if isinstance(model_value, str) and model_value else None
    revision_value = voice.get("revision")
    revision = (
        str(revision_value)
        if isinstance(revision_value, str) and revision_value
        else None
    )
    if model and revision is None and "@" in model:
        candidate_model, candidate_revision = model.rsplit("@", 1)
        if candidate_model and re.fullmatch(r"[0-9a-f]{40,64}", candidate_revision):
            model, revision = candidate_model, candidate_revision
    options: dict[str, Any] = {"word_timestamps": True}
    language = voice.get("language")
    if isinstance(language, str) and language:
        options["language"] = language
    return model, revision, options


def _validate_selected_take(project: Project, segment: Segment) -> list[Diagnostic]:
    take_id = project.state.selected_takes.get(segment.id)
    if take_id is None:
        return [
            Diagnostic(
                "warning",
                "take-unselected",
                "no voice take selected yet",
                segment_id=segment.id,
            )
        ]
    if not isinstance(take_id, str) or not take_id:
        return [
            Diagnostic(
                "error",
                "take-selection-invalid",
                "selected take id must be a non-empty string",
                ".chalk/state.json",
                segment_id=segment.id,
            )
        ]

    record = take_record(project, take_id)
    if record is None:
        return [
            Diagnostic(
                "error",
                "take-selection-unknown",
                f"selected take id {take_id!r} is absent from media/takes/takes.json",
                ".chalk/state.json",
                segment_id=segment.id,
            )
        ]
    if getattr(record, "segment_id", None) != segment.id:
        return [
            Diagnostic(
                "error",
                "take-segment-mismatch",
                f"selected take {take_id!r} belongs to {record.segment_id!r}",
                "media/takes/takes.json",
                segment_id=segment.id,
            )
        ]

    record_path = Path(str(getattr(record, "path", "")))
    try:
        (project.root / record_path).resolve().relative_to(project.root.resolve())
    except ValueError:
        return [
            Diagnostic(
                "error",
                "take-path-invalid",
                "selected take path escapes the project",
                "media/takes/takes.json",
                segment_id=segment.id,
            )
        ]

    diagnostics: list[Diagnostic] = []
    path = take_path(project, segment.id, take_id)
    if path is None:
        diagnostics.append(
            Diagnostic(
                "warning",
                "take-missing",
                f"selected take {take_id[:12]} has no retained audio blob",
                str(getattr(record, "path", "media/takes/sha256")),
                segment_id=segment.id,
            )
        )
    else:
        actual = hash_file(path)
        expected_audio = str(getattr(record, "audio_sha256", ""))
        if actual != expected_audio:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "take-hash-mismatch",
                    f"selected audio hashes to {actual}, expected {expected_audio}",
                    _relative(project, path),
                    segment_id=segment.id,
                )
            )
    recorded_against = str(getattr(record, "narration_sha256", ""))
    if recorded_against != segment.narration_hash:
        diagnostics.append(
            Diagnostic(
                "warning",
                "take-stale",
                "selected take was recorded against different narration",
                "media/takes/takes.json",
                segment_id=segment.id,
            )
        )

    transcript = transcript_path(project, segment.id, take_id)
    if transcript is None:
        diagnostics.append(
            Diagnostic(
                "warning",
                "transcript-missing",
                "selected take has no local transcript",
                segment_id=segment.id,
            )
        )
    else:
        try:
            value = json.loads(transcript.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping):
                raise ValueError("root is not an object")
            transcript_audio = value.get("audio_sha256")
            if transcript_audio != getattr(record, "audio_sha256", None):
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "transcript-audio-mismatch",
                        "transcript does not refer to the selected take's audio hash",
                        _relative(project, transcript),
                        segment_id=segment.id,
                    )
                )
            required = {
                "cache_key",
                "model",
                "model_fingerprint",
                "options",
                "words",
            }
            missing = sorted(required.difference(value))
            if missing:
                raise ValueError(
                    "missing exact provenance fields: " + ", ".join(missing)
                )
            from .audio import AudioStore

            options = value.get("options")
            if not isinstance(options, Mapping):
                raise ValueError("options is not an object")
            expected_key = AudioStore.transcript_cache_key(
                str(value.get("audio_sha256", "")),
                str(value.get("model_fingerprint", "")),
                options,
            )
            if value.get("cache_key") != expected_key:
                raise ValueError("cache key does not match audio/model/options")

            expected_model, expected_revision, expected_options = (
                _expected_transcript_configuration(project)
            )
            configuration_matches = (
                (expected_model is None or value.get("model") == expected_model)
                and (
                    expected_revision is None
                    or value.get("model_revision") == expected_revision
                )
                and all(options.get(key) == item for key, item in expected_options.items())
            )
            if not configuration_matches:
                diagnostics.append(
                    Diagnostic(
                        "warning",
                        "transcript-config-stale",
                        "transcript was produced with different voice model settings",
                        _relative(project, transcript),
                        segment_id=segment.id,
                    )
                )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            diagnostics.append(
                Diagnostic(
                    "warning",
                    "transcript-invalid",
                    f"cannot use transcript: {error}",
                    _relative(project, transcript),
                    segment_id=segment.id,
                )
            )
    return diagnostics


def validate_project(project: Project, scope: str | None = None) -> ValidationReport:
    """Validate global identity plus either all scenes or one requested segment.

    A scoped draft never fails because some unrelated scene or recording is not
    ready.  Missing recordings/transcripts remain warnings in every mode.
    """

    diagnostics = [
        item
        for item in project.script.diagnostics
        if scope is None or item.segment_id == scope
    ]
    if project.config.schema_version != 1:
        diagnostics.append(
            Diagnostic(
                "error",
                "config-schema-unsupported",
                f"unsupported chalk.toml schema_version {project.config.schema_version}",
                "chalk.toml",
            )
        )
    if not project.config.project_id:
        diagnostics.append(
            Diagnostic(
                "error", "project-id-missing", "project_id is required", "chalk.toml"
            )
        )
    if project.state.schema_version != STATE_SCHEMA_VERSION:
        diagnostics.append(
            Diagnostic(
                "error",
                "state-schema-unsupported",
                f"unsupported state schema_version {project.state.schema_version}",
                ".chalk/state.json",
            )
        )

    registered = {segment.id for segment in project.segments if segment.id}
    if scope is not None and scope not in registered:
        diagnostics.append(
            Diagnostic("error", "scope-unknown", f"unknown segment '{scope}'", segment_id=scope)
        )
        return ValidationReport(tuple(diagnostics), scope)

    selected_segments = [
        segment
        for segment in project.segments
        if segment.id and (scope is None or segment.id == scope)
    ]
    for segment in selected_segments:
        diagnostics.extend(_validate_scene(project, segment))
        diagnostics.extend(_validate_selected_take(project, segment))

    if scope is None:
        scenes_dir = project.root / project.config.scenes
        if scenes_dir.is_dir():
            for scene in sorted(scenes_dir.glob("*.py")):
                if scene.name == "__init__.py" or scene.stem.startswith("_"):
                    continue
                if scene.stem not in registered:
                    diagnostics.append(
                        Diagnostic(
                            "error",
                            "scene-orphan",
                            f"scene has no script segment marker: {scene.name}",
                            _relative(project, scene),
                        )
                    )

    for segment_id, take_id in project.state.selected_takes.items():
        if scope is not None and segment_id != scope:
            continue
        if segment_id not in registered:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "state-segment-unknown",
                    f"selected take refers to unknown segment '{segment_id}'",
                    ".chalk/state.json",
                    segment_id=segment_id,
                )
            )
        elif not isinstance(take_id, str) or not take_id:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "take-selection-invalid",
                    "selected take id must be a non-empty string",
                    ".chalk/state.json",
                    segment_id=segment_id,
                )
            )

    if scope is None:
        for key, approval in project.state.approvals.items():
            target = key.removeprefix("segment:")
            if key != "project" and target not in registered:
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "approval-segment-unknown",
                        f"approval refers to unknown segment '{target}'",
                        ".chalk/state.json",
                        segment_id=target,
                    )
                )
            if not isinstance(approval, Mapping):
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "approval-invalid",
                        f"approval '{key}' must be an object",
                        ".chalk/state.json",
                    )
                )
        for key, digest in project.state.current_actions.items():
            if (
                not isinstance(key, str)
                or not isinstance(digest, str)
                or not SHA256_RE.fullmatch(digest)
            ):
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "action-ref-invalid",
                        "current action refs must map names to full SHA-256 digests",
                        ".chalk/state.json",
                    )
                )

    diagnostics.sort(
        key=lambda item: (
            0 if item.severity == "error" else 1,
            item.path or "",
            item.line or 0,
            item.code,
        )
    )
    return ValidationReport(tuple(diagnostics), scope)


validate = validate_project


def _feedback_open_count(root: Path, segment_id: str | None = None) -> int:
    path = root / "feedback.md"
    if not path.is_file():
        return 0
    count = 0
    current_open = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("- [ ] **"):
            count += 1 if segment_id is None else 0
            current_open = True
        elif line.startswith("- [x] **") or line.startswith("- [X] **"):
            current_open = False
        if "<!-- chalk:note " in line and segment_id is not None:
            try:
                payload = line.split("<!-- chalk:note ", 1)[1].rsplit(" -->", 1)[0]
                current_segment = json.loads(payload).get("segment")
            except (json.JSONDecodeError, AttributeError):
                current_segment = None
            if current_open and current_segment == segment_id:
                count += 1
    return count


@dataclass(frozen=True)
class SegmentStatus:
    id: str
    title: str
    scene: str
    take: str
    transcript: str
    render: str
    cut: str
    approval: str
    unresolved_notes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "scene": self.scene,
            "take": self.take,
            "transcript": self.transcript,
            "render": self.render,
            "cut": self.cut,
            "approval": self.approval,
            "unresolved_notes": self.unresolved_notes,
        }


@dataclass(frozen=True)
class ProjectStatus:
    project_id: str
    title: str
    segments: tuple[SegmentStatus, ...]
    errors: int
    warnings: int
    unresolved_notes: int
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "title": self.title,
            "errors": self.errors,
            "warnings": self.warnings,
            "unresolved_notes": self.unresolved_notes,
            "next_action": self.next_action,
            "segments": [segment.to_dict() for segment in self.segments],
        }


def project_status(project: Project) -> ProjectStatus:
    report = validate_project(project)
    artifact_segments: Mapping[str, Any] = {}
    try:
        from .build import project_artifacts

        build_state = project_artifacts(project)
        value = build_state.get("segments", {})
        if isinstance(value, Mapping):
            artifact_segments = value
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        pass
    statuses: list[SegmentStatus] = []
    for segment in project.segments:
        if not segment.id:
            continue
        scoped = validate_project(project, segment.id)
        codes = {item.code for item in scoped.diagnostics}
        scene = (
            "missing"
            if "scene-missing" in codes
            else "invalid"
            if any(code.startswith("scene-") for code in codes)
            else "ready"
        )
        if "take-unselected" in codes:
            take = "unselected"
        elif "take-missing" in codes:
            take = "missing"
        elif "take-stale" in codes:
            take = "stale"
        elif any(
            code in codes
            for code in {
                "take-hash-mismatch",
                "take-selection-invalid",
                "take-selection-unknown",
                "take-segment-mismatch",
                "take-path-invalid",
            }
        ):
            take = "invalid"
        else:
            take = "current"
        transcript = (
            "unavailable"
            if take in {"unselected", "missing", "invalid"}
            else "missing"
            if "transcript-missing" in codes
            else "invalid"
            if "transcript-invalid" in codes or "transcript-audio-mismatch" in codes
            else "stale"
            if "transcript-config-stale" in codes
            else "current"
        )
        segment_artifacts = artifact_segments.get(segment.id, {})

        def artifact_state(kind: str) -> str:
            value = (
                segment_artifacts.get(kind, {})
                if isinstance(segment_artifacts, Mapping)
                else {}
            )
            if not isinstance(value, Mapping):
                return "missing"
            if bool(value.get("current")):
                return "current"
            return "stale" if bool(value.get("exists")) else "missing"

        render = artifact_state("render")
        cut = artifact_state("cut")

        approval_value = project.state.approvals.get(f"segment:{segment.id}")
        approval = "recorded" if isinstance(approval_value, Mapping) else "none"
        try:
            from .review import approval_status

            approval = approval_status(project, segment.id).state
        except (ImportError, AttributeError, ModelError):
            pass
        statuses.append(
            SegmentStatus(
                segment.id,
                segment.title,
                scene,
                take,
                transcript,
                render,
                cut,
                approval,
                _feedback_open_count(project.root, segment.id),
            )
        )

    provisional = ProjectStatus(
        project.config.project_id,
        project.config.title,
        tuple(statuses),
        len(report.errors),
        len(report.warnings),
        _feedback_open_count(project.root),
        "",
    )
    return ProjectStatus(
        provisional.project_id,
        provisional.title,
        provisional.segments,
        provisional.errors,
        provisional.warnings,
        provisional.unresolved_notes,
        next_action(provisional),
    )


status = project_status


def next_action(value: Project | ProjectStatus) -> str:
    current = project_status(value) if isinstance(value, Project) else value
    if current.errors:
        return "Fix structural errors (`chalk check`)."
    for segment in current.segments:
        if segment.scene != "ready":
            return f"Finish the scene for {segment.id} (`chalk watch {segment.id}`)."
    for segment in current.segments:
        if segment.render != "current":
            return f"Watch the visual draft for {segment.id} (`chalk watch {segment.id}`)."
    for segment in current.segments:
        if segment.take in {"unselected", "missing", "stale", "invalid"}:
            return f"Record or select a current take (`chalk record {segment.id}`)."
    for segment in current.segments:
        if segment.transcript != "current":
            return f"Transcribe the selected take for {segment.id}."
    for segment in current.segments:
        if segment.cut != "current":
            return f"Rebuild and review the A/V cut for {segment.id} (`chalk review {segment.id}`)."
    for segment in current.segments:
        if segment.unresolved_notes:
            return f"Address open feedback for {segment.id} (`chalk review {segment.id}`)."
    for segment in current.segments:
        if segment.approval != "current":
            return f"Review and approve {segment.id} (`chalk review {segment.id}`)."
    return "Review the full cut (`chalk review --full`) or create a release."


__all__ = [
    "Diagnostic",
    "ModelError",
    "ParsedScript",
    "Project",
    "ProjectConfig",
    "ProjectState",
    "ProjectStatus",
    "Segment",
    "SegmentStatus",
    "ValidationReport",
    "atomic_write_json",
    "atomic_write_text",
    "canonical_json_bytes",
    "find_project_root",
    "hash_bytes",
    "hash_file",
    "hash_text",
    "load_config",
    "load_project",
    "load_state",
    "next_action",
    "parse_script",
    "project_status",
    "project_lock",
    "save_state",
    "status",
    "take_record",
    "take_path",
    "transcript_path",
    "validate",
    "validate_project",
]
