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
import uuid
from datetime import datetime
from pathlib import Path
# 会话根目录
ROOT_DIR = Path.home() / ".claude_sessions"
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
# 对话轮次分隔符
TURN_SEPARATOR = "\n---\n"
# 临时模式最大上下文轮次
MAX_CONTEXT_TURNS = 20
# Claude 会话恢复失败错误标记
SESSION_ID_NOT_FOUND_MARKER = "No conversation found with session ID"
# 会话恢复失败时返回给调用方的固定提示
SESSION_ID_NOT_FOUND_OUTPUT = "ID检索失败，请更换ID"
# Claude CLI 超时时间（秒）
CLAUDE_CALL_TIMEOUT_S = 90
# 统一错误输出文案
SESSION_ID_INVALID_OUTPUT = "ID无效，请更换ID"
CLAUDE_CLI_NOT_FOUND_OUTPUT = "Claude CLI不可用，请检查安装与PATH"
CLAUDE_CALL_TIMEOUT_OUTPUT = "Claude响应超时，请稍后重试"
CLAUDE_RATE_LIMIT_OUTPUT = "Claude请求过于频繁，请稍后重试"
CLAUDE_AUTH_ERROR_OUTPUT = "Claude鉴权失败，请重新登录"
CLAUDE_GENERIC_ERROR_OUTPUT = "Claude调用失败，请稍后重试"

def ensure_base_dirs() -> None:
    """确保基础目录结构存在。"""
    UID_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

def resolve_short_code_dir(short_code: str) -> Path:
    """解析短码会话目录（目录名直接使用 4 位短码大写）。"""
    code = short_code.upper()
    code_dir = UID_DIR / code
    code_dir.mkdir(parents=True, exist_ok=True)
    return code_dir

def resolve_target(raw_input: str) -> tuple[Path, str, bool]:
    """解析用户输入，确定目标会话目录和模式。
    输入格式：
    - "普通消息" → 临时模式
    - "{CODE}{\n|, |. |，|。}{消息内容}" → UID 持久化模式
    Args:
        raw_input: 用户输入的原始文本
    Returns:
        (目标目录, 消息内容, 是否使用 session 恢复)
    Raises:
        ValueError: 输入为空或格式错误
    """
    stripped = raw_input.strip()
    if not stripped:
        raise ValueError("input is empty")
    # 支持终端输入中的字面量 \n
    expanded = raw_input.replace("\\n", "\n")
    expanded_stripped = expanded.strip()
    # UID 模式：短码 + 分隔符 + 消息
    matched = SHORT_CODE_MESSAGE_PATTERN.match(expanded_stripped)
    if matched:
        code = matched.group(1).upper()
        message = matched.group(2).strip()
        if not message:
            raise ValueError("uid is provided but message is empty")
        uid_dir = resolve_short_code_dir(code)
        # UID 模式：使用持久化 Claude session
        return uid_dir, message, True
    # 仅有短码但没有消息
    if SHORT_CODE_PATTERN.fullmatch(expanded_stripped):
        raise ValueError(
            "uid detected without message, use "
            "'UID\\nmessage' or 'UID, message' or 'UID. message'"
        )
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    # 临时模式：不使用 session-id 恢复，依赖注入的最近历史
    return TEMP_DIR, expanded_stripped, False

def load_or_create_session_id(target_dir: Path) -> tuple[str, bool]:
    """加载或创建 session_id。
    Args:
        target_dir: 目标会话目录
    Returns:
        (session_id, 是否为新创建的 session)
    Raises:
        ValueError: session_id 文件内容无效
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    session_file = target_dir / "session_id.txt"
    if session_file.exists():
        sid = session_file.read_text(encoding="utf-8").strip()
        try:
            uuid.UUID(sid)
            return sid, False
        except ValueError:
            raise ValueError(f"invalid session id in {session_file}: {sid!r}")
    sid = str(uuid.uuid4())
    session_file.write_text(sid, encoding="utf-8")
    return sid, True

def load_recent_turns(memory_file: Path, max_turns: int = MAX_CONTEXT_TURNS) -> list[str]:
    """从 memory.md 加载最近的对话轮次。
    Args:
        memory_file: memory.md 文件路径
        max_turns: 最大加载轮次数
    Returns:
        对话轮次列表（每轮是一个字符串块）
    """
    if not memory_file.exists():
        return []
    content = memory_file.read_text(encoding="utf-8").strip()
    if not content:
        return []
    turns = [chunk.strip() for chunk in content.split(TURN_SEPARATOR) if chunk.strip()]
    return turns[-max_turns:]

def build_prompt(message: str, memory_file: Path) -> str:
    """构建带有历史上下文的提示词（临时模式）。
    Args:
        message: 用户当前消息
        memory_file: memory.md 文件路径
    Returns:
        构建好的完整提示词
    """
    history = load_recent_turns(memory_file)
    if not history:
        return message
    history_text = "\n\n".join(history)
    return (
        "Use the following recent conversation as context. "
        "Keep answers concise and continue naturally.\n\n"
        f"{history_text}\n\n"
        "将使用纯文本展示，减少markdown标识符的使用"
        "Current user message:\n"
        f"{message}"
    )

def ask_claude(message: str, target_dir: Path, use_session_resume: bool) -> str:
    """调用 Claude CLI 获取回复。
    Args:
        message: 用户消息
        target_dir: 目标会话目录
        use_session_resume: 是否使用 session 恢复模式
    Returns:
        Claude 的回复文本
    """
    memory_file = target_dir / "memory.md"
    cmd = ["claude", "-p"]
    if use_session_resume:
        # UID 持久化模式：使用 --session-id 或 -r
        try:
            sid, is_new_session = load_or_create_session_id(target_dir)
        except ValueError:
            return SESSION_ID_INVALID_OUTPUT
        if is_new_session:
            cmd.extend(["--session-id", sid])
        else:
            cmd.extend(["-r", sid])
        prompt = message
    else:
        # 临时模式：注入历史上下文
        prompt = build_prompt(message, memory_file)
    cmd.extend(
        [
            "--dangerously-skip-permissions",
            "--permission-mode",
            "bypassPermissions",
        ]
    )
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=CLAUDE_CALL_TIMEOUT_S,
        )
    except FileNotFoundError:
        return CLAUDE_CLI_NOT_FOUND_OUTPUT
    except subprocess.TimeoutExpired:
        return CLAUDE_CALL_TIMEOUT_OUTPUT
    if proc.returncode != 0:
        stderr_text = proc.stderr.strip()
        if SESSION_ID_NOT_FOUND_MARKER in stderr_text:
            return f"{SESSION_ID_NOT_FOUND_OUTPUT}\nmemory文件: {memory_file}"
        stderr_lower = stderr_text.lower()
        if "rate limit" in stderr_lower or "too many requests" in stderr_lower:
            return CLAUDE_RATE_LIMIT_OUTPUT
        if (
            "unauthorized" in stderr_lower
            or "authentication" in stderr_lower
            or "auth" in stderr_lower
            or "login" in stderr_lower
        ):
            return CLAUDE_AUTH_ERROR_OUTPUT
        return CLAUDE_GENERIC_ERROR_OUTPUT
    return proc.stdout.strip()

def append_memory(memory_file: Path, user_text: str, assistant_text: str) -> None:
    """追加对话记录到 memory.md。
    Args:
        memory_file: memory.md 文件路径
        user_text: 用户输入
        assistant_text: Claude 回复
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    block = (
        f"## {timestamp}\n"
        "### user\n"
        f"{user_text}\n\n"
        "### assistant\n"
        f"{assistant_text}\n"
        f"{TURN_SEPARATOR}"
    )
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    with memory_file.open("a", encoding="utf-8") as f:
        f.write(block)

def main() -> int:
    """命令行入口：读取标准输入，调用 Claude，输出结果。
    用法：
        echo "你好" | python claude_io_utlities.py
        echo "ABCD\\n你好" | python claude_io_utlities.py
        echo "ABCD, 你好" | python claude_io_utlities.py
        echo "ABCD。你好" | python claude_io_utlities.py
    Returns:
        退出码
    """
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
            o = ask_claude(message, target_dir, use_session_resume)
            append_memory(target_dir / "memory.md", i, o)
            print(o)
        except Exception as exc:
            print(f"[error] {exc}", file=sys.stderr)

if __name__ == "__main__":
    raise SystemExit(main())
