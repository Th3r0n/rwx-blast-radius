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
    p.add_argument("--front", default="", help="front.marshal: front mode for test_execute_tpcds only")
    p.add_argument("--no-slice", action="store_true", help="front mode: skip the all-affected-items reference")
    p.add_argument("--patch-dir", default="", help="front mode: extra unlabeled patches (*.patch), set name 'extra'")
    a = p.parse_args(argv)
    front = slices.load_front(a.front) if a.front else None
    if a.front and front is None:
        raise SystemExit(f"cannot load {a.front}")
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
    sources = [(s, os.path.join(_REPO, "lab", "corpus", s, "patches")) for s in a.sets.split(",") if s]
    if a.patch_dir:
        sources.append(("extra", a.patch_dir))
    for set_name, pdir in sources:
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
                    if tid == slices.TPCDS_TEST:
                        row["rest_items"] = slices.affected_split(smap, tid, res.changes, res.data_changes)[1]
                if front is not None and tid != slices.TPCDS_TEST:
                    continue
                rows.append(row)
                if not hot:
                    continue
                job = verify.Job(cid, [by_id[tid]], dict(res.py_files),
                                 {pth: new for pth, _old, new in res.data_changes},
                                 cpu_limit=6 * t.seconds + 60, wall_limit=10 * t.seconds + 120)
                if front is not None:
                    if items:
                        plan.append((row, "front", job, items))
                        if not a.no_slice:
                            plan.append((row, "slice", job, items))
                    continue
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
            c0 = time.process_time()
            if kind == "front":
                rep = {}
                to_run = slices.plan_in_child(row["test"], items, row.get("rest_items"), front, smap, report=rep)
                out["ran_items"] = to_run
                out["report"] = rep
                out["front_cpu"] = rep.get("front_cpu", 0.0)
                if not to_run:
                    out.update(outcome="pass", detail=None, test_cpu=0.0, run_cpu=time.process_time() - c0)
                    dump(out)
                    return
            elif kind == "dropcheck":
                # The items the front proof dropped, run alone with the patch active and
                # without any front recomputation first: each must pass.
                slices.install_filter(row["test"], items, smap)
            elif items is not None:
                slices.install_filter(row["test"], items, smap)
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
        if kind in ("slice", "front") and out.get("outcome") == "pass" and (row["label"] == "fail"):
            log(f"DISAGREEMENT {row['cid']} {row['test']} items={row['items']}")
        if kind == "dropcheck" and out.get("outcome") != "pass":
            log(f"DROPCHECK {out.get('outcome')} {row['cid']} dropped={items} {str(out.get('detail'))[:300]}")

    order = sorted(plan, key=lambda w: -smap.get(w[0]["test"]).seconds)
    wall = pool.run(order, body_of, on_done, lambda w: w[2].wall_limit)
    if front is not None:
        # Phase 2: the dropped items of every front job, in fresh children.
        drop = []
        for row, kind, job, items in plan:
            if kind != "front":
                continue
            eq = ((row.get("front") or {}).get("report") or {}).get("equal") or []
            if eq:
                drop.append((row, "dropcheck", job, eq))
        log(f"dropcheck: {len(drop)} jobs")
        wall += pool.run(drop, body_of, on_done, lambda w: w[2].wall_limit)
    write_json(os.path.join(a.out, "rows.json"), rows)
    summary = summarize(rows, stats)
    if front is not None:
        summary["front"] = summarize_front(rows)
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
        ran = r["slice"] or r.get("front")
        sv = "pass" if r["items"] == [] else (ran or {}).get("outcome")
        fv = (r["full"] or {}).get("outcome")
        if r.get("front"):
            bt["front_items_run"] = bt.get("front_items_run", 0) + len(r["front"].get("ran_items") or [])
            bt["front_zero"] = bt.get("front_zero", 0) + (r["front"].get("ran_items") == [])
            bt["cpu_front"] = bt.get("cpu_front", 0.0) + r["front"]["cpu"]
        if r["full"] and r["items"] and ran:
            bt["cpu_slice"] += ran["cpu"]
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


def summarize_front(rows):
    """test_execute_tpcds front-proof numbers (rows of a --front run)."""
    s = {"pairs": 0, "sliced": 0, "zero_items": 0, "front_jobs": 0, "decided_by_front": 0,
         "items_total": 0, "items_affected": 0, "candidates": 0, "equal": 0, "items_run": 0,
         "cpu_front_check": 0.0, "cpu_front_child": 0.0, "cpu_slice_child": 0.0, "cpu_pairs": 0,
         "label_fail": 0, "unsound_verdict": [], "unsound_drop": [], "noverdict": [],
         "front_fail_label_pass": [], "front_vs_slice": {"same": 0, "front_pass_slice_fail": [], "front_fail_slice_pass": []}}
    for r in rows:
        s["pairs"] += 1
        if r["items"] is None:
            continue
        s["sliced"] += 1
        s["items_total"] += r["total"]
        s["items_affected"] += len(r["items"])
        if r["items"] == []:
            s["zero_items"] += 1
            if r["label"] == "fail":
                s["unsound_verdict"].append((r["set"], r["cid"], "zero items"))
            continue
        f = r.get("front")
        if not f:
            continue
        s["front_jobs"] += 1
        s["label_fail"] += r["label"] == "fail"
        rep = f.get("report") or {}
        s["candidates"] += len(rep.get("candidates") or [])
        s["equal"] += len(rep.get("equal") or [])
        ran = f.get("ran_items")
        if ran is not None:
            s["items_run"] += len(ran)
            s["decided_by_front"] += ran == []
        s["cpu_front_check"] += f.get("front_cpu") or 0.0
        out = f.get("outcome")
        if out not in ("pass", "fail", "skip"):
            s["noverdict"].append((r["set"], r["cid"], out))
        if out == "pass" and r["label"] == "fail":
            s["unsound_verdict"].append((r["set"], r["cid"], "front pass, label fail"))
        if out == "fail" and r["label"] == "pass":
            s["front_fail_label_pass"].append((r["set"], r["cid"]))
        d = r.get("dropcheck")
        if d is not None and d.get("outcome") != "pass":
            s["unsound_drop"].append((r["set"], r["cid"], d.get("outcome"), (d.get("detail") or "")[:300]))
        sl = r.get("slice")
        if sl:
            s["cpu_front_child"] += f["cpu"]
            s["cpu_slice_child"] += sl["cpu"]
            s["cpu_pairs"] += 1
            if sl.get("outcome") == out:
                s["front_vs_slice"]["same"] += 1
            elif out == "pass":
                s["front_vs_slice"]["front_pass_slice_fail"].append((r["set"], r["cid"], sl.get("outcome")))
            else:
                s["front_vs_slice"]["front_fail_slice_pass"].append((r["set"], r["cid"], out, sl.get("outcome")))
    return s


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
