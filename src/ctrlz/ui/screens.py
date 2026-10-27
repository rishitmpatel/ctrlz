"""Modal screens: confirmations, prompts, history search, help."""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from .render import (
    Palette,
    classify_worktree_diff,
    describe,
    rel_time,
    error_text,
    summarize_paths,
)

MODAL_CSS = """
ConfirmScreen, RestoreAllScreen, PromptScreen, FindScreen, HelpScreen {
    align: center middle;
    background: $background 60%;
}
.dialog {
    width: 68;
    max-width: 94%;
    height: auto;
    max-height: 90%;
    background: $surface;
    border: round $primary;
    border-title-color: $text;
    border-title-style: bold;
    padding: 1 2;
}
.dialog.danger { border: round $warning; }
.dialog .body { height: auto; margin-bottom: 1; }
.dialog .buttons { height: 3; align-horizontal: right; }
.dialog .buttons Button { margin-left: 2; min-width: 14; }
.dialog .hint { color: $text-muted; height: auto; }
#find-box { width: 96; height: 80%; }
#find-results { height: 1fr; margin-top: 1; border: none; background: transparent; padding: 0; }
#find-status { height: 1; color: $text-muted; margin-top: 1; }
#help-box { width: 78; }
#help-scroll { height: auto; max-height: 100%; }
"""


def _post(screen: Any, fn: Any, *args: Any) -> None:
    try:
        screen.app.call_from_thread(fn, *args)
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------- confirm


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n,escape,q", "cancel", "No", show=False),
        Binding("left,right", "toggle_focus", show=False),
    ]

    def __init__(
        self,
        title: str,
        body: Any,
        confirm_label: str = "Restore",
        danger: bool = True,
        hint: str = "y/enter confirm  ·  n/esc cancel",
    ) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._confirm_label = confirm_label
        self._danger = danger
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog danger" if self._danger else "dialog") as box:
            box.border_title = f" {self._title} "
            yield Static(self._body, classes="body", id="confirm-body")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    self._confirm_label,
                    id="ok",
                    variant="warning" if self._danger else "primary",
                )
            yield Static(self._hint, classes="hint")

    def on_mount(self) -> None:
        self.query_one("#ok", Button).focus()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _no(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        if not self.query_one("#ok", Button).disabled:
            self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_toggle_focus(self) -> None:
        self.focus_next()


# ---------------------------------------------------------------------------- restore whole snapshot


class RestoreAllScreen(ModalScreen["dict | None"]):
    """Summarise what restoring a whole snapshot would overwrite / create / delete."""

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n,escape,q", "cancel", "No", show=False),
        Binding("k", "toggle_keep", "Keep new files", show=False),
        Binding("left,right", "toggle_focus", show=False),
    ]

    def __init__(self, store: Any, snap: Any, pal: Palette) -> None:
        super().__init__()
        self.store = store
        self.snap = snap
        self.pal = pal
        self.keep_new = False
        self.preview: dict[str, list[str]] | None = None
        self.error: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog danger") as box:
            box.border_title = f" Restore whole project to {self.snap.short} "
            yield Static(Text("Comparing snapshot with the working tree…", style=self.pal.muted), classes="body", id="body")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Restore all", id="ok", variant="warning", disabled=True)
            yield Static("y/enter confirm  ·  k keep new files  ·  n/esc cancel", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()
        self._compute()

    @work(thread=True, exclusive=True, group="preview")
    def _compute(self) -> None:
        try:
            planner = getattr(self.store, "plan_restore", None)  # optional dry-run offered by the core
            if planner is not None:
                plan = planner(self.snap.id, None, True)
                prev = {
                    "overwrite": list(plan.overwrite),
                    "create": list(plan.create),
                    "delete": list(plan.delete),
                }
            else:
                prev = classify_worktree_diff(self.store.diff(self.snap.id, None))
            err = None
        except Exception as exc:
            prev, err = None, error_text(exc)
        if not get_current_worker().is_cancelled:
            _post(self, self._show, prev, err)

    def _show(self, prev: dict[str, list[str]] | None, err: str | None) -> None:
        self.preview, self.error = prev, err
        ok = self.query_one("#ok", Button)
        if prev is not None:
            ok.disabled = False
            ok.focus()
        self._paint()

    def _paint(self) -> None:
        pal = self.pal
        body = self.query_one("#body", Static)
        if self.error:
            body.update(Text(f"Could not compare with the working tree.\n{self.error}", style=pal.rem))
            return
        assert self.preview is not None
        ow, cr, de = self.preview["overwrite"], self.preview["create"], self.preview["delete"]
        glyph, attr, what = describe(self.snap)
        head = Text()
        head.append(f"{self.snap.short}", style=f"bold {pal.accent}")
        head.append(f"  {what}  ", style="bold")
        head.append(rel_time(self.snap.time), style=pal.muted)
        parts: list[Any] = [head, Text("")]
        if not (ow or cr or de):
            parts.append(Text("✓ The working tree already matches this snapshot. Nothing to do.", style=pal.add))
        else:
            tbl = Table.grid(padding=(0, 2))
            tbl.add_column(justify="right", no_wrap=True)
            tbl.add_column()
            tbl.add_row(Text(str(len(ow)), style=f"bold {pal.mod}"), Text("files will be overwritten", style=pal.mod))
            tbl.add_row(Text(str(len(cr)), style=f"bold {pal.add}"), Text("files will be re-created", style=pal.add))
            if self.keep_new:
                tbl.add_row(Text(str(len(de)), style=f"bold {pal.muted}"), Text("new files will be kept  (k to delete)", style=pal.muted))
            else:
                tbl.add_row(Text(str(len(de)), style=f"bold {pal.rem}"), Text("new files will be deleted  (k to keep)", style=pal.rem))
            parts.append(tbl)
            shown: list[tuple[str, str, str]] = []
            for sym, col, lst in (("~", pal.mod, ow), ("+", pal.add, cr), ("-", pal.muted if self.keep_new else pal.rem, de)):
                for p in summarize_paths(lst, 5):
                    shown.append((sym, col, p))
            if shown:
                parts.append(Text(""))
                for sym, col, p in shown[:14]:
                    line = Text()
                    line.append(f" {sym} ", style=f"bold {col}")
                    line.append(p, style=pal.fg)
                    parts.append(line)
        parts.append(Text(""))
        parts.append(Text("A safety snapshot of the current state is taken first — press u afterwards to undo.", style=pal.muted))
        body.update(Group(*parts))

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss({"delete_extra": not self.keep_new})

    @on(Button.Pressed, "#cancel")
    def _no(self) -> None:
        self.dismiss(None)

    def action_confirm(self) -> None:
        if not self.query_one("#ok", Button).disabled:
            self.dismiss({"delete_extra": not self.keep_new})

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_toggle_keep(self) -> None:
        if self.preview is not None:
            self.keep_new = not self.keep_new
            self._paint()

    def action_toggle_focus(self) -> None:
        self.focus_next()


# ---------------------------------------------------------------------------- prompt


class PromptScreen(ModalScreen["str | None"]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, placeholder: str = "", hint: str = "enter confirm  ·  esc cancel") -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as box:
            box.border_title = f" {self._title} "
            yield Input(placeholder=self._placeholder, id="prompt-input")
            yield Static(self._hint, classes="hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------- find in history


class FindScreen(ModalScreen["tuple[Any, str] | None"]):
    """Search history for text ("when did this appear / vanish?") and jump to a hit."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+r", "toggle_regex", "Regex", show=False),
        Binding("down", "to_results", show=False),
        Binding("shift+tab", "to_input", show=False),
    ]

    def __init__(self, store: Any, pal: Palette) -> None:
        super().__init__()
        self.store = store
        self.pal = pal
        self.regex = False
        self.hits: list[tuple[Any, str, str]] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="find-box") as box:
            box.border_title = " Find in history "
            yield Input(placeholder="text to look for…  (when did it appear / vanish?)", id="find-input")
            yield OptionList(id="find-results")
            yield Static("", id="find-status")
            yield Static(self._hint(), classes="hint", id="find-hint")

    def _hint(self) -> str:
        mode = "regex ON" if self.regex else "regex off"
        return f"enter search / jump  ·  ↓ results  ·  ctrl+r {mode}  ·  esc close"

    def on_mount(self) -> None:
        self.query_one("#find-input", Input).focus()
        self.query_one("#find-status", Static).update(Text("Type something and press enter.", style=self.pal.muted))

    @on(Input.Submitted, "#find-input")
    def _submit(self, event: Input.Submitted) -> None:
        event.stop()
        q = event.value
        if not q.strip():
            return
        self.query_one("#find-status", Static).update(Text("Searching history…", style=self.pal.accent))
        self.query_one("#find-results", OptionList).clear_options()
        self._search(q, self.regex)

    @work(thread=True, exclusive=True, group="find")
    def _search(self, q: str, regex: bool) -> None:
        try:
            hits = self.store.grep_history(q, regex=regex)
            err = None
        except Exception as exc:
            hits, err = [], error_text(exc)
        if not get_current_worker().is_cancelled:
            _post(self, self._show, hits, err)

    def _show(self, hits: list[tuple[Any, str, str]], err: str | None) -> None:
        pal = self.pal
        self.hits = hits
        ol = self.query_one("#find-results", OptionList)
        ol.clear_options()
        status = self.query_one("#find-status", Static)
        if err:
            status.update(Text(err, style=pal.rem))
            return
        if not hits:
            status.update(Text("No snapshot ever added or removed that text.", style=pal.mod))
            return
        opts = []
        for idx, (snap, path, kind) in enumerate(hits):
            row = Table.grid(expand=True, padding=(0, 1, 0, 0))
            row.add_column(width=7, no_wrap=True)
            row.add_column(width=8, justify="right", no_wrap=True)
            row.add_column(width=7, no_wrap=True)
            row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
            added = kind == "added"
            row.add_row(
                Text("+ added" if added else "- gone", style=f"bold {pal.add if added else pal.rem}"),
                Text(rel_time(snap.time), style=pal.muted),
                Text(snap.short, style=pal.accent),
                Text(path),
            )
            opts.append(Option(row, id=f"hit{idx}"))
        ol.add_options(opts)
        ol.highlighted = 0
        status.update(Text(f"{len(hits)} hit{'s' if len(hits) != 1 else ''} — enter to jump to a snapshot", style=pal.muted))
        ol.focus()

    @on(OptionList.OptionSelected, "#find-results")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        idx = int(str(event.option.id)[3:])
        snap, path, _kind = self.hits[idx]
        self.dismiss((snap, path))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_toggle_regex(self) -> None:
        self.regex = not self.regex
        self.query_one("#find-hint", Static).update(self._hint())

    def action_to_results(self) -> None:
        ol = self.query_one("#find-results", OptionList)
        if ol.option_count:
            ol.focus()

    def action_to_input(self) -> None:
        self.query_one("#find-input", Input).focus()


# ---------------------------------------------------------------------------- help

HELP_ROWS = [
    ("Navigate", [
        ("↑ ↓  j k", "move in the focused pane"),
        ("tab  shift+tab  1 2 3", "switch pane (Timeline / Changes / Diff)"),
        ("h l  ← →", "previous / next pane"),
        ("g  G", "jump to top / bottom"),
        ("pgup  pgdn  ctrl+u  ctrl+d", "scroll faster"),
    ]),
    ("Look", [
        ("w", "diff against the parent snapshot  ⇄  your working tree"),
        ("/", "filter the timeline by file path (esc clears)"),
        ("f", "find text in history: when did it appear / vanish?"),
        ("t", "toggle dark / light theme  (ctrl+p: command palette)"),
    ]),
    ("Rewind", [
        ("r", "restore the selected file to this snapshot"),
        ("R", "restore the whole project to this snapshot"),
        ("u", "undo the most recent change"),
        ("s", "take a manual snapshot (optional label)"),
    ]),
    ("Other", [
        ("?", "this help"),
        ("q", "quit"),
    ]),
]


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark,enter", "close", show=False)]

    def __init__(self, pal: Palette) -> None:
        super().__init__()
        self.pal = pal

    def compose(self) -> ComposeResult:
        pal = self.pal
        with Vertical(classes="dialog", id="help-box") as box:
            box.border_title = " ctrlz — keys "
            with VerticalScroll(id="help-scroll"):
                parts: list[Any] = []
                for title, rows in HELP_ROWS:
                    parts.append(Text(title.upper(), style=f"bold {pal.primary}"))
                    tbl = Table.grid(padding=(0, 2))
                    tbl.add_column(no_wrap=True, min_width=28)
                    tbl.add_column()
                    for k, d in rows:
                        tbl.add_row(Text(k, style=f"bold {pal.accent}"), Text(d))
                    parts.append(tbl)
                    parts.append(Text(""))
                parts.append(Text("Every restore snapshots the current state first, so nothing is ever lost:", style=pal.muted))
                parts.append(Text("after a restore or undo, press u to take it back.", style=pal.muted))
                yield Static(Group(*parts))
            yield Static("press any of esc / q / ? to close", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)
