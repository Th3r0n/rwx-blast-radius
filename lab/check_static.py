"""Soundness check of the static selector on labeled mutants (runs in br-lab).

For every labeled patch: failing tests must be a subset of the static selection.
Prints misses (with reasons) and the simulated cost of the static answers.
"""
import glob, json, os, sys
sys.path.insert(0, "/src")
from br.maps import Maps
from br.static import StaticSelector
from br import pack

tests = json.load(open("/src/kit/sample/tests.json"))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
num = {t["id"]: t["number"] for t in tests}
maps = Maps("/src/maps")
bitnum = [num.get(i) for i in maps.tests]
sel = StaticSelector(maps, "/opt/sqlglot")
sets = sys.argv[1:] or ["sample", "dev1", "ood1", "targeted1", "state1"]
tot_cost = tot_oracle = n = misses = 0
for s in sets:
    for lab in sorted(glob.glob(f"/src/lab/corpus/{s}/labels/*.json")):
        L = json.load(open(lab))
        if L.get("status") != "ok":
            continue
        cid = os.path.basename(lab)[:-5]
        patch = open(f"/src/lab/corpus/{s}/patches/{cid}.patch", "rb").read()
        res = sel.select(patch)
        if res.all:
            chosen = set(ms)
        else:
            chosen = set(); b = res.bits | res.lazy_bits; i = 0
            while b:
                if b & 1 and bitnum[i] is not None: chosen.add(bitnum[i])
                b >>= 1; i += 1
        failing = set(L["failing"])
        missed = failing - chosen
        c = pack.pack([(x, ms[x]) for x in chosen])[0]; o = pack.pack([(x, ms[x]) for x in failing])[0]
        tot_cost += c; tot_oracle += o; n += 1
        if missed:
            misses += 1
            print("MISS", s, cid, "missed", sorted(missed)[:10], "reasons:", "; ".join(res.reasons)[:400])
print(f"checked {n} labeled patches, misses {misses}, static cost {tot_cost/1000:.1f}s vs oracle {tot_oracle/1000:.1f}s")
