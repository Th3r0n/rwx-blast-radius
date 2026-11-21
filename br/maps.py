"""Coverage and data-read maps built from the pinned commit (see lab/build_maps.py).

Layout of maps/:
- index.marshal  marshal'd dict:
    tests        list of test ids; bit i of every bitset is tests[i]
    files        {path: (offset, length)} into cov.bin, one block per source file
    preload      {path: [(qualname, firstlineno), ...]} code objects that ran
                 while the warm state was built (imports, metaclasses, ...)
    preload_mods [path, ...] modules imported while the warm state was built
    reads        {path: bitset} tests that opened the file as data
    lists        {dir: bitset} tests that listed the directory
    subprocs     bitset of tests that started a subprocess
    tokens_blob  (offset, length) of the zlib'd identifier set in cov.bin
- cov.bin  zlib(marshal) blocks. A file block is a dict:
    codes   {(qualname, firstlineno): bitset_id}   tests that started the code object
    lines   {(qualname, firstlineno, line): bitset_id}  tests that ran that line in it
    sets    [bitset, ...]  deduplicated bitsets (Python ints)

Only the blocks of files a patch touches are decompressed.
"""

from __future__ import annotations

import marshal
import os
import zlib


class Maps:
    def __init__(self, directory: str):
        self.directory = directory
        with open(os.path.join(directory, "index.marshal"), "rb") as f:
            index = marshal.load(f)
        self.tests: list[str] = index["tests"]
        self._files: dict = index["files"]
        self.preload: dict = {path: set(codes) for path, codes in index["preload"].items()}
        self.preload_mods: set = set(index["preload_mods"])
        self.reads: dict = index["reads"]
        self.lists: dict = index["lists"]
        self.subprocs: int = index["subprocs"]
        self.cpu = index.get("cpu")  # per test: CPU seconds of one isolated run here, or None
        self._tokens_at = index.get("tokens_blob")
        self._index_names_at = index.get("names_blob")
        self._names = None
        self._blocks: dict = {}
        self._tokens = None
        self._cov = None

    def _read(self, offset: int, length: int) -> bytes:
        if self._cov is None:
            self._cov = open(os.path.join(self.directory, "cov.bin"), "rb")
        self._cov.seek(offset)
        return zlib.decompress(self._cov.read(length))

    def has_file(self, path: str) -> bool:
        return path in self._files

    def block(self, path: str):
        block = self._blocks.get(path)
        if block is None:
            where = self._files.get(path)
            block = {"codes": {}, "lines": {}, "sets": []} if where is None else marshal.loads(self._read(*where))
            self._blocks[path] = block
        return block

    def code_tests(self, path: str, key) -> int:
        block = self.block(path)
        i = block["codes"].get(key)
        return 0 if i is None else block["sets"][i]

    def line_tests(self, path: str, key, lines) -> int:
        block = self.block(path)
        out = 0
        get = block["lines"].get
        sets = block["sets"]
        for line in lines:
            i = get((key[0], key[1], line))
            if i is not None:
                out |= sets[i]
        return out

    def file_tests(self, path: str) -> int:
        """Tests that started any code object of the file."""
        block = self.block(path)
        out = 0
        for i in set(block["codes"].values()):
            out |= block["sets"][i]
        return out

    def readers(self, path: str) -> int:
        """Tests that opened the file's content (a modification is visible to them)."""
        return self.reads.get(path, 0)

    def dir_listers(self, directory: str) -> int:
        """Tests that listed the directory or an ancestor (a new or removed name is visible)."""
        out = 0
        while True:
            out |= self.lists.get(directory, 0)
            if not directory:
                break
            directory = os.path.dirname(directory)
        return out

    def name_readers(self, name: str) -> int:
        """Tests that ran a code object naming `name` (attribute, global or string constant)."""
        if self._names is None:
            at = self._index_names_at
            self._names = marshal.loads(self._read(*at)) if at else {"names": {}, "sets": []}
        i = self._names["names"].get(name)
        return 0 if i is None else self._names["sets"][i]

    def tokens(self) -> frozenset:
        if self._tokens is None:
            self._tokens = frozenset(marshal.loads(self._read(*self._tokens_at))) if self._tokens_at else frozenset()
        return self._tokens
