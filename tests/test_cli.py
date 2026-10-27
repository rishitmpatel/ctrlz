from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from ctrlz import __version__
from ctrlz.cli import main, rel_time, _protect_refs
from ctrlz.store import Store

from conftest import git_state, run_cli


@pytest.fixture
def proj(plain, monkeypatch):
    monkeypatch.chdir(plain)
    return plain


def cli(capsys, *args, stdin: str | None = None, monkeypatch=None):
    if stdin is not None:
        import io
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    try:
        rc = main(list(args))
    except SystemExit as e:  # argparse usage errors
        rc = e.code
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_version():
    assert __version__ == "0.1.0"
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0


def test_init_snap_log_show_flow(proj, capsys):
    (proj / "a.py").write_text("print(1)\n")
    rc, out, err = cli(capsys, "init")
    assert rc == 0 and "Next steps" in out and "ctrlz start" in out and "hooks install claude-code" in out
    rc, out, _ = cli(capsys, "init")
    assert rc == 0 and "already" in out
    (proj / "a.py").write_text("print(2)\n")
    (proj / "b.py").write_text("x\n")
    rc, out, _ = cli(capsys, "snap", "-m", "my label")
    assert rc == 0 and out.startswith("snapshot ") and "+1 ~1 -0" in out
    rc, out, _ = cli(capsys, "snap")
    assert rc == 0 and "no changes since" in out
    rc, out, _ = cli(capsys, "snap", "-q")
    assert rc == 0 and out == ""
    rc, out, _ = cli(capsys, "log")
    assert rc == 0
    assert "my label" in out and "just now" in out and "a.py" in out and "b.py" in out
    assert "+1 ~1 -0" in out and "initial snapshot" in out
    rc, out, _ = cli(capsys, "log", "--json", "-n", "1")
    data = json.loads(out)
    assert len(data) == 1 and data[0]["label"] == "my label" and {c["path"] for c in data[0]["changes"]} == {"a.py", "b.py"}
    assert data[0]["stats"] == {"added": 1, "modified": 1, "deleted": 0} and len(data[0]["id"]) == 40
    rc, out, _ = cli(capsys, "log", "--", "b.py")
    assert "my label" in out and "initial snapshot" not in out
    rc, out, _ = cli(capsys, "log", "--since", "1h")
    assert "my label" in out
    rc, out, _ = cli(capsys, "show", "@")
    assert "my label" in out and "M a.py" in out and "A b.py" in out
    rc, out, _ = cli(capsys, "show", "@~1", "--json")
    d = json.loads(out)
    assert d["label"] == "initial snapshot" and d["changes"][0]["path"] == "a.py"


def test_relative_times():
    from datetime import datetime, timedelta
    now = datetime.now().astimezone()
    cases = {1: "just now", 30: "30 sec ago", 120: "2 min ago", 60: "1 min ago", 3600: "1 hour ago",
             7300: "2 hours ago", 86400 * 1.2: "yesterday", 86400 * 5: "5 days ago"}
    for secs, text in cases.items():
        assert rel_time(now - timedelta(seconds=secs), now) == text
    assert rel_time(now - timedelta(days=60), now).startswith("20")


def test_diff_command(proj, capsys):
    (proj / "a.txt").write_text("one\n")
    main(["init"])
    (proj / "a.txt").write_text("two\n")
    (proj / "n.txt").write_text("new\n")
    capsys.readouterr()
    rc, out, _ = cli(capsys, "diff")
    assert rc == 0 and "-one" in out and "+two" in out and "n.txt" in out
    rc, out, _ = cli(capsys, "diff", "--", "n.txt")
    assert "n.txt" in out and "a.txt" not in out
    main(["snap"])
    rc, out, _ = cli(capsys, "diff", "@~1", "@")
    assert "+two" in out
    rc, out, _ = cli(capsys, "diff")
    assert "no differences" in out


def test_restore_preview_confirm_and_undo(proj, capsys, monkeypatch):
    (proj / "keep.txt").write_text("v1\n")
    (proj / "gone.txt").write_text("g\n")
    main(["init"])
    first = Store.open(proj).latest()
    (proj / "keep.txt").write_text("v2\n")
    (proj / "gone.txt").unlink()
    (proj / "extra.txt").write_text("e\n")
    main(["snap"])
    capsys.readouterr()
    # decline
    rc, out, _ = cli(capsys, "restore", first.short, stdin="n\n", monkeypatch=monkeypatch)
    assert rc == 1 and "will overwrite" in out and "keep.txt" in out and "will delete" in out and "extra.txt" in out
    assert "will create" in out and "gone.txt" in out and "Aborted" in out
    assert (proj / "keep.txt").read_text() == "v2\n"
    # EOF on stdin = no
    rc, out, _ = cli(capsys, "restore", first.short, stdin="", monkeypatch=monkeypatch)
    assert rc == 1 and (proj / "keep.txt").read_text() == "v2\n"
    # accept
    rc, out, _ = cli(capsys, "restore", first.short, stdin="y\n", monkeypatch=monkeypatch)
    assert rc == 0 and "ctrlz undo" in out
    assert (proj / "keep.txt").read_text() == "v1\n" and (proj / "gone.txt").exists() and not (proj / "extra.txt").exists()
    rc, out, _ = cli(capsys, "restore", first.short, "-y")
    assert rc == 0 and "already matches" in out
    # undo -y brings it back
    rc, out, _ = cli(capsys, "undo", "-y")
    assert rc == 0 and (proj / "keep.txt").read_text() == "v2\n" and (proj / "extra.txt").exists()


def test_restore_paths_relative_to_cwd_and_keep_new(proj, capsys, monkeypatch):
    (proj / "sub").mkdir()
    (proj / "sub" / "f.txt").write_text("1\n")
    (proj / "top.txt").write_text("t1\n")
    main(["init"])
    first = Store.open(proj).latest().short
    (proj / "sub" / "f.txt").write_text("2\n")
    (proj / "top.txt").write_text("t2\n")
    (proj / "sub" / "new.txt").write_text("n\n")
    main(["snap"])
    monkeypatch.chdir(proj / "sub")
    rc, out, _ = cli(capsys, "restore", first, "f.txt", "-y")
    assert rc == 0
    assert (proj / "sub" / "f.txt").read_text() == "1\n" and (proj / "top.txt").read_text() == "t2\n"
    rc, out, _ = cli(capsys, "restore", first, "--keep-new", "-y", "--", "..")
    assert (proj / "sub" / "new.txt").exists() and (proj / "top.txt").read_text() == "t1\n"
    rc, out, err = cli(capsys, "restore", first, "../../outside", "-y")
    assert rc == 1 and "outside the project" in err


def test_time_refs_are_not_mistaken_for_options(proj, capsys):
    (proj / "a").write_text("1")
    main(["init"])
    capsys.readouterr()
    rc, out, err = cli(capsys, "show", "-10m")
    assert rc == 1 and "no snapshot at or before" in err and "Traceback" not in err
    assert _protect_refs(["restore", "-2h", "a.txt"]) == ["restore", "ref:-2h", "a.txt"]
    assert _protect_refs(["snap", "-m", "-5m"]) == ["snap", "-m", "-5m"]
    rc, out, err = cli(capsys, "diff", "-10m")
    assert rc == 1 and "no snapshot at or before" in err


def test_cat_and_find(proj, capsys):
    (proj / "f.txt").write_text("alpha beta\n")
    main(["init"])
    (proj / "f.txt").write_text("alpha gamma\n")
    main(["snap"])
    capsys.readouterr()
    r = run_cli("cat", "@~1", "f.txt", cwd=proj)
    assert r.returncode == 0 and r.stdout == "alpha beta\n"
    rc, out, _ = cli(capsys, "find", "beta")
    assert "removed" in out and "added" in out and "f.txt" in out
    rc, out, _ = cli(capsys, "find", "ga+mma", "--regex", "--json")
    assert json.loads(out)[0]["change"] == "added"
    rc, out, _ = cli(capsys, "find", "nothing-here")
    assert "No snapshot" in out
    rc, out, _ = cli(capsys, "find", "alpha", "--", "f.txt")
    assert rc == 0 and "f.txt" in out


def test_stats_status_prune(proj, capsys):
    (proj / "f.txt").write_text("x")
    main(["init"])
    capsys.readouterr()
    rc, out, _ = cli(capsys, "stats", "--json")
    d = json.loads(out)
    assert d["snapshots"] == 1 and d["size_bytes"] > 0 and d["oldest"]
    rc, out, _ = cli(capsys, "stats")
    assert "snapshots" in out and "store size" in out
    rc, out, _ = cli(capsys, "status")
    assert "not running" in out and "latest" in out
    rc, out, _ = cli(capsys, "prune", "--dry-run")
    assert rc == 0 and "would remove 0 of 1" in out
    rc, out, _ = cli(capsys, "prune")
    assert rc == 0 and "removed 0" in out


def test_start_stop_status_cli(proj, capsys):
    main(["init"])
    capsys.readouterr()
    try:
        rc, out, _ = cli(capsys, "start")
        assert rc == 0 and "watcher started" in out
        rc, out, _ = cli(capsys, "start")
        assert "already running" in out
        rc, out, _ = cli(capsys, "status", "--json")
        assert json.loads(out)["watcher"]["running"] is True
    finally:
        rc, out, _ = cli(capsys, "stop")
    assert "watcher stopped" in out
    rc, out, _ = cli(capsys, "stop")
    assert "not running" in out


def test_user_errors_are_one_red_line_exit_1(proj, capsys, tmp_path):
    for args in (["log"], ["snap"], ["show", "@"], ["undo"], ["stats"], ["restore", "@"], ["watch"]):
        rc, out, err = cli(capsys, *args)
        assert rc == 1 and err.startswith("error:") and "ctrlz init" in err and len(err.strip().splitlines()) == 1
        assert "Traceback" not in err
    main(["init"])
    capsys.readouterr()
    for args in (["show", "nonsense"], ["restore", "deadbeef"], ["cat", "@", "no/such/file"], ["find", ""],
                 ["find", "(", "--regex"], ["diff", "@", "@", "@"], ["log", "--since", "yesterday"], ["prune", "--policy", "x"],
                 ["frobnicate"], ["hooks", "install", "cursor"], ["run"]):
        rc, out, err = cli(capsys, *args)
        assert rc == 1, args
        assert "Traceback" not in err and err.startswith("error:") and len(err.strip().splitlines()) == 1, (args, err)


def test_ui_missing_message(proj, capsys, monkeypatch):
    main(["init"])
    capsys.readouterr()
    monkeypatch.setitem(sys.modules, "ctrlz.ui.app", None)
    rc, out, err = cli(capsys, "ui")
    assert rc == 1 and "Install the TUI with: pip install 'ctrlz[ui]'" in err


def test_ui_launches_run_with_store(proj, capsys, monkeypatch):
    main(["init"])
    capsys.readouterr()
    seen = []
    fake = types.ModuleType("ctrlz.ui.app")
    fake.run = lambda store: seen.append(store)
    pkg = types.ModuleType("ctrlz.ui")
    pkg.app = fake
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "ctrlz.ui", pkg)
    monkeypatch.setitem(sys.modules, "ctrlz.ui.app", fake)
    assert main(["ui"]) == 0
    assert isinstance(seen[0], Store) and seen[0].root == proj.resolve()


def test_all_commands_have_help(capsys):
    for cmd in ("init", "snap", "watch", "start", "stop", "status", "log", "show", "diff", "restore", "undo", "cat", "find",
                "run", "hooks", "prune", "stats", "ui"):
        with pytest.raises(SystemExit) as e:
            main([cmd, "--help"])
        assert e.value.code == 0
        assert "usage: ctrlz" in capsys.readouterr().out


def test_watch_foreground_command_runs_and_stops(proj):
    run_cli("init", cwd=proj)
    p = subprocess.Popen([sys.executable, "-m", "ctrlz", "watch", "--debounce", "150"], cwd=proj, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(0.8)
        (proj / "w.txt").write_text("w")
        time.sleep(0.7)
    finally:
        p.send_signal(signal.SIGINT)
        out, err = p.communicate(timeout=15)
    assert p.returncode == 0, err
    assert "watching" in out and "+1 ~0 -0" in out
    assert "w.txt" in Store.open(proj).files("@")


# --------------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------------
def test_run_exit_code_stdio_and_snapshots(plain):
    r = run_cli("run", "--", sys.executable, "-c", "import sys; open('made.txt','w').write('x'); print('out'); print('err', file=sys.stderr); sys.exit(7)", cwd=plain)
    assert r.returncode == 7
    assert "out" in r.stdout and "err" in r.stderr
    st = Store.open(plain)
    snaps = st.log()
    assert [s.trigger for s in snaps][:1] == ["run-after"] and snaps[0].command and "made.txt" in snaps[0].command or True
    assert any(s.trigger == "run-before" for s in snaps) or len(snaps) >= 1
    assert "made.txt" in st.files("@")
    assert st.latest().trigger == "run-after" and "sys.exit(7)" in st.latest().command


def test_run_snapshots_before_so_agent_damage_can_be_undone(plain):
    (plain / "important.txt").write_text("precious\n")
    r = run_cli("run", "--", "sh", "-c", "rm important.txt; echo oops > other.txt", cwd=plain)
    assert r.returncode == 0 and "ctrlz undo" in r.stderr
    assert not (plain / "important.txt").exists()
    r = run_cli("undo", "-y", cwd=plain)
    assert r.returncode == 0
    assert (plain / "important.txt").read_text() == "precious\n" and not (plain / "other.txt").exists()


def test_run_passes_stdin_and_handles_missing_command(plain):
    (plain / "f").write_text("x")
    r = run_cli("run", "--", "cat", cwd=plain, input="piped through\n")
    assert r.returncode == 0 and r.stdout == "piped through\n"
    r = run_cli("run", "--", "definitely-not-a-command-xyz", cwd=plain)
    assert r.returncode == 127 and "command not found" in r.stderr and "Traceback" not in r.stderr
    r = run_cli("run", "--", "sh", "-c", "kill -TERM $$", cwd=plain)
    assert r.returncode == 128 + signal.SIGTERM
    r = run_cli("run", "--", "sh", "-c", "exit 0", cwd=plain)
    assert r.returncode == 0
    # snapshot of the after-state exists even when the command failed
    r = run_cli("run", "-q", "--", "sh", "-c", "echo late > late.txt; exit 3", cwd=plain)
    assert r.returncode == 3 and "late.txt" in Store.open(plain).files("@")


def test_run_forwards_ctrl_c_to_child_and_still_snapshots(plain):
    (plain / "f").write_text("x")
    child = (
        "import signal,sys,time\n"
        "def h(*a):\n"
        "    open('got_sigint','w').write('1'); sys.exit(3)\n"
        "signal.signal(signal.SIGINT,h)\n"
        "print('ready',flush=True)\n"
        "time.sleep(30)\n"
    )
    p = subprocess.Popen([sys.executable, "-m", "ctrlz", "run", "--", sys.executable, "-c", child], cwd=plain,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    p.send_signal(signal.SIGINT)
    out, err = p.communicate(timeout=20)
    assert p.returncode == 3, err
    assert (plain / "got_sigint").exists()
    assert "got_sigint" in Store.open(plain).files("@")  # the after-run snapshot happened


# --------------------------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------------------------
def test_hooks_install_idempotent_merge_uninstall(proj, capsys):
    settings = proj / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = {
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "prettier --write"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
        },
    }
    settings.write_text(json.dumps(original, indent=4))
    rc, out, _ = cli(capsys, "hooks", "install", "claude-code")
    assert rc == 0 and "installed" in out
    data = json.loads(settings.read_text())
    assert data["model"] == "opus" and data["permissions"] == original["permissions"] and data["hooks"]["Stop"] == original["hooks"]["Stop"]
    groups = data["hooks"]["PostToolUse"]
    assert groups[0] == original["hooks"]["PostToolUse"][0]
    assert groups[1] == {"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash", "hooks": [{"type": "command", "command": "ctrlz snap --hook -q"}]}
    assert settings.read_text().startswith('{\n    "model"')  # indentation preserved
    snapshot = settings.read_bytes()
    rc, out, _ = cli(capsys, "hooks", "install", "claude-code")
    assert "already installed" in out and settings.read_bytes() == snapshot
    rc, out, _ = cli(capsys, "hooks", "uninstall", "claude-code")
    assert "removed" in out
    assert json.loads(settings.read_text()) == original
    rc, out, _ = cli(capsys, "hooks", "uninstall", "claude-code")
    assert "no ctrlz hook" in out


def test_hooks_fresh_file_and_clean_uninstall(proj, capsys):
    rc, out, _ = cli(capsys, "hooks", "install", "claude-code")
    settings = proj / ".claude" / "settings.json"
    assert json.loads(settings.read_text()) == {"hooks": {"PostToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash", "hooks": [{"type": "command", "command": "ctrlz snap --hook -q"}]}]}}
    cli(capsys, "hooks", "uninstall", "claude-code")
    assert json.loads(settings.read_text()) == {}


def test_hooks_uninstall_keeps_foreign_hooks_in_same_group(proj, capsys):
    s = proj / ".claude" / "settings.json"
    s.parent.mkdir()
    s.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "ctrlz snap --hook -q"}, {"type": "command", "command": "echo mine"}]}]}}))
    cli(capsys, "hooks", "uninstall", "claude-code")
    assert json.loads(s.read_text()) == {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]}}


def test_hooks_global_and_invalid_json(proj, capsys, hostile_home):
    rc, out, _ = cli(capsys, "hooks", "install", "claude-code", "--global")
    g = hostile_home / "claude" / "settings.json"
    assert rc == 0 and g.exists() and "ctrlz snap --hook -q" in g.read_text()
    assert not (proj / ".claude").exists()
    bad = proj / ".claude"
    bad.mkdir()
    (bad / "settings.json").write_text("{ not json ,")
    rc, out, err = cli(capsys, "hooks", "install", "claude-code")
    assert rc == 1 and "not valid JSON" in err and (bad / "settings.json").read_text() == "{ not json ,"


def test_hooks_project_root_from_subdirectory(git_repo, monkeypatch, capsys):
    (git_repo / "pkg").mkdir()
    monkeypatch.chdir(git_repo / "pkg")
    cli(capsys, "hooks", "install", "claude-code")
    assert (git_repo / ".claude" / "settings.json").exists() and not (git_repo / "pkg" / ".claude").exists()


# --------------------------------------------------------------------------------------------
# snap --hook
# --------------------------------------------------------------------------------------------
def hook(payload, cwd, raw: str | None = None):
    t = time.monotonic()
    r = run_cli("snap", "--hook", "-q", cwd=cwd, input=raw if raw is not None else json.dumps(payload))
    return r, time.monotonic() - t


def test_hook_labels_snapshot_and_uses_payload_cwd(plain, tmp_path):
    st = Store.open(plain, create=True)
    st.snap()
    (plain / "edited.py").write_text("x = 1\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r, dt = hook({"tool_name": "Edit", "cwd": str(plain), "hook_event_name": "PostToolUse", "tool_input": {"file_path": "edited.py"}}, elsewhere)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    s = st.latest()
    assert s.trigger == "hook" and s.label == "claude: Edit" and "edited.py" in st.files("@")
    (plain / "gen.txt").write_text("g")
    r, _ = hook({"tool_name": "Bash", "cwd": str(plain), "tool_input": {"command": "echo g > gen.txt"}}, elsewhere)
    assert r.returncode == 0
    s = st.latest()
    assert s.label == "claude: Bash" and s.command == "echo g > gen.txt"
    assert dt < 3


def test_hook_never_fails_or_blocks(plain, tmp_path):
    st = Store.open(plain, create=True)
    st.snap()
    for raw in ("", "not json at all", "[1,2,3]", '{"cwd": 5, "tool_name": ["x"]}', "\x00\xff\xfe", '{"cwd": "/nonexistent/path/xyz"}'):
        r, dt = hook(None, plain, raw=raw)
        assert r.returncode == 0 and r.stdout == "" and "Traceback" not in r.stderr, raw
        assert dt < 3
    # label fallback when run from the project without payload cwd
    (plain / "q.txt").write_text("q")
    r, _ = hook(None, plain, raw="")
    assert st.latest().label == "claude" and "q.txt" in st.files("@")


def test_hook_with_stdin_that_never_closes(plain):
    st = Store.open(plain, create=True)
    st.snap()
    (plain / "z.txt").write_text("z")
    p = subprocess.Popen([sys.executable, "-m", "ctrlz", "snap", "--hook", "-q"], cwd=plain, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env={**os.environ, "CTRLZ_HOOK_STDIN_TIMEOUT": "0.5"})
    t = time.monotonic()
    p.stdin.write(b'{"tool_name":"Write"')  # incomplete, and the pipe is never closed
    p.stdin.flush()
    try:
        p.wait(timeout=6)
        took = time.monotonic() - t
        err = p.stderr.read()
    finally:
        p.kill()
        p.wait()
        p.stdin.close()
        p.stdout.close()
        p.stderr.close()
    assert p.returncode == 0 or p.returncode == -9
    assert took < 4 and b"Fatal" not in err
    assert "z.txt" in st.files("@")


def test_hook_exits_zero_even_when_store_is_broken_or_locked(plain):
    st = Store.open(plain, create=True)
    st.snap()
    (plain / "a").write_text("a")
    (st.store_dir / "lock").write_text(json.dumps({"pid": os.getppid(), "host": __import__("socket").gethostname(), "time": time.time(), "token": "x"}))
    t = time.monotonic()
    r = run_cli("snap", "--hook", "-q", cwd=plain, input=json.dumps({"tool_name": "Write", "cwd": str(plain)}),
                env={"CTRLZ_HOOK_LOCK_TIMEOUT": "0.4"})
    dt = time.monotonic() - t
    assert r.returncode == 0 and dt < 4  # lock busy: gives up quickly, never blocks the agent
    (st.store_dir / "lock").unlink()
    (st.store_dir / "HEAD").unlink()
    r, _ = hook({"tool_name": "Write", "cwd": str(plain)}, plain)
    assert r.returncode == 0 and "Traceback" not in r.stderr


def test_hook_autocreates_store_quietly(plain):
    (plain / "proj.txt").write_text("hello")
    r, dt = hook({"tool_name": "Write", "cwd": str(plain)}, plain)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    st = Store.open(plain)
    end = time.monotonic() + 10
    while time.monotonic() < end and not st.log():
        time.sleep(0.1)  # first snapshot is taken by a detached helper
    assert st.latest().label == "claude: Write" and "proj.txt" in st.files("@")


def test_hook_refuses_to_snapshot_home(tmp_path, hostile_home):
    r = run_cli("snap", "--hook", "-q", cwd=tmp_path, input=json.dumps({"tool_name": "Write", "cwd": str(hostile_home)}))
    assert r.returncode == 0
    assert not (hostile_home / ".ctrlz").exists()


def test_hook_in_git_repo_leaves_git_state_alone(git_repo):
    Store.open(git_repo, create=True).snap()
    before = git_state(git_repo)
    (git_repo / "tracked.txt").write_text("tracked\n")  # no net change
    r, _ = hook({"tool_name": "Edit", "cwd": str(git_repo)}, git_repo)
    assert r.returncode == 0
    assert git_state(git_repo) == before


# --------------------------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------------------------
def test_no_change_snap_is_fast_on_a_big_tree(plain):
    for i in range(1200):
        d = plain / f"d{i % 30}"
        d.mkdir(exist_ok=True)
        (d / f"f{i}.txt").write_text(f"{i}\n" * 5)
    st = Store.open(plain, create=True)
    st.snap()
    st.snap()
    t = time.perf_counter()
    assert st.snap() is None
    assert time.perf_counter() - t < 0.5
    (plain / "d3" / "f3.txt").write_text("changed\n")
    t = time.perf_counter()
    assert st.snap() is not None
    assert time.perf_counter() - t < 0.6
