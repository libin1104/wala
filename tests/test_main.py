from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import main
from claude_io_utlities import ClaudeCallResult
from wechat_openclaw_agent import SyncMessage


class _DummyEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, bool, dict[str, object]]] = []

    def emit(self, event_type: str, *, ok: bool = True, payload: dict[str, object] | None = None) -> None:
        self.events.append((event_type, ok, payload or {}))


class _DummyAgent:
    def __init__(self) -> None:
        self.emitter = _DummyEmitter()
        self.sent_texts: list[str] = []
        self.sent_resources: list[object] = []
        self.clear_pending_attachments_called = False

    def resolve_temp_dir(self, message: SyncMessage) -> Path:
        return Path("/tmp/peer-temp")

    def resolve_uid_root(self, message: SyncMessage) -> Path:
        return Path("/tmp/peer-uid")

    def load_pending_attachments(self, message: SyncMessage) -> list[object]:
        return []

    def clear_pending_attachments(self, message: SyncMessage) -> None:
        self.clear_pending_attachments_called = True

    def send_text(self, message: SyncMessage, text: str) -> None:
        self.sent_texts.append(text)

    def send_claude_resources(self, message: SyncMessage, resources: list[object]) -> None:
        self.sent_resources.extend(resources)

    def send_screenshot(self, message: SyncMessage) -> None:
        raise AssertionError("unexpected screenshot send")


class MainProcessMessageTests(unittest.TestCase):
    def test_claude_failure_emits_error_event_instead_of_response(self) -> None:
        agent = _DummyAgent()
        message = SyncMessage(
            message_id="1",
            text="cpu占用",
            create_time_ms=1773601109,
            from_user_id="user@im.wechat",
            to_user_id="default-im-bot",
            context_token="ctx-token",
            attachments=(),
            raw={},
        )
        claude_result = ClaudeCallResult(
            ok=False,
            text="Claude调用失败，请稍后重试",
            error_type="generic_error",
            stderr="internal failure",
            return_code=1,
        )

        with patch.object(main, "resolve_target", return_value=(Path("/tmp/temp"), "cpu占用", False)):
            with patch.object(main, "ask_claude_with_progress", return_value=claude_result):
                with patch.object(main, "append_memory") as append_memory_mock:
                    with patch.object(main, "build_runtime_prompt", return_value="runtime"):
                        main.process_message(agent, message=message)

        append_memory_mock.assert_called_once()
        self.assertEqual(agent.sent_texts, ["Claude调用失败，请稍后重试"])
        self.assertEqual(agent.sent_resources, [])
        self.assertFalse(agent.clear_pending_attachments_called)

        error_events = [event for event in agent.emitter.events if event[0] == "error"]
        self.assertEqual(len(error_events), 1)
        _, ok, payload = error_events[0]
        self.assertFalse(ok)
        self.assertEqual(payload["stage"], "claude_call")
        self.assertEqual(payload["target"], "temp")
        self.assertEqual(payload["message"], "Claude调用失败，请稍后重试")
        self.assertEqual(payload["error_type"], "generic_error")
        self.assertEqual(payload["stderr"], "internal failure")
        self.assertEqual(payload["return_code"], 1)
        self.assertFalse(any(event[0] == "claude_response" for event in agent.emitter.events))


if __name__ == "__main__":
    unittest.main()
