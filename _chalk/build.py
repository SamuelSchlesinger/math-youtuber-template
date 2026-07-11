"""Incremental Manim/ffmpeg orchestration over the local action store.

This module is intentionally not a renderer.  It computes exact action inputs,
runs ordinary Manim and ffmpeg commands in isolated directories, and publishes
verified outputs through :mod:`_chalk.store`.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .store import (
    ActionProducts,
    ActionRecord,
    Store,
    action_key,
    canonical_json,
    hash_bytes,
    hash_file,
    hash_tree,
)


RENDER_RECIPE = {
    "id": "chalk-manim-render-v2",
    "scene_class": "Visual",
    "output": "h264-mp4",
    "validation": "ffprobe-count-frames-v1",
}
SEGMENT_RECIPE = {
    "id": "chalk-segment-mux-v2",
    "audio_codec": "aac",
    "audio_bitrate": "192k",
    "audio_rate": 48_000,
    "video_codec": "libx264",
    "pixel_format": "yuv420p",
    "audio_padding": "apad",
    "video_padding": "clone-last-frame",
    "validation": "ffprobe-count-frames-v1",
}
FULL_RECIPE = {
    "id": "chalk-full-compose-v3",
    "video_codec": "libx264",
    "preset": "slow",
    "pixel_format": "yuv420p",
    "audio_codec": "aac",
    "audio_bitrate": "192k",
    "loudness": {
        "filter": "loudnorm",
        "mode": "two-pass-linear",
        "integrated_lufs": -14,
        "true_peak": -1,
        "lra": 11,
        "analysis_format": "json",
    },
    "validation": "ffprobe-count-frames-v1",
}

RENDER_ENVIRONMENT_KEYS = (
    "python",
    "manim_dependencies",
    "requirements_lock",
    "platform",
    "runtime",
    "manim",
    "ffmpeg",
    "ffprobe",
    "cairo",
    "pango",
    "pangocairo",
    "latex",
    "dvisvgm",
    "fontconfig",
    "media_validator",
)
FFMPEG_ENVIRONMENT_KEYS = (
    "ffmpeg",
    "ffprobe",
    "platform",
    "media_validator",
)


class BuildError(RuntimeError):
    """A requested artifact could not be built or validated."""


class _RenderSourcesChanged(BuildError):
    pass


class CommandExecutor(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        expected_output: Path,
        env: Mapping[str, str] | None = None,
    ) -> Any: ...

    def capture(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class MediaRequirements:
    kind: str
    width: int
    height: int
    fps: int
    duration_us: int
    require_audio: bool


class MediaValidator(Protocol):
    def validate(self, path: Path, requirements: MediaRequirements) -> None: ...


class FFprobeValidator:
    """Reject incomplete, malformed, or structurally wrong media outputs."""

    def __init__(
        self,
        executable: str = "ffprobe",
        *,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self.executable = executable
        self.runner = runner or subprocess.run

    @staticmethod
    def _frame_count(stream: Mapping[str, Any]) -> int:
        for key in ("nb_read_frames", "nb_frames"):
            value = stream.get(key)
            if value not in (None, "", "N/A"):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return 0

    @staticmethod
    def _duration_seconds(
        payload: Mapping[str, Any], stream: Mapping[str, Any]
    ) -> float:
        candidates = (
            _get(payload.get("format", {}), "duration"),
            stream.get("duration"),
        )
        for value in candidates:
            if value not in (None, "", "N/A"):
                try:
                    duration = float(value)
                except (TypeError, ValueError):
                    continue
                if duration > 0:
                    return duration
        return 0.0

    @staticmethod
    def _fps(stream: Mapping[str, Any]) -> float:
        for key in ("avg_frame_rate", "r_frame_rate"):
            value = stream.get(key)
            if value not in (None, "", "N/A", "0/0"):
                try:
                    result = float(Fraction(str(value)))
                except (ValueError, ZeroDivisionError):
                    continue
                if result > 0:
                    return result
        return 0.0

    def validate(self, path: Path, requirements: MediaRequirements) -> None:
        try:
            result = self.runner(
                [
                    self.executable,
                    "-v",
                    "error",
                    "-count_frames",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(path),
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            raise BuildError(f"could not run {self.executable}: {error}") from error
        stderr = str(getattr(result, "stderr", "") or "").strip()
        if getattr(result, "returncode", 1) != 0 or stderr:
            detail = stderr or "probe command failed"
            raise BuildError(f"invalid {requirements.kind} output: {detail}")
        try:
            payload = json.loads(str(getattr(result, "stdout", "")))
        except json.JSONDecodeError as error:
            raise BuildError(
                f"invalid {requirements.kind} output: malformed ffprobe response"
            ) from error
        if not isinstance(payload, Mapping):
            raise BuildError(
                f"invalid {requirements.kind} output: ffprobe root is not an object"
            )
        streams = payload.get("streams", ())
        if not isinstance(streams, Sequence) or isinstance(streams, (str, bytes)):
            streams = ()
        video = next(
            (
                stream
                for stream in streams
                if isinstance(stream, Mapping) and stream.get("codec_type") == "video"
            ),
            None,
        )
        if video is None or self._frame_count(video) <= 0:
            raise BuildError(
                f"invalid {requirements.kind} output: no decodable video frames"
            )
        try:
            width = int(video.get("width", 0))
            height = int(video.get("height", 0))
        except (TypeError, ValueError):
            width = height = 0
        if width != requirements.width or height != requirements.height:
            raise BuildError(
                f"invalid {requirements.kind} output: expected "
                f"{requirements.width}x{requirements.height} video"
            )
        actual_fps = self._fps(video)
        fps_tolerance = max(0.05, requirements.fps * 0.002)
        if abs(actual_fps - requirements.fps) > fps_tolerance:
            raise BuildError(
                f"invalid {requirements.kind} output: expected {requirements.fps} fps, "
                f"got {actual_fps:g}"
            )
        if requirements.require_audio:
            audio = next(
                (
                    stream
                    for stream in streams
                    if isinstance(stream, Mapping)
                    and stream.get("codec_type") == "audio"
                ),
                None,
            )
            if audio is None or self._frame_count(audio) <= 0:
                raise BuildError(
                    f"invalid {requirements.kind} output: no decodable audio frames"
                )
        duration = self._duration_seconds(payload, video)
        expected_duration = requirements.duration_us / 1_000_000
        duration_tolerance = max(
            0.5 if requirements.kind == "render" else 0.75,
            3 / requirements.fps,
        )
        if abs(duration - expected_duration) > duration_tolerance:
            raise BuildError(
                f"invalid {requirements.kind} output: expected about "
                f"{expected_duration:.3f}s, got {duration:.3f}s"
            )


class SubprocessExecutor:
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        expected_output: Path,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del expected_output
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            check=True,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def capture(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            check=False,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


@dataclass(frozen=True)
class RenderProfile:
    name: str
    width: int
    height: int
    fps: int
    manim_quality: str
    video_crf: int
    lead_in_us: int
    tail_us: int
    speech_wpm: float
    extra_manim_args: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "manim_quality": self.manim_quality,
            "video_crf": self.video_crf,
            "lead_in_us": self.lead_in_us,
            "tail_us": self.tail_us,
            "speech_wpm": self.speech_wpm,
            "extra_manim_args": list(self.extra_manim_args),
        }


@dataclass(frozen=True)
class Artifact:
    kind: str
    action_key: str
    blob_id: str
    path: Path
    cache_hit: bool
    profile: str
    segment_id: str | None = None
    duration_us: int | None = None
    timing: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def current(self) -> bool:
        return True

    @property
    def artifact_id(self) -> str:
        return self.action_key

    @property
    def key(self) -> str:
        return self.action_key

    @property
    def blob(self) -> str:
        return self.blob_id

    @property
    def segment(self) -> str | None:
        return self.segment_id

    @property
    def duration(self) -> float:
        return (self.duration_us or 0) / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "blob": self.blob,
            "actionKey": self.action_key,
            "artifactId": self.action_key,
            "blobId": self.blob_id,
            "path": self.path.as_posix(),
            "exists": self.path.is_file(),
            "current": self.current,
            "cacheHit": self.cache_hit,
            "cache_hit": self.cache_hit,
            "profile": self.profile,
            "segmentId": self.segment_id,
            "segment": self.segment,
            "duration": self.duration,
            "durationSeconds": (self.duration_us or 0) / 1_000_000,
            "timing": self.timing,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class BuildResult:
    artifacts: tuple[Artifact, ...]
    final: Artifact | None = None

    @property
    def cache_hits(self) -> int:
        values = (*self.artifacts, *((self.final,) if self.final else ()))
        return sum(artifact.cache_hit for artifact in values)


@dataclass(frozen=True)
class ReleaseResult:
    name: str
    artifact: Artifact
    manifest_path: Path
    media_path: Path
    snapshot: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "artifact": self.artifact.to_dict(),
            "manifest": self.manifest_path.as_posix(),
            "media": self.media_path.as_posix(),
            "snapshot": self.snapshot,
        }


@dataclass(frozen=True)
class _TimelineInputs:
    record: Mapping[str, Any]
    digest: str
    duration_us: int
    lead_in_us: int
    source: str
    take: Any = None
    audio_path: Path | None = None
    warnings: tuple[str, ...] = ()


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _raw_config(project: Any) -> Mapping[str, Any]:
    config = _get(project, "config", {})
    raw = _get(config, "raw", config)
    return raw if isinstance(raw, Mapping) else {}


def _shared_python_hash(root: Path) -> str:
    """Hash project helpers without walking caches or segment-local scenes.

    Segment scenes (``scenes/<id>.py``) are hashed per segment, so they stay out
    of this shared fingerprint. Shared scene helpers that every render can import
    (``scenes/__init__.py`` and ``scenes/_*.py``) are not segment-local — editing
    one changes what every scene renders — so they belong here. This is the exact
    set validation permits in ``scenes/`` without a script segment marker.
    """

    excluded = {
        ".chalk",
        ".git",
        ".venv",
        "_chalk",
        "assets",
        "media",
        "output",
        "releases",
        "scenes",
        "transcripts",
    }
    records: list[dict[str, str]] = []
    for directory, names, files in os.walk(root):
        relative_directory = Path(directory).relative_to(root)
        names[:] = sorted(name for name in names if name not in excluded)
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix != ".py":
                continue
            relative = (relative_directory / name).as_posix()
            records.append({"path": relative, "hash": hash_file(path)})
    scenes_dir = root / "scenes"
    if scenes_dir.is_dir():
        for path in sorted(scenes_dir.glob("*.py")):
            if path.name == "__init__.py" or path.name.startswith("_"):
                relative = path.relative_to(root).as_posix()
                records.append({"path": relative, "hash": hash_file(path)})
    records.sort(key=lambda record: record["path"])
    return hash_bytes(canonical_json(records))


def _merge_current_action(root: Path, key: str, action_key: str) -> None:
    """Persist one ``current_actions`` pointer without clobbering concurrent edits.

    Takes the cross-process state lock, reloads a fresh project, merges only this
    single key, and writes the result back. A whole-state write would drop a
    take-selection or approval another process committed after we loaded.
    """

    from .model import load_project, project_lock, save_state

    with project_lock(root, "state"):
        current = load_project(root)
        current.state.current_actions[key] = action_key
        save_state(root, current.state)


def load_profile(project: Any, name: str | None = None) -> RenderProfile:
    raw = _raw_config(project)
    settings = raw.get("settings", {})
    if not isinstance(settings, Mapping):
        settings = {}
    profiles = raw.get("profiles", {})
    if not isinstance(profiles, Mapping):
        raise BuildError("chalk.toml [profiles] must be a table")
    if name is None:
        name = str(settings.get("default_profile", raw.get("default_profile", "draft")))
    value = profiles.get(name)
    if not isinstance(value, Mapping):
        raise BuildError(f"chalk.toml has no explicit profile {name!r}")

    def positive_integer(key: str) -> int:
        result = int(value.get(key, 0))
        if result <= 0:
            raise BuildError(f"profile {name!r} {key} must be positive")
        return result

    lead_in = float(
        value.get(
            "lead_in_seconds",
            settings.get("lead_in_seconds", raw.get("lead_in_seconds", 0.6)),
        )
    )
    tail = float(
        value.get(
            "tail_seconds",
            settings.get("tail_seconds", raw.get("tail_seconds", 0.35)),
        )
    )
    wpm = float(
        value.get("speech_wpm", settings.get("speech_wpm", raw.get("speech_wpm", 150)))
    )
    if lead_in < 0 or tail < 0 or wpm <= 0:
        raise BuildError(f"profile {name!r} has invalid timing settings")
    extra = value.get("manim_args", ())
    if not isinstance(extra, Sequence) or isinstance(extra, (str, bytes)):
        raise BuildError(f"profile {name!r} manim_args must be an array")
    return RenderProfile(
        name=name,
        width=positive_integer("width"),
        height=positive_integer("height"),
        fps=positive_integer("fps"),
        manim_quality=str(value.get("manim_quality", "-ql")),
        video_crf=int(value.get("video_crf", 23)),
        lead_in_us=round(lead_in * 1_000_000),
        tail_us=round(tail * 1_000_000),
        speech_wpm=wpm,
        extra_manim_args=tuple(str(argument) for argument in extra),
    )


def _tool_identity(
    executable: str,
    arguments: Sequence[str],
    *,
    timeout: int = 10,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [executable, *arguments],
            check=False,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "available": False,
            "error": type(error).__name__,
            "output_sha256": hash_bytes(b""),
            "output": "",
        }
    output = result.stdout or ""
    return {
        "available": result.returncode == 0,
        "returncode": result.returncode,
        "output_sha256": hash_bytes(output.encode("utf-8")),
        "output": output,
    }


def _executable_identity(
    executable: str,
    arguments: Sequence[str],
    *,
    timeout: int = 10,
) -> dict[str, Any]:
    """Fingerprint the configured command, including its resolved file bytes."""

    identity = _tool_identity(executable, arguments, timeout=timeout)
    identity["executable"] = executable
    resolved = shutil.which(executable)
    if resolved is None:
        candidate = Path(executable).expanduser()
        resolved = str(candidate.resolve()) if candidate.is_file() else None
    identity["binary_sha256"] = (
        hash_file(resolved) if resolved is not None and Path(resolved).is_file() else None
    )
    return identity


def _manim_dependency_versions() -> list[dict[str, str]]:
    """Versions of manim and its transitive (non-extra) dependency closure.

    A render's pixels depend on manim and the packages it pulls in — numpy,
    Pillow, pycairo, and so on — not on unrelated packages that merely share the
    environment. Fingerprinting the whole installed inventory made any unrelated
    ``pip install`` invalidate every cached render; walking manim's own closure
    keeps the fingerprint relevant while still catching a real dependency bump.
    """

    seen: dict[str, str] = {}
    frontier: list[str] = ["manim"]
    while frontier:
        raw = frontier.pop()
        name = re.split(r"[<>=!~;,\[\(\s]", raw, maxsplit=1)[0]
        name = name.strip().lower().replace("_", "-")
        if not name or name in seen:
            continue
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            seen[name] = "unavailable"
            continue
        seen[name] = str(distribution.version)
        for requirement in distribution.requires or []:
            # Skip optional extras: a project that does not install them cannot
            # have its render affected by them.
            if "extra ==" in requirement or "extra==" in requirement:
                continue
            frontier.append(requirement)
    return sorted(
        ({"name": name, "version": version} for name, version in seen.items()),
        key=lambda item: item["name"],
    )


def _fontconfig_identity() -> dict[str, Any]:
    identity = _tool_identity("fc-list", (), timeout=20)
    output = str(identity.pop("output", ""))
    lines = sorted(line for line in output.splitlines() if line)
    identity["font_count"] = len(lines)
    identity["inventory_sha256"] = hash_bytes(canonical_json(lines))
    return identity


_ENVIRONMENT_CACHE: dict[str, dict[str, Any]] = {}
_ENVIRONMENT_CACHE_LOCK = threading.Lock()


def _reset_environment_cache() -> None:
    """Drop the per-process environment fingerprint cache (used by tests)."""

    with _ENVIRONMENT_CACHE_LOCK:
        _ENVIRONMENT_CACHE.clear()


def default_environment(
    project_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    # The environment fingerprint runs ~10 tool subprocesses and walks manim's
    # dependency closure. The local server rebuilds a BuildEngine on every
    # /api/project request, so memoize per resolved root: a page load or a
    # cross-origin GET must not restart that whole probe each time. A toolchain
    # change is picked up on the next process, the right cadence for one session.
    root = Path(project_root).resolve() if project_root is not None else None
    cache_key = str(root) if root is not None else None
    if cache_key is not None:
        with _ENVIRONMENT_CACHE_LOCK:
            cached = _ENVIRONMENT_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)
    try:
        manim_package = importlib.metadata.version("manim")
    except importlib.metadata.PackageNotFoundError:
        manim_package = "unavailable"
    requirements_lock = root / "requirements.lock" if root is not None else None
    environment: dict[str, Any] = {
        "python": platform.python_version(),
        "manim_dependencies": _manim_dependency_versions(),
        "requirements_lock": (
            hash_file(requirements_lock)
            if requirements_lock is not None and requirements_lock.is_file()
            else None
        ),
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "manim": {
            "package": manim_package,
            "command": _executable_identity("manim", ("--version",)),
        },
        "ffmpeg": _executable_identity("ffmpeg", ("-version",)),
        "ffprobe": _executable_identity("ffprobe", ("-version",)),
        "cairo": _tool_identity("pkg-config", ("--modversion", "cairo")),
        "pango": _tool_identity("pkg-config", ("--modversion", "pango")),
        "pangocairo": _tool_identity(
            "pkg-config", ("--modversion", "pangocairo")
        ),
        "latex": _tool_identity("latex", ("--version",)),
        "dvisvgm": _tool_identity("dvisvgm", ("--version",)),
        "fontconfig": _fontconfig_identity(),
        "runtime": "chalk-runtime-v1",
    }
    if cache_key is not None:
        with _ENVIRONMENT_CACHE_LOCK:
            _ENVIRONMENT_CACHE[cache_key] = dict(environment)
    return environment


class BuildEngine:
    def __init__(
        self,
        project: Any,
        *,
        store: Store | None = None,
        executor: CommandExecutor | Callable[..., Any] | None = None,
        validator: MediaValidator
        | Callable[[Path, MediaRequirements], None]
        | None = None,
        environment: Mapping[str, Any] | None = None,
        max_workers: int | None = None,
        defer_state: bool = False,
    ) -> None:
        self.project = project
        self.root = Path(_get(project, "root")).resolve()
        self.store = store or Store(self.root)
        self.executor = executor or SubprocessExecutor()
        tools = _raw_config(project).get("tools", {})
        ffprobe = (
            str(tools.get("ffprobe", "ffprobe"))
            if isinstance(tools, Mapping)
            else "ffprobe"
        )
        self.validator = validator or FFprobeValidator(ffprobe)
        self.environment = dict(environment or default_environment(self.root))
        if environment is None and isinstance(tools, Mapping):
            manim_identity = self.environment.get("manim", {})
            manim_package = (
                manim_identity.get("package")
                if isinstance(manim_identity, Mapping)
                else "unavailable"
            )
            if "manim" in tools:
                manim = str(tools["manim"])
                self.environment["manim"] = {
                    "package": manim_package,
                    "command": _executable_identity(manim, ("--version",)),
                }
            if "ffmpeg" in tools:
                self.environment["ffmpeg"] = _executable_identity(
                    str(tools["ffmpeg"]), ("-version",)
                )
            if "ffprobe" in tools:
                self.environment["ffprobe"] = _executable_identity(
                    str(tools["ffprobe"]), ("-version",)
                )
        if isinstance(self.validator, FFprobeValidator):
            validator_identity = {
                "kind": "ffprobe-count-frames-v1",
                "executable": self.validator.executable,
            }
        else:
            validator_type = type(self.validator)
            validator_module = getattr(
                self.validator,
                "__module__",
                validator_type.__module__,
            )
            validator_name = getattr(
                self.validator,
                "__qualname__",
                validator_type.__qualname__,
            )
            validator_identity = {
                "kind": "injected",
                "type": f"{validator_module}.{validator_name}",
            }
        self.environment.setdefault("media_validator", validator_identity)
        settings = _raw_config(project).get("settings", {})
        configured_jobs = (
            settings.get("render_jobs") if isinstance(settings, Mapping) else None
        )
        self.max_workers = max(
            1,
            int(max_workers or configured_jobs or min(4, os.cpu_count() or 1)),
        )
        self.defer_state = defer_state
        self._state_lock = threading.Lock()
        self._shared_inputs_lock = threading.Lock()
        self._shared_inputs_cache: dict[str, Any] | None = None

    def segment(self, segment_id: str) -> Any:
        method = getattr(self.project, "segment", None)
        if callable(method):
            return method(segment_id)
        for segment in _get(self.project, "segments", ()):
            if str(_get(segment, "id", "")) == segment_id:
                return segment
        raise BuildError(f"unknown segment {segment_id!r}")

    def ordered_segments(self) -> tuple[Any, ...]:
        return tuple(
            sorted(
                _get(self.project, "segments", ()),
                key=lambda item: int(_get(item, "order", 0)),
            )
        )

    def scene_path(self, segment: Any) -> Path:
        method = getattr(self.project, "scene_path", None)
        if callable(method):
            path = Path(method(segment))
        else:
            source = _get(segment, "source", Path("scenes") / f"{_get(segment, 'id')}.py")
            path = Path(source)
            if not path.is_absolute():
                path = self.root / path
        try:
            path.resolve().relative_to(self.root)
        except ValueError as error:
            raise BuildError(f"scene source escapes the project: {path}") from error
        if not path.is_file():
            raise BuildError(f"missing scene source: {path}")
        return path

    def _profile(self, profile: str | RenderProfile | None) -> RenderProfile:
        return (
            profile
            if isinstance(profile, RenderProfile)
            else load_profile(self.project, profile)
        )

    def _action_environment(self, keys: Sequence[str]) -> dict[str, Any]:
        return {key: self.environment.get(key) for key in keys}

    def _timeline(self, segment: Any, profile: RenderProfile, *, draft: bool) -> _TimelineInputs:
        from .audio import AudioStore
        from .timeline import resolve_timeline

        segment_id = str(_get(segment, "id"))
        narration = str(_get(segment, "narration", ""))
        audio = AudioStore(self.root)
        selected_take = audio.selected_take(segment_id)
        take_is_stale = selected_take is not None and selected_take.is_stale_for(
            narration
        )
        take = None if take_is_stale else selected_take
        transcript = (
            audio.load_transcript(take.audio_sha256) if take is not None else None
        )
        if not draft and selected_take is None:
            raise BuildError(f"segment {segment_id!r} has no selected voice take")
        if not draft and take_is_stale:
            raise BuildError(
                f"segment {segment_id!r} selected take is stale for its narration"
            )
        if not draft and transcript is None:
            raise BuildError(f"segment {segment_id!r} has no current selected transcript")
        words = transcript.words if transcript is not None else None
        duration = take.duration_us if transcript is not None and take is not None else None
        orientation = "portrait" if profile.height > profile.width else "landscape"
        timeline = resolve_timeline(
            segment_id,
            narration,
            words,
            wpm=profile.speech_wpm,
            audio_duration_us=duration,
            lead_in_us=profile.lead_in_us,
            tail_us=profile.tail_us,
            orientation=orientation,
        )
        record = timeline.to_record()
        return _TimelineInputs(
            record=record,
            digest=f"sha256:{timeline.digest}",
            duration_us=timeline.total_duration_us,
            lead_in_us=timeline.lead_in_us,
            source=timeline.source,
            take=take,
            audio_path=audio.take_path(take) if take is not None else None,
            warnings=(
                ("selected take is stale; using silence",) if take_is_stale else ()
            ),
        )

    def refresh_inputs(self) -> None:
        """Start a new shared-source fingerprint boundary for this engine."""

        with self._shared_inputs_lock:
            self._shared_inputs_cache = None

    def _calculate_shared_inputs(self) -> dict[str, Any]:
        config = _get(self.project, "config", {})
        style_relative = Path(_get(config, "style", "style.py"))
        style = self.root / style_relative
        runtime = self.root / "chalk_runtime.py"
        assets = self.root / "assets"
        chalk_python = self.root / "_chalk"
        timeline_runtime = chalk_python / "timeline.py"
        return {
            "style": hash_file(style) if style.is_file() else None,
            "runtime": hash_file(runtime) if runtime.is_file() else None,
            "timeline_runtime": (
                hash_file(timeline_runtime) if timeline_runtime.is_file() else None
            ),
            "shared_python": _shared_python_hash(self.root),
            "assets_tree": hash_tree(assets),
        }

    def _shared_inputs(self, *, fresh: bool = False) -> dict[str, Any]:
        if fresh:
            return self._calculate_shared_inputs()
        with self._shared_inputs_lock:
            if self._shared_inputs_cache is None:
                self._shared_inputs_cache = self._calculate_shared_inputs()
            return dict(self._shared_inputs_cache)

    def _render_action_inputs(
        self,
        segment: Any,
        profile: RenderProfile,
        timeline: _TimelineInputs,
        *,
        scene: Path | None = None,
        fresh_shared: bool = False,
    ) -> dict[str, Any]:
        segment_id = str(_get(segment, "id"))
        scene = scene or self.scene_path(segment)
        return {
            "segment_id": segment_id,
            "scene_entry": f"scenes/{segment_id}.py::Visual",
            "scene": hash_file(scene),
            **self._shared_inputs(fresh=fresh_shared),
            "timeline": timeline.digest,
            "profile": profile.to_record(),
        }

    def desired_render_key(
        self, segment_id: str, profile: str | RenderProfile | None = None
    ) -> str:
        """Compute the canonical current render key without running Manim."""

        segment = self.segment(segment_id)
        render_profile = self._profile(profile)
        timeline = self._timeline(segment, render_profile, draft=True)
        inputs = self._render_action_inputs(segment, render_profile, timeline)
        environment = self._action_environment(RENDER_ENVIRONMENT_KEYS)
        return action_key(
            "scene-render",
            inputs=inputs,
            recipe=RENDER_RECIPE,
            environment=environment,
        )

    def desired_cut_key(
        self,
        segment_id: str,
        profile: str | RenderProfile | None = None,
        *,
        render_record: ActionRecord | None = None,
    ) -> str | None:
        """Compute the current mux key when its exact render exists."""

        segment = self.segment(segment_id)
        render_profile = self._profile(profile)
        timeline = self._timeline(segment, render_profile, draft=True)
        render_record = render_record or self.store.lookup_action(
            self.desired_render_key(segment_id, render_profile)
        )
        if render_record is None:
            return None
        render_blob = render_record.outputs.get("video")
        if render_blob is None:
            return None
        tail_us = int(timeline.record.get("tail_us", render_profile.tail_us))
        duration_us = max(
            timeline.duration_us,
            (
                timeline.lead_in_us + timeline.take.duration_us + tail_us
                if timeline.take is not None
                else timeline.duration_us
            ),
        )
        if timeline.take is None:
            audio_input: Mapping[str, Any] = {
                "kind": "generated-silence",
                "duration_us": duration_us,
                "sample_rate": 48_000,
                "channels": "stereo",
            }
        else:
            audio_input = {
                "kind": "selected-take",
                "take_id": timeline.take.id,
                "audio": f"sha256:{timeline.take.audio_sha256}",
                "narration": f"sha256:{timeline.take.narration_sha256}",
            }
        inputs = {
            "segment_id": segment_id,
            "render_action": render_record.key,
            "render_blob": render_blob,
            "audio": audio_input,
            "lead_in_us": timeline.lead_in_us,
            "duration_us": duration_us,
            "profile": render_profile.to_record(),
        }
        environment = self._action_environment(FFMPEG_ENVIRONMENT_KEYS)
        return action_key(
            "segment-composite",
            inputs=inputs,
            recipe=SEGMENT_RECIPE,
            environment=environment,
        )

    def desired_full_key(
        self,
        profile: str | RenderProfile | None = None,
        *,
        cut_records: Mapping[str, ActionRecord] | None = None,
    ) -> str | None:
        """Compute the order-sensitive full-cut key without running ffmpeg."""

        render_profile = self._profile(profile)
        records: list[ActionRecord] = []
        for segment in self.ordered_segments():
            segment_id = str(_get(segment, "id"))
            record = (cut_records or {}).get(segment_id)
            if record is None:
                key = self.desired_cut_key(segment_id, render_profile)
                record = self.store.lookup_action(key) if key else None
            if record is None:
                return None
            records.append(record)
        inputs = {
            "profile": render_profile.to_record(),
            "segments": [
                {
                    "segment_id": str(_get(segment, "id")),
                    "action": record.key,
                    "blob": record.outputs["video"],
                }
                for segment, record in zip(self.ordered_segments(), records, strict=True)
            ],
        }
        environment = self._action_environment(FFMPEG_ENVIRONMENT_KEYS)
        return action_key(
            "full-composite",
            inputs=inputs,
            recipe=FULL_RECIPE,
            environment=environment,
        )

    def _run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        expected_output: Path,
        env: Mapping[str, str] | None = None,
    ) -> None:
        method = getattr(self.executor, "run", self.executor)
        method(command, cwd=cwd, expected_output=expected_output, env=env)
        if expected_output.is_file() and expected_output.stat().st_size:
            return
        candidates = [
            path
            for path in expected_output.parent.rglob(expected_output.name)
            if path.is_file() and path != expected_output
        ]
        if len(candidates) == 1:
            expected_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidates[0], expected_output)
        if not expected_output.is_file() or expected_output.stat().st_size == 0:
            raise BuildError(f"command produced no output: {' '.join(command)}")

    def _capture(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> tuple[str, str]:
        method = getattr(self.executor, "capture", None)
        if not callable(method):
            raise BuildError(
                "the configured command executor cannot capture loudness analysis"
            )
        result = method(command, cwd=cwd, env=env)
        if getattr(result, "returncode", 1) != 0:
            detail = str(getattr(result, "stderr", "") or "").strip()
            raise BuildError(
                f"command failed ({getattr(result, 'returncode', '?')}): "
                f"{' '.join(command)}"
                + (f": {detail}" if detail else "")
            )
        return (
            str(getattr(result, "stdout", "") or ""),
            str(getattr(result, "stderr", "") or ""),
        )

    @staticmethod
    def _loudness_measurements(output: str) -> dict[str, str]:
        required = (
            "input_i",
            "input_tp",
            "input_lra",
            "input_thresh",
            "target_offset",
        )
        for match in reversed(tuple(re.finditer(r"\{[^{}]*\}", output, re.DOTALL))):
            try:
                value = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
            if not isinstance(value, Mapping) or not all(key in value for key in required):
                continue
            measurements: dict[str, str] = {}
            for key in required:
                try:
                    numeric = float(value[key])
                except (TypeError, ValueError) as error:
                    raise BuildError(
                        f"ffmpeg loudness analysis returned invalid {key}"
                    ) from error
                if not math.isfinite(numeric):
                    raise BuildError(
                        f"ffmpeg loudness analysis returned non-finite {key}"
                    )
                measurements[key] = format(numeric, ".6f")
            return measurements
        raise BuildError("ffmpeg loudness analysis returned no measurement JSON")

    def _validate_media(
        self,
        path: Path,
        *,
        kind: str,
        profile: RenderProfile,
        duration_us: int,
        require_audio: bool,
    ) -> None:
        requirements = MediaRequirements(
            kind=kind,
            width=profile.width,
            height=profile.height,
            fps=profile.fps,
            duration_us=duration_us,
            require_audio=require_audio,
        )
        method = getattr(self.validator, "validate", self.validator)
        method(path, requirements)

    def render_segment(
        self,
        segment_id: str,
        profile: str | RenderProfile | None = None,
        *,
        draft: bool = True,
        materialize: bool = True,
        force: bool = False,
    ) -> Artifact:
        segment = self.segment(segment_id)
        render_profile = self._profile(profile)
        environment = self._action_environment(RENDER_ENVIRONMENT_KEYS)

        for attempt in range(3):
            scene = self.scene_path(segment)
            timeline = self._timeline(segment, render_profile, draft=draft)
            stable_inputs = self._render_action_inputs(
                segment,
                render_profile,
                timeline,
                scene=scene,
            )
            inputs = dict(stable_inputs)
            if force:
                inputs["force_nonce"] = time.time_ns()

            def sources_still_match() -> bool:
                try:
                    current_timeline = self._timeline(
                        segment,
                        render_profile,
                        draft=draft,
                    )
                    current = self._render_action_inputs(
                        segment,
                        render_profile,
                        current_timeline,
                        fresh_shared=True,
                    )
                except (BuildError, KeyError, OSError, TypeError, ValueError):
                    return False
                return current == stable_inputs

            def produce(workdir: Path) -> ActionProducts:
                timeline_path = workdir / "timeline.json"
                timeline_path.write_bytes(canonical_json(timeline.record) + b"\n")
                output = workdir / "render.mp4"
                media_dir = workdir / "media"
                tools = _raw_config(self.project).get("tools", {})
                manim = (
                    str(tools.get("manim", "manim"))
                    if isinstance(tools, Mapping)
                    else "manim"
                )
                command = [
                    manim,
                    "render",
                    render_profile.manim_quality,
                    "-r",
                    f"{render_profile.width},{render_profile.height}",
                    "--fps",
                    str(render_profile.fps),
                    "--media_dir",
                    str(media_dir),
                    "--output_file",
                    output.name,
                    *render_profile.extra_manim_args,
                    str(scene),
                    "Visual",
                ]
                process_env = os.environ.copy()
                process_env.update(
                    {
                        "CHALK_TIMELINE": str(timeline_path),
                        "CHALK_TIMELINE_PATH": str(timeline_path),
                        "CHALK_SEGMENT_ID": segment_id,
                        "CHALK_OUTPUT_PATH": str(output),
                        "PYTHONPATH": os.pathsep.join(
                            filter(
                                None,
                                [
                                    str(self.root),
                                    process_env.get("PYTHONPATH", ""),
                                ],
                            )
                        ),
                    }
                )
                # Scene code commonly opens project-relative assets. Manim's
                # own media directory remains action-local.
                self._run(
                    command,
                    cwd=self.root,
                    expected_output=output,
                    env=process_env,
                )
                self._validate_media(
                    output,
                    kind="render",
                    profile=render_profile,
                    duration_us=timeline.duration_us,
                    require_audio=False,
                )
                if not sources_still_match():
                    raise _RenderSourcesChanged(
                        "render sources changed while Manim was running"
                    )
                return ActionProducts(
                    {"video": output},
                    {
                        "duration_us": timeline.duration_us,
                        "timing": timeline.source,
                        "profile": render_profile.name,
                    },
                )

            try:
                result = self.store.run_action(
                    "scene-render",
                    inputs=inputs,
                    recipe=RENDER_RECIPE,
                    environment=environment,
                    producer=produce,
                )
            except _RenderSourcesChanged:
                if attempt == 2:
                    raise BuildError(
                        "render sources kept changing; retry after edits settle"
                    ) from None
                self.refresh_inputs()
                continue
            if sources_still_match():
                break
            if attempt == 2:
                raise BuildError(
                    "render sources kept changing; retry after edits settle"
                )
            self.refresh_inputs()
        else:  # pragma: no cover - the loop either breaks or raises
            raise BuildError("render did not reach a stable source revision")
        alias = (
            self.root
            / "output"
            / render_profile.name
            / "renders"
            / f"{segment_id}.mp4"
        )
        if materialize:
            self.store.materialize(result.record.outputs["video"], alias)
        artifact = Artifact(
            "render",
            result.record.key,
            result.record.outputs["video"],
            alias,
            result.cache_hit,
            render_profile.name,
            segment_id,
            timeline.duration_us,
            timeline.source,
            (
                (*timeline.warnings, "using estimated narration timing")
                if timeline.source == "estimated"
                else timeline.warnings
            ),
        )
        self._remember(f"segment:{segment_id}:render", artifact.action_key)
        self._remember(
            f"render:{render_profile.name}:{segment_id}", artifact.action_key
        )
        return artifact

    def composite_segment(
        self,
        segment_id: str,
        profile: str | RenderProfile | None = None,
        *,
        draft: bool = True,
        render: Artifact | None = None,
        materialize: bool = True,
        force: bool = False,
    ) -> Artifact:
        segment = self.segment(segment_id)
        render_profile = self._profile(profile)
        timeline = self._timeline(segment, render_profile, draft=draft)
        render = render or self.render_segment(segment_id, render_profile, draft=draft)
        tail_us = int(timeline.record.get("tail_us", render_profile.tail_us))
        cut_duration_us = max(
            timeline.duration_us,
            (
                timeline.lead_in_us + timeline.take.duration_us + tail_us
                if timeline.take is not None
                else timeline.duration_us
            ),
        )
        take = timeline.take
        audio_present = (
            take is not None
            and timeline.audio_path is not None
            and timeline.audio_path.is_file()
        )
        if audio_present:
            actual_audio = hash_file(timeline.audio_path).removeprefix("sha256:")
            if actual_audio != take.audio_sha256:
                raise BuildError(
                    f"selected audio for {segment_id!r} fails its SHA-256 check"
                )
        # A missing selected-take blob (for example a fresh clone before its Git
        # LFS media is pulled) is drafting status, not a structural error: fall
        # back to silence with a warning, exactly like an unselected take. A hash
        # mismatch above is a real integrity failure and still raises; only a
        # release insists on real audio being present.
        missing_warnings: tuple[str, ...] = ()
        if take is None:
            if not draft:
                raise BuildError(f"segment {segment_id!r} has no selected voice take")
            use_silence = True
        elif not audio_present:
            if not draft:
                raise BuildError(f"selected audio for {segment_id!r} is missing")
            use_silence = True
            missing_warnings = (
                f"selected take for {segment_id!r} has no local audio; "
                "previewing with silence",
            )
        else:
            use_silence = False
        if use_silence:
            audio_input: Mapping[str, Any] = {
                "kind": "generated-silence",
                "duration_us": cut_duration_us,
                "sample_rate": 48_000,
                "channels": "stereo",
            }
        else:
            audio_input = {
                "kind": "selected-take",
                "take_id": take.id,
                "audio": f"sha256:{take.audio_sha256}",
                "narration": f"sha256:{take.narration_sha256}",
            }
        inputs = {
            "segment_id": segment_id,
            "render_action": render.action_key,
            "render_blob": render.blob_id,
            "audio": audio_input,
            "lead_in_us": timeline.lead_in_us,
            "duration_us": cut_duration_us,
            "profile": render_profile.to_record(),
        }
        if force:
            inputs["force_nonce"] = time.time_ns()
        environment = self._action_environment(FFMPEG_ENVIRONMENT_KEYS)

        def produce(workdir: Path) -> ActionProducts:
            video = self.store.materialize(render.blob_id, workdir / "render.mp4")
            output = workdir / "segment.mp4"
            ffmpeg = "ffmpeg"
            tools = _raw_config(self.project).get("tools", {})
            if isinstance(tools, Mapping):
                ffmpeg = str(tools.get("ffmpeg", ffmpeg))
            if use_silence:
                seconds = f"{cut_duration_us / 1_000_000:.6f}"
                command = [
                    ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(video),
                    "-f",
                    "lavfi",
                    "-t",
                    seconds,
                    "-i",
                    "anullsrc=r=48000:cl=stereo",
                    "-vf", f"tpad=stop_mode=clone:stop_duration={seconds}",
                    "-map", "0:v:0", "-map", "1:a:0", "-t", seconds,
                ]
            else:
                delay_ms = round(timeline.lead_in_us / 1000)
                seconds = f"{cut_duration_us / 1_000_000:.6f}"
                assert timeline.audio_path is not None
                audio = workdir / "audio.flac"
                shutil.copyfile(timeline.audio_path, audio)
                if hash_file(audio).removeprefix("sha256:") != timeline.take.audio_sha256:
                    raise BuildError(
                        f"selected audio for {segment_id!r} changed during staging"
                    )
                command = [
                    ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(video),
                    "-i",
                    str(audio),
                    "-filter_complex",
                    f"[0:v]tpad=stop_mode=clone:stop_duration={seconds}[v];"
                    f"[1:a]adelay={delay_ms}:all=1,apad[a]",
                    "-map", "[v]", "-map", "[a]", "-t", seconds,
                ]
            command.extend(
                [
                    "-r", str(render_profile.fps),
                    "-c:v", "libx264", "-crf", str(render_profile.video_crf),
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                    "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
                    str(output),
                ]
            )
            self._run(command, cwd=workdir, expected_output=output)
            self._validate_media(
                output,
                kind="segment cut",
                profile=render_profile,
                duration_us=cut_duration_us,
                require_audio=True,
            )
            return ActionProducts(
                {"video": output},
                {
                    "duration_us": cut_duration_us,
                    "timing": timeline.source,
                    "profile": render_profile.name,
                },
            )

        result = self.store.run_action(
            "segment-composite",
            inputs=inputs,
            recipe=SEGMENT_RECIPE,
            environment=environment,
            producer=produce,
            dependencies=(render.action_key,),
        )
        alias = (
            self.root
            / "output"
            / render_profile.name
            / "segments"
            / f"{segment_id}.mp4"
        )
        if materialize:
            self.store.materialize(result.record.outputs["video"], alias)
        cut_warnings = tuple(timeline.warnings) + missing_warnings
        if use_silence and not missing_warnings:
            cut_warnings += ("draft cut uses generated silence",)
        artifact = Artifact(
            "cut",
            result.record.key,
            result.record.outputs["video"],
            alias,
            result.cache_hit,
            render_profile.name,
            segment_id,
            cut_duration_us,
            timeline.source,
            cut_warnings,
        )
        self._remember(f"segment:{segment_id}:cut", artifact.action_key)
        self._remember(f"cut:{render_profile.name}:{segment_id}", artifact.action_key)
        return artifact

    def build_segments(
        self,
        segment_ids: Sequence[str],
        profile: str | RenderProfile | None = None,
        *,
        draft: bool = True,
    ) -> tuple[Artifact, ...]:
        # Resolve the complete requested scope before starting work.  No other
        # segment is inspected or built after this point.
        requested = tuple(dict.fromkeys(segment_ids))
        for segment_id in requested:
            self.segment(segment_id)
        render_profile = self._profile(profile)
        with ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(requested)))) as pool:
            rendered = list(
                pool.map(
                    lambda segment_id: self.render_segment(segment_id, render_profile, draft=draft),
                    requested,
                )
            )
        render_by_segment = {artifact.segment_id: artifact for artifact in rendered}
        with ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(requested)))) as pool:
            cuts = list(
                pool.map(
                    lambda segment_id: self.composite_segment(
                        segment_id,
                        render_profile,
                        draft=draft,
                        render=render_by_segment[segment_id],
                    ),
                    requested,
                )
            )
        return tuple(cuts)

    def render_segments(
        self,
        segment_ids: Sequence[str],
        profile: str | RenderProfile | None = None,
        *,
        draft: bool = True,
    ) -> tuple[Artifact, ...]:
        requested = tuple(dict.fromkeys(segment_ids))
        for segment_id in requested:
            self.segment(segment_id)
        render_profile = self._profile(profile)
        with ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(requested)))) as pool:
            return tuple(
                pool.map(
                    lambda segment_id: self.render_segment(segment_id, render_profile, draft=draft),
                    requested,
                )
            )

    render = render_segments

    def build_segment(
        self,
        segment_id: str,
        profile: str | RenderProfile | None = None,
        *,
        draft: bool = True,
    ) -> Artifact:
        return self.build_segments((segment_id,), profile, draft=draft)[0]

    def build_full(
        self,
        profile: str | RenderProfile | None = None,
        *,
        draft: bool = True,
        force: bool = False,
    ) -> BuildResult:
        render_profile = self._profile(profile)
        ordered = self.ordered_segments()
        ids = tuple(str(_get(segment, "id")) for segment in ordered)
        cuts = self.build_segments(ids, render_profile, draft=draft)
        inputs = {
            "profile": render_profile.to_record(),
            "segments": [
                {
                    "segment_id": artifact.segment_id,
                    "action": artifact.action_key,
                    "blob": artifact.blob_id,
                }
                for artifact in cuts
            ],
        }
        if force:
            inputs["force_nonce"] = time.time_ns()
        environment = self._action_environment(FFMPEG_ENVIRONMENT_KEYS)

        def produce(workdir: Path) -> ActionProducts:
            concat = workdir / "segments.txt"
            lines: list[str] = []
            for index, artifact in enumerate(cuts):
                path = self.store.materialize(artifact.blob_id, workdir / f"segment-{index:04}.mp4")
                escaped = str(path).replace("'", "'\\''")
                lines.append(f"file '{escaped}'")
            concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
            output = workdir / "full.mp4"
            ffmpeg = "ffmpeg"
            tools = _raw_config(self.project).get("tools", {})
            if isinstance(tools, Mapping):
                ffmpeg = str(tools.get("ffmpeg", ffmpeg))
            analysis_filter = "loudnorm=I=-14:TP=-1:LRA=11:print_format=json"
            analysis_command = [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-nostats",
                "-v",
                "info",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat),
                "-af",
                analysis_filter,
                "-f",
                "null",
                "-",
            ]
            analysis_stdout, analysis_stderr = self._capture(
                analysis_command,
                cwd=workdir,
            )
            measurements = self._loudness_measurements(
                analysis_stdout + "\n" + analysis_stderr
            )
            normalization_filter = (
                "loudnorm=I=-14:TP=-1:LRA=11"
                f":measured_I={measurements['input_i']}"
                f":measured_TP={measurements['input_tp']}"
                f":measured_LRA={measurements['input_lra']}"
                f":measured_thresh={measurements['input_thresh']}"
                f":offset={measurements['target_offset']}"
                ":linear=true:print_format=summary"
            )
            command = [
                ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                "-c:v", "libx264", "-preset", "slow", "-crf", str(render_profile.video_crf),
                "-pix_fmt", "yuv420p", "-af", normalization_filter,
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart", str(output),
            ]
            self._run(command, cwd=workdir, expected_output=output)
            self._validate_media(
                output,
                kind="full cut",
                profile=render_profile,
                duration_us=sum(artifact.duration_us or 0 for artifact in cuts),
                require_audio=True,
            )
            return ActionProducts(
                {"video": output},
                {
                    "duration_us": sum(
                        artifact.duration_us or 0 for artifact in cuts
                    ),
                    "profile": render_profile.name,
                    "loudness": {
                        "recipe": dict(FULL_RECIPE["loudness"]),
                        "measurements": measurements,
                        "analysis_filter": analysis_filter,
                        "normalization_filter": normalization_filter,
                    },
                },
            )

        result = self.store.run_action(
            "full-composite",
            inputs=inputs,
            recipe=FULL_RECIPE,
            environment=environment,
            producer=produce,
            dependencies=tuple(artifact.action_key for artifact in cuts),
        )
        alias = self.root / "output" / render_profile.name / "full.mp4"
        self.store.materialize(result.record.outputs["video"], alias)
        final = Artifact(
            "full-cut",
            result.record.key,
            result.record.outputs["video"],
            alias,
            result.cache_hit,
            render_profile.name,
            duration_us=sum(artifact.duration_us or 0 for artifact in cuts),
            timing=(
                "mixed"
                if len({artifact.timing for artifact in cuts}) > 1
                else (cuts[0].timing if cuts else None)
            ),
        )
        self._remember("full", final.action_key)
        self._remember(f"full:{render_profile.name}", final.action_key)
        return BuildResult(cuts, final)

    def compose(
        self,
        segment_ids: Sequence[str],
        profile: str | RenderProfile | None = None,
        *,
        full: bool = False,
        renders: Sequence[Artifact] | None = None,
        draft: bool = True,
    ) -> Artifact | tuple[Artifact, ...]:
        if full:
            return self.build_full(profile, draft=draft).final  # type: ignore[return-value]
        render_profile = self._profile(profile)
        rendered = {artifact.segment_id: artifact for artifact in (renders or ())}
        return tuple(
            self.composite_segment(
                segment_id,
                render_profile,
                draft=draft,
                render=rendered.get(segment_id),
            )
            for segment_id in segment_ids
        )

    composite = compose

    def watch(
        self,
        segment_ids: Sequence[str],
        profile: str | RenderProfile | None = None,
        *,
        play: bool = True,
        full: bool = False,
        draft: bool = True,
    ) -> Artifact | tuple[Artifact, ...]:
        if full:
            final = self.build_full(profile, draft=draft).final
            if final is None:  # pragma: no cover - build_full always returns one
                raise BuildError("full build produced no final artifact")
            result: Artifact | tuple[Artifact, ...] = final
        else:
            result = self.build_segments(segment_ids, profile, draft=draft)
        if play:
            self.play(result)
        return result

    def play(self, artifact: Artifact | Sequence[Artifact] | str | os.PathLike[str]) -> None:
        if isinstance(artifact, Artifact):
            path = artifact.path
        elif isinstance(artifact, (str, os.PathLike)):
            path = Path(artifact)
        else:
            values = tuple(artifact)
            if not values:
                raise BuildError("there is no artifact to play")
            path = values[0].path
        if not path.is_file():
            raise BuildError(f"artifact does not exist: {path}")
        configured = _raw_config(self.project).get("tools", {})
        player = (
            str(configured.get("player", "ffplay"))
            if isinstance(configured, Mapping)
            else "ffplay"
        )
        try:
            subprocess.Popen(
                [player, "-autoexit", str(path)],
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise BuildError(f"could not launch {player}: {error}") from error

    def release(
        self,
        name: str,
        profile: str | RenderProfile | None = "final",
    ) -> ReleaseResult:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name):
            raise BuildError("release names use letters, digits, '.', '_', and '-'")
        try:
            from .model import (
                atomic_write_json,
                load_project,
                project_lock,
                save_state,
                validate_project,
            )
            from .review import create_snapshot, list_notes

            report = validate_project(self.project)
            strict_input_codes = {
                "transcript-audio-mismatch",
                "transcript-config-stale",
                "transcript-invalid",
                "transcript-missing",
            }
            final_input_diagnostics = tuple(
                diagnostic
                for diagnostic in report.diagnostics
                if diagnostic.code in strict_input_codes
            )
            if report.errors or final_input_diagnostics:
                diagnostics = tuple(
                    dict.fromkeys((*report.errors, *final_input_diagnostics))
                )
                detail = "; ".join(
                    f"{diagnostic.code}: {diagnostic.message}"
                    for diagnostic in diagnostics
                )
                raise BuildError(f"release is blocked: {detail}")
            blockers = [
                note
                for note in list_notes(self.project)
                if str(_get(note, "severity", "")).casefold()
                in {"blocker", "blocking"}
            ]
            if blockers:
                raise BuildError(f"release is blocked by {len(blockers)} open blocker(s)")
        except ImportError as error:  # pragma: no cover - project copies include these
            raise BuildError(f"release support is incomplete: {error}") from error

        from .audio import AudioStore

        audio = AudioStore(self.root)
        for segment in self.ordered_segments():
            segment_id = str(_get(segment, "id"))
            narration = str(_get(segment, "narration", ""))
            try:
                take = audio.selected_take(
                    segment_id, narration=narration, require_fresh=True
                )
            except (KeyError, OSError, TypeError, ValueError) as error:
                raise BuildError(
                    f"release requires a current selected take for {segment_id!r}: {error}"
                ) from error
            if take is None:
                raise BuildError(
                    f"release requires a current selected take for {segment_id!r}"
                )
            path = audio.take_path(take)
            if (
                not path.is_file()
                or hash_file(path).removeprefix("sha256:") != take.audio_sha256
            ):
                raise BuildError(
                    f"release selected audio for {segment_id!r} is missing or corrupt"
                )
            try:
                transcript = audio.current_selected_transcript(
                    segment_id,
                    narration=narration,
                    require_fresh=True,
                )
            except (OSError, TypeError, ValueError) as error:
                raise BuildError(
                    f"release transcript for {segment_id!r} is invalid: {error}"
                ) from error
            if transcript is None:
                raise BuildError(
                    f"release requires a current transcript for {segment_id!r}"
                )

        result = self.build_full(profile, draft=False)
        assert result.final is not None
        pending_state = _get(self.project, "state", {})
        pending_actions = _get(pending_state, "current_actions", {})
        with project_lock(self.root, "state"):
            current = load_project(self.root)
            if isinstance(pending_actions, Mapping):
                current.state.current_actions.update(
                    {
                        str(key): str(value)
                        for key, value in pending_actions.items()
                        if isinstance(key, str)
                        and isinstance(value, str)
                        and value.startswith("sha256:")
                    }
                )
            save_state(self.root, current.state)
        self.project = current
        release_dir = self.root / "releases"
        media_path = release_dir / f"{name}.mp4"
        manifest_path = release_dir / f"{name}.json"
        closure = [record.to_dict() for record in self.trace(result.final)]
        content_identity = {
            "root_action": result.final.action_key,
            "output_blob": result.final.blob_id,
            "profile": result.final.profile,
            "actions": closure,
        }
        if manifest_path.is_file():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise BuildError(f"cannot verify existing release manifest: {error}") from error
            if not isinstance(existing, Mapping) or any(
                existing.get(key) != value
                for key, value in content_identity.items()
            ):
                raise BuildError(f"release {name!r} already identifies different content")
            snapshot_digest = existing.get("snapshot")
            if not isinstance(snapshot_digest, str) or not snapshot_digest:
                raise BuildError(
                    f"existing release {name!r} has no valid snapshot identity"
                )
        else:
            snapshot = create_snapshot(self.project)
            snapshot_digest = snapshot.digest
            payload = {
                "schema_version": 1,
                "name": name,
                "snapshot": snapshot_digest,
                **content_identity,
                "media": media_path.relative_to(self.root).as_posix(),
            }
            atomic_write_json(manifest_path, payload)
        self.store.materialize(result.final.blob_id, media_path)
        return ReleaseResult(
            name,
            result.final,
            manifest_path.relative_to(self.root),
            media_path.relative_to(self.root),
            snapshot_digest,
        )

    def trace(self, artifact: Artifact | str) -> tuple[ActionRecord, ...]:
        return self.store.trace(artifact.action_key if isinstance(artifact, Artifact) else artifact)

    def _remember(self, key: str, action_key: str) -> None:
        state = _get(self.project, "state", None)
        actions = _get(state, "current_actions", None)
        if not isinstance(actions, dict):
            return
        with self._state_lock:
            actions[key] = action_key
            if self.defer_state:
                return
            # Persist just this derived-action pointer. Writing the engine's whole
            # in-memory state here would clobber a concurrent take-selection or
            # approval edit made after this engine loaded. For a real project on
            # disk, merge only this key under the cross-process lock (the same
            # discipline as _commit_current); a test double with no chalk.toml
            # keeps the older in-memory save.
            if (self.root / "chalk.toml").is_file():
                _merge_current_action(self.root, key, action_key)
                return
            save = getattr(self.project, "save_state", None)
            if callable(save):
                save()


class _CallableExecutor:
    """Adapt the small Builder runner callback to the executor protocol."""

    def __init__(self, runner: Callable[..., Any]) -> None:
        self.runner = runner

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        expected_output: Path,
        env: Mapping[str, str] | None = None,
    ) -> Any:
        process_env = dict(env or os.environ)
        process_env["CHALK_OUTPUT_PATH"] = str(expected_output)
        result = self.runner(list(command), cwd=cwd, env=process_env)
        return_code = getattr(result, "returncode", 0)
        if return_code not in (None, 0):
            detail = str(getattr(result, "stderr", "")).strip()
            raise BuildError(
                f"command failed ({return_code}): {' '.join(command)}"
                + (f": {detail}" if detail else "")
            )
        return result

    def capture(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> Any:
        return self.runner(list(command), cwd=cwd, env=dict(env or os.environ))


class Builder(BuildEngine):
    """Fresh-project, CLI-oriented facade over :class:`BuildEngine`."""

    def __init__(
        self,
        project: Any,
        *,
        runner: Callable[..., Any] | None = None,
        executor: CommandExecutor | Callable[..., Any] | None = None,
        validator: MediaValidator
        | Callable[[Path, MediaRequirements], None]
        | None = None,
        environment: Mapping[str, Any] | None = None,
        jobs: int | None = None,
        store: Store | None = None,
    ) -> None:
        from .model import Project as ModelProject, load_project

        loaded = project if isinstance(project, ModelProject) else load_project(project)
        if runner is not None and executor is not None:
            raise BuildError("pass runner or executor, not both")
        selected_executor = _CallableExecutor(runner) if runner is not None else executor
        super().__init__(
            loaded,
            store=store,
            executor=selected_executor,
            validator=validator,
            environment=environment,
            max_workers=jobs,
            defer_state=True,
        )
        self._runner_callback = runner

    def _fresh_engine(self, jobs: int | None = None) -> BuildEngine:
        from .model import load_project

        return BuildEngine(
            load_project(self.root),
            store=self.store,
            executor=self.executor,
            validator=self.validator,
            environment=self.environment,
            max_workers=jobs or self.max_workers,
            defer_state=True,
        )

    def profiles(self) -> tuple[RenderProfile, ...]:
        raw = _raw_config(self._fresh_engine().project).get("profiles", {})
        if not isinstance(raw, Mapping) or not raw:
            raise BuildError("chalk.toml must define explicit [profiles.<name>] tables")
        return tuple(load_profile(self.project, str(name)) for name in raw)

    def profile(self, name: str | None = None) -> RenderProfile:
        return load_profile(self._fresh_engine().project, name)

    def resolve_timeline(
        self, segment: str | Any, *, profile: str | None = None
    ) -> Mapping[str, Any]:
        engine = self._fresh_engine()
        value = engine.segment(segment) if isinstance(segment, str) else segment
        return engine._timeline(value, engine._profile(profile), draft=True).record

    @staticmethod
    def _selection(
        project: Any,
        segments: str | Any | Iterable[str | Any] | None,
        segment_ids: Iterable[str] | None,
    ) -> tuple[str, ...]:
        if segments is not None and segment_ids is not None:
            raise BuildError("pass segments or segment_ids, not both")
        requested: Any = segment_ids if segment_ids is not None else segments
        ordered = tuple(
            str(_get(segment, "id"))
            for segment in sorted(
                _get(project, "segments", ()), key=lambda item: int(_get(item, "order", 0))
            )
            if str(_get(segment, "id", ""))
        )
        if requested is None:
            return ordered
        if isinstance(requested, str) or hasattr(requested, "id"):
            values = (requested,)
        else:
            values = tuple(requested)
        wanted = tuple(
            dict.fromkeys(
                str(_get(value, "id")) if not isinstance(value, str) else value
                for value in values
            )
        )
        unknown = sorted(set(wanted) - set(ordered))
        if unknown:
            raise BuildError("unknown segment(s): " + ", ".join(unknown))
        return tuple(segment_id for segment_id in ordered if segment_id in wanted)

    @staticmethod
    def _check(project: Any, ids: Sequence[str], *, whole_project: bool) -> None:
        from .model import validate_project

        reports = (
            (validate_project(project),)
            if whole_project
            else tuple(validate_project(project, segment_id) for segment_id in ids)
        )
        errors = [diagnostic for report in reports for diagnostic in report.errors]
        if errors:
            detail = "; ".join(
                f"{diagnostic.code}: {diagnostic.message}" for diagnostic in errors
            )
            raise BuildError("project is not buildable: " + detail)

    def _commit_current(
        self,
        artifacts: Sequence[Artifact],
        *,
        final: Artifact | None = None,
        action_map: Mapping[str, str] | None = None,
    ) -> None:
        from .model import load_project, project_lock, save_state

        # Keep the expensive build outside the lock, then merge only its refs
        # into the latest state so concurrent take/approval edits survive.
        with project_lock(self.root, "state"):
            project = load_project(self.root)
            if action_map is not None:
                project.state.current_actions.update(
                    {
                        str(key): str(value)
                        for key, value in action_map.items()
                        if isinstance(key, str)
                        and isinstance(value, str)
                        and value.startswith("sha256:")
                    }
                )
            for artifact in artifacts:
                if artifact.segment_id is None:
                    continue
                legacy = "render" if artifact.kind == "render" else "cut"
                project.state.current_actions[
                    f"segment:{artifact.segment_id}:{legacy}"
                ] = artifact.action_key
                project.state.current_actions[
                    f"{legacy}:{artifact.profile}:{artifact.segment_id}"
                ] = artifact.action_key
            if final is not None:
                project.state.current_actions["full"] = final.action_key
                project.state.current_actions[
                    f"full:{final.profile}"
                ] = final.action_key
            save_state(self.root, project.state)

    def render(
        self,
        segments: str | Any | Iterable[str | Any] | None = None,
        *,
        segment_ids: Iterable[str] | None = None,
        profile: str | None = None,
        force: bool = False,
        jobs: int | None = None,
    ) -> tuple[Artifact, ...]:
        engine = self._fresh_engine(jobs)
        ids = self._selection(engine.project, segments, segment_ids)
        self._check(
            engine.project,
            ids,
            whole_project=segments is None and segment_ids is None,
        )
        selected_profile = engine._profile(profile)
        with ThreadPoolExecutor(
            max_workers=min(engine.max_workers, max(1, len(ids)))
        ) as pool:
            artifacts = tuple(
                pool.map(
                    lambda segment_id: engine.render_segment(
                        segment_id, selected_profile, force=force
                    ),
                    ids,
                )
            )
        self._commit_current(artifacts)
        return artifacts

    def composite(
        self,
        segments: str | Any | Iterable[str | Any] | None = None,
        *,
        segment_ids: Iterable[str] | None = None,
        profile: str | None = None,
        force: bool = False,
        jobs: int | None = None,
    ) -> tuple[Artifact, ...]:
        rendered = self.render(
            segments,
            segment_ids=segment_ids,
            profile=profile,
            jobs=jobs,
        )
        engine = self._fresh_engine(jobs)
        ids = tuple(artifact.segment_id for artifact in rendered if artifact.segment_id)
        selected_profile = engine._profile(profile)
        render_by_id = {artifact.segment_id: artifact for artifact in rendered}
        with ThreadPoolExecutor(
            max_workers=min(engine.max_workers, max(1, len(ids)))
        ) as pool:
            cuts = tuple(
                pool.map(
                    lambda segment_id: engine.composite_segment(
                        segment_id,
                        selected_profile,
                        render=render_by_id[segment_id],
                        force=force,
                    ),
                    ids,
                )
            )
        self._commit_current((*rendered, *cuts))
        return cuts

    def full(
        self,
        profile: str | None = None,
        *,
        force: bool = False,
        jobs: int | None = None,
    ) -> Artifact:
        engine = self._fresh_engine(jobs)
        self._check(engine.project, (), whole_project=True)
        result = engine.build_full(profile, draft=True, force=force)
        if result.final is None:
            raise BuildError("full build produced no final artifact")
        engine_state = _get(engine.project, "state", {})
        engine_actions = _get(engine_state, "current_actions", {})
        self._commit_current(
            result.artifacts,
            final=result.final,
            action_map=(engine_actions if isinstance(engine_actions, Mapping) else None),
        )
        return result.final

    def watch(
        self,
        segments: str | Any | Iterable[str | Any] | None = None,
        *,
        segment_ids: Iterable[str] | None = None,
        profile: str | None = None,
        play: bool = True,
        full: bool = False,
        force: bool = False,
        jobs: int | None = None,
    ) -> Artifact | tuple[Artifact, ...]:
        if full:
            result: Artifact | tuple[Artifact, ...] = self.full(
                profile=profile, force=force, jobs=jobs
            )
        else:
            result = self.composite(
                segments,
                segment_ids=segment_ids,
                profile=profile,
                force=force,
                jobs=jobs,
            )
        if play:
            self.play(result)
        return result

    def play(
        self, artifact: Artifact | Sequence[Artifact] | str | os.PathLike[str]
    ) -> Path:
        if isinstance(artifact, Artifact):
            path = artifact.path
        elif isinstance(artifact, (str, os.PathLike)):
            path = Path(artifact)
        else:
            values = tuple(artifact)
            if not values:
                raise BuildError("there is no artifact to play")
            path = values[0].path
        if not path.is_file():
            raise BuildError(f"artifact does not exist: {path}")
        if self._runner_callback is None:
            super().play(path)
        else:
            result = self._runner_callback(
                ["ffplay", "-autoexit", str(path)], cwd=self.root, env=dict(os.environ)
            )
            if getattr(result, "returncode", 0) not in (None, 0):
                raise BuildError("player command failed")
        return path

    def release(
        self, name: str, profile: str | RenderProfile | None = "final"
    ) -> ReleaseResult:
        engine = self._fresh_engine()
        result = engine.release(name, profile)
        self._commit_current((), final=result.artifact)
        return result

    def trace(self, artifact: Artifact | str) -> tuple[ActionRecord, ...]:
        return self.store.trace(
            artifact.action_key if isinstance(artifact, Artifact) else artifact
        )


def project_artifacts(project: Any) -> dict[str, Any]:
    """Return verified materialized artifacts for the project-room backend."""

    root = Path(_get(project, "root")).resolve()
    store = Store(root)
    state = _get(project, "state", {})
    actions = _get(state, "current_actions", {})
    if not isinstance(actions, Mapping):
        actions = {}
    profiles = _raw_config(project).get("profiles", {})
    profile_names = list(profiles) if isinstance(profiles, Mapping) else ["draft"]
    default = _raw_config(project).get("settings", {})
    default_profile = (
        str(
            default.get(
                "default_profile",
                profile_names[0] if profile_names else "draft",
            )
        )
        if isinstance(default, Mapping)
        else "draft"
    )

    def action_value(key: str) -> str | None:
        value = actions.get(key)
        if not isinstance(value, str):
            return None
        return value if value.startswith("sha256:") else f"sha256:{value}"

    engine = BuildEngine(project, store=store)

    def matches(record: ActionRecord | None, expected: str | None) -> bool:
        if record is None or expected is None:
            return False
        if record.key == expected:
            return True
        if "force_nonce" not in record.inputs:
            return False
        normalized = dict(record.inputs)
        normalized.pop("force_nonce", None)
        return (
            action_key(
                record.kind,
                inputs=normalized,
                recipe=record.recipe,
                environment=record.environment,
            )
            == expected
        )

    def record_profile(
        record: ActionRecord | None, fallback: str = default_profile
    ) -> str:
        if record is None:
            return fallback
        metadata_profile = record.metadata.get("profile")
        if isinstance(metadata_profile, str) and metadata_profile:
            return metadata_profile
        input_profile = record.inputs.get("profile")
        if isinstance(input_profile, Mapping):
            name = input_profile.get("name")
            if isinstance(name, str) and name:
                return name
        return fallback

    def expected_render(segment_id: str, profile: str) -> str | None:
        try:
            return engine.desired_render_key(segment_id, profile)
        except (BuildError, OSError, TypeError, ValueError):
            return None

    def render_for_profile(
        segment_id: str,
        profile: str,
        expected: str | None,
        legacy: ActionRecord | None = None,
    ) -> ActionRecord | None:
        profile_ref = action_value(f"render:{profile}:{segment_id}")
        profile_record = store.lookup_action(profile_ref) if profile_ref else None
        for candidate in (profile_record, legacy):
            if matches(candidate, expected):
                return candidate
        return store.lookup_action(expected) if expected else None

    def expected_cut(
        segment_id: str,
        profile: str,
        render_record: ActionRecord | None,
    ) -> str | None:
        try:
            return engine.desired_cut_key(
                segment_id,
                profile,
                render_record=render_record,
            )
        except (BuildError, OSError, TypeError, ValueError):
            return None

    expected_renders: dict[str, str | None] = {}
    expected_cuts: dict[str, str | None] = {}
    for segment in _get(project, "segments", ()):
        segment_id = str(_get(segment, "id"))
        render_ref = action_value(f"segment:{segment_id}:render")
        render_record = store.lookup_action(render_ref) if render_ref else None
        render_profile = record_profile(render_record)
        render_key = expected_render(segment_id, render_profile)
        expected_renders[segment_id] = render_key

        cut_ref = action_value(f"segment:{segment_id}:cut")
        cut_record = store.lookup_action(cut_ref) if cut_ref else None
        cut_profile = record_profile(cut_record)
        cut_render_key = expected_render(segment_id, cut_profile)
        usable_render = render_for_profile(
            segment_id,
            cut_profile,
            cut_render_key,
            render_record,
        )
        expected_cuts[segment_id] = expected_cut(
            segment_id,
            cut_profile,
            usable_render,
        )

    full_ref = action_value("full")
    full_record = store.lookup_action(full_ref) if full_ref else None
    full_profile = record_profile(full_record)
    full_cut_records: dict[str, ActionRecord] = {}
    for segment in _get(project, "segments", ()):
        segment_id = str(_get(segment, "id"))
        render_key = expected_render(segment_id, full_profile)
        render_record = render_for_profile(segment_id, full_profile, render_key)
        cut_key = expected_cut(
            segment_id,
            full_profile,
            render_record,
        )
        cut_ref = action_value(f"cut:{full_profile}:{segment_id}")
        cut_record = store.lookup_action(cut_ref) if cut_ref else None
        if matches(cut_record, cut_key):
            assert cut_record is not None
            full_cut_records[segment_id] = cut_record
    try:
        expected_full = engine.desired_full_key(
            full_profile,
            cut_records=full_cut_records,
        )
    except (BuildError, OSError, TypeError, ValueError):
        expected_full = None

    def artifact(
        key: str,
        kind: str,
        expected: str | None,
        segment_id: str | None = None,
    ) -> Mapping[str, Any]:
        action = action_value(key)
        record = store.lookup_action(action) if action else None
        blob = record.outputs.get("video") if record else None
        metadata = dict(record.metadata) if record else {}
        artifact_profile = str(metadata.get("profile", default_profile))
        if kind == "render":
            assert segment_id is not None
            path = root / "output" / artifact_profile / "renders" / f"{segment_id}.mp4"
        elif kind == "cut":
            assert segment_id is not None
            path = root / "output" / artifact_profile / "segments" / f"{segment_id}.mp4"
        else:
            path = root / "output" / artifact_profile / "full.mp4"
        current = bool(
            matches(record, expected)
            and blob
            and path.is_file()
            and hash_file(path) == blob
        )
        return {
            "path": path,
            "exists": path.is_file(),
            "current": current,
            "artifactId": action,
            "hash": blob,
            "profile": artifact_profile,
            "timing": metadata.get("timing"),
            "duration_us": metadata.get("duration_us", 0),
        }

    segments: dict[str, Any] = {}
    for segment in _get(project, "segments", ()):
        segment_id = str(_get(segment, "id"))
        segments[segment_id] = {
            "render": artifact(
                f"segment:{segment_id}:render",
                "render",
                expected_renders[segment_id],
                segment_id,
            ),
            "cut": artifact(
                f"segment:{segment_id}:cut",
                "cut",
                expected_cuts[segment_id],
                segment_id,
            ),
        }
    return {
        "segments": segments,
        "fullCut": artifact("full", "full", expected_full),
    }


def build_segment(
    project: Any,
    segment_id: str,
    profile: str | None = None,
    *,
    draft: bool = True,
    **engine_options: Any,
) -> Artifact:
    return BuildEngine(project, **engine_options).build_segment(segment_id, profile, draft=draft)


def build_full(
    project: Any,
    profile: str | None = None,
    *,
    draft: bool = True,
    **engine_options: Any,
) -> BuildResult:
    return BuildEngine(project, **engine_options).build_full(profile, draft=draft)


__all__ = [
    "Artifact",
    "BuildEngine",
    "BuildError",
    "BuildResult",
    "Builder",
    "CommandExecutor",
    "FFprobeValidator",
    "MediaRequirements",
    "MediaValidator",
    "RenderProfile",
    "ReleaseResult",
    "SubprocessExecutor",
    "build_full",
    "build_segment",
    "default_environment",
    "load_profile",
    "project_artifacts",
]
