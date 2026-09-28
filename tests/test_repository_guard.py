from __future__ import annotations

import unittest
from pathlib import Path


class RepositoryGuardTests(unittest.TestCase):
    def test_removed_execution_tree_and_imports_do_not_return(self) -> None:
        root = Path(__file__).resolve().parents[1]
        package = root / "PhyAgentOS"
        removed_tree = package / ("run" + "time")
        self.assertFalse(removed_tree.exists())

        forbidden_import = "PhyAgentOS." + "run" + "time"
        offenders = []
        for path in [*package.rglob("*.py"), *(root / "scripts").rglob("*.py")]:
            if forbidden_import in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])

        templates = package / "templates"
        removed_templates = [
            "TARGET" + "S.md",
            "SKILL" + "RUN" + "TIME" + ".md",
            "SESSION" + "S.md",
        ]
        self.assertEqual(
            [name for name in removed_templates if (templates / name).exists()], []
        )


if __name__ == "__main__":
    unittest.main()
