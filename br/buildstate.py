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


def import_all_sqlglot(root):
    """Import every sqlglot module (not __main__); returns the names that failed."""
    import importlib
    import pkgutil

    if root not in sys.path:
        sys.path.insert(0, root)
    import sqlglot

    failed = []
    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            failed.append(info.name)
    return failed


def import_graph(root):
    """{module: sorted modules it imports} for every sqlglot module, from the source
    (module-level and function-level import statements, relative ones resolved)."""
    import ast

    graph = {}
    base = os.path.join(root, "sqlglot")
    for dirpath, _, names in os.walk(base):
        for name in names:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            mod = rel[:-3].replace("/", ".")
            is_pkg = mod.endswith(".__init__")
            if is_pkg:
                mod = mod[: -len(".__init__")]
            package = mod if is_pkg else mod.rpartition(".")[0]
            deps = set()
            try:
                tree = ast.parse(open(full, "rb").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        deps.add(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        parts = package.split(".")
                        up = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                        target = ".".join(up + ([node.module] if node.module else []))
                    else:
                        target = node.module or ""
                    deps.add(target)
                    for alias in node.names:
                        deps.add(f"{target}.{alias.name}")  # "from pkg import submodule"
            graph[mod] = sorted(d for d in deps if d.startswith("sqlglot"))
    return graph


def child(out, root, maps_dir, unordered_path=None, full=False):
    from . import statediff, unitrun

    if full:
        os.chdir(root)
        import_all_sqlglot(root)
    else:
        tests = _tests_from_maps(maps_dir)
        unitrun.build_warm_state(tests, root)
    unordered = frozenset()
    if unordered_path:
        with open(unordered_path, "rb") as f:
            unordered = frozenset(marshal.load(f))
    import gc

    gc.collect()
    prefix = ("sqlglot",) if full else ("sqlglot", "tests")
    snap = statediff.snapshot([m for m in sys.modules if m.startswith(prefix)], prefix=prefix, unordered=unordered)
    funcs = statediff.function_keys(list(snap), root)
    with open(out, "wb") as f:
        marshal.dump({"cells": snap, "funcs": funcs}, f)


def parent(out, root, maps_dir):
    warm = _combined(out, root, maps_dir, full=False)
    full = _combined(out, root, maps_dir, full=True)
    full["graph"] = import_graph(root)
    warm["full"] = full
    with open(out, "wb") as f:
        marshal.dump(warm, f)


def _combined(out, root, maps_dir, full):
    tmp = os.path.dirname(os.path.abspath(out))
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def run(seed, path, unordered=None):
        env = dict(os.environ, PYTHONHASHSEED=str(seed), PYTHONPATH=here)
        cmd = [sys.executable, "-m", "br.buildstate", "--full-child" if full else "--child", path, root, maps_dir]
        if unordered:
            cmd.append(unordered)
        subprocess.run(cmd, check=True, env=env, cwd=here, stdout=subprocess.DEVNULL)
        with open(path, "rb") as f:
            got = marshal.load(f)
        funcs_seen.update(got["funcs"])
        return got["cells"]

    funcs_seen = {}

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
    result = {
        "cells": again[0],
        "unstable": sorted(unstable, key=repr),
        "chaotic": sorted(chaotic, key=repr),
        "funcs": funcs_seen,  # pinned function behind each function-valued cell
    }
    for name in os.listdir(tmp):
        if name.startswith((".snap", ".usnap", ".unordered")):
            os.remove(os.path.join(tmp, name))
    print(
        f"state ({'all modules' if full else 'warm'}): {len(again[0])} cells, {len(unstable)} unstable,"
        f" {len(chaotic)} chaotic: {sorted(chaotic, key=repr)[:5]}"
    )
    return result


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] in ("--child", "--full-child"):
        child(args[1], args[2], args[3], args[4] if len(args) > 4 else None, full=args[0] == "--full-child")
    else:
        out = args[0]
        root = args[1] if len(args) > 1 else "/opt/sqlglot"
        maps_dir = args[2] if len(args) > 2 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maps")
        parent(out, root, maps_dir)
