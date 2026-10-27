"""Unit tests for the pure rendering helpers (no Textual app needed)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ctrlz.ui.render import (
    MAX_DIFF_LINES,
    Palette,
    build_diff_doc,
    classify_worktree_diff,
    human_size,
    is_binary_diff,
    layout_diff,
    rel_time,
    synth_added_diff,
)

VARS = {
    "foreground": "#e0e0e0", "surface": "#1e1e1e", "primary": "#0178d4", "accent": "#fea62b",
    "success": "#4ebf71", "error": "#b93c5b", "warning": "#fea62b",
}

DIFF = """diff --git a/app.py b/app.py
index 111..222 100644
--- a/app.py
+++ b/app.py
@@ -1,4 +1,4 @@
 def main():
-    print("hello")
+    print("hello, world")
     return 0
 
\\ No newline at end of file
"""


def test_rel_time_buckets():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert rel_time(now - timedelta(seconds=12), now) == "12s ago"
    assert rel_time(now - timedelta(minutes=4, seconds=5), now) == "4m ago"
    assert rel_time(now - timedelta(hours=3), now) == "3h ago"
    assert rel_time(now - timedelta(days=2), now) == "2d ago"
    assert rel_time(now + timedelta(seconds=5), now) == "0s ago"  # clock skew never goes negative


def test_human_size():
    assert human_size(150) == "150 B"
    assert human_size(1234) == "1.2 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"


def test_parse_unified_diff():
    doc = build_diff_doc(DIFF, "app.py", Palette.from_vars(VARS, True))
    kinds = [l.kind for l in doc.lines]
    assert kinds == ["hunk", "ctx", "del", "add", "ctx", "ctx", "note"]
    assert (doc.n_add, doc.n_del) == (1, 1)
    d = next(l for l in doc.lines if l.kind == "del")
    a = next(l for l in doc.lines if l.kind == "add")
    assert (d.old, d.new, a.old, a.new) == (2, None, None, 2)
    assert d.code.plain == '    print("hello")'
    assert a.marks  # intra-line change is marked


def test_layout_pads_and_is_stable():
    pal = Palette.from_vars(VARS, False)
    doc = build_diff_doc(DIFF, "app.py", pal)
    text = layout_diff(doc, pal, 60)
    lines = text.plain.split("\n")
    changed = [l for l in lines if "hello" in l]
    assert len(changed) == 2 and all(len(l) >= 60 for l in changed)  # tinted lines span the width


def test_large_diff_is_truncated_with_notice():
    n = MAX_DIFF_LINES + 500
    body = "".join(f"+line {i}\n" for i in range(n))
    big = f"--- /dev/null\n+++ b/big.txt\n@@ -0,0 +1,{n} @@\n{body}"
    doc = build_diff_doc(big, "big.txt")
    assert len(doc.lines) <= MAX_DIFF_LINES + 1
    assert doc.notice and "truncated" in doc.notice
    assert "truncated" in layout_diff(doc, Palette.from_vars(VARS, True), 80).plain


def test_binary_detection_and_synth():
    assert is_binary_diff("diff --git a/x b/x\nBinary files a/x and b/x differ\n")
    assert not is_binary_diff(DIFF)
    assert synth_added_diff("a.txt", b"x\ny\n").endswith("+x\n+y\n")
    assert synth_added_diff("empty.txt", b"") == ""


def test_classify_worktree_diff():
    # snapshot -> working tree: "new file" exists only on disk (restore deletes it)
    text = (
        "diff --git a/mod.py b/mod.py\n--- a/mod.py\n+++ b/mod.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/extra.txt b/extra.txt\nnew file mode 100644\n--- /dev/null\n+++ b/extra.txt\n@@ -0,0 +1 @@\n+x\n"
        "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n"
    )
    r = classify_worktree_diff(text)
    assert r == {"overwrite": ["mod.py", "img.png"], "create": ["gone.txt"], "delete": ["extra.txt"]}
