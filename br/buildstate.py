"""Build step: the pinned import-time state snapshot (maps/ is read-only at run time).

    python -m br.buildstate OUT            # parent: runs three children, combines
    python -m br.buildstate --child OUT    # child: warm state + snapshot

The snapshot is taken in the warm state the verifier uses (every test module
imported in collection order, unittest first). Cells whose digest differs
between processes with different hash seeds are *unstable* (sqlglot itself
builds some tuples from sets); they are digested order-insensitively. Cells
that still differ are *chaotic* (e.g. a loop variable leaked out of a loop over
a set) and are ignored unless the patch touches their module.
"""

from __future__ import annotations

import marshal
import os
import subprocess
import sys

import unittest  # noqa: F401  (must precede sqlglot)


def _tests_from_maps(maps_dir):
    with open(os.path.join(maps_dir, "index.marshal"), "rb") as f:
        index = marshal.load(f)
    return [{"number": i, "id": test_id, "file": test_id.split("::", 1)[0]} for i, test_id in enumerate(index["tests"])]


def child(out, root, maps_dir, unordered_path=None):
    from . import statediff, unitrun

    tests = _tests_from_maps(maps_dir)
    unitrun.build_warm_state(tests, root)
    unordered = frozenset()
    if unordered_path:
        with open(unordered_path, "rb") as f:
            unordered = frozenset(marshal.load(f))
    snap = statediff.snapshot([m for m in sys.modules if m.startswith("sqlglot")], unordered=unordered)
    with open(out, "wb") as f:
        marshal.dump(snap, f)


def parent(out, root, maps_dir):
    tmp = os.path.dirname(os.path.abspath(out))
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def run(seed, path, unordered=None):
        env = dict(os.environ, PYTHONHASHSEED=str(seed), PYTHONPATH=here)
        cmd = [sys.executable, "-m", "br.buildstate", "--child", path, root, maps_dir]
        if unordered:
            cmd.append(unordered)
        subprocess.run(cmd, check=True, env=env, cwd=here, stdout=subprocess.DEVNULL)
        with open(path, "rb") as f:
            return marshal.load(f)

    snaps = [run(seed, os.path.join(tmp, f".snap{seed}")) for seed in (0, 1, 2)]
    unstable = set()
    for other in snaps[1:]:
        for key in set(snaps[0]) | set(other):
            if snaps[0].get(key) != other.get(key):
                unstable.add(key)
    upath = os.path.join(tmp, ".unordered")
    with open(upath, "wb") as f:
        marshal.dump(sorted(unstable, key=repr), f)
    again = [run(seed, os.path.join(tmp, f".usnap{seed}"), upath) for seed in (0, 3)]
    chaotic = {key for key in set(again[0]) | set(again[1]) if again[0].get(key) != again[1].get(key)}
    result = {"cells": again[0], "unstable": sorted(unstable, key=repr), "chaotic": sorted(chaotic, key=repr)}
    with open(out, "wb") as f:
        marshal.dump(result, f)
    for name in os.listdir(tmp):
        if name.startswith((".snap", ".usnap", ".unordered")):
            os.remove(os.path.join(tmp, name))
    print(f"state: {len(again[0])} cells, {len(unstable)} unstable, {len(chaotic)} chaotic: {sorted(chaotic, key=repr)[:5]}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--child":
        child(args[1], args[2], args[3], args[4] if len(args) > 4 else None)
    else:
        out = args[0]
        root = args[1] if len(args) > 1 else "/opt/sqlglot"
        maps_dir = args[2] if len(args) > 2 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maps")
        parent(out, root, maps_dir)
