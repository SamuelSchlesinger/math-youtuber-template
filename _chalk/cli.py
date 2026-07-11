"""Human-first, project-local command line interface for Chalk."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


class CLIError(RuntimeError):
    """An expected, user-actionable command failure."""


def _tool_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if hasattr(value, "to_record") and callable(value.to_record):
        return _jsonable(value.to_record())
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _emit_json(value: Any) -> None:
    print(json.dumps(_jsonable(value), indent=2, sort_keys=True, ensure_ascii=False))


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise CLIError(f"required command is unavailable: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        message = f"{' '.join(command)} failed with exit status {error.returncode}"
        raise CLIError(f"{message}: {detail}" if detail else message) from error


def _load_project(start: str | os.PathLike[str] | None = None) -> Any:
    from .model import ModelError, load_project

    try:
        return load_project(start)
    except ModelError as error:
        raise CLIError(str(error)) from error


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not result:
        raise CLIError("title must contain at least one letter or digit")
    return result


def _tool_tree_digest(root: Path) -> str:
    records: list[dict[str, Any]] = []
    candidates = [root / "chalk", root / "chalk_runtime.py"]
    package = root / "_chalk"
    if package.is_dir():
        candidates.extend(
            path
            for path in sorted(package.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        )
    for path in candidates:
        if not path.is_file():
            continue
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _tool_identity(root: Path) -> tuple[str, str, str]:
    version = "development"
    config = root / "pyproject.toml"
    if config.is_file():
        try:
            with config.open("rb") as stream:
                version = str(tomllib.load(stream).get("project", {}).get("version", version))
        except (OSError, tomllib.TOMLDecodeError, AttributeError):
            pass
    commit = "uncommitted"
    result = _run(["git", "rev-parse", "HEAD"], cwd=root, check=False)
    if result.returncode == 0 and result.stdout.strip():
        commit = result.stdout.strip()
        dirty = _run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            check=False,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            commit += "+dirty"
    return version, commit, _tool_tree_digest(root)


def _copy_tool_files(source_root: Path, destination: Path) -> None:
    scaffold = source_root / "scaffold"
    if not scaffold.is_dir():
        raise CLIError(
            "this Chalk copy has no scaffold; run `chalk new` from the tool repository"
        )
    shutil.copytree(scaffold, destination, dirs_exist_ok=True)
    for name in ("chalk", "chalk_runtime.py"):
        source = source_root / name
        if not source.is_file():
            raise CLIError(f"tool copy is incomplete: missing {name}")
        shutil.copy2(source, destination / name)

    package_source = source_root / "_chalk"
    package_destination = destination / "_chalk"
    for source in sorted(package_source.rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        relative = source.relative_to(package_source)
        if source.suffix != ".py" and (not relative.parts or relative.parts[0] != "ui"):
            continue
        target = package_destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (destination / "chalk").chmod(0o755)


def _substitute_project_tokens(
    destination: Path,
    *,
    title: str,
    project_id: str,
    slug: str,
    version: str,
    commit: str,
    source_tree: str,
) -> None:
    replacements = {
        "__TITLE__": title,
        "__PROJECT_ID__": project_id,
        "__SLUG__": slug,
        "__TITLE_TOML__": json.dumps(title, ensure_ascii=False),
        "__TITLE_PYTHON__": repr(title),
        "__CHALK_VERSION_TOML__": json.dumps(version, ensure_ascii=False),
        "__CHALK_COMMIT_TOML__": json.dumps(commit, ensure_ascii=False),
        "__CHALK_TREE_TOML__": json.dumps(source_tree, ensure_ascii=False),
    }
    text_suffixes = {"", ".md", ".py", ".sh", ".toml", ".txt", ".json"}
    for path in sorted(destination.rglob("*")):
        relative = path.relative_to(destination)
        if (
            not path.is_file()
            or ".git" in path.parts
            or path.suffix not in text_suffixes
            or relative.parts[0] == "_chalk"
            or relative.as_posix() in {"chalk", "chalk_runtime.py"}
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        updated = text
        for token, value in replacements.items():
            updated = updated.replace(token, value)
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def _initialize_git(destination: Path) -> tuple[str | None, str | None]:
    initialized = _run(["git", "init", "-b", "main"], cwd=destination, check=False)
    if initialized.returncode != 0:
        _run(["git", "init"], cwd=destination)
    _run(["git", "add", "-A"], cwd=destination)
    commit = _run(
        ["git", "commit", "-m", "Initialize Chalk video"],
        cwd=destination,
        check=False,
    )
    warning = None
    if commit.returncode == 0:
        head = _run(["git", "rev-parse", "HEAD"], cwd=destination).stdout.strip()
    else:
        head = None
        detail = (commit.stderr or commit.stdout).strip()
        warning = (
            "initial files are staged but Git could not commit them; configure "
            "user.name/user.email and run `git commit`"
        )
        if detail:
            warning += f" ({detail.splitlines()[-1]})"
    remotes = _run(["git", "remote"], cwd=destination).stdout.strip()
    if remotes:
        raise CLIError("new project unexpectedly inherited a Git remote")
    return head, warning


def _cmd_new(args: argparse.Namespace) -> int:
    destination = Path(args.path).expanduser().resolve()
    title = " ".join((args.title or destination.name.replace("-", " ").title()).split())
    if not title:
        raise CLIError("title must not be empty")
    slug = _slug(title)
    project_id = f"project-{uuid.uuid4().hex}"
    existed = destination.exists()
    if existed and not destination.is_dir():
        raise CLIError(f"destination is not a directory: {destination}")
    if existed and any(destination.iterdir()):
        raise CLIError(f"destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    source_root = _tool_root()
    version, commit, source_tree = _tool_identity(source_root)
    try:
        _copy_tool_files(source_root, destination)
        _substitute_project_tokens(
            destination,
            title=title,
            project_id=project_id,
            slug=slug,
            version=version,
            commit=commit,
            source_tree=source_tree,
        )
        from .model import ProjectState, save_state

        save_state(destination, ProjectState())
        for relative in (
            "context",
            "media/takes/sha256",
            "transcripts",
            "releases",
        ):
            (destination / relative).mkdir(parents=True, exist_ok=True)
        for executable in ("chalk", "setup.sh"):
            path = destination / executable
            if path.is_file():
                path.chmod(0o755)
        head, git_warning = _initialize_git(destination)
    except Exception:
        # Keep a pre-existing empty directory, but do not leave a half-created
        # project when Chalk itself created the destination.
        if not existed:
            shutil.rmtree(destination, ignore_errors=True)
        raise

    result = {
        "path": str(destination),
        "title": title,
        "project_id": project_id,
        "chalk_version": version,
        "source_commit": commit,
        "source_tree": source_tree,
        "initial_commit": head,
        "warning": git_warning,
    }
    if args.json_output:
        _emit_json(result)
    else:
        print(f"created {title!r} at {destination}")
        if git_warning:
            print(f"warning: {git_warning}")
        else:
            print("fresh Git history initialized")
        print("next: edit brief.md and outline.md")
    return 0


def _human_status(status: Any) -> None:
    print(status.title)
    if not status.segments:
        print("  no script segments yet")
    for segment in status.segments:
        scene_mark = "✓" if segment.scene == "ready" else "!"
        print(
            f"  {scene_mark} {segment.id:<24} "
            f"scene {segment.scene}; take {segment.take}; "
            f"transcript {segment.transcript}; cut {segment.cut}; "
            f"approval {segment.approval}"
        )
    summary = f"{status.errors} error(s), {status.warnings} warning(s)"
    if status.unresolved_notes:
        summary += f", {status.unresolved_notes} open note(s)"
    print(summary)
    print(f"next: {status.next_action}")


def _cmd_status(args: argparse.Namespace) -> int:
    project = _load_project()
    from .model import project_status

    status = project_status(project)
    if args.json_output:
        _emit_json(status)
    else:
        _human_status(status)
    return 0


def _diagnostic_line(diagnostic: Any) -> str:
    location = diagnostic.path or "project"
    if diagnostic.line is not None:
        location += f":{diagnostic.line}"
    segment = f" [{diagnostic.segment_id}]" if diagnostic.segment_id else ""
    return f"{diagnostic.severity.upper():7} {location}{segment}: {diagnostic.message}"


def _cmd_check(args: argparse.Namespace) -> int:
    project = _load_project()
    from .model import validate_project

    scopes: list[str | None] = list(args.segments) or [None]
    reports = [validate_project(project, scope) for scope in scopes]
    errors = sum(len(report.errors) for report in reports)
    warnings = sum(len(report.warnings) for report in reports)
    if args.json_output:
        _emit_json(
            {
                "ok": errors == 0,
                "errors": errors,
                "warnings": warnings,
                "reports": [report.to_dict() for report in reports],
            }
        )
    else:
        for report in reports:
            if len(reports) > 1:
                print(f"[{report.scope or 'project'}]")
            for diagnostic in report.diagnostics:
                print(_diagnostic_line(diagnostic))
        if errors == 0:
            print(f"✓ check passed ({warnings} warning(s))")
        else:
            print(f"check failed: {errors} error(s), {warnings} warning(s)")
    return 0 if errors == 0 else 1


def _segment_source(title: str) -> str:
    return (
        "from manim import *\n\n"
        "from chalk_runtime import ChalkScene\n"
        "from style import BG, WHITE\n\n\n"
        "class Visual(ChalkScene):\n"
        "    def construct(self):\n"
        "        self.camera.background_color = BG\n"
        f"        title = Text({title!r}, font_size=64, color=WHITE)\n"
        '        self.play_on("narration goes here", FadeIn(title), lead=0.2)\n'
        "        self.finish()\n"
    )


def _cmd_segment_add(args: argparse.Namespace) -> int:
    project = _load_project()
    from .model import ModelError, atomic_write_text

    segment_id = args.segment_id or _slug(args.title)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", segment_id):
        raise CLIError(
            "segment ids use lowercase letters, digits, '.', '_', and '-'"
        )
    try:
        existing_ids = {segment.id for segment in project.segments}
        if segment_id in existing_ids:
            raise CLIError(f"segment id already exists: {segment_id}")
        after = project.segment(args.after) if args.after else None
    except ModelError as error:
        raise CLIError(str(error)) from error

    script_path = project.root / project.config.script
    source = script_path.read_text(encoding="utf-8")
    section = (
        f"## {args.title}\n"
        f"<!-- chalk:segment {segment_id} -->\n\n"
        "Narration goes here.\n\n"
        "> **[VISUAL]** Describe the visual argument for this segment.\n"
    )
    if after is None:
        updated = source.rstrip() + "\n\n" + section
    else:
        lines = source.splitlines(keepends=True)
        following = next(
            (
                segment
                for segment in project.segments
                if segment.order == after.order + 1
            ),
            None,
        )
        insertion = following.source_line - 1 if following is not None else len(lines)
        prefix = "" if insertion == 0 or lines[insertion - 1].endswith("\n\n") else "\n"
        lines.insert(insertion, prefix + section + "\n")
        updated = "".join(lines)

    scene_path = project.root / project.config.scenes / f"{segment_id}.py"
    if scene_path.exists():
        raise CLIError(f"scene already exists: {scene_path.relative_to(project.root)}")
    scene_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with scene_path.open("x", encoding="utf-8") as stream:
            stream.write(_segment_source(args.title))
        atomic_write_text(script_path, updated)
    except Exception:
        scene_path.unlink(missing_ok=True)
        raise

    result = {
        "id": segment_id,
        "title": args.title,
        "scene": str(scene_path.relative_to(project.root)),
        "after": args.after,
    }
    if args.json_output:
        _emit_json(result)
    else:
        print(f"added {segment_id}: edit script.md and {result['scene']}")
    return 0


def _selected_segments(
    project: Any,
    values: Sequence[str],
    *,
    allow_empty: bool = False,
    purpose: str | None = None,
) -> list[Any]:
    if values:
        try:
            return [project.segment(value) for value in values]
        except Exception as error:
            raise CLIError(str(error)) from error
    if allow_empty:
        return list(project.segments)
    if len(project.segments) == 1:
        return [project.segments[0]]
    if not project.segments:
        raise CLIError("the project has no script segments")
    if purpose is not None:
        from .model import project_status

        status = project_status(project)
        predicates = {
            "watch": lambda item: item.render != "current",
            "record": lambda item: item.take != "current",
            "review": lambda item: (
                item.unresolved_notes > 0
                or item.cut != "current"
                or item.approval != "current"
            ),
        }
        predicate = predicates.get(purpose)
        if predicate is not None:
            candidate = next((item.id for item in status.segments if predicate(item)), None)
            if candidate is not None:
                return [project.segment(candidate)]
    return [project.segments[0]]


def _builder(project: Any) -> Any:
    try:
        module = importlib.import_module("_chalk.build")
    except ImportError as error:
        raise CLIError(
            "this tool copy has no build support; rerun `chalk new` from Chalk"
        ) from error
    # Prefer the fresh-state facade when present. It merges action refs after a
    # long build without clobbering a take selection or approval changed in a
    # concurrent agent/user interaction.
    builder_type = getattr(module, "Builder", None) or getattr(
        module, "BuildEngine", None
    )
    if builder_type is None:
        return module
    try:
        return builder_type(project)
    except TypeError as error:
        raise CLIError(f"could not initialize the installed build adapter: {error}") from error


def _call_capability(target: Any, names: Sequence[str], **values: Any) -> Any:
    function = next(
        (
            getattr(target, name, None)
            for name in names
            if callable(getattr(target, name, None))
        ),
        None,
    )
    if function is None:
        raise CLIError(f"installed integration does not provide {'/'.join(names)}")
    try:
        signature = inspect.signature(function)
        accepts_extra = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        kwargs = {
            key: value
            for key, value in values.items()
            if accepts_extra or key in signature.parameters
        }
        return function(**kwargs)
    except TypeError as error:
        raise CLIError(
            f"integration signature mismatch for {function.__name__}: {error}"
        ) from error


def _render_adapter(project: Any, segments: Sequence[str], profile: str | None) -> Any:
    builder = _builder(project)
    if callable(getattr(builder, "render", None)) or callable(
        getattr(builder, "render_segments", None)
    ):
        return _call_capability(
            builder,
            ("render", "render_segments"),
            project=project,
            segments=list(segments),
            segment_ids=list(segments),
            profile=profile,
            draft=True,
        )
    if callable(getattr(builder, "build_segments", None)):
        return _call_capability(
            builder,
            ("build_segments",),
            project=project,
            segments=list(segments),
            segment_ids=list(segments),
            profile=profile,
            draft=True,
        )
    if callable(getattr(builder, "build_segment", None)):
        return tuple(
            _call_capability(
                builder,
                ("build_segment",),
                project=project,
                segment=segment_id,
                segment_id=segment_id,
                profile=profile,
                draft=True,
            )
            for segment_id in segments
        )
    raise CLIError("installed build integration cannot render segments")


def _playable_artifact(result: Any) -> Any:
    final = getattr(result, "final", None)
    if final is not None:
        return final
    if isinstance(result, Mapping):
        if result.get("final") is not None:
            return result["final"]
        if result.get("path") is not None:
            return result
    if isinstance(result, (list, tuple)) and len(result) == 1:
        return result[0]
    return result


def _artifact_paths(value: Any) -> list[Path]:
    found: list[Path] = []

    def visit(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, Path):
            found.append(item)
            return
        if isinstance(item, Mapping):
            path = item.get("path")
            if isinstance(path, (str, os.PathLike)):
                found.append(Path(path))
            for key in ("final", "artifacts"):
                if key in item:
                    visit(item[key])
            return
        path = getattr(item, "path", None)
        if isinstance(path, (str, os.PathLike)):
            found.append(Path(path))
        final = getattr(item, "final", None)
        if final is not None:
            visit(final)
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return list(dict.fromkeys(path.expanduser().resolve() for path in found))


def _open_artifacts(result: Any) -> None:
    paths = _artifact_paths(_playable_artifact(result))
    if not paths:
        raise CLIError("the build integration returned no playable artifact")
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise CLIError(f"built artifact is missing: {missing[0]}")
    if sys.platform == "darwin":
        _run(["open", *[str(path) for path in paths]])
        return
    if os.name == "nt":
        for path in paths:
            _run(["cmd", "/c", "start", "", str(path)])
        return
    opener = shutil.which("xdg-open")
    if opener is None:
        raise CLIError("no media opener found; install xdg-open or use --no-play")
    for path in paths:
        _run([opener, str(path)])


def _play_result(builder: Any, result: Any) -> None:
    playable = _playable_artifact(result)
    if callable(getattr(builder, "play", None)):
        paths = _artifact_paths(playable)
        _call_capability(
            builder,
            ("play",),
            artifact=playable,
            result=result,
            path=paths[0] if paths else None,
        )
        return
    _open_artifacts(playable)


def _watch_adapter(
    project: Any,
    segments: Sequence[str],
    profile: str | None,
    *,
    play: bool = True,
    full: bool = False,
) -> Any:
    builder = _builder(project)
    # The facade's watch method owns fresh-state merging and playback. Calling
    # inherited low-level methods first would leave its deferred action refs
    # only in memory and disconnect the built cut from snapshots/the UI.
    if callable(getattr(builder, "watch", None)):
        return _call_capability(
            builder,
            ("watch",),
            project=project,
            segments=list(segments),
            segment_ids=list(segments),
            profile=profile,
            play=play,
            full=full,
        )
    if full and callable(getattr(builder, "build_full", None)):
        result = _call_capability(
            builder,
            ("build_full",),
            project=project,
            profile=profile,
            draft=True,
        )
    elif not full and len(segments) == 1 and callable(
        getattr(builder, "build_segment", None)
    ):
        result = _call_capability(
            builder,
            ("build_segment",),
            project=project,
            segment=segments[0],
            segment_id=segments[0],
            profile=profile,
            draft=True,
        )
    elif not full and callable(getattr(builder, "build_segments", None)):
        result = _call_capability(
            builder,
            ("build_segments",),
            project=project,
            segments=list(segments),
            segment_ids=list(segments),
            profile=profile,
            draft=True,
        )
    else:
        rendered = _call_capability(
            builder,
            ("render", "render_segments"),
            project=project,
            segments=list(segments),
            segment_ids=list(segments),
            profile=profile,
        )
        result = _call_capability(
            builder,
            ("full", "composite", "compose"),
            project=project,
            segments=list(segments),
            segment_ids=list(segments),
            profile=profile,
            full=full,
            renders=rendered,
        )
    if play:
        _play_result(builder, result)
    return result


def _cmd_render(args: argparse.Namespace) -> int:
    project = _load_project()
    segments = [
        segment.id
        for segment in _selected_segments(project, args.segments, allow_empty=True)
    ]
    result = _render_adapter(project, segments, args.profile)
    if args.json_output:
        _emit_json(result)
    else:
        print(f"rendered {len(segments)} segment(s)")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    project = _load_project()
    segments = [
        segment.id
        for segment in _selected_segments(
            project, [args.segment] if args.segment else [], purpose="watch"
        )
    ]
    result = _watch_adapter(project, segments, args.profile, play=not args.no_play)
    if args.json_output:
        _emit_json(result)
    return 0


def _voice_config(project: Any) -> Mapping[str, Any]:
    voice = project.config.raw.get("voice", {})
    return voice if isinstance(voice, Mapping) else {}


def _default_model(project: Any) -> str:
    voice = _voice_config(project)
    if isinstance(voice.get("model"), str):
        return str(voice["model"])
    return os.environ.get("CHALK_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")


def _default_model_revision(project: Any) -> str | None:
    voice = _voice_config(project)
    if isinstance(voice.get("revision"), str):
        return str(voice["revision"])
    return os.environ.get("CHALK_WHISPER_REVISION")


def _default_language(project: Any) -> str:
    voice = _voice_config(project)
    if isinstance(voice.get("language"), str) and voice["language"].strip():
        return str(voice["language"])
    return os.environ.get("CHALK_WHISPER_LANGUAGE", "en")


def _cmd_record(args: argparse.Namespace) -> int:
    project = _load_project()
    segment = _selected_segments(
        project, [args.segment] if args.segment else [], purpose="record"
    )[0]
    from .audio import AudioStore

    store = AudioStore(project.root)
    take = store.record_interactive(segment.id, segment.narration)
    if take is None:
        return 1
    transcript = store.transcribe_take(
        take,
        model=args.model or _default_model(project),
        revision=args.revision or _default_model_revision(project),
        options={"language": args.language or _default_language(project)},
    )
    cut = _watch_adapter(
        project.reload(), [segment.id], args.profile, play=not args.no_play
    )
    result = {"take": take, "transcript": transcript, "cut": cut}
    if args.json_output:
        _emit_json(result)
    else:
        print(f"retained {take.id[:12]} and rebuilt {segment.id}")
    return 0


def _cmd_take_import(args: argparse.Namespace) -> int:
    project = _load_project()
    segment = project.segment(args.segment)
    from .audio import AudioStore

    take = AudioStore(project.root).import_take(
        segment.id,
        segment.narration,
        args.path,
        select=args.select,
    )
    if args.json_output:
        _emit_json(take)
    else:
        selection = " and selected" if args.select else ""
        print(f"imported take {take.id[:12]}{selection}")
    return 0


def _cmd_take_list(args: argparse.Namespace) -> int:
    project = _load_project()
    from .audio import AudioStore

    store = AudioStore(project.root)
    takes = store.list_takes(args.segment)
    selections = project.state.selected_takes
    records = [
        {**take.to_record(), "selected": selections.get(take.segment_id) == take.id}
        for take in takes
    ]
    if args.json_output:
        _emit_json(records)
    elif not records:
        print("no retained takes")
    else:
        for record in records:
            mark = "*" if record["selected"] else " "
            stale = ""
            try:
                current_hash = project.segment(record["segment_id"]).narration_hash
                stale = " stale" if current_hash != record["narration_sha256"] else ""
            except Exception:
                pass
            print(
                f"{mark} {record['id'][:12]}  {record['segment_id']}  "
                f"{record['duration_us'] / 1_000_000:.2f}s{stale}"
            )
    return 0


def _cmd_take_select(args: argparse.Namespace) -> int:
    project = _load_project()
    from .audio import AudioStore

    take = AudioStore(project.root).select_take(args.segment, args.take_id)
    if args.json_output:
        _emit_json(take)
    else:
        print(f"selected {take.id[:12]} for {take.segment_id}")
    return 0


def _cmd_transcribe(args: argparse.Namespace) -> int:
    project = _load_project()
    segments = _selected_segments(project, args.segments, allow_empty=True)
    if not args.segments:
        segments = [
            segment
            for segment in segments
            if segment.id in project.state.selected_takes
        ]
        if not segments:
            raise CLIError("there are no selected recordings to transcribe")
    from .audio import AudioStore

    store = AudioStore(project.root)
    records = []
    for segment in segments:
        records.append(
            store.transcribe_selected(
                segment.id,
                model=args.model or _default_model(project),
                revision=args.revision or _default_model_revision(project),
                options={"language": args.language or _default_language(project)},
            )
        )
    if args.json_output:
        _emit_json(records)
    else:
        print(f"transcribed {len(records)} selected take(s)")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    project = _load_project()
    if args.full:
        segments = [segment.id for segment in project.segments]
    else:
        segments = [
            segment.id
            for segment in _selected_segments(project, args.segments, purpose="review")
        ]
    result = _watch_adapter(
        project, segments, args.profile, play=not args.no_play, full=args.full
    )
    if args.json_output:
        _emit_json(result)
    return 0


def _cmd_open(args: argparse.Namespace) -> int:
    project = _load_project()
    try:
        server = importlib.import_module("_chalk.server")
        serve = getattr(server, "serve")
    except (ImportError, AttributeError) as error:
        raise CLIError("this tool copy has no local review server") from error
    handle = serve(
        project,
        host="127.0.0.1",
        port=args.port,
        open_browser=not args.no_browser,
    )
    try:
        handle.wait()
    except KeyboardInterrupt:
        shutdown = getattr(handle, "shutdown", None)
        if callable(shutdown):
            shutdown()
    return 0


def _cmd_note_add(args: argparse.Namespace) -> int:
    project = _load_project()
    from .review import append_note

    timecode = f"{args.at:g}s" if args.at is not None else None
    note = append_note(
        project,
        args.text,
        segment=None if args.segment == "-" else args.segment,
        category=args.category,
        severity=args.severity,
        timecode=timecode,
        artifact=args.artifact,
    )
    if args.json_output:
        _emit_json(note)
    else:
        print(f"added {note.id}")
    return 0


def _cmd_note_list(args: argparse.Namespace) -> int:
    project = _load_project()
    from .review import list_notes

    notes = list_notes(project, include_resolved=args.all)
    if args.segment:
        notes = [note for note in notes if note.segment == args.segment]
    if args.json_output:
        _emit_json(notes)
    elif not notes:
        print("no feedback notes")
    else:
        for note in notes:
            mark = "x" if note.resolved else " "
            target = note.segment or "project"
            at = f" @{note.timecode}" if note.timecode else ""
            print(f"[{mark}] {note.id} {target}{at}: {note.text}")
    return 0


def _cmd_note_resolve(args: argparse.Namespace) -> int:
    project = _load_project()
    from .review import resolve_note

    note = resolve_note(project, args.note_id, resolution=args.resolution)
    if args.json_output:
        _emit_json(note)
    else:
        print(f"resolved {note.id}")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    project = _load_project()
    from .review import approve

    scopes: list[str | None] = list(args.segments) or [None]
    approvals = [approve(project.reload(), scope) for scope in scopes]
    if args.json_output:
        _emit_json(approvals)
    else:
        for approval in approvals:
            print(f"approved {approval.scope} at {approval.current_hash[:12]}")
    return 0


def _context_files(source: Path, files: Sequence[str]) -> list[Path]:
    if not files:
        return [source]
    if not source.is_dir():
        raise CLIError("--files requires a source directory")
    selected = [(source / value).resolve() for value in files]
    for path in selected:
        try:
            path.relative_to(source.resolve())
        except ValueError as error:
            raise CLIError(f"context file escapes source directory: {path}") from error
        if not path.is_file():
            raise CLIError(f"context file does not exist: {path}")
    return selected


def _cmd_context_add(args: argparse.Namespace) -> int:
    project = _load_project()
    from .model import atomic_write_text
    from .review import context_add

    source = Path(args.source).expanduser().resolve()
    pack = context_add(
        project,
        _context_files(source, args.files),
        name=args.label,
    )
    if args.note:
        index = pack.index_path.read_text(encoding="utf-8")
        atomic_write_text(
            pack.index_path,
            index.rstrip() + f"\n  - Purpose: {' '.join(args.note.split())}\n",
        )
    if args.json_output:
        _emit_json(pack)
    else:
        print(f"pinned {len(pack.files)} file(s) as {pack.digest[:12]}")
    return 0


def _cmd_context_list(args: argparse.Namespace) -> int:
    project = _load_project()
    directory = project.root / project.config.context
    packs = sorted(
        path for path in directory.glob("*.md") if path.name != "index.md"
    ) if directory.is_dir() else []
    records = [
        {"digest": path.stem, "path": str(path.relative_to(project.root))}
        for path in packs
    ]
    if args.json_output:
        _emit_json(records)
    elif not records:
        print("no pinned context")
    else:
        for record in records:
            print(f"{record['digest'][:12]}  {record['path']}")
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    project = _load_project()
    from .review import create_snapshot

    snapshot = create_snapshot(project, args.name)
    if args.json_output:
        _emit_json(snapshot)
    else:
        name = f" ({args.name})" if args.name else ""
        print(f"snapshot {snapshot.digest}{name}")
    return 0


_VENV_COMMANDS = {
    "doctor",
    "open",
    "record",
    "release",
    "render",
    "review",
    "transcribe",
    "watch",
}


def _maybe_reexec_in_project_venv(
    arguments: Sequence[str], command: str | None
) -> None:
    """Use the pinned project interpreter for production dependencies."""

    if command not in _VENV_COMMANDS or os.environ.get("CHALK_VENV_REEXEC"):
        return
    try:
        from .model import find_project_root

        root = find_project_root()
    except Exception:
        return
    venv = root / ".venv"
    python = venv / "bin" / "python"
    entrypoint = root / "chalk"
    if not python.is_file() or not entrypoint.is_file():
        return
    try:
        already_active = Path(sys.prefix).resolve() == venv.resolve()
    except OSError:
        already_active = False
    if already_active:
        return
    environment = dict(os.environ)
    environment["CHALK_VENV_REEXEC"] = "1"
    environment["VIRTUAL_ENV"] = str(venv)
    environment["PATH"] = os.pathsep.join(
        filter(None, [str(venv / "bin"), environment.get("PATH", "")])
    )
    os.execve(
        str(python),
        [str(python), str(entrypoint), *arguments],
        environment,
    )


def _cmd_checkpoint(args: argparse.Namespace) -> int:
    project = _load_project()
    from .review import create_snapshot

    snapshot = create_snapshot(project, args.name)
    # A Chalk project is a dedicated repository, and generated/cache/scratch
    # paths are already excluded by its .gitignore.  Staging the whole worktree
    # ensures new assets and helper modules are not silently omitted.
    _run(["git", "add", "-A"], cwd=project.root)
    _run(["git", "commit", "-m", f"Checkpoint: {args.name}"], cwd=project.root)
    head = _run(["git", "rev-parse", "HEAD"], cwd=project.root).stdout.strip()
    result = {"snapshot": snapshot.digest, "commit": head, "name": args.name}
    if args.json_output:
        _emit_json(result)
    else:
        print(f"checkpoint {args.name}: {head[:12]} (snapshot {snapshot.digest[:12]})")
    return 0


def _release_adapter(project: Any, name: str, profile: str | None) -> Any:
    return _call_capability(
        _builder(project),
        ("release",),
        project=project,
        name=name,
        profile=profile or "final",
    )


def _cmd_release(args: argparse.Namespace) -> int:
    project = _load_project()
    result = _release_adapter(project, args.name, args.profile)
    if args.json_output:
        _emit_json(result)
    else:
        print(f"released {args.name}")
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    project = _load_project()
    if args.ref:
        from .review import resolve_snapshot

        ref_path = project.root / ".chalk" / "refs" / f"{args.ref}.json"
        snapshot_path = project.root / ".chalk" / "snapshots" / f"{args.ref}.json"
        if ref_path.is_file() or snapshot_path.is_file():
            snapshot = resolve_snapshot(project, args.ref)
            components = snapshot.data.get("components", {})
            embedded = components.get("actions", [])
            roots = components.get("current_actions", {})
            root_action = roots.get("full") if isinstance(roots, Mapping) else None
            result = {
                "snapshot": snapshot.digest,
                "rootAction": root_action,
                "rootActions": roots,
                "actions": embedded,
                "unresolvedActionRefs": components.get(
                    "unresolved_action_refs", []
                ),
            }
            if args.json_output:
                _emit_json(result)
            else:
                print(f"snapshot lineage for {snapshot.digest}")
                for record in embedded:
                    print(
                        f"  {str(record.get('kind', 'action')):<18} "
                        f"{str(record.get('key', ''))[:19]}  "
                        f"{len(record.get('dependencies', []))} input action(s)"
                    )
            return 0
    actions = project.state.current_actions
    if args.ref:
        action_key = actions.get(args.ref)
        if action_key is None:
            action_key = actions.get(f"segment:{args.ref}:cut", args.ref)
    else:
        action_key = actions.get("full")
        if action_key is None:
            for segment in project.segments:
                candidate = actions.get(f"segment:{segment.id}:cut")
                if candidate is not None:
                    action_key = candidate
                    break
    if action_key is None:
        raise CLIError("there is no current build to trace; run `chalk watch` first")
    records = _call_capability(
        _builder(project),
        ("trace",),
        artifact=action_key,
        artifact_or_key=action_key,
        key=action_key,
        root_key=action_key,
    )
    result = {"rootAction": action_key, "actions": records}
    if args.json_output:
        _emit_json(result)
    else:
        print(f"action lineage for {action_key}")
        for record in records:
            kind = getattr(record, "kind", "action")
            key = getattr(record, "key", "")
            dependencies = getattr(record, "dependencies", ())
            print(f"  {kind:<18} {str(key)[:19]}  {len(dependencies)} input action(s)")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    project = None
    try:
        project = _load_project()
    except CLIError:
        pass
    tools = {
        name: shutil.which(name)
        for name in (
            "git",
            "ffmpeg",
            "ffprobe",
            "rec",
            "latex",
            "dvisvgm",
            "pkg-config",
        )
    }
    modules = {
        name: importlib.util.find_spec(name) is not None
        for name in ("manim", "mlx_whisper")
    }
    result = {
        "python": sys.version.split()[0],
        "project": str(project.root) if project is not None else None,
        "tools": tools,
        "modules": modules,
    }
    if args.json_output:
        _emit_json(result)
    else:
        print(f"Python {result['python']}")
        for name, path in tools.items():
            print(f"  {'✓' if path else '!'} {name}: {path or 'missing'}")
        for name, available in modules.items():
            print(f"  {'✓' if available else '!'} {name}")
    return 0


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit machine-readable JSON",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chalk",
        description="AI-native Markdown, Manim, voice, and review workspace",
    )
    parser.set_defaults(json_output=False)
    parser.add_argument("--json", dest="json_output", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    status = subparsers.add_parser("status", help="show current state and next action")
    _add_json(status)
    status.set_defaults(handler=_cmd_status)

    new = subparsers.add_parser("new", help="create an independent video project")
    new.add_argument("path")
    new.add_argument("--title")
    _add_json(new)
    new.set_defaults(handler=_cmd_new)

    check = subparsers.add_parser("check", help="validate a project or selected segments")
    check.add_argument("segments", nargs="*")
    _add_json(check)
    check.set_defaults(handler=_cmd_check)

    segment = subparsers.add_parser("segment", help="add a stable script/scene pair")
    segment_commands = segment.add_subparsers(dest="segment_command", required=True)
    segment_add = segment_commands.add_parser("add", help="add a segment")
    segment_add.add_argument("title")
    segment_add.add_argument("--id", dest="segment_id")
    segment_add.add_argument("--after")
    _add_json(segment_add)
    segment_add.set_defaults(handler=_cmd_segment_add)

    render = subparsers.add_parser("render", help="render without opening a player")
    render.add_argument("segments", nargs="*")
    render.add_argument("--profile")
    _add_json(render)
    render.set_defaults(handler=_cmd_render)

    watch = subparsers.add_parser("watch", help="build and play a segment proxy")
    watch.add_argument("segment", nargs="?")
    watch.add_argument("--profile")
    watch.add_argument("--no-play", action="store_true")
    _add_json(watch)
    watch.set_defaults(handler=_cmd_watch)

    record = subparsers.add_parser("record", help="record, transcribe, rebuild, and review")
    record.add_argument("segment", nargs="?")
    record.add_argument("--profile")
    record.add_argument("--model")
    record.add_argument("--revision")
    record.add_argument("--language")
    record.add_argument("--no-play", action="store_true")
    _add_json(record)
    record.set_defaults(handler=_cmd_record)

    take = subparsers.add_parser("take", help="manage immutable recordings")
    take_commands = take.add_subparsers(dest="take_command", required=True)
    take_import = take_commands.add_parser("import", help="import an existing recording")
    take_import.add_argument("segment")
    take_import.add_argument("path")
    take_import.add_argument("--select", action="store_true")
    _add_json(take_import)
    take_import.set_defaults(handler=_cmd_take_import)
    take_list = take_commands.add_parser("list", help="list retained recordings")
    take_list.add_argument("segment", nargs="?")
    _add_json(take_list)
    take_list.set_defaults(handler=_cmd_take_list)
    take_select = take_commands.add_parser("select", help="select a retained recording")
    take_select.add_argument("segment")
    take_select.add_argument("take_id")
    _add_json(take_select)
    take_select.set_defaults(handler=_cmd_take_select)

    transcribe = subparsers.add_parser("transcribe", help="transcribe selected recordings")
    transcribe.add_argument("segments", nargs="*")
    transcribe.add_argument("--model")
    transcribe.add_argument("--revision")
    transcribe.add_argument("--language")
    _add_json(transcribe)
    transcribe.set_defaults(handler=_cmd_transcribe)

    review = subparsers.add_parser("review", help="build and play a segment or full cut")
    review.add_argument("segments", nargs="*")
    review.add_argument("--full", action="store_true")
    review.add_argument("--profile", default="review")
    review.add_argument("--no-play", action="store_true")
    _add_json(review)
    review.set_defaults(handler=_cmd_review)

    open_command = subparsers.add_parser("open", help="open the local review surface")
    open_command.add_argument("--no-browser", action="store_true")
    open_command.add_argument("--port", type=int, default=0)
    open_command.set_defaults(handler=_cmd_open)

    note = subparsers.add_parser("note", help="capture and resolve feedback")
    note_commands = note.add_subparsers(dest="note_command", required=True)
    note_add = note_commands.add_parser("add", help="add durable feedback")
    note_add.add_argument("segment", help="segment id, or '-' for project-level feedback")
    note_add.add_argument("text")
    note_add.add_argument("--at", type=float)
    note_add.add_argument("--category", default="general")
    note_add.add_argument("--severity", default="normal")
    note_add.add_argument("--artifact")
    _add_json(note_add)
    note_add.set_defaults(handler=_cmd_note_add)
    note_list = note_commands.add_parser("list", help="list feedback")
    note_list.add_argument("--segment")
    note_list.add_argument("--all", action="store_true")
    _add_json(note_list)
    note_list.set_defaults(handler=_cmd_note_list)
    note_resolve = note_commands.add_parser("resolve", help="resolve checked feedback")
    note_resolve.add_argument("note_id")
    note_resolve.add_argument("--resolution")
    _add_json(note_resolve)
    note_resolve.set_defaults(handler=_cmd_note_resolve)

    approve = subparsers.add_parser("approve", help="approve current segment or project content")
    approve.add_argument("segments", nargs="*")
    _add_json(approve)
    approve.set_defaults(handler=_cmd_approve)

    context = subparsers.add_parser("context", help="pin exact reference material")
    context_commands = context.add_subparsers(dest="context_command", required=True)
    context_add = context_commands.add_parser("add", help="add a content-addressed context pack")
    context_add.add_argument("source")
    context_add.add_argument("--files", nargs="*", default=[])
    context_add.add_argument("--label")
    context_add.add_argument("--note")
    _add_json(context_add)
    context_add.set_defaults(handler=_cmd_context_add)
    context_list = context_commands.add_parser("list", help="list pinned context")
    _add_json(context_list)
    context_list.set_defaults(handler=_cmd_context_list)

    snapshot = subparsers.add_parser("snapshot", help="write an exact content snapshot")
    snapshot.add_argument("name", nargs="?")
    _add_json(snapshot)
    snapshot.set_defaults(handler=_cmd_snapshot)

    checkpoint = subparsers.add_parser("checkpoint", help="snapshot and commit known source")
    checkpoint.add_argument("name")
    _add_json(checkpoint)
    checkpoint.set_defaults(handler=_cmd_checkpoint)

    release = subparsers.add_parser("release", help="build and record a named release")
    release.add_argument("name")
    release.add_argument("--profile", default="final")
    _add_json(release)
    release.set_defaults(handler=_cmd_release)

    trace = subparsers.add_parser("trace", help="inspect snapshot lineage")
    trace.add_argument("ref", nargs="?")
    _add_json(trace)
    trace.set_defaults(handler=_cmd_trace)

    doctor = subparsers.add_parser("doctor", help="inspect local tools")
    _add_json(doctor)
    doctor.set_defaults(handler=_cmd_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    try:
        args = parser.parse_args(arguments)
        if args.command is None:
            args.handler = _cmd_status
        _maybe_reexec_in_project_venv(arguments, args.command)
        return int(args.handler(args))
    except KeyboardInterrupt:
        if "--json" in arguments:
            _emit_json({"ok": False, "error": "interrupted"})
        else:
            print("interrupted", file=sys.stderr)
        return 130
    except (
        ImportError,
        RuntimeError,
        OSError,
        ValueError,
        KeyError,
    ) as error:
        if "--json" in arguments:
            _emit_json({"ok": False, "error": str(error)})
        else:
            print(f"error: {error}", file=sys.stderr)
        if os.environ.get("CHALK_DEBUG"):
            raise
        return 2


__all__ = ["CLIError", "build_parser", "main"]
