"""The tiny author-facing runtime layered on top of Manim.

Scene files name narration phrases rather than maintaining duration tables or
elapsed-time counters.  The module remains importable without Manim so timeline
logic and project inspection work in lightweight environments.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from _chalk.timeline import (
    MICROSECONDS_PER_SECOND,
    ResolvedTimeline,
    seconds_to_us,
)


try:  # Guarded production import; all timing helpers work without Manim.
    from manim import Scene as _ManimScene  # type: ignore[import-not-found]

    MANIM_AVAILABLE = True
    _MANIM_IMPORT_ERROR: ImportError | None = None
except ImportError as error:  # pragma: no cover - behavior exercised indirectly
    MANIM_AVAILABLE = False
    _MANIM_IMPORT_ERROR = error

    class _ManimScene:  # type: ignore[no-redef]
        """Minimal clock stub that keeps scene modules importable in tests."""

        def __init__(self, *_: object, **__: object) -> None:
            self.renderer = SimpleNamespace(time=0.0)

        def wait(self, duration: float = 1.0, *_: object, **__: object) -> None:
            self.renderer.time += float(duration)

        def play(self, *animations: object, **kwargs: object) -> None:
            self.renderer.time += _animation_runtime_seconds(animations, kwargs)


@dataclass(frozen=True, slots=True)
class TimingDecision:
    target_us: int
    current_us: int
    wait_us: int
    late_by_us: int


@dataclass(frozen=True, slots=True)
class TimingController:
    """Pure timing arithmetic shared by :class:`ChalkScene` and tests."""

    timeline: ResolvedTimeline

    def cue_us(
        self,
        phrase: str,
        *,
        occurrence: int | None = None,
        anchor: str = "start",
    ) -> int:
        return self.timeline.cue_time_us(
            phrase, occurrence=occurrence, anchor=anchor, scene_time=True
        )

    @staticmethod
    def decision(current_us: int, target_us: int) -> TimingDecision:
        wait_us = max(target_us - current_us, 0)
        return TimingDecision(
            target_us=target_us,
            current_us=current_us,
            wait_us=wait_us,
            late_by_us=max(current_us - target_us, 0),
        )

    def play_on(
        self,
        phrase: str,
        current_us: int,
        *,
        occurrence: int | None = None,
        lead_us: int = 0,
    ) -> TimingDecision:
        target = max(
            self.cue_us(phrase, occurrence=occurrence, anchor="start") - lead_us,
            0,
        )
        return self.decision(current_us, target)

    def land_on(
        self,
        phrase: str,
        current_us: int,
        animation_runtime_us: int,
        *,
        occurrence: int | None = None,
        lead_us: int = 0,
    ) -> TimingDecision:
        target = max(
            self.cue_us(phrase, occurrence=occurrence, anchor="start")
            - lead_us
            - animation_runtime_us,
            0,
        )
        return self.decision(current_us, target)

    def finish(
        self, current_us: int, *, tail_us: int | None = None
    ) -> TimingDecision:
        target = (
            self.timeline.total_duration_us
            if tail_us is None
            else self.timeline.lead_in_us
            + self.timeline.narration_duration_us
            + tail_us
        )
        return self.decision(current_us, target)


def _animation_runtime_seconds(
    animations: Sequence[object], kwargs: Mapping[str, object]
) -> float:
    explicit = kwargs.get("run_time")
    if explicit is not None:
        return max(float(explicit), 0.0)
    durations: list[float] = []
    for animation in animations:
        getter = getattr(animation, "get_run_time", None)
        if callable(getter):
            try:
                durations.append(float(getter()))
                continue
            except (TypeError, ValueError):
                pass
        value = getattr(animation, "run_time", None)
        if value is not None:
            try:
                durations.append(float(value))
            except (TypeError, ValueError):
                pass
    return max(durations, default=1.0)


def _timeline_from_environment() -> ResolvedTimeline | None:
    path_value = os.environ.get("CHALK_TIMELINE_PATH")
    if path_value:
        return ResolvedTimeline.read(path_value)

    value = os.environ.get("CHALK_TIMELINE")
    if not value:
        return None
    possible_path = Path(value)
    if not value.lstrip().startswith(("{", "[")) and possible_path.is_file():
        return ResolvedTimeline.read(possible_path)
    return ResolvedTimeline.from_record(json.loads(value))


class ChalkScene(_ManimScene):
    """A Manim scene whose clock is driven by narration phrases."""

    timeline: ResolvedTimeline | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        configured = self.timeline or _timeline_from_environment()
        self._chalk_timeline = configured
        self._chalk_timing = TimingController(configured) if configured else None
        self._chalk_elapsed_us = 0
        self._chalk_renderer_start = self._renderer_time_seconds()
        self.orientation = os.environ.get(
            "CHALK_ORIENTATION",
            configured.orientation if configured is not None else "landscape",
        )

    @property
    def is_vertical(self) -> bool:
        return self.orientation.casefold() in {"vertical", "portrait", "shorts", "9:16"}

    @property
    def resolved_timeline(self) -> ResolvedTimeline:
        if self._chalk_timeline is None:
            raise RuntimeError(
                "no Chalk timeline is configured; set CHALK_TIMELINE_PATH or "
                "CHALK_TIMELINE before rendering"
            )
        return self._chalk_timeline

    @property
    def timing(self) -> TimingController:
        if self._chalk_timing is None:
            # Produce the more useful environment diagnostic above.
            self.resolved_timeline
            raise AssertionError("unreachable")
        return self._chalk_timing

    def _renderer_time_seconds(self) -> float | None:
        renderer = getattr(self, "renderer", None)
        value = getattr(renderer, "time", None)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _current_time_us(self) -> int:
        renderer_time = self._renderer_time_seconds()
        if renderer_time is not None and self._chalk_renderer_start is not None:
            rendered_us = seconds_to_us(
                max(renderer_time - self._chalk_renderer_start, 0.0)
            )
            self._chalk_elapsed_us = max(self._chalk_elapsed_us, rendered_us)
        return self._chalk_elapsed_us

    @property
    def current_time(self) -> float:
        return self._current_time_us() / MICROSECONDS_PER_SECOND

    def cue_time(
        self,
        phrase: str,
        *,
        occurrence: int | None = None,
        anchor: str = "start",
    ) -> float:
        """Return a phrase's resolved scene time in seconds."""

        return (
            self.timing.cue_us(phrase, occurrence=occurrence, anchor=anchor)
            / MICROSECONDS_PER_SECOND
        )

    # Short alias for scene code that only needs the numeric cue.
    cue = cue_time

    def _wait_for_decision(self, decision: TimingDecision) -> None:
        if decision.wait_us > 0:
            self.wait(decision.wait_us / MICROSECONDS_PER_SECOND)

    def wait(self, duration: float = 1.0, *args: object, **kwargs: object) -> Any:
        duration = max(float(duration), 0.0)
        before = self._current_time_us()
        result = super().wait(duration, *args, **kwargs)
        self._chalk_elapsed_us = max(
            self._chalk_elapsed_us, before + seconds_to_us(duration)
        )
        self._current_time_us()
        return result

    def play(self, *animations: object, **kwargs: object) -> Any:
        runtime = _animation_runtime_seconds(animations, kwargs)
        before = self._current_time_us()
        result = super().play(*animations, **kwargs)
        self._chalk_elapsed_us = max(
            self._chalk_elapsed_us, before + seconds_to_us(runtime)
        )
        self._current_time_us()
        return result

    def play_on(
        self,
        phrase: str,
        *animations: object,
        occurrence: int | None = None,
        lead: float = 0.0,
        **play_kwargs: object,
    ) -> Any:
        """Start animations when ``phrase`` begins (optionally slightly early)."""

        decision = self.timing.play_on(
            phrase,
            self._current_time_us(),
            occurrence=occurrence,
            lead_us=seconds_to_us(lead),
        )
        self._wait_for_decision(decision)
        return self.play(*animations, **play_kwargs)

    def land_on(
        self,
        phrase: str,
        *animations: object,
        occurrence: int | None = None,
        lead: float = 0.0,
        **play_kwargs: object,
    ) -> Any:
        """Time animations so they finish when ``phrase`` begins."""

        runtime_us = seconds_to_us(
            _animation_runtime_seconds(animations, play_kwargs)
        )
        decision = self.timing.land_on(
            phrase,
            self._current_time_us(),
            runtime_us,
            occurrence=occurrence,
            lead_us=seconds_to_us(lead),
        )
        self._wait_for_decision(decision)
        return self.play(*animations, **play_kwargs)

    def finish(self, *, tail: float | None = None) -> float:
        """Pad to the selected recording or estimated narration duration."""

        if tail is not None and tail < 0:
            raise ValueError("tail must be non-negative")
        decision = self.timing.finish(
            self._current_time_us(),
            tail_us=seconds_to_us(tail) if tail is not None else None,
        )
        self._wait_for_decision(decision)
        return decision.wait_us / MICROSECONDS_PER_SECOND


__all__ = [
    "ChalkScene",
    "MANIM_AVAILABLE",
    "TimingController",
    "TimingDecision",
]
