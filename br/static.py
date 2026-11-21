"""Static, sound candidate selection for one patch.

The result is a superset of the tests that can fail: a bitset over the map's
test list, or ALL. Every rule here only *adds* tests; anything not understood
widens the selection. See docs/DECISIONS.md for the reasoning behind each rule.
"""

from __future__ import annotations

import os
import posixpath

from . import codediff, fixtures, patch as patchmod

ALL = -1  # sentinel: every test

PYTEST_CONFIG = {"pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py", ".pytest.ini"}
TEST_MODULE_NAMES = ("test_",)


class Result:
    def __init__(self):
        self.bits = 0
        self.all = False
        self.reasons: list[str] = []
        self.changes: list = []  # (path, codediff.Change) for later stages
        self.data_changes: list = []  # (path, old, new)
        self.py_files: dict = {}  # path -> (old_bytes, new_bytes)

    def add(self, bits: int, why: str):
        if bits:
            self.bits |= bits
        self.reasons.append(why)

    def widen(self, why: str):
        self.all = True
        self.reasons.append("ALL: " + why)


def _is_test_module(path: str) -> bool:
    base = posixpath.basename(path)
    return base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py")


def _module_name(path: str) -> str:
    stem = path[:-3] if path.endswith(".py") else path
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    return stem.replace("/", ".")


class StaticSelector:
    def __init__(self, maps, root: str):
        self.maps = maps
        self.root = root

    def read_pinned(self, path: str):
        full = os.path.join(self.root, path)
        try:
            with open(full, "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    # ------------------------------------------------------------------
    def select(self, patch_bytes: bytes) -> Result:
        res = Result()
        try:
            filepatches = patchmod.parse(patch_bytes)
        except Exception as exc:
            res.widen(f"patch not understood: {exc!r}")
            return res
        if not filepatches:
            res.widen("empty patch")
            return res
        for fp in filepatches:
            if res.all:
                break
            try:
                self._file(fp, res)
            except Exception as exc:  # never let an analysis bug drop tests
                res.widen(f"analysis error on {fp.old_path or fp.new_path}: {exc!r}")
        return res

    def _file(self, fp, res: Result):
        old_path, new_path = fp.old_path, fp.new_path
        path = new_path or old_path
        if fp.is_binary:
            return res.widen(f"binary change {path}")
        if fp.is_rename or (old_path and new_path and old_path != new_path):
            return res.widen(f"rename {old_path} -> {new_path}")
        if getattr(fp, "mode_changed", False) and not fp.is_new and not fp.is_deleted:
            return res.widen(f"mode change {path}")
        base = posixpath.basename(path)
        if base in PYTEST_CONFIG or path.startswith(".") and "/" not in path and base not in (".gitignore",):
            return res.widen(f"configuration file {path}")
        if fp.is_new:
            return self._created(fp, res)
        if fp.is_deleted:
            return res.widen(f"deleted file {path}")
        old = self.read_pinned(path)
        if old is None:
            return res.widen(f"modified file missing from the pinned tree {path}")
        new = patchmod.apply(fp, old)
        if new == old:
            return res.reasons.append(f"no-op {path}")
        # Anyone who reads the bytes sees the change.
        res.add(self.maps.readers(path), f"readers of {path}")
        if path.endswith(".py"):
            return self._python(path, old, new, res)
        if path.startswith("tests/fixtures/") and path.endswith(".sql"):
            res.data_changes.append((path, old, new))
            if fixtures.entries_equal(old, new):
                return res.reasons.append(f"fixture entries unchanged {path}")
            return res.add(self.maps.readers(path), f"fixture {path}")
        res.data_changes.append((path, old, new))
        # Other data files: only their readers can observe them.
        if path.startswith(("sqlglot/", "tests/", "sqlglotc/")) and not self.maps.reads.get(path):
            # A file inside the source trees that no traced test opened, e.g.
            # py.typed or a fixture of a skipped suite: still nothing reads it.
            return res.reasons.append(f"unread data file {path}")
        return None

    def _created(self, fp, res: Result):
        path = fp.new_path
        directory = posixpath.dirname(path)
        listers = self.maps.dir_listers(directory)
        base = posixpath.basename(path)
        if path.endswith(".py"):
            if _is_test_module(path) or base == "__init__.py":
                return res.widen(f"new collectable/package module {path}")
            stem = base[:-3]
            if stem in self.maps.tokens() or _module_name(path) in self.maps.tokens():
                return res.widen(f"new module whose name is used somewhere {path}")
            res.add(listers, f"listers of {directory}")
            return res.reasons.append(f"new module nothing names {path}")
        if base in PYTEST_CONFIG:
            return res.widen(f"new configuration {path}")
        res.add(listers, f"listers of {directory}")
        return None

    def _python(self, path: str, old: bytes, new: bytes, res: Result):
        full = os.path.join(self.root, path)
        res.py_files[path] = (old, new)
        try:
            old_code = codediff.compile_bytes(old, full)
        except SyntaxError:
            return res.widen(f"pinned {path} does not compile here")
        try:
            new_code = codediff.compile_bytes(new, full)
        except (SyntaxError, ValueError) as exc:
            return res.widen(f"patched {path} does not compile: {exc!r}")
        changes = codediff.diff_code(old_code, new_code)
        if not changes:
            return res.reasons.append(f"same code {path}")
        for change in changes:
            res.changes.append((path, change))
        if path.startswith("sqlglot/"):
            # Subprocess tests import the patched package themselves.
            res.add(self.maps.subprocs, "tests that start a Python subprocess")
        preloaded = path in self.maps.preload_mods
        if not preloaded and not self.maps.has_file(path):
            # No test ever imported or executed this file, and the patch
            # changes nothing that would make anyone import it.
            return res.reasons.append(f"code nobody runs {path}")
        preload = self.maps.preload.get(path, set())
        for change in changes:
            if change.import_time or change.kind != "function":
                if preloaded:
                    return res.widen(f"import-time change in preloaded module {path}:{change.key}")
                # Lazily imported module: only tests that imported or ran it.
                res.add(self.maps.file_tests(path), f"import-time change in lazy module {path}")
                continue
            if change.key in preload:
                return res.widen(f"function that runs at import time {path}:{change.key}")
            self._runtime(path, change, res)
        return None

    def _runtime(self, path, change, res):
        if change.whole:
            res.add(self.maps.code_tests(path, change.key), f"started {path}:{change.key}")
        else:
            res.add(
                self.maps.line_tests(path, change.key, change.lines),
                f"ran {path}:{change.key} lines {sorted(change.lines)}",
            )
