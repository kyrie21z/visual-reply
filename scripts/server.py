#!/usr/bin/env python3
"""visual-reply 本地预览服务 v2（纯标准库）。

三种渲染模式：
    md     兼容模式：阅读 / 脑图 / 分屏
    json   Visual Canvas 交互画布：kpis / graph / comparison / bento / steps
    html   HTML Artifact 沙箱：iframe 渲染 + 源码查看

接口：
    GET /                              单页 HTML
    GET /api/latest                    最新内容 {version, format, title, content, mtime, updated_at}
    GET /api/history                   最近 20 条历史
    GET /api/history/<file>            单条历史
    GET /artifact/latest               HTML 原文（供 iframe 加载）
    GET /artifact/history/<file>       历史 HTML 原文
    GET /healthz                       健康检查（含 version）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

DEFAULT_PORT = 18765
DEFAULT_CACHE_DIR = Path("~/.cache/agent-visual-reply").expanduser()
SERVICE_NAME = "visual-reply"
VERSION = 3
HISTORY_LIMIT = 20

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
  body.view-split #stage { flex-direction: column; }
  body.view-split #mindmap-pane { flex: 0 0 45%; border-left: 0; border-top: 1px solid var(--border); }
  #history-select { max-width: 170px; }
  #canvas-root { padding: 20px 16px 120px; }
  .bento { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .bento-card { grid-column: span 1; }
  body.drawer-open #stage { margin-right: 0; }
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
<main id="stage">
  <section id="reading-pane" class="pane">
    <article id="content" class="markdown-body" hidden></article>
    <div id="empty" class="empty">还没有内容。让 Agent 发布一条回复即可在此看到。</div>
  </section>
  <section id="mindmap-pane" class="pane">
    <svg id="mindmap-svg"></svg>
    <div id="mindmap-empty" class="empty" hidden>脑图库未加载</div>
  </section>
  <section id="canvas-pane" class="pane">
    <div id="canvas-root"></div>
  </section>
  <section id="artifact-pane" class="pane" data-sub="full">
    <iframe id="artifact-frame" sandbox="allow-scripts" referrerpolicy="no-referrer" title="HTML Artifact"></iframe>
    <pre id="artifact-source" hidden></pre>
  </section>
</main>
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

  var state = {
    mode: 'md',
    subview: 'reading',
    theme: localStorage.getItem('vr.theme') || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'),
    content: '',
    format: 'md',
    title: '',
    latestMtime: 0,
    latestLoaded: false,
    viewingHistory: false,
    currentFile: null,
    canvasData: null,
    drawer: {},
    drawerSeq: 0,
    artifactSrc: '',
    mm: null,
    seq: 0,
    polling: false
  };

  var article = $('#content');
  var emptyBox = $('#empty');
  var canvasRoot = $('#canvas-root');
  var drawer = $('#drawer');

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
    box.textContent = message;
    box.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { box.hidden = true; }, 2600);
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

  function renderMarkdown(text) {
    if (!text) return '';
    if (!window.marked) return '<pre>' + escapeHtml(text) + '</pre>';
    try { return marked.parse(String(text), { gfm: true, breaks: false }); }
    catch (err) { return '<pre>' + escapeHtml(text) + '</pre>'; }
  }

  function setTheme(theme, rerender) {
    state.theme = theme;
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
    if (rerender && state.mode === 'md' && state.content) {
      renderReading(state.content);
      if (state.mm && typeof state.mm.destroy === 'function') {
        try { state.mm.destroy(); } catch (err) { /* ignore */ }
        state.mm = null;
      }
      renderMindmap(state.content);
    }
  }

  /* ---------- 模式与子视图 ---------- */

  function setMode(mode) {
    if (state.mode === mode) return false;
    state.mode = mode;
    document.body.classList.remove('mode-md', 'mode-canvas', 'mode-html');
    document.body.classList.add('mode-' + mode);
    var saved = mode === 'md' ? localStorage.getItem('vr.sub.md') : null;
    var ids = VIEWS[mode].map(function (v) { return v.id; });
    state.subview = saved && ids.indexOf(saved) >= 0 ? saved : ids[0];
    rebuildViewSwitch();
    closeDrawer();
    return true;
  }

  function rebuildViewSwitch() {
    var box = $('#view-switch');
    box.innerHTML = '';
    VIEWS[state.mode].forEach(function (v) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.dataset.view = v.id;
      btn.textContent = v.label;
      btn.addEventListener('click', function () {
        state.subview = v.id;
        applySubview();
      });
      box.appendChild(btn);
    });
  }

  function applySubview() {
    closeDrawer();
    $$('#view-switch button').forEach(function (btn) {
      btn.dataset.active = String(btn.dataset.view === state.subview);
    });
    if (state.mode === 'md') localStorage.setItem('vr.sub.md', state.subview);

    if (state.mode === 'md') {
      var v = ['reading', 'mindmap', 'split'].indexOf(state.subview) >= 0 ? state.subview : 'reading';
      ['reading', 'mindmap', 'split'].forEach(function (name) {
        document.body.classList.toggle('view-' + name, name === v);
      });
      if (v !== 'reading') setTimeout(fitMindmap, 60);
    } else if (state.mode === 'canvas') {
      renderCanvas();
    } else {
      $('#artifact-pane').dataset.sub = state.subview;
      $('#artifact-frame').hidden = state.subview === 'source';
      $('#artifact-source').hidden = state.subview !== 'source';
      syncArtifact();
    }
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

  function renderReading(md) {
    if (!window.marked) {
      article.innerHTML = '<pre>' + escapeHtml(md) + '</pre>';
      return;
    }
    var extracted = extractMermaid(md);
    var html = marked.parse(extracted.text, { gfm: true, breaks: false });
    article.innerHTML = injectMermaid(html, extracted.blocks);
    buildSections(article);
    wrapTables(article);
    markExternalLinks(article);
    renderDiagrams();
  }

  function renderDiagrams() {
    var nodes = $$('.mermaid-block', article);
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
        var id = 'mmd-' + (++state.seq);
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

  function renderMindmap(md) {
    var lib = window.markmap;
    var fallback = $('#mindmap-empty');
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
      if (!state.mm) {
        state.mm = lib.Markmap.create($('#mindmap-svg'), {
          duration: 220,
          maxWidth: 300,
          spacingVertical: 8,
          paddingX: 12,
          colorFreezeLevel: 2,
          initialExpandLevel: -1
        }, root);
      } else {
        state.mm.setData(root);
      }
      fitMindmap();
    } catch (err) {
      fallback.hidden = false;
      fallback.textContent = '脑图渲染失败：' + (err && err.message ? err.message : err);
    }
  }

  function fitMindmap() {
    if (!state.mm) return;
    var pane = $('#mindmap-pane');
    if (!pane || pane.clientWidth < 60 || pane.clientHeight < 60) return;
    requestAnimationFrame(function () {
      try { state.mm.fit(); } catch (err) { /* ignore */ }
    });
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

  function registerDrawer(payload) {
    var key = 'd' + (++state.drawerSeq);
    state.drawer[key] = payload;
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

  function renderGraph(sec) {
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
      var key = registerDrawer({
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

  function renderComparison(sec) {
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
      var key = registerDrawer({
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

  function renderBento(sec) {
    var cards = Array.isArray(sec.cards) ? sec.cards : [];
    if (!cards.length) return '';
    return '<div class="bento">' + cards.map(function (c) {
      var span = Number(c.span) === 2 ? 2 : 1;
      var key = registerDrawer({
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

  function renderSteps(sec) {
    var steps = Array.isArray(sec.steps) ? sec.steps : [];
    if (!steps.length) return '';
    return '<div class="step-track">' + steps.map(function (s, i) {
      var status = s.status || 'pending';
      var key = registerDrawer({
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

  function renderSection(sec) {
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
    else if (type === 'graph') body = renderGraph(sec);
    else if (type === 'comparison') body = renderComparison(sec);
    else if (type === 'bento') body = renderBento(sec);
    else if (type === 'steps') body = renderSteps(sec);
    else return '';
    if (!body) return '';
    var title = sec.title ? '<div class="cv-sec-title">' + escapeHtml(sec.title) + '</div>' : '';
    return '<section class="cv-section" data-type="' + escapeAttr(type) + '">' + title + body + '</section>';
  }

  function renderCanvas() {
    if (!state.canvasData) {
      canvasRoot.innerHTML = '<div class="empty" style="height:60vh">还没有内容。让 Agent 发布一条回复即可在此看到。</div>';
      return;
    }
    if (state.subview === 'raw') {
      canvasRoot.innerHTML = '<pre class="raw-json">' + escapeHtml(JSON.stringify(state.canvasData, null, 2)) + '</pre>';
      return;
    }
    var data = state.canvasData;
    state.drawer = {};
    state.drawerSeq = 0;
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
      data.sections.forEach(function (sec) { html += renderSection(sec); });
    }
    canvasRoot.innerHTML = html;
    bindGraphHover();
    bindGraphTools();
    requestAnimationFrame(fitGraphs);
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

  function fitGraphs() {
    $$('.graph-wrap', canvasRoot).forEach(function (wrap) {
      if (wrap.dataset.mode === 'manual') return;
      fitGraph(wrap);
    });
  }

  function bindGraphTools() {
    $$('.graph-block', canvasRoot).forEach(function (block) {
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

  function bindGraphHover() {
    $$('.graph-wrap', canvasRoot).forEach(function (wrap) {
      $$('.g-node', wrap).forEach(function (node) {
        node.addEventListener('mouseenter', function () { highlightGraph(wrap, node.dataset.node); });
        node.addEventListener('mouseleave', function () { highlightGraph(wrap, null); });
      });
    });
  }

  /* ---------- HTML Artifact ---------- */

  function artifactUrl() {
    if (!state.content || !state.content.trim()) return 'about:blank';
    if (state.currentFile) return '/artifact/history/' + encodeURIComponent(state.currentFile);
    return '/artifact/latest?_=' + state.latestMtime;
  }

  function syncArtifact() {
    var url = artifactUrl();
    if (url === state.artifactSrc) return;
    state.artifactSrc = url;
    $('#artifact-frame').src = url;
    var source = $('#artifact-source');
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

  /* ---------- 文档装载 ---------- */

  function renderDoc(content, format, filename) {
    state.content = content || '';
    state.format = format || 'md';
    state.currentFile = filename || null;
    state.artifactSrc = '';
    state.canvasData = null;

    var hasContent = !!(state.content && state.content.trim());
    var mode = state.format;

    if (mode === 'json') {
      if (!hasContent) mode = 'md';
      else {
        try {
          var parsed = JSON.parse(state.content);
          if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
            state.canvasData = normalizeCanvas(parsed);
            mode = 'canvas';
          } else {
            mode = 'md';
          }
        } catch (err) {
          mode = 'md';
        }
      }
    }

    if (mode === 'md') {
      emptyBox.hidden = hasContent;
      article.hidden = !hasContent;
      if (hasContent) {
        renderReading(state.content);
        renderMindmap(state.content);
      }
    }
    if (mode === 'canvas' || mode === 'html') state.subview = VIEWS[mode][0].id;
    setMode(mode);
    applySubview();
  }

  function setHeader(title, sub) {
    state.title = title || '未命名回复';
    $('#doc-title').textContent = state.title;
    $('#doc-sub').textContent = sub || '';
    document.title = state.title + ' · Visual Reply';
  }

  function applyLatest(data) {
    state.latestLoaded = true;
    state.latestMtime = data.mtime || 0;
    state.viewingHistory = false;
    state.format = data.format || 'md';
    $('#back-btn').hidden = true;
    var sub = data.updated_at ? '更新于 ' + fmtTime(data.updated_at) : '等待发布';
    if (data.content) sub += ' · ' + data.content.length + ' 字符 · ' + String(state.format).toUpperCase();
    setHeader(data.title, sub);
    renderDoc(data.content || '', state.format, null);
    $('#history-select').value = '';
  }

  function showHistoryItem(data, filename) {
    state.viewingHistory = true;
    var fmt = data.format || 'md';
    setHeader(data.title || filename, '历史记录 · ' + String(fmt).toUpperCase() + ' · ' + fmtTime(data.updated_at));
    renderDoc(data.content || '', fmt, filename);
    $('#back-btn').hidden = false;
  }

  function refreshHistory() {
    return fetchJSON('/api/history').then(function (data) {
      var sel = $('#history-select');
      var current = sel.value;
      var items = data.items || [];
      sel.innerHTML = '';
      sel.appendChild(new Option('最新内容', ''));
      items.forEach(function (item) {
        var label = fmtTime(item.updated_at) + '  [' + String(item.format || 'md').toUpperCase() + '] ' +
          (item.title || item.filename);
        sel.appendChild(new Option(label, item.filename));
      });
      var exists = items.some(function (item) { return item.filename === current; });
      sel.value = exists ? current : '';
    }).catch(function () { /* 历史列表失败不打断主流程 */ });
  }

  function poll() {
    if (state.polling) return Promise.resolve();
    state.polling = true;
    return fetchJSON('/api/latest?_=' + Date.now()).then(function (data) {
      setStatus('online', '已同步 ' + clock());
      if (!state.latestLoaded) {
        applyLatest(data);
        return refreshHistory();
      }
      if (data.mtime !== state.latestMtime) {
        state.latestMtime = data.mtime;
        if (state.viewingHistory) {
          setStatus('stale', '有新内容');
          $('#back-btn').hidden = false;
        } else {
          applyLatest(data);
        }
        return refreshHistory();
      }
    }).catch(function () {
      setStatus('offline', '连接中断，重试中');
    }).then(function () {
      state.polling = false;
    });
  }

  function refreshLatestNow() {
    return fetchJSON('/api/latest?_=' + Date.now()).then(function (data) {
      applyLatest(data);
      setStatus('online', '已同步 ' + clock());
    }).catch(function () {
      setStatus('offline', '连接中断，重试中');
    });
  }

  function loadHistory(filename) {
    if (!filename) {
      refreshLatestNow();
      return;
    }
    fetchJSON('/api/history/' + encodeURIComponent(filename)).then(function (data) {
      showHistoryItem(data, filename);
      setStatus('online', '浏览历史');
    }).catch(function (err) {
      toast('历史记录加载失败：' + err.message);
      refreshHistory();
    });
  }

  function bind() {
    $('#theme-btn').addEventListener('click', function () {
      setTheme(state.theme === 'dark' ? 'light' : 'dark', true);
    });
    $('#history-select').addEventListener('change', function (ev) {
      loadHistory(ev.target.value);
    });
    $('#back-btn').addEventListener('click', function () {
      $('#history-select').value = '';
      refreshLatestNow();
    });
    $('#drawer-close').addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') closeDrawer();
    });
    canvasRoot.addEventListener('click', function (ev) {
      var el = ev.target && ev.target.closest ? ev.target.closest('[data-drawer]') : null;
      if (!el) return;
      var payload = state.drawer[el.getAttribute('data-drawer')];
      if (payload) openDrawer(payload);
    });
    canvasRoot.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      var el = ev.target && ev.target.closest ? ev.target.closest('[data-drawer]') : null;
      if (!el) return;
      ev.preventDefault();
      var payload = state.drawer[el.getAttribute('data-drawer')];
      if (payload) openDrawer(payload);
    });
    window.addEventListener('resize', function () {
      clearTimeout(bind._t);
      bind._t = setTimeout(function () {
        fitMindmap();
        fitGraphs();
      }, 180);
    });
  }

  bind();
  setTheme(state.theme, false);
  setMode(state.mode);
  rebuildViewSwitch();
  applySubview();
  poll();
  setInterval(poll, 1000);
})();
</script>
</body>
</html>
"""


def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


class Handler(BaseHTTPRequestHandler):
    server_version = "VisualReply/2.0"
    protocol_version = "HTTP/1.1"

    cache_dir: Path = DEFAULT_CACHE_DIR

    # ---------- 输出工具 ----------

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
        }

    def _history_files(self) -> list[Path]:
        hist = self.cache_dir / "history"
        if not hist.is_dir():
            return []
        files = []
        for path in hist.iterdir():
            if not path.is_file():
                continue
            name = path.name
            if name.endswith(".meta.json"):
                continue
            fmt = EXT_FORMAT.get(path.suffix.lower())
            if fmt is None:
                continue
            # 旧版 `.md` 归档的标题 sidecar 是同名 `.json`，跳过它
            if fmt == "json" and path.with_suffix(".md").is_file():
                continue
            files.append(path)
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:HISTORY_LIMIT]

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

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        if path == "/":
            self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path == "/healthz":
            self._json({"ok": True, "service": SERVICE_NAME, "version": VERSION,
                        "pid": os.getpid(),
                        "port": self.server.server_address[1],
                        "cache_dir": str(self.cache_dir)})
            return
        if path == "/api/latest":
            self._json(self._latest())
            return
        if path == "/api/history":
            items = [self._history_item(p) for p in self._history_files()]
            self._json({"version": VERSION, "items": items, "count": len(items)})
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

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def log_request(self, code="-", size="-") -> None:
        pass


class VisualReplyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="visual-reply 本地预览服务")
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

    handler = type("BoundHandler", (Handler,), {"cache_dir": cache_dir})
    httpd = VisualReplyServer((args.host, args.port), handler)
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
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print(f"{SERVICE_NAME} v{VERSION} listening on http://{args.host}:{port} (cache={cache_dir})",
          file=sys.stderr, flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
