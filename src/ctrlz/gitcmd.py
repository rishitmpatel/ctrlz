"""The one and only way ctrlz talks to git.

Every invocation is pinned to the *shadow* repository: the environment is scrubbed of every
``GIT_*`` variable the caller may have (hooks, CI, worktrees ...), user/system config and hooks are
disabled, nothing can prompt, page, sign or convert line endings.  The user's own repository is never
addressed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class GitError(Exception):
    def __init__(self, args_: list[str], rc: int, stderr: bytes):
        self.cmd = args_
        self.rc = rc
        self.stderr = stderr.decode("utf-8", "replace").strip()
        super().__init__(f"git {' '.join(args_[:3])} failed ({rc}): {self.stderr}")


_GIT: str | None = None


def git_binary() -> str:
    global _GIT
    if _GIT is None:
        found = shutil.which("git")
        if not found:
            raise GitError(["git"], 127, b"the `git` binary was not found on PATH (ctrlz needs git >= 2.30)")
        _GIT = found
    return _GIT


def scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    return env


def shadow_env(store_dir: Path, root: Path, index_file: Path | None = None) -> dict[str, str]:
    env = scrubbed_env()
    sd = str(store_dir)
    env.update(
        GIT_DIR=sd,
        GIT_WORK_TREE=str(root),
        GIT_INDEX_FILE=str(index_file if index_file is not None else store_dir / "index"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ATTR_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_PAGER="cat",
        GIT_OPTIONAL_LOCKS="0",
        GIT_EDITOR="true",
        GIT_AUTHOR_NAME="ctrlz",
        GIT_AUTHOR_EMAIL="ctrlz@localhost",
        GIT_COMMITTER_NAME="ctrlz",
        GIT_COMMITTER_EMAIL="ctrlz@localhost",
        # old gits (<2.32) ignore GIT_CONFIG_GLOBAL; point HOME / XDG at an empty dir instead
        HOME=str(store_dir / "gitenv"),
        XDG_CONFIG_HOME=str(store_dir / "gitenv"),
        LC_ALL="C",
        LANG="C",
    )
    if os.name == "nt":
        env["USERPROFILE"] = str(store_dir / "gitenv")
    return env


def safe_config(store_dir: Path) -> list[str]:
    """-c options that neutralise anything in the environment that could change git's behaviour."""
    opts = {
        "core.autocrlf": "false",
        "core.safecrlf": "false",
        "core.quotepath": "false",
        "core.hooksPath": str(store_dir / "nohooks"),
        "core.fsmonitor": "false",
        "core.pager": "cat",
        "core.precomposeunicode": "false",
        "core.sparseCheckout": "false",
        "gc.auto": "0",
        "commit.gpgsign": "false",
        "tag.gpgsign": "false",
        "advice.addEmbeddedRepo": "false",
        "advice.addIgnoredFile": "false",
        "diff.renameLimit": "3000",
        "diff.external": "",
        "color.ui": "false",
        "safe.directory": "*",
    }
    out: list[str] = []
    for k, v in opts.items():
        out += ["-c", f"{k}={v}"]
    return out


def run_git(
    store_dir: Path,
    root: Path,
    args: list[str],
    *,
    input: bytes | None = None,
    check: bool = True,
    ok_codes: tuple[int, ...] = (0,),
    index_file: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    cmd = [git_binary(), *safe_config(store_dir), *args]
    try:
        cp = subprocess.run(
            cmd,
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(root),
            env=shadow_env(store_dir, root, index_file),
            timeout=timeout,
        )
    except FileNotFoundError as e:  # root vanished / git vanished
        raise GitError(args, 127, str(e).encode())
    if check and cp.returncode not in ok_codes:
        raise GitError(args, cp.returncode, cp.stderr)
    return cp


def popen_git(store_dir: Path, root: Path, args: list[str], **kw) -> subprocess.Popen:
    cmd = [git_binary(), *safe_config(store_dir), *args]
    return subprocess.Popen(cmd, cwd=str(root), env=shadow_env(store_dir, root), **kw)


def plain_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """git without any repo pinned (used for ``git init`` of the shadow repo itself)."""
    env = scrubbed_env()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    cp = subprocess.run([git_binary(), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                        cwd=str(cwd) if cwd else None)
    if cp.returncode != 0:
        raise GitError(args, cp.returncode, cp.stderr)
    return cp
