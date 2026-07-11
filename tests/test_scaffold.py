"""Guards on the scaffold that `chalk new` stamps into every project.

The tool repo keeps two copies of the craft notes: `docs/CRAFT.md` (the
canonical reference) and `scaffold/docs/CRAFT.md` (the copy shipped into each
new project). They are the same document. Keeping them identical here means a
voice or craft fix to one can never silently fail to reach real projects the
way an earlier `move` -> `idea` edit did.
"""

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class ScaffoldSyncTest(unittest.TestCase):
    def test_craft_notes_match_between_repo_and_scaffold(self) -> None:
        canonical = REPO / "docs" / "CRAFT.md"
        shipped = REPO / "scaffold" / "docs" / "CRAFT.md"
        self.assertEqual(
            canonical.read_text(encoding="utf-8"),
            shipped.read_text(encoding="utf-8"),
            "docs/CRAFT.md and scaffold/docs/CRAFT.md have drifted; they are the "
            "same craft notes and must be edited identically.",
        )


if __name__ == "__main__":
    unittest.main()
