from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wechat_browser_agent import AGENT_REPLY_PREFIX, BrowserFileHelperAgent, chunk_text_with_prefix
from wechat_media_bridge import PendingAttachmentStore, StoredAttachment


class _DummyEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, bool, dict[str, object]]] = []

    def emit(self, event_type: str, *, ok: bool = True, payload: dict[str, object] | None = None) -> None:
        self.events.append((event_type, ok, payload or {}))


class BrowserAttachmentHandlingTests(unittest.TestCase):
    def test_class_name_hint_marks_item_as_self_attachment(self) -> None:
        agent = BrowserFileHelperAgent()
        item = {"class_name": "msg-item mine", "dataset": {}}

        self.assertTrue(agent._is_self_message_item(item))

    def test_failed_attachment_capture_is_retriable_until_success(self) -> None:
        agent = BrowserFileHelperAgent()
        agent._dom_attachment_scan_warmed = True
        agent.emitter = _DummyEmitter()
        attachment_item = {
            "has_image": True,
            "file_title": "",
            "text": "",
            "image_src": "https://example.com/image.png",
            "link_href": "",
            "dataset": {},
        }
        agent._snapshot_recent_message_items = lambda page: [attachment_item]  # type: ignore[method-assign]

        with tempfile.TemporaryDirectory() as temp_dir:
            store_path = Path(temp_dir) / "pending_attachments.json"
            agent.pending_attachment_store = PendingAttachmentStore(store_path)

            agent._capture_image_attachment = lambda page, item: StoredAttachment(  # type: ignore[method-assign]
                key=str(item["attachment_key"]),
                kind="image",
                filename="image.png",
                local_path="",
                source_url=str(item["image_src"]),
                content_type="image/png",
                received_at=0.0,
                error="download failed",
            )
            first_result = agent._collect_new_attachments(page=None)
            self.assertEqual(first_result, [])
            self.assertEqual(agent.pending_attachment_store.load(), [])
            self.assertNotIn(str(attachment_item["attachment_key"]), agent._seen_dom_attachment_keys)

            agent._capture_image_attachment = lambda page, item: StoredAttachment(  # type: ignore[method-assign]
                key=str(item["attachment_key"]),
                kind="image",
                filename="image.png",
                local_path="/tmp/image.png",
                source_url=str(item["image_src"]),
                content_type="image/png",
                received_at=1.0,
            )
            second_result = agent._collect_new_attachments(page=None)
            self.assertEqual(len(second_result), 1)
            self.assertEqual(agent.pending_attachment_store.load()[0].local_path, "/tmp/image.png")
            self.assertIn(str(attachment_item["attachment_key"]), agent._seen_dom_attachment_keys)

    def test_recent_outbound_file_is_not_treated_as_inbound_attachment(self) -> None:
        agent = BrowserFileHelperAgent()
        agent._dom_attachment_scan_warmed = True
        agent.emitter = _DummyEmitter()
        file_item = {
            "has_file": True,
            "has_image": False,
            "file_title": "report.pdf",
            "file_desc": "",
            "text": "report.pdf",
            "link_href": "https://example.com/report.pdf",
            "image_src": "",
            "dataset": {},
        }
        agent._snapshot_recent_message_items = lambda page: [file_item]  # type: ignore[method-assign]
        agent._capture_file_attachment = lambda page, item: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("outbound file should not be captured as inbound")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store_path = Path(temp_dir) / "pending_attachments.json"
            agent.pending_attachment_store = PendingAttachmentStore(store_path)
            agent.outbound_attachment_tracker.remember("file", "report.pdf")

            result = agent._collect_new_attachments(page=None)

            self.assertEqual(result, [])
            self.assertIn(str(file_item["attachment_key"]), agent._seen_dom_attachment_keys)

    def test_self_sent_image_is_not_treated_as_inbound_attachment(self) -> None:
        agent = BrowserFileHelperAgent()
        agent._dom_attachment_scan_warmed = True
        agent.emitter = _DummyEmitter()
        image_item = {
            "class_name": "msg-item mine",
            "has_file": False,
            "has_image": True,
            "file_title": "",
            "file_desc": "",
            "text": "",
            "link_href": "",
            "image_src": "blob:https://example.com/outbound-image",
            "dataset": {},
        }
        agent._snapshot_recent_message_items = lambda page: [image_item]  # type: ignore[method-assign]
        agent._capture_image_attachment = lambda page, item: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("self-sent image should not be captured as inbound")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store_path = Path(temp_dir) / "pending_attachments.json"
            agent.pending_attachment_store = PendingAttachmentStore(store_path)
            agent.outbound_attachment_tracker.remember("image", "generated.png")

            result = agent._collect_new_attachments(page=None)

            self.assertEqual(result, [])
            self.assertIn(str(image_item["attachment_key"]), agent._seen_dom_attachment_keys)

    def test_outbound_file_dedupe_is_consumed_once(self) -> None:
        agent = BrowserFileHelperAgent()
        agent._dom_attachment_scan_warmed = True
        agent.emitter = _DummyEmitter()
        sent_item = {
            "has_file": True,
            "has_image": False,
            "file_title": "report.pdf",
            "file_desc": "",
            "text": "report.pdf",
            "link_href": "https://example.com/sent-report.pdf",
            "image_src": "",
            "dataset": {"msgid": "sent"},
        }
        inbound_item = {
            "has_file": True,
            "has_image": False,
            "file_title": "report.pdf",
            "file_desc": "",
            "text": "report.pdf",
            "link_href": "https://example.com/inbound-report.pdf",
            "image_src": "",
            "dataset": {"msgid": "inbound"},
        }
        agent._snapshot_recent_message_items = lambda page: [sent_item, inbound_item]  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as temp_dir:
            store_path = Path(temp_dir) / "pending_attachments.json"
            agent.pending_attachment_store = PendingAttachmentStore(store_path)
            agent.outbound_attachment_tracker.remember("file", "report.pdf")
            agent._capture_file_attachment = lambda page, item: StoredAttachment(  # type: ignore[method-assign]
                key=str(item["attachment_key"]),
                kind="file",
                filename=str(item["file_title"]),
                local_path=f"/tmp/{item['file_title']}",
                source_url=str(item["link_href"]),
                content_type="application/pdf",
                received_at=2.0,
            )

            result = agent._collect_new_attachments(page=None)

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].source_url, "https://example.com/inbound-report.pdf")
            self.assertIn(str(sent_item["attachment_key"]), agent._seen_dom_attachment_keys)
            self.assertIn(str(inbound_item["attachment_key"]), agent._seen_dom_attachment_keys)


class BrowserReplyFormattingTests(unittest.TestCase):
    def test_send_text_prefixes_web_reply(self) -> None:
        agent = BrowserFileHelperAgent()
        agent.emitter = _DummyEmitter()
        filled_texts: list[str] = []

        class _DummyLocator:
            def click(self) -> None:
                return None

            def fill(self, text: str) -> None:
                filled_texts.append(text)

        class _DummyPage:
            def wait_for_timeout(self, _ms: int) -> None:
                return None

        agent._resolve_textbox = lambda page: _DummyLocator()  # type: ignore[method-assign]
        agent._click_send_button = lambda page: None  # type: ignore[method-assign]

        agent._send_text(_DummyPage(), "hello")

        self.assertEqual(filled_texts, [f"{AGENT_REPLY_PREFIX}hello"])

    def test_chunked_reply_keeps_agent_prefix_on_every_segment(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
