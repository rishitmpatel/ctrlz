"""Claude Code hook integration: merge/unmerge our PostToolUse hook in a settings.json safely."""

from __future__ import annotations

import json
import os
import select
import sys
import threading
import time
import uuid
from pathlib import Path

from .store import StoreError

MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"
COMMAND = "ctrlz snap --hook -q"
EVENT = "PostToolUse"


def _is_ours(hook: object) -> bool:
    return isinstance(hook, dict) and isinstance(hook.get("command"), str) and hook["command"].strip().startswith("ctrlz snap --hook")


def settings_path(project_root: Path, global_: bool) -> Path:
    if global_:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "settings.json"
    return project_root / ".claude" / "settings.json"


def _load(path: Path) -> tuple[dict, int]:
    if not path.exists():
        return {}, 2
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}, 2
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise StoreError(f"{path} is not valid JSON ({e}); fix it first - I did not change anything")
    if not isinstance(data, dict):
        raise StoreError(f"{path} does not contain a JSON object; I did not change anything")
    indent = 2
    for line in raw.splitlines()[1:3]:
        stripped = line.lstrip(" ")
        if stripped and line.startswith(" "):
            indent = len(line) - len(stripped)
            break
        if line.startswith("\t"):
            indent = "\t"  # type: ignore[assignment]
            break
    return data, indent  # type: ignore[return-value]


def _save(path: Path, data: dict, indent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def is_installed(data: dict) -> bool:
    groups = (data.get("hooks") or {}).get(EVENT) if isinstance(data.get("hooks"), dict) else None
    if not isinstance(groups, list):
        return False
    return any(isinstance(g, dict) and any(_is_ours(h) for h in (g.get("hooks") or [])) for g in groups)


def install(path: Path) -> bool:
    """Returns True if the file changed."""
    data, indent = _load(path)
    if is_installed(data):
        return False
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise StoreError(f"{path}: \"hooks\" is not an object; I did not change anything")
    groups = hooks.setdefault(EVENT, [])
    if not isinstance(groups, list):
        raise StoreError(f"{path}: hooks.{EVENT} is not a list; I did not change anything")
    groups.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": COMMAND}]})
    _save(path, data, indent)
    return True


def uninstall(path: Path) -> bool:
    if not path.exists():
        return False
    data, indent = _load(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get(EVENT), list):
        return False
    changed = False
    new_groups = []
    for g in hooks[EVENT]:
        if isinstance(g, dict) and isinstance(g.get("hooks"), list) and any(_is_ours(h) for h in g["hooks"]):
            rest = [h for h in g["hooks"] if not _is_ours(h)]
            changed = True
            if rest:
                g = dict(g, hooks=rest)
                new_groups.append(g)
            continue
        new_groups.append(g)
    if not changed:
        return False
    if new_groups:
        hooks[EVENT] = new_groups
    else:
        del hooks[EVENT]
        if not hooks:
            del data["hooks"]
    _save(path, data, indent)
    return True


THREAD_LEFT_RUNNING = False  # set when a blocked stdin reader thread had to be abandoned (Windows)


def read_payload(timeout: float = 1.0) -> dict:
    """Read the hook JSON from stdin without ever blocking longer than `timeout`."""
    global THREAD_LEFT_RUNNING
    try:
        if sys.stdin is None or sys.stdin.closed:
            return {}
        try:
            if sys.stdin.isatty():
                return {}
            fd = sys.stdin.fileno()
        except (ValueError, OSError):
            return {}
        chunks: list[bytes] = []
        deadline = time.monotonic() + timeout
        if os.name == "posix":
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                r, _, _ = select.select([fd], [], [], left)
                if not r:
                    break
                data = os.read(fd, 65536)
                if not data:
                    break  # EOF: complete payload
                chunks.append(data)
                if sum(map(len, chunks)) > 4_000_000:
                    break
        else:  # pragma: no cover - windows: select() does not work on pipes
            def reader() -> None:
                try:
                    chunks.append(sys.stdin.buffer.read())
                except Exception:
                    pass

            t = threading.Thread(target=reader, daemon=True)
            t.start()
            t.join(timeout)
            if t.is_alive():
                THREAD_LEFT_RUNNING = True
        raw = b"".join(chunks)
        if not raw.strip():
            return {}
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def hook_main(argv: list[str]) -> int:
    """`ctrlz snap --hook`: must never fail, never hang, never print, never import heavy modules."""
    import subprocess

    from .store import Store

    label_override = None
    for i, a in enumerate(argv):
        if a in ("-m", "--message", "--label") and i + 1 < len(argv):
            label_override = argv[i + 1]
    try:
        payload = read_payload(_env_float("CTRLZ_HOOK_STDIN_TIMEOUT", 1.0))
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) and payload.get("cwd") else os.getcwd()
        tool = payload.get("tool_name") if isinstance(payload.get("tool_name"), str) else None
        label = label_override or (f"claude: {tool}" if tool else "claude")
        command = None
        ti = payload.get("tool_input")
        if isinstance(ti, dict) and isinstance(ti.get("command"), str):
            command = ti["command"][:500]
        try:
            store = Store.open(Path(cwd))
        except StoreError:
            store = Store.open(Path(cwd), create=True)  # quietly set up on first use
            # the first snapshot of a big project can take a while: do it detached, don't block the agent
            kw: dict = {}
            if os.name == "nt":  # pragma: no cover
                kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
            else:
                kw["start_new_session"] = True
            subprocess.Popen(
                [sys.executable, "-m", "ctrlz", "snap", "-q", "--trigger", "hook", "-m", label],
                cwd=str(store.root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, **kw,
            )
            return _finish(0)
        store.lock_timeout = _env_float("CTRLZ_HOOK_LOCK_TIMEOUT", 1.5)
        try:
            store.snap("hook", label, command)
        except BaseException as e:
            try:
                p = store.store_dir / "hook-errors.log"
                if p.exists() and p.stat().st_size > 100_000:
                    p.unlink()
                with open(p, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {e}\n")
            except OSError:
                pass
    except BaseException:
        pass
    return _finish(0)


def _finish(rc: int) -> int:
    if THREAD_LEFT_RUNNING:  # a stdin reader is stuck; do not let interpreter shutdown trip over it
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(rc)
    return rc
