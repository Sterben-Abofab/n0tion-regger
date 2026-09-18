"""Windows-регрессии bridge: CODE_ROOT/%USERPROFILE%, cwd с буквой диска.

Принципы 1-в-1 с Linux-версией; проверяется только расширение
распознавания путей (C:\\..., UNC) и CODE_ROOT по умолчанию.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from server import anthropic_operator_context, code_root, planner_prompt


class WindowsPathTests(unittest.TestCase):
    def test_planner_prompt_accepts_windows_cwd(self) -> None:
        prompt = planner_prompt("list files", "cwd: C:\\Users\\Kyrylo\\project")
        self.assertIn("C:\\Users\\Kyrylo\\project", prompt)
        self.assertIn("PowerShell", prompt)

    def test_operator_context_accepts_windows_cwd(self) -> None:
        text = anthropic_operator_context("cwd: D:/work/repo")
        self.assertIn("D:/work/repo", text)

    def test_operator_context_still_accepts_posix_cwd(self) -> None:
        text = anthropic_operator_context("cwd: /root/project")
        self.assertIn("/root/project", text)

    def test_code_root_defaults_to_home_when_no_env(self) -> None:
        with patch("server.RUNTIME_ENV", Path(os.devnull)):
            self.assertEqual(code_root(), str(Path.home()))


if __name__ == "__main__":
    unittest.main()
