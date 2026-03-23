#!/usr/bin/env python3
"""基于官方 openclaw-weixin HTTP API 的微信代理。"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import mimetypes
import os
import random
import re
import signal
import socket
import subprocess
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import qrcode
    from qrcode.image.svg import SvgPathImage
except ImportError:  # pragma: no cover - 依赖缺失时在运行时给出更明确错误
    qrcode = None  # type: ignore[assignment]
    SvgPathImage = None  # type: ignore[assignment]

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
except ImportError:  # pragma: no cover - 依赖缺失时在运行时给出更明确错误
    AES = None  # type: ignore[assignment]

    def pad(*_args: Any, **_kwargs: Any) -> bytes:
        raise RuntimeError("缺少依赖 pycryptodome，请先执行 `pip install -r requirements.txt`")

    def unpad(*_args: Any, **_kwargs: Any) -> bytes:
        raise RuntimeError("缺少依赖 pycryptodome，请先执行 `pip install -r requirements.txt`")

from claude_io_utlities import ROOT_DIR
from schedual_utilities import (
    execute_due_schedule_tasks,
    load_email_config_from_env,
    render_markdown_email_html,
    send_email,
    sync_and_save_schedule_state,
)
from wechat_media_bridge import (
    DEFAULT_HTTP_TIMEOUT_S,
    PendingAttachmentStore,
    PreparedOutboundResource,
    StoredAttachment,
    build_attachment_key,
    ensure_media_dirs,
    make_inbound_error_attachment,
    prepare_outbound_resource,
    store_inbound_bytes,
)


DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_LOGIN_TIMEOUT_S = 300.0
DEFAULT_PROFILE_NAME = "default"
DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000
DEFAULT_API_TIMEOUT_S = 15.0
DEFAULT_LOGIN_EMAIL_INTERVAL_S = 60.0
DEFAULT_LOGIN_QR_REFRESH_LIMIT = 3
DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_ILINK_BOT_TYPE = "3"
TEXT_MESSAGE_TYPE = 1
IMAGE_MESSAGE_TYPE = 2
VOICE_MESSAGE_TYPE = 3
FILE_MESSAGE_TYPE = 4
VIDEO_MESSAGE_TYPE = 5
BOT_MESSAGE_TYPE = 2
MESSAGE_STATE_FINISH = 2
AGENT_REPLY_PREFIX = ""
DEFAULT_MAX_CHARS_PER_MESSAGE = 4000
PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_COMPONENT_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
SESSION_EXPIRED_ERRCODE = -14
OPENCLAW_ROOT = ROOT_DIR / "openclaw_weixin"
ACCOUNT_ROOT = OPENCLAW_ROOT / "accounts"
SYNC_STATE_ROOT = OPENCLAW_ROOT / "sync_state"
PEER_SESSION_ROOT = ROOT_DIR / "peer_sessions" / "openclaw_weixin"
PENDING_ATTACHMENT_ROOT = ROOT_DIR / "runtime" / "openclaw_weixin" / "pending_attachments"
SCREENSHOT_ROOT = ROOT_DIR / "runtime" / "openclaw_weixin" / "screenshots"
LOGIN_QR_ROOT = ROOT_DIR / "runtime" / "openclaw_weixin" / "login_qr"
CHANNEL_VERSION = "wala-openclaw-weixin/1.0"
USER_AGENT = "wala-openclaw-weixin/1.0"


@dataclass(frozen=True)
class SyncMessage:
    message_id: str
    text: str
    create_time_ms: int
    from_user_id: str
    to_user_id: str
    context_token: str
    attachments: tuple[StoredAttachment, ...]
    raw: dict[str, Any]


@dataclass
class WeixinAccount:
    profile_name: str
    token: str
    bot_account_id: str
    user_id: str
    base_url: str
    cdn_base_url: str
    saved_at: str


class JsonEmitter:
    """统一输出 NDJSON 事件。"""

    def __init__(self, session: str, chat: str = "openclaw-weixin") -> None:
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


def format_runtime_error(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "配置或系统资源不存在，请检查路径与安装状态"
    if isinstance(exc, PermissionError):
        return "权限不足，请检查系统辅助功能与文件权限"
    text = str(exc).strip()
    return f"执行失败：{text or exc.__class__.__name__}"


def chunk_text_with_prefix(
    text: str,
    max_chars_per_message: int = DEFAULT_MAX_CHARS_PER_MESSAGE,
    *,
    prefix: str = "",
) -> list[str]:
    normalized_text = "" if text is None else str(text)
    normalized_prefix = "" if prefix is None else str(prefix)
    if normalized_prefix and normalized_text.startswith(normalized_prefix):
        normalized_text = normalized_text[len(normalized_prefix) :]
    if len(normalized_prefix) + len(normalized_text) <= max_chars_per_message:
        return [normalized_prefix + normalized_text]
    if max_chars_per_message <= len(normalized_prefix) + len("[1/1] "):
        raise ValueError("max_chars_per_message is too small for chunk prefix")

    total_chunks = 1
    while True:
        chunks: list[str] = []
        cursor = 0
        chunk_index = 1
        while cursor < len(normalized_text):
            chunk_prefix = f"[{chunk_index}/{total_chunks}] "
            payload_limit = max_chars_per_message - len(normalized_prefix) - len(chunk_prefix)
            if payload_limit <= 0:
                raise ValueError("max_chars_per_message is too small for chunk payload")
            next_cursor = min(len(normalized_text), cursor + payload_limit)
            chunks.append(normalized_prefix + chunk_prefix + normalized_text[cursor:next_cursor])
            cursor = next_cursor
            chunk_index += 1
        if len(chunks) == total_chunks:
            return chunks
        total_chunks = len(chunks)


def _safe_component(value: str) -> str:
    cleaned = SAFE_COMPONENT_PATTERN.sub("_", (value or "").strip())
    return cleaned[:96] or hashlib.sha1((value or "").encode("utf-8")).hexdigest()[:16]


def _random_wechat_uin_header() -> str:
    value = random.getrandbits(32)
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _parse_aes_key(aes_key_base64: str) -> bytes:
    decoded = base64.b64decode(aes_key_base64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and re.fullmatch(rb"[0-9A-Fa-f]{32}", decoded):
        return bytes.fromhex(decoded.decode("ascii"))
    raise ValueError(f"aes_key 长度非法: {len(decoded)}")


class OpenClawWeixinAgent:
    """通过官方 openclaw-weixin API 与微信交互。"""

    def __init__(
        self,
        *,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        login_timeout_s: float = DEFAULT_LOGIN_TIMEOUT_S,
        profile_name: str = DEFAULT_PROFILE_NAME,
    ) -> None:
        normalized_profile = profile_name.strip()
        if not PROFILE_NAME_PATTERN.fullmatch(normalized_profile):
            raise ValueError(
                "profile_name 非法：仅支持字母、数字、点、下划线、连字符，且必须以字母或数字开头"
            )
        self.poll_interval_s = poll_interval_s
        self.login_timeout_s = login_timeout_s
        self.profile_name = normalized_profile
        self.runtime_id = os.urandom(16).hex()
        self.emitter = JsonEmitter(self.runtime_id)
        self.message_queue: Deque[SyncMessage] = deque()
        self.seen_message_ids: set[str] = set()
        self.stop_requested = False
        self._signal_handlers_installed = False
        self._previous_signal_handlers: dict[int, Any] = {}
        self._login_email_config: Optional[dict[str, object]] = None
        self._login_email_sent_count = 0
        self._login_required_emitted = False
        self._next_long_poll_timeout_ms = DEFAULT_LONG_POLL_TIMEOUT_MS
        self.base_url = os.environ.get("WECHAT_OPENCLAW_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        self.cdn_base_url = os.environ.get("WECHAT_OPENCLAW_CDN_BASE_URL", DEFAULT_CDN_BASE_URL).strip() or DEFAULT_CDN_BASE_URL
        self.route_tag = os.environ.get("WECHAT_OPENCLAW_ROUTE_TAG", "").strip()
        self.bot_type = os.environ.get("WECHAT_OPENCLAW_BOT_TYPE", DEFAULT_ILINK_BOT_TYPE).strip() or DEFAULT_ILINK_BOT_TYPE
        self.account_file = ACCOUNT_ROOT / f"{self.profile_name}.json"
        self.sync_state_file = SYNC_STATE_ROOT / f"{self.profile_name}.json"
        self.peer_session_root = PEER_SESSION_ROOT / self.profile_name
        self.pending_attachment_root = PENDING_ATTACHMENT_ROOT / self.profile_name
        self._authenticated_account: Optional[WeixinAccount] = None

    def bootstrap(self) -> None:
        ensure_media_dirs()
        ACCOUNT_ROOT.mkdir(parents=True, exist_ok=True)
        SYNC_STATE_ROOT.mkdir(parents=True, exist_ok=True)
        self.peer_session_root.mkdir(parents=True, exist_ok=True)
        self.pending_attachment_root.mkdir(parents=True, exist_ok=True)
        SCREENSHOT_ROOT.mkdir(parents=True, exist_ok=True)
        LOGIN_QR_ROOT.mkdir(parents=True, exist_ok=True)
        if qrcode is None:
            raise RuntimeError("缺少依赖 qrcode，请先执行 `pip install -r requirements.txt`")
        if AES is None:
            raise RuntimeError("缺少依赖 pycryptodome，请先执行 `pip install -r requirements.txt`")
        self._install_signal_handlers()
        self.emitter.emit(
            "status",
            payload={
                "stage": "startup",
                "profile_name": self.profile_name,
                "base_url": self.base_url,
                "cdn_base_url": self.cdn_base_url,
            },
        )

    def prepare_session(self) -> None:
        self._ensure_authenticated_account(force_relogin=False)
        self.emitter.emit("auth", payload={"state": "logged_in", "profile_name": self.profile_name})
        self._prepare_schedule_runtime()
        self.emitter.emit("status", payload={"stage": "background_ready", "mode": "openclaw-api"})

    def begin_listening(self) -> None:
        self.emitter.emit("status", payload={"stage": "listening"})

    def should_continue(self) -> bool:
        return not self.stop_requested

    def dequeue_message(self) -> Optional[SyncMessage]:
        if not self.message_queue:
            return None
        return self.message_queue.popleft()

    def wait_for_next_poll(self) -> bool:
        if self.stop_requested:
            return False
        try:
            self._poll_updates_once()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            self.emitter.emit(
                "error",
                ok=False,
                payload={"stage": "poll_updates", "message": format_runtime_error(exc)},
            )
            time.sleep(self.poll_interval_s)
        return not self.stop_requested

    def run_due_schedule_tasks_once(self) -> None:
        self._run_due_schedule_tasks_once()

    def resolve_temp_dir(self, message: SyncMessage) -> Path:
        return self._peer_root(message.from_user_id) / "temp"

    def resolve_uid_root(self, message: SyncMessage) -> Path:
        return self._peer_root(message.from_user_id) / "uid"

    def load_pending_attachments(self, message: SyncMessage) -> list[StoredAttachment]:
        return self._pending_store(message.from_user_id).load()

    def append_pending_attachments(self, message: SyncMessage) -> list[StoredAttachment]:
        if not message.attachments:
            return self.load_pending_attachments(message)
        return self._pending_store(message.from_user_id).append(list(message.attachments))

    def clear_pending_attachments(self, message: SyncMessage) -> None:
        self._pending_store(message.from_user_id).clear()

    def send_text(self, message: SyncMessage, text: str) -> None:
        if not text.strip():
            return
        for chunk in chunk_text_with_prefix(text, prefix=AGENT_REPLY_PREFIX):
            payload = {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": message.from_user_id,
                    "client_id": self._generate_client_id(),
                    "message_type": BOT_MESSAGE_TYPE,
                    "message_state": MESSAGE_STATE_FINISH,
                    "context_token": message.context_token or None,
                    "item_list": [
                        {
                            "type": TEXT_MESSAGE_TYPE,
                            "text_item": {"text": chunk},
                        }
                    ],
                }
            }
            self._post_api_json(
                "ilink/bot/sendmessage",
                payload,
                timeout_s=DEFAULT_API_TIMEOUT_S,
            )
            self.emitter.emit(
                "message_out",
                payload={
                    "kind": "text",
                    "text": chunk,
                    "to_user_id": message.from_user_id,
                },
            )

    def send_screenshot(self, message: SyncMessage) -> None:
        SCREENSHOT_ROOT.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_path = SCREENSHOT_ROOT / f"wala-shot-{timestamp}.png"
        try:
            subprocess.run(["screencapture", "-x", str(temp_path)], check=True)
            resource = PreparedOutboundResource(
                source=str(temp_path),
                resolved_source=str(temp_path),
                local_path=str(temp_path),
                display_name=temp_path.name,
                kind="image",
                content_type="image/png",
            )
            self._send_prepared_resource(message, resource)
        finally:
            temp_path.unlink(missing_ok=True)

    def send_claude_resources(self, message: SyncMessage, resources: list[Any]) -> None:
        for resource in resources:
            prepared = prepare_outbound_resource(resource, timeout=DEFAULT_HTTP_TIMEOUT_S)
            try:
                self._send_prepared_resource(message, prepared)
            finally:
                prepared.cleanup()

    def should_suppress_exception(self, exc: Exception) -> bool:
        return self.stop_requested and isinstance(exc, KeyboardInterrupt)

    def shutdown(self) -> None:
        self._restore_signal_handlers()

    def _peer_root(self, peer_user_id: str) -> Path:
        return self.peer_session_root / _safe_component(peer_user_id)

    def _pending_store(self, peer_user_id: str) -> PendingAttachmentStore:
        path = self.pending_attachment_root / f"{_safe_component(peer_user_id)}.json"
        return PendingAttachmentStore(path)

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

    def _load_account(self) -> Optional[WeixinAccount]:
        if not self.account_file.exists():
            return None
        try:
            payload = json.loads(self.account_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        token = str(payload.get("token") or "").strip()
        bot_account_id = str(payload.get("bot_account_id") or "").strip()
        if not token or not bot_account_id:
            return None
        return WeixinAccount(
            profile_name=self.profile_name,
            token=token,
            bot_account_id=bot_account_id,
            user_id=str(payload.get("user_id") or "").strip(),
            base_url=str(payload.get("base_url") or self.base_url).strip() or self.base_url,
            cdn_base_url=str(payload.get("cdn_base_url") or self.cdn_base_url).strip() or self.cdn_base_url,
            saved_at=str(payload.get("saved_at") or ""),
        )

    def _save_account(
        self,
        *,
        token: str,
        bot_account_id: str,
        user_id: str,
        base_url: str,
        cdn_base_url: str,
    ) -> WeixinAccount:
        account = WeixinAccount(
            profile_name=self.profile_name,
            token=token,
            bot_account_id=bot_account_id,
            user_id=user_id,
            base_url=base_url,
            cdn_base_url=cdn_base_url,
            saved_at=datetime.now().isoformat(timespec="seconds"),
        )
        self.account_file.parent.mkdir(parents=True, exist_ok=True)
        self.account_file.write_text(
            json.dumps(
                {
                    "token": account.token,
                    "bot_account_id": account.bot_account_id,
                    "user_id": account.user_id,
                    "base_url": account.base_url,
                    "cdn_base_url": account.cdn_base_url,
                    "saved_at": account.saved_at,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            os.chmod(self.account_file, 0o600)
        except OSError:
            pass
        self._authenticated_account = account
        self.base_url = account.base_url
        self.cdn_base_url = account.cdn_base_url
        return account

    def _clear_account(self) -> None:
        self.account_file.unlink(missing_ok=True)
        self._authenticated_account = None

    def _load_sync_state(self) -> str:
        if not self.sync_state_file.exists():
            return ""
        try:
            payload = json.loads(self.sync_state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return ""
        return str(payload.get("get_updates_buf") or "")

    def _save_sync_state(self, get_updates_buf: str) -> None:
        self.sync_state_file.parent.mkdir(parents=True, exist_ok=True)
        self.sync_state_file.write_text(
            json.dumps({"get_updates_buf": get_updates_buf}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _ensure_authenticated_account(self, *, force_relogin: bool) -> WeixinAccount:
        if not force_relogin:
            cached = self._authenticated_account or self._load_account()
            if cached is not None:
                self._authenticated_account = cached
                self.base_url = cached.base_url
                self.cdn_base_url = cached.cdn_base_url
                return cached

        if not self._login_required_emitted:
            self._login_required_emitted = True
            self.emitter.emit(
                "auth",
                ok=False,
                payload={"state": "login_required", "delivery": "email_or_terminal"},
            )

        deadline = time.monotonic() + self.login_timeout_s
        refresh_count = 0
        while time.monotonic() < deadline and not self.stop_requested:
            qr_payload = self._fetch_qr_code()
            refresh_count += 1
            self._deliver_login_qr(qr_payload["qrcode_img_content"], attempt=refresh_count)
            status = self._wait_for_qr_confirmation(
                qrcode=qr_payload["qrcode"],
                deadline=deadline,
            )
            if status.get("status") == "confirmed":
                token = str(status.get("bot_token") or "").strip()
                bot_account_id = str(status.get("ilink_bot_id") or "").strip()
                if not token or not bot_account_id:
                    raise RuntimeError("登录已确认，但服务端未返回完整凭证")
                return self._save_account(
                    token=token,
                    bot_account_id=bot_account_id,
                    user_id=str(status.get("ilink_user_id") or "").strip(),
                    base_url=str(status.get("baseurl") or self.base_url).strip() or self.base_url,
                    cdn_base_url=self.cdn_base_url,
                )
            if status.get("status") != "expired":
                raise RuntimeError(str(status.get("error") or "登录失败"))
            if refresh_count >= DEFAULT_LOGIN_QR_REFRESH_LIMIT:
                break
        raise TimeoutError("登录超时，请重新运行并完成扫码确认")

    def _fetch_qr_code(self) -> dict[str, str]:
        query = urllib.parse.urlencode({"bot_type": self.bot_type})
        response = self._request_json(
            f"ilink/bot/get_bot_qrcode?{query}",
            method="GET",
            timeout_s=DEFAULT_API_TIMEOUT_S,
            include_auth=False,
        )
        qrcode_token = str(response.get("qrcode") or "").strip()
        qrcode_img_content = str(response.get("qrcode_img_content") or "").strip()
        if not qrcode_token or not qrcode_img_content:
            raise RuntimeError("二维码接口未返回有效内容")
        return {"qrcode": qrcode_token, "qrcode_img_content": qrcode_img_content}

    def _wait_for_qr_confirmation(self, *, qrcode: str, deadline: float) -> dict[str, str]:
        while time.monotonic() < deadline and not self.stop_requested:
            query = urllib.parse.urlencode({"qrcode": qrcode})
            try:
                response = self._request_json(
                    f"ilink/bot/get_qrcode_status?{query}",
                    method="GET",
                    timeout_s=(DEFAULT_LONG_POLL_TIMEOUT_MS / 1000.0) + 5.0,
                    include_auth=False,
                    extra_headers={"iLink-App-ClientVersion": "1"},
                )
            except TimeoutError:
                continue
            status = str(response.get("status") or "").strip()
            if status in {"wait", "scaned"}:
                time.sleep(1.0)
                continue
            if status == "confirmed":
                return {
                    "status": status,
                    "bot_token": str(response.get("bot_token") or "").strip(),
                    "ilink_bot_id": str(response.get("ilink_bot_id") or "").strip(),
                    "baseurl": str(response.get("baseurl") or "").strip(),
                    "ilink_user_id": str(response.get("ilink_user_id") or "").strip(),
                }
            if status == "expired":
                return {"status": "expired"}
            return {"status": status or "error", "error": json.dumps(response, ensure_ascii=False)}
        if self.stop_requested:
            raise KeyboardInterrupt("stopped")
        return {"status": "expired"}

    def _deliver_login_qr(self, qr_content: str, *, attempt: int) -> None:
        delivered = False
        try:
            if self._login_email_config is None:
                self._login_email_config = load_email_config_from_env()
            attachment = self._build_login_qr_attachment(qr_content)
            subject = f"WALA | 微信登录二维码 | {time.strftime('%Y-%m-%d %H:%M:%S')}"
            html_body = render_markdown_email_html(
                "\n".join(
                    [
                        "### 微信登录二维码",
                        "",
                        f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                        "- 请查看附件中的二维码并尽快扫码确认。",
                        "- 若二维码失效，程序会自动刷新并重发。",
                    ]
                )
            )
            send_email(
                subject=subject,
                html_body=html_body,
                config=self._login_email_config,
                attachments=[attachment],
            )
            self._login_email_sent_count += 1
            delivered = True
            self.emitter.emit(
                "auth",
                ok=False,
                payload={
                    "state": "login_email_sent",
                    "attempt": self._login_email_sent_count,
                    "asset": "qrcode_svg",
                    "to": list(self._login_email_config.get("to_addrs") or []),
                },
            )
        except Exception as exc:
            self.emitter.emit(
                "error",
                ok=False,
                payload={"stage": "login_qr_email", "message": format_runtime_error(exc)},
            )
        if delivered:
            return
        terminal_qr = self._render_terminal_qr(qr_content)
        print(terminal_qr, flush=True)
        self.emitter.emit(
            "auth",
            ok=False,
            payload={"state": "login_qr_terminal_ready", "attempt": attempt, "delivery": "terminal"},
        )

    def _build_login_qr_attachment(self, qr_content: str) -> dict[str, object]:
        svg_bytes = self._build_qr_svg(qr_content)
        return {
            "filename": f"wala-wechat-login-{int(time.time())}.svg",
            "content": svg_bytes,
            "maintype": "image",
            "subtype": "svg+xml",
        }

    def _build_qr_svg(self, qr_content: str) -> bytes:
        qr = qrcode.QRCode(border=2, box_size=10)
        qr.add_data(qr_content)
        qr.make(fit=True)
        image = qr.make_image(image_factory=SvgPathImage)
        buffer = io.BytesIO()
        image.save(buffer)
        return buffer.getvalue()

    def _render_terminal_qr(self, qr_content: str) -> str:
        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_content)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        lines = []
        for row in matrix:
            lines.append("".join("██" if cell else "  " for cell in row))
        return "\n".join(lines)

    def _request_json(
        self,
        endpoint: str,
        *,
        method: str,
        body: Optional[dict[str, Any]] = None,
        timeout_s: float,
        include_auth: bool,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        url = urllib.parse.urljoin(self.base_url.rstrip("/") + "/", endpoint)
        payload_bytes = b""
        headers = {"User-Agent": USER_AGENT}
        if body is not None:
            payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload_bytes))
        if include_auth:
            account = self._authenticated_account or self._load_account()
            if account is None:
                raise RuntimeError("微信账号尚未登录")
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["Authorization"] = f"Bearer {account.token}"
            headers["X-WECHAT-UIN"] = _random_wechat_uin_header()
        if self.route_tag:
            headers["SKRouteTag"] = self.route_tag
        if extra_headers:
            headers.update(extra_headers)
        request = Request(url, data=payload_bytes or None, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=timeout_s) as response:
                raw_body = response.read()
        except HTTPError as exc:
            if include_auth and exc.code in {401, 403}:
                raise RuntimeError("session expired") from exc
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body_text}") from exc
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise TimeoutError("请求超时") from exc
            raise RuntimeError(f"网络请求失败: {reason}") from exc
        text = raw_body.decode("utf-8", errors="replace")
        if not text.strip():
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"响应 JSON 解析失败: {text[:200]}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("响应格式非法")
        return payload

    def _post_api_json(self, endpoint: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        body = {
            **payload,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        response = self._request_json(
            endpoint,
            method="POST",
            body=body,
            timeout_s=timeout_s,
            include_auth=True,
        )
        errcode = response.get("errcode")
        ret = response.get("ret")
        if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
            raise RuntimeError("session expired")
        if (ret not in (None, 0)) or (errcode not in (None, 0)):
            raise RuntimeError(
                f"API 调用失败 ret={ret!r} errcode={errcode!r} errmsg={response.get('errmsg')!r}"
            )
        return response

    def _poll_updates_once(self) -> None:
        account = self._ensure_authenticated_account(force_relogin=False)
        self.base_url = account.base_url
        self.cdn_base_url = account.cdn_base_url
        get_updates_buf = self._load_sync_state()
        try:
            response = self._post_api_json(
                "ilink/bot/getupdates",
                {
                    "get_updates_buf": get_updates_buf,
                },
                timeout_s=(self._next_long_poll_timeout_ms / 1000.0) + 5.0,
            )
        except RuntimeError as exc:
            if str(exc).strip() == "session expired":
                self.emitter.emit(
                    "auth",
                    ok=False,
                    payload={"state": "session_expired", "profile_name": self.profile_name},
                )
                self._clear_account()
                self.sync_state_file.unlink(missing_ok=True)
                self._ensure_authenticated_account(force_relogin=True)
                return
            raise

        next_timeout_ms = int(response.get("longpolling_timeout_ms") or self._next_long_poll_timeout_ms)
        if next_timeout_ms > 0:
            self._next_long_poll_timeout_ms = next_timeout_ms
        new_buf = str(response.get("get_updates_buf") or "")
        if new_buf:
            self._save_sync_state(new_buf)
        for raw_message in response.get("msgs") or []:
            sync_message = self._convert_raw_message(raw_message)
            if sync_message is None:
                continue
            if sync_message.message_id in self.seen_message_ids:
                continue
            self.seen_message_ids.add(sync_message.message_id)
            self.message_queue.append(sync_message)

    def _convert_raw_message(self, raw_message: Any) -> Optional[SyncMessage]:
        if not isinstance(raw_message, dict):
            return None
        try:
            message_type = int(raw_message.get("message_type") or 0)
        except (TypeError, ValueError):
            message_type = 0
        if message_type == BOT_MESSAGE_TYPE:
            return None
        from_user_id = str(raw_message.get("from_user_id") or "").strip()
        to_user_id = str(raw_message.get("to_user_id") or "").strip()
        context_token = str(raw_message.get("context_token") or "").strip()
        item_list = raw_message.get("item_list") or []
        if not from_user_id:
            return None
        text = self._extract_message_text(item_list)
        attachments = self._extract_message_attachments(raw_message, item_list)
        if not text and not attachments:
            return None
        create_time_ms = int(raw_message.get("create_time_ms") or int(time.time() * 1000))
        message_id = str(raw_message.get("message_id") or raw_message.get("seq") or os.urandom(8).hex())
        return SyncMessage(
            message_id=message_id,
            text=text,
            create_time_ms=create_time_ms,
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            context_token=context_token,
            attachments=tuple(attachments),
            raw=raw_message,
        )

    def _extract_message_text(self, item_list: list[Any]) -> str:
        for item in item_list:
            if not isinstance(item, dict):
                continue
            try:
                item_type = int(item.get("type") or 0)
            except (TypeError, ValueError):
                continue
            if item_type == TEXT_MESSAGE_TYPE:
                text = str(((item.get("text_item") or {}) if isinstance(item.get("text_item"), dict) else {}).get("text") or "")
                return html.unescape(text).strip()
            if item_type == VOICE_MESSAGE_TYPE:
                voice_item = item.get("voice_item") or {}
                if isinstance(voice_item, dict) and voice_item.get("text"):
                    return str(voice_item.get("text") or "").strip()
        return ""

    def _extract_message_attachments(
        self,
        raw_message: dict[str, Any],
        item_list: list[Any],
    ) -> list[StoredAttachment]:
        attachments: list[StoredAttachment] = []
        message_id = str(raw_message.get("message_id") or raw_message.get("seq") or os.urandom(8).hex())
        for index, item in enumerate(item_list):
            if not isinstance(item, dict):
                continue
            attachment = self._download_attachment(item, message_id=message_id, index=index)
            if attachment is None:
                continue
            if not attachment.local_path:
                self.emitter.emit(
                    "error",
                    ok=False,
                    payload={
                        "stage": "attachment_capture",
                        "message_id": message_id,
                        "filename": attachment.filename,
                        "message": attachment.error or "附件抓取失败",
                    },
                )
                continue
            attachments.append(attachment)
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
        return attachments

    def _download_attachment(
        self,
        item: dict[str, Any],
        *,
        message_id: str,
        index: int,
    ) -> Optional[StoredAttachment]:
        try:
            item_type = int(item.get("type") or 0)
        except (TypeError, ValueError):
            return None

        kind = ""
        filename = ""
        content_type = "application/octet-stream"
        encrypted_query_param = ""
        aes_key_bytes: Optional[bytes] = None

        try:
            if item_type == IMAGE_MESSAGE_TYPE:
                image_item = item.get("image_item") if isinstance(item.get("image_item"), dict) else {}
                media = image_item.get("media") if isinstance(image_item.get("media"), dict) else {}
                encrypted_query_param = str(media.get("encrypt_query_param") or "").strip()
                filename = "image.png"
                content_type = "image/png"
                kind = "image"
                if image_item.get("aeskey"):
                    try:
                        aes_key_bytes = bytes.fromhex(str(image_item.get("aeskey")))
                    except ValueError:
                        aes_key_bytes = None
                elif media.get("aes_key"):
                    aes_key_bytes = _parse_aes_key(str(media.get("aes_key")))
            elif item_type == FILE_MESSAGE_TYPE:
                file_item = item.get("file_item") if isinstance(item.get("file_item"), dict) else {}
                media = file_item.get("media") if isinstance(file_item.get("media"), dict) else {}
                encrypted_query_param = str(media.get("encrypt_query_param") or "").strip()
                filename = str(file_item.get("file_name") or "file.bin").strip() or "file.bin"
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                kind = "file"
                if media.get("aes_key"):
                    aes_key_bytes = _parse_aes_key(str(media.get("aes_key")))
            elif item_type == VIDEO_MESSAGE_TYPE:
                video_item = item.get("video_item") if isinstance(item.get("video_item"), dict) else {}
                media = video_item.get("media") if isinstance(video_item.get("media"), dict) else {}
                encrypted_query_param = str(media.get("encrypt_query_param") or "").strip()
                filename = "video.mp4"
                content_type = "video/mp4"
                kind = "file"
                if media.get("aes_key"):
                    aes_key_bytes = _parse_aes_key(str(media.get("aes_key")))
            elif item_type == VOICE_MESSAGE_TYPE:
                voice_item = item.get("voice_item") if isinstance(item.get("voice_item"), dict) else {}
                media = voice_item.get("media") if isinstance(voice_item.get("media"), dict) else {}
                encrypted_query_param = str(media.get("encrypt_query_param") or "").strip()
                filename = "voice.silk"
                content_type = "audio/silk"
                kind = "file"
                if media.get("aes_key"):
                    aes_key_bytes = _parse_aes_key(str(media.get("aes_key")))
            else:
                return None
        except Exception as exc:
            error_key = hashlib.sha1(f"{message_id}:{index}".encode("utf-8")).hexdigest()
            return make_inbound_error_attachment(
                key=error_key,
                kind=kind or "file",
                filename=filename or "file",
                source_url=encrypted_query_param,
                content_type=content_type,
                error=format_runtime_error(exc),
            )

        if not encrypted_query_param:
            return None

        download_url = (
            f"{self.cdn_base_url.rstrip('/')}/download?"
            f"encrypted_query_param={urllib.parse.quote(encrypted_query_param, safe='')}"
        )
        key = build_attachment_key(
            kind=kind,
            title=filename,
            text=message_id,
            source_hint=encrypted_query_param,
            dataset={"message_id": message_id, "item_index": str(index)},
        )
        try:
            data = self._download_cdn_buffer(download_url)
            if aes_key_bytes is not None:
                data = self._decrypt_aes_ecb(data, aes_key_bytes)
            return store_inbound_bytes(
                key=key,
                kind=kind,
                filename=filename,
                source_url=download_url,
                content_type=content_type,
                data=data,
            )
        except Exception as exc:
            return make_inbound_error_attachment(
                key=key,
                kind=kind or "file",
                filename=filename or "file",
                source_url=download_url,
                content_type=content_type,
                error=format_runtime_error(exc),
            )

    def _download_cdn_buffer(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=DEFAULT_API_TIMEOUT_S) as response:
                return response.read()
        except HTTPError as exc:
            raise RuntimeError(f"CDN 下载失败 HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"CDN 下载失败: {exc.reason}") from exc

    def _decrypt_aes_ecb(self, ciphertext: bytes, key: bytes) -> bytes:
        cipher = AES.new(key, AES.MODE_ECB)
        return unpad(cipher.decrypt(ciphertext), AES.block_size)

    def _encrypt_aes_ecb(self, plaintext: bytes, key: bytes) -> bytes:
        cipher = AES.new(key, AES.MODE_ECB)
        return cipher.encrypt(pad(plaintext, AES.block_size))

    def _generate_client_id(self) -> str:
        return f"wala-openclaw-{int(time.time() * 1000)}-{os.urandom(4).hex()}"

    def _send_prepared_resource(self, message: SyncMessage, prepared: PreparedOutboundResource) -> None:
        local_path = Path(prepared.local_path)
        plaintext = local_path.read_bytes()
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        aeskey = os.urandom(16)
        ciphertext = self._encrypt_aes_ecb(plaintext, aeskey)
        filesize = len(ciphertext)
        filekey = os.urandom(16).hex()
        media_type = IMAGE_MESSAGE_TYPE if prepared.kind == "image" else VIDEO_MESSAGE_TYPE if prepared.content_type.startswith("video/") else FILE_MESSAGE_TYPE
        upload_resp = self._post_api_json(
            "ilink/bot/getuploadurl",
            {
                "filekey": filekey,
                "media_type": self._upload_media_type(prepared),
                "to_user_id": message.from_user_id,
                "rawsize": rawsize,
                "rawfilemd5": rawfilemd5,
                "filesize": filesize,
                "no_need_thumb": True,
                "aeskey": aeskey.hex(),
            },
            timeout_s=DEFAULT_API_TIMEOUT_S,
        )
        upload_param = str(upload_resp.get("upload_param") or "").strip()
        if not upload_param:
            raise RuntimeError("getuploadurl 未返回 upload_param")
        download_param = self._upload_ciphertext_to_cdn(
            ciphertext=ciphertext,
            upload_param=upload_param,
            filekey=filekey,
        )
        aes_key_header = base64.b64encode(aeskey.hex().encode("ascii")).decode("ascii")

        if media_type == IMAGE_MESSAGE_TYPE:
            item = {
                "type": IMAGE_MESSAGE_TYPE,
                "image_item": {
                    "media": {
                        "encrypt_query_param": download_param,
                        "aes_key": aes_key_header,
                        "encrypt_type": 1,
                    },
                    "mid_size": filesize,
                },
            }
        elif media_type == VIDEO_MESSAGE_TYPE:
            item = {
                "type": VIDEO_MESSAGE_TYPE,
                "video_item": {
                    "media": {
                        "encrypt_query_param": download_param,
                        "aes_key": aes_key_header,
                        "encrypt_type": 1,
                    },
                    "video_size": filesize,
                },
            }
        else:
            item = {
                "type": FILE_MESSAGE_TYPE,
                "file_item": {
                    "media": {
                        "encrypt_query_param": download_param,
                        "aes_key": aes_key_header,
                        "encrypt_type": 1,
                    },
                    "file_name": prepared.display_name,
                    "len": str(rawsize),
                    "md5": rawfilemd5,
                },
            }

        self._post_api_json(
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": message.from_user_id,
                    "client_id": self._generate_client_id(),
                    "message_type": BOT_MESSAGE_TYPE,
                    "message_state": MESSAGE_STATE_FINISH,
                    "context_token": message.context_token or None,
                    "item_list": [item],
                }
            },
            timeout_s=DEFAULT_API_TIMEOUT_S,
        )
        self.emitter.emit(
            "message_out",
            payload={
                "kind": prepared.kind,
                "file_path": prepared.local_path,
                "display_name": prepared.display_name,
                "source": prepared.source,
                "to_user_id": message.from_user_id,
            },
        )

    def _upload_media_type(self, prepared: PreparedOutboundResource) -> int:
        if prepared.kind == "image":
            return 1
        if prepared.content_type.startswith("video/"):
            return 2
        return 3

    def _upload_ciphertext_to_cdn(self, *, ciphertext: bytes, upload_param: str, filekey: str) -> str:
        upload_url = (
            f"{self.cdn_base_url.rstrip('/')}/upload?"
            f"encrypted_query_param={urllib.parse.quote(upload_param, safe='')}&"
            f"filekey={urllib.parse.quote(filekey, safe='')}"
        )
        request = Request(
            upload_url,
            data=ciphertext,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(ciphertext)),
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=DEFAULT_API_TIMEOUT_S) as response:
                download_param = response.headers.get("x-encrypted-param") or ""
        except HTTPError as exc:
            message = exc.headers.get("x-error-message") or exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"CDN 上传失败 HTTP {exc.code}: {message}") from exc
        except URLError as exc:
            raise RuntimeError(f"CDN 上传失败: {exc.reason}") from exc
        if not download_param:
            raise RuntimeError("CDN 上传成功但未返回 x-encrypted-param")
        return download_param

    def _prepare_schedule_runtime(self) -> None:
        try:
            _, _, changed = sync_and_save_schedule_state(skip_past_due=True)
            if changed:
                self.emitter.emit("status", payload={"stage": "schedule_runtime_synchronized"})
        except Exception as exc:
            self.emitter.emit(
                "error",
                ok=False,
                payload={"stage": "schedule_runtime", "message": format_runtime_error(exc)},
            )

    def _run_due_schedule_tasks_once(self) -> None:
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
                self.emitter.emit("status", payload={"stage": "schedule_sent", "count": count})
        except Exception as exc:
            self.emitter.emit(
                "error",
                ok=False,
                payload={"stage": "schedule_execute", "message": format_runtime_error(exc)},
            )


__all__ = [
    "AGENT_REPLY_PREFIX",
    "DEFAULT_LOGIN_TIMEOUT_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_PROFILE_NAME",
    "OpenClawWeixinAgent",
    "SyncMessage",
    "chunk_text_with_prefix",
    "format_runtime_error",
]
