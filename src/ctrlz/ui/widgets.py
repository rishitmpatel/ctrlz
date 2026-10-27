"""Custom widgets for the ctrlz TUI."""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import OptionList, Static

from .render import Palette


class VimOptionList(OptionList):
    """OptionList with vim-style movement keys."""

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "first", "Top", show=False),
        Binding("G", "last", "Bottom", show=False),
        Binding("ctrl+d", "page_down", "Page down", show=False),
        Binding("ctrl+u", "page_up", "Page up", show=False),
    ]


class SnapList(VimOptionList):
    """The timeline."""


class ChangeList(VimOptionList):
    """Files changed by the selected snapshot."""


class DiffView(VerticalScroll, can_focus=True):
    """Scrollable, horizontally scrollable diff body."""

    BINDINGS = [
        Binding("j", "scroll_down", "Down", show=False),
        Binding("k", "scroll_up", "Up", show=False),
        Binding("g", "scroll_home", "Top", show=False),
        Binding("G", "scroll_end", "Bottom", show=False),
        Binding("ctrl+d", "page_down", "Page down", show=False),
        Binding("ctrl+u", "page_up", "Page up", show=False),
        Binding("space", "page_down", "Page down", show=False),
        Binding("H", "scroll_left", "Left", show=False),
        Binding("L", "scroll_right", "Right", show=False),
    ]

    relayout = None  # set by the app: called when the width changes

    def compose(self):
        yield Static("", id="diff-body")

    def on_resize(self, event) -> None:
        if self.relayout is not None:
            self.relayout()

    def show(self, content: Text | str, keep_scroll: bool = False) -> None:
        sy, sx = self.scroll_y, self.scroll_x
        self.query_one("#diff-body", Static).update(content)
        if keep_scroll:
            self.call_after_refresh(lambda: self.scroll_to(sx, sy, animate=False, force=True))
        else:
            self.scroll_to(0, 0, animate=False, force=True)


class HeaderBar(Static):
    """One-line header: brand, project path, snapshot count, store size."""

    def set_info(
        self,
        pal: Palette,
        root: str,
        count: int,
        size: str,
        latest: str,
        status: str = "",
        newer: int = 0,
    ) -> None:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(" ctrlz ", style=f"bold {pal.bg} on {pal.primary}")
        t.append("  ")
        t.append(root, style="bold")
        t.append("   ")
        t.append(f"{count:,}", style=f"bold {pal.accent}")
        t.append(" snapshots" if count != 1 else " snapshot", style=pal.muted)
        t.append("  ·  ", style=pal.faint)
        t.append(size, style=f"bold {pal.accent}")
        t.append(" on disk", style=pal.muted)
        if latest:
            t.append("  ·  ", style=pal.faint)
            t.append("latest ", style=pal.muted)
            t.append(latest)
        if newer:
            t.append("  ")
            t.append(f" ▲ {newer} new — g ", style=f"bold {pal.bg} on {pal.add}")
        if status:
            t.append("  ")
            t.append(f" {status} ", style=f"bold {pal.bg} on {pal.mod}")
        self.update(t)
