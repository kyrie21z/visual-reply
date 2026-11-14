#!/usr/bin/env python3
"""visual-reply v4 共享存储层（纯标准库）。

职责：
    1. 跨平台进程锁：`<cache>/.publish.lock`
       POSIX 用 fcntl.flock，Windows 用 msvcrt.locking（先保证文件至少 1 字节）；
    2. 会话识别：显式 --session > 环境变量 > 终端复用器 > 父进程兜底；
    3. 回复归档：sessions/<sid>/replies/<reply_id>.<ext> + sidecar，
       同时维护 state.json、global/feed.jsonl 与 v3 的 latest/meta/history 投影。

publish.py 与 server.py 都把脚本目录插入 sys.path 后 `import vr_store`，
因此直接执行与 importlib 按路径加载都能工作。本文件只依赖标准库。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

STORE_VERSION = 4

FORMAT_EXT = {"json": ".json", "html": ".html", "md": ".md"}
EXT_FORMAT = {".json": "json", ".html": "html", ".md": "md"}

#: 会话色板（前端左侧栏强调条），由 session_id 的 sha256 决定下标。
SESSION_COLORS = (
    "#2f6feb",
    "#16a34a",
    "#d97706",
    "#a855f7",
    "#0891b2",
    "#dc2626",
    "#4d7c0f",
    "#db2777",
)

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

#: 会话检测会读的环境变量；测试用它构造干净环境，避免宿主变量串味。
SESSION_ENV_KEYS = (
    "VISUAL_REPLY_SESSION",
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

_LOCK_NAME = ".publish.lock"
_SERVER_LOCK_NAME = ".server.lock"


class LockTimeout(TimeoutError):
    """等待 `.publish.lock` 超时。"""


def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def slugify(title: str, limit: int = 40) -> str:
    """人类可读的文件名片段（保留中文），用于 history/ 归档。"""
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "-", title or "")
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip(" .-")[:limit].strip(" .-") or "reply"


def safe_slug(title: str, limit: int = 32) -> str:
    """ASCII 片段，用于 reply_id —— 必须满足 ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$。"""
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", title or "")
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-._")[:limit].strip("-._") or "reply"


def is_safe_id(value: object) -> bool:
    """会话 ID / 回复 ID 白名单校验：首字符必须是字母数字，禁止路径分隔符。"""
    if not isinstance(value, str):
        return False
    if not value or ".." in value or "/" in value or "\\" in value or "\x00" in value:
        return False
    return bool(SESSION_ID_RE.match(value))


def normalize_id(raw: str, prefix: str = "s") -> str:
    """把外部 ID（UUID、路径、带空格的名称）收敛成安全可用的短 ID。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if is_safe_id(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def color_index(session_id: str) -> int:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % len(SESSION_COLORS)


# ---------------------------------------------------------------------------
# 跨平台进程锁
# ---------------------------------------------------------------------------


def _lock_acquire(handle, deadline: float) -> None:
    if os.name == "nt":  # pragma: no cover - Windows 分支
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() < 1:
            handle.write(b"\0")
            handle.flush()
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout("等待 .publish.lock 超时（Windows）") from None
                time.sleep(0.05)
    else:
        import fcntl

        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout("等待 .publish.lock 超时") from None
                time.sleep(0.05)


def _lock_release(handle) -> None:
    try:
        if os.name == "nt":  # pragma: no cover - Windows 分支
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float):
    """在任意锁文件上持排他锁；`cache_lock` 与 `server_lock` 共用这一份实现。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    deadline = time.monotonic() + max(0.1, float(timeout))
    try:
        _lock_acquire(handle, deadline)
        try:
            yield handle
        finally:
            _lock_release(handle)
    finally:
        handle.close()


@contextlib.contextmanager
def cache_lock(cache_dir: Path | str, timeout: float = 10.0):
    """在 `<cache>/.publish.lock` 上持排他锁，所有写操作都必须在锁内完成。

    锁的是独立空文件，绝不锁正在被 os.replace() 原子替换的 JSON。
    """
    with _file_lock(Path(cache_dir) / _LOCK_NAME, timeout) as handle:
        yield handle


@contextlib.contextmanager
def server_lock(cache_dir: Path | str, timeout: float = 15.0):
    """在 `<cache>/.server.lock` 上持排他锁，串行化冷启动时的服务拉起。

    多个 Agent 同时打到空缓存时，只有一个进程负责 `start_server()`，其余在锁外
    等待后直接复用，不会同时 bind 同一端口。
    """
    with _file_lock(Path(cache_dir) / _SERVER_LOCK_NAME, timeout) as handle:
        yield handle


# ---------------------------------------------------------------------------
# 会话识别
# ---------------------------------------------------------------------------


def detect_project(cwd: Path | str | None = None) -> tuple[str, Path]:
    """向上找到最近的 .git（目录或 worktree 文件），返回（项目名, 项目根）。"""
    start = Path(cwd or os.getcwd())
    try:
        start = start.resolve()
    except OSError:
        start = Path.cwd()
    current = start
    while True:
        if (current / ".git").exists():
            return (current.name or "workspace"), current
        if current.parent == current:
            break
        current = current.parent
    return (start.name or "workspace"), start


def _agent_env_session(env) -> tuple[str, str, str] | None:
    conversation = (env.get("ANTIGRAVITY_CONVERSATION_ID") or "").strip()
    if conversation:
        return normalize_id(conversation, "ag"), "antigravity", "env:ANTIGRAVITY_CONVERSATION_ID"
    project = (env.get("ANTIGRAVITY_PROJECT_ID") or "").strip()
    agent = (env.get("ANTIGRAVITY_AGENT") or "").strip()
    if project or agent:
        raw = f"antigravity|{project or 'project'}|{agent or 'agent'}"
        return normalize_id(raw, "ag"), "antigravity", "env:ANTIGRAVITY_PROJECT_ID"

    for key, client in (
        ("CODEX_THREAD_ID", "codex"),
        ("CODEX_SESSION_ID", "codex"),
        ("OPENCODE_SESSION_ID", "opencode"),
        ("GEMINI_SESSION_ID", "gemini"),
        ("CLAUDE_SESSION_ID", "claude"),
        ("CLAUDE_CODE_REPL_ID", "claude"),
    ):
        raw = (env.get(key) or "").strip()
        if raw:
            return normalize_id(raw, "s"), client, f"env:{key}"
    return None


def _terminal_session(env, project_root: Path) -> tuple[str, str] | None:
    for key in ("WT_SESSION", "TMUX_PANE", "WEZTERM_PANE", "KITTY_WINDOW_ID", "ITERM_SESSION_ID"):
        raw = (env.get(key) or "").strip()
        if raw:
            digest = hashlib.sha256(f"{key}:{raw}|{project_root}".encode("utf-8")).hexdigest()[:12]
            return f"t-{digest}", f"terminal:{key}"
    return None


def fallback_session_id(project_root: Path | str, ppid: int | None = None) -> str:
    """同一交互 shell / 父进程绑定同一会话，不同终端或子进程自然分开。"""
    parent = os.getppid() if ppid is None else int(ppid)
    return hashlib.sha256(f"{parent}|{project_root}".encode("utf-8")).hexdigest()[:12]


def resolve_session(explicit_session: str | None = None,
                    session_name: str | None = None,
                    agent: str | None = None,
                    env=None,
                    cwd: Path | str | None = None) -> dict:
    """零配置会话解析。

    优先级：显式 --session > VISUAL_REPLY_SESSION > Agent 客户端环境变量 >
    终端复用器 > sha256(ppid|项目根)。返回的 dict 可直接交给 commit_reply()。
    """
    environment = os.environ if env is None else env
    project, project_root = detect_project(cwd)

    session_id = ""
    client = (agent or "").strip()
    source = ""

    raw_explicit = (explicit_session or "").strip()
    if raw_explicit:
        if not is_safe_id(raw_explicit):
            raise ValueError(
                f"非法的会话 ID: {raw_explicit!r}"
                "（只允许字母数字开头，且仅含 A-Z a-z 0-9 . _ -，长度 1..80）"
            )
        session_id = raw_explicit
        source = "explicit"

    if not session_id:
        raw = (environment.get("VISUAL_REPLY_SESSION") or "").strip()
        if raw:
            session_id = normalize_id(raw, "s")
            source = "env:VISUAL_REPLY_SESSION"

    if not session_id:
        found = _agent_env_session(environment)
        if found:
            session_id, detected_client, source = found
            client = client or detected_client

    if not session_id:
        terminal = _terminal_session(environment, project_root)
        if terminal:
            session_id, source = terminal

    if not session_id:
        session_id = fallback_session_id(project_root)
        source = "fallback:ppid"

    if not client:
        client = "shell" if source.startswith(("fallback", "terminal")) else "agent"

    label = ""
    custom_label = False
    name = (session_name or "").strip()
    if name:
        label = name[:60]
        custom_label = True
    else:
        short = session_id[:6]
        label = f"{project} · {client} · {short}"

    index = color_index(session_id)
    return {
        "id": session_id,
        "label": label,
        "custom_label": custom_label,
        "client": client,
        "project": project,
        "project_root": str(project_root),
        "source": source,
        "color": SESSION_COLORS[index],
        "color_idx": index,
    }


# ---------------------------------------------------------------------------
# 原子读写
# ---------------------------------------------------------------------------


def _atomic_replace(tmp: Path, target: Path) -> None:
    for attempt in range(6):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:  # pragma: no cover - Windows 上文件被短暂占用
            if attempt == 5:
                raise
            time.sleep(0.03)


def atomic_write_text(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    _atomic_replace(tmp, path)


def atomic_write_json(path: Path, payload) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _tail_lines(path: Path, limit: int, chunk: int = 8192) -> list[str]:
    """从文件尾部倒着读，避免把持续追加的 JSONL 全量载入内存。"""
    if limit <= 0:
        return []
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            data = b""
            position = size
            while position > 0 and data.count(b"\n") <= limit:
                step = min(chunk, position)
                position -= step
                handle.seek(position)
                data = handle.read(step) + data
            lines = data.splitlines()
            return [line.decode("utf-8", errors="replace") for line in lines[-limit:]]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# 目录与查询
# ---------------------------------------------------------------------------


def sessions_root(cache_dir: Path | str) -> Path:
    return Path(cache_dir) / "sessions"


def session_dir(cache_dir: Path | str, session_id: str) -> Path:
    """带 containment 校验的会话目录；非法 ID 直接抛 ValueError。"""
    if not is_safe_id(session_id):
        raise ValueError("非法的会话 ID")
    base = sessions_root(cache_dir).resolve()
    target = (base / session_id).resolve()
    if target.parent != base:
        raise ValueError("会话 ID 越出缓存目录")
    return target


def replies_root(cache_dir: Path | str, session_id: str) -> Path:
    return session_dir(cache_dir, session_id) / "replies"


def read_state(cache_dir: Path | str) -> dict:
    data = read_json(Path(cache_dir) / "state.json", {})
    if not isinstance(data, dict):
        data = {}
    try:
        seq = int(data.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return {
        "version": STORE_VERSION,
        "seq": seq,
        "latest_session_id": data.get("latest_session_id") or "",
        "latest_reply_id": data.get("latest_reply_id") or "",
        "updated_at": data.get("updated_at") or "",
    }


def write_state(cache_dir: Path | str, state: dict) -> None:
    atomic_write_json(Path(cache_dir) / "state.json", state)


def read_session(cache_dir: Path | str, session_id: str) -> dict | None:
    try:
        path = session_dir(cache_dir, session_id) / "session.json"
    except ValueError:
        return None
    data = read_json(path, None)
    return data if isinstance(data, dict) and data.get("id") else None


def list_sessions(cache_dir: Path | str) -> list[dict]:
    root = sessions_root(cache_dir)
    items: list[dict] = []
    if not root.is_dir():
        return items
    try:
        entries = list(root.iterdir())
    except OSError:
        return items
    for entry in entries:
        if not entry.is_dir():
            continue
        data = read_json(entry / "session.json", None)
        if isinstance(data, dict) and data.get("id"):
            items.append(data)
    items.sort(key=lambda s: (1 if s.get("pinned") else 0, int(s.get("seq") or 0)), reverse=True)
    return items


def list_replies(cache_dir: Path | str, session_id: str, limit: int = 200) -> list[dict]:
    try:
        root = replies_root(cache_dir, session_id)
    except ValueError:
        return []
    if not root.is_dir():
        return []
    items: list[dict] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return items
    for entry in entries:
        if not entry.is_file() or not entry.name.endswith(".meta.json"):
            continue
        meta = read_json(entry, None)
        if isinstance(meta, dict) and meta.get("reply_id"):
            meta.setdefault("session_id", session_id)
            items.append(meta)
    items.sort(key=lambda m: (int(m.get("seq") or 0), str(m.get("reply_id") or "")), reverse=True)
    if limit and limit > 0:
        items = items[:limit]
    return items


def read_reply_meta(cache_dir: Path | str, session_id: str, reply_id: str) -> dict | None:
    if not is_safe_id(reply_id):
        return None
    try:
        root = replies_root(cache_dir, session_id)
    except ValueError:
        return None
    for ext in (".json", ".html", ".md"):
        meta = read_json(root / f"{reply_id}{ext}.meta.json", None)
        if isinstance(meta, dict):
            return meta
    return None


def reply_file(cache_dir: Path | str, session_id: str, reply_id: str) -> Path | None:
    if not is_safe_id(reply_id):
        return None
    try:
        root = replies_root(cache_dir, session_id)
    except ValueError:
        return None
    try:
        base = root.resolve()
    except OSError:
        return None
    for ext in (".json", ".html", ".md"):
        candidate = (root / f"{reply_id}{ext}").resolve()
        if candidate.parent != base:
            return None
        if candidate.is_file():
            return candidate
    return None


def read_reply(cache_dir: Path | str, session_id: str, reply_id: str) -> dict | None:
    path = reply_file(cache_dir, session_id, reply_id)
    if path is None:
        return None
    meta = read_reply_meta(cache_dir, session_id, reply_id) or {}
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    reply = {
        "reply_id": meta.get("reply_id") or reply_id,
        "session_id": session_id,
        "format": meta.get("format") or EXT_FORMAT.get(path.suffix.lower(), "md"),
        "title": meta.get("title") or "",
        "chars": int(meta.get("chars") or len(content)),
        "seq": int(meta.get("seq") or 0),
        "created_at": meta.get("created_at") or "",
        "updated_at": meta.get("updated_at") or iso_time(path.stat().st_mtime),
        "filename": path.name,
    }
    return {"reply": reply, "content": content}


def reply_event(cache_dir: Path | str, session_id: str, reply_id: str) -> dict:
    """构造一条 SSE / 轮询都能直接消费的事件。"""
    meta = read_reply_meta(cache_dir, session_id, reply_id) or {}
    session = read_session(cache_dir, session_id) or {}
    return {
        "seq": int(meta.get("seq") or 0),
        "session_id": session_id,
        "session_label": session.get("label") or session_id,
        "client": session.get("client") or "",
        "project": session.get("project") or "",
        "reply_id": reply_id,
        "format": meta.get("format") or "md",
        "title": meta.get("title") or "",
        "chars": int(meta.get("chars") or 0),
        "updated_at": meta.get("updated_at") or session.get("updated_at") or "",
    }


def feed_items(cache_dir: Path | str, limit: int = 50) -> list[dict]:
    path = Path(cache_dir) / "global" / "feed.jsonl"
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in _tail_lines(path, max(limit, 1) * 2):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("seq") is not None:
            rows.append(data)
    rows.reverse()
    return rows[:limit]


def state_events(cache_dir: Path | str, since: int = 0, limit: int = 200) -> list[dict]:
    """返回 seq > since 的事件，按 seq 升序，便于前端顺序补播。"""
    rows = feed_items(cache_dir, limit=max(limit * 2, 200))
    picked = [row for row in rows if int(row.get("seq") or 0) > int(since or 0)]
    picked.sort(key=lambda row: int(row.get("seq") or 0))
    return picked[-limit:] if limit else picked


def legacy_history_files(cache_dir: Path | str) -> list[Path]:
    hist = Path(cache_dir) / "history"
    if not hist.is_dir():
        return []
    files: list[Path] = []
    try:
        entries = list(hist.iterdir())
    except OSError:
        return []
    for path in entries:
        if not path.is_file() or path.name.endswith(".meta.json"):
            continue
        if path.suffix.lower() not in EXT_FORMAT:
            continue
        # v1 的 md 归档旁挂同名 .json sidecar，不能当成独立载荷
        if path.suffix.lower() == ".json" and path.with_suffix(".md").is_file():
            continue
        files.append(path)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def update_session(cache_dir: Path | str, session_id: str,
                   label: str | None = None, pinned: bool | None = None) -> dict | None:
    """更新可变的展示元数据（label / pinned）；调用方需持有 cache_lock。"""
    record = read_session(cache_dir, session_id)
    if record is None:
        return None
    if label is not None:
        text = str(label).strip()[:60]
        if text:
            record["label"] = text
            record["custom_label"] = True
    if pinned is not None:
        record["pinned"] = bool(pinned)
    record["updated_at"] = iso_time(time.time())
    atomic_write_json(session_dir(cache_dir, session_id) / "session.json", record)
    return record


# ---------------------------------------------------------------------------
# 写入一条回复（调用方必须持有 cache_lock）
# ---------------------------------------------------------------------------


def _pick_history_stem(history_dir: Path, base: str) -> str:
    used = set()
    try:
        for item in history_dir.iterdir():
            if item.is_file() and not item.name.endswith(".meta.json") and item.suffix.lower() in EXT_FORMAT:
                used.add(item.stem)
    except OSError:
        return base
    stem = base
    index = 2
    while stem in used:
        stem = f"{base}-{index}"
        index += 1
    return stem


def commit_reply(cache_dir: Path | str,
                 session: dict,
                 content: str,
                 fmt: str,
                 title: str) -> dict:
    """在锁内提交一条回复，返回回执所需的全部基本信息。"""
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    ext = FORMAT_EXT.get(fmt, ".md")
    session_id = session["id"]

    state = read_state(cache)
    seq = int(state.get("seq") or 0) + 1
    now_ts = time.time()
    now = iso_time(now_ts)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now_ts))
    slug = slugify(title)
    reply_id = f"{stamp}-{seq:06d}-{safe_slug(title)}"

    sdir = session_dir(cache, session_id)
    rdir = sdir / "replies"
    rdir.mkdir(parents=True, exist_ok=True)

    payload = rdir / f"{reply_id}{ext}"
    atomic_write_text(payload, content)
    mtime = payload.stat().st_mtime

    reply_meta = {
        "version": STORE_VERSION,
        "seq": seq,
        "session_id": session_id,
        "reply_id": reply_id,
        "format": fmt,
        "title": title,
        "chars": len(content),
        "filename": payload.name,
        "created_at": now,
        "updated_at": iso_time(mtime),
    }
    atomic_write_json(rdir / f"{reply_id}{ext}.meta.json", reply_meta)

    previous = read_json(sdir / "session.json", {})
    if not isinstance(previous, dict):
        previous = {}

    label = session.get("label") or ""
    custom_label = bool(session.get("custom_label"))
    if not (custom_label and label):
        if previous.get("custom_label") and previous.get("label"):
            label = str(previous["label"])
            custom_label = True
        else:
            label = label or f"{session.get('project') or 'workspace'} · {session.get('client') or 'agent'}"

    record = {
        "id": session_id,
        "label": label,
        "custom_label": custom_label,
        "client": session.get("client") or "agent",
        "project": session.get("project") or "workspace",
        "source": session.get("source") or "unknown",
        "color": session.get("color") or SESSION_COLORS[color_index(session_id)],
        "pinned": bool(previous.get("pinned")),
        "seq": seq,
        "last_reply_id": reply_id,
        "last_title": title,
        "last_format": fmt,
        "reply_count": int(previous.get("reply_count") or 0) + 1,
        "created_at": previous.get("created_at") or now,
        "updated_at": now,
    }
    atomic_write_json(sdir / "session.json", record)

    write_state(cache, {
        "version": STORE_VERSION,
        "seq": seq,
        "latest_session_id": session_id,
        "latest_reply_id": reply_id,
        "updated_at": now,
    })

    feed_dir = cache / "global"
    feed_dir.mkdir(parents=True, exist_ok=True)
    feed_line = {
        "seq": seq,
        "session_id": session_id,
        "session_label": label,
        "client": record["client"],
        "project": record["project"],
        "reply_id": reply_id,
        "format": fmt,
        "title": title,
        "chars": len(content),
        "updated_at": iso_time(mtime),
    }
    with open(feed_dir / "feed.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(feed_line, ensure_ascii=False) + "\n")

    # ---- v3 兼容投影 ----
    latest = cache / f"latest{ext}"
    atomic_write_text(latest, content)
    atomic_write_json(cache / "meta.json", {
        "version": STORE_VERSION,
        "format": fmt,
        "title": title,
        "mtime": mtime,
        "updated_at": iso_time(mtime),
        "chars": len(content),
        "payload_file": latest.name,
        "seq": seq,
        "session_id": session_id,
        "reply_id": reply_id,
    })

    history_dir = cache / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    stem = _pick_history_stem(history_dir, f"{stamp}-{slug}")
    history_file = history_dir / f"{stem}{ext}"
    atomic_write_text(history_file, content)
    history_meta = history_file.with_name(history_file.name + ".meta.json")
    atomic_write_json(history_meta, {
        "version": STORE_VERSION,
        "format": fmt,
        "title": title,
        "created_at": now,
        "updated_at": iso_time(history_file.stat().st_mtime),
        "chars": len(content),
        "seq": seq,
        "session_id": session_id,
        "reply_id": reply_id,
    })

    return {
        "seq": seq,
        "reply_id": reply_id,
        "reply_path": str(payload),
        "reply_meta": str(rdir / f"{reply_id}{ext}.meta.json"),
        "latest": str(latest),
        "meta": str(cache / "meta.json"),
        "history_file": str(history_file),
        "history_meta": str(history_meta),
        "session": record,
        "feed": feed_line,
        "mtime": mtime,
        "chars": len(content),
    }
