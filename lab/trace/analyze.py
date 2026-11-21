"""Summarizes tracer outputs (host Python is fine: json/gzip only).

    python lab/trace/analyze.py [lab/out/trace]
"""

import collections
import glob
import gzip
import json
import os
import sys


def load(path):
    with open(path, "rb") as f:
        return json.loads(gzip.decompress(f.read()))


def line_keys(rec):
    return {(f, q, l) for f, d in rec["lines"].items() for q, ls in d.items() for l in ls}


def records(d):
    out = {}
    for p in sorted(glob.glob(os.path.join(d, "[0-9][0-9][0-9][0-9].json.gz"))):
        out[int(os.path.basename(p)[:4])] = p
    return out


def main(base):
    tests = json.load(open(os.path.join(os.path.dirname(__file__), "..", "..", "kit", "sample", "tests.json")))["tests"]
    secs = {t["number"]: t["seconds"] for t in tests}
    passes = {}
    for name in ("isolated", "inorder"):
        d = os.path.join(base, name)
        if os.path.isdir(d):
            passes[name] = records(d)
    stats = {}
    keysets = {}
    for name, recs in passes.items():
        codes, lines = set(), set()
        sizes, outcomes, notpass = [], collections.Counter(), []
        per_test = {}
        for n, p in recs.items():
            r = load(p)
            sizes.append(os.path.getsize(p))
            outcomes[r["outcome"]] += 1
            if r["outcome"] != "pass":
                notpass.append((n, r["id"], r["outcome"], (r.get("detail") or "")[:300]))
            cs = {tuple(c) for c in r["codes"]}
            ls = line_keys(r)
            codes |= cs
            lines |= ls
            per_test[n] = ls
        keysets[name] = per_test
        sizes.sort()
        stats[name] = {
            "records": len(recs),
            "outcomes": dict(outcomes),
            "distinct_code_keys": len(codes),
            "distinct_code_line_keys": len(lines),
            "test_line_pairs": sum(len(v) for v in per_test.values()),
            "gz_bytes_total": sum(sizes),
            "gz_bytes_median": sizes[len(sizes) // 2] if sizes else 0,
            "gz_bytes_max": sizes[-1] if sizes else 0,
            "not_pass": notpass,
        }
    if len(keysets) == 2:
        a, b = keysets["isolated"], keysets["inorder"]
        only_a = only_b = 0
        tests_diff = []
        for n in sorted(set(a) | set(b)):
            sa, sb = a.get(n, set()), b.get(n, set())
            da, db = len(sa - sb), len(sb - sa)
            only_a += da
            only_b += db
            if da or db:
                tests_diff.append((n, da, db))
        tests_diff.sort(key=lambda t: -(t[1] + t[2]))
        stats["diff"] = {
            "pairs_only_isolated": only_a,
            "pairs_only_inorder": only_b,
            "tests_with_diff": len(tests_diff),
            "tests_only_inorder_extra": sum(1 for t in tests_diff if t[2]),
            "top": tests_diff[:15],
        }
    calib = os.path.join(base, "calib", "calib.json")
    if os.path.exists(calib):
        c = json.load(open(calib))
        stats["calib"] = {k: c[k] for k in ("fork", "jobs", "pins", "wall", "totals")}
        stats["calib"]["warm"] = {k: c["warm"][k] for k in ("wall", "cpu_total", "cpu_warmup", "os_threads", "maxrss_kb")}
    json.dump(stats, sys.stdout, indent=1, default=str)
    print()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "out", "trace"))
