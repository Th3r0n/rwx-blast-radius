"""Soundness check of lazy mode on labeled mutants (runs in br-lab)."""
import ast, glob, json, marshal, os, sys
sys.path.insert(0, "/src")
from br.maps import Maps
from br.static import StaticSelector
from br import policy, verify

tests = json.load(open("/src/kit/sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}
maps = Maps("/src/maps")
bitnum = [num.get(i) for i in maps.tests]
sel = StaticSelector(maps, "/opt/sqlglot")
full = marshal.load(open("/src/lab/out/state.marshal", "rb"))["full"]
attrs = policy._load_attrs("/src/maps")
pd = policy.dict_attrs(full["cells"])
def nums(bits):
    out, i = set(), 0
    while bits:
        if bits & 1 and bitnum[i] is not None: out.add(bitnum[i])
        bits >>= 1; i += 1
    return out
n = bad = 0
for s in sys.argv[1:] or ["sample", "dev1", "ood1", "targeted1", "state1"]:
    for lab in sorted(glob.glob(f"/src/lab/corpus/{s}/labels/*.json")):
        L = json.load(open(lab))
        if L.get("status") != "ok": continue
        cid = os.path.basename(lab)[:-5]
        res = sel.select(open(f"/src/lab/corpus/{s}/patches/{cid}.patch", "rb").read())
        if not res.lazy or res.all: continue
        n += 1
        job = verify.Job(cid, [], dict(res.py_files), {p: nw for p, _o, nw in res.data_changes}, 20, 30)
        info = verify.run_lazy_diff(job, full)
        failing = set(L["failing"])
        if info.get("error") or not info.get("proof"):
            print("NOPROOF", s, cid, info.get("error")); continue
        cells = [tuple(c) for c in info["cells"]]
        funcs = {ast.literal_eval(k): (tuple(v) if v else None) for k, v in (info.get("funcs") or {}).items()}
        r = policy.cell_readers(cells, maps, attrs, full["cells"], funcs, {tuple(c) for c in info.get("dicts") or []}, pd, info.get("subs") or {}, full.get("funcs"))
        cover = None if r is None else nums(r | res.bits)
        ok = cover is None or failing <= cover
        bad += not ok
        print("OK  " if ok else "MISS", s, cid, "fail", len(failing), "static", len(nums(res.bits | res.lazy_bits)), "now", "ALL" if cover is None else len(cover),
              "missed", sorted(failing - cover)[:8] if cover is not None else [], str(cells)[:150])
print(f"checked {n}, problems {bad}")
