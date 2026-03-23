from __future__ import annotations

import unittest

from wechat_openclaw_agent import (
    AGENT_REPLY_PREFIX,
    OpenClawWeixinAgent,
    SyncMessage,
    chunk_text_with_prefix,
)


class _DummyEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, bool, dict[str, object]]] = []

    def emit(self, event_type: str, *, ok: bool = True, payload: dict[str, object] | None = None) -> None:
        self.events.append((event_type, ok, payload or {}))


class OpenClawReplyFormattingTests(unittest.TestCase):
    def test_chunked_reply_keeps_chunk_markers_without_prefix(self) -> None:
        message = "abcdefghi"
        max_chars = len(AGENT_REPLY_PREFIX) + len("[1/3] ") + 2

        chunks = chunk_text_with_prefix(
            message,
            max_chars_per_message=max_chars,
            prefix=AGENT_REPLY_PREFIX,
        )

        self.assertEqual(
            chunks,
            [
                f"{AGENT_REPLY_PREFIX}[1/5] ab",
                f"{AGENT_REPLY_PREFIX}[2/5] cd",
                f"{AGENT_REPLY_PREFIX}[3/5] ef",
                f"{AGENT_REPLY_PREFIX}[4/5] gh",
                f"{AGENT_REPLY_PREFIX}[5/5] i",
            ],
        )

    def test_send_text_uses_current_message_context(self) -> None:
        agent = OpenClawWeixinAgent()
        agent.emitter = _DummyEmitter()
        captured_payloads: list[dict[str, object]] = []

        def _capture(endpoint: str, payload: dict[str, object], *, timeout_s: float) -> dict[str, object]:
            self.assertEqual(endpoint, "ilink/bot/sendmessage")
            self.assertEqual(timeout_s, 15.0)
            captured_payloads.append(payload)
            return {}

        agent._post_api_json = _capture  # type: ignore[method-assign]
        message = SyncMessage(
            message_id="1",
            text="hello",
            create_time_ms=1,
            from_user_id="peer@im.wechat",
            to_user_id="bot@im.bot",
            context_token="ctx-token",
            attachments=(),
            raw={},
        )

        agent.send_text(message, "world")

        self.assertEqual(len(captured_payloads), 1)
        payload = captured_payloads[0]["msg"]  # type: ignore[index]
        self.assertEqual(payload["to_user_id"], "peer@im.wechat")  # type: ignore[index]
        self.assertEqual(payload["context_token"], "ctx-token")  # type: ignore[index]
        self.assertEqual(
            payload["item_list"][0]["text_item"]["text"],  # type: ignore[index]
            f"{AGENT_REPLY_PREFIX}world",
        )

    def test_peer_temp_dirs_are_isolated(self) -> None:
        agent = OpenClawWeixinAgent(profile_name="default")
        message_a = SyncMessage(
            message_id="1",
            text="a",
            create_time_ms=1,
            from_user_id="alice@im.wechat",
            to_user_id="bot@im.bot",
            context_token="ctx-a",
            attachments=(),
            raw={},
        )
        message_b = SyncMessage(
            message_id="2",
            text="b",
            create_time_ms=2,
            from_user_id="bob@im.wechat",
            to_user_id="bot@im.bot",
            context_token="ctx-b",
            attachments=(),
            raw={},
        )

        self.assertNotEqual(agent.resolve_temp_dir(message_a), agent.resolve_temp_dir(message_b))
        self.assertNotEqual(agent.resolve_uid_root(message_a), agent.resolve_uid_root(message_b))


if __name__ == "__main__":
    unittest.main()
