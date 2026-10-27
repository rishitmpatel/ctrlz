from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOSTILE_GITCONFIG = """\
[core]
    autocrlf = true
    safecrlf = true
    hooksPath = /nonexistent/hooks
    fsmonitor = true
    excludesFile = /nonexistent/ignore
    quotepath = true
[commit]
    gpgsign = true
[tag]
    gpgsign = true
[user]
    name = Hostile Config
    email = hostile@example.com
[diff]
    external = /bin/false
    noprefix = true
[init]
    templateDir = /nonexistent/templates
"""


@pytest.fixture(autouse=True)
def hostile_home(tmp_path_factory, monkeypatch):
    """Every test runs with a nasty global git config that ctrlz must be immune to."""
    home = tmp_path_factory.mktemp("home")
    cfg = home / "gitconfig"
    cfg.write_text(HOSTILE_GITCONFIG)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "claude"))
    monkeypatch.delenv("CTRLZ_ALLOW_ANY", raising=False)
    for k in list(os.environ):
        if k.startswith("GIT_") and k not in ("GIT_CONFIG_GLOBAL",):
            monkeypatch.delenv(k, raising=False)
    return home


def ugit(repo: Path, *args: str, check: bool = True) -> str:
    """Run git as 'the user' (with a sane config, independent of the hostile one)."""
    env = dict(os.environ)
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="u", GIT_AUTHOR_EMAIL="u@x",
               GIT_COMMITTER_NAME="u", GIT_COMMITTER_EMAIL="u@x", GIT_OPTIONAL_LOCKS="0")
    cp = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True)
    if check and cp.returncode:
        raise RuntimeError(cp.stderr.decode())
    return cp.stdout.decode(errors="replace")


def git_state(repo: Path, include_status: bool = True) -> dict:
    """Everything about the user's git state that ctrlz must never change."""
    gd = repo / ".git"
    files = {}
    for dp, dn, fn in os.walk(gd):
        dn[:] = [d for d in dn if not (Path(dp) == gd and d == "ctrlz")]
        for f in fn:
            p = Path(dp) / f
            files[str(p.relative_to(gd))] = hashlib.sha256(p.read_bytes()).hexdigest()
    st = {
        "files": files,  # includes index, HEAD, refs, config, packed objects, stash
        "head": ugit(repo, "rev-parse", "-q", "--verify", "HEAD", check=False),
        "refs": ugit(repo, "for-each-ref"),
        "stash": ugit(repo, "stash", "list"),
        "config": ugit(repo, "config", "--list", "--local"),
    }
    if include_status:
        st["status"] = ugit(repo, "status", "--porcelain=v1", "-z", "--ignored")
    return st


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ugit(repo, "init", "-q", "-b", "main")
    (repo / "tracked.txt").write_text("tracked\n")
    (repo / ".gitignore").write_text("ignored/\n*.log\nbuild/\n")
    ugit(repo, "add", "-A")
    ugit(repo, "commit", "-q", "-m", "init")
    (repo / "staged.txt").write_text("staged\n")
    ugit(repo, "add", "staged.txt")
    (repo / "untracked.txt").write_text("untracked\n")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "secret.txt").write_text("ignored data\n")
    ugit(repo, "stash", "push", "-q", "-m", "wip", "--include-untracked", "--", "untracked.txt")
    (repo / "untracked.txt").write_text("untracked again\n")
    return repo


@pytest.fixture
def plain(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    return d


@pytest.fixture
def store(plain):
    from ctrlz.store import Store

    return Store.open(plain, create=True)


def run_cli(*args: str, cwd: Path, input: str | None = None, env: dict | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, "-m", "ctrlz", *args], cwd=cwd, input=input, text=True, capture_output=True,
                          env=e, timeout=timeout)
