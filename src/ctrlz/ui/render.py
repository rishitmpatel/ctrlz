"""Pure rendering helpers for the ctrlz TUI (no widgets, no Store access).

Everything here turns plain data (snapshots, diffs) into Rich renderables, so it
can be unit-tested without running a Textual app.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from rich.cells import cell_len
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual.color import Color

# --------------------------------------------------------------------------- palette


def _blend(base: str, over: str, factor: float) -> str:
    return Color.parse(base).blend(Color.parse(over), factor).hex


@dataclass(frozen=True)
class Palette:
    """Colours derived from the active Textual theme."""

    dark: bool
    fg: str
    muted: str
    faint: str
    bg: str
    primary: str
    accent: str
    add: str
    mod: str
    rem: str
    add_bg: str
    rem_bg: str
    add_hi: str
    rem_hi: str
    add_gutter: str
    rem_gutter: str
    hunk: str
    syntax_theme: str

    @classmethod
    def from_vars(cls, v: dict[str, str], dark: bool) -> "Palette":
        fg = Color.parse(v["foreground"]).hex
        bg = Color.parse(v["surface"]).hex
        primary = Color.parse(v["primary"]).hex
        accent = Color.parse(v["accent"]).hex
        success = Color.parse(v["success"]).hex
        error = Color.parse(v["error"]).hex
        warning = Color.parse(v["warning"]).hex
        if not dark:
            # saturated mid-tones are hard to read on light backgrounds
            success = _blend(success, "#000000", 0.38)
            error = _blend(error, "#000000", 0.12)
            warning = _blend(warning, "#000000", 0.42)
            primary = _blend(primary, "#000000", 0.0)
        return cls(
            dark=dark,
            fg=fg,
            muted=_blend(bg, fg, 0.70),
            faint=_blend(bg, fg, 0.38),
            bg=bg,
            primary=primary,
            accent=accent,
            add=success,
            mod=warning,
            rem=error,
            add_bg=_blend(bg, success, 0.15 if dark else 0.20),
            rem_bg=_blend(bg, error, 0.15 if dark else 0.17),
            add_hi=_blend(bg, success, 0.38 if dark else 0.42),
            rem_hi=_blend(bg, error, 0.38 if dark else 0.38),
            add_gutter=_blend(bg, success, 0.25 if dark else 0.30),
            rem_gutter=_blend(bg, error, 0.25 if dark else 0.27),
            hunk=_blend(bg, primary, 0.14),
            syntax_theme="github-dark" if dark else "friendly",
        )


# --------------------------------------------------------------------------- formatting

# trigger -> (glyph, palette attribute, short name)
TRIGGERS: dict[str, tuple[str, str, str]] = {
    "watch": ("●", "muted", "auto"),
    "manual": ("✎", "primary", "manual"),
    "run-before": ("▶", "mod", "before run"),
    "run-after": ("▶", "add", "after run"),
    "restore-before": ("↺", "accent", "before restore"),
    "hook": ("⚡", "accent", "hook"),
}


def human_size(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"  # pragma: no cover


def rel_time(dt: datetime, now: datetime | None = None) -> str:
    """'12s ago', '4m ago', '3h ago', '2d ago', then a calendar date."""
    if now is None:
        now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    secs = int((now - dt).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 86400 * 14:
        return f"{secs // 86400}d ago"
    return dt.astimezone().strftime("%b %d")


def describe(snap: Any) -> tuple[str, str, str]:
    """(glyph, palette-attr, human text) for a snapshot row."""
    glyph, attr, name = TRIGGERS.get(snap.trigger, ("●", "muted", snap.trigger or "snapshot"))
    label = snap.label
    command = snap.command
    if snap.trigger in ("run-before", "run-after"):
        prefix = "before" if snap.trigger == "run-before" else "after"
        what = label or command or "run"
        return glyph, attr, f"{prefix}: {what}"
    if snap.trigger == "restore-before":
        what = label or "state before restore"
        return glyph, attr, what
    text = label or command
    if not text:
        text = name
    return glyph, attr, text


def _counts_text(stats: dict[str, int], pal: Palette, bold: bool = False) -> Text:
    t = Text(no_wrap=True)
    a, m, d = stats.get("added", 0), stats.get("modified", 0), stats.get("deleted", 0)
    b = " bold" if bold else ""
    parts: list[tuple[str, str]] = []
    if a:
        parts.append((f"+{a}", pal.add))
    if m:
        parts.append((f"~{m}", pal.mod))
    if d:
        parts.append((f"-{d}", pal.rem))
    for i, (s, c) in enumerate(parts):
        if i:
            t.append(" ")
        t.append(s, style=f"{c}{b}")
    return t


def snapshot_row(snap: Any, pal: Palette, now: datetime | None = None) -> Table:
    """One timeline row: glyph, relative time, label, +A ~M -D."""
    glyph, attr, text = describe(snap)
    color = getattr(pal, attr)
    tbl = Table.grid(expand=True, padding=(0, 1, 0, 0))
    tbl.add_column(width=1, no_wrap=True)
    tbl.add_column(width=7, no_wrap=True, justify="right")
    tbl.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    tbl.add_column(no_wrap=True, justify="right")
    label = Text(text, no_wrap=True, overflow="ellipsis")
    if snap.trigger in ("watch",) and not (snap.label or snap.command):
        label.stylize(pal.muted)
    tbl.add_row(
        Text(glyph, style=color),
        Text(rel_time(snap.time, now), style=pal.muted, no_wrap=True),
        label,
        _counts_text(snap.stats, pal),
    )
    return tbl


STATUS_STYLE = {"A": "add", "M": "mod", "D": "rem", "R": "accent"}


def change_row(ch: Any, pal: Palette) -> Table:
    """One 'Changes' row: status letter, dim directory + bold file name."""
    color = getattr(pal, STATUS_STYLE.get(ch.status, "muted"))
    tbl = Table.grid(expand=True, padding=(0, 1, 0, 0))
    tbl.add_column(width=1, no_wrap=True)
    tbl.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    name = Text(no_wrap=True, overflow="ellipsis")
    path = ch.path
    if ch.status == "R" and ch.old_path:
        name.append(ch.old_path, style=pal.muted)
        name.append(" → ", style=pal.faint)
    d, sep, base = path.rpartition("/")
    if sep:
        name.append(d + "/", style=pal.muted)
    name.append(base, style="bold" if ch.status != "D" else f"{pal.rem} strike")
    tbl.add_row(Text(ch.status, style=f"bold {color}"), name)
    return tbl


# --------------------------------------------------------------------------- diff model

MAX_DIFF_LINES = 2000
MAX_DIFF_CHARS = 1_500_000
MAX_LINE_CHARS = 1200

_HUNK_RE = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@+ ?(.*)$")
_BINARY_RE = re.compile(r"^(Binary files .* differ|GIT binary patch)", re.M)


@dataclass
class DLine:
    kind: str  # meta | hunk | ctx | add | del | note
    old: int | None = None
    new: int | None = None
    code: Text | None = None  # highlighted content (no sign)
    raw: str = ""  # raw text for meta/hunk/note
    marks: list[tuple[int, int]] = field(default_factory=list)  # intra-line changed ranges


@dataclass
class DiffDoc:
    lines: list[DLine] = field(default_factory=list)
    n_add: int = 0
    n_del: int = 0
    notice: str | None = None  # e.g. truncation message
    binary: bool = False


def is_binary_diff(text: str) -> bool:
    return bool(_BINARY_RE.search(text))


def _lexer_for(path: str, sample: str) -> str:
    try:
        name = Syntax.guess_lexer(path, sample)
    except Exception:
        return "text"
    return name or "text"


def _highlight(lines: list[str], lexer: str, theme: str) -> list[Text]:
    """Highlight a block of source lines, returning one Text per line (no bg colours)."""
    if not lines:
        return []
    plain = [Text(l.replace("\t", "    ")[:MAX_LINE_CHARS], no_wrap=True) for l in lines]
    if lexer in ("text", "") or sum(len(l) for l in lines) > 400_000:
        return plain
    code = "\n".join(l.replace("\t", "    ")[:MAX_LINE_CHARS] for l in lines)
    try:
        syn = Syntax(code, lexer, theme=theme, background_color="default")
        rendered = syn.highlight(code)
        rendered.no_wrap = True
        parts = list(rendered.split("\n", allow_blank=True))
    except Exception:
        return plain
    if len(parts) != len(lines):
        # pygments may append a trailing newline
        parts = parts[: len(lines)]
        if len(parts) != len(lines):
            return plain
    out: list[Text] = []
    for p in parts:
        # drop background colours so our diff tints show through
        p.spans = [
            type(s)(
                s.start,
                s.end,
                Style(
                    color=s.style.color if isinstance(s.style, Style) else None,
                    bold=s.style.bold if isinstance(s.style, Style) else None,
                    italic=s.style.italic if isinstance(s.style, Style) else None,
                    underline=s.style.underline if isinstance(s.style, Style) else None,
                ),
            )
            for s in p.spans
            if isinstance(s.style, Style)
        ]
        p.style = ""
        p.no_wrap = True
        out.append(p)
    return out


def _pair_marks(old: str, new: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    if len(old) > 400 or len(new) > 400:
        return [], []
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    if sm.ratio() < 0.45:
        return [], []
    om: list[tuple[int, int]] = []
    nm: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete") and i2 > i1:
            om.append((i1, i2))
        if tag in ("replace", "insert") and j2 > j1:
            nm.append((j1, j2))
    return om, nm


def build_diff_doc(
    diff_text: str,
    path_hint: str = "",
    pal: Palette | None = None,
    max_lines: int = MAX_DIFF_LINES,
) -> DiffDoc:
    """Parse a unified diff into highlighted lines. Tolerates git-style and plain headers."""
    theme = pal.syntax_theme if pal else "github-dark"
    doc = DiffDoc()
    truncated_chars = False
    if len(diff_text) > MAX_DIFF_CHARS:
        diff_text = diff_text[:MAX_DIFF_CHARS]
        truncated_chars = True
    raw_lines = diff_text.splitlines()
    total = len(raw_lines)
    i = 0
    cur_path = path_hint
    emitted = 0
    # buffered hunk
    hunk: list[tuple[str, str]] = []  # (kind, code)
    hunk_nums: list[tuple[int | None, int | None]] = []
    hunk_header: DLine | None = None

    def flush() -> None:
        nonlocal hunk, hunk_nums, emitted
        if not hunk:
            return
        lexer = _lexer_for(cur_path, "\n".join(c for k, c in hunk[:40]))
        old_src = [c for k, c in hunk if k in ("ctx", "del")]
        new_src = [c for k, c in hunk if k in ("ctx", "add")]
        old_hl = iter(_highlight(old_src, lexer, theme))
        new_hl = iter(_highlight(new_src, lexer, theme))
        built: list[DLine] = []
        for (k, c), (o, n) in zip(hunk, hunk_nums):
            if k == "ctx":
                _o = next(old_hl, None)
                _n = next(new_hl, None)
                built.append(DLine("ctx", o, n, _n or _o or Text(c)))
            elif k == "del":
                built.append(DLine("del", o, None, next(old_hl, None) or Text(c)))
                doc.n_del += 1
            elif k == "add":
                built.append(DLine("add", None, n, next(new_hl, None) or Text(c)))
                doc.n_add += 1
            else:
                built.append(DLine("note", raw=c))
        # intra-line change marks for -/+ runs
        j = 0
        while j < len(built):
            if built[j].kind == "del":
                k2 = j
                while k2 < len(built) and built[k2].kind == "del":
                    k2 += 1
                k3 = k2
                while k3 < len(built) and built[k3].kind == "add":
                    k3 += 1
                dels, adds = built[j:k2], built[k2:k3]
                for dl, al in zip(dels, adds):
                    dm, am = _pair_marks(dl.code.plain if dl.code else "", al.code.plain if al.code else "")
                    dl.marks, al.marks = dm, am
                j = max(k3, j + 1)
            else:
                j += 1
        doc.lines.extend(built)
        emitted += len(built)
        hunk, hunk_nums = [], []

    while i < total:
        if emitted >= max_lines:
            break
        line = raw_lines[i]
        m = _HUNK_RE.match(line)
        if m:
            flush()
            o_start = int(m.group(1))
            o_left = int(m.group(2)) if m.group(2) is not None else 1
            n_start = int(m.group(3))
            n_left = int(m.group(4)) if m.group(4) is not None else 1
            ctx = m.group(5)
            doc.lines.append(DLine("hunk", raw=f"{line.split('@@')[1].strip()}" + (f"  {ctx}" if ctx else "")))
            emitted += 1
            o, n = o_start, n_start
            i += 1
            while i < total and (o_left > 0 or n_left > 0 or (i < total and raw_lines[i].startswith("\\"))):
                if emitted + len(hunk) >= max_lines:
                    break
                hl = raw_lines[i]
                if hl.startswith("\\"):
                    hunk.append(("note", hl.lstrip("\\ ").strip()))
                    hunk_nums.append((None, None))
                    i += 1
                    continue
                tag, body = (hl[:1], hl[1:]) if hl else (" ", "")
                if tag == "+":
                    hunk.append(("add", body))
                    hunk_nums.append((None, n))
                    n += 1
                    n_left -= 1
                elif tag == "-":
                    hunk.append(("del", body))
                    hunk_nums.append((o, None))
                    o += 1
                    o_left -= 1
                elif tag == " " or hl == "":
                    hunk.append(("ctx", body))
                    hunk_nums.append((o, n))
                    o += 1
                    n += 1
                    o_left -= 1
                    n_left -= 1
                else:
                    break  # malformed: let the outer loop handle it
                i += 1
            flush()
            continue
        # headers
        if line.startswith("diff --git "):
            flush()
            mm = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if mm:
                cur_path = mm.group(2)
            i += 1
            continue
        if line.startswith("+++ "):
            p = line[4:].split("\t")[0].strip()
            if p != "/dev/null":
                cur_path = p[2:] if p.startswith("b/") else p
            i += 1
            continue
        if line.startswith(("--- ", "index ", "similarity index")):
            i += 1
            continue
        if line.startswith(("new file mode", "deleted file mode", "old mode", "new mode", "rename ", "copy ")):
            doc.lines.append(DLine("meta", raw=line))
            emitted += 1
            i += 1
            continue
        if _BINARY_RE.match(line):
            doc.binary = True
            i += 1
            continue
        i += 1  # unknown line: ignore
    flush()
    consumed = i
    if consumed < total or truncated_chars:
        shown = emitted
        doc.notice = (
            f"diff truncated: showing the first {shown:,} lines"
            + (f" of {total:,}" if not truncated_chars else "")
            + " — run `ctrlz diff` for the full output"
        )
    return doc


def layout_diff(doc: DiffDoc, pal: Palette, width: int) -> Text:
    """Lay a DiffDoc out as one Text, padded so tinted lines span the full width."""
    nums = [l.old for l in doc.lines if l.old] + [l.new for l in doc.lines if l.new]
    nw = max(3, len(str(max(nums))) if nums else 3)
    gutter = nw * 2 + 3  # "old new " + sign + space
    code_w = max((l.code.cell_len for l in doc.lines if l.code is not None), default=0)
    total = max(width, gutter + code_w + 1)
    out = Text(no_wrap=True, overflow="crop")
    first = True

    def pad(t: Text, bg: str | None) -> None:
        extra = total - t.cell_len
        if extra > 0:
            t.append(" " * extra, style=f"on {bg}" if bg else "")

    for ln in doc.lines:
        if not first:
            out.append("\n")
        first = False
        if ln.kind == "hunk":
            t = Text(no_wrap=True)
            t.append(" " * (nw * 2 + 2), style=f"on {pal.hunk}")
            t.append(f" ⋯ {ln.raw}", style=f"{pal.primary} italic on {pal.hunk}")
            pad(t, pal.hunk)
            out.append(t)
            continue
        if ln.kind == "meta":
            t = Text(f"  {ln.raw}", style=f"{pal.muted} italic", no_wrap=True)
            out.append(t)
            continue
        if ln.kind == "note":
            t = Text(f"{' ' * (nw * 2 + 3)}\\ {ln.raw}", style=f"{pal.faint} italic", no_wrap=True)
            out.append(t)
            continue
        if ln.kind == "add":
            bg, gbg, hi, sign, scol = pal.add_bg, pal.add_gutter, pal.add_hi, "+", pal.add
        elif ln.kind == "del":
            bg, gbg, hi, sign, scol = pal.rem_bg, pal.rem_gutter, pal.rem_hi, "-", pal.rem
        else:
            bg, gbg, hi, sign, scol = None, None, None, " ", pal.faint
        t = Text(no_wrap=True)
        o = f"{ln.old:>{nw}}" if ln.old else " " * nw
        n = f"{ln.new:>{nw}}" if ln.new else " " * nw
        gstyle = f"{pal.muted} on {gbg}" if gbg else pal.faint
        t.append(f"{o} {n}", style=gstyle)
        t.append(f" {sign} " if sign != " " else "   ", style=f"bold {scol} on {bg}" if bg else "")
        code = ln.code.copy() if ln.code is not None else Text("")
        if bg:
            code.stylize(f"on {bg}")
            for a, b in ln.marks:
                code.stylize(f"on {hi}", a, b)
        t.append_text(code)
        pad(t, bg)
        out.append(t)
    if doc.notice:
        out.append("\n")
        out.append(Text(f"\n  ⚠ {doc.notice}", style=f"bold {pal.mod}", no_wrap=True))
    return out


def note_text(title: str, detail: str, pal: Palette, glyph: str = "◆") -> Text:
    t = Text()
    t.append(f"\n  {glyph} ", style=f"bold {pal.accent}")
    t.append(title, style="bold")
    if detail:
        t.append(f"\n\n    {detail}", style=pal.muted)
    return t


def synth_added_diff(path: str, data: bytes) -> str:
    """A unified diff that adds the whole file (used for the root snapshot)."""
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    body = "\n".join("+" + l for l in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n" if lines else ""


# --------------------------------------------------------------------------- restore preview


def classify_worktree_diff(diff_text: str) -> dict[str, list[str]]:
    """Given `diff(snapshot, working tree)`, say what a restore would do per file.

    Direction: snapshot (a) -> working tree (b). A file only in b would be deleted by a
    restore, a file only in a would be created, others overwritten.
    """
    overwrite: list[str] = []
    create: list[str] = []
    delete: list[str] = []
    cur: str | None = None
    kind = "overwrite"
    pending_a: str | None = None

    def commit() -> None:
        nonlocal cur, kind
        if cur is not None:
            {"overwrite": overwrite, "create": create, "delete": delete}[kind].append(cur)
        cur, kind = None, "overwrite"

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            commit()
            mm = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            cur = mm.group(2) if mm else line[11:]
            pending_a = mm.group(1) if mm else None
        elif line.startswith("new file mode"):
            kind = "delete"  # exists only in the working tree
        elif line.startswith("deleted file mode"):
            kind = "create"  # exists only in the snapshot
        elif line.startswith("--- /dev/null"):
            kind = "delete"
            if cur is None:
                cur = "?"
        elif line.startswith("+++ /dev/null"):
            kind = "create"
        elif line.startswith("+++ ") and cur in (None, "?") and not line.startswith("+++ /dev/null"):
            cur = line[4:].strip()
            cur = cur[2:] if cur.startswith("b/") else cur
        elif line.startswith("Binary files ") and cur is None:
            m = re.match(r"Binary files (.+) and (.+) differ", line)
            if m:
                a, b = m.groups()
                if a == "/dev/null":
                    cur, kind = b[2:] if b.startswith("b/") else b, "delete"
                elif b == "/dev/null":
                    cur, kind = a[2:] if a.startswith("a/") else a, "create"
                else:
                    cur = b[2:] if b.startswith("b/") else b
    commit()
    del pending_a
    return {"overwrite": overwrite, "create": create, "delete": delete}


def error_text(exc: BaseException) -> str:
    """User-facing text for an exception (the core's StoreError is already a clean sentence)."""
    if type(exc).__name__ == "StoreError":
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def summarize_paths(paths: Iterable[str], limit: int = 8) -> list[str]:
    ps = list(paths)
    out = ps[:limit]
    if len(ps) > limit:
        out.append(f"… and {len(ps) - limit} more")
    return out
