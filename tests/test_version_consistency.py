"""Keep runtime, package metadata, JSON badge, and release docs in sync."""
import ast
from datetime import date
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class VersionConsistencyTests(unittest.TestCase):
    def test_declared_release_version_is_consistent_across_project_files(self):
        tree = ast.parse((ROOT / "constants.py").read_text(encoding="utf-8-sig"))
        values = [node.value.value for node in tree.body
                  if isinstance(node, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "PLUGIN_VERSION" for t in node.targets)
                  and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
        self.assertEqual(len(values), 1)
        version = values[0]

        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertEqual(re.search(r"(?m)^version:\s*(\S+)", metadata).group(1), version)
        self.assertEqual(re.search(r"(?m)^repo:\s*(\S+)", metadata).group(1),
                         "https://github.com/zcj-ui/astrbot_plugin_group_guardian")
        self.assertNotIn("\ufeff", (ROOT / "version.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((ROOT / "version.json").read_text(encoding="utf-8"))["version"], version)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertRegex(readme, rf"(?m)^> 当前版本：\*\*{re.escape(version)}\*\*")
        history = re.search(rf"(?m)^\|\s*(.+?)\s*\|\s*{re.escape(version)}\s*\|$", readme)
        self.assertIsNotNone(history, "README must include a release-history row for this version")
        self.assertTrue(history.group(1).strip())
        self.assertNotIn("本仓库 fork 自", readme)

        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        heading = re.search(rf"(?m)^## {re.escape(version)} - (\d{{4}}-\d{{2}}-\d{{2}})$", changelog)
        self.assertIsNotNone(heading, "CHANGELOG must have a dated heading for this version")
        date.fromisoformat(heading.group(1))


if __name__ == "__main__":
    unittest.main()
