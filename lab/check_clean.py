"""Soundness check of clean mode (state diff) on labeled mutants (runs in br-lab).

For each labeled patch with import-time changes in preloaded modules: run the
clean child, get the changed cells; then check that every failing test is in
readers(cells) | runtime candidates, using the attribute map if present.
"""
import glob, json, marshal, os, sys
sys.path.insert(0, "/src")
from br.maps import Maps
from br.static import StaticSelector
from br import policy, verify

tests = json.load(open("/src/kit/sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}
maps = Maps("/src/maps")
bitnum = [num.get(i) for i in maps.tests]
sel = StaticSelector(maps, "/opt/sqlglot")
pristine = marshal.load(open("/src/lab/out/state.marshal", "rb"))
attrs = policy._load_attrs("/src/maps")
sets = sys.argv[1:] or ["sample", "dev1", "ood1", "targeted1"]

def nums(bits):
    out, i = set(), 0
    while bits:
        if bits & 1 and bitnum[i] is not None: out.add(bitnum[i])
        bits >>= 1; i += 1
    return out

n = bad = 0
for s in sets:
    for lab in sorted(glob.glob(f"/src/lab/corpus/{s}/labels/*.json")):
        L = json.load(open(lab))
        if L.get("status") != "ok": continue
        cid = os.path.basename(lab)[:-5]
        res = sel.select(open(f"/src/lab/corpus/{s}/patches/{cid}.patch", "rb").read())
        if not res.clean or getattr(res, "hard", False): continue
        n += 1
        out = {}
        def choose(cells, errs, funcs=None, dicts_now=None, subs_map=None, out=out):
            out["cells"] = cells; out["errs"] = errs
            r = policy.cell_readers(cells, maps, attrs, pristine["cells"])
            out["readers"] = None if r is None else nums(r)
            return [], []
        job = verify.Job(cid, [], dict(res.py_files), {p: nw for p, _o, nw in res.data_changes}, 60, 90)
        _, info = verify.run_clean_job(tests, job, pristine, choose)
        cells = [tuple(c) for c in (info.get("cells") or [])]
        out["cells"] = cells
        import ast
        funcs = {ast.literal_eval(k): (tuple(v) if v else None) for k, v in (info.get("funcs") or {}).items()}
        dnow = {tuple(c) for c in (info.get("dicts") or [])}
        r = policy.cell_readers(cells, maps, attrs, pristine["cells"], funcs, dnow, policy.dict_attrs(pristine["cells"]), info.get("subs") or {}, pristine.get("funcs"))
        out["readers"] = None if r is None else nums(r)
        if info.get("keep") is None: out["readers"] = None
        failing = set(L["failing"])
        base = nums(res.bits)
        rd = out.get("readers")
        cover = None if rd is None else (rd | base)
        ok = cover is None or failing <= cover
        if not ok or info.get("error"):
            bad += 1
        print(("OK  " if ok else "MISS"), s, cid, "fail", len(failing), "cells", len(out.get("cells") or []),
              "readers", "ALL" if rd is None else len(rd), "err", info.get("error"),
              "missed", sorted(failing - cover)[:8] if cover is not None else [], str(out.get("cells"))[:200])
print(f"checked {n}, problems {bad}")
