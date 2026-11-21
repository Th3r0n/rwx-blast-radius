"""Build maps/ (see br/maps.py) from the raw tracer output of the pinned commit.

Run inside br-lab:
    python /src/lab/build_maps.py --tests /src/kit/sample/tests.json \
        --trace /src/lab/out/trace/isolated --trace /src/lab/out/trace/inorder \
        --preload /src/lab/out/trace/isolated/preload.json --root /opt/sqlglot --out /src/maps

Every trace directory holds one record per test (gzip JSON). The union of all
passes is taken, so a test is charged with everything it did in any pass.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import marshal
import os
import re
import zlib
from collections import defaultdict

IDENT = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
SOURCE_DIRS = ("sqlglot", "tests", "sqlglotc")


def records(directory):
    paths = sorted(glob.glob(os.path.join(directory, "*.json.gz")) + glob.glob(os.path.join(directory, "*.jsonl.gz")))
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            text = f.read()
        text = text.strip()
        if not text:
            continue
        if path.endswith(".jsonl.gz"):
            for line in text.split("\n"):
                if line.strip():
                    yield json.loads(line)
        else:
            obj = json.loads(text)
            if isinstance(obj, list):
                yield from obj
            else:
                yield obj


def rel(path, root):
    if path.startswith(root.rstrip("/") + "/"):
        return path[len(root.rstrip("/")) + 1 :]
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", required=True)
    ap.add_argument("--trace", action="append", required=True)
    ap.add_argument("--preload", required=True)
    ap.add_argument("--root", default="/opt/sqlglot")
    ap.add_argument("--out", required=True)
    ap.add_argument("--calib", help="calibration output (per-test CPU of an untraced isolated run)")
    args = ap.parse_args()

    tests = json.load(open(args.tests))["tests"]
    ids = [t["id"] for t in tests]
    bit = {test_id: i for i, test_id in enumerate(ids)}

    codes = defaultdict(lambda: defaultdict(int))  # file -> (q, l) -> bits
    lines = defaultdict(lambda: defaultdict(int))  # file -> (q, l, line) -> bits
    reads = defaultdict(int)
    lists = defaultdict(int)
    subprocs = 0
    seen = defaultdict(set)
    bad = []

    for directory in args.trace:
        for rec in records(directory):
            test_id = rec.get("id")
            if test_id not in bit:
                continue
            b = 1 << bit[test_id]
            seen[test_id].add(directory)
            if rec.get("outcome") not in ("pass", "skip"):
                bad.append((directory, test_id, rec.get("outcome")))
            for f, q, l in rec.get("codes", ()):
                codes[rel(f, args.root)][(q, l)] |= b
            for f, per_code in rec.get("lines", {}).items():
                f = rel(f, args.root)
                for key, lns in per_code.items():
                    q, _, l = key.rpartition("@")
                    l = int(l)
                    codes[f][(q, l)] |= b
                    for ln in lns:
                        lines[f][(q, l, ln)] |= b
            for entry in rec.get("opens", ()):
                if isinstance(entry, dict):
                    if entry.get("imp"):
                        continue
                    path = entry.get("path")
                else:
                    path = entry[0] if isinstance(entry, (list, tuple)) else entry
                if path:
                    reads[rel(path, args.root)] |= b
            for entry in rec.get("lists", ()):
                if isinstance(entry, dict):
                    if entry.get("imp"):
                        continue
                    path = entry.get("path")
                else:
                    path = entry[0] if isinstance(entry, (list, tuple)) else entry
                if path:
                    lists[rel(path, args.root).rstrip("/")] |= b
            if rec.get("subprocs"):
                subprocs |= b

    missing = [t for t in ids if not seen.get(t)]
    print(f"tests with records: {len(seen)}/{len(ids)}; missing {len(missing)}: {missing[:5]}")
    print(f"non-pass records: {len(bad)} {bad[:5]}")
    if len(missing) > 1:  # the skipped test is not in tests.json; nothing else may be missing
        raise SystemExit("refusing to build maps with missing tests")

    pre = json.load(open(args.preload))
    preload = {}
    preload_mods = set()
    raw = pre.get("codes", {})
    if isinstance(raw, list):  # [[file, qualname, firstlineno], ...]
        grouped = defaultdict(list)
        for f, q, l in raw:
            grouped[f].append([q, l])
        raw = grouped
    for f, entries in raw.items():
        f = rel(f, args.root)
        keys = []
        for e in entries:
            if isinstance(e, str):
                q, _, l = e.rpartition("@")
                keys.append((q, int(l)))
            else:
                keys.append((e[0], int(e[1])))
        preload[f] = keys
        if any(q == "<module>" for q, _ in keys):
            preload_mods.add(f)
    for f in pre.get("modules", ()):
        preload_mods.add(rel(f, args.root))

    os.makedirs(args.out, exist_ok=True)
    blob = bytearray()
    files = {}
    for f in sorted(set(codes) | set(lines)):
        uniq = {}
        sets = []

        def sid(v):
            i = uniq.get(v)
            if i is None:
                i = uniq[v] = len(sets)
                sets.append(v)
            return i

        block = {
            "codes": {k: sid(v) for k, v in codes[f].items()},
            "lines": {k: sid(v) for k, v in lines[f].items()},
            "sets": sets,
        }
        data = zlib.compress(marshal.dumps(block), 9)
        files[f] = (len(blob), len(data))
        blob += data

    # Name index: tests that ran a code object naming an identifier (in
    # co_names, as an identifier-like string constant, or a local of it).
    import types as _types
    ident = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    name_bits = defaultdict(int)
    for f, per_code in codes.items():
        full = os.path.join(args.root, f)
        if not f.endswith(".py") or not os.path.exists(full):
            continue
        try:
            top = compile(open(full, "rb").read(), full, "exec", dont_inherit=True)
        except SyntaxError:
            continue
        stack = [top]
        while stack:
            co = stack.pop()
            bits_here = per_code.get((co.co_qualname, co.co_firstlineno), 0)
            for c in co.co_consts:
                if isinstance(c, _types.CodeType):
                    stack.append(c)
                elif isinstance(c, str) and ident.match(c) and bits_here:
                    name_bits[c] |= bits_here
                elif isinstance(c, tuple) and bits_here:
                    for x in c:
                        if isinstance(x, str) and ident.match(x):
                            name_bits[x] |= bits_here
            if bits_here:
                for nm in co.co_names:
                    name_bits[nm] |= bits_here
    uniq = {}
    nsets = []
    names = {}
    for nm, v in name_bits.items():
        i = uniq.get(v)
        if i is None:
            i = uniq[v] = len(nsets)
            nsets.append(v)
        names[nm] = i
    nb = zlib.compress(marshal.dumps({"names": names, "sets": nsets}), 9)
    names_at = (len(blob), len(nb))
    blob += nb
    print(f"name index: {len(names)} names, {len(nsets)} distinct sets, {len(nb)/1e6:.2f} MB")

    tokens = set()
    for top in SOURCE_DIRS:
        for dirpath, _, names in os.walk(os.path.join(args.root, top)):
            for name in names:
                full = os.path.join(dirpath, name)
                try:
                    with open(full, "rb") as fh:
                        for m in IDENT.findall(fh.read()):
                            tokens.add(m.decode())
                except OSError:
                    pass
    tok = zlib.compress(marshal.dumps(sorted(tokens)), 9)
    tokens_at = (len(blob), len(tok))
    blob += tok

    cpu = None
    if args.calib and os.path.exists(args.calib):
        cal = json.load(open(args.calib))
        rows = cal.get("tests", cal) if isinstance(cal, dict) else cal
        by_id = {}
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict) and "id" in r:
                by_id[r["id"]] = float(r.get("cpu", r.get("test_cpu", 0.0)))
        cpu = [by_id.get(t, None) for t in ids]
        cpu = [c if c is not None else 1.0 for c in cpu]
        print(f"calibration: {len(by_id)} tests, total {sum(cpu):.1f}s cpu")
    index = {
        "cpu": cpu,
        "tests": ids,
        "files": files,
        "preload": preload,
        "preload_mods": sorted(preload_mods),
        "reads": dict(reads),
        "lists": dict(lists),
        "subprocs": subprocs,
        "tokens_blob": tokens_at,
        "names_blob": names_at,
    }
    with open(os.path.join(args.out, "cov.bin"), "wb") as f:
        f.write(blob)
    with open(os.path.join(args.out, "index.marshal"), "wb") as f:
        marshal.dump(index, f)
    print(
        f"files {len(files)}  code keys {sum(len(v) for v in codes.values())}  line keys {sum(len(v) for v in lines.values())}"
        f"  cov.bin {len(blob)/1e6:.2f} MB  reads {len(reads)} lists {len(lists)}  subproc tests {bin(subprocs).count('1')}"
        f"  preload files {len(preload)} preload modules {len(preload_mods)}"
    )


if __name__ == "__main__":
    main()
