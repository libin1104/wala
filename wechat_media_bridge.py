#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from claude_io_utlities import PROJECT_DIR, ROOT_DIR


RUNTIME_ROOT = ROOT_DIR / "runtime"
ATTACHMENT_INBOX_DIR = RUNTIME_ROOT / "inbox"
OUTBOUND_TEMP_DIR = RUNTIME_ROOT / "outbound_tmp"
PENDING_ATTACHMENTS_FILE = RUNTIME_ROOT / "pending_attachments.json"
PENDING_ATTACHMENTS_ACK_TEXT = "文件已接收，接下来想对这个文件做什么呢？"
ATTACHMENT_ACK_DELAY_S = 2.0
DEFAULT_HTTP_TIMEOUT_S = 30.0
FILE_LINE_PATTERN = re.compile(r"^\s*FILE:\s*(.+?)\s*$", re.IGNORECASE)
SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


@dataclass
class StoredAttachment:
    key: str
    kind: str
    filename: str
    local_path: str
    source_url: str
    content_type: str
    received_at: float
    ack_sent: bool = False
    error: str | None = None


@dataclass(frozen=True)
class OutboundMediaResource:
    source: str
    display_name: str


@dataclass(frozen=True)
class ParsedClaudeReply:
    text: str
    resources: list[OutboundMediaResource]


@dataclass
class PreparedOutboundResource:
    source: str
    resolved_source: str
    local_path: str
    display_name: str
    kind: str
    content_type: str
    cleanup_path: str | None = None

    def cleanup(self) -> None:
        if self.cleanup_path:
            Path(self.cleanup_path).unlink(missing_ok=True)


class PendingAttachmentStore:
    def __init__(self, file_path: Path = PENDING_ATTACHMENTS_FILE) -> None:
        self.file_path = file_path

    def load(self) -> list[StoredAttachment]:
        if not self.file_path.exists():
            return []
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        attachments: list[StoredAttachment] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                attachments.append(
                    StoredAttachment(
                        key=str(item["key"]),
                        kind=str(item["kind"]),
                        filename=str(item["filename"]),
                        local_path=str(item.get("local_path") or ""),
                        source_url=str(item.get("source_url") or ""),
                        content_type=str(item.get("content_type") or "application/octet-stream"),
                        received_at=float(item.get("received_at") or time.time()),
                        ack_sent=bool(item.get("ack_sent")),
                        error=str(item["error"]) if item.get("error") is not None else None,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return attachments

    def append(self, attachments: list[StoredAttachment]) -> list[StoredAttachment]:
        current = self.load()
        index_by_key = {item.key: index for index, item in enumerate(current)}
        changed = False
        for item in attachments:
            existing_index = index_by_key.get(item.key)
            if existing_index is None:
                current.append(item)
                index_by_key[item.key] = len(current) - 1
                changed = True
                continue
            existing = current[existing_index]
            # 允许用后续成功下载结果覆盖之前的失败占位记录。
            if existing.local_path or not item.local_path:
                continue
            current[existing_index] = item
            changed = True
        if changed:
            self._save(current)
        return current

    def mark_ack_sent(self, keys: list[str]) -> None:
        if not keys:
            return
        current = self.load()
        changed = False
        key_set = set(keys)
        for item in current:
            if item.key in key_set and not item.ack_sent:
                item.ack_sent = True
                changed = True
        if changed:
            self._save(current)

    def clear(self) -> None:
        if self.file_path.exists():
            self.file_path.unlink()

    def _save(self, attachments: list[StoredAttachment]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(
            json.dumps([asdict(item) for item in attachments], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def ensure_media_dirs() -> None:
    ATTACHMENT_INBOX_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOUND_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_ATTACHMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)


def build_claude_input(message_text: str, attachments: list[StoredAttachment]) -> str:
    text = message_text.strip()
    if not attachments:
        return text
    lines = [text, "", "附件路径:"]
    for item in attachments:
        lines.append(f"- {item.local_path or '[无]'}")
    return "\n".join(lines).strip()


def parse_claude_reply(reply_text: str) -> ParsedClaudeReply:
    resources: list[OutboundMediaResource] = []
    text_lines: list[str] = []
    seen_sources: set[str] = set()
    for line in reply_text.splitlines():
        match = FILE_LINE_PATTERN.match(line)
        if not match:
            text_lines.append(line)
            continue
        raw = match.group(1).strip()
        if not raw or raw in seen_sources:
            continue
        seen_sources.add(raw)
        resources.append(
            OutboundMediaResource(
                source=raw,
                display_name=_display_name_from_source(raw, fallback="file"),
            )
        )
    return ParsedClaudeReply(text="\n".join(text_lines).strip(), resources=resources)


def prepare_outbound_resource(
    resource: OutboundMediaResource,
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
) -> PreparedOutboundResource:
    ensure_media_dirs()
    raw = resource.source.strip()
    if not raw:
        raise RuntimeError("资源路径不能为空")
    if _is_http_url(raw):
        return _prepare_url_resource(raw, display_name=resource.display_name, timeout=timeout)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (PROJECT_DIR / path).resolve()
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"本地文件不存在: {path}")
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return PreparedOutboundResource(
        source=raw,
        resolved_source=str(path),
        local_path=str(path),
        display_name=path.name or resource.display_name,
        kind=_kind_from_metadata(path.name or resource.display_name, content_type),
        content_type=content_type,
    )


def build_attachment_key(
    *,
    kind: str,
    title: str,
    text: str,
    source_hint: str,
    dataset: dict[str, str],
) -> str:
    payload = json.dumps(
        {
            "kind": kind,
            "title": title.strip(),
            "text": text.strip(),
            "source_hint": source_hint.strip(),
            "dataset": {key: dataset[key] for key in sorted(dataset)},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def build_inbound_attachment_path(
    *,
    key: str,
    filename: str,
    content_type: str,
    source_url: str,
    kind: str,
) -> Path:
    ensure_media_dirs()
    ext = _guess_extension(filename=filename, content_type=content_type, source_url=source_url)
    stem = Path(filename).stem if filename else kind
    safe_stem = _safe_name(stem) or kind
    suffix = ext if ext.startswith(".") else f".{ext}" if ext else ""
    return ATTACHMENT_INBOX_DIR / f"{int(time.time())}_{key}_{safe_stem}{suffix}"


def store_inbound_bytes(
    *,
    key: str,
    kind: str,
    filename: str,
    source_url: str,
    content_type: str,
    data: bytes,
    error: str | None = None,
) -> StoredAttachment:
    destination = build_inbound_attachment_path(
        key=key,
        filename=filename,
        content_type=content_type,
        source_url=source_url,
        kind=kind,
    )
    destination.write_bytes(data)
    return StoredAttachment(
        key=key,
        kind=kind,
        filename=destination.name,
        local_path=str(destination),
        source_url=source_url,
        content_type=content_type,
        received_at=time.time(),
        error=error,
    )


def make_inbound_error_attachment(
    *,
    key: str,
    kind: str,
    filename: str,
    source_url: str,
    content_type: str,
    error: str,
) -> StoredAttachment:
    return StoredAttachment(
        key=key,
        kind=kind,
        filename=filename or kind,
        local_path="",
        source_url=source_url,
        content_type=content_type,
        received_at=time.time(),
        error=error,
    )


def _prepare_url_resource(url: str, *, display_name: str, timeout: float) -> PreparedOutboundResource:
    request = Request(url, headers={"User-Agent": "wechat_claude_web/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read()
            content_type = response.headers.get_content_type() or "application/octet-stream"
    except HTTPError as exc:
        raise RuntimeError(f"下载失败 HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"下载失败: {exc.reason}") from exc
    if not data:
        raise RuntimeError("下载失败: 内容为空")
    filename = _display_name_from_source(url, fallback=display_name or "file")
    suffix = _guess_extension(filename=filename, content_type=content_type, source_url=url)
    fd, temp_path = tempfile.mkstemp(
        prefix="wala-outbound-",
        suffix=suffix if suffix.startswith(".") else f".{suffix}" if suffix else "",
        dir=str(OUTBOUND_TEMP_DIR),
    )
    os.close(fd)
    Path(temp_path).write_bytes(data)
    Path(temp_path).chmod(0o600)
    return PreparedOutboundResource(
        source=url,
        resolved_source=url,
        local_path=temp_path,
        display_name=filename,
        kind=_kind_from_metadata(filename, content_type),
        content_type=content_type,
        cleanup_path=temp_path,
    )


def _guess_extension(*, filename: str, content_type: str, source_url: str) -> str:
    if filename and Path(filename).suffix:
        return Path(filename).suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed:
            return guessed
    if source_url:
        suffix = Path(urlparse(source_url).path).suffix
        if suffix:
            return suffix
    return ""


def _display_name_from_source(source: str, *, fallback: str) -> str:
    if _is_http_url(source):
        path = urlparse(source).path
        name = Path(path).name
        return name or fallback
    return Path(source).expanduser().name or fallback


def _kind_from_metadata(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if content_type.lower().startswith("image/") or suffix in IMAGE_EXTENSIONS:
        return "image"
    return "file"


def _is_http_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def _safe_name(value: str) -> str:
    return SAFE_NAME_PATTERN.sub("_", value.strip())


__all__ = [
    "ATTACHMENT_ACK_DELAY_S",
    "ATTACHMENT_INBOX_DIR",
    "DEFAULT_HTTP_TIMEOUT_S",
    "PENDING_ATTACHMENTS_ACK_TEXT",
    "ParsedClaudeReply",
    "PendingAttachmentStore",
    "PreparedOutboundResource",
    "StoredAttachment",
    "build_attachment_key",
    "build_claude_input",
    "build_inbound_attachment_path",
    "ensure_media_dirs",
    "make_inbound_error_attachment",
    "parse_claude_reply",
    "prepare_outbound_resource",
    "store_inbound_bytes",
]
