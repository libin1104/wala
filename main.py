#!/usr/bin/env python3

from claude_io_utlities import (
    resolve_target,
    ask_claude,
    append_memory
)
from wechat_auto_utlities import (
    load_wechat_config,
    capture_region_image,
    compute_diff_ratio,
    copy_dialog_to_clipboard,
    paste_from_clipboard_and_send,
    capture_and_send,
)
from wechat_coord_gui import selector
import time
import pyautogui
import re


# 默认配置文件路径
DEFAULT_CONFIG_PATH = "wechat_info.config"
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
UID_ONLY_MESSAGE = "uid detected without message, use 'UID, message'"
# 截屏触发词
SCREENSHOT_TRIGGER = "截屏"


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



def main(
    config_path: str = DEFAULT_CONFIG_PATH,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    diff_ratio_threshold: float = DEFAULT_DIFF_RATIO_THRESHOLD,
    pixel_threshold: float = DEFAULT_PIXEL_THRESHOLD,
    corner_zone_px: int = DEFAULT_EXIT_CORNER_ZONE_PX,
) -> None:
    """监控新对话变化并复制内容输出给claude code。
    Args:
        config_path: 配置文件路径
        poll_interval_s: 轮询间隔（秒）
        diff_ratio_threshold: 差异比率阈值
    """
    try:
        selector()
    except:
        ...
    cfg = load_wechat_config(config_path)
    print("Start monitoring for new dialog changes.")
    print("Press Ctrl+C to stop, or move mouse to left-top/right-top corner to exit.")
    dialog_img_prev=capture_region_image(cfg)
    try:
        while True:
            if _mouse_in_exit_corner(corner_zone_px):
                print("\nStopped by mouse corner exit.")
                break
            dialog_img_curr=capture_region_image(cfg)
            diff = compute_diff_ratio(dialog_img_curr, dialog_img_prev, 
                     pixel_threshold=pixel_threshold)
            changed = (diff >= diff_ratio_threshold)
            if changed:
                i = copy_dialog_to_clipboard(cfg)
                print(f"    [Q]:{i}")
                stripped_i = i.strip()
                should_send_text = True
                if stripped_i == SCREENSHOT_TRIGGER:
                    capture_and_send(cfg)
                    o = "screenshot captured and sent"
                    should_send_text = False
                elif UID_ONLY_PATTERN.fullmatch(stripped_i):
                    o = UID_ONLY_MESSAGE
                else:
                    target_dir, message, use_session_resume = resolve_target(i)
                    o = ask_claude(message, target_dir, use_session_resume)
                    append_memory(target_dir / "memory.md", i, o)
                print(f"    [A]:{o}")
                if should_send_text:
                    paste_from_clipboard_and_send(cfg, o)
                dialog_img_prev=capture_region_image(cfg)
            if _interruptible_wait(
                total_wait_s=poll_interval_s,
                step_s=DEFAULT_WAIT_STEP_S,
                corner_zone_px=corner_zone_px,
            ):
                print("\nStopped by mouse corner exit.")
                break
    except KeyboardInterrupt:
        print("\nStopped.")
    except pyautogui.FailSafeException:
        print("\nStopped by pyautogui failsafe.")
        
if __name__ == "__main__":
    main()
