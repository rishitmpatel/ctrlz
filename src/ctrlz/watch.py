"""Filesystem watcher (watchdog, with a polling fallback) and the background daemon."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .lock import OWN_CHILDREN, FileLock, pid_alive
from .store import Snapshot, Store, StoreError, TMP_PREFIX, _now

IGNORE_REFRESH_S = 30.0
_EVENTS = {"created", "deleted", "modified", "moved"}


class _EventFilter:
    """Cheap, allocation-light relevance check for raw watchdog events."""

    def __init__(self, store: Store, on_ignore_file: Callable[[], None]):
        self.root = str(store.root)
        self.prefix = self.root.rstrip(os.sep) + os.sep
        self.store_dir = str(store.store_dir)
        self.ignored: set[str] = set()
        self.on_ignore_file = on_ignore_file

    def _rel(self, path) -> str | None:
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if path.startswith(self.prefix):
            return path[len(self.prefix):].replace(os.sep, "/")
        if path == self.root:
            return ""
        return None

    def path_relevant(self, path) -> bool:
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if path == self.store_dir or path.startswith(self.store_dir + os.sep):
            return False
        rel = self._rel(path)
        if rel is None or rel == "":
            return False
        parts = rel.split("/")
        if parts[0] in (".git", ".ctrlz"):
            return False
        if parts[-1].startswith(TMP_PREFIX):
            return False
        if parts[-1] in (".gitignore", ".ctrlzignore"):
            self.on_ignore_file()
            return True
        ign = self.ignored
        if ign:
            acc = ""
            for c in parts:
                acc = c if not acc else acc + "/" + c
                if acc in ign:
                    return False
        return True

    def relevant(self, event) -> bool:
        et = event.event_type
        if et not in _EVENTS:
            return False  # opened / closed / closed_no_write would make us react to our own reads
        if event.is_directory and et == "modified":
            return False
        if self.path_relevant(event.src_path):
            return True
        dest = getattr(event, "dest_path", None)
        return bool(dest) and self.path_relevant(dest)


def watch(
    store: Store,
    debounce_ms: int | None = None,
    on_snap: Callable[[Snapshot], None] | None = None,
    stop: threading.Event | None = None,
    *,
    max_latency_s: float = 10.0,
    poll_interval_s: float = 2.0,
    force_polling: bool = False,
    on_error: Callable[[BaseException], None] | None = None,
    stop_file: Path | None = None,
    on_ready: Callable[[str], None] | None = None,
    trigger: str = "watch",
) -> None:
    """Block, snapshotting `store` after changes settle. Returns when `stop` is set."""
    stop = stop or threading.Event()
    debounce = (debounce_ms if debounce_ms is not None else int(store.config.get("debounce_ms", 1500))) / 1000.0
    cv = threading.Condition()
    state: dict = {"first": None, "last": None, "ign_dirty": True}

    def mark() -> None:
        now = time.monotonic()
        with cv:
            if state["first"] is None:
                state["first"] = now
            state["last"] = now
            cv.notify_all()

    def ignore_file_changed() -> None:
        state["ign_dirty"] = True

    flt = _EventFilter(store, ignore_file_changed)

    def handler_cb(event) -> None:
        try:
            if flt.relevant(event):
                mark()
        except Exception:  # never let a weird event kill the observer thread
            mark()

    observer = None
    mode = "polling"
    if not force_polling:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            class _H(FileSystemEventHandler):
                def on_any_event(self, event):  # noqa: N802
                    handler_cb(event)

            observer = Observer()
            observer.schedule(_H(), str(store.root), recursive=True)
            observer.start()
            mode = "watchdog"
        except Exception:
            observer = None
            mode = "polling"
    if on_ready:
        on_ready(mode)

    def do_snap() -> None:
        try:
            s = store.snap(trigger)
        except StoreError as e:
            if on_error:
                on_error(e)
            mark()  # retry after the debounce (e.g. store was busy)
            return
        except Exception as e:  # keep the watcher alive no matter what
            if on_error:
                on_error(e)
            mark()
            return
        if s is not None and on_snap:
            try:
                on_snap(s)
            except Exception as e:
                if on_error:
                    on_error(e)

    def refresh_ignored() -> None:
        try:
            flt.ignored = store.ignored_paths()
        except Exception:
            pass
        state["ign_dirty"] = False
        state["ign_at"] = time.monotonic()

    try:
        refresh_ignored()
        do_snap()  # capture the state at start-up
        while not stop.is_set():
            if stop_file is not None and stop_file.exists():
                break
            if mode == "polling":
                if stop.wait(poll_interval_s):
                    break
                do_snap()
                continue
            with cv:
                first, last = state["first"], state["last"]
                if first is None:
                    cv.wait(0.25)
                    continue
                now = time.monotonic()
                due = min(last + debounce, first + max_latency_s)
                if now < due:
                    cv.wait(min(due - now, 0.25))
                    continue
                state["first"] = state["last"] = None
            if state["ign_dirty"] or time.monotonic() - state.get("ign_at", 0) > IGNORE_REFRESH_S:
                refresh_ignored()
            do_snap()
        # flush anything still pending so the very last edits are never lost
        if mode == "watchdog" and state["first"] is not None:
            state["first"] = state["last"] = None
            do_snap()
    finally:
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=3)
            except Exception:
                pass


# ----------------------------------------------------------------------------------------------
# daemon
# ----------------------------------------------------------------------------------------------
_CHILDREN: dict[int, subprocess.Popen] = {}


def _forget_child(pid: int) -> None:
    proc = _CHILDREN.pop(pid, None)
    OWN_CHILDREN.discard(pid)
    if proc is not None:
        try:
            proc.poll()
        except Exception:
            pass


def _paths(store: Store) -> tuple[Path, Path, Path]:
    d = store.store_dir
    return d / "watch.pid", d / "watch.log", d / "watch.stop"


def _read_pidfile(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) and isinstance(d.get("pid"), int) else None
    except (OSError, ValueError):
        return None


def _is_our_process(pid: int) -> bool:
    if not pid_alive(pid):
        return False
    try:  # guard against pid reuse where we can
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        return b"ctrlz" in cmd
    except OSError:
        return True


def daemon_status(store: Store) -> dict:
    pidfile, log, _ = _paths(store)
    info = _read_pidfile(pidfile)
    running = bool(info) and _is_our_process(info["pid"])
    return {
        "running": running,
        "pid": info["pid"] if running else None,
        "started": info.get("started") if running else None,
        "log": str(log),
        "pidfile": str(pidfile),
    }


def start_daemon(store: Store) -> dict:
    """Spawn the detached watcher. Returns daemon_status() (raises StoreError if it fails to come up)."""
    pidfile, log, stopfile = _paths(store)
    with FileLock(store.store_dir / "daemon.lock", timeout=10):
        st = daemon_status(store)
        if st["running"]:
            st["already"] = True
            return st
        try:
            pidfile.unlink()
        except OSError:
            pass
        try:
            stopfile.unlink()
        except OSError:
            pass
        logf = open(log, "ab")
        kwargs: dict = {}
        if os.name == "nt":  # pragma: no cover
            kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "ctrlz", "_daemon", str(store.root)],
            stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, cwd=str(store.root), close_fds=True, env=env, **kwargs,
        )
        logf.close()
        _CHILDREN[proc.pid] = proc
        OWN_CHILDREN.add(proc.pid)
        pidfile.write_text(json.dumps({"pid": proc.pid, "started": _now().isoformat(timespec="seconds"),
                                       "root": str(store.root)}))
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:  # wait until it is demonstrably up (or dead)
            if proc.poll() is not None:
                break
            try:
                if b"mode:" in log.read_bytes():
                    break
            except OSError:
                pass
            time.sleep(0.03)
        if proc.poll() is not None:
            _forget_child(proc.pid)
            try:
                pidfile.unlink()
            except OSError:
                pass
            tail = ""
            try:
                tail = log.read_text(errors="replace").strip().splitlines()[-1]
            except (OSError, IndexError):
                pass
            raise StoreError(f"the watcher exited immediately{': ' + tail if tail else ''} (see {log})")
    st = daemon_status(store)
    st["already"] = False
    return st


def stop_daemon(store: Store, timeout: float = 10.0) -> bool:
    """Stop the watcher. Returns True if one was running. Verifies the process is really gone."""
    pidfile, _, stopfile = _paths(store)
    info = _read_pidfile(pidfile)
    if not info or not _is_our_process(info["pid"]):
        try:
            pidfile.unlink()
        except OSError:
            pass
        return False
    pid = info["pid"]
    try:
        stopfile.write_text("stop")
    except OSError:
        pass
    if os.name == "posix":
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.05)
    if pid_alive(pid):  # did not exit gracefully: force
        if os.name == "posix":
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        else:  # pragma: no cover
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        t2 = time.monotonic() + 5
        while time.monotonic() < t2 and pid_alive(pid):
            time.sleep(0.05)
        if pid_alive(pid):
            raise StoreError(f"could not stop the watcher (pid {pid})")
    _forget_child(pid)
    for f in (pidfile, stopfile):
        try:
            f.unlink()
        except OSError:
            pass
    return True


def run_daemon(store: Store) -> int:
    """Entry point of the detached process (``ctrlz _daemon``)."""
    pidfile, _, stopfile = _paths(store)
    try:
        stopfile.unlink()
    except OSError:
        pass
    stop = threading.Event()

    def on_term(signum, frame):  # noqa: ARG001
        stop.set()

    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, on_term)
            except (OSError, ValueError):
                pass

    def log(msg: str) -> None:
        print(f"{_now().isoformat(timespec='seconds')} {msg}", flush=True)

    count = {"n": 0}

    def on_snap(s: Snapshot) -> None:
        count["n"] += 1
        log(f"snapshot {s.short} +{s.stats['added']} ~{s.stats['modified']} -{s.stats['deleted']}")
        if count["n"] % 200 == 0:
            store.pack()
            log("packed store")

    def on_error(e: BaseException) -> None:
        log(f"error: {e}")

    log(f"watcher started for {store.root} (pid {os.getpid()})")
    store.lock_timeout = 30.0
    try:
        watch(store, on_snap=on_snap, stop=stop, on_error=on_error, stop_file=stopfile,
              on_ready=lambda m: log(f"mode: {m}"))
    finally:
        info = _read_pidfile(pidfile)
        if info and info.get("pid") == os.getpid():
            try:
                pidfile.unlink()
            except OSError:
                pass
        try:
            stopfile.unlink()
        except OSError:
            pass
        log("watcher stopped")
    return 0
