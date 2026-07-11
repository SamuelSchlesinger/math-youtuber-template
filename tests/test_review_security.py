"""Security regression: a note's artifact reference must not leak arbitrary
files' hashes/sizes into the tracked feedback.md (security review finding #2).
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from _chalk.review import _artifact_record


class ArtifactRecordScopeTest(unittest.TestCase):
    def test_outside_root_reference_is_not_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "proj"
            root.mkdir()
            project = SimpleNamespace(root=root)

            secret = base / "secret.txt"
            secret.write_text("token", encoding="utf-8")

            record = _artifact_record(project, str(secret))
            self.assertEqual(record, {"ref": str(secret)})
            self.assertNotIn("sha256", record)
            self.assertNotIn("size", record)

    def test_tilde_is_not_expanded_to_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = SimpleNamespace(root=Path(tmp))
            record = _artifact_record(project, "~/.ssh/id_rsa")
            self.assertNotIn("sha256", record)

    def test_in_project_artifact_still_binds_hash_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = SimpleNamespace(root=root)
            artifact = root / "output" / "full.mp4"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"video")

            record = _artifact_record(project, "output/full.mp4")
            self.assertIn("sha256", record)
            self.assertEqual(record["path"], "output/full.mp4")
            self.assertEqual(record["size"], len(b"video"))


if __name__ == "__main__":
    unittest.main()
