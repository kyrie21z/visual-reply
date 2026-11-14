#!/usr/bin/env python3
"""visual-reply 本地预览服务 v4（纯标准库，多会话工作台）。

三种渲染模式：
    md     兼容模式：阅读 / 脑图 / 分屏
    json   Visual Canvas 交互画布：kpis / graph / comparison / bento / steps
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
VERSION = 4
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
.cv-section { margin-top: 30px; }
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
  flex: 0 0 272px; display: flex; flex-direction: column; min-height: 0;
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
  position: relative; display: flex; gap: 8px; padding: 8px 9px 8px 12px;
  border: 1px solid var(--border); border-radius: 11px; background: var(--bg-soft);
  cursor: pointer; transition: border-color .15s, background .15s;
}
.rail-card:hover { border-color: var(--accent); }
.rail-card[data-active="true"] { border-color: var(--accent); background: var(--accent-soft); }
.rail-accent { position: absolute; left: 0; top: 8px; bottom: 8px; width: 3px; border-radius: 0 3px 3px 0; }
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
  #session-rail { flex-basis: 210px; }
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
    <div class="rail-list" id="rail-list"></div>
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
    pendingBanner: null,
    sse: null,
    polling: false
  };
  try {
    app.lastSeen = JSON.parse(localStorage.getItem('vr.lastSeen') || '{}') || {};
  } catch (err) {
    app.lastSeen = {};
  }

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
        drawer: {}, drawerSeq: 0, seq: 0
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
      if (table.parentElement && table.parentElement.classList.contains('table-wrap')) return;
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

  function renderSection(pane, sec) {
    if (!sec || typeof sec !== 'object') return '';
    var type = sec.type;
    if (!type) {
      if (sec.nodes) type = 'graph';
      else if (sec.cards) type = 'bento';
      else if (sec.steps) type = 'steps';
      else if (sec.kpis) type = 'kpis';
      else if (sec.items) type = 'comparison';
    }
    var body = '';
    if (type === 'kpis') body = renderKpis(sec);
    else if (type === 'graph') body = renderGraph(pane, sec);
    else if (type === 'comparison') body = renderComparison(pane, sec);
    else if (type === 'bento') body = renderBento(pane, sec);
    else if (type === 'steps') body = renderSteps(pane, sec);
    else return '';
    if (!body) return '';
    var title = sec.title ? '<div class="cv-sec-title">' + escapeHtml(sec.title) + '</div>' : '';
    return '<section class="cv-section" data-type="' + escapeAttr(type) + '">' + title + body + '</section>';
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
    var html = '';
    if (data.headline) html += '<h1 class="canvas-headline">' + escapeHtml(data.headline) + '</h1>';
    if (data.badges.length) {
      html += '<div class="badges">' + data.badges.map(function (b) {
        var text = typeof b === 'string' ? b : (b && (b.text || b.label)) || '';
        var tone = (b && b.tone) || '';
        return '<span class="badge" data-tone="' + escapeAttr(tone) + '">' + escapeHtml(text) + '</span>';
      }).join('') + '</div>';
    }
    if (!data.sections.length) {
      html += '<div class="cv-section"><div class="empty" style="height:200px">画布为空：请提供 sections / nodes / cards。</div></div>';
    } else {
      data.sections.forEach(function (sec) { html += renderSection(pane, sec); });
    }
    root.innerHTML = html;
    bindGraphHover(pane);
    bindGraphTools(pane);
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

  function sessionCard(session) {
    var card = document.createElement('div');
    card.className = 'rail-card';
    card.dataset.sid = session.id;
    card.dataset.active = String(panes.primary.sessionId === session.id);
    card.title = session.label + (session.project ? ' · ' + session.project : '');

    var accent = document.createElement('span');
    accent.className = 'rail-accent';
    accent.style.background = session.color || 'var(--accent)';
    card.appendChild(accent);

    var main = document.createElement('div');
    main.className = 'rail-main';
    var top = document.createElement('div');
    top.className = 'rail-top';
    var label = document.createElement('span');
    label.className = 'rail-label';
    label.textContent = session.label || session.id;
    top.appendChild(label);
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

    card.addEventListener('click', function () { focusSession(session.id); });
    return card;
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
    var rows = app.sessions.slice();
    if (app.filter === 'unread') rows = rows.filter(function (s) { return unreadCount(s) > 0; });
    else if (app.filter === 'pinned') rows = rows.filter(function (s) { return !!s.pinned; });

    if (!rows.length) {
      list.appendChild(railEmpty(app.sessions.length ? '没有符合筛选条件的会话' : '还没有会话。让 Agent 发布一条回复即可。'));
      return;
    }
    rows.forEach(function (s) { list.appendChild(sessionCard(s)); });
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
      renderRail();
      renderSplitSelect();
      return data;
    }).catch(function () { return null; });
  }

  function loadSessionReplies(sessionId, force) {
    if (!sessionId) return Promise.resolve([]);
    if (app.replies[sessionId] && !force) return Promise.resolve(app.replies[sessionId]);
    return fetchJSON('/api/sessions/' + encodeURIComponent(sessionId) + '/replies?limit=200')
      .then(function (data) {
        app.replies[sessionId] = Array.isArray(data.replies) ? data.replies : [];
        return app.replies[sessionId];
      })
      .catch(function () { return app.replies[sessionId] || []; });
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
    server_version = "VisualReply/4.0"
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
