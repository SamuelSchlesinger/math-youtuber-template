"""Content snapshots, review notes, approvals, and pinned reference context."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .model import (
    Project,
    atomic_write_json,
    atomic_write_text,
    canonical_json_bytes,
    hash_bytes,
    hash_file,
    load_project,
    save_state,
    take_record,
    take_path,
    transcript_path,
)


SNAPSHOT_SCHEMA_VERSION = 1
NOTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
NOTE_LINE_RE = re.compile(r"^- \[([ xX])\] \*\*([^*]+)\*\*(?:\s+—\s+(.*))?$")
NOTE_META_RE = re.compile(r"^\s*<!-- chalk:note (\{.*\}) -->\s*$")
TIMECODE_RE = re.compile(r"^(?:\d{1,3}:)?[0-5]?\d(?::[0-5]\d)?(?:\.\d{1,3})?$|^\d+(?:\.\d+)?s$")


class ReviewError(RuntimeError):
    """Review source or context could not be updated safely."""


def _fresh_project(value: Project | str | os.PathLike[str]) -> Project:
    """Reload physical source/state so review never uses a stale caller view."""

    return load_project(value.root if isinstance(value, Project) else value)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _file_record(root: Path, path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"path": _relative(root, path), "sha256": None, "missing": True}
    return {
        "path": _relative(root, path),
        "sha256": hash_file(path),
        "size": path.stat().st_size,
    }


def _git_output(
    root: Path, *arguments: str, strip: bool = True
) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() if strip else result.stdout.rstrip("\r\n")


def git_provenance(root: Path) -> dict[str, Any]:
    """Describe Git without allowing snapshot/ref writes to perturb snapshots."""

    top = _git_output(root, "rev-parse", "--show-toplevel")
    if top is None:
        return {"root": None, "head": None, "dirty": None, "dirty_paths": []}
    if Path(top).resolve() != root.resolve():
        return {"root": None, "head": None, "dirty": None, "dirty_paths": []}
    head = _git_output(root, "rev-parse", "HEAD")
    porcelain = (
        _git_output(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            strip=False,
        )
        or ""
    )
    ignored_prefixes = (
        ".chalk/snapshots/",
        ".chalk/refs/",
        ".chalk/cache/",
        ".chalk/tmp/",
    )
    paths: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        value = value.strip('"')
        if not value.startswith(ignored_prefixes):
            paths.append(value)
    return {
        "root": ".",
        "head": head,
        "dirty": bool(paths),
        "dirty_paths": sorted(paths),
    }


def _tool_provenance(project: Project, override: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = project.config.raw
    configured = raw.get("tool", {})
    if not isinstance(configured, Mapping):
        configured = {}
    result: dict[str, Any] = {
        "runtime": "chalk-project-local",
        "python": platform.python_version(),
    }
    result.update({str(key): value for key, value in configured.items()})
    for key in ("chalk_version", "source_commit", "created_with", "template_commit"):
        if key in raw and key not in result:
            result[key] = raw[key]
    if override:
        result.update(override)
    return result


def _take_metadata(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    for candidate in (
        path.with_suffix(".json"),
        path.with_name(f"{path.stem}.take.json"),
        path.with_name(f"{path.stem}.meta.json"),
    ):
        if not candidate.is_file():
            continue
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _transcript_component(root: Path, path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    component = _file_record(root, path)
    if not path.is_file():
        return component
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        cache_key = value.get("cache_key") if isinstance(value, Mapping) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        cache_key = None
    if isinstance(cache_key, str) and re.fullmatch(r"[0-9a-f]{64}", cache_key):
        immutable = root / "transcripts" / "sha256" / f"{cache_key}.json"
        component["cache_key"] = cache_key
        component["immutable"] = _file_record(root, immutable)
    return component


def _context_records(project: Project) -> list[dict[str, Any]]:
    directory = project.root / project.config.context
    if not directory.is_dir():
        return []
    return [
        _file_record(project.root, path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]


def _asset_records(project: Project) -> list[dict[str, Any]]:
    """Bind every project asset while keeping the snapshot human-inspectable."""

    directory = project.root / "assets"
    if not directory.is_dir():
        return []
    return [
        _file_record(project.root, path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]


def _tool_records(project: Project) -> list[dict[str, Any]]:
    """Bind the exact project-local code capable of producing an artifact.

    ``chalk.toml`` records the friendly tool version and source commit, but a
    project-local copy may legitimately be dirty.  Hashing its executable,
    runtime, Python implementation, and dependency/setup locks makes the
    snapshot's claim exact even in that case.
    """

    candidates = [
        project.root / "chalk",
        project.root / "chalk_runtime.py",
        project.root / "requirements.txt",
        project.root / "requirements.lock",
        project.root / "setup.sh",
    ]
    package = project.root / "_chalk"
    if package.is_dir():
        candidates.extend(sorted(package.rglob("*.py")))
    return [_file_record(project.root, path) for path in candidates if path.is_file()]


def _helper_records(project: Project) -> list[dict[str, Any]]:
    """Bind author-owned Python imported by scenes outside the scene directory."""

    excluded_roots = {".chalk", ".venv", "_chalk", "output", "scenes"}
    excluded_files = {project.config.style.as_posix(), "chalk_runtime.py"}
    candidates: list[Path] = []
    for path in sorted(project.root.rglob("*.py")):
        relative = path.relative_to(project.root)
        if relative.parts[0] in excluded_roots or relative.as_posix() in excluded_files:
            continue
        if path.is_file():
            candidates.append(path)
    return [_file_record(project.root, path) for path in candidates]


def _action_closure(project: Project) -> tuple[list[dict[str, Any]], list[str]]:
    """Copy current derivation records into tracked snapshot data.

    The working CAS is intentionally ignored by Git. Embedding these small JSON
    records keeps a snapshot's recipes and environment traceable after a local
    cache cleanup; large output blobs remain reproducible derived data.
    """

    try:
        from .store import Store, StoreError
    except ImportError:
        return [], sorted(set(project.state.current_actions.values()))
    store = Store(project.root)
    records: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    for value in sorted(set(project.state.current_actions.values())):
        key = value if value.startswith("sha256:") else f"sha256:{value}"
        try:
            for record in store.trace(key):
                records[record.key] = record.to_dict()
        except (StoreError, OSError, TypeError, ValueError):
            unresolved.append(key)
    return [records[key] for key in sorted(records)], sorted(set(unresolved))


def _snapshot_payload(
    project: Project,
    *,
    tool: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    actions, unresolved_actions = _action_closure(project)
    components: dict[str, Any] = {
        "config": _file_record(project.root, project.config.path),
        "brief": _file_record(project.root, project.root / project.config.brief),
        "outline": _file_record(project.root, project.root / project.config.outline),
        "feedback": _file_record(project.root, project.root / "feedback.md"),
        "script": {
            "path": project.config.script.as_posix(),
            "sha256": hash_file(project.root / project.config.script),
            "segments": [],
        },
        "style": _file_record(project.root, project.root / project.config.style),
        "assets": _asset_records(project),
        "helpers": _helper_records(project),
        "context": _context_records(project),
        "scenes": [],
        "selections": [],
        "current_actions": dict(sorted(project.state.current_actions.items())),
        "actions": actions,
        "unresolved_action_refs": unresolved_actions,
        "tool_sources": _tool_records(project),
    }

    for segment in project.segments:
        components["script"]["segments"].append(
            {
                "id": segment.id,
                "title": segment.title,
                "order": segment.order,
                "source_line": segment.source_line,
                "facets": segment.facet_hashes(),
            }
        )
        if segment.id:
            components["scenes"].append(
                {
                    "segment": segment.id,
                    **_file_record(project.root, project.scene_path(segment)),
                }
            )

        selected = project.state.selected_takes.get(segment.id)
        record = take_record(project, selected) if isinstance(selected, str) else None
        take = take_path(project, segment.id, selected) if isinstance(selected, str) else None
        transcript = (
            transcript_path(project, segment.id, selected)
            if isinstance(selected, str)
            else None
        )
        if record is not None and hasattr(record, "to_record"):
            take_metadata: Mapping[str, Any] | None = record.to_record()
        else:
            take_metadata = _take_metadata(take)
        components["selections"].append(
            {
                "segment": segment.id,
                "take": {
                    "selected_take_id": selected,
                    "file": _file_record(project.root, take) if take else None,
                    "record": take_metadata,
                },
                "transcript": _transcript_component(project.root, transcript),
            }
        )

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "project": {
            "id": project.config.project_id,
            "title": project.config.title,
            "slug": project.config.slug,
        },
        "components": components,
        "provenance": {
            "tool": _tool_provenance(project, tool),
            "git": git_provenance(project.root),
        },
    }


@dataclass(frozen=True)
class Snapshot:
    digest: str
    path: Path
    data: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"digest": self.digest, "path": str(self.path), **dict(self.data)}


def create_snapshot(
    project: Project | str | os.PathLike[str],
    name: str | None = None,
    *,
    tool: Mapping[str, Any] | None = None,
) -> Snapshot:
    """Materialize one deterministic content snapshot and optional named ref."""

    current = _fresh_project(project)
    payload = _snapshot_payload(current, tool=tool)
    digest = hash_bytes(canonical_json_bytes(payload))
    record = {"digest": digest, **payload}
    path = current.root / ".chalk" / "snapshots" / f"{digest}.json"
    encoded = json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReviewError(f"cannot verify existing snapshot {path}: {error}") from error
        if existing != record:
            raise ReviewError(f"snapshot collision or corruption at {path}")
    else:
        atomic_write_text(path, encoded)

    if name is not None:
        write_snapshot_ref(current, name, digest)
    return Snapshot(digest=digest, path=path, data=record)


snapshot = create_snapshot


def _ref_path(project: Project, name: str) -> Path:
    if not REF_RE.fullmatch(name) or ".." in Path(name).parts:
        raise ReviewError(
            "snapshot ref names use letters, digits, '.', '_', '-', and safe '/' components"
        )
    return project.root / ".chalk" / "refs" / f"{name}.json"


def write_snapshot_ref(project: Project, name: str, digest: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ReviewError("snapshot digest must be a full lowercase SHA-256")
    path = _ref_path(project, name)
    atomic_write_json(path, {"schema_version": 1, "digest": digest})
    return path


def resolve_snapshot(project: Project, digest_or_ref: str) -> Snapshot:
    digest = digest_or_ref
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        path = _ref_path(project, digest_or_ref)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            digest = value["digest"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ReviewError(f"cannot resolve snapshot ref '{digest_or_ref}': {error}") from error
    path = project.root / ".chalk" / "snapshots" / f"{digest}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReviewError(f"cannot read snapshot {digest}: {error}") from error
    if data.get("digest") != digest:
        raise ReviewError(f"snapshot {digest} does not identify itself correctly")
    payload = dict(data)
    payload.pop("digest", None)
    if hash_bytes(canonical_json_bytes(payload)) != digest:
        raise ReviewError(f"snapshot {digest} failed its content hash check")
    return Snapshot(digest=digest, path=path, data=data)


@dataclass(frozen=True)
class ReviewNote:
    id: str
    text: str
    resolved: bool
    segment: str | None
    category: str
    severity: str
    timecode: str | None
    snapshot: str
    artifact: Mapping[str, Any] | None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "resolved": self.resolved,
            "segment": self.segment,
            "category": self.category,
            "severity": self.severity,
            "timecode": self.timecode,
            "snapshot": self.snapshot,
            "artifact": dict(self.artifact) if self.artifact else None,
            "created_at": self.created_at,
        }


def _artifact_record(project: Project, artifact: Any) -> Mapping[str, Any] | None:
    if artifact is None:
        return None
    if isinstance(artifact, Mapping):
        return dict(artifact)
    path = Path(os.fspath(artifact)).expanduser()
    if not path.is_absolute():
        path = project.root / path
    if path.is_file():
        return _file_record(project.root, path)
    return {"ref": os.fspath(artifact)}


def _feedback_path(project: Project) -> Path:
    return project.root / "feedback.md"


def _insert_under_heading(contents: str, heading: str, block: str) -> str:
    """Insert a note block at the end of a human-owned Markdown section."""

    match = re.search(rf"(?m)^{re.escape(heading)}\s*$", contents)
    if match is None:
        if contents and not contents.endswith("\n"):
            contents += "\n"
        contents += f"\n{heading}\n\n"
        match = re.search(rf"(?m)^{re.escape(heading)}\s*$", contents)
        assert match is not None
    next_heading = re.search(r"(?m)^##\s+", contents[match.end() :])
    insertion = (
        match.end() + next_heading.start()
        if next_heading is not None
        else len(contents)
    )
    before = contents[:insertion].rstrip() + "\n\n"
    after = contents[insertion:].lstrip("\n")
    return before + block.rstrip() + "\n\n" + after


def append_note(
    project: Project | str | os.PathLike[str],
    text: str,
    *,
    segment: str | None = None,
    category: str = "general",
    severity: str = "normal",
    timecode: str | None = None,
    snapshot_digest: str | None = None,
    artifact: Any = None,
    note_id: str | None = None,
) -> ReviewNote:
    """Append a readable feedback item bound to the exact current snapshot."""

    current = _fresh_project(project)
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ReviewError("feedback text must not be empty")
    if segment is not None:
        current.segment(segment)
    if timecode is not None and not TIMECODE_RE.fullmatch(timecode):
        raise ReviewError(f"invalid timecode '{timecode}'")
    if snapshot_digest is None:
        snapshot_digest = create_snapshot(current).digest
    else:
        snapshot_digest = resolve_snapshot(current, snapshot_digest).digest

    metadata: dict[str, Any] = {
        "artifact": _artifact_record(current, artifact),
        "category": category,
        "created_at": _now(),
        "segment": segment,
        "severity": severity,
        "snapshot": snapshot_digest,
        "text": cleaned,
        "timecode": timecode,
    }
    from .model import project_lock

    with project_lock(current.root, "feedback"):
        existing_ids = {
            note.id for note in list_notes(current, include_resolved=True)
        }
        if note_id is None:
            attempt = 0
            while True:
                identity = {**metadata, "attempt": attempt}
                note_id = f"note-{hash_bytes(canonical_json_bytes(identity))[:12]}"
                if note_id not in existing_ids:
                    break
                attempt += 1
        if not NOTE_ID_RE.fullmatch(note_id):
            raise ReviewError("note ids use letters, digits, '.', '_', and '-'")
        if note_id in existing_ids:
            raise ReviewError(f"feedback note '{note_id}' already exists")
        metadata["id"] = note_id

        path = _feedback_path(current)
        if path.exists():
            contents = path.read_text(encoding="utf-8")
        else:
            contents = "# Feedback\n\n## Open\n\n## Resolved\n\n## Decisions\n"
        meta_json = json.dumps(
            metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        block = f"- [ ] **{note_id}** — {cleaned}\n  <!-- chalk:note {meta_json} -->"
        contents = _insert_under_heading(contents, "## Open", block)
        atomic_write_text(path, contents)
    return ReviewNote(
        id=note_id,
        text=cleaned,
        resolved=False,
        segment=segment,
        category=category,
        severity=severity,
        timecode=timecode,
        snapshot=snapshot_digest,
        artifact=metadata["artifact"],
        created_at=metadata["created_at"],
    )


note_add = append_note


def list_notes(
    project: Project | str | os.PathLike[str], *, include_resolved: bool = False
) -> list[ReviewNote]:
    current = _fresh_project(project)
    path = _feedback_path(current)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    notes: list[ReviewNote] = []
    for index, line in enumerate(lines):
        match = NOTE_LINE_RE.match(line)
        if not match:
            continue
        resolved = match.group(1).lower() == "x"
        note_id = match.group(2).strip()
        metadata: Mapping[str, Any] | None = None
        cursor = index + 1
        while cursor < len(lines) and not NOTE_LINE_RE.match(lines[cursor]):
            meta = NOTE_META_RE.match(lines[cursor])
            if meta:
                try:
                    loaded = json.loads(meta.group(1))
                    if isinstance(loaded, Mapping):
                        metadata = loaded
                except json.JSONDecodeError:
                    pass
                break
            cursor += 1
        if resolved and not include_resolved:
            continue
        visible_text = (match.group(3) or "").strip()
        metadata = metadata or {}
        note_text = visible_text or str(metadata.get("text", note_id))
        tagged_severity = re.match(
            r"^\[(blocker|blocking|bug|note|normal)\]\s*", note_text, re.I
        )
        severity = str(metadata.get("severity", "normal"))
        if tagged_severity:
            severity = tagged_severity.group(1).casefold()
            if severity == "blocking":
                severity = "blocker"
        notes.append(
            ReviewNote(
                id=note_id,
                text=note_text,
                resolved=resolved,
                segment=(
                    metadata.get("segment")
                    if isinstance(metadata.get("segment"), str)
                    else None
                ),
                category=str(metadata.get("category", "general")),
                severity=severity,
                timecode=(
                    metadata.get("timecode")
                    if isinstance(metadata.get("timecode"), str)
                    else None
                ),
                snapshot=str(metadata.get("snapshot", "")),
                artifact=(
                    metadata.get("artifact")
                    if isinstance(metadata.get("artifact"), Mapping)
                    else None
                ),
                created_at=(
                    metadata.get("created_at")
                    if isinstance(metadata.get("created_at"), str)
                    else None
                ),
            )
        )
    return notes


note_list = list_notes


def _resolve_note_unlocked(
    current: Project,
    note_id: str,
    *,
    resolution: str | None = None,
) -> ReviewNote:
    path = _feedback_path(current)
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ReviewError("feedback.md has no notes") from error
    pattern = re.compile(
        rf"(?ms)^- \[ \] \*\*{re.escape(note_id)}\*\*.*?"
        rf"(?=^- \[[ xX]\] \*\*|^##\s+|\Z)"
    )
    match = pattern.search(contents)
    if match is None:
        all_notes = {note.id: note for note in list_notes(current, include_resolved=True)}
        if note_id in all_notes and all_notes[note_id].resolved:
            return all_notes[note_id]
        raise ReviewError(f"open feedback note '{note_id}' not found")
    block = match.group(0).strip()
    block = block.replace(f"- [ ] **{note_id}**", f"- [x] **{note_id}**", 1)
    cleaned_resolution = " ".join((resolution or "").split())
    if cleaned_resolution:
        block_lines = block.splitlines()
        metadata_line = next(
            (
                index
                for index, line in enumerate(block_lines)
                if NOTE_META_RE.match(line)
            ),
            0,
        )
        block_lines.insert(metadata_line + 1, f"  Resolution: {cleaned_resolution}")
        block = "\n".join(block_lines)
    updated = contents[: match.start()] + contents[match.end() :]
    updated = _insert_under_heading(updated, "## Resolved", block)
    atomic_write_text(path, updated)
    return next(
        note
        for note in list_notes(current, include_resolved=True)
        if note.id == note_id
    )


def resolve_note(
    project: Project | str | os.PathLike[str],
    note_id: str,
    *,
    resolution: str | None = None,
) -> ReviewNote:
    from .model import project_lock

    current = _fresh_project(project)
    with project_lock(current.root, "feedback"):
        return _resolve_note_unlocked(
            _fresh_project(current), note_id, resolution=resolution
        )


note_resolve = resolve_note


def _approval_payload(project: Project, segment_id: str | None) -> dict[str, Any]:
    snapshot_payload = _snapshot_payload(project)
    components = snapshot_payload["components"]
    if segment_id is None:
        return {
            "scope": "project",
            "config": components["config"],
            "brief": components["brief"],
            "outline": components["outline"],
            "script": components["script"],
            "style": components["style"],
            "assets": components["assets"],
            "helpers": components["helpers"],
            "context": components["context"],
            "scenes": components["scenes"],
            "selections": components["selections"],
            "current_actions": components["current_actions"],
        }

    project.segment(segment_id)
    script = next(
        item for item in components["script"]["segments"] if item["id"] == segment_id
    )
    scene = next(item for item in components["scenes"] if item["segment"] == segment_id)
    selection = next(
        item for item in components["selections"] if item["segment"] == segment_id
    )
    actions = {
        key: value
        for key, value in components["current_actions"].items()
        if segment_id in re.split(r"[:/]", key)
    }
    return {
        "scope": f"segment:{segment_id}",
        "script": script,
        "scene": scene,
        "style": components["style"],
        "assets": components["assets"],
        "helpers": components["helpers"],
        "selection": selection,
        "current_actions": actions,
    }


def approval_fingerprint(project: Project, segment_id: str | None = None) -> str:
    return hash_bytes(canonical_json_bytes(_approval_payload(project, segment_id)))


@dataclass(frozen=True)
class ApprovalStatus:
    scope: str
    state: str
    recorded_hash: str | None
    current_hash: str
    snapshot: str | None

    @property
    def current(self) -> bool:
        return self.state == "current"


def approve(
    project: Project | str | os.PathLike[str],
    segment_id: str | None = None,
    *,
    snapshot_digest: str | None = None,
) -> ApprovalStatus:
    from .model import project_lock

    initial = _fresh_project(project)
    with project_lock(initial.root, "state"):
        current = _fresh_project(initial)
        if segment_id is not None:
            current.segment(segment_id)
        if snapshot_digest is None:
            snapshot_digest = create_snapshot(current).digest
        else:
            snapshot_digest = resolve_snapshot(current, snapshot_digest).digest
        key = f"segment:{segment_id}" if segment_id else "project"
        fingerprint = approval_fingerprint(current, segment_id)
        current.state.approvals[key] = {
            "content_hash": fingerprint,
            "snapshot": snapshot_digest,
        }
        save_state(current.root, current.state)
        return ApprovalStatus(
            key, "current", fingerprint, fingerprint, snapshot_digest
        )


def approval_status(
    project: Project | str | os.PathLike[str], segment_id: str | None = None
) -> ApprovalStatus:
    current = _fresh_project(project)
    if segment_id is not None:
        current.segment(segment_id)
    key = f"segment:{segment_id}" if segment_id else "project"
    value = current.state.approvals.get(key)
    fingerprint = approval_fingerprint(current, segment_id)
    if not isinstance(value, Mapping):
        return ApprovalStatus(key, "missing", None, fingerprint, None)
    recorded = value.get("content_hash")
    snapshot_digest = value.get("snapshot")
    state = "current" if recorded == fingerprint else "stale"
    return ApprovalStatus(
        key,
        state,
        recorded if isinstance(recorded, str) else None,
        fingerprint,
        snapshot_digest if isinstance(snapshot_digest, str) else None,
    )


def _expand_context_paths(paths: Iterable[str | os.PathLike[str]]) -> list[Path]:
    expanded: list[Path] = []
    for value in paths:
        path = Path(value).expanduser().resolve()
        if path.is_dir():
            expanded.extend(
                candidate
                for candidate in sorted(path.rglob("*"))
                if candidate.is_file()
                and ".git" not in candidate.parts
                and ".chalk" not in candidate.parts
            )
        elif path.is_file():
            expanded.append(path)
        else:
            raise ReviewError(f"context source does not exist: {path}")
    unique = sorted(set(expanded))
    if not unique:
        raise ReviewError("no context files selected")
    return unique


def _source_root(files: Sequence[Path]) -> Path:
    common = Path(os.path.commonpath([str(path) for path in files]))
    return common if common.is_dir() else common.parent


def _source_git_provenance(root: Path) -> dict[str, Any]:
    top = _git_output(root, "rev-parse", "--show-toplevel")
    if top is None:
        return {"root": None, "head": None, "dirty": None}
    repo = Path(top)
    status = (
        _git_output(
            repo,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            strip=False,
        )
        or ""
    )
    return {
        "root": str(repo),
        "head": _git_output(repo, "rev-parse", "HEAD"),
        "dirty": bool(status),
    }


def _markdown_fence(text: str) -> str:
    runs = [len(match.group(0)) for match in re.finditer(r"`+", text)]
    return "`" * max(3, (max(runs) + 1) if runs else 3)


def _language(path: Path) -> str:
    return {
        ".md": "markdown",
        ".py": "python",
        ".toml": "toml",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".sh": "bash",
        ".txt": "text",
    }.get(path.suffix.lower(), "text")


@dataclass(frozen=True)
class ContextPack:
    digest: str
    path: Path
    index_path: Path
    files: tuple[Mapping[str, Any], ...]


def context_add(
    project: Project | str | os.PathLike[str],
    paths: Iterable[str | os.PathLike[str]],
    *,
    name: str | None = None,
) -> ContextPack:
    """Pin selected reference files into one content-addressed Markdown pack."""

    current = _fresh_project(project)
    files = _expand_context_paths(paths)
    source_root = _source_root(files)
    git = _source_git_provenance(source_root)
    records: list[dict[str, Any]] = []
    contents: list[tuple[Path, str]] = []
    for path in files:
        try:
            source_bytes = path.read_bytes()
            text = source_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ReviewError(f"context packs currently require UTF-8 text: {path}") from error
        relative = path.relative_to(source_root).as_posix()
        records.append(
            {
                "path": relative,
                "source_path": str(path),
                "sha256": hash_bytes(source_bytes),
                "size": len(source_bytes),
            }
        )
        contents.append((path, text))

    manifest = {
        "schema_version": 1,
        "source_root": str(source_root),
        "git": git,
        "files": records,
    }
    digest = hash_bytes(canonical_json_bytes(manifest))
    context_dir = current.root / current.config.context
    pack_path = context_dir / f"{digest}.md"
    pack_title = source_root.name or "reference"
    title = name or pack_title
    lines = [
        f"# Context: {pack_title}",
        "",
        f"- Pack: `{digest}`",
        f"- Source: `{source_root}`",
        f"- Git HEAD: `{git['head'] or 'unavailable'}`",
        f"- Source dirty: `{git['dirty']}`",
        "",
        (
            "<!-- chalk:context "
            + json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + " -->"
        ),
        "",
    ]
    for (path, text), record in zip(contents, records, strict=True):
        fence = _markdown_fence(text)
        lines.extend(
            [
                f"## `{record['path']}`",
                "",
                f"Source: `{record['source_path']}`  ",
                f"SHA-256: `{record['sha256']}`",
                "",
                f"{fence}{_language(path)}",
                text.rstrip("\n"),
                fence,
                "",
            ]
        )
    rendered = "\n".join(lines).rstrip() + "\n"
    if pack_path.exists():
        if pack_path.read_text(encoding="utf-8") != rendered:
            raise ReviewError(f"context pack collision or corruption at {pack_path}")
    else:
        atomic_write_text(pack_path, rendered)

    from .model import project_lock

    index_path = context_dir / "index.md"
    with project_lock(current.root, "context"):
        if index_path.exists():
            index = index_path.read_text(encoding="utf-8")
            if index and not index.endswith("\n"):
                index += "\n"
        else:
            index = "# Pinned context\n\n"
        entry = f"- [{title}]({digest}.md) — `{digest[:12]}` from `{source_root}`"
        if entry not in index.splitlines():
            index += entry + "\n"
            atomic_write_text(index_path, index)

    return ContextPack(digest, pack_path, index_path, tuple(records))


__all__ = [
    "ApprovalStatus",
    "ContextPack",
    "ReviewError",
    "ReviewNote",
    "Snapshot",
    "append_note",
    "approval_fingerprint",
    "approval_status",
    "approve",
    "context_add",
    "create_snapshot",
    "git_provenance",
    "list_notes",
    "note_add",
    "note_list",
    "note_resolve",
    "resolve_note",
    "resolve_snapshot",
    "snapshot",
    "write_snapshot_ref",
]
