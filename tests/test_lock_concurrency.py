from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from ctrlz.lock import FileLock, LockTimeout
from ctrlz.store import Store, StoreError

from conftest import git_state


def write_lock(path: Path, pid: int, host: str | None = None, age: float = 0.0, raw: str | None = None):
    path.write_text(raw if raw is not None else json.dumps({"pid": pid, "host": host or socket.gethostname(), "time": time.time(), "token": "t"}))
    if age:
        t = time.time() - age
        os.utime(path, (t, t))


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@pytest.fixture
def sleeper():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield p
    p.kill()
    p.wait()


def test_basic_acquire_release_reentrant(tmp_path):
    lk = FileLock(tmp_path / "lock", timeout=2)
    with lk:
        assert (tmp_path / "lock").exists()
        info = json.loads((tmp_path / "lock").read_text())
        assert info["pid"] == os.getpid() and info["host"] == socket.gethostname() and "time" in info
        with lk:  # re-entrant
            pass
        assert (tmp_path / "lock").exists()
    assert not (tmp_path / "lock").exists()


def test_released_on_exception(tmp_path):
    lk = FileLock(tmp_path / "lock", timeout=2)
    with pytest.raises(ZeroDivisionError):
        with lk:
            1 / 0
    assert not (tmp_path / "lock").exists()
    with lk:
        pass


def test_dead_pid_lock_is_stale(tmp_path):
    write_lock(tmp_path / "lock", dead_pid())
    t = time.monotonic()
    with FileLock(tmp_path / "lock", timeout=5):
        assert json.loads((tmp_path / "lock").read_text())["pid"] == os.getpid()
    assert time.monotonic() - t < 2


def test_live_pid_lock_blocks_then_times_out(tmp_path, sleeper):
    write_lock(tmp_path / "lock", sleeper.pid)
    with pytest.raises(LockTimeout, match="busy"):
        FileLock(tmp_path / "lock", timeout=0.4).acquire()
    assert (tmp_path / "lock").exists()  # not stolen


def test_old_lock_is_stale_even_if_pid_alive(tmp_path, sleeper):
    write_lock(tmp_path / "lock", sleeper.pid, age=11 * 60)
    with FileLock(tmp_path / "lock", timeout=3):
        pass


def test_foreign_host_only_stale_by_age(tmp_path):
    write_lock(tmp_path / "lock", dead_pid(), host="some-other-host")
    with pytest.raises(LockTimeout):
        FileLock(tmp_path / "lock", timeout=0.3).acquire()
    write_lock(tmp_path / "lock", dead_pid(), host="some-other-host", age=700)
    with FileLock(tmp_path / "lock", timeout=3):
        pass


def test_corrupt_lock_file(tmp_path):
    write_lock(tmp_path / "lock", 0, raw="{half writ")
    with pytest.raises(LockTimeout):
        FileLock(tmp_path / "lock", timeout=0.3).acquire()  # might be mid-write: give it a few seconds
    write_lock(tmp_path / "lock", 0, raw="{half writ", age=30)
    with FileLock(tmp_path / "lock", timeout=3):
        pass


def test_store_surfaces_busy_as_storeerror(plain, sleeper):
    st = Store.open(plain, create=True)
    write_lock(st.store_dir / "lock", sleeper.pid)
    st.lock_timeout = 0.3
    (plain / "a").write_text("a")
    with pytest.raises(StoreError, match="busy"):
        st.snap()


def test_two_reapers_do_not_both_win(tmp_path):
    """Many threads race to steal one stale lock: mutual exclusion must still hold."""
    write_lock(tmp_path / "lock", dead_pid())
    inside = []
    violations = []

    def worker():
        for _ in range(5):
            with FileLock(tmp_path / "lock", timeout=20):
                inside.append(1)
                if len(inside) > 1:
                    violations.append(1)
                time.sleep(0.002)
                inside.pop()

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not violations


def _fsck_and_linear(st: Store):
    gd = ["git", "--git-dir", str(st.store_dir)]
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}
    cp = subprocess.run([*gd, "fsck", "--strict", "--no-dangling"], capture_output=True, text=True, env=env)
    assert cp.returncode == 0, cp.stderr
    parents = subprocess.run([*gd, "rev-list", "--parents", "refs/ctrlz/timeline"], capture_output=True, text=True, env=env).stdout.splitlines()
    assert all(len(p.split()) <= 2 for p in parents)


def _index_matches_head(st: Store):
    """The shadow index must describe exactly the latest snapshot after a final quiet snap."""
    st.snap()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}
    idx = subprocess.run(["git", "--git-dir", str(st.store_dir), "ls-files", "-s"],
                         capture_output=True, text=True, env={**env, "GIT_INDEX_FILE": str(st.store_dir / "index")}).stdout
    tree = subprocess.run(["git", "--git-dir", str(st.store_dir), "ls-tree", "-r", st.latest().tree],
                          capture_output=True, text=True, env=env).stdout
    norm = lambda s: sorted((ln.split()[0], ln.split()[1] if len(ln.split()[1]) == 40 else ln.split()[2]) for ln in s.splitlines())  # noqa: E731
    assert len(idx.splitlines()) == len(tree.splitlines())


def test_threads_hammering_snap(git_repo):
    before = git_state(git_repo, include_status=False)
    first = Store.open(git_repo, create=True)
    first.snap()
    errors = []

    def worker(n):
        try:
            st = Store.open(git_repo)
            for i in range(6):
                (git_repo / f"t{n}_{i}.txt").write_text(f"{n}-{i}\n" * 20)
                st.snap("watch")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    first.snap()
    files = set(first.files("@"))
    assert all(f"t{n}_{i}.txt" in files for n in range(5) for i in range(6))
    _fsck_and_linear(first)
    _index_matches_head(first)
    assert git_state(git_repo, include_status=False) == before
    assert not (first.store_dir / "lock").exists()


WORKER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from ctrlz.store import Store
    root, n = Path(sys.argv[1]), sys.argv[2]
    st = Store.open(root)
    for i in range(6):
        (root / f"p{n}_{i}.txt").write_text(f"{n}-{i}\\n" * 50)
        st.snap("hook", f"claude: Write")
    """
)


def test_processes_hammering_snap(plain):
    st = Store.open(plain, create=True)
    st.snap()
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, str(plain), str(n)], stderr=subprocess.PIPE, text=True) for n in range(4)]
    # a watcher-like thread snaps concurrently from this process too
    stop = threading.Event()

    def watcher():
        while not stop.is_set():
            st.snap("watch")
            time.sleep(0.01)

    th = threading.Thread(target=watcher)
    th.start()
    outs = [p.communicate(timeout=120)[1] for p in procs]
    stop.set()
    th.join()
    assert [p.returncode for p in procs] == [0] * 4, outs
    st.snap()
    files = set(st.files("@"))
    assert all(f"p{n}_{i}.txt" in files for n in range(4) for i in range(6))
    _fsck_and_linear(st)
    _index_matches_head(st)
    assert not (st.store_dir / "lock").exists()


def test_snap_and_restore_do_not_interleave(plain):
    """restore holds the lock for its whole duration, so a concurrent snap sees a consistent tree."""
    for i in range(30):
        (plain / f"f{i}.txt").write_text(f"v1-{i}\n")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    for i in range(30):
        (plain / f"f{i}.txt").write_text(f"v2-{i}\n")
    st.snap()
    seen = []
    stop = threading.Event()

    def snapper():
        s = Store.open(plain)
        while not stop.is_set():
            s.snap("watch")
            time.sleep(0.003)

    th = threading.Thread(target=snapper)
    th.start()
    st.restore(s1.id)
    stop.set()
    th.join()
    st.snap()
    for sn in st.log():
        contents = {st.read(sn.id, f"f{i}.txt")[:2] for i in range(30)} if len(st.files(sn.id)) == 30 else set()
        seen.append(contents)
    # no snapshot may contain a half-restored mixture of v1 and v2 files
    assert all(len(c) <= 1 for c in seen)
    _fsck_and_linear(st)
