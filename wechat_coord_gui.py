#!/usr/bin/env python3
"""WeChat coordinate config helper GUI.

Collects these points:
- screenshot region: left-top and right-bottom
- copy flow: dialog area point, copy button point
- paste flow: input box point, paste/send button point
- screen capture button point

Saves into wechat_info.config (INI format).
"""

from __future__ import annotations

import configparser
import os
import tkinter as tk
from tkinter import messagebox

from pathlib import Path

CONFIG_PATH = Path.home() / ".claude_sessions" 
CONFIG_NAME = CONFIG_PATH / "wechat_info.config"
COUNTDOWN_SECONDS = 3


class CoordCollectorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("WeChat 坐标采集器")
        self.root.geometry("640x380")

        self.values: dict[str, tuple[int, int] | None] = {
            "screenshot_left_top": None,
            "screenshot_right_bottom": None,
            "copy_dialog": None,
            "copy_button": None,
            "paste_input": None,
            "paste_button": None,
        }
        self.value_labels: dict[str, tk.Label] = {}
        self.status_var = tk.StringVar(value="点击右侧按钮开始采集坐标")
        self.is_capturing = False
        self.current_group_name = ""
        self.current_steps: list[tuple[str, str]] = []
        self.current_step_index = 0

        self._build_ui()
        self._load_existing_config()

    def _build_ui(self) -> None:
        header = tk.Label(
            self.root,
            text=(
                "操作方式：点击分组采集按钮后，按提示把鼠标移动到目标位置。"
                "每个点位在倒计时 3 秒后自动记录。"
            ),
            anchor="w",
            justify="left",
            wraplength=720,
        )
        header.pack(fill="x", padx=12, pady=(12, 8))

        table = tk.Frame(self.root)
        table.pack(fill="both", expand=True, padx=12, pady=6)

        rows = [
            ("screenshot_left_top", "截图区域左上角"),
            ("screenshot_right_bottom", "截图区域右下角"),
            ("copy_dialog", "复制流程：对话框位置"),
            ("copy_button", "复制流程：复制按钮位置"),
            ("paste_input", "粘贴流程：文本框位置"),
            ("paste_button", "粘贴流程：粘贴/发送按钮位置"),
        ]

        for idx, (key, title) in enumerate(rows):
            tk.Label(table, text=title, width=28, anchor="w").grid(
                row=idx, column=0, sticky="w", padx=(0, 8), pady=6
            )
            value_label = tk.Label(table, text="未设置", width=28, anchor="w", fg="#444")
            value_label.grid(row=idx, column=1, sticky="w", padx=(0, 8), pady=6)
            self.value_labels[key] = value_label

        table.columnconfigure(1, weight=1)

        actions = tk.Frame(self.root)
        actions.pack(fill="x", padx=12, pady=(0, 4))
        tk.Button(
            actions,
            text="采集截图区域(两点)",
            width=16,
            command=self.start_capture_screenshot_group,
        ).pack(side="left")
        tk.Button(
            actions,
            text="采集复制流程(两点)",
            width=16,
            command=self.start_capture_copy_group,
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            actions,
            text="采集粘贴流程(两点)",
            width=16,
            command=self.start_capture_paste_group,
        ).pack(side="left", padx=(8, 0))

        status = tk.Label(
            self.root,
            textvariable=self.status_var,
            anchor="w",
            fg="#0b5394",
            wraplength=720,
            font=("", 16, "bold"),
        )
        status.pack(fill="x", padx=12, pady=(8, 6))

        foot = tk.Frame(self.root)
        foot.pack(fill="x", padx=12, pady=(0, 12))

        tk.Button(foot, text="保存配置", width=12, command=self.save_config).pack(
            side="left"
        )
        tk.Button(foot, text="清空全部", width=12, command=self.clear_all).pack(
            side="left", padx=(8, 0)
        )

        self.save_path_var = tk.StringVar(value=f"{os.path.abspath(CONFIG_NAME)}")
        tk.Label(foot, textvariable=self.save_path_var, anchor="w", fg="#666").pack(
            side="left", padx=(16, 0)
        )

    def _load_existing_config(self) -> None:
        if not os.path.exists(CONFIG_NAME):
            return

        parser = configparser.ConfigParser()
        parser.read(CONFIG_NAME, encoding="utf-8")

        mapping = {
            "screenshot_left_top": ("screenshot", "left_top"),
            "screenshot_right_bottom": ("screenshot", "right_bottom"),
            "copy_dialog": ("copy", "dialog_pos"),
            "copy_button": ("copy", "button_pos"),
            "paste_input": ("paste", "input_pos"),
            "paste_button": ("paste", "button_pos"),
        }

        for key, (section, option) in mapping.items():
            raw = parser.get(section, option, fallback="").strip()
            pos = self._parse_pos(raw)
            if pos is not None:
                self.values[key] = pos
                self._refresh_value_label(key)

        self.status_var.set("已加载现有配置，可继续调整后保存")

    def _parse_pos(self, value: str) -> tuple[int, int] | None:
        if not value or "," not in value:
            return None
        x, y = value.split(",", 1)
        try:
            return int(x.strip()), int(y.strip())
        except ValueError:
            return None

    def start_capture_screenshot_group(self) -> None:
        self._start_capture_group(
            "对话框区域",
            [
                ("screenshot_left_top", "对话框左上角"),
                ("screenshot_right_bottom", "对话框右下角"),
            ],
        )

    def start_capture_copy_group(self) -> None:
        self._start_capture_group(
            "复制流程",
            [
                ("copy_dialog", "复制流程：对话框位置"),
                ("copy_button", "复制流程：复制按钮位置"),
            ],
        )

    def start_capture_paste_group(self) -> None:
        self._start_capture_group(
            "粘贴流程",
            [
                ("paste_input", "粘贴流程：文本框位置"),
                ("paste_button", "粘贴流程：粘贴/发送按钮位置"),
            ],
        )

    def _start_capture_group(self, group_name: str, steps: list[tuple[str, str]]) -> None:
        if self.is_capturing:
            self.status_var.set("当前正在采集，请等待本轮完成")
            return
        self.is_capturing = True
        self.current_group_name = group_name
        self.current_steps = steps
        self.current_step_index = 0
        self._run_group_step_countdown(COUNTDOWN_SECONDS)

    def _run_group_step_countdown(self, remain: int) -> None:
        key, title = self.current_steps[self.current_step_index]
        step_no = self.current_step_index + 1
        total = len(self.current_steps)
        if remain > 0:
            self.status_var.set(
                f"[{self.current_group_name}] 第 {step_no}/{total} 步："
                f"请把鼠标移到【{title}】，{remain} 秒后记录"
            )
            self.root.after(1000, lambda: self._run_group_step_countdown(remain - 1))
            return

        x, y = self.root.winfo_pointerxy()
        self.values[key] = (x, y)
        self._refresh_value_label(key)
        self.status_var.set(
            f"[{self.current_group_name}] 完成 {step_no}/{total}【{title}】: ({x}, {y})"
        )

        self.current_step_index += 1
        if self.current_step_index >= len(self.current_steps):
            self.is_capturing = False
            self.current_group_name = ""
            self.current_steps = []
            self.current_step_index = 0
            self.status_var.set(f"{self.status_var.get()}；本轮采集完成")
            return

        self._run_group_step_countdown(COUNTDOWN_SECONDS)

    def _refresh_value_label(self, key: str) -> None:
        pos = self.values.get(key)
        label = self.value_labels[key]
        if pos is None:
            label.config(text="未设置", fg="#444")
        else:
            label.config(text=f"({pos[0]}, {pos[1]})", fg="#0b5394")

    def clear_all(self) -> None:
        if self.is_capturing:
            self.status_var.set("当前正在采集，不能清空")
            return
        for key in self.values:
            self.values[key] = None
            self._refresh_value_label(key)
        self.status_var.set("已清空所有采集结果")

    def _validate(self) -> bool:
        missing = [k for k, v in self.values.items() if v is None]
        if missing:
            messagebox.showerror("缺少坐标", "请先完成全部坐标采集后再保存")
            return False

        lt = self.values["screenshot_left_top"]
        rb = self.values["screenshot_right_bottom"]
        assert lt is not None and rb is not None
        if rb[0] <= lt[0] or rb[1] <= lt[1]:
            messagebox.showerror("截图区域不合法", "右下角坐标必须大于左上角坐标")
            return False

        return True

    def save_config(self) -> None:
        if not self._validate():
            return

        parser = configparser.ConfigParser()

        lt = self.values["screenshot_left_top"]
        rb = self.values["screenshot_right_bottom"]
        cd = self.values["copy_dialog"]
        cb = self.values["copy_button"]
        pi = self.values["paste_input"]
        pb = self.values["paste_button"]

        assert lt and rb and cd and cb and pi and pb

        parser["screenshot"] = {
            "left_top": f"{lt[0]},{lt[1]}",
            "right_bottom": f"{rb[0]},{rb[1]}",
        }
        parser["copy"] = {
            "dialog_pos": f"{cd[0]},{cd[1]}",
            "button_pos": f"{cb[0]},{cb[1]}",
        }
        parser["paste"] = {
            "input_pos": f"{pi[0]},{pi[1]}",
            "button_pos": f"{pb[0]},{pb[1]}",
        }

        os.makedirs(CONFIG_PATH, exist_ok=True)
        with open(CONFIG_NAME, "w", encoding="utf-8") as f:
            parser.write(f)

        path = os.path.abspath(CONFIG_NAME)
        self.status_var.set(f"配置已保存：{path}")
        messagebox.showinfo("保存成功", f"已保存到\n{path}")        


def selector() -> None:
    root = tk.Tk()
    app = CoordCollectorApp(root)
    root.mainloop()


if __name__ == "__main__":
    selector()
