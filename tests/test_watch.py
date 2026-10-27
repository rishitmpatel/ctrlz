from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from ctrlz.lock import pid_alive
from ctrlz.store import Store
from ctrlz.watch import _EventFilter, daemon_status, start_daemon, stop_daemon, watch

from conftest import git_state


class Watcher:
    def __init__(self, store: Store, **kw):
        self.store = store
        self.stop = threading.Event()
        self.snaps = []
        self.calls = 0
        self.errors = []
        self.ready = threading.Event()
        orig = store.snap

        def counting(*a, **k):
            self.calls += 1
            return orig(*a, **k)

        store.snap = counting  # type: ignore[method-assign]
        self.thread = threading.Thread(
            target=watch, args=(store,), daemon=True,
            kwargs=dict(stop=self.stop, on_snap=self.snaps.append, on_error=self.errors.append,
                        on_ready=lambda m: self.ready.set(), **kw),
        )

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5)
        time.sleep(0.1)  # let the observer settle after the start-up snapshot
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(10)
        assert not self.thread.is_alive()

    def wait_for(self, n: int, timeout: float = 6.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if len(self.snaps) >= n:
                return True
            time.sleep(0.02)
        return False


def test_snapshot_after_quiet_period(plain):
    st = Store.open(plain, create=True)
    st.snap()
    with Watcher(st, debounce_ms=200) as w:
        t0 = time.monotonic()
        (plain / "new.txt").write_text("hello\n")
        assert w.wait_for(1)
        assert time.monotonic() - t0 >= 0.19  # waited for the debounce
        assert w.snaps[0].trigger == "watch" and "new.txt" in st.files("@")


def test_burst_is_coalesced_into_one_snapshot(plain):
    st = Store.open(plain, create=True)
    st.snap()
    with Watcher(st, debounce_ms=350) as w:
        for i in range(6):
            (plain / f"f{i}.txt").write_text(str(i))
            time.sleep(0.04)
        assert w.wait_for(1)
        time.sleep(0.5)
        assert len(w.snaps) == 1
        assert {f"f{i}.txt" for i in range(6)} <= set(st.files("@"))


def test_max_latency_forces_snapshots_during_continuous_writes(plain):
    st = Store.open(plain, create=True)
    st.snap()
    with Watcher(st, debounce_ms=400, max_latency_s=0.6) as w:
        end = time.monotonic() + 1.5
        i = 0
        while time.monotonic() < end:  # never quiet for 600 ms
            (plain / "busy.txt").write_text(f"{i}\n")
            i += 1
            time.sleep(0.06)
        assert len(w.snaps) >= 2, "continuous writes must still produce snapshots (max latency)"


def test_own_activity_and_git_internals_never_retrigger(git_repo):
    st = Store.open(git_repo, create=True)
    st.snap()
    before = git_state(git_repo, include_status=False)
    with Watcher(st, debounce_ms=150) as w:
        base = w.calls
        # noise inside .git (including the store itself) must be ignored
        (git_repo / ".git" / "some_file").write_text("x")
        (git_repo / ".git" / "ctrlz" / "noise").write_text("x")
        time.sleep(0.45)
        assert w.calls == base
        (git_repo / "real.txt").write_text("r\n")
        assert w.wait_for(1)
        time.sleep(0.5)  # a feedback loop would keep snapping
        assert w.calls == base + 1 and len(w.snaps) == 1
    (git_repo / ".git" / "some_file").unlink()
    assert git_state(git_repo, include_status=False) == before


def test_plain_folder_store_dir_is_ignored(plain):
    st = Store.open(plain, create=True)
    st.snap()
    with Watcher(st, debounce_ms=100) as w:
        base = w.calls
        (plain / ".ctrlz" / "noise").write_text("x")
        time.sleep(0.35)
        assert w.calls == base


def test_ignored_directories_do_not_wake_the_watcher(plain):
    (plain / ".gitignore").write_text("build/\n*.log\n")
    (plain / "build").mkdir()
    (plain / "build" / "seed").write_text("s")
    st = Store.open(plain, create=True)
    st.snap()
    with Watcher(st, debounce_ms=150) as w:
        base = w.calls
        for i in range(5):
            (plain / "build" / f"o{i}.o").write_text("x")
        time.sleep(0.35)
        assert w.calls == base
        (plain / "src.py").write_text("x")
        assert w.wait_for(1)


def test_event_filter_unit(plain):
    st = Store.open(plain, create=True)
    flag = []
    f = _EventFilter(st, lambda: flag.append(1))
    f.ignored = {"node_modules", "out/gen.js"}
    p = str(st.root)
    assert not f.path_relevant(p + "/.git/HEAD")
    assert not f.path_relevant(p + "/.ctrlz/lock")
    assert not f.path_relevant(p + "/sub/.ctrlz-tmp-123-abc")
    assert not f.path_relevant(p + "/node_modules/a/b.js")
    assert not f.path_relevant(p + "/out/gen.js")
    assert f.path_relevant(p + "/out/other.js")
    assert f.path_relevant(p + "/src/a.py")
    assert not f.path_relevant("/somewhere/else.py")
    assert f.path_relevant(p + "/.gitignore") and flag == [1]

    class Ev:
        def __init__(self, t, path, d=False):
            self.event_type, self.src_path, self.is_directory = t, path, d

    assert not f.relevant(Ev("opened", p + "/a.py"))
    assert not f.relevant(Ev("closed_no_write", p + "/a.py"))
    assert not f.relevant(Ev("modified", p + "/dir", d=True))
    assert f.relevant(Ev("created", p + "/a.py")) and f.relevant(Ev("deleted", p + "/a.py"))


def test_polling_fallback(plain):
    st = Store.open(plain, create=True)
    st.snap()
    with Watcher(st, force_polling=True, poll_interval_s=0.2) as w:
        (plain / "polled.txt").write_text("p")
        assert w.wait_for(1)


def test_pending_changes_flushed_on_stop(plain):
    st = Store.open(plain, create=True)
    st.snap()
    w = Watcher(st, debounce_ms=30_000)
    with w:
        (plain / "last_second.txt").write_text("don't lose me")
        time.sleep(0.5)
        assert not w.snaps
    assert "last_second.txt" in st.files("@")


def test_startup_snapshot_and_errors_dont_kill_watcher(plain, monkeypatch):
    (plain / "pre.txt").write_text("existing")
    st = Store.open(plain, create=True)
    real = st.snap
    state = {"fail": 1}

    def flaky(*a, **k):
        if state["fail"] == 0:
            state["fail"] = -1
            raise RuntimeError("transient")
        if state["fail"] > 0:
            state["fail"] -= 1
        return real(*a, **k)

    monkeypatch.setattr(st, "snap", flaky)
    stop = threading.Event()
    snaps, errors = [], []
    ready = threading.Event()
    t = threading.Thread(target=watch, args=(st,), kwargs=dict(debounce_ms=100, stop=stop, on_snap=snaps.append,
                                                               on_error=errors.append, on_ready=lambda m: ready.set()), daemon=True)
    t.start()
    ready.wait(5)
    time.sleep(0.25)
    assert snaps and snaps[0].trigger == "watch"  # state at start-up captured
    (plain / "b.txt").write_text("b")
    time.sleep(0.4)
    (plain / "c.txt").write_text("c")
    time.sleep(0.45)
    stop.set()
    t.join(5)
    assert errors and isinstance(errors[0], RuntimeError)
    assert {"pre.txt", "b.txt", "c.txt"} <= set(st.files("@"))


# --------------------------------------------------------------------------------------------
# background daemon
# --------------------------------------------------------------------------------------------
def test_daemon_start_status_snapshot_stop(plain):
    st = Store.open(plain, create=True)
    st.config["debounce_ms"] = 200
    st.save_config()
    st.snap()
    assert not daemon_status(st)["running"]
    info = start_daemon(st)
    pid = info["pid"]
    try:
        assert info["running"] and pid_alive(pid) and not info["already"]
        assert os.getsid(pid) == pid  # detached into its own session
        assert start_daemon(st)["already"] and daemon_status(st)["pid"] == pid
        (plain / "from_daemon.txt").write_text("hello")
        end = time.monotonic() + 10
        while time.monotonic() < end and "from_daemon.txt" not in st.files("@"):
            time.sleep(0.1)
        assert "from_daemon.txt" in st.files("@")
        assert st.latest().trigger == "watch"
        log = Path(info["log"]).read_text()
        assert "watcher started" in log and "snapshot" in log
        (plain / "late.txt").write_text("last-moment edit")
        time.sleep(0.1)
    finally:
        assert stop_daemon(st) is True
    assert not pid_alive(pid), "stop must really kill the daemon"
    assert not daemon_status(st)["running"]
    assert not Path(info["pidfile"]).exists()
    assert "late.txt" in st.files("@")  # pending edits are flushed on stop
    assert stop_daemon(st) is False




def test_stale_pidfile_is_ignored(plain):
    st = Store.open(plain, create=True)
    import subprocess, sys
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    (st.store_dir / "watch.pid").write_text(json.dumps({"pid": p.pid, "started": "x"}))
    assert not daemon_status(st)["running"]
    assert stop_daemon(st) is False
    assert not (st.store_dir / "watch.pid").exists()  # stale pidfile cleaned up
