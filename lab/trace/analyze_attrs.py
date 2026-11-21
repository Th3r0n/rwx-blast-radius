"""Summarizes the attrs pass (host Python is fine).

    python lab/trace/analyze_attrs.py [lab/out/trace/attrs]
"""

import collections
import glob
import gzip
import json
import os
import sys

CHECKS = [
    ("sqlglot.expressions.ddl:TriggerProperties", "arg_types", ["V:str:'constraint'"]),
    ("sqlglot.expressions.ddl:TriggerProperties", "required_args", []),
    ("sqlglot.dialects.snowflake:Snowflake.Tokenizer", "NUMERIC_ESCAPES", ["V:str:'u'"]),
]


def hist(counts):
    buckets = [(1, 1), (2, 10), (11, 100), (101, 500), (501, 1000), (1001, 10**9)]
    out = {}
    for lo, hi in buckets:
        out[f"{lo}-{hi}" if hi < 10**9 else f">{lo - 1}"] = sum(1 for c in counts if lo <= c <= hi)
    return out


def main(d):
    cell_readers = collections.defaultdict(set)
    key_readers = collections.defaultdict(set)
    outcomes = collections.Counter()
    sizes = []
    ids = {}
    cpu = 0.0
    for p in sorted(glob.glob(os.path.join(d, "[0-9][0-9][0-9][0-9].json.gz"))):
        sizes.append(os.path.getsize(p))
        with open(p, "rb") as f:
            r = json.loads(gzip.decompress(f.read()))
        n = r["n"]
        ids[n] = r["id"]
        cpu += r.get("cpu") or 0
        outcomes[r["outcome"]] += 1
        for owner, attr, _via in r["reads"]:
            cell_readers[(owner, attr)].add(n)
        for owner, attr, key in r["keyreads"]:
            key_readers[(owner, attr, key)].add(n)
    counts = [len(v) for v in cell_readers.values()]
    kcounts = [len(v) for k, v in key_readers.items() if k[2] != "*"]
    star = [len(v) for k, v in key_readers.items() if k[2] == "*"]
    res = {
        "records": len(sizes),
        "outcomes": dict(outcomes),
        "child_cpu_sum": cpu,
        "gz_bytes_total": sum(sizes),
        "gz_bytes_max": max(sizes) if sizes else 0,
        "cells_read": len(cell_readers),
        "readers_per_cell": hist(counts),
        "median_readers_per_cell": sorted(counts)[len(counts) // 2] if counts else 0,
        "key_cells_read": len(kcounts),
        "readers_per_key_cell": hist(kcounts),
        "star_cells": len(star),
        "readers_per_star_cell": hist(star),
        "top_cells": sorted(((len(v), k) for k, v in cell_readers.items()), reverse=True)[:10],
        "checks": {},
    }
    for owner, attr, keys in CHECKS:
        c = {"attr_readers": len(cell_readers.get((owner, attr), ())),
             "star_readers": len(key_readers.get((owner, attr, "*"), ()))}
        for k in keys:
            readers = sorted(key_readers.get((owner, attr, k), ()))
            c[f"key {k} readers"] = len(readers)
            c[f"key {k} tests"] = [ids[n] for n in readers][:20]
            c[f"key {k} or * readers"] = len(key_readers.get((owner, attr, k), set()) | key_readers.get((owner, attr, "*"), set()))
        res["checks"][f"{owner}.{attr}"] = c
    json.dump(res, sys.stdout, indent=1, default=str)
    print()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "out", "trace", "attrs"))
