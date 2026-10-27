"""ctrlz command line implementation (imported lazily by ctrlz.cli so that agent hooks stay fast)."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__
from . import hooks as hooks_mod
from .store import FileChange, RestorePlan, Snapshot, Store, StoreError, parse_duration

out = Console(highlight=False)
err = Console(stderr=True, highlight=False)

C_ID, C_ADD, C_MOD, C_DEL, C_REN = "cyan", "green", "yellow", "red", "magenta"


def _fail(msg: str) -> None:
    err.print(Text(msg, style="red"), soft_wrap=True)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # usage errors are user errors: one red line, exit 1
        _fail(f"error: {message}")
        raise SystemExit(1)


# ----------------------------------------------------------------------------------------------
# formatting helpers
# ----------------------------------------------------------------------------------------------
def rel_time(dt: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    secs = (now - dt).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 5:
        return "just now"
    if secs < 60:
        return f"{int(secs)} sec ago"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        h = int(secs // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = int(secs // 86400)
    if d == 1:
        return "yesterday"
    if d < 30:
        return f"{d} days ago"
    return dt.strftime("%Y-%m-%d")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def counts_text(stats: dict) -> Text:
    t = Text()
    for sym, key, color in (("+", "added", C_ADD), ("~", "modified", C_MOD), ("-", "deleted", C_DEL)):
        n = stats.get(key, 0)
        t.append(f"{sym}{n}", style=color if n else "dim")
        t.append(" ")
    t.rstrip()
    return t


def what_text(s: Snapshot) -> Text:
    t = Text()
    if s.label:
        t.append(s.label)
        if s.trigger not in ("manual", "hook", "restore-before", "restore-after"):
            t.append(f" ({s.trigger})", style="dim")
    else:
        t.append(s.trigger, style="dim" if s.trigger == "watch" else "")
    if s.command and s.trigger in ("run-before", "run-after"):
        t.append(f" {s.command}", style="dim")
    return t


STATUS_STYLE = {"A": C_ADD, "M": C_MOD, "D": C_DEL, "R": C_REN}


def changes_text(changes: list[FileChange], limit: int = 3) -> Text:
    t = Text(no_wrap=True, overflow="ellipsis")
    for i, c in enumerate(changes[:limit]):
        if i:
            t.append(", ", style="dim")
        t.append(c.path, style=STATUS_STYLE.get(c.status, ""))
    if len(changes) > limit:
        t.append(f"  +{len(changes) - limit} more", style="dim")
    return t


def snap_json(s: Snapshot) -> dict:
    return {
        "id": s.id, "short": s.short, "time": s.time.isoformat(timespec="seconds"), "trigger": s.trigger,
        "label": s.label, "command": s.command, "stats": s.stats, "tree": s.tree,
    }


def print_diff(text: str) -> None:
    for line in text.splitlines():
        if line.startswith(("diff --git", "index ", "new file", "deleted file", "similarity", "rename ", "old mode", "new mode")):
            out.print(Text(line, style="bold"))
        elif line.startswith(("+++", "---")):
            out.print(Text(line, style="bold"))
        elif line.startswith("@@"):
            out.print(Text(line, style="cyan"))
        elif line.startswith("+"):
            out.print(Text(line, style=C_ADD))
        elif line.startswith("-"):
            out.print(Text(line, style=C_DEL))
        else:
            out.print(Text(line))


def ref_arg(value: str) -> str:
    return value[4:] if value.startswith("ref:") else value


_DURATION_REF = re.compile(r"^-\d+(\.\d+)?[smhdw]$")


def _protect_refs(argv: list[str]) -> list[str]:
    """argparse would read `-10m` as an option; mark such tokens so they survive as positionals."""
    if not argv:
        return argv
    cmd_idx = next((i for i, a in enumerate(argv) if not a.startswith("-")), None)
    if cmd_idx is None or argv[cmd_idx] not in ("show", "diff", "restore", "cat"):
        return argv
    res = list(argv)
    seen = 0
    for i in range(cmd_idx + 1, len(res)):
        if _DURATION_REF.match(res[i]) and res[i - 1] not in ("-m", "--message", "--label", "--policy") and seen < 2:
            res[i] = "ref:" + res[i]
        if not res[i].startswith("-") or res[i].startswith("ref:"):
            seen += 1
    return res


def need_store(create: bool = False) -> Store:
    return Store.open(Path.cwd(), create=create)


def cwd_paths(store: Store, paths: list[str]) -> list[str] | None:
    if not paths:
        return None
    return [store.relpath(p) for p in paths]


def warn_skipped(store: Store) -> None:
    if store.last_skipped:
        names = ", ".join(sorted(store.last_skipped)[:3])
        more = f" +{len(store.last_skipped) - 3} more" if len(store.last_skipped) > 3 else ""
        err.print(Text(f"note: skipped {len(store.last_skipped)} file(s) larger than {store.config.get('max_file_mb')} MB: {names}{more}",
                       style="yellow"))
    for w in store.last_warnings[:3]:
        err.print(Text(f"warning: {w}", style="yellow"))


# ----------------------------------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------------------------------
def cmd_init(a) -> int:
    existed = True
    try:
        store = Store.open(Path.cwd())
    except StoreError:
        existed = False
        store = Store.open(Path.cwd(), create=True)
    if existed:
        out.print(f"ctrlz is already set up here ({store.store_dir}).")
        return 0
    s = store.snap("manual", "initial snapshot")
    t = Text()
    t.append("ctrlz is watching over ", style="bold")
    t.append(str(store.root))
    out.print(t)
    if s:
        line = Text("  first snapshot ")
        line.append(s.short, style=C_ID)
        line.append(f"  {s.stats['added']} files")
        out.print(line)
    out.print(Text(f"  store: {store.store_dir}", style="dim"))
    warn_skipped(store)
    out.print()
    out.print("Next steps:")
    for cmd, desc in (
        ("ctrlz start", "snapshot automatically in the background"),
        ("ctrlz hooks install claude-code", "also snapshot after every Claude Code edit"),
        ("ctrlz log", "see your timeline"),
        ("ctrlz restore @~1", "rewind to the previous snapshot"),
    ):
        row = Text("  ")
        row.append(f"{cmd:<34}", style="bold")
        row.append(desc, style="dim")
        out.print(row)
    return 0


def cmd_snap(a) -> int:
    if a.hook:  # normally intercepted earlier by ctrlz.cli (so hooks never import rich)
        return hooks_mod.hook_main(["snap", "--hook"] + (["-m", a.message] if a.message else []))
    store = need_store()
    s = store.snap(a.trigger, a.message)
    if a.json:
        out.print_json(json.dumps(snap_json(s) if s else None))
        return 0
    if not a.quiet:
        if s:
            line = Text("snapshot ")
            line.append(s.short, style=C_ID)
            line.append("  ")
            line.append_text(counts_text(s.stats))
            out.print(line)
        else:
            head = store.latest()
            out.print(f"no changes since {head.short} ({rel_time(head.time)})" if head else "nothing to snapshot")
        warn_skipped(store)
    return 0


def cmd_watch(a) -> int:
    from .watch import watch

    store = need_store()
    quiet = a.quiet

    def on_snap(s: Snapshot) -> None:
        if not quiet:
            line = Text(datetime.now().strftime("%H:%M:%S "), style="dim")
            line.append(s.short, style=C_ID)
            line.append("  ")
            line.append_text(counts_text(s.stats))
            out.print(line)

    def on_error(e: BaseException) -> None:
        err.print(Text(f"warning: {e}", style="yellow"))

    def ready(mode: str) -> None:
        if not quiet:
            out.print(Text(f"watching {store.root}  [{mode}] - Ctrl+C to stop", style="bold"))

    try:
        watch(store, debounce_ms=a.debounce, on_snap=on_snap, on_error=on_error, on_ready=ready)
    except KeyboardInterrupt:
        pass
    if not quiet:
        out.print(Text("stopped", style="dim"))
    return 0


def cmd_daemon(a) -> int:
    from .watch import run_daemon

    return run_daemon(Store.open(Path(a.root)))


def cmd_start(a) -> int:
    from .watch import start_daemon

    store = need_store(create=False)
    st = start_daemon(store)
    if st.get("already"):
        out.print(f"watcher already running (pid {st['pid']})")
    else:
        out.print(f"watcher started (pid {st['pid']}) - log: {st['log']}")
    return 0


def cmd_stop(a) -> int:
    from .watch import stop_daemon

    store = need_store()
    out.print("watcher stopped" if stop_daemon(store) else "watcher is not running")
    return 0


def cmd_status(a) -> int:
    from .watch import daemon_status

    store = need_store()
    st = daemon_status(store)
    head = store.latest()
    if a.json:
        out.print_json(json.dumps({"root": str(store.root), "store": str(store.store_dir), "watcher": st,
                                   "latest": snap_json(head) if head else None}))
        return 0
    t = Table.grid(padding=(0, 2))
    t.add_row(Text("project", style="dim"), str(store.root))
    t.add_row(Text("store", style="dim"), str(store.store_dir))
    t.add_row(Text("watcher", style="dim"),
              Text(f"running (pid {st['pid']})", style=C_ADD) if st["running"] else Text("not running - start with `ctrlz start`", style="yellow"))
    t.add_row(Text("latest", style="dim"), f"{head.short}  {rel_time(head.time)}" if head else "no snapshots yet")
    out.print(t)
    return 0


def cmd_log(a) -> int:
    store = need_store()
    since = None
    if a.since:
        since = datetime.now().astimezone() - __import__("datetime").timedelta(seconds=parse_duration(a.since))
    paths = list(a.paths) + list(a.after)
    path = store.relpath(paths[0]) if paths else None
    snaps = store.log(limit=a.n, since=since, path=path)
    if a.json:
        rows = []
        for s in snaps:
            d = snap_json(s)
            d["changes"] = [{"path": c.path, "status": c.status, "old_path": c.old_path} for c in store.changes(s)]
            rows.append(d)
        out.print_json(json.dumps(rows))
        return 0
    if not snaps:
        out.print("No snapshots yet. Run `ctrlz snap` (or `ctrlz start`)." if not (since or path) else "No matching snapshots.")
        return 0
    now = datetime.now().astimezone()
    rows = [(Text(s.short, style=C_ID), Text(rel_time(s.time, now), style="dim"), what_text(s), counts_text(s.stats),
             changes_text(store.changes(s))) for s in snaps]
    w_id, w_when = 7, max(len(r[1].plain) for r in rows)
    w_what = min(30, max(len(r[2].plain) for r in rows))
    w_cnt = max(len(r[3].plain) for r in rows)
    header = Text("snapshot".ljust(w_id) + "  " + "when".ljust(w_when) + "  " + "what".ljust(w_what) + "  "
                  + "changes".ljust(w_cnt) + "  files", style="dim")
    out.print(header, no_wrap=True, overflow="ellipsis")

    def cell(t: Text, width: int) -> Text:
        t = t.copy()
        t.truncate(width, overflow="ellipsis", pad=True)
        return t

    for r in rows:
        line = Text(no_wrap=True, overflow="ellipsis")
        for t, wd in zip(r[:4], (w_id, w_when, w_what, w_cnt)):
            line.append_text(cell(t, wd))
            line.append("  ")
        line.append_text(r[4])
        line.truncate(max(out.width, 20), overflow="ellipsis")
        out.print(line, no_wrap=True, overflow="ellipsis", soft_wrap=False)
    return 0


def cmd_show(a) -> int:
    store = need_store()
    s = store.get(a.ref)
    ch = store.changes(s)
    if a.json:
        d = snap_json(s)
        d["changes"] = [{"path": c.path, "status": c.status, "old_path": c.old_path} for c in ch]
        out.print_json(json.dumps(d))
        return 0
    t = Table.grid(padding=(0, 2))
    t.add_row(Text("snapshot", style="dim"), Text(s.id, style=C_ID))
    t.add_row(Text("time", style="dim"), f"{s.time:%Y-%m-%d %H:%M:%S}  ({rel_time(s.time)})")
    t.add_row(Text("trigger", style="dim"), s.trigger)
    if s.label:
        t.add_row(Text("label", style="dim"), s.label)
    if s.command:
        t.add_row(Text("command", style="dim"), s.command)
    t.add_row(Text("changes", style="dim"), counts_text(s.stats))
    out.print(t)
    out.print()
    for c in ch[:200]:
        line = Text()
        line.append(f"{c.status} ", style=STATUS_STYLE.get(c.status, ""))
        line.append(f"{c.old_path} -> {c.path}" if c.old_path else c.path)
        out.print(line)
    if len(ch) > 200:
        out.print(Text(f"... and {len(ch) - 200} more", style="dim"))
    if not ch:
        out.print(Text("(no file changes)", style="dim"))
    return 0


def cmd_diff(a) -> int:
    store = need_store()
    refs = list(a.refs)
    if len(refs) > 2:
        raise StoreError("diff takes at most two snapshots")
    paths = cwd_paths(store, list(a.after))
    ref_a = refs[0] if refs else "@"
    ref_b = refs[1] if len(refs) > 1 else None
    text = store.diff(ref_a, ref_b, paths)
    if not text.strip():
        out.print(Text("no differences", style="dim"))
        return 0
    print_diff(text)
    return 0


def _plan_table(plan: RestorePlan) -> None:
    head = Text("Restore to ", style="bold")
    head.append(plan.target.short, style=C_ID)
    head.append(f"  ({rel_time(plan.target.time)}{', ' + plan.target.label if plan.target.label else ''})", style="dim")
    out.print(head)
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(no_wrap=True)
    table.add_column(overflow="fold")
    rows: list[tuple[str, str, str]] = []
    rows += [("will delete", C_DEL, p) for p in plan.delete]
    rows += [("will overwrite", C_MOD, p) for p in plan.overwrite]
    rows += [("will create", C_ADD, p) for p in plan.create]
    rows += [("mode change", "dim", p) for p in plan.chmod]
    shown = rows[:40]
    for label, color, p in shown:
        table.add_row(Text(label, style=color), Text(p))
    out.print(table)
    if len(rows) > len(shown):
        out.print(Text(f"... and {len(rows) - len(shown)} more", style="dim"))
    summary = Text()
    summary.append(f"{len(plan.delete)} delete", style=C_DEL)
    summary.append(", ")
    summary.append(f"{len(plan.overwrite)} overwrite", style=C_MOD)
    summary.append(", ")
    summary.append(f"{len(plan.create)} create", style=C_ADD)
    out.print(summary)
    for p in plan.skipped[:10]:
        out.print(Text(f"  skip {p}: {plan.skipped_reasons.get(p, '')}", style="yellow"))


def _confirm(prompt: str) -> bool:
    try:
        ans = out.input(Text(prompt + " [y/N] ", style="bold"))
    except (EOFError, KeyboardInterrupt):
        out.print()
        return False
    return ans.strip().lower() in ("y", "yes")


def _report_restore(res, store: Store) -> None:
    line = Text()
    line.append("done: ", style="bold")
    line.append(f"{len(res.written)} written", style=C_ADD)
    line.append(", ")
    line.append(f"{len(res.deleted)} deleted", style=C_DEL)
    if res.skipped:
        line.append(f", {len(res.skipped)} skipped", style="yellow")
    out.print(line)
    for p in res.skipped[:10]:
        out.print(Text(f"  skipped {p}: {res.skipped_reasons.get(p, '')}", style="yellow"))
    hint = Text("Changed your mind? ", style="dim")
    hint.append("ctrlz undo", style="bold")
    if res.undo_ref:
        hint.append(f"   (or: ctrlz restore {res.undo_ref})", style="dim")
    out.print(hint)


def cmd_restore(a) -> int:
    store = need_store()
    paths = cwd_paths(store, list(a.paths) + list(a.after))
    plan = store.plan_restore(a.ref, paths, delete_extra=not a.keep_new)
    if plan.empty:
        out.print(f"Nothing to do - the project already matches {plan.target.short}.")
        for p in plan.skipped[:10]:
            out.print(Text(f"  skip {p}: {plan.skipped_reasons.get(p, '')}", style="yellow"))
        return 0
    _plan_table(plan)
    if not a.yes and not _confirm("Proceed?"):
        out.print("Aborted - nothing was changed.")
        return 1
    res = store.restore(plan.target.id, paths, delete_extra=not a.keep_new)
    _report_restore(res, store)
    return 0


def cmd_undo(a) -> int:
    store = need_store()
    plan = store.plan_undo()
    if plan.empty:
        out.print("Nothing to undo.")
        return 0
    _plan_table(plan)
    if not a.yes and not _confirm("Undo the most recent change?"):
        out.print("Aborted - nothing was changed.")
        return 1
    res = store.undo()
    _report_restore(res, store)
    return 0


def cmd_cat(a) -> int:
    store = need_store()
    data = store.read(a.ref, store.relpath(a.path))
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


def cmd_find(a) -> int:
    store = need_store()
    paths = list(a.paths) + list(a.after)
    path = store.relpath(paths[0]) if paths else None
    hits = store.grep_history(a.text, path=path, regex=a.regex)
    if a.json:
        out.print_json(json.dumps([{"snapshot": snap_json(s), "path": p, "change": k} for s, p, k in hits]))
        return 0
    if not hits:
        out.print(f"No snapshot ever added or removed {a.text!r}.")
        return 0
    table = Table(box=None, pad_edge=False, show_header=True, header_style="dim")
    for col in ("snapshot", "when", "change"):
        table.add_column(col, no_wrap=True)
    table.add_column("file", overflow="fold")
    now = datetime.now().astimezone()
    for s, p, kind in hits[:100]:
        table.add_row(Text(s.short, style=C_ID), Text(rel_time(s.time, now), style="dim"),
                      Text(kind, style=C_ADD if kind == "added" else C_DEL), Text(p))
    out.print(table)
    if len(hits) > 100:
        out.print(Text(f"... and {len(hits) - 100} more", style="dim"))
    return 0


def cmd_run(a) -> int:
    cmd = list(a.after) if a.after else [c for c in a.cmd if c != "--"]
    if not cmd:
        raise StoreError("usage: ctrlz run -- COMMAND [ARGS...]")
    try:
        store = Store.open(Path.cwd())
    except StoreError:
        store = Store.open(Path.cwd(), create=True)
        err.print(Text(f"ctrlz: set up a new store at {store.store_dir}", style="dim"))
    shown = shlex.join(cmd) if hasattr(shlex, "join") else " ".join(cmd)
    try:
        store.snap("run-before", None, shown)
    except StoreError as e:
        err.print(Text(f"ctrlz: warning - could not snapshot before the run: {e}", style="yellow"))
    rc = 0
    proc = None
    old: dict = {}
    try:
        try:
            proc = subprocess.Popen(cmd)
        except FileNotFoundError:
            _fail(f"error: command not found: {cmd[0]}")
            rc = 127
        except PermissionError:
            _fail(f"error: permission denied: {cmd[0]}")
            rc = 126
        if proc is not None:
            tty = False
            try:
                tty = os.isatty(0)
            except OSError:
                pass

            def forward(signum, frame):  # noqa: ARG001
                try:
                    proc.send_signal(signum)
                except (OSError, ValueError):
                    pass

            for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT", "SIGBREAK"):
                sig = getattr(signal, name, None)
                if sig is None:
                    continue
                try:
                    if name == "SIGINT" and tty:
                        # the terminal already delivers Ctrl+C to the child; just don't die ourselves
                        old[sig] = signal.signal(sig, signal.SIG_IGN)
                    else:
                        old[sig] = signal.signal(sig, forward)
                except (OSError, ValueError):
                    pass
            while True:
                try:
                    rc = proc.wait()
                    break
                except KeyboardInterrupt:
                    continue
            if rc < 0:
                rc = 128 - rc if os.name == "posix" else 1
    finally:
        for sig, h in old.items():
            try:
                signal.signal(sig, h)
            except (OSError, ValueError):
                pass
        try:
            after = store.snap("run-after", None, shown)
            if after and not a.quiet:
                line = Text("ctrlz: the run changed files ", style="dim")
                line.append_text(counts_text(after.stats))
                line.append(" - rewind with `ctrlz undo`", style="dim")
                err.print(line)
        except StoreError as e:
            err.print(Text(f"ctrlz: warning - could not snapshot after the run: {e}", style="yellow"))
    return rc


def _hooks_target(a) -> Path:
    if a.global_:
        return hooks_mod.settings_path(Path.home(), True)
    try:
        root = Store.open(Path.cwd()).root
    except StoreError:
        root = Path.cwd()
        for d in (root, *root.parents):
            if (d / ".git").exists() or (d / ".claude").is_dir():
                root = d
                break
    return hooks_mod.settings_path(root, False)


def cmd_hooks(a) -> int:
    path = _hooks_target(a)
    if a.action == "install":
        changed = hooks_mod.install(path)
        out.print(f"{'installed hook in' if changed else 'hook already installed in'} {path}")
        if changed or True:
            if shutil.which("ctrlz") is None:
                err.print(Text("warning: `ctrlz` is not on PATH, so Claude Code will not be able to run the hook", style="yellow"))
        if not a.global_:
            out.print(Text("Claude Code snapshots this project after every Edit/Write/Bash tool call.", style="dim"))
    else:
        changed = hooks_mod.uninstall(path)
        out.print(f"removed ctrlz hook from {path}" if changed else f"no ctrlz hook found in {path}")
    return 0


def cmd_prune(a) -> int:
    store = need_store()
    before = store.stats()
    n = store.prune(a.policy, dry_run=a.dry_run)
    if a.dry_run:
        out.print(f"would remove {n} of {before['snapshots']} snapshots")
        return 0
    after = store.stats()
    out.print(f"removed {n} snapshot(s); {after['snapshots']} left; store {human_size(before['size_bytes'])} -> {human_size(after['size_bytes'])}")
    return 0


def cmd_stats(a) -> int:
    store = need_store()
    st = store.stats()
    if a.json:
        d = dict(st)
        d["oldest"] = st["oldest"].isoformat(timespec="seconds") if st["oldest"] else None
        d["newest"] = st["newest"].isoformat(timespec="seconds") if st["newest"] else None
        out.print_json(json.dumps(d))
        return 0
    t = Table.grid(padding=(0, 2))
    t.add_row(Text("project", style="dim"), st["root"])
    t.add_row(Text("snapshots", style="dim"), str(st["snapshots"]))
    t.add_row(Text("store size", style="dim"), human_size(st["size_bytes"]))
    if st["oldest"]:
        t.add_row(Text("oldest", style="dim"), f"{st['oldest']:%Y-%m-%d %H:%M}  ({rel_time(st['oldest'])})")
        t.add_row(Text("newest", style="dim"), f"{st['newest']:%Y-%m-%d %H:%M}  ({rel_time(st['newest'])})")
    if st["skipped_large"]:
        t.add_row(Text("skipped", style="dim"), f"{st['skipped_large']} file(s) over {store.config.get('max_file_mb')} MB")
    out.print(t)
    return 0


def cmd_ui(a) -> int:
    try:
        from ctrlz.ui.app import run
    except ImportError:
        print("Install the TUI with: pip install 'ctrlz[ui]'", file=sys.stderr)
        return 1
    run(need_store())
    return 0


# ----------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="ctrlz", description="Undo anything. ctrlz snapshots your project so you can rewind what an AI agent (or you) broke.")
    p.add_argument("--version", action="version", version=f"ctrlz {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)

    def add(name, func, help_, **kw):
        sp = sub.add_parser(name, help=help_, description=help_, **kw)
        sp.set_defaults(func=func)
        return sp

    add("init", cmd_init, "create the store and take the first snapshot")
    sp = add("snap", cmd_snap, "take a snapshot now")
    sp.add_argument("-m", "--message", "--label", dest="message", help="label for the snapshot")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--hook", action="store_true", help="agent hook mode: read the hook JSON on stdin, never fail")
    sp.add_argument("--trigger", default="manual", help=argparse.SUPPRESS)
    sp = add("watch", cmd_watch, "watch in the foreground (Ctrl+C to stop)")
    sp.add_argument("--debounce", type=int, default=None, metavar="MS")
    sp.add_argument("-q", "--quiet", action="store_true")
    add("start", cmd_start, "start the background watcher")
    add("stop", cmd_stop, "stop the background watcher")
    sp = add("status", cmd_status, "show store and watcher status")
    sp.add_argument("--json", action="store_true")
    sp = add("log", cmd_log, "show the snapshot timeline")
    sp.add_argument("-n", type=int, default=20, help="number of snapshots (default 20)")
    sp.add_argument("--since", metavar="AGE", help="only snapshots newer than e.g. 2h, 3d")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("paths", nargs="*", help="only snapshots that touched PATH (also accepted after `--`)")
    sp = add("show", cmd_show, "show one snapshot")
    sp.add_argument("ref", type=ref_arg)
    sp.add_argument("--json", action="store_true")
    sp = add("diff", cmd_diff, "diff snapshots (default: latest vs working tree)")
    sp.add_argument("refs", nargs="*", type=ref_arg)
    sp = add("restore", cmd_restore, "restore files (or everything) from a snapshot")
    sp.add_argument("ref", type=ref_arg)
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--keep-new", action="store_true", help="do not delete files that did not exist in the snapshot")
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    sp = add("undo", cmd_undo, "revert the most recent change")
    sp.add_argument("-y", "--yes", action="store_true")
    sp = add("cat", cmd_cat, "print a file as it was in a snapshot")
    sp.add_argument("ref", type=ref_arg)
    sp.add_argument("path")
    sp = add("find", cmd_find, "when did TEXT appear or disappear?")
    sp.add_argument("text")
    sp.add_argument("--regex", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("paths", nargs="*")
    sp = add("run", cmd_run, "snapshot before and after running a command: ctrlz run -- claude")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp.add_argument("cmd", nargs=argparse.REMAINDER)
    sp = add("hooks", cmd_hooks, "install/uninstall agent hooks")
    sp.add_argument("action", choices=["install", "uninstall"])
    sp.add_argument("agent", choices=["claude-code"])
    sp.add_argument("--global", dest="global_", action="store_true", help="use ~/.claude/settings.json")
    sp = add("prune", cmd_prune, "thin out old snapshots according to the retention policy")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--policy", help="e.g. 1h=all,1d=10m,7d=1h,90d=1d")
    sp = add("stats", cmd_stats, "store statistics")
    sp.add_argument("--json", action="store_true")
    add("ui", cmd_ui, "open the terminal UI (pip install 'ctrlz[ui]')")
    return p


def types_ns(**kw):
    return argparse.Namespace(**kw)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "_daemon":  # internal: the detached watcher process
        try:
            return cmd_daemon(types_ns(root=argv[1]))
        except StoreError as e:
            _fail(f"error: {e}")
            return 1
    after: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, after = argv[:i], argv[i + 1:]
    parser = build_parser()
    args = parser.parse_args(_protect_refs(argv))
    args.after = after
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except StoreError as e:
        _fail(f"error: {e}")
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except Exception as e:  # never show a traceback for something a user can hit
        if os.environ.get("CTRLZ_DEBUG"):
            raise
        _fail(f"error: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
