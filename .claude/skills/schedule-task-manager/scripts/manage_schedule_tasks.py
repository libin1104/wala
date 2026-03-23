#!/usr/bin/env python3
"""Deterministic task management for the qclaude workspace."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    """Find the repository root by walking upward from the current workspace."""
    for candidate in [start, *start.parents]:
        if (
            (candidate / "main.py").exists()
            and (candidate / "schedual_utilities.py").exists()
            and (candidate / "claude_code.py").exists()
            and (candidate / "gateway_bot.py").exists()
            and (candidate / "client.py").exists()
        ):
            return candidate
    raise FileNotFoundError(
        "未找到 qclaude 仓库根目录；请在仓库内使用这个 skill，"
        "或通过 --repo-root 指定根目录。"
    )


def _load_helpers(repo_root: Path):
    """Import repository helpers after resolving the repo root."""
    sys.path.insert(0, str(repo_root.parent))
    from qclaude.schedual_utilities import (
        create_schedule_task_from_request,
        create_schedule_task_from_definition,
        create_schedule_task_response,
        delete_schedule_task,
        format_recent_temp_turns,
        list_schedule_tasks,
        set_schedule_task_enabled,
        task_schedule_text,
        update_schedule_task_from_definition,
        update_schedule_task_from_request,
    )

    return {
        "create_schedule_task_from_request": create_schedule_task_from_request,
        "create_schedule_task_from_definition": create_schedule_task_from_definition,
        "create_schedule_task_response": create_schedule_task_response,
        "delete_schedule_task": delete_schedule_task,
        "format_recent_temp_turns": format_recent_temp_turns,
        "list_schedule_tasks": list_schedule_tasks,
        "set_schedule_task_enabled": set_schedule_task_enabled,
        "task_schedule_text": task_schedule_text,
        "update_schedule_task_from_definition": update_schedule_task_from_definition,
        "update_schedule_task_from_request": update_schedule_task_from_request,
    }


def _resolve_repo_root(raw_repo_root: str | None) -> Path:
    """Resolve the repository root from CLI args or current working directory."""
    if raw_repo_root:
        repo_root = Path(raw_repo_root).expanduser().resolve()
        if not repo_root.exists():
            raise FileNotFoundError(f"repo root 不存在: {repo_root}")
        return repo_root
    return _find_repo_root(Path.cwd().resolve())


def _build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(description="管理 qclaude 的定时邮件任务")
    parser.add_argument(
        "--repo-root",
        help="qclaude 仓库根目录；默认从当前工作目录向上查找",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="创建任务")
    create_parser.add_argument("request", help="自然语言任务需求")

    subparsers.add_parser("context", help="查看最近 temp 上下文")

    subparsers.add_parser("create-from-json", help="从 stdin 的 JSON 对象创建任务")

    subparsers.add_parser("list", help="查看任务列表")

    enable_parser = subparsers.add_parser("enable", help="启用任务")
    enable_parser.add_argument("selector", help="任务 id、精确名称或唯一名称片段")

    disable_parser = subparsers.add_parser("disable", help="停用任务")
    disable_parser.add_argument("selector", help="任务 id、精确名称或唯一名称片段")

    delete_parser = subparsers.add_parser("delete", help="删除任务")
    delete_parser.add_argument("selector", help="任务 id、精确名称或唯一名称片段")

    update_parser = subparsers.add_parser("update", help="更新任务")
    update_parser.add_argument("selector", help="任务 id、精确名称或唯一名称片段")
    update_parser.add_argument("request", help="新的自然语言修改需求")

    update_json_parser = subparsers.add_parser(
        "update-from-json",
        help="用 stdin 的 JSON 对象替换任务定义",
    )
    update_json_parser.add_argument("selector", help="任务 id、精确名称或唯一名称片段")

    return parser


def _read_task_definition_from_stdin() -> dict[str, object]:
    """Read a JSON object from stdin for deterministic create/update flows."""
    raw_text = sys.stdin.read().strip()
    if not raw_text:
        raise ValueError("stdin 中没有任务 JSON")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"任务 JSON 解析失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("任务 JSON 必须是对象")
    return payload


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        repo_root = _resolve_repo_root(args.repo_root)
        helpers = _load_helpers(repo_root)
        current_dt = datetime.now()

        if args.command == "context":
            print(
                "当前本地时间（Asia/Shanghai）:\n"
                f"{current_dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "最近 10 条 temp 对话：\n"
                f"{helpers['format_recent_temp_turns']()}"
            )
            return 0

        if args.command == "create":
            task, state = helpers["create_schedule_task_from_request"](args.request, now=current_dt)
            print(helpers["create_schedule_task_response"](task, state))
            return 0

        if args.command == "create-from-json":
            task, state = helpers["create_schedule_task_from_definition"](
                _read_task_definition_from_stdin(),
                now=current_dt,
            )
            print(helpers["create_schedule_task_response"](task, state))
            return 0

        if args.command == "list":
            print(helpers["list_schedule_tasks"](now=current_dt))
            return 0

        if args.command == "enable":
            task, state = helpers["set_schedule_task_enabled"](args.selector, True, now=current_dt)
            print(helpers["create_schedule_task_response"](task, state))
            return 0

        if args.command == "disable":
            task, _ = helpers["set_schedule_task_enabled"](args.selector, False, now=current_dt)
            print(
                "定时任务已停用\n"
                f"名称: {task['name']}\n"
                f"规则: {helpers['task_schedule_text'](task)}"
            )
            return 0

        if args.command == "delete":
            task, _ = helpers["delete_schedule_task"](args.selector)
            print(f"定时任务已删除\n名称: {task['name']}\nID: {task['id']}")
            return 0

        if args.command == "update":
            task, state = helpers["update_schedule_task_from_request"](
                args.selector,
                args.request,
                now=current_dt,
            )
            print(helpers["create_schedule_task_response"](task, state))
            return 0

        if args.command == "update-from-json":
            task, state = helpers["update_schedule_task_from_definition"](
                args.selector,
                _read_task_definition_from_stdin(),
                now=current_dt,
            )
            print(helpers["create_schedule_task_response"](task, state))
            return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
