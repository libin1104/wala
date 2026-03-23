#!/usr/bin/env python3
"""Utilities for scheduled email task management, rendering, and execution."""

from __future__ import annotations

import json
import os
import re
import smtplib
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Optional

import bleach
import markdown as markdown_lib

from claude_io_utlities import (
    ROOT_DIR,
    TEMP_DIR,
    _run_claude_prompt,
    is_known_claude_error_output,
    load_recent_turns,
)


SCHEDULE_TASKS_FILE = ROOT_DIR / "schedule_tasks.json"
SCHEDULE_STATE_FILE = ROOT_DIR / "schedule_state.json"
MAX_TASK_CONTEXT_TURNS = 10
TIME_OF_DAY_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
MARKDOWN_WRAPPER_PATTERNS = (
    re.compile(r"^\s*(下面是|以下是|这是|邮件正文如下|你可以直接发送)", re.MULTILINE),
    re.compile(r"^\s*(这段内容|这封邮件|这份提醒).*(可以直接发送|供你参考)", re.MULTILINE),
    re.compile(r"^\s*---+\s*$", re.MULTILINE),
)

DEFAULT_POLL_INTERVAL_S = 30.0
DEFAULT_SMTP_PORT = 587
PROJECT_DOTENV_PATH = Path(__file__).resolve().parent / ".env"
MARKDOWN_EXTENSIONS = [
    "extra",
    "fenced_code",
    "sane_lists",
    "tables",
    "nl2br",
]
ALLOWED_TAGS = sorted(
    set(bleach.sanitizer.ALLOWED_TAGS).union(
        {
            "p",
            "br",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "hr",
            "pre",
            "code",
            "blockquote",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
        }
    )
)
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
    "th": ["colspan", "rowspan", "align"],
    "td": ["colspan", "rowspan", "align"],
}
ALLOWED_PROTOCOLS = set(bleach.sanitizer.ALLOWED_PROTOCOLS).union({"mailto"})
TAG_INLINE_STYLES = {
    "h1": "margin:0 0 16px;font-size:24px;line-height:1.3;",
    "h2": "margin:24px 0 12px;font-size:20px;line-height:1.4;",
    "h3": "margin:20px 0 10px;font-size:16px;line-height:1.4;",
    "h4": "margin:18px 0 8px;font-size:14px;line-height:1.4;",
    "h5": "margin:16px 0 8px;font-size:13px;line-height:1.4;",
    "h6": "margin:16px 0 8px;font-size:12px;line-height:1.4;",
    "p": "margin:0 0 14px;line-height:1.75;",
    "ul": "margin:0 0 14px 20px;padding:0;line-height:1.75;",
    "ol": "margin:0 0 14px 20px;padding:0;line-height:1.75;",
    "li": "margin:0 0 6px;",
    "blockquote": (
        "margin:16px 0;padding:0 0 0 12px;border-left:3px solid #d0d7de;"
        "color:#57606a;"
    ),
    "pre": (
        "margin:0 0 16px;padding:12px;background:#f6f8fa;border:1px solid #d0d7de;"
        "border-radius:6px;overflow-x:auto;font-size:12px;line-height:1.6;"
    ),
    "code": (
        "font-family:SFMono-Regular,Consolas,Monaco,'Liberation Mono',monospace;"
        "font-size:12px;background:#f6f8fa;border-radius:4px;padding:2px 4px;"
    ),
    "table": (
        "width:100%;border-collapse:collapse;margin:0 0 16px;font-size:13px;"
        "line-height:1.6;"
    ),
    "th": (
        "padding:8px 10px;border:1px solid #d0d7de;background:#f6f8fa;"
        "text-align:left;vertical-align:top;"
    ),
    "td": (
        "padding:8px 10px;border:1px solid #d0d7de;text-align:left;"
        "vertical-align:top;"
    ),
    "hr": "border:none;border-top:1px solid #d8dee4;margin:20px 0;",
}
STYLE_TAG_PATTERN = re.compile(
    r"<(?P<tag>"
    + "|".join(sorted(TAG_INLINE_STYLES.keys(), key=len, reverse=True))
    + r")(?P<attrs>\s[^>]*)?>",
    re.IGNORECASE,
)
DOTENV_LINE_PATTERN = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)\s*$"
)


def get_recent_temp_turns(max_turns: int = MAX_TASK_CONTEXT_TURNS) -> list[str]:
    """读取 temp 最近若干轮对话。"""
    return load_recent_turns(TEMP_DIR / "memory.md", max_turns=max_turns)


def format_recent_temp_turns(max_turns: int = MAX_TASK_CONTEXT_TURNS) -> str:
    """格式化 temp 最近若干轮对话，供 skill 读取。"""
    turns = get_recent_temp_turns(max_turns=max_turns)
    if not turns:
        return "(无最近 temp 上下文)"
    return "\n\n".join(turns)


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    """从 Claude 输出中提取 JSON 对象。"""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Claude 未返回合法 JSON")
    candidate = text[start : end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Claude 返回的 JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Claude 返回的任务定义不是 JSON 对象")
    return data


def _parse_daily_times(raw_value: Any) -> list[str]:
    """校验并规范每日触发时间。"""
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise ValueError("daily_times 必须是字符串数组")
    normalized: list[str] = []
    for item in raw_value:
        if not isinstance(item, str):
            raise ValueError("daily_times 必须只包含字符串")
        value = item.strip()
        if not TIME_OF_DAY_PATTERN.fullmatch(value):
            raise ValueError(f"daily_times 存在非法时间: {value!r}")
        normalized.append(value)
    return sorted(set(normalized))


def _parse_run_at(raw_value: Any) -> Optional[str]:
    """校验并规范一次性绝对触发时间。"""
    if raw_value in (None, ""):
        return None
    if not isinstance(raw_value, str):
        raise ValueError("run_at 必须是 ISO 时间字符串或 null")
    value = raw_value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"run_at 非法: {value!r}") from exc
    if parsed.tzinfo is not None:
        raise ValueError("run_at 必须是不带时区偏移的本地 ISO 时间")
    return parsed.replace(microsecond=0).isoformat(timespec="seconds")


def _normalize_task_id(raw_value: Any) -> str:
    """规范任务 ID，同时兼容旧任务中的自定义字符串 ID。"""
    value = "" if raw_value is None else str(raw_value).strip()
    if not value:
        return str(uuid.uuid4())
    return value


def normalize_schedule_task(
    raw_task: dict[str, Any],
    *,
    task_id: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """把 Claude 生成的原始对象规整成任务定义。"""
    if not isinstance(raw_task, dict):
        raise ValueError("任务定义必须是 JSON 对象")
    name = str(raw_task.get("name", "")).strip()
    task_summary = str(raw_task.get("task_summary", "")).strip()
    prompt_template = str(raw_task.get("prompt_template", "")).strip()
    if not name:
        raise ValueError("任务定义缺少 name")
    if not task_summary:
        raise ValueError("任务定义缺少 task_summary")
    if not prompt_template:
        raise ValueError("任务定义缺少 prompt_template")

    interval_raw = raw_task.get("interval_minutes")
    interval_minutes: Optional[int]
    if interval_raw in (None, ""):
        interval_minutes = None
    else:
        try:
            interval_minutes = int(interval_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("interval_minutes 必须是整数或 null") from exc
        if interval_minutes < 1:
            raise ValueError("interval_minutes 必须 >= 1")

    daily_times = _parse_daily_times(raw_task.get("daily_times"))
    run_at = _parse_run_at(raw_task.get("run_at"))
    if interval_minutes is None and not daily_times and run_at is None:
        raise ValueError("interval_minutes、daily_times 和 run_at 不能同时为空")

    enabled_raw = raw_task.get("enabled", True)
    if not isinstance(enabled_raw, bool):
        raise ValueError("enabled 必须是布尔值")

    resolved_task_id = (
        task_id.strip() if task_id is not None else _normalize_task_id(raw_task.get("id"))
    )
    if not resolved_task_id:
        raise ValueError("任务定义缺少 id")

    created_at_dt = (created_at if created_at is not None else datetime.now()).replace(
        microsecond=0
    )
    if run_at is not None and datetime.fromisoformat(run_at) <= created_at_dt:
        raise ValueError("run_at 必须晚于当前时间")

    return {
        "id": resolved_task_id,
        "name": name,
        "enabled": enabled_raw,
        "task_summary": task_summary,
        "prompt_template": prompt_template,
        "interval_minutes": interval_minutes,
        "daily_times": daily_times,
        "run_at": run_at,
        "created_at": created_at_dt.isoformat(timespec="seconds"),
    }


def validate_schedule_task(task: dict[str, Any]) -> dict[str, Any]:
    """校验任务文件中的任务对象。"""
    if not isinstance(task, dict):
        raise ValueError("任务项必须是对象")
    task_id = str(task.get("id", "")).strip()
    name = str(task.get("name", "")).strip()
    summary = str(task.get("task_summary", "")).strip()
    prompt_template = str(task.get("prompt_template", "")).strip()
    created_at = str(task.get("created_at", "")).strip()
    if not task_id:
        raise ValueError("任务缺少 id")
    if not name:
        raise ValueError(f"任务 {task_id} 缺少 name")
    if not summary:
        raise ValueError(f"任务 {task_id} 缺少 task_summary")
    if not prompt_template:
        raise ValueError(f"任务 {task_id} 缺少 prompt_template")
    if not created_at:
        raise ValueError(f"任务 {task_id} 缺少 created_at")
    try:
        datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise ValueError(f"任务 {task_id} 的 created_at 非法: {created_at!r}") from exc

    interval_raw = task.get("interval_minutes")
    interval_minutes: Optional[int]
    if interval_raw in (None, ""):
        interval_minutes = None
    else:
        try:
            interval_minutes = int(interval_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"任务 {task_id} 的 interval_minutes 非法") from exc
        if interval_minutes < 1:
            raise ValueError(f"任务 {task_id} 的 interval_minutes 必须 >= 1")

    enabled_raw = task.get("enabled", True)
    if not isinstance(enabled_raw, bool):
        raise ValueError(f"任务 {task_id} 的 enabled 必须是布尔值")
    daily_times = _parse_daily_times(task.get("daily_times"))
    run_at = _parse_run_at(task.get("run_at"))
    if interval_minutes is None and not daily_times and run_at is None:
        raise ValueError(f"任务 {task_id} 缺少调度规则")

    return {
        "id": task_id,
        "name": name,
        "enabled": enabled_raw,
        "task_summary": summary,
        "prompt_template": prompt_template,
        "interval_minutes": interval_minutes,
        "daily_times": daily_times,
        "run_at": run_at,
        "created_at": created_at,
    }


def load_schedule_tasks(tasks_file: Path = SCHEDULE_TASKS_FILE) -> list[dict[str, Any]]:
    """读取任务列表。"""
    if not tasks_file.exists():
        return []
    try:
        raw = json.loads(tasks_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"任务文件 JSON 非法: {tasks_file}") from exc
    if not isinstance(raw, list):
        raise ValueError(f"任务文件根节点必须是数组: {tasks_file}")
    return [validate_schedule_task(task) for task in raw]


def save_schedule_tasks(
    tasks: list[dict[str, Any]],
    tasks_file: Path = SCHEDULE_TASKS_FILE,
) -> None:
    """保存任务列表。"""
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    validated = [validate_schedule_task(task) for task in tasks]
    tasks_file.write_text(
        json.dumps(validated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_schedule_state(state_file: Path = SCHEDULE_STATE_FILE) -> dict[str, dict[str, Any]]:
    """读取任务运行状态。"""
    if not state_file.exists():
        return {}
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"任务状态文件 JSON 非法: {state_file}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"任务状态文件根节点必须是对象: {state_file}")
    state: dict[str, dict[str, Any]] = {}
    for task_id, item in raw.items():
        if not isinstance(task_id, str) or not isinstance(item, dict):
            continue
        state[task_id] = {
            "last_run_at": item.get("last_run_at"),
            "next_run_at": item.get("next_run_at"),
            "last_error": item.get("last_error"),
            "task_signature": item.get("task_signature"),
        }
    return state


def save_schedule_state(
    state: dict[str, dict[str, Any]],
    state_file: Path = SCHEDULE_STATE_FILE,
) -> None:
    """保存任务运行状态。"""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_task_creation_prompt(user_request: str, recent_turns: list[str]) -> str:
    """构造定时任务创建提示词。"""
    history_text = "\n\n".join(recent_turns) if recent_turns else "(无最近 temp 上下文)"
    current_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "你是一个定时任务生成器。"
        "请根据用户需求和最近对话，总结出一个适合定时执行的任务定义。"
        "不要把最近对话原文写入输出 JSON，只保留总结后的任务背景与稳定提示词。\n\n"
        "输出要求：\n"
        "1. 只输出一个 JSON 对象，不要输出 Markdown 代码块。\n"
        "2. 字段必须严格为："
        'name, enabled, task_summary, prompt_template, interval_minutes, daily_times, run_at。\n'
        "3. interval_minutes 为整数或 null。\n"
        '4. daily_times 为 "HH:MM" 字符串数组，可为空数组。\n'
        '5. run_at 为本地 ISO 时间字符串（格式：YYYY-MM-DDTHH:MM:SS）或 null，不要带时区偏移；用于绝对日期的一次性任务。\n'
        "6. enabled 默认返回 true。\n"
        "7. 至少提供一种调度规则：interval_minutes、daily_times 或 run_at。\n"
        "8. 如果用户表达的是绝对日期的一次性提醒，例如“2026-03-15 下午3点”或“明天下午三点提醒我一次”，优先使用 run_at。\n"
        "9. prompt_template 要能让 Claude 在定时触发时直接生成可发送邮件的 Markdown 正文。\n"
        '10. prompt_template 只描述内容目标，不要要求解释说明或“你可以直接发送”之类包装语。\n'
        "11. 可以按内容需要使用标题、列表、表格、引用、链接、代码块，但不要强制每次都使用。\n\n"
        "当前本地时间（Asia/Shanghai）:\n"
        f"{current_dt}\n\n"
        "最近 10 条 temp 对话：\n"
        f"{history_text}\n\n"
        "用户的建任务请求：\n"
        f"{user_request}\n"
    )


def build_task_regeneration_prompt(
    existing_task: dict[str, Any],
    user_request: str,
) -> str:
    """构造基于现有任务修改后的生成提示词。"""
    safe_task = validate_schedule_task(existing_task)
    current_dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "你是一个定时任务修改器。"
        "请根据现有任务定义和新的修改要求，输出一个新的任务定义。"
        "不要输出解释说明，只输出一个 JSON 对象。\n\n"
        "输出要求：\n"
        "1. 字段必须严格为："
        'name, enabled, task_summary, prompt_template, interval_minutes, daily_times, run_at。\n'
        "2. interval_minutes 为整数或 null。\n"
        '3. daily_times 为 "HH:MM" 字符串数组，可为空数组。\n'
        '4. run_at 为本地 ISO 时间字符串（格式：YYYY-MM-DDTHH:MM:SS）或 null，不要带时区偏移；用于绝对日期的一次性任务。\n'
        "5. 至少提供一种调度规则：interval_minutes、daily_times 或 run_at。\n"
        "6. prompt_template 要能让 Claude 在定时触发时直接生成可发送邮件的 Markdown 正文。\n\n"
        "当前本地时间（Asia/Shanghai）:\n"
        f"{current_dt}\n\n"
        "现有任务定义：\n"
        f"- 名称: {safe_task['name']}\n"
        f"- 启用: {safe_task['enabled']}\n"
        f"- 摘要: {safe_task['task_summary']}\n"
        f"- 提示词: {safe_task['prompt_template']}\n"
        f"- interval_minutes: {safe_task['interval_minutes']}\n"
        f"- daily_times: {', '.join(safe_task['daily_times']) if safe_task['daily_times'] else '(空)'}\n"
        f"- run_at: {safe_task['run_at'] or '(空)'}\n\n"
        "修改要求：\n"
        f"{user_request.strip()}\n"
    )


def generate_schedule_task(user_request: str) -> dict[str, Any]:
    """调用 Claude 生成任务定义。"""
    request = user_request.strip()
    if not request:
        raise ValueError("定时任务描述为空")
    recent_turns = get_recent_temp_turns()
    prompt = build_task_creation_prompt(request, recent_turns)
    raw_output = _run_claude_prompt(prompt)
    if is_known_claude_error_output(raw_output):
        raise RuntimeError(raw_output)
    task_obj = _extract_json_object(raw_output)
    return normalize_schedule_task(task_obj)


def regenerate_schedule_task(existing_task: dict[str, Any], user_request: str) -> dict[str, Any]:
    """基于已有任务和修改要求重生成任务定义。"""
    request = user_request.strip()
    if not request:
        raise ValueError("任务修改描述为空")
    prompt = build_task_regeneration_prompt(existing_task, request)
    raw_output = _run_claude_prompt(prompt)
    if is_known_claude_error_output(raw_output):
        raise RuntimeError(raw_output)
    task_obj = _extract_json_object(raw_output)
    return normalize_schedule_task(task_obj)


def _should_rewrite_markdown_body(text: str) -> bool:
    """Detect wrapper text that should be rewritten into clean Markdown."""
    stripped = text.strip()
    if not stripped:
        return False
    for pattern in MARKDOWN_WRAPPER_PATTERNS:
        if pattern.search(stripped):
            return True
    return False


def _rewrite_markdown_body(text: str) -> str:
    """Rewrite wrapped output into clean Markdown email content."""
    prompt = (
        "把下面内容改写成可直接发送的 Markdown 邮件正文。\n"
        "严格要求：\n"
        "1. 只输出最终 Markdown 正文，不要任何说明、前缀或结尾总结。\n"
        "2. 删除“下面是”“你可以直接发送”等包装语。\n"
        "3. 保留原文的核心事实和提醒事项。\n"
        "4. 可以使用标题、列表、表格、引用、链接，但只在有必要时使用。\n"
        "5. 不要把全文包在 Markdown 代码块里。\n\n"
        "原文：\n"
        f"{text.strip()}\n"
    )
    return _run_claude_prompt(prompt)


def build_schedule_execution_prompt(task: dict[str, Any], now: Optional[datetime] = None) -> str:
    """构造定时任务执行时发送给 Claude 的提示词。"""
    safe_task = validate_schedule_task(task)
    current_dt = now if now is not None else datetime.now()
    schedule_bits: list[str] = []
    if safe_task["interval_minutes"] is not None:
        schedule_bits.append(f"固定间隔 {safe_task['interval_minutes']} 分钟")
    if safe_task["daily_times"]:
        schedule_bits.append("每日时间点 " + ", ".join(safe_task["daily_times"]))
    if safe_task["run_at"] is not None:
        schedule_bits.append("一次性时间点 " + safe_task["run_at"].replace("T", " "))
    schedule_text = "；".join(schedule_bits) if schedule_bits else "未配置"
    return (
        "你正在执行一个邮件定时任务。\n"
        f"任务名称: {safe_task['name']}\n"
        f"任务摘要: {safe_task['task_summary']}\n"
        f"调度规则: {schedule_text}\n"
        f"当前触发时间: {current_dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "请根据以下固定提示生成一段可直接作为邮件正文发送的 Markdown 内容。\n"
        "严格要求：\n"
        "1. 只输出最终邮件正文，不要解释、不要前言、不要结语、不要自我说明。\n"
        "2. 输出格式为标准 Markdown，可按内容需要使用标题、列表、表格、引用、链接、代码块。\n"
        '3. 不要说“下面是”“你可以直接发送”等包装语。\n'
        "4. 不要把整篇正文包在 Markdown 代码块里。\n"
        "5. 内容优先可读性，不要为了格式而堆砌结构。\n\n"
        f"{safe_task['prompt_template']}\n"
    )


def run_schedule_task(task: dict[str, Any], now: Optional[datetime] = None) -> str:
    """执行定时任务，返回 Markdown 邮件正文。"""
    prompt = build_schedule_execution_prompt(task, now=now)
    output = _run_claude_prompt(prompt)
    if is_known_claude_error_output(output):
        return output
    if _should_rewrite_markdown_body(output):
        rewritten = _rewrite_markdown_body(output)
        if not is_known_claude_error_output(rewritten) and rewritten.strip():
            return rewritten.strip()
    return output.strip()


def schedule_task_signature(task: dict[str, Any]) -> str:
    """基于任务配置内容生成稳定签名。"""
    safe_task = validate_schedule_task(task)
    signature_payload = {
        "name": safe_task["name"],
        "enabled": safe_task["enabled"],
        "task_summary": safe_task["task_summary"],
        "prompt_template": safe_task["prompt_template"],
        "interval_minutes": safe_task["interval_minutes"],
        "daily_times": safe_task["daily_times"],
        "run_at": safe_task["run_at"],
    }
    return json.dumps(signature_payload, ensure_ascii=False, sort_keys=True)


def compute_next_schedule_run(task: dict[str, Any], base_dt: datetime) -> Optional[datetime]:
    """根据任务规则，计算 base_dt 之后的下一次触发时间。"""
    safe_task = validate_schedule_task(task)
    candidates: list[datetime] = []
    interval_minutes = safe_task["interval_minutes"]
    if interval_minutes is not None:
        candidates.append(base_dt + timedelta(minutes=interval_minutes))
    for clock in safe_task["daily_times"]:
        hour, minute = clock.split(":", 1)
        candidate = base_dt.replace(
            hour=int(hour),
            minute=int(minute),
            second=0,
            microsecond=0,
        )
        if candidate <= base_dt:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    if safe_task["run_at"] is not None:
        run_at_dt = datetime.fromisoformat(safe_task["run_at"])
        if run_at_dt > base_dt:
            candidates.append(run_at_dt)
    if not candidates:
        return None
    return min(candidates)


def format_task_list_text(
    tasks: list[dict[str, Any]],
    state: Optional[dict[str, dict[str, Any]]] = None,
) -> str:
    """格式化任务列表，便于命令行查看。"""
    safe_tasks = [validate_schedule_task(task) for task in tasks]
    if not safe_tasks:
        return "当前没有定时任务"
    state_map = state or {}
    lines: list[str] = [f"当前共有 {len(safe_tasks)} 个定时任务："]
    for idx, task in enumerate(safe_tasks, start=1):
        entry = state_map.get(task["id"], {})
        next_run = entry.get("next_run_at")
        if not next_run:
            if task["run_at"] is not None and entry.get("last_run_at"):
                next_run = "已执行"
            else:
                next_run = "未计算"
        last_error = entry.get("last_error")
        enabled = "启用" if task["enabled"] else "停用"
        schedule_bits: list[str] = []
        if task["interval_minutes"] is not None:
            schedule_bits.append(f"每 {task['interval_minutes']} 分钟")
        if task["daily_times"]:
            schedule_bits.append("每日 " + ",".join(task["daily_times"]))
        if task["run_at"] is not None:
            schedule_bits.append("一次性 " + task["run_at"].replace("T", " "))
        if not schedule_bits:
            schedule_bits.append("无规则")
        line = f"{idx}. {task['name']} [{enabled}] | {' + '.join(schedule_bits)} | next: {next_run}"
        if last_error:
            line += f" | error: {last_error}"
        lines.append(line)
    return "\n".join(lines)


def parse_iso_datetime(raw_value: Any) -> Optional[datetime]:
    """Parse ISO datetime strings from state files."""
    if raw_value in (None, ""):
        return None
    if not isinstance(raw_value, str):
        return None
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return None


def sync_schedule_state(
    now: datetime,
    skip_past_due: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], bool]:
    """Synchronize task definitions and runtime state."""
    tasks = load_schedule_tasks()
    state = load_schedule_state()
    changed = False
    active_task_ids = {task["id"] for task in tasks}

    for task_id in list(state.keys()):
        if task_id not in active_task_ids:
            del state[task_id]
            changed = True

    for task in tasks:
        task_id = task["id"]
        signature = schedule_task_signature(task)
        entry = state.get(task_id)
        desired_next: Optional[str]

        if entry is None:
            next_dt = compute_next_schedule_run(task, now) if task["enabled"] else None
            desired_next = next_dt.isoformat(timespec="seconds") if next_dt else None
            state[task_id] = {
                "last_run_at": None,
                "next_run_at": desired_next,
                "last_error": None,
                "task_signature": signature,
            }
            changed = True
            continue

        if not task["enabled"]:
            desired_next = None
        elif entry.get("task_signature") != signature:
            next_dt = compute_next_schedule_run(task, now)
            desired_next = next_dt.isoformat(timespec="seconds") if next_dt else None
            state[task_id] = {
                "last_run_at": None,
                "next_run_at": desired_next,
                "last_error": None,
                "task_signature": signature,
            }
            changed = True
            continue
        else:
            desired_next = entry.get("next_run_at")
            if desired_next in (None, "") or parse_iso_datetime(desired_next) is None:
                recomputed = compute_next_schedule_run(task, now)
                desired_next = recomputed.isoformat(timespec="seconds") if recomputed else None

        if skip_past_due:
            next_dt = parse_iso_datetime(desired_next)
            if next_dt is not None and next_dt <= now:
                recomputed = compute_next_schedule_run(task, now)
                desired_next = recomputed.isoformat(timespec="seconds") if recomputed else None

        if entry.get("next_run_at") != desired_next:
            entry["next_run_at"] = desired_next
            changed = True
        if entry.get("task_signature") != signature:
            entry["task_signature"] = signature
            changed = True
        if not task["enabled"] and entry.get("last_error") is not None:
            entry["last_error"] = None
            changed = True

    return tasks, state, changed


def sync_and_save_schedule_state(
    *,
    now: Optional[datetime] = None,
    skip_past_due: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], bool]:
    """Synchronize runtime state and persist it when changed."""
    current_dt = now if now is not None else datetime.now()
    tasks, state, changed = sync_schedule_state(now=current_dt, skip_past_due=skip_past_due)
    if changed:
        save_schedule_state(state)
    return tasks, state, changed


def task_schedule_text(task: dict[str, Any]) -> str:
    """Render a readable schedule rule summary."""
    schedule_bits: list[str] = []
    if task["interval_minutes"] is not None:
        schedule_bits.append(f"每 {task['interval_minutes']} 分钟")
    if task["daily_times"]:
        schedule_bits.append("每日 " + ",".join(task["daily_times"]))
    if task["run_at"] is not None:
        schedule_bits.append("一次性 " + task["run_at"].replace("T", " "))
    return " + ".join(schedule_bits) if schedule_bits else "无规则"


def create_schedule_task_response(task: dict[str, Any], state: dict[str, dict[str, Any]]) -> str:
    """Render a confirmation message after task creation or update."""
    entry = state.get(task["id"], {})
    next_run_at = entry.get("next_run_at") or "未计算"
    return (
        "定时任务已写入\n"
        f"名称: {task['name']}\n"
        f"摘要: {task['task_summary']}\n"
        f"规则: {task_schedule_text(task)}\n"
        f"下次触发: {next_run_at}"
    )


def _initialize_task_state(
    task: dict[str, Any],
    state: dict[str, dict[str, Any]],
    *,
    now: datetime,
) -> None:
    """Initialize or replace state for a task."""
    created_at = parse_iso_datetime(task["created_at"]) or now
    next_dt = compute_next_schedule_run(task, created_at) if task["enabled"] else None
    state[task["id"]] = {
        "last_run_at": None,
        "next_run_at": next_dt.isoformat(timespec="seconds") if next_dt else None,
        "last_error": None,
        "task_signature": schedule_task_signature(task),
    }


def create_schedule_task_from_request(
    request: str,
    *,
    now: Optional[datetime] = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Create a task from natural language and persist it."""
    current_dt = now if now is not None else datetime.now()
    task = normalize_schedule_task(generate_schedule_task(request), created_at=current_dt)
    tasks = load_schedule_tasks()
    tasks.append(task)
    save_schedule_tasks(tasks)
    state = load_schedule_state()
    _initialize_task_state(task, state, now=current_dt)
    save_schedule_state(state)
    return task, state


def create_schedule_task_from_definition(
    task_definition: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """持久化当前模型已经生成好的任务定义。"""
    current_dt = now if now is not None else datetime.now()
    task = normalize_schedule_task(task_definition, created_at=current_dt)
    tasks = load_schedule_tasks()
    tasks.append(task)
    save_schedule_tasks(tasks)
    state = load_schedule_state()
    _initialize_task_state(task, state, now=current_dt)
    save_schedule_state(state)
    return task, state


def list_schedule_tasks(now: Optional[datetime] = None) -> str:
    """Return a formatted task list after syncing state."""
    current_dt = now if now is not None else datetime.now()
    _, state, _ = sync_and_save_schedule_state(now=current_dt, skip_past_due=False)
    return format_task_list_text(load_schedule_tasks(), state)


def _match_tasks(tasks: list[dict[str, Any]], selector: str) -> list[dict[str, Any]]:
    """Return tasks matching id, exact name, or unique substring."""
    needle = selector.strip()
    if not needle:
        raise ValueError("任务选择器为空")

    exact_id_matches = [task for task in tasks if task["id"] == needle]
    if exact_id_matches:
        return exact_id_matches

    lower = needle.lower()
    exact_name_matches = [task for task in tasks if task["name"].lower() == lower]
    if exact_name_matches:
        return exact_name_matches

    return [task for task in tasks if lower in task["name"].lower()]


def _resolve_task(tasks: list[dict[str, Any]], selector: str) -> tuple[int, dict[str, Any]]:
    """Resolve a task selector into a stable list index and task object."""
    matches = _match_tasks(tasks, selector)
    if not matches:
        raise ValueError(f"未找到任务: {selector}")
    if len(matches) > 1:
        lines = [f"匹配到多个任务: {selector}"]
        for task in matches:
            lines.append(f"- {task['name']} ({task['id']})")
        raise ValueError("\n".join(lines))

    matched = matches[0]
    for index, task in enumerate(tasks):
        if task["id"] == matched["id"]:
            return index, task
    raise ValueError(f"任务不存在: {selector}")


def set_schedule_task_enabled(
    selector: str,
    enabled: bool,
    *,
    now: Optional[datetime] = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Enable or disable a task and persist the updated state."""
    current_dt = now if now is not None else datetime.now()
    tasks = load_schedule_tasks()
    index, task = _resolve_task(tasks, selector)
    updated_task = dict(task)
    updated_task["enabled"] = enabled
    tasks[index] = updated_task
    save_schedule_tasks(tasks)

    state = load_schedule_state()
    if enabled:
        _initialize_task_state(updated_task, state, now=current_dt)
    else:
        state[updated_task["id"]] = {
            "last_run_at": state.get(updated_task["id"], {}).get("last_run_at"),
            "next_run_at": None,
            "last_error": None,
            "task_signature": schedule_task_signature(updated_task),
        }
    save_schedule_state(state)
    return updated_task, state


def delete_schedule_task(selector: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Delete a task and remove its runtime state."""
    tasks = load_schedule_tasks()
    index, task = _resolve_task(tasks, selector)
    del tasks[index]
    save_schedule_tasks(tasks)

    state = load_schedule_state()
    if task["id"] in state:
        del state[task["id"]]
        save_schedule_state(state)
    return task, tasks


def update_schedule_task_from_request(
    selector: str,
    request: str,
    *,
    now: Optional[datetime] = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Update a task via Claude and persist the replacement."""
    current_dt = now if now is not None else datetime.now()
    tasks = load_schedule_tasks()
    index, task = _resolve_task(tasks, selector)
    replacement = normalize_schedule_task(
        regenerate_schedule_task(task, request),
        task_id=task["id"],
        created_at=current_dt,
    )
    tasks[index] = replacement
    save_schedule_tasks(tasks)

    state = load_schedule_state()
    _initialize_task_state(replacement, state, now=current_dt)
    save_schedule_state(state)
    return replacement, state


def update_schedule_task_from_definition(
    selector: str,
    task_definition: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """持久化当前模型已经生成好的替换任务定义。"""
    current_dt = now if now is not None else datetime.now()
    tasks = load_schedule_tasks()
    index, task = _resolve_task(tasks, selector)
    replacement = normalize_schedule_task(
        task_definition,
        task_id=task["id"],
        created_at=current_dt,
    )
    tasks[index] = replacement
    save_schedule_tasks(tasks)

    state = load_schedule_state()
    _initialize_task_state(replacement, state, now=current_dt)
    save_schedule_state(state)
    return replacement, state


def _parse_bool(raw_value: str | None, default: bool) -> bool:
    """Parse common boolean strings from environment variables."""
    if raw_value is None:
        return default
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _strip_matching_quotes(value: str) -> str:
    """Strip matching single or double quotes around a value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_project_dotenv(env_path: Path = PROJECT_DOTENV_PATH) -> None:
    """Load key-value pairs from the project .env without overriding real env vars."""
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = DOTENV_LINE_PATTERN.match(raw_line)
        if not match:
            continue
        key = match.group("key")
        value = _strip_matching_quotes(match.group("value").strip())
        os.environ.setdefault(key, value)


def load_email_config_from_env() -> dict[str, object]:
    """Load SMTP config from environment variables."""
    load_project_dotenv()
    host = os.environ.get("SMTP_HOST", "").strip()
    from_addr = os.environ.get("SMTP_FROM", "").strip()
    to_raw = os.environ.get("SMTP_TO", "").strip()
    if not host:
        raise ValueError("缺少环境变量 SMTP_HOST")
    if not from_addr:
        raise ValueError("缺少环境变量 SMTP_FROM")
    if not to_raw:
        raise ValueError("缺少环境变量 SMTP_TO")

    port_raw = os.environ.get("SMTP_PORT", str(DEFAULT_SMTP_PORT)).strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError(f"SMTP_PORT 非法: {port_raw!r}") from exc
    if port <= 0:
        raise ValueError("SMTP_PORT 必须大于 0")

    recipients = [item.strip() for item in to_raw.split(",") if item.strip()]
    if not recipients:
        raise ValueError("SMTP_TO 不能为空")

    return {
        "host": host,
        "port": port,
        "user": os.environ.get("SMTP_USER", "").strip(),
        "password": os.environ.get("SMTP_PASS", ""),
        "from_addr": from_addr,
        "to_addrs": recipients,
        "use_tls": _parse_bool(os.environ.get("SMTP_USE_TLS"), True),
    }


def _inject_inline_styles(html_fragment: str) -> str:
    """Apply a minimal set of inline styles for email clients."""

    def repl(match: re.Match[str]) -> str:
        tag = match.group("tag").lower()
        attrs = match.group("attrs") or ""
        style = TAG_INLINE_STYLES[tag]
        return f"<{tag}{attrs} style=\"{style}\">"

    return STYLE_TAG_PATTERN.sub(repl, html_fragment)


def render_markdown_email_html(markdown_body: str) -> str:
    """Render Markdown into sanitized HTML suitable for emails."""
    source = markdown_body.strip()
    if not source:
        raise ValueError("邮件 Markdown 正文为空")
    raw_html = markdown_lib.markdown(source, extensions=MARKDOWN_EXTENSIONS)
    clean_html = bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    styled_html = _inject_inline_styles(clean_html)
    return (
        "<!doctype html>"
        "<html>"
        "<body style=\"margin:0;padding:24px;background:#f5f5f5;color:#24292f;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;\">"
        "<div style=\"max-width:760px;margin:0 auto;padding:24px;background:#ffffff;"
        "border:1px solid #d8dee4;border-radius:8px;\">"
        f"{styled_html}"
        "</div>"
        "</body>"
        "</html>"
    )


def send_email(
    *,
    subject: str,
    html_body: str,
    config: dict[str, object],
    attachments: Optional[list[dict[str, object]]] = None,
) -> None:
    """Send an HTML email through SMTP."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = str(config["from_addr"])
    message["To"] = ", ".join(config["to_addrs"])
    message.set_content(html_body, subtype="html")
    for attachment in attachments or []:
        filename = str(attachment["filename"])
        content = attachment["content"]
        if isinstance(content, (bytes, bytearray)):
            payload = bytes(content)
        elif isinstance(content, str):
            payload = content.encode("utf-8")
        else:
            raise TypeError(f"附件内容类型非法: {type(content).__name__}")
        message.add_attachment(
            payload,
            maintype=str(attachment.get("maintype") or "application"),
            subtype=str(attachment.get("subtype") or "octet-stream"),
            filename=filename,
        )

    host = str(config["host"])
    port = int(config["port"])
    user = str(config.get("user") or "")
    password = str(config.get("password") or "")
    use_tls = bool(config.get("use_tls"))

    with smtplib.SMTP(host, port, timeout=30) as client:
        client.ehlo()
        if use_tls:
            client.starttls()
            client.ehlo()
        if user:
            client.login(user, password)
        client.send_message(message)


def _email_subject_for_task(task: dict[str, object]) -> str:
    """Build the email subject for a scheduled task."""
    return f"定时任务 | {task['name']}"


def execute_due_schedule_tasks(
    *,
    dry_run: bool = False,
    on_task_error: Optional[Callable[[dict[str, Any], str], None]] = None,
) -> int:
    """Execute all due tasks once and send emails for due items."""
    now = datetime.now()
    tasks, state, changed = sync_schedule_state(now=now, skip_past_due=False)
    sent_count = 0
    email_config: Optional[dict[str, object]] = None

    for task in tasks:
        if not task["enabled"]:
            continue
        entry = state.get(task["id"])
        if entry is None:
            continue
        next_run_dt = parse_iso_datetime(entry.get("next_run_at"))
        if next_run_dt is None or next_run_dt > now:
            continue

        attempt_time = datetime.now()
        try:
            # 检查是否有 command 字段，如果有则直接执行命令
            task_command = task.get("command")
            if task_command:
                # 执行命令获取输出
                import subprocess
                result = subprocess.run(
                    task_command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=Path(__file__).resolve().parent
                )
                if result.returncode != 0:
                    raise RuntimeError(f"命令执行失败: {result.stderr}")
                markdown_body = result.stdout.strip()
                subject = _email_subject_for_task(task)
            else:
                # 原有的 Claude 生成逻辑
                markdown_body = run_schedule_task(task, now=attempt_time).strip()
                if not markdown_body:
                    raise RuntimeError("定时任务未生成正文")
                if is_known_claude_error_output(markdown_body):
                    raise RuntimeError(markdown_body)
                subject = _email_subject_for_task(task)

            html_body = render_markdown_email_html(markdown_body)
            if dry_run:
                print(f"[dry-run] {subject}")
                print("[markdown]")
                print(markdown_body)
                print()
                print("[html]")
                print(html_body)
                print()
            else:
                if email_config is None:
                    email_config = load_email_config_from_env()
                send_email(subject=subject, html_body=html_body, config=email_config)
            sent_count += 1
            entry["last_error"] = None
        except Exception as exc:
            entry["last_error"] = str(exc).strip() or exc.__class__.__name__
            if on_task_error is not None:
                on_task_error(task, entry["last_error"])
            else:
                print(f"[task-error] {task['name']}: {entry['last_error']}")
        finally:
            entry["last_run_at"] = attempt_time.isoformat(timespec="seconds")
            next_dt = compute_next_schedule_run(task, attempt_time) if task["enabled"] else None
            entry["next_run_at"] = next_dt.isoformat(timespec="seconds") if next_dt else None
            changed = True

    if changed:
        save_schedule_state(state)
    return sent_count
