"""Build maps/slices.marshal (see br/slices.py) from lab/trace/slices_trace.py output.

    python lab/build_slices.py [--slices lab/out/slices] [--out maps/slices.marshal]
                               [--tests kit/sample/tests.json] [--report lab/out/slices/build.json]

Host or container, stdlib only. Per sliceable test T of discover.json:
  * every job record must exist and pass (zero, full, all, each item alone);
  * "all" (T filtered to every item) must have exactly the coverage of "full" (T
    unfiltered), i.e. the filter is transparent;
  * no subprocess may be started (its coverage would be invisible);
  * common = zero-item coverage, plus whatever the full/all runs executed that no
    single-item run did (code that only runs when items follow each other);
  * per item: the (file, code key, line) and started code keys of its single-item run
    (test_execute_tpcds: front and rest phases together; "rest" stored separately).
Tests failing a check are left out of the map (reported).
"""

from __future__ import annotations

import argparse
import gzip
import json
import marshal
import os
import sys
import zlib

TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def load(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def cov_of(rec, part=None):
    """(lines, codes): {(file, qualname, firstlineno, line)}, {(file, qualname, firstlineno)}."""
    src = rec if part is None else rec.get(part) or {"codes": [], "lines": {}}
    lines = set()
    for file, by_code in src["lines"].items():
        for qk, ls in by_code.items():
            q, _, fl = qk.rpartition("@")
            fl = int(fl)
            for line in ls:
                lines.add((file, q, fl, line))
    codes = {(c[0], c[1], c[2]) for c in src["codes"]}
    return lines, codes


def full_cov(rec, split):
    lines, codes = cov_of(rec)
    if split:
        fl, fc = cov_of(rec, "front")
        lines |= fl
        codes |= fc
    return lines, codes


def encode(item_lines, item_codes, common_lines, common_codes):
    """Compact {"sets", "files"} structure (sid -1 = common)."""
    sets = []
    sid_of = {}

    def sid(bits):
        s = sid_of.get(bits)
        if s is None:
            s = sid_of[bits] = len(sets)
            sets.append(bits)
        return s

    files = {}
    line_bits = {}
    for i, ls in enumerate(item_lines):
        bit = 1 << i
        for k in ls:
            if k not in common_lines:
                line_bits[k] = line_bits.get(k, 0) | bit
    code_bits = {}
    for i, cs in enumerate(item_codes):
        bit = 1 << i
        for k in cs:
            if k not in common_codes:
                code_bits[k] = code_bits.get(k, 0) | bit
    for (f, q, fl, line), bits in sorted(line_bits.items()):
        files.setdefault(f, {"c": {}, "l": {}})["l"].setdefault((q, fl), {})[line] = sid(bits)
    for (f, q, fl, line) in sorted(common_lines):
        files.setdefault(f, {"c": {}, "l": {}})["l"].setdefault((q, fl), {})[line] = -1
    for (f, q, fl), bits in sorted(code_bits.items()):
        files.setdefault(f, {"c": {}, "l": {}})["c"][(q, fl)] = sid(bits)
    for (f, q, fl) in sorted(common_codes):
        files.setdefault(f, {"c": {}, "l": {}})["c"][(q, fl)] = -1
    return {"sets": sets, "files": files}


def build_test(spec, trace_dir, calib):
    n = spec["n_items"]
    split = spec["id"] == TPCDS
    d = os.path.join(trace_dir, f"{spec['n']:04d}")
    problems = []
    recs = {}
    for job in ["zero", "full", "all"] + [f"i{k:04d}" for k in range(n)]:
        path = os.path.join(d, f"{job}.json.gz")
        if not os.path.exists(path):
            problems.append(f"missing {job}")
            continue
        recs[job] = r = load(path)
        if r["outcome"] != "pass":
            problems.append(f"{job}: {r['outcome']} {str(r.get('detail'))[:200]}")
        if r.get("subprocs"):
            problems.append(f"{job}: starts subprocesses")
    if problems:
        return None, {"problems": problems[:20]}
    zero_l, zero_c = full_cov(recs["zero"], split)
    full_l, full_c = full_cov(recs["full"], split)
    all_l, all_c = full_cov(recs["all"], split)
    if (full_l, full_c) != (all_l, all_c):
        return None, {"problems": [f"filter not transparent: full-all {len(full_l - all_l)} lines, all-full {len(all_l - full_l)} lines",
                                   sorted(full_l ^ all_l)[:20]]}
    item_l, item_c, rest_l, rest_c = [], [], [], []
    union_l, union_c = set(), set()
    for k in range(n):
        r = recs[f"i{k:04d}"]
        ls, cs = full_cov(r, split)
        item_l.append(ls)
        item_c.append(cs)
        union_l |= ls
        union_c |= cs
        if split:
            rl, rc = cov_of(r)
            rest_l.append(rl)
            rest_c.append(rc)
    seq_l = full_l - union_l - zero_l
    seq_c = full_c - union_c - zero_c
    common_l = zero_l | seq_l
    common_c = zero_c | seq_c
    opens = set()
    for r in recs.values():
        for o in r.get("opens", ()):
            if not o["imp"] and not o["path"].startswith(("/", "<")):
                opens.add(o["path"])
    cov = encode(item_l, item_c, common_l, common_c)
    rest = None
    if split:
        rest = encode(rest_l, rest_c, common_l, common_c)
    cpu_base = cpu_items = None
    if calib is not None and "zero" in calib and all(f"i{k:04d}" in calib for k in range(n)):
        cpu_base = round(calib["zero"]["cpu"], 4)
        cpu_items = [round(max(0.0, calib[f"i{k:04d}"]["cpu"] - calib["zero"]["cpu"]), 4) for k in range(n)]
    # Slicing potential: how many items a changed line reaches, over non-common lines.
    sizes = sorted(bin(b).count("1") for b in cov["sets"])
    entry = {
        "module": spec["module"], "cls": spec["cls"], "method": spec["method"],
        "sources": [tuple(s) for s in spec["sources"]], "n": n,
        "opens": sorted(opens), "cpu_base": cpu_base, "cpu_items": cpu_items,
        "seconds": spec["seconds"],
        "cov": zlib.compress(marshal.dumps(cov), 9),
        "rest": zlib.compress(marshal.dumps(rest), 9) if rest is not None else None,
    }
    item_only = union_l - common_l
    per_line = {}
    for ls in item_l:
        for k in ls:
            if k in item_only:
                per_line[k] = per_line.get(k, 0) + 1
    counts = sorted(per_line.values())
    info = {
        "n": n,
        "common_lines": len(common_l), "seq_only_lines": len(seq_l), "seq_only_codes": len(seq_c),
        "seq_only_sample": sorted(seq_l)[:10],
        "item_only_lines": len(item_only),
        "median_items_per_item_line": counts[len(counts) // 2] if counts else 0,
        "lines_in_all_items": sum(1 for c in counts if c == n),
        "distinct_sets": len(cov["sets"]), "set_sizes_median": sizes[len(sizes) // 2] if sizes else 0,
        "cov_bytes": len(entry["cov"]), "rest_bytes": len(entry["rest"] or b""),
        "opens": sorted(opens),
        "cpu_full_traced": recs["full"]["cpu"], "cpu_zero_traced": recs["zero"]["cpu"],
        "cpu_base": cpu_base, "cpu_items_sum": round(sum(cpu_items), 3) if cpu_items else None,
        "cpu_full_calib": calib.get("full", {}).get("cpu") if calib else None,
    }
    return entry, info


def main(argv=None):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--slices", default=os.path.join(here, "lab", "out", "slices"))
    ap.add_argument("--out", default=os.path.join(here, "maps", "slices.marshal"))
    ap.add_argument("--report", default=None)
    ap.add_argument("--min-seconds", type=float, default=0.0, help="leave out tests recorded faster than this")
    a = ap.parse_args(argv)
    with open(os.path.join(a.slices, "discover.json"), encoding="utf-8") as f:
        specs = json.load(f)["specs"]
    tests = {}
    report = {}
    for spec in specs:
        if spec["seconds"] < a.min_seconds:
            report[spec["id"]] = {"skipped": f"seconds {spec['seconds']} < {a.min_seconds}"}
            continue
        calib = None
        cpath = os.path.join(a.slices, "calib", f"{spec['n']:04d}.json")
        if os.path.exists(cpath):
            with open(cpath, encoding="utf-8") as f:
                calib = json.load(f)
        entry, info = build_test(spec, os.path.join(a.slices, "trace"), calib)
        report[spec["id"]] = info
        if entry is None:
            print(f"LEFT OUT {spec['id']}: {info['problems'][:3]}", file=sys.stderr)
            continue
        tests[spec["id"]] = entry
        print(f"{spec['n']} {spec['id'].split('::')[-1]}: n={info['n']} common={info['common_lines']} "
              f"seq_only={info['seq_only_lines']} item_lines={info['item_only_lines']} "
              f"median_items/line={info['median_items_per_item_line']} in_all={info['lines_in_all_items']} "
              f"sets={info['distinct_sets']} bytes={info['cov_bytes']}+{info['rest_bytes']} "
              f"cpu base={info['cpu_base']} items={info['cpu_items_sum']} full={info['cpu_full_calib']}",
              file=sys.stderr)
    data = marshal.dumps({"version": 1, "tests": tests})
    tmp = a.out + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, a.out)
    print(f"wrote {a.out}: {len(tests)} tests, {len(data)} bytes", file=sys.stderr)
    rpath = a.report or os.path.join(a.slices, "build.json")
    with open(rpath, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
