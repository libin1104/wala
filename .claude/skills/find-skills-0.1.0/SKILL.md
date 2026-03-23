---
name: find-skills
description: 当用户提出“我怎么做 X”“帮我找一个做 X 的 skill”“有没有能……的 skill”之类的问题，或表示希望扩展能力时，帮助用户发现并安装 agent skill。用户在寻找某种可能以可安装 skill 形式存在的功能时，应使用此 skill。
---

# 查找 Skills

这个 skill 用于帮助你从开放的 agent skills 生态中发现并安装 skill。

## 何时使用此 Skill

当用户出现以下情况时，使用此 skill：

- 提出“我怎么做 X”之类的问题，而 X 可能是已有 skill 覆盖的常见任务
- 说“帮我找一个做 X 的 skill”或“有没有做 X 的 skill”
- 问“你能做 X 吗”，而 X 是一种专业化能力
- 表达出希望扩展 agent 能力的意图
- 想搜索工具、模板或工作流
- 提到自己希望在某个特定领域（设计、测试、部署等）获得帮助

## 什么是 Skills CLI？

Skills CLI（`npx skills`）是开放 agent skills 生态的包管理器。Skill 是模块化软件包，可通过专门知识、工作流和工具来扩展 agent 的能力。

**核心命令：**

- `npx skills find [query]` - 以交互方式或按关键词搜索 skill
- `npx skills add <package>` - 从 GitHub 或其他来源安装 skill
- `npx skills check` - 检查 skill 更新
- `npx skills update` - 更新所有已安装的 skill

**浏览 skill：** https://skills.sh/

## 如何帮助用户查找 Skills

### 第 1 步：理解用户需要什么

当用户请求某方面帮助时，先识别：

1. 所属领域（例如 React、测试、设计、部署）
2. 具体任务（例如编写测试、制作动画、评审 PR）
3. 这是否是足够常见、很可能已有 skill 的任务

### 第 2 步：搜索 Skills

使用相关查询词运行查找命令：

```bash
npx skills find [query]
```

例如：

- 用户问“我怎么让 React app 更快？” → `npx skills find react performance`
- 用户问“你能帮我做 PR review 吗？” → `npx skills find pr review`
- 用户说“我需要生成 changelog” → `npx skills find changelog`

命令会返回类似如下结果：

```
Install with npx skills add <owner/repo@skill>

vercel-labs/agent-skills@vercel-react-best-practices
└ https://skills.sh/vercel-labs/agent-skills/vercel-react-best-practices
```

### 第 3 步：向用户展示可选项

找到相关 skill 后，向用户提供以下信息：

1. skill 名称及其作用
2. 他们可以执行的安装命令
3. 指向 skills.sh 的详情链接

示例回复：

```
我找到了一个可能有帮助的 skill！“vercel-react-best-practices” skill 提供
来自 Vercel Engineering 的 React 和 Next.js 性能优化指南。

安装命令：
npx skills add vercel-labs/agent-skills@vercel-react-best-practices

了解更多：https://skills.sh/vercel-labs/agent-skills/vercel-react-best-practices
```

### 第 4 步：主动提供安装

如果用户想继续，你可以帮他们安装该 skill：

```bash
npx skills add <owner/repo@skill> -g -y
```

其中 `-g` 表示全局安装（用户级），`-y` 表示跳过确认提示。

## 常见 Skill 类别

搜索时，可以优先考虑这些常见类别：

| 类别 | 示例查询词 |
| ---- | ---------- |
| Web 开发 | react, nextjs, typescript, css, tailwind |
| 测试 | testing, jest, playwright, e2e |
| DevOps | deploy, docker, kubernetes, ci-cd |
| 文档 | docs, readme, changelog, api-docs |
| 代码质量 | review, lint, refactor, best-practices |
| 设计 | ui, ux, design-system, accessibility |
| 生产力 | workflow, automation, git |

## 提高搜索效果的建议

1. **使用更具体的关键词**：“react testing” 比单独搜 “testing” 更好
2. **尝试近义词或替代表达**：如果 “deploy” 没结果，可以试试 “deployment” 或 “ci-cd”
3. **优先查看常见来源**：很多 skill 来自 `vercel-labs/agent-skills` 或 `ComposioHQ/awesome-claude-skills`

## 找不到 Skill 时怎么办

如果没有找到相关 skill：

1. 明确说明当前没有找到现成 skill
2. 表示你仍然可以用通用能力直接帮助完成任务
3. 建议用户使用 `npx skills init` 创建自己的 skill

示例：

```
我搜索了与 “xyz” 相关的 skill，但没有找到匹配项。
我仍然可以直接帮你处理这个任务。要我继续吗？

如果这是你经常要做的事情，也可以创建你自己的 skill：
npx skills init my-xyz-skill
```
