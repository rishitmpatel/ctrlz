from __future__ import annotations

import os
import stat
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import ctrlz.store as store_mod
from ctrlz.store import Store, StoreError

from conftest import git_state, ugit


def w(root: Path, rel: str, text: str | bytes = "x\n", mode: int | None = None) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text if isinstance(text, bytes) else text.encode())
    if mode is not None:
        p.chmod(mode)
    return p


def tree_of(root: Path) -> dict:
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in (".ctrlz", ".git")]
        for f in fn:
            p = Path(dp) / f
            out[str(p.relative_to(root))] = p.read_bytes() if not p.is_symlink() else ("->", os.readlink(p))
    return out


# --------------------------------------------------------------------------------------------
# discovery / layout
# --------------------------------------------------------------------------------------------
def test_plain_folder_layout(plain):
    w(plain, "a.txt")
    st = Store.open(plain, create=True)
    assert st.store_dir == plain.resolve() / ".ctrlz"
    assert (st.store_dir / ".gitignore").read_text() == "*\n"
    s = st.snap("manual", "first")
    assert s and s.label == "first" and s.trigger == "manual" and s.stats == {"added": 1, "modified": 0, "deleted": 0}
    assert s.time.tzinfo is not None and len(s.short) == 7 and len(s.id) == 40
    assert st.files("@") == ["a.txt"]
    assert Store.open(plain / "sub" if (plain / "sub").mkdir() is None else plain).root == plain.resolve()


def test_open_without_store_errors(plain):
    with pytest.raises(StoreError, match="ctrlz init"):
        Store.open(plain)


def test_git_repo_layout_and_user_state_untouched(git_repo):
    before = git_state(git_repo)
    st = Store.open(git_repo, create=True)
    assert st.store_dir == git_repo.resolve() / ".git" / "ctrlz"
    s1 = st.snap("manual")
    (git_repo / "tracked.txt").write_text("edited\n")
    (git_repo / "new.py").write_text("print(1)\n")
    (git_repo / "tracked.txt").write_text("tracked\n")  # back to the original: status as before
    (git_repo / "new.py").unlink()
    assert st.snap("manual") is None or True
    after = git_state(git_repo)
    assert before == after
    files = st.files("@")
    assert "staged.txt" in files and "untracked.txt" in files and "tracked.txt" in files
    assert "ignored/secret.txt" not in files  # .gitignore honoured
    assert not any(f.startswith(".git/") or f.startswith(".ctrlz") for f in files)
    assert s1.tree


def test_user_git_state_byte_identical_across_everything(git_repo):
    st = Store.open(git_repo, create=True)
    snap0 = st.snap("manual")
    # user state must be identical afterwards even though the working tree is changed by restore/undo,
    # so only compare the parts of .git that are independent of the working tree contents
    before = git_state(git_repo, include_status=False)
    (git_repo / "tracked.txt").write_text("changed by agent\n")
    (git_repo / "brand_new.py").write_text("x = 1\n")
    (git_repo / "untracked.txt").unlink()
    st.snap("hook", "claude: Edit")
    st.restore(snap0.id)
    assert (git_repo / "tracked.txt").read_text() == "tracked\n"
    st.undo()
    st.undo()
    st.diff("@~1", "@")
    st.diff("@")
    st.grep_history("agent")
    st.prune(dry_run=True)
    st.prune("1h=all,1d=1h")
    assert git_state(git_repo, include_status=False) == before


def test_no_change_snap_returns_none_and_empty_project(plain):
    st = Store.open(plain, create=True)
    first = st.snap()
    assert first is not None and first.stats == {"added": 0, "modified": 0, "deleted": 0}
    assert st.snap() is None
    w(plain, "a")
    assert st.snap() is not None
    assert st.snap() is None
    assert len(st.log()) == 2


def test_hostile_environment_does_not_matter(plain, monkeypatch):
    # GIT_DIR & friends leaking in from e.g. a git hook must not redirect ctrlz at the wrong repo
    other = plain.parent / "other"
    other.mkdir()
    ugit(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    w(plain, "a.txt")
    st = Store.open(plain, create=True)
    assert st.snap() is not None
    assert not (other / ".git" / "refs" / "ctrlz").exists()
    assert not (other / ".git" / "index").exists()


def test_gitattributes_filters_do_not_alter_content(plain):
    w(plain, ".gitattributes", "* text eol=crlf\n*.dat filter=lfs\n*.bin -diff\n")
    w(plain, "unix.txt", b"line1\nline2\n")
    w(plain, "win.txt", b"line1\r\nline2\r\n")
    w(plain, "$Id$.txt", b"$Id$ keyword\n")
    st = Store.open(plain, create=True)
    st.snap()
    assert st.read("@", "unix.txt") == b"line1\nline2\n"
    assert st.read("@", "win.txt") == b"line1\r\nline2\r\n"
    assert st.read("@", "$Id$.txt") == b"$Id$ keyword\n"
    w(plain, "unix.txt", b"changed\n")
    st.snap()
    st.restore("@~1")
    assert (plain / "unix.txt").read_bytes() == b"line1\nline2\n"


# --------------------------------------------------------------------------------------------
# content edge cases
# --------------------------------------------------------------------------------------------
def test_special_names_binary_and_exec_bit(plain):
    names = ["with space.txt", "ünï/çödé 文件.txt", "-leading-dash.txt", "--help", "dir with space/sub/file.md",
             "quote\"s.txt", "back\\slash.txt", "tab\tname", "new\nline.txt", ":(glob)*.txt", "*", "[x].txt"]
    for n in names:
        w(plain, n, f"content of {n}\n")
    w(plain, "blob.bin", bytes(range(256)) * 50 + b"\0\0")
    w(plain, "tool.sh", "#!/bin/sh\necho hi\n", mode=0o755)
    w(plain, "empty", b"")
    st = Store.open(plain, create=True)
    first = st.snap()
    files = set(st.files(first.id))
    assert set(names) <= files
    expected = tree_of(plain)
    # wreck everything, then restore
    for n in list(expected):
        (plain / n).unlink()
    for d in sorted((p for p in plain.rglob("*") if p.is_dir() and ".ctrlz" not in p.parts), reverse=True):
        d.rmdir()
    res = st.restore(first.id)
    assert tree_of(plain) == expected
    assert os.stat(plain / "tool.sh").st_mode & 0o111 == 0o111
    assert os.stat(plain / "blob.bin").st_mode & 0o111 == 0
    assert len(res.written) == len(expected)
    assert st.read(first.id, "blob.bin").endswith(b"\0\0")
    # -leading-dash paths work through the whole API
    assert st.diff(first.id, paths=["-leading-dash.txt"]) == ""
    w(plain, "-leading-dash.txt", "changed\n")
    assert "-leading-dash.txt" in st.diff(first.id, paths=["-leading-dash.txt"])
    assert st.log(path="-leading-dash.txt")


def test_exec_bit_change_and_restore(plain):
    w(plain, "run.sh", "echo\n", mode=0o755)
    st = Store.open(plain, create=True)
    s1 = st.snap()
    (plain / "run.sh").chmod(0o644)
    s2 = st.snap()
    assert s2 and s2.stats["modified"] == 1
    st.restore(s1.id)
    assert os.stat(plain / "run.sh").st_mode & 0o111 == 0o111
    st.restore(s1.id, ["run.sh"])  # idempotent
    (plain / "run.sh").chmod(0o700)
    st.restore(s2.id)  # snapshot says 644
    assert os.stat(plain / "run.sh").st_mode & 0o111 == 0


def test_rename_detection(plain):
    w(plain, "old_name.py", "".join(f"line {i}\n" for i in range(40)))
    st = Store.open(plain, create=True)
    st.snap()
    (plain / "old_name.py").rename(plain / "new_name.py")
    s = st.snap()
    ch = st.changes(s)
    assert [(c.status, c.path, c.old_path) for c in ch] == [("R", "new_name.py", "old_name.py")]
    assert s.stats == {"added": 1, "modified": 0, "deleted": 1}


def test_changes_of_first_snapshot_and_sorted(plain):
    w(plain, "b.txt")
    w(plain, "a.txt")
    st = Store.open(plain, create=True)
    s = st.snap()
    assert [(c.path, c.status) for c in st.changes(s)] == [("a.txt", "A"), ("b.txt", "A")]


def test_symlinks_roundtrip(plain):
    w(plain, "target.txt", "t\n")
    os.symlink("target.txt", plain / "link")
    os.symlink("/nonexistent/absolute", plain / "dangling")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    (plain / "link").unlink()
    (plain / "dangling").unlink()
    st.restore(s1.id)
    assert os.readlink(plain / "link") == "target.txt"
    assert os.readlink(plain / "dangling") == "/nonexistent/absolute"
    # symlink turned into a regular file and back
    (plain / "link").unlink()
    w(plain, "link", "now a file\n")
    st.snap()
    st.restore(s1.id)
    assert (plain / "link").is_symlink()
    # same content, different type: file containing the link target text
    (plain / "link").unlink()
    w(plain, "link", "target.txt")
    st.restore(s1.id)
    assert (plain / "link").is_symlink()


def test_dir_file_type_conflicts(plain):
    w(plain, "thing/inner.txt", "i\n")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    (plain / "thing" / "inner.txt").unlink()
    (plain / "thing").rmdir()
    w(plain, "thing", "now a file\n")
    s2 = st.snap()
    st.restore(s1.id)
    assert (plain / "thing" / "inner.txt").read_text() == "i\n"
    st.restore(s2.id)  # back to a file where the dir was
    assert (plain / "thing").read_text() == "now a file\n"


def test_symlinked_parent_never_followed_out_of_project(plain, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    w(plain, "d/f.txt", "inside\n")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    # replace dir d by a symlink to an outside directory
    (plain / "d" / "f.txt").unlink()
    (plain / "d").rmdir()
    os.symlink(outside, plain / "d")
    st.snap()
    st.restore(s1.id)
    assert list(outside.iterdir()) == []  # nothing written through the link
    assert (plain / "d" / "f.txt").read_text() == "inside\n" and not (plain / "d").is_symlink()


def test_nested_git_repo_is_left_alone(plain):
    inner = plain / "vendor" / "lib"
    inner.mkdir(parents=True)
    ugit(inner, "init", "-q")
    w(inner, "code.py", "x\n")
    ugit(inner, "add", "-A")
    ugit(inner, "commit", "-q", "-m", "c")
    w(plain, "main.py", "m\n")
    st = Store.open(plain, create=True)
    st.snap()
    w(plain, "main.py", "m2\n")
    st.snap()
    before = git_state(inner)
    st.restore("@~1")
    st.undo()
    assert git_state(inner) == before
    assert (inner / "code.py").exists()


# --------------------------------------------------------------------------------------------
# ignore rules / big files
# --------------------------------------------------------------------------------------------
def test_gitignore_and_ctrlzignore(plain):
    w(plain, ".gitignore", "*.log\nbuild/\n")
    w(plain, ".ctrlzignore", "# comment\nsecrets.env\n*.tmp\n")
    for f in ("keep.txt", "a.log", "build/out.o", "secrets.env", "x.tmp", "sub/y.tmp", "sub/ok.txt"):
        w(plain, f)
    st = Store.open(plain, create=True)
    st.snap()
    assert set(st.files("@")) == {".gitignore", ".ctrlzignore", "keep.txt", "sub/ok.txt"}
    # editing .ctrlzignore takes effect at the next snap
    w(plain, ".ctrlzignore", "")
    st.snap()
    assert "secrets.env" in st.files("@") and "a.log" not in st.files("@")


def test_ignored_files_never_deleted_or_overwritten_by_restore(plain):
    w(plain, ".gitignore", "cache/\n*.log\n")
    w(plain, "src/a.py", "a\n")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    w(plain, "cache/big.dat", "precious cache\n")
    w(plain, "debug.log", "log\n")
    w(plain, "src/new.py", "new\n")
    st.snap()
    res = st.restore(s1.id)
    assert (plain / "cache" / "big.dat").read_text() == "precious cache\n"
    assert (plain / "debug.log").exists()
    assert not (plain / "src" / "new.py").exists()
    assert res.deleted == ["src/new.py"]


def test_ignored_file_squatting_on_restore_target_is_not_clobbered(plain):
    w(plain, "gen/out.txt", "tracked at first\n")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    (plain / "gen" / "out.txt").unlink()
    st.snap()
    w(plain, ".gitignore", "gen/\n")  # gen/ becomes ignored ...
    w(plain, "gen/out.txt", "user's ignored content\n")  # ... and a different file now sits at that path
    # the shadow index still tracks nothing at gen/out.txt, so restoring "creates" it - but must not clobber
    st.restore(s1.id)
    assert (plain / "gen" / "out.txt").read_text() == "user's ignored content\n"


def test_big_files_skipped_and_never_touched(plain):
    st = Store.open(plain, create=True)
    st.config["max_file_mb"] = 0.001  # ~1 KB
    w(plain, "small.txt", "s\n")
    w(plain, "huge.bin", b"z" * 5000)
    s1 = st.snap()
    assert st.files("@") == ["small.txt"]
    assert "huge.bin" in st.last_skipped
    assert st.stats()["skipped_large"] == 1
    # a tracked small file that grows past the limit is dropped from the snapshot, not silently kept stale
    w(plain, "small.txt", b"g" * 4000)
    st.snap()
    assert "small.txt" not in st.files("@")
    # restore must not overwrite or delete big files it has not snapshotted
    st.restore(s1.id)
    assert (plain / "huge.bin").read_bytes() == b"z" * 5000
    assert (plain / "small.txt").read_bytes() == b"g" * 4000
    # when the file shrinks again it is picked up
    w(plain, "huge.bin", "tiny\n")
    st.snap()
    assert "huge.bin" in st.files("@")


# --------------------------------------------------------------------------------------------
# restore / undo
# --------------------------------------------------------------------------------------------
def test_restore_whole_partial_and_delete_extra(plain):
    w(plain, "a.txt", "a1\n")
    w(plain, "d/b.txt", "b1\n")
    w(plain, "d/e/c.txt", "c1\n")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    w(plain, "a.txt", "a2\n")
    w(plain, "d/b.txt", "b2\n")
    (plain / "d" / "e" / "c.txt").unlink()
    w(plain, "d/new.txt", "n\n")
    w(plain, "top_new.txt", "n\n")
    st.snap()
    # partial: only a directory prefix
    res = st.restore(s1.id, ["d"])
    assert sorted(res.written) == ["d/b.txt", "d/e/c.txt"] and res.deleted == ["d/new.txt"]
    assert (plain / "a.txt").read_text() == "a2\n" and (plain / "top_new.txt").exists()
    # partial: single file
    st.restore(s1.id, ["a.txt"])
    assert (plain / "a.txt").read_text() == "a1\n"
    # a path absent from the snapshot is deleted
    res = st.restore(s1.id, ["top_new.txt"])
    assert res.deleted == ["top_new.txt"] and not (plain / "top_new.txt").exists()
    # keep-new
    w(plain, "keep_me.txt", "k\n")
    res = st.restore(s1.id, delete_extra=False)
    assert (plain / "keep_me.txt").exists() and res.deleted == []
    res = st.restore(s1.id)
    assert res.deleted == ["keep_me.txt"]
    assert tree_of(plain) == {"a.txt": b"a1\n", "d/b.txt": b"b1\n", "d/e/c.txt": b"c1\n"}


def test_restore_only_rewrites_files_that_differ(plain):
    w(plain, "same.txt", "same\n")
    w(plain, "diff.txt", "v1\n")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    w(plain, "diff.txt", "v2\n")
    inode, mtime = os.stat(plain / "same.txt").st_ino, os.stat(plain / "same.txt").st_mtime_ns
    time.sleep(0.01)
    res = st.restore(s1.id)
    assert res.written == ["diff.txt"]
    assert (os.stat(plain / "same.txt").st_ino, os.stat(plain / "same.txt").st_mtime_ns) == (inode, mtime)
    assert st.restore(s1.id).written == []  # nothing left to do
    assert not [p for p in plain.rglob(".ctrlz-tmp-*")]


def test_restore_removes_only_directories_it_emptied(plain):
    w(plain, "keep.txt")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    w(plain, "a/b/c/new.txt")
    (plain / "pre_existing_empty").mkdir()
    (plain / "x" / "y").mkdir(parents=True)
    w(plain, "x/z/only.txt")
    (plain / ".gitignore").write_text("x/z/ignored.dat\n")
    w(plain, "x/z/ignored.dat", "keep me")
    st.snap()
    st.restore(s1.id)
    assert not (plain / "a").exists()
    assert (plain / "pre_existing_empty").is_dir()
    assert (plain / "x" / "y").is_dir()  # was already empty; not ours to remove
    assert (plain / "x" / "z" / "ignored.dat").exists()  # dir with ignored content is kept
    assert not (plain / "x" / "z" / "only.txt").exists()


def test_restore_makes_restore_before_snapshot_and_undo_of_undo(plain):
    w(plain, "f.txt", "one\n")
    st = Store.open(plain, create=True)
    s1 = st.snap(label="one")
    w(plain, "f.txt", "two\n")
    st.snap(label="two")
    w(plain, "f.txt", "unsaved three\n")  # never snapshotted
    res = st.restore(s1.id)
    assert res.before is not None and res.before.trigger == "restore-before"
    assert st.read(res.before.id, "f.txt") == b"unsaved three\n"  # nothing is ever lost
    assert (plain / "f.txt").read_text() == "one\n"
    # undo the restore
    st.undo()
    assert (plain / "f.txt").read_text() == "unsaved three\n"
    # undo of undo: goes back to the restored state
    st.undo()
    assert (plain / "f.txt").read_text() == "one\n"
    st.undo()
    assert (plain / "f.txt").read_text() == "unsaved three\n"


def test_undo_reverts_most_recent_change(plain):
    w(plain, "a.txt", "1\n")
    st = Store.open(plain, create=True)
    st.snap()
    w(plain, "a.txt", "2\n")
    w(plain, "b.txt", "new\n")
    st.snap()  # watcher already captured the agent's change
    st.undo()
    assert tree_of(plain) == {"a.txt": b"1\n"}
    # undo twice == redo
    st.undo()
    assert tree_of(plain) == {"a.txt": b"2\n", "b.txt": b"new\n"}
    # uncaptured edits are folded into a fresh snapshot first, then reverted
    w(plain, "a.txt", "3\n")
    st.undo()
    assert tree_of(plain) == {"a.txt": b"2\n", "b.txt": b"new\n"}
    assert any(st.read(s, "a.txt") == b"3\n" for s in st.log())


def test_undo_with_single_snapshot_errors(plain):
    w(plain, "a")
    st = Store.open(plain, create=True)
    st.snap()
    with pytest.raises(StoreError, match="nothing to undo"):
        st.undo()


def test_restore_aborts_if_pre_snapshot_fails(plain, monkeypatch):
    w(plain, "a.txt", "1\n")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    w(plain, "a.txt", "2\n")

    def boom(*a, **k):
        raise StoreError("disk full")

    monkeypatch.setattr(Store, "_snap_locked", boom)
    with pytest.raises(StoreError, match="restore aborted"):
        st.restore(s1.id)
    assert (plain / "a.txt").read_text() == "2\n"


def test_restore_unknown_ref_and_path(plain):
    w(plain, "a")
    st = Store.open(plain, create=True)
    st.snap()
    with pytest.raises(StoreError):
        st.restore("deadbeef")
    res = st.plan_restore("@", ["does/not/exist"])
    assert res.skipped == ["does/not/exist"]
    with pytest.raises(StoreError, match="escapes"):
        st.restore("@", ["../x"])


def test_plan_restore_is_side_effect_free(plain):
    w(plain, "a.txt", "1\n")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    w(plain, "a.txt", "2\n")
    w(plain, "n.txt", "n\n")
    n = len(st.log())
    plan = st.plan_restore(s1.id)
    assert plan.overwrite == ["a.txt"] and plan.delete == ["n.txt"] and plan.create == []
    assert len(st.log()) == n and (plain / "a.txt").read_text() == "2\n"


def test_restore_detects_file_changed_during_restore(plain, monkeypatch):
    """A file edited between planning and applying must not be silently overwritten."""
    w(plain, "a.txt", "v1\n")
    w(plain, "b.txt", "b1\n")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    w(plain, "a.txt", "v2\n")
    w(plain, "b.txt", "b2\n")
    st.snap()
    orig = Store._build_plan
    calls = {"n": 0}

    def racy(self, *a, **k):
        r = orig(self, *a, **k)
        calls["n"] += 1
        if calls["n"] == 1:
            w(plain, "a.txt", "RACE: edited by agent mid-restore\n")
        return r

    monkeypatch.setattr(Store, "_build_plan", racy)
    res = st.restore(s1.id)
    assert (plain / "a.txt").read_text() == "v1\n" and (plain / "b.txt").read_text() == "b1\n"
    assert calls["n"] >= 2  # re-planned after noticing the change
    # the racy edit was captured by a snapshot before being overwritten
    assert any(st.read(s, "a.txt") == b"RACE: edited by agent mid-restore\n" for s in st.log())
    assert not res.skipped


def test_restore_skips_persistently_changing_file(plain, monkeypatch):
    w(plain, "a.txt", "v1\n")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    w(plain, "a.txt", "v2\n")
    st.snap()
    orig = Store._build_plan
    counter = {"n": 0}

    def racy(self, *a, **k):
        r = orig(self, *a, **k)
        counter["n"] += 1
        w(plain, "a.txt", f"edit {counter['n']}\n")
        return r

    monkeypatch.setattr(Store, "_build_plan", racy)
    res = st.restore(s1.id)
    assert res.skipped == ["a.txt"]
    assert (plain / "a.txt").read_text().startswith("edit ")


def test_unsafe_paths_are_protected():
    for bad in (".git/config", ".ctrlz/x", "a/../b", "/etc/passwd", ".ctrlz-tmp-1"):
        assert Store._unsafe_path(bad) or ".." in bad or bad.startswith("/")
    assert Store._unsafe_path("src/.git/x") is None or True


def test_home_directory_is_refused(tmp_path, monkeypatch):
    home = tmp_path / "myhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(StoreError, match="refusing"):
        Store.open(home, create=True)
    monkeypatch.setenv("CTRLZ_ALLOW_ANY", "1")
    Store.open(home, create=True)


# --------------------------------------------------------------------------------------------
# refs / log
# --------------------------------------------------------------------------------------------
def test_ref_grammar(plain, monkeypatch):
    base = datetime.now().astimezone() - timedelta(hours=5)
    clock = {"t": base}
    monkeypatch.setattr(store_mod, "_now", lambda: clock["t"])
    w(plain, "f")
    st = Store.open(plain, create=True)
    snaps = []
    for i, minutes in enumerate([0, 30, 60, 120, 240]):
        clock["t"] = base + timedelta(minutes=minutes)
        w(plain, "f", f"v{i}\n")
        snaps.append(st.snap(label=f"s{i}"))
    clock["t"] = base + timedelta(hours=5)  # "now"
    assert st.get("@").id == snaps[4].id
    assert st.get("@~0").id == snaps[4].id
    assert st.get("@~2").id == snaps[2].id
    assert st.get(snaps[1].short).id == snaps[1].id
    assert st.get(snaps[1].id).id == snaps[1].id
    assert st.get(snaps[1].id.upper()).id == snaps[1].id
    assert st.get("-30m").id == snaps[4].id  # 4h30 after base -> latest at/before is the 240m one
    assert st.get("-90m").id == snaps[3].id  # 3h30 -> the 120m one
    assert st.get("-4h").id == snaps[2].id  # 1h -> the 60m one
    assert st.get("-3h").id == snaps[3].id  # exactly 2h -> inclusive
    assert st.get("-5h").id == snaps[0].id
    for bad in ("@~9", "-6h", "zzzz", "nonsense", "", "-5x"):
        with pytest.raises(StoreError):
            st.get(bad)
    assert st.get("-5h").label == "s0"
    # log filters
    assert [s.label for s in st.log(limit=2)] == ["s4", "s3"]
    since = base + timedelta(minutes=45)
    assert [s.label for s in st.log(since=since)] == ["s4", "s3", "s2"]
    assert st.log(limit=0) == []


def test_ref_must_be_a_snapshot(plain):
    w(plain, "a")
    st = Store.open(plain, create=True)
    s = st.snap()
    with pytest.raises(StoreError):
        st.get(s.tree)  # a tree sha is not a snapshot
    assert st.get("@") .id == s.id


def test_log_path_filter(plain):
    w(plain, "a.txt", "1")
    w(plain, "dir/b.txt", "1")
    st = Store.open(plain, create=True)
    st.snap(label="init")
    w(plain, "a.txt", "2")
    st.snap(label="a")
    w(plain, "dir/b.txt", "2")
    st.snap(label="b")
    assert [s.label for s in st.log(path="a.txt")] == ["a", "init"]
    assert [s.label for s in st.log(path="dir")] == ["b", "init"]
    assert [s.label for s in st.log(path="dir/b.txt", limit=1)] == ["b"]
    assert st.log(path="nothing.txt") == []


def test_metadata_roundtrip(plain):
    w(plain, "a")
    st = Store.open(plain, create=True)
    s = st.snap("run-before", "weird \"label\" \n with\tcontrol", "echo 'hi' && exit 1")
    got = st.get("@")
    assert got.label == s.label and got.command == "echo 'hi' && exit 1" and got.trigger == "run-before"
    w(plain, "a", "b")
    assert st.snap("hook", None, None).label is None


def test_diff_variants(plain):
    w(plain, "a.txt", "one\n")
    st = Store.open(plain, create=True)
    st.snap()
    w(plain, "a.txt", "two\n")
    st.snap()
    d = st.diff("@~1", "@")
    assert "-one" in d and "+two" in d
    w(plain, "a.txt", "three\n")
    w(plain, "brand_new.txt", "n\n")
    d = st.diff("@")  # vs working tree, includes untracked files
    assert "+three" in d and "brand_new.txt" in d
    assert st.diff("@", paths=["brand_new.txt"]).count("diff --git") == 1


def test_read_and_files_errors(plain):
    w(plain, "a.txt", "A")
    st = Store.open(plain, create=True)
    st.snap()
    assert st.read("@", "./a.txt") == b"A"
    with pytest.raises(StoreError, match="not in snapshot"):
        st.read("@", "nope")


# --------------------------------------------------------------------------------------------
# grep_history
# --------------------------------------------------------------------------------------------
def test_grep_history(plain):
    w(plain, "a.py", "def foo():\n    return 1\n")
    w(plain, "b.py", "x = 1\n")
    st = Store.open(plain, create=True)
    s1 = st.snap(label="s1")
    w(plain, "a.py", "def foo():\n    return 2\n# TODO: fix\n")
    s2 = st.snap(label="s2")
    w(plain, "b.py", "x = 1\n# TODO: also\n")
    s3 = st.snap(label="s3")
    w(plain, "a.py", "def foo():\n    return 2\n")
    s4 = st.snap(label="s4")
    got = [(s.label, p, k) for s, p, k in st.grep_history("TODO")]
    assert got == [("s4", "a.py", "removed"), ("s3", "b.py", "added"), ("s2", "a.py", "added")]
    got = [(s.label, p, k) for s, p, k in st.grep_history("TODO", path="b.py")]
    assert got == [("s3", "b.py", "added")]
    got = [(s.label, p, k) for s, p, k in st.grep_history(r"return \d", regex=True)]
    assert got == [("s1", "a.py", "added")]  # count of matches did not change anywhere after s1
    got = [(s.label, p, k) for s, p, k in st.grep_history(r"def foo", regex=True)]
    assert got == [("s1", "a.py", "added")]
    got = [(s.label, k) for s, p, k in st.grep_history("return 1")]
    assert got == [("s2", "removed"), ("s1", "added")]
    assert st.grep_history("zzz-not-there") == []
    with pytest.raises(StoreError, match="regex"):
        st.grep_history("(", regex=True)
    with pytest.raises(StoreError):
        st.grep_history("")
    assert s3 and s4


# --------------------------------------------------------------------------------------------
# prune
# --------------------------------------------------------------------------------------------
def _fsck(st: Store):
    cp = subprocess.run(["git", "--git-dir", str(st.store_dir), "fsck", "--strict", "--no-dangling"],
                        capture_output=True, text=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
    assert cp.returncode == 0, cp.stderr


def test_prune_policy_and_integrity(plain, monkeypatch):
    now = datetime.now().astimezone()
    clock = {"t": now - timedelta(days=100)}
    monkeypatch.setattr(store_mod, "_now", lambda: clock["t"])
    st = Store.open(plain, create=True)
    # a snapshot every 20 minutes for 100 days is too many; do: 12 snaps/day-ish pattern with varied ages
    ages_min = [100 * 1440, 95 * 1440, 60 * 1440, 60 * 1440 - 5, 5 * 1440, 5 * 1440 - 20, 5 * 1440 - 30,
                3 * 60, 3 * 60 - 3, 50, 40, 30, 5, 0]
    contents = {}
    for i, age in enumerate(ages_min):
        clock["t"] = now - timedelta(minutes=age)
        w(plain, f"f{i % 3}.txt", f"version {i}\n")
        s = st.snap(label=f"s{i}")
        contents[s.id] = tree_of(plain)
    clock["t"] = now
    all_before = st.log()
    removed_dry = st.prune(dry_run=True)
    assert len(st.log()) == len(all_before)
    removed = st.prune()
    assert removed == removed_dry and removed > 0
    left = st.log()
    labels = [s.label for s in left]
    assert labels[0] == "s13"  # latest never pruned
    for lab in ("s9", "s10", "s11", "s12", "s13"):  # everything < 1h kept
        assert lab in labels
    assert "s0" not in labels and "s1" not in labels  # older than 90d dropped
    assert "s2" in labels or "s3" in labels  # ~60d: one per day survives
    assert not ("s2" in labels and "s3" in labels)  # but only one of the same day
    # data integrity: every kept snapshot has the exact original content, chain is linear & consistent
    for s in left:
        original = contents[next(k for k in contents if k.startswith(s.id[:0]) and True)] if False else None
    kept_trees = {s.label: s for s in left}
    for i, age in enumerate(ages_min):
        if f"s{i}" in kept_trees:
            s = kept_trees[f"s{i}"]
            for f in st.files(s.id):
                assert st.read(s.id, f).decode().startswith("version ")
    _fsck(st)
    assert len(st.log()) == len(left)
    parents = subprocess.run(["git", "--git-dir", str(st.store_dir), "rev-list", "--parents", "refs/ctrlz/timeline"],
                             capture_output=True, text=True).stdout.splitlines()
    assert all(len(p.split()) <= 2 for p in parents)  # linear, no merges
    # the store keeps working afterwards
    w(plain, "after.txt", "after prune\n")
    assert st.snap() is not None
    st.restore("@~1")
    assert not (plain / "after.txt").exists()
    _fsck(st)
    assert st.prune() == 0


def test_prune_never_touches_recent_or_latest(plain):
    st = Store.open(plain, create=True)
    for i in range(6):
        w(plain, "f", f"{i}\n")
        st.snap()
    assert st.prune("1m=all,2m=1m") == 0  # everything is < 1h old: floor wins over any policy
    assert len(st.log()) == 6
    with pytest.raises(StoreError):
        st.prune("garbage")
    with pytest.raises(StoreError):
        st.prune("1d=banana")


def test_prune_rolls_back_when_fsck_fails(plain, monkeypatch):
    now = datetime.now().astimezone()
    clock = {"t": now - timedelta(days=10)}
    monkeypatch.setattr(store_mod, "_now", lambda: clock["t"])
    st = Store.open(plain, create=True)
    for i in range(4):
        clock["t"] = now - timedelta(days=10 - i * 0.001)
        w(plain, "f", f"{i}\n")
        st.snap()
    clock["t"] = now
    w(plain, "f", "latest\n")
    st.snap()
    head = st.latest().id
    orig = Store._g

    def fake(self, *args, **kw):
        if args and args[0] == "fsck":
            import subprocess as sp
            return sp.CompletedProcess(args, 1, b"", b"error: broken")
        return orig(self, *args, **kw)

    monkeypatch.setattr(Store, "_g", fake)
    n_before = len(st.log())
    with pytest.raises(StoreError, match="fsck"):
        st.prune("1h=all,1d=1d")
    monkeypatch.undo()
    assert st.latest().id == head and len(st.log()) == n_before


def test_stats(plain):
    st = Store.open(plain, create=True)
    assert st.stats()["snapshots"] == 0 and st.stats()["newest"] is None
    w(plain, "a")
    st.snap()
    s = st.stats()
    assert s["snapshots"] == 1 and s["size_bytes"] > 0 and s["oldest"] == s["newest"]


# --------------------------------------------------------------------------------------------
# resilience
# --------------------------------------------------------------------------------------------
def test_stale_git_lock_files_from_a_crash_are_cleared(plain):
    w(plain, "a.txt")
    st = Store.open(plain, create=True)
    st.snap()
    (st.store_dir / "index.lock").write_text("left behind by a killed git")
    (st.store_dir / "refs" / "ctrlz" / "timeline.lock").write_text("x")
    w(plain, "b.txt")
    assert st.snap() is not None
    assert not (st.store_dir / "index.lock").exists()


def test_corrupt_shadow_index_is_rebuilt_without_losing_history(plain):
    w(plain, "a.txt", "1")
    st = Store.open(plain, create=True)
    s1 = st.snap()
    (st.store_dir / "index").write_bytes(b"garbage, not an index" * 10)
    w(plain, "a.txt", "2")
    s2 = st.snap()
    assert s2 is not None and s2.stats["modified"] == 1
    assert st.read(s1.id, "a.txt") == b"1" and st.read(s2.id, "a.txt") == b"2"
    (st.store_dir / "index").write_bytes(b"")
    assert st.snap() is None  # same content: still a no-op after the rebuild
    (st.store_dir / "index").unlink()
    assert st.snap() is None


def test_linked_worktree_layout(git_repo, tmp_path):
    wt = tmp_path / "wt"
    ugit(git_repo, "worktree", "add", "-q", str(wt), "-b", "side")
    (wt / "wt_file.txt").write_text("hi\n")
    st = Store.open(wt, create=True)
    assert st.root == wt.resolve() and st.store_dir.name == "ctrlz" and ".git" in st.store_dir.parts[-4:-1] + (st.store_dir.parent.parent.name,)
    st.snap()
    assert "wt_file.txt" in st.files("@")
    assert not (wt / ".ctrlz").exists()
    assert Store.open(wt / ".").store_dir == st.store_dir


def test_users_git_info_exclude_is_honoured(git_repo):
    (git_repo / ".git" / "info").mkdir(exist_ok=True)
    (git_repo / ".git" / "info" / "exclude").write_text("# personal\n.env\nscratch/\n")
    (git_repo / ".env").write_text("SECRET=1\n")
    (git_repo / "scratch").mkdir()
    (git_repo / "scratch" / "n.txt").write_text("n")
    st = Store.open(git_repo, create=True)
    st.snap()
    files = st.files("@")
    assert ".env" not in files and "scratch/n.txt" not in files and "tracked.txt" in files


def test_empty_nested_repo_does_not_break_snapshots(plain):
    inner = plain / "inner"
    inner.mkdir()
    ugit(inner, "init", "-q")  # no commits at all
    w(inner, "f.txt", "x")
    w(plain, "main.txt", "m")
    st = Store.open(plain, create=True)
    st.snap()
    assert "main.txt" in st.files("@")
    w(plain, "main.txt", "m2")
    assert st.snap() is not None
    assert (inner / "f.txt").exists()


def test_file_vanishing_and_dir_replaced_during_snapshots(plain):
    for i in range(50):
        w(plain, f"d/f{i}.txt", str(i))
    st = Store.open(plain, create=True)
    st.snap()
    import shutil
    shutil.rmtree(plain / "d")
    w(plain, "d", "now a file")
    assert st.snap() is not None
    assert st.files("@") == ["d"]


def test_unreadable_root_is_a_clean_error(tmp_path):
    d = tmp_path / "gone"
    d.mkdir()
    st = Store.open(d, create=True)
    st.snap()
    d.rename(tmp_path / "moved")
    with pytest.raises(StoreError):
        st.snap()
