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
# Never run in a verification child: 46 CPU-s and 1.7 GB (test_max_nodes); the
# TPC-DS executor test goes through its front proof only.
NEVER_RUN = frozenset(
    {
        "tests/test_parser.py::TestParser::test_max_nodes",
        "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    }
)
# Measured child CPU / estimate on 40-commit batches of labeled mutants (median):
# isolated-run calibration over-counts shared class setup and lazy imports.
EST_PLAIN = 0.65
EST_SLICED = 0.5
EST_FRONT = 0.3
SAFETY = 0.7  # verify only when CPU estimate <= SAFETY * gain


def _cost(numbers, ms):
    if not numbers:
        return 0
    return pack.pack([(n, ms[n]) for n in numbers])[0]


def _hot_eligible(res, maps):
    if res.all or getattr(res, "deleted", False):
        return False  # the verifier cannot make a pinned file disappear
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
        if t["id"] in NEVER_RUN:
            never.add(t["number"])

    from . import slices as slmod

    smap = slmod.load_default()
    plans = []
    for cid, res in statics.items():
        chosen = answers[cid]
        if not chosen or res.all or not _hot_eligible(res, maps):
            continue
        sliceable = smap is not None and not getattr(res, "created", False)
        fixture_only = not res.changes and bool(res.data_changes)
        keep, runnable, sliced, dropped, fronts = [], [], {}, [], {}
        for n in chosen:
            tid = rows[n]["id"]
            items = None
            if sliceable and tid == slmod.TPCDS_TEST and tid in smap and not fixture_only:
                # Front proof: items reached only through parse/pickle/optimize may be
                # dropped when their optimized tree is unchanged (br/slices.py).
                f_items, f_rest = slmod.affected_split(smap, tid, res.changes, res.data_changes)
                fe = slmod.estimate_front_cpu(smap, f_items, f_rest) if f_items else None
                if f_items == []:
                    dropped.append(n)
                    continue
                if fe is not None:
                    check, worst = fe
                    cand = sorted(set(f_items) - set(f_rest))
                    rest_cpu = slmod.estimate_cpu(smap, tid, f_rest) if f_rest else 0.0
                    cand_cpu = (slmod.estimate_cpu(smap, tid, cand) or 0.0) if cand else 0.0
                    # The child stops at the first changed front and only runs the
                    # remaining items within the budget: pay the check, plus the
                    # rest items when the budget allows them.
                    expected = check + (rest_cpu or 0.0)
                    runnable.append((expected * EST_FRONT, n))
                    fronts[tid] = (f_items, f_rest, worst)
                    continue
            if sliceable and tid in smap:
                items = slmod.affected(smap, tid, res.changes, res.data_changes)
            if items is not None:
                if not items:
                    dropped.append(n)  # no item and no shared code can see the patch
                    continue
                if fixture_only:
                    keep.append(n)  # its own changed entries: expected to fail
                    continue
                c = slmod.estimate_cpu(smap, tid, items)
                if c is not None:
                    runnable.append((c * EST_SLICED, n))
                    sliced[tid] = items
                    continue
            if n in never or fixture_only:
                keep.append(n)
                continue
            runnable.append((est.get(n, ms[n] / 1000.0) * EST_PLAIN, n))
        if dropped:
            chosen = [n for n in chosen if n not in set(dropped)]
            answers[cid] = chosen
            log(cid, f"{len(dropped)} sliced tests cannot reach the patch: dropped")
        if not runnable:
            continue
        # Verify the prefix (cheapest per selected millisecond first) with the best
        # expected saving if everything passes.
        runnable.sort(key=lambda cn: (cn[0] / max(ms[cn[1]], 1), cn[1]))
        base = _cost(chosen, ms)
        best = (0, 0, 0.0)
        # Evaluate prefixes of every size up to 24, then geometrically spaced sizes
        # (packing is the costly part), always including the full set.
        sizes = set(range(1, min(len(runnable), 24) + 1))
        k = 24
        while k < len(runnable):
            k = int(k * 1.3) + 1
            sizes.add(min(k, len(runnable)))
        sizes.add(len(runnable))
        cum = []
        total = 0.0
        for c, _n in runnable:
            total += c * factor
            cum.append(total)
        for k in sorted(sizes):
            removed = {x for _, x in runnable[:k]}
            rest = [m for m in chosen if m not in removed]
            gain = base - _cost(rest, ms)
            cpu = cum[k - 1]
            net = SAFETY * gain - cpu * 1000
            if net > best[0]:
                best = (net, k, cpu)
        if not best[1]:
            log(cid, f"not verified: best plan does not pay (base {base / 1000:.2f}s)")
            continue
        run = [n for _, n in runnable[: best[1]]]
        keep_all = [n for n in chosen if n not in set(run)]
        plans.append(
            (
                best[0],
                cid,
                run,
                keep_all,
                best[2],
                {rows[n]["id"]: sliced[rows[n]["id"]] for n in run if rows[n]["id"] in sliced},
                {rows[n]["id"]: fronts[rows[n]["id"]] for n in run if rows[n]["id"] in fronts},
            )
        )
    if not plans:
        return
    plans.sort(key=lambda p: (-p[0], p[1]))
    warm = verify.Warm(tests)
    for _, cid, light, keep, cpu, sliced, fronts in plans:
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
        job.slices = sliced
        job.front = {tid: (items, rest) for tid, (items, rest, _w) in fronts.items()}
        if fronts:
            # Items may run only within what verifying the rest of the job leaves of
            # the expected saving (in this machine's seconds).
            job.front_budget = max(0.0, cpu / factor)
            job.cpu_limit = max(job.cpu_limit, cpu / factor * 3 + 8)
            job.wall_limit = max(job.wall_limit, cpu / factor * 4 + 12)
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
            f"verified {len(light)} ({len(sliced)} sliced, {len(fronts)} front): {len(failing)} fail, {len(unverified)} unverified, kept {len(keep)};"
            f" child cpu {info.get('cpu', 0):.2f}s (est {cpu / factor:.2f}s) wall {time.monotonic() - t0:.2f}s"
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


def _class_users(attrs, subs, cache):
    """Tests that read any attribute through one of these classes (or instances of them)."""
    key = tuple(sorted(subs))
    hit = cache.get(key)
    if hit is not None:
        return hit
    index = cache.get("__via_index__")
    if index is None:
        index = {}
        for (via, _attr), i in attrs["via"].items():
            index.setdefault(via, []).append(i)
        cache["__via_index__"] = index
    bits = 0
    for cid in subs:
        for i in index.get(cid, ()):
            bits |= _set(attrs, i)
    cache[key] = bits
    return bits


def _module_path(maps, module):
    base = module.replace(".", "/")
    for path in (base + ".py", base + "/__init__.py"):
        if maps.has_file(path) or path in maps.preload_mods:
            return path
    return None


def _class_fallback(maps, attrs, cid, attr, subs, cache):
    """Every way a test could reach an attribute we did not measure: code naming the
    attribute or the class, any measured read through the class family, and any test
    that ran a method of the class family."""
    bits = maps.name_readers(attr)
    for sub in subs:
        mod, _, qual = sub.partition(":")
        qual = qual.split("#", 1)[0]
        bits |= maps.name_readers(qual.rsplit(".", 1)[-1])
        path = _module_path(maps, mod)
        if path is not None:
            bits |= maps.class_code_tests(path, qual)
    if attrs is not None:
        bits |= _class_users(attrs, subs, cache)
    return bits


def cell_readers(
    cells,
    maps,
    attrs,
    pristine_cells,
    funcs=None,
    dicts_now=None,
    pinned_dicts=None,
    subs_map=None,
    pinned_funcs=None,
    measured=None,
):
    """Bitset of tests that can observe the changed cells, or None (= every test).

    funcs: {cell: (path, qualname, firstlineno) | None} for cells holding a function now.
    pinned_funcs: same for the pinned value (from the build-time snapshot).
    dicts_now: cells ("c", owner, attr) whose patched value is a dict.
    pinned_dicts: (owner, attr) pairs that held a dict at the pinned commit.
    subs_map: {class_id: [ids of the class and its subclasses] | None} (from the child).

    Rules, each a superset of who can observe the cell:
    - a function value (pinned or patched): the tests that started the pinned code
      object (callers see changed defaults or a replaced/removed function only by
      calling it); a function nobody ran cannot matter;
    - a new attribute or global (absent at the pinned commit): lookups that used to
      fail or resolve elsewhere: every test that used the class or a subclass (any
      recorded attribute read through it) plus every code object naming it;
    - a dict attribute that is a dict before and after: its key cells and whole-dict
      readers; a key cell: readers of that key and whole-dict readers;
    - any other class attribute: measured reads through the owner or a subclass;
    - a module global: code objects naming it (LOAD_GLOBAL / LOAD_ATTR / strings).
    """
    bits = 0
    funcs = funcs or {}
    pinned_funcs = pinned_funcs or {}
    dicts_now = dicts_now or set()
    pinned_dicts = pinned_dicts or set()
    measured = measured if measured is not None else set()
    cache = {}
    for cell in cells:
        kind = cell[0]
        if kind == "mro":
            return None
        is_new = cell not in pristine_cells
        handled_as_function = False
        unmappable = False
        for table in (pinned_funcs, funcs):
            if cell in table:
                where = table[cell]
                handled_as_function = True
                if where is None:
                    unmappable = True  # e.g. dataclass-generated methods
                else:
                    path, qual, line = where
                    bits |= maps.code_tests(path, (qual, line))
        if kind in ("g", "gk"):
            bits |= maps.name_readers(cell[2])
            continue
        if kind not in ("c", "ck") or attrs is None:
            return None
        cid, attr = cell[1], cell[2]
        if kind == "c":
            subs = subs_map.get(cid) if subs_map is not None else _subclass_ids(cid)
            if subs is None:
                return None
            unmeasured = not handled_as_function and cell not in measured and cell not in dicts_now
            if is_new or unmappable or unmeasured:
                # Dunders, slots, properties, generated methods, new names: reads
                # we did not record. Take every way the class family is reached.
                bits |= _class_fallback(maps, attrs, cid, attr, subs, cache)
                if is_new or unmappable:
                    continue
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
        if handled_as_function:
            continue
        if cell in dicts_now and (cid, attr) in pinned_dicts:
            # A dict before and after: the change is exactly its key cells.
            i = attrs["allkeys"].get((cid, attr))
            if i is not None:
                bits |= _set(attrs, i)
            continue
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


def _preload_third_party():
    """Import the third-party packages the test modules import (sqlglot uses none of
    them), so every clean child forked from here inherits them instead of paying for
    them again. Safe before sqlglot: nothing in sqlglot reads them at import."""
    import gc
    import importlib

    for name in ("numpy", "pandas", "pandas.testing", "duckdb"):
        try:
            importlib.import_module(name)
        except Exception:
            pass
    gc.collect()
    gc.freeze()


def refine_clean(answers, statics, tests, ms, maps, number_of, deadline, log, maps_dir, state_path):
    """Clean jobs run before the warm state exists in this process (they fork from it bare)."""
    import marshal
    import sys

    if "sqlglot" in sys.modules or not os.path.exists(state_path):
        return
    jobs = [
        cid for cid, res in statics.items() if res.clean and not getattr(res, "hard", False) and not getattr(res, "deleted", False)
    ]
    if not jobs:
        return
    with open(state_path, "rb") as f:
        pristine = marshal.load(f)
    if "full" in pristine:  # older single-file layout
        pristine = {k: v for k, v in pristine.items() if k != "full"}
    attrs = _load_attrs(maps_dir)
    pinned_dicts = dict_attrs(pristine["cells"])
    _preload_third_party()
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
        if t["id"] in NEVER_RUN:
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
        # Lazily imported modules are not loaded in the warm state: their changes keep
        # every test that imported them (refine_lazy skips commits that need clean mode).
        base_bits = res.bits | res.lazy_bits

        def choose(cells, import_errors, funcs=None, dicts_now=None, subs_map=None, base_bits=base_bits):
            if import_errors:
                return [], every
            readers = cell_readers(
                cells,
                maps,
                attrs,
                pristine["cells"],
                funcs,
                dicts_now,
                pinned_dicts,
                subs_map,
                pristine.get("funcs"),
                set(map(tuple, pristine.get("measured") or ())),
            )
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
            cpu = sum(est.get(n, ms[n] / 1000.0) for n in light) * factor * EST_PLAIN
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

    from .buildstate import full_path

    if "sqlglot" in sys.modules:
        return
    jobs = [cid for cid, res in statics.items() if res.lazy and not res.all and not getattr(res, "deleted", False)]
    if not jobs:
        return
    path = full_path(state_path)
    if os.path.exists(path):
        with open(path, "rb") as f:
            full = marshal.load(f)
    elif os.path.exists(state_path):
        with open(state_path, "rb") as f:
            full = marshal.load(f).get("full")
    else:
        return
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
        readers = cell_readers(
            cells,
            maps,
            attrs,
            full["cells"],
            funcs,
            dicts_now,
            pinned_dicts,
            info.get("subs") or {},
            full.get("funcs"),
            set(map(tuple, full.get("measured") or ())),
        )
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
