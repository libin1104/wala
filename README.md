# WALA

> **WechAt-cLAude** - 微信 AI 自动化解决方案

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 缘起

微信没有官方 API，AI 自动化一直是死局。能不能像openclaw-tg一样操作ClaudeCode-Wechat?

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
- 让python接管ClaudeCode的io
- 复制消息到剪贴板，读取剪贴板作为输入
- claude输出到剪贴板，粘贴到微信对话框并发送
- 默认claudecode具有所有权限
- **多会话管理系统**：4 位短码区分不同客户/对话
- 对话历史持久化存储
- GUI 可视化配置，5 分钟上手
- 支持截屏自动发送
- 长消息智能分段

## 核心原理

WALA 通过屏幕坐标识别 + AI 接口，打通微信和任何 AI 模型。

不需要微信官方 API，不需要逆向破解，不需要特殊权限。

python接管Claude Code的io，同样也可以接管codex，kimicode等等，任何大模型都能用。

通过剪贴板操作，微信/QQ都能用

![gif](https://github.com/user-attachments/assets/166eb684-4206-4467-98a5-28512acb7d89)


## 安装

### 环境要求

- Python 3.8+
- Claude CLI (已安装并配置)
- macOS (目前仅支持 macOS)

### 依赖安装

```bash
pip install pyautogui Pillow pyperclip
```

### 快速开始

1. 克隆仓库
```bash
git clone https://github.com/libin1104/wala.git
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
   - 把鼠标放到微信对话区域左上角
   - 把鼠标放到微信对话区右下角

2. **复制流程配置**
   - 把鼠标放到消息框
   - 右击消息框，把鼠标放到复制按钮上

3. **粘贴流程配置**
   - 把鼠标放到输入框
   - 右击输入框，把鼠标放到粘贴按钮上

4. 保存配置、叉掉退出

<img width="474" height="871" alt="背景" src="https://github.com/user-attachments/assets/a66c8ab1-63a9-4b4a-b186-3c810010f853" />


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


### 失败处理（最新）

为避免自动化中断，当前版本对常见失败采用“不中断 + 明确错误文案回传”策略：

- 会话恢复失败：第一行返回 `ID检索失败，请更换ID`，第二行返回 `memory文件: <该ID目录>/memory.md`
- `session_id.txt` 非法：`ID无效，请更换ID`
- Claude CLI 不可用：`Claude CLI不可用，请检查安装与PATH`
- Claude 响应超时：`Claude响应超时，请稍后重试`
- Claude 限流：`Claude请求过于频繁，请稍后重试`
- Claude 鉴权异常：`Claude鉴权失败，请重新登录`
- 其他异常：`Claude调用失败，请稍后重试` 或 `执行失败：...`

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
│  屏幕区域截图     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  图像差异检测     │
└────────┬────────┘
         │
         ▼ 检测到变化
┌─────────────────┐
│  复制对话内容     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Claude -> 剪贴板 │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    粘贴并发送     │
└─────────────────┘
```

## 配置文件说明

`wechat_info.config` 存储微信界面坐标信息：

```ini
[screenshot]
left_top = 40,204
right_bottom = 529,810

[copy]
dialog_pos = 441,775
button_pos = 484,778

[paste]
input_pos = 173,897
button_pos = 202,928
```

- `screenshot`: 截图区域（对话区域的左上角和右下角）
- `copy`: 复制流程（对话框位置和复制按钮位置）
- `paste`: 粘贴流程（输入框位置和发送按钮位置）
- 默认配置路径：`~/.claude_sessions/wechat_info.config`

## 安全说明

- 所有对话历史存储在本地 `~/.claude_sessions/` 目录
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
3. 使用 Claude CLI 前请确保已正确配置
4. 可以将微信对话页面置顶

## License

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！

---

**WALA - 微信 AI 自动化，从此开始**
