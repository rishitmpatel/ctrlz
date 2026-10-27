"""The ctrlz terminal UI (Textual).

Layout (lazygit style)::

    ┌ Timeline ──────┐┌ Diff ─────────────────────────────┐
    │ newest first   ││ syntax-highlighted unified diff   │
    ├ Changes ───────┤│                                   │
    │ files of snap  ││                                   │
    └────────────────┘└───────────────────────────────────┘

Only the public ``Store`` API is used. Every call that can be slow (git) runs in a
thread worker so the UI never blocks.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from .render import (
    DiffDoc,
    Palette,
    build_diff_doc,
    change_row,
    describe,
    error_text,
    human_size,
    is_binary_diff,
    layout_diff,
    note_text,
    rel_time,
    snapshot_row,
    synth_added_diff,
)
from .screens import MODAL_CSS, ConfirmScreen, FindScreen, HelpScreen, PromptScreen, RestoreAllScreen
from .widgets import ChangeList, DiffView, HeaderBar, SnapList

PAGE = 200  # snapshots loaded at once; more are fetched while scrolling
FILTER_LIMIT = 1000  # how far back a path filter scans
DARK_THEME = "tokyo-night"
LIGHT_THEME = "textual-light"

# actions that must not fire while a modal is open
_MAIN_ACTIONS = {
    "restore_file", "restore_all", "undo", "toggle_worktree", "filter", "find", "snap", "help",
    "next_pane", "prev_pane", "focus_pane", "toggle_theme", "clear_filter", "go_top",
}


def _default_theme() -> str:
    forced = os.environ.get("CTRLZ_THEME")
    if forced:
        return forced
    fgbg = os.environ.get("COLORFGBG", "")
    if fgbg:
        try:
            bg = int(fgbg.split(";")[-1])
            if bg in (7, 15):
                return LIGHT_THEME
        except ValueError:
            pass
    return DARK_THEME


def _pick(d: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


CSS = (
    """
Screen { background: $background; }
HeaderBar { dock: top; height: 1; background: $panel; color: $text; padding: 0; }
Footer { background: $panel; }
#main { height: 1fr; padding: 0; }
#left { width: 52; min-width: 36; max-width: 50%; }
.pane {
    border: round $foreground 28%;
    background: $surface;
    border-title-color: $text-muted;
    border-title-style: bold;
    border-subtitle-color: $text-muted;
    padding: 0;
}
.pane:focus-within {
    border: round $primary;
    border-title-color: $text;
}
#timeline-box { height: 2fr; }
#changes-box { height: 1fr; min-height: 5; }
#diff-box { width: 1fr; }
SnapList, ChangeList {
    border: none;
    background: transparent;
    padding: 0;
    height: 1fr;
    scrollbar-size-vertical: 1;
}
SnapList:focus, ChangeList:focus { border: none; background: transparent; }
SnapList > .option-list--option-highlighted, ChangeList > .option-list--option-highlighted {
    background: $primary 30%;
    text-style: none;
}
SnapList:focus > .option-list--option-highlighted, ChangeList:focus > .option-list--option-highlighted {
    background: $primary 50%;
    text-style: none;
}
SnapList > .option-list--option-hover, ChangeList > .option-list--option-hover { background: $primary 12%; }
#filter {
    dock: bottom;
    height: 1;
    border: none;
    padding: 0 1;
    background: $panel;
    display: none;
}
#filter.-active { display: block; }
#diff-meta { height: auto; max-height: 2; padding: 0 1; color: $text-muted; background: $surface; }
DiffView {
    height: 1fr;
    background: transparent;
    overflow-x: auto;
    overflow-y: auto;
    scrollbar-size-vertical: 1;
    scrollbar-size-horizontal: 1;
    scrollbar-gutter: stable;
}
DiffView #diff-body { width: auto; min-width: 100%; height: auto; background: transparent; }
Toast { width: 62; }
.narrow #main { layout: vertical; }
.narrow #left { width: 100%; max-width: 100%; height: 45%; layout: horizontal; }
.narrow #timeline-box { width: 1fr; height: 100%; }
.narrow #changes-box { width: 1fr; height: 100%; }
.narrow #diff-box { height: 1fr; width: 100%; }
"""
    + MODAL_CSS
)


class CtrlzApp(App[None]):
    TITLE = "ctrlz"
    SUB_TITLE = "undo anything"
    CSS = CSS

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("tab", "next_pane", "Pane", priority=False),
        Binding("shift+tab", "prev_pane", "Prev pane", show=False),
        Binding("1", "focus_pane('timeline')", "Timeline", show=False),
        Binding("2", "focus_pane('changes')", "Changes", show=False),
        Binding("3", "focus_pane('diff')", "Diff", show=False),
        Binding("h,left", "prev_pane", "Prev pane", show=False),
        Binding("l,right", "next_pane", "Next pane", show=False),
        Binding("r", "restore_file", "Restore file"),
        Binding("R", "restore_all", "Restore all", key_display="R"),
        Binding("u", "undo", "Undo"),
        Binding("w", "toggle_worktree", "Vs worktree"),
        Binding("slash", "filter", "Filter", key_display="/"),
        Binding("f", "find", "Find"),
        Binding("s", "snap", "Snapshot"),
        Binding("t", "toggle_theme", "Theme", show=False),
        Binding("escape", "clear_filter", "Clear filter", show=False),
    ]

    def __init__(
        self,
        store: Any,
        poll_interval: float = 1.0,
        page: int = PAGE,
        theme: str | None = None,
    ) -> None:
        super().__init__()
        self.store = store
        self.poll_interval = poll_interval
        self.page = page
        self._theme_name = theme or _default_theme()
        # data
        self._snaps: list[Any] = []  # unfiltered, newest first
        self._view: list[Any] = []  # what the timeline shows
        self._by_id: dict[str, Any] = {}
        self._limit: int | None = page
        self._exhausted = False
        self._loading = False
        self._filter = ""
        self._stats: dict[str, Any] = {}
        self._changes_cache: dict[str, list[Any]] = {}
        self._doc_cache: dict[tuple, DiffDoc] = {}
        self._rel_cache: dict[str, str] = {}
        # selection
        self._sel_id: str | None = None
        self._sel: Any = None
        self._changes: list[Any] = []
        self._changes_for: str | None = None
        self._cur: Any = None  # selected FileChange
        self._want_path: str | None = None
        self._vs_wt = False
        self._unseen = 0
        self._busy = ""
        self._doc: DiffDoc | None = None
        self._doc_note: Text | None = None
        self._laid_for: DiffDoc | None = None
        self._shown: tuple[str, str, bool] | None = None  # (snapshot id, path, vs_wt) the diff pane displays
        self._seq = 0
        self._sel_timer: Timer | None = None
        self._diff_timer: Timer | None = None
        self._filter_timer: Timer | None = None
        self.pal: Palette | None = None

    # ------------------------------------------------------------------ compose / mount

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                with Vertical(id="timeline-box", classes="pane") as tb:
                    tb.border_title = "1 Timeline"
                    yield SnapList(id="timeline")
                    yield Input(placeholder="filter by path…  (enter keeps, esc clears)", id="filter")
                with Vertical(id="changes-box", classes="pane") as cb:
                    cb.border_title = "2 Changes"
                    yield ChangeList(id="changes")
            with Vertical(id="diff-box", classes="pane") as db:
                db.border_title = "3 Diff"
                yield Static("", id="diff-meta")
                yield DiffView(id="diff")
        yield Footer()

    def on_mount(self) -> None:
        try:
            self.theme = self._theme_name
        except Exception:
            self.theme = DARK_THEME
        self._update_palette()
        self.theme_changed_signal.subscribe(self, self._on_theme_changed)
        self._q("#diff", DiffView).relayout = self._relayout
        self._q("#timeline", SnapList).focus()
        self._set_narrow(self.size.width)
        self._show_message("Loading…", "")
        self._loading = True
        self._refresh(self._limit, "")
        self.set_interval(self.poll_interval, self._tick)

    def _update_palette(self) -> None:
        self.pal = Palette.from_vars(self.get_css_variables(), self.current_theme.dark)

    def _on_theme_changed(self, _theme: Any) -> None:
        self._update_palette()
        self._rebuild_timeline(self._sel_id)
        self._fill_changes()
        self._update_header()
        if self._doc is not None:
            # syntax colours depend on the theme: rebuild the diff
            self._doc_cache.clear()
            self._request_diff()

    def on_resize(self, event: Any) -> None:
        self._set_narrow(event.size.width)

    def _set_narrow(self, width: int) -> None:
        self.set_class(width < 110, "narrow")

    # ------------------------------------------------------------------ helpers

    def _post(self, fn: Any, *args: Any) -> None:
        """call_from_thread that is a no-op once the app is shutting down."""
        try:
            self.call_from_thread(fn, *args)
        except RuntimeError:
            pass

    def _q(self, selector: Any, expect: Any = None) -> Any:
        """Query the main screen even while a modal screen is on top."""
        return self.screen_stack[0].query_one(selector, expect) if expect else self.screen_stack[0].query_one(selector)

    @property
    def timeline(self) -> SnapList:
        return self._q("#timeline", SnapList)

    @property
    def changes_list(self) -> ChangeList:
        return self._q("#changes", ChangeList)

    @property
    def diff_view(self) -> DiffView:
        return self._q("#diff", DiffView)

    def _parent_of(self, snap: Any) -> tuple[Any, bool]:
        """(parent, needs_lookup). needs_lookup=True means: not loaded yet, ask the store."""
        ids = [s.id for s in self._snaps]
        try:
            i = ids.index(snap.id)
        except ValueError:
            return None, True
        if i + 1 < len(ids):
            return self._snaps[i + 1], False
        return None, not self._exhausted

    def _changes_of(self, snap: Any) -> list[Any]:
        got = self._changes_cache.get(snap.id)
        if got is None:
            got = list(self.store.changes(snap))
            self._changes_cache[snap.id] = got
        return got

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in _MAIN_ACTIONS and isinstance(self.screen, ModalScreen):
            return False
        return True

    def _toast(self, message: str, title: str = "", severity: str = "information", timeout: float = 9.0) -> None:
        self.notify(message, title=title, severity=severity, timeout=timeout)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ header

    def _update_header(self) -> None:
        if self.pal is None:
            return
        root = str(getattr(self.store, "display_root", None) or self.store.root)
        home = str(Path.home())
        if root == home or root.startswith(home + os.sep):
            root = "~" + root[len(home):]
        count = _pick(self._stats, ("snapshots", "count", "total"), None)
        if count is None:
            count = len(self._snaps)
        size = _pick(self._stats, ("size_bytes", "store_size", "size", "bytes", "disk"), 0)
        try:
            size_s = human_size(int(size))
        except (TypeError, ValueError):
            size_s = str(size)
        latest = rel_time(self._snaps[0].time) if self._snaps else ""
        try:
            header = self._q(HeaderBar)
        except NoMatches:
            return
        header.set_info(
            self.pal, root, int(count), size_s, latest, status=self._busy, newer=self._unseen
        )

    # ------------------------------------------------------------------ polling & loading

    def _tick(self) -> None:
        if not self.is_running:
            return
        self._refresh_times()
        if not self._loading and not self._busy:
            self._poll()

    def _refresh_times(self) -> None:
        if self.pal is None or not self._view:
            return
        tl = self.timeline
        now = datetime.now(timezone.utc)
        changed = False
        for i, s in enumerate(self._view):
            r = rel_time(s.time, now)
            if self._rel_cache.get(s.id) != r:
                self._rel_cache[s.id] = r
                try:
                    tl.replace_option_prompt_at_index(i, snapshot_row(s, self.pal, now))
                except Exception:
                    pass
                changed = True
        if changed or self._snaps:
            self._update_header()

    @work(thread=True, exclusive=True, group="poll")
    def _poll(self) -> None:
        try:
            head = self.store.log(limit=1)
        except Exception:
            return
        head_id = head[0].id if head else None
        known = self._snaps[0].id if self._snaps else None
        if head_id != known and not self._loading:
            self._post(self._start_refresh)

    def _start_refresh(self, limit: int | None | str = "keep", select_id: str | None = None, path: str | None = None) -> None:
        if limit == "keep":
            limit = self._limit
        self._loading = True
        self._refresh(limit, self._filter, select_id, path)  # type: ignore[arg-type]

    @work(thread=True, exclusive=True, group="refresh")
    def _refresh(self, limit: int | None, flt: str, select_id: str | None = None, path: str | None = None) -> None:
        worker = get_current_worker()
        try:
            lim = limit
            if flt and lim is not None:
                lim = max(lim, FILTER_LIMIT)
            snaps = list(self.store.log(limit=lim))
            exhausted = lim is None or len(snaps) < lim
            try:
                stats = dict(self.store.stats())
            except Exception:
                stats = {}
        except Exception as exc:
            self._post(self._load_failed, exc)
            return
        view: list[Any]
        if flt:
            view = []
            fl = flt.lower()
            last = time.monotonic()
            for i, s in enumerate(snaps):
                if worker.is_cancelled:
                    return
                try:
                    chs = self._changes_of(s)
                except Exception:
                    chs = []
                if any(fl in c.path.lower() or (c.old_path and fl in c.old_path.lower()) for c in chs):
                    view.append(s)
                if time.monotonic() - last > 0.25 and i + 1 < len(snaps):
                    last = time.monotonic()
                    self._post(
                        self._apply, snaps, list(view), stats, limit, exhausted, select_id, path, f"scanning {i + 1}/{len(snaps)}"
                    )
        else:
            view = snaps
        if worker.is_cancelled:
            return
        self._post(self._apply, snaps, view, stats, limit, exhausted, select_id, path, None)

    def _load_failed(self, exc: Exception) -> None:
        self._loading = False
        self._toast(error_text(exc), "Could not read the timeline", "error")

    def _apply(
        self,
        snaps: list[Any],
        view: list[Any],
        stats: dict[str, Any],
        limit: int | None,
        exhausted: bool,
        select_id: str | None,
        path: str | None,
        scanning: str | None,
    ) -> None:
        old_head = self._snaps[0].id if self._snaps else None
        old_ids = {s.id for s in self._snaps}
        self._snaps = snaps
        self._by_id = {s.id: s for s in snaps}
        self._view = view
        self._stats = stats
        self._limit = limit
        self._exhausted = exhausted
        if scanning is None:
            self._loading = False
        # unseen counter: snapshots that arrived above the user's selection
        if old_ids and snaps:
            fresh = [s for s in snaps if s.id not in old_ids]
            if fresh and self._sel_id is not None and view and view[0].id != self._sel_id:
                self._unseen += len(fresh)
        if path:
            self._want_path = path
        self._rebuild_timeline(select_id or self._sel_id)
        if self._filter:
            suffix = f"  ·  {scanning}" if scanning else ""
            self._q("#timeline-box").border_subtitle = f"/{self._filter}  ({len(view)} match{'es' if len(view) != 1 else ''}){suffix}"
        else:
            self._q("#timeline-box").border_subtitle = ""
        self._update_header()
        # a restore / external change may have staled a working-tree diff
        if self._vs_wt and self._cur is not None and snaps and snaps[0].id != old_head:
            self._request_diff()

    # ------------------------------------------------------------------ timeline

    def _rebuild_timeline(self, select_id: str | None) -> None:
        if self.pal is None:
            return
        tl = self.timeline
        now = datetime.now(timezone.utc)
        old_idx = tl.highlighted
        old_scroll = tl.scroll_y
        opts = [Option(snapshot_row(s, self.pal, now), id=s.id) for s in self._view]
        self._rel_cache = {s.id: rel_time(s.time, now) for s in self._view}
        with self.batch_update():
            tl.clear_options()
            tl.add_options(opts)
        if not self._view:
            self._sel_id, self._sel = None, None
            self._clear_panes()
            return
        ids = [s.id for s in self._view]
        target = select_id if select_id in ids else None
        idx = ids.index(target) if target else 0
        tl.highlighted = idx
        if old_idx is not None and target and old_scroll:
            shift = idx - old_idx
            y = max(0, old_scroll + shift)
            tl.call_after_refresh(lambda: tl.scroll_to(y=y, animate=False, force=True))
        self._update_timeline_title(idx)

    def _update_timeline_title(self, idx: int | None) -> None:
        box = self._q("#timeline-box")
        n = len(self._view)
        total = _pick(self._stats, ("snapshots", "count", "total"), None)
        if idx is None or not n:
            box.border_title = "1 Timeline"
        else:
            more = "+" if not self._exhausted and not self._filter else ""
            box.border_title = f"1 Timeline  {idx + 1}/{n}{more}"
        del total

    @on(OptionList.OptionHighlighted, "#timeline")
    def _timeline_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        sid = event.option.id
        idx = event.option_index
        self._update_timeline_title(idx)
        if idx == 0:
            self._unseen = 0
            self._update_header()
        if (
            not self._filter
            and not self._exhausted
            and not self._loading
            and self._limit is not None
            and idx >= len(self._view) - 8
        ):
            self._start_refresh(self._limit + self.page)
        if sid == self._sel_id and self._changes_for == sid:
            return
        self._sel_id = sid
        self._sel = self._by_id.get(sid)
        if self._sel_timer is not None:
            self._sel_timer.stop()
        self._sel_timer = self.set_timer(0.04, self._load_selected)

    def _load_selected(self) -> None:
        snap = self._sel
        if snap is None:
            return
        self._load_changes(snap)

    # ------------------------------------------------------------------ changes pane

    @work(thread=True, exclusive=True, group="changes")
    def _load_changes(self, snap: Any) -> None:
        try:
            chs = self._changes_of(snap)
            err = None
        except Exception as exc:
            chs, err = [], error_text(exc)
        if not get_current_worker().is_cancelled:
            self._post(self._apply_changes, snap, chs, err)

    def _apply_changes(self, snap: Any, chs: list[Any], err: str | None) -> None:
        if snap.id != self._sel_id:
            return
        if err:
            self._toast(err, "Could not read changes", "error")
        prev_path = self._cur.path if self._cur is not None else None
        self._changes = chs
        self._changes_for = snap.id
        want = self._want_path or prev_path
        self._want_path = None
        self._fill_changes(prefer=want)

    def _fill_changes(self, prefer: str | None = None) -> None:
        if self.pal is None:
            return
        cl = self.changes_list
        if prefer is None and self._cur is not None:
            prefer = self._cur.path
        with self.batch_update():
            cl.clear_options()
            cl.add_options([Option(change_row(c, self.pal), id=str(i)) for i, c in enumerate(self._changes)])
        box = self._q("#changes-box")
        snap = self._sel
        n = len(self._changes)
        box.border_title = f"2 Changes  {snap.short if snap else ''}  ({n} file{'s' if n != 1 else ''})" if snap else "2 Changes"
        if not self._changes:
            self._cur = None
            self._show_message(
                "No file changes in this snapshot" if snap else "",
                "It is identical to its parent." if snap else "",
            )
            return
        idx = 0
        if prefer:
            for i, c in enumerate(self._changes):
                if c.path == prefer:
                    idx = i
                    break
        self._cur = None  # force diff refresh
        cl.highlighted = idx
        self._cur = self._changes[idx]
        self._request_diff()

    @on(OptionList.OptionHighlighted, "#changes")
    def _change_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        idx = event.option_index
        if idx is None or idx >= len(self._changes):
            return
        ch = self._changes[idx]
        if self._cur is ch and self._doc is not None:
            return
        self._cur = ch
        if self._diff_timer is not None:
            self._diff_timer.stop()
        self._diff_timer = self.set_timer(0.03, self._request_diff)

    # ------------------------------------------------------------------ diff pane

    def _clear_panes(self) -> None:
        self._changes = []
        self._changes_for = None
        self._cur = None
        self._doc = None
        self.changes_list.clear_options()
        self._q("#changes-box").border_title = "2 Changes"
        if self._filter:
            self._show_message(f"No snapshot touched a path matching “{self._filter}”", "Press esc to clear the filter.")
        else:
            self._show_message(
                "No snapshots yet",
                "Press s to take one, or run `ctrlz start` to snapshot automatically.",
            )

    def _show_message(self, title: str, detail: str, meta: str = "") -> None:
        if self.pal is None:
            return
        self._doc = None
        self._shown = None
        self._doc_note = note_text(title, detail, self.pal) if title else Text("")
        self.diff_view.show(self._doc_note)
        self._q("#diff-meta").update(meta)  # type: ignore[attr-defined]

    def _request_diff(self) -> None:
        snap, ch = self._sel, self._cur
        if snap is None or ch is None or self.pal is None:
            return
        self._seq += 1
        parent, lookup = self._parent_of(snap)
        key = (snap.id, parent.id if parent else None, ch.path, self.pal.syntax_theme)
        if not self._vs_wt and key in self._doc_cache:
            self._set_doc(snap, ch, parent, self._doc_cache[key], None)
            return
        self._diff_worker(self._seq, snap, ch, parent, lookup, self._vs_wt, self.pal, key)

    @work(thread=True, exclusive=True, group="diff")
    def _diff_worker(self, seq: int, snap: Any, ch: Any, parent: Any, lookup: bool, wt: bool, pal: Palette, key: tuple) -> None:
        store = self.store
        worker = get_current_worker()
        paths = [ch.path] + ([ch.old_path] if getattr(ch, "old_path", None) else [])
        doc: DiffDoc | None = None
        note: Text | None = None
        try:
            if lookup and not wt:
                lst = store.log(limit=len(self._snaps) + 2)
                ids = [s.id for s in lst]
                if snap.id in ids and ids.index(snap.id) + 1 < len(lst):
                    parent = lst[ids.index(snap.id) + 1]
            if wt:
                text = store.diff(snap.id, None, paths=paths)
                binary = is_binary_diff(text)
            elif parent is None:
                data = store.read(snap.id, ch.path)
                binary = b"\0" in data[:8192]
                text = "" if binary else synth_added_diff(ch.path, data)
            else:
                text = store.diff(parent.id, snap.id, paths=paths)
                binary = is_binary_diff(text)
            if binary:
                a, b = self._binary_sizes(snap, parent, ch, wt)
                note = note_text(
                    f"binary file changed (size {a} → {b})",
                    "Binary files are not shown as a diff. Restore the file with r to get it back.",
                    pal,
                    "▣",
                )
            elif not text.strip():
                msg = (
                    ("identical to your working tree", "Nothing to show — the file on disk matches this snapshot.")
                    if wt
                    else ("no textual changes", "Only the file mode (permissions) changed, or the content is identical.")
                )
                note = note_text(msg[0], msg[1], pal, "✓")
            else:
                doc = build_diff_doc(text, ch.path, pal)
        except Exception as exc:
            note = note_text("could not load diff", error_text(exc), pal, "✗")
        if worker.is_cancelled:
            return
        if doc is not None and not wt:
            self._doc_cache[key] = doc
            if len(self._doc_cache) > 80:
                self._doc_cache.pop(next(iter(self._doc_cache)))
        self._post(self._finish_diff, seq, snap, ch, parent, doc, note)

    def _binary_sizes(self, snap: Any, parent: Any, ch: Any, wt: bool) -> tuple[str, str]:
        def size(ref: str | None, path: str) -> str:
            if ref is None:
                return "—"
            try:
                return human_size(len(self.store.read(ref, path)))
            except Exception:
                return "—"

        if wt:
            a = size(snap.id, ch.path)
            try:
                b = human_size((Path(self.store.root) / ch.path).stat().st_size)
            except OSError:
                b = "—"
            return a, b
        old_path = getattr(ch, "old_path", None) or ch.path
        a = "—" if ch.status == "A" or parent is None else size(parent.id, old_path)
        b = "—" if ch.status == "D" else size(snap.id, ch.path)
        return a, b

    def _finish_diff(self, seq: int, snap: Any, ch: Any, parent: Any, doc: DiffDoc | None, note: Text | None) -> None:
        if seq != self._seq or snap.id != self._sel_id or ch is not self._cur:
            return
        self._set_doc(snap, ch, parent, doc, note)

    def _set_doc(self, snap: Any, ch: Any, parent: Any, doc: DiffDoc | None, note: Text | None) -> None:
        if self.pal is None:
            return
        pal = self.pal
        self._doc, self._doc_note = doc, note
        self._laid_for = None
        self._shown = (snap.id, ch.path, self._vs_wt)
        base = f"parent {parent.short}" if parent is not None else "nothing (first snapshot)"
        against = "working tree" if self._vs_wt else base
        box = self._q("#diff-box")
        box.border_title = f"3 Diff  {ch.path}"
        box.border_subtitle = f"{snap.short} vs {against}" if not self._vs_wt else f"{snap.short} → working tree"
        meta = Text(no_wrap=True, overflow="ellipsis")
        if doc is not None:
            meta.append(f"+{doc.n_add}", style=f"bold {pal.add}")
            meta.append(" ")
            meta.append(f"-{doc.n_del}", style=f"bold {pal.rem}")
            meta.append("   ")
        meta.append(ch.status, style="bold")
        meta.append(f"  {ch.path}")
        if self._vs_wt:
            meta.append("   (what a restore of this file would change)" if doc is not None else "", style=pal.faint)
        self._q("#diff-meta").update(meta)  # type: ignore[attr-defined]
        if doc is not None:
            self._relayout()
        elif note is not None:
            self.diff_view.show(note)

    def _relayout(self) -> None:
        if self._doc is None or self.pal is None:
            return
        view = self.diff_view
        width = max(20, view.scrollable_content_region.width)
        keep = self._laid_for is self._doc
        self._laid_for = self._doc
        view.show(layout_diff(self._doc, self.pal, width), keep_scroll=keep)

    # ------------------------------------------------------------------ actions: navigation

    _PANES = ("timeline", "changes", "diff")

    def _focused_pane(self) -> int:
        f = self.focused
        for i, name in enumerate(self._PANES):
            if f is not None and f.id == name:
                return i
        return 0

    def action_next_pane(self) -> None:
        self._q("#" + self._PANES[(self._focused_pane() + 1) % 3]).focus()

    def action_prev_pane(self) -> None:
        self._q("#" + self._PANES[(self._focused_pane() - 1) % 3]).focus()

    def action_focus_pane(self, name: str) -> None:
        self._q("#" + name).focus()

    def action_go_top(self) -> None:
        self.timeline.highlighted = 0

    def action_toggle_theme(self) -> None:
        self.theme = LIGHT_THEME if self.current_theme.dark else DARK_THEME

    def action_help(self) -> None:
        if self.pal is not None:
            self.push_screen(HelpScreen(self.pal))

    def action_toggle_worktree(self) -> None:
        self._vs_wt = not self._vs_wt
        self._toast(
            "Diff now compares the snapshot with your working tree" if self._vs_wt else "Diff now compares the snapshot with its parent",
            timeout=2.5,
        )
        self._laid_for = None
        self._request_diff()

    # ------------------------------------------------------------------ filter

    def action_filter(self) -> None:
        inp = self._q("#filter", Input)
        inp.add_class("-active")
        inp.value = self._filter
        inp.focus()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        event.stop()
        if self._filter_timer is not None:
            self._filter_timer.stop()
        value = event.value
        self._filter_timer = self.set_timer(0.25, lambda: self._set_filter(value))

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._set_filter(event.value)
        if not self._filter:
            event.input.remove_class("-active")
        self.timeline.focus()

    def _set_filter(self, text: str) -> None:
        text = text.strip()
        if text == self._filter:
            return
        self._filter = text
        self._loading = True
        self._refresh(self._limit, text)

    def action_clear_filter(self) -> None:
        inp = self._q("#filter", Input)
        if inp.has_class("-active") or self._filter:
            if self._filter_timer is not None:
                self._filter_timer.stop()
            inp.value = ""
            inp.remove_class("-active")
            self.timeline.focus()
            if self._filter:
                self._filter = ""
                self._loading = True
                self._refresh(self._limit, "")

    # ------------------------------------------------------------------ find

    def action_find(self) -> None:
        if self.pal is None:
            return
        self.push_screen(FindScreen(self.store, self.pal), self._found)

    def _found(self, hit: tuple[Any, str] | None) -> None:
        if not hit:
            return
        snap, path = hit
        self.jump_to(snap.id, path)

    def jump_to(self, snap_id: str, path: str | None = None) -> None:
        """Select a snapshot (and optionally a file in it), clearing any filter."""
        inp = self._q("#filter", Input)
        inp.value = ""
        inp.remove_class("-active")
        self._filter = ""
        self._loading = True
        self._unseen = 0
        limit = self._limit if snap_id in self._by_id else None
        self._sel_id = None  # make sure the selection event is not skipped
        self._changes_for = None
        self.changes_list.focus()
        self._refresh(limit, "", snap_id, path)

    # ------------------------------------------------------------------ restore / undo / snapshot

    def _set_busy(self, text: str) -> None:
        self._busy = text
        self._update_header()

    def action_restore_file(self) -> None:
        snap, ch = self._sel, self._cur
        if snap is None or ch is None:
            self._toast("Select a file in the Changes pane first.", severity="warning", timeout=3)
            return
        parent, lookup = self._parent_of(snap)
        self._prepare_restore_file(snap, ch, parent, lookup)

    @work(thread=True, exclusive=True, group="restore-prep")
    def _prepare_restore_file(self, snap: Any, ch: Any, parent: Any, lookup: bool) -> None:
        store = self.store
        ref = snap
        try:
            if ch.status == "D":
                # deleted by this snapshot: bring back the last version that existed
                if lookup:
                    lst = store.log(limit=len(self._snaps) + 2)
                    ids = [s.id for s in lst]
                    if snap.id in ids and ids.index(snap.id) + 1 < len(lst):
                        parent = lst[ids.index(snap.id) + 1]
                if parent is None:
                    self._post(self._toast, "Nothing earlier to restore this file from.", "", "warning", 4)
                    return
                ref = parent
            want = store.read(ref.id, ch.path)
            disk_path = Path(store.root) / ch.path
            have = disk_path.read_bytes() if disk_path.is_file() else None
            if have is not None and have == want:
                self._post(self._toast, f"{ch.path} already matches {ref.short}.", "Nothing to restore", "information", 4)
                return
            lines_in = lines_out = 0
            if have is not None:
                try:
                    d = store.diff(ref.id, None, paths=[ch.path])
                    for ln in d.splitlines():
                        if ln.startswith("-") and not ln.startswith("---"):
                            lines_in += 1
                        elif ln.startswith("+") and not ln.startswith("+++"):
                            lines_out += 1
                except Exception:
                    pass
        except Exception as exc:
            self._post(self._toast, error_text(exc), "Cannot restore", "error")
            return
        self._post(self._confirm_restore_file, snap, ref, ch, have is None, lines_in, lines_out)

    def _confirm_restore_file(self, snap: Any, ref: Any, ch: Any, missing: bool, lines_in: int, lines_out: int) -> None:
        pal = self.pal
        assert pal is not None
        glyph, attr, what = describe(ref)
        body = Text()
        body.append(ch.path + "\n", style=f"bold {pal.accent}")
        body.append("\nfrom snapshot ", style=pal.muted)
        body.append(ref.short, style=f"bold {pal.accent}")
        body.append(f"  {what}  ", style="bold")
        body.append(rel_time(ref.time) + "\n", style=pal.muted)
        if ch.status == "D":
            body.append("\nThis file was deleted by the selected snapshot; the last version before that is brought back.\n", style=pal.muted)
        body.append("\n")
        if missing:
            body.append("+ The file is missing on disk and will be re-created.", style=pal.add)
        else:
            body.append("~ The file on disk will be overwritten", style=pal.mod)
            if lines_in or lines_out:
                body.append(f"  (+{lines_in} lines restored, -{lines_out} lines dropped)", style=pal.muted)
            body.append(".", style=pal.mod)
        body.append("\n\nA safety snapshot is taken first — press u afterwards to undo.", style=pal.muted)
        self.push_screen(
            ConfirmScreen("Restore file", body, "Restore"),
            lambda ok: self._run_restore(ref, [ch.path], True, f"1 file from {ref.short}") if ok else None,
        )

    def action_restore_all(self) -> None:
        snap = self._sel
        if snap is None or self.pal is None:
            return
        self.push_screen(
            RestoreAllScreen(self.store, snap, self.pal),
            lambda r: self._run_restore(snap, None, r["delete_extra"], f"whole project to {snap.short}") if r else None,
        )

    def _run_restore(self, ref: Any, paths: list[str] | None, delete_extra: bool, what: str) -> None:
        self._set_busy("restoring…")
        self._restore_worker(ref, paths, delete_extra, what)

    @work(thread=True, exclusive=True, group="mutate")
    def _restore_worker(self, ref: Any, paths: list[str] | None, delete_extra: bool, what: str) -> None:
        try:
            res = self.store.restore(ref.id, paths=paths, delete_extra=delete_extra)
        except Exception as exc:
            self._post(self._mutation_failed, "Restore failed", exc)
            return
        self._post(self._restore_done, res, what)

    def _restore_done(self, res: Any, what: str) -> None:
        self._set_busy("")
        w, d, s = len(res.written), len(res.deleted), len(res.skipped)
        parts = []
        if w:
            parts.append(f"{w} written")
        if d:
            parts.append(f"{d} deleted")
        if s:
            parts.append(f"{s} skipped")
        summary = ", ".join(parts) if parts else "nothing needed changing"
        saved = getattr(res, "undo_ref", None) or (res.before.short if getattr(res, "before", None) else None)
        before = f"  (previous state saved as {saved})" if saved else ""
        self._toast(
            f"Restored {what}: {summary}.{before}\nChanged your mind?  Press u to undo this restore.",
            "Restored",
            timeout=12,
        )
        self._after_mutation()

    def action_undo(self) -> None:
        if not self._snaps or self.pal is None:
            self._toast("No snapshots yet — nothing to undo.", severity="warning", timeout=3)
            return
        self._prepare_undo(self._snaps[0])

    @work(thread=True, exclusive=True, group="restore-prep")
    def _prepare_undo(self, latest: Any) -> None:
        plan = None
        try:
            planner = getattr(self.store, "plan_undo", None)  # optional dry-run offered by the core
            if planner is not None:
                plan = planner()
        except Exception as exc:
            self._post(self._toast, error_text(exc), "Nothing to undo", "warning", 5)
            return
        self._post(self._confirm_undo, latest, plan)

    def _confirm_undo(self, latest: Any, plan: Any) -> None:
        pal = self.pal
        assert pal is not None
        if plan is not None and getattr(plan, "empty", False):
            self._toast("The project already matches the previous state.", "Nothing to undo", timeout=4)
            return
        body = Text()
        body.append("Revert the most recent change:\n\n", style="bold")
        target = getattr(plan, "target", None) if plan is not None else None
        if target is not None and target.id == latest.id:
            body.append("Changes since the last snapshot are thrown away; the project goes back to\n", style=pal.muted)
        glyph, attr, what = describe(latest)
        body.append(f"{latest.short}", style=f"bold {pal.accent}")
        body.append(f"  {what}  ", style="bold")
        body.append(rel_time(latest.time), style=pal.muted)
        body.append("  ")
        st = latest.stats
        if st.get("added"):
            body.append(f"+{st['added']} ", style=pal.add)
        if st.get("modified"):
            body.append(f"~{st['modified']} ", style=pal.mod)
        if st.get("deleted"):
            body.append(f"-{st['deleted']}", style=pal.rem)
        if plan is not None:
            body.append("\n\n")
            body.append(f"{len(plan.overwrite)} overwritten", style=pal.mod)
            body.append("  ·  ", style=pal.faint)
            body.append(f"{len(plan.create)} re-created", style=pal.add)
            body.append("  ·  ", style=pal.faint)
            body.append(f"{len(plan.delete)} deleted", style=pal.rem)
        body.append("\n\nThe current state is saved first, so undo is itself undoable (press u again).", style=pal.muted)
        self.push_screen(ConfirmScreen("Undo last change", body, "Undo"), lambda ok: self._run_undo() if ok else None)

    def _run_undo(self) -> None:
        self._set_busy("undoing…")
        self._undo_worker()

    @work(thread=True, exclusive=True, group="mutate")
    def _undo_worker(self) -> None:
        try:
            res = self.store.undo()
        except Exception as exc:
            self._post(self._mutation_failed, "Undo failed", exc)
            return
        self._post(self._undo_done, res)

    def _undo_done(self, res: Any) -> None:
        self._set_busy("")
        w, d = len(res.written), len(res.deleted)
        parts = []
        if w:
            parts.append(f"{w} file{'s' if w != 1 else ''} written")
        if d:
            parts.append(f"{d} deleted")
        summary = ", ".join(parts) if parts else "nothing needed changing"
        self._toast(f"Undid the last change: {summary}.\nChanged your mind?  Press u again to undo the undo.", "Undone", timeout=12)
        self._after_mutation()

    def action_snap(self) -> None:
        self.push_screen(
            PromptScreen("Take a snapshot", "optional label, e.g. before refactor", "enter save  ·  esc cancel"),
            lambda label: self._run_snap(label) if label is not None else None,
        )

    def _run_snap(self, label: str) -> None:
        self._set_busy("snapshotting…")
        self._snap_worker(label or None)

    @work(thread=True, exclusive=True, group="mutate")
    def _snap_worker(self, label: str | None) -> None:
        try:
            snap = self.store.snap("manual", label=label)
        except Exception as exc:
            self._post(self._mutation_failed, "Snapshot failed", exc)
            return
        self._post(self._snap_done, snap)

    def _snap_done(self, snap: Any) -> None:
        self._set_busy("")
        if snap is None:
            self._toast("Nothing changed since the last snapshot.", "No snapshot taken", timeout=4)
        else:
            self._toast(f"Saved snapshot {snap.short}" + (f" — {snap.label}" if snap.label else ""), "Snapshot", timeout=5)
        self._after_mutation(jump_top=snap is not None)

    def _mutation_failed(self, title: str, exc: Exception) -> None:
        self._set_busy("")
        self._toast(error_text(exc), title, "error", 12)

    def _after_mutation(self, jump_top: bool = False) -> None:
        self._changes_for = None
        self._loading = True
        self._refresh(self._limit, self._filter, select_id=None if jump_top else self._sel_id)
        if jump_top:
            self._sel_id = None


def run(store: Any) -> None:
    """Run the TUI for ``store`` (blocks until the user quits)."""
    CtrlzApp(store).run()
