#!/usr/bin/env python3
"""visual-reply 本地预览服务 v6（纯标准库，多会话工作台）。

三种渲染模式：
    md     兼容模式：阅读 / 脑图 / 分屏
    json   Visual Canvas 交互画布：kpis / graph / comparison / bento / steps /
           table / chart / prose
    html   HTML Artifact 沙箱：iframe 渲染 + 源码查看

接口：
    GET  /                                  单页 HTML（多会话工作台）
    GET  /api/latest                        最新内容 {version, format, title, content, mtime, updated_at}
    GET  /api/history                       最近 20 条历史（v3 投影）
    GET  /api/history/<file>                单条历史
    GET  /api/sessions                      会话列表（置顶优先，seq 倒序）
    GET  /api/sessions/<sid>/replies        某会话的回复轮次（最新在前）
    GET  /api/sessions/<sid>/replies/<rid>  单条回复（含正文）
    GET  /api/feed?limit=50                 跨会话全局时间线
    GET  /api/state?since=<seq>             增量补播事件
    GET  /api/events                        SSE 实时推送
    POST /api/notify                        唤醒 SSE（发布后立即刷新）
    POST /api/sessions/<sid>                更新 label / pinned
    GET  /artifact/latest                   HTML 原文（供 iframe 加载）
    GET  /artifact/history/<file>           历史 HTML 原文
    GET  /artifact/session/<sid>/<rid>      会话回复的 HTML 原文
    GET  /healthz                           健康检查（含 version）
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import vr_store  # noqa: E402  - 保证按路径加载时也能 import

DEFAULT_PORT = 18765
DEFAULT_CACHE_DIR = Path("~/.cache/agent-visual-reply").expanduser()
SERVICE_NAME = "visual-reply"
VERSION = 6
HISTORY_LIMIT = 20
REPLY_LIMIT = 200
FEED_LIMIT = 50
SSE_CLIENTS = 32
SSE_QUEUE = 64
SSE_POLL = 0.35
SSE_KEEPALIVE = 20.0

FORMAT_EXT = {"md": ".md", "json": ".json", "html": ".html"}
EXT_FORMAT = {".md": "md", ".json": "json", ".html": "html"}

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Visual Reply</title>
<style>
* { box-sizing: border-box; }
[hidden] { display: none !important; }
:root {
  color-scheme: light;
  --bg: #ffffff;
  --bg-soft: #f6f8fa;
  --bg-elev: #ffffff;
  --fg: #1f2328;
  --fg-muted: #6b7280;
  --border: #e4e8ec;
  --border-strong: #d5dbe1;
  --accent: #2f6feb;
  --accent-soft: rgba(47, 111, 235, .10);
  --code-bg: #f6f8fa;
  --table-head: #f2f5f8;
  --table-alt: #fafbfc;
  --shadow: 0 4px 14px rgba(15, 23, 42, .10);
  --ok: #16a34a;
  --warn: #d97706;
  --err: #dc2626;
  --info: #2f6feb;
  --vc-0: #2f6feb;
  --vc-1: #0891b2;
  --vc-2: #16a34a;
  --vc-3: #d97706;
  --vc-4: #9333ea;
  --vc-5: #dc2626;
}
html[data-theme="dark"] {
  color-scheme: dark;
  --bg: #0d1117;
  --bg-soft: #161b22;
  --bg-elev: #161b22;
  --fg: #e6edf3;
  --fg-muted: #8b949e;
  --border: #21262d;
  --border-strong: #30363d;
  --accent: #58a6ff;
  --accent-soft: rgba(88, 166, 255, .12);
  --code-bg: #161b22;
  --table-head: #161b22;
  --table-alt: #11161d;
  --shadow: 0 4px 14px rgba(0, 0, 0, .45);
  --ok: #3fb950;
  --warn: #d29922;
  --err: #f85149;
  --info: #58a6ff;
  --vc-0: #58a6ff;
  --vc-1: #39c5cf;
  --vc-2: #3fb950;
  --vc-3: #d29922;
  --vc-4: #bc8cff;
  --vc-5: #f85149;
}
html, body { height: 100%; }
body {
  margin: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  font-size: 15px;
  -webkit-font-smoothing: antialiased;
}
#topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 9px 16px;
  background: var(--bg-elev);
  border-bottom: 1px solid var(--border);
  z-index: 10;
}
.brand { display: flex; align-items: center; gap: 7px; font-size: 13.5px; font-weight: 650; letter-spacing: .2px; }
.brand .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.doc { min-width: 0; max-width: 42vw; }
.doc-title { font-size: 13.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.doc-sub { font-size: 11.5px; color: var(--fg-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.spacer { flex: 1 1 auto; }
button, select {
  font: inherit;
  font-size: 12.5px;
  color: var(--fg);
  background: var(--bg-soft);
  border: 1px solid var(--border-strong);
  border-radius: 9px;
  padding: 6px 10px;
  cursor: pointer;
  transition: background .15s, border-color .15s, color .15s;
}
button:hover, select:hover { border-color: var(--accent); }
button:focus-visible, select:focus-visible { outline: 2px solid var(--accent-soft); outline-offset: 1px; }
#history-select { max-width: 260px; }
.seg { display: flex; gap: 2px; padding: 2px; background: var(--bg-soft); border: 1px solid var(--border-strong); border-radius: 10px; }
.seg button { border: 0; background: transparent; color: var(--fg-muted); font-weight: 550; padding: 5px 12px; border-radius: 8px; }
.seg button[data-active="true"] { background: var(--bg-elev); color: var(--fg); box-shadow: 0 1px 3px rgba(0, 0, 0, .14); }
.status { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--fg-muted); white-space: nowrap; }
.status i { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 3px rgba(22, 163, 74, .14); }
.status[data-state="offline"] i { background: var(--err); box-shadow: 0 0 0 3px rgba(220, 38, 38, .14); }
.status[data-state="stale"] i { background: var(--warn); box-shadow: 0 0 0 3px rgba(217, 119, 6, .14); }
#stage { flex: 1 1 auto; display: flex; min-height: 0; }
.pane { position: relative; min-width: 0; }
#reading-pane { flex: 1 1 auto; overflow: auto; }
#mindmap-pane { flex: 0 0 46%; overflow: hidden; border-left: 1px solid var(--border); background: var(--bg); }
body.view-reading #mindmap-pane { display: none; }
body.view-mindmap #reading-pane { display: none; }
body.view-mindmap #mindmap-pane { flex: 1 1 auto; border-left: 0; }
#mindmap-svg { display: block; width: 100%; height: 100%; }
#mindmap-pane .empty { position: absolute; inset: 0; }
#canvas-pane { flex: 1 1 auto; overflow: auto; display: none; }
#artifact-pane { flex: 1 1 auto; display: none; flex-direction: column; min-height: 0; }
body.mode-canvas #canvas-pane { display: block; }
body.mode-html #artifact-pane { display: flex; }
body.mode-canvas #reading-pane, body.mode-canvas #mindmap-pane,
body.mode-html #reading-pane, body.mode-html #mindmap-pane,
body.mode-html #canvas-pane { display: none !important; }
#artifact-frame { flex: 1 1 auto; width: 100%; border: 0; background: #ffffff; }
#artifact-source {
  flex: 1 1 auto; margin: 0; padding: 22px 26px; overflow: auto; white-space: pre-wrap;
  font-family: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace;
  font-size: 12.5px; line-height: 1.6; color: var(--fg); background: var(--bg);
}
.markdown-body { max-width: 980px; margin: 0 auto; padding: 32px 44px 140px; line-height: 1.78; font-size: 15.5px; }
.markdown-body > :first-child { margin-top: 0; }
.markdown-body h1, .markdown-body h2, .markdown-body h3,
.markdown-body h4, .markdown-body h5, .markdown-body h6 { line-height: 1.35; margin: 1.6em 0 .65em; font-weight: 650; }
.markdown-body h1 { font-size: 1.85em; letter-spacing: -.01em; }
.markdown-body h2 { font-size: 1.4em; padding-bottom: .3em; border-bottom: 1px solid var(--border); }
.markdown-body h3 { font-size: 1.16em; }
.markdown-body h4 { font-size: 1.02em; }
.markdown-body p { margin: .75em 0; }
.markdown-body a { color: var(--accent); text-decoration: none; }
.markdown-body a:hover { text-decoration: underline; }
.markdown-body ul, .markdown-body ol { margin: .7em 0; padding-left: 1.6em; }
.markdown-body li { margin: .28em 0; }
.markdown-body li > p { margin: .3em 0; }
.markdown-body strong { font-weight: 650; }
.markdown-body blockquote {
  margin: 1em 0; padding: .2em 1em; color: var(--fg-muted);
  border-left: 3px solid var(--border-strong); background: var(--bg-soft);
  border-radius: 0 8px 8px 0;
}
.markdown-body hr { border: 0; border-top: 1px solid var(--border); margin: 2em 0; }
.markdown-body img { max-width: 100%; border-radius: 10px; }
.markdown-body code {
  font-family: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace;
  font-size: .88em; background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: .12em .38em;
}
.markdown-body pre {
  margin: 1em 0; padding: 14px 16px; overflow: auto;
  background: var(--bg-soft); border: 1px solid var(--border-strong); border-radius: 12px;
}
.markdown-body pre code { background: none; border: 0; padding: 0; font-size: 13px; }
.markdown-body .table-wrap { margin: 1.2em 0; border: 1px solid var(--border-strong); border-radius: 12px; overflow: auto; }
.markdown-body table { width: 100%; border-collapse: collapse; font-size: 14px; }
.markdown-body thead th {
  background: var(--table-head); text-align: left; font-weight: 650;
  padding: 11px 14px; border-bottom: 1px solid var(--border-strong); white-space: nowrap;
}
.markdown-body tbody td { padding: 10px 14px; border-bottom: 1px solid var(--border); vertical-align: top; }
.markdown-body tbody tr:last-child td { border-bottom: 0; }
.markdown-body tbody tr:nth-child(even) { background: var(--table-alt); }
.markdown-body tbody tr:hover { background: var(--accent-soft); }
.sec > h1, .sec > h2, .sec > h3, .sec > h4, .sec > h5, .sec > h6 { position: relative; padding-left: 17px; cursor: pointer; }
.sec > h1::before, .sec > h2::before, .sec > h3::before,
.sec > h4::before, .sec > h5::before, .sec > h6::before {
  content: "\25BE"; position: absolute; left: 0; top: 50%; transform: translateY(-50%);
  font-size: .62em; color: var(--fg-muted); opacity: .8; transition: transform .15s ease;
}
.sec.collapsed > h1::before, .sec.collapsed > h2::before, .sec.collapsed > h3::before,
.sec.collapsed > h4::before, .sec.collapsed > h5::before, .sec.collapsed > h6::before {
  transform: translateY(-50%) rotate(-90deg);
}
.sec-body { min-height: 0; }
.sec.collapsed > .sec-body { display: none; }
.mermaid-block {
  display: flex; justify-content: center; margin: 1.3em 0; padding: 16px;
  border: 1px solid var(--border); border-radius: 12px; background: var(--bg-soft); overflow: auto;
}
.mermaid-block svg { max-width: 100%; height: auto; }
.mermaid-hint { color: var(--fg-muted); font-size: 12.5px; }
.diagram-error { margin: 0; padding: 0; border: 0; background: none; color: var(--err); font-size: 12.5px; white-space: pre-wrap; }
.empty { display: flex; align-items: center; justify-content: center; height: 100%; padding: 40px; text-align: center; color: var(--fg-muted); font-size: 14px; }
#toast {
  position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%);
  padding: 9px 15px; border-radius: 10px; font-size: 13px;
  background: var(--bg-elev); border: 1px solid var(--border-strong); box-shadow: var(--shadow);
  z-index: 90;
}

/* ---------- Visual Canvas ---------- */
#canvas-root { max-width: 1200px; margin: 0 auto; padding: 30px 30px 140px; }
.canvas-headline { margin: 0; font-size: 22px; font-weight: 680; line-height: 1.45; letter-spacing: -.01em; }
.badges { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 13px; }
.badge {
  font-size: 11.5px; font-weight: 550; padding: 3px 9px; border-radius: 999px;
  border: 1px solid var(--border-strong); background: var(--bg-soft); color: var(--fg-muted);
}
.badge[data-tone="ok"] { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 40%, transparent); background: color-mix(in srgb, var(--ok) 10%, transparent); }
.badge[data-tone="warn"] { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, transparent); background: color-mix(in srgb, var(--warn) 10%, transparent); }
.badge[data-tone="err"] { color: var(--err); border-color: color-mix(in srgb, var(--err) 40%, transparent); background: color-mix(in srgb, var(--err) 10%, transparent); }
.badge[data-tone="accent"] { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, transparent); background: var(--accent-soft); }
.cv-sections { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; margin-top: 30px; }
.cv-section { grid-column: 1 / -1; min-width: 0; margin-top: 0; }
.cv-section[data-span="1"] { grid-column: span 1; }
#stage[data-split] .cv-section[data-span="1"] { grid-column: 1 / -1; }
.cv-sec-title { font-size: 11.5px; font-weight: 650; letter-spacing: .09em; text-transform: uppercase; color: var(--fg-muted); margin-bottom: 12px; }
.raw-json {
  margin: 0; padding: 18px 20px; overflow: auto; white-space: pre-wrap;
  font-family: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace;
  font-size: 12.5px; line-height: 1.65; color: var(--fg);
  background: var(--bg-soft); border: 1px solid var(--border); border-radius: 14px;
}
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(158px, 1fr)); gap: 12px; }
.kpi { position: relative; padding: 15px 16px 14px; border: 1px solid var(--border); border-radius: 14px; background: var(--bg-elev); }
.kpi-value { font-size: 25px; font-weight: 690; line-height: 1.15; letter-spacing: -.02em; }
.kpi-label { margin-top: 6px; font-size: 12px; color: var(--fg-muted); }
.kpi-note { margin-top: 5px; font-size: 11px; color: var(--fg-muted); opacity: .8; }
.kpi-dot { position: absolute; top: 15px; right: 15px; width: 8px; height: 8px; border-radius: 50%; background: var(--info); }
.kpi[data-status="ok"] .kpi-dot { background: var(--ok); box-shadow: 0 0 0 3px rgba(22, 163, 74, .14); }
.kpi[data-status="warn"] .kpi-dot { background: var(--warn); box-shadow: 0 0 0 3px rgba(217, 119, 6, .14); }
.kpi[data-status="err"] .kpi-dot { background: var(--err); box-shadow: 0 0 0 3px rgba(220, 38, 38, .14); }
.kpi[data-status="info"] .kpi-dot { background: var(--info); box-shadow: 0 0 0 3px var(--accent-soft); }

.graph-block { position: relative; }
.graph-tools { display: flex; align-items: center; justify-content: flex-end; gap: 4px; margin: 0 0 8px; }
.graph-tools button {
  font-size: 11.5px; line-height: 1; padding: 5px 9px; border-radius: 7px;
  background: var(--bg-soft); border: 1px solid var(--border-strong); color: var(--fg-muted);
}
.graph-tools button:hover { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
.graph-zoom { min-width: 44px; text-align: center; font-size: 11.5px; font-variant-numeric: tabular-nums; color: var(--fg-muted); }
.graph-wrap { position: relative; border: 1px solid var(--border); border-radius: 16px; background: var(--bg-soft); overflow: auto; max-height: 72vh; }
.graph-scale-box { position: relative; }
.graph-inner { position: relative; transform-origin: 0 0; }
.graph-svg { position: absolute; left: 0; top: 0; z-index: 1; pointer-events: none; overflow: visible; }
.graph-svg .g-edge { fill: none; stroke: var(--fg-muted); stroke-opacity: .5; stroke-width: 1.6; transition: stroke .16s, opacity .16s, stroke-opacity .16s; }
.graph-svg .g-edge.hl { stroke: var(--accent); stroke-opacity: 1; stroke-width: 2.2; }
.graph-svg .g-edge.dim { opacity: .16; }
.graph-svg .g-arrow { fill: var(--fg-muted); fill-opacity: .65; }
.graph-labels { position: absolute; inset: 0; z-index: 3; pointer-events: none; }
.g-edge-pill {
  position: absolute; transform: translate(-50%, -50%);
  padding: 1px 8px; border-radius: 999px; white-space: nowrap;
  font-size: 10.5px; line-height: 15px; color: var(--fg-muted);
  background: var(--bg-elev); border: 1px solid var(--border-strong);
  box-shadow: 0 1px 2px rgba(15, 23, 42, .07);
  transition: color .16s, border-color .16s, opacity .16s;
}
.g-edge-pill.hl { color: var(--accent); border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
.g-edge-pill.dim { opacity: .2; }
.g-node {
  position: absolute; z-index: 2; display: flex; flex-direction: column; justify-content: center; gap: 2px;
  padding: 8px 12px; border: 1px solid var(--border-strong); border-radius: 12px;
  background: var(--bg-elev); cursor: pointer; overflow: hidden;
  transition: border-color .16s, box-shadow .16s, opacity .16s;
}
.g-node:hover { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
.g-node .g-label {
  font-size: 12.8px; font-weight: 620; line-height: 1.28;
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2;
  overflow: hidden; overflow-wrap: anywhere;
}
.g-node .g-tag { font-size: 10.5px; color: var(--fg-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.g-node .g-dot { position: absolute; right: 9px; top: 9px; width: 7px; height: 7px; border-radius: 50%; background: var(--info); }
.g-node[data-status="ok"] .g-dot { background: var(--ok); }
.g-node[data-status="warn"] .g-dot { background: var(--warn); }
.g-node[data-status="err"] .g-dot { background: var(--err); }
.g-node.hl { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
.g-node.dim { opacity: .26; }
.g-node:focus-visible, .bento-card:focus-visible, .step:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.graph-legend { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 10px; font-size: 11px; color: var(--fg-muted); }
.graph-legend span { display: inline-flex; align-items: center; gap: 5px; }
.graph-legend span::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--info); }
.graph-legend span[data-status="ok"]::before { background: var(--ok); }
.graph-legend span[data-status="warn"]::before { background: var(--warn); }
.graph-legend span[data-status="err"]::before { background: var(--err); }

.cmp-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(252px, 1fr)); gap: 14px; }
.cmp-card { display: flex; flex-direction: column; gap: 10px; padding: 16px; border: 1px solid var(--border); border-radius: 15px; background: var(--bg-elev); }
.cmp-card[data-recommended="true"] { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
.cmp-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.cmp-name { font-size: 15px; font-weight: 660; }
.cmp-chip { font-size: 10.5px; font-weight: 600; padding: 2px 8px; border-radius: 999px; color: var(--accent); background: var(--accent-soft); border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent); }
.cmp-score-row { display: grid; grid-template-columns: 64px 1fr 24px; align-items: center; gap: 8px; font-size: 11.5px; color: var(--fg-muted); }
.cmp-score-row b { font-weight: 600; color: var(--fg); text-align: right; font-variant-numeric: tabular-nums; }
.bar { height: 6px; border-radius: 999px; background: var(--bg-soft); border: 1px solid var(--border); overflow: hidden; }
.bar > i { display: block; height: 100%; background: var(--accent); border-radius: 999px; transition: width .3s ease; }
.cmp-card[data-recommended="true"] .bar > i { background: var(--ok); }
.cmp-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; }
.cmp-list li { display: flex; gap: 6px; align-items: flex-start; line-height: 1.5; }
.cmp-list li::before { flex: 0 0 auto; font-weight: 700; }
.cmp-list.pros li::before { content: "\2713"; color: var(--ok); }
.cmp-list.cons li::before { content: "\2715"; color: var(--err); }
.cmp-summary { font-size: 12.5px; color: var(--fg-muted); line-height: 1.55; }
.cmp-foot { margin-top: auto; }
.ghost-btn { font-size: 12px; padding: 5px 11px; border-radius: 8px; background: transparent; border: 1px solid var(--border-strong); color: var(--fg-muted); }
.ghost-btn:hover { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }

.bento { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.bento-card {
  grid-column: span var(--span, 1); display: flex; flex-direction: column; gap: 6px;
  padding: 15px 16px; border: 1px solid var(--border); border-radius: 15px; background: var(--bg-elev);
  cursor: pointer; transition: border-color .15s, transform .15s, box-shadow .15s;
}
.bento-card:hover { border-color: var(--accent); transform: translateY(-1px); box-shadow: var(--shadow); }
.bento-card[data-tone="accent"] { border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); background: linear-gradient(180deg, var(--accent-soft), transparent 60%), var(--bg-elev); }
.bento-card[data-tone="ok"] { border-color: color-mix(in srgb, var(--ok) 38%, var(--border)); }
.bento-card[data-tone="warn"] { border-color: color-mix(in srgb, var(--warn) 38%, var(--border)); }
.bento-card[data-tone="err"] { border-color: color-mix(in srgb, var(--err) 38%, var(--border)); }
.bc-tag { font-size: 10.5px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: var(--fg-muted); }
.bento-card[data-tone="accent"] .bc-tag { color: var(--accent); }
.bento-card[data-tone="ok"] .bc-tag { color: var(--ok); }
.bento-card[data-tone="warn"] .bc-tag { color: var(--warn); }
.bento-card[data-tone="err"] .bc-tag { color: var(--err); }
.bc-metric { font-size: 24px; font-weight: 690; letter-spacing: -.02em; line-height: 1.15; }
.bc-title { font-size: 13.5px; font-weight: 620; }
.bc-summary { font-size: 12px; color: var(--fg-muted); line-height: 1.5; }

.step-track { display: flex; align-items: stretch; border: 1px solid var(--border); border-radius: 15px; background: var(--bg-elev); padding: 16px 18px 14px; overflow-x: auto; }
.step { position: relative; flex: 1 0 122px; padding: 22px 16px 0 0; cursor: pointer; min-width: 0; }
.step .dot { position: absolute; top: 0; left: 0; width: 13px; height: 13px; border-radius: 50%; border: 2px solid var(--border-strong); background: var(--bg); z-index: 2; }
.step .line { position: absolute; top: 6px; left: 15px; right: 0; height: 2px; background: var(--border-strong); }
.step:last-child .line { display: none; }
.step[data-status="done"] .dot { background: var(--ok); border-color: var(--ok); }
.step[data-status="done"] .line { background: var(--ok); }
.step[data-status="active"] .dot { background: var(--accent); border-color: var(--accent); box-shadow: 0 0 0 4px var(--accent-soft); }
.step-num { font-size: 10.5px; font-weight: 650; color: var(--fg-muted); letter-spacing: .05em; }
.step-title { margin-top: 3px; font-size: 13px; font-weight: 620; line-height: 1.4; }
.step-summary { margin-top: 4px; font-size: 11.5px; color: var(--fg-muted); line-height: 1.5; }
.step[data-status="active"] .step-title { color: var(--accent); }

/* ---------- 科研表格 / 图表 / 长文 ---------- */
.tbl-card { border: 1px solid var(--border); border-radius: 16px; background: var(--bg-elev); overflow: hidden; }
.tbl-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 10px 12px; border-bottom: 1px solid var(--border); background: var(--bg-soft); }
.tbl-caption { flex: 1 1 150px; min-width: 0; font-size: 12px; color: var(--fg-muted); }
.tbl-tools { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-left: auto; }
.tbl-search {
  width: 138px; padding: 5px 9px; font-size: 12px; color: var(--fg);
  background: var(--bg-elev); border: 1px solid var(--border-strong); border-radius: 8px;
}
.tbl-search:focus { outline: 2px solid var(--accent-soft); border-color: var(--accent); }
.tbl-btn {
  font-size: 11.5px; line-height: 1; padding: 5px 9px; border-radius: 7px;
  background: var(--bg-elev); border: 1px solid var(--border-strong); color: var(--fg-muted);
}
.tbl-btn:hover { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
.tbl-btn[aria-pressed="true"] { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 45%, transparent); background: var(--accent-soft); }
.tbl-scroll { overflow: auto; max-height: 68vh; }
.exp-tbl { width: 100%; border-collapse: collapse; font-size: 12.8px; font-variant-numeric: tabular-nums; }
.exp-tbl thead th {
  position: sticky; top: 0; z-index: 2; padding: 9px 11px; white-space: nowrap;
  background: var(--table-head); border-bottom: 1px solid var(--border-strong);
  font-weight: 650; font-size: 11.5px; color: var(--fg-muted); cursor: pointer; user-select: none;
}
.exp-tbl thead th.tbl-act-head { cursor: default; width: 72px; }
.exp-tbl thead th:hover { color: var(--accent); }
.exp-tbl thead th.sorted { color: var(--accent); }
.exp-tbl thead th.tbl-group-th { text-align: center; cursor: default; background: var(--bg-soft); font-size: 10.5px; letter-spacing: .04em; }
.exp-tbl thead tr.tbl-group-row th { position: static; }
.tbl-unit { font-weight: 500; color: var(--fg-muted); font-size: 10.5px; }
.tbl-sort { margin-left: 3px; font-size: 10px; opacity: .85; }
.exp-tbl tbody td { position: relative; padding: 8px 11px; border-bottom: 1px solid var(--border); vertical-align: top; }
.exp-tbl tbody tr:last-child td { border-bottom: 0; }
.exp-tbl tbody tr:nth-child(even) { background: var(--table-alt); }
.exp-tbl tbody tr:hover { background: var(--accent-soft); }
.exp-tbl tbody tr[data-drawer] { cursor: pointer; }
.exp-tbl tbody tr.divider td { border-top: 1px solid var(--border-strong); }
.exp-tbl tbody tr.emph td { background: color-mix(in srgb, var(--accent) 7%, transparent); font-weight: 600; }
.exp-tbl tbody tr.is-base td { background: color-mix(in srgb, var(--warn) 9%, transparent); }
.tbl-act { width: 72px; white-space: nowrap; }
.tbl-base-btn {
  padding: 3px 7px; font-size: 10.5px; line-height: 1.2; border-radius: 6px;
  color: var(--fg-muted); background: transparent; border: 1px solid var(--border-strong);
  opacity: .32; transition: opacity .15s, color .15s, border-color .15s;
}
.exp-tbl tbody tr:hover .tbl-base-btn { opacity: 1; }
.tbl-base-btn:hover { color: var(--warn); border-color: var(--warn); background: color-mix(in srgb, var(--warn) 10%, transparent); }
.tbl-base-tag { display: inline-block; padding: 2px 7px; border-radius: 999px; font-size: 10.5px; font-weight: 600; color: var(--warn); background: color-mix(in srgb, var(--warn) 12%, transparent); border: 1px solid color-mix(in srgb, var(--warn) 38%, transparent); }
.tbl-best { font-weight: 700; color: var(--ok); }
.tbl-second { font-weight: 600; text-decoration: underline; text-underline-offset: 2px; color: var(--fg); }
.tbl-delta { display: block; margin-top: 2px; font-size: 10.5px; line-height: 1.2; font-variant-numeric: tabular-nums; }
.tbl-delta[data-tone="pos"] { color: var(--ok); }
.tbl-delta[data-tone="neg"] { color: var(--err); }
.tbl-delta[data-tone="zero"] { color: var(--fg-muted); }
.tbl-cell-note { border-bottom: 1px dotted var(--fg-muted); cursor: help; }
.tbl-note { padding: 9px 12px; border-top: 1px solid var(--border); font-size: 11.5px; color: var(--fg-muted); line-height: 1.55; }
.tbl-empty { padding: 26px 12px; text-align: center; color: var(--fg-muted); font-size: 12.5px; }
.tbl-export-box { border-top: 1px solid var(--border); background: var(--bg-soft); }
.tbl-export-head { display: flex; align-items: center; gap: 8px; padding: 8px 12px 0; font-size: 11px; font-weight: 650; letter-spacing: .05em; text-transform: uppercase; color: var(--fg-muted); }
.tbl-export-close { margin-left: auto; padding: 2px 7px; font-size: 11px; border-radius: 6px; }
.tbl-export-preview {
  margin: 8px 12px 12px; padding: 10px 12px; max-height: 220px; overflow: auto;
  white-space: pre; font-family: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace;
  font-size: 11.5px; line-height: 1.55; color: var(--fg);
  background: var(--bg-elev); border: 1px solid var(--border); border-radius: 9px;
}

.chart-card { border: 1px solid var(--border); border-radius: 16px; background: var(--bg-elev); padding: 12px 12px 8px; }
.chart-stage { position: relative; }
.chart-svg-host { min-height: 120px; }
.chart-svg { display: block; width: 100%; height: auto; overflow: visible; }
.chart-grid { stroke: var(--border); stroke-width: 1; }
.chart-tick { fill: var(--fg-muted); font-size: 10.5px; font-variant-numeric: tabular-nums; }
.chart-axis-label { fill: var(--fg-muted); font-size: 11px; font-weight: 550; }
.chart-ref { stroke: var(--warn); stroke-width: 1.3; stroke-dasharray: 5 4; opacity: .8; }
.chart-ref-label { fill: var(--warn); font-size: 10.5px; }
.chart-bar { transition: opacity .15s; }
.chart-bar:hover { opacity: .82; }
.chart-line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.chart-line.dashed { stroke-dasharray: 6 4; }
.chart-point { stroke: var(--bg); stroke-width: 1.4; }
.chart-point:hover { stroke: var(--accent); stroke-width: 2; }
.chart-pareto { fill: none; stroke: var(--accent); stroke-width: 1.4; stroke-dasharray: 7 4; opacity: .7; }
.chart-point-label { fill: var(--fg-muted); font-size: 10px; pointer-events: none; }
.chart-cell { stroke: var(--bg); stroke-width: 1; }
.chart-cell[data-tone="good"] { fill: var(--ok); }
.chart-cell[data-tone="bad"] { fill: var(--err); }
.chart-cell[data-tone="strong"] { fill: var(--accent); }
.chart-cell[data-tone="mid"] { fill: var(--accent); }
.chart-cell-text { fill: var(--fg); font-size: 10.5px; font-variant-numeric: tabular-nums; pointer-events: none; }
.chart-tip {
  position: absolute; z-index: 5; max-width: 240px; padding: 5px 8px; border-radius: 7px;
  font-size: 11px; line-height: 1.45; white-space: nowrap; pointer-events: none;
  color: var(--fg); background: var(--bg-elev); border: 1px solid var(--border-strong);
  box-shadow: var(--shadow);
}
.chart-legend { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; padding: 8px 2px 4px; min-height: 28px; }
.chart-pill {
  display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; border-radius: 999px;
  font-size: 11px; color: var(--fg-muted); background: var(--bg-soft); border: 1px solid var(--border-strong);
}
.chart-pill:hover { color: var(--accent); border-color: var(--accent); }
.chart-pill[data-hidden="true"] { opacity: .45; text-decoration: line-through; }
.chart-pill i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.chart-scale-label { font-size: 11px; color: var(--fg-muted); font-variant-numeric: tabular-nums; }
.chart-note { padding: 2px 2px 4px; font-size: 11.5px; color: var(--fg-muted); line-height: 1.55; }

.prose-card.markdown-body {
  max-width: none; margin: 0; padding: 18px 20px; font-size: 14px; line-height: 1.72;
  border: 1px solid var(--border); border-radius: 16px; background: var(--bg-elev);
}
.prose-card.markdown-body > :first-child { margin-top: 0; }
.prose-card.markdown-body > :last-child { margin-bottom: 0; }
.prose-card[data-tone="ok"] { border-color: color-mix(in srgb, var(--ok) 38%, var(--border)); }
.prose-card[data-tone="warn"] { border-color: color-mix(in srgb, var(--warn) 38%, var(--border)); }
.prose-card[data-tone="err"] { border-color: color-mix(in srgb, var(--err) 38%, var(--border)); }
.prose-card[data-tone="accent"] { border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); }
@media (max-width: 960px) {
  .cv-sections { grid-template-columns: minmax(0, 1fr); }
  .cv-section[data-span="1"] { grid-column: 1 / -1; }
  .tbl-search { width: 112px; }
}

/* ---------- Inspector 抽屉 ---------- */
#drawer {
  position: fixed; top: 0; right: 0; height: 100%; width: min(486px, 94vw);
  display: flex; flex-direction: column; z-index: 60;
  background: var(--bg-elev); border-left: 1px solid var(--border-strong);
  box-shadow: -10px 0 32px rgba(15, 23, 42, .16);
  transform: translateX(103%); transition: transform .26s cubic-bezier(.22, .61, .36, 1);
}
#drawer.open { transform: none; }
.drawer-head { display: flex; align-items: flex-start; gap: 10px; padding: 16px 16px 13px; border-bottom: 1px solid var(--border); }
.drawer-head-main { min-width: 0; flex: 1 1 auto; }
.drawer-kicker { font-size: 10.5px; font-weight: 650; letter-spacing: .08em; text-transform: uppercase; color: var(--accent); }
.drawer-title { margin-top: 4px; font-size: 16px; font-weight: 670; line-height: 1.4; word-break: break-word; }
.drawer-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.drawer-close { flex: 0 0 auto; width: 30px; height: 30px; padding: 0; font-size: 17px; line-height: 1; border-radius: 8px; background: var(--bg-soft); }
.drawer-body { flex: 1 1 auto; overflow: auto; padding: 16px; }
.drawer-summary { font-size: 13px; color: var(--fg-muted); line-height: 1.6; padding: 10px 12px; border-radius: 10px; background: var(--bg-soft); border: 1px solid var(--border); }
.drawer-body .markdown-body { max-width: none; padding: 0; font-size: 13.6px; line-height: 1.72; }
.drawer-files { margin-top: 16px; }
.drawer-files-title { font-size: 10.5px; font-weight: 650; letter-spacing: .07em; text-transform: uppercase; color: var(--fg-muted); margin-bottom: 7px; }
.file-chip {
  display: inline-block; margin: 0 6px 6px 0; padding: 4px 8px; border-radius: 7px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11.5px;
  background: var(--code-bg); border: 1px solid var(--border); color: var(--fg-muted);
  word-break: break-all;
}
.drawer-foot { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 10px 16px; border-top: 1px solid var(--border); font-size: 11.5px; color: var(--fg-muted); }
body.drawer-open #stage { margin-right: min(486px, 94vw); transition: margin-right .26s cubic-bezier(.22, .61, .36, 1); }
#stage { transition: margin-right .26s cubic-bezier(.22, .61, .36, 1); }
@media (max-width: 900px) {
  .doc { order: 10; flex-basis: 100%; max-width: 100%; }
  .markdown-body { padding: 22px 20px 100px; font-size: 15px; }
  .pane-set.view-split .pane-frame { flex-direction: column; }
  .pane-set.view-split .pane[data-pane-role="mindmap"] { flex: 0 0 45%; border-left: 0; border-top: 1px solid var(--border); }
  #history-select { max-width: 170px; }
  #canvas-root { padding: 20px 16px 120px; }
  .bento { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .bento-card { grid-column: span 1; }
  body.drawer-open #stage { margin-right: 0; }
}
/* ---------- v4 Workbench：会话栏 / 分屏 / 阅读守卫 ---------- */
#shell { flex: 1 1 auto; display: flex; min-height: 0; min-width: 0; }
#session-rail {
  flex: 0 0 296px; display: flex; flex-direction: column; min-height: 0;
  background: var(--bg-elev); border-right: 1px solid var(--border);
  transition: flex-basis .18s ease;
}
#session-rail[data-open="false"] { flex-basis: 0; overflow: hidden; border-right: 0; }
.rail-head {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 9px 10px 9px 12px; border-bottom: 1px solid var(--border);
}
.rail-title { font-size: 11px; font-weight: 650; letter-spacing: .08em; text-transform: uppercase; color: var(--fg-muted); }
.rail-tabs { display: flex; gap: 2px; padding: 2px; background: var(--bg-soft); border: 1px solid var(--border-strong); border-radius: 9px; }
.rail-tabs button { border: 0; background: transparent; color: var(--fg-muted); padding: 3px 7px; font-size: 11px; border-radius: 7px; }
.rail-tabs button[data-active="true"] { background: var(--bg-elev); color: var(--fg); box-shadow: 0 1px 3px rgba(0, 0, 0, .14); }
.rail-list { flex: 1 1 auto; overflow: auto; padding: 8px; display: flex; flex-direction: column; gap: 6px; }
.rail-empty { padding: 26px 12px; text-align: center; font-size: 12px; color: var(--fg-muted); line-height: 1.6; }
.rail-card {
  position: relative; display: flex; align-items: flex-start; gap: 6px; padding: 8px 9px 8px 11px;
  border: 1px solid var(--border); border-radius: 11px; background: var(--bg-soft);
  cursor: pointer; transition: border-color .15s, background .15s;
}
.rail-card:hover { border-color: var(--accent); }
.rail-card[data-active="true"] { border-color: var(--accent); background: var(--accent-soft); }
.rail-card:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.rail-accent { position: absolute; left: 0; top: 8px; bottom: 8px; width: 3px; border-radius: 0 3px 3px 0; }
.rail-caret {
  flex: 0 0 auto; width: 16px; height: 16px; padding: 0; margin-top: 1px;
  border: 0; background: transparent; color: var(--fg-muted);
  font-size: 9.5px; line-height: 16px; text-align: center; border-radius: 4px;
}
.rail-caret:hover { color: var(--accent); background: var(--accent-soft); border-color: transparent; }
.rail-main { min-width: 0; flex: 1 1 auto; padding-right: 26px; }
.rail-top { display: flex; align-items: center; gap: 6px; }
.rail-label { font-size: 12.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rail-client {
  flex: 0 0 auto; font-size: 9.5px; font-weight: 650; text-transform: uppercase;
  letter-spacing: .05em; color: var(--fg-muted); border: 1px solid var(--border-strong);
  border-radius: 999px; padding: 0 6px;
}
.rail-sub { margin-top: 3px; font-size: 11.5px; color: var(--fg-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rail-meta { margin-top: 3px; font-size: 10.5px; color: var(--fg-muted); opacity: .85; }
.rail-unread {
  position: absolute; top: 7px; right: 8px; min-width: 16px; height: 16px; padding: 0 4px;
  border-radius: 999px; background: var(--err); color: #fff; font-size: 10px; font-weight: 650;
  line-height: 16px; text-align: center;
}
.rail-actions { position: absolute; right: 6px; bottom: 5px; display: none; gap: 2px; }
.rail-card:hover .rail-actions { display: flex; }
.rail-actions button { padding: 1px 6px; font-size: 10.5px; line-height: 1.5; border-radius: 6px; background: var(--bg-elev); }
.rail-project-pill {
  flex: 0 1 auto; max-width: 88px; padding: 0 6px;
  font-size: 9.5px; font-weight: 600; line-height: 15px; color: var(--fg-muted);
  background: var(--bg-elev); border: 1px solid var(--border); border-radius: 999px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.rail-group { display: flex; flex-direction: column; gap: 2px; }
.rail-group + .rail-group { margin-top: 2px; }
.rail-group-head {
  display: flex; align-items: center; gap: 6px; width: 100%;
  padding: 5px 7px; border: 0; background: transparent; border-radius: 8px;
  color: var(--fg-muted); text-align: left; font-size: 11px; font-weight: 650;
  letter-spacing: .02em; cursor: pointer;
}
.rail-group-head:hover { color: var(--fg); background: var(--bg-soft); border-color: transparent; }
.rail-group-head:focus-visible { outline: 2px solid var(--accent); outline-offset: 0; }
.rail-chevron { flex: 0 0 auto; width: 12px; font-size: 9px; text-align: center; opacity: .85; }
.rail-group-label { flex: 1 1 auto; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rail-group-pinned .rail-group-label { color: var(--warn); }
.rail-group-count {
  flex: 0 0 auto; padding: 0 6px; font-size: 10px; font-weight: 600;
  color: var(--fg-muted); background: var(--bg-soft); border: 1px solid var(--border);
  border-radius: 999px; line-height: 15px;
}
.rail-group-unread {
  flex: 0 0 auto; min-width: 15px; height: 15px; padding: 0 4px;
  border-radius: 999px; background: var(--err); color: #fff;
  font-size: 9.5px; font-weight: 650; line-height: 15px; text-align: center;
}
.rail-group-body {
  display: flex; flex-direction: column; gap: 5px;
  margin-left: 12px; padding-left: 9px; border-left: 1px solid var(--border);
}
.rail-node { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.rail-replies {
  display: flex; flex-direction: column; gap: 2px;
  margin: 1px 0 3px 20px; padding: 3px 0 3px 9px;
  border-left: 2px solid var(--border-strong);
}
.rail-reply {
  position: relative; display: flex; align-items: center; gap: 6px; min-width: 0;
  padding: 4px 6px 4px 7px; border: 1px solid transparent; border-radius: 7px;
  cursor: pointer; transition: background .15s, border-color .15s;
}
.rail-reply:hover { background: var(--bg-soft); border-color: var(--border); }
.rail-reply[data-active="true"] {
  background: var(--accent-soft);
  border-color: color-mix(in srgb, var(--accent) 35%, transparent);
}
.rail-reply:focus-visible { outline: 2px solid var(--accent); outline-offset: 0; }
.rail-reply-num { flex: 0 0 auto; font-size: 10px; font-variant-numeric: tabular-nums; color: var(--fg-muted); }
.rail-reply-main { min-width: 0; flex: 1 1 auto; display: flex; flex-direction: column; gap: 1px; }
.rail-reply-line { display: flex; align-items: center; gap: 4px; min-width: 0; }
.rail-reply-title { flex: 1 1 auto; min-width: 0; font-size: 11.5px; font-weight: 550; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rail-reply-tag {
  flex: 0 0 auto; padding: 0 5px; border-radius: 999px;
  font-size: 9px; font-weight: 650; line-height: 14px;
  color: var(--accent); background: var(--accent-soft);
}
.rail-reply-meta { font-size: 9.5px; color: var(--fg-muted); opacity: .85; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rail-reply-unread { flex: 0 0 auto; width: 5px; height: 5px; border-radius: 50%; background: var(--err); }
.rail-reply-hint { padding: 4px 7px; font-size: 10.5px; color: var(--fg-muted); opacity: .8; }
.rail-foot { padding: 8px 10px; border-top: 1px solid var(--border); }
.rail-foot button { width: 100%; }
.feed-item {
  display: flex; gap: 8px; align-items: flex-start; padding: 8px 10px;
  border: 1px solid var(--border); border-radius: 10px; background: var(--bg-soft); cursor: pointer;
}
.feed-item:hover { border-color: var(--accent); }
.feed-seq { flex: 0 0 auto; font-size: 10.5px; font-variant-numeric: tabular-nums; color: var(--fg-muted); padding-top: 1px; }
.feed-main { min-width: 0; flex: 1 1 auto; }
.feed-title { font-size: 12px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.feed-sub { margin-top: 2px; font-size: 10.5px; color: var(--fg-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#new-reply-banner {
  display: flex; align-items: center; gap: 10px; padding: 7px 16px;
  background: var(--accent-soft); border-bottom: 1px solid var(--border); font-size: 12.5px; z-index: 9;
}
#new-reply-banner .spacer { flex: 1 1 auto; }
#new-reply-banner button { padding: 3px 10px; border-radius: 7px; }
#toast .toast-action { margin-left: 9px; padding: 2px 9px; border-radius: 6px; border-color: var(--accent); color: var(--accent); }
#stage { min-width: 0; }
#stage[data-split="h"] #pane-primary { flex: 1 1 50%; }
#stage[data-split="h"] #pane-secondary { flex: 1 1 50%; border-left: 1px solid var(--border-strong); }
#stage[data-split="v"] { flex-direction: column; }
#stage[data-split="v"] #pane-primary { flex: 1 1 50%; }
#stage[data-split="v"] #pane-secondary { flex: 1 1 50%; border-top: 1px solid var(--border-strong); }
.pane-set { display: flex; flex-direction: column; min-width: 0; min-height: 0; flex: 1 1 100%; }
.pane-frame { flex: 1 1 auto; display: flex; min-height: 0; min-width: 0; }
.pane-bar { display: flex; align-items: center; gap: 8px; padding: 5px 10px; border-bottom: 1px solid var(--border); background: var(--bg-elev); }
.pane-bar .doc { max-width: 42%; }
.pane-bar select { max-width: 190px; }
.pane-set .pane[data-pane-role="reading"] { flex: 1 1 auto; overflow: auto; }
.pane-set .pane[data-pane-role="mindmap"] { flex: 0 0 46%; overflow: hidden; border-left: 1px solid var(--border); background: var(--bg); }
.pane-set .pane[data-pane-role="canvas"] { flex: 1 1 auto; overflow: auto; display: none; }
.pane-set .pane[data-pane-role="artifact"] { flex: 1 1 auto; display: none; flex-direction: column; min-height: 0; }
.pane-set.mode-canvas .pane[data-pane-role="canvas"] { display: block; }
.pane-set.mode-html .pane[data-pane-role="artifact"] { display: flex; }
.pane-set.view-reading .pane[data-pane-role="mindmap"] { display: none; }
.pane-set.view-mindmap .pane[data-pane-role="reading"] { display: none; }
.pane-set.view-mindmap .pane[data-pane-role="mindmap"] { flex: 1 1 auto; border-left: 0; }
.pane-set.mode-canvas .pane[data-pane-role="reading"],
.pane-set.mode-canvas .pane[data-pane-role="mindmap"],
.pane-set.mode-html .pane[data-pane-role="reading"],
.pane-set.mode-html .pane[data-pane-role="mindmap"],
.pane-set.mode-html .pane[data-pane-role="canvas"] { display: none !important; }
.pane-canvas-root { max-width: 1200px; margin: 0 auto; padding: 30px 30px 120px; }
.pane-artifact-frame { flex: 1 1 auto; width: 100%; border: 0; background: #ffffff; }
.pane-artifact-source {
  flex: 1 1 auto; margin: 0; padding: 22px 26px; overflow: auto; white-space: pre-wrap;
  font-family: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace;
  font-size: 12.5px; line-height: 1.6; color: var(--fg); background: var(--bg);
}
.pane-mindmap-svg { display: block; width: 100%; height: 100%; }
#pane-secondary .markdown-body { padding: 24px 30px 120px; }
@media (max-width: 900px) {
  #session-rail { flex-basis: 244px; }
  .pane-bar .doc { display: none; }
  #stage[data-split="h"] { flex-direction: column; }
  #stage[data-split="h"] #pane-secondary { border-left: 0; border-top: 1px solid var(--border-strong); }
  .pane-canvas-root { padding: 20px 16px 110px; }
}
</style>
</head>
<body class="mode-md view-reading">
<header id="topbar">
  <div class="brand"><span class="dot"></span>Visual Reply</div>
  <div class="doc">
    <div id="doc-title" class="doc-title">等待内容</div>
    <div id="doc-sub" class="doc-sub"></div>
  </div>
  <div class="spacer"></div>
  <button type="button" id="rail-toggle" title="显示 / 隐藏会话栏">会话</button>
  <button type="button" id="mode-btn" title="锁定当前会话，或跟随全局最新">跟随全局</button>
  <div class="seg" id="view-switch" role="group" aria-label="视图切换">
    <button type="button" data-view="reading">阅读</button>
    <button type="button" data-view="mindmap">脑图</button>
    <button type="button" data-view="split">分屏</button>
  </div>
  <button type="button" id="back-btn" hidden>回到最新</button>
  <select id="history-select" aria-label="历史记录"><option value="">最新内容</option></select>
  <button type="button" id="theme-btn">主题：浅色</button>
  <span id="status" class="status" data-state="online"><i></i><span>连接中</span></span>
</header>
<div id="new-reply-banner" hidden>
  <span id="new-reply-banner-text">本会话有新回复</span>
  <span class="spacer"></span>
  <button type="button" id="new-reply-jump">切到最新</button>
  <button type="button" id="new-reply-dismiss">忽略</button>
</div>
<div id="shell">
  <aside id="session-rail" data-open="true" aria-label="会话列表">
    <div class="rail-head">
      <span class="rail-title" id="rail-title">会话</span>
      <div class="rail-tabs" id="rail-tabs" role="group" aria-label="会话筛选">
        <button type="button" data-filter="all" data-active="true">全部</button>
        <button type="button" data-filter="unread">未读</button>
        <button type="button" data-filter="pinned">置顶</button>
      </div>
    </div>
    <div class="rail-list" id="rail-list" role="tree" aria-label="项目 · 会话 · 回复"></div>
    <div class="rail-list" id="feed-list" hidden></div>
    <div class="rail-foot">
      <button type="button" id="feed-btn">全局时间线</button>
    </div>
  </aside>
  <main id="stage">
    <section id="pane-primary" class="pane-set mode-md view-reading" data-pane="primary">
      <div class="pane-frame">
        <section id="reading-pane" class="pane" data-pane-role="reading">
          <article id="content" class="markdown-body pane-content" hidden></article>
          <div id="empty" class="empty pane-empty">还没有内容。让 Agent 发布一条回复即可在此看到。</div>
        </section>
        <section id="mindmap-pane" class="pane" data-pane-role="mindmap">
          <svg id="mindmap-svg" class="pane-mindmap-svg"></svg>
          <div id="mindmap-empty" class="empty pane-mindmap-empty" hidden>脑图库未加载</div>
        </section>
        <section id="canvas-pane" class="pane" data-pane-role="canvas">
          <div id="canvas-root" class="pane-canvas-root"></div>
        </section>
        <section id="artifact-pane" class="pane" data-pane-role="artifact" data-sub="full">
          <iframe id="artifact-frame" class="pane-artifact-frame" sandbox="allow-scripts" referrerpolicy="no-referrer" title="HTML Artifact"></iframe>
          <pre id="artifact-source" class="pane-artifact-source" hidden></pre>
        </section>
      </div>
    </section>
    <section id="pane-secondary" class="pane-set mode-md view-reading" data-pane="secondary" hidden>
      <div class="pane-bar">
        <div class="doc">
          <div class="pane-doc-title doc-title">分屏会话</div>
          <div class="pane-doc-sub doc-sub"></div>
        </div>
        <div class="spacer"></div>
        <div class="seg view-switch" role="group" aria-label="分屏视图切换">
          <button type="button" data-view="reading">阅读</button>
          <button type="button" data-view="mindmap">脑图</button>
          <button type="button" data-view="split">分屏</button>
        </div>
        <select class="sec-reply-select" aria-label="分屏回复轮次"><option value="">最新</option></select>
        <select class="sec-session-select" aria-label="分屏会话"></select>
        <button type="button" class="sec-close" title="关闭分屏">关闭分屏</button>
      </div>
      <div class="pane-frame">
        <section class="pane" data-pane-role="reading">
          <article class="markdown-body pane-content" hidden></article>
          <div class="empty pane-empty">分屏会话还没有内容。</div>
        </section>
        <section class="pane" data-pane-role="mindmap">
          <svg class="pane-mindmap-svg"></svg>
          <div class="empty pane-mindmap-empty" hidden>脑图库未加载</div>
        </section>
        <section class="pane" data-pane-role="canvas">
          <div class="pane-canvas-root"></div>
        </section>
        <section class="pane" data-pane-role="artifact" data-sub="full">
          <iframe class="pane-artifact-frame" sandbox="allow-scripts" referrerpolicy="no-referrer" title="HTML Artifact"></iframe>
          <pre class="pane-artifact-source" hidden></pre>
        </section>
      </div>
    </section>
  </main>
</div>
<aside id="drawer" aria-hidden="true" aria-label="详情抽屉">
  <div class="drawer-head">
    <div class="drawer-head-main">
      <div id="drawer-kicker" class="drawer-kicker"></div>
      <div id="drawer-title" class="drawer-title"></div>
      <div id="drawer-tags" class="drawer-tags"></div>
    </div>
    <button type="button" id="drawer-close" class="drawer-close" aria-label="关闭详情">&#215;</button>
  </div>
  <div class="drawer-body">
    <div id="drawer-summary" class="drawer-summary" hidden></div>
    <div id="drawer-details" class="markdown-body"></div>
    <div id="drawer-files" class="drawer-files" hidden>
      <div class="drawer-files-title">相关文件</div>
      <div id="drawer-files-list"></div>
    </div>
  </div>
  <div class="drawer-foot"><span>点击画布元素查看详情</span><span>Esc 关闭</span></div>
</aside>
<div id="toast" hidden></div>
<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/markmap-lib@0.15/dist/browser/index.js"></script>
<script src="https://cdn.jsdelivr.net/npm/markmap-view@0.15/dist/browser/index.js"></script>
<script>
(function () {
  'use strict';

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  var VIEWS = {
    md: [{ id: 'reading', label: '阅读' }, { id: 'mindmap', label: '脑图' }, { id: 'split', label: '分屏' }],
    canvas: [{ id: 'canvas', label: '视觉画布' }, { id: 'raw', label: '原始数据' }],
    html: [{ id: 'full', label: '全屏' }, { id: 'source', label: '源码' }]
  };

  var app = {
    theme: localStorage.getItem('vr.theme') || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'),
    mode: 'global',
    layout: 'h',
    railView: 'sessions',
    filter: 'all',
    sessions: [],
    replies: {},
    globalSeq: 0,
    lastHandledSeq: 0,
    latestSessionId: '',
    latestReplyId: '',
    lastSeen: {},
    collapsedGroups: {},
    expandedSessions: {},
    loadingReplies: {},
    pendingBanner: null,
    sse: null,
    polling: false
  };
  function readStoredMap(key) {
    try {
      var value = JSON.parse(localStorage.getItem(key) || '{}');
      return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    } catch (err) {
      return {};
    }
  }
  app.lastSeen = readStoredMap('vr.lastSeen');
  app.collapsedGroups = readStoredMap('vr.collapsedGroups');
  app.expandedSessions = readStoredMap('vr.expandedSessions');

  var drawer = $('#drawer');
  var mmdSeq = 0;

  /* ---------- 基础工具 ---------- */

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var escapeAttr = escapeHtml;

  function toText(value) {
    if (value === null || value === undefined) return '';
    if (typeof value === 'string') return value;
    try { return '```json\n' + JSON.stringify(value, null, 2) + '\n```'; }
    catch (err) { return String(value); }
  }

  function clock() {
    var d = new Date();
    var p = function (n) { return n < 10 ? '0' + n : '' + n; };
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function fmtTime(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    var p = function (n) { return n < 10 ? '0' + n : '' + n; };
    return (d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function toast(message) {
    var box = $('#toast');
    box.innerHTML = '';
    var span = document.createElement('span');
    span.textContent = message;
    box.appendChild(span);
    box.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { box.hidden = true; }, 2600);
  }

  /* 跨会话新回复：带一个「查看」按钮，不打断当前阅读 */
  function toastAction(message, actionLabel, onAction) {
    var box = $('#toast');
    box.innerHTML = '';
    var span = document.createElement('span');
    span.textContent = message;
    box.appendChild(span);
    if (actionLabel) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'toast-action';
      btn.textContent = actionLabel;
      btn.addEventListener('click', function () {
        box.hidden = true;
        onAction();
      });
      box.appendChild(btn);
    }
    box.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { box.hidden = true; }, 8000);
  }

  function setStatus(mode, text) {
    var el = $('#status');
    el.dataset.state = mode;
    el.lastElementChild.textContent = text;
  }

  function fetchJSON(url) {
    return fetch(url, { cache: 'no-store' }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    });
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    });
  }

  function renderMarkdown(text) {
    if (!text) return '';
    if (!window.marked) return '<pre>' + escapeHtml(text) + '</pre>';
    try { return marked.parse(String(text), { gfm: true, breaks: false }); }
    catch (err) { return '<pre>' + escapeHtml(text) + '</pre>'; }
  }

  function setTheme(theme, rerender) {
    app.theme = theme;
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('vr.theme', theme);
    $('#theme-btn').textContent = theme === 'dark' ? '主题：深色' : '主题：浅色';
    if (window.mermaid) {
      try {
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: theme === 'dark' ? 'dark' : 'default',
          flowchart: { curve: 'basis', htmlLabels: true },
          sequence: { mirrorActors: false }
        });
      } catch (err) { /* 初始化失败时保持默认配置 */ }
    }
    if (rerender) {
      [panes.primary, panes.secondary].forEach(function (pane) {
        var v = pane.viewer;
        if (v.mode !== 'md' || !v.content) return;
        renderReading(pane, v.content);
        if (v.mm && typeof v.mm.destroy === 'function') {
          try { v.mm.destroy(); } catch (err) { /* ignore */ }
          v.mm = null;
        }
        renderMindmap(pane, v.content);
      });
    }
  }

  /* ---------- 双窗格 ---------- */

  function createPane(root, name) {
    var pane = {
      name: name,
      el: root,
      content: $('.pane-content', root),
      empty: $('.pane-empty', root),
      mindmapPane: $('[data-pane-role="mindmap"]', root),
      mindmapSvg: $('[data-pane-role="mindmap"] svg', root),
      mindmapEmpty: $('.pane-mindmap-empty', root),
      canvasPane: $('[data-pane-role="canvas"]', root),
      canvasRoot: $('.pane-canvas-root', root),
      artifactPane: $('[data-pane-role="artifact"]', root),
      artifactFrame: $('.pane-artifact-frame', root),
      artifactSource: $('.pane-artifact-source', root),
      sessionId: '',
      replyId: '',
      viewingHistory: false,
      viewer: {
        mode: 'md', subview: 'reading', format: 'md', title: '', content: '',
        currentFile: null, canvasData: null, artifactSrc: '', mtime: 0, mm: null,
        drawer: {}, drawerSeq: 0, seq: 0,
        tables: {}, tableSeq: 0, charts: {}, chartSeq: 0
      }
    };
    if (name === 'primary') {
      pane.docTitle = $('#doc-title');
      pane.docSub = $('#doc-sub');
      pane.viewSwitch = $('#view-switch');
    } else {
      pane.docTitle = $('.pane-doc-title', root);
      pane.docSub = $('.pane-doc-sub', root);
      pane.viewSwitch = $('.view-switch', root);
    }
    return pane;
  }

  var panes = {
    primary: createPane($('#pane-primary'), 'primary'),
    secondary: createPane($('#pane-secondary'), 'secondary')
  };

  function rebuildViewSwitch(pane) {
    var box = pane.viewSwitch;
    if (!box) return;
    box.innerHTML = '';
    VIEWS[pane.viewer.mode].forEach(function (v) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.dataset.view = v.id;
      btn.textContent = v.label;
      btn.addEventListener('click', function () {
        pane.viewer.subview = v.id;
        applySubview(pane);
      });
      box.appendChild(btn);
    });
  }

  function setMode(pane, mode) {
    var v = pane.viewer;
    var changed = v.mode !== mode;
    v.mode = mode;
    pane.el.classList.remove('mode-md', 'mode-canvas', 'mode-html');
    pane.el.classList.add('mode-' + mode);
    if (pane.name === 'primary') {
      document.body.classList.remove('mode-md', 'mode-canvas', 'mode-html');
      document.body.classList.add('mode-' + mode);
    }
    var ids = VIEWS[mode].map(function (x) { return x.id; });
    if (changed) {
      var saved = mode === 'md' ? localStorage.getItem('vr.sub.md') : null;
      v.subview = saved && ids.indexOf(saved) >= 0 ? saved : ids[0];
      closeDrawer();
    } else if (ids.indexOf(v.subview) < 0) {
      v.subview = ids[0];
    }
    rebuildViewSwitch(pane);
  }

  function applySubview(pane) {
    var v = pane.viewer;
    if (v.mode === 'md' && pane.name === 'primary') closeDrawer();
    $$('button', pane.viewSwitch).forEach(function (btn) {
      btn.dataset.active = String(btn.dataset.view === v.subview);
    });
    if (pane.name === 'primary' && v.mode === 'md') {
      localStorage.setItem('vr.sub.md', v.subview);
    }
    pane.el.classList.remove('view-reading', 'view-mindmap', 'view-split');
    if (v.mode === 'md') {
      var sub = ['reading', 'mindmap', 'split'].indexOf(v.subview) >= 0 ? v.subview : 'reading';
      pane.el.classList.add('view-' + sub);
      if (pane.name === 'primary') {
        ['reading', 'mindmap', 'split'].forEach(function (name) {
          document.body.classList.toggle('view-' + name, name === sub);
        });
      }
      if (sub !== 'reading') setTimeout(function () { fitMindmap(pane); }, 60);
      return;
    }
    pane.el.classList.add('view-reading');
    if (v.mode === 'canvas') {
      renderCanvas(pane);
      return;
    }
    pane.artifactPane.dataset.sub = v.subview;
    pane.artifactFrame.hidden = v.subview === 'source';
    pane.artifactSource.hidden = v.subview !== 'source';
    syncArtifact(pane);
  }

  /* ---------- Markdown 阅读 / 脑图（兼容模式） ---------- */

  function extractMermaid(md) {
    var blocks = [];
    var text = md.replace(/^[ \t]*```mermaid[ \t]*\r?\n([\s\S]*?)^[ \t]*```[ \t]*$/gm, function (m, code) {
      blocks.push(code);
      return '\n\n@@MM_BLOCK_' + (blocks.length - 1) + '@@\n\n';
    });
    return { text: text, blocks: blocks };
  }

  function injectMermaid(html, blocks) {
    var make = function (code) {
      return '<div class="mermaid-block" data-code="' + encodeURIComponent(code) +
        '"><span class="mermaid-hint">图表渲染中</span></div>';
    };
    html = html.replace(/<p>\s*@@MM_BLOCK_(\d+)@@\s*<\/p>/g, function (m, i) {
      return make(blocks[parseInt(i, 10)] || '');
    });
    html = html.replace(/@@MM_BLOCK_(\d+)@@/g, function (m, i) {
      return make(blocks[parseInt(i, 10)] || '');
    });
    return html;
  }

  function buildSections(root) {
    var nodes = Array.prototype.slice.call(root.childNodes);
    var stack = [{ level: 0, body: root }];
    nodes.forEach(function (node) {
      var isHeading = node.nodeType === 1 && /^H[1-6]$/.test(node.tagName);
      if (!isHeading) {
        stack[stack.length - 1].body.appendChild(node);
        return;
      }
      var level = parseInt(node.tagName.slice(1), 10);
      while (stack.length > 1 && stack[stack.length - 1].level >= level) stack.pop();
      var parent = stack[stack.length - 1].body;
      var sec = document.createElement('section');
      sec.className = 'sec';
      var body = document.createElement('div');
      body.className = 'sec-body';
      parent.appendChild(sec);
      sec.appendChild(node);
      sec.appendChild(body);
      node.title = '点击折叠 / 展开';
      node.addEventListener('click', function () {
        var sel = window.getSelection && String(window.getSelection());
        if (sel) return;
        sec.classList.toggle('collapsed');
      });
      stack.push({ level: level, body: body });
    });
  }

  function wrapTables(root) {
    $$('table', root).forEach(function (table) {
      if (table.classList.contains('exp-tbl')) return;
      if (table.parentElement && (table.parentElement.classList.contains('table-wrap') ||
          table.parentElement.classList.contains('tbl-scroll'))) return;
      var wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
    });
  }

  function markExternalLinks(root) {
    $$('a[href]', root).forEach(function (a) {
      var href = a.getAttribute('href') || '';
      if (/^https?:\/\//i.test(href)) {
        a.target = '_blank';
        a.rel = 'noreferrer noopener';
      }
    });
  }

  function renderReading(pane, md) {
    if (!window.marked) {
      pane.content.innerHTML = '<pre>' + escapeHtml(md) + '</pre>';
      return;
    }
    var extracted = extractMermaid(md);
    var html = marked.parse(extracted.text, { gfm: true, breaks: false });
    pane.content.innerHTML = injectMermaid(html, extracted.blocks);
    buildSections(pane.content);
    wrapTables(pane.content);
    markExternalLinks(pane.content);
    renderDiagrams(pane);
  }

  function renderDiagrams(pane) {
    var nodes = $$('.mermaid-block', pane.content);
    if (!nodes.length) return;
    if (!window.mermaid) {
      nodes.forEach(function (node) {
        node.innerHTML = '<pre class="diagram-error">图表库未加载，需要网络访问 CDN。</pre>';
      });
      return;
    }
    nodes.reduce(function (chain, node) {
      return chain.then(function () {
        var code = decodeURIComponent(node.dataset.code || '');
        if (!code.trim()) return;
        var id = 'mmd-' + (++mmdSeq);
        return mermaid.render(id, code).then(function (result) {
          node.innerHTML = result.svg;
          node.classList.add('rendered');
          if (result.bindFunctions) result.bindFunctions(node);
        }).catch(function (err) {
          node.innerHTML = '<pre class="diagram-error">' + escapeHtml(String(err && err.message || err)) + '</pre>';
          node.classList.add('failed');
        });
      });
    }, Promise.resolve());
  }

  function renderMindmap(pane, md) {
    var lib = window.markmap;
    var fallback = pane.mindmapEmpty;
    if (!lib || !lib.Transformer || !lib.Markmap) {
      fallback.hidden = false;
      fallback.textContent = '脑图库未加载，需要网络访问 CDN。';
      return;
    }
    fallback.hidden = true;
    var clean = md.replace(/^[ \t]*```mermaid[ \t]*\r?\n[\s\S]*?^[ \t]*```[ \t]*$/gm, '');
    var root;
    try {
      root = new lib.Transformer().transform(clean).root;
    } catch (err) {
      fallback.hidden = false;
      fallback.textContent = '无法生成脑图：' + (err && err.message ? err.message : err);
      return;
    }
    try {
      if (!pane.viewer.mm) {
        pane.viewer.mm = lib.Markmap.create(pane.mindmapSvg, {
          duration: 220,
          maxWidth: 300,
          spacingVertical: 8,
          paddingX: 12,
          colorFreezeLevel: 2,
          initialExpandLevel: -1
        }, root);
      } else {
        pane.viewer.mm.setData(root);
      }
      fitMindmap(pane);
    } catch (err) {
      fallback.hidden = false;
      fallback.textContent = '脑图渲染失败：' + (err && err.message ? err.message : err);
    }
  }

  function fitMindmap(pane) {
    if (!pane.viewer.mm) return;
    var box = pane.mindmapPane;
    if (!box || box.clientWidth < 60 || box.clientHeight < 60) return;
    requestAnimationFrame(function () {
      try { pane.viewer.mm.fit(); } catch (err) { /* ignore */ }
    });
  }

  /* ---------- HTML Artifact ---------- */

  function artifactUrl(pane) {
    var v = pane.viewer;
    if (!v.content || !v.content.trim()) return 'about:blank';
    if (v.sessionId && v.replyId) {
      return '/artifact/session/' + encodeURIComponent(v.sessionId) + '/' + encodeURIComponent(v.replyId);
    }
    if (v.currentFile) return '/artifact/history/' + encodeURIComponent(v.currentFile);
    return '/artifact/latest?_=' + (v.mtime || 0);
  }

  function syncArtifact(pane) {
    var url = artifactUrl(pane);
    if (url === pane.viewer.artifactSrc) return;
    pane.viewer.artifactSrc = url;
    pane.artifactFrame.src = url;
    var source = pane.artifactSource;
    if (url === 'about:blank') { source.textContent = '还没有内容。'; return; }
    fetch(url, { cache: 'no-store' }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.text();
    }).then(function (text) {
      source.textContent = text;
    }).catch(function (err) {
      source.textContent = '源码加载失败：' + err.message;
    });
  }

  /* ---------- Inspector 抽屉（两个窗格共用） ---------- */

  function registerDrawer(pane, payload) {
    var key = 'd' + (++pane.viewer.drawerSeq);
    pane.viewer.drawer[key] = payload;
    return key;
  }

  function openDrawer(payload) {
    if (!payload) return;
    $('#drawer-kicker').textContent = payload.kicker || '详情';
    $('#drawer-title').textContent = payload.title || '';
    var tags = $('#drawer-tags');
    tags.innerHTML = '';
    (payload.tags || []).filter(Boolean).forEach(function (tag) {
      var span = document.createElement('span');
      span.className = 'badge';
      span.textContent = tag;
      tags.appendChild(span);
    });
    var summary = $('#drawer-summary');
    if (payload.summary) {
      summary.hidden = false;
      summary.textContent = payload.summary;
    } else {
      summary.hidden = true;
      summary.textContent = '';
    }
    var details = toText(payload.details);
    $('#drawer-details').innerHTML = details ? renderMarkdown(details) : '<p style="color:var(--fg-muted)">没有更多细节。</p>';
    $$('#drawer-details a[href]').forEach(function (a) {
      if (/^https?:\/\//i.test(a.getAttribute('href') || '')) {
        a.target = '_blank';
        a.rel = 'noreferrer noopener';
      }
    });
    var files = Array.isArray(payload.files) ? payload.files.filter(Boolean) : [];
    var filesBox = $('#drawer-files');
    var list = $('#drawer-files-list');
    list.innerHTML = files.map(function (f) {
      return '<span class="file-chip">' + escapeHtml(f) + '</span>';
    }).join('');
    filesBox.hidden = !files.length;
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    document.body.classList.add('drawer-open');
    $('.drawer-body', drawer).scrollTop = 0;
  }

  function closeDrawer() {
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('drawer-open');
  }

  function drawerOpen() {
    return drawer.classList.contains('open');
  }
  /* ---------- Visual Canvas ---------- */

  function normalizeCanvas(data) {
    var out = {
      headline: data.headline || data.title || '',
      badges: Array.isArray(data.badges) ? data.badges : [],
      sections: Array.isArray(data.sections) ? data.sections.slice() : []
    };
    if (!out.sections.length) {
      if (Array.isArray(data.nodes)) out.sections.push({ type: 'graph', nodes: data.nodes, edges: data.edges || [] });
      if (Array.isArray(data.cards)) out.sections.push({ type: 'bento', cards: data.cards });
      if (Array.isArray(data.steps)) out.sections.push({ type: 'steps', steps: data.steps });
      if (Array.isArray(data.kpis)) out.sections.push({ type: 'kpis', items: data.kpis });
      if (Array.isArray(data.columns) && Array.isArray(data.rows)) {
        out.sections.push({
          type: 'table', title: data.section_title || '',
          columns: data.columns, rows: data.rows, groups: data.groups,
          baseline_id: data.baseline_id, show_delta: data.show_delta,
          searchable: data.searchable, caption: data.caption, note: data.note,
          span: data.span
        });
      }
      if (data.chart_type || ((Array.isArray(data.matrix) || Array.isArray(data.cells))
          && Array.isArray(data.x_labels))) {
        var chart = {};
        Object.keys(data).forEach(function (k) { chart[k] = data[k]; });
        chart.type = 'chart';
        chart.title = data.section_title || '';
        chart.span = data.span;
        out.sections.push(chart);
      }
      if (typeof data.prose === 'string') {
        out.sections.push({ type: 'prose', title: data.section_title || '', content: data.prose, tone: data.tone, span: data.span });
      }
    }
    return out;
  }

  function renderKpis(sec) {
    var items = Array.isArray(sec.items) ? sec.items : [];
    if (!items.length) return '';
    return '<div class="kpi-grid">' + items.map(function (it) {
      var status = it.status || 'info';
      return '<div class="kpi" data-status="' + escapeAttr(status) + '">' +
        '<span class="kpi-dot"></span>' +
        '<div class="kpi-value">' + escapeHtml(it.value === undefined ? '' : it.value) + '</div>' +
        '<div class="kpi-label">' + escapeHtml(it.label || '') + '</div>' +
        (it.note ? '<div class="kpi-note">' + escapeHtml(it.note) + '</div>' : '') +
        '</div>';
    }).join('') + '</div>';
  }

/* ==== VR_TABLE_CORE_START ==== */
  /* 纯表格计算，不依赖 DOM；test_visual_reply.py 会把这段抽出来在 node vm 里跑。 */
  var TableCore = (function () {
    'use strict';

    var EPS = 1e-9;

    function finite(v) {
      if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null;
      var n = Number(v);
      return isFinite(n) ? n : null;
    }

    function parseCell(raw) {
      var out = { raw: raw, num: null, text: '', note: '', status: '', hasDisplay: false };
      if (raw === null || raw === undefined) return out;
      if (typeof raw === 'object') {
        var value = raw.value;
        out.note = raw.note === null || raw.note === undefined ? '' : String(raw.note);
        out.status = raw.status === null || raw.status === undefined ? '' : String(raw.status);
        if (typeof raw.display === 'string') {
          out.hasDisplay = true;
          out.text = raw.display;
        } else {
          out.text = value === null || value === undefined ? '' : String(value);
        }
        out.num = finite(value);
        return out;
      }
      out.text = String(raw);
      out.num = finite(raw);
      return out;
    }

    function getCell(row, key) {
      if (!row || typeof row !== 'object' || !key) return parseCell(undefined);
      var raw;
      if (row.cells && typeof row.cells === 'object'
          && Object.prototype.hasOwnProperty.call(row.cells, key)) {
        raw = row.cells[key];
      } else if (Object.prototype.hasOwnProperty.call(row, key)) {
        raw = row[key];
      }
      return parseCell(raw);
    }

    function isBlankCell(cell) {
      if (!cell || cell.num !== null) return false;
      var text = String(cell.text || '').trim().toLowerCase();
      return text === '' || text === '-' || text === '--' || text === '\u2014'
        || text === '\u2013' || text === 'n/a' || text === 'na' || text === 'null';
    }

    function effectiveHighlight(col) {
      if (!col || typeof col !== 'object') return 'none';
      if (col.highlight === 'max' || col.highlight === 'min' || col.highlight === 'none') {
        return col.highlight;
      }
      if (col.higher_is_better === true) return 'max';
      if (col.higher_is_better === false) return 'min';
      return 'none';
    }

    function rankColumns(columns, rows) {
      var out = {};
      (Array.isArray(columns) ? columns : []).forEach(function (col) {
        if (!col || !col.key) return;
        var values = [];
        (Array.isArray(rows) ? rows : []).forEach(function (row) {
          var num = getCell(row, col.key).num;
          if (num === null) return;
          for (var i = 0; i < values.length; i++) {
            if (Math.abs(values[i] - num) < EPS) return;
          }
          values.push(num);
        });
        var mode = effectiveHighlight(col);
        if (!values.length || mode === 'none') {
          out[col.key] = { best: null, second: null };
          return;
        }
        values.sort(function (a, b) { return mode === 'max' ? b - a : a - b; });
        out[col.key] = { best: values[0], second: values.length > 1 ? values[1] : null };
      });
      return out;
    }

    function inferDecimals(col, values) {
      if (col && typeof col.decimals === 'number' && isFinite(col.decimals) && col.decimals >= 0) {
        return Math.max(0, Math.min(4, Math.round(col.decimals)));
      }
      var fractional = false;
      (Array.isArray(values) ? values : []).forEach(function (raw) {
        var num = finite(raw);
        if (num === null) return;
        if (Math.abs(num - Math.round(num)) > EPS) fractional = true;
      });
      return fractional ? 1 : 0;
    }

    function formatNumber(value, decimals) {
      var num = finite(value);
      if (num === null) return '';
      var d = finite(decimals);
      d = d === null ? 2 : Math.max(0, Math.min(4, Math.round(d)));
      var text = num.toFixed(d);
      if (/^-0(\.0+)?$/.test(text)) text = text.slice(1);
      return text;
    }

    function computeDelta(num, baseNum, col, decimals) {
      var cur = finite(num);
      var base = finite(baseNum);
      if (cur === null || base === null) return null;
      var diff = cur - base;
      var d = finite(decimals);
      d = d === null ? 2 : Math.max(0, Math.min(4, Math.round(d)));
      var body = Math.abs(diff).toFixed(d);
      if (body.indexOf('.') >= 0) body = body.replace(/0+$/, '').replace(/\.$/, '');
      var sign = diff > EPS ? '+' : (diff < -EPS ? '-' : '');
      var unit = col && col.delta_unit ? String(col.delta_unit) : '';
      var tone = 'zero';
      if (Math.abs(diff) >= EPS) {
        var higherBetter = !(col && col.higher_is_better === false);
        tone = (diff > 0) === higherBetter ? 'pos' : 'neg';
      }
      return { diff: diff, text: sign + body + unit, tone: tone };
    }

    function sortRows(rows, colKey, dir, pinnedTopIds) {
      var pinned = {};
      (Array.isArray(pinnedTopIds) ? pinnedTopIds : []).forEach(function (id) {
        pinned[String(id)] = true;
      });
      var sign = dir === 'asc' ? 1 : -1;
      var decorated = (Array.isArray(rows) ? rows : []).map(function (row, idx) {
        return { row: row, idx: idx };
      });
      decorated.sort(function (a, b) {
        var pinA = pinned[String(a.row && a.row.id)] ? 1 : 0;
        var pinB = pinned[String(b.row && b.row.id)] ? 1 : 0;
        if (pinA !== pinB) return pinB - pinA;
        var cellA = getCell(a.row, colKey);
        var cellB = getCell(b.row, colKey);
        var blankA = isBlankCell(cellA);
        var blankB = isBlankCell(cellB);
        if (blankA !== blankB) return blankA ? 1 : -1;
        if (blankA && blankB) return a.idx - b.idx;
        if (cellA.num !== null && cellB.num !== null) {
          if (Math.abs(cellA.num - cellB.num) > EPS) return sign * (cellA.num - cellB.num);
          return a.idx - b.idx;
        }
        if (cellA.num !== null) return -1;
        if (cellB.num !== null) return 1;
        var cmp = String(cellA.text).localeCompare(String(cellB.text));
        return cmp !== 0 ? sign * cmp : a.idx - b.idx;
      });
      return decorated.map(function (item) { return item.row; });
    }

    function filterRows(rows, columns, query) {
      var needle = String(query === null || query === undefined ? '' : query).trim().toLowerCase();
      var list = Array.isArray(rows) ? rows : [];
      if (!needle) return list.slice();
      var cols = (Array.isArray(columns) ? columns : []).filter(function (c) { return c && c.key; });
      return list.filter(function (row) {
        var parts = [];
        if (cols.length) {
          cols.forEach(function (col) { parts.push(getCell(row, col.key).text); });
        } else {
          Object.keys(row || {}).forEach(function (key) {
            if (key === 'details' || key === 'files' || key === 'cells') return;
            parts.push(parseCell(row[key]).text);
          });
        }
        return parts.join(' ').toLowerCase().indexOf(needle) >= 0;
      });
    }

    function normalizeRows(rows) {
      return (Array.isArray(rows) ? rows : []).map(function (row, idx) {
        var copy = {};
        Object.keys(row || {}).forEach(function (key) { copy[key] = row[key]; });
        var id = row && row.id !== undefined && row.id !== null ? String(row.id) : '';
        copy.id = id || 'r' + idx;
        return copy;
      });
    }

    function resolveBaselineId(rows, explicitId) {
      var list = Array.isArray(rows) ? rows : [];
      var target = explicitId === null || explicitId === undefined ? '' : String(explicitId);
      if (target) {
        for (var i = 0; i < list.length; i++) {
          if (String((list[i] && list[i].id) || '') === target) return target;
        }
      }
      for (var j = 0; j < list.length; j++) {
        if (list[j] && list[j].is_baseline) return String(list[j].id || '');
      }
      return '';
    }

    function normalizeGroups(columns, groups) {
      var cols = Array.isArray(columns) ? columns : [];
      if (!Array.isArray(groups) || !groups.length) return [];
      var indexOf = {};
      cols.forEach(function (col, i) { indexOf[col.key] = i; });
      var out = [];
      var cursor = 0;
      groups.forEach(function (group) {
        if (!group) return;
        var start = cursor;
        var span = 0;
        if (Array.isArray(group.cols) && group.cols.length) {
          var indexes = [];
          group.cols.forEach(function (key) {
            if (Object.prototype.hasOwnProperty.call(indexOf, key)) indexes.push(indexOf[key]);
          });
          if (!indexes.length) return;
          start = Math.min.apply(null, indexes);
          var end = Math.max.apply(null, indexes);
          span = end - start + 1;
          var expected = cols.slice(start, end + 1).map(function (col) { return col.key; });
          if (expected.length !== group.cols.length) return;
          for (var k = 0; k < expected.length; k++) {
            if (expected[k] !== group.cols[k]) return;
          }
        } else {
          span = Math.round(Number(group.span) || 0);
          start = cursor;
        }
        if (span <= 0 || start + span > cols.length) return;
        out.push({ label: String(group.label || ''), span: span, start: start });
        cursor = start + span;
      });
      out.sort(function (a, b) { return a.start - b.start; });
      var prevEnd = 0;
      for (var i = 0; i < out.length; i++) {
        if (out[i].start < prevEnd) return [];
        prevEnd = out[i].start + out[i].span;
      }
      return out;
    }

    function escapeLatex(text) {
      var sentinel = '@@VRBS@@';
      var value = text === null || text === undefined ? '' : String(text);
      return value
        .replace(/\\/g, sentinel)
        .replace(/&/g, '\\&')
        .replace(/%/g, '\\%')
        .replace(/\$/g, '\\$')
        .replace(/#/g, '\\#')
        .replace(/_/g, '\\_')
        .replace(/\{/g, '\\{')
        .replace(/\}/g, '\\}')
        .replace(/~/g, '\\textasciitilde{}')
        .replace(/\^/g, '\\textasciicircum{}')
        .replace(new RegExp(sentinel, 'g'), '\\textbackslash{}');
    }

    function alignOf(col, rows) {
      if (col.align === 'left' || col.align === 'center' || col.align === 'right') return col.align;
      var numeric = (Array.isArray(rows) ? rows : []).some(function (row) {
        return getCell(row, col.key).num !== null;
      });
      return numeric ? 'right' : 'left';
    }

    function headerLabel(col) {
      var label = col.label === undefined || col.label === null ? String(col.key) : String(col.label);
      return col.unit ? label + ' (' + col.unit + ')' : label;
    }

    function cellExportText(cell, col, decimals) {
      if (cell.hasDisplay) return cell.text;
      if (cell.num !== null) return formatNumber(cell.num, decimals);
      return cell.text;
    }

    function toLatex(columns, rows, groups, opts) {
      var cols = (Array.isArray(columns) ? columns : []).filter(function (c) { return c && c.key; });
      if (!cols.length) return '';
      var list = Array.isArray(rows) ? rows : [];
      var options = opts || {};
      var ranks = rankColumns(cols, list);
      var layout = cols.map(function (col) { return alignOf(col, list); })
        .map(function (a) { return a === 'right' ? 'r' : (a === 'center' ? 'c' : 'l'); }).join('');
      var decimals = {};
      cols.forEach(function (col) {
        decimals[col.key] = inferDecimals(col, list.map(function (row) {
          return getCell(row, col.key).num;
        }));
      });
      var lines = [];
      if (options.caption) {
        lines.push('\\begin{table}[htbp]');
        lines.push('\\centering');
      }
      lines.push('\\begin{tabular}{' + layout + '}');
      lines.push('\\toprule');
      var blocks = normalizeGroups(cols, groups);
      if (blocks.length) {
        var groupCells = [];
        var rules = [];
        var cursor = 0;
        blocks.forEach(function (block) {
          while (cursor < block.start) { groupCells.push(''); cursor += 1; }
          groupCells.push('\\multicolumn{' + block.span + '}{c}{' + escapeLatex(block.label) + '}');
          rules.push('\\cmidrule(lr){' + (block.start + 1) + '-' + (block.start + block.span) + '}');
          cursor = block.start + block.span;
        });
        while (cursor < cols.length) { groupCells.push(''); cursor += 1; }
        lines.push(groupCells.join(' & ') + ' \\\\');
        lines.push(rules.join(' '));
      }
      lines.push(cols.map(function (col) {
        return escapeLatex(col.label === undefined || col.label === null ? col.key : col.label);
      }).join(' & ') + ' \\\\');
      lines.push('\\midrule');
      list.forEach(function (row, rowIdx) {
        if (rowIdx > 0 && row && row.divider_before) lines.push('\\midrule');
        if (rowIdx === list.length - 1 && list.length > 1 && row && row.emphasis && !row.divider_before) {
          lines.push('\\midrule');
        }
        var cells = cols.map(function (col) {
          var cell = getCell(row, col.key);
          var text;
          if (cell.num !== null && !cell.hasDisplay) {
            text = formatNumber(cell.num, decimals[col.key]);
            var rank = ranks[col.key];
            if (rank && rank.best !== null && Math.abs(cell.num - rank.best) < EPS) {
              text = '\\textbf{' + text + '}';
            } else if (rank && rank.second !== null && Math.abs(cell.num - rank.second) < EPS) {
              text = '\\underline{' + text + '}';
            }
          } else {
            text = escapeLatex(cell.text);
          }
          return text;
        });
        lines.push(cells.join(' & ') + ' \\\\');
      });
      lines.push('\\bottomrule');
      lines.push('\\end{tabular}');
      if (options.caption) {
        lines.push('\\caption{' + escapeLatex(options.caption) + '}');
        lines.push('\\end{table}');
      }
      return lines.join('\n');
    }

    function toMarkdown(columns, rows, opts) {
      var cols = (Array.isArray(columns) ? columns : []).filter(function (c) { return c && c.key; });
      if (!cols.length) return '';
      var list = Array.isArray(rows) ? rows : [];
      var ranks = rankColumns(cols, list);
      var decimals = {};
      cols.forEach(function (col) {
        decimals[col.key] = inferDecimals(col, list.map(function (row) {
          return getCell(row, col.key).num;
        }));
      });
      function mdText(text) {
        return String(text === null || text === undefined ? '' : text)
          .replace(/\|/g, '\\|').replace(/\r?\n/g, ' ');
      }
      var head = '| ' + cols.map(function (col) { return mdText(headerLabel(col)); }).join(' | ') + ' |';
      var sep = '| ' + cols.map(function (col) {
        var align = alignOf(col, list);
        return align === 'center' ? ':---:' : (align === 'right' ? '---:' : ':---');
      }).join(' | ') + ' |';
      var body = list.map(function (row) {
        return '| ' + cols.map(function (col) {
          var cell = getCell(row, col.key);
          var text = mdText(cellExportText(cell, col, decimals[col.key]));
          if (cell.num !== null && !cell.hasDisplay) {
            var rank = ranks[col.key];
            if (rank && rank.best !== null && Math.abs(cell.num - rank.best) < EPS) text = '**' + text + '**';
            else if (rank && rank.second !== null && Math.abs(cell.num - rank.second) < EPS) text = '__' + text + '__';
          }
          return text;
        }).join(' | ') + ' |';
      });
      return [head, sep].concat(body).join('\n');
    }

    function csvCell(value) {
      var text = value === null || value === undefined ? '' : String(value);
      if (/[",\r\n]/.test(text)) return '"' + text.replace(/"/g, '""') + '"';
      return text;
    }

    function toCsv(columns, rows) {
      var cols = (Array.isArray(columns) ? columns : []).filter(function (c) { return c && c.key; });
      if (!cols.length) return '';
      var list = Array.isArray(rows) ? rows : [];
      var decimals = {};
      cols.forEach(function (col) {
        decimals[col.key] = inferDecimals(col, list.map(function (row) {
          return getCell(row, col.key).num;
        }));
      });
      var lines = [cols.map(function (col) { return csvCell(headerLabel(col)); }).join(',')];
      list.forEach(function (row) {
        lines.push(cols.map(function (col) {
          var cell = getCell(row, col.key);
          return csvCell(cellExportText(cell, col, decimals[col.key]));
        }).join(','));
      });
      return lines.join('\r\n');
    }

    return {
      finite: finite,
      parseCell: parseCell,
      getCell: getCell,
      isBlankCell: isBlankCell,
      effectiveHighlight: effectiveHighlight,
      rankColumns: rankColumns,
      inferDecimals: inferDecimals,
      formatNumber: formatNumber,
      computeDelta: computeDelta,
      sortRows: sortRows,
      filterRows: filterRows,
      normalizeRows: normalizeRows,
      resolveBaselineId: resolveBaselineId,
      normalizeGroups: normalizeGroups,
      alignOf: alignOf,
      headerLabel: headerLabel,
      cellExportText: cellExportText,
      escapeLatex: escapeLatex,
      toLatex: toLatex,
      toMarkdown: toMarkdown,
      toCsv: toCsv
    };
  })();
  (typeof globalThis !== 'undefined' ? globalThis : this).TableCore = TableCore;
  /* ==== VR_TABLE_CORE_END ==== */
/* ==== VR_CHART_CORE_START ==== */
  /* 纯图表几何，不依赖 DOM 或任何第三方库；test_visual_reply.py 会抽出来在 node vm 里跑。 */
  var ChartCore = (function () {
    'use strict';

    var EPS = 1e-9;
    var PALETTE = 6;

    function finite(v) {
      if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null;
      var n = Number(v);
      return isFinite(n) ? n : null;
    }

    function clamp(v, lo, hi) {
      if (!isFinite(v)) return lo;
      return v < lo ? lo : (v > hi ? hi : v);
    }

    function r2(v) {
      if (!isFinite(v)) return 0;
      return Math.round(v * 100) / 100;
    }

    function colorOf(index) {
      return 'var(--vc-' + (Math.abs(Math.round(index)) % PALETTE) + ')';
    }

    function seriesKey(series, index) {
      if (series && series.name !== undefined && series.name !== null && String(series.name) !== '') {
        return String(series.name);
      }
      return 'S' + (index + 1);
    }

    function decimalsForStep(step) {
      var s = Math.abs(finite(step) === null ? 1 : step);
      if (!(s > 0)) return 0;
      var d = 0;
      while (d < 4 && Math.abs(s - Math.round(s)) > 1e-9) { s *= 10; d += 1; }
      return d;
    }

    function formatNumber(value, decimals) {
      var num = finite(value);
      if (num === null) return '';
      var d = finite(decimals);
      d = d === null ? 2 : Math.max(0, Math.min(4, Math.round(d)));
      var text = num.toFixed(d);
      if (/^-0(\.0+)?$/.test(text)) text = text.slice(1);
      return text;
    }

    function niceNum(range, round) {
      if (!(range > 0) || !isFinite(range)) return 1;
      var exp = Math.floor(Math.log(range) / Math.LN10);
      var frac = range / Math.pow(10, exp);
      var nice;
      if (round) nice = frac < 1.5 ? 1 : (frac < 3 ? 2 : (frac < 7 ? 5 : 10));
      else nice = frac <= 1 ? 1 : (frac <= 2 ? 2 : (frac <= 2.5 ? 2.5 : (frac <= 5 ? 5 : 10)));
      var out = nice * Math.pow(10, exp);
      return isFinite(out) && out > 0 ? out : 1;
    }

    function niceScale(rawMin, rawMax, targetTicks, includeZero) {
      var lo = finite(rawMin);
      var hi = finite(rawMax);
      if (lo === null) lo = 0;
      if (hi === null) hi = 0;
      if (includeZero) {
        lo = Math.min(lo, 0);
        hi = Math.max(hi, 0);
      }
      if (!(hi > lo)) {
        var base = Math.abs(lo) > EPS ? Math.abs(lo) : 1;
        if (Math.abs(hi - lo) < EPS) {
          lo = lo - base * 0.5;
          hi = hi + base * 0.5;
        } else {
          var swap = lo;
          lo = hi;
          hi = swap;
        }
        if (includeZero) {
          lo = Math.min(lo, 0);
          hi = Math.max(hi, 0);
        }
        if (!(hi > lo)) hi = lo + 1;
      }
      var want = finite(targetTicks);
      want = want === null ? 5 : Math.max(2, Math.min(12, Math.round(want)));
      var step = niceNum((hi - lo) / want, true);
      if (!(step > 0) || !isFinite(step)) step = 1;
      var start = Math.floor((lo + step * 1e-9) / step) * step;
      var end = Math.ceil((hi - step * 1e-9) / step) * step;
      if (!(end > start)) end = start + step;
      var count = Math.round((end - start) / step);
      if (!isFinite(count) || count < 1) {
        end = start + step;
        count = 1;
      }
      if (count > 60) {
        step = (end - start) / 60;
        count = 60;
      }
      var decimals = decimalsForStep(step);
      var ticks = [];
      for (var i = 0; i <= count; i++) {
        var v = start + step * i;
        var rounded = Number(v.toFixed(Math.min(6, decimals + 2)));
        ticks.push(isFinite(rounded) ? rounded : r2(start));
      }
      return {
        min: isFinite(start) ? r2(start) : 0,
        max: isFinite(end) ? r2(end) : 1,
        step: step,
        ticks: ticks,
        decimals: decimals
      };
    }

    function formatTick(value, step) {
      var num = finite(value);
      if (num === null) return '';
      var d = decimalsForStep(finite(step) === null ? 1 : step);
      return num.toFixed(Math.min(6, d));
    }

    function chartKind(spec) {
      var declared = spec && (spec.chart_type || spec.subtype);
      if (!declared) {
        if (Array.isArray(spec && spec.points)) return 'scatter';
        if ((Array.isArray(spec && spec.values) || Array.isArray(spec && spec.matrix)
             || Array.isArray(spec && spec.cells))
            && Array.isArray(spec && spec.x_labels)) return 'heatmap';
        if (Array.isArray(spec && spec.x) && Array.isArray(spec && spec.series)) return 'line';
        return 'bar';
      }
      var raw = String(declared).toLowerCase().replace(/\s+/g, '_');
      if (raw === 'grouped_bar' || raw === 'bar_chart' || raw === 'column' || raw === 'columns') return 'bar';
      if (raw === 'line_chart' || raw === 'lines') return 'line';
      if (raw === 'bubble' || raw === 'pareto' || raw === 'scatter_plot') return 'scatter';
      if (raw === 'matrix' || raw === 'heat_map') return 'heatmap';
      return raw;
    }

    function textWidth(text) {
      var out = 0;
      var value = String(text === null || text === undefined ? '' : text);
      for (var i = 0; i < value.length; i++) {
        out += value.charCodeAt(i) > 0xff ? 12 : 6.4;
      }
      return out;
    }

    function clipText(text, max) {
      var value = String(text === null || text === undefined ? '' : text);
      if (value.length <= max) return value;
      return value.slice(0, Math.max(1, max - 1)) + '\u2026';
    }

    function padOf(spec, kind) {
      var left = 62;
      if (kind === 'heatmap') left = 88;
      if (kind === 'line' || kind === 'scatter') left = 66;
      if (spec && spec.y_label) left += 14;
      return { top: 26, right: 24, bottom: 54, left: left };
    }

    function plotOf(width, height, pad) {
      var w = Math.max(40, width - pad.left - pad.right);
      var h = Math.max(40, height - pad.top - pad.bottom);
      return { x: pad.left, y: pad.top, w: r2(w), h: r2(h) };
    }

    function ticksWithY(scale, yOf) {
      return (scale.ticks || []).map(function (value) {
        return { value: value, y: r2(yOf(value)), label: formatTick(value, scale.step) };
      });
    }

    function layoutBar(spec, frame) {
      var plot = frame.plot;
      var hidden = spec.hiddenSeries && typeof spec.hiddenSeries === 'object' ? spec.hiddenSeries : {};
      var allSeries = Array.isArray(spec.series) ? spec.series : [];
      var categories = (Array.isArray(spec.categories) ? spec.categories : []).map(function (c) {
        return String(c === null || c === undefined ? '' : c);
      });
      if (!categories.length) {
        var longest = 0;
        allSeries.forEach(function (s) {
          if (s && Array.isArray(s.data)) longest = Math.max(longest, s.data.length);
        });
        for (var i = 0; i < longest; i++) categories.push(String(i + 1));
      }
      var series = [];
      allSeries.forEach(function (s, idx) {
        if (!s) return;
        var key = seriesKey(s, idx);
        if (hidden[key]) return;
        series.push({ source: s, index: idx, key: key, name: key, color: s.color || colorOf(idx) });
      });
      var values = [];
      series.forEach(function (s) {
        (Array.isArray(s.source.data) ? s.source.data : []).forEach(function (raw) {
          var n = finite(raw);
          if (n !== null) values.push(n);
        });
      });
      var refs = (Array.isArray(spec.ref_lines) ? spec.ref_lines : []).filter(Boolean);
      refs.forEach(function (ref) {
        var n = finite(ref.value);
        if (n !== null) values.push(n);
      });
      var lo = values.length ? values[0] : 0;
      var hi = values.length ? values[0] : 1;
      values.forEach(function (v) {
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      });
      var scale = niceScale(lo, hi, 5, true);
      var yMin = finite(spec.y_min);
      var yMax = finite(spec.y_max);
      if (yMin !== null || yMax !== null) {
        var lo2 = yMin !== null ? yMin : scale.min;
        var hi2 = yMax !== null ? yMax : scale.max;
        if (!(hi2 > lo2)) hi2 = lo2 + 1;
        var step2 = niceNum((hi2 - lo2) / 5, true);
        var ticks2 = [];
        for (var t = lo2, guard = 0; t <= hi2 + step2 * 1e-9 && guard < 24; t += step2, guard++) {
          ticks2.push(Number(t.toFixed(6)));
        }
        if (ticks2.length < 2) ticks2 = [lo2, hi2];
        scale = { min: lo2, max: hi2, step: step2, ticks: ticks2, decimals: decimalsForStep(step2) };
      }
      function yOf(value) {
        return plot.y + plot.h - ((value - scale.min) / (scale.max - scale.min)) * plot.h;
      }
      var count = categories.length;
      var bands = plot.w / Math.max(1, count);
      var slotCount = Math.max(1, series.length);
      var inner = bands * 0.72;
      var slot = inner / slotCount;
      var barW = Math.max(3, Math.min(36, slot * 0.82));
      var zeroY = clamp(yOf(clamp(0, scale.min, scale.max)), plot.y, plot.y + plot.h);
      var bars = [];
      series.forEach(function (s, si) {
        for (var ci = 0; ci < count; ci++) {
          var raw = Array.isArray(s.source.data) ? s.source.data[ci] : null;
          var value = finite(raw);
          if (value === null) continue;
          var yv = clamp(yOf(value), plot.y, plot.y + plot.h);
          var centre = plot.x + bands * ci + bands / 2 + (si - (series.length - 1) / 2) * slot;
          var x = clamp(centre - barW / 2, plot.x, plot.x + plot.w - barW);
          var top = Math.min(yv, zeroY);
          var h = Math.abs(zeroY - yv);
          bars.push({
            x: r2(x), y: r2(clamp(top, plot.y, plot.y + plot.h)),
            w: r2(barW), h: r2(clamp(h, 0, plot.h)),
            value: value, catIndex: ci, seriesIndex: s.index, seriesName: s.name,
            category: categories[ci], color: s.color,
            tip: categories[ci] + ' · ' + s.name + ': ' + formatNumber(value, scale.decimals) + (spec.unit || '')
          });
        }
      });
      var xLabels = categories.map(function (text, ci) {
        return {
          text: clipText(text, 14), x: r2(plot.x + bands * ci + bands / 2),
          y: r2(plot.y + plot.h + 20), anchor: 'middle'
        };
      });
      var refLines = refs.map(function (ref) {
        var value = finite(ref.value);
        if (value === null) return null;
        return {
          value: value, y: r2(clamp(yOf(value), plot.y, plot.y + plot.h)),
          label: ref.label === undefined || ref.label === null ? '' : String(ref.label),
          tone: ref.tone || 'warn'
        };
      }).filter(Boolean);
      var legend = [];
      allSeries.forEach(function (s, idx) {
        if (!s) return;
        var key = seriesKey(s, idx);
        legend.push({ name: key, key: key, color: s.color || colorOf(idx), hidden: !!hidden[key] });
      });
      return {
        kind: 'bar', width: frame.width, height: frame.height, pad: frame.pad, plot: plot,
        title: spec.title || '', categories: categories, bars: bars, xLabels: xLabels,
        yAxis: { min: scale.min, max: scale.max, step: scale.step, decimals: scale.decimals,
          ticks: ticksWithY(scale, yOf), label: spec.y_label || '', unit: spec.unit || '' },
        refLines: refLines, legend: legend, unit: spec.unit || ''
      };
    }

    function layoutLine(spec, frame) {
      var plot = frame.plot;
      var hidden = spec.hiddenSeries && typeof spec.hiddenSeries === 'object' ? spec.hiddenSeries : {};
      var allSeries = Array.isArray(spec.series) ? spec.series : [];
      var xs = Array.isArray(spec.x) ? spec.x : [];
      var numericX = xs.length > 0 && xs.every(function (v) { return finite(v) !== null; });
      if (!xs.length) {
        var longest = 0;
        allSeries.forEach(function (s) {
          if (s && Array.isArray(s.data)) longest = Math.max(longest, s.data.length);
        });
        for (var i = 0; i < longest; i++) xs.push(i + 1);
      }
      var categories = xs.map(function (v) { return String(v === null || v === undefined ? '' : v); });
      var series = [];
      allSeries.forEach(function (s, idx) {
        if (!s) return;
        var key = seriesKey(s, idx);
        if (hidden[key]) return;
        series.push({ source: s, index: idx, key: key, name: key, color: s.color || colorOf(idx), dashed: !!s.dashed });
      });
      var values = [];
      series.forEach(function (s) {
        (Array.isArray(s.source.data) ? s.source.data : []).forEach(function (raw) {
          var n = finite(raw);
          if (n !== null) values.push(n);
        });
      });
      var refs = (Array.isArray(spec.ref_lines) ? spec.ref_lines : []).filter(Boolean);
      refs.forEach(function (ref) {
        var n = finite(ref.value);
        if (n !== null) values.push(n);
      });
      var lo = values.length ? values[0] : 0;
      var hi = values.length ? values[0] : 1;
      values.forEach(function (v) {
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      });
      var scale = niceScale(lo, hi, 5, true);
      var yMin = finite(spec.y_min);
      var yMax = finite(spec.y_max);
      if (yMin !== null || yMax !== null) {
        scale.min = yMin !== null ? yMin : scale.min;
        scale.max = yMax !== null ? yMax : scale.max;
        if (!(scale.max > scale.min)) scale.max = scale.min + 1;
        scale.ticks = scale.ticks.filter(function (v) {
          return v >= scale.min - EPS && v <= scale.max + EPS;
        });
        if (scale.ticks.length < 2) scale.ticks = [scale.min, scale.max];
      }
      function yOf(value) {
        return plot.y + plot.h - ((value - scale.min) / (scale.max - scale.min)) * plot.h;
      }
      var xNums = numericX ? xs.map(function (v) { return finite(v); }) : null;
      var xMin = 0;
      var xMax = 1;
      if (numericX) {
        xMin = xNums[0];
        xMax = xNums[0];
        xNums.forEach(function (v) {
          if (v < xMin) xMin = v;
          if (v > xMax) xMax = v;
        });
        if (!(xMax > xMin)) {
          var base = Math.abs(xMin) > EPS ? Math.abs(xMin) : 1;
          xMin -= base * 0.5;
          xMax += base * 0.5;
        }
      }
      function xOf(index) {
        if (!numericX) {
          return categories.length <= 1
            ? plot.x + plot.w / 2
            : plot.x + (index / (categories.length - 1)) * plot.w;
        }
        return plot.x + ((xNums[index] - xMin) / (xMax - xMin)) * plot.w;
      }
      var lines = [];
      var points = [];
      series.forEach(function (s) {
        var segments = [];
        var current = [];
        var data = Array.isArray(s.source.data) ? s.source.data : [];
        for (var i = 0; i < data.length && i < categories.length; i++) {
          var value = finite(data[i]);
          if (value === null) {
            if (current.length) segments.push(current);
            current = [];
            continue;
          }
          var point = {
            x: r2(clamp(xOf(i), plot.x, plot.x + plot.w)),
            y: r2(clamp(yOf(value), plot.y, plot.y + plot.h)),
            value: value, xLabel: categories[i], catIndex: i,
            seriesIndex: s.index, seriesName: s.name, color: s.color,
            tip: categories[i] + ' · ' + s.name + ': ' + formatNumber(value, scale.decimals) + (spec.unit || '')
          };
          current.push(point);
          points.push(point);
        }
        if (current.length) segments.push(current);
        var d = segments.map(function (seg) {
          return seg.map(function (p, i) { return (i === 0 ? 'M' : 'L') + p.x + ' ' + p.y; }).join(' ');
        }).join(' ');
        lines.push({
          name: s.name, key: s.key, seriesIndex: s.index, color: s.color,
          dashed: s.dashed, d: d, segments: segments
        });
      });
      var xLabels = [];
      if (numericX) {
        var xScale = niceScale(xMin, xMax, 4, false);
        xLabels = xScale.ticks.map(function (v) {
          return {
            text: formatTick(v, xScale.step),
            x: r2(clamp(plot.x + ((v - xMin) / (xMax - xMin)) * plot.w, plot.x, plot.x + plot.w)),
            y: r2(plot.y + plot.h + 20), anchor: 'middle'
          };
        });
      } else {
        var every = categories.length > 10 ? Math.ceil(categories.length / 8) : 1;
        categories.forEach(function (text, ci) {
          if (ci % every !== 0 && ci !== categories.length - 1) return;
          xLabels.push({
            text: clipText(text, 12), x: r2(xOf(ci)),
            y: r2(plot.y + plot.h + 20), anchor: 'middle'
          });
        });
      }
      var refLines = refs.map(function (ref) {
        var value = finite(ref.value);
        if (value === null) return null;
        return {
          value: value, y: r2(clamp(yOf(value), plot.y, plot.y + plot.h)),
          label: ref.label === undefined || ref.label === null ? '' : String(ref.label),
          tone: ref.tone || 'warn'
        };
      }).filter(Boolean);
      var legend = [];
      allSeries.forEach(function (s, idx) {
        if (!s) return;
        var key = seriesKey(s, idx);
        legend.push({ name: key, key: key, color: s.color || colorOf(idx), hidden: !!hidden[key] });
      });
      return {
        kind: 'line', width: frame.width, height: frame.height, pad: frame.pad, plot: plot,
        title: spec.title || '', categories: categories, lines: lines, points: points,
        xLabels: xLabels,
        yAxis: { min: scale.min, max: scale.max, step: scale.step, decimals: scale.decimals,
          ticks: ticksWithY(scale, yOf), label: spec.y_label || '', unit: spec.unit || '' },
        refLines: refLines, legend: legend, unit: spec.unit || ''
      };
    }

    function paretoFrontier(points, mode) {
      if (!mode || mode === 'false') return [];
      var list = points.slice();
      var out = [];
      var best = 0;
      if (mode === 'max_y_min_x') {
        list.sort(function (a, b) { return a.x - b.x || b.y - a.y; });
        best = -Infinity;
        list.forEach(function (p) {
          if (p.y > best + EPS) { out.push(p); best = p.y; }
        });
        return out;
      }
      if (mode === 'max_xy') {
        list.sort(function (a, b) { return b.x - a.x || b.y - a.y; });
        best = -Infinity;
        list.forEach(function (p) {
          if (p.y > best + EPS) { out.push(p); best = p.y; }
        });
        return out.reverse();
      }
      if (mode === 'min_xy') {
        list.sort(function (a, b) { return a.x - b.x || a.y - b.y; });
        best = Infinity;
        list.forEach(function (p) {
          if (p.y < best - EPS) { out.push(p); best = p.y; }
        });
        return out;
      }
      return [];
    }

    function layoutScatter(spec, frame) {
      var plot = frame.plot;
      var rawPoints = Array.isArray(spec.points) ? spec.points : [];
      var points = [];
      var sizeValues = [];
      rawPoints.forEach(function (p, index) {
        if (!p) return;
        var x = finite(p.x);
        var y = finite(p.y);
        if (x === null || y === null) return;
        var size = finite(p.size);
        if (size !== null) sizeValues.push(size);
        points.push({ source: p, sourceIndex: index, x: x, y: y, size: size });
      });
      var xs = points.map(function (p) { return p.x; });
      var ys = points.map(function (p) { return p.y; });
      var xMin = xs.length ? Math.min.apply(null, xs) : 0;
      var xMax = xs.length ? Math.max.apply(null, xs) : 1;
      var yMin = ys.length ? Math.min.apply(null, ys) : 0;
      var yMax = ys.length ? Math.max.apply(null, ys) : 1;
      if (xMax - xMin < EPS) {
        var bx = Math.abs(xMin) > EPS ? Math.abs(xMin) * 0.1 : 1;
        xMin -= bx;
        xMax += bx;
      }
      if (yMax - yMin < EPS) {
        var by = Math.abs(yMin) > EPS ? Math.abs(yMin) * 0.1 : 1;
        yMin -= by;
        yMax += by;
      }
      var xPad = (xMax - xMin) * 0.08;
      var yPad = (yMax - yMin) * 0.08;
      var xScale = niceScale(finite(spec.x_min) !== null ? spec.x_min : xMin - xPad,
        finite(spec.x_max) !== null ? spec.x_max : xMax + xPad, 4, false);
      var yScale = niceScale(finite(spec.y_min) !== null ? spec.y_min : yMin - yPad,
        finite(spec.y_max) !== null ? spec.y_max : yMax + yPad, 5, false);
      function xOf(v) {
        return plot.x + ((v - xScale.min) / (xScale.max - xScale.min)) * plot.w;
      }
      function yOf(v) {
        return plot.y + plot.h - ((v - yScale.min) / (yScale.max - yScale.min)) * plot.h;
      }
      var sizeMin = sizeValues.length ? Math.min.apply(null, sizeValues) : 0;
      var sizeMax = sizeValues.length ? Math.max.apply(null, sizeValues) : 1;
      var hidden = spec.hiddenSeries && typeof spec.hiddenSeries === 'object' ? spec.hiddenSeries : {};
      var groups = [];
      var out = [];
      points.forEach(function (p) {
        var group = p.source.group === undefined || p.source.group === null ? '' : String(p.source.group);
        if (group && groups.indexOf(group) < 0) groups.push(group);
        var groupIndex = group ? groups.indexOf(group) : 0;
        if (group && hidden[group]) return;
        var radius = 6;
        if (p.size !== null && sizeValues.length) {
          var t = sizeMax - sizeMin < EPS ? 0.5 : (p.size - sizeMin) / (sizeMax - sizeMin);
          radius = 5 + t * 11;
        }
        out.push({
          index: p.sourceIndex,
          label: p.source.label === undefined || p.source.label === null ? '' : String(p.source.label),
          x: r2(clamp(xOf(p.x), plot.x, plot.x + plot.w)),
          y: r2(clamp(yOf(p.y), plot.y, plot.y + plot.h)),
          r: r2(radius),
          value: p.y,
          xValue: p.x,
          group: group,
          groupIndex: groupIndex,
          color: p.source.highlight ? 'var(--warn)' : colorOf(groupIndex),
          onFrontier: false,
          tip: (p.source.label === undefined || p.source.label === null ? '' : String(p.source.label))
            + ': ' + (spec.x_label || 'x') + ' ' + formatNumber(p.x, 2)
            + ', ' + (spec.y_label || 'y') + ' ' + formatNumber(p.y, 2)
        });
      });
      var frontier = paretoFrontier(out.map(function (p) {
        return { ref: p, x: p.xValue, y: p.value };
      }), spec.pareto || false);
      var frontierRefs = frontier.map(function (item) { return item.ref; });
      frontierRefs.forEach(function (p) { p.onFrontier = true; });
      var paretoPath = frontierRefs.length
        ? frontierRefs.map(function (p, i) { return (i === 0 ? 'M' : 'L') + p.x + ' ' + p.y; }).join(' ')
        : '';
      var placed = [];
      var offsets = [[9, -9], [9, 15], [-9, -9], [-9, 15], [0, -17], [0, 21]];
      out.forEach(function (p) {
        var width = Math.max(18, textWidth(p.label) + 4);
        var chosen = offsets[0];
        for (var oi = 0; oi < offsets.length; oi++) {
          var ox = offsets[oi][0];
          var oy = offsets[oi][1];
          var lx = clamp(p.x + ox, plot.x + 2, plot.x + plot.w - width - 2);
          var ly = clamp(p.y + oy, plot.y + 12, plot.y + plot.h - 4);
          var hit = false;
          for (var pi = 0; pi < placed.length; pi++) {
            var q = placed[pi];
            if (Math.abs(q.x - lx) < (q.w + width) / 2 && Math.abs(q.y - ly) < 14) { hit = true; break; }
          }
          if (!hit) {
            chosen = [lx, ly, width];
            break;
          }
          if (oi === offsets.length - 1) chosen = [lx, ly, width];
        }
        p.labelX = r2(clamp(chosen[0], plot.x + 2, plot.x + plot.w - width - 2));
        p.labelY = r2(clamp(chosen[1], plot.y + 12, plot.y + plot.h - 4));
        p.labelAnchor = p.labelX >= p.x ? 'start' : 'end';
        placed.push({ x: p.labelX, y: p.labelY, w: width });
      });
      var xLabels = (xScale.ticks || []).map(function (v) {
        return {
          text: formatTick(v, xScale.step),
          x: r2(clamp(xOf(v), plot.x, plot.x + plot.w)),
          y: r2(plot.y + plot.h + 20), anchor: 'middle'
        };
      });
      var legend = groups.map(function (group, idx) {
        return { name: group, key: group, color: colorOf(idx), hidden: !!hidden[group] };
      });
      return {
        kind: 'scatter', width: frame.width, height: frame.height, pad: frame.pad, plot: plot,
        title: spec.title || '', points: out, paretoPath: paretoPath, frontier: frontierRefs,
        xLabels: xLabels,
        xAxis: { min: xScale.min, max: xScale.max, step: xScale.step, decimals: xScale.decimals,
          ticks: (xScale.ticks || []).map(function (v) {
            return { value: v, x: r2(clamp(xOf(v), plot.x, plot.x + plot.w)), label: formatTick(v, xScale.step) };
          }), label: spec.x_label || '', unit: spec.x_unit || '' },
        yAxis: { min: yScale.min, max: yScale.max, step: yScale.step, decimals: yScale.decimals,
          ticks: ticksWithY(yScale, yOf), label: spec.y_label || '', unit: spec.y_unit || '' },
        legend: legend
      };
    }

    function detailAt(details, row, col) {
      if (!details) return null;
      if (Array.isArray(details)) {
        var line = details[row];
        if (!Array.isArray(line)) return null;
        var cell = line[col];
        return cell && typeof cell === 'object' ? cell : null;
      }
      if (typeof details === 'object') {
        return details[row + ',' + col] || details[row + ':' + col] || null;
      }
      return null;
    }

    function layoutHeatmap(spec, frame) {
      var plot = frame.plot;
      var xLabelsIn = (Array.isArray(spec.x_labels) ? spec.x_labels : []).map(String);
      var yLabelsIn = (Array.isArray(spec.y_labels) ? spec.y_labels : []).map(String);
      var source = Array.isArray(spec.values) ? spec.values : (Array.isArray(spec.matrix) ? spec.matrix : []);
      var rows = Math.max(yLabelsIn.length, source.length);
      var cols = xLabelsIn.length;
      source.forEach(function (line) {
        if (Array.isArray(line)) cols = Math.max(cols, line.length);
      });
      (Array.isArray(spec.cells) ? spec.cells : []).forEach(function (cell) {
        if (!cell) return;
        var r = finite(cell.row);
        var c = finite(cell.col);
        if (r === null || c === null) return;
        rows = Math.max(rows, Math.round(r) + 1);
        cols = Math.max(cols, Math.round(c) + 1);
      });
      while (xLabelsIn.length < cols) xLabelsIn.push(String(xLabelsIn.length + 1));
      while (yLabelsIn.length < rows) yLabelsIn.push(String(yLabelsIn.length + 1));
      var grid = [];
      var values = [];
      for (var ri = 0; ri < rows; ri++) {
        grid.push([]);
        for (var ci = 0; ci < cols; ci++) {
          var raw = Array.isArray(source[ri]) ? source[ri][ci] : null;
          var value = finite(raw);
          grid[ri].push(value);
          if (value !== null) values.push(value);
        }
      }
      var sparse = Array.isArray(spec.cells) ? spec.cells : [];
      sparse.forEach(function (cell, index) {
        if (!cell) return;
        var r = finite(cell.row);
        var c = finite(cell.col);
        var v = finite(cell.value);
        if (r === null || c === null) return;
        var ri = Math.round(r);
        var ci = Math.round(c);
        if (ri < 0 || ci < 0 || ri >= rows || ci >= cols) return;
        grid[ri][ci] = v;
        if (v !== null) values.push(v);
      });
      var min = finite(spec.min);
      var max = finite(spec.max);
      if (min === null) min = values.length ? Math.min.apply(null, values) : 0;
      if (max === null) max = values.length ? Math.max.apply(null, values) : 1;
      if (!(max > min)) {
        var base = Math.abs(min) > EPS ? Math.abs(min) : 1;
        min -= base * 0.5;
        max += base * 0.5;
      }
      var mode = spec.scale_mode || 'max_good';
      var decimals = finite(spec.decimals);
      decimals = decimals === null ? (max - min < 10 ? 2 : (max - min < 100 ? 1 : 0)) : Math.round(decimals);
      var cw = plot.w / Math.max(1, cols);
      var ch = plot.h / Math.max(1, rows);
      var cells = [];
      for (var y = 0; y < rows; y++) {
        for (var x = 0; x < cols; x++) {
          var val = grid[y][x];
          var norm = val === null ? 0 : clamp((val - min) / (max - min), 0, 1);
          var alpha;
          if (mode === 'min_good') alpha = 0.10 + 0.82 * (1 - norm);
          else if (mode === 'neutral') alpha = 0.10 + 0.82 * (1 - Math.abs(norm - 0.5) * 2);
          else alpha = 0.10 + 0.82 * norm;
          var tone = 'mid';
          if (mode === 'max_good') tone = norm >= 0.66 ? 'good' : (norm <= 0.33 ? 'bad' : 'mid');
          else if (mode === 'min_good') tone = norm <= 0.33 ? 'good' : (norm >= 0.66 ? 'bad' : 'mid');
          else tone = Math.abs(norm - 0.5) < 0.2 ? 'mid' : 'strong';
          var detail = detailAt(spec.cell_details, y, x);
          var sparseCell = null;
          for (var si = 0; si < sparse.length; si++) {
            var sc = sparse[si];
            if (!sc) continue;
            var sr = finite(sc.row);
            var scx = finite(sc.col);
            if (sr === null || scx === null) continue;
            if (Math.round(sr) === y && Math.round(scx) === x) {
              sparseCell = sc;
              break;
            }
          }
          cells.push({
            x: r2(clamp(plot.x + x * cw, plot.x, plot.x + plot.w)),
            y: r2(clamp(plot.y + y * ch, plot.y, plot.y + plot.h)),
            w: r2(Math.max(1, cw - 2)),
            h: r2(Math.max(1, ch - 2)),
            row: y, col: x, value: val,
            text: val === null ? '' : formatNumber(val, decimals),
            norm: r2(norm), alpha: r2(alpha), tone: tone,
            xLabel: xLabelsIn[x], yLabel: yLabelsIn[y],
            sparseIndex: sparseCell ? sparse.indexOf(sparseCell) : null,
            summary: (detail && detail.summary) || (sparseCell && sparseCell.summary) || '',
            details: (detail && detail.details) || (sparseCell && sparseCell.details) || '',
            files: (detail && detail.files) || (sparseCell && sparseCell.files) || [],
            tip: yLabelsIn[y] + ' × ' + xLabelsIn[x] + ': '
              + (val === null ? '—' : formatNumber(val, decimals) + (spec.unit || ''))
          });
        }
      }
      var xLabels = xLabelsIn.map(function (text, ci) {
        return {
          text: clipText(text, 12), x: r2(plot.x + cw * ci + cw / 2),
          y: r2(plot.y + plot.h + 20), anchor: 'middle'
        };
      });
      var yLabels = yLabelsIn.map(function (text, ri) {
        return {
          text: clipText(text, 14), x: r2(plot.x - 10),
          y: r2(plot.y + ch * ri + ch / 2 + 4), anchor: 'end'
        };
      });
      return {
        kind: 'heatmap', width: frame.width, height: frame.height, pad: frame.pad, plot: plot,
        title: spec.title || '', cells: cells, rows: rows, cols: cols,
        xLabels: xLabels, yLabels: yLabels,
        scale: { min: min, max: max, mode: mode, unit: spec.unit || '' },
        scaleLabel: formatNumber(min, decimals) + ' \u2192 ' + formatNumber(max, decimals),
        legend: []
      };
    }

    function layoutChart(spec) {
      var src = spec && typeof spec === 'object' ? spec : {};
      var kind = chartKind(src);
      if (['bar', 'line', 'scatter', 'heatmap'].indexOf(kind) < 0) kind = 'bar';
      var width = finite(src.width);
      width = width === null ? 720 : Math.max(360, Math.min(1200, Math.round(width)));
      var height = finite(src.height);
      if (height === null) {
        height = kind === 'heatmap' ? 320 : (kind === 'bar' ? 340 : 360);
        if (kind === 'heatmap' && Array.isArray(src.y_labels)) {
          height = Math.max(260, Math.min(520, 120 + src.y_labels.length * 34));
        }
      }
      height = Math.max(220, Math.min(720, Math.round(height)));
      var pad = padOf(src, kind);
      var frame = { width: width, height: height, pad: pad, plot: plotOf(width, height, pad) };
      if (kind === 'line') return layoutLine(src, frame);
      if (kind === 'scatter') return layoutScatter(src, frame);
      if (kind === 'heatmap') return layoutHeatmap(src, frame);
      return layoutBar(src, frame);
    }

    return {
      finite: finite,
      clamp: clamp,
      niceNum: niceNum,
      niceScale: niceScale,
      formatTick: formatTick,
      formatNumber: formatNumber,
      chartKind: chartKind,
      layoutChart: layoutChart,
      layoutBar: layoutBar,
      layoutLine: layoutLine,
      layoutScatter: layoutScatter,
      layoutHeatmap: layoutHeatmap,
      paretoFrontier: paretoFrontier
    };
  })();
  (typeof globalThis !== 'undefined' ? globalThis : this).ChartCore = ChartCore;
  /* ==== VR_CHART_CORE_END ==== */
/* ==== VR_GRAPH_CORE_START ==== */
  /* 纯几何计算，不依赖 DOM；test_visual_reply.py 会把这段抽出来在 node vm 里跑。 */
  var GraphCore = (function () {
    'use strict';

    var GRAPH = {
      NW: 204,            // 节点宽度
      NH: 68,             // 节点高度
      GY: 16,             // 同列行距
      PAD: 34,            // 画布基础内边距
      MIN_GAP: 108,       // 相邻列最小间隙
      MAX_GAP: 230,       // 相邻列最大间隙
      LABEL_PAD: 36,      // 标签两侧预留
      FULL_W: 12,         // 全角字符估算宽度
      ASCII_W: 7,         // ASCII 字符估算宽度
      LANE_GAP: 34,       // Bypass 通道间距
      LABEL_MIN_DY: 24,   // 同间隙标签最小 Y 间距
      LABEL_H: 20,        // 标签胶囊高度
      CORNER: 10,         // 折线圆角半径
      PORT_SPAN: 0.62     // 顶/底接口在节点宽度上的分布比例
    };

    function num(v) { return Math.round(v * 100) / 100; }

    function estimateLabelWidth(text) {
      var s = text === null || text === undefined ? '' : String(text);
      var w = 0;
      for (var i = 0; i < s.length; i++) {
        var code = s.charCodeAt(i);
        if (code >= 0xd800 && code <= 0xdbff && i + 1 < s.length) {
          w += GRAPH.FULL_W;   // 代理对（emoji 等）按全角计
          i += 1;
        } else if (code > 0xff) {
          w += GRAPH.FULL_W;
        } else {
          w += GRAPH.ASCII_W;
        }
      }
      return w;
    }

    function computeLayers(nodes, edges) {
      var ids = [];
      var byId = {};
      (nodes || []).forEach(function (n) {
        if (!n || n.id === undefined || n.id === null) return;
        var id = String(n.id);
        if (Object.prototype.hasOwnProperty.call(byId, id)) return;
        byId[id] = n;
        ids.push(id);
      });
      var indeg = {};
      var adj = {};
      ids.forEach(function (id) { indeg[id] = 0; adj[id] = []; });
      (edges || []).forEach(function (e) {
        if (!e) return;
        var a = String(e.from);
        var b = String(e.to);
        if (a === b || !byId[a] || !byId[b]) return;
        adj[a].push(b);
        indeg[b] += 1;
      });

      var layerOf = {};
      var hasLayer = (nodes || []).some(function (n) {
        return n && n.layer !== undefined && n.layer !== null && n.layer !== '';
      });

      if (hasLayer) {
        (nodes || []).forEach(function (n) {
          if (!n || n.id === undefined || n.id === null) return;
          var v = Number(n.layer);
          layerOf[String(n.id)] = isFinite(v) ? v : 0;
        });
        return layerOf;
      }

      var deg = {};
      ids.forEach(function (id) { deg[id] = indeg[id]; });
      var queue = ids.filter(function (id) { return deg[id] === 0; });
      queue.forEach(function (id) { layerOf[id] = 0; });
      while (queue.length) {
        var cur = queue.shift();
        adj[cur].forEach(function (nxt) {
          var cand = (layerOf[cur] || 0) + 1;
          if (layerOf[nxt] === undefined || cand > layerOf[nxt]) layerOf[nxt] = cand;
          deg[nxt] -= 1;
          if (deg[nxt] === 0) queue.push(nxt);
        });
      }
      ids.forEach(function (id) { if (layerOf[id] === undefined) layerOf[id] = 0; });
      return layerOf;
    }

    /* 三次贝塞尔（相邻前向边）：源右中 → 目标左中 */
    function adjacentPath(a, b) {
      var x1 = a.x + a.w;
      var y1 = a.y + a.h / 2;
      var x2 = b.x;
      var y2 = b.y + b.h / 2;
      var dx = Math.max(40, Math.abs(x2 - x1) * 0.46);
      var c1 = [x1 + dx, y1];
      var c2 = [x2 - dx, y2];
      return {
        d: 'M' + num(x1) + ',' + num(y1) +
           ' C' + num(c1[0]) + ',' + num(c1[1]) + ' ' + num(c2[0]) + ',' + num(c2[1]) +
           ' ' + num(x2) + ',' + num(y2),
        pts: [[x1, y1], c1, c2, [x2, y2]],
        x1: x1, y1: y1, x2: x2, y2: y2
      };
    }

    /* 圆角折线（Bypass 通道）：源顶/底 → 水平轨道 → 目标顶/底 */
    function bypassPath(sx, sy, tx, ty, laneY, up) {
      var hLen = Math.abs(tx - sx);
      var vRoom = up ? Math.min(sy, ty) - laneY : laneY - Math.max(sy, ty);
      var r = Math.min(GRAPH.CORNER, hLen / 2, Math.max(0, vRoom / 2));
      if (r < 0.5) r = 0;
      var dir = tx >= sx ? 1 : -1;
      var pts = [[sx, sy]];
      var d = 'M' + num(sx) + ',' + num(sy);
      var bend = up ? laneY + r : laneY - r;

      if (r > 0) {
        d += ' L' + num(sx) + ',' + num(bend);
        pts.push([sx, bend]);
        d += ' Q' + num(sx) + ',' + num(laneY) + ' ' + num(sx + dir * r) + ',' + num(laneY);
        pts.push([sx, laneY], [sx + dir * r, laneY]);
        d += ' L' + num(tx - dir * r) + ',' + num(laneY);
        pts.push([tx - dir * r, laneY]);
        d += ' Q' + num(tx) + ',' + num(laneY) + ' ' + num(tx) + ',' + num(bend);
        pts.push([tx, laneY], [tx, bend]);
      } else {
        d += ' L' + num(sx) + ',' + num(laneY) + ' L' + num(tx) + ',' + num(laneY);
        pts.push([sx, laneY], [tx, laneY]);
      }
      d += ' L' + num(tx) + ',' + num(ty);
      pts.push([tx, ty]);
      return { d: d, pts: pts };
    }

    function layoutGraph(nodes, edges) {
      var NW = GRAPH.NW;
      var NH = GRAPH.NH;
      var GY = GRAPH.GY;
      var PAD = GRAPH.PAD;

      var list = [];
      var byId = {};
      (nodes || []).forEach(function (n) {
        if (!n || n.id === undefined || n.id === null) return;
        var id = String(n.id);
        if (Object.prototype.hasOwnProperty.call(byId, id)) return;
        byId[id] = n;
        list.push(n);
      });

      var links = [];
      (edges || []).forEach(function (e) {
        if (!e) return;
        var a = String(e.from);
        var b = String(e.to);
        if (!Object.prototype.hasOwnProperty.call(byId, a)) return;
        if (!Object.prototype.hasOwnProperty.call(byId, b)) return;
        links.push({
          from: a,
          to: b,
          label: e.label === undefined || e.label === null ? '' : String(e.label)
        });
      });
      links.forEach(function (e, i) { e.idx = i; });

      var empty = {
        width: PAD * 2, height: PAD * 2, nw: NW, nh: NH,
        padTop: PAD, padBottom: PAD, gaps: [], colX: [],
        lanes: { top: [], bottom: [] }, nodes: [], edges: []
      };
      if (!list.length) return empty;

      /* ---- 1. 分层 → 列 ---- */
      var layerOf = computeLayers(list, links);
      var byLayer = {};
      var layerKeys = [];
      list.forEach(function (n) {
        var L = Number(layerOf[String(n.id)]);
        if (!isFinite(L)) L = 0;
        if (!byLayer[L]) { byLayer[L] = []; layerKeys.push(L); }
        byLayer[L].push(n);
      });
      layerKeys.sort(function (a, b) { return a - b; });
      var columns = layerKeys.map(function (L) { return byLayer[L].slice(); });

      var colOf = {};
      columns.forEach(function (col, ci) {
        col.forEach(function (n) { colOf[String(n.id)] = ci; });
      });

      /* ---- 2. 同列 Barycenter 排序（顺向 + 反向各一轮） ---- */
      var rows = [];
      function rowMap(ci) {
        var map = {};
        columns[ci].forEach(function (n, i) { map[String(n.id)] = i; });
        return map;
      }
      columns.forEach(function (_col, ci) { rows[ci] = rowMap(ci); });

      function reorder(ci, side) {
        var col = columns[ci];
        if (col.length < 2) { rows[ci] = rowMap(ci); return; }
        var keyed = col.map(function (n, i) {
          var id = String(n.id);
          var acc = 0;
          var cnt = 0;
          links.forEach(function (e) {
            var other = side === 'up' ? (e.to === id ? e.from : null) : (e.from === id ? e.to : null);
            if (other === null) return;
            var oc = colOf[other];
            if (oc === undefined) return;
            if (side === 'up' ? oc >= ci : oc <= ci) return;
            var r = rows[oc] ? rows[oc][other] : undefined;
            acc += r === undefined ? 0 : r;
            cnt += 1;
          });
          return { node: n, idx: i, key: cnt ? acc / cnt : i, has: cnt > 0 };
        });
        keyed.sort(function (a, b) {
          if (a.key !== b.key) return a.key - b.key;
          if (a.has !== b.has) return a.has ? -1 : 1;
          return a.idx - b.idx;
        });
        columns[ci] = keyed.map(function (k) { return k.node; });
        rows[ci] = rowMap(ci);
      }
      for (var up = 1; up < columns.length; up++) reorder(up, 'up');
      for (var down = columns.length - 2; down >= 0; down--) reorder(down, 'down');

      /* ---- 3. 动态列间距：按相邻前向边的标签宽度算 ---- */
      var gaps = [];
      for (var gi = 0; gi < columns.length - 1; gi++) {
        var maxPx = 0;
        links.forEach(function (e) {
          if (colOf[e.from] !== gi || colOf[e.to] !== gi + 1 || !e.label) return;
          maxPx = Math.max(maxPx, estimateLabelWidth(e.label));
        });
        gaps.push(Math.max(GRAPH.MIN_GAP, Math.min(GRAPH.MAX_GAP, maxPx + GRAPH.LABEL_PAD)));
      }
      var colX = [PAD];
      for (var xi = 1; xi < columns.length; xi++) colX.push(colX[xi - 1] + NW + gaps[xi - 1]);

      /* ---- 4. 边分类：相邻前向 / 顶部通道 / 底部通道 ---- */
      var adjEdges = [];
      var topEdges = [];
      var bottomEdges = [];
      links.forEach(function (e) {
        var cf = colOf[e.from];
        var ct = colOf[e.to];
        if (cf === undefined || ct === undefined) return;
        if (e.from === e.to) { bottomEdges.push(e); return; }
        if (ct === cf + 1) { adjEdges.push(e); return; }
        if (ct > cf + 1) { topEdges.push(e); return; }
        bottomEdges.push(e);
      });
      topEdges.sort(function (a, b) {
        return (colOf[a.to] - colOf[a.from]) - (colOf[b.to] - colOf[b.from]);
      });
      bottomEdges.sort(function (a, b) {
        return (colOf[a.from] - colOf[a.to]) - (colOf[b.from] - colOf[b.to]);
      });

      /* ---- 5. 画布尺寸：通道数量决定上下留白 ---- */
      var maxRows = 1;
      columns.forEach(function (col) { maxRows = Math.max(maxRows, col.length); });
      var contentH = maxRows * NH + (maxRows - 1) * GY;
      var padTop = PAD + topEdges.length * GRAPH.LANE_GAP;
      var padBottom = PAD + bottomEdges.length * GRAPH.LANE_GAP;
      var width = colX[columns.length - 1] + NW + PAD;
      var height = padTop + contentH + padBottom;

      /* ---- 6. 节点坐标 ---- */
      var outNodes = [];
      var nodeById = {};
      columns.forEach(function (col, ci) {
        var colH = col.length * NH + (col.length - 1) * GY;
        var y0 = padTop + (contentH - colH) / 2;
        col.forEach(function (n, ri) {
          var rec = {
            id: String(n.id),
            x: num(colX[ci]),
            y: num(y0 + ri * (NH + GY)),
            w: NW,
            h: NH,
            col: ci,
            row: ri,
            source: n
          };
          nodeById[rec.id] = rec;
          outNodes.push(rec);
        });
      });

      /* ---- 7. 通道位置：顶部向上、底部向下递增外扩 ---- */
      var topLaneY = topEdges.map(function (_e, i) { return padTop - GRAPH.LANE_GAP * (i + 1); });
      var bottomLaneY = bottomEdges.map(function (_e, i) {
        return padTop + contentH + GRAPH.LANE_GAP * (i + 1);
      });

      /* ---- 8. 顶/底接口：同一节点多条边按序错开 X ---- */
      var portReq = {};
      function wantPort(id, side, key) {
        var rec = portReq[id] || (portReq[id] = { top: [], bottom: [] });
        rec[side].push(key);
      }
      topEdges.forEach(function (e) {
        wantPort(e.from, 'top', e.idx + ':from');
        wantPort(e.to, 'top', e.idx + ':to');
      });
      bottomEdges.forEach(function (e) {
        wantPort(e.from, 'bottom', e.idx + ':from');
        wantPort(e.to, 'bottom', e.idx + ':to');
      });

      var portPos = {};
      Object.keys(portReq).forEach(function (id) {
        var node = nodeById[id];
        if (!node) return;
        ['top', 'bottom'].forEach(function (side) {
          var arr = portReq[id][side];
          var span = node.w * GRAPH.PORT_SPAN;
          var start = node.x + (node.w - span) / 2;
          arr.forEach(function (key, i) {
            var px = arr.length <= 1 ? node.x + node.w / 2 : start + span * (i + 0.5) / arr.length;
            portPos[key] = {
              x: num(px),
              y: side === 'top' ? node.y : num(node.y + node.h),
              side: side
            };
          });
        });
      });

      /* ---- 9. 生成边几何 ---- */
      var outEdges = [];
      adjEdges.forEach(function (e) {
        var a = nodeById[e.from];
        var b = nodeById[e.to];
        if (!a || !b) return;
        var geo = adjacentPath(a, b);
        outEdges.push({
          from: e.from,
          to: e.to,
          label: e.label,
          kind: 'adj',
          colFrom: colOf[e.from],
          colTo: colOf[e.to],
          d: geo.d,
          pts: geo.pts,
          labelX: (geo.x1 + geo.x2) / 2,
          labelY: (geo.y1 + geo.y2) / 2,
          labelW: estimateLabelWidth(e.label)
        });
      });

      function pushBypass(e, kind, laneY, laneIdx) {
        var a = portPos[e.idx + ':from'];
        var b = portPos[e.idx + ':to'];
        if (!a || !b) return;
        var geo = bypassPath(a.x, a.y, b.x, b.y, laneY, kind === 'top');
        outEdges.push({
          from: e.from,
          to: e.to,
          label: e.label,
          kind: kind,
          laneIdx: laneIdx,
          laneY: laneY,
          colFrom: colOf[e.from],
          colTo: colOf[e.to],
          d: geo.d,
          pts: geo.pts,
          labelX: (a.x + b.x) / 2,
          labelY: laneY,
          labelW: estimateLabelWidth(e.label)
        });
      }
      topEdges.forEach(function (e, i) { pushBypass(e, 'top', topLaneY[i], i); });
      bottomEdges.forEach(function (e, i) { pushBypass(e, 'bottom', bottomLaneY[i], i); });

      /* ---- 10. 标签坐标：先夹进画布，再按间隙分组避让 ---- */
      outEdges.forEach(function (e) {
        var half = e.labelW / 2 + 10;   // 胶囊内边距 + 边框
        if (width - half < half) e.labelX = width / 2;
        else e.labelX = Math.min(Math.max(e.labelX, half), width - half);
      });

      var groups = {};
      outEdges.forEach(function (e) {
        if (e.kind !== 'adj' || !e.label) return;
        var key = e.colFrom + '>' + e.colTo;
        (groups[key] = groups[key] || []).push(e);
      });
      Object.keys(groups).forEach(function (key) {
        var arr = groups[key].slice().sort(function (a, b) { return a.labelY - b.labelY; });
        for (var i = 1; i < arr.length; i++) {
          if (arr[i].labelY - arr[i - 1].labelY < GRAPH.LABEL_MIN_DY) {
            arr[i].labelY = arr[i - 1].labelY + GRAPH.LABEL_MIN_DY;
          }
        }
        var halfH = GRAPH.LABEL_H / 2 + 2;
        var shift = 0;
        if (arr[arr.length - 1].labelY > height - halfH) shift = height - halfH - arr[arr.length - 1].labelY;
        if (arr[0].labelY + shift < halfH) shift = halfH - arr[0].labelY;
        if (shift) arr.forEach(function (e) { e.labelY += shift; });
      });

      outEdges.forEach(function (e) {
        e.labelX = num(e.labelX);
        e.labelY = num(e.labelY);
        if (e.laneY !== undefined) e.laneY = num(e.laneY);
      });

      return {
        width: num(width),
        height: num(height),
        nw: NW,
        nh: NH,
        padTop: num(padTop),
        padBottom: num(padBottom),
        gaps: gaps,
        colX: colX,
        lanes: { top: topLaneY, bottom: bottomLaneY },
        nodes: outNodes,
        edges: outEdges
      };
    }

    return {
      GRAPH: GRAPH,
      estimateLabelWidth: estimateLabelWidth,
      computeLayers: computeLayers,
      layoutGraph: layoutGraph
    };
  })();
  /* ==== VR_GRAPH_CORE_END ==== */

/* ==== VR_RAIL_CORE_START ==== */
  /* 纯数据变换，不依赖 DOM；test_visual_reply.py 会把这段抽出来在 node vm 里跑。 */
  var RailTreeCore = (function () {
    'use strict';

    function parseCreatedMs(session) {
      return Date.parse(session && (session.created_at || session.updated_at) || '') || 0;
    }

    /* 固定创建顺序：不读 updated_at / seq，会话收到新回复时位置不变。 */
    function compareSessionsFixed(a, b) {
      return (parseCreatedMs(b) - parseCreatedMs(a)) ||
        (Number(b.created_seq || 0) - Number(a.created_seq || 0)) ||
        String(b.id || '').localeCompare(String(a.id || ''));
    }

    function stripProjectPrefix(session) {
      if (!session) return '';
      var project = session.project;
      if (!session.custom_label && project) {
        var prefix = String(project) + ' · ';
        if (String(session.label || '').indexOf(prefix) === 0) {
          return String(session.label || '').slice(prefix.length);
        }
      }
      return session.label || session.id || '';
    }

    /* 最新 #N 永远排第一。 */
    function sortRepliesDesc(replies) {
      return (replies || []).slice().sort(function (a, b) {
        return (Number(b.seq || 0) - Number(a.seq || 0)) ||
          String(b.reply_id || '').localeCompare(String(a.reply_id || ''));
      });
    }

    function projectKeyOf(session) {
      return String(session.project || 'workspace').trim() || 'workspace';
    }

    function buildRailTree(sessions, opts) {
      opts = opts || {};
      var filter = opts.filter || 'all';
      var unreadCountOf = opts.unreadCountOf || function () { return 0; };

      var filtered = (sessions || []).slice().filter(function (s) {
        if (!s) return false;
        if (filter === 'unread') return unreadCountOf(s) > 0;
        if (filter === 'pinned') return !!s.pinned;
        return true;
      });

      var pinnedSessions = filtered.filter(function (s) { return !!s.pinned; });
      pinnedSessions.sort(compareSessionsFixed);

      var pinnedGroup = null;
      if (pinnedSessions.length) {
        var pinnedUnread = 0;
        pinnedSessions.forEach(function (s) { pinnedUnread += unreadCountOf(s); });
        pinnedGroup = {
          kind: 'pinned',
          key: '__pinned__',
          label: '置顶',
          sessions: pinnedSessions,
          unreadTotal: pinnedUnread
        };
      }

      var groups = {};
      filtered.forEach(function (s) {
        if (s.pinned) return;
        var key = projectKeyOf(s);
        if (!groups[key]) groups[key] = [];
        groups[key].push(s);
      });

      var projects = Object.keys(groups).map(function (key) {
        var rows = groups[key].slice().sort(compareSessionsFixed);
        var newestCreatedMs = 0;
        var newestCreatedSeq = 0;
        var unreadTotal = 0;
        rows.forEach(function (s) {
          newestCreatedMs = Math.max(newestCreatedMs, parseCreatedMs(s));
          newestCreatedSeq = Math.max(newestCreatedSeq, Number(s.created_seq || 0));
          unreadTotal += unreadCountOf(s);
        });
        return {
          kind: 'project',
          key: key,
          label: key,
          sessions: rows,
          newestCreatedMs: newestCreatedMs,
          newestCreatedSeq: newestCreatedSeq,
          unreadTotal: unreadTotal
        };
      });

      projects.sort(function (a, b) {
        return compareSessionsFixed(a.sessions[0], b.sessions[0]) ||
          a.key.localeCompare(b.key);
      });

      return { pinned: pinnedGroup, projects: projects, totalCount: filtered.length };
    }

    return {
      parseCreatedMs: parseCreatedMs,
      compareSessionsFixed: compareSessionsFixed,
      stripProjectPrefix: stripProjectPrefix,
      buildRailTree: buildRailTree,
      sortRepliesDesc: sortRepliesDesc
    };
  })();
  (typeof globalThis !== 'undefined' ? globalThis : this).RailTreeCore = RailTreeCore;
  /* ==== VR_RAIL_CORE_END ==== */

  function renderGraph(pane, sec) {
    var nodes = (Array.isArray(sec.nodes) ? sec.nodes : []).filter(function (n) {
      return n && n.id !== undefined && n.id !== null;
    });
    var edges = Array.isArray(sec.edges) ? sec.edges : [];
    if (!nodes.length) return '';
    var g = GraphCore.layoutGraph(nodes, edges);
    var w = g.width;
    var h = g.height;

    var svg = '<svg class="graph-svg" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">';
    svg += '<defs><marker id="vr-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">' +
      '<path class="g-arrow" d="M0,0 L10,5 L0,10 z"></path></marker></defs>';
    g.edges.forEach(function (e) {
      svg += '<path class="g-edge" data-from="' + escapeAttr(e.from) + '" data-to="' + escapeAttr(e.to) +
        '" data-kind="' + escapeAttr(e.kind) + '" d="' + e.d + '" marker-end="url(#vr-arrow)"></path>';
    });
    svg += '</svg>';

    var pills = g.edges.filter(function (e) { return e.label; }).map(function (e) {
      return '<span class="g-edge-pill" data-from="' + escapeAttr(e.from) + '" data-to="' + escapeAttr(e.to) +
        '" data-kind="' + escapeAttr(e.kind) + '"' +
        ' style="left:' + e.labelX + 'px;top:' + e.labelY + 'px"' +
        ' title="' + escapeAttr(e.from + ' → ' + e.to + '：' + e.label) + '">' +
        escapeHtml(e.label) + '</span>';
    }).join('');

    var cards = g.nodes.map(function (p) {
      var n = p.source;
      var status = n.status || 'info';
      var key = registerDrawer(pane, {
        kicker: '节点详情',
        title: n.label || String(n.id),
        tags: [n.tag, n.layer !== undefined && n.layer !== null ? 'layer ' + n.layer : ''].filter(Boolean),
        summary: n.summary || '',
        details: n.details || '',
        files: n.files || []
      });
      return '<div class="g-node" role="button" tabindex="0" data-node="' + escapeAttr(p.id) +
        '" data-status="' + escapeAttr(status) + '" data-drawer="' + key +
        '" style="left:' + p.x + 'px;top:' + p.y + 'px;width:' + p.w + 'px;height:' + p.h + 'px">' +
        '<span class="g-dot"></span>' +
        '<div class="g-label">' + escapeHtml(n.label || p.id) + '</div>' +
        (n.tag ? '<div class="g-tag">' + escapeHtml(n.tag) + '</div>' : '') +
        '</div>';
    }).join('');

    return '<div class="graph-block">' +
      '<div class="graph-tools">' +
      '<button type="button" class="graph-tool" data-gact="fit" title="缩放到容器宽度">适应</button>' +
      '<button type="button" class="graph-tool" data-gact="reset" title="恢复 100%">100%</button>' +
      '<button type="button" class="graph-tool" data-gact="out" title="缩小" aria-label="缩小">&#8722;</button>' +
      '<span class="graph-zoom" data-role="zoom">100%</span>' +
      '<button type="button" class="graph-tool" data-gact="in" title="放大" aria-label="放大">+</button>' +
      '</div>' +
      '<div class="graph-wrap" data-w="' + w + '" data-h="' + h + '" data-scale="1" data-mode="fit"' +
      ' data-edges="' + escapeAttr(JSON.stringify(edges)) + '">' +
      '<div class="graph-scale-box" style="width:' + w + 'px;height:' + h + 'px">' +
      '<div class="graph-inner" style="width:' + w + 'px;height:' + h + 'px">' + svg + cards +
      (pills ? '<div class="graph-labels">' + pills + '</div>' : '') +
      '</div></div></div>' +
      '<div class="graph-legend">' +
      '<span data-status="ok">正常</span><span data-status="warn">注意</span>' +
      '<span data-status="err">异常</span><span data-status="info">信息</span>' +
      '<span data-status="info" style="--info:var(--fg-muted)">点击节点看详情</span></div>' +
      '</div>';
  }

  function renderComparison(pane, sec) {
    var items = Array.isArray(sec.items) ? sec.items : [];
    if (!items.length) return '';
    var dims = [];
    items.forEach(function (it) {
      Object.keys(it.scores || {}).forEach(function (k) {
        if (dims.indexOf(k) < 0) dims.push(k);
      });
    });

    function scoreBlock(it) {
      if (!dims.length) return '';
      return dims.map(function (dim) {
        var raw = it.scores ? it.scores[dim] : undefined;
        var val = Number(raw);
        if (!isFinite(val)) return '';
        var pct = Math.max(0, Math.min(100, (val / 5) * 100));
        return '<div class="cmp-score-row"><span>' + escapeHtml(dim) + '</span>' +
          '<span class="bar"><i style="width:' + pct + '%"></i></span><b>' + val + '</b></div>';
      }).join('');
    }

    function listBlock(kind, arr) {
      var list = Array.isArray(arr) ? arr.filter(Boolean) : [];
      if (!list.length) return '';
      return '<ul class="cmp-list ' + kind + '">' + list.map(function (x) {
        return '<li><span>' + escapeHtml(x) + '</span></li>';
      }).join('') + '</ul>';
    }

    return '<div class="cmp-grid">' + items.map(function (it) {
      var key = registerDrawer(pane, {
        kicker: '方案详情',
        title: it.name || '未命名方案',
        tags: [it.badge, it.recommended ? '推荐' : ''].filter(Boolean),
        summary: it.summary || '',
        details: it.details || '',
        files: it.files || []
      });
      return '<div class="cmp-card" data-recommended="' + (it.recommended ? 'true' : 'false') + '">' +
        '<div class="cmp-head"><span class="cmp-name">' + escapeHtml(it.name || '') + '</span>' +
        (it.badge ? '<span class="badge">' + escapeHtml(it.badge) + '</span>' : '') +
        (it.recommended ? '<span class="cmp-chip">推荐</span>' : '') + '</div>' +
        scoreBlock(it) + listBlock('pros', it.pros) + listBlock('cons', it.cons) +
        (it.summary ? '<div class="cmp-summary">' + escapeHtml(it.summary) + '</div>' : '') +
        '<div class="cmp-foot"><button type="button" class="ghost-btn" data-drawer="' + key + '">查看详情</button></div>' +
        '</div>';
    }).join('') + '</div>';
  }

  function renderBento(pane, sec) {
    var cards = Array.isArray(sec.cards) ? sec.cards : [];
    if (!cards.length) return '';
    return '<div class="bento">' + cards.map(function (c) {
      var span = Number(c.span) === 2 ? 2 : 1;
      var key = registerDrawer(pane, {
        kicker: '卡片详情',
        title: c.title || '未命名卡片',
        tags: [c.tag, c.tone].filter(Boolean),
        summary: c.summary || '',
        details: c.details || '',
        files: c.files || []
      });
      return '<div class="bento-card" role="button" tabindex="0" data-tone="' + escapeAttr(c.tone || '') + '" data-drawer="' + key +
        '" style="--span:' + span + '">' +
        (c.tag ? '<div class="bc-tag">' + escapeHtml(c.tag) + '</div>' : '') +
        (c.metric ? '<div class="bc-metric">' + escapeHtml(c.metric) + '</div>' : '') +
        '<div class="bc-title">' + escapeHtml(c.title || '') + '</div>' +
        (c.summary ? '<div class="bc-summary">' + escapeHtml(c.summary) + '</div>' : '') +
        '</div>';
    }).join('') + '</div>';
  }

  function renderSteps(pane, sec) {
    var steps = Array.isArray(sec.steps) ? sec.steps : [];
    if (!steps.length) return '';
    return '<div class="step-track">' + steps.map(function (s, i) {
      var status = s.status || 'pending';
      var key = registerDrawer(pane, {
        kicker: '步骤详情',
        title: s.title || ('第 ' + (i + 1) + ' 步'),
        tags: [status],
        summary: s.summary || '',
        details: s.details || '',
        files: s.files || []
      });
      return '<div class="step" role="button" tabindex="0" data-status="' + escapeAttr(status) + '" data-drawer="' + key + '">' +
        '<span class="dot"></span><span class="line"></span>' +
        '<div class="step-num">' + escapeHtml(s.step !== undefined ? s.step : String(i + 1)) + '</div>' +
        '<div class="step-title">' + escapeHtml(s.title || '') + '</div>' +
        (s.summary ? '<div class="step-summary">' + escapeHtml(s.summary) + '</div>' : '') +
        '</div>';
    }).join('') + '</div>';
  }

  function tableRowKey(pane, row, columns) {
    var notes = [];
    columns.forEach(function (col) {
      var cell = TableCore.getCell(row, col.key);
      if (cell.note) notes.push((col.label || col.key) + '：' + cell.note);
    });
    var hasFiles = Array.isArray(row.files) && row.files.length > 0;
    if (!row.details && !row.summary && !hasFiles && !notes.length) return '';
    var details = row.details || '';
    if (notes.length) details = (details ? details + '\n\n' : '') + notes.map(function (line) {
      return '- ' + line;
    }).join('\n');
    var title = row.label || row.name || '';
    if (!title) {
      var first = TableCore.getCell(row, columns[0].key);
      title = first.text || row.id;
    }
    return registerDrawer(pane, {
      kicker: '表格行详情',
      title: title,
      tags: [row.is_baseline ? '基准' : '', row.emphasis ? '汇总' : ''].filter(Boolean),
      summary: row.summary || '',
      details: details,
      files: row.files || []
    });
  }

  function tableHeadHtml(state) {
    var columns = state.columns;
    var html = '<thead>';
    var groups = TableCore.normalizeGroups(columns, state.groups);
    if (groups.length) {
      html += '<tr class="tbl-group-row"><th class="tbl-act-head"></th>';
      var cursor = 0;
      groups.forEach(function (group) {
        if (group.start > cursor) {
          html += '<th colspan="' + (group.start - cursor) + '"></th>';
        }
        html += '<th colspan="' + group.span + '" class="tbl-group-th">' + escapeHtml(group.label) + '</th>';
        cursor = group.start + group.span;
      });
      if (cursor < columns.length) {
        html += '<th colspan="' + (columns.length - cursor) + '"></th>';
      }
      html += '</tr>';
    }
    html += '<tr><th class="tbl-act-head" scope="col"></th>';
    columns.forEach(function (col) {
      var active = state.sortKey === col.key;
      var align = TableCore.alignOf(col, state.rows);
      html += '<th scope="col" data-tcol="' + escapeAttr(col.key) + '"' +
        (active ? ' class="sorted"' : '') + ' style="text-align:' + align + '">' +
        escapeHtml(col.label === undefined || col.label === null ? col.key : col.label) +
        (col.unit ? ' <span class="tbl-unit">(' + escapeHtml(col.unit) + ')</span>' : '') +
        '<span class="tbl-sort">' + (active ? (state.sortDir === 'asc' ? '↑' : '↓') : '') + '</span></th>';
    });
    html += '</tr></thead>';
    return html;
  }

  function tableCellHtml(state, row, col, baseRow) {
    var cell = TableCore.getCell(row, col.key);
    var decimals = state.decimals[col.key];
    var text = TableCore.cellExportText(cell, col, decimals);
    var classes = [];
    var rank = state.ranks[col.key];
    if (cell.num !== null && rank && rank.best !== null && Math.abs(cell.num - rank.best) < 1e-9) {
      classes.push('tbl-best');
    } else if (cell.num !== null && rank && rank.second !== null && Math.abs(cell.num - rank.second) < 1e-9) {
      classes.push('tbl-second');
    }
    if (cell.note) classes.push('tbl-cell-note');
    var inner = escapeHtml(text);
    if (classes.length || cell.note) {
      inner = '<span class="' + classes.join(' ') + '"' +
        (cell.note ? ' title="' + escapeAttr(cell.note) + '"' : '') + '>' + inner + '</span>';
    }
    if (state.showDelta && baseRow && row.id !== state.baselineId && cell.num !== null) {
      var baseCell = TableCore.getCell(baseRow, col.key);
      var delta = TableCore.computeDelta(cell.num, baseCell.num, col, decimals);
      if (delta) {
        inner += '<span class="tbl-delta" data-tone="' + delta.tone + '">&#916; ' + escapeHtml(delta.text) + '</span>';
      }
    }
    return '<td style="text-align:' + TableCore.alignOf(col, state.rows) + '">' + inner + '</td>';
  }

  function tableBodyHtml(state) {
    var baseRow = null;
    if (state.baselineId) {
      for (var i = 0; i < state.rows.length; i++) {
        if (String(state.rows[i].id) === String(state.baselineId)) { baseRow = state.rows[i]; break; }
      }
    }
    var rows = TableCore.filterRows(state.rows, state.columns, state.query);
    if (state.sortKey) {
      rows = TableCore.sortRows(rows, state.sortKey, state.sortDir, state.baselineId ? [state.baselineId] : []);
    }
    var html = '';
    rows.forEach(function (row) {
      var classes = [];
      if (String(row.id) === String(state.baselineId)) classes.push('is-base');
      if (row.emphasis) classes.push('emph');
      if (row.divider_before) classes.push('divider');
      var key = state.rowKeys[row.id] || '';
      html += '<tr' + (classes.length ? ' class="' + classes.join(' ') + '"' : '') +
        (key ? ' data-drawer="' + key + '"' : '') + '>';
      html += '<td class="tbl-act">';
      if (String(row.id) === String(state.baselineId)) {
        html += '<span class="tbl-base-tag">基准</span>';
      } else {
        html += '<button type="button" class="tbl-base-btn" data-base-row="' + escapeAttr(row.id) + '">设为基准</button>';
      }
      html += '</td>';
      state.columns.forEach(function (col) { html += tableCellHtml(state, row, col, baseRow); });
      html += '</tr>';
    });
    if (!html) {
      html = '<tr><td class="tbl-empty" colspan="' + (state.columns.length + 1) + '">没有匹配的行</td></tr>';
    }
    return html;
  }

  function tableExportText(state, kind) {
    if (kind === 'latex') {
      return TableCore.toLatex(state.columns, state.rows, state.groups, { caption: state.caption });
    }
    if (kind === 'md') return TableCore.toMarkdown(state.columns, state.rows, {});
    return TableCore.toCsv(state.columns, state.rows);
  }

  function copyText(text) {
    return new Promise(function (resolve) {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        navigator.clipboard.writeText(text).then(function () { resolve(true); }, function () { resolve(false); });
      } else {
        resolve(false);
      }
    });
  }

  function showTableExport(card, state, kind) {
    var text = tableExportText(state, kind);
    var box = $('.tbl-export-box', card);
    var title = $('.tbl-export-title', card);
    if (title) {
      title.textContent = kind === 'latex' ? 'LaTeX · booktabs' : (kind === 'md' ? 'Markdown' : 'CSV');
    }
    var preview = $('.tbl-export-preview', card);
    if (preview) preview.textContent = text;
    if (box) box.hidden = false;
    copyText(text).then(function (ok) {
      toast(ok ? '已复制到剪贴板' : '剪贴板不可用，已显示导出内容');
    });
  }

  function renderTable(pane, sec) {
    var columns = (Array.isArray(sec.columns) ? sec.columns : []).map(function (col) {
      if (typeof col === 'string') return { key: col, label: col };
      return col;
    }).filter(function (col) { return col && col.key; });
    var rawRows = Array.isArray(sec.rows) ? sec.rows : [];
    if (!columns.length && rawRows.length && rawRows[0] && typeof rawRows[0] === 'object') {
      Object.keys(rawRows[0]).forEach(function (key) {
        if (['id', 'cells', 'details', 'files', 'summary', 'is_baseline', 'emphasis', 'divider_before'].indexOf(key) >= 0) return;
        columns.push({ key: key, label: key });
      });
    }
    if (!columns.length || !rawRows.length) return '';
    var rows = TableCore.normalizeRows(rawRows);
    var baselineId = TableCore.resolveBaselineId(rows, sec.baseline_id);
    var id = 't' + (++pane.viewer.tableSeq);
    var state = {
      columns: columns, rows: rows, groups: Array.isArray(sec.groups) ? sec.groups : [],
      baselineId: baselineId,
      showDelta: sec.show_delta === undefined ? !!baselineId : !!sec.show_delta,
      searchable: sec.searchable === undefined ? rows.length >= 6 : !!sec.searchable,
      query: '', sortKey: '', sortDir: 'desc',
      caption: sec.caption || '', note: sec.note || '',
      rowKeys: {}, decimals: {}, ranks: {}
    };
    columns.forEach(function (col) {
      state.decimals[col.key] = TableCore.inferDecimals(col, rows.map(function (row) {
        return TableCore.getCell(row, col.key).num;
      }));
    });
    state.ranks = TableCore.rankColumns(columns, rows);
    rows.forEach(function (row) {
      var key = tableRowKey(pane, row, columns);
      if (key) state.rowKeys[row.id] = key;
    });
    pane.viewer.tables[id] = state;
    var html = '<div class="tbl-card" data-tbl-id="' + id + '">' +
      '<div class="tbl-bar">' +
      (state.caption ? '<div class="tbl-caption">' + escapeHtml(state.caption) + '</div>' : '') +
      '<div class="tbl-tools">' +
      (state.searchable ? '<input type="search" class="tbl-search" placeholder="搜索行" aria-label="搜索表格">' : '') +
      '<button type="button" class="tbl-btn" data-tact="delta" aria-pressed="' + (state.showDelta ? 'true' : 'false') + '" title="与基准行对比">Δ 基准对比</button>' +
      '<button type="button" class="tbl-btn" data-tact="latex" title="复制 booktabs LaTeX">LaTeX</button>' +
      '<button type="button" class="tbl-btn" data-tact="md" title="复制 Markdown 表格">Markdown</button>' +
      '<button type="button" class="tbl-btn" data-tact="csv" title="复制 CSV">CSV</button>' +
      '</div></div>' +
      '<div class="tbl-scroll"><table class="exp-tbl">' + tableHeadHtml(state) +
      '<tbody>' + tableBodyHtml(state) + '</tbody></table></div>' +
      (state.note ? '<div class="tbl-note">' + escapeHtml(state.note) + '</div>' : '') +
      '<div class="tbl-export-box" hidden><div class="tbl-export-head">' +
      '<span class="tbl-export-title">导出</span>' +
      '<button type="button" class="tbl-btn tbl-export-close">关闭</button></div>' +
      '<pre class="tbl-export-preview"></pre></div>' +
      '</div>';
    return html;
  }

  function bindTables(pane) {
    $$('.tbl-card', pane.canvasRoot).forEach(function (card) {
      var id = card.getAttribute('data-tbl-id');
      var state = pane.viewer.tables[id];
      if (!state) return;
      var tbody = $('tbody', card);
      function syncDeltaButton() {
        var btn = $('[data-tact="delta"]', card);
        if (btn) btn.setAttribute('aria-pressed', state.showDelta ? 'true' : 'false');
      }
      function repaintBody() {
        if (tbody) tbody.innerHTML = tableBodyHtml(state);
        syncDeltaButton();
      }
      $$('th[data-tcol]', card).forEach(function (th) {
        th.addEventListener('click', function () {
          var key = th.getAttribute('data-tcol');
          if (state.sortKey === key) {
            state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
          } else {
            state.sortKey = key;
            state.sortDir = 'desc';
          }
          $$('th[data-tcol]', card).forEach(function (other) {
            var active = other.getAttribute('data-tcol') === state.sortKey;
            other.classList.toggle('sorted', active);
            var mark = $('.tbl-sort', other);
            if (mark) mark.textContent = active ? (state.sortDir === 'asc' ? '↑' : '↓') : '';
          });
          repaintBody();
        });
      });
      var search = $('.tbl-search', card);
      if (search) {
        search.addEventListener('input', function () {
          state.query = search.value;
          repaintBody();
        });
      }
      $$('[data-tact]', card).forEach(function (btn) {
        var act = btn.getAttribute('data-tact');
        if (act === 'delta') {
          btn.addEventListener('click', function () {
            state.showDelta = !state.showDelta;
            repaintBody();
          });
        } else if (act === 'latex' || act === 'md' || act === 'csv') {
          btn.addEventListener('click', function () { showTableExport(card, state, act); });
        }
      });
      var close = $('.tbl-export-close', card);
      if (close) {
        close.addEventListener('click', function () {
          var box = $('.tbl-export-box', card);
          if (box) box.hidden = true;
        });
      }
      card.addEventListener('click', function (ev) {
        var target = ev.target && ev.target.closest ? ev.target.closest('.tbl-base-btn') : null;
        if (!target) return;
        ev.preventDefault();
        ev.stopPropagation();
        state.baselineId = target.getAttribute('data-base-row') || '';
        state.showDelta = !!state.baselineId;
        repaintBody();
      });
      syncDeltaButton();
    });
  }

  function chartDrawerKeys(pane, sec, kind, state) {
    if (kind === 'bar' || kind === 'line') {
      (Array.isArray(sec.series) ? sec.series : []).forEach(function (series, index) {
        if (!series) return;
        var key = series.name === undefined || series.name === null || String(series.name) === ''
          ? 'S' + (index + 1) : String(series.name);
        if (!series.details && !series.summary && !(Array.isArray(series.files) && series.files.length)) return;
        state.seriesKeys[index] = registerDrawer(pane, {
          kicker: kind === 'line' ? '折线系列' : '柱状系列',
          title: key,
          tags: [series.unit].filter(Boolean),
          summary: series.summary || '',
          details: series.details || '',
          files: series.files || []
        });
      });
      return;
    }
    if (kind === 'scatter') {
      (Array.isArray(sec.points) ? sec.points : []).forEach(function (point, index) {
        if (!point) return;
        if (!point.details && !point.summary && !(Array.isArray(point.files) && point.files.length)) return;
        state.pointKeys[index] = registerDrawer(pane, {
          kicker: '散点详情',
          title: point.label || ('点 ' + (index + 1)),
          tags: [point.group, point.highlight ? '重点' : ''].filter(Boolean),
          summary: point.summary || '',
          details: point.details || '',
          files: point.files || []
        });
      });
      return;
    }
    if (kind === 'heatmap') {
      if (Array.isArray(sec.cell_details)) {
        sec.cell_details.forEach(function (line, rowIdx) {
          if (!Array.isArray(line)) return;
          line.forEach(function (cell, colIdx) {
            if (!cell || typeof cell !== 'object') return;
            if (!cell.details && !cell.summary && !(Array.isArray(cell.files) && cell.files.length)) return;
            state.cellKeys[rowIdx + ':' + colIdx] = registerDrawer(pane, {
              kicker: '热力格详情',
              title: (cell.label || ((cell.row_label || rowIdx) + ' × ' + (cell.col_label || colIdx))),
              tags: cell.status ? [cell.status] : [],
              summary: cell.summary || '',
              details: cell.details || '',
              files: cell.files || []
            });
          });
        });
      }
      (Array.isArray(sec.cells) ? sec.cells : []).forEach(function (cell, index) {
        if (!cell) return;
        if (!cell.details && !cell.summary && !(Array.isArray(cell.files) && cell.files.length)) return;
        state.sparseKeys[index] = registerDrawer(pane, {
          kicker: '热力格详情',
          title: cell.label || ('格 ' + (index + 1)),
          tags: cell.status ? [cell.status] : [],
          summary: cell.summary || '',
          details: cell.details || '',
          files: cell.files || []
        });
      });
    }
  }

  function renderChart(pane, sec) {
    var id = 'c' + (++pane.viewer.chartSeq);
    var kind = ChartCore.chartKind(sec);
    if (['bar', 'line', 'scatter', 'heatmap'].indexOf(kind) < 0) kind = 'bar';
    var state = { sec: sec, kind: kind, hidden: {}, seriesKeys: {}, pointKeys: {}, cellKeys: {}, sparseKeys: {} };
    chartDrawerKeys(pane, sec, kind, state);
    pane.viewer.charts[id] = state;
    return '<div class="chart-card" data-chart-id="' + id + '" data-chart-kind="' + escapeAttr(kind) + '">' +
      '<div class="chart-stage"><div class="chart-svg-host"></div><div class="chart-tip" hidden></div></div>' +
      '<div class="chart-legend"></div>' +
      (sec.note ? '<div class="chart-note">' + escapeHtml(sec.note) + '</div>' : '') +
      '</div>';
  }

  function chartSpecFor(state) {
    var spec = {};
    Object.keys(state.sec).forEach(function (key) { spec[key] = state.sec[key]; });
    spec.chart_type = state.kind;
    spec.hiddenSeries = state.hidden;
    spec.title = state.sec.chart_title || '';
    return spec;
  }

  function chartGridHtml(layout) {
    var plot = layout.plot;
    var out = '';
    (layout.yAxis && layout.yAxis.ticks ? layout.yAxis.ticks : []).forEach(function (tick) {
      out += '<line class="chart-grid" x1="' + plot.x + '" y1="' + tick.y + '" x2="' +
        (plot.x + plot.w) + '" y2="' + tick.y + '"></line>';
      out += '<text class="chart-tick" x="' + (plot.x - 8) + '" y="' + (tick.y + 4) +
        '" text-anchor="end">' + escapeHtml(tick.label) + '</text>';
    });
    if (layout.yAxis && layout.yAxis.label) {
      var cx = Math.max(12, plot.x - 46);
      var cy = plot.y + plot.h / 2;
      out += '<text class="chart-axis-label" x="' + cx + '" y="' + cy +
        '" text-anchor="middle" transform="rotate(-90 ' + cx + ' ' + cy + ')">' +
        escapeHtml(layout.yAxis.label) + '</text>';
    }
    return out;
  }

  function chartRefHtml(layout) {
    var plot = layout.plot;
    return (layout.refLines || []).map(function (ref) {
      var out = '<line class="chart-ref" x1="' + plot.x + '" y1="' + ref.y + '" x2="' +
        (plot.x + plot.w) + '" y2="' + ref.y + '"></line>';
      if (ref.label) {
        out += '<text class="chart-ref-label" x="' + (plot.x + plot.w) + '" y="' + (ref.y - 5) +
          '" text-anchor="end">' + escapeHtml(ref.label) + '</text>';
      }
      return out;
    }).join('');
  }

  function chartXLabelsHtml(layout) {
    return (layout.xLabels || []).map(function (label) {
      return '<text class="chart-tick" x="' + label.x + '" y="' + label.y +
        '" text-anchor="' + (label.anchor || 'middle') + '">' + escapeHtml(label.text) + '</text>';
    }).join('');
  }

  function chartDrawerAttr(state, key) {
    return key ? ' data-drawer="' + key + '"' : '';
  }

  function chartSvgHtml(layout, state) {
    var plot = layout.plot;
    var out = '<svg class="chart-svg" viewBox="0 0 ' + layout.width + ' ' + layout.height +
      '" preserveAspectRatio="xMidYMid meet" role="img" aria-label="' +
      escapeAttr(layout.title || layout.kind) + '">';
    if (layout.title) {
      out += '<text class="chart-axis-label" x="' + plot.x + '" y="15">' + escapeHtml(layout.title) + '</text>';
    }
    if (layout.kind === 'heatmap') {
      layout.cells.forEach(function (cell) {
        var key = '';
        if (cell.sparseIndex !== null && cell.sparseIndex !== undefined) key = state.sparseKeys[cell.sparseIndex] || '';
        else key = state.cellKeys[cell.row + ':' + cell.col] || '';
        out += '<rect class="chart-cell" data-tone="' + escapeAttr(cell.tone) + '" x="' + cell.x +
          '" y="' + cell.y + '" width="' + cell.w + '" height="' + cell.h + '" rx="2" fill-opacity="' +
          cell.alpha + '" data-tip="' + escapeAttr(cell.tip) + '"' + chartDrawerAttr(state, key) + '></rect>';
        if (cell.text) {
          out += '<text class="chart-cell-text" x="' + (cell.x + cell.w / 2) + '" y="' +
            (cell.y + cell.h / 2 + 4) + '" text-anchor="middle">' + escapeHtml(cell.text) + '</text>';
        }
      });
      (layout.yLabels || []).forEach(function (label) {
        out += '<text class="chart-tick" x="' + label.x + '" y="' + label.y +
          '" text-anchor="' + (label.anchor || 'end') + '">' + escapeHtml(label.text) + '</text>';
      });
      out += chartXLabelsHtml(layout);
      out += '</svg>';
      return out;
    }
    out += chartGridHtml(layout);
    if (layout.kind === 'scatter') {
      (layout.xAxis && layout.xAxis.ticks ? layout.xAxis.ticks : []).forEach(function (tick) {
        out += '<line class="chart-grid" x1="' + tick.x + '" y1="' + plot.y + '" x2="' +
          tick.x + '" y2="' + (plot.y + plot.h) + '"></line>';
      });
      if (layout.xAxis && layout.xAxis.label) {
        out += '<text class="chart-axis-label" x="' + (plot.x + plot.w / 2) + '" y="' +
          (layout.height - 8) + '" text-anchor="middle">' + escapeHtml(layout.xAxis.label) + '</text>';
      }
    }
    out += chartRefHtml(layout);
    if (layout.kind === 'bar') {
      layout.bars.forEach(function (bar) {
        var key = state.seriesKeys[bar.seriesIndex] || '';
        out += '<rect class="chart-bar" x="' + bar.x + '" y="' + bar.y + '" width="' + bar.w +
          '" height="' + bar.h + '" rx="2" fill="' + escapeAttr(bar.color) + '" data-tip="' +
          escapeAttr(bar.tip) + '"' + chartDrawerAttr(state, key) + '></rect>';
      });
    } else if (layout.kind === 'line') {
      layout.lines.forEach(function (line) {
        if (!line.d) return;
        out += '<path class="chart-line' + (line.dashed ? ' dashed' : '') + '" stroke="' +
          escapeAttr(line.color) + '" d="' + line.d + '"></path>';
      });
      layout.points.forEach(function (point) {
        var key = state.seriesKeys[point.seriesIndex] || '';
        out += '<circle class="chart-point" cx="' + point.x + '" cy="' + point.y +
          '" r="3.2" fill="' + escapeAttr(point.color) + '" data-tip="' +
          escapeAttr(point.tip) + '"' + chartDrawerAttr(state, key) + '></circle>';
      });
    } else if (layout.kind === 'scatter') {
      if (layout.paretoPath) {
        out += '<path class="chart-pareto" d="' + layout.paretoPath + '"></path>';
      }
      layout.points.forEach(function (point) {
        var key = state.pointKeys[point.index] || '';
        out += '<circle class="chart-point" cx="' + point.x + '" cy="' + point.y + '" r="' +
          point.r + '" fill="' + escapeAttr(point.color) + '" fill-opacity="0.85" data-tip="' +
          escapeAttr(point.tip) + '"' + chartDrawerAttr(state, key) + '></circle>';
        if (point.label) {
          out += '<text class="chart-point-label" x="' + point.labelX + '" y="' + point.labelY +
            '" text-anchor="' + (point.labelAnchor || 'start') + '">' + escapeHtml(point.label) + '</text>';
        }
      });
    }
    out += chartXLabelsHtml(layout);
    out += '</svg>';
    return out;
  }

  function chartLegendHtml(layout, state) {
    if (layout.kind === 'heatmap') {
      var scale = layout.scale || {};
      return '<span class="chart-scale-label">' + escapeHtml(scale.mode || 'max_good') + ' · ' +
        escapeHtml(layout.scaleLabel || '') + '</span>';
    }
    return (layout.legend || []).map(function (item) {
      var key = item.key || item.name || '';
      var hidden = state.hidden[key] ? 'true' : 'false';
      return '<button type="button" class="chart-pill" data-series="' + escapeAttr(key) +
        '" data-hidden="' + hidden + '" aria-pressed="' + (hidden === 'true' ? 'false' : 'true') + '">' +
        '<i style="background:' + escapeAttr(item.color || 'var(--accent)') + '"></i>' +
        escapeHtml(item.name || key) + '</button>';
    }).join('');
  }

  function paintChart(pane, card) {
    var id = card.getAttribute('data-chart-id');
    var state = pane.viewer.charts[id];
    if (!state) return;
    var layout = ChartCore.layoutChart(chartSpecFor(state));
    var host = $('.chart-svg-host', card);
    if (host) host.innerHTML = chartSvgHtml(layout, state);
    var legend = $('.chart-legend', card);
    if (legend) {
      legend.innerHTML = chartLegendHtml(layout, state);
      if (layout.kind !== 'heatmap') {
        $$('.chart-pill', legend).forEach(function (pill) {
          pill.addEventListener('click', function () {
            var key = pill.getAttribute('data-series');
            if (!key) return;
            if (state.hidden[key]) delete state.hidden[key];
            else state.hidden[key] = true;
            paintChart(pane, card);
          });
        });
      }
    }
  }

  function bindCharts(pane) {
    $$('.chart-card', pane.canvasRoot).forEach(function (card) {
      paintChart(pane, card);
      var stage = $('.chart-stage', card);
      var tip = $('.chart-tip', card);
      if (!stage || !tip) return;
      stage.addEventListener('mousemove', function (ev) {
        var target = ev.target && ev.target.closest ? ev.target.closest('[data-tip]') : null;
        if (!target) { tip.hidden = true; return; }
        tip.textContent = target.getAttribute('data-tip') || '';
        tip.hidden = false;
        var box = stage.getBoundingClientRect();
        var x = ev.clientX - box.left;
        var y = ev.clientY - box.top;
        tip.style.left = Math.round(Math.max(6, Math.min(box.width - 160, x + 14))) + 'px';
        tip.style.top = Math.round(Math.max(6, y - 34)) + 'px';
      });
      stage.addEventListener('mouseleave', function () { tip.hidden = true; });
    });
  }

  function renderProse(pane, sec) {
    var text = '';
    if (typeof sec.content === 'string') text = sec.content;
    else if (typeof sec.markdown === 'string') text = sec.markdown;
    else if (typeof sec.text === 'string') text = sec.text;
    else if (typeof sec.prose === 'string') text = sec.prose;
    if (!text.trim()) return '';
    return '<div class="prose-card markdown-body"' +
      (sec.tone ? ' data-tone="' + escapeAttr(sec.tone) + '"' : '') + '>' +
      renderMarkdown(text) + '</div>';
  }

  function renderSection(pane, sec) {
    if (!sec || typeof sec !== 'object') return '';
    var type = sec.type;
    if (!type) {
      if (sec.nodes) type = 'graph';
      else if (sec.cards) type = 'bento';
      else if (sec.steps) type = 'steps';
      else if (sec.kpis) type = 'kpis';
      else if (sec.columns && sec.rows) type = 'table';
      else if (sec.chart_type || sec.subtype || ((sec.matrix || sec.cells) && sec.x_labels)) type = 'chart';
      else if (sec.items) type = 'comparison';
      else if (typeof sec.prose === 'string' || typeof sec.markdown === 'string') type = 'prose';
    }
    var body = '';
    if (type === 'kpis') body = renderKpis(sec);
    else if (type === 'graph') body = renderGraph(pane, sec);
    else if (type === 'comparison') body = renderComparison(pane, sec);
    else if (type === 'bento') body = renderBento(pane, sec);
    else if (type === 'steps') body = renderSteps(pane, sec);
    else if (type === 'table') body = renderTable(pane, sec);
    else if (type === 'chart') body = renderChart(pane, sec);
    else if (type === 'prose') body = renderProse(pane, sec);
    else return '';
    if (!body) return '';
    var span = Number(sec.span) === 1 ? 1 : 2;
    var title = sec.title ? '<div class="cv-sec-title">' + escapeHtml(sec.title) + '</div>' : '';
    return '<section class="cv-section" data-type="' + escapeAttr(type) + '" data-span="' + span + '">' + title + body + '</section>';
  }

  function renderCanvas(pane) {
    var root = pane.canvasRoot;
    var data = pane.viewer.canvasData;
    if (!data) {
      root.innerHTML = '<div class="empty" style="height:60vh">还没有内容。让 Agent 发布一条回复即可在此看到。</div>';
      return;
    }
    if (pane.viewer.subview === 'raw') {
      root.innerHTML = '<pre class="raw-json">' + escapeHtml(JSON.stringify(data, null, 2)) + '</pre>';
      return;
    }
    pane.viewer.drawer = {};
    pane.viewer.drawerSeq = 0;
    pane.viewer.tables = {};
    pane.viewer.tableSeq = 0;
    pane.viewer.charts = {};
    pane.viewer.chartSeq = 0;
    var html = '';
    if (data.headline) html += '<h1 class="canvas-headline">' + escapeHtml(data.headline) + '</h1>';
    if (data.badges.length) {
      html += '<div class="badges">' + data.badges.map(function (b) {
        var text = typeof b === 'string' ? b : (b && (b.text || b.label)) || '';
        var tone = (b && b.tone) || '';
        return '<span class="badge" data-tone="' + escapeAttr(tone) + '">' + escapeHtml(text) + '</span>';
      }).join('') + '</div>';
    }
    html += '<div class="cv-sections">';
    if (!data.sections.length) {
      html += '<div class="cv-section"><div class="empty" style="height:200px">画布为空：请提供 sections / nodes / cards。</div></div>';
    } else {
      data.sections.forEach(function (sec) { html += renderSection(pane, sec); });
    }
    html += '</div>';
    root.innerHTML = html;
    wrapTables(root);
    markExternalLinks(root);
    bindGraphHover(pane);
    bindGraphTools(pane);
    bindTables(pane);
    bindCharts(pane);
    requestAnimationFrame(function () { fitGraphs(pane); });
  }

  function neighbors(startId, adj) {
    var seen = {};
    var stack = [startId];
    while (stack.length) {
      var cur = stack.pop();
      (adj[cur] || []).forEach(function (nxt) {
        if (!seen[nxt]) { seen[nxt] = true; stack.push(nxt); }
      });
    }
    return seen;
  }

  var GRAPH_MIN_SCALE = 0.35;
  var GRAPH_MAX_SCALE = 2.5;

  /* transform: scale() 缩放：容器撑开缩放后的宽高，避免出界与被裁切 */
  function applyGraphScale(wrap, scale) {
    var inner = $('.graph-inner', wrap);
    var box = $('.graph-scale-box', wrap);
    if (!inner || !box) return;
    var w = parseFloat(wrap.dataset.w) || 1;
    var h = parseFloat(wrap.dataset.h) || 1;
    var s = Math.max(GRAPH_MIN_SCALE, Math.min(GRAPH_MAX_SCALE, scale || 1));
    inner.style.transform = 'scale(' + s + ')';
    box.style.width = Math.round(w * s) + 'px';
    box.style.height = Math.round(h * s) + 'px';
    wrap.dataset.scale = String(s);
    var block = wrap.parentNode;
    var zoom = block ? $('[data-role="zoom"]', block) : null;
    if (zoom) zoom.textContent = Math.round(s * 100) + '%';
  }

  function fitGraph(wrap) {
    var w = parseFloat(wrap.dataset.w) || 1;
    var avail = wrap.clientWidth - 2;
    var s = avail > 120 ? Math.min(1, avail / w) : 1;
    applyGraphScale(wrap, Math.max(GRAPH_MIN_SCALE, s));
  }

  function fitGraphs(pane) {
    $$('.graph-wrap', pane.canvasRoot).forEach(function (wrap) {
      if (wrap.dataset.mode === 'manual') return;
      fitGraph(wrap);
    });
  }

  function bindGraphTools(pane) {
    $$('.graph-block', pane.canvasRoot).forEach(function (block) {
      var wrap = $('.graph-wrap', block);
      if (!wrap) return;
      $$('[data-gact]', block).forEach(function (btn) {
        btn.addEventListener('click', function () {
          var act = btn.dataset.gact;
          if (act === 'fit') {
            wrap.dataset.mode = 'fit';
            fitGraph(wrap);
            return;
          }
          var cur = parseFloat(wrap.dataset.scale) || 1;
          wrap.dataset.mode = 'manual';
          if (act === 'reset') applyGraphScale(wrap, 1);
          else if (act === 'in') applyGraphScale(wrap, cur * 1.15);
          else if (act === 'out') applyGraphScale(wrap, cur / 1.15);
        });
      });
    });
  }

  function highlightGraph(wrap, startId) {
    var edges;
    try { edges = JSON.parse(wrap.dataset.edges || '[]'); }
    catch (err) { edges = []; }
    var nodes = $$('.g-node', wrap);
    var paths = $$('.g-edge', wrap);
    var pills = $$('.g-edge-pill', wrap);
    if (!startId) {
      nodes.forEach(function (n) { n.classList.remove('hl', 'dim'); });
      paths.forEach(function (p) { p.classList.remove('hl', 'dim'); });
      pills.forEach(function (p) { p.classList.remove('hl', 'dim'); });
      return;
    }
    var fwd = {};
    var rev = {};
    edges.forEach(function (e) {
      (fwd[e.from] = fwd[e.from] || []).push(e.to);
      (rev[e.to] = rev[e.to] || []).push(e.from);
    });
    var related = { };
    related[startId] = true;
    Object.keys(neighbors(startId, fwd)).forEach(function (k) { related[k] = true; });
    Object.keys(neighbors(startId, rev)).forEach(function (k) { related[k] = true; });

    nodes.forEach(function (n) {
      var id = n.dataset.node;
      var on = related[id] === true;
      n.classList.toggle('hl', on);
      n.classList.toggle('dim', !on);
    });
    paths.forEach(function (p) {
      var on = related[p.dataset.from] === true && related[p.dataset.to] === true;
      p.classList.toggle('hl', on);
      p.classList.toggle('dim', !on);
    });
    pills.forEach(function (p) {
      var on = related[p.dataset.from] === true && related[p.dataset.to] === true;
      p.classList.toggle('hl', on);
      p.classList.toggle('dim', !on);
    });
  }

  function bindGraphHover(pane) {
    $$('.graph-wrap', pane.canvasRoot).forEach(function (wrap) {
      $$('.g-node', wrap).forEach(function (node) {
        node.addEventListener('mouseenter', function () { highlightGraph(wrap, node.dataset.node); });
        node.addEventListener('mouseleave', function () { highlightGraph(wrap, null); });
      });
    });
  }

  /* ---------- 文档装载 ---------- */

  function setPaneHeader(pane, doc, hasContent) {
    var title = doc.title || '未命名回复';
    var sub = doc.updated_at ? '更新于 ' + fmtTime(doc.updated_at) : '等待发布';
    if (doc.session_label) sub = doc.session_label + ' · ' + sub;
    if (doc.kind === 'history') sub = '历史记录 · ' + String(doc.format || 'md').toUpperCase() + ' · ' + fmtTime(doc.updated_at);
    if (hasContent) sub += ' · ' + String(doc.content || '').length + ' 字符 · ' + String(doc.format || 'md').toUpperCase();
    pane.docTitle.textContent = title;
    pane.docSub.textContent = sub;
    if (pane.name === 'primary') updateDocumentTitle();
  }

  /* 单一渲染入口：主窗格与分屏窗格共用同一套画布 / Markdown / Artifact 逻辑 */
  function renderIntoPane(pane, doc, opts) {
    opts = opts || {};
    var v = pane.viewer;
    v.content = doc.content || '';
    v.format = doc.format || 'md';
    v.title = doc.title || '';
    v.currentFile = doc.filename || null;
    v.artifactSrc = '';
    v.canvasData = null;
    v.drawer = {};
    v.drawerSeq = 0;
    v.seq = Number(doc.seq) || 0;
    v.mtime = doc.mtime || 0;
    v.sessionId = doc.session_id || pane.sessionId || '';
    v.replyId = doc.reply_id || '';
    if (v.sessionId) pane.sessionId = v.sessionId;
    pane.replyId = v.replyId;
    pane.viewingHistory = !!opts.history;

    var hasContent = !!(v.content && v.content.trim());
    var mode = v.format;
    if (mode === 'json') {
      if (!hasContent) {
        mode = 'md';
      } else {
        try {
          var parsed = JSON.parse(v.content);
          if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
            v.canvasData = normalizeCanvas(parsed);
            mode = 'canvas';
          } else {
            mode = 'md';
          }
        } catch (err) {
          mode = 'md';
        }
      }
    }

    setMode(pane, mode);
    setPaneHeader(pane, doc, hasContent);

    if (mode === 'md') {
      pane.empty.hidden = hasContent;
      pane.content.hidden = !hasContent;
      if (hasContent) {
        renderReading(pane, v.content);
        renderMindmap(pane, v.content);
      } else {
        pane.content.innerHTML = '';
      }
    }
    applySubview(pane);
  }
  /* ---------- 会话栏 / 未读 ---------- */

  function saveSeen() {
    try { localStorage.setItem('vr.lastSeen', JSON.stringify(app.lastSeen)); } catch (err) { /* ignore */ }
  }

  function markSeen(sessionId, seq) {
    if (!sessionId) return;
    var current = Number(app.lastSeen[sessionId] || 0);
    if (Number(seq || 0) > current) {
      app.lastSeen[sessionId] = Number(seq || 0);
      saveSeen();
      renderRail();
    }
  }

  function unreadCount(session) {
    var seen = Number(app.lastSeen[session.id] || 0);
    if (Number(session.seq || 0) <= seen) return 0;
    var rows = app.replies[session.id];
    if (rows && rows.length) {
      var unread = rows.filter(function (r) { return Number(r.seq || 0) > seen; }).length;
      if (unread) return unread;
    }
    return 1;
  }

  function totalUnread() {
    var total = 0;
    app.sessions.forEach(function (s) { total += unreadCount(s); });
    return total;
  }

  /* 标题栏显示跨会话未读总数：(N) 标题 · Visual Reply */
  function updateDocumentTitle() {
    var base = panes.primary.viewer.title || '';
    var unread = totalUnread();
    var prefix = unread > 0 ? '(' + unread + ') ' : '';
    document.title = base ? prefix + base + ' · Visual Reply' : prefix + 'Visual Reply';
  }

  function railEmpty(text) {
    var box = document.createElement('div');
    box.className = 'rail-empty';
    box.textContent = text;
    return box;
  }

  function railButton(label, title, onClick) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = label;
    btn.title = title;
    btn.addEventListener('click', onClick);
    return btn;
  }

  function saveRailState() {
    try {
      localStorage.setItem('vr.collapsedGroups', JSON.stringify(app.collapsedGroups));
      localStorage.setItem('vr.expandedSessions', JSON.stringify(app.expandedSessions));
    } catch (err) { /* ignore */ }
  }

  function railProjectKey(session) {
    return String((session && session.project) || 'workspace').trim() || 'workspace';
  }

  function railGroupKey(session) {
    return session.pinned ? '__pinned__' : railProjectKey(session);
  }

  /* 显式展开状态优先；未记录时只有当前焦点会话默认展开。 */
  function isSessionExpanded(sessionId) {
    if (Object.prototype.hasOwnProperty.call(app.expandedSessions, sessionId)) {
      return !!app.expandedSessions[sessionId];
    }
    return panes.primary.sessionId === sessionId;
  }

  function railGroupHead(group, pinned) {
    var collapsed = !!app.collapsedGroups[group.key];
    var head = document.createElement('button');
    head.type = 'button';
    head.className = 'rail-group-head';
    head.dataset.groupKey = group.key;
    head.setAttribute('aria-expanded', String(!collapsed));

    var chevron = document.createElement('span');
    chevron.className = 'rail-chevron';
    chevron.textContent = collapsed ? '▸' : '▾';
    head.appendChild(chevron);

    var label = document.createElement('span');
    label.className = 'rail-group-label';
    label.textContent = pinned ? '★ ' + group.label : group.label;
    head.appendChild(label);

    var count = document.createElement('span');
    count.className = 'rail-group-count';
    count.textContent = String(group.sessions.length);
    head.appendChild(count);

    if (group.unreadTotal > 0) {
      var unread = document.createElement('span');
      unread.className = 'rail-group-unread';
      unread.textContent = group.unreadTotal > 9 ? '9+' : String(group.unreadTotal);
      unread.title = group.unreadTotal + ' 条未读';
      head.appendChild(unread);
    }

    head.addEventListener('click', function () {
      if (collapsed) delete app.collapsedGroups[group.key];
      else app.collapsedGroups[group.key] = true;
      saveRailState();
      renderRail();
    });
    return head;
  }

  function railReplyHint(text) {
    var hint = document.createElement('div');
    hint.className = 'rail-reply-hint';
    hint.textContent = text;
    return hint;
  }

  function sessionReplies(session) {
    var box = document.createElement('div');
    box.className = 'rail-replies';
    box.setAttribute('role', 'group');
    box.dataset.sid = session.id;

    var rows = app.replies[session.id];
    if (!rows) {
      box.appendChild(railReplyHint('加载回复中…'));
      if (!app.loadingReplies[session.id]) {
        loadSessionReplies(session.id, false).then(function () { renderRail(); });
      }
      return box;
    }

    var ordered = RailTreeCore.sortRepliesDesc(rows);
    if (!ordered.length) {
      box.appendChild(railReplyHint('暂无回复'));
      return box;
    }

    var total = ordered.length;
    ordered.forEach(function (row, index) {
      var number = total - index;
      var unread = Number(row.seq || 0) > Number(app.lastSeen[session.id] || 0);

      var item = document.createElement('div');
      item.className = 'rail-reply';
      item.dataset.sid = session.id;
      item.dataset.rid = row.reply_id;
      item.dataset.active = String(
        panes.primary.sessionId === session.id && panes.primary.replyId === row.reply_id);
      item.dataset.unread = String(unread);
      item.setAttribute('role', 'treeitem');
      item.setAttribute('aria-level', '3');
      item.tabIndex = 0;
      item.title = '#' + number + ' · ' + (row.title || row.reply_id || '');

      var num = document.createElement('span');
      num.className = 'rail-reply-num';
      num.textContent = '#' + number;
      item.appendChild(num);

      var main = document.createElement('span');
      main.className = 'rail-reply-main';
      var line = document.createElement('span');
      line.className = 'rail-reply-line';
      var title = document.createElement('span');
      title.className = 'rail-reply-title';
      title.textContent = row.title || row.reply_id || '未命名回复';
      line.appendChild(title);
      if (index === 0) {
        var tag = document.createElement('span');
        tag.className = 'rail-reply-tag';
        tag.textContent = '最新';
        line.appendChild(tag);
      }
      main.appendChild(line);
      var meta = document.createElement('span');
      meta.className = 'rail-reply-meta';
      meta.textContent = String(row.format || 'md').toUpperCase() +
        ' · ' + fmtTime(row.updated_at || row.created_at);
      main.appendChild(meta);
      item.appendChild(main);

      if (unread) {
        var dot = document.createElement('span');
        dot.className = 'rail-reply-unread';
        dot.title = '未读';
        item.appendChild(dot);
      }

      function openReply(ev) {
        if (ev) ev.stopPropagation();
        closeDrawer();
        if (panes.primary.sessionId === session.id) {
          loadReply(panes.primary, session.id, row.reply_id);
        } else {
          focusSession(session.id, { reply: row.reply_id });
        }
      }
      item.addEventListener('click', openReply);
      item.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          openReply(ev);
        }
      });
      box.appendChild(item);
    });
    return box;
  }

  function sessionTreeNode(session, opts) {
    opts = opts || {};
    var inPinned = !!opts.inPinned;
    var expanded = isSessionExpanded(session.id);

    var node = document.createElement('div');
    node.className = 'rail-node';
    node.dataset.sid = session.id;

    var card = document.createElement('div');
    card.className = 'rail-card';
    card.dataset.sid = session.id;
    card.dataset.active = String(panes.primary.sessionId === session.id);
    card.setAttribute('role', 'treeitem');
    card.setAttribute('aria-level', '2');
    card.setAttribute('aria-expanded', String(expanded));
    card.tabIndex = 0;
    card.title = session.label + (session.project ? ' · ' + session.project : '');

    var accent = document.createElement('span');
    accent.className = 'rail-accent';
    accent.style.background = session.color || 'var(--accent)';
    card.appendChild(accent);

    var caret = document.createElement('button');
    caret.type = 'button';
    caret.className = 'rail-caret';
    caret.textContent = expanded ? '▾' : '▸';
    caret.title = expanded ? '收起回复' : '展开回复';
    caret.setAttribute('aria-label', (expanded ? '收起 ' : '展开 ') + (session.label || session.id));
    caret.addEventListener('click', function (ev) {
      ev.stopPropagation();
      app.expandedSessions[session.id] = !isSessionExpanded(session.id);
      saveRailState();
      if (!app.replies[session.id]) {
        loadSessionReplies(session.id, false).then(function () { renderRail(); });
      }
      renderRail();
    });
    card.appendChild(caret);

    var main = document.createElement('div');
    main.className = 'rail-main';
    var top = document.createElement('div');
    top.className = 'rail-top';
    var label = document.createElement('span');
    label.className = 'rail-label';
    label.textContent = inPinned
      ? (session.label || session.id)
      : RailTreeCore.stripProjectPrefix(session);
    top.appendChild(label);
    if (inPinned && session.project) {
      var pill = document.createElement('span');
      pill.className = 'rail-project-pill';
      pill.textContent = session.project;
      pill.title = session.project;
      top.appendChild(pill);
    }
    if (session.client) {
      var client = document.createElement('span');
      client.className = 'rail-client';
      client.textContent = session.client;
      top.appendChild(client);
    }
    var sub = document.createElement('div');
    sub.className = 'rail-sub';
    sub.textContent = session.last_title || '暂无回复';
    var meta = document.createElement('div');
    meta.className = 'rail-meta';
    meta.textContent = (session.reply_count || 0) + ' 条 · ' + fmtTime(session.updated_at);
    main.appendChild(top);
    main.appendChild(sub);
    main.appendChild(meta);
    card.appendChild(main);

    var unread = unreadCount(session);
    if (unread) {
      var badge = document.createElement('span');
      badge.className = 'rail-unread';
      badge.textContent = unread > 9 ? '9+' : String(unread);
      card.appendChild(badge);
    }

    var actions = document.createElement('div');
    actions.className = 'rail-actions';
    actions.appendChild(railButton(session.pinned ? '★' : '☆', session.pinned ? '取消置顶' : '置顶', function (ev) {
      ev.stopPropagation();
      togglePin(session);
    }));
    actions.appendChild(railButton('改', '重命名会话', function (ev) {
      ev.stopPropagation();
      renameSession(session);
    }));
    actions.appendChild(railButton('◫', '在分屏中打开', function (ev) {
      ev.stopPropagation();
      openSplit(session.id);
    }));
    card.appendChild(actions);

    function openSession(ev) {
      if (ev) ev.stopPropagation();
      focusSession(session.id);
    }
    card.addEventListener('click', openSession);
    card.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        openSession(ev);
      }
    });
    node.appendChild(card);

    if (expanded) node.appendChild(sessionReplies(session));
    return node;
  }

  function railGroup(group, pinned) {
    var wrap = document.createElement('section');
    wrap.className = 'rail-group' + (pinned ? ' rail-group-pinned' : '');
    wrap.dataset.groupKey = group.key;
    wrap.appendChild(railGroupHead(group, pinned));

    if (!app.collapsedGroups[group.key]) {
      var body = document.createElement('div');
      body.className = 'rail-group-body';
      body.setAttribute('role', 'group');
      group.sessions.forEach(function (session) {
        body.appendChild(sessionTreeNode(session, { inPinned: pinned }));
      });
      wrap.appendChild(body);
    }
    return wrap;
  }

  function renderRail() {
    updateDocumentTitle();
    var list = $('#rail-list');
    var feed = $('#feed-list');
    var isFeed = app.railView === 'feed';
    list.hidden = isFeed;
    feed.hidden = !isFeed;
    $('#rail-title').textContent = isFeed ? '全局时间线'
      : ('会话' + (app.sessions.length ? ' · ' + app.sessions.length : ''));

    if (isFeed) { renderFeed(); return; }

    list.innerHTML = '';
    var tree = RailTreeCore.buildRailTree(app.sessions, {
      filter: app.filter,
      unreadCountOf: unreadCount
    });

    if (!tree.totalCount) {
      list.appendChild(railEmpty(app.sessions.length ? '没有符合筛选条件的会话' : '还没有会话。让 Agent 发布一条回复即可。'));
      return;
    }
    if (tree.pinned) list.appendChild(railGroup(tree.pinned, true));
    tree.projects.forEach(function (group) { list.appendChild(railGroup(group, false)); });
  }

  function renderFeed() {
    var box = $('#feed-list');
    fetchJSON('/api/feed?limit=60').then(function (data) {
      var items = data.items || [];
      box.innerHTML = '';
      if (!items.length) {
        box.appendChild(railEmpty('还没有跨会话记录。'));
        return;
      }
      items.forEach(function (item) {
        var row = document.createElement('div');
        row.className = 'feed-item';
        var seq = document.createElement('span');
        seq.className = 'feed-seq';
        seq.textContent = item.legacy ? 'v3' : '#' + (item.seq || 0);
        var main = document.createElement('div');
        main.className = 'feed-main';
        var title = document.createElement('div');
        title.className = 'feed-title';
        title.textContent = item.title || item.reply_id || item.filename || '未命名回复';
        var sub = document.createElement('div');
        sub.className = 'feed-sub';
        sub.textContent = (item.session_label || (item.legacy ? '历史归档' : '')) +
          ' · ' + String(item.format || 'md').toUpperCase() + ' · ' + fmtTime(item.updated_at);
        main.appendChild(title);
        main.appendChild(sub);
        row.appendChild(seq);
        row.appendChild(main);
        row.addEventListener('click', function () {
          if (item.legacy) loadLegacyHistory(item.filename);
          else focusSession(item.session_id, { reply: item.reply_id });
        });
        box.appendChild(row);
      });
    }).catch(function () {
      box.innerHTML = '';
      box.appendChild(railEmpty('全局时间线加载失败。'));
    });
  }

  function setRailView(view) {
    app.railView = view;
    $('#feed-btn').textContent = view === 'feed' ? '返回会话列表' : '全局时间线';
    renderRail();
  }

  function refitPanes() {
    setTimeout(function () {
      fitGraphs(panes.primary);
      fitGraphs(panes.secondary);
      fitMindmap(panes.primary);
      fitMindmap(panes.secondary);
    }, 210);
  }

  function toggleRail() {
    var rail = $('#session-rail');
    var open = rail.dataset.open !== 'false';
    rail.dataset.open = String(!open);
    try { localStorage.setItem('vr.rail', open ? '0' : '1'); } catch (err) { /* ignore */ }
    refitPanes();
  }

  /* ---------- 数据加载 ---------- */

  function refreshSessions() {
    return fetchJSON('/api/sessions').then(function (data) {
      app.sessions = Array.isArray(data.sessions) ? data.sessions : [];
      var seq = Number(data.seq || 0);
      if (seq > app.globalSeq) app.globalSeq = seq;
      app.latestSessionId = data.latest_session_id || app.latestSessionId;
      app.latestReplyId = data.latest_reply_id || app.latestReplyId;
      app.sessions.forEach(function (s) {
        if (!isSessionExpanded(s.id)) return;
        var rows = app.replies[s.id];
        var stale = !rows ||
          (s.reply_count && rows.length < s.reply_count) ||
          (s.seq && (!rows[0] || Number(rows[0].seq || 0) < Number(s.seq)));
        if (stale) {
          loadSessionReplies(s.id, true).then(function () { renderRail(); });
        }
      });
      renderRail();
      renderSplitSelect();
      return data;
    }).catch(function () { return null; });
  }

  function loadSessionReplies(sessionId, force) {
    if (!sessionId) return Promise.resolve([]);
    if (app.replies[sessionId] && !force) return Promise.resolve(app.replies[sessionId]);
    if (app.loadingReplies[sessionId]) return app.loadingReplies[sessionId];
    var job = fetchJSON('/api/sessions/' + encodeURIComponent(sessionId) + '/replies?limit=200')
      .then(function (data) {
        app.replies[sessionId] = Array.isArray(data.replies) ? data.replies : [];
        return app.replies[sessionId];
      })
      .catch(function () { return app.replies[sessionId] || []; })
      .then(function (rows) {
        delete app.loadingReplies[sessionId];
        return rows;
      });
    app.loadingReplies[sessionId] = job;
    return job;
  }

  function loadReply(pane, sessionId, replyId, opts) {
    opts = opts || {};
    if (!sessionId || !replyId) return Promise.resolve(null);
    return fetchJSON('/api/sessions/' + encodeURIComponent(sessionId) + '/replies/' + encodeURIComponent(replyId))
      .then(function (data) {
        var reply = data.reply || {};
        var session = data.session || {};
        var doc = {
          format: reply.format || 'md',
          title: reply.title || '',
          content: data.content || '',
          updated_at: reply.updated_at || '',
          mtime: Date.now(),
          seq: reply.seq || 0,
          session_id: sessionId,
          reply_id: reply.reply_id || replyId,
          session_label: session.label || '',
          kind: 'session'
        };
        var isLatest = !!session.last_reply_id && session.last_reply_id === doc.reply_id;
        pane.sessionId = sessionId;
        renderIntoPane(pane, doc, { history: !isLatest });
        if (pane.name === 'primary') {
          if (isLatest) {
            markSeen(sessionId, doc.seq);
            $('#new-reply-banner').hidden = true;
            app.pendingBanner = null;
          }
          if (!opts.noUrl) writeUrl({ session: sessionId, reply: doc.reply_id });
          $('#back-btn').hidden = isLatest;
          refreshHistory();
        } else {
          renderSplitReplies();
        }
        renderRail();
        setStatus('online', '已同步 ' + clock());
        return data;
      })
      .catch(function (err) {
        setStatus('stale', '回复加载失败');
        toast('回复加载失败：' + err.message);
        return null;
      });
  }

  function loadGlobalLatest(pane) {
    return fetchJSON('/api/latest?_=' + Date.now()).then(function (data) {
      pane.sessionId = data.session_id || '';
      pane.replyId = data.reply_id || '';
      pane.viewingHistory = false;
      renderIntoPane(pane, data, { history: false });
      $('#back-btn').hidden = true;
      setStatus('online', '已同步 ' + clock());
      refreshHistory();
      renderRail();
      return data;
    }).catch(function () {
      setStatus('offline', '连接中断，重试中');
      return null;
    });
  }

  function refreshLatestNow() {
    var pane = panes.primary;
    app.pendingBanner = null;
    $('#new-reply-banner').hidden = true;
    $('#back-btn').hidden = true;
    if (app.mode === 'lock' && pane.sessionId) {
      return loadSessionReplies(pane.sessionId, true).then(function (rows) {
        if (rows.length) return loadReply(pane, pane.sessionId, rows[0].reply_id);
        return loadGlobalLatest(pane);
      });
    }
    return loadGlobalLatest(pane);
  }

  function loadLegacyHistory(filename) {
    if (!filename) return Promise.resolve(null);
    return fetchJSON('/api/history/' + encodeURIComponent(filename)).then(function (data) {
      var pane = panes.primary;
      pane.sessionId = data.session_id || '';
      pane.replyId = data.reply_id || '';
      pane.viewingHistory = true;
      renderIntoPane(pane, {
        format: data.format,
        title: data.title || filename,
        content: data.content || '',
        updated_at: data.updated_at,
        mtime: data.mtime || 0,
        seq: data.seq || 0,
        session_id: data.session_id || '',
        reply_id: data.reply_id || '',
        filename: filename,
        kind: 'history'
      }, { history: true });
      $('#back-btn').hidden = false;
      $('#new-reply-banner').hidden = true;
      setStatus('online', '浏览历史');
      return data;
    }).catch(function (err) {
      toast('历史记录加载失败：' + err.message);
      refreshHistory();
      return null;
    });
  }

  function refreshHistory() {
    var sel = $('#history-select');
    var previous = sel.value;
    var sessionId = panes.primary.sessionId;
    var keep = function () {
      var exists = Array.prototype.some.call(sel.options, function (o) { return o.value === previous; });
      sel.value = exists ? previous : '';
    };

    if (!sessionId) {
      return fetchJSON('/api/history').then(function (data) {
        sel.innerHTML = '';
        sel.appendChild(new Option('最新内容', ''));
        (data.items || []).forEach(function (item) {
          var label = fmtTime(item.updated_at) + '  [' + String(item.format || 'md').toUpperCase() + '] ' +
            (item.title || item.filename);
          sel.appendChild(new Option(label, 'h:' + item.filename));
        });
        keep();
      }).catch(function () { /* 历史列表失败不打断主流程 */ });
    }

    return loadSessionReplies(sessionId, true).then(function (rows) {
      sel.innerHTML = '';
      sel.appendChild(new Option('最新内容', ''));
      var total = rows.length;
      rows.forEach(function (row, index) {
        var number = total - index;
        var text = '#' + number + (index === 0 ? ' (最新)' : '') + ' · ' + (row.title || row.reply_id);
        sel.appendChild(new Option(text, 'r:' + row.reply_id));
      });
      return fetchJSON('/api/history').then(function (data) {
        var items = data.items || [];
        if (!items.length) return;
        var group = document.createElement('optgroup');
        group.label = '历史归档（v3）';
        items.forEach(function (item) {
          var label = fmtTime(item.updated_at) + '  [' + String(item.format || 'md').toUpperCase() + '] ' +
            (item.title || item.filename);
          group.appendChild(new Option(label, 'h:' + item.filename));
        });
        sel.appendChild(group);
      }).catch(function () { /* ignore */ });
    }).then(keep).catch(function () { /* ignore */ });
  }

  function loadHistory(value) {
    if (!value) return refreshLatestNow();
    if (value.indexOf('r:') === 0) {
      var replyId = value.slice(2);
      if (panes.primary.sessionId) return loadReply(panes.primary, panes.primary.sessionId, replyId);
      return Promise.resolve(null);
    }
    if (value.indexOf('h:') === 0) return loadLegacyHistory(value.slice(2));
    return refreshLatestNow();
  }

  /* ---------- 会话焦点 / 分屏 / 元数据 ---------- */

  function renderEmptyPane(pane, message) {
    pane.viewer.content = '';
    pane.viewer.canvasData = null;
    pane.content.innerHTML = '';
    pane.content.hidden = true;
    pane.empty.hidden = false;
    pane.empty.textContent = message;
    pane.docTitle.textContent = '空会话';
    pane.docSub.textContent = '';
  }

  function focusSession(sessionId, opts) {
    opts = opts || {};
    if (!opts.keepMode) {
      app.mode = 'lock';
      updateModeButton();
    }
    var pane = panes.primary;
    pane.sessionId = sessionId;
    pane.viewingHistory = false;
    app.pendingBanner = null;
    $('#new-reply-banner').hidden = true;
    var target = null;
    for (var i = 0; i < app.sessions.length; i++) {
      if (app.sessions[i].id === sessionId) { target = app.sessions[i]; break; }
    }
    app.expandedSessions[sessionId] = true;
    if (target) delete app.collapsedGroups[railGroupKey(target)];
    saveRailState();
    renderRail();
    if (!opts.silent) writeUrl({ session: sessionId, reply: opts.reply || null, mode: 'lock' });
    return loadSessionReplies(sessionId, true).then(function (rows) {
      renderRail();
      var replyId = opts.reply || (rows[0] && rows[0].reply_id);
      if (!replyId) {
        pane.replyId = '';
        renderEmptyPane(pane, '这个会话还没有回复。');
        refreshHistory();
        return null;
      }
      return loadReply(pane, sessionId, replyId, { noUrl: !!opts.silent });
    });
  }

  function renderSplitSelect() {
    var sel = $('.sec-session-select');
    if (!sel) return;
    var current = panes.secondary.sessionId;
    sel.innerHTML = '';
    sel.appendChild(new Option('选择分屏会话', ''));
    app.sessions.forEach(function (s) {
      if (s.id === panes.primary.sessionId) return;
      sel.appendChild(new Option(s.label, s.id));
    });
    var exists = app.sessions.some(function (s) { return s.id === current; });
    sel.value = exists ? current : '';
  }

  function renderSplitReplies() {
    var sel = $('.sec-reply-select');
    if (!sel) return;
    var rows = app.replies[panes.secondary.sessionId] || [];
    var previous = sel.value;
    sel.innerHTML = '';
    sel.appendChild(new Option('最新', ''));
    var total = rows.length;
    rows.forEach(function (row, index) {
      var number = total - index;
      var text = '#' + number + (index === 0 ? ' (最新)' : '') + ' · ' + (row.title || row.reply_id);
      sel.appendChild(new Option(text, row.reply_id));
    });
    var exists = Array.prototype.some.call(sel.options, function (o) { return o.value === previous; });
    sel.value = exists ? previous : '';
  }

  function splitVisible() {
    return !$('#pane-secondary').hidden;
  }

  function openSplit(sessionId, opts) {
    opts = opts || {};
    if (!sessionId) return Promise.resolve(null);
    if (sessionId === panes.primary.sessionId) {
      if (!opts.silent) toast('请选择另一个会话来分屏');
      return Promise.resolve(null);
    }
    var pane = panes.secondary;
    $('#pane-secondary').hidden = false;
    $('#stage').dataset.split = app.layout || 'h';
    pane.sessionId = sessionId;
    if (!opts.silent) writeUrl({ split: sessionId, layout: app.layout || 'h' });
    renderSplitSelect();
    refitPanes();
    return loadSessionReplies(sessionId, true).then(function (rows) {
      renderRail();
      renderSplitReplies();
      if (rows[0]) return loadReply(pane, sessionId, rows[0].reply_id, { noUrl: true });
      renderEmptyPane(pane, '这个会话还没有回复。');
      return null;
    });
  }

  function closeSplit() {
    var pane = panes.secondary;
    $('#pane-secondary').hidden = true;
    $('#stage').dataset.split = '';
    pane.sessionId = '';
    pane.replyId = '';
    pane.viewer.content = '';
    pane.viewer.canvasData = null;
    pane.viewer.drawer = {};
    if (pane.viewer.mm && typeof pane.viewer.mm.destroy === 'function') {
      try { pane.viewer.mm.destroy(); } catch (err) { /* ignore */ }
      pane.viewer.mm = null;
    }
    var sel = $('.sec-reply-select');
    if (sel) sel.innerHTML = '<option value="">最新</option>';
    writeUrl({ split: null, layout: null });
    renderRail();
    refitPanes();
  }

  function togglePin(session) {
    return postJSON('/api/sessions/' + encodeURIComponent(session.id), { pinned: !session.pinned })
      .then(function () { return refreshSessions(); })
      .catch(function (err) { toast('置顶失败：' + err.message); });
  }

  function renameSession(session) {
    var name = window.prompt('会话显示名', session.label || '');
    if (name === null) return;
    name = String(name).trim();
    if (!name) return;
    postJSON('/api/sessions/' + encodeURIComponent(session.id), { label: name })
      .then(function () { return refreshSessions(); })
      .catch(function (err) { toast('重命名失败：' + err.message); });
  }

  /* ---------- URL 状态 ---------- */

  function readUrl() {
    var params = new URLSearchParams(window.location.search);
    return {
      session: params.get('session') || '',
      reply: params.get('reply') || '',
      split: params.get('split') || '',
      layout: params.get('layout') || '',
      mode: params.get('mode') || ''
    };
  }

  function writeUrl(patch) {
    var params = new URLSearchParams(window.location.search);
    Object.keys(patch || {}).forEach(function (key) {
      var value = patch[key];
      if (value === null || value === undefined || value === '') params.delete(key);
      else params.set(key, String(value));
    });
    var query = params.toString();
    window.history.replaceState(null, '', window.location.pathname + (query ? '?' + query : ''));
  }

  function updateModeButton() {
    var btn = $('#mode-btn');
    btn.textContent = app.mode === 'lock' ? '锁定会话' : '跟随全局';
    btn.dataset.mode = app.mode;
  }

  function applyUrl() {
    var url = readUrl();
    if (url.mode === 'lock' || url.mode === 'global') app.mode = url.mode;
    else if (url.session) app.mode = 'lock';
    app.layout = url.layout === 'v' ? 'v' : 'h';
    updateModeButton();
    var job;
    if (url.session) {
      job = focusSession(url.session, {
        reply: url.reply,
        silent: true,
        keepMode: url.mode === 'global'
      });
    } else {
      job = loadGlobalLatest(panes.primary);
    }
    return job.then(function () {
      if (url.split) return openSplit(url.split, { silent: true });
      if (splitVisible()) closeSplit();
      return null;
    });
  }

  /* ---------- 实时同步：SSE + 轮询兜底 ---------- */

  function showNewReplyBanner(ev) {
    app.pendingBanner = { session_id: ev.session_id, reply_id: ev.reply_id, title: ev.title };
    $('#new-reply-banner-text').textContent = '本会话有新回复' + (ev.title ? '：' + ev.title : '');
    $('#new-reply-banner').hidden = false;
  }

  function switchToLatestReply() {
    var pending = app.pendingBanner;
    app.pendingBanner = null;
    $('#new-reply-banner').hidden = true;
    var sessionId = (pending && pending.session_id) || panes.primary.sessionId;
    if (!sessionId) return refreshLatestNow();
    closeDrawer();
    panes.primary.viewingHistory = false;
    return loadSessionReplies(sessionId, true).then(function (rows) {
      var replyId = (rows[0] && rows[0].reply_id) || (pending && pending.reply_id);
      if (!replyId) return refreshLatestNow();
      return loadReply(panes.primary, sessionId, replyId);
    });
  }

  function handleEvent(ev) {
    if (!ev || typeof ev !== 'object') return;
    var seq = Number(ev.seq || 0);
    // SSE 与轮询可能同时送达同一条事件：按 seq 去重，避免重复 loadReply / 重复 toast
    if (seq && seq <= app.lastHandledSeq) return;
    if (seq) app.lastHandledSeq = seq;
    if (seq > app.globalSeq) app.globalSeq = seq;
    var sessionId = ev.session_id || '';
    var primary = panes.primary;
    if (!sessionId) {
      refreshLatestNow();
      return;
    }

    /* Level 3 实时刷新：焦点会话与所有展开会话都要立刻看到新的 #N（阅读守卫下也不例外）。 */
    if (sessionId === primary.sessionId || isSessionExpanded(sessionId)) {
      loadSessionReplies(sessionId, true).then(function () { renderRail(); });
    }

    if (sessionId === primary.sessionId) {
      if (drawerOpen() || primary.viewingHistory) {
        // 阅读守卫：不打断抽屉 / 不替换正在回看的历史轮次
        showNewReplyBanner(ev);
        refreshSessions();
        return;
      }
      markSeen(sessionId, seq);
      loadReply(primary, sessionId, ev.reply_id, { noUrl: true }).then(function () {
        writeUrl({ session: sessionId, reply: ev.reply_id });
      });
      refreshSessions();
      return;
    }

    if (splitVisible() && sessionId === panes.secondary.sessionId) {
      if (!panes.secondary.viewingHistory) {
        loadReply(panes.secondary, sessionId, ev.reply_id, { noUrl: true });
      }
      refreshSessions();
      return;
    }

    if (app.mode === 'global') {
      loadReply(primary, sessionId, ev.reply_id, { noUrl: true }).then(function () {
        writeUrl({ session: sessionId, reply: ev.reply_id, mode: 'global' });
      });
      refreshSessions();
      return;
    }

    // 锁定当前会话：只标记未读 + 非侵入式提示，绝不劫持主窗格
    toastAction('[' + (ev.session_label || sessionId) + ' · ' + (ev.title || '新回复') + ']', '查看', function () {
      focusSession(sessionId, { reply: ev.reply_id });
    });
    refreshSessions();
  }

  function poll() {
    if (app.polling) return Promise.resolve();
    app.polling = true;
    return fetchJSON('/api/state?since=' + app.globalSeq + '&_=' + Date.now()).then(function (data) {
      setStatus('online', '已同步 ' + clock());
      (data.events || []).forEach(handleEvent);
      if (typeof data.seq === 'number' && data.seq > app.globalSeq) app.globalSeq = data.seq;
      return refreshSessions();
    }).catch(function () {
      setStatus('offline', '连接中断，重试中');
    }).then(function () {
      app.polling = false;
    });
  }

  function connectEvents() {
    if (!('EventSource' in window) || app.sse) return;
    try {
      app.sse = new EventSource('/api/events');
    } catch (err) {
      app.sse = null;
      return;
    }
    app.sse.addEventListener('open', function () {
      setStatus('online', '实时已连接');
    });
    app.sse.addEventListener('reply', function (message) {
      var payload = null;
      try { payload = JSON.parse(message.data); } catch (err) { return; }
      handleEvent(payload);
    });
    app.sse.addEventListener('error', function () {
      setStatus('stale', '实时断线，轮询中');
    });
  }

  /* ---------- 绑定与启动 ---------- */

  function bind() {
    $('#theme-btn').addEventListener('click', function () {
      setTheme(app.theme === 'dark' ? 'light' : 'dark', true);
    });
    $('#history-select').addEventListener('change', function (ev) {
      loadHistory(ev.target.value);
    });
    $('#back-btn').addEventListener('click', function () {
      $('#history-select').value = '';
      refreshLatestNow();
    });
    $('#drawer-close').addEventListener('click', closeDrawer);
    $('#rail-toggle').addEventListener('click', toggleRail);
    $('#mode-btn').addEventListener('click', function () {
      app.mode = app.mode === 'lock' ? 'global' : 'lock';
      writeUrl({ mode: app.mode });
      updateModeButton();
      toast(app.mode === 'lock' ? '已锁定当前会话：其它会话只提示，不跳转' : '已切换为跟随全局最新回复');
    });
    $('#rail-tabs').addEventListener('click', function (ev) {
      var btn = ev.target.closest ? ev.target.closest('button[data-filter]') : null;
      if (!btn) return;
      app.filter = btn.dataset.filter;
      $$('#rail-tabs button').forEach(function (b) { b.dataset.active = String(b === btn); });
      renderRail();
    });
    $('#feed-btn').addEventListener('click', function () {
      setRailView(app.railView === 'feed' ? 'sessions' : 'feed');
    });
    $('#new-reply-jump').addEventListener('click', switchToLatestReply);
    $('#new-reply-dismiss').addEventListener('click', function () {
      app.pendingBanner = null;
      $('#new-reply-banner').hidden = true;
    });
    $('.sec-close').addEventListener('click', closeSplit);
    $('.sec-session-select').addEventListener('change', function (ev) {
      if (ev.target.value) openSplit(ev.target.value);
    });
    $('.sec-reply-select').addEventListener('change', function (ev) {
      var sessionId = panes.secondary.sessionId;
      if (!sessionId) return;
      var rows = app.replies[sessionId] || [];
      var replyId = ev.target.value || (rows[0] && rows[0].reply_id);
      if (!replyId) return;
      panes.secondary.viewingHistory = false;
      loadReply(panes.secondary, sessionId, replyId, { noUrl: true });
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') closeDrawer();
    });
    [panes.primary, panes.secondary].forEach(function (pane) {
      pane.el.addEventListener('click', function (ev) {
        var el = ev.target && ev.target.closest ? ev.target.closest('[data-drawer]') : null;
        if (!el) return;
        var payload = pane.viewer.drawer[el.getAttribute('data-drawer')];
        if (payload) openDrawer(payload);
      });
      pane.el.addEventListener('keydown', function (ev) {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        var el = ev.target && ev.target.closest ? ev.target.closest('[data-drawer]') : null;
        if (!el) return;
        ev.preventDefault();
        var payload = pane.viewer.drawer[el.getAttribute('data-drawer')];
        if (payload) openDrawer(payload);
      });
    });
    window.addEventListener('resize', function () {
      clearTimeout(bind._t);
      bind._t = setTimeout(function () {
        fitMindmap(panes.primary);
        fitMindmap(panes.secondary);
        fitGraphs(panes.primary);
        fitGraphs(panes.secondary);
      }, 180);
    });
    window.addEventListener('popstate', function () { applyUrl(); });
  }

  bind();
  setTheme(app.theme, false);
  updateModeButton();
  if (localStorage.getItem('vr.rail') === '0') $('#session-rail').dataset.open = 'false';
  setMode(panes.primary, 'md');
  setMode(panes.secondary, 'md');
  applySubview(panes.primary);
  applySubview(panes.secondary);
  renderRail();
  refreshSessions().then(function () { return applyUrl(); }).then(function () {
    // 初始状态已通过 refreshSessions()/applyUrl() 对齐，之后的事件才需要逐条处理
    app.lastHandledSeq = app.globalSeq;
    connectEvents();
    poll();
    setInterval(poll, 3000);
  });
})();
</script>
</body>
</html>
"""

def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


class SSEBroker:
    """极简 stdlib SSE 广播：后台 watcher 兜底，/api/notify 立即唤醒。"""

    def __init__(self, cache_dir: Path, limit: int = SSE_CLIENTS) -> None:
        self.cache_dir = Path(cache_dir)
        self.stop_event = threading.Event()
        self.slots = threading.Semaphore(limit)
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._sent_seq = 0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._sent_seq = int(vr_store.read_state(self.cache_dir).get("seq") or 0)
        self._thread = threading.Thread(target=self._watch, name="vr-sse-watch", daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        """state.json 的 seq 变了就推事件，即使写入方没有调用 /api/notify。"""
        while not self.stop_event.wait(SSE_POLL):
            try:
                state = vr_store.read_state(self.cache_dir)
            except OSError:
                continue
            seq = int(state.get("seq") or 0)
            if seq <= self._sent_seq:
                continue
            session_id = state.get("latest_session_id") or ""
            reply_id = state.get("latest_reply_id") or ""
            if not session_id or not reply_id:
                self._sent_seq = seq
                continue
            self.push(vr_store.reply_event(self.cache_dir, session_id, reply_id))

    def push(self, payload: dict) -> None:
        event = dict(payload)
        try:
            seq = int(event.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        with self._lock:
            if seq:
                self._sent_seq = max(self._sent_seq, seq)
            clients = list(self._clients)
        for client in clients:
            try:
                client.put_nowait(event)
            except queue.Full:
                # 慢客户端：丢最旧的一条，保证仍能收到最新状态
                try:
                    client.get_nowait()
                except queue.Empty:
                    pass
                try:
                    client.put_nowait(event)
                except queue.Full:
                    pass

    def register(self) -> queue.Queue | None:
        if not self.slots.acquire(blocking=False):
            return None
        client: queue.Queue = queue.Queue(maxsize=SSE_QUEUE)
        with self._lock:
            self._clients.add(client)
        return client

    def unregister(self, client: queue.Queue) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.discard(client)
                self.slots.release()

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.put_nowait(None)
            except queue.Full:
                pass


class Handler(BaseHTTPRequestHandler):
    server_version = "VisualReply/5.0"
    protocol_version = "HTTP/1.1"

    cache_dir: Path = DEFAULT_CACHE_DIR

    # ---------- 输出工具 ----------

    @property
    def broker(self) -> SSEBroker:
        return self.server.broker

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=None).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"ok": False, "error": message, "status": status}, status)

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > 4 * 1024 * 1024:
            return None
        if length == 0:
            return b""
        try:
            return self.rfile.read(length)
        except OSError:
            return None

    # ---------- 数据读取 ----------

    def _read_meta(self) -> dict:
        path = self.cache_dir / "meta.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _detect_format(self, meta: dict | None = None) -> str:
        meta = meta if meta is not None else self._read_meta()
        fmt = meta.get("format")
        if fmt in FORMAT_EXT:
            return fmt
        for candidate in ("md", "json", "html"):
            if (self.cache_dir / f"latest{FORMAT_EXT[candidate]}").is_file():
                return candidate
        return "md"

    def _payload_path(self, fmt: str, meta: dict) -> Path:
        name = meta.get("payload_file") if meta.get("format") == fmt else None
        if not isinstance(name, str) or not name:
            name = f"latest{FORMAT_EXT[fmt]}"
        return self.cache_dir / Path(name).name

    @staticmethod
    def _first_heading(text: str) -> str:
        for line in text.splitlines()[:40]:
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
        return ""

    def _title_for(self, fmt: str, content: str, fallback: str = "") -> str:
        title = ""
        if fmt == "md":
            title = self._first_heading(content)
        elif fmt == "json":
            try:
                data = json.loads(content)
            except ValueError:
                data = None
            if isinstance(data, dict):
                for key in ("title", "headline"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        title = value.strip()
                        break
        elif fmt == "html":
            match = re.search(r"<title[^>]*>(.*?)</title>", content, re.I | re.S)
            if match:
                title = re.sub(r"\s+", " ", match.group(1)).strip()
        return title or fallback

    def _latest(self) -> dict:
        meta = self._read_meta()
        fmt = self._detect_format(meta)
        payload = self._payload_path(fmt, meta)

        if not payload.is_file():
            for candidate in ("md", "json", "html"):
                alt = self.cache_dir / f"latest{FORMAT_EXT[candidate]}"
                if alt.is_file():
                    fmt, payload = candidate, alt
                    break
            else:
                return {
                    "version": VERSION,
                    "format": fmt,
                    "title": meta.get("title", ""),
                    "content": "",
                    "mtime": 0,
                    "updated_at": None,
                }
        try:
            content = payload.read_text(encoding="utf-8", errors="replace")
            mtime = payload.stat().st_mtime
        except OSError:
            return {"version": VERSION, "format": fmt, "title": "",
                    "content": "", "mtime": 0, "updated_at": None}

        return {
            "version": VERSION,
            "format": fmt,
            "title": meta.get("title") or self._title_for(fmt, content) or "未命名回复",
            "content": content,
            "mtime": mtime,
            "updated_at": iso_time(mtime),
            "seq": int(meta.get("seq") or 0),
            "session_id": meta.get("session_id") or "",
            "reply_id": meta.get("reply_id") or "",
        }

    def _history_files(self) -> list[Path]:
        files = vr_store.legacy_history_files(self.cache_dir)
        return files[:HISTORY_LIMIT]

    def _legacy_only_history_files(self) -> list[Path]:
        """真正的 v3 遗留归档：sidecar 里没有 reply_id 的历史文件。"""
        files = []
        for path in vr_store.legacy_history_files(self.cache_dir):
            sidecar = self._sidecar_meta(path)
            if not sidecar.get("reply_id"):
                files.append(path)
        return files

    def _sidecar_meta(self, path: Path) -> dict:
        candidates = [path.with_name(path.name + ".meta.json")]
        if path.suffix == ".md":
            candidates.append(path.with_suffix(".json"))
        for sidecar in candidates:
            if not sidecar.is_file():
                continue
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                return data
        return {}

    def _history_item(self, path: Path) -> dict:
        fmt = EXT_FORMAT.get(path.suffix.lower(), "md")
        sidecar = self._sidecar_meta(path)

        title = sidecar.get("title") if isinstance(sidecar.get("title"), str) else ""
        if not title:
            try:
                head = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                head = ""
            title = self._title_for(fmt, head)

        try:
            st = path.stat()
        except OSError:
            st = None

        updated_at = sidecar.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at:
            updated_at = iso_time(st.st_mtime) if st else None

        return {
            "filename": path.name,
            "format": fmt,
            "title": title or path.stem,
            "mtime": st.st_mtime if st else 0,
            "updated_at": updated_at,
            "size": st.st_size if st else 0,
            "seq": int(sidecar.get("seq") or 0),
            "session_id": sidecar.get("session_id") or "",
            "reply_id": sidecar.get("reply_id") or "",
        }

    @staticmethod
    def _safe_name(name: str) -> bool:
        if not name or name.startswith(".") or ".." in name:
            return False
        if "/" in name or "\\" in name or "\x00" in name:
            return False
        return name.endswith((".md", ".json", ".html"))

    def _history_path(self, name: str) -> Path | None:
        if not self._safe_name(name):
            return None
        hist = self.cache_dir / "history"
        target = hist / name
        try:
            if target.resolve().parent != hist.resolve():
                return None
        except OSError:
            return None
        return target if target.is_file() else None

    def _artifact_payload(self, name: str | None) -> Path | None:
        if name is None:
            meta = self._read_meta()
            fmt = self._detect_format(meta)
            if fmt != "html":
                return None
            payload = self._payload_path(fmt, meta)
            return payload if payload.is_file() else None
        target = self._history_path(name)
        if target is None or target.suffix.lower() != ".html":
            return None
        return target

    # ---------- 会话接口 ----------

    def _sessions_payload(self) -> dict:
        state = vr_store.read_state(self.cache_dir)
        sessions = vr_store.list_sessions(self.cache_dir)
        legacy = len(self._legacy_only_history_files())
        return {
            "version": VERSION,
            "seq": state["seq"],
            "latest_session_id": state["latest_session_id"],
            "latest_reply_id": state["latest_reply_id"],
            "sessions": sessions,
            "count": len(sessions),
            "legacy_count": legacy,
        }

    def _feed_payload(self, limit: int) -> dict:
        items = vr_store.feed_items(self.cache_dir, limit)
        legacy_items: list[dict] = []
        remaining = max(0, limit - len(items))
        if remaining:
            for path in self._legacy_only_history_files()[:remaining]:
                item = self._history_item(path)
                legacy_items.append({
                    "seq": int(item.get("seq") or 0),
                    "session_id": item.get("session_id") or "",
                    "session_label": "历史归档",
                    "client": "",
                    "project": "",
                    "reply_id": item.get("reply_id") or "",
                    "filename": item.get("filename"),
                    "format": item.get("format"),
                    "title": item.get("title"),
                    "chars": item.get("size") or 0,
                    "updated_at": item.get("updated_at"),
                    "legacy": True,
                })
        merged = items + legacy_items
        return {
            "version": VERSION,
            "items": merged,
            "count": len(merged),
            "legacy_count": len(legacy_items),
        }

    def _state_payload(self, since: int) -> dict:
        state = vr_store.read_state(self.cache_dir)
        events = vr_store.state_events(self.cache_dir, since, limit=200)
        return {
            "version": VERSION,
            "seq": state["seq"],
            "events": events,
            "latest_session_id": state["latest_session_id"],
            "latest_reply_id": state["latest_reply_id"],
        }

    # ---------- SSE ----------

    def _sse(self) -> None:
        broker = self.broker
        client = broker.register()
        if client is None:
            self._error(503, "SSE 客户端已达上限")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(b": connected\n\n")
            self.wfile.write(b"retry: 1500\n\n")
            self.wfile.flush()
            while not broker.stop_event.is_set():
                try:
                    event = client.get(timeout=SSE_KEEPALIVE)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    break
                frame = (
                    "id: " + str(int(event.get("seq") or 0)) + "\n"
                    "event: reply\n"
                    "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                ).encode("utf-8")
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            broker.unregister(client)

    # ---------- 路由 ----------

    def _segments(self, path: str) -> list[str]:
        return [unquote(part) for part in path.split("/") if part]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        query = parse_qs(parsed.query)

        if path == "/":
            self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path == "/healthz":
            broker = self.server.broker
            self._json({"ok": True, "service": SERVICE_NAME, "version": VERSION,
                        "pid": os.getpid(),
                        "port": self.server.server_address[1],
                        "cache_dir": str(self.cache_dir),
                        "seq": vr_store.read_state(self.cache_dir)["seq"],
                        "sse_clients": broker.client_count() if broker else 0})
            return
        if path == "/api/latest":
            self._json(self._latest())
            return
        if path == "/api/history":
            items = [self._history_item(p) for p in self._history_files()]
            self._json({"version": VERSION, "items": items, "count": len(items)})
            return
        if path == "/api/sessions":
            self._json(self._sessions_payload())
            return
        if path == "/api/feed":
            try:
                limit = int((query.get("limit") or [str(FEED_LIMIT)])[0])
            except (TypeError, ValueError):
                limit = FEED_LIMIT
            limit = max(1, min(limit, 500))
            self._json(self._feed_payload(limit))
            return
        if path == "/api/state":
            try:
                since = int((query.get("since") or ["0"])[0])
            except (TypeError, ValueError):
                self._error(400, "since 必须是整数")
                return
            self._json(self._state_payload(max(0, since)))
            return
        if path == "/api/events":
            self._sse()
            return
        if path.startswith("/api/history/"):
            raw = unquote(path[len("/api/history/"):])
            if not self._safe_name(raw):
                self._error(400, "非法的文件名")
                return
            target = self._history_path(raw)
            if target is None:
                self._error(404, "历史记录不存在")
                return
            item = self._history_item(target)
            try:
                item["content"] = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                self._error(500, f"读取失败: {exc}")
                return
            self._json(item)
            return
        if path.startswith("/artifact/session/"):
            parts = self._segments(path)
            if len(parts) != 4 or parts[0] != "artifact" or parts[1] != "session":
                self._error(404, "HTML Artifact 不存在")
                return
            session_id, reply_id = parts[2], parts[3]
            if not vr_store.is_safe_id(session_id) or not vr_store.is_safe_id(reply_id):
                self._error(400, "非法的会话或回复 ID")
                return
            meta = vr_store.read_reply_meta(self.cache_dir, session_id, reply_id)
            target = vr_store.reply_file(self.cache_dir, session_id, reply_id)
            if meta is None or target is None or target.suffix.lower() != ".html":
                self._error(404, "HTML Artifact 不存在")
                return
            try:
                body = target.read_bytes()
            except OSError as exc:
                self._error(500, f"读取失败: {exc}")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path.startswith("/api/sessions/"):
            parts = self._segments(path)
            if len(parts) < 3 or parts[0] != "api" or parts[1] != "sessions":
                self._error(404, "未找到该路径")
                return
            session_id = parts[2]
            if not vr_store.is_safe_id(session_id):
                self._error(400, "非法的会话 ID")
                return
            session = vr_store.read_session(self.cache_dir, session_id)
            if session is None:
                self._error(404, "会话不存在")
                return
            if len(parts) == 4 and parts[3] == "replies":
                try:
                    limit = int((query.get("limit") or [str(REPLY_LIMIT)])[0])
                except (TypeError, ValueError):
                    limit = REPLY_LIMIT
                limit = max(1, min(limit, 1000))
                replies = vr_store.list_replies(self.cache_dir, session_id, limit)
                self._json({"version": VERSION, "session": session,
                            "replies": replies, "count": len(replies)})
                return
            if len(parts) == 5 and parts[3] == "replies":
                reply_id = parts[4]
                if not vr_store.is_safe_id(reply_id):
                    self._error(400, "非法的回复 ID")
                    return
                data = vr_store.read_reply(self.cache_dir, session_id, reply_id)
                if data is None:
                    self._error(404, "回复不存在")
                    return
                self._json({"version": VERSION, "session": session,
                            "reply": data["reply"], "content": data["content"]})
                return
            self._error(404, "未找到该路径")
            return
        if path == "/artifact/latest":
            target = self._artifact_payload(None)
            if target is None:
                self._error(404, "当前不是 HTML Artifact")
                return
            try:
                body = target.read_bytes()
            except OSError as exc:
                self._error(500, f"读取失败: {exc}")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path.startswith("/artifact/history/"):
            raw = unquote(path[len("/artifact/history/"):])
            target = self._artifact_payload(raw)
            if target is None:
                self._error(404, "HTML Artifact 不存在")
                return
            try:
                body = target.read_bytes()
            except OSError as exc:
                self._error(500, f"读取失败: {exc}")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return

        self._error(404, "未找到该路径")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        body = self._read_body()
        if body is None:
            self._error(400, "请求体缺失或过大")
            return
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._error(400, "JSON 解析失败")
            return
        if not isinstance(payload, dict):
            self._error(400, "请求体必须是 JSON 对象")
            return

        if path == "/api/notify":
            self.broker.push(payload)
            self._json({"ok": True, "version": VERSION})
            return

        parts = self._segments(path)
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "sessions":
            session_id = parts[2]
            if not vr_store.is_safe_id(session_id):
                self._error(400, "非法的会话 ID")
                return
            label = payload.get("label")
            pinned = payload.get("pinned")
            if label is not None and not isinstance(label, str):
                self._error(400, "label 必须是字符串")
                return
            if pinned is not None and not isinstance(pinned, bool):
                self._error(400, "pinned 必须是布尔值")
                return
            if label is None and pinned is None:
                self._error(400, "没有可更新的字段（label / pinned）")
                return
            try:
                with vr_store.cache_lock(self.cache_dir):
                    record = vr_store.update_session(self.cache_dir, session_id,
                                                     label=label, pinned=pinned)
            except vr_store.LockTimeout:
                self._error(503, "缓存目录正忙，请稍后重试")
                return
            except OSError as exc:
                self._error(500, f"写入失败: {exc}")
                return
            if record is None:
                self._error(404, "会话不存在")
                return
            self._json({"ok": True, "version": VERSION, "session": record})
            return

        self._error(404, "未找到该路径")

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def log_request(self, code="-", size="-") -> None:
        pass


class VisualReplyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    #: 多 Agent 会同时发布 / 轮询，默认 backlog=5 会丢 SYN（客户端要等 1s 重传）
    request_queue_size = 128
    broker: SSEBroker | None = None


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="visual-reply 本地预览服务（多会话工作台）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                        help=f"缓存目录（默认 {DEFAULT_CACHE_DIR}）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cache_dir = Path(args.cache_dir).expanduser().resolve()
    (cache_dir / "history").mkdir(parents=True, exist_ok=True)
    (cache_dir / "sessions").mkdir(parents=True, exist_ok=True)

    broker = SSEBroker(cache_dir)
    handler = type("BoundHandler", (Handler,), {"cache_dir": cache_dir})
    httpd = VisualReplyServer((args.host, args.port), handler)
    httpd.broker = broker
    port = httpd.server_address[1]

    pid_file = cache_dir / f".server-{port}.json"
    pid_file.write_text(json.dumps({
        "service": SERVICE_NAME,
        "version": VERSION,
        "pid": os.getpid(),
        "port": port,
        "cache_dir": str(cache_dir),
        "started_at": iso_time(time.time()),
    }, ensure_ascii=False), encoding="utf-8")

    def cleanup() -> None:
        try:
            if pid_file.exists():
                data = json.loads(pid_file.read_text(encoding="utf-8"))
                if data.get("pid") == os.getpid():
                    pid_file.unlink()
        except (OSError, ValueError):
            pass

    def stop(_signum, _frame) -> None:
        # 先放 SSE 处理器退出，再关闭 serve_forever；daemon 线程保证毫秒级退出
        broker.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    broker.start()
    print(f"{SERVICE_NAME} v{VERSION} listening on http://{args.host}:{port} (cache={cache_dir})",
          file=sys.stderr, flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        broker.stop()
        httpd.server_close()
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
