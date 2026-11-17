#!/usr/bin/env python3
"""visual-reply v7 端到端自动化测试。

覆盖：
    md / json / html 三种载荷、auto 自动识别、八类画布组件元数据、
    前端 PAGE_HTML 组件存在性、artifact 接口、混合历史、路径穿越防护、
    旧版服务接管升级、复杂 6 列回环拓扑的 JS 几何断言、发布长度提醒，
    多会话识别与深链回执、多进程并发写入、SSE 实时推送与快速退出、
    会话置顶/重命名与安全校验、科研表格与图表核心、实时面板健康度与
    幂等更新、面板接口与回复流隔离，以及开源卫生检查。

使用临时目录与随机可用端口，不触碰真实缓存目录；测试结束后关闭服务并清理。

    python3 test_visual_reply.py
"""

from __future__ import annotations

import hashlib
import http.client as http_client
import importlib.util
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PUBLISH = SCRIPT_DIR / "publish.py"
SERVER = SCRIPT_DIR / "server.py"
STORE = SCRIPT_DIR / "vr_store.py"
SKILL_MD = SCRIPT_DIR.parent / "SKILL.md"

PASSED = 0
FAILED: list[str] = []


def source_version(path: Path) -> int:
    """从脚本里读 VERSION，避免升级版本号后测试集体失效。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    match = re.search(r"^VERSION\s*=\s*(\d+)", text, re.M)
    return int(match.group(1)) if match else 0


EXPECTED_VERSION = source_version(SERVER)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" -> {detail}" if detail else ""))


#: 测试端口刻意避开系统 ephemeral 端口段。若服务端口落在该段内，客户端随机源端口
#: 可能与目标端口相同，在 Linux 上会形成 TCP self-connect：SYN 回到客户端自己的
#: socket，服务端永远收不到请求，表现为「连接超时」。18000-32000 在 Linux/macOS/
#: Windows 上都位于 ephemeral 段之外。
PORT_LOW, PORT_HIGH = 18000, 32000


def free_port() -> int:
    for _ in range(300):
        candidate = random.randint(PORT_LOW, PORT_HIGH)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def unique_port(taken: set[int]) -> int:
    """free_port 可能连续返回同一个端口，测试内必须去重。"""
    for _ in range(50):
        candidate = free_port()
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise RuntimeError("无法分配不重复的测试端口")


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_port_closed(port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_open(port):
            return True
        time.sleep(0.1)
    return not port_open(port)


def http(url: str, attempts: int = 2):
    """带一次重试的 GET。

    WSL 的 loopback 偶发丢 SYN：connect 会卡到 7s 才失败，而服务端日志里根本没有这条
    请求。这属于宿主网络栈问题，不是服务端错误，幂等 GET 重试一次即可。
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers={"Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.2)
    raise last_error  # pragma: no cover - 循环必然 return 或 raise


def get_json(url: str):
    status, body, _ = http(url)
    return status, json.loads(body.decode("utf-8"))


def status_of(url: str) -> int:
    try:
        return http(url)[0]
    except urllib.error.HTTPError as exc:
        return exc.code


MERMAID_FLOW = """flowchart LR
    A[用户请求] --> B{是否需要可视化}
    B -- 是 --> C[发布画布]
    C --> D[视觉画布]
    C --> E[HTML Artifact]
    B -- 否 --> F[直接文本回复]
"""

MERMAID_SEQ = """sequenceDiagram
    participant Agent
    participant Publish
    participant Server
    Agent->>Publish: stdin JSON
    Publish->>Server: 写入 latest.json
    Server-->>Agent: 返回预览链接
"""

MD = f"""# 兼容模式测试

这是一段用于验证 **visual-reply** 兼容模式的 Markdown 内容，包含表格、代码与图表。

## 方案对比

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 轮询 | 实现简单，兼容性好 | 有 1s 延迟 | 采用 |
| SSE | 实时性高 | 连接需保活 | 备选 |

### 图形一：流程图

```mermaid
{MERMAID_FLOW}```

### 图形二：时序图

```mermaid
{MERMAID_SEQ}```

## 代码块

```python
def hello(name: str) -> str:
    return f"hi {{name}}"
```

## 清单

- [x] 发布内容
- [x] 归档历史
- [ ] 人工确认
"""

CANVAS = {
    "headline": "推荐方案 B：改动集中在中台层，回滚成本最低",
    "badges": [
        {"text": "决策就绪", "tone": "ok"},
        {"text": "3 个风险点", "tone": "warn"},
    ],
    "sections": [
        {
            "type": "kpis",
            "title": "核心指标",
            "items": [
                {"label": "P99 延迟", "value": "82ms", "status": "ok", "note": "较基线 -34%"},
                {"label": "改动文件", "value": "12", "status": "info"},
                {"label": "新增依赖", "value": "0", "status": "ok"},
                {"label": "回滚耗时", "value": "90s", "status": "warn"},
            ],
        },
        {
            "type": "graph",
            "title": "调用链拓扑",
            "nodes": [
                {"id": "cli", "label": "命令行入口", "tag": "cli", "layer": 0, "status": "ok",
                 "summary": "解析参数后读取内容",
                 "details": "## 入口细节\n\n- 支持 `--stdin` 与 `--file`\n- 自动识别格式",
                 "files": ["scripts/publish.py"]},
                {"id": "detect", "label": "格式识别", "tag": "core", "layer": 1, "status": "ok",
                 "summary": "json / html / md 三选一",
                 "details": "先尝试 `json.loads`，再匹配 HTML 前缀，其余回落到 md。"},
                {"id": "store", "label": "落盘与归档", "tag": "io", "layer": 2, "status": "warn",
                 "summary": "latest.<ext> + meta.json",
                 "details": "归档时写入独立 `.meta.json` sidecar，避免与 json 载荷冲突。",
                 "files": ["scripts/publish.py", "~/.cache/agent-visual-reply"]},
                {"id": "browser", "label": "浏览器画布", "tag": "ui", "layer": 3, "status": "info",
                 "summary": "视觉优先，细节折叠",
                 "details": "首屏只放图表与卡片，长文本进右侧抽屉。"},
            ],
            "edges": [
                {"from": "cli", "to": "detect", "label": "内容"},
                {"from": "detect", "to": "store"},
                {"from": "store", "to": "browser", "label": "1s 轮询"},
            ],
        },
        {
            "type": "comparison",
            "title": "方案对比",
            "items": [
                {
                    "name": "方案 A：同步渲染",
                    "badge": "现状",
                    "recommended": False,
                    "scores": {"性能": 3, "复杂度": 2, "可维护性": 3},
                    "pros": ["无需新增服务"],
                    "cons": ["长文阻塞首屏", "扩展性差"],
                    "summary": "改动最小，但阅读体验没有改善。",
                    "details": "如果只做短期过渡，可以先保留该方案。",
                },
                {
                    "name": "方案 B：交互画布",
                    "badge": "推荐",
                    "recommended": True,
                    "scores": {"性能": 5, "复杂度": 4, "可维护性": 4},
                    "pros": ["首屏无长句", "细节按需展开", "支持 HTML Artifact"],
                    "cons": ["需要前端模板"],
                    "summary": "视觉优先，信息密度可控。",
                    "details": "### 落地要点\n\n1. JSON Schema\n2. Drawer 组件\n3. Artifact 沙箱",
                },
            ],
        },
        {
            "type": "bento",
            "title": "关键模块",
            "cards": [
                {"title": "Visual Canvas", "metric": "5 种组件", "tag": "core",
                 "tone": "accent", "span": 2, "summary": "kpis / graph / comparison / bento / steps",
                 "details": "所有组件共享同一个 Inspector 抽屉。"},
                {"title": "Artifact 沙箱", "metric": "iframe", "tag": "html", "tone": "ok",
                 "summary": "sandbox=allow-scripts",
                 "details": "只放行脚本，不放行同源，避免访问父页面。"},
                {"title": "兼容模式", "metric": "md", "tag": "legacy", "tone": "warn",
                 "summary": "阅读 / 脑图 / 分屏",
                 "details": "旧内容照常可读。"},
            ],
        },
        {
            "type": "steps",
            "title": "落地步骤",
            "steps": [
                {"step": "01", "title": "定义 Schema", "status": "done",
                 "summary": "sections 五类组件", "details": "给出完整示例与校验脚本。"},
                {"step": "02", "title": "实现画布", "status": "done",
                 "summary": "布局 + 抽屉", "details": "图布局用拓扑分层，不引入 d3 依赖。"},
                {"step": "03", "title": "接管旧服务", "status": "active",
                 "summary": "healthz 版本探测", "details": "SIGTERM 后等待端口释放再拉起。"},
                {"step": "04", "title": "回归测试", "status": "pending",
                 "summary": "覆盖三种格式", "details": "临时目录 + 随机端口。"},
            ],
        },
    ],
}

CANVAS_TEXT = json.dumps(CANVAS, ensure_ascii=False, indent=2)

#: v6 科研画布：表格 + 四类图表 + 长文，全部使用通用基准命名。
SCIENTIFIC_CANVAS = {
    "headline": "统一基准：表格、四类图表与结论文本同屏",
    "badges": [
        {"text": "可复现实测", "tone": "ok"},
        {"text": "离线渲染", "tone": "accent"},
    ],
    "sections": [
        {
            "type": "table",
            "title": "主结果（三次运行均值）",
            "span": 1,
            "caption": "Table 1 · 主基准，列内标注越高越好 / 越低越好",
            "note": "点击表头排序，行内按钮切换基准；LaTeX / Markdown / CSV 一键导出。",
            "groups": [
                {"label": "Quality", "cols": ["acc", "f1"]},
                {"label": "Cost", "cols": ["lat", "mem"]},
            ],
            "baseline_id": "base",
            "columns": [
                {"key": "model", "label": "Model"},
                {"key": "params", "label": "Params", "unit": "M", "decimals": 1},
                {"key": "acc", "label": "Acc", "unit": "%", "higher_is_better": True, "decimals": 1},
                {"key": "f1", "label": "F1", "unit": "%", "higher_is_better": True, "decimals": 1},
                {"key": "lat", "label": "Latency", "unit": "ms", "higher_is_better": False,
                 "decimals": 1},
                {"key": "mem", "label": "Memory", "unit": "GB", "higher_is_better": False,
                 "decimals": 2},
            ],
            "rows": [
                {"id": "base", "model": "Baseline", "params": 24.0, "acc": 70.2, "f1": 68.4,
                 "lat": 92.5, "mem": 5.6, "is_baseline": True,
                 "details": "## Baseline\n\n固定随机种子与数据切分。"},
                {"id": "small", "model": "Small", "params": 12.5, "acc": 68.1, "f1": 66.2,
                 "lat": 61.4, "mem": 2.8},
                {"id": "wide", "model": "Wide", "params": 48.0, "acc": 75.3, "f1": 73.9,
                 "lat": 140.2, "mem": 11.4,
                 "details": "## Wide\n\n参数量翻倍，延迟明显上升。"},
                {"id": "ours", "model": "Ours", "params": 26.5, "acc": 77.6, "f1": 76.1,
                 "lat": 95.1, "mem": 5.9, "emphasis": True,
                 "cells": {"acc": {"value": 77.6, "note": "三次运行均值"},
                           "f1": {"value": 76.1, "display": "76.1", "note": "标准差 0.2"}},
                 "summary": "精度最高，延迟与基准基本持平。"},
            ],
        },
        {
            "type": "chart",
            "title": "分组柱状：质量与成本",
            "span": 1,
            "chart_type": "bar",
            "categories": ["Acc", "F1", "Throughput"],
            "series": [
                {"name": "Baseline", "data": [70.2, 68.4, 62.0], "summary": "基准实现"},
                {"name": "Ours", "data": [77.6, 76.1, 74.5],
                 "details": "## Ours\n\n统一训练配置。"},
            ],
            "unit": "%",
            "y_label": "Score",
            "ref_lines": [{"value": 75, "label": "target", "tone": "warn"}],
            "note": "参考线为验收阈值。",
        },
        {
            "type": "chart",
            "title": "训练曲线",
            "chart_type": "line",
            "x": ["1", "2", "3", "4", "5"],
            "series": [
                {"name": "Baseline", "data": [52.0, 61.4, 66.8, 69.1, 70.2]},
                {"name": "Ours", "data": [55.1, 65.2, 71.4, 75.0, 77.6]},
                {"name": "Ablation", "data": [50.2, 58.1, None, 66.4, 68.0], "dashed": True},
            ],
            "unit": "%",
            "y_label": "Acc",
            "ref_lines": [{"value": 75, "label": "target"}],
        },
        {
            "type": "chart",
            "title": "精度-延迟前沿",
            "span": 1,
            "chart_type": "scatter",
            "x_label": "Latency (ms)",
            "y_label": "Acc (%)",
            "pareto": "max_y_min_x",
            "points": [
                {"label": "Base", "x": 92.5, "y": 70.2, "group": "baseline"},
                {"label": "Small", "x": 61.4, "y": 68.1, "group": "candidate",
                 "summary": "延迟最低"},
                {"label": "Wide", "x": 140.2, "y": 75.3, "group": "candidate",
                 "details": "## Wide\n\n参数翻倍。"},
                {"label": "Ours", "x": 95.1, "y": 77.6, "group": "candidate", "highlight": True,
                 "details": "## Ours\n\n精度最高。"},
            ],
            "note": "虚线为帕累托前沿：同等延迟下精度最高。",
        },
        {
            "type": "chart",
            "title": "消融矩阵",
            "span": 1,
            "chart_type": "heatmap",
            "x_labels": ["w/o A", "w/o B", "full"],
            "y_labels": ["Seed 1", "Seed 2", "Seed 3"],
            "values": [[74.1, 75.2, 77.6], [73.8, 74.9, 77.2], [74.4, 75.6, 77.9]],
            "scale_mode": "max_good",
            "unit": "%",
            "cell_details": [
                [None, None, {"details": "## full\n\n完整配置。"}],
                [None, None, None],
                [None, None, None],
            ],
        },
        {
            "type": "prose",
            "title": "结论",
            "tone": "accent",
            "content": "## 结论\n\n- **Ours** 在同量级延迟下取得最高精度。\n"
                       "- 表格支持基准对比与三种导出格式。\n\n"
                       "| 维度 | 结论 |\n| --- | --- |\n| Quality | Ours 最优 |\n"
                       "| Cost | 与 Baseline 持平 |",
        },
    ],
}
SCIENTIFIC_TEXT = json.dumps(SCIENTIFIC_CANVAS, ensure_ascii=False)

TABLE_COLUMNS = [
    {"key": "model", "label": "Model"},
    {"key": "acc", "label": "Acc (%)", "higher_is_better": True, "decimals": 1},
    {"key": "lat", "label": "Lat_ms", "higher_is_better": False, "decimals": 1},
    {"key": "gain", "label": "Gain", "decimals": 2},
    {"key": "tag", "label": "Tag"},
]

TABLE_ROWS = [
    {"id": "base", "model": "Baseline", "acc": 70.2, "lat": 92.5, "gain": 0.0,
     "tag": "a_b & 100%", "is_baseline": True},
    {"id": "m1", "model": "Model A", "acc": 74.6, "lat": 88.1, "tag": "seed 1|2",
     "cells": {"gain": {"value": 4.4, "display": "+4.4", "note": "three runs"}}},
    {"id": "m2", "model": "Model B", "acc": 76.1, "lat": 95.4, "gain": 5.9, "tag": "mean"},
    {"id": "m3", "model": "Model C", "acc": 76.1, "lat": None, "gain": None, "tag": "n/a"},
]

NODES_ONLY = {
    "headline": "仅 nodes 的最小拓扑",
    "nodes": [
        {"id": "a", "label": "起点", "status": "ok"},
        {"id": "b", "label": "终点", "status": "warn"},
    ],
    "edges": [{"from": "a", "to": "b", "label": "触发"}],
}

# 6 列 × 2 行 = 12 节点：相邻前向边 + 跨列长边 + 逆向回环 + 同列互连 + 自环 + 长标签
SIX_COL_GRAPH = {
    "headline": "六列拓扑压测：回环、跨列长边与长标签同时出现",
    "badges": [{"text": "几何压测", "tone": "warn"}],
    "sections": [
        {
            "type": "graph",
            "title": "03 / 04 课程对照",
            "nodes": [
                {"id": "a1", "label": "03 课程导论", "tag": "03", "layer": 0, "status": "ok"},
                {"id": "b1", "label": "04 课程导论", "tag": "04", "layer": 0, "status": "info"},
                {"id": "a2", "label": "03 核心概念", "tag": "03", "layer": 1, "status": "ok"},
                {"id": "b2", "label": "04 核心概念", "tag": "04", "layer": 1, "status": "info"},
                {"id": "a3", "label": "03 建模方法", "tag": "03", "layer": 2, "status": "warn"},
                {"id": "b3", "label": "04 建模方法", "tag": "04", "layer": 2, "status": "info"},
                {"id": "a4", "label": "03 实验环节", "tag": "03", "layer": 3, "status": "ok"},
                {"id": "b4", "label": "04 实验环节", "tag": "04", "layer": 3, "status": "info"},
                {"id": "a5", "label": "03 课程项目", "tag": "03", "layer": 4, "status": "ok"},
                {"id": "b5", "label": "04 课程项目", "tag": "04", "layer": 4, "status": "info"},
                {"id": "a6", "label": "03 复盘总结", "tag": "03", "layer": 5, "status": "ok"},
                {"id": "b6", "label": "04 复盘总结", "tag": "04", "layer": 5, "status": "info"},
            ],
            "edges": [
                # 相邻前向：03 链
                {"from": "a1", "to": "a2", "label": "概念铺垫"},
                {"from": "a2", "to": "a3", "label": "方法迁移"},
                {"from": "a3", "to": "a4", "label": "实验验证"},
                {"from": "a4", "to": "a5", "label": "项目落地"},
                {"from": "a5", "to": "a6", "label": "产出复盘"},
                # 同一对节点的第二条边：强制同间隙标签碰撞
                {"from": "a1", "to": "a2", "label": "补强练习"},
                # 相邻前向：04 链，长标签把列间距逼到上限
                {"from": "b1", "to": "b2", "label": "导论到核心概念的完整铺垫"},
                {"from": "b2", "to": "b3", "label": "同一方法在 04 课程中的完整迁移路径与边界条件"},
                {"from": "b3", "to": "b4", "label": "实验"},
                {"from": "b4", "to": "b5", "label": "项目"},
                {"from": "b5", "to": "b6", "label": "复盘"},
                # 跨列长边 → 上方 Bypass 通道
                {"from": "a1", "to": "a4", "label": "导论直达实验"},
                {"from": "b1", "to": "b6", "label": "导论直达复盘总结的完整链路"},
                {"from": "a2", "to": "a6", "label": "核心概念直达复盘"},
                # 逆向回环 / 同列互连 / 自环 → 下方 Bypass 通道
                {"from": "a6", "to": "b2", "label": "复盘回流核心"},
                {"from": "a4", "to": "a2", "label": "实验反馈修正"},
                {"from": "a3", "to": "b3", "label": "同期对照"},
                {"from": "b6", "to": "b6", "label": "迭代自检"},
            ],
        }
    ],
}

RAIL_SESSIONS = [
    {"id": "pin-one", "label": "alpha · codex · pin-one", "project": "alpha",
     "client": "codex", "pinned": True, "created_at": "2025-12-31T09:00:00+00:00",
     "created_seq": 1, "updated_at": "2026-01-02T09:00:00+00:00", "seq": 12,
     "reply_count": 2},
    {"id": "alpha-old", "label": "alpha · codex · alpha-", "project": "alpha",
     "client": "codex", "pinned": False, "created_at": "2026-01-01T10:00:00+00:00",
     "created_seq": 2, "updated_at": "2026-01-01T10:00:00+00:00", "seq": 20,
     "reply_count": 3},
    {"id": "beta-one", "label": "beta · codex · beta-o", "project": "beta",
     "client": "codex", "pinned": False, "created_at": "2026-01-02T10:00:00+00:00",
     "created_seq": 3, "updated_at": "2026-01-02T10:00:00+00:00", "seq": 30,
     "reply_count": 1},
    {"id": "pin-two", "label": "beta · claude · pin-two", "project": "beta",
     "client": "claude", "pinned": True, "created_at": "2026-01-03T10:00:00+00:00",
     "created_seq": 4, "updated_at": "2026-01-03T10:00:00+00:00", "seq": 40,
     "reply_count": 1},
    {"id": "alpha-new", "label": "alpha · codex · alpha-n", "project": "alpha",
     "client": "codex", "pinned": False, "created_at": "2026-01-01T10:00:00+00:00",
     "created_seq": 5, "updated_at": "2026-01-01T10:00:00+00:00", "seq": 50,
     "reply_count": 1},
]


def rail_signature(tree: dict) -> dict:
    pinned = (tree.get("pinned") or {}).get("sessions") or []
    return {
        "pinned": [s.get("id") for s in pinned],
        "projects": [(g.get("key"), [s.get("id") for s in g.get("sessions", [])])
                     for g in tree.get("projects", [])],
        "totalCount": tree.get("totalCount"),
    }


NODE_DRIVER = r"""
const vm = require('vm');
const fs = require('fs');

const core = fs.readFileSync(process.argv[2], 'utf8');
const data = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const sandbox = { console: console };
vm.createContext(sandbox);
try {
  vm.runInContext(core, sandbox, { timeout: 5000 });
} catch (err) {
  console.log(JSON.stringify({ ok: false, error: '几何核心执行失败: ' + err.message }));
  process.exit(0);
}
if (!sandbox.GraphCore || typeof sandbox.GraphCore.layoutGraph !== 'function') {
  console.log(JSON.stringify({ ok: false, error: '未导出 GraphCore.layoutGraph' }));
  process.exit(0);
}
const geo = sandbox.GraphCore.layoutGraph(data.nodes, data.edges);
console.log(JSON.stringify({ ok: true, geometry: geo }));
"""


def run_graph_core(page: str, graph: dict, workdir: Path) -> dict:
    """抽出 PAGE_HTML 里的纯几何核心，在本机 node 的 vm 沙箱里实跑。"""
    node = shutil.which("node")
    if not node:
        return {"ok": False, "error": "node 未安装，无法跑几何断言"}
    start = "/* ==== VR_GRAPH_CORE_START ==== */"
    end = "/* ==== VR_GRAPH_CORE_END ==== */"
    if start not in page or end not in page:
        return {"ok": False, "error": "PAGE_HTML 缺少几何核心标记"}
    core = page.split(start, 1)[1].split(end, 1)[0]

    core_path = workdir / "graph_core.js"
    data_path = workdir / "graph_payload.json"
    driver_path = workdir / "graph_driver.js"
    core_path.write_text(core, encoding="utf-8")
    data_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    driver_path.write_text(NODE_DRIVER, encoding="utf-8")

    try:
        proc = subprocess.run(
            [node, str(driver_path), str(core_path), str(data_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"node 执行异常: {exc}"}
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(out)
    except ValueError:
        err = proc.stderr.decode("utf-8", errors="replace")[:300]
        return {"ok": False, "error": f"node 输出无法解析: {out[:200]} {err}"}


RAIL_DRIVER = r"""
const vm = require('vm');
const fs = require('fs');

const core = fs.readFileSync(process.argv[2], 'utf8');
const data = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const sandbox = { console: console };
vm.createContext(sandbox);
try {
  vm.runInContext(core, sandbox, { timeout: 5000 });
} catch (err) {
  console.log(JSON.stringify({ ok: false, error: '会话树核心执行失败: ' + err.message }));
  process.exit(0);
}
const api = sandbox.RailTreeCore;
if (!api || typeof api.buildRailTree !== 'function') {
  console.log(JSON.stringify({ ok: false, error: '未导出 RailTreeCore.buildRailTree' }));
  process.exit(0);
}
const unread = {};
(data.unread || []).forEach(function (id) { unread[id] = 1; });
const out = {};
try {
  if (data.op === 'build') {
    const opts = {
      filter: data.filter || 'all',
      unreadCountOf: function (s) { return Number(unread[s.id] || 0); }
    };
    if (data.boards) opts.boards = data.boards;
    if (data.nowMs !== undefined && data.nowMs !== null) opts.nowMs = data.nowMs;
    out.tree = api.buildRailTree(data.sessions, opts);
  } else if (data.op === 'compare') {
    out.order = (data.sessions || []).slice().sort(api.compareSessionsFixed)
      .map(function (s) { return s.id; });
  } else if (data.op === 'strip') {
    out.values = (data.sessions || []).map(function (s) { return api.stripProjectPrefix(s); });
  } else if (data.op === 'replies') {
    out.order = api.sortRepliesDesc(data.replies || []).map(function (r) { return r.reply_id; });
  } else if (data.op === 'parse') {
    out.values = (data.sessions || []).map(function (s) { return api.parseCreatedMs(s); });
  } else if (data.op === 'health') {
    out.values = (data.boards || []).map(function (b) {
      return api.computeBoardHealth(b, data.nowMs);
    });
  } else if (data.op === 'age') {
    out.values = (data.ages || []).map(function (a) { return api.formatAgeShort(a); });
  } else {
    out.error = 'unknown op';
  }
} catch (err) {
  console.log(JSON.stringify({ ok: false, error: 'RailTreeCore 执行失败: ' + err.message }));
  process.exit(0);
}
console.log(JSON.stringify({ ok: true, result: out }));
"""


def run_rail_core(page: str, payload: dict, workdir: Path) -> dict:
    """抽出 PAGE_HTML 里的纯会话树核心，在本机 node 的 vm 沙箱里实跑。"""
    node = shutil.which("node")
    if not node:
        return {"ok": False, "error": "node 未安装，无法跑会话树断言"}
    start = "/* ==== VR_RAIL_CORE_START ==== */"
    end = "/* ==== VR_RAIL_CORE_END ==== */"
    if start not in page or end not in page:
        return {"ok": False, "error": "PAGE_HTML 缺少会话树核心标记"}
    core = page.split(start, 1)[1].split(end, 1)[0]

    core_path = workdir / "rail_core.js"
    data_path = workdir / "rail_payload.json"
    driver_path = workdir / "rail_driver.js"
    core_path.write_text(core, encoding="utf-8")
    data_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    driver_path.write_text(RAIL_DRIVER, encoding="utf-8")

    try:
        proc = subprocess.run(
            [node, str(driver_path), str(core_path), str(data_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"node 执行异常: {exc}"}
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(out)
    except ValueError:
        err = proc.stderr.decode("utf-8", errors="replace")[:300]
        return {"ok": False, "error": f"node 输出无法解析: {out[:200]} {err}"}


TABLE_DRIVER = r"""
const vm = require('vm');
const fs = require('fs');

const core = fs.readFileSync(process.argv[2], 'utf8');
const data = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const sandbox = { console: console };
vm.createContext(sandbox);
try {
  vm.runInContext(core, sandbox, { timeout: 5000 });
} catch (err) {
  console.log(JSON.stringify({ ok: false, error: '表格核心执行失败: ' + err.message }));
  process.exit(0);
}
const api = sandbox.TableCore;
if (!api || typeof api.toLatex !== 'function') {
  console.log(JSON.stringify({ ok: false, error: '未导出 TableCore.toLatex' }));
  process.exit(0);
}
const out = {};
try {
  if (data.op === 'rank') out.ranks = api.rankColumns(data.columns, data.rows);
  else if (data.op === 'highlight') {
    out.values = (data.columns || []).map(function (c) { return api.effectiveHighlight(c); });
  } else if (data.op === 'delta') {
    out.values = (data.cases || []).map(function (c) {
      return api.computeDelta(c.num, c.base, c.col, c.decimals);
    });
  } else if (data.op === 'sort') {
    out.rows = api.sortRows(data.rows, data.colKey, data.dir, data.pinnedTopIds || [])
      .map(function (r) { return r.id; });
  } else if (data.op === 'filter') {
    out.rows = api.filterRows(data.rows, data.columns, data.query)
      .map(function (r) { return r.id; });
  } else if (data.op === 'latex') {
    out.value = api.toLatex(data.columns, data.rows, data.groups, data.opts || {});
  } else if (data.op === 'markdown') {
    out.value = api.toMarkdown(data.columns, data.rows, {});
  } else if (data.op === 'csv') {
    out.value = api.toCsv(data.columns, data.rows);
  } else if (data.op === 'escape') {
    out.values = (data.values || []).map(function (v) { return api.escapeLatex(v); });
  } else if (data.op === 'resolve') {
    out.values = (data.cases || []).map(function (c) {
      return api.resolveBaselineId(c.rows, c.explicitId);
    });
  } else if (data.op === 'decimals') {
    out.values = (data.cases || []).map(function (c) { return api.inferDecimals(c.col, c.values); });
  } else if (data.op === 'cell') {
    out.values = (data.rows || []).map(function (r) { return api.getCell(r, data.key); });
  } else if (data.op === 'parse') {
    out.values = (data.cells || []).map(function (raw) { return api.parseCell(raw); });
  } else {
    out.error = 'unknown op';
  }
} catch (err) {
  console.log(JSON.stringify({ ok: false, error: 'TableCore 执行失败: ' + err.message }));
  process.exit(0);
}
console.log(JSON.stringify({ ok: true, result: out }));
"""


def run_table_core(page: str, payload: dict, workdir: Path) -> dict:
    """抽出 PAGE_HTML 里的纯表格核心，在本机 node 的 vm 沙箱里实跑。"""
    node = shutil.which("node")
    if not node:
        return {"ok": False, "error": "node 未安装，无法跑表格断言"}
    start = "/* ==== VR_TABLE_CORE_START ==== */"
    end = "/* ==== VR_TABLE_CORE_END ==== */"
    if start not in page or end not in page:
        return {"ok": False, "error": "PAGE_HTML 缺少表格核心标记"}
    core = page.split(start, 1)[1].split(end, 1)[0]

    core_path = workdir / "table_core.js"
    data_path = workdir / "table_payload.json"
    driver_path = workdir / "table_driver.js"
    core_path.write_text(core, encoding="utf-8")
    data_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    driver_path.write_text(TABLE_DRIVER, encoding="utf-8")

    try:
        proc = subprocess.run(
            [node, str(driver_path), str(core_path), str(data_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"node 执行异常: {exc}"}
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(out)
    except ValueError:
        err = proc.stderr.decode("utf-8", errors="replace")[:300]
        return {"ok": False, "error": f"node 输出无法解析: {out[:200]} {err}"}


CHART_DRIVER = r"""
const vm = require('vm');
const fs = require('fs');

const core = fs.readFileSync(process.argv[2], 'utf8');
const data = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const sandbox = { console: console };
vm.createContext(sandbox);
try {
  vm.runInContext(core, sandbox, { timeout: 5000 });
} catch (err) {
  console.log(JSON.stringify({ ok: false, error: '图表核心执行失败: ' + err.message }));
  process.exit(0);
}
const api = sandbox.ChartCore;
if (!api || typeof api.layoutChart !== 'function') {
  console.log(JSON.stringify({ ok: false, error: '未导出 ChartCore.layoutChart' }));
  process.exit(0);
}
function collect(value, path, bad) {
  if (value === null || value === undefined) return;
  if (typeof value === 'number') {
    if (!isFinite(value)) bad.push(path + '=' + value);
    return;
  }
  if (Array.isArray(value)) {
    value.forEach(function (item, index) { collect(item, path + '[' + index + ']', bad); });
    return;
  }
  if (typeof value === 'object') {
    Object.keys(value).forEach(function (key) { collect(value[key], path + '.' + key, bad); });
  }
}
const out = {};
try {
  if (data.op === 'scale') {
    out.scale = api.niceScale(data.min, data.max, data.ticks, !!data.includeZero);
    out.bad = [];
    collect(out.scale, 'scale', out.bad);
  } else if (data.op === 'kind') {
    out.kind = api.chartKind(data.spec || {});
  } else {
    out.layout = api.layoutChart(data.spec || {});
    out.bad = [];
    collect(out.layout, 'layout', out.bad);
  }
} catch (err) {
  console.log(JSON.stringify({ ok: false, error: 'ChartCore 执行失败: ' + err.message }));
  process.exit(0);
}
console.log(JSON.stringify({ ok: true, result: out }));
"""


def run_chart_core(page: str, payload: dict, workdir: Path) -> dict:
    """抽出 PAGE_HTML 里的纯图表核心，在本机 node 的 vm 沙箱里实跑。"""
    node = shutil.which("node")
    if not node:
        return {"ok": False, "error": "node 未安装，无法跑图表断言"}
    start = "/* ==== VR_CHART_CORE_START ==== */"
    end = "/* ==== VR_CHART_CORE_END ==== */"
    if start not in page or end not in page:
        return {"ok": False, "error": "PAGE_HTML 缺少图表核心标记"}
    core = page.split(start, 1)[1].split(end, 1)[0]

    core_path = workdir / "chart_core.js"
    data_path = workdir / "chart_payload.json"
    driver_path = workdir / "chart_driver.js"
    core_path.write_text(core, encoding="utf-8")
    data_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    driver_path.write_text(CHART_DRIVER, encoding="utf-8")

    try:
        proc = subprocess.run(
            [node, str(driver_path), str(core_path), str(data_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"node 执行异常: {exc}"}
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(out)
    except ValueError:
        err = proc.stderr.decode("utf-8", errors="replace")[:300]
        return {"ok": False, "error": f"node 输出无法解析: {out[:200]} {err}"}


def estimate_label_px(text: str) -> int:
    """与前端 estimateLabelWidth 同口径：全角 12px，ASCII 7px。"""
    return sum(12 if ord(ch) > 0xFF else 7 for ch in text)


HTML_DOC = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>交互原型 Artifact</title>
<style>
  body { margin: 0; font-family: system-ui, sans-serif; background: #0b1020; color: #e8eefc; }
  .wrap { padding: 32px; }
  h1 { font-size: 22px; margin: 0 0 8px; }
  button { font: inherit; padding: 8px 16px; border-radius: 8px; border: 0;
           background: #4f7cff; color: #fff; cursor: pointer; }
  .out { margin-top: 14px; font-size: 14px; color: #9fb0d0; }
</style>
</head>
<body>
<div class="wrap">
  <h1>交互原型 Artifact</h1>
  <button id="go" type="button">点我计数</button>
  <div class="out" id="out">点击 0 次</div>
</div>
<script>
  var n = 0;
  document.getElementById('go').addEventListener('click', function () {
    n += 1;
    document.getElementById('out').textContent = '点击 ' + n + ' 次';
  });
</script>
</body>
</html>
"""

FAKE_V1 = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port = int(sys.argv[1])
cache = sys.argv[2]

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        body = json.dumps({"ok": True, "service": "visual-reply", "version": 1,
                           "pid": os.getpid(), "port": port, "cache_dir": cache}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

ThreadingHTTPServer.allow_reuse_address = True
httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
print("fake-v1-ready", flush=True)
httpd.serve_forever()
'''


SESSION_ENV_KEYS = (
    "VISUAL_REPLY_SESSION",
    "VISUAL_REPLY_PUBLIC_URL",
    "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_PROJECT_ID",
    "ANTIGRAVITY_AGENT",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "GEMINI_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_REPL_ID",
    "WT_SESSION",
    "TMUX_PANE",
    "WEZTERM_PANE",
    "KITTY_WINDOW_ID",
    "ITERM_SESSION_ID",
)


def clean_env(**extra) -> dict:
    """去掉宿主可能残留的会话变量，保证自动检测结果可预期。"""
    env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV_KEYS}
    env.update({k: str(v) for k, v in extra.items()})
    return env


def detect_project_root(start: Path) -> Path:
    current = start.resolve()
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return start.resolve()
        current = current.parent


def raw_request(port: int, method: str, path: str,
                body: bytes | str | None = None,
                headers: dict | None = None) -> tuple[int, bytes, dict]:
    """用 http.client 直发原始路径，绕过客户端对 ../ 的归一化。"""
    if isinstance(body, str):
        body = body.encode("utf-8")
    conn = http_client.HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data, dict(resp.getheaders())
    finally:
        conn.close()


def sse_connect(port: int, timeout: float = 6.0) -> socket.socket:
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        time.sleep(0.2)
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    request = (
        f"GET /api/events HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Accept: text/event-stream\r\n\r\n"
    )
    sock.sendall(request.encode("ascii"))
    return sock


def read_until(sock: socket.socket, needle: bytes, deadline: float) -> bytes:
    buffer = b""
    while needle not in buffer and time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buffer += chunk
    return buffer


def publish(port: int, cache: Path, content: str, title: str, fmt: str = "auto",
            env: dict | None = None, extra: list[str] | None = None) -> tuple[int, dict]:
    cmd = [sys.executable, str(PUBLISH),
           "--title", title, "--stdin", "--no-browser",
           "--format", fmt,
           "--port", str(port), "--cache-dir", str(cache)]
    if extra:
        cmd.extend(extra)
    proc = subprocess.run(
        cmd,
        input=content.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        env=env if env is not None else clean_env(),
    )
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(out)
    except ValueError:
        data = {"raw_stdout": out, "raw_stderr": proc.stderr.decode("utf-8", errors="replace")}
    return proc.returncode, data


def stop_server(cache: Path, port: int) -> None:
    pid_file = cache / f".server-{port}.json"
    if not pid_file.exists():
        return
    try:
        pid = json.loads(pid_file.read_text(encoding="utf-8")).get("pid")
    except (OSError, ValueError):
        return
    if not pid:
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    wait_port_closed(port)


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def main() -> int:
    cache = Path(tempfile.mkdtemp(prefix="vr-test-")).resolve()
    cache_v1 = Path(tempfile.mkdtemp(prefix="vr-test-v1-")).resolve()
    geo_dir = Path(tempfile.mkdtemp(prefix="vr-geo-")).resolve()
    stress_cache = Path(tempfile.mkdtemp(prefix="vr-stress-")).resolve()
    cold_cache = Path(tempfile.mkdtemp(prefix="vr-cold-")).resolve()
    store_cache = Path(tempfile.mkdtemp(prefix="vr-store-")).resolve()
    used_ports: set[int] = set()
    port = unique_port(used_ports)
    port_v1 = unique_port(used_ports)
    stress_port = unique_port(used_ports)
    cold_port = unique_port(used_ports)
    print(f"临时目录: {cache}")
    print(f"测试端口: {port}")
    print(f"并发测试: {stress_cache} @ {stress_port}")
    print(f"冷启动测试: {cold_cache} @ {cold_port}")

    fake_proc = None
    stress_proc = None

    try:
        # ---------- 1. md 发布（auto 识别） ----------
        print("\n[1] md 发布（auto 自动识别）")
        code, data = publish(port, cache, MD, "兼容模式测试")
        check("publish 退出码为 0", code == 0, str(data))
        check("publish 返回 ok", data.get("ok") is True, str(data))
        check("auto 识别为 md", data.get("format") == "md", str(data.get("format")))
        check("server 状态为 started", data.get("server") == "started", str(data))
        check("latest.md 已写入", (cache / "latest.md").is_file())
        check("meta.json 已写入", (cache / "meta.json").is_file())

        meta = json.loads((cache / "meta.json").read_text("utf-8"))
        check(f"meta.version == {EXPECTED_VERSION}", meta.get("version") == EXPECTED_VERSION, str(meta.get("version")))
        check("meta.format == md", meta.get("format") == "md", str(meta.get("format")))
        check("meta.payload_file == latest.md", meta.get("payload_file") == "latest.md",
              str(meta.get("payload_file")))
        check("meta.title 正确", meta.get("title") == "兼容模式测试", str(meta.get("title")))
        check("meta.updated_at 存在", bool(meta.get("updated_at")))
        check("meta.chars 正确", meta.get("chars") == len(MD), str(meta.get("chars")))

        hist_file = Path(data.get("history_file", ""))
        check("历史归档存在", hist_file.is_file() and hist_file.parent == cache / "history",
              str(hist_file))
        check("历史归档扩展名 .md", hist_file.suffix == ".md", hist_file.name)
        check("历史 sidecar 命名为 .md.meta.json",
              Path(data.get("history_meta", "")).name == hist_file.name + ".meta.json",
              str(data.get("history_meta")))

        status, body, headers = http(f"http://127.0.0.1:{port}/")
        page = body.decode("utf-8", errors="replace")
        check("GET / 返回 200", status == 200, str(status))
        check("Content-Type 为 html", "text/html" in headers.get("Content-Type", ""))
        for token in ("marked", "mermaid", "markmap-lib", "markmap-view", "d3@7"):
            check(f"页面引入 {token}", token in page)
        for view in ('data-view="reading"', 'data-view="mindmap"', 'data-view="split"'):
            check(f"页面包含视图按钮 {view}", view in page)
        check("页面包含暗色主题样式", 'html[data-theme="dark"]' in page)
        check("页面包含历史下拉", 'id="history-select"' in page)
        check("页面包含轮询兜底逻辑", "setInterval(poll, 3000)" in page)
        check("折叠样式会隐藏区块内容", ".sec.collapsed > .sec-body { display: none; }" in page)
        check("页面包含回到最新逻辑", "function refreshLatestNow" in page)

        status, latest = get_json(f"http://127.0.0.1:{port}/api/latest")
        check("GET /api/latest 返回 200", status == 200, str(status))
        check(f"latest.version == {EXPECTED_VERSION}", latest.get("version") == EXPECTED_VERSION, str(latest.get("version")))
        check("latest.format == md", latest.get("format") == "md", str(latest.get("format")))
        check("latest.title 正确", latest.get("title") == "兼容模式测试", str(latest.get("title")))
        check("latest.content 完整", latest.get("content") == MD)
        check("latest.mtime > 0", isinstance(latest.get("mtime"), (int, float))
              and latest.get("mtime") > 0, str(latest.get("mtime")))
        check("latest.updated_at 存在", bool(latest.get("updated_at")))

        status, history = get_json(f"http://127.0.0.1:{port}/api/history")
        check("GET /api/history 返回 200", status == 200, str(status))
        items = history.get("items", [])
        check("history 至少一条", len(items) >= 1, str(len(items)))
        check("history 条目标题正确", items[0].get("title") == "兼容模式测试", str(items[:1]))
        check("history 条目带 format", items[0].get("format") == "md", str(items[:1]))

        filename = items[0]["filename"]
        quoted = urllib.parse.quote(filename)
        status, item = get_json(f"http://127.0.0.1:{port}/api/history/{quoted}")
        check("GET /api/history/<file> 返回 200", status == 200, str(status))
        check("历史内容一致", item.get("content") == MD)
        check("历史条目 format 正确", item.get("format") == "md", str(item.get("format")))
        check("历史文件名带时间戳", filename[:8].isdigit() and filename.endswith(".md"), filename)
        check("md 模式下 /artifact/latest 返回 404",
              status_of(f"http://127.0.0.1:{port}/artifact/latest") == 404)

        # ---------- 2. json 发布 ----------
        print("\n[2] json 发布（Visual Canvas）")
        time.sleep(0.02)
        code, data = publish(port, cache, CANVAS_TEXT, "视觉画布测试")
        check("json publish 退出码为 0", code == 0, str(data))
        check("auto 识别为 json", data.get("format") == "json", str(data.get("format")))
        check("server 状态为 reused", data.get("server") == "reused", str(data))
        check("latest.json 已写入", (cache / "latest.json").is_file())

        meta = json.loads((cache / "meta.json").read_text("utf-8"))
        check("meta.format 切换为 json", meta.get("format") == "json", str(meta.get("format")))
        check("meta.payload_file == latest.json", meta.get("payload_file") == "latest.json",
              str(meta.get("payload_file")))
        check(f"meta.version 仍为 {EXPECTED_VERSION}", meta.get("version") == EXPECTED_VERSION, str(meta.get("version")))

        jh = Path(data.get("history_file", ""))
        check("json 历史载荷扩展名 .json", jh.suffix == ".json", jh.name)
        check("json 历史 sidecar 为 .json.meta.json",
              Path(data.get("history_meta", "")).name == jh.name + ".meta.json",
              str(data.get("history_meta")))
        check("json 历史 sidecar 存在", Path(data.get("history_meta", "")).is_file())

        status, latest = get_json(f"http://127.0.0.1:{port}/api/latest")
        check("api/latest 切换到 json", latest.get("format") == "json", str(latest.get("format")))
        check("api/latest 内容与发布一致", latest.get("content") == CANVAS_TEXT)
        check("json 标题取 headline 之外的显式标题", latest.get("title") == "视觉画布测试",
              str(latest.get("title")))
        check("json 模式下 /artifact/latest 返回 404",
              status_of(f"http://127.0.0.1:{port}/artifact/latest") == 404)

        parsed = json.loads(latest.get("content", "{}"))
        check("画布 headline 存在", bool(parsed.get("headline")))
        check("画布 badges 存在", isinstance(parsed.get("badges"), list)
              and len(parsed["badges"]) == 2, str(parsed.get("badges")))
        section_types = [s.get("type") for s in parsed.get("sections", [])]
        check("五种组件元数据齐全",
              section_types == ["kpis", "graph", "comparison", "bento", "steps"],
              str(section_types))

        graph = next(s for s in parsed["sections"] if s["type"] == "graph")
        check("graph 节点带 details", all("details" in n for n in graph["nodes"]))
        check("graph 节点带 files", any(n.get("files") for n in graph["nodes"]))
        check("graph 边带 label", any(e.get("label") for e in graph["edges"]))
        check("graph 节点带 layer", all("layer" in n for n in graph["nodes"]))
        check("graph 节点状态取值合法",
              {n.get("status") for n in graph["nodes"]} <= {"ok", "warn", "err", "info"},
              str({n.get("status") for n in graph["nodes"]}))

        cmp_sec = next(s for s in parsed["sections"] if s["type"] == "comparison")
        check("comparison 含推荐项",
              any(i.get("recommended") for i in cmp_sec["items"]))
        check("comparison 含评分维度",
              all(isinstance(i.get("scores"), dict) for i in cmp_sec["items"]))

        bento = next(s for s in parsed["sections"] if s["type"] == "bento")
        check("bento 含 span 卡片", any(c.get("span") == 2 for c in bento["cards"]))
        check("bento tone 取值合法",
              {c.get("tone") for c in bento["cards"]} <= {"accent", "ok", "warn", "err", None},
              str({c.get("tone") for c in bento["cards"]}))

        steps = next(s for s in parsed["sections"] if s["type"] == "steps")
        check("steps 状态取值合法",
              {s.get("status") for s in steps["steps"]} <= {"done", "active", "pending"},
              str({s.get("status") for s in steps["steps"]}))

        status, history = get_json(f"http://127.0.0.1:{port}/api/history")
        check("json 历史排在首位", history["items"][0].get("format") == "json",
              str(history["items"][:1]))
        check("json 历史 title 正确", history["items"][0].get("title") == "视觉画布测试",
              str(history["items"][0].get("title")))

        # ---------- 3. 前端组件检测 ----------
        print("\n[3] PAGE_HTML 前端组件检测")
        status, body, _ = http(f"http://127.0.0.1:{port}/")
        page = body.decode("utf-8", errors="replace")
        for fn in ("renderKpis", "renderGraph", "renderComparison", "renderBento", "renderSteps",
                   "layoutGraph", "computeLayers", "openDrawer", "closeDrawer",
                   "highlightGraph", "normalizeCanvas"):
            check(f"前端实现 {fn}", f"function {fn}" in page)
        for token in ("视觉画布", "原始数据", "data-drawer", 'id="drawer"',
                      'id="drawer-details"', 'id="drawer-files"', "Esc 关闭",
                      "allow-scripts", "/artifact/latest", "artifact/history/"):
            check(f"前端包含 {token}", token in page)
        check("前端支持 HTML 全屏/源码切换", "data-sub" in page and "源码" in page)
        check("graph 使用 SVG 贝塞尔连线",
              "marker-end" in page and " C" in page and "graph-svg" in page)

        # ---------- 4. html 发布 ----------
        print("\n[4] html 发布（Artifact 沙箱）")
        time.sleep(0.02)
        code, data = publish(port, cache, HTML_DOC, "交互原型 Artifact")
        check("html publish 退出码为 0", code == 0, str(data))
        check("auto 识别为 html", data.get("format") == "html", str(data.get("format")))
        check("latest.html 已写入", (cache / "latest.html").is_file())

        meta = json.loads((cache / "meta.json").read_text("utf-8"))
        check("meta.format 切换为 html", meta.get("format") == "html", str(meta.get("format")))
        check("meta.payload_file == latest.html", meta.get("payload_file") == "latest.html",
              str(meta.get("payload_file")))
        hh = Path(data.get("history_file", ""))
        check("html 历史载荷扩展名 .html", hh.suffix == ".html", hh.name)
        check("html 历史 sidecar 存在", Path(data.get("history_meta", "")).is_file())
        check("html 标题从 <title> 提取", data.get("title") == "交互原型 Artifact",
              str(data.get("title")))

        status, body, headers = http(f"http://127.0.0.1:{port}/artifact/latest")
        check("GET /artifact/latest 返回 200", status == 200, str(status))
        check("artifact Content-Type 为 text/html",
              "text/html" in headers.get("Content-Type", ""),
              headers.get("Content-Type", ""))
        check("artifact 编码为 utf-8", "utf-8" in headers.get("Content-Type", "").lower(),
              headers.get("Content-Type", ""))
        check("artifact 原文一致", body.decode("utf-8") == HTML_DOC)

        status, latest = get_json(f"http://127.0.0.1:{port}/api/latest")
        check("api/latest 切换到 html", latest.get("format") == "html", str(latest.get("format")))
        check("api/latest html 内容一致", latest.get("content") == HTML_DOC)

        status, history = get_json(f"http://127.0.0.1:{port}/api/history")
        html_name = history["items"][0]["filename"]
        check("html 历史排在首位", history["items"][0].get("format") == "html",
              str(history["items"][:1]))
        status, body, headers = http(
            f"http://127.0.0.1:{port}/artifact/history/{urllib.parse.quote(html_name)}")
        check("GET /artifact/history/<file> 返回 200", status == 200, str(status))
        check("历史 artifact Content-Type 为 text/html",
              "text/html" in headers.get("Content-Type", ""),
              headers.get("Content-Type", ""))
        check("历史 artifact 原文一致", body.decode("utf-8") == HTML_DOC)

        # ---------- 5. md 显式发布 + 混合历史 ----------
        print("\n[5] md 显式发布与混合历史")
        time.sleep(0.02)
        code, data = publish(port, cache, MD.replace("# 兼容模式测试", "# 兼容模式测试（回退）"),
                             "兼容回退", fmt="md")
        check("--format md 生效", data.get("format") == "md", str(data.get("format")))
        status, latest = get_json(f"http://127.0.0.1:{port}/api/latest")
        check("回退后 api/latest 为 md", latest.get("format") == "md", str(latest.get("format")))
        check("回退后内容正确", "兼容模式测试（回退）" in latest.get("content", ""))
        check("回退后 /artifact/latest 返回 404",
              status_of(f"http://127.0.0.1:{port}/artifact/latest") == 404)

        status, history = get_json(f"http://127.0.0.1:{port}/api/history")
        items = history["items"]
        check("混合历史累计 >= 4 条", len(items) >= 4, str(len(items)))
        check("混合历史仅记录真实载荷（不含 sidecar）",
              len(items) == len({i["filename"] for i in items}), str([i["filename"] for i in items]))
        formats = {i.get("format") for i in items}
        check("历史覆盖 md/json/html", formats == {"md", "json", "html"}, str(formats))
        check("混合历史按时间倒序",
              all(items[i]["mtime"] >= items[i + 1]["mtime"] for i in range(len(items) - 1)),
              str([round(i["mtime"], 3) for i in items]))
        check("历史条目均带 updated_at", all(i.get("updated_at") for i in items))
        json_items = [i for i in items if i["format"] == "json"]
        check("旧版 md 的 .json sidecar 未被误判为 json 记录",
              len(json_items) == 1, str([i["filename"] for i in json_items]))

        # 同名同秒跨格式发布，不应互相遮挡
        publish(port, cache, MD, "同名冲突", fmt="md")
        code, data = publish(port, cache, CANVAS_TEXT, "同名冲突", fmt="json")
        check("同名跨格式 publish 成功", code == 0 and data.get("ok") is True, str(data))
        status, history = get_json(f"http://127.0.0.1:{port}/api/history")
        same = [i for i in history["items"] if i["title"] == "同名冲突"]
        check("同名 md/json 各留一条", len(same) == 2, str([i["filename"] for i in same]))
        check("同名记录格式不重复", {i["format"] for i in same} == {"md", "json"},
              str([i["format"] for i in same]))
        check("同名记录文件名不同", same[0]["filename"] != same[1]["filename"],
              str([i["filename"] for i in same]))

        # nodes-only 的 auto 识别
        code, data = publish(port, cache, json.dumps(NODES_ONLY, ensure_ascii=False), "最小拓扑")
        check("仅含 nodes 的 JSON 也识别为 json", data.get("format") == "json",
              str(data.get("format")))

        # ---------- 6. 路径穿越防护 ----------
        print("\n[6] 非法路径防护")
        for label, url in [
            ("/api/history/../meta.json", f"http://127.0.0.1:{port}/api/history/../meta.json"),
            ("/api/history/%2e%2e%2fmeta.json",
             f"http://127.0.0.1:{port}/api/history/%2e%2e%2fmeta.json"),
            ("/api/history/..%5Cmeta.json",
             f"http://127.0.0.1:{port}/api/history/..%5Cmeta.json"),
            ("/api/history/.server-x.json",
             f"http://127.0.0.1:{port}/api/history/.server-x.json"),
        ]:
            code = status_of(url)
            check(f"穿越被拒绝 {label}", code in (400, 404), str(code))

        for label, url in [
            ("/artifact/history/..%2F..%2Fmeta.json",
             f"http://127.0.0.1:{port}/artifact/history/..%2F..%2Fmeta.json"),
            ("/artifact/history/%2e%2e%2f%2e%2e%2fREADME.html",
             f"http://127.0.0.1:{port}/artifact/history/%2e%2e%2f%2e%2e%2fREADME.html"),
            ("/artifact/history/../latest.html",
             f"http://127.0.0.1:{port}/artifact/history/../latest.html"),
        ]:
            code = status_of(url)
            check(f"穿越被拒绝 {label}", code in (400, 404), str(code))

        # ---------- 7. 旧版 v1 服务接管 ----------
        print("\n[7] 旧版 v1 服务接管升级")
        fake_script = cache_v1 / "fake_v1.py"
        fake_script.write_text(FAKE_V1, encoding="utf-8")
        fake_proc = subprocess.Popen(
            [sys.executable, str(fake_script), str(port_v1), str(cache_v1)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )

        def v1_up() -> bool:
            try:
                _, info = get_json(f"http://127.0.0.1:{port_v1}/healthz")
                return info.get("version") == 1
            except Exception:
                return False

        check("假 v1 服务已就绪", wait_for(v1_up, 10), "无法连接假 v1 服务")

        code, data = publish(port_v1, cache_v1, CANVAS_TEXT, "接管测试")
        check("接管 publish 退出码为 0", code == 0, str(data))
        check("报告 server == upgraded", data.get("server") == "upgraded", str(data.get("server")))
        check(f"报告 server_version == {EXPECTED_VERSION}", data.get("server_version") == EXPECTED_VERSION,
              str(data.get("server_version")))

        def current_up() -> bool:
            try:
                _, info = get_json(f"http://127.0.0.1:{port_v1}/healthz")
                return info.get("version") == EXPECTED_VERSION
            except Exception:
                return False

        check(f"端口上运行的是 v{EXPECTED_VERSION} 服务",
              wait_for(current_up, 10), f"v{EXPECTED_VERSION} 未接管端口")
        _, info = get_json(f"http://127.0.0.1:{port_v1}/healthz")
        check(f"接管后 healthz 版本为 {EXPECTED_VERSION}", info.get("version") == EXPECTED_VERSION, str(info))
        check("接管后缓存目录正确", Path(info.get("cache_dir", "")).resolve() == cache_v1,
              str(info.get("cache_dir")))
        if fake_proc is not None:
            check("旧进程已被终止", fake_proc.poll() is not None, str(fake_proc.poll()))

        # ---------- 8. 复杂 6 列回环拓扑几何 ----------
        print("\n[8] 复杂 6 列回环拓扑几何断言")
        status, body, _ = http(f"http://127.0.0.1:{port}/")
        page = body.decode("utf-8", errors="replace")
        graph = SIX_COL_GRAPH["sections"][0]

        result = run_graph_core(page, graph, geo_dir)
        geo = result.get("geometry") if result.get("ok") else None
        check("几何核心可在 node vm 中执行", bool(geo), str(result.get("error")))

        if geo:
            width = geo["width"]
            height = geo["height"]
            nodes = geo["nodes"]
            edges = geo["edges"]

            check("画布尺寸为正", width > 0 and height > 0, f"{width}x{height}")
            check("节点尺寸 NW=204 / NH=68", geo["nw"] == 204 and geo["nh"] == 68,
                  f'{geo["nw"]}x{geo["nh"]}')
            check("6 列生成 5 个列间隙", len(geo["gaps"]) == 5, str(geo["gaps"]))
            check("12 个节点全部完成布局", len(nodes) == 12, str(len(nodes)))
            check("节点覆盖 6 列",
                  sorted({n["col"] for n in nodes}) == [0, 1, 2, 3, 4, 5],
                  str(sorted({n["col"] for n in nodes})))
            check("18 条边全部生成几何", len(edges) == 18, str(len(edges)))

            # 1. 边界：节点 / 端点 / 控制点 / 标签全部落在画布内
            out_nodes = [n["id"] for n in nodes
                         if n["x"] < 0 or n["y"] < 0
                         or n["x"] + n["w"] > width or n["y"] + n["h"] > height]
            check("节点矩形全部在画布内", not out_nodes, str(out_nodes))

            bad_pts = []
            for e in edges:
                for px, py in e["pts"]:
                    if px < -0.01 or py < -0.01 or px > width + 0.01 or py > height + 0.01:
                        bad_pts.append((e["from"], e["to"], round(px, 1), round(py, 1)))
            check("所有端点与控制点不越界", not bad_pts, str(bad_pts[:4]))

            bad_labels = []
            for e in edges:
                if not e.get("label"):
                    continue
                half = e["labelW"] / 2.0 + 10
                if e["labelX"] - half < -0.01 or e["labelX"] + half > width + 0.01:
                    bad_labels.append((e["from"], e["to"], "x", e["labelX"]))
                if e["labelY"] - 11 < -0.01 or e["labelY"] + 11 > height + 0.01:
                    bad_labels.append((e["from"], e["to"], "y", e["labelY"]))
            check("边标签胶囊完整落在画布内", not bad_labels, str(bad_labels[:4]))

            # 2. 通道：Bypass 轨道必须完全避开节点矩形
            bypass = [e for e in edges if e["kind"] in ("top", "bottom")]
            top_edges = [e for e in bypass if e["kind"] == "top"]
            bottom_edges = [e for e in bypass if e["kind"] == "bottom"]
            check("Bypass 边共 7 条（3 上 + 4 下）", len(bypass) == 7, str(len(bypass)))
            check("跨列长边全部走上通道", len(top_edges) == 3, str(len(top_edges)))
            check("逆向回环全部走下通道", len(bottom_edges) == 4, str(len(bottom_edges)))

            crossed = []
            for e in bypass:
                ly = e["laneY"]
                for n in nodes:
                    if n["y"] - 0.01 <= ly <= n["y"] + n["h"] + 0.01:
                        crossed.append((e["from"], e["to"], ly, n["id"]))
            check("Bypass 轨道与所有节点矩形零交集", not crossed, str(crossed[:4]))

            top_lanes = geo["lanes"]["top"]
            bottom_lanes = geo["lanes"]["bottom"]
            check("上下通道数量与边分类一致",
                  len(top_lanes) == 3 and len(bottom_lanes) == 4,
                  f"{top_lanes} / {bottom_lanes}")
            check("通道间距恒为 34px",
                  all(abs(b - a) == 34 for a, b in zip(top_lanes, top_lanes[1:]))
                  and all(abs(b - a) == 34 for a, b in zip(bottom_lanes, bottom_lanes[1:])),
                  f"{top_lanes} / {bottom_lanes}")
            check("画布上下留白随通道数扩张",
                  geo["padTop"] == 34 + 3 * 34 and geo["padBottom"] == 34 + 4 * 34,
                  f'{geo["padTop"]} / {geo["padBottom"]}')

            # 3. 列间距：按相邻前向边最长标签动态计算
            col_of = {n["id"]: n["layer"] for n in graph["nodes"]}
            max_px = []
            for gi in range(5):
                widest = 0
                for e in graph["edges"]:
                    label = e.get("label") or ""
                    if not label:
                        continue
                    if col_of[e["from"]] == gi and col_of[e["to"]] == gi + 1:
                        widest = max(widest, estimate_label_px(label))
                max_px.append(widest)
            expected_gaps = [max(108, min(230, px + 36)) for px in max_px]
            check("列间距按最长标签动态计算", geo["gaps"] == expected_gaps,
                  f'{geo["gaps"]} != {expected_gaps}')
            check("未触顶的列间隙容得下最长标签",
                  all(geo["gaps"][i] >= max_px[i] + 36
                      for i in range(5) if max_px[i] + 36 <= 230),
                  f'{geo["gaps"]} / {max_px}')
            check("超长标签的列间隙封顶 230", geo["gaps"][1] == 230, str(geo["gaps"]))
            check("短标签的列间隙保持 108 下限", min(geo["gaps"]) == 108, str(geo["gaps"]))

            # 4. 标签避让：同间隙相邻标签 Y 间距 >= 22px
            gap_groups = {}
            for e in edges:
                if e["kind"] != "adj" or not e.get("label"):
                    continue
                gap_groups.setdefault((e["colFrom"], e["colTo"]), []).append(e["labelY"])
            too_close = []
            for key, ys in gap_groups.items():
                ys = sorted(ys)
                for a, b in zip(ys, ys[1:]):
                    if b - a < 22:
                        too_close.append((key, round(a, 1), round(b, 1)))
            check("同间隙相邻标签 Y 间距 >= 22px", not too_close, str(too_close[:4]))

            dup = [e for e in edges
                   if e["from"] == "a1" and e["to"] == "a2" and e.get("label")]
            check("同一对节点的两条边标签被推开",
                  len(dup) == 2 and abs(dup[0]["labelY"] - dup[1]["labelY"]) >= 22,
                  str([e["labelY"] for e in dup]))

            # 5. 前端实现：transform 缩放 + 顶层胶囊标签
            check("前端已移除 style.zoom", "style.zoom" not in page)
            check("前端使用 transform: scale 缩放",
                  "style.transform = 'scale('" in page)
            check("存在缩放尺寸容器 .graph-scale-box", "graph-scale-box" in page)
            check("存在顶层标签层 .graph-labels", "graph-labels" in page)
            check("存在胶囊标签 .g-edge-pill", "g-edge-pill" in page)
            check("标签层 z-index 为 3",
                  ".graph-labels { position: absolute; inset: 0; z-index: 3;" in page)
            check("连线层 z-index 为 1", "z-index: 1; pointer-events: none;" in page)
            check("节点层 z-index 为 2", "position: absolute; z-index: 2;" in page)
            check("默认缩放允许低至 0.35", "GRAPH_MIN_SCALE = 0.35" in page)
            check("节点宽高常量为 204x68", "NW: 204" in page and "NH: 68" in page)
            check("节点标签支持两行自动换行", "-webkit-line-clamp: 2" in page)
            for act in ("fit", "reset", "out", "in"):
                check(f"工具栏包含 {act} 操作", f'data-gact="{act}"' in page)
            check("悬停联动胶囊标签高亮", "pills.forEach(function (p) {" in page)

        # ---------- 9. 发布长度提醒 ----------
        print("\n[9] publish.py 长度提醒")
        long_payload = {
            "headline": "这是一条故意写得非常长的结论标题用来验证发布脚本的长度提醒是否正常工作并且不阻塞发布流程",
            "sections": [{
                "type": "graph",
                "nodes": [{"id": "x", "label": "起点"}, {"id": "y", "label": "终点"}],
                "edges": [{"from": "x", "to": "y", "label": "这是一条超过十四个字的超长边标签"}],
            }],
        }
        code, data = publish(port, cache, json.dumps(long_payload, ensure_ascii=False), "超长提醒")
        check("超长载荷仍发布成功", code == 0 and data.get("ok") is True, str(data))
        warnings = data.get("warnings")
        check("返回 warnings 数组", isinstance(warnings, list) and bool(warnings), str(warnings))
        warnings = warnings or []
        check("warnings 点名 headline", any("headline" in w for w in warnings), str(warnings))
        check("warnings 点名 edge.label", any("边标签" in w for w in warnings), str(warnings))
        status, latest = get_json(f"http://127.0.0.1:{port}/api/latest")
        check("超长载荷照常可访问", latest.get("format") == "json"
              and "超长边标签" in latest.get("content", ""))

        code, data = publish(port, cache, json.dumps(NODES_ONLY, ensure_ascii=False), "正常长度")
        check("正常载荷不返回 warnings", "warnings" not in data, str(data.get("warnings")))
        check("正常载荷退出码为 0", code == 0, str(data))

        # ---------- 10. 文档硬规范 ----------
        print("\n[10] SKILL.md 长度硬规范")
        skill_text = SKILL_MD.read_text(encoding="utf-8")
        check("SKILL.md 写明 headline ≤ 30 字", "≤ 30 字" in skill_text)
        check("SKILL.md 写明 node.label ≤ 12 字", "≤ 12 字" in skill_text)
        check("SKILL.md 写明 edge.label ≤ 10 字", "≤ 10 字" in skill_text)
        check("SKILL.md 说明 warnings 不阻塞发布",
              "warnings" in skill_text and "退出码仍为 0" in skill_text)
        check("publish.py 与 server.py 版本一致",
              source_version(PUBLISH) == EXPECTED_VERSION,
              f"{source_version(PUBLISH)} != {EXPECTED_VERSION}")

        # ---------- 11. 开源卫生检查 ----------
        print("\n[11] 开源卫生检查")
        root = SCRIPT_DIR.parent
        skill_text = SKILL_MD.read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")
        readme_cn = (root / "README_CN.md").read_text(encoding="utf-8")
        license_text = (root / "LICENSE").read_text(encoding="utf-8")
        publish_text = PUBLISH.read_text(encoding="utf-8")

        # 扫描器本身也在扫描范围内，模式用动态拼接构造，避免自我误报
        home_patterns = ("/" + "home/", "/" + "Users/", "C:" + "\\Users\\")
        secret_patterns = (
            "sk-" + "ant-", "sk-" + "proj-", "gho" + "_", "ghp" + "_",
            "@qq" + ".com", "1c7507" + "a3",
        )
        author_handle = "kyrie" + "21z"

        store_text = STORE.read_text(encoding="utf-8")
        scanned = [skill_text, readme, readme_cn, publish_text,
                   SERVER.read_text(encoding="utf-8"),
                   store_text,
                   Path(__file__).read_text(encoding="utf-8")]
        names = ["SKILL.md", "README.md", "README_CN.md", "scripts/publish.py",
                 "scripts/server.py", "scripts/vr_store.py",
                 "scripts/test_visual_reply.py"]

        home_hits = [f"{n}: {p}" for n, t in zip(names, scanned) for p in home_patterns if p in t]
        secret_hits = [f"{n}: {m}" for n, t in zip(names, scanned)
                       for m in secret_patterns if m in t]
        check("公开文件不含本机绝对路径", not home_hits, str(home_hits[:6]))
        check("公开文件不含密钥或私人标识", not secret_hits, str(secret_hits[:6]))

        # 作者 handle 只允许出现在 README 安装命令与 LICENSE 中
        handle_hits = [n for n, t in zip(names, scanned)
                       if n == "SKILL.md" or n.endswith(".py") if author_handle in t]
        check("SKILL.md 与脚本不含作者 handle", not handle_hits, str(handle_hits))

        check("LICENSE 为 MIT 且署名正确",
              "MIT License" in license_text
              and f"Copyright (c) 2026 {author_handle}" in license_text)

        check("README.md 链接中文文档", "README_CN.md" in readme)
        check("README_CN.md 链接英文文档", "](README.md)" in readme_cn)
        check("README 提供 npx 安装命令",
              f"npx skills add {author_handle}/visual-reply" in readme
              and f"npx skills add {author_handle}/visual-reply" in readme_cn)
        check("README 覆盖四类客户端软链", all(
            d in readme for d in (".claude/skills", ".codex/skills",
                                  ".gemini/antigravity-cli/skills", ".agents/skills")))
        check("README 含可复制的单行 publish 示例",
              "scripts/publish.py --title" in readme and "--stdin" in readme)
        check("README 说明零第三方依赖", "standard library" in readme
              and "3.10" in readme)
        check("README 说明测试命令", "python3 scripts/test_visual_reply.py" in readme)
        check("SKILL.md 使用 ${SKILL_DIR} 变量", "${SKILL_DIR}/scripts/publish.py" in skill_text)
        check("SKILL.md 不含任何主目录前缀", "/" + "home/" not in skill_text)

        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        for entry in ("__pycache__/", "*.py[cod]", ".pytest_cache/", ".DS_Store",
                      ".venv/", "*.log"):
            check(f".gitignore 忽略 {entry}", entry in gitignore)

        workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        check("CI 在 push / pull_request 触发",
              "push:" in workflow and "pull_request:" in workflow and "main" in workflow)
        check("CI 运行于 ubuntu-latest", "ubuntu-latest" in workflow)
        check("CI 使用 setup-python@v5",
              "actions/setup-python@v5" in workflow)
        check("CI 矩阵覆盖 3.11 / 3.12 / 3.13",
              all(f'"{v}"' in workflow for v in ("3.11", "3.12", "3.13")))
        check("CI 使用 setup-node@v4 且 Node 22",
              "actions/setup-node@v4" in workflow and 'node-version: "22"' in workflow)
        check("CI 运行测试脚本",
              "python3 scripts/test_visual_reply.py" in workflow)

        # 跨平台打开浏览器：结构存在 + 函数可调用
        check("open_browser 仅 macOS 使用 open",
              'sys.platform == "darwin"' in publish_text)
        check("open_browser 覆盖 xdg-open / wslview / explorer.exe",
              all(t in publish_text for t in ("xdg-open", "wslview", "explorer.exe")))
        check("explorer.exe 绝对路径仅在存在时使用",
              'if os.path.exists(fallback)' in publish_text)
        check("open_browser 以 webbrowser.open 兜底",
              "webbrowser.open(url)" in publish_text)

        spec = importlib.util.spec_from_file_location("vr_publish", PUBLISH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidates = module.browser_candidates("http://127.0.0.1:18765")
        check("browser_candidates 返回 (渠道, argv) 列表",
              isinstance(candidates, list) and all(
                  isinstance(c, tuple) and len(c) == 2
                  and isinstance(c[0], str) and isinstance(c[1], list)
                  and c[1] and c[1][-1] == "http://127.0.0.1:18765"
                  for c in candidates),
              str(candidates))

        # ---------- 12. 多会话隔离与深链回执 ----------
        print("\n[12] 多会话隔离与深链回执")
        base = f"http://127.0.0.1:{port}"

        code, first = publish(port, cache, MD, "会话 A 第一条",
                              env=clean_env(ANTIGRAVITY_CONVERSATION_ID="conv-alpha"))
        check("Antigravity 会话发布成功", code == 0 and first.get("ok") is True, str(first))
        check("会话 ID 取自 ANTIGRAVITY_CONVERSATION_ID",
              first.get("session_id") == "conv-alpha", str(first.get("session_id")))

        receipt_url = urllib.parse.urlparse(first.get("url", ""))
        receipt_qs = urllib.parse.parse_qs(receipt_url.query)
        check("回执 url 为 ?session=&reply= 深链",
              receipt_qs.get("session") == ["conv-alpha"]
              and receipt_qs.get("reply") == [first.get("reply_id")],
              str(first.get("url")))
        check("回执包含 session_url 与 base_url",
              first.get("session_url", "").endswith("?session=conv-alpha")
              and first.get("base_url", "").startswith("http://127.0.0.1:"),
              f'{first.get("session_url")} / {first.get("base_url")}')
        check("回执包含 seq / reply_id / session_name",
              isinstance(first.get("seq"), int) and bool(first.get("reply_id"))
              and bool(first.get("session_name")), str(first))
        check("回执报告 notified", first.get("notified") is True, str(first.get("notified")))

        session_file = cache / "sessions" / "conv-alpha" / "session.json"
        session_rec = json.loads(session_file.read_text("utf-8"))
        check("会话 client 标记为 antigravity", session_rec.get("client") == "antigravity",
              str(session_rec.get("client")))
        check("会话 source 记录环境变量来源",
              session_rec.get("source") == "env:ANTIGRAVITY_CONVERSATION_ID",
              str(session_rec.get("source")))
        check("会话 color 为固定色板值", str(session_rec.get("color", "")).startswith("#"),
              str(session_rec.get("color")))

        code, second = publish(port, cache, MD, "会话 A 第二条",
                               env=clean_env(ANTIGRAVITY_CONVERSATION_ID="conv-alpha"))
        check("同一会话第二条复用 session_id", second.get("session_id") == "conv-alpha",
              str(second.get("session_id")))
        check("会话内 seq 递增", second.get("seq") == first.get("seq") + 1,
              f'{second.get("seq")} != {first.get("seq")} + 1')

        status, replies_a = get_json(f"{base}/api/sessions/conv-alpha/replies")
        check("GET /api/sessions/<sid>/replies 返回 200", status == 200, str(status))
        rows_a = replies_a.get("replies", [])
        check("会话回复条数为 2", len(rows_a) == 2, str(len(rows_a)))
        check("会话回复最新在前", rows_a and rows_a[0].get("reply_id") == second.get("reply_id"),
              str([r.get("reply_id") for r in rows_a]))

        _, codex = publish(port, cache, MD, "Codex 线程", env=clean_env(CODEX_THREAD_ID="thread-9"))
        check("CODEX_THREAD_ID 映射到独立会话", codex.get("session_id") == "thread-9",
              str(codex.get("session_id")))
        _, opencode = publish(port, cache, MD, "OpenCode 会话",
                              env=clean_env(OPENCODE_SESSION_ID="ses_open_1"))
        check("OPENCODE_SESSION_ID 映射到独立会话",
              opencode.get("session_id") == "ses_open_1", str(opencode.get("session_id")))
        _, gemini = publish(port, cache, MD, "Gemini 会话", env=clean_env(GEMINI_SESSION_ID="gem-1"))
        check("GEMINI_SESSION_ID 映射到独立会话", gemini.get("session_id") == "gem-1",
              str(gemini.get("session_id")))
        _, claude = publish(port, cache, MD, "Claude 会话",
                            env=clean_env(CLAUDE_CODE_REPL_ID="repl-7"))
        check("CLAUDE_CODE_REPL_ID 映射到独立会话", claude.get("session_id") == "repl-7",
              str(claude.get("session_id")))

        _, wt_one = publish(port, cache, MD, "终端会话一", env=clean_env(WT_SESSION="wt-abc"))
        _, wt_two = publish(port, cache, MD, "终端会话二", env=clean_env(WT_SESSION="wt-abc"))
        _, wt_other = publish(port, cache, MD, "终端会话三", env=clean_env(WT_SESSION="wt-xyz"))
        check("WT_SESSION 生成终端会话 ID",
              str(wt_one.get("session_id", "")).startswith("t-"), str(wt_one.get("session_id")))
        check("同一终端窗口保持同一会话",
              wt_one.get("session_id") == wt_two.get("session_id"),
              f'{wt_one.get("session_id")} != {wt_two.get("session_id")}')
        check("不同终端窗口拆成不同会话",
              wt_one.get("session_id") != wt_other.get("session_id"),
              f'{wt_one.get("session_id")} / {wt_other.get("session_id")}')

        code, explicit = publish(port, cache, MD, "显式会话",
                                 env=clean_env(ANTIGRAVITY_CONVERSATION_ID="conv-alpha"),
                                 extra=["--session", "team-x", "--session-name", "发布线程"])
        check("--session 优先级高于环境变量", explicit.get("session_id") == "team-x",
              str(explicit.get("session_id")))
        check("--session-name 写入显示名", explicit.get("session_name") == "发布线程",
              str(explicit.get("session_name")))
        explicit_rec = json.loads(
            (cache / "sessions" / "team-x" / "session.json").read_text("utf-8"))
        check("--session-name 标记 custom_label", explicit_rec.get("custom_label") is True,
              str(explicit_rec.get("custom_label")))
        check("--session-name 持久化到 session.json", explicit_rec.get("label") == "发布线程",
              str(explicit_rec.get("label")))

        code, rejected = publish(port, cache, MD, "非法会话", extra=["--session", "../evil"])
        check("非法 --session 被拒绝", code == 1 and rejected.get("ok") is False,
              f"{code} {rejected}")
        check("非法 --session 报错点名会话 ID", "会话 ID" in str(rejected.get("error", "")),
              str(rejected.get("error")))

        _, fallback = publish(port, cache, MD, "兜底会话")
        project_root = detect_project_root(Path.cwd())
        expected_id = hashlib.sha256(
            f"{os.getpid()}|{project_root}".encode("utf-8")).hexdigest()[:12]
        check("无环境变量时按 sha256(ppid|项目根) 兜底",
              fallback.get("session_id") == expected_id,
              f'{fallback.get("session_id")} != {expected_id}')
        fallback_rec = json.loads(
            (cache / "sessions" / expected_id / "session.json").read_text("utf-8"))
        check("兜底会话 source 为 fallback:ppid",
              fallback_rec.get("source") == "fallback:ppid", str(fallback_rec.get("source")))

        older_file = cache / "sessions" / "conv-alpha" / "session.json"
        newer_file = cache / "sessions" / "team-x" / "session.json"
        older_before = json.loads(older_file.read_text("utf-8"))
        newer_rec = json.loads(newer_file.read_text("utf-8"))
        check("新会话写入递增的 created_seq",
              int(older_before.get("created_seq") or 0) > 0
              and int(newer_rec.get("created_seq") or 0)
              > int(older_before.get("created_seq") or 0),
              f'{older_before.get("created_seq")} / {newer_rec.get("created_seq")}')

        code, older_reply = publish(port, cache, MD, "旧会话追加回复",
                                    env=clean_env(ANTIGRAVITY_CONVERSATION_ID="conv-alpha"))
        check("旧会话追加回复成功", code == 0 and older_reply.get("ok") is True, str(older_reply))
        older_after = json.loads(older_file.read_text("utf-8"))
        check("created_seq 跨后续回复保持不变",
              older_after.get("created_seq") == older_before.get("created_seq"),
              f'{older_after.get("created_seq")} != {older_before.get("created_seq")}')
        check("created_at 跨后续回复保持不变",
              older_after.get("created_at") == older_before.get("created_at"),
              f'{older_after.get("created_at")} != {older_before.get("created_at")}')

        status, sessions_payload = get_json(f"{base}/api/sessions")
        check("GET /api/sessions 返回 200", status == 200, str(status))
        check(f"sessions 版本为 {EXPECTED_VERSION}", sessions_payload.get("version") == EXPECTED_VERSION,
              str(sessions_payload.get("version")))
        listed = {s.get("id"): s for s in sessions_payload.get("sessions", [])}
        check("会话列表包含全部新建会话",
              {"conv-alpha", "thread-9", "ses_open_1", "gem-1", "repl-7", "team-x"} <= set(listed),
              str(sorted(listed)))
        check("会话列表按固定创建顺序倒序",
              all(int(sessions_payload["sessions"][i].get("created_seq") or 0)
                  >= int(sessions_payload["sessions"][i + 1].get("created_seq") or 0)
                  for i in range(len(sessions_payload["sessions"]) - 1)),
              str([s.get("created_seq") for s in sessions_payload.get("sessions", [])]))
        listed_order = [s.get("id") for s in sessions_payload.get("sessions", [])]
        check("旧会话收到新回复后仍排在新会话之后",
              listed_order.index("team-x") < listed_order.index("conv-alpha"),
              str(listed_order))
        check("会话列表含 legacy_count 汇总", "legacy_count" in sessions_payload,
              str(sessions_payload.get("legacy_count")))

        spec = importlib.util.spec_from_file_location("vr_publish_flags", PUBLISH)
        flags_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(flags_module)
        parsed_flags = flags_module.parse_args(
            ["--session", "s1", "--session-name", "名字", "--agent", "codex"])
        check("publish 支持 --session / --session-name / --agent",
              parsed_flags.session == "s1" and parsed_flags.session_name == "名字"
              and parsed_flags.agent == "codex", str(vars(parsed_flags)))

        # ---------- 13. 多进程并发写入 ----------
        print("\n[13] 多进程并发写入（8 并发 × 3 会话）")
        stress_base = f"http://127.0.0.1:{stress_port}"
        stress_proc = subprocess.Popen(
            [sys.executable, str(SERVER), "--port", str(stress_port),
             "--cache-dir", str(stress_cache)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )

        def stress_up() -> bool:
            try:
                _, info = get_json(f"{stress_base}/healthz")
                return (info.get("version") == EXPECTED_VERSION
                        and Path(info.get("cache_dir", "")).resolve() == stress_cache)
            except Exception:
                return False

        check("并发测试服务已就绪", wait_for(stress_up, 12), "无法连接并发测试服务")

        def run_job(item):
            index, session_id = item
            return publish(
                stress_port, stress_cache,
                f"# 并发 {index}\n\n第 {index} 条并发内容。\n",
                f"并发 {index}",
                fmt="md",
                extra=["--session", session_id],
            )

        plan = [(i, ["stress-a", "stress-b", "stress-c"][i % 3]) for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run_job, plan))

        checks_codes = [code for code, _ in results]
        receipts = [data for _, data in results]
        check("8 个并发 publish 退出码全为 0", checks_codes == [0] * 8, str(checks_codes))
        check("8 个并发 publish 全部 ok", all(r.get("ok") is True for r in receipts),
              str([r.get("ok") for r in receipts]))
        reply_ids = [r.get("reply_id") for r in receipts]
        check("并发回复 ID 无重复", len(set(reply_ids)) == 8, str(reply_ids))
        seqs = sorted(int(r.get("seq") or 0) for r in receipts)
        check("并发后 seq 严格单调 1..8", seqs == list(range(1, 9)), str(seqs))

        status, stress_sessions = get_json(f"{stress_base}/api/sessions")
        by_id = {s.get("id"): s for s in stress_sessions.get("sessions", [])}
        check("并发建立 3 个会话",
              {"stress-a", "stress-b", "stress-c"} <= set(by_id), str(sorted(by_id)))
        check("并发会话回复计数正确",
              by_id["stress-a"].get("reply_count") == 3
              and by_id["stress-b"].get("reply_count") == 3
              and by_id["stress-c"].get("reply_count") == 2,
              str({k: v.get("reply_count") for k, v in by_id.items()}))

        for session_id, expected in (("stress-a", 3), ("stress-b", 3), ("stress-c", 2)):
            status, reply_payload = get_json(
                f"{stress_base}/api/sessions/{session_id}/replies")
            rows = reply_payload.get("replies", [])
            check(f"{session_id} 回复条数为 {expected}", len(rows) == expected, str(len(rows)))
            check(f"{session_id} 回复按 seq 倒序",
                  all(int(rows[i]["seq"]) >= int(rows[i + 1]["seq"])
                      for i in range(len(rows) - 1)),
                  str([r.get("seq") for r in rows]))

        stress_meta = json.loads((stress_cache / "meta.json").read_text("utf-8"))
        check("并发后 meta.json 仍可解析", isinstance(stress_meta, dict)
              and stress_meta.get("seq") == 8, str(stress_meta.get("seq")))
        latest_path = stress_cache / "latest.md"
        check("并发后 latest.md 仍可读", latest_path.is_file()
              and bool(latest_path.read_text("utf-8").strip()))

        sidecars = list((stress_cache / "sessions").glob("*/replies/*.meta.json"))
        check("并发后回复 sidecar 共 8 份", len(sidecars) == 8, str(len(sidecars)))
        parse_errors = []
        for sidecar in sidecars:
            try:
                json.loads(sidecar.read_text("utf-8"))
            except ValueError:
                parse_errors.append(sidecar.name)
        check("并发后所有 sidecar 均为合法 JSON", not parse_errors, str(parse_errors[:3]))

        feed_path = stress_cache / "global" / "feed.jsonl"
        feed_lines = [line for line in feed_path.read_text("utf-8").splitlines() if line.strip()]
        feed_seqs = []
        for line in feed_lines:
            try:
                feed_seqs.append(int(json.loads(line).get("seq") or 0))
            except ValueError:
                feed_seqs.append(-1)
        check("feed.jsonl 记录 8 条且 seq 连续", feed_seqs == list(range(1, 9)), str(feed_seqs))

        status, stress_state = get_json(f"{stress_base}/api/state?since=0")
        check("并发后 /api/state 补播 8 条事件",
              len(stress_state.get("events", [])) == 8, str(len(stress_state.get("events", []))))

        # ---------- 13b. 冷启动并发（无预启动服务） ----------
        print("\n[13b] 冷启动并发（4 进程同时拉起服务）")
        cold_base = f"http://127.0.0.1:{cold_port}"

        def cold_job(item):
            index, session_id = item
            return publish(
                cold_port, cold_cache,
                f"# 冷启动 {index}\n\n第 {index} 条并发内容。\n",
                f"冷启动 {index}",
                fmt="md",
                extra=["--session", session_id],
            )

        cold_plan = [(i, "cold-" + chr(ord("a") + i)) for i in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            cold_results = list(pool.map(cold_job, cold_plan))

        cold_codes = [code for code, _ in cold_results]
        cold_receipts = [data for _, data in cold_results]
        check("冷启动 4 个并发 publish 全部退出 0", cold_codes == [0] * 4, str(cold_codes))
        check("冷启动 4 个并发 publish 全部 ok",
              all(r.get("ok") is True for r in cold_receipts),
              str([r.get("ok") for r in cold_receipts]))
        cold_servers = [r.get("server") for r in cold_receipts]
        check("冷启动只有 1 个进程真正 start",
              cold_servers.count("started") == 1 and cold_servers.count("reused") == 3,
              str(cold_servers))
        cold_seqs = sorted(int(r.get("seq") or 0) for r in cold_receipts)
        check("冷启动回复 seq 为 1..4", cold_seqs == [1, 2, 3, 4], str(cold_seqs))

        status, cold_health = get_json(f"{cold_base}/healthz")
        pid_file_cold = cold_cache / f".server-{cold_port}.json"
        cold_pid = None
        if pid_file_cold.exists():
            cold_pid = json.loads(pid_file_cold.read_text("utf-8")).get("pid")
        check("冷启动只留下一个服务 PID",
              status == 200 and bool(cold_pid) and cold_health.get("pid") == cold_pid,
              f'{cold_health.get("pid")} vs {cold_pid}')
        check("冷启动服务版本正确", cold_health.get("version") == EXPECTED_VERSION,
              str(cold_health.get("version")))

        status, cold_sessions = get_json(f"{cold_base}/api/sessions")
        cold_ids = {s.get("id") for s in cold_sessions.get("sessions", [])}
        check("冷启动 4 个会话各自归档",
              cold_ids == {session_id for _, session_id in cold_plan}, str(sorted(cold_ids)))
        check("冷启动每个会话各 1 条回复",
              all(s.get("reply_count") == 1 for s in cold_sessions.get("sessions", [])),
              str([s.get("reply_count") for s in cold_sessions.get("sessions", [])]))
        status, cold_state = get_json(f"{cold_base}/api/state?since=0")
        check("冷启动事件恰好 4 条", len(cold_state.get("events", [])) == 4,
              str(len(cold_state.get("events", []))))

        # ---------- 14. SSE 实时推送与快速退出 ----------
        print("\n[14] SSE 实时推送 / 增量补播 / SIGTERM 快速退出")
        sse = sse_connect(port)
        banner = read_until(sse, b"retry: 1500", time.time() + 6)
        check("SSE 返回 200", banner.startswith(b"HTTP/1.") and b" 200 " in banner.split(b"\r\n")[0],
              banner[:80])
        check("SSE Content-Type 为 text/event-stream",
              b"text/event-stream" in banner.lower(), banner[:200])
        check("SSE 首帧为 : connected 注释", b": connected" in banner, banner[:120])
        check("SSE 下发 retry: 1500", b"retry: 1500" in banner)

        started_at = time.time()
        code, live = publish(port, cache, MD, "SSE 实时推送",
                             env=clean_env(CODEX_THREAD_ID="sse-thread"))
        frame = read_until(sse, b"event: reply", started_at + 4)
        elapsed = time.time() - started_at
        check("发布后 SSE 在 2 秒内推送", b"event: reply" in frame and elapsed < 2.0,
              f"{elapsed:.2f}s")

        live_events = []
        for match in re.finditer(rb"data: (.+?)\r?\n", frame):
            try:
                live_events.append(json.loads(match.group(1).decode("utf-8")))
            except ValueError:
                continue
        live_event = next((e for e in live_events if e.get("reply_id") == live.get("reply_id")), None)
        check("SSE 事件与发布回执一致", live_event is not None, str(live_events[:2]))
        if live_event:
            check("SSE 事件带 session_id / seq",
                  live_event.get("session_id") == "sse-thread"
                  and live_event.get("seq") == live.get("seq"), str(live_event))
            check("SSE 事件带标题与格式",
                  live_event.get("title") == "SSE 实时推送"
                  and live_event.get("format") == "md", str(live_event))
            check("SSE 事件是 bytes 单帧（含 id/event/data）",
                  b"id: " + str(live.get("seq")).encode() in frame
                  and b"event: reply" in frame, frame[-300:])

        status, since_payload = get_json(
            f"{base}/api/state?since={int(live.get('seq') or 0) - 1}")
        check("GET /api/state?since= 可补播该事件",
              any(e.get("reply_id") == live.get("reply_id")
                  for e in since_payload.get("events", [])),
              str(since_payload.get("events"))[:300])
        check("/api/state 返回最新会话指针",
              since_payload.get("latest_session_id") == "sse-thread",
              str(since_payload.get("latest_session_id")))
        try:
            sse.close()
        except OSError:
            pass

        # SIGTERM：挂着 SSE 连接也必须毫秒级退出
        sse2 = sse_connect(stress_port)
        banner2 = read_until(sse2, b": connected", time.time() + 6)
        check("并发测试服务 SSE 已连接", b": connected" in banner2, banner2[:120])
        stress_pid = None
        pid_file_stress = stress_cache / f".server-{stress_port}.json"
        if pid_file_stress.exists():
            stress_pid = json.loads(pid_file_stress.read_text("utf-8")).get("pid")
        term_start = time.time()
        if stress_pid:
            os.kill(int(stress_pid), signal.SIGTERM)
        stress_closed = wait_port_closed(stress_port, timeout=6)
        term_elapsed = time.time() - term_start
        check("SIGTERM 在 1.5s 内退出（SSE 连接仍开着）",
              bool(stress_pid) and stress_closed and term_elapsed < 1.5,
              f"{term_elapsed:.3f}s closed={stress_closed}")
        try:
            sse2.close()
        except OSError:
            pass
        if stress_proc is not None:
            try:
                stress_proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                stress_proc.kill()
            check("并发测试服务进程已回收", stress_proc.poll() is not None)

        # ---------- 15. 会话元数据 / 路径穿越 / 会话 Artifact ----------
        print("\n[15] 会话置顶重命名、路径穿越防护、会话 Artifact")
        status, body, _ = raw_request(
            port, "POST", "/api/sessions/conv-alpha",
            body=json.dumps({"label": "重命名后的会话", "pinned": True}, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
        )
        check("POST /api/sessions/<sid> 返回 200", status == 200, f"{status} {body[:120]}")
        updated = json.loads(body.decode("utf-8"))
        check("重命名写入 label", updated.get("session", {}).get("label") == "重命名后的会话",
              str(updated.get("session", {}).get("label")))
        check("置顶写入 pinned", updated.get("session", {}).get("pinned") is True,
              str(updated.get("session", {}).get("pinned")))

        status, pinned_payload = get_json(f"{base}/api/sessions")
        check("置顶会话排在列表首位",
              pinned_payload.get("sessions") and pinned_payload["sessions"][0].get("id") == "conv-alpha",
              str([s.get("id") for s in pinned_payload.get("sessions", [])][:3]))

        status, body, _ = raw_request(
            port, "POST", "/api/sessions/conv-alpha", body="{not-json",
            headers={"Content-Type": "application/json"})
        check("POST 非法 JSON 返回 400", status == 400, str(status))

        status, body, _ = raw_request(
            port, "POST", "/api/sessions/does-not-exist", body='{"label":"x"}',
            headers={"Content-Type": "application/json"})
        check("POST 未知会话返回 404", status == 404, str(status))

        for label, path in [
            ("/api/sessions/../replies", "/api/sessions/../replies"),
            ("/api/sessions/%2e%2e/replies", "/api/sessions/%2e%2e/replies"),
            ("/api/sessions/../../etc", "/api/sessions/../../etc"),
            ("/artifact/session/../x", "/artifact/session/../x"),
            ("/artifact/session/..%2Fmeta.json", "/artifact/session/..%2Fmeta.json"),
            ("/artifact/session/%2e%2e/%2e%2e", "/artifact/session/%2e%2e/%2e%2e"),
        ]:
            status, _, _ = raw_request(port, "GET", path)
            check(f"会话路径穿越被拒绝 {label}", status in (400, 404), str(status))

        status, _, _ = raw_request(
            port, "POST", "/api/sessions/..", body='{"pinned":true}',
            headers={"Content-Type": "application/json"})
        check("POST 路径穿越返回 400", status == 400, str(status))

        code, artifact_receipt = publish(
            port, cache, HTML_DOC, "会话 Artifact", env=clean_env(),
            extra=["--session", "art-1"])
        check("会话 Artifact 发布成功", code == 0 and artifact_receipt.get("ok") is True,
              str(artifact_receipt))
        artifact_rid = urllib.parse.quote(artifact_receipt.get("reply_id", ""), safe="")
        status, body, headers = raw_request(
            port, "GET", f"/artifact/session/art-1/{artifact_rid}")
        check("GET /artifact/session/<sid>/<rid> 返回 200", status == 200, str(status))
        check("会话 Artifact 为 text/html",
              "text/html" in headers.get("Content-Type", ""), headers.get("Content-Type", ""))
        check("会话 Artifact 原文一致", body.decode("utf-8") == HTML_DOC)

        code, markdown_receipt = publish(
            port, cache, MD, "会话里的 Markdown", env=clean_env(),
            extra=["--session", "art-1"])
        md_rid = urllib.parse.quote(markdown_receipt.get("reply_id", ""), safe="")
        status, _, _ = raw_request(port, "GET", f"/artifact/session/art-1/{md_rid}")
        check("非 html 回复的 artifact 返回 404", status == 404, str(status))

        status, _, _ = raw_request(port, "GET", "/artifact/session/art-1/..%2F..%2Fmeta.json")
        check("artifact rid 穿越被拒绝", status in (400, 404), str(status))

        # ---------- 16. 工作台前端标记 ----------
        print("\n[16] 多会话工作台前端标记")
        status, body, _ = http(f"{base}/")
        page = body.decode("utf-8", errors="replace")

        for token in ('id="session-rail"', 'id="pane-primary"', 'id="pane-secondary"',
                      'id="new-reply-banner"', 'id="mode-btn"', 'id="rail-toggle"',
                      'id="feed-btn"', 'id="reading-pane"', 'id="content"',
                      'id="canvas-root"', 'id="artifact-frame"', 'id="artifact-source"',
                      'id="mindmap-pane"', 'id="history-select"', 'id="drawer"'):
            check(f"页面保留/新增 {token}", token in page)

        for fn in ("renderIntoPane", "openSplit", "closeSplit", "handleEvent",
                   "focusSession", "renderRail", "renderFeed", "connectEvents",
                   "switchToLatestReply", "unreadCount", "togglePin", "renameSession",
                   "poll", "refreshLatestNow", "applyUrl", "writeUrl", "readUrl",
                   "totalUnread", "updateDocumentTitle", "refitPanes",
                   "railBoardCard", "loadBoard", "focusBoard", "handleBoardEvent",
                   "refreshBoardClocks", "snapshotPaneView", "restorePaneView"):
            check(f"前端实现 {fn}", f"function {fn}" in page)

        for token in ("new EventSource('/api/events')", "URLSearchParams",
                      "history.replaceState", "popstate", "vr.lastSeen",
                      "vr.collapsedGroups", "vr.expandedSessions",
                      "VR_RAIL_CORE_START", "VR_RAIL_CORE_END", "RailTreeCore",
                      "buildRailTree", "compareSessionsFixed", "sortRepliesDesc",
                      "stripProjectPrefix", "rail-group-head", "rail-group-pinned",
                      "rail-replies", "rail-reply", "rail-reply-hint",
                      "rail-project-pill", "aria-expanded",
                      "锁定会话", "跟随全局", "本会话有新回复", "切到最新",
                      "全局时间线", "未读", "置顶", "分屏", "data-filter=",
                      "/api/sessions/", "/api/state?since=", "/api/feed",
                      'data-pane-role="reading"', 'pane-canvas-root', 'pane-artifact-frame',
                      "app.lastHandledSeq", "seq <= app.lastHandledSeq",
                      ".pane-set.view-split .pane-frame { flex-direction: column; }",
                      "pane.name === 'primary') closeDrawer();",
                      "'(' + unread + ') '", "setTimeout(function () {",
                      ".rail-board", "rail-board-dot", "board-status-pill",
                      "computeBoardHealth", "formatAgeShort", "tbl-meter", "tbl-sub",
                      "实时面板", "app.sse.addEventListener('board'",
                      "refreshBoardClocks, 5000", "params.get('board')", "'board:' +"):
            check(f"页面包含 {token}", token in page)

        check("会话树容器带 role=tree",
              'id="rail-list" role="tree"' in page, page[page.find('id="rail-list"'):][:80])
        check("会话节点声明 treeitem 层级",
              "card.setAttribute('role', 'treeitem')" in page
              and "card.setAttribute('aria-level', '2')" in page
              and "item.setAttribute('aria-level', '3')" in page)

        print("\n[16b] 会话树核心（Node VM）")
        rail_result = run_rail_core(
            page, {"op": "build", "sessions": RAIL_SESSIONS, "unread": ["alpha-old"]}, geo_dir)
        tree = (rail_result.get("result") or {}).get("tree") if rail_result.get("ok") else None
        check("会话树核心可在 node vm 中执行", bool(tree), str(rail_result.get("error")))

        if tree:
            pinned_ids = [s.get("id") for s in (tree.get("pinned") or {}).get("sessions", [])]
            project_ids = [s.get("id") for g in tree.get("projects", [])
                           for s in g.get("sessions", [])]
            check("置顶会话单独成组且按创建顺序固定",
                  pinned_ids == ["pin-two", "pin-one"], str(pinned_ids))
            check("置顶会话不会重复出现在项目组",
                  not (set(pinned_ids) & set(project_ids)), str(project_ids))
            check("非置顶会话按 project 分组",
                  [(g.get("key"), [s.get("id") for s in g.get("sessions", [])])
                   for g in tree.get("projects", [])]
                  == [("beta", ["beta-one"]), ("alpha", ["alpha-new", "alpha-old"])],
                  str([(g.get("key"), [s.get("id") for s in g.get("sessions", [])])
                       for g in tree.get("projects", [])]))
            check("tree.totalCount 等于匹配会话数",
                  tree.get("totalCount") == len(RAIL_SESSIONS), str(tree.get("totalCount")))
            check("项目组按组内最新创建顺序排列",
                  [g.get("key") for g in tree.get("projects", [])] == ["beta", "alpha"],
                  str([g.get("key") for g in tree.get("projects", [])]))
            alpha_group = next(g for g in tree["projects"] if g["key"] == "alpha")
            check("项目内按 created_at / created_seq 倒序",
                  [s["id"] for s in alpha_group["sessions"]] == ["alpha-new", "alpha-old"],
                  str([s["id"] for s in alpha_group["sessions"]]))

            baseline = rail_signature(tree)
            mutated = json.loads(json.dumps(RAIL_SESSIONS))
            for session in mutated:
                if session["id"] == "alpha-old":
                    session["updated_at"] = "2031-01-01T00:00:00+00:00"
                    session["seq"] = 999
            mutated_result = run_rail_core(
                page, {"op": "build", "sessions": mutated, "unread": ["alpha-old"]}, geo_dir)
            mutated_tree = ((mutated_result.get("result") or {}).get("tree")
                            if mutated_result.get("ok") else None)
            check("旧会话收到新回复后会话与项目位置不变",
                  bool(mutated_tree) and rail_signature(mutated_tree) == baseline,
                  f"{rail_signature(mutated_tree or {})} != {baseline}")
            if mutated_tree:
                alpha_after = next(g for g in mutated_tree["projects"] if g["key"] == "alpha")
                check("项目 newestCreatedSeq 不被回复 seq 污染",
                      alpha_after.get("newestCreatedSeq") == 5,
                      str(alpha_after.get("newestCreatedSeq")))

            compare_result = run_rail_core(page, {"op": "compare", "sessions": mutated}, geo_dir)
            check("compareSessionsFixed 忽略 updated_at / seq",
                  (compare_result.get("result") or {}).get("order")
                  == ["pin-two", "beta-one", "alpha-new", "alpha-old", "pin-one"],
                  str(compare_result))

        strip_result = run_rail_core(page, {"op": "strip", "sessions": [
            {"id": "a", "project": "alpha", "label": "alpha · codex · abcdef",
             "custom_label": False},
            {"id": "b", "project": "alpha", "label": "alpha · 自定义名", "custom_label": True},
            {"id": "c", "label": "独立会话"},
        ]}, geo_dir)
        check("stripProjectPrefix 去掉默认项目前缀且保留自定义名",
              (strip_result.get("result") or {}).get("values")
              == ["codex · abcdef", "alpha · 自定义名", "独立会话"],
              str(strip_result))

        replies_result = run_rail_core(page, {"op": "replies", "replies": [
            {"reply_id": "r1", "seq": 1},
            {"reply_id": "r2", "seq": 3},
            {"reply_id": "r3", "seq": 2},
        ]}, geo_dir)
        check("sortRepliesDesc 把最新回复放在 index 0",
              (replies_result.get("result") or {}).get("order") == ["r2", "r3", "r1"],
              str(replies_result))

        parse_result = run_rail_core(page, {"op": "parse", "sessions": [
            {"created_at": "2026-01-01T00:00:00Z"},
            {"updated_at": "2026-01-02T00:00:00Z"},
            {},
        ]}, geo_dir)
        check("parseCreatedMs 解析 Z 后缀并回退 updated_at",
              (parse_result.get("result") or {}).get("values")
              == [1767225600000, 1767312000000, 0],
              str(parse_result))

        unread_result = run_rail_core(page, {
            "op": "build", "sessions": RAIL_SESSIONS,
            "unread": ["alpha-old"], "filter": "unread"}, geo_dir)
        unread_tree = ((unread_result.get("result") or {}).get("tree")
                       if unread_result.get("ok") else None)
        check("unread 筛选只保留未读会话",
              bool(unread_tree) and unread_tree.get("totalCount") == 1
              and [(g.get("key"), [s.get("id") for s in g.get("sessions", [])])
                   for g in unread_tree.get("projects", [])] == [("alpha", ["alpha-old"])],
              str(unread_tree))

        pinned_result = run_rail_core(page, {
            "op": "build", "sessions": RAIL_SESSIONS, "filter": "pinned"}, geo_dir)
        pinned_tree = ((pinned_result.get("result") or {}).get("tree")
                       if pinned_result.get("ok") else None)
        check("pinned 筛选只返回置顶组",
              bool(pinned_tree) and pinned_tree.get("totalCount") == 2
              and not pinned_tree.get("projects")
              and [s.get("id") for s in (pinned_tree.get("pinned") or {}).get("sessions", [])]
              == ["pin-two", "pin-one"], str(pinned_tree))

        print("\n[16c] 表格核心（Node VM）")
        rank_result = run_table_core(
            page, {"op": "rank", "columns": TABLE_COLUMNS, "rows": TABLE_ROWS}, geo_dir)
        ranks = (rank_result.get("result") or {}).get("ranks") or {}
        check("表格核心可在 node vm 中执行", bool(ranks), str(rank_result.get("error")))
        if ranks:
            check("rankColumns 并列最优共享 best，次优取下一档",
                  ranks.get("acc", {}).get("best") == 76.1
                  and ranks.get("acc", {}).get("second") == 74.6,
                  str(ranks.get("acc")))
            check("rankColumns 对越低越好的列取最小值",
                  ranks.get("lat", {}).get("best") == 88.1
                  and ranks.get("lat", {}).get("second") == 92.5,
                  str(ranks.get("lat")))
            check("rankColumns 未声明方向时不排名",
                  ranks.get("gain") == {"best": None, "second": None}, str(ranks.get("gain")))

        highlight_result = run_table_core(page, {"op": "highlight", "columns": [
            {"key": "a", "higher_is_better": True},
            {"key": "b", "higher_is_better": False},
            {"key": "c", "highlight": "max", "higher_is_better": False},
            {"key": "d", "highlight": "none", "higher_is_better": True},
            {"key": "e"},
        ]}, geo_dir)
        check("effectiveHighlight 支持 max / min / none",
              (highlight_result.get("result") or {}).get("values")
              == ["max", "min", "max", "none", "none"], str(highlight_result))

        delta_result = run_table_core(page, {"op": "delta", "cases": [
            {"num": 76.1, "base": 70.2, "col": {"higher_is_better": True}, "decimals": 1},
            {"num": 88.1, "base": 92.5, "col": {"higher_is_better": False}, "decimals": 1},
            {"num": 95.4, "base": 92.5, "col": {"higher_is_better": False}, "decimals": 1},
            {"num": 70.2, "base": 70.2, "col": {"higher_is_better": True}, "decimals": 1},
            {"num": 42, "base": 40,
             "col": {"higher_is_better": True, "delta_unit": "%"}, "decimals": 0},
        ]}, geo_dir)
        deltas = (delta_result.get("result") or {}).get("values") or []
        check("computeDelta 输出数值、符号与方向",
              len(deltas) == 5
              and abs(deltas[0].get("diff", 0) - 5.9) < 1e-9
              and deltas[0].get("text") == "+5.9" and deltas[0].get("tone") == "pos"
              and deltas[1].get("text") == "-4.4" and deltas[1].get("tone") == "pos"
              and deltas[2].get("text") == "+2.9" and deltas[2].get("tone") == "neg"
              and deltas[3].get("tone") == "zero"
              and deltas[4].get("text") == "+2%", str(deltas))

        sort_desc = run_table_core(
            page, {"op": "sort", "rows": TABLE_ROWS, "colKey": "acc", "dir": "desc"}, geo_dir)
        check("sortRows 数值降序且并列保持原序",
              (sort_desc.get("result") or {}).get("rows") == ["m2", "m3", "m1", "base"],
              str(sort_desc))
        sort_asc = run_table_core(
            page, {"op": "sort", "rows": TABLE_ROWS, "colKey": "acc", "dir": "asc"}, geo_dir)
        check("sortRows 数值升序",
              (sort_asc.get("result") or {}).get("rows") == ["base", "m1", "m2", "m3"],
              str(sort_asc))
        null_asc = run_table_core(
            page, {"op": "sort", "rows": TABLE_ROWS, "colKey": "lat", "dir": "asc"}, geo_dir)
        null_desc = run_table_core(
            page, {"op": "sort", "rows": TABLE_ROWS, "colKey": "lat", "dir": "desc"}, geo_dir)
        check("sortRows 缺失值无论升降序都沉底",
              (null_asc.get("result") or {}).get("rows") == ["m1", "base", "m2", "m3"]
              and (null_desc.get("result") or {}).get("rows") == ["m2", "base", "m1", "m3"],
              f"{null_asc} / {null_desc}")
        pinned = run_table_core(page, {
            "op": "sort", "rows": TABLE_ROWS, "colKey": "acc", "dir": "desc",
            "pinnedTopIds": ["m1"]}, geo_dir)
        check("sortRows 支持置顶基准行",
              (pinned.get("result") or {}).get("rows") == ["m1", "m2", "m3", "base"],
              str(pinned))

        filter_upper = run_table_core(page, {
            "op": "filter", "rows": TABLE_ROWS, "columns": TABLE_COLUMNS, "query": "MODEL b"}, geo_dir)
        filter_cell = run_table_core(page, {
            "op": "filter", "rows": TABLE_ROWS, "columns": TABLE_COLUMNS, "query": "seed"}, geo_dir)
        filter_blank = run_table_core(page, {
            "op": "filter", "rows": TABLE_ROWS, "columns": TABLE_COLUMNS, "query": "N/A"}, geo_dir)
        check("filterRows 大小写不敏感并覆盖所有可见单元格",
              (filter_upper.get("result") or {}).get("rows") == ["m2"]
              and (filter_cell.get("result") or {}).get("rows") == ["m1"]
              and (filter_blank.get("result") or {}).get("rows") == ["m3"],
              f"{filter_upper} / {filter_cell} / {filter_blank}")

        latex_result = run_table_core(page, {
            "op": "latex", "columns": TABLE_COLUMNS, "rows": TABLE_ROWS,
            "groups": [{"label": "Quality", "cols": ["acc", "lat"]},
                       {"label": "Gain", "cols": ["gain"]}]}, geo_dir)
        latex_text = (latex_result.get("result") or {}).get("value") or ""
        check("toLatex 输出 booktabs 表格与分组表头",
              "\\begin{tabular}{lrrrl}" in latex_text
              and "\\toprule" in latex_text and "\\bottomrule" in latex_text
              and "\\multicolumn{2}{c}{Quality}" in latex_text
              and "\\cmidrule(lr){2-3}" in latex_text
              and "\\midrule" in latex_text, latex_text[:400])
        check("toLatex 最佳加粗、次优下划线",
              "\\textbf{76.1}" in latex_text and "\\underline{74.6}" in latex_text,
              latex_text)
        check("toLatex 转义 % _ & 且不破坏命令反斜杠",
              "Acc (\\%)" in latex_text and "Lat\\_ms" in latex_text
              and "a\\_b \\& 100\\%" in latex_text
              and "\\\\toprule" not in latex_text and "\\\\textbf" not in latex_text,
              latex_text)

        markdown_result = run_table_core(page, {
            "op": "markdown", "columns": TABLE_COLUMNS, "rows": TABLE_ROWS}, geo_dir)
        markdown_text = (markdown_result.get("result") or {}).get("value") or ""
        check("toMarkdown 输出对齐表头与最佳/次优标记",
              "| Model | Acc (%) |" in markdown_text
              and "| :--- | ---: | ---: |" in markdown_text
              and "**76.1**" in markdown_text and "__74.6__" in markdown_text,
              markdown_text[:300])
        check("toMarkdown 转义竖线",
              "seed 1\\|2" in markdown_text, markdown_text[:300])

        csv_result = run_table_core(page, {
            "op": "csv",
            "columns": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}],
            "rows": [{"id": "r0", "a": "x,y", "b": "he said \"hi\"\nnext"}]}, geo_dir)
        csv_text = (csv_result.get("result") or {}).get("value") or ""
        check("toCsv 按 RFC 4180 转义逗号、引号与换行",
              '"x,y"' in csv_text and '"he said ""hi""\nnext"' in csv_text,
              repr(csv_text))

        resolve_result = run_table_core(page, {"op": "resolve", "cases": [
            {"rows": TABLE_ROWS, "explicitId": "m2"},
            {"rows": TABLE_ROWS, "explicitId": "missing"},
            {"rows": [{"id": "x", "acc": 1}, {"id": "y", "acc": 2}], "explicitId": ""},
        ]}, geo_dir)
        check("resolveBaselineId 显式优先并回退 is_baseline",
              (resolve_result.get("result") or {}).get("values") == ["m2", "base", ""],
              str(resolve_result))

        decimals_result = run_table_core(page, {"op": "decimals", "cases": [
            {"col": {"decimals": 3}, "values": [1, 2]},
            {"col": {"decimals": 9}, "values": [1.5]},
            {"col": {}, "values": [1, 2]},
            {"col": {}, "values": [1.25, 2]},
        ]}, geo_dir)
        check("inferDecimals 尊重显式值并夹到 0..4",
              (decimals_result.get("result") or {}).get("values") == [3, 4, 0, 1],
              str(decimals_result))

        cell_result = run_table_core(page, {
            "op": "cell", "rows": [TABLE_ROWS[1]], "key": "gain"}, geo_dir)
        cell = ((cell_result.get("result") or {}).get("values") or [{}])[0]
        check("getCell 解析对象单元格的 value / display / note",
              cell.get("num") == 4.4 and cell.get("text") == "+4.4"
              and cell.get("note") == "three runs" and cell.get("hasDisplay") is True,
              str(cell))

        escape_result = run_table_core(page, {"op": "escape", "values": [
            "100% & a_b", "C:\\tmp", "x~y^z#1$2"]}, geo_dir)
        escaped = (escape_result.get("result") or {}).get("values") or []
        check("escapeLatex 覆盖反斜杠与 LaTeX 特殊字符",
              escaped[0] == "100\\% \\& a\\_b"
              and "\\textbackslash{}tmp" in escaped[1]
              and "\\textasciitilde{}" in escaped[2]
              and "\\textasciicircum{}" in escaped[2]
              and "\\#" in escaped[2] and "\\$" in escaped[2],
              str(escaped))

        print("\n[16d] 图表核心（Node VM）")
        scale_case = run_chart_core(page, {
            "op": "scale", "min": 0, "max": 100, "ticks": 5, "includeZero": True}, geo_dir)
        scale_data = (scale_case.get("result") or {}).get("scale") or {}
        check("niceScale 常规区间返回干净刻度",
              bool(scale_data) and scale_data.get("min") <= 0 and scale_data.get("max") >= 100
              and len(scale_data.get("ticks", [])) >= 3
              and (scale_case.get("result") or {}).get("bad") == [],
              str(scale_case))
        negative = run_chart_core(page, {"op": "scale", "min": -42.5, "max": -3.5, "ticks": 5}, geo_dir)
        check("niceScale 负区间不产生 NaN / Infinity",
              (negative.get("result") or {}).get("bad") == []
              and (negative.get("result") or {}).get("scale", {}).get("max", 0) > -3.5, str(negative))
        constant = run_chart_core(page, {"op": "scale", "min": 50, "max": 50, "ticks": 5}, geo_dir)
        constant_scale = (constant.get("result") or {}).get("scale") or {}
        check("niceScale 常量输入仍满足 max > min",
              constant_scale.get("max", 0) > constant_scale.get("min", 0)
              and len(constant_scale.get("ticks", [])) >= 2
              and (constant.get("result") or {}).get("bad") == [], str(constant))
        empty_scale = run_chart_core(page, {"op": "scale", "ticks": 5}, geo_dir)
        empty_data = (empty_scale.get("result") or {}).get("scale") or {}
        check("niceScale 空输入也返回有限区间",
              empty_data.get("max", 0) > empty_data.get("min", 0)
              and (empty_scale.get("result") or {}).get("bad") == [], str(empty_scale))

        bar_case = run_chart_core(page, {"op": "bar", "spec": {
            "categories": ["A", "B", "C"],
            "series": [{"name": "M1", "data": [10, 20, None]},
                       {"name": "M2", "data": [8, 15, 22]}],
            "unit": "%", "ref_lines": [{"value": 18, "label": "target"}]}}, geo_dir)
        bar_layout = (bar_case.get("result") or {}).get("layout") or {}
        bar_bounds = all(
            0 <= b.get("x", -1) and b.get("x", 0) + b.get("w", 0) <= bar_layout.get("width", 0)
            and 0 <= b.get("y", -1) and b.get("y", 0) + b.get("h", 0) <= bar_layout.get("height", 0)
            for b in bar_layout.get("bars", []))
        check("bar 布局生成柱体、图例与参考线",
              len(bar_layout.get("bars", [])) == 5
              and len(bar_layout.get("legend", [])) == 2
              and len(bar_layout.get("refLines", [])) == 1, str(bar_layout)[:300])
        check("bar 所有坐标落在 viewBox 内且无 NaN",
              bar_bounds and (bar_case.get("result") or {}).get("bad") == [],
              f"{bar_bounds} / {(bar_case.get('result') or {}).get('bad')}")

        line_case = run_chart_core(page, {"op": "line", "spec": {
            "x": ["a", "b", "c", "d"],
            "series": [{"name": "S1", "data": [1, None, 3, 4]},
                       {"name": "S2", "data": [2, 3, 4, 5]}],
            "hiddenSeries": {"S2": True}}}, geo_dir)
        line_layout = (line_case.get("result") or {}).get("layout") or {}
        check("line 在 null 处拆分路径段",
              len(line_layout.get("lines", [])) == 1
              and len((line_layout.get("lines") or [{}])[0].get("segments", [])) == 2
              and ((line_layout.get("lines") or [{}])[0].get("d") or "").count("M") == 2,
              str(line_layout)[:300])
        check("line hiddenSeries 关闭图例系列",
              len(line_layout.get("points", [])) == 3
              and (line_case.get("result") or {}).get("bad") == [], str(line_layout)[:300])

        scatter_case = run_chart_core(page, {"op": "scatter", "spec": {
            "x_label": "Latency", "y_label": "Acc", "pareto": "max_y_min_x",
            "points": [{"label": "A", "x": 1, "y": 10}, {"label": "B", "x": 2, "y": 12},
                       {"label": "C", "x": 3, "y": 11}, {"label": "D", "x": 4, "y": 14}]}}, geo_dir)
        scatter_layout = (scatter_case.get("result") or {}).get("layout") or {}
        frontier = [p.get("label") for p in scatter_layout.get("frontier", [])]
        check("scatter 帕累托前沿按 max_y_min_x 计算",
              frontier == ["A", "B", "D"]
              and str(scatter_layout.get("paretoPath", "")).startswith("M"), str(scatter_layout)[:300])
        scatter_bounds = all(
            0 <= p.get("x", -1) <= scatter_layout.get("width", 0)
            and 0 <= p.get("y", -1) <= scatter_layout.get("height", 0)
            and p.get("labelX") is not None and p.get("labelY") is not None
            for p in scatter_layout.get("points", []))
        check("scatter 点与标签坐标有限且不越界",
              scatter_bounds and (scatter_case.get("result") or {}).get("bad") == [],
              f"{scatter_bounds} / {(scatter_case.get('result') or {}).get('bad')}")

        heatmap_case = run_chart_core(page, {"op": "heatmap", "spec": {
            "x_labels": ["x1", "x2", "x3"], "y_labels": ["y1", "y2"],
            "values": [[0.1, 0.5, 0.9], [0.2, None, 0.7]],
            "scale_mode": "max_good", "unit": "%"}}, geo_dir)
        heatmap_layout = (heatmap_case.get("result") or {}).get("layout") or {}
        heat_cells = heatmap_layout.get("cells", [])
        heat_norms = [c.get("norm") for c in heat_cells]
        heat_alphas = [c.get("alpha") for c in heat_cells]
        check("heatmap 生成 2×3 网格并归一化强度",
              len(heat_cells) == 6
              and all(0 <= n <= 1 for n in heat_norms)
              and all(0 <= a <= 1 for a in heat_alphas)
              and (heatmap_case.get("result") or {}).get("bad") == [],
              f"{heat_norms} / {heat_alphas}")
        heat_plot = heatmap_layout.get("plot") or {}
        check("heatmap 单元格不越出绘图区",
              all(heat_plot.get("x", 0) <= c.get("x", -1)
                  and c.get("x", 0) + c.get("w", 0) <= heat_plot.get("x", 0) + heat_plot.get("w", 0)
                  and heat_plot.get("y", 0) <= c.get("y", -1)
                  and c.get("y", 0) + c.get("h", 0) <= heat_plot.get("y", 0) + heat_plot.get("h", 0)
                  for c in heat_cells), str(heat_plot))

        sparse_case = run_chart_core(page, {"op": "heatmap", "spec": {
            "x_labels": ["a", "b"], "y_labels": ["r1"],
            "cells": [{"row": 0, "col": 1, "value": 5, "details": "## cell"}]}}, geo_dir)
        sparse_layout = (sparse_case.get("result") or {}).get("layout") or {}
        check("heatmap 支持稀疏 cells 输入",
              len(sparse_layout.get("cells", [])) == 2
              and any(c.get("value") == 5 and c.get("col") == 1
                      for c in sparse_layout.get("cells", [])),
              str(sparse_layout)[:300])

        kind_cases = [
            ({"matrix": [[1]], "x_labels": ["a"]}, "heatmap"),
            ({"points": [{"x": 1, "y": 2}]}, "scatter"),
            ({"x": [1, 2], "series": [{"name": "s", "data": [1, 2]}]}, "line"),
            ({"categories": ["a"], "series": [{"name": "s", "data": [1]}]}, "bar"),
        ]
        kind_values = []
        for spec_case, _ in kind_cases:
            result = run_chart_core(page, {"op": "kind", "spec": spec_case}, geo_dir)
            kind_values.append((result.get("result") or {}).get("kind"))
        check("chartKind 从简写字段推断类型",
              kind_values == [expected for _, expected in kind_cases], str(kind_values))


        check("Markdown 渲染逻辑只有一份",
              page.count("function renderReading(") == 1, str(page.count("function renderReading(")))
        check("画布渲染逻辑只有一份",
              page.count("function renderCanvas(") == 1, str(page.count("function renderCanvas(")))
        check("Artifact 渲染逻辑只有一份",
              page.count("function syncArtifact(") == 1, str(page.count("function syncArtifact(")))
        check("两窗格共用 renderIntoPane",
              page.count("renderIntoPane(") >= 4, str(page.count("renderIntoPane(")))
        check("主窗格保留 legacy DOM id",
              all(f'id="{x}"' in page for x in
                  ("reading-pane", "content", "canvas-root", "artifact-frame", "empty")))
        check("焦点锁提示文案存在",
              "其它会话只提示，不跳转" in page or "不劫持" in page
              or "锁定当前会话" in page)

        # ---------- 16e. v7 实时面板健康度与目录树（Node VM） ----------
        print("\n[16e] 实时面板健康度与目录树（Node VM）")
        now_ms = 1767225600000  # 2026-01-01T00:00:00Z
        health_probe = run_rail_core(page, {
            "op": "health", "nowMs": now_ms,
            "boards": [
                {"id": "h-live", "updated_epoch": now_ms / 1000 - 30, "ttl_sec": 120},
                {"id": "h-edge", "updated_epoch": now_ms / 1000 - 120, "ttl_sec": 120},
                {"id": "h-stale", "updated_epoch": now_ms / 1000 - 200, "ttl_sec": 120},
                {"id": "h-dead", "updated_epoch": now_ms / 1000 - 999, "ttl_sec": 120},
                {"id": "h-done", "updated_epoch": now_ms / 1000 - 999, "ttl_sec": 120,
                 "status_hint": "complete"},
                {"id": "h-alert", "updated_at": "2026-01-01T00:00:00Z", "ttl_sec": 5,
                 "status_hint": "err"},
                {"id": "h-static", "updated_at": "2026-01-01T00:00:00Z", "ttl_sec": 0},
                {"id": "h-warn", "updated_at": "2026-01-01T00:00:00Z", "status_hint": "warn"},
                {"id": "h-iso", "updated_at": "2025-12-31T23:58:00Z", "ttl_sec": 60},
            ],
        }, geo_dir)
        health_values = (health_probe.get("result") or {}).get("values") or []
        check("面板健康度核心可在 node vm 中执行",
              len(health_values) == 9, str(health_probe.get("error")))
        if len(health_values) == 9:
            states = [(v.get("state"), v.get("label")) for v in health_values]
            check("TTL 边界：age≤ttl 为 LIVE，=2×ttl 仍为 STALE，超过为 OFFLINE",
                  states[0] == ("live", "LIVE") and states[1] == ("live", "LIVE")
                  and states[2] == ("stale", "STALE") and states[3] == ("dead", "OFFLINE"),
                  str(states[:4]))
            check("status_hint complete / err 覆盖 TTL 判定",
                  states[4] == ("complete", "DONE") and states[5] == ("err", "ALERT"),
                  str(states[4:6]))
            check("无 TTL 时按 static / warn 显示 BOARD / WARN",
                  states[6] == ("static", "BOARD") and states[7] == ("stale", "WARN"),
                  str(states[6:8]))
            check("ISO updated_at 参与 age / overdue 计算",
                  health_values[8].get("ageSec") == 120
                  and health_values[8].get("ttlSec") == 60
                  and health_values[8].get("overdueSec") == 60
                  and health_values[0].get("overdueSec") == 0,
                  str(health_values[8]))
            check("缺失时间戳时 age=0 且不抛错",
                  health_values[6].get("ageSec") == 0, str(health_values[6]))

        age_probe = run_rail_core(page, {
            "op": "age", "ages": [0, 4, 5, 59, 60, 119, 3599, 3600, 7380],
        }, geo_dir)
        check("formatAgeShort 覆盖 刚刚 / s / m / h+m 四档",
              (age_probe.get("result") or {}).get("values")
              == ["刚刚", "刚刚", "5s前", "59s前", "1m前", "1m前",
                  "59m前", "1h0m前", "2h3m前"],
              str(age_probe))

        rail_boards = [
            {"id": "board-pinned", "label": "CI 构建", "project": "release", "pinned": True,
             "created_at": "2026-01-04T00:00:00+00:00", "created_seq": 6, "seq": 61,
             "updated_at": "2026-01-04T00:00:00+00:00", "updated_epoch": now_ms / 1000,
             "ttl_sec": 120, "status_hint": "ok"},
            {"id": "board-alpha", "label": "alpha · train-live", "project": "alpha",
             "pinned": False, "created_at": "2026-01-02T00:00:00+00:00", "created_seq": 7,
             "seq": 62, "updated_at": "2026-01-02T00:00:00+00:00", "updated_epoch": 0,
             "ttl_sec": 0, "status_hint": ""},
            {"id": "board-ops", "label": "ops · deploy-live", "project": "gamma",
             "pinned": False, "created_at": "2026-01-03T00:00:00+00:00", "created_seq": 8,
             "seq": 63, "updated_at": "2026-01-03T00:00:00+00:00",
             "updated_epoch": now_ms / 1000 - 500, "ttl_sec": 60, "status_hint": ""},
        ]
        board_tree_result = run_rail_core(page, {
            "op": "build", "sessions": RAIL_SESSIONS, "boards": rail_boards,
            "unread": ["alpha-old"], "nowMs": now_ms}, geo_dir)
        board_tree = ((board_tree_result.get("result") or {}).get("tree")
                      if board_tree_result.get("ok") else None)
        check("目录树核心接受 boards 扩展", bool(board_tree),
              str(board_tree_result.get("error")))
        if board_tree:
            pinned_group = board_tree.get("pinned") or {}
            pinned_board_ids = [b.get("id") for b in pinned_group.get("boards", [])]
            project_board_ids = {g.get("key"): [b.get("id") for b in g.get("boards", [])]
                                 for g in board_tree.get("projects", [])}
            check("置顶面板与置顶会话同组且不重复出现",
                  pinned_board_ids == ["board-pinned"]
                  and all("board-pinned" not in ids for ids in project_board_ids.values()),
                  str((pinned_board_ids, project_board_ids)))
            check("面板参与项目分组且纯面板项目也会出现",
                  project_board_ids.get("alpha") == ["board-alpha"]
                  and project_board_ids.get("gamma") == ["board-ops"],
                  str(project_board_ids))
            check("tree.totalCount 同时计入会话与面板",
                  board_tree.get("totalCount") == len(RAIL_SESSIONS) + len(rail_boards),
                  str(board_tree.get("totalCount")))
            check("面板参与项目排序（gamma 最新创建排第一）",
                  [g.get("key") for g in board_tree.get("projects", [])]
                  == ["gamma", "beta", "alpha"],
                  str([g.get("key") for g in board_tree.get("projects", [])]))

        board_unread_result = run_rail_core(page, {
            "op": "build", "sessions": RAIL_SESSIONS, "boards": rail_boards,
            "unread": ["alpha-old"], "filter": "unread", "nowMs": now_ms}, geo_dir)
        board_unread_tree = ((board_unread_result.get("result") or {}).get("tree")
                             if board_unread_result.get("ok") else None)
        check("unread 筛选把 STALE / OFFLINE / err 面板计入未读",
              bool(board_unread_tree) and board_unread_tree.get("totalCount") == 2
              and any([b.get("id") for b in g.get("boards", [])] == ["board-ops"]
                      for g in board_unread_tree.get("projects", [])),
              str(board_unread_tree))

        board_pinned_result = run_rail_core(page, {
            "op": "build", "sessions": RAIL_SESSIONS, "boards": rail_boards,
            "filter": "pinned"}, geo_dir)
        board_pinned_tree = ((board_pinned_result.get("result") or {}).get("tree")
                             if board_pinned_result.get("ok") else None)
        check("pinned 筛选同时返回置顶会话与置顶面板",
              bool(board_pinned_tree) and board_pinned_tree.get("totalCount") == 3
              and [b.get("id") for b in (board_pinned_tree.get("pinned") or {}).get("boards", [])]
              == ["board-pinned"] and not board_pinned_tree.get("projects"),
              str(board_pinned_tree))

        # ---------- 17. 存储层 created_seq 与固定排序 ----------
        print("\n[17] 存储层 created_seq 与固定排序")
        spec = importlib.util.spec_from_file_location("vr_store_rail", STORE)
        store_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(store_module)

        check("_iso_epoch 支持 Z 后缀",
              store_module._iso_epoch("2026-01-01T00:00:00Z") == 1767225600.0,
              str(store_module._iso_epoch("2026-01-01T00:00:00Z")))
        check("_iso_epoch 非法输入回退 0", store_module._iso_epoch("not-a-time") == 0.0)

        legacy_dir = store_cache / "sessions" / "legacy-1"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "session.json").write_text(json.dumps({
            "id": "legacy-1", "label": "legacy · codex · legacy", "client": "codex",
            "project": "legacy", "source": "explicit", "pinned": False,
            "seq": 7, "reply_count": 1,
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-02T00:00:00+00:00",
        }, ensure_ascii=False), encoding="utf-8")
        legacy_seed = {
            "id": "legacy-1", "label": "legacy · codex · legacy", "custom_label": False,
            "client": "codex", "project": "legacy", "source": "explicit",
            "color": "#2f6feb",
        }
        fresh_seed = {
            "id": "fresh-1", "label": "legacy · codex · fresh", "custom_label": False,
            "client": "codex", "project": "legacy", "source": "explicit",
            "color": "#0891b2",
        }
        with store_module.cache_lock(store_cache):
            store_module.commit_reply(store_cache, legacy_seed, "# 旧会话\n", "md", "旧会话")
        legacy_rec = json.loads((legacy_dir / "session.json").read_text("utf-8"))
        check("v4 旧会话补写 created_seq=0", legacy_rec.get("created_seq") == 0,
              str(legacy_rec.get("created_seq")))

        with store_module.cache_lock(store_cache):
            fresh = store_module.commit_reply(
                store_cache, fresh_seed, "# 新会话\n", "md", "新会话")
            store_module.commit_reply(
                store_cache, legacy_seed, "# 旧会话续写\n", "md", "旧会话续写")
        fresh_rec = json.loads((store_cache / "sessions" / "fresh-1" / "session.json").read_text("utf-8"))
        legacy_rec2 = json.loads((legacy_dir / "session.json").read_text("utf-8"))
        check("新会话 created_seq 等于首次 seq",
              fresh_rec.get("created_seq") == fresh.get("seq"),
              f'{fresh_rec.get("created_seq")} != {fresh.get("seq")}')
        check("旧会话续写仍保持 created_seq=0", legacy_rec2.get("created_seq") == 0,
              str(legacy_rec2.get("created_seq")))
        ordered_unpinned = [s["id"] for s in store_module.list_sessions(store_cache)]
        check("list_sessions 固定创建顺序（旧会话续写不超越新会话）",
              ordered_unpinned == ["fresh-1", "legacy-1"], str(ordered_unpinned))

        with store_module.cache_lock(store_cache):
            store_module.update_session(store_cache, "legacy-1", pinned=True)
        ordered_pinned = [s["id"] for s in store_module.list_sessions(store_cache)]
        check("list_sessions 置顶优先", ordered_pinned[0] == "legacy-1", str(ordered_pinned))
        with store_module.cache_lock(store_cache):
            store_module.update_session(store_cache, "legacy-1", pinned=False)

        # 在运行中的 HTTP 服务上验证：早先创建的非置顶会话收到新回复后，不会超越后创建的非置顶会话
        code, fixed_old = publish(port, cache, MD, "固定顺序旧会话",
                                  env=clean_env(CODEX_THREAD_ID="fixed-older"))
        time.sleep(0.05)
        code2, fixed_new = publish(port, cache, MD, "固定顺序新会话",
                                   env=clean_env(CODEX_THREAD_ID="fixed-newer"))
        check("固定顺序对照会话创建成功", code == 0 and code2 == 0)
        publish(port, cache, MD, "固定顺序旧会话后续新回复",
                env=clean_env(CODEX_THREAD_ID="fixed-older"))
        status, fixed_payload = get_json(f"{base}/api/sessions")
        non_pinned_ids = [s.get("id") for s in fixed_payload.get("sessions", []) if not s.get("pinned")]
        check("/api/sessions 非置顶新会话保持在旧会话之前",
              status == 200 and "fixed-newer" in non_pinned_ids and "fixed-older" in non_pinned_ids
              and non_pinned_ids.index("fixed-newer") < non_pinned_ids.index("fixed-older"),
              str(non_pinned_ids))

        # ---------- 17b. v6 科研画布发布与端到端接口 ----------
        print("\n[17b] v6 科研画布发布与端到端接口")
        code, sci = publish(port, cache, SCIENTIFIC_TEXT, "科研画布端到端")
        check("v6 科研画布发布成功", code == 0 and sci.get("ok") is True, str(sci)[:400])
        check(f"publish 版本为 {EXPECTED_VERSION}", sci.get("version") == EXPECTED_VERSION,
              str(sci.get("version")))
        check("科研画布自动识别为 json", sci.get("format") == "json", str(sci.get("format")))

        status, sci_latest = get_json(f"{base}/api/latest")
        check("/api/latest 返回科研画布",
              status == 200 and sci_latest.get("format") == "json", str(status))
        parsed_sci = json.loads(sci_latest.get("content") or "{}")
        sci_types = [sec.get("type") for sec in parsed_sci.get("sections", [])]
        check("科研画布包含 table / bar / line / scatter / heatmap / prose",
              sci_types == ["table", "chart", "chart", "chart", "chart", "prose"],
              str(sci_types))
        check("科研画布保留 span=1 双列布局标记",
              any(sec.get("span") == 1 for sec in parsed_sci.get("sections", [])), str(sci_types))
        sci_table = next(sec for sec in parsed_sci["sections"] if sec.get("type") == "table")
        check("表格带 groups / baseline_id / is_baseline 语义",
              bool(sci_table.get("groups")) and sci_table.get("baseline_id") == "base"
              and any(row.get("is_baseline") for row in sci_table.get("rows", [])),
              str(sci_table)[:300])
        sci_kinds = [sec.get("chart_type") for sec in parsed_sci["sections"]
                     if sec.get("type") == "chart"]
        check("四种图表类型齐全",
              sci_kinds == ["bar", "line", "scatter", "heatmap"], str(sci_kinds))

        sid = urllib.parse.quote(sci.get("session_id", ""), safe="")
        rid = urllib.parse.quote(sci.get("reply_id", ""), safe="")
        status, sci_reply = get_json(f"{base}/api/sessions/{sid}/replies/{rid}")
        check("会话回复接口返回完整科研画布",
              status == 200 and sci_reply.get("content") == SCIENTIFIC_TEXT, str(status))

        shorthand_table = {
            "headline": "简写表格：columns + rows 直接放顶层",
            "columns": [
                {"key": "name", "label": "Name"},
                {"key": "score", "label": "Score", "higher_is_better": True, "decimals": 1},
            ],
            "rows": [{"name": "alpha", "score": 1.5}, {"name": "beta", "score": 2.5}],
        }
        code, short_table = publish(
            port, cache, json.dumps(shorthand_table, ensure_ascii=False), "简写表格")
        check("顶层 columns+rows 自动识别为 json",
              code == 0 and short_table.get("format") == "json", str(short_table)[:300])

        shorthand_chart = {
            "headline": "简写图表：chart_type 直接放顶层",
            "chart_type": "bar",
            "categories": ["a", "b"],
            "series": [{"name": "s", "data": [1, 2]}],
        }
        code, short_chart = publish(
            port, cache, json.dumps(shorthand_chart, ensure_ascii=False), "简写图表")
        check("顶层 chart_type 自动识别为 json",
              code == 0 and short_chart.get("format") == "json", str(short_chart)[:300])

        shorthand_matrix = {
            "headline": "简写热力：matrix + x_labels",
            "matrix": [[1, 2], [3, 4]],
            "x_labels": ["x1", "x2"],
        }
        code, short_matrix = publish(
            port, cache, json.dumps(shorthand_matrix, ensure_ascii=False), "简写热力")
        check("顶层 matrix + x_labels 自动识别为 json",
              code == 0 and short_matrix.get("format") == "json", str(short_matrix)[:300])

        prose_only = {"headline": "简写长文：prose 字段", "prose": "## 说明\n\n正文段落。"}
        code, prose_payload = publish(
            port, cache, json.dumps(prose_only, ensure_ascii=False), "简写长文", fmt="json")
        check("强制 json 的 prose 简写发布成功",
              code == 0 and prose_payload.get("format") == "json", str(prose_payload)[:300])

        status, body, _ = http(f"{base}/")
        sci_page = body.decode("utf-8", errors="replace")
        for token in ("VR_TABLE_CORE_START", "VR_CHART_CORE_START", "TableCore", "ChartCore",
                      "function renderTable", "function renderChart", "function renderProse",
                      "function bindTables", "function bindCharts", "toLatex", "toMarkdown",
                      "toCsv", "tbl-base-btn", "tbl-export-preview", "chart-tip", "chart-pill",
                      "cv-sections", '.cv-section[data-span="1"]', "--vc-0", "prose-card",
                      "navigator.clipboard", "data-tcol", "data-tact", "hiddenSeries"):
            check(f"v6 页面包含 {token}", token in sci_page)
        check("normalizeCanvas 支持 table / chart / prose 简写",
              "Array.isArray(data.columns) && Array.isArray(data.rows)" in sci_page
              and "Array.isArray(data.matrix)" in sci_page
              and "typeof data.prose === 'string'" in sci_page)

        sci_serialized = json.dumps(SCIENTIFIC_CANVAS, ensure_ascii=False) + SCIENTIFIC_TEXT
        home_patterns = ("/" + "home/", "/" + "Users/", "C:" + "\\Users\\")
        check("科研画布样例不含本机路径或邮箱",
              not any(pattern in sci_serialized for pattern in home_patterns)
              and "@" not in sci_serialized)

        # ---------- 17c. v7 实时面板幂等更新、隔离与端到端接口 ----------
        print("\n[17c] v7 实时面板幂等更新、隔离与端到端接口")
        feed_path = cache / "global" / "feed.jsonl"
        history_dir = cache / "history"
        sessions_dir = cache / "sessions"

        def reply_projection() -> dict:
            snap = {}
            for name in ("latest.json", "latest.html", "latest.md", "meta.json"):
                path = cache / name
                snap[name] = path.read_bytes() if path.is_file() else None
            snap["feed"] = feed_path.read_bytes() if feed_path.is_file() else None
            snap["history"] = (sorted(p.name for p in history_dir.iterdir())
                               if history_dir.is_dir() else [])
            snap["sessions"] = (sorted(p.name for p in sessions_dir.iterdir())
                                if sessions_dir.is_dir() else [])
            return snap

        projection_before = reply_projection()
        state_before = json.loads((cache / "state.json").read_text(encoding="utf-8"))

        board_one = {
            "headline": "build 4821 · 3/5 stages green",
            "sections": [{
                "type": "table",
                "title": "Stages",
                "columns": [
                    {"key": "stage", "label": "Stage"},
                    {"key": "progress", "label": "Progress", "bar": True, "max": 100,
                     "unit": "%"},
                    {"key": "tests", "label": "Tests", "decimals": 0},
                ],
                "rows": [
                    {"id": "lint", "stage": "lint",
                     "progress": {"value": 100, "sub": "done", "status": "ok"}, "tests": 0},
                    {"id": "unit", "stage": "unit",
                     "progress": {"value": 78, "sub": "1402/1798"}, "tests": 1402},
                ],
            }],
        }
        board_one_text = json.dumps(board_one, ensure_ascii=False)
        code, b1 = publish(port, cache, board_one_text, "构建状态", extra=[
            "--board", "ci-live", "--board-name", "CI 构建", "--project", "release",
            "--ttl", "120", "--board-status", "ok"])
        check("v7 面板首次发布成功", code == 0 and b1.get("ok") is True, str(b1)[:400])
        check("面板回执标记 mode=board 且带 board 深链",
              b1.get("mode") == "board" and b1.get("board_id") == "ci-live"
              and str(b1.get("url", "")).endswith("?board=ci-live")
              and b1.get("version") == EXPECTED_VERSION, str(b1)[:400])
        check("首轮回执保留名称 / 项目 / TTL / 状态提示",
              b1.get("board_name") == "CI 构建" and b1.get("project") == "release"
              and b1.get("ttl_sec") == 120 and b1.get("status_hint") == "ok"
              and b1.get("update_count") == 1, str(b1)[:400])
        check("面板载荷落在 boards/<bid>/payload.json",
              (cache / "boards" / "ci-live" / "payload.json").is_file()
              and not (cache / "boards" / "ci-live" / "payload.md").exists(),
              str(sorted(p.name for p in (cache / "boards" / "ci-live").iterdir())))
        check("面板发布不写 latest.* / meta.json / history/ / sessions/",
              reply_projection() == projection_before, "回复侧投影被面板污染")

        state_after = json.loads((cache / "state.json").read_text(encoding="utf-8"))
        check("面板只推进 seq 并记录 latest_board_id",
              int(state_after.get("seq") or 0) == int(state_before.get("seq") or 0) + 1
              and state_after.get("latest_board_id") == "ci-live"
              and state_after.get("latest_session_id") == state_before.get("latest_session_id")
              and state_after.get("latest_reply_id") == state_before.get("latest_reply_id"),
              str(state_after)[:400])

        status, boards_payload = get_json(f"{base}/api/boards")
        listed = boards_payload.get("boards") or []
        check("/api/boards 返回面板列表与计数",
              status == 200 and boards_payload.get("count") == len(listed)
              and any(b.get("id") == "ci-live" for b in listed), str(boards_payload)[:400])
        live_listed = next((b for b in listed if b.get("id") == "ci-live"), {})
        check("/api/boards 面板元数据完整",
              live_listed.get("label") == "CI 构建"
              and live_listed.get("project") == "release"
              and live_listed.get("update_count") == 1
              and live_listed.get("ttl_sec") == 120
              and live_listed.get("status_hint") == "ok"
              and float(live_listed.get("updated_epoch") or 0) > 0, str(live_listed)[:400])

        status, board_detail = get_json(f"{base}/api/boards/ci-live")
        check("/api/boards/<bid> 返回元数据与正文",
              status == 200 and (board_detail.get("board") or {}).get("id") == "ci-live"
              and board_detail.get("content") == board_one_text, str(status))

        status, _, _ = raw_request(port, "GET", "/api/artifact/board/ci-live")
        check("非 html 面板的 artifact 返回 404", status == 404, str(status))
        status, _, _ = raw_request(port, "GET", "/api/boards/..%2F..%2Fmeta.json")
        check("/api/boards 路径穿越被拒绝", status == 400, str(status))
        status, _, _ = raw_request(port, "GET", "/artifact/board/..%2F..%2Fmeta.json")
        check("/artifact/board 路径穿越被拒绝", status == 400, str(status))
        status, _, _ = raw_request(port, "GET", "/api/boards/missing-board")
        check("/api/boards 未知面板返回 404", status == 404, str(status))

        status, body, _ = raw_request(
            port, "POST", "/api/boards/ci-live",
            body=json.dumps({"label": "CI 构建 · 主分支"}),
            headers={"Content-Type": "application/json"})
        renamed = json.loads(body.decode("utf-8") or "{}")
        check("POST /api/boards/<bid> 可重命名并固化 custom_label",
              status == 200 and renamed.get("ok") is True
              and (renamed.get("board") or {}).get("label") == "CI 构建 · 主分支"
              and (renamed.get("board") or {}).get("custom_label") is True,
              str(renamed)[:400])
        status, body, _ = raw_request(
            port, "POST", "/api/boards/ci-live",
            body=json.dumps({"pinned": True}),
            headers={"Content-Type": "application/json"})
        pinned_board = json.loads(body.decode("utf-8") or "{}")
        check("POST /api/boards/<bid> 可置顶且不改动更新时间",
              status == 200 and (pinned_board.get("board") or {}).get("pinned") is True
              and (pinned_board.get("board") or {}).get("updated_at")
              == (renamed.get("board") or {}).get("updated_at"),
              str(pinned_board)[:400])
        status, _, _ = raw_request(
            port, "POST", "/api/boards/ci-live", body=json.dumps({}),
            headers={"Content-Type": "application/json"})
        check("POST /api/boards/<bid> 无字段返回 400", status == 400, str(status))
        status, _, _ = raw_request(
            port, "POST", "/api/boards/missing-board", body=json.dumps({"pinned": True}),
            headers={"Content-Type": "application/json"})
        check("POST 未知面板返回 404", status == 404, str(status))

        board_two = {
            "headline": "build 4821 · 5/5 stages green",
            "sections": [{
                "type": "kpis",
                "items": [
                    {"label": "stages", "value": "5/5", "status": "ok"},
                    {"label": "tests", "value": "1798", "status": "ok"},
                ],
            }],
        }
        board_two_text = json.dumps(board_two, ensure_ascii=False)
        code, b2 = publish(port, cache, board_two_text, "构建状态", extra=[
            "--board", "ci-live", "--project", "release", "--ttl", "120"])
        check("重复发布同一 board 原地更新（不新增回复）",
              code == 0 and b2.get("update_count") == 2
              and int(b2.get("seq") or 0) == int(b1.get("seq") or 0) + 1,
              str(b2)[:400])
        check("--board-name 粘性：心跳不传仍保留自定义名",
              b2.get("board_name") == "CI 构建 · 主分支", str(b2.get("board_name")))
        check("心跳未传 --board-status 时清空状态提示",
              b2.get("status_hint") == "", str(b2.get("status_hint")))
        status, board_detail2 = get_json(f"{base}/api/boards/ci-live")
        check("面板正文被最新心跳覆盖",
              status == 200 and board_detail2.get("content") == board_two_text,
              str(status))
        check("心跳保留 pinned 状态",
              (board_detail2.get("board") or {}).get("pinned") is True,
              str(board_detail2.get("board"))[:300])
        check("心跳同样不污染回复侧投影", reply_projection() == projection_before,
              "回复侧投影被面板心跳污染")

        html_board = "<!doctype html><html><body><h1>build 4821</h1></body></html>"
        code, b3 = publish(port, cache, html_board, "构建状态", fmt="html", extra=[
            "--board", "ci-live", "--project", "release", "--ttl", "60"])
        check("同一 board 可切换为 html 载荷",
              code == 0 and b3.get("format") == "html"
              and b3.get("update_count") == 3, str(b3)[:400])
        check("切换格式后旧 payload.json 被删除",
              (cache / "boards" / "ci-live" / "payload.html").is_file()
              and not (cache / "boards" / "ci-live" / "payload.json").exists(),
              str(sorted(p.name for p in (cache / "boards" / "ci-live").iterdir())))
        status, body, headers = raw_request(port, "GET", "/artifact/board/ci-live")
        check("html 面板 artifact 返回原文",
              status == 200 and body.decode("utf-8") == html_board
              and "text/html" in headers.get("Content-Type", ""),
              f"{status} / {headers.get('Content-Type')}")

        sse = sse_connect(port)
        banner = read_until(sse, b": connected", time.time() + 6)
        check("面板 SSE 已连接", b": connected" in banner, banner[:120])
        board_four = {"headline": "build 4821 · done", "sections": [{"type": "prose",
                                                                     "content": "全部通过。"}]}
        started_at = time.time()
        code, b4 = publish(
            port, cache, json.dumps(board_four, ensure_ascii=False), "构建状态", extra=[
                "--board", "ci-live", "--project", "release", "--ttl", "60",
                "--board-status", "complete"])
        frame = read_until(sse, ("id: " + str(b4.get("seq"))).encode(), started_at + 4)
        elapsed = time.time() - started_at
        try:
            sse.close()
        except OSError:
            pass
        check("面板发布后 SSE 在 2 秒内推送",
              b"event: board" in frame and elapsed < 2.0, f"{elapsed:.2f}s")
        board_live_events = []
        for match in re.finditer(rb"data: (.+?)\r?\n", frame):
            try:
                board_live_events.append(json.loads(match.group(1).decode("utf-8")))
            except ValueError:
                continue
        board_event = next((e for e in board_live_events
                            if e.get("seq") == b4.get("seq")), None)
        check("SSE 面板事件字段完整",
              board_event is not None
              and board_event.get("event_type") == "board"
              and board_event.get("board_id") == "ci-live"
              and board_event.get("update_count") == 4
              and board_event.get("status_hint") == "complete", str(board_event)[:400])
        check("b4 回执更新计数为 4",
              code == 0 and b4.get("update_count") == 4, str(b4)[:300])

        status, board_state = get_json(
            f"{base}/api/state?since={int(state_before.get('seq') or 0)}")
        merged_events = board_state.get("events") or []
        board_events = [e for e in merged_events if e.get("event_type") == "board"]
        check("/api/state 合并面板事件且 event_type=board",
              status == 200 and len(board_events) == 4
              and all(e.get("board_id") == "ci-live" for e in board_events)
              and board_events[-1].get("seq") == b4.get("seq"),
              str(board_events)[:400])
        check("/api/state 最新会话指针不被面板改写",
              board_state.get("latest_session_id") == state_before.get("latest_session_id")
              and board_state.get("latest_reply_id") == state_before.get("latest_reply_id"),
              str(board_state.get("latest_session_id")))

        status, sessions_payload = get_json(f"{base}/api/sessions")
        boards_field = sessions_payload.get("boards") or []
        live_board = next((b for b in boards_field if b.get("id") == "ci-live"), {})
        check("/api/sessions 同时返回 boards 字段",
              status == 200 and bool(live_board), str(boards_field)[:300])
        check("面板记录带健康度所需字段",
              live_board.get("ttl_sec") == 60
              and live_board.get("status_hint") == "complete"
              and live_board.get("update_count") == 4
              and live_board.get("format") == "json"
              and float(live_board.get("updated_epoch") or 0) > 0,
              str(live_board)[:400])

        status, feed_payload = get_json(f"{base}/api/feed?limit=200")
        check("全局时间线不包含面板刷新",
              status == 200 and not any(
                  item.get("event_type") == "board" or item.get("board_id")
                  for item in feed_payload.get("items", [])),
              str(feed_payload)[:300])
        check("全部面板操作后回复侧投影仍不变",
              reply_projection() == projection_before)

        status, _, _ = raw_request(port, "GET", "/artifact/board/ci-live")
        check("面板切回 json 后 artifact 恢复 404", status == 404, str(status))

        # ---------- 17d. 公开地址（public_url）回执 ----------
        print("\n[17d] 公开地址回执与配置优先级")
        config_path = cache / "config.json"
        if config_path.exists():
            config_path.unlink()

        code, baseline = publish(port, cache, MD, "公开地址基线")
        check("未配置公开地址时回执退回本地深链",
              code == 0 and baseline.get("url") == baseline.get("local_url")
              and str(baseline.get("url", "")).startswith("http://127.0.0.1:")
              and baseline.get("public_url") is None
              and baseline.get("public_base_url") is None
              and baseline.get("base_url") == baseline.get("local_base_url"),
              str(baseline)[:400])

        config_path.write_text(json.dumps({"public_url": "https://vr.example.com/"}),
                               encoding="utf-8")
        code, configured = publish(
            port, cache, MD, "公开地址配置",
            env=clean_env(ANTIGRAVITY_CONVERSATION_ID="public-url-config"))
        sid = configured.get("session_id")
        full_query = urllib.parse.urlencode({"session": sid, "reply": configured.get("reply_id")})
        session_query = urllib.parse.urlencode({"session": sid})
        check("config.json 的 public_url 生效并去掉末尾斜杠",
              code == 0
              and configured.get("public_base_url") == "https://vr.example.com"
              and configured.get("base_url") == "https://vr.example.com"
              and configured.get("url") == f"https://vr.example.com/?{full_query}"
              and configured.get("session_url") == f"https://vr.example.com/?{session_query}",
              str(configured)[:400])
        check("公开地址生效时 local_url 仍指向本机",
              configured.get("local_url") == f"http://127.0.0.1:{port}/?{full_query}"
              and configured.get("local_session_url")
              == f"http://127.0.0.1:{port}/?{session_query}"
              and str(configured.get("local_url", "")).startswith("http://127.0.0.1:"),
              str(configured.get("local_url")))

        code, env_reply = publish(
            port, cache, MD, "公开地址环境变量",
            env=clean_env(VISUAL_REPLY_PUBLIC_URL="https://vr-env.example.com"))
        check("VISUAL_REPLY_PUBLIC_URL 覆盖 config.json",
              code == 0 and env_reply.get("public_base_url") == "https://vr-env.example.com"
              and str(env_reply.get("url", "")).startswith("https://vr-env.example.com/?session="),
              str(env_reply)[:400])

        code, env_board = publish(
            port, cache, board_one_text, "公开地址面板",
            extra=["--board", "public-board", "--project", "release"],
            env=clean_env(VISUAL_REPLY_PUBLIC_URL="https://vr-env.example.com"))
        check("面板回执同样使用公开地址",
              code == 0
              and env_board.get("url") == "https://vr-env.example.com/?board=public-board"
              and env_board.get("public_url")
              == "https://vr-env.example.com/?board=public-board"
              and env_board.get("public_base_url") == "https://vr-env.example.com"
              and str(env_board.get("local_url", "")).startswith("http://127.0.0.1:")
              and str(env_board.get("local_url", "")).endswith("/?board=public-board"),
              str(env_board)[:400])

        code, cli_receipt = publish(
            port, cache, MD, "公开地址命令行",
            extra=["--public-url", "https://vr-cli.example.com"],
            env=clean_env(VISUAL_REPLY_PUBLIC_URL="https://vr-env.example.com"))
        check("--public-url 覆盖 VISUAL_REPLY_PUBLIC_URL",
              code == 0 and cli_receipt.get("public_base_url") == "https://vr-cli.example.com"
              and str(cli_receipt.get("url", "")).startswith("https://vr-cli.example.com/?session="),
              str(cli_receipt)[:400])

        code, bad = publish(
            port, cache, MD, "公开地址非法",
            extra=["--public-url", "ftp://bad.example"])
        check("非法 --public-url 安全回退本地深链",
              code == 0 and bad.get("public_url") is None
              and bad.get("public_base_url") is None
              and bad.get("url") == bad.get("local_url")
              and str(bad.get("url", "")).startswith("http://127.0.0.1:"),
              str(bad)[:400])

        if config_path.exists():
            config_path.unlink()
        check("公开地址测试配置已清理", not config_path.exists(), str(config_path))

    finally:
        print("\n[18] 清理")
        if fake_proc is not None and fake_proc.poll() is None:
            fake_proc.kill()
            fake_proc.wait(timeout=5)
        if stress_proc is not None and stress_proc.poll() is None:
            stress_proc.kill()
            stress_proc.wait(timeout=5)
        stop_server(cache, port)
        stop_server(cache_v1, port_v1)
        stop_server(stress_cache, stress_port)
        stop_server(cold_cache, cold_port)
        closed = (wait_port_closed(port) and wait_port_closed(port_v1)
                  and wait_port_closed(stress_port) and wait_port_closed(cold_port))
        check("测试服务已关闭", closed,
              f"端口 {port} / {port_v1} / {stress_port} / {cold_port} 仍可连接")
        for path in (cache, cache_v1, geo_dir, stress_cache, cold_cache, store_cache):
            shutil.rmtree(path, ignore_errors=True)
        check("临时目录已清理",
              not any(p.exists() for p in (cache, cache_v1, geo_dir, stress_cache, cold_cache, store_cache)),
              f"{cache} / {cache_v1} / {geo_dir} / {stress_cache} / {cold_cache} / {store_cache}")

    total = PASSED + len(FAILED)
    print(f"\n结果: {PASSED}/{total} 通过")
    if FAILED:
        print("失败项:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("全部测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
