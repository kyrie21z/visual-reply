#!/usr/bin/env python3
"""把结构化内容发布到 visual-reply 本地预览服务（v2）。

支持三种载荷格式：
    json  Visual Canvas 交互画布（sections: kpis / graph / comparison / bento / steps）
    html  HTML Artifact 沙箱（iframe 渲染）
    md    兼容模式（阅读 / 脑图 / 分屏）

用法示例：
    python3 publish.py --title "架构对比" --format json --file canvas.json
    python3 publish.py --title "交互原型" --format html --file demo.html
    python3 publish.py --title "说明" --stdin
    python3 publish.py --title "方案" --stdin --port 18800 --cache-dir /tmp/vr

行为：
    1. 检测 / 校验载荷格式，写入 <cache>/latest.<ext> 与 meta.json（version=2）；
    2. 在 <cache>/history/ 归档载荷与独立 sidecar（.<ext>.meta.json）；
    3. 端口未监听时后台启动 server.py；若发现旧版 v1 服务则接管重启；
    4. 仅在本次新拉起服务且未指定 --no-browser 时打开浏览器；
    5. 输出 JSON 状态。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

DEFAULT_PORT = 18765
DEFAULT_CACHE_DIR = Path("~/.cache/agent-visual-reply").expanduser()
SERVICE_NAME = "visual-reply"
SERVER_SCRIPT = Path(__file__).resolve().parent / "server.py"

VERSION = 3
FORMAT_EXT = {"json": ".json", "html": ".html", "md": ".md"}

# 长度提醒阈值：只警告，不阻塞发布
HEADLINE_MAX_CHARS = 40
EDGE_LABEL_MAX_CHARS = 14


def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def slugify(title: str, limit: int = 40) -> str:
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "-", title).strip(" .-")
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return (text[:limit].strip("-") or "reply")


def first_heading(text: str) -> str:
    for line in text.splitlines()[:40]:
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def detect_format(content: str) -> str:
    """按内容自动判定载荷格式。"""
    text = content.lstrip("\ufeff \t\r\n")
    if not text:
        return "md"

    if text[0] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict) and any(k in data for k in ("sections", "nodes", "cards")):
            return "json"

    head = text[:64].lower()
    for prefix in ("<!doctype", "<html", "<svg", "<div"):
        if head.startswith(prefix):
            return "html"
    return "md"


def derive_title(fmt: str, content: str) -> str:
    if fmt == "json":
        try:
            data = json.loads(content)
        except ValueError:
            return ""
        if isinstance(data, dict):
            for key in ("title", "headline"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""
    if fmt == "html":
        match = re.search(r"<title[^>]*>(.*?)</title>", content, re.I | re.S)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
        return ""
    return first_heading(content)


def graph_sections(data: dict) -> list:
    """收集所有拓扑图 section（含顶层 nodes/edges 的简写形态）。"""
    out = []
    sections = data.get("sections")
    if isinstance(sections, list):
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            if sec.get("type") == "graph" or (sec.get("type") is None and isinstance(sec.get("edges"), list)):
                out.append(sec)
    if isinstance(data.get("edges"), list):
        out.append({"type": "graph", "edges": data["edges"]})
    return out


def length_warnings(data: dict) -> list:
    """长度超限提醒：只随返回 JSON 回传，不影响发布与返回码。"""
    warnings = []
    headline = data.get("headline")
    if isinstance(headline, str):
        text = headline.strip()
        if len(text) > HEADLINE_MAX_CHARS:
            warnings.append(
                f"headline 超过 {HEADLINE_MAX_CHARS} 字（当前 {len(text)} 字），"
                "首屏会挤压换行，建议压缩到 30 字以内"
            )
    for gi, sec in enumerate(graph_sections(data), 1):
        edges = sec.get("edges")
        if not isinstance(edges, list):
            continue
        for ei, edge in enumerate(edges, 1):
            if not isinstance(edge, dict):
                continue
            label = edge.get("label")
            if not isinstance(label, str):
                continue
            text = label.strip()
            if len(text) > EDGE_LABEL_MAX_CHARS:
                warnings.append(
                    f"第 {gi} 个拓扑图第 {ei} 条边标签超过 {EDGE_LABEL_MAX_CHARS} 字"
                    f"（当前 {len(text)} 字）：{text[:24]}"
                )
    return warnings


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def read_content(args: argparse.Namespace) -> str:
    if args.file:
        path = Path(args.file).expanduser()
        if not path.is_file():
            raise SystemExit(json.dumps({
                "ok": False, "error": f"文件不存在: {path}"
            }, ensure_ascii=False))
        return path.read_text(encoding="utf-8", errors="replace")
    if args.stdin or not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit(json.dumps({
        "ok": False,
        "error": "没有输入内容：请使用 --stdin 管道传入，或使用 --file 指定文件",
    }, ensure_ascii=False))


def http_json(url: str, timeout: float = 1.5):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def health(port: int, timeout: float = 1.0):
    try:
        return http_json(f"http://127.0.0.1:{port}/healthz", timeout=timeout)
    except (urllib.error.URLError, socket.timeout, ValueError, OSError):
        return None


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


def browser_candidates(url: str) -> list[tuple[str, list[str]]]:
    """按平台返回可用的浏览器启动命令，越靠前优先级越高。"""
    candidates: list[tuple[str, list[str]]] = []

    def add(name: str) -> None:
        path = shutil.which(name)
        if path:
            candidates.append((name, [path, url]))

    if sys.platform == "darwin":
        # macOS 的 open 是系统命令，优先使用；Linux 上其它名为 open 的程序语义不同
        add("open")
    else:
        # Linux 桌面（含 WSLg）与 WSL 下的浏览器桥接命令
        add("xdg-open")
        add("wslview")
        explorer = shutil.which("explorer.exe")
        if not explorer:
            # WSL 默认不把 Windows 可执行文件目录放进 PATH
            fallback = "/mnt/c/Windows/explorer.exe"
            if os.path.exists(fallback):
                explorer = fallback
        if explorer:
            candidates.append(("explorer.exe", [explorer, url]))
        # Windows 原生 Python
        if os.name == "nt":
            add("explorer.exe")

    return candidates


def open_browser(url: str) -> str | None:
    """尽力打开浏览器，返回实际使用的启动方式；失败返回 None。"""
    for name, cmd in browser_candidates(url):
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            return name
        except OSError:
            continue

    # 兜底：标准库 webbrowser（某些环境下不可用会抛异常）
    try:
        if webbrowser.open(url):
            return "webbrowser"
    except Exception:  # noqa: BLE001 - 打开浏览器失败不应影响发布结果
        pass
    return None


def start_server(port: int, cache_dir: Path) -> subprocess.Popen:
    log_path = cache_dir / "server.log"
    log = open(log_path, "ab")
    try:
        return subprocess.Popen(
            [sys.executable, str(SERVER_SCRIPT),
             "--port", str(port), "--cache-dir", str(cache_dir)],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(SERVER_SCRIPT.parent),
        )
    finally:
        log.close()


def wait_ready(port: int, proc: subprocess.Popen | None, cache_dir: Path,
               timeout: float = 12.0, expect_version: int | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = health(port)
        if info and info.get("service") == SERVICE_NAME:
            if expect_version is None or int(info.get("version", 1) or 1) >= expect_version:
                return info
        if proc is not None and proc.poll() is not None:
            break
        time.sleep(0.1)
    tail = ""
    log_path = cache_dir / "server.log"
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-8:])
        except OSError:
            pass
    raise RuntimeError("服务启动失败或健康检查超时" + (f"\n--- server.log ---\n{tail}" if tail else ""))


def archive(cache_dir: Path, title: str, content: str, fmt: str) -> tuple[Path, Path]:
    """归档载荷到 history/，并写入独立的 .meta.json sidecar。"""
    history = cache_dir / "history"
    history.mkdir(parents=True, exist_ok=True)
    ext = FORMAT_EXT[fmt]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{slugify(title)}"

    # 同一时间戳下按“词干”去重，避免 json 载荷与 md 归档同名而互相遮挡
    used = set()
    for item in history.iterdir():
        if not item.is_file() or item.name.endswith(".meta.json"):
            continue
        if item.suffix in (".md", ".json", ".html"):
            used.add(item.stem)

    stem = base
    index = 2
    while stem in used:
        stem = f"{base}-{index}"
        index += 1
    target = history / f"{stem}{ext}"

    target.write_text(content, encoding="utf-8")
    sidecar = target.with_name(target.name + ".meta.json")
    sidecar.write_text(json.dumps({
        "version": VERSION,
        "format": fmt,
        "title": title,
        "created_at": iso_time(time.time()),
        "updated_at": iso_time(target.stat().st_mtime),
        "chars": len(content),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return target, sidecar


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="发布可视化内容到 visual-reply 预览服务")
    parser.add_argument("--title", default="", help="标题")
    parser.add_argument("--file", help="从文件读取内容")
    parser.add_argument("--stdin", action="store_true", help="从标准输入读取内容")
    parser.add_argument("--format", default="auto", choices=("auto", "json", "html", "md"),
                        help="载荷格式（默认 auto 自动识别）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"服务端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                        help=f"缓存目录（默认 {DEFAULT_CACHE_DIR}）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cache_dir = Path(args.cache_dir).expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "history").mkdir(parents=True, exist_ok=True)

    content = read_content(args)
    if not content or not content.strip():
        print(json.dumps({"ok": False, "error": "内容为空，未发布"}, ensure_ascii=False))
        return 1

    # 0. 确定格式
    fmt = args.format if args.format != "auto" else detect_format(content)
    parsed = None
    if fmt == "json":
        try:
            parsed = json.loads(content)
        except ValueError as exc:
            print(json.dumps({
                "ok": False, "format": "json",
                "error": f"JSON 解析失败: {exc}",
            }, ensure_ascii=False))
            return 1
        if not isinstance(parsed, dict):
            print(json.dumps({
                "ok": False, "format": "json",
                "error": "JSON 顶层必须是对象（含 sections / nodes / cards）",
            }, ensure_ascii=False))
            return 1

    title = (args.title or derive_title(fmt, content) or "未命名回复").strip()

    # 1. 写入最新载荷
    ext = FORMAT_EXT[fmt]
    latest = cache_dir / f"latest{ext}"
    atomic_write(latest, content)
    mtime = latest.stat().st_mtime
    atomic_write(cache_dir / "meta.json", json.dumps({
        "version": VERSION,
        "format": fmt,
        "title": title,
        "mtime": mtime,
        "updated_at": iso_time(mtime),
        "chars": len(content),
        "payload_file": latest.name,
    }, ensure_ascii=False, indent=2))

    # 2. 归档历史
    history_file, history_meta = archive(cache_dir, title, content, fmt)

    # 3. 确保服务在线（必要时接管旧版 v1）
    started = False
    proc = None
    info = health(args.port)
    if info and info.get("service") == SERVICE_NAME:
        running_cache = Path(info.get("cache_dir", "")).resolve()
        if running_cache != cache_dir:
            print(json.dumps({
                "ok": False,
                "error": "该端口已被另一个 visual-reply 实例占用（缓存目录不同）",
                "port": args.port,
                "running_cache_dir": str(running_cache),
                "requested_cache_dir": str(cache_dir),
            }, ensure_ascii=False))
            return 2

        running_version = int(info.get("version", 1) or 1)
        if running_version < VERSION:
            pid = info.get("pid")
            if not isinstance(pid, int) or pid <= 0:
                print(json.dumps({
                    "ok": False,
                    "error": "检测到旧版服务但缺少 pid，无法自动接管，请手动结束该进程",
                    "port": args.port,
                    "running_version": running_version,
                }, ensure_ascii=False))
                return 2
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                print(json.dumps({
                    "ok": False,
                    "error": f"无法终止旧版服务 pid={pid}: {exc}",
                    "port": args.port,
                }, ensure_ascii=False))
                return 2
            if not wait_port_closed(args.port):
                print(json.dumps({
                    "ok": False,
                    "error": "旧版服务未在超时内释放端口",
                    "port": args.port,
                    "pid": pid,
                }, ensure_ascii=False))
                return 2
            try:
                proc = start_server(args.port, cache_dir)
                info = wait_ready(args.port, proc, cache_dir, expect_version=VERSION)
            except RuntimeError as exc:
                print(json.dumps({"ok": False, "error": str(exc), "port": args.port},
                                 ensure_ascii=False))
                return 2
            started = True
            server_state = "upgraded"
        else:
            server_state = "reused"
    elif port_open(args.port):
        print(json.dumps({
            "ok": False,
            "error": f"端口 {args.port} 已被其他进程占用",
            "port": args.port,
        }, ensure_ascii=False))
        return 2
    else:
        proc = start_server(args.port, cache_dir)
        started = True
        try:
            info = wait_ready(args.port, proc, cache_dir, expect_version=VERSION)
        except RuntimeError as exc:
            print(json.dumps({"ok": False, "error": str(exc), "port": args.port},
                             ensure_ascii=False))
            return 2
        server_state = "started"

    # 4. 仅在本次拉起服务时打开浏览器
    url = f"http://127.0.0.1:{args.port}"
    browser = None
    if started and not args.no_browser:
        browser = open_browser(url)

    result = {
        "ok": True,
        "version": VERSION,
        "format": fmt,
        "title": title,
        "url": url,
        "port": args.port,
        "cache_dir": str(cache_dir),
        "latest": str(latest),
        "history_file": str(history_file),
        "history_meta": str(history_meta),
        "chars": len(content),
        "server": server_state,
        "server_version": info.get("version") if info else None,
        "browser": browser,
    }
    warnings = length_warnings(parsed) if isinstance(parsed, dict) else []
    if warnings:
        result["warnings"] = warnings
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
