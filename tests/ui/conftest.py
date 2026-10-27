from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("textual")

from ctrlz.store import Store  # noqa: E402

from ui_helpers import write  # noqa: E402


@pytest.fixture
def project(tmp_path: Path):
    """A plain folder with five real snapshots.

    s1 baseline : a.txt=one, b.txt, src/app.py, logo.bin (binary)
    s2          : a.txt=two
    s3          : a.txt=three + new c.txt
    s4          : b.txt deleted
    s5          : logo.bin changed, src/app.py edited
    """
    root = tmp_path / "proj"
    root.mkdir()
    store = Store.open(root, create=True)
    write(root, "a.txt", "one\nline2\nline3\n")
    write(root, "b.txt", "bee\n")
    write(root, "src/app.py", 'def main():\n    print("hello")\n\n\nif __name__ == "__main__":\n    main()\n')
    write(root, "logo.bin", b"\x00\x01\x02" * 50)
    s1 = store.snap("manual", label="baseline")
    write(root, "a.txt", "two\nline2\nline3\n")
    s2 = store.snap("hook", label="claude: Edit")
    write(root, "a.txt", "three\nline2\nline3\n")
    write(root, "c.txt", "see\n")
    s3 = store.snap("watch")
    (root / "b.txt").unlink()
    s4 = store.snap("hook", label="claude: Bash", command="rm b.txt")
    write(root, "logo.bin", b"\x00\x01\x02" * 400)
    write(root, "src/app.py", 'def main():\n    print("hello, world")\n    return 0\n\n\nif __name__ == "__main__":\n    main()\n')
    s5 = store.snap("hook", label="claude: Write")
    snaps = [s1, s2, s3, s4, s5]
    assert all(s is not None for s in snaps)
    return type("Project", (), {"root": root, "store": store, "snaps": snaps})()
