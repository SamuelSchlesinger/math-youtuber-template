"""Regression guards for the render fingerprint boundary (Finding 1).

Segment scenes (``scenes/<id>.py``) are hashed per segment and must stay out of
the shared fingerprint; shared scene helpers (``scenes/__init__.py`` and
``scenes/_*.py``) are imported by every scene and must be inside it. Before the
fix, editing a shared helper left the render key unchanged, so ``chalk watch``
served a stale cached video.
"""

import tempfile
import unittest
from pathlib import Path

from _chalk.build import _shared_python_hash


def _seed_project(root: Path) -> None:
    (root / "scenes").mkdir()
    (root / "style.py").write_text("BG = 'black'\n", encoding="utf-8")
    (root / "scenes" / "intro.py").write_text("# segment scene\n", encoding="utf-8")


class SharedSceneHelperHashTest(unittest.TestCase):
    def test_editing_shared_helper_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_project(root)
            helper = root / "scenes" / "_shapes.py"
            helper.write_text("class Box:\n    size = 1\n", encoding="utf-8")
            before = _shared_python_hash(root)
            helper.write_text("class Box:\n    size = 2\n", encoding="utf-8")
            self.assertNotEqual(
                before,
                _shared_python_hash(root),
                "editing scenes/_shapes.py must change the shared render fingerprint",
            )

    def test_editing_scenes_init_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_project(root)
            init = root / "scenes" / "__init__.py"
            init.write_text("", encoding="utf-8")
            before = _shared_python_hash(root)
            init.write_text("from scenes._shapes import Box\n", encoding="utf-8")
            self.assertNotEqual(before, _shared_python_hash(root))

    def test_editing_segment_scene_leaves_shared_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_project(root)
            before = _shared_python_hash(root)
            (root / "scenes" / "intro.py").write_text(
                "# changed segment scene\n", encoding="utf-8"
            )
            self.assertEqual(
                before,
                _shared_python_hash(root),
                "a segment scene is hashed per segment; it must not move the shared key",
            )


if __name__ == "__main__":
    unittest.main()
