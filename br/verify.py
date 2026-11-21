"""Run candidate tests against the patched code in forked children.

The parent (the selector process) builds the warm state once: unittest, then
every test module in collection order, exactly like pytest's collection. Each
job forks a child that

1. makes the patch active:
   - *hot*: replaces the code object of every live function that runs a changed
     pinned code object (and of its enclosing functions, so closures created
     later get the new code too);
   - *on demand*: modules not imported yet are served from the patched source
     by a meta-path finder placed first;
   - data files (and patched sources read as data) are served by wrapping
     `open`, `io.open` and `io.open_code`;
2. proves it: every swapped function must hold the new code object;
3. runs the tests with unitrun (collection order, class fixtures, subtests)
   with the process pools replaced by the inline pool (real pickle round trips),
   streaming one JSON verdict per line to the parent.

Anything abnormal (crash, timeout, CPU limit, missing proof) leaves the
remaining tests without a verdict: they stay selected.
"""

from __future__ import annotations

import builtins
import gc
import importlib.abc
import importlib.machinery
import importlib.util
import io
import json
import os
import select
import signal
import sys
import time
import types

import unittest  # noqa: F401  (must precede sqlglot)

from . import codediff, unitrun

ROOT = os.environ.get("BR_ROOT", "/opt/sqlglot")


def module_name(path: str) -> str:
    stem = path[:-3]
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    return stem.replace("/", ".")


# ---------------------------------------------------------------------------
# Parent side


class Warm:
    """The warm state, built once in the selector process."""

    def __init__(self, tests_rows):
        self.rows = tests_rows
        self.state = None
        self.funcs = None

    def build(self):
        if self.state is None:
            self.state = unitrun.build_warm_state(self.rows, ROOT)
            gc.collect()
            self.funcs = _function_index()
            gc.freeze()
        return self.state


def _function_index():
    """{(co_filename, co_qualname, co_firstlineno): [function, ...]} for live functions."""
    index: dict = {}
    for obj in gc.get_objects():
        if type(obj) is types.FunctionType:
            co = obj.__code__
            index.setdefault((co.co_filename, co.co_qualname, co.co_firstlineno), []).append(obj)
    return index


class Job:
    def __init__(self, cid, tests, py_files, data_files, cpu_limit, wall_limit, stop_on_fail=False):
        self.cid = cid
        self.tests = tests  # tests.json rows, collection order
        self.py_files = py_files  # {path: (old_bytes, new_bytes)} with only runtime changes
        self.data_files = data_files  # {path: new_bytes}
        self.cpu_limit = cpu_limit
        self.wall_limit = wall_limit
        self.stop_on_fail = stop_on_fail
        self.slices = {}  # {test id: item indexes} for item-sliced tests


def run_job(warm: Warm, job: Job):
    """Returns ({test id: "pass"|"fail"|"skip"}, info dict)."""
    warm.build()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        os.close(r)
        code = 0
        try:
            _child(warm, job, w)
        except BaseException as exc:  # report and die; parent keeps the tests
            try:
                os.write(w, (json.dumps({"error": repr(exc)[:500]}) + "\n").encode())
            except Exception:
                pass
            code = 3
        finally:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(code)
    os.close(w)
    verdicts: dict = {}
    info = {"proof": None, "error": None, "timeout": False}
    deadline = time.monotonic() + job.wall_limit
    buf = b""
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                info["timeout"] = True
                os.kill(pid, signal.SIGKILL)
                break
            ready, _, _ = select.select([r], [], [], min(left, 1.0))
            if not ready:
                continue
            chunk = os.read(r, 65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "id" in msg:
                    verdicts[msg["id"]] = msg["outcome"]
                elif "proof" in msg:
                    info["proof"] = msg["proof"]
                elif "error" in msg:
                    info["error"] = msg["error"]
    finally:
        os.close(r)
        try:
            _, status, usage = os.wait4(pid, 0)
            info["status"] = status
            info["cpu"] = usage.ru_utime + usage.ru_stime
        except ChildProcessError:
            pass
    if not info.get("proof"):
        return {}, info  # the patch was not proven active: trust nothing
    return verdicts, info


# ---------------------------------------------------------------------------
# Child side


class _PatchedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, modules):
        self.modules = modules  # name -> (path, code)
        self.served = set()

    def find_spec(self, fullname, path=None, target=None):
        entry = self.modules.get(fullname)
        if entry is None:
            return None
        rel, code = entry
        full = os.path.join(ROOT, rel)
        loader = _PatchedLoader(self, fullname, code, full)
        is_pkg = rel.endswith("/__init__.py")
        spec = importlib.util.spec_from_file_location(
            fullname, full, loader=loader, submodule_search_locations=[os.path.dirname(full)] if is_pkg else None
        )
        return spec


class _PatchedLoader(importlib.abc.Loader):
    def __init__(self, finder, name, code, full):
        self.finder, self.name, self.code, self.full = finder, name, code, full

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        self.finder.served.add(self.name)
        module.__file__ = self.full
        exec(self.code, module.__dict__)


def _install_redirects(files: dict):
    """Serve patched bytes for the given absolute paths."""
    if not files:
        return
    real_open = io.open
    real_open_code = io.open_code

    def target(file):
        try:
            p = os.fspath(file)
        except TypeError:
            return None
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        if not isinstance(p, str):
            return None
        return files.get(os.path.abspath(p))

    def patched_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
        data = target(file) if isinstance(file, (str, bytes, os.PathLike)) else None
        if data is not None and not any(c in mode for c in "wax+"):
            raw = io.BytesIO(data)
            if "b" in mode:
                return raw
            return io.TextIOWrapper(raw, encoding=encoding or "utf-8", errors=errors, newline=newline)
        return real_open(file, mode, buffering, encoding, errors, newline, closefd, opener)

    def patched_open_code(path):
        data = target(path)
        if data is not None:
            return io.BytesIO(data)
        return real_open_code(path)

    builtins.open = patched_open
    io.open = patched_open
    io.open_code = patched_open_code


def _code_pairs(old_code, new_code):
    """{(qualname, firstlineno) of a pinned code object: patched code object}."""
    pairs = {}

    def walk(o, n):
        pairs[(o.co_qualname, o.co_firstlineno)] = n
        nc = codediff._children(n)
        for key, child in codediff._children(o).items():
            other = nc.get(key)
            if other is not None:
                walk(child, other)

    walk(old_code, new_code)
    return pairs


def _parents(code):
    """{id(child code): parent code} over a code tree."""
    out = {}
    stack = [code]
    while stack:
        c = stack.pop()
        for const in c.co_consts:
            if type(const) is types.CodeType:
                out[id(const)] = c
                stack.append(const)
    return out


def _activate(warm: Warm, job: Job):
    """Make the patch live in this (child) process. Returns a proof dict or raises."""
    proof = {"swapped": 0, "lazy": []}
    lazy = {}
    redirect = {os.path.join(ROOT, p): data for p, data in job.data_files.items()}
    for path, (old, new) in job.py_files.items():
        full = os.path.join(ROOT, path)
        old_code = codediff.compile_bytes(old, full)
        new_code = codediff.compile_bytes(new, full)
        redirect[full] = new  # readers of the source as data
        name = module_name(path)
        if name not in sys.modules:
            lazy[name] = (path, new_code)
            proof["lazy"].append(name)
            continue
        changes = codediff.diff_code(old_code, new_code)
        pairs = _code_pairs(old_code, new_code)
        parents = _parents(old_code)
        done = set()
        for change in changes:
            if change.import_time or change.kind != "function":
                raise RuntimeError(f"import-time change cannot be hot-swapped: {path} {change.key}")
            # The changed code object and every enclosing function: replacing
            # an enclosing function makes the closures it creates later new.
            chain = []
            code = change.code
            while code is not None and codediff._kind(code) == "function":
                chain.append(code)
                code = parents.get(id(code))
            live = 0
            for code in chain:
                new = pairs.get((code.co_qualname, code.co_firstlineno))
                if new is None:
                    raise RuntimeError(f"no patched counterpart for {code.co_qualname}")
                for fn in warm.funcs.get((full, code.co_qualname, code.co_firstlineno), ()):
                    live += 1
                    if id(fn) in done:
                        continue
                    if len(fn.__code__.co_freevars) != len(new.co_freevars):
                        raise RuntimeError(f"closure layout changed for {code.co_qualname}")
                    fn.__code__ = new
                    if fn.__code__ is not new:
                        raise RuntimeError("swap did not take")
                    done.add(id(fn))
                    proof["swapped"] += 1
            if not live:
                # No live function holds this code or an enclosing one. A test
                # may still reach it in a way we did not foresee: vouch for nothing.
                raise RuntimeError(f"no live function for {path}:{change.key}")
    if lazy:
        finder = _PatchedFinder(lazy)
        sys.meta_path.insert(0, finder)
        proof["finder"] = finder
    _install_redirects(redirect)
    return proof


def run_clean_job(tests_rows, job: Job, pristine: dict, choose):
    """Clean mode, forked from a process that has not imported sqlglot.

    The child imports the patched tree like pytest's collection, diffs the
    import-time state against `pristine` (see buildstate), then calls
    `choose(changed_cells, import_errors)` -> (tests_to_run, tests_to_keep) and
    runs tests_to_run. Returns (verdicts, info) where info["keep"] lists the
    ids to select without running and info["cells"] the changed cells.
    """
    if "sqlglot" in sys.modules:
        raise RuntimeError("clean mode needs a process without sqlglot")
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        code = 0
        try:
            _clean_child(tests_rows, job, pristine, choose, w)
        except BaseException as exc:
            try:
                os.write(w, (json.dumps({"error": repr(exc)[:500]}) + "\n").encode())
            except Exception:
                pass
            code = 3
        finally:
            os._exit(code)
    os.close(w)
    return _collect(pid, r, job)


def _collect(pid, r, job):
    verdicts: dict = {}
    info = {"proof": None, "error": None, "timeout": False, "keep": None, "cells": None}
    deadline = time.monotonic() + job.wall_limit
    buf = b""
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                info["timeout"] = True
                os.kill(pid, signal.SIGKILL)
                break
            ready, _, _ = select.select([r], [], [], min(left, 1.0))
            if not ready:
                continue
            chunk = os.read(r, 1 << 16)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "id" in msg:
                    verdicts[msg["id"]] = msg["outcome"]
                else:
                    for key in ("proof", "error", "keep", "cells", "served", "run", "funcs", "dicts", "subs"):
                        if key in msg:
                            info[key] = msg[key]
    finally:
        os.close(r)
        try:
            _, status, usage = os.wait4(pid, 0)
            info["status"] = status
            info["cpu"] = usage.ru_utime + usage.ru_stime
        except ChildProcessError:
            pass
    if not info.get("proof"):
        verdicts = {}
    return verdicts, info


def _limits(job):
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (max(1, int(job.cpu_limit)), max(2, int(job.cpu_limit) + 1)))
    except Exception:
        pass
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except Exception:
        pass


def _line_map(old: bytes, new: bytes):
    """Map patched line numbers to pinned ones (None for inserted lines)."""
    from difflib import SequenceMatcher

    a = old.split(b"\n")
    b = new.split(b"\n")
    out = {}
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            for d in range(j2 - j1):
                out[j1 + d + 1] = i1 + d + 1
    return out


def _resolve_class(class_id):
    mod, _, qual = class_id.partition(":")
    if "#" in qual:
        return None
    obj = sys.modules.get(mod)
    for part in qual.split("."):
        obj = getattr(obj, part, None) if obj is not None else None
    return obj if isinstance(obj, type) else None


def _function_cells(cells, job):
    """{cell: (path, qualname, pinned firstlineno) or None} for cells holding functions.

    A function-valued cell (method, classmethod, staticmethod, property
    accessor, module-level function) changed by its def header (defaults,
    decorators) is observable only by running it: its readers are the tests
    that started the pinned code object. None means the pinned code object
    cannot be identified (new function, line not mappable).
    """
    maps_ = {}
    out = {}
    for cell in cells:
        if cell[0] not in ("c", "g"):
            continue
        if cell[0] == "c":
            owner = _resolve_class(cell[1])
            value = owner.__dict__.get(cell[2]) if owner is not None else None
        else:
            module = sys.modules.get(cell[1])
            value = getattr(module, "__dict__", {}).get(cell[2]) if module is not None else None
        fn = value
        if isinstance(fn, (classmethod, staticmethod)):
            fn = fn.__func__
        elif isinstance(fn, property):
            fn = fn.fget
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        if not isinstance(fn, types.FunctionType):
            continue
        co = fn.__code__
        full = co.co_filename
        if not full.startswith(ROOT + "/"):
            out[cell] = None
            continue
        path = full[len(ROOT) + 1 :]
        line = co.co_firstlineno
        if path in job.py_files:
            if path not in maps_:
                maps_[path] = _line_map(*job.py_files[path])
            line = maps_[path].get(line)
        out[cell] = None if line is None else (path, co.co_qualname, line)
    return out


def _clean_child(tests_rows, job, pristine, choose, w):
    from . import statediff

    _limits(job)
    gc.disable()
    modules = {}
    redirect = {os.path.join(ROOT, p): data for p, data in job.data_files.items()}
    for path, (old, new) in job.py_files.items():
        full = os.path.join(ROOT, path)
        modules[module_name(path)] = (path, codediff.compile_bytes(new, full))
        redirect[full] = new
    finder = _PatchedFinder(modules)
    sys.meta_path.insert(0, finder)
    _install_redirects(redirect)
    state = unitrun.build_warm_state(tests_rows, ROOT)
    touched = sorted(finder.served)
    # Proof: every patched module that the warm state imports came from the patch.
    for name in modules:
        if name in sys.modules and name not in finder.served:
            raise RuntimeError(f"{name} was imported without the patch")
    gc.collect()  # dead dynamically created classes must not count as subclasses
    snap = statediff.snapshot(
        [m for m in sys.modules if m.startswith(("sqlglot", "tests"))],
        prefix=("sqlglot", "tests"),
        unordered=frozenset(map(tuple, pristine["unstable"])),
    )
    chaotic = set(map(tuple, pristine["chaotic"]))
    cells = [
        c
        for c in statediff.diff(pristine["cells"], snap, touched)
        if c not in chaotic or statediff.cell_module(c) in touched
    ]
    import_errors = sorted(state.import_errors)
    funcs = _function_cells(cells, job)
    dicts_now = set()
    for cell in cells:
        if cell[0] == "c":
            owner = _resolve_class(cell[1])
            if owner is not None and isinstance(owner.__dict__.get(cell[2]), dict):
                dicts_now.add(cell)
    from .policy import _subclass_ids

    subs_map = {c[1]: (sorted(_subclass_ids(c[1])) if _subclass_ids(c[1]) is not None else None) for c in cells if c[0] == "c"}
    to_run, to_keep = choose(cells, import_errors, funcs, dicts_now, subs_map)
    report = {
        "cells": [list(c) for c in cells[:5000]],
        "keep": sorted(to_keep),
        "run": [r["number"] for r in to_run],
        "funcs": {repr(k): v for k, v in list(funcs.items())[:500]},
        "dicts": [list(c) for c in sorted(dicts_now)][:500],
        "subs": subs_map,
    }
    os.write(w, (json.dumps(report) + "\n").encode())
    from . import inlinepool

    inlinepool.install([m for n, m in list(sys.modules.items()) if n.startswith("tests") and m is not None])
    os.write(w, (json.dumps({"proof": {"served": touched, "cells": len(cells)}}) + "\n").encode())

    def on_verdict(v):
        os.write(w, (json.dumps({"id": v["id"], "outcome": v["outcome"]}) + "\n").encode())

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    if to_run:
        unitrun.run_tests(to_run, root=ROOT, warm=state, on_verdict=on_verdict)


def _child(warm: Warm, job: Job, w: int):
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (max(1, int(job.cpu_limit)), max(2, int(job.cpu_limit) + 1)))
    except Exception:
        pass
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except Exception:
        pass
    gc.disable()
    proof = _activate(warm, job)
    from . import inlinepool

    inlinepool.install([m for n, m in list(sys.modules.items()) if n.startswith("tests") and m is not None])
    from . import slices as slmod

    slmod.duckdb_single_thread()  # stay far below the container's thread limit
    sliced = getattr(job, "slices", None) or {}
    if sliced:
        smap = slmod.load_default()
        for test_id, items in sliced.items():
            slmod.install_filter(test_id, items, smap)  # raises: no proof, nothing trusted
        proof["sliced"] = {k: len(v) for k, v in sliced.items()}
    finder = proof.pop("finder", None)
    os.write(w, (json.dumps({"proof": proof}) + "\n").encode())

    class _Stop(Exception):
        pass

    def on_verdict(v):
        os.write(w, (json.dumps({"id": v["id"], "outcome": v["outcome"]}) + "\n").encode())
        if job.stop_on_fail and v["outcome"] == "fail":
            raise _Stop()

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)  # tests must never write to the real stdout
    try:
        unitrun.run_tests(job.tests, root=ROOT, warm=warm.state, on_verdict=on_verdict)
    except _Stop:
        pass
    if finder is not None:
        os.write(w, (json.dumps({"served": sorted(finder.served)}) + "\n").encode())


# ---------------------------------------------------------------------------
# Lazy mode: state diff of modules that the warm state does not import.


def run_lazy_diff(job: Job, pristine_full: dict):
    """Fork (from a process without sqlglot), import every sqlglot module with
    the patch served, diff against the pinned all-modules snapshot.

    Returns info with "cells", "funcs", "dicts", "subs" (as run_clean_job), or
    an "error". No test runs here: the readers are verified later, in the warm
    state, with the patched module served on demand.
    """
    if "sqlglot" in sys.modules:
        raise RuntimeError("lazy diff needs a process without sqlglot")
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        code = 0
        try:
            _lazy_child(job, pristine_full, w)
        except BaseException as exc:
            try:
                os.write(w, (json.dumps({"error": repr(exc)[:500]}) + "\n").encode())
            except Exception:
                pass
            code = 3
        finally:
            os._exit(code)
    os.close(w)
    _, info = _collect(pid, r, job)
    return info


def _family(patched, graph):
    """Patched modules plus every sqlglot module that imports one of them, transitively."""
    if not graph:
        return None
    importers: dict = {}
    for mod, deps in graph.items():
        for dep in deps:
            importers.setdefault(dep, set()).add(mod)
    out = set()
    stack = list(patched)
    while stack:
        mod = stack.pop()
        if mod in out:
            continue
        if mod.endswith("__main__"):
            continue  # the CLI entry point is never imported by tests
        out.add(mod)
        stack.extend(importers.get(mod, ()))
        # "import sqlglot.a.b" also makes sqlglot.a an importer of sorts: packages
        # re-export submodules; include parent packages that import the module.
    return out


def _lazy_child(job, pristine_full, w):
    from . import buildstate, statediff
    from .policy import _subclass_ids

    _limits(job)
    gc.disable()
    modules = {}
    redirect = {os.path.join(ROOT, p): data for p, data in job.data_files.items()}
    for path, (old, new) in job.py_files.items():
        full = os.path.join(ROOT, path)
        modules[module_name(path)] = (path, codediff.compile_bytes(new, full))
        redirect[full] = new
    finder = _PatchedFinder(modules)
    sys.meta_path.insert(0, finder)
    _install_redirects(redirect)
    os.chdir(ROOT)
    family = _family([m for m in modules if m.startswith("sqlglot")], pristine_full.get("graph"))
    if family is None:
        failed = buildstate.import_all_sqlglot(ROOT)
    else:
        import importlib

        sys.path.insert(0, ROOT) if ROOT not in sys.path else None
        import sqlglot  # noqa: F401

        failed = []
        for name in sorted(family):
            try:
                importlib.import_module(name)
            except Exception:
                failed.append(name)
    for name in modules:
        if name.startswith("sqlglot") and name in sys.modules and name not in finder.served:
            raise RuntimeError(f"{name} was imported without the patch")
    if failed:
        raise RuntimeError(f"patched modules fail to import: {failed[:5]}")
    gc.collect()
    names = [m for m in sys.modules if m.startswith("sqlglot")]
    if family is not None:
        names = [m for m in names if m in family]
    snap = statediff.snapshot(names, prefix=("sqlglot",), unordered=frozenset(map(tuple, pristine_full["unstable"])))
    pinned = pristine_full["cells"]
    if family is not None:
        # Only cells owned by the family are comparable: everything else was
        # built with other modules loaded (registries) in the pinned snapshot.
        snap = {c: d for c, d in snap.items() if statediff.cell_module(c) in family}
        pinned = {c: d for c, d in pinned.items() if statediff.cell_module(c) in family}
    touched = sorted(finder.served)
    chaotic = set(map(tuple, pristine_full["chaotic"]))
    cells = [
        c for c in statediff.diff(pinned, snap, touched) if c not in chaotic or statediff.cell_module(c) in touched
    ]
    funcs = _function_cells(cells, job)
    dicts_now = set()
    for cell in cells:
        if cell[0] == "c":
            owner = _resolve_class(cell[1])
            if owner is not None and isinstance(owner.__dict__.get(cell[2]), dict):
                dicts_now.add(cell)
    subs_map = {}
    for c in cells:
        if c[0] == "c" and c[1] not in subs_map:
            ids = _subclass_ids(c[1])
            subs_map[c[1]] = sorted(ids) if ids is not None else None
    report = {
        "cells": [list(c) for c in cells[:5000]],
        "funcs": {repr(k): v for k, v in funcs.items()},
        "dicts": [list(c) for c in sorted(dicts_now)],
        "subs": subs_map,
        "keep": [],
        "run": [],
    }
    if len(cells) > 5000:
        raise RuntimeError("too many changed cells")
    os.write(w, (json.dumps(report) + "\n").encode())
    os.write(w, (json.dumps({"proof": {"served": touched, "cells": len(cells)}}) + "\n").encode())
