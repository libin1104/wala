#!/usr/bin/env python3
"""
Claude 对话管理工具。
功能：
1. 支持临时会话和持久化 UID 会话两种模式
2. UID 模式：使用 4 位短码标识独立对话上下文
3. 自动管理 Claude CLI 的 session-id
4. 保存对话历史到 memory.md
"""

import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

# 会话根目录
ROOT_DIR = Path.home() / ".wclaude_sessions"
# 项目根目录（用于让 Claude CLI 稳定发现项目级 .claude/skills）
PROJECT_DIR = Path(__file__).resolve().parent
# UID 持久化会话目录
UID_DIR = ROOT_DIR / "uid"
# 临时会话目录
TEMP_DIR = ROOT_DIR / "temp"
# 4 位短码正则（字母+数字，不区分大小写）
SHORT_CODE_PATTERN = re.compile(r"^[A-Za-z0-9]{4}$")
# 支持的输入格式：{CODE}{\n|, |. |，|。}{msg}
SHORT_CODE_MESSAGE_PATTERN = re.compile(
    r"^\s*([A-Za-z0-9]{4})(?:\n+|,\s*|\.\s*|，\s*|。\s*)([\s\S]+?)\s*$"
)
DATA_URL_PATTERN = re.compile(r"data:[^,\s]+,[A-Za-z0-9+/=\s]+", re.IGNORECASE)
BASE64_BLOB_PATTERN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{512,}={0,2}(?![A-Za-z0-9+/=])")
# 对话轮次分隔符
TURN_SEPARATOR = "\n---\n"
# 临时模式最大上下文轮次
MAX_CONTEXT_TURNS = 20
MAX_CONTEXT_CHARS = 12000
# Claude 会话恢复失败错误标记
SESSION_ID_NOT_FOUND_MARKER = "No conversation found with session ID"
# 会话恢复失败时返回给调用方的固定提示
SESSION_ID_NOT_FOUND_OUTPUT = "ID检索失败，请更换ID"
# Claude CLI 超时时间（秒）
CLAUDE_CALL_TIMEOUT_S = 30 * 60
# 统一错误输出文案
SESSION_ID_INVALID_OUTPUT = "ID无效，请更换ID"
CLAUDE_CLI_NOT_FOUND_OUTPUT = "Claude CLI不可用，请检查安装与PATH"
CLAUDE_CALL_TIMEOUT_OUTPUT = "Claude响应超时，请稍后重试"
CLAUDE_RATE_LIMIT_OUTPUT = "Claude请求过于频繁，请稍后重试"
CLAUDE_AUTH_ERROR_OUTPUT = "Claude鉴权失败，请重新登录"
CLAUDE_GENERIC_ERROR_OUTPUT = "Claude调用失败，请稍后重试"
KNOWN_CLAUDE_ERROR_OUTPUTS = {
    SESSION_ID_NOT_FOUND_OUTPUT,
    SESSION_ID_INVALID_OUTPUT,
    CLAUDE_CLI_NOT_FOUND_OUTPUT,
    CLAUDE_CALL_TIMEOUT_OUTPUT,
    CLAUDE_RATE_LIMIT_OUTPUT,
    CLAUDE_AUTH_ERROR_OUTPUT,
    CLAUDE_GENERIC_ERROR_OUTPUT,
}


@dataclass(frozen=True)
class ClaudeCallResult:
    ok: bool
    text: str
    error_type: Optional[str] = None
    stderr: str = ""
    return_code: Optional[int] = None


def ensure_base_dirs() -> None:
    """确保基础目录结构存在。"""
    UID_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)


def resolve_short_code_dir(short_code: str, *, base_dir: Optional[Path] = None) -> Path:
    """解析短码会话目录（目录名直接使用 4 位短码大写）。"""
    code = short_code.upper()
    root = UID_DIR if base_dir is None else base_dir
    code_dir = root / code
    code_dir.mkdir(parents=True, exist_ok=True)
    return code_dir


def resolve_target(
    raw_input: str,
    *,
    temp_dir: Optional[Path] = None,
    uid_base_dir: Optional[Path] = None,
) -> tuple[Path, str, bool]:
    """解析用户输入，确定目标会话目录和模式。"""
    stripped = raw_input.strip()
    if not stripped:
        raise ValueError("input is empty")
    # 支持终端输入中的字面量 \n
    expanded = raw_input.replace("\\n", "\n")
    expanded_stripped = expanded.strip()
    matched = SHORT_CODE_MESSAGE_PATTERN.match(expanded_stripped)
    if matched:
        code = matched.group(1).upper()
        message = matched.group(2).strip()
        if not message:
            raise ValueError("uid is provided but message is empty")
        uid_dir = resolve_short_code_dir(code, base_dir=uid_base_dir)
        return uid_dir, message, True
    if SHORT_CODE_PATTERN.fullmatch(expanded_stripped):
        raise ValueError(
            "uid detected without message, use "
            "'UID\\nmessage' or 'UID, message' or 'UID. message'"
        )
    resolved_temp_dir = TEMP_DIR if temp_dir is None else temp_dir
    resolved_temp_dir.mkdir(parents=True, exist_ok=True)
    return resolved_temp_dir, expanded_stripped, False


def load_or_create_session_id(target_dir: Path) -> tuple[str, bool]:
    """加载或创建 session_id。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    session_file = target_dir / "session_id.txt"
    if session_file.exists():
        sid = session_file.read_text(encoding="utf-8").strip()
        try:
            uuid.UUID(sid)
            return sid, False
        except ValueError as exc:
            raise ValueError(f"invalid session id in {session_file}: {sid!r}") from exc
    sid = str(uuid.uuid4())
    session_file.write_text(sid, encoding="utf-8")
    return sid, True


def load_recent_turns(memory_file: Path, max_turns: int = MAX_CONTEXT_TURNS) -> list[str]:
    """从 memory.md 加载最近的对话轮次。"""
    if not memory_file.exists():
        return []
    content = memory_file.read_text(encoding="utf-8").strip()
    if not content:
        return []
    turns = [chunk.strip() for chunk in content.split(TURN_SEPARATOR) if chunk.strip()]
    return turns[-max_turns:]


def build_prompt(message: str, memory_file: Path) -> str:
    """构建带有历史上下文的提示词（临时模式）。"""
    history = load_recent_turns(memory_file)
    if not history:
        return message
    selected_turns: list[str] = []
    total_chars = 0
    for turn in reversed(history):
        turn_len = len(turn)
        if turn_len > MAX_CONTEXT_CHARS:
            continue
        projected = total_chars + turn_len + (2 if selected_turns else 0)
        if projected > MAX_CONTEXT_CHARS:
            break
        selected_turns.append(turn)
        total_chars = projected
    selected_turns.reverse()
    if not selected_turns:
        return message
    history_text = "\n\n".join(selected_turns)
    return (
        "Use the following recent conversation as context. "
        "Keep answers concise and continue naturally.\n\n"
        f"{history_text}\n\n"
        "Current user message:\n"
        f"{message}"
    )


def is_known_claude_error_output(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped in KNOWN_CLAUDE_ERROR_OUTPUTS:
        return True
    return stripped.startswith(f"{SESSION_ID_NOT_FOUND_OUTPUT}\n")


def _build_claude_result(
    *,
    ok: bool,
    text: str,
    error_type: Optional[str] = None,
    stderr: str = "",
    return_code: Optional[int] = None,
) -> ClaudeCallResult:
    return ClaudeCallResult(
        ok=ok,
        text=text.strip(),
        error_type=error_type,
        stderr=stderr.strip(),
        return_code=return_code,
    )


def _run_claude_prompt(
    prompt: str,
    session_id: Optional[str] = None,
    resume_session: bool = False,
    on_wait: Optional[Callable[[float], None]] = None,
) -> str:
    """统一调用 Claude CLI，兼容旧调用方仅返回文本。"""
    return _run_claude_prompt_result(
        prompt,
        session_id=session_id,
        resume_session=resume_session,
        on_wait=on_wait,
    ).text


def _run_claude_prompt_result(
    prompt: str,
    session_id: Optional[str] = None,
    resume_session: bool = False,
    on_wait: Optional[Callable[[float], None]] = None,
) -> ClaudeCallResult:
    """统一调用 Claude CLI，返回带状态的结果对象。"""
    cmd = ["claude", "-p"]
    if session_id:
        if resume_session:
            cmd.extend(["-r", session_id])
        else:
            cmd.extend(["--session-id", session_id])
    cmd.extend(
        [
            "--dangerously-skip-permissions",
            "--permission-mode",
            "bypassPermissions",
        ]
    )
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=PROJECT_DIR,
        )
    except FileNotFoundError:
        return _build_claude_result(
            ok=False,
            text=CLAUDE_CLI_NOT_FOUND_OUTPUT,
            error_type="cli_not_found",
        )
    started_at = time.monotonic()
    pending_input: Optional[str] = prompt
    while True:
        try:
            stdout_text, stderr_text = proc.communicate(input=pending_input, timeout=1)
            break
        except subprocess.TimeoutExpired:
            pending_input = None
            elapsed_s = time.monotonic() - started_at
            if elapsed_s >= CLAUDE_CALL_TIMEOUT_S:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return _build_claude_result(
                    ok=False,
                    text=CLAUDE_CALL_TIMEOUT_OUTPUT,
                    error_type="timeout",
                )
            if on_wait is not None:
                try:
                    on_wait(elapsed_s)
                except Exception:
                    proc.terminate()
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate()
                    raise
    if proc.returncode != 0:
        stderr_text = stderr_text.strip()
        stdout_text = stdout_text.strip()
        debug_text = stderr_text or stdout_text
        if SESSION_ID_NOT_FOUND_MARKER in stderr_text:
            return _build_claude_result(
                ok=False,
                text=SESSION_ID_NOT_FOUND_OUTPUT,
                error_type="session_not_found",
                stderr=debug_text,
                return_code=proc.returncode,
            )
        stderr_lower = stderr_text.lower()
        if "rate limit" in stderr_lower or "too many requests" in stderr_lower:
            return _build_claude_result(
                ok=False,
                text=CLAUDE_RATE_LIMIT_OUTPUT,
                error_type="rate_limit",
                stderr=debug_text,
                return_code=proc.returncode,
            )
        if (
            "unauthorized" in stderr_lower
            or "authentication" in stderr_lower
            or "auth" in stderr_lower
            or "login" in stderr_lower
        ):
            return _build_claude_result(
                ok=False,
                text=CLAUDE_AUTH_ERROR_OUTPUT,
                error_type="auth",
                stderr=debug_text,
                return_code=proc.returncode,
            )
        return _build_claude_result(
            ok=False,
            text=CLAUDE_GENERIC_ERROR_OUTPUT,
            error_type="generic_error",
            stderr=debug_text,
            return_code=proc.returncode,
        )
    return _build_claude_result(ok=True, text=stdout_text)


def ask_claude(message: str, target_dir: Path, use_session_resume: bool) -> str:
    """调用 Claude CLI 获取回复。"""
    return ask_claude_with_callback(message, target_dir, use_session_resume)


def ask_claude_with_callback(
    message: str,
    target_dir: Path,
    use_session_resume: bool,
    on_wait: Optional[Callable[[float], None]] = None,
) -> str:
    """调用 Claude CLI 获取回复，并在等待期间回调。"""
    return ask_claude_result_with_callback(
        message,
        target_dir,
        use_session_resume,
        on_wait=on_wait,
    ).text


def ask_claude_result_with_callback(
    message: str,
    target_dir: Path,
    use_session_resume: bool,
    on_wait: Optional[Callable[[float], None]] = None,
) -> ClaudeCallResult:
    """调用 Claude CLI 获取回复，并返回带状态的结果对象。"""
    memory_file = target_dir / "memory.md"
    if use_session_resume:
        try:
            sid, is_new_session = load_or_create_session_id(target_dir)
        except ValueError:
            return _build_claude_result(
                ok=False,
                text=SESSION_ID_INVALID_OUTPUT,
                error_type="session_invalid",
            )
        output = _run_claude_prompt_result(
            message,
            session_id=sid,
            resume_session=(not is_new_session),
            on_wait=on_wait,
        )
        if output.error_type == "session_not_found":
            return _build_claude_result(
                ok=False,
                text=f"{SESSION_ID_NOT_FOUND_OUTPUT}\nmemory文件: {memory_file}",
                error_type=output.error_type,
                stderr=output.stderr,
                return_code=output.return_code,
            )
        return output
    prompt = build_prompt(message, memory_file)
    return _run_claude_prompt_result(prompt, on_wait=on_wait)


def append_memory(memory_file: Path, user_text: str, assistant_text: str) -> None:
    """追加对话记录到 memory.md。"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sanitized_user_text = _sanitize_memory_text(user_text)
    sanitized_assistant_text = _sanitize_memory_text(assistant_text)
    block = (
        f"## {timestamp}\n"
        "### user\n"
        f"{sanitized_user_text}\n\n"
        "### assistant\n"
        f"{sanitized_assistant_text}\n"
        f"{TURN_SEPARATOR}"
    )
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    with memory_file.open("a", encoding="utf-8") as f:
        f.write(block)


def _sanitize_memory_text(text: str, *, max_line_chars: int = 1000) -> str:
    sanitized = DATA_URL_PATTERN.sub("[embedded-binary omitted]", text or "")
    sanitized = BASE64_BLOB_PATTERN.sub("[embedded-binary omitted]", sanitized)
    lines: list[str] = []
    for line in sanitized.splitlines():
        if len(line) <= max_line_chars:
            lines.append(line)
            continue
        omitted = len(line) - max_line_chars
        lines.append(f"{line[:max_line_chars]}...[truncated {omitted} chars]")
    return "\n".join(lines)


def build_runtime_prompt() -> str:
    """构建简洁的运行环境提示。"""
    current_time = time.strftime("%Y-%m-%d %H:%M:%S (UTC+8)", time.localtime())
    return (
        f"当前时间：{current_time}\n"
        "最终输出协议:\n"
        "1. 不要出现markdown标识符(如#*等)\n"
        "2. 如果你成功生成或编辑了图片，最终回复中必须为每张图片单独输出一行 "
        "FILE: /绝对路径\n"
        "3. 如果有本机生成文件，优先使用本机生成文件的绝对路径\n"
        "4. 如果要发送普通文件，单独一行写 FILE: /绝对路径/或https://url\n"
    )


def main() -> int:
    """命令行入口：读取标准输入，调用 Claude，输出结果。"""
    ensure_base_dirs()
    while True:
        try:
            i = input()
        except EOFError:
            return 0
        except KeyboardInterrupt:
            print()
            return 0
        if not i.strip():
            continue
        try:
            target_dir, message, use_session_resume = resolve_target(i)
            prompt = build_runtime_prompt()
            o = ask_claude(f"{message}\n\n{prompt}", target_dir, use_session_resume)
            append_memory(target_dir / "memory.md", message, o)
            print(o)
        except Exception as exc:
            print(f"[error] {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
