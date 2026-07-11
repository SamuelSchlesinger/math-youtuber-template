"""Regression guard for Finding 2: persisting a derived-action pointer must not
clobber a take-selection or approval another process committed concurrently.
"""

import tempfile
import unittest
from pathlib import Path

from _chalk.build import _merge_current_action
from _chalk.model import ProjectState, load_state, save_state

CHALK_TOML = """schema = 1
project_id = "persist-project"
title = "Persist project"
slug = "persist-project"

[settings]
default_profile = "draft"
speech_wpm = 150
lead_in_seconds = 0.6
tail_seconds = 0.35

[voice]
model = "fake"
revision = "rev"

[profiles.draft]
manim_quality = "-ql"
width = 854
height = 480
fps = 15
video_crf = 25
"""

SCRIPT = """# Persist

## Intro
<!-- chalk:segment intro -->

Hello.
"""


def _seed_project(root: Path) -> None:
    (root / "scenes").mkdir()
    (root / "chalk.toml").write_text(CHALK_TOML, encoding="utf-8")
    (root / "script.md").write_text(SCRIPT, encoding="utf-8")
    (root / "style.py").write_text("BG = '#000'\n", encoding="utf-8")
    (root / "chalk_runtime.py").write_text("class ChalkScene: pass\n", encoding="utf-8")
    (root / "scenes" / "intro.py").write_text("class Visual: pass\n", encoding="utf-8")


class MergeCurrentActionTest(unittest.TestCase):
    def test_merge_preserves_concurrent_selection_and_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_project(root)

            # A concurrent process selects a take and records a prior action.
            state = ProjectState()
            state.selected_takes["intro"] = "take-abc"
            state.current_actions["render:intro"] = "sha256:old"
            save_state(root, state)

            # A build finishes and records a new derived-action pointer.
            _merge_current_action(root, "cut:intro", "sha256:new")

            merged = load_state(root)
            self.assertEqual(
                merged.selected_takes.get("intro"),
                "take-abc",
                "take selection must survive a current-actions write",
            )
            self.assertEqual(merged.current_actions.get("cut:intro"), "sha256:new")
            self.assertEqual(
                merged.current_actions.get("render:intro"),
                "sha256:old",
                "existing action pointers must be preserved",
            )


if __name__ == "__main__":
    unittest.main()
