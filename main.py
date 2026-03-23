#!/usr/bin/env python3
"""主入口：运行基于官方 openclaw-weixin 的微信代理。"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Optional

from claude_io_utlities import (
    ClaudeCallResult,
    append_memory,
    ask_claude_result_with_callback,
    build_runtime_prompt,
    ensure_base_dirs,
    resolve_target,
)
from wechat_media_bridge import PENDING_ATTACHMENTS_ACK_TEXT, build_claude_input, parse_claude_reply
from wechat_openclaw_agent import (
    DEFAULT_LOGIN_TIMEOUT_S,
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_PROFILE_NAME,
    OpenClawWeixinAgent,
    SyncMessage,
    format_runtime_error,
)

SCREENSHOT_TRIGGER = "截屏"
UID_ONLY_PATTERN = re.compile(r"^[A-Za-z0-9]{4}$")
UID_ONLY_MESSAGE = "ID格式错误：仅输入了UID，请使用 'UID, message'"
DEFAULT_PROGRESS_UPDATE_INTERVAL_S = 120.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行官方 openclaw-weixin 微信代理。",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=DEFAULT_POLL_INTERVAL_S,
        help="主循环轮询间隔（秒）",
    )
    parser.add_argument(
        "--login-timeout-s",
        type=float,
        default=DEFAULT_LOGIN_TIMEOUT_S,
        help="首次登录等待超时（秒）",
    )
    parser.add_argument(
        "--profile-name",
        default=DEFAULT_PROFILE_NAME,
        help="登录态 profile 名称，默认复用 `default`",
    )
    return parser


def ask_claude_with_progress(
    agent: OpenClawWeixinAgent,
    reply_to: SyncMessage,
    prompt: str,
    target_dir: Path,
    use_session_resume: bool,
    *,
    target_name: str,
) -> ClaudeCallResult:
    next_progress_at = DEFAULT_PROGRESS_UPDATE_INTERVAL_S

    def _on_wait(elapsed_s: float) -> None:
        nonlocal next_progress_at
        if agent.stop_requested:
            raise KeyboardInterrupt("stopped")
        if elapsed_s < next_progress_at:
            return
        elapsed_min = max(2, int(elapsed_s // 60))
        progress_text = f"Agent 仍在处理，已用时{elapsed_min} min"
        agent.send_text(reply_to, progress_text)
        agent.emitter.emit(
            "status",
            payload={
                "stage": "claude_progress",
                "peer_user_id": reply_to.from_user_id,
                "target": target_name,
                "elapsed_min": elapsed_min,
            },
        )
        next_progress_at += DEFAULT_PROGRESS_UPDATE_INTERVAL_S

    return ask_claude_result_with_callback(
        prompt,
        target_dir,
        use_session_resume,
        on_wait=_on_wait,
    )


def process_message(agent: OpenClawWeixinAgent, message: SyncMessage) -> None:
    agent.emitter.emit(
        "message_in",
        payload={
            "message_id": message.message_id,
            "text": message.text,
            "timestamp": message.create_time_ms,
            "from_user_id": message.from_user_id,
            "to_user_id": message.to_user_id,
            "attachment_count": len(message.attachments),
        },
    )
    stripped = message.text.strip()
    try:
        if message.attachments and not stripped:
            agent.append_pending_attachments(message)
            agent.send_text(message, PENDING_ATTACHMENTS_ACK_TEXT)
            return
        if stripped == SCREENSHOT_TRIGGER:
            agent.send_screenshot(message)
            return
        if UID_ONLY_PATTERN.fullmatch(stripped):
            agent.send_text(message, UID_ONLY_MESSAGE)
            return

        target_dir, prompt, use_session_resume = resolve_target(
            message.text,
            temp_dir=agent.resolve_temp_dir(message),
            uid_base_dir=agent.resolve_uid_root(message),
        )
        target_name = target_dir.name
        pending_attachments = agent.load_pending_attachments(message)
        attachments_for_claude = [*pending_attachments, *list(message.attachments)]
        # `prompt` 是解析后的纯文本需求；有附件时再拼成真正发给 Claude 的输入。
        prompt_for_claude = (
            build_claude_input(prompt, attachments_for_claude) if attachments_for_claude else prompt
        )
        agent.emitter.emit(
            "claude_request",
            payload={
                "target": target_name,
                "message": prompt_for_claude,
                "attachment_count": len(attachments_for_claude),
                "peer_user_id": message.from_user_id,
            },
        )
        # 运行时协议提示单独追加，避免覆盖附件信息或会话正文。
        claude_result = ask_claude_with_progress(
            agent,
            message,
            f"{prompt_for_claude}\n\n{build_runtime_prompt()}",
            target_dir,
            use_session_resume,
            target_name=target_name,
        )
        append_memory(target_dir / "memory.md", prompt_for_claude, claude_result.text)
        if not claude_result.ok:
            error_payload = {
                "stage": "claude_call",
                "target": target_name,
                "message": claude_result.text,
                "peer_user_id": message.from_user_id,
            }
            if claude_result.error_type:
                error_payload["error_type"] = claude_result.error_type
            if claude_result.return_code is not None:
                error_payload["return_code"] = claude_result.return_code
            if claude_result.stderr:
                error_payload["stderr"] = claude_result.stderr
            agent.emitter.emit(
                "error",
                ok=False,
                payload=error_payload,
            )
            agent.send_text(message, claude_result.text)
            return
        raw_reply = claude_result.text
        parsed_reply = parse_claude_reply(raw_reply)
        agent.emitter.emit(
            "claude_response",
            payload={
                "target": target_name,
                "message": raw_reply,
                "resource_count": len(parsed_reply.resources),
                "peer_user_id": message.from_user_id,
            },
        )
        if parsed_reply.text:
            text_to_send = (
                parsed_reply.text if target_name == "temp" else f"[{target_name}]:\n{parsed_reply.text}"
            )
            agent.send_text(message, text_to_send)
        agent.send_claude_resources(message, parsed_reply.resources)
        if pending_attachments:
            agent.clear_pending_attachments(message)
    except Exception as exc:
        message_text = format_runtime_error(exc)
        agent.emitter.emit(
            "error",
            ok=False,
            payload={
                "stage": "process_message",
                "message": message_text,
                "peer_user_id": message.from_user_id,
            },
        )
        try:
            agent.send_text(message, message_text)
        except Exception as send_exc:
            agent.emitter.emit(
                "error",
                ok=False,
                payload={"stage": "send_error_fallback", "message": format_runtime_error(send_exc)},
            )


def run_agent(agent: OpenClawWeixinAgent) -> int:
    try:
        ensure_base_dirs()
        agent.bootstrap()
        agent.prepare_session()
        agent.begin_listening()
        while agent.should_continue():
            agent.run_due_schedule_tasks_once()
            message = agent.dequeue_message()
            if message is not None:
                process_message(agent, message)
                continue
            if not agent.wait_for_next_poll():
                break
    except Exception as exc:
        if agent.should_suppress_exception(exc):
            return 0
        raise
    finally:
        agent.shutdown()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    agent = OpenClawWeixinAgent(
        poll_interval_s=args.poll_interval_s,
        login_timeout_s=args.login_timeout_s,
        profile_name=args.profile_name,
    )
    try:
        return run_agent(agent)
    except KeyboardInterrupt:
        agent.emitter.emit("status", payload={"stage": "stopped"})
        return 0
    except Exception as exc:
        agent.emitter.emit(
            "error",
            ok=False,
            payload={"stage": "fatal", "message": format_runtime_error(exc)},
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
