#!/usr/bin/env python3

from claude_io_utlities import (
    ROOT_DIR,
    append_memory,
    ask_claude,
    resolve_target,
)
from wechat_auto_utlities import (
    capture_and_send,
    capture_region_image,
    compute_diff_ratio,
    copy_dialog_to_clipboard,
    load_wechat_config,
    paste_from_clipboard_and_send,
)
from wechat_coord_gui import selector
import pyautogui
import re
import time


# 默认配置文件路径
DEFAULT_CONFIG_PATH = ROOT_DIR / "wechat_info.config"
# 默认轮询间隔（秒）
DEFAULT_POLL_INTERVAL_S = 1.0
# 默认差异比率阈值
DEFAULT_DIFF_RATIO_THRESHOLD = 0.015
# 默认像素阈值
DEFAULT_PIXEL_THRESHOLD = 20
# 触发退出的角落像素范围（左上角/右上角）
DEFAULT_EXIT_CORNER_ZONE_PX = 10
# 轮询等待步进（秒）
DEFAULT_WAIT_STEP_S = 0.05
# 仅 UID（无消息）输入检测
UID_ONLY_PATTERN = re.compile(r"^[A-Za-z0-9]{4}$")
# 仅 UID 时回传提示
UID_ONLY_MESSAGE = "ID格式错误：仅输入了UID，请使用 'UID, message'"
# 截屏触发词
SCREENSHOT_TRIGGER = "截屏"
# 初始化失败提示
INIT_ERROR_PREFIX = "初始化失败"


def _mouse_in_exit_corner(corner_zone_px: int = DEFAULT_EXIT_CORNER_ZONE_PX) -> bool:
    """检测鼠标是否位于左上角或右上角退出区域。"""
    if corner_zone_px <= 0:
        return False
    x, y = pyautogui.position()
    screen_width, _ = pyautogui.size()
    in_left_top = (x < corner_zone_px and y < corner_zone_px)
    in_right_top = (x >= screen_width - corner_zone_px and y < corner_zone_px)
    return in_left_top or in_right_top


def _interruptible_wait(
    total_wait_s: float,
    step_s: float = DEFAULT_WAIT_STEP_S,
    corner_zone_px: int = DEFAULT_EXIT_CORNER_ZONE_PX,
) -> bool:
    """细粒度等待，期间如果鼠标进入退出角落则立即返回 True。"""
    if total_wait_s <= 0:
        return _mouse_in_exit_corner(corner_zone_px)
    deadline = time.monotonic() + total_wait_s
    while True:
        if _mouse_in_exit_corner(corner_zone_px):
            return True
        remain = deadline - time.monotonic()
        if remain <= 0:
            return False
        time.sleep(min(step_s, remain))


def _format_runtime_error(exc: Exception) -> str:
    """把异常转换为可直接回传给微信的明确文案。"""
    if isinstance(exc, FileNotFoundError):
        return "配置或系统资源不存在，请检查路径与安装状态"
    if isinstance(exc, PermissionError):
        return "权限不足，请检查系统辅助功能与文件权限"
    text = str(exc).strip()
    if not text:
        text = exc.__class__.__name__
    return f"执行失败：{text}"


def _safe_send_text(cfg, text: str) -> None:
    """尽力发送文本，发送失败时仅记录错误，不中断主流程。"""
    try:
        paste_from_clipboard_and_send(cfg, text)
    except Exception as send_exc:
        print(f"  [error]: 发送失败: {_format_runtime_error(send_exc)}")


def main(
    config_path: str = DEFAULT_CONFIG_PATH,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    diff_ratio_threshold: float = DEFAULT_DIFF_RATIO_THRESHOLD,
    pixel_threshold: float = DEFAULT_PIXEL_THRESHOLD,
    corner_zone_px: int = DEFAULT_EXIT_CORNER_ZONE_PX,
) -> None:
    """监控新对话变化并复制内容输出给claude code。"""
    try:
        selector()
    except Exception:
        ...
    print("Start monitoring for new dialog changes.")
    print("Press Ctrl+C to stop, or move mouse to left-top/right-top corner to exit.")

    cfg = None
    dialog_img_prev = None
    while True:
        if _mouse_in_exit_corner(corner_zone_px):
            print("\nStopped by mouse corner exit.")
            return
        try:
            cfg = load_wechat_config(config_path)
            dialog_img_prev = capture_region_image(cfg)
            break
        except Exception as exc:
            o = f"{INIT_ERROR_PREFIX}：{_format_runtime_error(exc)}"
            print(f"[error]: {o}")
            if cfg is not None:
                _safe_send_text(cfg, o)
            if _interruptible_wait(
                total_wait_s=max(poll_interval_s, DEFAULT_WAIT_STEP_S),
                step_s=DEFAULT_WAIT_STEP_S,
                corner_zone_px=corner_zone_px,
            ):
                print("\nStopped by mouse corner exit.")
                return

    while True:
        if _mouse_in_exit_corner(corner_zone_px):
            print("\nStopped by mouse corner exit.")
            break
        try:
            dialog_img_curr = capture_region_image(cfg)
            diff = compute_diff_ratio(
                dialog_img_curr,
                dialog_img_prev,
                pixel_threshold=pixel_threshold,
            )
            changed = (diff >= diff_ratio_threshold)
            if changed:
                i = copy_dialog_to_clipboard(cfg)
                print(f"  [Q]:{i}")
                stripped_i = i.strip()
                target_name = "temp"
                should_send_text = True
                if stripped_i == SCREENSHOT_TRIGGER:
                    capture_and_send(cfg)
                    o = "screenshot captured and sent"
                    should_send_text = False
                elif UID_ONLY_PATTERN.fullmatch(stripped_i):
                    o = UID_ONLY_MESSAGE
                else:
                    target_dir, message, use_session_resume = resolve_target(i)
                    target_name = target_dir.name
                    o = ask_claude(message, target_dir, use_session_resume)
                    append_memory(target_dir / "memory.md", message, o)
                print(f"  [A]:{o.replace(chr(10), chr(10) + '    ')}")
                if should_send_text:
                    loc = f"[{target_name}]:\n" if (target_name != "temp") else ""
                    _safe_send_text(cfg, loc + o)
                dialog_img_prev = capture_region_image(cfg)
            if _interruptible_wait(
                total_wait_s=poll_interval_s,
                step_s=DEFAULT_WAIT_STEP_S,
                corner_zone_px=corner_zone_px,
            ):
                print("\nStopped by mouse corner exit.")
                break
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except pyautogui.FailSafeException:
            print("\nStopped by pyautogui failsafe.")
            break
        except Exception as exc:
            o = _format_runtime_error(exc)
            print(f"  [A]:{o}")
            _safe_send_text(cfg, o)
            try:
                dialog_img_prev = capture_region_image(cfg)
            except Exception:
                pass
            if _interruptible_wait(
                total_wait_s=max(poll_interval_s, DEFAULT_WAIT_STEP_S),
                step_s=DEFAULT_WAIT_STEP_S,
                corner_zone_px=corner_zone_px,
            ):
                print("\nStopped by mouse corner exit.")
                break


if __name__ == "__main__":
    main()
