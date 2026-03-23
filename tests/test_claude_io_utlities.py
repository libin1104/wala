from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_io_utlities import (
    CLAUDE_GENERIC_ERROR_OUTPUT,
    MAX_CONTEXT_CHARS,
    SESSION_ID_NOT_FOUND_OUTPUT,
    _run_claude_prompt_result,
    append_memory,
    build_prompt,
    is_known_claude_error_output,
)


class _FakeProcess:
    def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self, input: str | None = None, timeout: float | None = None) -> tuple[str, str]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        return None

    def terminate(self) -> None:
        return None


class ClaudeIoUtilitiesTests(unittest.TestCase):
    @patch("claude_io_utlities.subprocess.Popen")
    def test_generic_cli_failure_keeps_debug_details(self, popen_mock: object) -> None:
        popen_mock.return_value = _FakeProcess(returncode=1, stderr="internal failure")  # type: ignore[attr-defined]

        result = _run_claude_prompt_result("cpu占用")

        self.assertFalse(result.ok)
        self.assertEqual(result.text, CLAUDE_GENERIC_ERROR_OUTPUT)
        self.assertEqual(result.error_type, "generic_error")
        self.assertEqual(result.stderr, "internal failure")
        self.assertEqual(result.return_code, 1)

    def test_session_not_found_output_with_memory_file_is_known_error(self) -> None:
        text = f"{SESSION_ID_NOT_FOUND_OUTPUT}\nmemory文件: /tmp/demo/memory.md"

        self.assertTrue(is_known_claude_error_output(text))

    def test_append_memory_sanitizes_data_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_file = Path(temp_dir) / "memory.md"

            append_memory(
                memory_file,
                "附件信息:\nsource_url: data:image/png;base64,AAAA",
                "ok",
            )

            content = memory_file.read_text(encoding="utf-8")
            self.assertIn("[embedded-binary omitted]", content)
            self.assertNotIn("data:image/png;base64,AAAA", content)

    def test_append_memory_sanitizes_large_base64_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_file = Path(temp_dir) / "memory.md"
            blob = "A" * 600

            append_memory(memory_file, blob, "ok")

            content = memory_file.read_text(encoding="utf-8")
            self.assertIn("[embedded-binary omitted]", content)
            self.assertNotIn(blob, content)

    def test_build_prompt_limits_history_by_total_chars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_file = Path(temp_dir) / "memory.md"
            huge_turn = "A" * (MAX_CONTEXT_CHARS + 500)
            memory_file.write_text(huge_turn, encoding="utf-8")

            prompt = build_prompt("现在时间", memory_file)

            self.assertEqual(prompt, "现在时间")


if __name__ == "__main__":
    unittest.main()
