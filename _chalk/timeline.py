"""Narration timelines and phrase cue resolution.

The timeline is deliberately independent of Manim and Whisper.  Draft renders
use an estimated word clock; recorded renders use word timestamps supplied by a
transcriber.  Both modes expose the same phrase-oriented API and serialize to a
deterministic, integer-microsecond record.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import difflib
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


MICROSECONDS_PER_SECOND = 1_000_000
DEFAULT_WPM = 150.0
DEFAULT_LEAD_IN_US = 250_000
DEFAULT_TAIL_US = 350_000
_WORD_RE = re.compile(r"[^\W_]+(?:['\N{RIGHT SINGLE QUOTATION MARK}][^\W_]+)*", re.UNICODE)


class PhraseResolutionError(ValueError):
    """Base class for an unusable phrase cue."""


class CueNotFoundError(PhraseResolutionError):
    """The requested phrase does not occur in the canonical narration."""


class AmbiguousPhraseError(PhraseResolutionError):
    """The requested phrase occurs more than once without an occurrence."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def narration_digest(narration: str) -> str:
    """Return the revision identity of the exact spoken text."""

    return _sha256_bytes(narration.encode("utf-8"))


def normalize_word(word: str) -> str:
    """Normalize a word for script/transcript matching."""

    return "".join(character.casefold() for character in word if character.isalnum())


def seconds_to_us(seconds: int | float | str | Decimal) -> int:
    value = Decimal(str(seconds)) * MICROSECONDS_PER_SECOND
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class CanonicalToken:
    index: int
    text: str
    normalized: str
    start_char: int
    end_char: int
    pause_after_us: int


@dataclass(frozen=True, slots=True)
class TranscriptWord:
    word: str
    start_us: int
    end_us: int

    @classmethod
    def from_value(cls, value: TranscriptWord | Mapping[str, Any]) -> TranscriptWord:
        if isinstance(value, cls):
            return value
        word = str(value.get("word", value.get("text", ""))).strip()
        if "start_us" in value:
            start_us = int(value["start_us"])
        elif "startUs" in value:
            start_us = int(value["startUs"])
        else:
            start_us = seconds_to_us(value.get("start", 0))
        if "end_us" in value:
            end_us = int(value["end_us"])
        elif "endUs" in value:
            end_us = int(value["endUs"])
        else:
            end_us = seconds_to_us(value.get("end", value.get("start", 0)))
        if start_us < 0 or end_us < start_us:
            raise ValueError(
                f"invalid transcript timestamp for {word!r}: {start_us}..{end_us}us"
            )
        return cls(word=word, start_us=start_us, end_us=end_us)

    def to_record(self) -> dict[str, object]:
        return {"word": self.word, "start_us": self.start_us, "end_us": self.end_us}


@dataclass(frozen=True, slots=True)
class TimedWord:
    index: int
    word: str
    normalized: str
    start_us: int
    end_us: int
    resolution: str

    def to_record(self) -> dict[str, object]:
        return {
            "index": self.index,
            "word": self.word,
            "normalized": self.normalized,
            "start_us": self.start_us,
            "end_us": self.end_us,
            "resolution": self.resolution,
        }


@dataclass(frozen=True, slots=True)
class PhraseCue:
    phrase: str
    occurrence: int
    start_word: int
    end_word: int
    start_us: int
    end_us: int


@dataclass(frozen=True, slots=True)
class ResolvedTimeline:
    """The resolved word clock for one exact narration revision."""

    segment_id: str
    narration: str
    narration_sha256: str
    source: str
    words: tuple[TimedWord, ...]
    narration_duration_us: int
    lead_in_us: int = DEFAULT_LEAD_IN_US
    tail_us: int = DEFAULT_TAIL_US
    wpm: float = DEFAULT_WPM
    transcript_sha256: str | None = None
    orientation: str = "landscape"

    @property
    def total_duration_us(self) -> int:
        return self.lead_in_us + self.narration_duration_us + self.tail_us

    @property
    def digest(self) -> str:
        return _sha256_bytes(_canonical_json(self.to_record(include_digest=False)).encode())

    def to_record(self, *, include_digest: bool = True) -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": 1,
            "segment_id": self.segment_id,
            "narration": self.narration,
            "narration_sha256": self.narration_sha256,
            "source": self.source,
            "words": [word.to_record() for word in self.words],
            "narration_duration_us": self.narration_duration_us,
            "lead_in_us": self.lead_in_us,
            "tail_us": self.tail_us,
            "total_duration_us": self.total_duration_us,
            "wpm": self.wpm,
            "transcript_sha256": self.transcript_sha256,
            "orientation": self.orientation,
        }
        if include_digest:
            record["digest"] = self.digest
        return record

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_record(), indent=indent, sort_keys=True, ensure_ascii=False)

    def write(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.to_json() + "\n", encoding="utf-8")

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> ResolvedTimeline:
        words = tuple(
            TimedWord(
                index=int(word.get("index", index)),
                word=str(word["word"]),
                normalized=str(word.get("normalized", normalize_word(str(word["word"])))),
                start_us=int(word["start_us"]),
                end_us=int(word["end_us"]),
                resolution=str(word.get("resolution", "unknown")),
            )
            for index, word in enumerate(value.get("words", ()))
        )
        timeline = cls(
            segment_id=str(value.get("segment_id", "")),
            narration=str(value.get("narration", "")),
            narration_sha256=str(
                value.get("narration_sha256", narration_digest(str(value.get("narration", ""))))
            ),
            source=str(value.get("source", "estimated")),
            words=words,
            narration_duration_us=int(value.get("narration_duration_us", 0)),
            lead_in_us=int(value.get("lead_in_us", DEFAULT_LEAD_IN_US)),
            tail_us=int(value.get("tail_us", DEFAULT_TAIL_US)),
            wpm=float(value.get("wpm", DEFAULT_WPM)),
            transcript_sha256=(
                str(value["transcript_sha256"])
                if value.get("transcript_sha256") is not None
                else None
            ),
            orientation=str(value.get("orientation", "landscape")),
        )
        recorded_digest = value.get("digest")
        if recorded_digest is not None and str(recorded_digest) != timeline.digest:
            raise ValueError(
                f"timeline digest mismatch: recorded {recorded_digest}, resolved {timeline.digest}"
            )
        return timeline

    @classmethod
    def from_json(cls, value: str | bytes) -> ResolvedTimeline:
        return cls.from_record(json.loads(value))

    @classmethod
    def read(cls, path: str | Path) -> ResolvedTimeline:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def cue(self, phrase: str, *, occurrence: int | None = None) -> PhraseCue:
        phrase_words = [token.normalized for token in tokenize(phrase)]
        if not phrase_words:
            raise CueNotFoundError(f"cue phrase {phrase!r} contains no searchable words")

        canonical = [word.normalized for word in self.words]
        width = len(phrase_words)
        matches = [
            index
            for index in range(0, len(canonical) - width + 1)
            if canonical[index : index + width] == phrase_words
        ]
        if not matches:
            suggestions = _phrase_suggestions(phrase_words, self.words)
            message = f"cue phrase {phrase!r} does not appear in segment {self.segment_id!r}"
            if suggestions:
                message += "; closest narration phrases: " + ", ".join(
                    repr(suggestion) for suggestion in suggestions
                )
            raise CueNotFoundError(message)

        if occurrence is None:
            if len(matches) > 1:
                times = ", ".join(
                    format(
                        (self.lead_in_us + self.words[index].start_us)
                        / MICROSECONDS_PER_SECOND,
                        ".2f",
                    )
                    + "s"
                    for index in matches
                )
                raise AmbiguousPhraseError(
                    f"cue phrase {phrase!r} appears {len(matches)} times in segment "
                    f"{self.segment_id!r} (at {times}); pass occurrence=1..{len(matches)}"
                )
            occurrence = 1
        if occurrence < 1 or occurrence > len(matches):
            raise CueNotFoundError(
                f"cue phrase {phrase!r} has {len(matches)} occurrence(s), "
                f"not occurrence={occurrence}"
            )

        start_index = matches[occurrence - 1]
        end_index = start_index + width - 1
        return PhraseCue(
            phrase=phrase,
            occurrence=occurrence,
            start_word=start_index,
            end_word=end_index,
            start_us=self.words[start_index].start_us,
            end_us=self.words[end_index].end_us,
        )

    def cue_time_us(
        self,
        phrase: str,
        *,
        occurrence: int | None = None,
        anchor: str = "start",
        scene_time: bool = True,
    ) -> int:
        cue = self.cue(phrase, occurrence=occurrence)
        if anchor == "start":
            value = cue.start_us
        elif anchor == "end":
            value = cue.end_us
        else:
            raise ValueError("cue anchor must be 'start' or 'end'")
        return value + self.lead_in_us if scene_time else value


def _pause_after(fragment: str) -> int:
    if any(character in fragment for character in ".!?"):
        return 260_000
    if any(character in fragment for character in "\u2014\u2013"):
        return 180_000
    if any(character in fragment for character in ",;:"):
        return 120_000
    return 0


def tokenize(narration: str) -> tuple[CanonicalToken, ...]:
    """Tokenize narration while retaining punctuation-induced pauses."""

    matches = list(_WORD_RE.finditer(narration))
    tokens: list[CanonicalToken] = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(narration)
        tokens.append(
            CanonicalToken(
                index=index,
                text=match.group(0),
                normalized=normalize_word(match.group(0)),
                start_char=match.start(),
                end_char=match.end(),
                pause_after_us=_pause_after(narration[match.end() : next_start]),
            )
        )
    return tuple(token for token in tokens if token.normalized)


def _estimated_words(tokens: Sequence[CanonicalToken], wpm: float) -> tuple[TimedWord, ...]:
    if wpm <= 0:
        raise ValueError("wpm must be positive")
    word_duration_us = int(round(60 * MICROSECONDS_PER_SECOND / wpm))
    cursor = 0
    words: list[TimedWord] = []
    for token in tokens:
        start_us = cursor
        end_us = start_us + word_duration_us
        words.append(
            TimedWord(
                index=token.index,
                word=token.text,
                normalized=token.normalized,
                start_us=start_us,
                end_us=end_us,
                resolution="estimated",
            )
        )
        cursor = end_us + token.pause_after_us
    return tuple(words)


def _distribute_span(
    tokens: Sequence[CanonicalToken], start_us: int, end_us: int, resolution: str
) -> list[tuple[int, int, str]]:
    if not tokens:
        return []
    start_us = max(start_us, 0)
    end_us = max(end_us, start_us)
    span = end_us - start_us
    count = len(tokens)
    result: list[tuple[int, int, str]] = []
    for index in range(count):
        word_start = start_us + (span * index // count)
        word_end = start_us + (span * (index + 1) // count)
        result.append((word_start, max(word_end, word_start), resolution))
    return result


def _aligned_words(
    tokens: Sequence[CanonicalToken],
    transcript: Sequence[TranscriptWord],
    *,
    audio_duration_us: int | None,
) -> tuple[TimedWord, ...]:
    canonical_words = [token.normalized for token in tokens]
    transcript_words = [normalize_word(word.word) for word in transcript]
    matcher = difflib.SequenceMatcher(
        a=canonical_words, b=transcript_words, autojunk=False
    )
    resolved: list[tuple[int, int, str] | None] = [None] * len(tokens)

    for opcode, i1, i2, j1, j2 in matcher.get_opcodes():
        if opcode == "equal":
            for offset in range(i2 - i1):
                word = transcript[j1 + offset]
                resolved[i1 + offset] = (word.start_us, word.end_us, "exact")
        elif opcode == "replace" and j1 < j2:
            replacements = _distribute_span(
                tokens[i1:i2],
                transcript[j1].start_us,
                transcript[j2 - 1].end_us,
                "substitution",
            )
            resolved[i1:i2] = replacements
        # Deleted script words are filled by interpolation below. Inserted
        # transcript words need no canonical entry, but still shape boundaries.

    transcript_end = max((word.end_us for word in transcript), default=0)
    effective_duration = max(audio_duration_us or 0, transcript_end)
    index = 0
    while index < len(resolved):
        if resolved[index] is not None:
            index += 1
            continue
        run_start = index
        while index < len(resolved) and resolved[index] is None:
            index += 1
        run_end = index
        left_us = resolved[run_start - 1][1] if run_start > 0 else 0  # type: ignore[index]
        right_us = (
            resolved[run_end][0]  # type: ignore[index]
            if run_end < len(resolved)
            else effective_duration
        )
        resolved[run_start:run_end] = _distribute_span(
            tokens[run_start:run_end], left_us, max(right_us, left_us), "interpolated"
        )

    words: list[TimedWord] = []
    cursor = 0
    for token, timing in zip(tokens, resolved, strict=True):
        assert timing is not None
        start_us, end_us, resolution = timing
        start_us = max(start_us, cursor)
        end_us = max(end_us, start_us)
        words.append(
            TimedWord(
                index=token.index,
                word=token.text,
                normalized=token.normalized,
                start_us=start_us,
                end_us=end_us,
                resolution=resolution,
            )
        )
        cursor = end_us
    return tuple(words)


def _phrase_suggestions(
    phrase_words: Sequence[str], words: Sequence[TimedWord]
) -> list[str]:
    width = max(1, len(phrase_words))
    candidates = [
        " ".join(word.word for word in words[index : index + width])
        for index in range(max(0, len(words) - width + 1))
    ]
    normalized_to_display = {
        " ".join(normalize_word(part) for part in candidate.split()): candidate
        for candidate in candidates
    }
    wanted = " ".join(phrase_words)
    matches = difflib.get_close_matches(
        wanted, list(normalized_to_display), n=3, cutoff=0.25
    )
    return [normalized_to_display[match] for match in matches]


def _transcript_digest(words: Sequence[TranscriptWord]) -> str:
    return _sha256_bytes(
        _canonical_json([word.to_record() for word in words]).encode("utf-8")
    )


def resolve_timeline(
    segment_id: str,
    narration: str,
    transcript_words: Sequence[TranscriptWord | Mapping[str, Any]] | None = None,
    *,
    wpm: float = DEFAULT_WPM,
    audio_duration_us: int | None = None,
    lead_in_us: int = DEFAULT_LEAD_IN_US,
    tail_us: int = DEFAULT_TAIL_US,
    orientation: str = "landscape",
) -> ResolvedTimeline:
    """Resolve a draft or recorded timeline for ``narration``.

    ``transcript_words`` may contain :class:`TranscriptWord` objects or JSON
    records with integer microseconds (``start_us``/``end_us``).  Seconds-based
    Whisper records are also accepted at this boundary and normalized once.
    """

    wpm = float(wpm)
    if lead_in_us < 0 or tail_us < 0:
        raise ValueError("lead-in and tail durations must be non-negative")
    tokens = tokenize(narration)
    transcript = tuple(
        TranscriptWord.from_value(word) for word in (transcript_words or ())
    )
    if transcript:
        words = _aligned_words(
            tokens, transcript, audio_duration_us=audio_duration_us
        )
        source = "transcript"
        transcript_sha256 = _transcript_digest(transcript)
        narration_duration_us = max(
            audio_duration_us or 0,
            max((word.end_us for word in transcript), default=0),
            max((word.end_us for word in words), default=0),
        )
    else:
        words = _estimated_words(tokens, wpm)
        source = "estimated"
        transcript_sha256 = None
        narration_duration_us = max((word.end_us for word in words), default=0)
        if tokens:
            narration_duration_us += tokens[-1].pause_after_us

    return ResolvedTimeline(
        segment_id=segment_id,
        narration=narration,
        narration_sha256=narration_digest(narration),
        source=source,
        words=words,
        narration_duration_us=narration_duration_us,
        lead_in_us=lead_in_us,
        tail_us=tail_us,
        wpm=wpm,
        transcript_sha256=transcript_sha256,
        orientation=orientation,
    )


def load_timeline(path: str | Path) -> ResolvedTimeline:
    return ResolvedTimeline.read(path)


__all__ = [
    "AmbiguousPhraseError",
    "CanonicalToken",
    "CueNotFoundError",
    "DEFAULT_LEAD_IN_US",
    "DEFAULT_TAIL_US",
    "DEFAULT_WPM",
    "MICROSECONDS_PER_SECOND",
    "PhraseCue",
    "PhraseResolutionError",
    "ResolvedTimeline",
    "TimedWord",
    "TranscriptWord",
    "load_timeline",
    "narration_digest",
    "normalize_word",
    "resolve_timeline",
    "seconds_to_us",
    "tokenize",
]
