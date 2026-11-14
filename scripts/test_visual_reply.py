#!/usr/bin/env python3
"""visual-reply v2 端到端自动化测试。

覆盖：
    md / json / html 三种载荷、auto 自动识别、五种画布组件元数据、
    前端 PAGE_HTML 组件存在性、artifact 接口、混合历史、路径穿越防护、
    旧版服务接管升级、复杂 6 列回环拓扑的 JS 几何断言、发布长度提醒，
    以及开源卫生检查（隐私路径、文档互链、.gitignore、CI 配置、跨平台打开浏览器）。

使用临时目录与随机可用端口，不触碰真实缓存目录；测试结束后关闭服务并清理。

    python3 test_visual_reply.py
"""

from __future__ import annotations

import importlib.util
import json
import os
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
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PUBLISH = SCRIPT_DIR / "publish.py"
SERVER = SCRIPT_DIR / "server.py"
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


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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


def http(url: str):
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=6) as resp:
        return resp.status, resp.read(), resp.headers


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


def publish(port: int, cache: Path, content: str, title: str, fmt: str = "auto") -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(PUBLISH),
         "--title", title, "--stdin", "--no-browser",
         "--format", fmt,
         "--port", str(port), "--cache-dir", str(cache)],
        input=content.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=50,
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
    port = free_port()
    cache_v1 = Path(tempfile.mkdtemp(prefix="vr-test-v1-")).resolve()
    port_v1 = free_port()
    geo_dir = Path(tempfile.mkdtemp(prefix="vr-geo-")).resolve()
    print(f"临时目录: {cache}")
    print(f"测试端口: {port}")

    fake_proc = None

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
        check("页面包含轮询逻辑", "setInterval(poll, 1000)" in page)
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

        def v2_up() -> bool:
            try:
                _, info = get_json(f"http://127.0.0.1:{port_v1}/healthz")
                return info.get("version") == EXPECTED_VERSION
            except Exception:
                return False

        check("端口上运行的是 v2 服务", wait_for(v2_up, 10), "v2 未接管端口")
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

        scanned = [skill_text, readme, readme_cn, publish_text,
                   SERVER.read_text(encoding="utf-8"),
                   Path(__file__).read_text(encoding="utf-8")]
        names = ["SKILL.md", "README.md", "README_CN.md", "scripts/publish.py",
                 "scripts/server.py", "scripts/test_visual_reply.py"]

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

    finally:
        print("\n[12] 清理")
        if fake_proc is not None and fake_proc.poll() is None:
            fake_proc.kill()
            fake_proc.wait(timeout=5)
        stop_server(cache, port)
        stop_server(cache_v1, port_v1)
        closed = wait_port_closed(port) and wait_port_closed(port_v1)
        check("测试服务已关闭", closed, f"端口 {port} / {port_v1} 仍可连接")
        shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(cache_v1, ignore_errors=True)
        shutil.rmtree(geo_dir, ignore_errors=True)
        check("临时目录已清理", not cache.exists() and not cache_v1.exists() and not geo_dir.exists(),
              f"{cache} / {cache_v1} / {geo_dir}")

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
