"""Cross-platform advisory lock built on O_CREAT|O_EXCL.

The lock file holds ``{"pid", "host", "time", "token"}``.  A lock is *stale* when the holder is a dead
process on this host, or when nothing has touched the file for ``STALE_AFTER`` seconds (holders run a
heartbeat that touches it, so a slow-but-alive snapshot is never stolen).  Stale locks are reaped under a
second guard file so two reapers cannot both "win".
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

STALE_AFTER = 600.0  # 10 minutes without heartbeat
HEARTBEAT = 20.0


class LockTimeout(Exception):
    pass


# pids of child processes we spawned and are allowed to reap (see pid_alive)
OWN_CHILDREN: set[int] = set()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - windows only
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied -> exists
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        # reap if it is our own zombie child
        if pid in OWN_CHILDREN:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done == pid:
                    OWN_CHILDREN.discard(pid)
                    return False
            except ChildProcessError:
                OWN_CHILDREN.discard(pid)
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # zombie that is not our child?
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        state = data[data.rindex(b")") + 2 : data.rindex(b")") + 3]
        if state == b"Z":
            return False
    except (OSError, ValueError):
        pass
    return True


class _Shared:
    """Per-store-path, per-process state: threads of one process serialise on the RLock."""

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.hb_stop: threading.Event | None = None
        self.token: str | None = None


_REGISTRY: dict[str, _Shared] = {}
_REG_LOCK = threading.Lock()


def _shared(path: Path) -> _Shared:
    key = os.path.abspath(str(path))
    with _REG_LOCK:
        s = _REGISTRY.get(key)
        if s is None:
            s = _REGISTRY[key] = _Shared()
        return s


class FileLock:
    def __init__(self, path: Path, timeout: float = 60.0):
        self.path = Path(path)
        self.guard = Path(str(path) + ".reap")
        self.timeout = timeout
        self._s = _shared(self.path)

    # -- helpers -------------------------------------------------------------------------------
    def _read(self) -> tuple[dict | None, float | None]:
        try:
            st = os.stat(self.path)
            raw = self.path.read_bytes()
        except OSError:
            return None, None
        try:
            info = json.loads(raw.decode("utf-8"))
            if not isinstance(info, dict):
                info = None
        except ValueError:
            info = None
        return info, st.st_mtime

    def _stale(self, info: dict | None, mtime: float | None) -> bool:
        if mtime is None:
            return False  # vanished: just retry
        age = time.time() - mtime
        if info is None:
            return age > 5.0  # half-written / corrupt
        if age > STALE_AFTER:
            return True
        if info.get("host") == socket.gethostname():
            pid = info.get("pid")
            if isinstance(pid, int) and pid != os.getpid() and not pid_alive(pid):
                return True
        return False

    def _reap(self, seen_info: dict | None, seen_mtime: float | None) -> None:
        try:
            fd = os.open(self.guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.stat(self.guard).st_mtime > 30:
                    os.unlink(self.guard)
            except OSError:
                pass
            return
        except OSError:
            return
        try:
            os.close(fd)
            info, mtime = self._read()
            if info == seen_info and mtime == seen_mtime and self._stale(info, mtime):
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass
        finally:
            try:
                os.unlink(self.guard)
            except OSError:
                pass

    def _try_create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        token = uuid.uuid4().hex
        try:
            payload = {"pid": os.getpid(), "host": socket.gethostname(), "time": time.time(), "token": token}
            os.write(fd, json.dumps(payload).encode())
        finally:
            os.close(fd)
        self._s.token = token
        return True

    def _heartbeat(self, stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT):
            try:
                os.utime(self.path, None)
            except OSError:
                return

    # -- public --------------------------------------------------------------------------------
    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        if not self._s.rlock.acquire(timeout=max(0.0, self.timeout)):
            raise LockTimeout("another ctrlz operation in this process is taking too long")
        try:
            if self._s.depth > 0:
                self._s.depth += 1
                return
            delay = 0.005
            while True:
                if self._try_create():
                    break
                info, mtime = self._read()
                if self._stale(info, mtime):
                    self._reap(info, mtime)
                    continue
                if time.monotonic() >= deadline:
                    holder = f"pid {info.get('pid')}" if info else "another process"
                    raise LockTimeout(f"the ctrlz store is busy (locked by {holder})")
                time.sleep(delay)
                delay = min(delay * 1.5, 0.05)
            self._s.depth = 1
            stop = threading.Event()
            self._s.hb_stop = stop
            threading.Thread(target=self._heartbeat, args=(stop,), daemon=True).start()
        except BaseException:
            self._s.rlock.release()
            raise

    def release(self) -> None:
        s = self._s
        if s.depth <= 0:
            return
        s.depth -= 1
        if s.depth == 0:
            if s.hb_stop is not None:
                s.hb_stop.set()
            info, _ = self._read()
            # only remove the file if it is still ours
            if info is None or info.get("token") == s.token:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
            s.token = None
        s.rlock.release()

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
