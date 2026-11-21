"""Synthetic edge cases for br.patch (run: python -I selftest/test_patch.py).

Every valid case is also cross-checked against `git apply` in a scratch
directory when git is on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from br import patch as bp  # noqa: E402

GIT = shutil.which("git")


def numbered(count: int, prefix: str = "l") -> bytes:
    return b"".join(b"%s%d\n" % (prefix.encode(), n) for n in range(1, count + 1))


def git_apply(files: dict[str, bytes | None], patch: bytes, paths) -> dict[str, bytes | None]:
    """Apply with `git apply` in a scratch dir and read back the given paths."""
    with tempfile.TemporaryDirectory() as tmp:
        for path, data in files.items():
            if data is not None:
                os.makedirs(os.path.dirname(os.path.join(tmp, path)) or tmp, exist_ok=True)
                with open(os.path.join(tmp, path), "wb") as handle:
                    handle.write(data)
        with open(os.path.join(tmp, ".patch"), "wb") as handle:
            handle.write(patch)
        result = subprocess.run(
            [GIT, "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "apply", ".patch"],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise AssertionError("git apply failed: " + result.stderr.decode(errors="replace"))
        out = {}
        for path in paths:
            full = os.path.join(tmp, path)
            out[path] = None
            if os.path.isfile(full):
                with open(full, "rb") as handle:
                    out[path] = handle.read()
        return out


class Case(unittest.TestCase):
    def check(self, files: dict[str, bytes | None], patch: bytes, expected: dict[str, bytes | None]) -> dict:
        """apply_all must give `expected` (and agree with git when available)."""
        result = bp.apply_all(patch, files.get)
        self.assertEqual({path: after for path, (_before, after) in result.items()}, expected)
        for path, (before, _after) in result.items():
            self.assertEqual(before, files.get(path))
        if GIT:
            paths = {**{p: None for p in expected}, **files}
            from_git = git_apply(files, patch, paths)
            for path in paths:
                self.assertEqual(from_git.get(path), expected.get(path, files.get(path)), path)
        return result

    def fails(self, files: dict[str, bytes | None], patch: bytes) -> None:
        with self.assertRaises(bp.PatchError):
            bp.apply_all(patch, files.get)


def header(path: str = "f.txt") -> bytes:
    p = path.encode()
    return b"diff --git a/%s b/%s\nindex 1234567..89abcde 100644\n--- a/%s\n+++ b/%s\n" % (p, p, p, p)


class TestApply(Case):
    def test_middle_change(self):
        patch = header() + b"@@ -3,7 +3,7 @@ def heading():\n l3\n l4\n l5\n-l6\n+L6\n l7\n l8\n l9\n"
        old = numbered(10)
        result = self.check({"f.txt": old}, patch, {"f.txt": old.replace(b"l6\n", b"L6\n")})
        fp = bp.parse(patch)[0]
        self.assertEqual((fp.removed_lines(), fp.added_lines(), fp.insertion_points()), ([6], [6], []))
        self.assertEqual(fp.hunks[0].heading, b" def heading():")
        self.assertEqual(fp.old_mode, 0o100644)
        self.assertTrue(bp.index_matches(fp, old, result["f.txt"][1]) is False)

    def test_old_without_newline(self):
        patch = header() + b"@@ -1,3 +1,3 @@\n a\n b\n-c\n\\ No newline at end of file\n+c\n"
        self.check({"f.txt": b"a\nb\nc"}, patch, {"f.txt": b"a\nb\nc\n"})

    def test_both_without_newline_context(self):
        patch = header() + b"@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n\\ No newline at end of file\n"
        self.check({"f.txt": b"a\nb\nc"}, patch, {"f.txt": b"a\nB\nc"})

    def test_new_without_newline(self):
        patch = header() + b"@@ -1,2 +1,2 @@\n a\n-b\n+b\n\\ No newline at end of file\n"
        self.check({"f.txt": b"a\nb\n"}, patch, {"f.txt": b"a\nb"})

    def test_both_without_newline_changed(self):
        patch = header() + b"@@ -1,2 +1,2 @@\n x\n-y\n\\ No newline at end of file\n+z\n\\ No newline at end of file\n"
        self.check({"f.txt": b"x\ny"}, patch, {"f.txt": b"x\nz"})

    def test_newline_mismatch_fails(self):
        patch = header() + b"@@ -1,2 +1,2 @@\n x\n-y\n+z\n"
        self.fails({"f.txt": b"x\ny"}, patch)
        patch = header() + b"@@ -1,2 +1,2 @@\n x\n-y\n\\ No newline at end of file\n+z\n"
        self.fails({"f.txt": b"x\ny\n"}, patch)

    def test_create(self):
        patch = (b"diff --git a/pkg/new.py b/pkg/new.py\nnew file mode 100644\nindex 0000000..3333333\n"
                 b"--- /dev/null\n+++ b/pkg/new.py\n@@ -0,0 +1,2 @@\n+one\n+two\n")
        self.check({}, patch, {"pkg/new.py": b"one\ntwo\n"})
        fp = bp.parse(patch)[0]
        self.assertTrue(fp.is_new and fp.old_path is None and fp.new_mode == 0o100644)
        self.assertEqual(fp.insertion_points(), [(0, None)])
        self.assertEqual(fp.touched_old_lines(), [])

    def test_create_without_newline_and_empty(self):
        patch = (b"diff --git a/solo b/solo\nnew file mode 100755\nindex 0000000..3333333\n"
                 b"--- /dev/null\n+++ b/solo\n@@ -0,0 +1 @@\n+solo\n\\ No newline at end of file\n"
                 b"diff --git a/empty b/empty\nnew file mode 100644\nindex 0000000..e69de29\n")
        result = self.check({}, patch, {"solo": b"solo", "empty": b""})
        fps = bp.parse(patch)
        self.assertEqual(fps[0].new_mode, 0o100755)
        self.assertTrue(bp.index_matches(fps[1], None, result["empty"][1]))

    def test_create_existing_fails(self):
        patch = (b"diff --git a/f.txt b/f.txt\nnew file mode 100644\nindex 0000000..3333333\n"
                 b"--- /dev/null\n+++ b/f.txt\n@@ -0,0 +1 @@\n+x\n")
        self.fails({"f.txt": b"there\n"}, patch)

    def test_delete(self):
        patch = (b"diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\nindex 4444444..0000000\n"
                 b"--- a/gone.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n"
                 b"diff --git a/one b/one\ndeleted file mode 100644\nindex 4444444..0000000\n"
                 b"--- a/one\n+++ /dev/null\n@@ -1 +0,0 @@\n-solo\n\\ No newline at end of file\n"
                 b"diff --git a/empty b/empty\ndeleted file mode 100644\nindex e69de29..0000000\n")
        self.check({"gone.txt": b"one\ntwo\n", "one": b"solo", "empty": b""}, patch,
                   {"gone.txt": None, "one": None, "empty": None})
        self.assertEqual(bp.parse(patch)[0].removed_lines(), [1, 2])

    def test_delete_partial_fails(self):
        patch = (b"diff --git a/g b/g\ndeleted file mode 100644\nindex 4444444..0000000\n"
                 b"--- a/g\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n")
        self.fails({"g": b"one\ntwo\nthree\n"}, patch)
        self.fails({}, patch)

    def test_rename_with_changes(self):
        patch = (b"diff --git a/src/old.py b/dst/new_name.py\nsimilarity index 80%\n"
                 b"rename from src/old.py\nrename to dst/new_name.py\nindex 5555555..6666666 100644\n"
                 b"--- a/src/old.py\n+++ b/dst/new_name.py\n@@ -1,4 +1,4 @@\n a\n-b\n+B\n c\n d\n")
        self.check({"src/old.py": b"a\nb\nc\nd\n"}, patch, {"src/old.py": None, "dst/new_name.py": b"a\nB\nc\nd\n"})
        fp = bp.parse(patch)[0]
        self.assertTrue(fp.is_rename and fp.similarity == 80)
        self.assertEqual((fp.old_path, fp.new_path), ("src/old.py", "dst/new_name.py"))

    def test_pure_rename_and_mode_change(self):
        patch = (b"diff --git a/a.py b/b.py\nsimilarity index 100%\nrename from a.py\nrename to b.py\n"
                 b"diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n")
        self.check({"a.py": b"x\n", "run.sh": b"echo\n"}, patch, {"a.py": None, "b.py": b"x\n", "run.sh": b"echo\n"})
        self.assertTrue(bp.parse(patch)[1].mode_changed)

    def test_rename_onto_existing_fails(self):
        patch = b"diff --git a/a.py b/b.py\nsimilarity index 100%\nrename from a.py\nrename to b.py\n"
        self.fails({"a.py": b"x\n", "b.py": b"y\n"}, patch)

    def test_form_feed(self):
        old = b"x\n\f\ny = 1\fz\nw\n"
        patch = header() + b"@@ -1,4 +1,4 @@\n x\n \f\n-y = 1\fz\n+y = 2\fz\n w\n"
        self.check({"f.txt": old}, patch, {"f.txt": b"x\n\f\ny = 2\fz\nw\n"})

    def test_carriage_return(self):
        old = b"a\r\nb\r\nc\r\np\rq\n"
        patch = header() + b"@@ -1,4 +1,4 @@\n a\r\n-b\r\n+B\r\n c\r\n p\rq\n"
        self.check({"f.txt": old}, patch, {"f.txt": b"a\r\nB\r\nc\r\np\rq\n"})
        wrong = header() + b"@@ -1,4 +1,4 @@\n a\n-b\n+B\n c\n p\rq\n"
        self.fails({"f.txt": old}, wrong)

    def test_start_and_end_of_file(self):
        old = numbered(10)
        start = header() + b"@@ -1,4 +1,4 @@\n-l1\n+L1\n l2\n l3\n l4\n"
        self.check({"f.txt": old}, start, {"f.txt": old.replace(b"l1\n", b"L1\n", 1)})
        end = header() + b"@@ -7,4 +7,4 @@\n l7\n l8\n l9\n-l10\n+L10\n"
        self.check({"f.txt": old}, end, {"f.txt": old.replace(b"l10\n", b"L10\n")})
        append = header() + b"@@ -8,3 +8,4 @@\n l8\n l9\n l10\n+l11\n"
        self.check({"f.txt": old}, append, {"f.txt": old + b"l11\n"})
        self.assertEqual(bp.parse(append)[0].insertion_points(), [(10, None)])
        prepend = header() + b"@@ -1,3 +1,4 @@\n+l0\n l1\n l2\n l3\n"
        self.check({"f.txt": old}, prepend, {"f.txt": b"l0\n" + old})
        self.assertEqual(bp.parse(prepend)[0].insertion_points(), [(0, 1)])
        self.assertEqual(bp.parse(prepend)[0].touched_old_lines(), [1])
        # No trailing context means the hunk must end at EOF, as git requires.
        self.fails({"f.txt": old + b"l11\n"}, end)

    def test_omitted_counts(self):
        patch = header() + b"@@ -1 +1 @@\n-only\n+changed\n"
        self.check({"f.txt": b"only\n"}, patch, {"f.txt": b"changed\n"})
        hunk = bp.parse(patch)[0].hunks[0]
        self.assertEqual((hunk.old_start, hunk.old_len, hunk.new_start, hunk.new_len), (1, 1, 1, 1))

    def test_multiple_hunks_and_files(self):
        old = numbered(20)
        patch = (header() + b"@@ -1,5 +1,5 @@\n l1\n-l2\n+L2\n l3\n l4\n l5\n"
                 b"@@ -15,6 +15,7 @@\n l15\n l16\n l17\n-l18\n+L18\n+extra\n l19\n l20\n"
                 + header("g.txt") + b"@@ -1,2 +1,3 @@\n a\n+between\n b\n")
        new = old.replace(b"l2\n", b"L2\n", 1).replace(b"l18\n", b"L18\nextra\n")
        self.check({"f.txt": old, "g.txt": b"a\nb\n"}, patch, {"f.txt": new, "g.txt": b"a\nbetween\nb\n"})
        fps = bp.parse(patch)
        self.assertEqual((fps[0].removed_lines(), fps[0].added_lines()), ([2, 18], [2, 18, 19]))
        self.assertEqual(fps[1].insertion_points(), [(1, 2)])
        self.assertEqual(fps[1].touched_old_lines(), [1, 2])

    def test_empty_context_line(self):
        patch = header() + b"@@ -1,3 +1,3 @@\n a\n\n-b\n+B\n"
        self.check({"f.txt": b"a\n\nb\n"}, patch, {"f.txt": b"a\n\nB\n"})

    def test_names_with_spaces_and_quotes(self):
        patch = (b"diff --git a/sp ace.txt b/sp ace.txt\nindex 1234567..89abcde 100644\n"
                 b"--- a/sp ace.txt\t\n+++ b/sp ace.txt\t\n@@ -1 +1 @@\n-a\n+b\n"
                 b'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\nindex 1234567..89abcde 100644\n'
                 b'--- "a/caf\\303\\251.txt"\n+++ "b/caf\\303\\251.txt"\n@@ -1 +1 @@\n-a\n+b\n')
        result = bp.apply_all(patch, {"sp ace.txt": b"a\n", "café.txt": b"a\n"}.get)
        self.assertEqual(result, {"sp ace.txt": (b"a\n", b"b\n"), "café.txt": (b"a\n", b"b\n")})

    def test_preamble_and_signature(self):
        body = header() + b"@@ -1 +1 @@\n-a\n+b\n"
        patch = (b"From 0123 Mon Sep 17 00:00:00 2001\nSubject: [PATCH] x\n\n---\n f.txt | 2 +-\n\n"
                 + body + b"-- \n2.43.0\n\n")
        self.check({"f.txt": b"a\n"}, patch, {"f.txt": b"b\n"})

    def test_binary(self):
        patch = (b"diff --git a/img.png b/img.png\nindex 1234567..89abcde 100644\n"
                 b"Binary files a/img.png and b/img.png differ\n")
        fp = bp.parse(patch)[0]
        self.assertTrue(fp.is_binary)
        self.fails({"img.png": b"\x89PNG"}, patch)
        literal = (b"diff --git a/b.bin b/b.bin\nnew file mode 100644\nindex 0000000..1111111\n"
                   b"GIT binary patch\nliteral 3\nKcmZ?g00961\n\nliteral 0\nHcmV?d00001\n\n")
        self.assertTrue(bp.parse(literal)[0].is_binary)


class TestMalformed(unittest.TestCase):
    def raises(self, patch: bytes, old: bytes | None = b"a\nb\nc\n") -> None:
        with self.assertRaises(bp.PatchError):
            fps = bp.parse(patch)
            for fp in fps:
                bp.apply(fp, old)

    def test_counts(self):
        self.raises(header() + b"@@ -1,3 +1,3 @@\n a\n-b\n+B\n")           # truncated
        self.raises(header() + b"@@ -1,2 +1,2 @@\n a\n-b\n+B\n c\n")       # extra line
        self.raises(header() + b"@@ -1,3 +1,2 @@\n a\n-b\n+B\n c\n")       # new count too small
        self.raises(header() + b"@@ -1,x +1 @@\n-a\n+b\n")
        self.raises(header() + b"@@ -1,3 +1,3 @@\n a\n b\n c\n")           # no change
        self.raises(header() + b"@@ -1 +1 @@\n-a\n+b")                     # no final newline
        self.raises(header() + b"@@ -1,3 +1,3 @@\n a\n*b\n c\n")           # bad tag

    def test_positions(self):
        self.raises(header() + b"@@ -2,2 +3,2 @@\n-b\n+B\n c\n")             # inconsistent new start
        self.raises(header() + b"@@ -1,2 +1,2 @@\n-a\n+A\n b\n@@ -2,2 +2,2 @@\n-b\n+B\n c\n")  # overlap
        self.raises(header() + b"@@ -0,1 +0,1 @@\n-a\n+A\n")
        self.raises(header() + b"@@ -1,0 +2 @@\n+x\n")                       # git cannot place it
        self.raises(header() + b"@@ -2,2 +2,2 @@\n-x\n+B\n c\n")             # context mismatch

    def test_headers(self):
        self.raises(b"--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n")       # not a git diff
        self.raises(b"")
        self.raises(b"diff --git a/f.txt b/g.txt\n--- a/f.txt\n+++ b/g.txt\n@@ -1 +1 @@\n-a\n+b\n")
        self.raises(b"diff --git a/f.txt b/f.txt\nindex 1234567..89abcde 100644\n")
        self.raises(b"diff --git a/f.txt b/f.txt\nweird header\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n")
        self.raises(b"diff --git a/f.txt b/f.txt\nold mode 100644\nnew mode 120000\n")
        self.raises(b"diff --git a/../x b/../x\n--- a/../x\n+++ b/../x\n@@ -1 +1 @@\n-a\n+b\n")
        self.raises(header() + b"@@ -1 +1 @@\n-a\n+b\ngarbage\n")
        self.raises(header() + b"@@ -1 +1 @@\n-a\n\\ No newline at end of file\n\\ No newline at end of file\n+b\n")
        self.raises(header().replace(b"--- a/f.txt", b"--- /dev/null") + b"@@ -1 +1 @@\n-a\n+b\n")


if __name__ == "__main__":
    print(f"git cross-check: {'on (' + GIT + ')' if GIT else 'off'}", file=sys.stderr)
    unittest.main()
