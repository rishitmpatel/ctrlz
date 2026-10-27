"""End-to-end tests of the Textual UI against a real Store in a temp project."""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Button, Input, OptionList

from ctrlz.ui.app import CtrlzApp
from ctrlz.ui.screens import ConfirmScreen, FindScreen, HelpScreen, PromptScreen, RestoreAllScreen

from ui_helpers import SIZE, diff_text, make_app, ready, run, select, shown, until, write


def test_browse_navigate_and_diff(project):
    async def scenario():
        app = make_app(project.store)
        root = project.root
        async with app.run_test(size=SIZE) as pilot:
            await ready(app, pilot)
            tl = app.query_one("#timeline", OptionList)
            assert tl.option_count == 5
            # newest first
            assert app._view[0].id == project.snaps[-1].id
            assert app._sel_id == project.snaps[-1].id
            # changes of s5: logo.bin + src/app.py
            assert {c.path for c in app._changes} == {"logo.bin", "src/app.py"}
            assert app.query_one("#changes", OptionList).option_count == 2
            # header shows project path and snapshot count
            header = app.query_one("#header")
            text = str(header.render())
            assert "5" in text and "snapshots" in text
            assert str(project.root.name) in text
            # the first change is shown: logo.bin is binary
            assert "binary file changed" in diff_text(app)
            await ready(app, pilot)
            s1, s2, s3, s4, s5 = project.snaps
            await select(app, pilot, s2.id)  # a.txt: one -> two
            assert [c.path for c in app._changes] == ["a.txt"]
            d = diff_text(app)
            assert "-one" in d and "+two" in d
            # j/k and arrows both move
            await pilot.press("down")
            await until(pilot, lambda: app._sel_id == s1.id, what="down arrow")
            await pilot.press("k")
            await until(pilot, lambda: app._sel_id == s2.id, what="k")
            # tab goes to the Changes pane, then Diff, then back
            await pilot.press("tab")
            assert app.focused is not None and app.focused.id == "changes"
            await pilot.press("tab")
            assert app.focused.id == "diff"
            await pilot.press("tab")
            assert app.focused.id == "timeline"
            # first snapshot: whole files are additions
            await select(app, pilot, s1.id)
            assert any(c.status == "A" for c in app._changes)
            assert "+one" in diff_text(app)
            await ready(app, pilot)
            s4 = project.snaps[3]
            await select(app, pilot, s4.id)
            assert [(c.status, c.path) for c in app._changes] == [("D", "b.txt")]
            assert "-bee" in diff_text(app)
            # binary: size a -> b
            await select(app, pilot, project.snaps[4].id)
            await pilot.press("tab")  # Changes pane (logo.bin first)
            await until(pilot, lambda: app._cur is not None and app._cur.path == "logo.bin" and shown(app))
            await until(pilot, lambda: "binary file changed (size 150 B → 1.2 KB)" in diff_text(app), what="binary note")
            await ready(app, pilot)
            await select(app, pilot, project.snaps[1].id)  # a.txt in s2 = "two"
            assert "+two" in diff_text(app)
            await pilot.press("w")
            await until(pilot, lambda: app._vs_wt and "+three" in diff_text(app), what="vs working tree")
            assert "-two" in diff_text(app)
            await pilot.press("w")
            await until(pilot, lambda: not app._vs_wt and "+two" in diff_text(app), what="back to parent")

    run(scenario())


def test_restore_file_and_deleted_file(project):
    async def scenario():
        app = make_app(project.store)
        root = project.root
        async with app.run_test(size=SIZE) as pilot:
            await ready(app, pilot)
            s2 = project.snaps[1]
            await select(app, pilot, s2.id)
            assert (root / "a.txt").read_text().startswith("three")
            await pilot.press("r")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="confirm modal")
            # cancelling leaves disk alone
            await pilot.press("n")
            await until(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
            assert (root / "a.txt").read_text().startswith("three")
            # confirm
            await pilot.press("r")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")
            await until(pilot, lambda: (root / "a.txt").read_text().startswith("two"), what="restore on disk")
            # other files untouched, and a "restore-before" snapshot + toast exist
            assert (root / "c.txt").exists()
            await until(pilot, lambda: any("undo" in n.message.lower() for n in app._notifications), what="toast")
            assert any(n.title == "Restored" for n in app._notifications)
            await ready(app, pilot)
            await select(app, pilot, project.snaps[3].id)  # b.txt deleted here
            assert not (root / "b.txt").exists()
            await pilot.press("r")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")
            await until(pilot, lambda: (root / "b.txt").exists(), what="b.txt back")
            assert (root / "b.txt").read_text() == "bee\n"

    run(scenario())


def test_restore_whole_snapshot(project):
    async def scenario():
        app = make_app(project.store)
        root = project.root
        async with app.run_test(size=SIZE) as pilot:
            await ready(app, pilot)
            s1 = project.snaps[0]
            await select(app, pilot, s1.id)
            await pilot.press("R")
            await until(pilot, lambda: isinstance(app.screen, RestoreAllScreen), what="restore-all modal")
            modal = app.screen
            await until(pilot, lambda: modal.preview is not None, what="preview")
            # at s1: a.txt, logo.bin, src/app.py differ; b.txt is missing; c.txt is extra
            assert set(modal.preview["overwrite"]) == {"a.txt", "logo.bin", "src/app.py"}
            assert modal.preview["create"] == ["b.txt"]
            assert modal.preview["delete"] == ["c.txt"]
            assert not modal.query_one("#ok", Button).disabled
            # k keeps new files
            await pilot.press("k")
            assert modal.keep_new
            await pilot.press("y")
            await until(pilot, lambda: (root / "b.txt").exists(), what="restore all")
            assert (root / "a.txt").read_text().startswith("one")
            assert (root / "c.txt").exists()  # kept
            assert (root / "logo.bin").read_bytes() == b"\x00\x01\x02" * 50
            await ready(app, pilot)
            await select(app, pilot, project.snaps[0].id)
            await pilot.press("R")
            await until(pilot, lambda: isinstance(app.screen, RestoreAllScreen) and app.screen.preview is not None)
            await pilot.press("enter")  # focus is on the confirm button
            await until(pilot, lambda: not (root / "c.txt").exists(), what="c.txt deleted")
            assert (root / "b.txt").exists()

    run(scenario())


def test_undo_undo_of_undo_and_unsaved_edit(project):
    async def scenario():
        app = make_app(project.store)
        root = project.root
        before = (root / "src/app.py").read_text()
        assert "hello, world" in before
        async with app.run_test(size=SIZE) as pilot:
            await ready(app, pilot)
            await pilot.press("u")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="undo modal")
            await pilot.press("y")
            await until(pilot, lambda: "hello, world" not in (root / "src/app.py").read_text(), what="undo applied")
            assert len((root / "logo.bin").read_bytes()) == 150
            await until(pilot, lambda: any("Undid" in n.message for n in app._notifications), what="toast")
            assert any("u again" in n.message for n in app._notifications)
            # undo of undo brings it back
            await pilot.press("u")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), what="2nd undo modal")
            await pilot.press("y")
            await until(pilot, lambda: (root / "src/app.py").read_text() == before, what="undo of undo")
            await ready(app, pilot)
            write(root, "a.txt", "scribble\n")  # not snapshotted yet
            await pilot.press("u")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")
            await until(pilot, lambda: (root / "a.txt").read_text().startswith("three"), what="undo")

    run(scenario())


def test_find_in_history_jump_no_results(project):
    async def scenario():
        app = make_app(project.store)
        root = project.root
        async with app.run_test(size=SIZE) as pilot:
            await ready(app, pilot)
            await pilot.press("f")
            await until(pilot, lambda: isinstance(app.screen, FindScreen), what="find modal")
            for ch in "bee":
                await pilot.press(ch)
            await pilot.press("enter")
            modal = app.screen
            await until(pilot, lambda: len(modal.hits) > 0, what="hits")
            kinds = {(h[0].id, h[1], h[2]) for h in modal.hits}
            assert (project.snaps[0].id, "b.txt", "added") in kinds
            assert (project.snaps[3].id, "b.txt", "removed") in kinds
            # results list is focused: pick the first hit (newest = removal in s4)
            await pilot.press("enter")
            await until(pilot, lambda: not isinstance(app.screen, FindScreen), what="modal closed")
            await until(pilot, lambda: app._sel_id == project.snaps[3].id and app._cur is not None, what="jump")
            assert app._cur.path == "b.txt"
            assert app.focused.id == "changes"
            await ready(app, pilot)
            await pilot.press("f")
            await until(pilot, lambda: isinstance(app.screen, FindScreen))
            for ch in "zzzqqq":
                await pilot.press(ch)
            await pilot.press("enter")
            await until(pilot, lambda: "No snapshot ever" in str(app.screen.query_one("#find-status").render()), what="empty result message")
            assert app.screen.hits == []
            await pilot.press("escape")
            await until(pilot, lambda: not isinstance(app.screen, FindScreen))

    run(scenario())


def test_filter_timeline_by_path(project):
    async def scenario():
        app = make_app(project.store)
        async with app.run_test(size=SIZE) as pilot:
            await ready(app, pilot)
            await pilot.press("slash")
            await until(pilot, lambda: isinstance(app.focused, Input), what="filter input focus")
            for ch in "a.txt":
                await pilot.press(ch)
            # a.txt is touched by s1, s2, s3 only
            await until(pilot, lambda: [s.id for s in app._view] == [project.snaps[i].id for i in (2, 1, 0)], what="filter result")
            assert app.query_one("#timeline", OptionList).option_count == 3
            # enter keeps the filter and returns focus to the timeline
            await pilot.press("enter")
            assert app.focused.id == "timeline"
            assert app._filter == "a.txt"
            # esc clears
            await pilot.press("escape")
            await until(pilot, lambda: len(app._view) == 5, what="filter cleared")
            assert app._filter == ""

    run(scenario())


def test_live_refresh_prepends_without_losing_selection(project):
    async def scenario():
        app = make_app(project.store, poll_interval=0.1)
        root = project.root
        async with app.run_test(size=SIZE) as pilot:
            await ready(app, pilot)
            s2 = project.snaps[1]
            await select(app, pilot, s2.id)
            write(root, "new.txt", "hi\n")
            new = project.store.snap("watch")
            assert new is not None
            await until(pilot, lambda: app._view and app._view[0].id == new.id, what="new snapshot prepended")
            assert app.query_one("#timeline", OptionList).option_count == 6
            assert app._sel_id == s2.id  # selection kept
            assert app.query_one("#timeline", OptionList).highlighted == 5 - 1  # s2 now index 4
            assert app._unseen == 1

    run(scenario())


def test_manual_snapshot_with_label(project):
    async def scenario():
        app = make_app(project.store)
        root = project.root
        async with app.run_test(size=SIZE) as pilot:
            await ready(app, pilot)
            write(root, "d.txt", "dee\n")
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PromptScreen))
            for ch in "mine":
                await pilot.press(ch)
            await pilot.press("enter")
            await until(pilot, lambda: len(app._snaps) == 6, what="snapshot taken")
            assert app._snaps[0].label == "mine" and app._snaps[0].trigger == "manual"
            await until(pilot, lambda: app._sel_id == app._snaps[0].id, what="jumped to newest")
            # again with nothing changed: toast, no new snapshot
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PromptScreen))
            await pilot.press("enter")
            await until(pilot, lambda: any("Nothing changed" in n.message for n in app._notifications), what="no-op toast")
            assert len(app._snaps) == 6

    run(scenario())


def test_help_and_modal_blocks_global_keys(project):
    async def scenario():
        app = make_app(project.store)
        async with app.run_test(size=SIZE) as pilot:
            await ready(app, pilot)
            await pilot.press("question_mark")
            await until(pilot, lambda: isinstance(app.screen, HelpScreen), what="help")
            await pilot.press("q")  # closes help, must not quit the app
            await until(pilot, lambda: not isinstance(app.screen, HelpScreen))
            assert app.is_running
            await select(app, pilot, project.snaps[1].id)  # a.txt differs from disk -> modal
            await pilot.press("r")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("u")  # ignored inside the modal
            await pilot.pause(0.1)
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("escape")
            await until(pilot, lambda: not isinstance(app.screen, ConfirmScreen))

    run(scenario())


def test_empty_store_and_quit(tmp_path: Path):
    from ctrlz.store import Store

    root = tmp_path / "empty"
    root.mkdir()
    store = Store.open(root, create=True)

    async def scenario():
        app = make_app(store)
        async with app.run_test(size=SIZE) as pilot:
            await until(pilot, lambda: not app._loading, what="initial load")
            await pilot.pause(0.1)
            assert app.query_one("#timeline", OptionList).option_count == 0
            await pilot.press("r")  # nothing selected: just a toast
            await pilot.press("R")
            await pilot.press("u")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ConfirmScreen)
            await pilot.press("q")
        assert not app.is_running

    run(scenario())


def test_light_theme_and_narrow_layout(project):
    async def scenario():
        app = make_app(project.store, theme="textual-light")
        async with app.run_test(size=(90, 40)) as pilot:
            await ready(app, pilot)
            assert not app.current_theme.dark
            assert app.has_class("narrow")
            await pilot.press("t")
            assert app.current_theme.dark
            await pilot.pause(0.2)
            assert shown(app)

    run(scenario())


def test_load_more_pages(project):
    async def scenario():
        app = make_app(project.store, page=2)
        async with app.run_test(size=SIZE) as pilot:
            await ready(app, pilot)
            # paging: more history is fetched as the cursor nears the end
            await until(pilot, lambda: len(app._snaps) == 5 and app._exhausted, what="all pages loaded")
            await pilot.press("G")
            await until(pilot, lambda: app._sel_id == project.snaps[0].id, what="oldest selected")
            # the first snapshot has no parent: its files are shown as additions
            await until(pilot, lambda: app._changes_for == project.snaps[0].id and shown(app))
            await pilot.press("tab")  # Changes pane
            await pilot.press("g")  # first file: a.txt
            await until(pilot, lambda: shown(app) and app._cur.path == "a.txt" and "+one" in diff_text(app), what="root diff")

    run(scenario())
