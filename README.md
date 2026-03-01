# WALA

> **WechAt-cLAude** - 微信 AI 自动化解决方案

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 缘起

微信没有官方 API，AI 自动化一直是死局。

现有方案的问题：
- 逆向 WeChat 协议 → 封号风险
- 网页版微信接口 → 已被官方限制
- Hook 注入 → 复杂且不稳定

WALA 选择了一条不同的路：**模拟人类操作流程**

```
截图识别变化 → 剪贴板复制 → AI 生成回复 → 模拟粘贴发送
```

绕过 API 限制，兼容所有 AI 模型，稳定可靠。

## 功能特性

- 实时监控微信对话，自动检测新消息
- Claude AI 智能生成回复，一键发送
- **多会话管理系统**：4 位短码区分不同客户/对话
- 对话历史持久化存储
- GUI 可视化配置，5 分钟上手
- 支持截屏自动发送
- 长消息智能分段

## 核心原理

WALA 通过屏幕坐标识别 + AI 接口，打通微信和任何 AI 模型。

不需要微信官方 API，不需要逆向破解，不需要特殊权限。

Claude 能用了，GPT 能用了，任何大模型都能用。

## 安装

### 环境要求

- Python 3.8+
- Claude CLI (已安装并配置)
- macOS (目前仅支持 macOS)

### 依赖安装

```bash
pip install pyautogui Pillow
```

### 快速开始

1. 克隆仓库
```bash
git clone https://github.com/yourusername/wala.git
cd wala
```

2. 运行主程序
```bash
python main.py
```

3. 首次运行会自动启动坐标配置 GUI

## 使用方法

### 配置坐标

运行程序后，会自动打开坐标配置工具：

1. **截图区域配置**
   - 点击 "选择截图左上角"，在微信对话区域左上角点击
   - 点击 "选择截图右下角"，在微信对话区域右下角点击

2. **复制流程配置**
   - 配置对话框坐标
   - 配置复制按钮坐标

3. **粘贴流程配置**
   - 配置输入框坐标
   - 配置发送按钮坐标

4. 保存配置

### 对话模式

WALA 支持两种对话模式：

#### 1. 临时模式

直接输入消息，进行临时对话：

```
你好，请帮我写一段代码
```

#### 2. UID 持久化模式

使用 4 位短码创建持久化对话上下文：

```
ABCD, 请帮我写一个排序算法
```

之后可以使用相同短码继续对话：

```
ABCD, 这个算法的时间复杂度是多少？
```

支持的分隔符：`,`、`.`、`、`、`。`、`\n`

### 特殊指令

- 发送 **"截屏"** 自动截屏并发送

### 退出程序

将鼠标移动到屏幕左上角或右上角即可退出

## 项目结构

```
wala/
├── main.py                      # 主程序入口
├── wechat_coord_gui.py          # 坐标配置 GUI
├── wechat_auto_utlities.py      # 微信自动化工具
├── claude_io_utlities.py        # Claude 对话管理
├── wechat_info.config           # 配置文件（自动生成）
└── README.md
```

## 工作流程

```
┌─────────────────┐
│  屏幕区域截图   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  图像差异检测   │
└────────┬────────┘
         │
         ▼ 检测到变化
┌─────────────────┐
│  复制对话内容   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  调用 Claude AI │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  粘贴并发送     │
└─────────────────┘
```

## 配置文件说明

`wechat_info.config` 存储微信界面坐标信息：

```ini
[capture_region]
left = 100
top = 100
right = 500
bottom = 800

[copy_flow]
dialog_box_x = 300
dialog_box_y = 200
copy_button_x = 350
copy_button_y = 150

[paste_flow]
input_box_x = 300
input_box_y = 750
send_button_x = 450
send_button_y = 750
```

## 安全说明

- 所有对话历史存储在本地 `~/claude_sessions/` 目录
- 不会上传任何数据到第三方服务器
- Claude 调用通过本地 Claude CLI 进行

## 适用场景

- 客户服务自动化
- 商务沟通辅助
- 社群运营管理
- 个人 AI 助手

## 技术栈

- Python 3.8+
- pyautogui - 自动化操作
- PIL/Pillow - 图像处理
- tkinter - GUI 界面
- Claude CLI - AI 接口

## 注意事项

1. 首次使用需要配置坐标，建议在微信窗口固定位置使用
2. 程序运行时请勿移动微信窗口
3. 建议关闭微信的"消息提醒"功能，避免干扰截图
4. 使用 Claude CLI 前请确保已正确配置

## License

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！

---

**WALA - 微信 AI 自动化，从此开始**
