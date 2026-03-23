from __future__ import annotations

import unittest

from wechat_media_bridge import StoredAttachment, build_claude_input


class WechatMediaBridgeTests(unittest.TestCase):
    def test_build_claude_input_keeps_only_attachment_paths(self) -> None:
        attachment = StoredAttachment(
            key="img-1",
            kind="image",
            filename="image.png",
            local_path="/tmp/image.png",
            source_url="data:image/png;base64,AAAA",
            content_type="image/png",
            received_at=1.0,
        )

        prompt = build_claude_input("请处理", [attachment])

        self.assertIn("附件路径:", prompt)
        self.assertIn("- /tmp/image.png", prompt)
        self.assertNotIn("filename:", prompt)
        self.assertNotIn("source_url:", prompt)
        self.assertNotIn("content_type:", prompt)
        self.assertNotIn("data:image/png;base64,AAAA", prompt)


if __name__ == "__main__":
    unittest.main()
