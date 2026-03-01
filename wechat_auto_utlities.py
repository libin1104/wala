#!/usr/bin/env python3
"""基于 wechat_info.config 的微信自动化辅助工具。
主要功能包括：
1) 通过截图像素差异比率检测新对话
2) 复制对话内容到剪贴板
3) 粘贴剪贴板内容并发送
4) 截取指定区域并通过微信截图流程发送
"""
from __future__ import annotations
import configparser
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import pyautogui
import pyperclip
from PIL import Image, ImageChops, ImageOps
# Point: 表示屏幕上的一个点坐标 (x, y)
Point = Tuple[int, int]
# Region: 表示屏幕上的一个矩形区域 (left, top, width, height)
Region = Tuple[int, int, int, int]
# 默认配置文件路径
DEFAULT_CONFIG_PATH = "wechat_info.config"
# 默认轮询间隔（秒）
DEFAULT_POLL_INTERVAL_S = 1.0
# 默认差异比率阈值
DEFAULT_DIFF_RATIO_THRESHOLD = 0.015
# 默认像素阈值
DEFAULT_PIXEL_THRESHOLD = 20
# 默认点击间隔（秒）
DEFAULT_CLICK_INTERVAL_S = 0.15
# 自适应降采样：目标像素预算（超过后会缩小再做diff）
DEFAULT_DIFF_TARGET_PIXELS = 400_000
# 自适应降采样：最小缩放比例
DEFAULT_DIFF_MIN_SCALE = 0.25
# 微信单条消息最大字符数
DEFAULT_MAX_CHARS_PER_MESSAGE = 2048
# 分批发送时，每条之间的最小等待
DEFAULT_CHUNK_INTERVAL_S = 0.08

def _parse_point(raw: str, field_name: str) -> Point:
    """解析坐标点字符串为元组。
    Args:
        raw: 格式为 "x,y" 的坐标字符串
        field_name: 字段名称（用于错误提示）
    Returns:
        包含 x, y 坐标的元组
    Raises:
        ValueError: 当格式无效或坐标值无效时
    """
    if "," not in raw:
        raise ValueError(f"invalid point format for {field_name!r}: {raw!r}")
    left, right = raw.split(",", 1)
    try:
        return int(left.strip()), int(right.strip())
    except ValueError as exc:
        raise ValueError(f"invalid point value for {field_name!r}: {raw!r}") from exc

def _read_point(parser: configparser.ConfigParser, section: str, option: str) -> Point:
    """从配置解析器中读取坐标点。
    Args:
        parser: ConfigParser 实例
        section: 配置节名称
        option: 配置项名称
    Returns:
        包含 x, y 坐标的元组
    Raises:
        ValueError: 当节或选项不存在时
    """
    if not parser.has_section(section):
        raise ValueError(f"missing section: [{section}]")
    if not parser.has_option(section, option):
        raise ValueError(f"missing option: [{section}] {option}")
    raw = parser.get(section, option).strip()
    return _parse_point(raw, f"{section}.{option}")

def load_wechat_config(config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Dict[str, Any]]:
    """加载微信配置文件。
    Args:
        config_path: 配置文件路径
    Returns:
        包含所有配置信息的字典，包括：
        - screenshot: 截图区域配置
        - copy: 复制操作的位置配置
        - paste: 粘贴操作的位置配置
        - meta: 元数据（如配置文件路径）
    Raises:
        FileNotFoundError: 配置文件不存在时
        ValueError: 配置格式无效或区域定义无效时
    """
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    # 读取截图区域的左上角和右下角坐标
    left_top = _read_point(parser, "screenshot", "left_top")
    right_bottom = _read_point(parser, "screenshot", "right_bottom")
    width = right_bottom[0] - left_top[0]
    height = right_bottom[1] - left_top[1]
    if width <= 0 or height <= 0:
        raise ValueError(
            "invalid screenshot region: right_bottom must be greater than left_top"
        )
    cfg: Dict[str, Dict[str, Any]] = {
        "screenshot": {
            "left_top": left_top,
            "right_bottom": right_bottom,
            "region": (left_top[0], left_top[1], width, height),
        },
        "copy": {
            "dialog_pos": _read_point(parser, "copy", "dialog_pos"),
            "button_pos": _read_point(parser, "copy", "button_pos"),
        },
        "paste": {
            "input_pos": _read_point(parser, "paste", "input_pos"),
            "button_pos": _read_point(parser, "paste", "button_pos"),
        },
    }
    cfg["meta"] = {"config_path": str(path)}
    return cfg

def capture_region_image(cfg: Dict[str, Dict[str, Any]]) -> Image.Image:
    """截取配置的屏幕区域图像。
    Args:
        cfg: 配置字典
    Returns:
        截取的 PIL Image 对象
    """
    region: Region = cfg["screenshot"]["region"]
    return pyautogui.screenshot(region=region)

def compute_diff_ratio(
    img_prev: Image.Image,
    img_curr: Image.Image,
    pixel_threshold: int = DEFAULT_PIXEL_THRESHOLD,
    adaptive_downsample: bool = True,
    diff_target_pixels: int = DEFAULT_DIFF_TARGET_PIXELS,
    diff_min_scale: float = DEFAULT_DIFF_MIN_SCALE,
) -> float:
    """计算两张图像之间的差异比率。
    Args:
        img_prev: 之前的图像
        img_curr: 当前的图像
        pixel_threshold: 像素差异阈值（1-255），只有差异大于此值的像素才被计入
        adaptive_downsample: 是否根据截图区域大小自动降采样
        diff_target_pixels: 降采样目标像素预算（总像素超过时触发）
        diff_min_scale: 降采样最小缩放比例（防止缩得过小）
    Returns:
        差异比率（0.0-1.0），表示发生变化像素占总像素的比例
    Raises:
        ValueError: 当参数无效或图像尺寸不匹配时
    """
    if pixel_threshold < 1 or pixel_threshold > 255:
        raise ValueError("pixel_threshold must be in [1, 255]")
    if img_prev.size != img_curr.size:
        raise ValueError("image size mismatch for diff computation")
    if diff_target_pixels <= 0:
        raise ValueError("diff_target_pixels must be > 0")
    if diff_min_scale <= 0 or diff_min_scale > 1:
        raise ValueError("diff_min_scale must be in (0, 1]")

    # 转换为灰度图
    prev_gray = ImageOps.grayscale(img_prev)
    curr_gray = ImageOps.grayscale(img_curr)
    # 区域过大时自适应降采样，降低每轮计算负担
    if adaptive_downsample:
        width, height = prev_gray.size
        area = width * height
        if area > diff_target_pixels:
            scale = (diff_target_pixels / float(area)) ** 0.5
            scale = min(1.0, max(diff_min_scale, scale))
            if scale < 0.999:
                resized_w = max(1, int(round(width * scale)))
                resized_h = max(1, int(round(height * scale)))
                resized_size = (resized_w, resized_h)
                prev_gray = prev_gray.resize(resized_size, resample=Image.BILINEAR)
                curr_gray = curr_gray.resize(resized_size, resample=Image.BILINEAR)
    # 计算差异图
    diff = ImageChops.difference(prev_gray, curr_gray)
    # 获取差异直方图
    hist = diff.histogram()
    # 统计差异超过阈值的像素数量
    changed_pixels = sum(hist[pixel_threshold:])
    total_pixels = prev_gray.size[0] * prev_gray.size[1]
    if total_pixels <= 0:
        return 0.0
    return changed_pixels / float(total_pixels)

def has_new_dialog(
    cfg: Dict[str, Dict[str, Any]],
    state: Dict[str, Any],
    diff_ratio_threshold: float = DEFAULT_DIFF_RATIO_THRESHOLD,
    pixel_threshold: int = DEFAULT_PIXEL_THRESHOLD,
) -> bool:
    """检测是否有新对话（通过屏幕截图差异）。
    Args:
        cfg: 配置字典
        state: 状态字典，用于存储前一次的截图和检测信息
        diff_ratio_threshold: 差异比率阈值（0-1），超过此值视为有新对话
        pixel_threshold: 像素差异阈值（1-255）
    Returns:
        如果检测到新对话返回 True，否则返回 False
    Raises:
        ValueError: 当差异比率阈值无效时
    """
    if diff_ratio_threshold < 0 or diff_ratio_threshold > 1:
        raise ValueError("diff_ratio_threshold must be in [0, 1]")
    curr_img = capture_region_image(cfg)
    prev_img = state.get("prev_img")
    state["prev_img"] = curr_img
    # 首次运行，没有前一张图像
    if prev_img is None:
        state["last_diff_ratio"] = 0.0
        state["last_check_ts"] = time.time()
        return False
    ratio = compute_diff_ratio(prev_img, curr_img, pixel_threshold=pixel_threshold)
    state["last_diff_ratio"] = ratio
    state["last_check_ts"] = time.time()
    return ratio >= diff_ratio_threshold

def wait_for_new_dialog(
    cfg: Dict[str, Dict[str, Any]],
    timeout_s: float = 60.0,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    diff_ratio_threshold: float = DEFAULT_DIFF_RATIO_THRESHOLD,
    pixel_threshold: int = DEFAULT_PIXEL_THRESHOLD,
    state: Optional[Dict[str, Any]] = None,
) -> bool:
    """等待新对话出现（轮询检测）。
    Args:
        cfg: 配置字典
        timeout_s: 超时时间（秒），超过此时间返回 False
        poll_interval_s: 轮询间隔（秒）
        diff_ratio_threshold: 差异比率阈值
        pixel_threshold: 像素差异阈值
        state: 状态字典，如果为 None 则创建新字典
    Returns:
        检测到新对话返回 True，超时返回 False
    Raises:
        ValueError: 当超时时间或轮询间隔无效时
    """
    if timeout_s < 0:
        raise ValueError("timeout_s must be >= 0")
    if poll_interval_s <= 0:
        raise ValueError("poll_interval_s must be > 0")
    shared_state: Dict[str, Any] = state if state is not None else {}
    start = time.time()
    while True:
        if has_new_dialog(
            cfg,
            shared_state,
            diff_ratio_threshold=diff_ratio_threshold,
            pixel_threshold=pixel_threshold,
        ):
            return True
        if time.time() - start >= timeout_s:
            return False
        time.sleep(poll_interval_s)

def _click(point: Point, click_interval_s: float = DEFAULT_CLICK_INTERVAL_S) -> None:
    """在指定坐标点击并等待。
    Args:
        point: 要点击的坐标 (x, y)
        click_interval_s: 点击后等待的秒数
    """
    pyautogui.click(point[0], point[1])
    if click_interval_s > 0:
        time.sleep(click_interval_s)

def _r_click(point: Point, click_interval_s: float = DEFAULT_CLICK_INTERVAL_S) -> None:
    """在指定坐标右键点击并等待。
    Args:
        point: 要右键点击的坐标 (x, y)
        click_interval_s: 点击后等待的秒数
    """
    pyautogui.rightClick(point[0], point[1])
    if click_interval_s > 0:
        time.sleep(click_interval_s)

def _paste_hotkey() -> None:
    """执行系统粘贴快捷键。"""
    if sys.platform == "darwin":
        pyautogui.hotkey("command", "v")
    else:
        pyautogui.hotkey("ctrl", "v")

def _send() -> None:
    """执行enter快捷键。"""
    if sys.platform == "darwin":
        pyautogui.press('enter')
    else:
        pyautogui.press('enter')


def _chunk_text_with_prefix(
    text: str,
    max_chars_per_message: int = DEFAULT_MAX_CHARS_PER_MESSAGE,
) -> List[str]:
    """按微信单条上限分段，超长时使用 [i/n] 前缀。"""
    normalized_text = "" if text is None else str(text)
    if max_chars_per_message <= 0:
        raise ValueError("max_chars_per_message must be > 0")
    if len(normalized_text) <= max_chars_per_message:
        return [normalized_text]
    if max_chars_per_message <= len("[1/1] "):
        raise ValueError("max_chars_per_message is too small for chunk prefix")

    total_chunks = 1
    while True:
        chunks: List[str] = []
        cursor = 0
        chunk_index = 1
        text_len = len(normalized_text)
        while cursor < text_len:
            prefix = f"[{chunk_index}/{total_chunks}] "
            payload_limit = max_chars_per_message - len(prefix)
            if payload_limit <= 0:
                raise ValueError("max_chars_per_message is too small for chunk payload")
            next_cursor = min(text_len, cursor + payload_limit)
            chunks.append(prefix + normalized_text[cursor:next_cursor])
            cursor = next_cursor
            chunk_index += 1
        if len(chunks) == total_chunks:
            return chunks
        total_chunks = len(chunks)

def copy_dialog_to_clipboard(
    cfg: Dict[str, Dict[str, Any]],
    click_interval_s: float = DEFAULT_CLICK_INTERVAL_S,
) -> str:
    """复制微信对话内容到剪贴板。
    Args:
        cfg: 配置字典
        click_interval_s: 点击间隔（秒）
    Returns:
        复制的内容字符串，如果为空则返回空字符串
    """
    _r_click(cfg["copy"]["dialog_pos"], click_interval_s)
    _click(cfg["copy"]["button_pos"], click_interval_s)
    time.sleep(max(0.05, click_interval_s))
    content = pyperclip.paste()
    pyautogui.moveTo(cfg["paste"]["input_pos"])
    return "" if content is None else str(content)

def paste_from_clipboard_and_send(
    cfg: Dict[str, Dict[str, Any]],
    text: Optional[str] = None,
    pre_click: bool = True,
    click_interval_s: float = DEFAULT_CLICK_INTERVAL_S,
    max_chars_per_message: int = DEFAULT_MAX_CHARS_PER_MESSAGE,
    chunk_interval_s: float = DEFAULT_CHUNK_INTERVAL_S,
) -> None:
    """从剪贴板粘贴内容并发送。
    Args:
        cfg: 配置字典
        text: 要发送的文本，如果为 None 则使用当前剪贴板内容
        pre_click: 是否在粘贴前点击输入框（聚焦）
        click_interval_s: 点击间隔（秒）
        max_chars_per_message: 单条消息允许的最大字符数（含分段前缀）
        chunk_interval_s: 分段发送时，每条之间的间隔（秒）
    """
    content = pyperclip.paste() if text is None else text
    messages = _chunk_text_with_prefix(
        text="" if content is None else str(content),
        max_chars_per_message=max_chars_per_message,
    )
    if pre_click:
        _click(cfg["paste"]["input_pos"], click_interval_s)
    for idx, message in enumerate(messages):
        pyperclip.copy(message)
        #_r_click(cfg["paste"]["input_pos"], click_interval_s)
        #_click(cfg["paste"]["button_pos"], click_interval_s)
        _paste_hotkey()
        time.sleep(max(0.05, click_interval_s))
        _send()
        if idx < len(messages) - 1 and chunk_interval_s > 0:
            time.sleep(chunk_interval_s)

def capture_and_send(
    cfg: Dict[str, Dict[str, Any]],
    auto_send: bool = True,
    click_interval_s: float = DEFAULT_CLICK_INTERVAL_S,
) -> str:
    """截取全屏到剪贴板并通过微信发送。
    Args:
        cfg: 配置字典
        auto_send: 是否自动发送（按回车）
        click_interval_s: 点击间隔（秒）
    Returns:
        空字符串（不保存到本地）
    """
    # 截图到剪贴板
    if sys.platform == "darwin":
        pyautogui.hotkey("ctrl", "command", "shift", "3")
    else:
        pyautogui.press("printscreen")
    time.sleep(max(0.1, click_interval_s))

    # 聚焦输入框
    _click(cfg["paste"]["input_pos"], click_interval_s)

    # 粘贴发送截图
    _paste_hotkey()
    time.sleep(max(0.1, click_interval_s))
    _send()
    return ""

def demo_loop(
    config_path: str = DEFAULT_CONFIG_PATH,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    diff_ratio_threshold: float = DEFAULT_DIFF_RATIO_THRESHOLD,
) -> None:
    """演示循环：监控新对话变化并自动复制内容。
    Args:
        config_path: 配置文件路径
        poll_interval_s: 轮询间隔（秒）
        diff_ratio_threshold: 差异比率阈值
    """
    cfg = load_wechat_config(config_path)
    print("Start monitoring for new dialog changes. Press Ctrl+C to stop.")
    dialog_img_prev=capture_region_image(cfg)
    try:
        while True:
            dialog_img_curr=capture_region_image(cfg)
            diff = compute_diff_ratio(dialog_img_curr, dialog_img_prev, 
                     pixel_threshold=pixel_threshold)
            changed = (diff >= diff_ratio_threshold)
            if changed:
                i = copy_dialog_to_clipboard(cfg)
                target_dir, message, use_session_resume = resolve_target(i)
                o = ask_claude(message, target_dir, use_session_resume)
                append_memory(target_dir / "memory.md", i, o)
                paste_from_clipboard_and_send(cfg, o)
                dialog_img_prev=capture_region_image(cfg)
            time.sleep(poll_interval_s)
    except KeyboardInterrupt:
        print("\nStopped.")

__all__ = [
    "load_wechat_config",
    "capture_region_image",
    "compute_diff_ratio",
    "has_new_dialog",
    "wait_for_new_dialog",
    "copy_dialog_to_clipboard",
    "paste_from_clipboard_and_send",
    "capture_and_send",
    "demo_loop",
]
