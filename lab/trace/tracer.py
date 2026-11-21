"""Build-time tracer for the sqlglot suite (runs inside the br-lab image).

    python /src/lab/trace/tracer.py --tests /src/kit/sample/tests.json --root /opt/sqlglot \
        --out /src/lab/out/trace/<pass> --pass isolated|inorder|calib --jobs 7

A server process builds the pytest-like warm state with br.unitrun (all test modules
imported in collection order, inline process pool installed), then forks children:

* isolated: one child per test (up to --jobs at once), sys.monitoring LINE+PY_START
  (each location fires once: callbacks return DISABLE) + an audit hook, the test run
  with its class setup. One record per test: <out>/<n:04d>.json.gz.
* inorder: one child runs every test in number order (classes grouped like pytest),
  calling sys.monitoring.restart_events() at each group setup, test start and group
  teardown; events go to the current test, group setup/teardown events to every test
  of the group. Same record format.
* calib: one child per test, no tracing; child CPU (os.wait4 rusage) and wall, plus
  warm-up CPU and fork cost. Output <out>/calib.json.

isolated/inorder also record the warm-up ("preload") coverage: <out>/preload.json (or
--preload-out). Every pass writes <out>/summary.json (calib: calib.json).

Coverage keys: code key = (co_filename relative to --root, co_qualname,
co_firstlineno), only for files under sqlglot/, tests/, sqlglotc/. A record:
  {"n", "id", "outcome": pass|fail|skip|timeout|crash, "phase", "detail" (non-pass),
   "cpu"   (isolated/calib: child user+sys from os.wait4; inorder: process CPU of
            TestCase.run), "wall" (isolated/calib: fork->reap; inorder: TestCase.run),
   "test_cpu", "test_wall" (TestCase.run only), "group_cpu", "group_wall" (inorder:
            setUpClass+tearDownClass of the test's group), "exit", "maxrss_kb",
   "codes": [[file, qualname, firstlineno], ...]            (PY_START seen)
   "lines": {file: {"qualname@firstlineno": [line, ...]}}  (LINE seen; may name code
            objects started before the test, e.g. resumed generators)
   "opens": [{"path", "mode", "imp", "src"}], "lists": [{"event", "path", "imp", "src"}],
   "subprocs": [{"event", "argv", "cwd"}], "new_modules": [module names imported],
   "audit_errors": int}
path is relative to --root when inside it, absolute otherwise; imp is True when the
call came from the import system (importlib bootstrap frames on the stack); src is
"file:line" of the innermost frame under --root (or null).
"""

from __future__ import annotations

import argparse
import gc
import gzip
import json
import os
import resource
import select
import signal
import sys
import time
import traceback

# unittest must be imported before anything imports sqlglot (sqlglot.expressions.core
# enables strict argument validation when "unittest" is in sys.modules).
import unittest  # noqa: F401

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO)
from br import inlinepool, unitrun  # noqa: E402

# Keep /src (namespace packages!) and the script dir off sys.path while tests run.
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") not in (_REPO, _SCRIPT_DIR)]

MON = sys.monitoring
DISABLE = MON.DISABLE
TRACKED_TOPS = ("sqlglot/", "tests/", "sqlglotc/")
SUBTRACE_DIR = os.path.join(_SCRIPT_DIR, "subtrace")

# -- coverage collector ----------------------------------------------------------------


class Bucket:
    __slots__ = ("codes", "lines", "opens", "lists", "subprocs", "sub_traced", "audit_errors")

    def __init__(self):
        self.codes = set()  # code keys
        self.lines = {}  # code key -> set(lines)
        self.opens = set()
        self.lists = set()
        self.subprocs = []
        self.sub_traced = []  # coverage files merged from traced Python subprocesses
        self.audit_errors = 0

    def merge(self, other):
        self.codes |= other.codes
        for k, s in other.lines.items():
            mine = self.lines.get(k)
            if mine is None:
                self.lines[k] = set(s)
            else:
                mine |= s
        self.opens |= other.opens
        self.lists |= other.lists
        self.subprocs.extend(other.subprocs)
        self.sub_traced.extend(other.sub_traced)
        self.audit_errors += other.audit_errors

    def merge_sub(self, path):
        """Merges a subtrace/sitecustomize.py coverage file into this bucket."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.codes.update(tuple(c) for c in data["codes"])
        n = 0
        for file, d in data["lines"].items():
            for qk, ls in d.items():
                q, _, l = qk.rpartition("@")
                k = (file, q, int(l))
                s = self.lines.get(k)
                if s is None:
                    self.lines[k] = s = set()
                s.update(ls)
                n += len(ls)
        self.sub_traced.append({"argv": data["argv"], "codes": len(data["codes"]), "lines": n})

    def coverage_json(self):
        lines = {}
        for (f, q, l), s in self.lines.items():
            lines.setdefault(f, {})[f"{q}@{l}"] = sorted(s)
        return sorted([list(k) for k in self.codes]), lines

    def to_json(self):
        codes, lines = self.coverage_json()
        return {
            "codes": codes,
            "lines": lines,
            "opens": [dict(path=p, mode=m, imp=i, src=s) for p, m, i, s in sorted(self.opens, key=repr)],
            "lists": [dict(event=e, path=p, imp=i, src=s) for e, p, i, s in sorted(self.lists, key=repr)],
            "subprocs": self.subprocs,
            "subprocs_traced": self.sub_traced,
            "audit_errors": self.audit_errors,
        }


class Collector:
    """sys.monitoring LINE/PY_START + audit hook, writing into the current Bucket."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.prefix = self.root.rstrip("/") + "/"
        self.keys = {}  # id(code) -> code key or None
        self.refs = []  # keep code objects alive so ids stay unique
        self.cur = Bucket()
        self.tool = None
        self.audit_on = False
        self._missing = object()

    # sys.monitoring ------------------------------------------------------------------

    def _key(self, code):
        k = None
        fn = code.co_filename
        if fn.startswith(self.prefix):
            rel = fn[len(self.prefix):]
            if rel.startswith(TRACKED_TOPS):
                k = (rel, code.co_qualname, code.co_firstlineno)
        self.keys[id(code)] = k
        self.refs.append(code)
        return k

    def _on_start(self, code, offset):
        k = self.keys.get(id(code), self._missing)
        if k is self._missing:
            k = self._key(code)
        if k is not None:
            self.cur.codes.add(k)
        return DISABLE

    def _on_line(self, code, line):
        k = self.keys.get(id(code), self._missing)
        if k is self._missing:
            k = self._key(code)
        if k is not None:
            s = self.cur.lines.get(k)
            if s is None:
                self.cur.lines[k] = s = set()
            s.add(line)
        return DISABLE

    def start_monitoring(self):
        for tid in range(6):
            if MON.get_tool(tid) is None:
                break
        else:
            raise RuntimeError("no free sys.monitoring tool id")
        MON.use_tool_id(tid, "br-tracer")
        MON.register_callback(tid, MON.events.PY_START, self._on_start)
        MON.register_callback(tid, MON.events.LINE, self._on_line)
        MON.set_events(tid, MON.events.PY_START | MON.events.LINE)
        MON.restart_events()
        self.tool = tid

    def stop_monitoring(self):
        if self.tool is None:
            return
        tid, self.tool = self.tool, None
        MON.set_events(tid, 0)
        MON.register_callback(tid, MON.events.PY_START, None)
        MON.register_callback(tid, MON.events.LINE, None)
        MON.free_tool_id(tid)
        MON.restart_events()

    def switch(self, bucket, restart=True):
        self.cur = bucket
        if restart and self.tool is not None:
            MON.restart_events()

    # audit hook ---------------------------------------------------------------------

    def _rel(self, path):
        try:
            if isinstance(path, int):
                return f"<fd {path}>"
            path = os.fspath(path)
            if isinstance(path, bytes):
                path = os.fsdecode(path)
            path = os.path.normpath(os.path.join(os.getcwd(), path))
        except Exception:
            return repr(path)
        if path.startswith(self.prefix):
            return path[len(self.prefix):]
        return path

    def _origin(self):
        """(imp, src): import-system frame on the stack?, innermost frame under root."""
        imp = False
        src = None
        f = sys._getframe(2)  # 0: _origin, 1: _audit, 2: Python caller of the audited call
        prefix = self.prefix
        while f is not None:
            fn = f.f_code.co_filename
            if fn.startswith("<frozen importlib") or "importlib/_bootstrap" in fn:
                imp = True
            elif src is None and fn.startswith(prefix):
                src = f"{fn[len(prefix):]}:{f.f_lineno}"
            f = f.f_back
        return imp, src

    def _audit(self, event, args):
        if event not in _WATCHED or not self.audit_on:
            return
        try:
            self.audit_on = False  # no recursion through our own calls
            if event == "open":
                path, mode, flags = args
                if mode is None:
                    acc = flags & (os.O_WRONLY | os.O_RDWR) if isinstance(flags, int) else 0
                    mode = "r" if acc == 0 and not (isinstance(flags, int) and flags & os.O_CREAT) else "w"
                imp, src = self._origin()
                self.cur.opens.add((self._rel(path), str(mode), imp, src))
            elif event in ("os.listdir", "os.scandir", "glob.glob", "glob.glob/2"):
                path = args[0] if args else None
                if path is None:
                    path = "."
                if event.startswith("glob.glob"):
                    p = os.fsdecode(path) if isinstance(path, bytes) else str(path)
                    root_dir = args[2] if event == "glob.glob/2" and len(args) > 2 else None
                    rel = self._rel(os.path.join(os.fspath(root_dir), p) if root_dir else p)
                else:
                    rel = self._rel(path)
                imp, src = self._origin()
                self.cur.lists.add((event, rel, imp, src))
            else:
                if event == "subprocess.Popen":
                    executable, argv, cwd, _env = args
                    argv = [os.fsdecode(a) if isinstance(a, bytes) else str(a) for a in (argv if isinstance(argv, (list, tuple)) else [argv])]
                    rec = {"event": event, "argv": argv, "cwd": None if cwd is None else str(cwd)}
                elif event == "os.system":
                    rec = {"event": event, "argv": [str(args[0])], "cwd": os.getcwd()}
                elif event == "os.spawn":
                    rec = {"event": event, "argv": [str(a) for a in args[2]], "cwd": os.getcwd()}
                else:  # os.exec, os.posix_spawn: (path, argv, env)
                    rec = {"event": event, "argv": [str(a) for a in args[1]], "cwd": os.getcwd()}
                _imp, rec["src"] = self._origin()
                self.cur.subprocs.append(rec)
        except Exception:
            self.cur.audit_errors += 1
        finally:
            self.audit_on = True

    # Python subprocesses ------------------------------------------------------------

    def begin_subtrace(self, dirpath):
        """Makes Python subprocesses started from now on record coverage into dirpath."""
        os.makedirs(dirpath, exist_ok=True)
        self._env_saved = {k: os.environ.get(k) for k in ("PYTHONPATH", "BR_SUBTRACE_DIR", "BR_SUBTRACE_ROOT")}
        pp = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = SUBTRACE_DIR + (os.pathsep + pp if pp else "")
        os.environ["BR_SUBTRACE_DIR"] = dirpath
        os.environ["BR_SUBTRACE_ROOT"] = self.root
        self._sub_dir = dirpath

    def end_subtrace(self, bucket):
        """Restores the environment and merges subprocess coverage into bucket."""
        for k, v in self._env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        d = self._sub_dir
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            if name.endswith(".json"):
                try:
                    bucket.merge_sub(path)
                except Exception as e:
                    bucket.sub_traced.append({"argv": None, "error": repr(e)})
            os.remove(path)
        os.rmdir(d)

    def start_audit(self):
        sys.addaudithook(self._audit)  # cannot be removed; only install in children
        self.audit_on = True


_WATCHED = frozenset(
    {
        "open",
        "os.listdir",
        "os.scandir",
        "glob.glob",
        "glob.glob/2",
        "subprocess.Popen",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.system",
    }
)

# -- helpers ---------------------------------------------------------------------------


def log(msg):
    print(f"[tracer {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def rusage_cpu(ru):
    return ru.ru_utime + ru.ru_stime


def self_cpu():
    return rusage_cpu(resource.getrusage(resource.RUSAGE_SELF))


def os_threads():
    try:
        return len(os.listdir("/proc/self/task"))
    except OSError:
        return -1


def write_bytes(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def write_json(path, obj, gz=False):
    data = json.dumps(obj, separators=(",", ":")).encode()
    write_bytes(path, gzip.compress(data, 6) if gz else data)


def parse_only(spec):
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


# -- server ----------------------------------------------------------------------------


class Server:
    def __init__(self, args):
        self.args = args
        self.root = os.path.abspath(args.root)
        self.tests, self.skipped = unitrun.load_tests_json(args.tests)
        self.by_id = {t["id"]: t for t in self.tests}
        self.selected = self.tests
        if args.only:
            only = parse_only(args.only)
            self.selected = [t for t in self.tests if t["number"] in only]
        if args.limit:
            self.selected = self.selected[: args.limit]
        self.out = os.path.abspath(args.out)
        os.makedirs(self.out, exist_ok=True)
        self.tmp = os.path.join(self.out, "tmp")
        self.logs = os.path.join(self.out, "logs")
        os.makedirs(self.tmp, exist_ok=True)
        os.makedirs(self.logs, exist_ok=True)
        self.warm = None
        self.warm_info = {}

    # warm-up -----------------------------------------------------------------------

    def warm_up(self, trace):
        cpu_start = self_cpu()
        t0 = time.perf_counter()
        collector = Collector(self.root) if trace else None
        by_module = {}
        new_modules = {}
        before = {}

        def on_module(name, phase):
            if phase == "before":
                before[name] = set(sys.modules)
                if collector is not None:
                    b = by_module[name] = Bucket()
                    collector.switch(b)
            else:
                new_modules[name] = sorted(set(sys.modules) - before.pop(name))
                if collector is not None:
                    collector.switch(Bucket(), restart=False)

        if collector is not None:
            collector.start_monitoring()
        self.warm = unitrun.build_warm_state(self.tests, self.root, on_module=on_module)
        if collector is not None:
            collector.stop_monitoring()
        patched = inlinepool.install(self.warm.modules)
        wall = time.perf_counter() - t0
        cpu = self_cpu()
        self.warm_info = {
            "wall": wall,
            "cpu_total": cpu,  # interpreter startup + tracer imports + warm-up
            "cpu_warmup": cpu - cpu_start,
            "unitrun_wall": self.warm.wall,
            "unitrun_cpu": self.warm.cpu,
            "modules": self.warm.names,
            "import_errors": self.warm.import_errors,
            "inline_pool_patched": patched,
            "os_threads": os_threads(),
            "maxrss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "hashseed": os.environ.get("PYTHONHASHSEED"),
            "traced": bool(trace),
        }
        log(f"warm-up: {len(self.warm.names)} modules, wall {wall:.2f}s, cpu {cpu - cpu_start:.2f}s, "
            f"import errors {len(self.warm.import_errors)}, os threads {self.warm_info['os_threads']}")
        if collector is not None:
            union = Bucket()
            for b in by_module.values():
                union.merge(b)
            codes, lines = union.coverage_json()
            preload = {
                "root": self.root,
                "order": self.warm.names,
                "files": self.warm.files,
                "import_errors": self.warm.import_errors,
                "wall": wall,
                "cpu": cpu - cpu_start,
                "codes": codes,
                "lines": lines,
                "new_modules": new_modules,
                "by_module": {},
            }
            for name, b in by_module.items():
                c, l = b.coverage_json()
                preload["by_module"][name] = {"codes": c, "lines": l}
            path = self.args.preload_out or os.path.join(self.out, "preload.json")
            write_json(path, preload)
            log(f"preload: {len(codes)} codes, {sum(len(v) for f in lines.values() for v in f.values())} lines -> {path}")
        gc.collect()
        gc.freeze()

    # child plumbing -----------------------------------------------------------------

    def _fork(self, body, cpu=None, log_path=None):
        """Forks a child running body(); the child always ends with os._exit."""
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                if cpu is not None:
                    os.sched_setaffinity(0, {cpu})
                if log_path is not None:
                    unitrun.silence_std_fds(log_path)
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                body()
            except BaseException:
                code = 3
                try:
                    traceback.print_exc()
                    sys.stderr.flush()
                except BaseException:
                    pass
            finally:
                os._exit(code)
        return pid

    def run_children(self, jobs, make_body, on_done, timeout_of, pins=None, order=None):
        """Runs one child per entry, at most `jobs` at once; on_done(entry, info)."""
        pending = list(order if order is not None else self.selected)
        running = {}  # pidfd -> dict
        free_cpus = list(pins) if pins else None
        done = 0
        total = len(pending)
        t_start = time.perf_counter()
        last_report = t_start
        def spawn():
            while pending and len(running) < jobs:
                entry = pending.pop(0)
                cpu = free_cpus.pop(0) if free_cpus else None
                log_path = os.path.join(self.tmp, f"{entry['number']:04d}.log")
                t0 = time.perf_counter()
                pid = self._fork(make_body(entry), cpu=cpu, log_path=log_path)
                fd = os.pidfd_open(pid)
                running[fd] = {
                    "pid": pid, "entry": entry, "t0": t0, "cpu_slot": cpu,
                    "deadline": t0 + timeout_of(entry), "log": log_path,
                }

        try:
            while pending or running:
                spawn()
                now = time.perf_counter()
                wait = max(0.0, min(r["deadline"] for r in running.values()) - now)
                ready, _, _ = select.select(list(running), [], [], min(wait, 5.0))
                now = time.perf_counter()
                finished = [(fd, False) for fd in ready]
                for fd, r in running.items():
                    if fd not in ready and now >= r["deadline"]:
                        try:
                            os.kill(r["pid"], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        finished.append((fd, True))
                reaped = []
                for fd, timed_out in finished:
                    r = running.pop(fd)
                    _, status, ru = os.wait4(r["pid"], 0)
                    wall = time.perf_counter() - r["t0"]
                    os.close(fd)
                    if free_cpus is not None and r["cpu_slot"] is not None:
                        free_cpus.append(r["cpu_slot"])
                    reaped.append((r["entry"], {
                        "cpu": rusage_cpu(ru),
                        "wall": wall,
                        "exit": os.waitstatus_to_exitcode(status),
                        "maxrss_kb": ru.ru_maxrss,
                        "timed_out": timed_out,
                        "log": r["log"],
                    }))
                spawn()  # start replacements before post-processing the finished ones
                for entry, info in reaped:
                    on_done(entry, info)
                    done += 1
                if time.perf_counter() - last_report > 30:
                    last_report = time.perf_counter()
                    log(f"{done}/{total} done, {len(running)} running, {last_report - t_start:.0f}s")
        finally:
            for r in running.values():
                try:
                    os.kill(r["pid"], signal.SIGKILL)
                    os.waitpid(r["pid"], 0)
                except OSError:
                    pass
        return time.perf_counter() - t_start

    def timeout_of(self, entry):
        return self.args.timeout_mult * entry.get("seconds", 1.0) + self.args.timeout_add

    def _keep_log(self, n, path, keep):
        try:
            if keep and os.path.getsize(path) > 0:
                os.replace(path, os.path.join(self.logs, f"{n:04d}.log"))
            else:
                os.remove(path)
        except OSError:
            pass

    # isolated pass ------------------------------------------------------------------

    def run_isolated(self):
        warm = self.warm
        root = self.root

        def make_body(entry):
            def body():
                n = entry["number"]
                col = Collector(root)
                bucket = Bucket()
                col.cur = bucket
                mods_before = set(sys.modules)
                col.begin_subtrace(os.path.join(self.tmp, f"sub-{n:04d}"))
                col.start_audit()
                col.start_monitoring()
                verdicts = unitrun.run_tests([entry], root=root, warm=warm)
                col.stop_monitoring()
                col.audit_on = False
                col.end_subtrace(bucket)
                v = verdicts[0]
                meta = {
                    "outcome": v["outcome"],
                    "phase": v["phase"],
                    "detail": v["detail"] if v["outcome"] != "pass" else None,
                    "test_cpu": v["cpu"],
                    "test_wall": v["wall"],
                }
                rec = dict(meta, new_modules=sorted(set(sys.modules) - mods_before))
                rec.update(bucket.to_json())
                path = os.path.join(self.tmp, f"{n:04d}")
                # Body first, meta last: meta present => body complete.
                with open(path + ".body", "w", encoding="utf-8") as f:
                    json.dump(rec, f, separators=(",", ":"))
                with open(path + ".meta.tmp", "w", encoding="utf-8") as f:
                    json.dump(meta, f)
                os.replace(path + ".meta.tmp", path + ".meta")

            return body

        rows = []

        def on_done(entry, info):
            n = entry["number"]
            base = os.path.join(self.tmp, f"{n:04d}")
            head = {"n": n, "id": entry["id"], "cpu": info["cpu"], "wall": info["wall"],
                    "exit": info["exit"], "maxrss_kb": info["maxrss_kb"]}
            raw = meta = None
            if os.path.exists(base + ".meta"):
                with open(base + ".meta", encoding="utf-8") as f:
                    meta = json.load(f)
                with open(base + ".body", "rb") as f:
                    raw = f.read()
            for suffix in (".meta", ".body"):
                try:
                    os.remove(base + suffix)
                except OSError:
                    pass
            if info["timed_out"]:
                outcome = "timeout"
            elif meta is None or info["exit"] != 0:
                outcome = "crash"
            else:
                outcome = meta["outcome"]
            if outcome not in ("timeout", "crash"):
                # Splice the parent-measured fields in front of the child's JSON object.
                prefix = json.dumps(head, separators=(",", ":"))[:-1] + ","
                data = prefix.encode() + raw[1:]
                write_bytes(os.path.join(self.out, f"{n:04d}.json.gz"), gzip.compress(data, 6))
                row = dict(head, outcome=outcome, phase=meta["phase"], test_cpu=meta["test_cpu"],
                           test_wall=meta["test_wall"], detail=meta["detail"], size=len(data))
            else:
                rec = dict(head, outcome=outcome, phase="runner", detail=f"child {outcome} (exit {info['exit']})",
                           codes=[], lines={}, opens=[], lists=[], subprocs=[], new_modules=[], audit_errors=0)
                write_json(os.path.join(self.out, f"{n:04d}.json.gz"), rec, gz=True)
                row = dict(head, outcome=outcome, phase="runner", detail=rec["detail"], size=0)
            self._keep_log(n, info["log"], outcome != "pass")
            if outcome != "pass":
                log(f"#{n} {entry['id']}: {outcome}")
            rows.append(row)

        # Longest first, for packing.
        order = sorted(self.selected, key=lambda t: -t.get("seconds", 0))
        wall = self.run_children(self.args.jobs, make_body, on_done, self.timeout_of, order=order)
        self.write_summary("isolated", rows, wall)

    # in-order pass ------------------------------------------------------------------

    def run_inorder(self):
        warm = self.warm
        root = self.root
        selected = self.selected
        out = self.out

        def body():
            col = Collector(root)
            rows = []
            state = {"group": None, "tests": {}, "mods": None, "gcpu": 0.0, "gwall": 0.0, "t": None}

            def switch(bucket):
                col.audit_on = False
                col.switch(bucket)
                col.audit_on = True

            def before_group(group):
                state["group"] = Bucket()
                state["tests"] = {}
                state["gcpu"], state["gwall"] = 0.0, 0.0
                state["t"] = (time.process_time(), time.perf_counter())
                switch(state["group"])

            def before_test(entry):
                c, w = state["t"]
                state["gcpu"] += time.process_time() - c
                state["gwall"] += time.perf_counter() - w
                b = Bucket()
                state["tests"][entry["number"]] = [b, None, None]
                state["mods"] = set(sys.modules)
                col.begin_subtrace(os.path.join(self.tmp, f"sub-{entry['number']:04d}"))
                switch(b)

            def after_test(entry, verdict):
                slot = state["tests"][entry["number"]]
                slot[2] = sorted(set(sys.modules) - state["mods"])
                col.switch(state["group"], restart=False)
                col.audit_on = False
                col.end_subtrace(slot[0])
                col.audit_on = True
                state["t"] = (time.process_time(), time.perf_counter())

            def before_teardown(group):
                switch(state["group"])

            def after_group(group, verdicts):
                c, w = state["t"]
                state["gcpu"] += time.process_time() - c
                state["gwall"] += time.perf_counter() - w
                col.audit_on = False
                g = state["group"]
                for v in verdicts:
                    b, _, new_mods = state["tests"][v["n"]]
                    b.merge(g)
                    rec = {
                        "n": v["n"], "id": v["id"], "outcome": v["outcome"], "phase": v["phase"],
                        "detail": v["detail"] if v["outcome"] != "pass" else None,
                        "cpu": v["cpu"], "wall": v["wall"], "test_cpu": v["cpu"], "test_wall": v["wall"],
                        "group_cpu": state["gcpu"], "group_wall": state["gwall"],
                        "group_size": len(verdicts), "exit": None, "maxrss_kb": None,
                        "new_modules": new_mods or [],
                    }
                    rec.update(b.to_json())
                    write_json(os.path.join(out, f"{v['n']:04d}.json.gz"), rec, gz=True)
                    rows.append({k: rec[k] for k in ("n", "id", "outcome", "phase", "detail", "cpu", "wall",
                                                     "group_cpu", "group_wall")})
                state["tests"] = {}
                col.cur = Bucket()
                col.audit_on = True

            col.start_audit()
            col.start_monitoring()
            t0, c0 = time.perf_counter(), time.process_time()
            unitrun.run_tests(selected, root=root, warm=warm, before_group=before_group,
                              after_group=after_group, before_test=before_test,
                              after_test=after_test, before_teardown=before_teardown)
            col.stop_monitoring()
            col.audit_on = False
            write_json(os.path.join(self.tmp, "inorder_rows.json"),
                       {"rows": rows, "wall": time.perf_counter() - t0, "cpu": time.process_time() - c0})

        result = {}

        def on_done(entry, info):
            result.update(info)

        total_timeout = sum(self.timeout_of(t) for t in selected) + 600
        fake = {"number": 9999, "id": "<inorder>"}
        wall = self.run_children(1, lambda e: body, on_done, lambda e: total_timeout, order=[fake])
        rows_path = os.path.join(self.tmp, "inorder_rows.json")
        child = {}
        if os.path.exists(rows_path):
            with open(rows_path, encoding="utf-8") as f:
                child = json.load(f)
            os.remove(rows_path)
        rows = child.get("rows", [])
        self._keep_log(9999, result.get("log", ""), True)
        if result.get("timed_out") or result.get("exit") != 0:
            log(f"in-order child failed: {result}")
        self.write_summary("inorder", rows, wall, extra={"child": result, "child_run_wall": child.get("wall"),
                                                         "child_run_cpu": child.get("cpu")})

    # calibration pass ---------------------------------------------------------------

    def measure_fork(self, reps=25):
        calls, cpus, walls = [], [], []
        for _ in range(reps):
            t0 = time.perf_counter()
            pid = os.fork()
            if pid == 0:
                os._exit(0)
            t1 = time.perf_counter()
            _, _, ru = os.wait4(pid, 0)
            t2 = time.perf_counter()
            calls.append(t1 - t0)
            walls.append(t2 - t0)
            cpus.append(rusage_cpu(ru))

        def med(xs):
            xs = sorted(xs)
            return xs[len(xs) // 2]

        # A child that runs an empty selection through unitrun (fork + bookkeeping).
        noop_cpu, noop_wall = [], []
        for _ in range(reps):
            t0 = time.perf_counter()
            pid = self._fork(lambda: unitrun.run_tests([], root=self.root, warm=self.warm))
            _, _, ru = os.wait4(pid, 0)
            noop_wall.append(time.perf_counter() - t0)
            noop_cpu.append(rusage_cpu(ru))
        return {
            "reps": reps,
            "fork_call_median": med(calls),
            "exit_child_cpu_median": med(cpus),
            "exit_child_wall_median": med(walls),
            "noop_child_cpu_median": med(noop_cpu),
            "noop_child_wall_median": med(noop_wall),
        }

    def run_calib(self):
        warm = self.warm
        root = self.root
        fork = self.measure_fork()
        log(f"fork cost: {fork}")

        def make_body(entry):
            def body():
                c0 = time.process_time()
                v = unitrun.run_tests([entry], root=root, warm=warm)[0]
                v["child_cpu_inside"] = time.process_time() - c0
                path = os.path.join(self.tmp, f"{entry['number']:04d}.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(v, f)

            return body

        rows = []

        def on_done(entry, info):
            n = entry["number"]
            path = os.path.join(self.tmp, f"{n:04d}.json")
            v = None
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    v = json.load(f)
                os.remove(path)
            if info["timed_out"]:
                outcome = "timeout"
            elif v is None or info["exit"] != 0:
                outcome = "crash"
            else:
                outcome = v["outcome"]
            row = {
                "n": n, "id": entry["id"], "outcome": outcome, "seconds": entry.get("seconds"),
                "cpu": info["cpu"], "wall": info["wall"], "maxrss_kb": info["maxrss_kb"],
                "exit": info["exit"],
                "test_cpu": v["cpu"] if v else None, "test_wall": v["wall"] if v else None,
                "detail": (v or {}).get("detail") if outcome != "pass" else None,
            }
            self._keep_log(n, info["log"], outcome != "pass")
            if outcome != "pass":
                log(f"#{n} {entry['id']}: {outcome}")
            rows.append(row)

        pins = [int(c) for c in self.args.pin.split(",")] if self.args.pin else None
        jobs = min(self.args.jobs, len(pins)) if pins else self.args.jobs
        order = sorted(self.selected, key=lambda t: -t.get("seconds", 0))
        wall = self.run_children(jobs, make_body, on_done, self.timeout_of, pins=pins, order=order)
        rows.sort(key=lambda r: r["n"])
        cpu = sum(r["cpu"] for r in rows)
        secs = sum(r["seconds"] or 0 for r in rows)
        calib = {
            "warm": self.warm_info,
            "fork": fork,
            "jobs": jobs,
            "pins": pins,
            "wall": wall,
            "totals": {
                "tests": len(rows),
                "child_cpu": cpu,
                "test_cpu": sum(r["test_cpu"] or 0 for r in rows),
                "recorded_seconds": secs,
                "ratio_child_cpu_to_seconds": cpu / secs if secs else None,
                "outcomes": _count(rows),
            },
            "tests": rows,
        }
        write_json(os.path.join(self.out, "calib.json"), calib)
        log(f"calib: {len(rows)} tests, child cpu {cpu:.1f}s vs recorded {secs:.1f}s "
            f"(ratio {cpu / secs if secs else float('nan'):.3f}), wall {wall:.1f}s")

    # summary ------------------------------------------------------------------------

    def write_summary(self, name, rows, wall, extra=None):
        rows.sort(key=lambda r: r["n"])
        summary = {
            "pass": name,
            "args": vars(self.args),
            "warm": self.warm_info,
            "wall": wall,
            "tests": len(rows),
            "outcomes": _count(rows),
            "cpu_sum": sum(r.get("cpu") or 0 for r in rows),
            "not_pass": [r for r in rows if r["outcome"] != "pass"],
            "rows": rows,
        }
        if extra:
            summary.update(extra)
        write_json(os.path.join(self.out, "summary.json"), summary)
        log(f"{name}: {len(rows)} tests in {wall:.1f}s, outcomes {summary['outcomes']}")


def _count(rows):
    c = {}
    for r in rows:
        c[r["outcome"]] = c.get(r["outcome"], 0) + 1
    return c


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tests", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--pass", dest="mode", required=True, choices=["isolated", "inorder", "calib"])
    p.add_argument("--jobs", type=int, default=7)
    p.add_argument("--only", help="test numbers, e.g. 1,5,10-20")
    p.add_argument("--limit", type=int)
    p.add_argument("--preload-out")
    p.add_argument("--pin", help="calib: comma-separated CPUs, one child per CPU")
    p.add_argument("--timeout-mult", type=float, default=10.0)
    p.add_argument("--timeout-add", type=float, default=30.0)
    args = p.parse_args(argv)
    t0 = time.perf_counter()
    server = Server(args)
    server.warm_up(trace=args.mode in ("isolated", "inorder"))
    if args.mode == "isolated":
        server.run_isolated()
    elif args.mode == "inorder":
        server.run_inorder()
    else:
        server.run_calib()
    try:
        os.rmdir(server.tmp)
    except OSError:
        pass
    log(f"total wall {time.perf_counter() - t0:.1f}s, server cpu {self_cpu():.1f}s")
    return 0


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)  # skip finalization (duckdb/nanobind print leak reports at exit)
