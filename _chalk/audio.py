"""Immutable voice takes and transcript caching.

This module owns only durable audio state.  Recording, conversion, probing and
transcription are injectable so unit tests (and alternate front ends) do not
need SoX, ffmpeg, ffprobe, Manim, or MLX.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .timeline import MICROSECONDS_PER_SECOND, TranscriptWord, narration_digest


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Converter = Callable[[Path, Path], None]
DurationProber = Callable[[Path], int]
Recorder = Callable[[Path], None]
Transcriber = Callable[[Path, str, Mapping[str, Any]], Mapping[str, Any]]
SnapshotDownloader = Callable[[str, str], Path]


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command), check=True, capture_output=True, text=True
    )


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path, default: object) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _take_identity(segment_id: str, narration_sha256: str, audio_sha256: str) -> str:
    return _sha256_bytes(
        _canonical_json(
            {
                "segment_id": segment_id,
                "narration_sha256": narration_sha256,
                "audio_sha256": audio_sha256,
            }
        ).encode("utf-8")
    )


@dataclass(frozen=True, slots=True)
class TakeRecord:
    id: str
    segment_id: str
    narration_sha256: str
    audio_sha256: str
    path: str
    duration_us: int
    created_at: str
    metadata: Mapping[str, Any]

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "segment_id": self.segment_id,
            "narration_sha256": self.narration_sha256,
            "audio_sha256": self.audio_sha256,
            "path": self.path,
            "duration_us": self.duration_us,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> TakeRecord:
        return cls(
            id=str(value["id"]),
            segment_id=str(value["segment_id"]),
            narration_sha256=str(value["narration_sha256"]),
            audio_sha256=str(value["audio_sha256"]),
            path=str(value["path"]),
            duration_us=int(value["duration_us"]),
            created_at=str(value.get("created_at", "")),
            metadata=dict(value.get("metadata", {})),
        )

    def is_stale_for(
        self,
        narration: str | None = None,
        *,
        narration_sha256: str | None = None,
    ) -> bool:
        expected = narration_sha256 or narration_digest(narration or "")
        return self.narration_sha256 != expected


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    audio_sha256: str
    cache_key: str
    model: str
    model_revision: str | None
    model_fingerprint: str
    options: Mapping[str, Any]
    words: tuple[TranscriptWord, ...]
    created_at: str
    provenance: Mapping[str, Any]

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "audio_sha256": self.audio_sha256,
            "cache_key": self.cache_key,
            "model": self.model,
            "model_revision": self.model_revision,
            "model_fingerprint": self.model_fingerprint,
            "options": dict(self.options),
            "words": [word.to_record() for word in self.words],
            "created_at": self.created_at,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> TranscriptRecord:
        return cls(
            audio_sha256=str(value["audio_sha256"]),
            cache_key=str(value["cache_key"]),
            model=str(value["model"]),
            model_revision=(
                str(value["model_revision"])
                if value.get("model_revision") is not None
                else None
            ),
            model_fingerprint=str(value["model_fingerprint"]),
            options=dict(value.get("options", {})),
            words=tuple(
                TranscriptWord.from_value(word) for word in value.get("words", ())
            ),
            created_at=str(value.get("created_at", "")),
            provenance=dict(value.get("provenance", {})),
        )


class AudioStore:
    """Project-local immutable take and transcript storage."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        runner: CommandRunner | None = None,
        converter: Converter | None = None,
        prober: DurationProber | None = None,
        transcriber: Transcriber | None = None,
        recorder: Recorder | None = None,
        snapshot_downloader: SnapshotDownloader | None = None,
    ) -> None:
        self.root = Path(project_root).resolve()
        self.runner = runner or _default_runner
        self.converter = converter or self._convert_with_ffmpeg
        self.prober = prober or self._probe_with_ffprobe
        self.transcriber = transcriber
        self.recorder = recorder
        self.snapshot_downloader = snapshot_downloader

        self.media_root = self.root / "media" / "takes"
        self.blob_root = self.media_root / "sha256"
        self.take_index_path = self.media_root / "takes.json"
        # The project has one canonical selection map.  Keep it in the shared
        # tracked state rather than introducing audio-owned pointer state.
        self.selection_path = self.root / ".chalk" / "state.json"
        self.transcript_root = self.root / "transcripts"
        self.scratch_root = self.root / ".chalk" / "scratch"

    def _convert_with_ffmpeg(self, source: Path, destination: Path) -> None:
        self.runner(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-map_metadata",
                "-1",
                "-vn",
                "-c:a",
                "flac",
                "-compression_level",
                "8",
                str(destination),
            ]
        )

    def _probe_with_ffprobe(self, source: Path) -> int:
        result = self.runner(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(source),
            ]
        )
        payload = json.loads(result.stdout)
        duration = Decimal(str(payload["format"]["duration"]))
        return int(
            (duration * MICROSECONDS_PER_SECOND).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )

    def _take_records(self) -> list[TakeRecord]:
        payload = _load_json(
            self.take_index_path, {"schema_version": 1, "takes": []}
        )
        return [TakeRecord.from_record(value) for value in payload.get("takes", ())]

    def _write_take_records(self, takes: Sequence[TakeRecord]) -> None:
        ordered = sorted(takes, key=lambda take: (take.segment_id, take.created_at, take.id))
        _atomic_json(
            self.take_index_path,
            {"schema_version": 1, "takes": [take.to_record() for take in ordered]},
        )

    def _selections(self) -> dict[str, str]:
        from .model import load_state

        return dict(load_state(self.root).selected_takes)

    def list_takes(self, segment_id: str | None = None) -> tuple[TakeRecord, ...]:
        takes = self._take_records()
        if segment_id is not None:
            takes = [take for take in takes if take.segment_id == segment_id]
        return tuple(takes)

    def get_take(self, take_id: str) -> TakeRecord:
        for take in self._take_records():
            if take.id == take_id:
                return take
        raise KeyError(f"unknown take {take_id!r}")

    def blob_path(self, audio_sha256: str) -> Path:
        return self.blob_root / f"{audio_sha256}.flac"

    def take_path(self, take: TakeRecord | str) -> Path:
        record = self.get_take(take) if isinstance(take, str) else take
        return self.root / record.path

    def import_take(
        self,
        segment_id: str,
        narration: str,
        source_path: str | Path,
        *,
        select: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> TakeRecord:
        """Convert and retain a take, selecting it only after full success."""

        source = Path(source_path)
        if not source.is_file():
            raise FileNotFoundError(source)
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix="converted-", suffix=".flac", dir=self.scratch_root
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            self.converter(source, temporary)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("audio conversion produced no retained FLAC")
            audio_sha256 = _sha256_file(temporary)
            duration_us = int(self.prober(temporary))
            if duration_us <= 0:
                raise ValueError(f"recording duration must be positive, got {duration_us}us")

            destination = self.blob_path(audio_sha256)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if _sha256_file(destination) != audio_sha256:
                    raise RuntimeError(f"retained audio hash mismatch at {destination}")
                temporary.unlink(missing_ok=True)
            else:
                os.replace(temporary, destination)

            narration_sha256 = narration_digest(narration)
            take_id = _take_identity(segment_id, narration_sha256, audio_sha256)
            from .model import project_lock

            with project_lock(self.root, "takes"):
                records = self._take_records()
                existing = next((take for take in records if take.id == take_id), None)
                if existing is None:
                    existing = TakeRecord(
                        id=take_id,
                        segment_id=segment_id,
                        narration_sha256=narration_sha256,
                        audio_sha256=audio_sha256,
                        path=destination.relative_to(self.root).as_posix(),
                        duration_us=duration_us,
                        created_at=_utc_now(),
                        metadata=dict(metadata or {}),
                    )
                    records.append(existing)
                    self._write_take_records(records)
            if select:
                self.select_take(segment_id, existing.id)
            return existing
        finally:
            temporary.unlink(missing_ok=True)

    def select_take(self, segment_id: str, take_id: str) -> TakeRecord:
        take = self.get_take(take_id)
        if take.segment_id != segment_id:
            raise ValueError(
                f"take {take_id!r} belongs to {take.segment_id!r}, not {segment_id!r}"
            )
        if not self.take_path(take).is_file():
            raise FileNotFoundError(self.take_path(take))
        from .model import load_state, project_lock, save_state

        with project_lock(self.root, "state"):
            state = load_state(self.root)
            state.selected_takes[segment_id] = take.id
            save_state(self.root, state)
        return take

    def selected_take(
        self,
        segment_id: str,
        *,
        narration: str | None = None,
        require_fresh: bool = False,
    ) -> TakeRecord | None:
        take_id = self._selections().get(segment_id)
        if take_id is None:
            return None
        take = self.get_take(take_id)
        if take.segment_id != segment_id:
            raise ValueError(
                f"selected take {take.id!r} belongs to {take.segment_id!r}, "
                f"not {segment_id!r}"
            )
        if require_fresh and narration is not None and take.is_stale_for(narration):
            raise ValueError(
                f"selected take {take.id!r} was recorded against narration "
                f"{take.narration_sha256}, not {narration_digest(narration)}"
            )
        return take

    def selected_take_path(self, segment_id: str) -> Path | None:
        take = self.selected_take(segment_id)
        return self.take_path(take) if take is not None else None

    def promote_recording(
        self,
        segment_id: str,
        narration: str,
        temporary_path: str | Path,
        *,
        keep: bool,
        metadata: Mapping[str, Any] | None = None,
    ) -> TakeRecord | None:
        """Promote a scratch recording without risking the prior selection."""

        temporary = Path(temporary_path)
        if not keep:
            temporary.unlink(missing_ok=True)
            return None
        # import_take changes the pointer last.  On failure the raw recording is
        # retained for recovery and the previous selected take remains selected.
        take = self.import_take(
            segment_id, narration, temporary, select=True, metadata=metadata
        )
        temporary.unlink(missing_ok=True)
        return take

    def _record_with_sox(self, destination: Path, input_fn: Callable[[str], str]) -> None:
        process = subprocess.Popen(
            [
                "rec",
                "--clobber",
                "-q",
                "-r",
                "24000",
                "-c",
                "1",
                "-b",
                "24",
                str(destination),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            input_fn("recording; press ENTER to stop ")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            return_code = process.wait()
        if return_code not in (0, 130):
            raise RuntimeError(f"SoX recording failed with exit status {return_code}")

    def record_interactive(
        self,
        segment_id: str,
        narration: str,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
        metadata: Mapping[str, Any] | None = None,
    ) -> TakeRecord | None:
        """Record to scratch and promote only after an explicit keep."""

        self.scratch_root.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f"recording-{segment_id}-", suffix=".wav", dir=self.scratch_root
        )
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            input_fn("press ENTER to begin recording ")
            if self.recorder is not None:
                self.recorder(temporary)
            else:
                self._record_with_sox(temporary, input_fn)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RuntimeError("recording produced no audio")
            choice = input_fn("(k)eep this take? [k/N] ").strip().casefold()
            keep = choice in {"k", "keep", "y", "yes"}
            take = self.promote_recording(
                segment_id,
                narration,
                temporary,
                keep=keep,
                metadata=metadata,
            )
            output_fn("take retained and selected" if take else "take rejected")
            return take
        except Exception:
            # Preserve nonempty failed recordings so conversion/probe failures
            # are recoverable. Empty scratch files are disposable.
            if temporary.exists() and temporary.stat().st_size == 0:
                temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def fingerprint_model(model: str | Path) -> str:
        """Fingerprint the actual local model file/tree, not a mutable name."""

        path = Path(model).expanduser()
        if path.is_file():
            return f"sha256:{_sha256_file(path)}"
        if path.is_dir():
            records = []
            for candidate in sorted(
                (item for item in path.rglob("*") if item.is_file()),
                key=lambda item: item.relative_to(path).as_posix(),
            ):
                records.append(
                    {
                        "path": candidate.relative_to(path).as_posix(),
                        "sha256": _sha256_file(candidate),
                        "size": candidate.stat().st_size,
                    }
                )
            return "tree-sha256:" + _sha256_bytes(
                _canonical_json(records).encode("utf-8")
            )
        raise ValueError(f"model path does not exist: {path}")

    @staticmethod
    def _model_reference(model: str, revision: str | None) -> tuple[str, str | None]:
        path = Path(model).expanduser()
        if path.exists():
            return str(path.resolve()), revision
        if revision:
            if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
                raise ValueError(
                    "remote Whisper model revision must be an immutable commit hash"
                )
            return model, revision
        if "@" in model:
            repo_id, embedded_revision = model.rsplit("@", 1)
            if repo_id and re.fullmatch(r"[0-9a-f]{40,64}", embedded_revision):
                return repo_id, embedded_revision
        raise ValueError(
            "remote Whisper models require an exact revision; configure "
            "[voice] revision or use model='repo@commit'"
        )

    def _snapshot_download(self, repo_id: str, revision: str) -> Path:
        if self.snapshot_downloader is not None:
            return Path(self.snapshot_downloader(repo_id, revision))
        # Production-only dependency: exact cache hits avoid this import.
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

        return Path(snapshot_download(repo_id=repo_id, revision=revision))

    @staticmethod
    def transcript_cache_key(
        audio_sha256: str,
        model_fingerprint: str,
        options: Mapping[str, Any],
    ) -> str:
        return _sha256_bytes(
            _canonical_json(
                {
                    "audio_sha256": audio_sha256,
                    "model_fingerprint": model_fingerprint,
                    "options": dict(options),
                }
            ).encode("utf-8")
        )

    def transcript_path(self, audio_sha256: str) -> Path:
        """Human-facing alias for the transcript currently chosen for audio."""

        return self.transcript_root / f"{audio_sha256}.json"

    def transcript_record_path(self, cache_key: str) -> Path:
        """Immutable transcript result for one exact audio/model/options action."""

        if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
            raise ValueError(f"invalid transcript cache key: {cache_key!r}")
        return self.transcript_root / "sha256" / f"{cache_key}.json"

    def _load_transcript_file(
        self, path: Path, *, expected_audio: str | None = None
    ) -> TranscriptRecord:
        record = TranscriptRecord.from_record(_load_json(path, {}))
        if expected_audio is not None and record.audio_sha256 != expected_audio:
            raise ValueError(
                f"transcript {path} names audio {record.audio_sha256}, "
                f"expected {expected_audio}"
            )
        expected_key = self.transcript_cache_key(
            record.audio_sha256,
            record.model_fingerprint,
            record.options,
        )
        if record.cache_key != expected_key:
            raise ValueError(
                f"transcript {path} has cache key {record.cache_key}, "
                f"expected {expected_key}"
            )
        return record

    def load_transcript(self, audio_sha256: str) -> TranscriptRecord | None:
        path = self.transcript_path(audio_sha256)
        if not path.is_file():
            return None
        return self._load_transcript_file(path, expected_audio=audio_sha256)

    def load_transcript_version(self, cache_key: str) -> TranscriptRecord | None:
        path = self.transcript_record_path(cache_key)
        if not path.is_file():
            return None
        record = self._load_transcript_file(path)
        if record.cache_key != cache_key:
            raise ValueError(
                f"transcript record {path} names cache key {record.cache_key}"
            )
        return record

    def transcript_versions(self, audio_sha256: str) -> tuple[TranscriptRecord, ...]:
        directory = self.transcript_root / "sha256"
        if not directory.is_dir():
            return ()
        records: list[TranscriptRecord] = []
        for path in sorted(directory.glob("*.json")):
            record = self._load_transcript_file(path)
            if record.audio_sha256 == audio_sha256:
                records.append(record)
        return tuple(records)

    def _transcribe_with_mlx(
        self, audio_path: Path, model: str, options: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        # Production-only dependency: cache hits and all tests avoid this import.
        import mlx_whisper  # type: ignore[import-not-found]

        return mlx_whisper.transcribe(
            str(audio_path), path_or_hf_repo=model, **dict(options)
        )

    @staticmethod
    def _words_from_result(result: Mapping[str, Any]) -> tuple[TranscriptWord, ...]:
        raw_words: list[Mapping[str, Any]] = []
        if isinstance(result.get("words"), list):
            raw_words.extend(result["words"])
        else:
            for segment in result.get("segments", ()):
                if isinstance(segment, Mapping):
                    raw_words.extend(segment.get("words", ()))
        words = tuple(
            TranscriptWord.from_value(word)
            for word in raw_words
            if str(word.get("word", word.get("text", ""))).strip()
        )
        if not words:
            raise ValueError("transcriber returned no word timestamps")
        return words

    def transcribe_take(
        self,
        take: TakeRecord | str,
        *,
        model: str,
        revision: str | None = None,
        model_fingerprint: str | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> TranscriptRecord:
        record = self.get_take(take) if isinstance(take, str) else take
        normalized_options = dict(options or {})
        normalized_options["word_timestamps"] = True
        model_reference, revision = self._model_reference(model, revision)
        model_path = Path(model_reference).expanduser()
        cached_alias = self.load_transcript(record.audio_sha256)
        candidates = [
            candidate
            for candidate in (cached_alias, *self.transcript_versions(record.audio_sha256))
            if candidate is not None
        ]

        # A pinned remote revision is enough to identify an existing transcript
        # without touching Hugging Face. The first uncached run also records the
        # actual downloaded model tree fingerprint.
        for cached in candidates:
            if (
                cached.model == model_reference
                and cached.model_revision == revision
                and dict(cached.options) == normalized_options
                and (
                    model_fingerprint is None
                    or cached.model_fingerprint == model_fingerprint
                )
            ):
                if cached_alias != cached:
                    _atomic_json(
                        self.transcript_path(record.audio_sha256),
                        cached.to_record(),
                    )
                return cached

        if model_path.exists():
            resolved_model_path = model_path.resolve()
            fingerprint = self.fingerprint_model(resolved_model_path)
            if model_fingerprint is not None and model_fingerprint != fingerprint:
                raise ValueError(
                    "configured model fingerprint does not match the local model tree"
                )
        elif self.transcriber is not None and model_fingerprint is not None:
            # Tests and alternate transcribers may inject a verified backend.
            resolved_model_path = Path(model_reference)
            fingerprint = model_fingerprint
        else:
            assert revision is not None
            resolved_model_path = self._snapshot_download(model_reference, revision)
            if not resolved_model_path.exists():
                raise FileNotFoundError(
                    f"model snapshot downloader returned no path: {resolved_model_path}"
                )
            fingerprint = self.fingerprint_model(resolved_model_path)
            if model_fingerprint is not None and model_fingerprint != fingerprint:
                raise ValueError(
                    "configured model fingerprint does not match the downloaded model tree"
                )

        cache_key = self.transcript_cache_key(
            record.audio_sha256, fingerprint, normalized_options
        )

        transcriber = self.transcriber or self._transcribe_with_mlx
        result = transcriber(
            self.take_path(record), str(resolved_model_path), normalized_options
        )
        words = self._words_from_result(result)
        transcript = TranscriptRecord(
            audio_sha256=record.audio_sha256,
            cache_key=cache_key,
            model=model_reference,
            model_revision=revision,
            model_fingerprint=fingerprint,
            options=normalized_options,
            words=words,
            created_at=_utc_now(),
            provenance={
                "backend": "mlx_whisper" if self.transcriber is None else "injected",
                "audio_sha256": record.audio_sha256,
                "model": model_reference,
                "model_revision": revision,
                "model_fingerprint": fingerprint,
                "options_sha256": _sha256_bytes(
                    _canonical_json(normalized_options).encode("utf-8")
                ),
                "cache_key": cache_key,
            },
        )
        immutable_path = self.transcript_record_path(cache_key)
        if immutable_path.is_file():
            existing = self._load_transcript_file(immutable_path)
            if existing != transcript:
                raise RuntimeError(
                    f"transcript action {cache_key} produced a conflicting result"
                )
        else:
            _atomic_json(immutable_path, transcript.to_record())
        _atomic_json(self.transcript_path(record.audio_sha256), transcript.to_record())
        return transcript

    def transcribe_selected(
        self,
        segment_id: str,
        *,
        model: str,
        revision: str | None = None,
        model_fingerprint: str | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> TranscriptRecord:
        take = self.selected_take(segment_id)
        if take is None:
            raise KeyError(f"segment {segment_id!r} has no selected take")
        return self.transcribe_take(
            take,
            model=model,
            revision=revision,
            model_fingerprint=model_fingerprint,
            options=options,
        )

    def current_selected_transcript(
        self,
        segment_id: str,
        *,
        narration: str | None = None,
        require_fresh: bool = False,
    ) -> TranscriptRecord | None:
        take = self.selected_take(
            segment_id, narration=narration, require_fresh=require_fresh
        )
        if take is None:
            return None
        return self.load_transcript(take.audio_sha256)


# The narrower name is useful to callers that only manage takes.
TakeStore = AudioStore


__all__ = [
    "AudioStore",
    "CommandRunner",
    "Converter",
    "DurationProber",
    "Recorder",
    "SnapshotDownloader",
    "TakeRecord",
    "TakeStore",
    "Transcriber",
    "TranscriptRecord",
]
