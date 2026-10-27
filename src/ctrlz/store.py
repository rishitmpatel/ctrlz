"""The ctrlz shadow store: a private bare git repo that snapshots a project directory.

Public API is documented in docs/SPEC.md.  Design rules (this is a *trust* tool):

* every git call goes through :mod:`ctrlz.gitcmd` and is pinned to the shadow repo;
* mutation of the store happens under a cross-process lock;
* restore never touches a file it did not first snapshot, never deletes ignored files, and
  re-verifies every file right before it overwrites/deletes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat as statmod
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal

from . import __version__
from .gitcmd import GitError, plain_git, popen_git, run_git
from .lock import FileLock, LockTimeout

REF = "refs/ctrlz/timeline"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DEFAULT_RETENTION = "1h=all,1d=10m,7d=1h,90d=1d"
MIN_PRUNE_AGE = 3600.0
STORE_NAMES = (".git", ".ctrlz")
TMP_PREFIX = ".ctrlz-tmp-"
DEFAULT_EXCLUDES = (
    "node_modules/", ".venv/", "venv/", "__pycache__/", "*.pyc",
    ".mypy_cache/", ".pytest_cache/", ".ruff_cache/", ".tox/", ".nox/",
    ".next/", ".nuxt/", ".turbo/", ".parcel-cache/", ".svelte-kit/",
    ".gradle/", ".DS_Store", "Thumbs.db",
)
TRIGGERS = ("watch", "manual", "run-before", "run-after", "restore-before", "restore-after", "hook")

DEFAULT_CONFIG = {
    "version": 1,
    "created_version": __version__,
    "max_file_mb": 50,
    "debounce_ms": 1500,
    "retention": DEFAULT_RETENTION,
}

_UMASK = os.umask(0)
os.umask(_UMASK)


class StoreError(Exception):
    """A user-facing error (bad ref, no store, locked ...). The CLI prints it as one red line."""


@dataclass(frozen=True)
class FileChange:
    path: str
    status: Literal["A", "M", "D", "R"]
    old_path: str | None = None


@dataclass(frozen=True)
class Snapshot:
    id: str
    short: str
    time: datetime
    trigger: str
    label: str | None
    command: str | None
    stats: dict
    tree: str


@dataclass
class RestoreResult:
    before: Snapshot | None
    written: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)  # subset of ``written`` that did not exist
    skipped_reasons: dict = field(default_factory=dict)
    undo_ref: str | None = None  # short id of the snapshot holding the pre-restore state
    target: Snapshot | None = None


@dataclass
class RestorePlan:
    target: Snapshot
    overwrite: list[str] = field(default_factory=list)
    create: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    chmod: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    skipped_reasons: dict = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.overwrite or self.create or self.delete or self.chmod)


@dataclass
class _Entry:
    path: str
    status: str
    tmode: str  # target side
    tsha: str
    cmode: str  # current side
    csha: str


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
def _fs(b: bytes) -> str:
    return os.fsdecode(b)


def _now() -> datetime:
    return datetime.now().astimezone()


def _git_date(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    mins = abs(mins)
    return f"{int(dt.timestamp())} {sign}{mins // 60:02d}{mins % 60:02d}"


def parse_duration(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*", text or "")
    if not m:
        raise StoreError(f"bad duration {text!r} (use e.g. 30s, 10m, 2h, 3d, 1w)")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]


def norm_path(path: str) -> str:
    """Normalise a project-relative path to git form (forward slashes, no ./ or trailing /)."""
    p = path.replace("\\", "/") if os.sep == "\\" else path
    parts = [c for c in p.split("/") if c not in ("", ".")]
    if ".." in parts:
        raise StoreError(f"path escapes the project: {path!r}")
    return "/".join(parts)


def _parse_retention(policy: str) -> list[tuple[float, float | None]]:
    tiers: list[tuple[float, float | None]] = []
    for part in policy.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise StoreError(f"bad retention tier {part!r} (expected AGE=INTERVAL, e.g. 1d=10m)")
        age, interval = (x.strip() for x in part.split("=", 1))
        tiers.append((parse_duration(age), None if interval == "all" else parse_duration(interval)))
    if not tiers:
        raise StoreError("empty retention policy")
    tiers.sort(key=lambda t: t[0])
    return tiers


def _ignore_escape(path: str) -> str:
    out = []
    for ch in path:
        if ch in "*?[]\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
    s = "".join(out)
    if s.startswith(("#", "!")) or s.endswith(" "):
        s = "\\" + s if s[0] in "#!" else s[:-1] + "\\ "
    return "/" + s


def _gitdir_of(d: Path) -> Path | None:
    g = d / ".git"
    try:
        if g.is_dir():
            return g
        if g.is_file():
            txt = g.read_text(errors="replace").strip()
            if txt.startswith("gitdir:"):
                target = Path(txt[7:].strip())
                if not target.is_absolute():
                    target = d / target
                if target.is_dir():
                    return target.resolve()
    except OSError:
        pass
    return None


def blob_sha_of_path(p: str) -> str | None:
    """git blob sha1 of a working-tree path ('dir' for directories, None if missing)."""
    try:
        st = os.lstat(p)
    except OSError:
        return None
    if statmod.S_ISLNK(st.st_mode):
        data = os.fsencode(os.readlink(p))
        return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    if statmod.S_ISDIR(st.st_mode):
        return "dir"
    if not statmod.S_ISREG(st.st_mode):
        return "special"
    h = hashlib.sha1()
    try:
        with open(p, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            h.update(b"blob %d\0" % size)
            seen = 0
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                seen += len(chunk)
                h.update(chunk)
            if seen != size:
                return "changed"
    except OSError:
        return None
    return h.hexdigest()


# ----------------------------------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------------------------------
class Store:
    root: Path
    store_dir: Path
    config: dict

    def __init__(self, root: Path, store_dir: Path):
        self.root = Path(root)
        self.store_dir = Path(store_dir)
        self.config = self._load_config()
        self.lock_timeout: float = 60.0
        self._oversize: dict[str, int] = self._load_oversize()
        self.last_skipped: dict[str, int] = dict(self._oversize)  # path -> size, files skipped as too large
        self.last_warnings: list[str] = []

    # -- discovery / creation -----------------------------------------------------------------
    @classmethod
    def find(cls, path: Path | str | None = None) -> "Store | None":
        try:
            return cls.open(path, create=False)
        except StoreError:
            return None

    @classmethod
    def open(cls, path: Path | str | None = None, create: bool = False) -> "Store":
        p = Path(path if path is not None else Path.cwd())
        try:
            p = p.expanduser().resolve()
        except OSError as e:
            raise StoreError(f"cannot access {p}: {e}")
        if p.is_file():
            p = p.parent
        if not p.is_dir():
            raise StoreError(f"not a directory: {p}")
        for d in (p, *p.parents):
            s = d / ".ctrlz"
            if (s / "ctrlz.json").is_file():
                return cls(d, s)
            gd = _gitdir_of(d)
            if gd is not None and (gd / "ctrlz" / "ctrlz.json").is_file():
                return cls(d, gd / "ctrlz")
        if not create:
            raise StoreError(f"no ctrlz store found in {p} or any parent directory - run `ctrlz init` first")
        root, store_dir = p, p / ".ctrlz"
        for d in (p, *p.parents):
            gd = _gitdir_of(d)
            if gd is not None:
                root, store_dir = d, gd / "ctrlz"
                break
        cls._guard_root(root)
        cls._init_store(root, store_dir)
        return cls(root, store_dir)

    @staticmethod
    def _guard_root(root: Path) -> None:
        if os.environ.get("CTRLZ_ALLOW_ANY") == "1":
            return
        if root == Path(root.anchor) or root == Path.home().resolve():
            raise StoreError(
                f"refusing to snapshot {root} (your home directory / filesystem root) - "
                "cd into a project folder first"
            )

    @staticmethod
    def _init_store(root: Path, store_dir: Path) -> None:
        try:
            store_dir.mkdir(parents=True, exist_ok=True)
            if not (store_dir / "HEAD").exists():
                plain_git(["init", "--bare", "--quiet", "--template=", str(store_dir)])
            cfg = {
                "core.bare": "false",
                "core.filemode": "true" if os.name == "posix" else "false",
                "core.symlinks": "true" if os.name == "posix" else "false",
                "core.autocrlf": "false",
                "core.safecrlf": "false",
                "core.untrackedCache": "true",
                "core.preloadIndex": "true",
                "core.logAllRefUpdates": "false",
                "core.looseCompression": "1",
                "core.longpaths": "true",
                "core.precomposeunicode": "false",
                "index.version": "4",
                "gc.auto": "0",
                "gc.autoDetach": "false",
                "advice.addEmbeddedRepo": "false",
            }
            for k, v in cfg.items():
                plain_git(["config", "--file", str(store_dir / "config"), k, v])
            plain_git(["--git-dir", str(store_dir), "symbolic-ref", "HEAD", "refs/heads/ctrlz"])
            (store_dir / "info").mkdir(exist_ok=True)
            (store_dir / "gitenv").mkdir(exist_ok=True)
            # info/attributes beats any in-tree .gitattributes: no filters, no eol conversion, ever.
            (store_dir / "info" / "attributes").write_text(
                "* -text -eol -crlf -filter -ident -working-tree-encoding\n"
            )
            (store_dir / "info" / "exclude").write_text(".ctrlz/\n.ctrlz-tmp-*\n")
            if store_dir.name == ".ctrlz":
                (store_dir / ".gitignore").write_text("*\n")  # invisible to a future `git init`
            cfgfile = store_dir / "ctrlz.json"
            if not cfgfile.exists():
                data = dict(DEFAULT_CONFIG)
                data["created"] = _now().isoformat(timespec="seconds")
                tmp = store_dir / f"ctrlz.json.{uuid.uuid4().hex}.tmp"
                tmp.write_text(json.dumps(data, indent=2) + "\n")
                os.replace(tmp, cfgfile)
        except (OSError, GitError) as e:
            raise StoreError(f"could not create the ctrlz store at {store_dir}: {e}")

    def _load_config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        try:
            cfg.update(json.loads((self.store_dir / "ctrlz.json").read_text()))
        except (OSError, ValueError):
            pass
        return cfg

    def save_config(self) -> None:
        tmp = self.store_dir / f"ctrlz.json.{uuid.uuid4().hex}.tmp"
        tmp.write_text(json.dumps(self.config, indent=2) + "\n")
        os.replace(tmp, self.store_dir / "ctrlz.json")

    def _load_oversize(self) -> dict[str, int]:
        try:
            d = json.loads((self.store_dir / "oversize.json").read_text())
            return {str(k): int(v) for k, v in d.items()}
        except (OSError, ValueError, AttributeError):
            return {}

    # -- plumbing -----------------------------------------------------------------------------
    @property
    def max_bytes(self) -> int:
        return int(float(self.config.get("max_file_mb", 50)) * 1024 * 1024)

    def _g(self, *args: str, input: bytes | None = None, check: bool = True, ok_codes=(0,)) -> subprocess.CompletedProcess:
        try:
            return run_git(self.store_dir, self.root, list(args), input=input, check=check, ok_codes=ok_codes)
        except GitError as e:
            raise StoreError(f"git error: {e.stderr or e}") from e

    def _lock(self) -> FileLock:
        return FileLock(self.store_dir / "lock", timeout=self.lock_timeout)

    def _locked(self):
        lk = self._lock()

        class _Ctx:
            def __enter__(_s):
                try:
                    lk.acquire()
                except LockTimeout as e:
                    raise StoreError(str(e)) from e
                except OSError as e:
                    raise StoreError(f"cannot lock the ctrlz store ({self.store_dir}): {e.strerror or e}") from e
                return lk

            def __exit__(_s, *exc):
                lk.release()

        return _Ctx()

    def relpath(self, path: str | Path, base: Path | None = None) -> str:
        """Convert a user supplied path (relative to ``base``/cwd, or absolute) to a root-relative git path."""
        p = Path(path)
        if not p.is_absolute():
            p = (base or Path.cwd()) / p
        ap = Path(os.path.abspath(p))
        for r in (self.root, self.root.resolve()):
            try:
                return norm_path(ap.relative_to(r).as_posix())
            except ValueError:
                continue
        try:
            return norm_path(Path(os.path.realpath(ap)).relative_to(self.root.resolve()).as_posix())
        except ValueError:
            raise StoreError(f"path is outside the project: {path}")

    # -- snapshots ----------------------------------------------------------------------------
    def _meta_to_snapshot(self, sha: str, tree: str, ct: str, body: bytes) -> Snapshot:
        try:
            meta = json.loads(body.decode("utf-8", "replace").strip())
            if not isinstance(meta, dict):
                raise ValueError
            t = datetime.fromisoformat(meta["time"]).astimezone()
        except (ValueError, KeyError, TypeError):
            meta = {}
            try:
                t = datetime.fromtimestamp(int(ct)).astimezone()
            except ValueError:
                t = _now()
        stats = meta.get("stats") or {}
        stats = {k: int(stats.get(k, 0) or 0) for k in ("added", "modified", "deleted")}
        return Snapshot(
            id=sha, short=sha[:7], time=t, trigger=str(meta.get("trigger") or "unknown"),
            label=meta.get("label"), command=meta.get("command"), stats=stats, tree=tree,
        )

    def _parse_log(self, raw: bytes) -> list[Snapshot]:
        out = []
        for rec in raw.split(b"\0"):
            rec = rec.strip(b"\n")
            if not rec:
                continue
            parts = rec.split(b"\x1f", 3)
            if len(parts) < 4:
                continue
            out.append(self._meta_to_snapshot(parts[0].decode(), parts[1].decode(), parts[2].decode(), parts[3]))
        return out

    _FMT = "--format=%H%x1f%T%x1f%ct%x1f%B"

    def _log_raw(self, *extra: str, rev: str = REF, pathspec: str | None = None) -> list[Snapshot]:
        tail = ["--", f":(literal){pathspec}"] if pathspec else ["--"]
        cp = self._g("log", "-z", self._FMT, "--no-renames", *extra, rev, *tail, check=False)
        if cp.returncode != 0:
            msg = cp.stderr.decode("utf-8", "replace")
            if any(x in msg for x in ("unknown revision", "bad default revision", "ambiguous argument", "bad revision")):
                return []
            raise StoreError(f"git error: {msg.strip()}")
        return self._parse_log(cp.stdout)

    def latest(self) -> Snapshot | None:
        r = self._log_raw("-n1")
        return r[0] if r else None

    def log(self, limit: int | None = None, since: datetime | None = None, path: str | None = None) -> list[Snapshot]:
        if limit is not None and int(limit) <= 0:
            return []
        extra: list[str] = ["-n", str(int(limit))] if (limit is not None and since is None) else []
        np = norm_path(path) if path else ""
        snaps = self._log_raw(*extra, pathspec=np or None)
        if since is not None:
            s = since if since.tzinfo else since.astimezone()
            snaps = [x for x in snaps if x.time >= s]
        if limit is not None:
            snaps = snaps[: int(limit)]
        return snaps

    def _commit(self, sha: str) -> Snapshot:
        r = self._log_raw("-n1", rev=sha)
        if not r:
            raise StoreError(f"unknown snapshot {sha}")
        return r[0]

    def get(self, ref) -> Snapshot:
        if isinstance(ref, Snapshot):
            return ref
        ref = str(ref).strip()
        if not ref:
            raise StoreError("empty snapshot reference")
        if ref == "@":
            s = self.latest()
            if s is None:
                raise StoreError("there are no snapshots yet - run `ctrlz snap`")
            return s
        m = re.fullmatch(r"@~(\d+)", ref)
        if m:
            n = int(m.group(1))
            snaps = self._log_raw("-n", str(n + 1))
            if len(snaps) <= n:
                raise StoreError(f"{ref}: only {len(snaps)} snapshot(s) exist")
            return snaps[n]
        m = re.fullmatch(r"-(\d+(?:\.\d+)?[smhdw])", ref)
        if m:
            return self._at_time(_now() - timedelta(seconds=parse_duration(m.group(1))), ref)
        if re.fullmatch(r"[0-9a-fA-F]{4,64}", ref):
            cp = self._g("rev-parse", "--verify", "--quiet", f"{ref.lower()}^{{commit}}", check=False)
            sha = cp.stdout.decode().strip()
            if cp.returncode != 0 or not sha:
                raise StoreError(f"unknown or ambiguous snapshot {ref!r}")
            anc = self._g("merge-base", "--is-ancestor", sha, REF, check=False)
            if anc.returncode != 0:
                raise StoreError(f"{ref!r} is not a ctrlz snapshot")
            return self._commit(sha)
        try:
            when = datetime.fromisoformat(ref)
        except ValueError:
            raise StoreError(
                f"bad snapshot reference {ref!r} (use a short sha, @, @~N, or a time like -10m / -2h / -3d)"
            )
        return self._at_time(when if when.tzinfo else when.astimezone(), ref)

    def _at_time(self, when: datetime, ref: str) -> Snapshot:
        snaps = self._log_raw()
        for s in snaps:
            if s.time <= when:
                return s
        if not snaps:
            raise StoreError("there are no snapshots yet")
        raise StoreError(f"no snapshot at or before {ref} (oldest is {snaps[-1].time:%Y-%m-%d %H:%M:%S})")

    def _id(self, ref) -> Snapshot:
        return self.get(ref)

    # -- snapshot creation ----------------------------------------------------------------------
    def _write_if_changed(self, path: Path, data: bytes) -> None:
        try:
            if path.read_bytes() == data:
                return
        except OSError:
            pass
        tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def _refresh_excludes(self) -> None:
        """Rebuild info/exclude (built-ins + .ctrlzignore + oversize files); drop big files from the index."""
        max_bytes = self.max_bytes
        # revalidate previously known oversize files
        keep: dict[str, int] = {}
        for rel, _ in self._oversize.items():
            try:
                sz = os.lstat(self.root / rel).st_size
            except OSError:
                continue
            if sz > max_bytes:
                keep[rel] = sz
        # candidates: untracked + modified tracked files (cheap stat-based listing)
        cp = self._g("ls-files", "-z", "-o", "-m", "--exclude-standard", check=False)
        remove_from_index: list[str] = []
        if cp.returncode == 0:
            for raw in cp.stdout.split(b"\0"):
                if not raw:
                    continue
                rel = _fs(raw)
                if rel in keep:
                    continue
                try:
                    st = os.lstat(self.root / rel)
                except OSError:
                    continue
                if statmod.S_ISREG(st.st_mode) and st.st_size > max_bytes:
                    keep[rel] = st.st_size
                    remove_from_index.append(rel)
        self._oversize = keep
        self.last_skipped = dict(keep)
        lines = [".ctrlz/", ".ctrlz-tmp-*"]
        # Regenerable dependency/cache folders. Listed before .ctrlzignore so a
        # user can re-include one with a `!pattern` line there.
        lines.append("# built-in: dependency and cache folders")
        lines.extend(DEFAULT_EXCLUDES)
        ign = self.root / ".ctrlzignore"
        try:
            user = ign.read_text(encoding="utf-8", errors="replace")
            lines.append("# .ctrlzignore")
            lines.extend(user.splitlines())
        except OSError:
            pass
        user_ex = self._user_info_exclude()
        if user_ex:
            lines.append("# .git/info/exclude")
            lines.extend(user_ex)
        lines.append("# files larger than max_file_mb")
        for rel in sorted(keep):
            if "\n" in rel:
                continue
            lines.append(_ignore_escape(rel))
        (self.store_dir / "info").mkdir(exist_ok=True)
        self._write_if_changed(self.store_dir / "info" / "exclude", ("\n".join(lines) + "\n").encode("utf-8", "surrogateescape"))
        self._write_if_changed(self.store_dir / "oversize.json", json.dumps(keep).encode())
        if remove_from_index:
            data = b"".join(os.fsencode(p) + b"\0" for p in remove_from_index)
            self._g("update-index", "--force-remove", "-z", "--stdin", input=data, check=False)

    def _user_info_exclude(self) -> list[str]:
        """The user's own .git/info/exclude (if the store lives inside a git dir), so personal ignores are honoured."""
        if self.store_dir.name != "ctrlz":
            return []
        gd = self.store_dir.parent
        candidates = [gd / "info" / "exclude"]
        try:  # linked worktree: info/exclude lives in the common dir
            common = (gd / "commondir").read_text().strip()
            candidates.append((gd / common).resolve() / "info" / "exclude")
        except OSError:
            pass
        for c in candidates:
            try:
                lines = c.read_text(encoding="utf-8", errors="replace").splitlines()
                return [ln for ln in lines if ln.strip() and not ln.startswith("#")]
            except OSError:
                continue
        return []

    def _clear_stale_git_locks(self) -> None:
        """We hold the store lock, so no other writer exists: leftover *.lock files are from a crashed git."""
        for rel in ("index.lock", "refs/ctrlz/timeline.lock", "HEAD.lock", "config.lock"):
            try:
                os.unlink(self.store_dir / rel)
            except OSError:
                pass

    def _stage(self) -> str:
        """Stage the working tree into the shadow index and return the tree sha. Caller holds the lock."""
        self._clear_stale_git_locks()
        try:
            return self._stage_once()
        except StoreError:
            # The shadow index is only a cache of (work tree + last snapshot): if it is damaged, rebuild it.
            idx = self.store_dir / "index"
            if not idx.exists():
                raise
            os.unlink(idx)
            return self._stage_once()

    def _stage_once(self) -> str:
        self._refresh_excludes()
        cp = self._g("add", "-A", "--ignore-errors", check=False, ok_codes=(0, 1))
        self.last_warnings = []
        if cp.returncode not in (0, 1):
            raise StoreError(f"git add failed: {cp.stderr.decode('utf-8', 'replace').strip()}")
        if cp.returncode == 1 and cp.stderr:
            self.last_warnings = [ln for ln in cp.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        wt = self._g("write-tree")
        return wt.stdout.decode().strip()

    def _head_and_tree(self) -> tuple[str | None, str | None]:
        cp = self._g("rev-parse", REF, f"{REF}^{{tree}}", check=False)
        if cp.returncode != 0:
            return None, None
        lines = cp.stdout.decode().split()
        if len(lines) != 2:
            return None, None
        return lines[0], lines[1]

    def _count_changes(self, old_tree: str, new_tree: str) -> dict:
        cp = self._g("diff-tree", "-r", "--name-status", "-z", "--no-renames", old_tree, new_tree)
        parts = cp.stdout.split(b"\0")
        a = m = d = 0
        i = 0
        while i + 1 < len(parts):
            st = parts[i][:1]
            if st == b"A":
                a += 1
            elif st == b"D":
                d += 1
            elif st:
                m += 1
            i += 2
        return {"added": a, "modified": m, "deleted": d}

    def _commit_tree(self, tree: str, parent: str | None, meta: dict, when: datetime) -> str:
        msg = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
        args = ["commit-tree", tree, "-F", "-"]
        if parent:
            args[2:2] = ["-p", parent]
        d = _git_date(when)
        env_extra = {"GIT_AUTHOR_DATE": d, "GIT_COMMITTER_DATE": d}
        from .gitcmd import git_binary, safe_config, shadow_env

        env = shadow_env(self.store_dir, self.root)
        env.update(env_extra)
        cp = subprocess.run([git_binary(), *safe_config(self.store_dir), *args], input=msg.encode("utf-8"),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(self.root), env=env)
        if cp.returncode != 0:
            raise StoreError(f"git commit-tree failed: {cp.stderr.decode('utf-8', 'replace').strip()}")
        return cp.stdout.decode().strip()

    def snap(self, trigger: str = "manual", label: str | None = None, command: str | None = None) -> Snapshot | None:
        with self._locked():
            return self._snap_locked(trigger, label, command)

    def _snap_locked(self, trigger: str, label: str | None, command: str | None) -> Snapshot | None:
        tree = self._stage()
        head, head_tree = self._head_and_tree()
        if head is not None and head_tree == tree:
            return None
        when = _now()
        stats = self._count_changes(head_tree or EMPTY_TREE, tree)
        meta = {
            "v": 1, "time": when.isoformat(timespec="milliseconds"), "trigger": trigger,
            "label": label, "command": command, "stats": stats,
        }
        new = self._commit_tree(tree, head, meta, when)
        upd = ["update-ref", REF, new]
        if head:
            upd.append(head)
        else:
            upd.append("0" * 40)
        self._g(*upd)
        return self._meta_to_snapshot(new, tree, str(int(when.timestamp())), json.dumps(meta).encode())

    # -- reading -------------------------------------------------------------------------------
    def changes(self, snap) -> list[FileChange]:
        s = self.get(snap)
        cp = self._g("diff-tree", "--root", "-r", "-M", "--name-status", "-z", "--no-commit-id", s.id)
        return self._parse_name_status(cp.stdout)

    @staticmethod
    def _parse_name_status(raw: bytes) -> list[FileChange]:
        parts = raw.split(b"\0")
        out: list[FileChange] = []
        i = 0
        while i < len(parts):
            st = parts[i].decode("ascii", "replace")
            if not st:
                i += 1
                continue
            if st[0] in "RC":
                if i + 2 >= len(parts):
                    break
                out.append(FileChange(_fs(parts[i + 2]), "R", _fs(parts[i + 1])))
                i += 3
            else:
                if i + 1 >= len(parts):
                    break
                code = st[0] if st[0] in "AMD" else "M"
                out.append(FileChange(_fs(parts[i + 1]), code, None))  # type: ignore[arg-type]
                i += 2
        out.sort(key=lambda c: c.path)
        return out

    def recent_changes(self, snaps: list[Snapshot]) -> dict[str, list[FileChange]]:
        return {s.id: self.changes(s) for s in snaps}

    def diff(self, a, b=None, paths: list[str] | None = None) -> str:
        ta = self.get(a).tree
        if b is None:
            with self._locked():
                tb = self._stage()
        else:
            tb = self.get(b).tree
        args = ["diff", "--no-color", "--no-ext-diff", "-M", ta, tb]
        if paths:
            args += ["--", *[f":(literal){norm_path(p)}" for p in paths if norm_path(p)]]
        cp = self._g(*args)
        return cp.stdout.decode("utf-8", "replace")

    def files(self, ref) -> list[str]:
        t = self.get(ref).tree
        cp = self._g("ls-tree", "-r", "-z", "--name-only", t)
        return [_fs(x) for x in cp.stdout.split(b"\0") if x]

    def read(self, ref, path: str) -> bytes:
        s = self.get(ref)
        p = norm_path(path)
        cp = self._g("cat-file", "blob", f"{s.tree}:{p}", check=False)
        if cp.returncode != 0:
            raise StoreError(f"{p}: not in snapshot {s.short}")
        return cp.stdout

    def ignored_paths(self) -> set[str]:
        """Ignored files/dirs (collapsed) - used by the watcher to drop noise events."""
        cp = self._g("ls-files", "-z", "-o", "-i", "--exclude-standard", "--directory", check=False)
        out = set()
        for raw in cp.stdout.split(b"\0"):
            if raw:
                out.add(_fs(raw).rstrip("/"))
        return out

    # -- history search ------------------------------------------------------------------------
    def grep_history(self, pattern: str, path: str | None = None, regex: bool = False):
        if not pattern:
            raise StoreError("empty search text")
        if regex:
            try:
                rx = re.compile(pattern.encode("utf-8"))
            except re.error as e:
                raise StoreError(f"bad regex: {e}")
            count = lambda b: len(rx.findall(b))  # noqa: E731
            pick = []
        else:
            needle = pattern.encode("utf-8")
            count = lambda b: b.count(needle)  # noqa: E731
            pick = [f"-S{pattern}"]
        np = norm_path(path) if path else ""
        args = ["log", "-z", "--format=%x01%H", "--raw", "--no-renames", "--root", "--no-abbrev", *pick, REF, "--"]
        if np:
            args.append(f":(literal){np}")
        cp = self._g(*args, check=False)
        if cp.returncode != 0:
            msg = cp.stderr.decode("utf-8", "replace")
            if "unknown revision" in msg or "bad default revision" in msg or "ambiguous" in msg:
                return []
            raise StoreError(f"git error: {msg.strip()}")
        # records: \x01<sha>\0 then (":modes shas S"\0 path\0)*  repeated
        tokens = cp.stdout.split(b"\0")
        entries: list[tuple[str, str, str, str]] = []  # sha, path, old, new
        cur = None
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t.startswith(b"\x01") or (t.startswith(b"\n") and t[1:2] == b"\x01"):
                cur = t.lstrip(b"\n")[1:].decode()
                i += 1
            elif t.lstrip(b"\n").startswith(b":") and cur and i + 1 < len(tokens):
                f = t.lstrip(b"\n")[1:].split()
                entries.append((cur, _fs(tokens[i + 1]), f[2].decode(), f[3].decode()))
                i += 2
            else:
                i += 1
        if np:
            entries = [e for e in entries if e[1] == np or e[1].startswith(np + "/")]
        blobs = _BlobReader(self)
        hits: list[tuple[str, str, str]] = []
        try:
            for sha, p, old, new in entries:
                o = 0 if set(old) == {"0"} else count(blobs.get(old))
                n = 0 if set(new) == {"0"} else count(blobs.get(new))
                if n > o:
                    hits.append((sha, p, "added"))
                elif n < o:
                    hits.append((sha, p, "removed"))
        finally:
            blobs.close()
        cache: dict[str, Snapshot] = {}
        out = []
        for sha, p, kind in hits:
            if sha not in cache:
                cache[sha] = self._commit(sha)
            out.append((cache[sha], p, kind))
        return out

    # -- restore -------------------------------------------------------------------------------
    @staticmethod
    def _unsafe_path(p: str) -> str | None:
        parts = p.split("/")
        if parts[0] in STORE_NAMES or ".." in parts or p.startswith("/") or (os.sep == "\\" and ":" in parts[0]):
            return "protected path"
        if any(c.startswith(TMP_PREFIX) for c in parts[-1:]):
            return "ctrlz temp file"
        return None

    def _diff_entries(self, target_tree: str, cur_tree: str) -> list[_Entry]:
        cp = self._g("diff-tree", "-r", "--raw", "-z", "--no-renames", "--no-abbrev", target_tree, cur_tree)
        toks = cp.stdout.split(b"\0")
        out = []
        i = 0
        while i + 1 < len(toks):
            meta = toks[i]
            if not meta.startswith(b":"):
                i += 1
                continue
            f = meta[1:].split()
            out.append(_Entry(_fs(toks[i + 1]), f[4].decode()[:1], f[0].decode(), f[2].decode(), f[1].decode(), f[3].decode()))
            i += 2
        return out

    def _ignored_tracked(self) -> set[str]:
        cp = self._g("ls-files", "-z", "-c", "-i", "--exclude-standard", check=False)
        return {_fs(x) for x in cp.stdout.split(b"\0") if x} if cp.returncode == 0 else set()

    def _build_plan(self, target: Snapshot, cur_tree: str, paths: list[str] | None, delete_extra: bool) -> tuple[RestorePlan, list[_Entry]]:
        plan = RestorePlan(target=target)
        prefixes = None
        if paths:
            prefixes = [norm_path(p) for p in paths]
            if any(p == "" for p in prefixes):
                prefixes = None
        explicit = prefixes is not None
        entries = self._diff_entries(target.tree, cur_tree)

        def wanted(p: str) -> bool:
            return prefixes is None or any(p == x or p.startswith(x + "/") for x in prefixes)

        ignored: set[str] | None = None
        todo: list[_Entry] = []
        matched = set()
        for e in entries:
            if not wanted(e.path):
                continue
            if prefixes:
                for x in prefixes:
                    if e.path == x or e.path.startswith(x + "/"):
                        matched.add(x)
            if "160000" in (e.tmode, e.cmode):
                continue  # nested repositories are never touched
            why = self._unsafe_path(e.path)
            if why:
                plan.skipped.append(e.path)
                plan.skipped_reasons[e.path] = why
                continue
            if e.path in self._oversize:
                plan.skipped.append(e.path)
                plan.skipped_reasons[e.path] = "larger than max_file_mb (not snapshotted)"
                continue
            if e.status == "A":  # only in the current state -> extra file
                named = bool(prefixes) and e.path in prefixes  # the user named this very file
                if not (delete_extra or named):
                    continue
                if ignored is None:
                    ignored = self._ignored_tracked()
                if e.path in ignored and not named:
                    plan.skipped.append(e.path)
                    plan.skipped_reasons[e.path] = "ignored file"
                    continue
                plan.delete.append(e.path)
                todo.append(e)
            elif e.status == "D":
                plan.create.append(e.path)
                todo.append(e)
            else:
                if e.tsha == e.csha and (e.tmode == "120000") == (e.cmode == "120000"):
                    plan.chmod.append(e.path)
                else:
                    plan.overwrite.append(e.path)
                todo.append(e)
        if prefixes:
            for x in prefixes:
                if x not in matched:
                    # unchanged (fine) or never existed anywhere (tell the user)
                    exists_target = bool(self._g("ls-tree", "-r", "--name-only", "-z", target.tree, "--", f":(literal){x}").stdout.strip(b"\0"))
                    if not exists_target and not os.path.lexists(self.root / x):
                        plan.skipped.append(x)
                        plan.skipped_reasons[x] = "not found in the snapshot or the project"
        for lst in (plan.overwrite, plan.create, plan.delete, plan.chmod):
            lst.sort()
        return plan, todo

    def plan_restore(self, ref, paths: list[str] | None = None, delete_extra: bool = True) -> RestorePlan:
        """What ``restore`` would do right now (no files or snapshots are written)."""
        target = self.get(ref)
        with self._locked():
            cur = self._stage()
            plan, _ = self._build_plan(target, cur, paths, delete_extra)
        return plan

    def _undo_target(self, cur_tree: str) -> Snapshot:
        head = self.latest()
        if head is None:
            raise StoreError("nothing to undo - there are no snapshots yet")
        if cur_tree != head.tree:
            return head  # a fresh snapshot of the current state would have `head` as its parent
        parents = self._g("rev-list", "--parents", "-n1", head.id).stdout.decode().split()[1:]
        if not parents:
            raise StoreError("nothing to undo - this is the first snapshot")
        return self._commit(parents[0])

    def plan_undo(self) -> RestorePlan:
        with self._locked():
            cur = self._stage()
            target = self._undo_target(cur)
            plan, _ = self._build_plan(target, cur, None, True)
        return plan

    def undo(self) -> RestoreResult:
        with self._locked():
            cur = self._stage()
            target = self._undo_target(cur)
            return self._restore_locked(target, None, True)

    def restore(self, ref, paths: list[str] | None = None, delete_extra: bool = True) -> RestoreResult:
        target = self.get(ref)
        with self._locked():
            return self._restore_locked(target, paths, delete_extra)

    def _restore_locked(self, target: Snapshot, paths, delete_extra: bool) -> RestoreResult:
        before: Snapshot | None = None
        plan: RestorePlan | None = None
        todo: list[_Entry] = []
        stale: dict[str, str] = {}
        for attempt in range(4):
            try:
                snap = self._snap_locked("restore-before", f"before restore to {target.short}", None)
            except StoreError as e:
                raise StoreError(f"restore aborted - could not snapshot the current state first: {e}") from e
            if snap is not None:
                before = snap
            latest = self.latest()
            assert latest is not None
            plan, todo = self._build_plan(target, latest.tree, paths, delete_extra)
            stale = self._verify_unchanged(todo)
            if not stale:
                break
        assert plan is not None
        res = RestoreResult(before=before, target=target)
        head_now = self.latest()
        res.undo_ref = head_now.short if head_now else None
        res.skipped = list(plan.skipped)
        res.skipped_reasons = dict(plan.skipped_reasons)
        for p, why in stale.items():
            res.skipped.append(p)
            res.skipped_reasons[p] = why
        todo = [e for e in todo if e.path not in stale]
        self._apply(todo, res)
        res.written.sort()
        res.deleted.sort()
        if res.written or res.deleted:
            # record the new state so the timeline always ends at "what is on disk now" (makes undo-of-undo exact)
            try:
                self._snap_locked("restore-after", f"restored to {target.short}", None)
            except StoreError:
                pass
        return res

    def _verify_unchanged(self, todo: list[_Entry]) -> dict[str, str]:
        """Re-check, right before touching them, that files still match what we just snapshotted."""
        stale: dict[str, str] = {}
        for e in todo:
            cur = blob_sha_of_path(str(self.root / e.path))
            if e.status == "D":  # to be created: nothing foreign may be sitting there
                if cur is not None and cur != "dir" and cur != e.tsha:  # (a dir is fine: its files are being deleted)
                    stale[e.path] = "a different file appeared there while restoring (or it is an ignored file)"
            elif cur is not None and cur != e.csha:  # to be overwritten / deleted
                stale[e.path] = "changed while restoring"
        return stale

    def _apply(self, todo: list[_Entry], res: RestoreResult) -> None:
        # 1. deletions first (also resolves file<->directory conflicts)
        for e in [x for x in todo if x.status == "A"]:
            ap = self.root / e.path
            try:
                if os.path.lexists(ap):
                    os.unlink(ap)
                res.deleted.append(e.path)
                self._rmdirs(ap.parent)
            except OSError as ex:
                res.skipped.append(e.path)
                res.skipped_reasons[e.path] = f"could not delete: {ex.strerror or ex}"
        # 2. writes
        writes = [x for x in todo if x.status != "A"]
        chmod_only = {x.path for x in writes if x.status != "D" and x.tsha == x.csha
                      and (x.tmode == "120000") == (x.cmode == "120000")}
        need = [x for x in writes if x.path not in chmod_only]
        blob_iter = self._stream_blobs([x.tsha for x in need])
        for e in writes:
            ap = self.root / e.path
            exec_bit = e.tmode == "100755"
            try:
                if e.path in chmod_only:
                    self._set_exec(ap, exec_bit)
                else:
                    sha, data = next(blob_iter)
                    self._ensure_parents(e.path)
                    self._write_atomic(ap, data, e.tmode)
                res.written.append(e.path)
                if e.status == "D":
                    res.created.append(e.path)
            except StopIteration:
                res.skipped.append(e.path)
                res.skipped_reasons[e.path] = "snapshot content unavailable"
            except (OSError, StoreError) as ex:
                res.skipped.append(e.path)
                res.skipped_reasons[e.path] = f"could not write: {getattr(ex, 'strerror', None) or ex}"
        blob_iter.close()

    def _ensure_parents(self, rel: str) -> None:
        cur = self.root
        for comp in rel.split("/")[:-1]:
            cur = cur / comp
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                os.mkdir(cur)
                continue
            if not statmod.S_ISDIR(st.st_mode) or statmod.S_ISLNK(st.st_mode):
                raise StoreError(f"{os.path.relpath(cur, self.root)} is in the way (not a plain directory)")

    def _rmdirs(self, d: Path) -> None:
        root = self.root
        while d != root and root in d.parents:
            try:
                os.rmdir(d)
            except OSError:
                return  # not empty (ignored files etc.) or gone
            d = d.parent

    @staticmethod
    def _set_exec(ap: Path, exec_bit: bool) -> None:
        if os.name != "posix":
            return
        st = os.stat(ap)
        mode = statmod.S_IMODE(st.st_mode)
        want = (mode | ((mode & 0o444) >> 2)) if exec_bit else (mode & ~0o111)
        if want != mode:
            os.chmod(ap, want)

    def _write_atomic(self, ap: Path, data: bytes, mode: str) -> None:
        if mode == "120000":
            tmp = ap.with_name(f"{TMP_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}")
            try:
                os.symlink(os.fsdecode(data), tmp)
                os.replace(tmp, ap)
            finally:
                if os.path.lexists(tmp):
                    os.unlink(tmp)
            return
        exec_bit = mode == "100755" and os.name == "posix"
        try:
            existing = os.lstat(ap)
            base_mode = statmod.S_IMODE(existing.st_mode) if statmod.S_ISREG(existing.st_mode) else None
        except OSError:
            base_mode = None
        if base_mode is None:
            base_mode = (0o666 & ~_UMASK)
        fd, tmp = tempfile.mkstemp(prefix=f"{TMP_PREFIX}{os.getpid()}-", dir=str(ap.parent))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
            if os.name == "posix":
                want = (base_mode | ((base_mode & 0o444) >> 2)) if exec_bit else (base_mode & ~0o111)
                os.chmod(tmp, want)
            os.replace(tmp, ap)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _stream_blobs(self, shas: list[str]) -> Iterator[tuple[str, bytes]]:
        return self._stream_gen(shas)

    def _stream_gen(self, shas: list[str]) -> Iterator[tuple[str, bytes]]:
        if not shas:
            return
        proc = popen_git(self.store_dir, self.root, ["cat-file", "--batch"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def feed() -> None:
            try:
                proc.stdin.write(("\n".join(shas) + "\n").encode())
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        t = threading.Thread(target=feed, daemon=True)
        t.start()
        try:
            for sha in shas:
                header = proc.stdout.readline().split()
                if len(header) != 3 or header[1] != b"blob":
                    raise StoreError(f"object {sha} missing from the store")
                size = int(header[2])
                data = proc.stdout.read(size)
                proc.stdout.read(1)
                yield sha, data
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait()
            t.join(timeout=2)

    # -- maintenance ---------------------------------------------------------------------------
    def stats(self) -> dict:
        snaps = self._log_raw()
        size = 0
        for dp, _dn, fn in os.walk(self.store_dir):
            for f in fn:
                try:
                    size += os.lstat(os.path.join(dp, f)).st_size
                except OSError:
                    pass
        return {
            "snapshots": len(snaps),
            "size_bytes": size,
            "oldest": snaps[-1].time if snaps else None,
            "newest": snaps[0].time if snaps else None,
            "root": str(self.root),
            "store": str(self.store_dir),
            "skipped_large": len(self._oversize),
        }

    def pack(self) -> None:
        """Pack loose objects (no history is removed)."""
        with self._locked():
            self._g("repack", "-d", "-q", check=False)

    def _plan_prune(self, snaps_old_to_new: list[Snapshot], policy: str, now: datetime) -> list[Snapshot]:
        tiers = _parse_retention(policy)
        keep: list[Snapshot] = []
        seen: set[tuple[int, int]] = set()
        newest_first = list(reversed(snaps_old_to_new))
        for idx, s in enumerate(newest_first):
            age = (now - s.time).total_seconds()
            if idx == 0 or age < MIN_PRUNE_AGE:
                keep.append(s)
                continue
            for ti, (max_age, interval) in enumerate(tiers):
                if age < max_age:
                    if interval is None:
                        keep.append(s)
                    else:
                        key = (ti, int(s.time.timestamp() // interval))
                        if key not in seen:
                            seen.add(key)
                            keep.append(s)
                    break
            # older than the last tier: dropped
        keep_ids = {s.id for s in keep}
        return [s for s in snaps_old_to_new if s.id in keep_ids]

    def prune(self, policy: str | None = None, dry_run: bool = False) -> int:
        policy = policy or str(self.config.get("retention") or DEFAULT_RETENTION)
        _parse_retention(policy)  # validate before doing anything
        with self._locked():
            self._clear_stale_git_locks()
            new_to_old = self._log_raw()
            if len(new_to_old) < 2:
                return 0
            snaps = list(reversed(new_to_old))  # oldest first
            kept = self._plan_prune(snaps, policy, _now())
            removed = len(snaps) - len(kept)
            if removed == 0 or dry_run:
                return removed
            old_head = snaps[-1].id
            # rewrite the chain: identical trees + metadata, new parents (stats recomputed)
            orig_parent = {snaps[i].id: (snaps[i - 1].id if i else None) for i in range(len(snaps))}
            rebuilt: dict[str, str] = {}
            new_parent: str | None = None
            untouched = True
            for i, s in enumerate(kept):
                prev_old = kept[i - 1].id if i else None
                if untouched and orig_parent[s.id] == prev_old:
                    rebuilt[s.id] = new_parent = s.id  # unchanged prefix: reuse the commit as is
                    continue
                untouched = False
                meta = {
                    "v": 1, "time": s.time.isoformat(timespec="milliseconds"), "trigger": s.trigger,
                    "label": s.label, "command": s.command,
                    "stats": s.stats if new_parent is None else self._count_changes(self._tree_of(new_parent), s.tree),
                }
                new = self._commit_tree(s.tree, new_parent, meta, s.time)
                rebuilt[s.id] = new_parent = new
            new_head = rebuilt[kept[-1].id]
            # verify before publishing the new chain
            check = self._g("rev-list", "--count", new_head).stdout.decode().strip()
            if int(check) != len(kept):
                raise StoreError("prune verification failed (chain length) - nothing was changed")
            for s in kept:
                if self._tree_of(rebuilt[s.id]) != s.tree:
                    raise StoreError("prune verification failed (tree mismatch) - nothing was changed")
            self._g("update-ref", REF, new_head, old_head)
            fsck = self._g("fsck", "--strict", "--no-dangling", check=False)
            if fsck.returncode != 0:
                self._g("update-ref", REF, old_head, new_head, check=False)
                raise StoreError("prune aborted: git fsck reported problems, history was left untouched: "
                                 + fsck.stderr.decode("utf-8", "replace").strip()[:300])
            self._g("gc", "--prune=now", "--quiet", check=False)
            return removed

    def _tree_of(self, commit: str) -> str:
        return self._g("rev-parse", f"{commit}^{{tree}}").stdout.decode().strip()


class _BlobReader:
    """Persistent ``git cat-file --batch`` with a size cap, for scanning history."""

    LIMIT = 16 * 1024 * 1024

    def __init__(self, store: Store):
        self.proc = popen_git(store.store_dir, store.root, ["cat-file", "--batch"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.cache: dict[str, bytes] = {}

    def get(self, sha: str) -> bytes:
        if sha in self.cache:
            return self.cache[sha]
        self.proc.stdin.write(sha.encode() + b"\n")
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().split()
        if len(header) != 3:
            return b""
        size = int(header[2])
        data = self.proc.stdout.read(size)
        self.proc.stdout.read(1)
        if size > self.LIMIT:
            data = b""
        if len(self.cache) > 64:
            self.cache.clear()
        self.cache[sha] = data
        return data

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.stdout.close()
        except OSError:
            pass
        try:
            self.proc.kill()
        except OSError:
            pass
        self.proc.wait()
