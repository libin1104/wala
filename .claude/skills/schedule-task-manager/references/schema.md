# 任务文件结构

## `~/.qclaude_session/schedule_tasks.json`

这是一个 JSON 数组，数组里的每一项都是一个任务对象，字段如下：

- `id`：非空字符串。新任务默认会生成 UUID，也兼容旧任务中的自定义字符串 ID
- `name`：任务名称
- `enabled`：是否启用
- `task_summary`：Claude 从上下文总结出的任务意图
- `prompt_template`：定时执行时用于生成 Markdown 邮件正文的提示词
- `interval_minutes`：固定间隔分钟数，或 `null`
- `daily_times`：`HH:MM` 格式的数组
- `run_at`：绝对日期的一次性触发时间，格式为本地 ISO 时间字符串 `YYYY-MM-DDTHH:MM:SS`，或 `null`
- `created_at`：ISO 时间字符串

调度规则至少要有一个：

- `interval_minutes`
- `daily_times`
- `run_at`

`run_at` 用于未来绝对日期的一次性任务，例如“明天下午 3 点提醒我一次”。执行完成后，这类任务的 `next_run_at` 会变成 `null`，不会再次触发。

## `~/.qclaude_session/schedule_state.json`

这是一个以任务 ID 为 key 的 JSON 对象，每个 value 包含：

- `last_run_at`：上次执行时间
- `next_run_at`：下次计划执行时间
- `last_error`：最近一次执行错误
- `task_signature`：由任务定义计算出的签名

状态文件由调度器和任务管理脚本维护。在创建、更新、启用、停用、删除任务后，脚本应同步更新状态。

## Skill 持久化约束

- 在 Codex skill 中，不要调用嵌套 `claude` CLI 来生成任务。
- 先由当前会话模型按上述 schema 生成 JSON 对象。
- 再通过 `scripts/manage_schedule_tasks.py create-from-json` 或 `update-from-json` 持久化。
