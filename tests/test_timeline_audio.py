from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from _chalk.audio import AudioStore
from _chalk.timeline import (
    AmbiguousPhraseError,
    CueNotFoundError,
    ResolvedTimeline,
    TranscriptWord,
    resolve_timeline,
)
from chalk_runtime import TimingController


class TimelineTests(unittest.TestCase):
    def test_estimated_timing_uses_wpm_punctuation_and_integer_microseconds(self) -> None:
        timeline = resolve_timeline(
            "intro",
            "alpha, beta. gamma",
            wpm=120,
            lead_in_us=1_000_000,
            tail_us=500_000,
        )

        self.assertEqual(timeline.source, "estimated")
        self.assertEqual(timeline.words[0].start_us, 0)
        self.assertEqual(timeline.words[0].end_us, 500_000)
        self.assertEqual(timeline.words[1].start_us, 620_000)  # comma pause
        self.assertEqual(timeline.words[2].start_us, 1_380_000)  # sentence pause
        self.assertIsInstance(timeline.cue("beta").start_us, int)
        self.assertEqual(timeline.cue_time_us("beta"), 1_620_000)

        restored = ResolvedTimeline.from_json(timeline.to_json())
        self.assertEqual(restored.digest, timeline.digest)
        self.assertEqual(restored.to_record(), timeline.to_record())

    def test_alignment_interpolates_omissions_and_maps_substitutions(self) -> None:
        transcript = [
            TranscriptWord("now", 0, 150_000),
            TranscriptWord("we", 150_000, 250_000),
            TranscriptWord("squared", 300_000, 550_000),
            TranscriptWord("sides", 550_000, 760_000),
            TranscriptWord("finish", 1_000_000, 1_220_000),
        ]
        timeline = resolve_timeline(
            "equation",
            "now we square both sides and finish",
            transcript,
            audio_duration_us=1_400_000,
            lead_in_us=200_000,
        )

        self.assertEqual(timeline.source, "transcript")
        self.assertEqual([word.resolution for word in timeline.words[:2]], ["exact", "exact"])
        self.assertIn(timeline.words[2].resolution, {"substitution", "interpolated"})
        self.assertIn(timeline.words[3].resolution, {"substitution", "interpolated"})
        self.assertEqual(timeline.words[5].resolution, "interpolated")  # omitted "and"
        self.assertLess(timeline.cue("square both sides").start_us, timeline.cue("finish").start_us)
        self.assertEqual(timeline.narration_duration_us, 1_400_000)
        for previous, following in zip(timeline.words, timeline.words[1:]):
            self.assertLessEqual(previous.end_us, following.start_us)

    def test_phrase_ambiguity_occurrence_and_missing_diagnostics(self) -> None:
        timeline = resolve_timeline("repeat", "again, and again, then done", wpm=150)

        with self.assertRaisesRegex(AmbiguousPhraseError, "occurrence=1..2"):
            timeline.cue("again")
        first = timeline.cue("again", occurrence=1)
        second = timeline.cue("again", occurrence=2)
        self.assertLess(first.start_us, second.start_us)
        with self.assertRaisesRegex(CueNotFoundError, "closest narration phrases"):
            timeline.cue("then donut")
        with self.assertRaisesRegex(CueNotFoundError, "has 2 occurrence"):
            timeline.cue("again", occurrence=3)

    def test_runtime_timing_math_centralizes_lead_runtime_and_finish(self) -> None:
        timeline = resolve_timeline(
            "clock",
            "alpha beta gamma",
            wpm=120,
            lead_in_us=1_000_000,
            tail_us=500_000,
        )
        timing = TimingController(timeline)

        play = timing.play_on("beta", 250_000)
        self.assertEqual(play.target_us, 1_500_000)
        self.assertEqual(play.wait_us, 1_250_000)

        land = timing.land_on("beta", 250_000, animation_runtime_us=400_000)
        self.assertEqual(land.target_us, 1_100_000)
        self.assertEqual(land.wait_us, 850_000)

        finish = timing.finish(2_000_000)
        self.assertEqual(finish.target_us, 3_000_000)
        self.assertEqual(finish.wait_us, 1_000_000)


class AudioStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.transcribe_calls = 0

        def fake_converter(source: Path, destination: Path) -> None:
            destination.write_bytes(b"fLaC\x00" + source.read_bytes())

        def fake_prober(path: Path) -> int:
            return 1_000_000 + len(path.read_bytes())

        def fake_transcriber(path: Path, model: str, options: object) -> dict[str, object]:
            del path, model, options
            self.transcribe_calls += 1
            return {
                "segments": [
                    {
                        "words": [
                            {"word": "hello", "start": 0.125, "end": 0.5},
                            {"word": "world", "start": 0.5, "end": 0.875},
                        ]
                    }
                ]
            }

        self.store = AudioStore(
            self.root,
            converter=fake_converter,
            prober=fake_prober,
            transcriber=fake_transcriber,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def fake_wav(self, name: str, contents: bytes) -> Path:
        path = self.root / name
        path.write_bytes(b"RIFF" + contents)
        return path

    def test_retakes_are_immutable_and_failed_or_rejected_takes_preserve_selection(self) -> None:
        first = self.store.import_take(
            "intro", "hello world", self.fake_wav("first.wav", b"first"), select=True
        )
        second = self.store.import_take(
            "intro", "hello world", self.fake_wav("second.wav", b"second"), select=True
        )

        self.assertNotEqual(first.id, second.id)
        self.assertTrue(self.store.take_path(first).is_file())
        self.assertTrue(self.store.take_path(second).is_file())
        self.assertEqual(
            self.store.take_path(second),
            self.root.resolve()
            / "media"
            / "takes"
            / "sha256"
            / f"{second.audio_sha256}.flac",
        )
        self.assertEqual(self.store.selected_take("intro"), second)
        self.assertEqual(len(self.store.list_takes("intro")), 2)
        state = json.loads(self.store.selection_path.read_text(encoding="utf-8"))
        self.assertEqual(state["selected_takes"], {"intro": second.id})
        self.assertFalse((self.root / "media" / "takes" / "selected.json").exists())

        previous_selection = json.loads(
            self.store.selection_path.read_text(encoding="utf-8")
        )

        def failing_converter(source: Path, destination: Path) -> None:
            del source, destination
            raise RuntimeError("conversion failed")

        self.store.converter = failing_converter
        with self.assertRaisesRegex(RuntimeError, "conversion failed"):
            self.store.import_take(
                "intro", "hello world", self.fake_wav("failed.wav", b"failed"), select=True
            )
        self.assertEqual(self.store.selected_take("intro"), second)
        self.assertEqual(
            json.loads(self.store.selection_path.read_text(encoding="utf-8")),
            previous_selection,
        )

        rejected = self.fake_wav("rejected.wav", b"rejected")
        self.assertIsNone(
            self.store.promote_recording(
                "intro", "hello world", rejected, keep=False
            )
        )
        self.assertFalse(rejected.exists())
        self.assertEqual(self.store.selected_take("intro"), second)

    def test_take_identity_binds_narration_and_reports_script_staleness(self) -> None:
        source = self.fake_wav("same.wav", b"same audio")
        original = self.store.import_take("proof", "original words", source, select=True)
        revised = self.store.import_take("proof", "revised words", source)

        self.assertEqual(original.audio_sha256, revised.audio_sha256)
        self.assertNotEqual(original.id, revised.id)
        self.assertFalse(original.is_stale_for("original words"))
        self.assertTrue(original.is_stale_for("revised words"))
        self.assertEqual(self.store.selected_take("proof"), original)
        with self.assertRaisesRegex(ValueError, "recorded against narration"):
            self.store.selected_take(
                "proof", narration="revised words", require_fresh=True
            )

    def test_concurrent_segment_selections_merge_instead_of_overwriting_state(self) -> None:
        intro = self.store.import_take(
            "intro", "intro words", self.fake_wav("intro.wav", b"intro")
        )
        proof = self.store.import_take(
            "proof", "proof words", self.fake_wav("proof.wav", b"proof")
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            tuple(
                pool.map(
                    lambda pair: self.store.select_take(*pair),
                    (("intro", intro.id), ("proof", proof.id)),
                )
            )
        state = json.loads(self.store.selection_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["selected_takes"], {"intro": intro.id, "proof": proof.id}
        )

    def test_transcription_cache_is_exact_and_uses_integer_microseconds(self) -> None:
        take = self.store.import_take(
            "intro", "hello world", self.fake_wav("voice.wav", b"voice"), select=True
        )
        first = self.store.transcribe_take(
            take,
            model="whisper@a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
            model_fingerprint="sha256:model-a",
            options={"language": "en"},
        )
        second = self.store.transcribe_take(
            take,
            model="whisper@a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
            model_fingerprint="sha256:model-a",
            options={"language": "en"},
        )

        self.assertEqual(self.transcribe_calls, 1)
        self.assertEqual(first, second)
        self.assertEqual(first.words[0].start_us, 125_000)
        self.assertEqual(first.words[1].end_us, 875_000)
        self.assertEqual(
            self.store.current_selected_transcript("intro"), first
        )
        self.assertTrue(self.store.transcript_path(take.audio_sha256).is_file())
        self.assertTrue(self.store.transcript_record_path(first.cache_key).is_file())

        changed = self.store.transcribe_take(
            take,
            model="whisper@a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
            model_fingerprint="sha256:model-a",
            options={"language": "fr"},
        )
        self.assertEqual(self.transcribe_calls, 2)
        self.assertNotEqual(changed.cache_key, first.cache_key)
        self.assertTrue(self.store.transcript_record_path(changed.cache_key).is_file())
        self.assertEqual(self.store.load_transcript_version(first.cache_key), first)

        # Switching back to an older exact configuration reuses its immutable
        # record and simply repoints the readable per-audio alias.
        restored = self.store.transcribe_take(
            take,
            model="whisper@a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
            model_fingerprint="sha256:model-a",
            options={"language": "en"},
        )
        self.assertEqual(restored, first)
        self.assertEqual(self.transcribe_calls, 2)
        self.assertEqual(self.store.load_transcript(take.audio_sha256), first)

    def test_remote_model_revision_is_pinned_and_actual_snapshot_tree_is_hashed(self) -> None:
        take = self.store.import_take(
            "intro", "hello world", self.fake_wav("pinned.wav", b"voice"), select=True
        )
        snapshot = self.root / "model-snapshot"
        snapshot.mkdir()
        (snapshot / "config.json").write_text('{"model":"whisper"}\n')
        (snapshot / "weights.safetensors").write_bytes(b"model weights")
        downloads: list[tuple[str, str]] = []
        model_paths: list[str] = []

        def download(repo_id: str, revision: str) -> Path:
            downloads.append((repo_id, revision))
            return snapshot

        def transcribe(path: Path, model: str, options: object) -> dict[str, object]:
            del path, options
            model_paths.append(model)
            return {
                "words": [
                    {"word": "hello", "start_us": 100_000, "end_us": 400_000},
                    {"word": "world", "start_us": 400_000, "end_us": 800_000},
                ]
            }

        pinned_store = AudioStore(
            self.root,
            converter=self.store.converter,
            prober=self.store.prober,
            transcriber=transcribe,
            snapshot_downloader=download,
        )
        first = pinned_store.transcribe_take(
            take,
            model="organization/whisper",
            revision="a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
            options={"language": "en"},
        )
        second = pinned_store.transcribe_take(
            take,
            model="organization/whisper",
            revision="a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
            options={"language": "en"},
        )

        self.assertEqual(first, second)
        self.assertEqual(
            downloads,
            [
                (
                    "organization/whisper",
                    "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
                )
            ],
        )
        self.assertEqual(model_paths, [str(snapshot)])
        self.assertTrue(first.model_fingerprint.startswith("tree-sha256:"))
        self.assertEqual(
            first.provenance["model_revision"],
            "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
        )

        with self.assertRaisesRegex(ValueError, "require an exact revision"):
            pinned_store.transcribe_take(
                take,
                model="organization/mutable-model",
                options={"language": "en"},
            )


if __name__ == "__main__":
    unittest.main()
