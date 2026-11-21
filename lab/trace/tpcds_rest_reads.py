"""What test_execute_tpcds reads outside its front phase, at the pinned commit.

Runs the whole test once (every item) in the warm state with the attribute-read
instrumentation of lab/trace/attrread.py, switching the recording bucket at the
front-phase boundaries used by lab/trace/slices_trace.py (parse_one in the main
process, the call-item pickle round trip, sqlglot.executor.optimize). Everything
outside the front (class setup, planner, Python executor, DuckDB transpile and
comparison) is "rest". Also derives the names used by the rest-phase code objects
(from the slices map's rest coverage), for module globals.

Output (marshal): {"reads": [(owner, attr)], "keys": [(owner, attr, key)],
"allkeys": [(owner, attr)], "names": [name], "codes": {path: [(qualname, line)]}}

    python /src/lab/trace/tpcds_rest_reads.py /src/kit/sample/tests.json /src/maps/tpcds_rest.marshal
"""

import functools
import gc
import json
import marshal
import os
import re
import sys
import types

sys.path.insert(0, "/src/lab/trace")
sys.path.insert(0, "/src")

import unittest  # noqa: F401,E402  (before sqlglot)

from br import inlinepool, slices, statediff, unitrun  # noqa: E402

import attrread  # noqa: E402

ROOT = "/opt/sqlglot"
TPCDS = slices.TPCDS_TEST
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    out = sys.argv[2]
    row = next(t for t in tests if t["id"] == TPCDS)
    warm = unitrun.build_warm_state(tests, ROOT)
    inlinepool.install([m for n, m in list(sys.modules.items()) if n.startswith("tests") and m is not None])
    slices.duckdb_single_thread()
    inst = attrread.Instrumenter(statediff.key_repr)
    inst.instrument()
    inst.install_import_hook()
    inst.reset()
    gc.collect()

    buckets = {"front": [set(), set()], "rest": [set(), set()]}

    def flush(name):
        reads, keyreads, _errors = inst.collect()
        inst.reset()
        buckets[name][0].update((o, a) for o, a, _via in reads)
        buckets[name][1].update(keyreads)

    import sqlglot.executor as ex
    import tests.test_executor as te

    state = {"depth": 0, "load_front": False}

    def enter():
        if state["depth"] == 0:
            flush("rest")
        state["depth"] += 1

    def leave():
        state["depth"] -= 1
        if state["depth"] == 0:
            flush("front")

    def wrap(fn):
        @functools.wraps(fn)
        def w(*a, **k):
            enter()
            try:
                return fn(*a, **k)
            finally:
                leave()

        return w

    ex.optimize = wrap(ex.optimize)
    te.parse_one = wrap(te.parse_one)
    real_dumps, real_loads = inlinepool._dumps, inlinepool._loads

    def dumps(obj):
        if type(obj) is tuple and len(obj) == 3 and getattr(obj[0], "__name__", None) == "mp_execute":
            enter()
            try:
                result = real_dumps(obj)
            finally:
                leave()
            state["load_front"] = True
            return result
        return real_dumps(obj)

    def loads(blob):
        if state["load_front"]:
            state["load_front"] = False
            enter()
            try:
                return real_loads(blob)
            finally:
                leave()
        return real_loads(blob)

    inlinepool._dumps = dumps
    inlinepool._loads = loads

    verdicts = unitrun.run_tests([row], root=ROOT, warm=warm)
    flush("rest")
    if verdicts[0]["outcome"] != "pass":
        raise SystemExit(f"tpcds did not pass under instrumentation: {verdicts[0]}")

    rest_reads, rest_keys = buckets["rest"]
    keys = sorted(k for k in rest_keys if k[2] != "*")
    allkeys = sorted({(o, a) for o, a, k in rest_keys if k == "*"})

    # Names used by rest-phase code (module globals are not measured by attrread).
    smap = slices.load_default()
    cov = smap.get(TPCDS).coverage("rest")
    codes = {}
    names = set()
    for path, block in cov["files"].items():
        keys_here = set(block["c"]) | set(block["l"])
        codes[path] = sorted(keys_here)
        full = os.path.join(ROOT, path)
        if not path.endswith(".py") or not os.path.exists(full):
            continue
        top = compile(open(full, "rb").read(), full, "exec", dont_inherit=True)
        stack = [top]
        while stack:
            co = stack.pop()
            if (co.co_qualname, co.co_firstlineno) in keys_here:
                names.update(co.co_names)
                for c in co.co_consts:
                    if isinstance(c, str) and IDENT.match(c):
                        names.add(c)
            stack.extend(c for c in co.co_consts if isinstance(c, types.CodeType))
    data = {
        "reads": sorted(rest_reads),
        "keys": keys,
        "allkeys": allkeys,
        "names": sorted(names),
        "codes": codes,
        "front_reads": len(buckets["front"][0]),
    }
    with open(out, "wb") as f:
        marshal.dump(data, f)
    print(
        f"rest reads {len(rest_reads)} keys {len(keys)} allkeys {len(allkeys)} names {len(names)}"
        f" files {len(codes)}; front reads {len(buckets['front'][0])}"
    )


if __name__ == "__main__":
    main()
