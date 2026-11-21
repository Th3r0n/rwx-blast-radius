"""Validate item slices against labels and full runs (runs inside the br-lab image).

    python /src/lab/trace/slices_validate.py --out /src/lab/out/slices/validate \
        [--sets sample,dev1,...] [--jobs 7] [--no-full] [--only-test N] [--limit K]

For every corpus patch (lab/corpus/<set>/patches/<id>.patch) the static selector
(maps/ via br.static.StaticSelector, as br/main.py calls it) gives the candidates;
for each sliceable candidate T (maps/slices.marshal) br.slices.affected() gives the
items the patch reaches. Then, in children forked from the runtime's warm state
(br.verify.Warm) with the patch made active by br.verify._activate (same as the
runtime's verification child), the inline pool installed and duckdb limited to one
thread:
  slice  T with br.slices.install_filter(T, items)   (not run when items == [])
  full   T unfiltered (reference; --no-full skips it)
Each verdict is compared with the label (failing numbers) when one exists and with
the full run. A disagreement = the slice says pass (or items == []) while the label
or the full run says fail. -> <out>/rows.json, <out>/summary.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import resource
import sys
import time

import unittest  # noqa: F401  (before sqlglot)

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO)
from br import inlinepool, slices, unitrun, verify  # noqa: E402
from br.maps import Maps  # noqa: E402
from br.static import StaticSelector  # noqa: E402

sys.path.insert(0, _HERE)
from slices_trace import Pool, log, read_log, write_json  # noqa: E402

sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") not in (_HERE, _REPO)]

ROOT = verify.ROOT


def hot_eligible(res, maps):
    for path, change in res.changes:
        loaded = path in maps.preload_mods
        if loaded and (change.import_time or change.kind != "function"):
            return False
        if loaded and change.key in maps.preload.get(path, ()):
            return False
    return True


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--sets", default="sample,dev1,targeted1,holdout1,ood1")
    p.add_argument("--tests", default=os.path.join(_REPO, "kit", "sample", "tests.json"))
    p.add_argument("--maps", default=os.path.join(_REPO, "maps"))
    p.add_argument("--slices", default=os.path.join(_REPO, "maps", "slices.marshal"))
    p.add_argument("--jobs", type=int, default=7)
    p.add_argument("--no-full", action="store_true")
    p.add_argument("--full-for", default="", help="comma test numbers: run the full reference only for these")
    p.add_argument("--only-test", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry", action="store_true")
    a = p.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    tests, _ = unitrun.load_tests_json(a.tests)
    by_id = {t["id"]: t for t in tests}
    number_of = {t["id"]: t["number"] for t in tests}
    maps = Maps(a.maps)
    selector = StaticSelector(maps, ROOT)
    smap = slices.SliceMap(a.slices)
    sliceable = set(smap.ids())
    if a.only_test:
        keep = {int(x) for x in a.only_test.split(",")}
        sliceable = {t for t in sliceable if number_of[t] in keep}
    full_for = {int(x) for x in a.full_for.split(",") if x}
    bit_of = {tid: i for i, tid in enumerate(maps.tests)}

    plan = []  # (row, job kind)
    rows = []
    stats = {"patches": 0, "all": 0, "no_sliceable_candidate": 0, "not_hot": 0, "pairs": 0}
    t0 = time.perf_counter()
    for set_name in a.sets.split(","):
        pdir = os.path.join(_REPO, "lab", "corpus", set_name, "patches")
        for path in sorted(glob.glob(os.path.join(pdir, "*.patch"))):
            cid = os.path.basename(path)[: -len(".patch")]
            if a.limit and stats["patches"] >= a.limit:
                break
            stats["patches"] += 1
            with open(path, "rb") as f:
                res = selector.select(f.read())
            label = None
            lpath = os.path.join(_REPO, "lab", "corpus", set_name, "labels", cid + ".json")
            if os.path.exists(lpath):
                with open(lpath) as f:
                    lab = json.load(f)
                if lab.get("status") == "ok":
                    label = set(lab["failing"])
            if res.all:
                stats["all"] += 1
                continue
            cands = [t for t in sliceable if res.bits >> bit_of[t] & 1] if True else []
            if not cands:
                stats["no_sliceable_candidate"] += 1
                continue
            hot = hot_eligible(res, maps)
            if not hot:
                stats["not_hot"] += 1
            for tid in sorted(cands):
                stats["pairs"] += 1
                items = slices.affected(smap, tid, res.changes, res.data_changes)
                t = smap.get(tid)
                n = number_of[tid]
                row = {"set": set_name, "cid": cid, "test": tid, "n": n, "total": t.n,
                       "items": items, "label": None if label is None else ("fail" if n in label else "pass"),
                       "hot": hot, "slice": None, "full": None, "est_cpu": None}
                if items is not None:
                    row["est_cpu"] = slices.estimate_cpu(smap, tid, items)
                rows.append(row)
                if not hot:
                    continue
                job = verify.Job(cid, [by_id[tid]], dict(res.py_files),
                                 {pth: new for pth, _old, new in res.data_changes},
                                 cpu_limit=6 * t.seconds + 60, wall_limit=10 * t.seconds + 120)
                if items:
                    plan.append((row, "slice", job, items))
                if not a.no_full and (not full_for or n in full_for) and items is not None:
                    plan.append((row, "full", job, None))
    log(f"static stage: {stats} in {time.perf_counter() - t0:.0f}s; {len(plan)} child jobs")
    if a.dry:
        write_json(os.path.join(a.out, "rows.json"), rows)
        return 0

    warm = verify.Warm(tests)
    warm.build()
    pool = Pool(a.jobs, os.path.join(a.out, "tmp"))
    tmp = pool.tmp

    def body_of(w):
        row, kind, job, items = w

        def body():
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (int(job.cpu_limit), int(job.cpu_limit) + 1))
            except Exception:
                pass
            gc_disable()
            out = {"proof": None}
            try:
                proof = verify._activate(warm, job)
                proof.pop("finder", None)
                out["proof"] = proof
            except Exception as exc:
                out["error"] = repr(exc)[:500]
                dump(out)
                return
            inlinepool.install([m for nm, m in list(sys.modules.items()) if nm.startswith("tests") and m is not None])
            slices.duckdb_single_thread()
            if items is not None:
                slices.install_filter(row["test"], items, smap)
            c0 = time.process_time()
            v = unitrun.run_tests([by_id[row["test"]]], root=ROOT, warm=warm.state)[0]
            out.update(outcome=v["outcome"], detail=(v["detail"] or "")[:1500] if v["outcome"] != "pass" else None,
                       test_cpu=v["cpu"], run_cpu=time.process_time() - c0)
            dump(out)

        def dump(out):
            with open(os.path.join(tmp, f"v-{row['cid']}-{row['n']}-{kind}.json"), "w") as f:
                json.dump(out, f)

        return body

    def on_done(w, info):
        row, kind, job, items = w
        path = os.path.join(tmp, f"v-{row['cid']}-{row['n']}-{kind}.json")
        out = None
        if os.path.exists(path):
            with open(path) as f:
                out = json.load(f)
            os.remove(path)
        if out is None:
            out = {"outcome": "timeout" if info["timed_out"] else "crash", "detail": read_log(info["log"])[-800:]}
        elif out.get("error"):
            out["outcome"] = "noproof"
        out["cpu"] = info["cpu"]
        row[kind] = out
        if kind == "slice" and out.get("outcome") == "pass" and (row["label"] == "fail"):
            log(f"DISAGREEMENT {row['cid']} {row['test']} items={row['items']}")

    order = sorted(plan, key=lambda w: -smap.get(w[0]["test"]).seconds)
    wall = pool.run(order, body_of, on_done, lambda w: w[2].wall_limit)
    write_json(os.path.join(a.out, "rows.json"), rows)
    summary = summarize(rows, stats)
    summary["wall"] = wall
    write_json(os.path.join(a.out, "summary.json"), summary)
    log(json.dumps(summary, indent=1)[:4000])
    return 0


def gc_disable():
    import gc

    gc.disable()


def summarize(rows, stats):
    s = dict(stats)
    s["sliceable_pairs"] = sum(1 for r in rows if r["items"] is not None)
    s["unsliceable_pairs"] = sum(1 for r in rows if r["items"] is None)
    s["zero_items"] = sum(1 for r in rows if r["items"] == [])
    s["by_test"] = {}
    dis = []
    cons = []
    for r in rows:
        bt = s["by_test"].setdefault(r["test"].split("::")[-1], {"pairs": 0, "sliced": 0, "zero": 0, "items_run": 0,
                                                                   "items_total": 0, "cpu_slice": 0.0, "cpu_full": 0.0,
                                                                   "cpu_pairs": 0})
        bt["pairs"] += 1
        if r["items"] is None:
            continue
        bt["sliced"] += 1
        bt["zero"] += r["items"] == []
        bt["items_run"] += len(r["items"])
        bt["items_total"] += r["total"]
        sv = "pass" if r["items"] == [] else (r["slice"] or {}).get("outcome")
        fv = (r["full"] or {}).get("outcome")
        if r["full"] and r["items"] and r["slice"]:
            bt["cpu_slice"] += r["slice"]["cpu"]
            bt["cpu_full"] += r["full"]["cpu"]
            bt["cpu_pairs"] += 1
        ref_fail = r["label"] == "fail" or fv == "fail"
        if sv in ("pass", "skip") and ref_fail:
            dis.append({k: r[k] for k in ("set", "cid", "test", "items", "label")} | {"full": fv, "slice": sv})
        if sv not in ("pass", "skip") and r["label"] == "pass" and fv != "fail":
            cons.append({k: r[k] for k in ("set", "cid", "test", "label")} | {"slice": sv, "full": fv,
                         "detail": ((r["slice"] or {}).get("detail") or "")[:300]})
    s["disagreements"] = dis
    s["conservative"] = cons[:50]
    s["conservative_count"] = len(cons)
    s["label_fail_pairs"] = sum(1 for r in rows if r["label"] == "fail")
    s["full_vs_label"] = {
        "full_fail_label_pass": sum(1 for r in rows if (r["full"] or {}).get("outcome") == "fail" and r["label"] == "pass"),
        "full_pass_label_fail": sum(1 for r in rows if (r["full"] or {}).get("outcome") == "pass" and r["label"] == "fail"),
    }
    return s


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
