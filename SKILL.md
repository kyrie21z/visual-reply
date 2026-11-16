---
name: visual-reply
description: Visual-First 交互画布：把结论、架构、方案对比、执行计划、实验表格与图表发布为本机可视化网页。JSON 画布（拓扑图 / 对比评分 / Bento / 步骤条 / KPI / 科研表格 / 柱线散点热力 / 长文）、HTML Artifact 沙箱、Markdown 兼容三模式，详情折叠进右侧抽屉；多会话工作台自动识别 Agent 线程，左侧会话栏 + 焦点锁 + 分屏，终端只回传短结论与 http://127.0.0.1:18765 链接。
---

# Visual Reply v6

把结构化结论发布成本机可视化网页，让用户在浏览器里**看**，而不是在终端里滚动长文。

核心原则：**首屏无长句**。首屏只出现交互图表、Bento 卡片、对比评分条、节点拓扑；所有解释、代码、文件清单默认折叠进右侧 Inspector 抽屉，点击才展开（Progressive Disclosure）。

## 定位脚本（跨客户端通用）

本 Skill 不绑定任何客户端或安装位置。下文所有命令里的 `${SKILL_DIR}` 指**当前 Skill 目录**，即包含本 `SKILL.md` 的目录：

```bash
# Git Bash / WSL / macOS / Linux
SKILL_DIR="$(cd "$(dirname "<本 SKILL.md 的路径>")" && pwd)"
python3 "${SKILL_DIR}/scripts/publish.py" --help

# PowerShell
$SKILL_DIR = Split-Path -Parent "<本 SKILL.md 的路径>"
python "${SKILL_DIR}/scripts/publish.py" --help
```

也可以先 `cd` 进 Skill 目录，再直接用相对路径 `python3 scripts/publish.py ...`。只依赖 Python 3.10+ 标准库，无需安装任何第三方包。

## 触发时机

满足任一条件即发布，不要把长文全部打印到终端：

- 回复预计超过 300 字，或包含 3 个以上并行维度
- 描述复杂架构、模块关系、调用链、数据流、状态机
- 两个及以上方案的对比选型
- 多步骤计划、迁移步骤、排查路径
- 需要定制动效 / 交互演示（用 HTML Artifact）
- 用户明确要求「画个图」「可视化看看」「脑图」「对比表」「整理一下」

简单问答、单行结论、代码片段解释不需要发布。

---

## Visual-First 输出准则（强制）

1. **优先结构化，不要倾倒文字**
   - 有结构的内容 → `--format json` 输出视觉画布（`graph` / `comparison` / `bento` / `steps` / `kpis` / `table` / `chart` / `prose`）
   - 需要动效、自定义布局、交互演示 → `--format html` 输出 HTML Artifact
   - **严禁再把几百字纯 Markdown 长文直接丢给 visual-reply**。`--format md` 只用于：用户明确要长文阅读、内容本身就是文档、或需要 Mermaid 时序图。

2. **两级信息密度**
   - 画布上的 `label` / `title` / `summary` / `metric` **控制在 15 字以内**，只放结论与数字。
   - 长解释、推导过程、代码片段、文件列表**全部放进 `details` 字段**，默认折叠在右侧抽屉里。
   - 典型结构：`headline` 一句话结论 → `kpis` 关键数字 → `graph`/`comparison` 主体 → `details` 承载全部细节。

3. **长度硬规范（拓扑图必须遵守）**

   | 字段 | 硬上限 | 超限后果 |
   | --- | --- | --- |
   | `headline` | **≤ 30 字** | 首屏折行挤压画布，超 40 字 `publish.py` 返回 `warnings` |
   | `node.label` | **≤ 12 字** | 节点宽 204px，最多两行，超出被省略号截断 |
   | `edge.label` | **≤ 10 字** | 列间隙最宽 230px，超出后标签会压到节点上；超 14 字返回 `warnings` |

   `publish.py` 的 `warnings` 只是提醒：发布照常成功、退出码仍为 0，但输出里会点名是哪条边超长。

4. **一个画布讲一件事**
   - `sections` 控制在 2 到 5 个，按「结论 → 依据 → 落地」排列。
   - 节点 `id` 稳定、`layer` 明确，避免让自动分层猜错。

---

## 多会话工作台（v4）

一个浏览器页面同时托管多个 Agent 会话，默认按「谁发布、谁归属」自动分栏，不需要任何配置。

### 会话是怎么识别的

`publish.py` 按下面的优先级推导会话 ID，命中即停：

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `--session <id>` | 显式指定，优先级最高；非法 ID 直接拒绝发布 |
| 2 | `VISUAL_REPLY_SESSION` | 通用环境变量，适合脚本批量发布 |
| 3 | Agent 客户端环境变量 | `ANTIGRAVITY_CONVERSATION_ID`、`CODEX_THREAD_ID` / `CODEX_SESSION_ID`、`OPENCODE_SESSION_ID`、`GEMINI_SESSION_ID`、`CLAUDE_SESSION_ID` / `CLAUDE_CODE_REPL_ID` |
| 4 | 终端复用器 | `WT_SESSION`、`TMUX_PANE`、`WEZTERM_PANE`、`KITTY_WINDOW_ID`、`ITERM_SESSION_ID` |
| 5 | 父进程兜底 | `sha256(父进程 ID \| 项目根)[:12]`，同一 shell 稳定、不同终端分开 |

会话显示名默认是 `项目 · 客户端 · 短 ID`。想固定成人类可读的名字：

```bash
python3 ${SKILL_DIR}/scripts/publish.py \
  --title "构建线程" --stdin --session build-42 --session-name "CI 构建"
```

`--session-name` 会持久化到该会话的 `session.json`，之后同会话发布不传也不会丢；想覆盖就再传一次。
`--agent codex` 只是给客户端徽标打标签，不参与 ID 推导。

### 页面里怎么用

- **左侧会话栏（三级目录树）**：顶部独立 `★ 置顶` 分组，其下按「项目 → 会话 → 会话内回复」三层展示；项目与会话按创建时间倒序**固定位置**（旧会话收到新回复不乱跳），展开会话可直接按倒序点击每一轮回复（新回复永远贴顶）。顶部可筛 `全部 / 未读 / 置顶`，底部 `全局时间线` 汇总跨会话历史。
- **焦点锁（默认）**：点开会话即锁定该会话。其它会话的新回复**不会**劫持主窗格，只在左侧标未读并弹一个带 `[查看]` 的小提示。
- **阅读守卫**：即使就在当前会话里，只要 Inspector 抽屉开着、或正在回看历史轮次，新回复也只显示顶部横幅「本会话有新回复 [切到最新]」，不关抽屉、不替换正在读的内容。
- **分屏**：会话卡片上的 `◫` 打开第二窗格，两个窗格各自选会话、各自切换阅读 / 脑图 / 分屏视图，共享同一个 Inspector 抽屉。
- **顶部按钮**：`锁定会话` / `跟随全局` 可随时切换；`会话` 折叠左栏；`回到最新` 从历史轮次回到最新。

深链参数（可直接贴给用户）：

| 参数 | 含义 |
| --- | --- |
| `?session=<sid>` | 聚焦某个会话 |
| `?reply=<rid>` | 直接打开某条回复（配合 `session`） |
| `?split=<sid2>` | 右侧分屏显示第二个会话 |
| `?layout=h\|v` | 分屏方向：左右 / 上下 |
| `?mode=lock\|global` | 焦点锁 / 跟随全局 |

发布回执里的 `url` 已经是 `?session=<sid>&reply=<rid>` 深链，直接打开即可定位到这次回复。

---

## 使用步骤

### 1. 组织 JSON 画布

完整 Schema（字段除 `type` 外均可选）：

```json
{
  "title": "可选，缺省取 headline 作为标题",
  "headline": "一句话核心结论，首屏最大字号",
  "badges": [
    "纯字符串也可",
    { "text": "决策就绪", "tone": "ok" }
  ],
  "sections": [
    {
      "type": "kpis",
      "title": "核心指标",
      "items": [
        { "label": "P99 延迟", "value": "82ms", "status": "ok", "note": "较基线 -34%" }
      ]
    },
    {
      "type": "graph",
      "title": "调用链拓扑",
      "nodes": [
        {
          "id": "cli", "label": "命令行入口", "tag": "cli",
          "layer": 0,
          "status": "ok",
          "summary": "读取参数与内容",
          "details": "## 细节\n\n支持 `--stdin` / `--file`。",
          "files": ["scripts/publish.py"]
        }
      ],
      "edges": [
        { "from": "cli", "to": "store", "label": "内容" }
      ]
    },
    {
      "type": "comparison",
      "title": "方案对比",
      "items": [
        {
          "name": "方案 B：交互画布",
          "badge": "推荐",
          "recommended": true,
          "scores": { "性能": 5, "复杂度": 4, "可维护性": 4 },
          "pros": ["首屏无长句", "细节按需展开"],
          "cons": ["需要前端模板"],
          "summary": "视觉优先，信息密度可控。",
          "details": "### 落地要点\n\n1. Schema\n2. Drawer"
        }
      ]
    },
    {
      "type": "bento",
      "title": "关键模块",
      "cards": [
        {
          "title": "Visual Canvas",
          "metric": "8 种组件",
          "tag": "core",
          "tone": "accent",
          "span": 2,
          "summary": "kpis / graph / comparison / bento / steps / table / chart / prose",
          "details": "所有组件共享同一个 Inspector 抽屉。"
        }
      ]
    },
    {
      "type": "steps",
      "title": "落地步骤",
      "steps": [
        { "step": "01", "title": "定义 Schema", "status": "done", "summary": "五类组件", "details": "…" },
        { "step": "02", "title": "实现画布", "status": "active", "summary": "布局 + 抽屉", "details": "…" },
        { "step": "03", "title": "回归测试", "status": "pending", "summary": "三种格式" }
      ]
    },
    {
      "type": "table",
      "title": "实验结果",
      "span": 1,
      "caption": "Table 1 · 三次运行均值",
      "groups": [
        { "label": "Quality", "cols": ["acc", "f1"] },
        { "label": "Cost", "cols": ["lat", "mem"] }
      ],
      "baseline_id": "base",
      "columns": [
        { "key": "model", "label": "Model" },
        { "key": "acc", "label": "Acc", "unit": "%", "higher_is_better": true, "decimals": 1 },
        { "key": "lat", "label": "Latency", "unit": "ms", "higher_is_better": false, "decimals": 1 }
      ],
      "rows": [
        { "id": "base", "model": "Baseline", "acc": 70.2, "lat": 92.5, "is_baseline": true },
        { "id": "ours", "model": "Ours", "acc": 77.6,
          "cells": { "lat": { "value": 95.1, "note": "三次运行均值" } },
          "details": "## 复现实验\n\n固定随机种子。" }
      ]
    },
    {
      "type": "chart",
      "chart_type": "bar",
      "title": "质量与成本",
      "span": 1,
      "categories": ["Acc", "F1"],
      "series": [
        { "name": "Baseline", "data": [70.2, 68.4] },
        { "name": "Ours", "data": [77.6, 76.1] }
      ],
      "y_label": "Score",
      "unit": "%",
      "ref_lines": [{ "value": 75, "label": "target" }]
    },
    {
      "type": "chart",
      "chart_type": "line",
      "title": "训练曲线",
      "x": ["1", "2", "3", "4"],
      "series": [
        { "name": "Baseline", "data": [52.0, 61.4, 66.8, 69.1] },
        { "name": "Ours", "data": [55.1, 65.2, 71.4, 75.0] }
      ]
    },
    {
      "type": "prose",
      "title": "结论",
      "content": "## 结论\n\n- **Ours** 在同量级延迟下精度最高。"
    }
  ]
}
```

图表还可写 `scatter`（`points` + `pareto: "max_y_min_x" | "max_xy" | "min_xy"`）与
`heatmap`（`x_labels` / `y_labels` / `values` 二维数组，或稀疏 `cells`）。所有图表都是
纯 SVG 离线绘制，不依赖 CDN。

字段约束：

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `status`（node） | `ok` `warn` `err` `info` | 状态色点，未给出按 `info` |
| `status`（kpi） | 同上 | 右上角状态圆点 |
| `status`（step） | `done` `active` `pending` | 进度条配色 |
| `tone`（card） | `accent` `ok` `warn` `err` | Bento 卡片描边与强调色 |
| `span`（card） | `1` `2` | Bento 列宽，4 列栅格 |
| `span`（section） | `1` `2` | 桌面两列栅格中占 1 列或整行；分屏与窄屏自动回落整行 |
| `columns[]` | `key` / `label` / `align` / `highlight` / `higher_is_better` / `decimals` / `unit` / `delta_unit` | 表格列定义；`higher_is_better` 同时决定最优方向与 Δ 色调 |
| `rows[]` | `id` / `cells` / `is_baseline` / `emphasis` / `divider_before` / `details` / `files` | 单元格可写原始值，或 `{value, display, note, status}` 对象 |
| `chart_type` | `bar` `line` `scatter` `heatmap` | 图表类型，缺省 `bar` |
| `scores` | `{维度: 1..5}` | 渲染为进度条，不用写文字形容高低 |
| `layer` | 整数，从 0 开始 | 缺省时按 edges 拓扑排序自动分层 |
| `details` | Markdown 字符串 | 右侧抽屉内渲染，可放代码块与列表 |
| `files` | 字符串数组 | 抽屉内以等宽 chip 展示 |
| `node.label` | ≤ 12 字 | 两行自动换行，超出省略 |
| `edge.label` | ≤ 10 字 | 胶囊标签，超 14 字触发 `warnings` |

顶层若只有一个图，可直接写成 `{ "headline": …, "nodes": [...], "edges": [...] }`；`cards` / `steps` / `kpis` 同理。表格可把 `columns` 与 `rows` 直接放顶层，图表可把 `chart_type`（或 `matrix` + `x_labels`）放顶层。自动识别规则：可解析为对象且含 `sections` / `nodes` / `cards` / `columns`+`rows` / `chart_type` / `matrix`+`x_labels` 即视为 json；只有 `prose` 字段时需显式 `--format json`。

### 2. 发布

> 下文 `${SKILL_DIR}` 指本 `SKILL.md` 所在目录，实际调用时请替换为该目录路径。

```bash
# 视觉画布（推荐）
python3 ${SKILL_DIR}/scripts/publish.py \
  --title "中台改造方案" --format json --file /tmp/canvas.json

# 结构化内容走 stdin，自动识别格式
python3 ${SKILL_DIR}/scripts/publish.py \
  --title "中台改造方案" --stdin <<'JSON'
{ "headline": "…", "sections": [ … ] }
JSON

# HTML Artifact（自定义动效 / 原型）
python3 ${SKILL_DIR}/scripts/publish.py \
  --title "交互原型" --format html --file /tmp/demo.html

# 兼容模式：确实需要长文阅读时
python3 ${SKILL_DIR}/scripts/publish.py \
  --title "长文说明" --format md --file notes.md
```

脚本自动完成：写入 `latest.<ext>` 与 `meta.json`、归档历史、按需后台拉起服务、首次发布打开浏览器（`--no-browser` 关闭），最后输出 JSON 状态。端口上是旧版本服务时会自动 `SIGTERM` 接管并拉起当前版本。

`--format json` 时会顺带检查文字长度：`headline` 超过 40 字、或任意 `edge.label` 超过 14 字，返回 JSON 里会多一个 `warnings` 数组点名具体条目；发布本身照常成功，退出码仍为 0。

### 3. 终端回执

发布成功后，终端**只输出**两样东西：

1. 3 到 6 行核心结论（不是全文复述）
2. 回执 JSON 里的 `url` **原样**打印 —— 它是带 `?session=<sid>&reply=<rid>` 的深链，点开就能定位到这次回复

```
已生成视觉画布：1 个拓扑图、2 个方案对比、3 张 Bento、4 步计划。
结论：方案 B 胜出，改动集中在中台层，回滚 90s。
细节已折叠在右侧抽屉：http://127.0.0.1:18765/?session=codex-7f3a&reply=20261114-101500-000001-plan-b
```

**不要截断或改写查询参数**：`?session=` 决定打开哪个会话、`?reply=` 定位到哪一轮回复，删掉后用户会落到默认页面、看不到刚发布的内容。只有回执无法解析（脚本异常、非 JSON 输出）时才退化为基地址 `http://127.0.0.1:18765`。

不要把发布过的正文再贴一遍，用户已经在浏览器里看到了。JSON 里的路径、字符数等细节无需复述，除非发布失败。

---

## 三种渲染模式

| 模式 | 触发 | 交互 |
| --- | --- | --- |
| A · Visual Canvas | `format = json` | headline + badges + 8 类组件（kpis / graph / comparison / bento / steps / table / chart / prose）；顶部 `[视觉画布]` / `[原始数据]` 切换；悬停节点高亮上下游；点击节点/卡片/步骤/表格行/散点/热力格在右侧抽屉展开 `details` 与 `files`；表格支持排序、搜索、基准 Δ 与三种导出，图表支持图例开关与 tooltip；`Esc` 或 × 关闭 |
| B · HTML Artifact | `format = html` | 全屏 `<iframe sandbox="allow-scripts">` 渲染 `/artifact/latest`；顶部 `[全屏]` / `[源码]` 切换；查看历史时加载 `/artifact/history/<filename>` |
| C · 兼容阅读 | `format = md` | 阅读 / 脑图 / 分屏三视图，Mermaid 图表、标题折叠、表格 |

所有模式共享：浅色/深色主题切换、会话内回复轮次下拉（`#n (最新) · 标题`，另附全局 v3 归档）、SSE 实时推送 + 3 秒轮询兜底——再次发布会在 100ms 级自动刷新当前页面。

组件语义速查：

| type | 视觉形态 | 适用 |
| --- | --- | --- |
| `kpis` | 横向指标卡，大数字 + 状态点 | 开门见山给关键数字 |
| `graph` | 分层节点拓扑 + SVG 贝塞尔箭头 | 架构、调用链、依赖、状态流转 |
| `comparison` | 方案卡 + 评分条 + 红绿标签 | 技术选型、方案取舍 |
| `bento` | Bento 网格，可跨列 | 并列模块/能力总览 |
| `steps` | 横向阶段进度条 | 迁移步骤、执行计划、排查路径 |
| `table` | 科研表格：分组表头、最优/次优标记、基准 Δ、搜索排序、LaTeX/Markdown/CSV 导出 | 基准测试、消融实验、参数对照 |
| `chart` | 纯 SVG 柱状 / 折线 / 散点 / 热力，悬停 tooltip、图例开关、帕累托前沿 | 趋势、分布、权衡关系、消融矩阵 |
| `prose` | Markdown 长文卡片，支持表格与外部链接 | 结论、方法、限制说明 |

科研表格与图表交互（全部离线，无第三方图表库）：

- **双列栅格**：桌面按两列排布，`span: 1` 的 section 并排；分屏或窄屏自动回落单列。
- **表格**：点表头排序（缺失值永远沉底），搜索框只重绘 `tbody` 不丢焦点；`设为基准` 按钮就地切换 Δ 对比行，点行本身才打开抽屉；`LaTeX` 输出 `booktabs` 的 `\toprule / \midrule / \textbf / \underline / \multicolumn`，`Markdown` 与 `CSV` 按 RFC 4180 转义。剪贴板不可用时，导出内容始终显示在表格下方的预览框里。
- **图表**：柱状/折线图例可点击隐藏系列，折线在 `null` 处自动断线；散点可选 `pareto` 前沿并显示标签；热力按 `scale_mode`（`max_good` / `min_good` / `neutral`）着色。`details` / `files` 挂在系列、散点或热力格上即可点击进抽屉。
- **长文**：`prose` 直接复用 Markdown 渲染与表格包裹逻辑，链接自动 `target="_blank"`。

拓扑图渲染规则（自动生效，无需额外字段）：

- **列间距自适应**：按相邻列之间最长边标签估算宽度撑开列间隙（108–230px），标签不会挤在节点上。
- **同列重心排序**：按上游前驱平均行号排同列节点，顺反各一轮，减少连线交叉。
- **非相邻边走 Bypass 通道**：跨列长边走上通道、逆向回环与自环走下方通道，轨道间距 34px，画布自动上下留白；任何边都不会横穿中间层节点。
- **标签独立顶层**：边标签是 `.g-edge-pill` 胶囊（z-index 3），不会被节点卡片切字；同一间隙内多条标签自动上下避让。
- **缩放工具栏**：图右上角 `适应 / 100% / − / +`，`transform: scale()` 实现，6 列宽图默认缩到 0.35 也能一屏看完。

---

## 参数

| 参数 | 说明 |
| --- | --- |
| `--title` | 标题；缺省时 json 取 `title`/`headline`，html 取 `<title>`，md 取第一个 H1 |
| `--format` | `auto`（默认）/ `json` / `html` / `md` |
| `--file` | 从文件读取内容 |
| `--stdin` | 从标准输入读取（管道或 heredoc） |
| `--no-browser` | 不自动打开浏览器 |
| `--port` | 服务端口，默认 `18765` |
| `--cache-dir` | 缓存目录，默认 `~/.cache/agent-visual-reply` |
| `--session` | 会话 ID（默认自动检测，优先级最高） |
| `--session-name` | 会话显示名，持久化到 `session.json` |
| `--agent` | 客户端徽标，如 `codex` / `claude` / `antigravity` |

缓存结构：

```
~/.cache/agent-visual-reply/
├── .publish.lock                     # 写锁：跨平台排他锁（POSIX flock / Windows msvcrt）
├── .server.lock                      # 启动锁：冷启动时串行化 server.py 拉起
├── state.json                        # {version: 6, seq, latest_session_id, latest_reply_id}
├── sessions/
│   └── <sid>/
│       ├── session.json              # label / client / project / pinned / seq / reply_count
│       └── replies/
│           ├── 20261114-101500-000001-plan-b.md
│           └── 20261114-101500-000001-plan-b.md.meta.json
├── global/feed.jsonl                 # 跨会话追加式时间线（seq 单调递增）
├── latest.json | latest.html | latest.md   # v3 兼容投影：当前载荷（按 format 只写其一）
├── meta.json                         # v3 兼容投影，额外带 seq / session_id / reply_id
├── server.log
└── history/                          # v3 兼容投影，旧客户端照常读
    ├── 20261114-101500-标题.json
    ├── 20261114-101500-标题.json.meta.json
    └── 20261114-095500-旧内容.md
```

`reply_id` 形如 `20261114-101500-000001-plan-b`：时间戳 + 全局序号 + ASCII 片段，天然可排序、可直接拼 URL。

## 服务接口

| 接口 | 返回 |
| --- | --- |
| `GET /` | 单页应用 |
| `GET /api/latest` | `{version, format, title, content, mtime, updated_at}` |
| `GET /api/history` | `{version, items: [{format, filename, title, updated_at, mtime}], count}`，最近 20 条 |
| `GET /api/history/<filename>` | `{format, title, content, updated_at, mtime}` |
| `GET /artifact/latest` | 当前 HTML Artifact 原文，`text/html; charset=utf-8`；非 html 格式返回 404 |
| `GET /artifact/history/<filename>` | 历史 HTML Artifact 原文 |
| `GET /api/sessions` | `{version, seq, latest_session_id, sessions: [...], legacy_count}`，置顶优先、seq 倒序 |
| `GET /api/sessions/<sid>/replies?limit=200` | 该会话的回复轮次，最新在前 |
| `GET /api/sessions/<sid>/replies/<rid>` | `{version, session, reply, content}` |
| `GET /api/feed?limit=50` | 跨会话全局时间线（含 v3 遗留归档） |
| `GET /api/state?since=<seq>` | 增量事件，供断线补播 |
| `GET /api/events` | SSE 实时推送（`text/event-stream`） |
| `POST /api/notify` | 发布后唤醒 SSE，`{seq, session_id, reply_id, title, format}` |
| `POST /api/sessions/<sid>` | 更新 `{label, pinned}`，原子写入 |
| `GET /artifact/session/<sid>/<rid>` | 会话回复的 HTML 原文 |
| `GET /healthz` | `{ok, service, version, pid, port, cache_dir, seq}` |

服务只监听 `127.0.0.1`。需要单独启动时：

```bash
python3 ${SKILL_DIR}/scripts/server.py --port 18765
```

## 排错

- **端口被其他程序占用**：`publish.py` 返回 `ok: false`、退出码 2，换 `--port` 重试。
- **旧版服务没被接管**：健康检查若缺少 `pid` 会拒绝自动接管，手动结束该进程后重试。
- **服务起不来**：查看 `~/.cache/agent-visual-reply/server.log`，报错信息里也会附带日志末尾。
- **画布空白**：确认 JSON 顶层是对象且含 `sections` / `nodes` / `cards` / `columns`+`rows` / `chart_type`；`json.loads` 失败的载荷会回落到 md 渲染。
- **节点分层不理想**：显式给 `layer`，不要让拓扑自动分层在有环图上推断。
- **宽图右侧看不全**：点拓扑图右上角「适应」按容器宽度缩放（最低 0.35）；`100%` 恢复原始尺寸，`−` / `+` 微调。
- **图表/脑图不显示**：页面通过 jsdelivr CDN 加载 `marked`、`mermaid`、`markmap`，离线时退化为纯文本；JSON 画布不依赖 CDN（Markdown 抽屉除外）。
- **改了内容但页面没变**：确认发布到同一端口与同一 `--cache-dir`；右上角状态点应显示「已同步」。
- **新回复没自动出现**：先看右上角状态点；SSE 断线时会自动降级为 3 秒轮询，状态点显示「实时断线，轮询中」。
- **会话被拆成好几个**：Agent 每次调用可能换了父进程或终端；用 `--session` 或 `--session-name` 固定，或在会话卡片上重命名（重命名不改 ID）。
- **发布跑到别的会话里**：检查环境变量里是否残留 `CODEX_THREAD_ID` / `CLAUDE_SESSION_ID` 等；显式 `--session` 永远优先。

## 自检

修改脚本后运行端到端测试（临时目录 + 随机端口，不污染真实缓存）：

```bash
python3 ${SKILL_DIR}/scripts/test_visual_reply.py
```

覆盖：md/json/html 发布、auto 识别、八类画布组件元数据、前端组件存在性、artifact 接口、混合历史排序、路径穿越防护、旧版服务接管升级，6 列回环拓扑的几何断言（用本机 `node` 跑 `PAGE_HTML` 里的纯几何核心，校验越界、穿模、列间距、标签叠字）；v4 追加多会话自动检测与深链回执、8 进程并发写入（seq 单调、零丢回复、sidecar 全可解析）、SSE 实时推送 / 增量补播 / 挂连接 SIGTERM 1.5s 内退出、会话置顶重命名与路径穿越、表格/图表纯核心（Node VM：排名、Δ 方向、缺失值沉底、LaTeX 转义、niceScale、断线分段、帕累托前沿、热力归一化）、v6 科研画布端到端发布与 table/chart/prose 简写识别，以及工作台前端标记与「渲染逻辑只有一份」的结构断言。没有 `node` 时会跳过几何与核心用例并明确报出。另含开源卫生检查：公开文件不得出现本机主目录、私人项目名或作者 handle（README/LICENSE 的安装与署名除外），并校验文档互链、`.gitignore` 与 CI 配置。
