# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
"""Every source file says its license (SPDX) and copyright at the top, in its own comment style (as in AI-Beach)."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (".py", ".js", ".cjs", ".mjs", ".ts", ".css", ".html", ".sh", ".ps1")
HEAD_LINES = 4
AGPL = "SPDX-License-Identifier: AGPL-3.0-only"
COPYRIGHT = "Copyright (C) 2026 Becky Northaven and the Debate Hall contributors"


def repo_files() -> list[str]:
    """The repository's files (tracked, plus new ones not ignored), as forward-slash paths."""
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         check=True, timeout=60)
    return [p for p in out.stdout.decode("utf-8").splitlines() if (ROOT / p).is_file()]


class SpdxTests(unittest.TestCase):
    def test_every_source_file_has_the_two_lines(self):
        missing = []
        for p in repo_files():
            if not p.endswith(SOURCE):
                continue
            head = "\n".join((ROOT / p).read_text(encoding="utf-8", errors="replace").splitlines()[:HEAD_LINES])
            if AGPL not in head or COPYRIGHT not in head:
                missing.append(p)
        self.assertEqual(missing, [], "add the two license lines at the top (see CONTRIBUTING.md)")


if __name__ == "__main__":
    unittest.main()
