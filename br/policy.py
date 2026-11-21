"""Decide which candidates to prove passing by running them (verify-or-select).

Verifying costs CPU, which is score; selecting costs simulated seconds. A job
is worth running when its estimated CPU is clearly below what it saves if the
tests pass: the cost of the commit with them minus the cost without them
(packing slack included, so tests that fit beside a long selected test are
never worth verifying). Estimates are deterministic (calibrated costs, a fixed
machine factor), so the three scored runs take the same decisions.

A test is dropped only with a "pass" or "skip" verdict from a child that
proved the patch active. Everything else stays selected.
"""

from __future__ import annotations

import os
import time

from . import pack, verify

HEAVY_MS = 1000
SAFETY = 0.7  # verify only when CPU estimate <= SAFETY * gain


def _cost(numbers, ms):
    if not numbers:
        return 0
    return pack.pack([(n, ms[n]) for n in numbers])[0]


def _hot_eligible(res, maps):
    if res.all:
        return False
    for path, change in res.changes:
        loaded = path in maps.preload_mods
        if loaded and (change.import_time or change.kind != "function"):
            return False  # needs a clean import of the patched tree
        if loaded and change.key in maps.preload.get(path, ()):
            return False  # ran while the warm state was built
    return True


def refine(answers, statics, tests, ms, maps, number_of, deadline, log):
    factor = float(os.environ.get("BR_CPU_FACTOR", "2.5"))
    rows = {t["number"]: t for t in tests}
    bit_numbers = [number_of.get(test_id) for test_id in maps.tests]
    est = {}
    cpu_list = getattr(maps, "cpu", None)
    for i, n in enumerate(bit_numbers):
        if n is not None and cpu_list:
            est[n] = cpu_list[i]
    never = set()
    bits = maps.subprocs
    i = 0
    while bits:
        if bits & 1 and bit_numbers[i] is not None:
            never.add(bit_numbers[i])
        bits >>= 1
        i += 1
    for t in tests:
        if t["file"] == "tests/test_executor.py" or ms[t["number"]] >= HEAVY_MS:
            never.add(t["number"])

    plans = []
    for cid, res in statics.items():
        chosen = answers[cid]
        if not chosen or res.all or not _hot_eligible(res, maps):
            continue
        if not res.changes and res.data_changes:
            continue  # only fixture entries changed: their readers almost always fail
        light = [n for n in chosen if n not in never]
        if not light:
            continue
        keep = [n for n in chosen if n in never]
        gain_ms = _cost(chosen, ms) - _cost(keep, ms)
        cpu = sum(est.get(n, ms[n] / 1000.0) for n in light) * factor
        if cpu * 1000 > SAFETY * gain_ms:
            log(cid, f"not verified: est {cpu:.2f}s cpu vs gain {gain_ms / 1000:.2f}s")
            continue
        plans.append((gain_ms - cpu * 1000, cid, light, keep, cpu))
    if not plans:
        return
    plans.sort(key=lambda p: (-p[0], p[1]))
    warm = verify.Warm(tests)
    for _, cid, light, keep, cpu in plans:
        if time.monotonic() > deadline:
            log("deadline reached; remaining commits keep their static answer")
            break
        res = statics[cid]
        job = verify.Job(
            cid,
            [rows[n] for n in sorted(light)],
            dict(res.py_files),
            {path: new for path, _old, new in res.data_changes},
            cpu_limit=max(5.0, cpu / factor * 4 + 3),
            wall_limit=max(10.0, cpu / factor * 6 + 5),
        )
        t0 = time.monotonic()
        verdicts, info = verify.run_job(warm, job)
        failing = []
        unverified = []
        for n in light:
            outcome = verdicts.get(rows[n]["id"])
            if outcome in ("pass", "skip"):
                continue
            (failing if outcome == "fail" else unverified).append(n)
        answers[cid] = sorted(set(keep) | set(failing) | set(unverified))
        log(
            cid,
            f"verified {len(light)}: {len(failing)} fail, {len(unverified)} unverified, kept {len(keep)};"
            f" child cpu {info.get('cpu', 0):.2f}s wall {time.monotonic() - t0:.2f}s"
            f" proof={info.get('proof')} err={info.get('error')}",
        )


# ---------------------------------------------------------------------------
# Clean mode: import-time changes in modules loaded during collection.


def _load_attrs(maps_dir):
    import marshal
    import zlib

    path = os.path.join(maps_dir, "attrs.bin")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return marshal.loads(zlib.decompress(f.read()))


def _set(attrs, i):
    """Bitset of reader tests (sparse sets are stored as uint16 index arrays)."""
    v = attrs["sets"][i]
    if isinstance(v, int):
        return v
    import array

    bits = 0
    for n in array.array("H", v):
        bits |= 1 << n
    return bits


def _subclass_ids(class_id):
    """Ids (statediff naming) of the class and its live subclasses, in this process."""
    import sys

    mod, _, qual = class_id.partition(":")
    qual = qual.split("#", 1)[0]
    obj = sys.modules.get(mod)
    for part in qual.split("."):
        obj = getattr(obj, part, None) if obj is not None else None
    if not isinstance(obj, type):
        return None
    out = set()
    stack = [obj]
    while stack:
        c = stack.pop()
        cid = f"{c.__module__}:{c.__qualname__}"
        if cid in out:
            continue
        out.add(cid)
        try:
            stack.extend(type.__subclasses__(c))
        except Exception:
            pass
    return out


def dict_attrs(pristine_cells):
    """(owner, attr) pairs whose pinned value is a dict (they have key cells)."""
    return {(c[1], c[2]) for c in pristine_cells if c[0] == "ck"}


def cell_readers(cells, maps, attrs, pristine_cells, funcs=None, dicts_now=None, pinned_dicts=None, subs_map=None):
    """Bitset of tests that can observe the changed cells, or None (= every test).

    funcs: {cell: (path, qualname, firstlineno) | None} for cells holding functions.
    dicts_now: cells ("c", owner, attr) whose patched value is a dict.
    pinned_dicts: (owner, attr) pairs that held a dict at the pinned commit.
    """
    bits = 0
    funcs = funcs or {}
    dicts_now = dicts_now or set()
    pinned_dicts = pinned_dicts or set()
    for cell in cells:
        kind = cell[0]
        if cell in funcs:
            where = funcs[cell]
            if where is None:
                bits |= maps.name_readers(cell[2])
                continue
            path, qual, line = where
            # Changed defaults/decorators are seen only by running it; a
            # function that was swapped for another one is seen by its callers,
            # who ran the pinned one.
            bits |= maps.code_tests(path, (qual, line))
            if cell not in pristine_cells:
                bits |= maps.name_readers(cell[2])
            continue
        if kind in ("g", "gk"):
            bits |= maps.name_readers(cell[2])
            continue
        if kind not in ("c", "ck") or attrs is None:
            return None
        cid, attr = cell[1], cell[2]
        if kind == "ck":
            # Key-level: every access to the dict object goes through the
            # recording dict, whichever class it was reached through.
            i = attrs["keys"].get((cid, attr, cell[3]))
            if i is not None:
                bits |= _set(attrs, i)
            i = attrs["allkeys"].get((cid, attr))
            if i is not None:
                bits |= _set(attrs, i)
            continue
        if cell in dicts_now and (cid, attr) in pinned_dicts:
            # A dict before and after: the change is exactly its key cells.
            i = attrs["allkeys"].get((cid, attr))
            if i is not None:
                bits |= _set(attrs, i)
            continue
        subs = subs_map.get(cid) if subs_map is not None else _subclass_ids(cid)
        if subs is None:
            return None
        if cell not in pristine_cells:
            # A new attribute: lookups that used to fail or resolve elsewhere.
            bits |= maps.name_readers(attr)
        i = attrs["owner"].get((cid, attr))
        if i is not None:
            bits |= _set(attrs, i)
        for sub in subs:
            i = attrs["via"].get((sub, attr))
            if i is not None:
                bits |= _set(attrs, i)
        i = attrs["allkeys"].get((cid, attr))
        if i is not None:
            bits |= _set(attrs, i)
    return bits


def refine_clean(answers, statics, tests, ms, maps, number_of, deadline, log, maps_dir, state_path):
    """Clean jobs run before the warm state exists in this process (they fork from it bare)."""
    import marshal
    import sys

    if "sqlglot" in sys.modules or not os.path.exists(state_path):
        return
    jobs = [cid for cid, res in statics.items() if res.clean and not getattr(res, "hard", False)]
    if not jobs:
        return
    with open(state_path, "rb") as f:
        pristine = marshal.load(f)
    attrs = _load_attrs(maps_dir)
    pinned_dicts = dict_attrs(pristine["cells"])
    factor = float(os.environ.get("BR_CPU_FACTOR", "2.5"))
    rows = {t["number"]: t for t in tests}
    bit_numbers = [number_of.get(test_id) for test_id in maps.tests]
    est = {}
    if maps.cpu:
        for i, n in enumerate(bit_numbers):
            if n is not None:
                est[n] = maps.cpu[i]
    never = set()
    for t in tests:
        if t["file"] == "tests/test_executor.py" or ms[t["number"]] >= HEAVY_MS:
            never.add(t["number"])
    b, i = maps.subprocs, 0
    while b:
        if b & 1 and bit_numbers[i] is not None:
            never.add(bit_numbers[i])
        b >>= 1
        i += 1
    every = sorted(ms)

    for cid in sorted(jobs):
        if time.monotonic() > deadline:
            break
        res = statics[cid]
        base_bits = res.bits

        def choose(cells, import_errors, funcs=None, dicts_now=None, subs_map=None, base_bits=base_bits):
            if import_errors:
                return [], every
            readers = cell_readers(cells, maps, attrs, pristine["cells"], funcs, dicts_now, pinned_dicts, subs_map)
            if readers is None:
                return [], every
            bits = readers | base_bits  # base_bits: runtime candidates and subprocess importers
            chosen, k = set(), 0
            while bits:
                if bits & 1 and bit_numbers[k] is not None:
                    chosen.add(bit_numbers[k])
                bits >>= 1
                k += 1
            light = sorted(n for n in chosen if n not in never)
            keep = sorted(n for n in chosen if n in never)
            gain = _cost(sorted(chosen), ms) - _cost(keep, ms)
            cpu = sum(est.get(n, ms[n] / 1000.0) for n in light) * factor
            if not light or cpu * 1000 > SAFETY * gain:
                return [], sorted(chosen)
            return [rows[n] for n in light], keep

        job = verify.Job(
            cid,
            [],
            dict(res.py_files),
            {path: new for path, _old, new in res.data_changes},
            cpu_limit=30.0,
            wall_limit=40.0,
        )
        t0 = time.monotonic()
        verdicts, info = verify.run_clean_job(tests, job, pristine, choose)
        keep = info.get("keep")
        if not info.get("proof") or keep is None:
            log(cid, "clean job gave no proof; keeping ALL:", info.get("error"), "timeout" if info.get("timeout") else "")
            continue
        ran = info.get("run") or []
        # Failing, and anything that was to run but got no verdict, stays selected.
        failing = [n for n in ran if verdicts.get(rows[n]["id"]) not in ("pass", "skip")]
        answers[cid] = sorted(set(keep) | set(failing))
        log(
            cid,
            f"clean: {info.get('proof', {}).get('cells')} changed cells, kept {len(keep)}, ran {len(ran)},"
            f" {len(failing)} fail; child cpu {info.get('cpu', 0):.2f}s wall {time.monotonic() - t0:.2f}s;"
            f" cells {str(info.get('cells'))[:300]}",
        )


def refine_lazy(answers, statics, tests, maps, number_of, unknown, deadline, log, maps_dir, state_path):
    """Narrow import-time changes in lazily imported modules to their readers."""
    import ast
    import marshal
    import sys

    if "sqlglot" in sys.modules or not os.path.exists(state_path):
        return
    jobs = [cid for cid, res in statics.items() if res.lazy and not res.all]
    if not jobs:
        return
    with open(state_path, "rb") as f:
        state = marshal.load(f)
    full = state.get("full")
    if not full:
        return
    attrs = _load_attrs(maps_dir)
    pinned_dicts = dict_attrs(full["cells"])
    bit_numbers = [number_of.get(test_id) for test_id in maps.tests]
    for cid in sorted(jobs):
        if time.monotonic() > deadline:
            break
        res = statics[cid]
        job = verify.Job(cid, [], dict(res.py_files), {p: new for p, _o, new in res.data_changes}, 20.0, 30.0)
        t0 = time.monotonic()
        info = verify.run_lazy_diff(job, full)
        if not info.get("proof") or info.get("error"):
            log(cid, "lazy diff gave no proof; keeping static answer:", info.get("error"))
            continue
        cells = [tuple(c) for c in info.get("cells") or []]
        funcs = {ast.literal_eval(k): (tuple(v) if v else None) for k, v in (info.get("funcs") or {}).items()}
        dicts_now = {tuple(c) for c in info.get("dicts") or []}
        readers = cell_readers(cells, maps, attrs, full["cells"], funcs, dicts_now, pinned_dicts, info.get("subs") or {})
        if readers is None:
            log(cid, f"lazy diff: {len(cells)} cells, readers unknown; keeping static answer")
            continue
        bits = res.bits | readers
        chosen, k = set(unknown), 0
        while bits:
            if bits & 1 and bit_numbers[k] is not None:
                chosen.add(bit_numbers[k])
            bits >>= 1
            k += 1
        before = len(answers[cid])
        answers[cid] = sorted(chosen)
        log(
            cid,
            f"lazy diff: {len(cells)} cells, {before} -> {len(chosen)} tests;"
            f" child cpu {info.get('cpu', 0):.2f}s wall {time.monotonic() - t0:.2f}s; cells {str(cells)[:200]}",
        )
