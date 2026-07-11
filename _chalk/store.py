"""Content-addressed storage and immutable action records.

The cache is deliberately small and boring.  Blobs are addressed by their raw
SHA-256 digest, while action keys hash canonical JSON with a domain separator.
Human-facing output paths never participate in cache identity.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Callable, Iterator, Mapping, Sequence

try:  # pragma: no cover - all supported production platforms currently have it
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


HASH_ALGORITHM = "sha256"
ACTION_SCHEMA = "chalk.action.v1"
ACTION_DOMAIN = b"chalk-action-v1\0"
TREE_DOMAIN = b"chalk-tree-v1\0"
_DIGEST_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")


class StoreError(RuntimeError):
    """Base error raised by the local artifact store."""


class CacheConflictError(StoreError):
    """The same action key produced a different immutable result."""


def _jsonable(value: Any) -> Any:
    """Convert common structured values to the canonical JSON data model."""

    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        converted = [_jsonable(item) for item in value]
        return sorted(converted, key=lambda item: canonical_json(item))
    if isinstance(value, Path):
        # Paths in action descriptions must be project-relative.  Callers that
        # have an absolute path should hash its contents and use that digest.
        if value.is_absolute():
            raise ValueError("absolute paths are not valid canonical action inputs")
        return value.as_posix()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON suitable for hashing.

    Python's encoder is stable for this restricted data model: keys are sorted,
    insignificant whitespace is removed, NaN/infinity are rejected, and Unicode
    is encoded directly.  Build records use integer microseconds rather than
    floating-point timestamps.
    """

    return json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def hash_bytes(data: bytes | bytearray | memoryview) -> str:
    """Return the algorithm-qualified SHA-256 ID of *data*."""

    return f"{HASH_ALGORITHM}:{hashlib.sha256(bytes(data)).hexdigest()}"


def hash_file(path: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024) -> str:
    """Hash file bytes without incorporating its name, mtime, or absolute path."""

    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return f"{HASH_ALGORITHM}:{digest.hexdigest()}"


def _digest_hex(identifier: str) -> str:
    match = _DIGEST_RE.fullmatch(identifier)
    if not match:
        raise ValueError(f"invalid SHA-256 identifier: {identifier!r}")
    return match.group(1)


def hash_tree(
    root: str | os.PathLike[str],
    *,
    include: Callable[[Path], bool] | None = None,
) -> str:
    """Hash a directory tree by relative path, file mode, and content.

    Absolute paths and mtimes are intentionally absent.  Symlinks are rejected
    so a tree cannot smuggle an undeclared dependency outside its root.
    """

    root_path = Path(root)
    if not root_path.exists():
        return hash_bytes(TREE_DOMAIN + canonical_json({"entries": []}))
    if not root_path.is_dir():
        raise ValueError(f"tree root is not a directory: {root_path}")

    entries: list[dict[str, Any]] = []
    paths = sorted(root_path.rglob("*"), key=lambda path: path.relative_to(root_path).as_posix())
    for path in paths:
        relative = path.relative_to(root_path)
        if include is not None and not include(relative):
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"symlinks are not valid tree inputs: {relative.as_posix()}")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"non-regular tree input: {relative.as_posix()}")
        entries.append(
            {
                "path": relative.as_posix(),
                "blob": hash_file(path),
                "executable": bool(metadata.st_mode & 0o111),
            }
        )
    digest = hashlib.sha256(TREE_DOMAIN + canonical_json({"entries": entries})).hexdigest()
    return f"{HASH_ALGORITHM}:{digest}"


def action_key(
    kind: str,
    *,
    inputs: Mapping[str, Any],
    recipe: Mapping[str, Any] | str,
    environment: Mapping[str, Any] | str,
) -> str:
    """Hash the exact derivation description for an action."""

    payload = {
        "schema": ACTION_SCHEMA,
        "kind": kind,
        "inputs": inputs,
        "recipe": recipe,
        "environment": environment,
    }
    digest = hashlib.sha256(ACTION_DOMAIN + canonical_json(payload)).hexdigest()
    return f"{HASH_ALGORITHM}:{digest}"


def take_path(root: str | os.PathLike[str], segment_id: str, digest: str) -> Path:
    """Canonical retained-take path used by the model and audio layers."""

    del segment_id  # Selection resolves take id -> audio hash through takes.json.
    return Path(root) / "media" / "takes" / "sha256" / f"{_digest_hex(digest)}.flac"


def transcript_path(root: str | os.PathLike[str], segment_id: str, digest: str) -> Path:
    """Canonical materialized transcript path for a selected audio digest."""

    del segment_id  # Audio content, rather than a mutable segment alias, owns it.
    return Path(root) / "transcripts" / f"{_digest_hex(digest)}.json"


@dataclass(frozen=True)
class ActionRecord:
    """Immutable description of one successful action and its output blobs."""

    key: str
    kind: str
    inputs: Mapping[str, Any]
    recipe: Mapping[str, Any] | str
    environment: Mapping[str, Any] | str
    outputs: Mapping[str, str]
    dependencies: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = ACTION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "key": self.key,
            "kind": self.kind,
            "inputs": _jsonable(self.inputs),
            "recipe": _jsonable(self.recipe),
            "environment": _jsonable(self.environment),
            "outputs": dict(sorted(self.outputs.items())),
            "dependencies": list(self.dependencies),
            "metadata": _jsonable(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActionRecord":
        if value.get("schema") != ACTION_SCHEMA:
            raise StoreError(f"unsupported action schema: {value.get('schema')!r}")
        return cls(
            schema=str(value["schema"]),
            key=str(value["key"]),
            kind=str(value["kind"]),
            inputs=dict(value.get("inputs", {})),
            recipe=value.get("recipe", {}),
            environment=value.get("environment", {}),
            outputs={str(name): str(blob) for name, blob in value.get("outputs", {}).items()},
            dependencies=tuple(str(item) for item in value.get("dependencies", [])),
            metadata=dict(value.get("metadata", {})),
        )

    def derived_key(self) -> str:
        return action_key(
            self.kind,
            inputs=self.inputs,
            recipe=self.recipe,
            environment=self.environment,
        )


@dataclass(frozen=True)
class ActionProducts:
    """Files and optional metadata returned by an action producer."""

    outputs: Mapping[str, str | os.PathLike[str] | bytes]
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionResult:
    record: ActionRecord
    cache_hit: bool


class Store:
    """Verified local content-addressed store rooted in a Chalk project."""

    def __init__(self, project_root: str | os.PathLike[str]) -> None:
        self.project_root = Path(project_root).resolve()
        self.cache_root = self.project_root / ".chalk" / "cache"
        self.blobs_root = self.cache_root / "blobs" / HASH_ALGORITHM
        self.actions_root = self.cache_root / "actions" / HASH_ALGORITHM
        self.locks_root = self.cache_root / "locks" / HASH_ALGORITHM
        self.temp_root = self.cache_root / "tmp"
        for directory in (
            self.blobs_root,
            self.actions_root,
            self.locks_root,
            self.temp_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def blob_path(self, blob_id: str) -> Path:
        digest = _digest_hex(blob_id)
        return self.blobs_root / digest[:2] / digest[2:]

    def action_path(self, key: str) -> Path:
        digest = _digest_hex(key)
        return self.actions_root / digest[:2] / f"{digest[2:]}.json"

    def _lock_path(self, key: str) -> Path:
        digest = _digest_hex(key)
        return self.locks_root / digest[:2] / f"{digest[2:]}.lock"

    def put_bytes(self, data: bytes) -> str:
        blob_id = hash_bytes(data)
        destination = self.blob_path(blob_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.verify_blob(blob_id):
            return blob_id
        self._atomic_write(destination, data, immutable=True)
        if not self.verify_blob(blob_id):  # pragma: no cover - protects disk faults
            raise StoreError(f"failed to install blob {blob_id}")
        return blob_id

    def put_file(self, source: str | os.PathLike[str]) -> str:
        """Copy and hash *source* in one pass before atomically installing it."""

        source_path = Path(source)
        self.blobs_root.mkdir(parents=True, exist_ok=True)
        fd, staged_name = tempfile.mkstemp(prefix="blob-", dir=self.blobs_root)
        staged = Path(staged_name)
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as target, source_path.open("rb") as source_file:
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            blob_id = f"{HASH_ALGORITHM}:{digest.hexdigest()}"
            destination = self.blob_path(blob_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if self.verify_blob(blob_id):
                staged.unlink(missing_ok=True)
                return blob_id
            os.chmod(staged, 0o444)
            os.replace(staged, destination)
            _fsync_directory(destination.parent)
            if not self.verify_blob(blob_id):  # pragma: no cover
                raise StoreError(f"failed to install blob {blob_id}")
            return blob_id
        finally:
            staged.unlink(missing_ok=True)

    def verify_blob(self, blob_id: str) -> bool:
        path = self.blob_path(blob_id)
        try:
            return path.is_file() and hash_file(path) == f"{HASH_ALGORITHM}:{_digest_hex(blob_id)}"
        except OSError:
            return False

    def read_blob(self, blob_id: str) -> bytes:
        if not self.verify_blob(blob_id):
            raise StoreError(f"missing or corrupt blob {blob_id}")
        return self.blob_path(blob_id).read_bytes()

    def lookup_action(self, key: str, *, verify_outputs: bool = True) -> ActionRecord | None:
        path = self.action_path(key)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            record = ActionRecord.from_dict(value)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, StoreError):
            return None
        normalized_key = f"{HASH_ALGORITHM}:{_digest_hex(key)}"
        if record.key != normalized_key or record.derived_key() != normalized_key:
            return None
        if verify_outputs and not all(self.verify_blob(blob) for blob in record.outputs.values()):
            return None
        return record

    def run_action(
        self,
        kind: str,
        *,
        inputs: Mapping[str, Any],
        recipe: Mapping[str, Any] | str,
        environment: Mapping[str, Any] | str,
        producer: Callable[[Path], ActionProducts | Mapping[str, str | os.PathLike[str] | bytes]],
        dependencies: Sequence[str] = (),
    ) -> ActionResult:
        """Run or reuse an action, publishing its record only after all outputs."""

        key = action_key(kind, inputs=inputs, recipe=recipe, environment=environment)
        cached = self.lookup_action(key)
        if cached is not None:
            return ActionResult(cached, cache_hit=True)

        with self.action_lock(key):
            cached = self.lookup_action(key)
            if cached is not None:
                return ActionResult(cached, cache_hit=True)

            workdir = Path(tempfile.mkdtemp(prefix=f"{_digest_hex(key)[:12]}-", dir=self.temp_root))
            try:
                produced = producer(workdir)
                products = produced if isinstance(produced, ActionProducts) else ActionProducts(produced)
                if not products.outputs:
                    raise StoreError(f"action {kind!r} produced no outputs")
                output_ids: dict[str, str] = {}
                for name, output in sorted(products.outputs.items()):
                    if not name or "/" in name or "\\" in name:
                        raise StoreError(f"invalid action output name: {name!r}")
                    if isinstance(output, bytes):
                        output_ids[name] = self.put_bytes(output)
                    else:
                        output_path = Path(output)
                        if not output_path.is_file():
                            raise StoreError(f"action output does not exist: {output_path}")
                        output_ids[name] = self.put_file(output_path)

                record = ActionRecord(
                    key=key,
                    kind=kind,
                    inputs=dict(inputs),
                    recipe=recipe,
                    environment=environment,
                    outputs=output_ids,
                    dependencies=tuple(dependencies),
                    metadata=dict(products.metadata),
                )
                self._publish_action(record)
                verified = self.lookup_action(key)
                if verified is None:  # pragma: no cover
                    raise StoreError(f"failed to publish verified action {key}")
                return ActionResult(verified, cache_hit=False)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    def materialize(
        self,
        blob_id: str,
        destination: str | os.PathLike[str],
        *,
        mode: int = 0o644,
    ) -> Path:
        """Atomically copy a verified immutable blob to a human-facing alias."""

        source = self.blob_path(blob_id)
        if not self.verify_blob(blob_id):
            raise StoreError(f"cannot materialize missing or corrupt blob {blob_id}")
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{destination_path.name}.", dir=destination_path.parent)
        temporary = Path(temporary_name)
        try:
            with source.open("rb") as input_file, os.fdopen(fd, "wb") as output_file:
                shutil.copyfileobj(input_file, output_file)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, destination_path)
            _fsync_directory(destination_path.parent)
            return destination_path
        finally:
            temporary.unlink(missing_ok=True)

    def trace(self, root_key: str) -> tuple[ActionRecord, ...]:
        """Return the deterministic transitive action closure for *root_key*."""

        ordered: list[ActionRecord] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visited:
                return
            if key in visiting:
                raise StoreError(f"cycle in action records at {key}")
            record = self.lookup_action(key)
            if record is None:
                raise StoreError(f"missing action record in trace: {key}")
            visiting.add(key)
            for dependency in record.dependencies:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)
            ordered.append(record)

        visit(root_key)
        return tuple(ordered)

    @contextmanager
    def action_lock(self, key: str) -> Iterator[None]:
        """Advisory cross-process single-flight lock for an action key."""

        path = self._lock_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _publish_action(self, record: ActionRecord) -> None:
        path = self.action_path(record.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: ActionRecord | None = None
        try:
            existing = ActionRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, StoreError):
            pass
        if existing is not None and canonical_json(existing.to_dict()) != canonical_json(record.to_dict()):
            raise CacheConflictError(f"action {record.key} produced a conflicting result")
        if existing is None:
            self._atomic_write(path, canonical_json(record.to_dict()) + b"\n", immutable=True)

    @staticmethod
    def _atomic_write(destination: Path, data: bytes, *, immutable: bool) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o444 if immutable else 0o644)
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:  # pragma: no cover - unusual filesystems may reject it
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ACTION_SCHEMA",
    "ActionProducts",
    "ActionRecord",
    "ActionResult",
    "CacheConflictError",
    "Store",
    "StoreError",
    "action_key",
    "canonical_json",
    "hash_bytes",
    "hash_file",
    "hash_tree",
    "take_path",
    "transcript_path",
]
