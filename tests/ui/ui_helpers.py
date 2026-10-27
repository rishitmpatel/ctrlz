"""Shared helpers for the TUI tests."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ctrlz.store import Store
from ctrlz.ui.app import CtrlzApp

SIZE = (130, 38)


def run(coro):
    """Run an async scenario to completion (no pytest-asyncio needed)."""
    return asyncio.run(asyncio.wait_for(coro, timeout=25))


async def until(pilot, cond, timeout: float = 6.0, what: str = "condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        await pilot.pause(0.03)
        try:
            if cond():
                return
        except Exception:
            pass
    raise AssertionError(f"timed out waiting for {what}")


def shown(app: CtrlzApp) -> bool:
    """True when the diff pane shows the selected snapshot + file."""
    return app._shown is not None and app._cur is not None and app._shown[:2] == (app._sel_id, app._cur.path)


async def ready(app: CtrlzApp, pilot) -> None:
    """Wait until the first snapshot is selected and its diff is shown."""
    await until(
        pilot,
        lambda: app._sel is not None and app._cur is not None and shown(app),
        what="initial load",
    )


async def select(app: CtrlzApp, pilot, snap_id: str) -> None:
    """Move the timeline cursor to a snapshot using j/k keys and wait for its changes."""
    app.timeline.focus()
    await pilot.pause(0.02)
    ids = [s.id for s in app._view]
    target = ids.index(snap_id)
    cur = ids.index(app._sel_id)
    key = "j" if target > cur else "k"
    for _ in range(abs(target - cur)):
        await pilot.press(key)
    await until(
        pilot,
        lambda: app._sel_id == snap_id and app._changes_for == snap_id and app._cur is not None and shown(app),
        what=f"selection of {snap_id[:7]}",
    )
    await pilot.pause(0.1)


def write(root: Path, rel: str, text: str | bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text)


def diff_text(app: CtrlzApp) -> str:
    """Plain text of the lines currently shown in the diff pane."""
    if app._doc is None:
        return app._doc_note.plain if app._doc_note is not None else ""
    out = []
    for ln in app._doc.lines:
        if ln.code is not None:
            out.append(("+" if ln.kind == "add" else "-" if ln.kind == "del" else " ") + ln.code.plain)
        else:
            out.append(ln.raw)
    return "\n".join(out)


def make_app(store: Store, **kw) -> CtrlzApp:
    kw.setdefault("poll_interval", 0.15)
    return CtrlzApp(store, **kw)
