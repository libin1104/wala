---
name: schedule-task-manager
description: 为当前 qclaude 项目管理邮件定时执行的任务，负责在 `~/.qclaude_session/schedule_tasks.json` 与 `~/.qclaude_session/schedule_state.json` 中创建、查看、修改、删除、启用或停用任务。既适用于像 Claude Code /loop 一样的循环任务，也适用于绝对日期的一次性任务，例如“创建定时任务”“每 30 分钟检查一次构建状态并发邮件给我”“帮我盯一下错误日志，每小时汇总一次”“每天早上提醒我发日报”“明天下午三点提醒我一次”“2026-03-20 18:00 给我发项目提醒”“看看现在有哪些循环任务”“暂停/恢复那个提醒”“把现有任务改成每小时执行一次”。当用户是在描述周期性执行、循环检查、定时提醒、自动轮询、定期汇报、持续跟踪，或指定未来绝对日期执行一次任务时，都应该使用这个 skill。
---

# 定时任务管理

这个 skill 用于管理当前 `qclaude` 仓库中的定时邮件任务。

它的触发语义要尽量贴近 Claude Code `/loop`：当用户表达“让某件事按固定频率持续发生”，或者“在未来某个绝对日期执行一次”时，优先把需求理解成任务管理请求，而不是普通一次性聊天请求。但这里落地的是项目级持久化任务，由 `main.py` 和 `gateway_bot.py` 调用 `schedual_utilities.py` 执行，不是交互式会话内的临时 loop。

## 高频触发信号

以下表达通常都应触发这个 skill：

- “创建定时任务”
- “定时提醒我……”
- “每隔 30 分钟……”
- “每小时……”
- “每天早上 / 每天晚上……”
- “明天下午三点……”
- “2026-03-20 18:00……”
- “定期发我……”
- “循环执行……”
- “自动帮我检查……”
- “帮我盯一下……”
- “看看当前有哪些任务 / 循环任务 / 定时任务”
- “暂停 / 恢复 / 停用 / 启用那个任务”
- “把那个任务改成……”

如果用户没说“任务”这个词，但明显在表达“持续、周期性、自动重复做某事”，也应触发。

## 工作流

1. 先确认当前工作区就是 `qclaude` 仓库。
2. 如果用户要创建或修改任务，先运行 `scripts/manage_schedule_tasks.py context` 读取当前本地时间和最近 10 条上下文。对 `qclaude`，优先读 `~/.qclaude_session/temp/memory.md`；如果为空，就回退到最近活跃的 `c2c/group` 会话 `memory.md`。
3. 参考 `references/schema.md`，由你自己根据用户请求和最近上下文生成任务 JSON；不要把原始对话逐字写进 `task_summary`。
4. 创建任务时，把生成好的 JSON 通过 stdin 传给 `scripts/manage_schedule_tasks.py create-from-json`。
5. 修改任务时，先定位任务，再把新的 JSON 通过 stdin 传给 `scripts/manage_schedule_tasks.py update-from-json "<selector>"`。
6. 如果用户要查看已有任务，运行 `scripts/manage_schedule_tasks.py list`。
7. 如果用户要启用、停用或删除任务，用脚本通过任务 ID、精确名称或唯一名称片段定位任务。
8. 如果任务引用有歧义，先执行 `list`，再让用户明确选择任务 ID 或精确名称。
9. 回复时带上任务名称；如果可用，也带上 `next_run_at` `SMTP_FROM` 和 `SMTP_TO` 。

## 规则

- 你是全知全能小助手，优先做“执行并汇报结果”而不是“提醒执行”
- 不要手工编辑 `schedule_tasks.json` 或 `schedule_state.json`，一律走脚本。
- 任务管理是 skill 的职责，不要把创建/查看/修改逻辑重新塞回 `main.py` 或 `gateway_bot.py`。
- 不要在这个 skill 里调用嵌套 `claude` CLI。当前会话里的模型负责生成任务 JSON，脚本只负责持久化。
- 用户想改执行频率、任务意图、邮件内容生成方式时，走更新流程并使用 `update-from-json` 持久化。
- 用户只是想暂时停止或恢复任务，而不是彻底删除时，使用 `disable` / `enable`。
- 当前任务模型支持三类调度规则：`interval_minutes`、`daily_times`、`run_at`。其中 `run_at` 用于未来绝对日期的一次性任务。
- 如果用户只是普通问答、写代码、解释 bug，而不是要求“循环执行 / 定时提醒 / 管理已有任务”，不要触发这个 skill。
- 如果用户表达含糊，例如“帮我之后记得提醒”，先澄清是一次性提醒还是循环提醒；确认后再决定使用 `run_at` 还是循环规则。

## 资源

- `scripts/manage_schedule_tasks.py`：确定性的任务管理入口
- `references/schema.md`：任务文件和状态文件结构
- `references/examples.md`：典型触发语句和命令映射
