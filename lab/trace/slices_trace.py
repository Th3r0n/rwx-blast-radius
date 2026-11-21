"""Per-item coverage of loop-over-fixture tests (runs inside the br-lab image).

    python /src/lab/trace/slices_trace.py --tests /src/kit/sample/tests.json \
        --root /opt/sqlglot --out /src/lab/out/slices --mode discover|trace|calib|duckcheck \
        [--jobs 7] [--only N,N-M] [--modules tests.test_optimizer,tests.test_executor]

A server builds the warm state like the runtime (br.unitrun: unittest, then every test
module in collection order; inline process pool), then forks one child per job.

discover  one child per test of the modules that bind a fixture loader
          (load_sql_fixture_pairs / load_sql_fixtures from tests.helpers): records
          every loader call (file, entries yielded, exhausted?, during setUpClass or the
          test) and which class attributes built from a setUpClass call the test read.
          -> <out>/discover.json (one spec per sliceable test, see br/slices.py sources)
trace     per sliceable test T (from discover.json): T with zero items ("zero"), T on
          each single item ("i<k>"), T filtered to all items ("all") and T unfiltered
          ("full"), each under the tracer's sys.monitoring recording (code keys and
          (code key, line), audit hook for opens). For test_execute_tpcds each record is
          split into phases: "front" (tests.test_executor.parse_one, the pickling of the
          call item, sqlglot.executor.optimize) and the rest; restart_events() at every
          phase switch. -> <out>/trace/<n>/<job>.json.gz
calib     the same zero/item jobs without tracing: child CPU (os.wait4) per job.
          -> <out>/calib/<n>.json
duckcheck T unfiltered with and without duckdb single-thread connections (verdicts).
          -> <out>/duckcheck.json

Every child calls br.slices.duckdb_single_thread() (except duckcheck's baseline) and
uses br.slices.install_filter, the runtime's filter, so traces see exactly what a
verification child does.
"""

from __future__ import annotations

import argparse
import functools
import gc
import gzip
import json
import os
import select
import signal
import sys
import time
import traceback

import unittest  # noqa: F401  (before sqlglot)

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import tracer  # noqa: E402  (puts /src on sys.path, imports br, then cleans sys.path)

from br import fixtures as brfixtures  # noqa: E402
from br import inlinepool, slices, unitrun  # noqa: E402

sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") not in (_HERE, tracer._REPO)]

LOADERS = slices.LOADERS
TPCDS = slices.TPCDS_TEST
DEFAULT_MODULES = "tests.test_optimizer,tests.test_executor"


def log(msg):
    print(f"[slices {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def write_json(path, obj, gz=False):
    data = json.dumps(obj, separators=(",", ":")).encode()
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(gzip.compress(data, 6) if gz else data)
    os.replace(tmp, path)


class Pool:
    """Fork-per-job runner (at most `jobs` children at once), like tracer.Server."""

    def __init__(self, jobs, tmp):
        self.jobs = jobs
        self.tmp = tmp
        os.makedirs(tmp, exist_ok=True)

    def run(self, work, body_of, on_done, timeout_of):
        pending = list(work)
        running = {}
        total = len(pending)
        done = 0
        t0 = time.perf_counter()
        last = t0
        try:
            while pending or running:
                while pending and len(running) < self.jobs:
                    w = pending.pop(0)
                    log_path = os.path.join(self.tmp, f"{os.getpid()}-{len(pending)}.log")
                    sys.stdout.flush()
                    sys.stderr.flush()
                    start = time.perf_counter()
                    pid = os.fork()
                    if pid == 0:
                        code = 0
                        try:
                            unitrun.silence_std_fds(log_path)
                            signal.signal(signal.SIGINT, signal.SIG_DFL)
                            signal.signal(signal.SIGTERM, signal.SIG_DFL)
                            body_of(w)()
                        except BaseException:
                            code = 3
                            try:
                                traceback.print_exc()
                                sys.stderr.flush()
                            except BaseException:
                                pass
                        finally:
                            os._exit(code)
                    fd = os.pidfd_open(pid)
                    running[fd] = (pid, w, start, start + timeout_of(w), log_path)
                now = time.perf_counter()
                wait = max(0.0, min(r[3] for r in running.values()) - now)
                ready, _, _ = select.select(list(running), [], [], min(wait, 5.0))
                now = time.perf_counter()
                finished = [(fd, False) for fd in ready]
                for fd, r in running.items():
                    if fd not in ready and now >= r[3]:
                        try:
                            os.kill(r[0], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        finished.append((fd, True))
                for fd, timed_out in finished:
                    pid, w, start, _deadline, log_path = running.pop(fd)
                    _, status, ru = os.wait4(pid, 0)
                    os.close(fd)
                    info = {
                        "cpu": ru.ru_utime + ru.ru_stime,
                        "wall": time.perf_counter() - start,
                        "exit": os.waitstatus_to_exitcode(status),
                        "timed_out": timed_out,
                        "maxrss_kb": ru.ru_maxrss,
                        "log": log_path,
                    }
                    try:
                        on_done(w, info)
                    finally:
                        try:
                            os.remove(log_path)
                        except OSError:
                            pass
                    done += 1
                if time.perf_counter() - last > 30:
                    last = time.perf_counter()
                    log(f"{done}/{total} done, {len(running)} running, {last - t0:.0f}s")
        finally:
            for r in running.values():
                try:
                    os.kill(r[0], signal.SIGKILL)
                    os.waitpid(r[0], 0)
                except OSError:
                    pass
        return time.perf_counter() - t0


def read_log(path, limit=4000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    return text[-limit:]


class Server:
    def __init__(self, args):
        self.args = args
        self.root = os.path.abspath(args.root)
        self.tests, _ = unitrun.load_tests_json(args.tests)
        self.by_id = {t["id"]: t for t in self.tests}
        self.out = os.path.abspath(args.out)
        os.makedirs(self.out, exist_ok=True)
        self.pool = Pool(args.jobs, os.path.join(self.out, "tmp"))
        self.warm = None

    def warm_up(self):
        t0, c0 = time.perf_counter(), time.process_time()
        self.warm = unitrun.build_warm_state(self.tests, self.root)
        inlinepool.install(self.warm.modules)
        gc.collect()
        gc.freeze()
        log(f"warm-up {time.perf_counter() - t0:.1f}s wall, {time.process_time() - c0:.1f}s cpu, "
            f"import errors {sorted(self.warm.import_errors)}")

    # -- discover ---------------------------------------------------------------------

    def loader_modules(self):
        import tests.helpers as helpers

        real = {name: getattr(helpers, name) for name in LOADERS}
        wanted = set(self.args.modules.split(",")) if self.args.modules else None
        out = {}
        for mod in self.warm.modules:
            if wanted is not None and mod.__name__ not in wanted:
                continue
            bound = [n for n in LOADERS if mod.__dict__.get(n) is real[n]]
            if bound:
                out[mod.__name__] = bound
        return out

    def discover(self):
        mods = self.loader_modules()
        log(f"modules binding a loader: {mods}")
        cands = []
        for t in self.tests:
            file, _cls, _m = unitrun.parse_id(t["id"])
            name = unitrun.module_name_for(file, self.root)
            if name in mods:
                cands.append((t, name, mods[name]))
        if self.args.only:
            only = tracer.parse_only(self.args.only)
            cands = [c for c in cands if c[0]["number"] in only]
        tmp = self.pool.tmp
        root = self.root
        warm = self.warm
        results = {}

        def body_of(c):
            entry, modname, loader_names = c

            def body():
                slices.duckdb_single_thread()
                mod = sys.modules[modname]
                state = {"phase": "import"}
                calls = []
                attr_hits = []

                def recording(lname, real):
                    def rec(filename, *a, **k):
                        r = {"loader": lname, "file": filename, "phase": state["phase"], "count": 0, "done": False}
                        calls.append(r)
                        got = []
                        for e in real(filename, *a, **k):
                            r["count"] += 1
                            got.append(e)
                            yield e
                        r["done"] = True
                        r["_entries"] = got

                    return rec

                for lname in loader_names:
                    setattr(mod, lname, recording(lname, mod.__dict__[lname]))

                class RecList(list):
                    pass

                def before_group(group):
                    state["phase"] = "class"

                def before_test(e):
                    state["phase"] = "test"
                    cls = getattr(mod, unitrun.parse_id(e["id"])[1])
                    for r in calls:
                        if r["phase"] != "class" or not r["done"]:
                            continue
                        for attr, value in list(vars(cls).items()):
                            if type(value) is list and value == r["_entries"]:
                                rl = RecList(value)
                                rl._br = {"attr": attr, "loader": r["loader"], "file": r["file"],
                                          "count": r["count"], "hit": False}
                                attr_hits.append(rl._br)
                                setattr(cls, attr, rl)

                    for name in ("__iter__", "__getitem__", "__len__"):
                        base = getattr(list, name)

                        def make(base):
                            def f(self, *a):
                                self._br["hit"] = True
                                return base(self, *a)

                            return f

                        setattr(RecList, name, make(base))

                def after_test(e, v):
                    state["phase"] = "teardown"

                v = unitrun.run_tests([entry], root=root, warm=warm, before_group=before_group,
                                      before_test=before_test, after_test=after_test)[0]
                rec = {
                    "n": entry["number"], "id": entry["id"], "module": modname,
                    "outcome": v["outcome"], "detail": v["detail"] if v["outcome"] != "pass" else None,
                    "calls": [{k: r[k] for k in ("loader", "file", "phase", "count", "done")} for r in calls],
                    "attrs": attr_hits,
                }
                with open(os.path.join(tmp, f"disc-{entry['number']}.json"), "w") as f:
                    json.dump(rec, f)

            return body

        def on_done(c, info):
            n = c[0]["number"]
            path = os.path.join(tmp, f"disc-{n}.json")
            if os.path.exists(path):
                with open(path) as f:
                    results[n] = json.load(f)
                os.remove(path)
            else:
                results[n] = {"n": n, "id": c[0]["id"], "outcome": "crash", "detail": read_log(info["log"])}

        self.pool.run(cands, body_of, on_done, lambda c: 20 * c[0]["seconds"] + 60)
        specs = []
        rejected = []
        for n in sorted(results):
            r = results[n]
            spec, why = self.make_spec(r)
            if spec is None:
                if r.get("calls"):
                    rejected.append({"n": n, "id": r["id"], "why": why})
                continue
            specs.append(spec)
        write_json(os.path.join(self.out, "discover.json"), {"specs": specs, "rejected": rejected,
                                                              "raw": [results[n] for n in sorted(results)]})
        log(f"discover: {len(specs)} sliceable tests, {len(rejected)} rejected")
        for s in specs:
            log(f"  {s['n']} {s['id']} n={s['n_items']} sources={s['sources']}")
        for r in rejected:
            log(f"  rejected {r['n']} {r['id']}: {r['why']}")

    def make_spec(self, r):
        if r.get("outcome") != "pass":
            return None, f"outcome {r.get('outcome')}"
        file, cls, method = unitrun.parse_id(r["id"])
        sources = []
        seen = {}
        for c in r["calls"]:
            if c["phase"] != "test":
                continue
            if not c["done"]:
                return None, f"loop over {c['file']} stops early"
            key = ("call", c["loader"], c["file"])
            if key in seen:
                if seen[key] != c["count"]:
                    return None, f"{c['file']} yields different counts"
                continue
            seen[key] = c["count"]
            sources.append(["call", c["loader"], c["loader"], c["file"], c["count"]])
        for a in r["attrs"]:
            if a["hit"]:
                sources.append(["attr", a["attr"], a["loader"], a["file"], a["count"]])
        if not sources:
            return None, "no loop over a fixture during the test"
        offset = 0
        for s in sources:
            path = os.path.join(self.root, "tests", "fixtures", s[3])
            with open(path, "rb") as f:
                data = f.read()
            expect = len(brfixtures.fixture_pairs(data)) if s[2] == "load_sql_fixture_pairs" else len(brfixtures.fixtures(data))
            if expect != s[4]:
                return None, f"{s[3]}: {s[4]} entries yielded, br.fixtures says {expect}"
            s.append(offset)
            offset += s[4]
        if offset < 2:
            return None, "fewer than 2 items"
        return {
            "n": r["n"], "id": r["id"], "module": r["module"], "cls": cls, "method": method,
            "sources": sources, "n_items": offset,
            "seconds": self.by_id[r["id"]]["seconds"],
        }, None

    # -- trace / calib ----------------------------------------------------------------

    def load_specs(self):
        with open(os.path.join(self.out, "discover.json")) as f:
            specs = json.load(f)["specs"]
        if self.args.only:
            only = tracer.parse_only(self.args.only)
            specs = [s for s in specs if s["n"] in only]
        return specs

    def job_list(self, specs, kinds):
        work = []
        for s in specs:
            for kind in kinds:
                if kind == "items":
                    work.extend((s, f"i{k:04d}", [k]) for k in range(s["n_items"]))
                elif kind == "zero":
                    work.append((s, "zero", []))
                elif kind == "all":
                    work.append((s, "all", list(range(s["n_items"]))))
                elif kind == "full":
                    work.append((s, "full", None))
        # Longest tests first (packing), items of one test together.
        work.sort(key=lambda w: -w[0]["seconds"])
        return work

    def run_jobs(self, mode):
        specs = self.load_specs()
        traced = mode == "trace"
        sub = "trace" if traced else "calib"
        kinds = ("zero", "full", "all", "items") if traced else ("zero", "items", "full")
        work = self.job_list(specs, kinds)
        if self.args.skip_done and traced:
            work = [w for w in work if not os.path.exists(os.path.join(self.out, sub, f"{w[0]['n']:04d}", f"{w[1]}.json.gz"))]
        log(f"{mode}: {len(specs)} tests, {len(work)} jobs")
        root = self.root
        warm = self.warm
        tmp = self.pool.tmp
        rows = {}

        def body_of(w):
            spec, job, items = w

            def body():
                slices.duckdb_single_thread()
                entry = self.by_id[spec["id"]]
                if items is not None:
                    slices.install_filter(spec["id"], items, spec={
                        "module": spec["module"], "cls": spec["cls"], "method": spec["method"],
                        "sources": spec["sources"], "n": spec["n_items"]})
                col = None
                front = None
                if traced:
                    col = tracer.Collector(root)
                    col.cur = tracer.Bucket()
                    rest = col.cur
                    if spec["id"] == TPCDS:
                        front = tracer.Bucket()
                        install_phases(col, front)
                    col.start_audit()
                    col.start_monitoring()
                mods = set(sys.modules)
                v = unitrun.run_tests([entry], root=root, warm=warm)[0]
                if col is not None:
                    col.stop_monitoring()
                    col.audit_on = False
                rec = {"n": entry["number"], "id": entry["id"], "job": job, "items": items,
                       "outcome": v["outcome"], "phase": v["phase"],
                       "detail": v["detail"] if v["outcome"] != "pass" else None,
                       "test_cpu": v["cpu"], "test_wall": v["wall"],
                       "new_modules": sorted(set(sys.modules) - mods)}
                if col is not None:
                    rec.update(rest.to_json())
                    if front is not None:
                        codes, lines = front.coverage_json()
                        rec["front"] = {"codes": codes, "lines": lines}
                with open(os.path.join(tmp, f"job-{entry['number']}-{job}.json"), "w") as f:
                    json.dump(rec, f, separators=(",", ":"))

            return body

        def on_done(w, info):
            spec, job, items = w
            n = spec["n"]
            path = os.path.join(tmp, f"job-{n}-{job}.json")
            rec = None
            if os.path.exists(path):
                with open(path) as f:
                    rec = json.load(f)
                os.remove(path)
            if rec is None or info["exit"] != 0 or info["timed_out"]:
                outcome = "timeout" if info["timed_out"] else "crash"
                rec = {"n": n, "id": spec["id"], "job": job, "items": items, "outcome": outcome,
                       "detail": read_log(info["log"])}
            rec["cpu"] = info["cpu"]
            rec["wall"] = info["wall"]
            rec["maxrss_kb"] = info["maxrss_kb"]
            if rec["outcome"] != "pass":
                log(f"#{n} {spec['id']} {job}: {rec['outcome']} {str(rec.get('detail'))[:300]}")
            if traced:
                d = os.path.join(self.out, sub, f"{n:04d}")
                os.makedirs(d, exist_ok=True)
                write_json(os.path.join(d, f"{job}.json.gz"), rec, gz=True)
            rows.setdefault(n, {})[job] = {k: rec.get(k) for k in ("outcome", "cpu", "wall", "test_cpu", "maxrss_kb", "detail")}

        wall = self.pool.run(work, body_of, on_done, lambda w: 10 * w[0]["seconds"] + 60)
        if traced:
            summary_path = os.path.join(self.out, "trace_summary.json")
            old = {}
            if os.path.exists(summary_path):
                with open(summary_path) as f:
                    old = json.load(f).get("rows", {})
            for n, jobs in rows.items():
                old.setdefault(str(n), {}).update(jobs)
            write_json(summary_path, {"rows": old, "wall": wall})
        else:
            d = os.path.join(self.out, sub)
            os.makedirs(d, exist_ok=True)
            for n, jobs in rows.items():
                write_json(os.path.join(d, f"{n:04d}.json"), jobs)
        bad = sum(1 for jobs in rows.values() for r in jobs.values() if r["outcome"] != "pass")
        log(f"{mode}: {sum(len(j) for j in rows.values())} jobs in {wall:.0f}s wall, {bad} not pass")

    # -- duckdb single-thread check --------------------------------------------------------

    def duckcheck(self):
        specs = self.load_specs()
        ids = [s["id"] for s in specs]
        # Every test of the modules that create duckdb connections, both ways.
        mods = {s["module"] for s in specs}
        rows = [t for t in self.tests if unitrun.module_name_for(unitrun.parse_id(t["id"])[0], self.root) in mods]
        tmp = self.pool.tmp
        root, warm = self.root, self.warm
        res = {}

        def body_of(w):
            single = w

            def body():
                if single:
                    slices.duckdb_single_thread()
                vs = unitrun.run_tests(rows, root=root, warm=warm)
                with open(os.path.join(tmp, f"duck-{int(single)}.json"), "w") as f:
                    json.dump({v["id"]: v["outcome"] for v in vs}, f)

            return body

        def on_done(w, info):
            path = os.path.join(tmp, f"duck-{int(w)}.json")
            res[w] = json.load(open(path)) if os.path.exists(path) else None
            res[str(w) + "_cpu"] = info["cpu"]

        self.pool.run([False, True], body_of, on_done, lambda w: 3600)
        a, b = res.get(False) or {}, res.get(True) or {}
        diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        write_json(os.path.join(self.out, "duckcheck.json"), {
            "tests": len(rows), "default": a, "single": b, "diff": diff,
            "cpu_default": res.get("False_cpu"), "cpu_single": res.get("True_cpu"), "sliceable": ids})
        log(f"duckcheck: {len(rows)} tests, {len(diff)} verdict differences {diff[:10]}; "
            f"cpu default {res.get('False_cpu'):.1f}s single {res.get('True_cpu'):.1f}s; "
            f"not pass (single): {sorted(k for k, v in b.items() if v != 'pass')[:10]}")


def install_phases(col, front):
    """test_execute_tpcds: events inside parse_one (main), call-item pickling and
    sqlglot.executor.optimize go to `front`, everything else to the current bucket."""
    import sqlglot.executor as ex
    import tests.test_executor as te

    state = {"depth": 0, "prev": None, "load_front": False}

    def enter():
        if state["depth"] == 0:
            state["prev"] = col.cur
            col.switch(front)
        state["depth"] += 1

    def leave():
        state["depth"] -= 1
        if state["depth"] == 0:
            col.switch(state["prev"])

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
                out = real_dumps(obj)
            finally:
                leave()
            state["load_front"] = True
            return out
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


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tests", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--mode", required=True, choices=["discover", "trace", "calib", "duckcheck"])
    p.add_argument("--jobs", type=int, default=7)
    p.add_argument("--only", help="test numbers, e.g. 1106,1300-1400")
    p.add_argument("--modules", default=DEFAULT_MODULES)
    p.add_argument("--skip-done", action="store_true", help="trace: skip jobs with a record")
    args = p.parse_args(argv)
    t0 = time.perf_counter()
    server = Server(args)
    server.warm_up()
    if args.mode == "discover":
        server.discover()
    elif args.mode == "duckcheck":
        server.duckcheck()
    else:
        server.run_jobs(args.mode)
    try:
        os.rmdir(server.pool.tmp)
    except OSError:
        pass
    log(f"total wall {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
