#!/usr/bin/env python3
"""基于 Playwright Chromium 的微信文件传输助手代理。"""

from __future__ import annotations

import base64
import binascii
import html
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Optional

from schedual_utilities import (
    execute_due_schedule_tasks,
    load_email_config_from_env,
    render_markdown_email_html,
    send_email,
    sync_and_save_schedule_state,
)
from wechat_media_bridge import (
    ATTACHMENT_ACK_DELAY_S,
    DEFAULT_HTTP_TIMEOUT_S,
    PENDING_ATTACHMENTS_ACK_TEXT,
    PendingAttachmentStore,
    PreparedOutboundResource,
    StoredAttachment,
    build_attachment_key,
    build_inbound_attachment_path,
    ensure_media_dirs,
    make_inbound_error_attachment,
    prepare_outbound_resource,
    store_inbound_bytes,
)

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import Page, Response, sync_playwright
except ImportError:  # pragma: no cover - 依赖缺失时在运行时给出更明确错误
    PlaywrightError = Exception
    Page = Any  # type: ignore[assignment]
    Response = Any  # type: ignore[assignment]
    sync_playwright = None


FILEHELPER_URL = "https://filehelper.weixin.qq.com/"
CHAT_NAME = "filehelper"
DEFAULT_POLL_INTERVAL_S = 0.5
DEFAULT_LOGIN_TIMEOUT_S = 300.0
DEFAULT_MESSAGE_SYNC_CUTOFF_S = 5
DEFAULT_MAX_CHARS_PER_MESSAGE = 2000
DEFAULT_OUTBOUND_DEDUPE_TTL_S = 90.0
DEFAULT_OUTBOUND_ATTACHMENT_DEDUPE_TTL_S = 30.0
DEFAULT_LOGIN_EMAIL_INTERVAL_S = 60.0
DEFAULT_PROFILE_NAME = "default"
AGENT_REPLY_PREFIX = ""
RUNTIME_ROOT = Path.home() / ".wclaude_sessions" / "runtime"
PROFILE_ROOT = RUNTIME_ROOT / "chromium_profiles"
SCREENSHOT_ROOT = RUNTIME_ROOT / "screenshots"
LOGIN_QRCODE_SELECTOR = "img.qrcode-img"
PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TEXTBOX_SELECTORS = (
    "textarea",
    "[contenteditable='true']",
    "[role='textbox']",
    "input:not([type='file'])",
)
FILE_INPUT_SELECTOR = "input[type='file']"
SEND_BUTTON_SELECTORS = (
    "a:has-text('发送')",
    "text=发送",
)
CHAT_BODY_SELECTOR = ".chat-panel__body"
MESSAGE_ITEM_SELECTOR = ".msg-item"
FILE_MESSAGE_SELECTOR = ".msg-file, .msg-appmsg"
FILE_TITLE_SELECTOR = ".msg-file__title, .msg-appmsg__title"
FILE_DESC_SELECTOR = ".msg-file__desc, .msg-appmsg__desc"
IMAGE_MESSAGE_SELECTOR = ".msg-image img, img.msg-image"
DOWNLOAD_TRIGGER_SELECTOR = ".icon__download, [download]"
ATTACHMENT_SCAN_WINDOW = 40
DEFAULT_ATTACHMENT_SCAN_INTERVAL_S = 1.0
SELF_MESSAGE_HINT_PATTERN = re.compile(
    r"(^|[^a-z])(self|outgoing|mine|owner|sender|is_send|issend|send|sent|me|right)([^a-z]|$)"
)
CP1252_UNICODE_TO_BYTE = {
    0x20AC: 0x80,
    0x201A: 0x82,
    0x0192: 0x83,
    0x201E: 0x84,
    0x2026: 0x85,
    0x2020: 0x86,
    0x2021: 0x87,
    0x02C6: 0x88,
    0x2030: 0x89,
    0x0160: 0x8A,
    0x2039: 0x8B,
    0x0152: 0x8C,
    0x017D: 0x8E,
    0x2018: 0x91,
    0x2019: 0x92,
    0x201C: 0x93,
    0x201D: 0x94,
    0x2022: 0x95,
    0x2013: 0x96,
    0x2014: 0x97,
    0x02DC: 0x98,
    0x2122: 0x99,
    0x0161: 0x9A,
    0x203A: 0x9B,
    0x0153: 0x9C,
    0x017E: 0x9E,
    0x0178: 0x9F,
}


@dataclass(frozen=True)
class SyncMessage:
    """从 webwxsync 中提取出的文本消息。"""

    message_id: str
    text: str
    create_time: int
    from_user_name: str
    to_user_name: str
    raw: dict[str, Any]


class JsonEmitter:
    """统一输出 NDJSON 事件。"""

    def __init__(self, session: str, chat: str = CHAT_NAME) -> None:
        self.session = session
        self.chat = chat

    def emit(self, event_type: str, *, ok: bool = True, payload: Optional[dict[str, Any]] = None) -> None:
        record = {
            "type": event_type,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "session": self.session,
            "chat": self.chat,
            "ok": ok,
            "payload": payload or {},
        }
        print(json.dumps(record, ensure_ascii=False), flush=True)


class OutboundTracker:
    """记录近期发送内容，避免 webwxsync 回显造成自触发。"""

    def __init__(self, ttl_s: float = DEFAULT_OUTBOUND_DEDUPE_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._entries: Deque[tuple[float, str]] = deque()

    def remember(self, text: str, now: Optional[float] = None) -> None:
        current = time.monotonic() if now is None else now
        self._prune(current)
        self._entries.append((current, normalize_message_text(text)))

    def matches(self, text: str, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else now
        self._prune(current)
        normalized = normalize_message_text(text)
        return any(item_text == normalized for _, item_text in self._entries)

    def _prune(self, now: float) -> None:
        while self._entries and now - self._entries[0][0] > self.ttl_s:
            self._entries.popleft()


class OutboundAttachmentTracker:
    """记录刚发送的附件名，避免网页扫描把出站文件误判为新入站附件。"""

    def __init__(self, ttl_s: float = DEFAULT_OUTBOUND_ATTACHMENT_DEDUPE_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._entries: Deque[tuple[float, str, str]] = deque()

    def remember(self, kind: str, display_name: str, now: Optional[float] = None) -> None:
        normalized_kind = self._normalize(kind)
        normalized_name = self._normalize_name(display_name)
        if not normalized_kind or not normalized_name:
            return
        current = time.monotonic() if now is None else now
        self._prune(current)
        self._entries.append((current, normalized_kind, normalized_name))

    def consume_match(
        self,
        kind: str,
        candidates: list[str],
        now: Optional[float] = None,
    ) -> bool:
        normalized_kind = self._normalize(kind)
        normalized_candidates = [
            self._normalize_name(candidate)
            for candidate in candidates
            if self._normalize_name(candidate)
        ]
        if not normalized_kind or not normalized_candidates:
            return False
        current = time.monotonic() if now is None else now
        self._prune(current)

        matched = False
        remaining: Deque[tuple[float, str, str]] = deque()
        for entry in self._entries:
            _, entry_kind, entry_name = entry
            if (
                not matched
                and entry_kind == normalized_kind
                and any(
                    entry_name == candidate or entry_name in candidate
                    for candidate in normalized_candidates
                )
            ):
                matched = True
                continue
            remaining.append(entry)
        self._entries = remaining
        return matched

    def _prune(self, now: float) -> None:
        while self._entries and now - self._entries[0][0] > self.ttl_s:
            self._entries.popleft()

    @staticmethod
    def _normalize(value: str) -> str:
        return (value or "").strip().lower()

    @staticmethod
    def _normalize_name(value: str) -> str:
        return (Path(value or "").name or "").strip().lower()


def normalize_message_text(text: Any) -> str:
    """规范化 webwx 文本内容。"""
    normalized = html.unescape("" if text is None else str(text))
    normalized = re.sub(r"<br\s*/?>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</?(?:span|div|p|label|font)[^>]*>", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.replace("\u2005", " ")
    normalized = repair_mojibake_text(normalized)
    return normalized.strip()


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def repair_mojibake_text(text: str) -> str:
    """尝试修复 UTF-8 被按 latin-1 解码后的乱码。"""
    if not text or _contains_cjk(text):
        return text
    try:
        repaired = text.encode("latin1").decode("utf-8")
    except UnicodeError:
        repaired = _repair_mixed_mojibake_text(text)
        if repaired is None:
            return text
    if repaired == text or _contains_cjk(repaired):
        return repaired
    return text


def _repair_mixed_mojibake_text(text: str) -> Optional[str]:
    data = bytearray()
    for ch in text:
        codepoint = ord(ch)
        if codepoint <= 0xFF:
            data.append(codepoint)
            continue
        mapped = CP1252_UNICODE_TO_BYTE.get(codepoint)
        if mapped is None:
            return None
        data.append(mapped)
    try:
        return data.decode("utf-8")
    except UnicodeError:
        return None


def repair_payload_strings(value: Any) -> Any:
    """递归修复 JSON 载荷中的乱码文本。"""
    if isinstance(value, str):
        return repair_mojibake_text(value)
    if isinstance(value, list):
        return [repair_payload_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_payload_strings(item) for key, item in value.items()}
    return value


def parse_json_body(body: bytes, *, content_type: str = "") -> dict[str, Any]:
    """从响应原始字节解析 JSON，避免 Playwright 文本解码导致中文乱码。"""
    encodings: list[str] = []
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type or "", flags=re.IGNORECASE)
    if match:
        encodings.append(match.group(1))
    encodings.extend(["utf-8", "utf-8-sig", "latin1"])

    last_error: Optional[Exception] = None
    seen: set[str] = set()
    for encoding in encodings:
        normalized_encoding = encoding.lower()
        if normalized_encoding in seen:
            continue
        seen.add(normalized_encoding)
        try:
            text = body.decode(encoding)
            payload = json.loads(text)
            repaired = repair_payload_strings(payload)
            if not isinstance(repaired, dict):
                raise ValueError("json payload is not an object")
            return repaired
        except (UnicodeDecodeError, json.JSONDecodeError, LookupError, ValueError) as exc:
            last_error = exc
    raise ValueError(f"unable to decode json body: {last_error}")


def chunk_text_with_prefix(
    text: str,
    max_chars_per_message: int = DEFAULT_MAX_CHARS_PER_MESSAGE,
    *,
    prefix: str = "",
) -> list[str]:
    """按微信单条上限分段，并为每段附加统一前缀。"""
    normalized_text = "" if text is None else str(text)
    normalized_prefix = "" if prefix is None else str(prefix)
    if normalized_prefix and normalized_text.startswith(normalized_prefix):
        normalized_text = normalized_text[len(normalized_prefix) :]
    if max_chars_per_message <= 0:
        raise ValueError("max_chars_per_message must be > 0")
    if len(normalized_prefix) + len(normalized_text) <= max_chars_per_message:
        return [normalized_prefix + normalized_text]
    if max_chars_per_message <= len(normalized_prefix) + len("[1/1] "):
        raise ValueError("max_chars_per_message is too small for chunk prefix")

    total_chunks = 1
    while True:
        chunks: list[str] = []
        cursor = 0
        chunk_index = 1
        text_len = len(normalized_text)
        while cursor < text_len:
            chunk_prefix = f"[{chunk_index}/{total_chunks}] "
            payload_limit = max_chars_per_message - len(normalized_prefix) - len(chunk_prefix)
            if payload_limit <= 0:
                raise ValueError("max_chars_per_message is too small for chunk payload")
            next_cursor = min(text_len, cursor + payload_limit)
            chunks.append(normalized_prefix + chunk_prefix + normalized_text[cursor:next_cursor])
            cursor = next_cursor
            chunk_index += 1
        if len(chunks) == total_chunks:
            return chunks
        total_chunks = len(chunks)


def extract_sync_messages(payload: dict[str, Any]) -> list[SyncMessage]:
    """从 webwxsync 响应中提取文件传输助手文本消息。"""
    messages: list[SyncMessage] = []
    for item in payload.get("AddMsgList", []) or []:
        try:
            msg_type = int(item.get("MsgType") or 0)
        except (TypeError, ValueError):
            msg_type = 0
        if msg_type != 1:
            continue
        from_user_name = str(item.get("FromUserName") or "")
        to_user_name = str(item.get("ToUserName") or "")
        if CHAT_NAME not in from_user_name and CHAT_NAME not in to_user_name:
            continue
        text = normalize_message_text(item.get("Content", ""))
        if not text:
            continue
        try:
            create_time = int(item.get("CreateTime") or 0)
        except (TypeError, ValueError):
            create_time = 0
        message_id = str(item.get("MsgId") or f"{create_time}:{text}")
        messages.append(
            SyncMessage(
                message_id=message_id,
                text=text,
                create_time=create_time,
                from_user_name=from_user_name,
                to_user_name=to_user_name,
                raw=item,
            )
        )
    return messages


def format_runtime_error(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "配置或系统资源不存在，请检查路径与安装状态"
    if isinstance(exc, PermissionError):
        return "权限不足，请检查系统辅助功能与文件权限"
    text = str(exc).strip()
    return f"执行失败：{text or exc.__class__.__name__}"


def _resolve_profile_dir(profile_root: Path, profile_name: str) -> Path:
    normalized = profile_name.strip()
    if not normalized:
        raise ValueError("profile_name 不能为空")
    if not PROFILE_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "profile_name 非法：仅支持字母、数字、点、下划线、连字符，且必须以字母或数字开头"
        )
    return profile_root / normalized


def cleanup_stale_profiles(profile_root: Path, *, max_age_s: float = 6 * 3600) -> None:
    """清理上次异常退出遗留的临时 profile。"""
    if not profile_root.exists():
        return
    now = time.time()
    for path in profile_root.glob("chromium-temp-*"):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if now - stat.st_mtime < max_age_s:
            continue
        shutil.rmtree(path, ignore_errors=True)


class BrowserFileHelperAgent:
    """文件传输助手浏览器代理。"""

    def __init__(
        self,
        *,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        login_timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S,
        profile_root: Path = PROFILE_ROOT,
        profile_name: str = DEFAULT_PROFILE_NAME,
    ) -> None:
        self.poll_interval_s = poll_interval_s
        self.login_timeout_s = login_timeout_s
        self.profile_root = profile_root
        self.profile_name = profile_name
        self.profile_dir = _resolve_profile_dir(profile_root, profile_name)
        self.runtime_id = uuid.uuid4().hex
        self.emitter = JsonEmitter(self.runtime_id)
        self.outbound_tracker = OutboundTracker()
        self.outbound_attachment_tracker = OutboundAttachmentTracker()
        self.message_queue: Deque[SyncMessage] = deque()
        self.seen_message_ids: set[str] = set()
        self.cutoff_create_time = int(time.time()) - DEFAULT_MESSAGE_SYNC_CUTOFF_S
        self.stop_requested = False
        self._login_required_emitted = False
        self._signal_handlers_installed = False
        self._previous_signal_handlers: dict[int, Any] = {}
        self._browser_closed_emitted = False
        self._login_email_config: Optional[dict[str, object]] = None
        self._login_email_sent_count = 0
        self._device_name = self._resolve_device_name()
        self._login_success_message_sent = False
        self.pending_attachment_store = PendingAttachmentStore()
        self._seen_dom_attachment_keys: set[str] = set()
        self._dom_attachment_scan_warmed = False
        self._next_attachment_scan_at = 0.0
        self.browser_mode = "headless"

    def bootstrap(self) -> None:
        ensure_media_dirs()
        self.profile_root.mkdir(parents=True, exist_ok=True)
        SCREENSHOT_ROOT.mkdir(parents=True, exist_ok=True)
        cleanup_stale_profiles(self.profile_root)
        self._install_signal_handlers()
        self.emitter.emit(
            "status",
            payload={
                "stage": "startup",
                "profile_dir": str(self.profile_dir),
                "profile_name": self.profile_name,
            },
        )

        if sync_playwright is None:
            raise RuntimeError("缺少依赖 playwright，请先执行 `pip install playwright`")

    def launch_browser(self, playwright: Any, *, headless: bool = True) -> tuple[Any, Page]:
        self.browser_mode = "headless" if headless else "headed"
        context, page = self._launch_browser_login(playwright, headless=headless)
        self._bind_lifecycle_events(context, page)
        return context, page

    def prepare_session(self, page: Page) -> None:
        self._wait_for_login(page, timeout_s=self.login_timeout_s, allow_emit_login_required=True)
        self.emitter.emit("auth", payload={"state": "logged_in"})
        self._send_login_success_message(page)
        self._prepare_schedule_runtime()
        self.emitter.emit("status", payload={"stage": "background_ready", "mode": self.browser_mode})
        page.on("response", self._on_response)

    def begin_listening(self) -> None:
        self.emitter.emit("status", payload={"stage": "listening"})

    def should_continue(self, page: Page) -> bool:
        if self.stop_requested:
            return False
        if page.is_closed():
            self._mark_browser_closed("page")
            return False
        return True

    def run_due_schedule_tasks_once(self) -> None:
        self._run_due_schedule_tasks_once()

    def scan_attachments_if_due(self, page: Page) -> list[StoredAttachment]:
        return self._scan_attachments_if_due(page)

    def scan_attachments(self, page: Page, *, force: bool = False) -> list[StoredAttachment]:
        return self._scan_attachments_if_due(page, force=force)

    def dequeue_message(self) -> Optional[SyncMessage]:
        if not self.message_queue:
            return None
        return self.message_queue.popleft()

    def send_text(self, page: Page, text: str) -> None:
        self._send_text(page, text)

    def send_screenshot(self, page: Page) -> None:
        self._send_screenshot(page)

    def load_pending_attachments(self) -> list[StoredAttachment]:
        return self.pending_attachment_store.load()

    def clear_pending_attachments(self) -> None:
        self.pending_attachment_store.clear()

    def send_claude_resources(self, page: Page, resources: list[Any]) -> None:
        self._send_claude_resources(page, resources)

    def send_pending_attachment_ack_if_needed(self, page: Page) -> None:
        self._send_pending_attachment_ack_if_needed(page)

    def wait_for_next_poll(self, page: Page) -> bool:
        try:
            page.wait_for_timeout(int(self.poll_interval_s * 1000))
        except PlaywrightError:
            self._mark_browser_closed("page")
            return False
        return not self.stop_requested

    def should_suppress_exception(self, exc: Exception) -> bool:
        if isinstance(exc, PlaywrightError):
            return self.stop_requested or self._browser_closed_emitted
        if isinstance(exc, RuntimeError):
            return (
                (self.stop_requested or self._browser_closed_emitted)
                and str(exc).strip() == "浏览器已关闭"
            )
        return False

    def shutdown(self, context: Any | None = None) -> None:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        self._cleanup_profile_dir()
        self._restore_signal_handlers()

    def _install_signal_handlers(self) -> None:
        if self._signal_handlers_installed:
            return

        def _handler(signum: int, _frame: Any) -> None:
            self.stop_requested = True
            self.emitter.emit("status", payload={"stage": "signal_received", "signal": signum})

        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _handler)
        self._signal_handlers_installed = True

    def _restore_signal_handlers(self) -> None:
        if not self._signal_handlers_installed:
            return
        for signum, previous in self._previous_signal_handlers.items():
            signal.signal(signum, previous)
        self._previous_signal_handlers.clear()
        self._signal_handlers_installed = False

    def _resolve_device_name(self) -> str:
        try:
            result = subprocess.run(
                ["scutil", "--get", "ComputerName"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0:
                name = result.stdout.strip()
                if name:
                    return name
        except Exception:
            pass
        try:
            name = os.uname().nodename.strip()
            if name:
                return name
        except Exception:
            pass
        return "当前设备"

    def _launch_context(self, playwright: Any, *, headless: bool) -> tuple[Any, Page]:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=headless,
            no_viewport=True,
            accept_downloads=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-size=1440,1200",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(FILEHELPER_URL, wait_until="domcontentloaded")
        return context, page

    def _launch_browser_login(self, playwright: Any, *, headless: bool) -> tuple[Any, Page]:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        context, page = self._launch_context(playwright, headless=headless)
        self.emitter.emit(
            "status",
            payload={"stage": "login_page_opened", "mode": self.browser_mode},
        )
        return context, page

    def _bind_lifecycle_events(self, context: Any, page: Page) -> None:
        page.on("close", lambda *_: self._mark_browser_closed("page"))
        context.on("close", lambda *_: self._mark_browser_closed("context"))

    def _wait_for_login(
        self,
        page: Page,
        *,
        timeout_s: float,
        allow_emit_login_required: bool,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        next_login_email_at = 0.0
        login_delivery = "email" if self.browser_mode == "headless" else "browser"
        while time.monotonic() < deadline and not self.stop_requested:
            if page.is_closed():
                self._mark_browser_closed("page")
                raise RuntimeError("浏览器已关闭")
            if self._is_logged_in(page):
                return
            if allow_emit_login_required and not self._login_required_emitted:
                self._login_required_emitted = True
                self.emitter.emit(
                    "auth",
                    ok=False,
                    payload={"state": "login_required", "url": page.url, "delivery": login_delivery},
                )
            now = time.monotonic()
            if (
                allow_emit_login_required
                and self.browser_mode == "headless"
                and now >= next_login_email_at
            ):
                self._send_login_page_email(page)
                next_login_email_at = now + DEFAULT_LOGIN_EMAIL_INTERVAL_S
            try:
                page.wait_for_timeout(1000)
            except PlaywrightError as exc:
                self._mark_browser_closed("page")
                raise RuntimeError("浏览器已关闭") from exc
        if self.stop_requested:
            raise KeyboardInterrupt("stopped")
        raise TimeoutError("登录超时，请重新运行并完成扫码或确认")

    def _send_login_success_message(self, page: Page) -> None:
        if self._login_success_message_sent or not self._login_required_emitted:
            return
        text = f"Ai agent 已成功连接{self._device_name}，今天想做点啥？"
        try:
            self._send_text(page, text)
            self._login_success_message_sent = True
            self.emitter.emit(
                "status",
                payload={
                    "stage": "login_success_message_sent",
                    "device_name": self._device_name,
                },
            )
        except Exception as exc:
            self.emitter.emit(
                "error",
                ok=False,
                payload={
                    "stage": "login_success_message",
                    "message": format_runtime_error(exc),
                    "device_name": self._device_name,
                },
            )

    def _is_logged_in(self, page: Page) -> bool:
        try:
            if not page.url.startswith(FILEHELPER_URL):
                return False
            has_upload = page.locator(FILE_INPUT_SELECTOR).count() > 0
            has_textbox = any(page.locator(selector).count() > 0 for selector in TEXTBOX_SELECTORS)
            return has_upload and has_textbox
        except PlaywrightError:
            return False

    def _prepare_schedule_runtime(self) -> None:
        """登录成功后同步定时任务状态。"""
        try:
            _, _, changed = sync_and_save_schedule_state(skip_past_due=True)
            if changed:
                self.emitter.emit(
                    "status",
                    payload={"stage": "schedule_runtime_synchronized"},
                )
        except Exception as exc:
            self.emitter.emit(
                "error",
                ok=False,
                payload={"stage": "schedule_runtime", "message": format_runtime_error(exc)},
            )

    def _run_due_schedule_tasks_once(self) -> None:
        """在主循环中检查并执行已到点的邮件定时任务。"""

        def _report_task_error(task: dict[str, Any], message: str) -> None:
            self.emitter.emit(
                "error",
                ok=False,
                payload={
                    "stage": "schedule_task",
                    "task_id": str(task.get("id", "")),
                    "task_name": str(task.get("name", "")),
                    "message": message,
                },
            )

        try:
            count = execute_due_schedule_tasks(on_task_error=_report_task_error)
            if count:
                self.emitter.emit(
                    "status",
                    payload={"stage": "schedule_sent", "count": count},
                )
        except Exception as exc:
            self.emitter.emit(
                "error",
                ok=False,
                payload={"stage": "schedule_execute", "message": format_runtime_error(exc)},
            )

    def _mark_browser_closed(self, source: str) -> None:
        self.stop_requested = True
        if self._browser_closed_emitted:
            return
        self._browser_closed_emitted = True
        self.emitter.emit("status", payload={"stage": "browser_closed", "source": source})

    def _on_response(self, response: Response) -> None:
        url = response.url
        if "/cgi-bin/mmwebwx-bin/webwxsync" not in url:
            return
        try:
            payload = parse_json_body(
                response.body(),
                content_type=response.headers.get("content-type", ""),
            )
        except Exception:
            return
        now = time.monotonic()
        for message in extract_sync_messages(payload):
            if message.message_id in self.seen_message_ids:
                continue
            self.seen_message_ids.add(message.message_id)
            if message.create_time and message.create_time < self.cutoff_create_time:
                continue
            if self.outbound_tracker.matches(message.text, now=now):
                continue
            self.message_queue.append(message)

    def _send_text(self, page: Page, text: str) -> None:
        for chunk in chunk_text_with_prefix(text, prefix=AGENT_REPLY_PREFIX):
            input_locator = self._resolve_textbox(page)
            input_locator.click()
            input_locator.fill(chunk)
            self._click_send_button(page)
            self.outbound_tracker.remember(chunk)
            self.emitter.emit(
                "message_out",
                payload={"kind": "text", "text": chunk},
            )
            page.wait_for_timeout(120)

    def _send_screenshot(self, page: Page) -> None:
        SCREENSHOT_ROOT.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(
            prefix="wala-shot-",
            suffix=".png",
            dir=str(SCREENSHOT_ROOT),
        )
        os.close(fd)
        temp_path = Path(raw_path)
        try:
            subprocess.run(["screencapture", "-x", str(temp_path)], check=True)
            self._upload_local_file(
                page,
                temp_path,
                kind="image",
                display_name=temp_path.name,
                source=str(temp_path),
            )
        finally:
            temp_path.unlink(missing_ok=True)

    def _scan_attachments_if_due(self, page: Page, *, force: bool = False) -> list[StoredAttachment]:
        now = time.monotonic()
        if not force and now < self._next_attachment_scan_at:
            return []
        self._next_attachment_scan_at = now + DEFAULT_ATTACHMENT_SCAN_INTERVAL_S
        try:
            return self._collect_new_attachments(page)
        except Exception as exc:
            self.emitter.emit(
                "error",
                ok=False,
                payload={"stage": "attachment_scan", "message": format_runtime_error(exc)},
            )
            return []

    def _collect_new_attachments(self, page: Page) -> list[StoredAttachment]:
        items = self._snapshot_recent_message_items(page)
        if not items:
            return []

        pending_keys = {
            item.key
            for item in self.pending_attachment_store.load()
            if item.local_path
        }
        attachment_items: list[dict[str, Any]] = []
        for item in items:
            kind = self._classify_attachment_item(item)
            if not kind:
                continue
            key = build_attachment_key(
                kind=kind,
                title=str(item.get("file_title") or ""),
                text=str(item.get("text") or ""),
                source_hint=str(item.get("image_src") or item.get("link_href") or ""),
                dataset=item.get("dataset") if isinstance(item.get("dataset"), dict) else {},
            )
            item["attachment_kind"] = kind
            item["attachment_key"] = key
            attachment_items.append(item)

        if not self._dom_attachment_scan_warmed:
            for item in attachment_items:
                self._seen_dom_attachment_keys.add(str(item["attachment_key"]))
            self._dom_attachment_scan_warmed = True
            self.emitter.emit(
                "status",
                payload={
                    "stage": "attachment_scan_warmed",
                    "visible_attachment_count": len(attachment_items),
                },
            )
            return []

        new_attachments: list[StoredAttachment] = []
        for item in attachment_items:
            key = str(item["attachment_key"])
            kind = str(item["attachment_kind"])
            if key in self._seen_dom_attachment_keys:
                continue
            if self._matches_recent_outbound_attachment(item, kind):
                self._seen_dom_attachment_keys.add(key)
                continue
            if self._is_self_message_item(item):
                self._seen_dom_attachment_keys.add(key)
                continue
            if key in pending_keys:
                self._seen_dom_attachment_keys.add(key)
                continue
            if kind == "image":
                attachment = self._capture_image_attachment(page, item)
            else:
                attachment = self._capture_file_attachment(page, item)
            if not attachment.local_path:
                self.emitter.emit(
                    "error",
                    ok=False,
                    payload={
                        "stage": "attachment_capture",
                        "attachment_kind": kind,
                        "attachment_key": key,
                        "filename": attachment.filename,
                        "message": attachment.error or "附件抓取失败",
                    },
                )
                continue
            self._seen_dom_attachment_keys.add(key)
            pending_keys.add(key)
            new_attachments.append(attachment)
            self.emitter.emit(
                "message_in",
                payload={
                    "kind": "attachment",
                    "attachment_kind": attachment.kind,
                    "attachment_key": attachment.key,
                    "filename": attachment.filename,
                    "local_path": attachment.local_path,
                    "source_url": attachment.source_url,
                    "error": attachment.error or "",
                },
            )

        if new_attachments:
            self.pending_attachment_store.append(new_attachments)
        return new_attachments

    def _snapshot_recent_message_items(self, page: Page) -> list[dict[str, Any]]:
        body = page.locator(CHAT_BODY_SELECTOR).first
        if body.count() == 0:
            return []
        snapshot = body.evaluate(
            """(node, maxItems) => {
                const allItems = Array.from(node.querySelectorAll('.msg-item'));
                const start = Math.max(0, allItems.length - maxItems);
                return allItems.slice(start).map((item, offset) => {
                    const dataset = {};
                    for (const [key, value] of Object.entries(item.dataset || {})) {
                        dataset[key] = String(value ?? '');
                    }
                    const fileTitle = item.querySelector('.msg-file__title, .msg-appmsg__title');
                    const fileDesc = item.querySelector('.msg-file__desc, .msg-appmsg__desc');
                    const imageNode = item.querySelector('.msg-image img, img.msg-image');
                    const anchor = item.querySelector('.msg-file a[href], .msg-appmsg a[href], a[href]');
                    return {
                        dom_index: start + offset,
                        class_name: typeof item.className === 'string' ? item.className : '',
                        dataset,
                        text: (item.innerText || '').trim(),
                        file_title: fileTitle ? (fileTitle.textContent || '').trim() : '',
                        file_desc: fileDesc ? (fileDesc.textContent || '').trim() : '',
                        image_src: imageNode ? (imageNode.currentSrc || imageNode.getAttribute('src') || '') : '',
                        link_href: anchor ? (anchor.getAttribute('href') || anchor.href || '') : '',
                        has_file: Boolean(item.querySelector('.msg-file, .msg-appmsg')),
                        has_image: Boolean(imageNode),
                    };
                });
            }""",
            ATTACHMENT_SCAN_WINDOW,
        )
        return snapshot if isinstance(snapshot, list) else []

    def _classify_attachment_item(self, item: dict[str, Any]) -> str:
        if bool(item.get("has_image")):
            return "image"
        if bool(item.get("has_file")):
            return "file"
        return ""

    def _is_self_message_item(self, item: dict[str, Any]) -> bool:
        fields: list[str] = []
        class_name = str(item.get("class_name") or "").strip()
        if class_name:
            fields.append(class_name)
        dataset = item.get("dataset")
        if isinstance(dataset, dict):
            for key, value in dataset.items():
                fields.append(str(key))
                fields.append(str(value))
        if not fields:
            return False
        normalized = " ".join(fields).lower()
        return SELF_MESSAGE_HINT_PATTERN.search(normalized) is not None

    def _matches_recent_outbound_attachment(self, item: dict[str, Any], kind: str) -> bool:
        candidates = self._build_outbound_attachment_candidates(item)
        return self.outbound_attachment_tracker.consume_match(kind, candidates)

    def _build_outbound_attachment_candidates(self, item: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        def _add_candidate(raw: Any) -> None:
            text = str(raw or "").strip()
            if not text:
                return
            parts = [text, *(line.strip() for line in text.splitlines() if line.strip())]
            for part in parts:
                if part in seen:
                    continue
                seen.add(part)
                candidates.append(part)

        for raw in (
            item.get("file_title"),
            item.get("file_desc"),
            item.get("text"),
        ):
            _add_candidate(raw)

        for raw_url in (
            item.get("image_src"),
            item.get("link_href"),
        ):
            url = str(raw_url or "").strip()
            if not url:
                continue
            parsed = urllib.parse.urlsplit(url)
            basename = Path(urllib.parse.unquote(parsed.path)).name
            _add_candidate(basename)

        return candidates

    def _capture_image_attachment(self, page: Page, item: dict[str, Any]) -> StoredAttachment:
        key = str(item.get("attachment_key") or "")
        source_url = self._normalize_attachment_url(page, str(item.get("image_src") or item.get("link_href") or ""))
        filename = str(item.get("file_title") or item.get("file_desc") or "image").strip() or "image"
        try:
            data, content_type = self._download_resource_bytes(page, source_url)
            return store_inbound_bytes(
                key=key,
                kind="image",
                filename=filename,
                source_url=source_url,
                content_type=content_type,
                data=data,
            )
        except Exception as exc:
            return make_inbound_error_attachment(
                key=key,
                kind="image",
                filename=filename,
                source_url=source_url,
                content_type="image/*",
                error=str(exc).strip() or exc.__class__.__name__,
            )

    def _capture_file_attachment(self, page: Page, item: dict[str, Any]) -> StoredAttachment:
        key = str(item.get("attachment_key") or "")
        source_url = self._normalize_attachment_url(page, str(item.get("link_href") or ""))
        filename = str(item.get("file_title") or item.get("file_desc") or "file").strip() or "file"
        item_locator = self._resolve_message_item_locator(page, item)
        if item_locator is None:
            return make_inbound_error_attachment(
                key=key,
                kind="file",
                filename=filename,
                source_url=source_url,
                content_type="application/octet-stream",
                error="未定位到文件消息节点",
            )
        try:
            item_locator.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        try:
            item_locator.hover(timeout=1500)
        except Exception:
            pass

        download_button = item_locator.locator(DOWNLOAD_TRIGGER_SELECTOR).first
        if download_button.count() > 0:
            try:
                with page.expect_download(timeout=int(DEFAULT_HTTP_TIMEOUT_S * 1000)) as download_info:
                    download_button.click(timeout=2000)
                download = download_info.value
                suggested_name = download.suggested_filename or filename
                content_type = mimetypes.guess_type(suggested_name)[0] or "application/octet-stream"
                destination = build_inbound_attachment_path(
                    key=key,
                    filename=suggested_name,
                    content_type=content_type,
                    source_url=source_url,
                    kind="file",
                )
                download.save_as(str(destination))
                return StoredAttachment(
                    key=key,
                    kind="file",
                    filename=destination.name,
                    local_path=str(destination),
                    source_url=source_url,
                    content_type=content_type,
                    received_at=time.time(),
                )
            except Exception:
                pass

        try:
            data, content_type = self._download_resource_bytes(page, source_url)
            return store_inbound_bytes(
                key=key,
                kind="file",
                filename=filename,
                source_url=source_url,
                content_type=content_type,
                data=data,
            )
        except Exception as exc:
            return make_inbound_error_attachment(
                key=key,
                kind="file",
                filename=filename,
                source_url=source_url,
                content_type="application/octet-stream",
                error=str(exc).strip() or exc.__class__.__name__,
            )

    def _resolve_message_item_locator(self, page: Page, item: dict[str, Any]) -> Any | None:
        try:
            dom_index = int(item.get("dom_index"))
        except (TypeError, ValueError):
            return None
        items = page.locator(f"{CHAT_BODY_SELECTOR} {MESSAGE_ITEM_SELECTOR}")
        if dom_index < 0 or items.count() <= dom_index:
            return None
        return items.nth(dom_index)

    def _normalize_attachment_url(self, page: Page, source_url: str) -> str:
        raw = (source_url or "").strip()
        if not raw or raw.lower().startswith("javascript:"):
            return ""
        if raw.startswith("data:") or raw.startswith("blob:"):
            return raw
        if raw.startswith("//"):
            return f"https:{raw}"
        return urllib.parse.urljoin(page.url, raw)

    def _download_resource_bytes(self, page: Page, source_url: str) -> tuple[bytes, str]:
        if not source_url:
            raise RuntimeError("附件缺少可下载地址")
        if source_url.startswith("data:"):
            return self._decode_data_url(source_url)
        if source_url.startswith("blob:"):
            return self._download_blob_resource(page, source_url)

        api_request = getattr(page.context, "request", None)
        if api_request is not None:
            try:
                response = api_request.get(
                    source_url,
                    timeout=int(DEFAULT_HTTP_TIMEOUT_S * 1000),
                    headers={"Referer": page.url},
                    fail_on_status_code=False,
                )
                if response.ok:
                    content_type = response.headers.get("content-type", "application/octet-stream")
                    data = response.body()
                    if data:
                        return data, content_type
                else:
                    raise RuntimeError(f"HTTP {response.status}")
            except Exception:
                pass

        request = urllib.request.Request(
            source_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": page.url,
            },
        )
        with urllib.request.urlopen(request, timeout=DEFAULT_HTTP_TIMEOUT_S) as response:
            data = response.read()
            content_type = response.headers.get_content_type() or "application/octet-stream"
        if not data:
            raise RuntimeError("下载失败: 内容为空")
        return data, content_type

    def _decode_data_url(self, source_url: str) -> tuple[bytes, str]:
        header, separator, payload = source_url.partition(",")
        if not separator:
            raise RuntimeError("无效的 data URL")
        content_type = header[5:].split(";", 1)[0].strip() or "application/octet-stream"
        try:
            if ";base64" in header.lower():
                return base64.b64decode(payload), content_type
            return urllib.parse.unquote_to_bytes(payload), content_type
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError(f"解析 data URL 失败: {exc}") from exc

    def _download_blob_resource(self, page: Page, source_url: str) -> tuple[bytes, str]:
        payload = page.evaluate(
            """async (url) => {
                const response = await fetch(url);
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const contentType = response.headers.get('content-type') || 'application/octet-stream';
                const buffer = await response.arrayBuffer();
                const bytes = new Uint8Array(buffer);
                let binary = '';
                const chunkSize = 0x8000;
                for (let index = 0; index < bytes.length; index += chunkSize) {
                    const chunk = bytes.subarray(index, index + chunkSize);
                    binary += String.fromCharCode(...chunk);
                }
                return { contentType, base64: btoa(binary) };
            }""",
            source_url,
        )
        if not isinstance(payload, dict):
            raise RuntimeError("下载 blob 资源失败")
        encoded = str(payload.get("base64") or "")
        if not encoded:
            raise RuntimeError("下载 blob 资源失败: 内容为空")
        return (
            base64.b64decode(encoded),
            str(payload.get("contentType") or "application/octet-stream"),
        )

    def _send_pending_attachment_ack_if_needed(self, page: Page) -> None:
        attachments = self.pending_attachment_store.load()
        if not attachments:
            return
        now = time.time()
        due = [
            item.key
            for item in attachments
            if not item.ack_sent and now - float(item.received_at) >= ATTACHMENT_ACK_DELAY_S
        ]
        if not due:
            return
        try:
            self._send_text(page, PENDING_ATTACHMENTS_ACK_TEXT)
            self.pending_attachment_store.mark_ack_sent(due)
            self.emitter.emit(
                "status",
                payload={"stage": "attachment_ack_sent", "count": len(due)},
            )
        except Exception as exc:
            self.emitter.emit(
                "error",
                ok=False,
                payload={"stage": "attachment_ack", "message": format_runtime_error(exc)},
            )

    def _send_claude_resources(self, page: Page, resources: list[Any]) -> None:
        for resource in resources:
            prepared: PreparedOutboundResource | None = None
            failure_display_name = getattr(resource, "display_name", "file")
            try:
                prepared = prepare_outbound_resource(resource, timeout=DEFAULT_HTTP_TIMEOUT_S)
                failure_display_name = prepared.display_name
                self._send_prepared_resource(page, prepared)
            except Exception as exc:
                failure_text = (
                    f"文件发送失败 {failure_display_name}: {str(exc).strip() or exc.__class__.__name__}"
                )
                self.emitter.emit(
                    "error",
                    ok=False,
                    payload={
                        "stage": "send_attachment",
                        "resource": getattr(resource, "source", ""),
                        "message": failure_text,
                    },
                )
                try:
                    self._send_text(page, failure_text)
                except Exception as send_exc:
                    self.emitter.emit(
                        "error",
                        ok=False,
                        payload={
                            "stage": "send_attachment_fallback",
                            "resource": getattr(resource, "source", ""),
                            "message": format_runtime_error(send_exc),
                        },
                    )
            finally:
                if prepared is not None:
                    prepared.cleanup()

    def _send_prepared_resource(self, page: Page, resource: PreparedOutboundResource) -> None:
        self._upload_local_file(
            page,
            Path(resource.local_path),
            kind=resource.kind,
            display_name=resource.display_name,
            source=resource.source,
        )

    def _upload_local_file(
        self,
        page: Page,
        file_path: Path,
        *,
        kind: str,
        display_name: str,
        source: str,
    ) -> None:
        file_locator = page.locator(FILE_INPUT_SELECTOR)
        if file_locator.count() == 0:
            raise RuntimeError("未找到文件上传控件")
        self.outbound_attachment_tracker.remember(kind, display_name)
        file_locator.first.set_input_files(str(file_path))
        page.wait_for_timeout(2500)
        self.emitter.emit(
            "message_out",
            payload={
                "kind": kind,
                "file_path": str(file_path),
                "display_name": display_name,
                "source": source,
            },
        )

    def _resolve_textbox(self, page: Page) -> Any:
        last_error: Optional[Exception] = None
        for selector in TEXTBOX_SELECTORS:
            locator = page.locator(selector).first
            try:
                if locator.count() == 0:
                    continue
                if hasattr(locator, "is_visible") and not locator.is_visible():
                    continue
                if hasattr(locator, "is_editable") and not locator.is_editable():
                    continue
                return locator
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise RuntimeError(f"定位输入框失败：{last_error}") from last_error
        raise RuntimeError("未找到消息输入框")

    def _click_send_button(self, page: Page) -> None:
        for selector in SEND_BUTTON_SELECTORS:
            locator = page.locator(selector).first
            try:
                if locator.count() == 0:
                    continue
                if hasattr(locator, "is_visible") and not locator.is_visible():
                    continue
                locator.click(timeout=2000)
                return
            except Exception:
                continue
        try:
            textbox = self._resolve_textbox(page)
            textbox.press("Enter")
            return
        except Exception as exc:
            raise RuntimeError("未找到发送按钮") from exc

    def _send_login_page_email(self, page: Page) -> None:
        try:
            if self._login_email_config is None:
                self._login_email_config = load_email_config_from_env()
            qrcode_attachment = self._build_login_qrcode_attachment(page)
            subject = f"WALA | 微信登录二维码 | {time.strftime('%Y-%m-%d %H:%M:%S')}"
            html_body = render_markdown_email_html(
                "\n".join(
                    [
                        "### 微信文件传输助手登录二维码",
                        "",
                        f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                        "- 请查看附件中的二维码，并尽快使用微信扫码登录。\n"
                        "- 每个二维码仅能扫描一次。\n"
                        "- 若二维码失效/登陆失败，一分钟后将再次发送。\n",
                    ]
                )
            )
            send_email(
                subject=subject,
                html_body=html_body,
                config=self._login_email_config,
                attachments=[qrcode_attachment],
            )
            self._login_email_sent_count += 1
            self.emitter.emit(
                "auth",
                ok=False,
                payload={
                    "state": "login_email_sent",
                    "attempt": self._login_email_sent_count,
                    "url": page.url,
                    "asset": "qrcode_image",
                    "qrcode_url": str(qrcode_attachment.get("source_url") or ""),
                    "to": list(self._login_email_config.get("to_addrs") or []),
                },
            )
        except Exception as exc:
            raise RuntimeError(
                f"登录页面邮件发送失败：{str(exc).strip() or exc.__class__.__name__}"
            ) from exc

    def _build_login_qrcode_attachment(self, page: Page) -> dict[str, object]:
        locator = page.locator(LOGIN_QRCODE_SELECTOR).first
        try:
            locator.wait_for(state="visible", timeout=5000)
            page.wait_for_function(
                """selector => {
                    const img = document.querySelector(selector);
                    return Boolean(img && img.currentSrc && img.complete && img.naturalWidth > 0);
                }""",
                arg=LOGIN_QRCODE_SELECTOR,
                timeout=5000,
            )
        except Exception as exc:
            raise RuntimeError(f"等待登录二维码加载失败：{exc}") from exc

        src = ""
        try:
            src = str(locator.evaluate("(img) => img.currentSrc || img.getAttribute('src') || ''")).strip()
        except Exception as exc:
            raise RuntimeError(f"读取登录二维码地址失败：{exc}") from exc
        if not src:
            raise RuntimeError("登录二维码地址为空")

        image_url = urllib.parse.urljoin(page.url, src)
        request = urllib.request.Request(
            image_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": page.url,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                content = response.read()
                content_type = response.headers.get_content_type() or "image/jpeg"
        except Exception as exc:
            raise RuntimeError(f"下载登录二维码失败：{exc}") from exc
        if not content:
            raise RuntimeError("登录二维码图片内容为空")

        maintype, slash, subtype = content_type.partition("/")
        if not slash:
            maintype = "image"
            subtype = "jpeg"
            content_type = "image/jpeg"
        extension = mimetypes.guess_extension(content_type, strict=False)
        if not extension:
            extension = ".jpg" if subtype == "jpeg" else f".{subtype}"
        filename = f"wala-login-qrcode-{time.strftime('%Y%m%d-%H%M%S')}{extension}"
        return {
            "filename": filename,
            "content": content,
            "maintype": maintype,
            "subtype": subtype,
            "source_url": image_url,
        }

    def _cleanup_profile_dir(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.emitter.emit(
            "status",
            payload={
                "stage": "cleanup_complete",
                "profile_dir": str(self.profile_dir),
                "profile_name": self.profile_name,
                "preserved": True,
            },
        )


__all__ = [
    "AGENT_REPLY_PREFIX",
    "BrowserFileHelperAgent",
    "DEFAULT_LOGIN_TIMEOUT_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_PROFILE_NAME",
    "JsonEmitter",
    "OutboundAttachmentTracker",
    "OutboundTracker",
    "PlaywrightError",
    "SyncMessage",
    "chunk_text_with_prefix",
    "cleanup_stale_profiles",
    "extract_sync_messages",
    "format_runtime_error",
    "normalize_message_text",
    "parse_json_body",
    "repair_mojibake_text",
    "sync_playwright",
]
