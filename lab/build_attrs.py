"""Build maps/attrs.marshal from the attribute-read pass (lab/out/trace/attrs).

    owner   {(owner_class_id, attr): set_id}       read through any class (descriptor on owner)
    via     {(via_class_id, attr): set_id}         read through that class (or an instance of it)
    keys    {(owner_class_id, attr, key_repr): set_id}  key-level reads of a dict-valued attribute
    allkeys {(owner_class_id, attr): set_id}       whole-dict reads (iteration, len, copy, ...)
    sets    [bitset over the map's test list]
"""
import glob, gzip, json, marshal, sys
from collections import defaultdict

tests = json.load(open(sys.argv[1]))["tests"]
bit = {t["id"]: i for i, t in enumerate(tests)}
owner, via, keys, allkeys = (defaultdict(int) for _ in range(4))
seen = set()
for f in sorted(glob.glob(sys.argv[2] + "/*.json.gz")):
    r = json.loads(gzip.open(f, "rt").read())
    if r.get("id") not in bit:
        continue
    if r.get("outcome") != "pass" and r.get("outcome") != "skip":
        raise SystemExit(f"attr pass not clean for {r.get('id')}: {r.get('outcome')}")
    seen.add(r["id"])
    b = 1 << bit[r["id"]]
    for o, a, v in r["reads"]:
        owner[(o, a)] |= b
        via[(v, a)] |= b
    for o, a, k in r["keyreads"]:
        if k == "*":
            allkeys[(o, a)] |= b
        else:
            keys[(o, a, k)] |= b
if len(seen) != len(tests):
    raise SystemExit(f"attr records for {len(seen)}/{len(tests)} tests")
uniq, sets = {}, []
def sid(v):
    i = uniq.get(v)
    if i is None:
        i = uniq[v] = len(sets); sets.append(v)
    return i
out = {name: {k: sid(v) for k, v in d.items()} for name, d in (("owner", owner), ("via", via), ("keys", keys), ("allkeys", allkeys))}
import array, zlib
def enc(v):
    if bin(v).count("1") > 96:
        return v
    idx = array.array("H", [i for i in range(v.bit_length()) if v >> i & 1])
    return idx.tobytes()
out["sets"] = [enc(v) for v in sets]
data = zlib.compress(marshal.dumps(out), 9)
open(sys.argv[3], "wb").write(data)
print(f"owner {len(owner)} via {len(via)} keys {len(keys)} allkeys {len(allkeys)} sets {len(sets)} -> {len(data)/1e6:.2f} MB")
