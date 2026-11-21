"""Single-process unittest driver that reproduces how pytest runs sqlglot's suite.

Stdlib only; shared by the build-time tracer (lab/trace/tracer.py) and the runtime.

Model (verified against the sqlglot suite at the pinned commit):
* Every test is a unittest.TestCase method, id "tests/x/test_y.py::Class::method".
* pytest imports every test module during collection, in collection order, before
  running anything (prepend import mode; tests/ is a package so modules are named
  tests.x.test_y and the repo root is sys.path[0]). build_warm_state() does that.
* Tests run in collection order; consecutive tests of one class share one
  setUpClass/tearDownClass (+ class cleanups), consecutive classes of one module share
  setUpModule/tearDownModule (+ module cleanups). run_tests() does that.

Verdicts (what pytest would report for the item):
* "fail": failure, error, failing/erroring subtest, unexpected success, error in
  setUp/tearDown/cleanups, setUpModule/setUpClass error (every test of the
  module/class), tearDownClass/class-cleanup/tearDownModule error (the last test run
  of that class/module: pytest reports it at that item's teardown), module import
  error, unresolvable id, or an internal runner error (always printed to stderr).
* "skip": skipped (test, class, SkipTest in setUpClass/setUpModule) with no failure.
* "pass": otherwise.

Hooks (all optional, called synchronously; exceptions raised by hooks propagate to
the caller, they are never swallowed):
  before_group(group)            before setUpModule (if the module changes)/setUpClass
  before_test(entry)             right before TestCase.run
  after_test(entry, verdict)     right after TestCase.run (verdict may still change if
                                 this is the last test of its group and teardown fails)
  before_teardown(group)         before tearDownClass/class cleanups/module teardown
  after_group(group, verdicts)   after teardown, with the final verdicts of the group
  on_verdict(verdict)            streaming: called once per test as soon as its verdict
                                 is final (after_test for all but the last test of a
                                 group, after teardown for the last one)
`group` is a dict {"module", "class", "tests": [entry, ...], "index"}.
An entry is a dict with at least "id" (tests.json rows work as-is: "number", "id",
"file", "seconds"). A verdict is a dict:
  {"n", "id", "outcome": "pass"|"fail"|"skip", "phase", "detail", "wall", "cpu"}
where phase is one of "call", "import", "collect", "setUpModule", "setUpClass",
"tearDownClass", "tearDownModule", "runner" (where the first failure happened, or
"call" for passing tests), detail is a short text (first failure/skip reason) or None,
and wall/cpu are the TestCase.run duration in seconds (wall clock / process CPU).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import traceback

import unittest  # must precede any sqlglot import (sqlglot checks "unittest" in sys.modules)
import unittest.case

__all__ = [
    "WarmState",
    "load_tests_json",
    "module_name_for",
    "parse_id",
    "collection_files",
    "build_warm_state",
    "run_tests",
    "silence_std_fds",
]

DETAIL_LIMIT = 4000
PASS, FAIL, SKIP = "pass", "fail", "skip"

_NORECURSE_EXACT = {"_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}", "__pycache__"}


def _log(msg):
    try:
        print("unitrun: " + msg, file=sys.stderr, flush=True)
    except Exception:  # stderr closed: nothing else we can do
        pass


def _clip(text, limit=DETAIL_LIMIT):
    if text is None:
        return None
    text = str(text)
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n...[clipped]...\n" + text[-half:]


# -- tests.json / ids --------------------------------------------------------------


def load_tests_json(path):
    """Returns (tests, skipped) from tests.json: rows sorted by number, skipped ids."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    tests = sorted(data["tests"], key=lambda t: t["number"])
    return tests, list(data.get("skipped", []))


def parse_id(test_id):
    """'tests/a/test_b.py::Class::method' -> ('tests/a/test_b.py', 'Class', 'method')."""
    parts = test_id.split("::")
    if len(parts) != 3:
        raise ValueError(f"unsupported test id (want file::Class::method): {test_id!r}")
    return parts[0], parts[1], parts[2]


def module_name_for(file, root=None):
    """Module name pytest's prepend import mode gives a test file.

    Walks up from the file's directory while __init__.py exists (pytest's
    resolve_package_path). Without root, assumes every directory is a package.
    """
    rel = file.replace("\\", "/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    parts = rel.split("/")
    if root is None:
        return ".".join(parts)
    pkg_parts = [parts[-1]]
    for i in range(len(parts) - 1, 0, -1):
        if os.path.isfile(os.path.join(root, *parts[:i], "__init__.py")):
            pkg_parts.append(parts[i - 1])
        else:
            break
    return ".".join(reversed(pkg_parts))


def _discover_test_files(root, top):
    """pytest-ordered list of test_*.py / *_test.py files under root/top (relative)."""
    out = []

    def walk(rel):
        full = os.path.join(root, rel)
        try:
            names = sorted(os.listdir(full))
        except OSError:
            return
        for name in names:
            p = os.path.join(full, name)
            r = f"{rel}/{name}"
            if os.path.isdir(p):
                if name.startswith(".") or name.endswith(".egg") or name in _NORECURSE_EXACT:
                    continue
                walk(r)
            elif name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
                out.append(r)

    walk(top)
    return out


def collection_files(tests, root=None, include_uncollected=True):
    """Test files in pytest collection order.

    Order comes from tests.json (rows sorted by number). With root and
    include_uncollected, test files pytest imports but that hold no collected test
    (sqlglot: tests/test_docs.py, tests/test_integration_loader.py) are inserted at
    their pytest position, provided the discovered order is consistent with tests.json.
    """
    order = []
    seen = set()
    for t in sorted(tests, key=lambda t: t.get("number", 0)):
        f = t.get("file") or parse_id(t["id"])[0]
        if f not in seen:
            seen.add(f)
            order.append(f)
    if not (root and include_uncollected):
        return order
    tops = []
    for f in order:
        top = f.split("/", 1)[0]
        if top not in tops:
            tops.append(top)
    discovered = []
    for top in tops:
        discovered.extend(_discover_test_files(root, top))
    it = iter(discovered)
    if all(any(f == d for d in it) for f in order):  # tests.json order is a subsequence
        return discovered
    _log("discovered test-file order disagrees with tests.json; using tests.json order only")
    return order


# -- warm state ------------------------------------------------------------------------


class WarmState:
    """Result of build_warm_state().

    modules: imported module objects, in import order
    names: module names attempted, in order
    import_errors: {module name: traceback text} for modules that failed to import
    files: {module name: test file}
    """

    def __init__(self):
        self.modules = []
        self.names = []
        self.import_errors = {}
        self.files = {}
        self.wall = 0.0
        self.cpu = 0.0

    def module_failed(self, name):
        return name in self.import_errors


def build_warm_state(tests, root, *, include_uncollected=True, on_module=None, chdir=True):
    """Imports every test module in collection order, like pytest's collection.

    tests: tests.json rows. root: repo root (sqlglot checkout), put at sys.path[0].
    on_module(name, phase) with phase "before"/"after" is called around each import
    (tracers use it to attribute import-time coverage). Import errors are recorded in
    the returned WarmState (and printed to stderr), never raised.
    """
    t0, c0 = time.perf_counter(), time.process_time()
    root = os.path.abspath(root)
    if chdir:
        os.chdir(root)
    if not sys.path or os.path.abspath(sys.path[0] or ".") != root:
        if root in sys.path:
            sys.path.remove(root)
        sys.path.insert(0, root)
    state = WarmState()
    for f in collection_files(tests, root, include_uncollected):
        name = module_name_for(f, root)
        state.names.append(name)
        state.files[name] = f
        if on_module is not None:
            on_module(name, "before")
        try:
            state.modules.append(importlib.import_module(name))
        except KeyboardInterrupt:
            raise
        except BaseException:  # SyntaxError, SystemExit at import time, ...
            state.import_errors[name] = _clip(traceback.format_exc())
            _log(f"import of {name} failed:\n{state.import_errors[name]}")
        if on_module is not None:
            on_module(name, "after")
    state.wall = time.perf_counter() - t0
    state.cpu = time.process_time() - c0
    return state


# -- result object ---------------------------------------------------------------------


class _Result(unittest.TestResult):
    """Collects the outcome of one TestCase.run() without storing tracebacks."""

    def __init__(self):
        super().__init__()
        self.reset()

    def reset(self):
        self.failed = False
        self.passed = False
        self.skipped = False
        self.fail_detail = None
        self.skip_detail = None

    @property
    def detail(self):
        return self.fail_detail if self.failed else self.skip_detail

    def _describe(self, kind, test, err):
        try:
            text = self._exc_info_to_string(err, test) if err is not None else ""
        except Exception as e:  # formatting must never hide the failure itself
            text = f"<could not format exception: {e!r}>"
        return f"{kind}: {test}\n{text}"

    def _fail(self, kind, test, err):
        self.failed = True
        if self.fail_detail is None:  # keep the first failure
            self.fail_detail = _clip(self._describe(kind, test, err))

    def addDuration(self, test, elapsed):  # do not accumulate per-test durations
        pass

    def addError(self, test, err):
        self._fail("error", test, err)

    def addFailure(self, test, err):
        self._fail("failure", test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            kind = "subfailure" if issubclass(err[0], test.failureException) else "suberror"
            self._fail(kind, subtest, err)

    def addSuccess(self, test):
        self.passed = True

    def addSkip(self, test, reason):
        self.skipped = True
        if self.skip_detail is None:
            self.skip_detail = _clip(f"skip: {reason}")

    def addExpectedFailure(self, test, err):
        self.passed = True

    def addUnexpectedSuccess(self, test):
        self._fail("unexpected success", test, None)

    def verdict(self):
        if self.failed:
            return FAIL
        if self.passed:
            return PASS
        if self.skipped:
            return SKIP
        return None  # nothing reported: caller treats as runner error


# -- running ---------------------------------------------------------------------------


def _verdict(entry, outcome, phase, detail=None, wall=0.0, cpu=0.0):
    return {
        "n": entry.get("number"),
        "id": entry["id"],
        "outcome": outcome,
        "phase": phase,
        "detail": _clip(detail),
        "wall": wall,
        "cpu": cpu,
    }


def _fail_verdict(v, phase, detail):
    """Turns verdict v into a failure (keeps an earlier failure's phase/detail)."""
    if v["outcome"] != FAIL:
        v["outcome"] = FAIL
        v["phase"] = phase
        v["detail"] = _clip(detail)
    else:
        v["detail"] = _clip(f"{v['detail']}\n[{phase}] {detail}")


def _exc_text():
    return traceback.format_exc()


def _sort_entries(tests, index):
    entries = []
    for t in tests:
        if isinstance(t, str):
            t = dict(index[t]) if index and t in index else {"id": t}
        entries.append(t)
    if all("number" in e for e in entries):
        entries.sort(key=lambda e: e["number"])
    return entries


def _groups(entries, root):
    groups = []
    for e in entries:
        try:
            file, cls, _ = parse_id(e["id"])
            module = module_name_for(file, root)
        except ValueError:
            module, cls = None, None
        if groups and groups[-1]["module"] == module and groups[-1]["class"] == cls and module:
            groups[-1]["tests"].append(e)
        else:
            groups.append({"module": module, "class": cls, "tests": [e], "index": len(groups)})
    return groups


def run_tests(
    tests,
    *,
    root=None,
    index=None,
    warm=None,
    before_group=None,
    after_group=None,
    before_test=None,
    after_test=None,
    before_teardown=None,
    on_verdict=None,
):
    """Runs tests in collection order in this process; returns verdicts (in run order).

    tests: tests.json rows (sorted by "number" when every row has one) or ids (looked
    up in index, a {id: row} dict, to get their number). root: repo root, used to
    derive module names (defaults to the current directory). warm: the WarmState, so
    tests of modules that failed to import fail with phase "import" (the module is
    otherwise imported on demand). See the module docstring for hooks and verdicts.
    """
    root = os.path.abspath(root or os.getcwd())
    entries = _sort_entries(tests, index)
    groups = _groups(entries, root)
    result = _Result()
    verdicts = []
    # Current module fixture: name, whether setUpModule succeeded, the failure tuple to
    # apply to every later group of the same module when it did not.
    module_state = {"name": None, "setup_ok": False, "failure": None}

    def emit(v):
        if on_verdict is not None:
            on_verdict(v)

    def module_teardown(last_verdict):
        """tearDownModule + module cleanups of the current module (if set up)."""
        name = module_state["name"]
        ok = module_state["setup_ok"]
        module_state.update(name=None, setup_ok=False, failure=None)
        if name is None or not ok:
            return
        mod = sys.modules.get(name)
        teardown = getattr(mod, "tearDownModule", None)
        if teardown is not None:
            try:
                teardown()
            except Exception:
                if last_verdict is not None:
                    _fail_verdict(last_verdict, "tearDownModule", _exc_text())
        try:
            unittest.case.doModuleCleanups()
        except Exception:
            if last_verdict is not None:
                _fail_verdict(last_verdict, "tearDownModule", _exc_text())

    for gi, group in enumerate(groups):
        if before_group is not None:
            before_group(group)
        gentries = group["tests"]
        gverdicts = []
        is_last_of_module = gi + 1 == len(groups) or groups[gi + 1]["module"] != group["module"]
        setup_failure = None  # (outcome, phase, detail) applied to every test of the group
        cls = None
        class_set_up = False

        try:
            modname = group["module"]
            if modname is None:
                setup_failure = (FAIL, "collect", f"unsupported test id {gentries[0]['id']!r}")
            elif warm is not None and warm.module_failed(modname):
                setup_failure = (FAIL, "import", "module import failed:\n" + warm.import_errors[modname])
            else:
                # Module import (normally already done by build_warm_state).
                mod = sys.modules.get(modname)
                if mod is None:
                    try:
                        mod = importlib.import_module(modname)
                    except KeyboardInterrupt:
                        raise
                    except BaseException:
                        setup_failure = (FAIL, "import", "module import failed:\n" + _exc_text())
                # Module fixture.
                if setup_failure is None and module_state["name"] != modname:
                    module_teardown(None)  # no-op: the previous module was torn down already
                    module_state.update(name=modname, setup_ok=True, failure=None)
                    setup_module = getattr(mod, "setUpModule", None)
                    if setup_module is not None:
                        try:
                            setup_module()
                        except unittest.SkipTest as e:
                            module_state.update(setup_ok=False, failure=(SKIP, "setUpModule", f"skip: {e}"))
                        except KeyboardInterrupt:
                            raise
                        except BaseException:
                            detail = _exc_text()
                            try:
                                unittest.case.doModuleCleanups()
                            except Exception:
                                detail += "\n" + _exc_text()
                            module_state.update(setup_ok=False, failure=(FAIL, "setUpModule", detail))
                if setup_failure is None and module_state["failure"] is not None:
                    setup_failure = module_state["failure"]
                # Class resolution and class fixture.
                if setup_failure is None:
                    cls = getattr(mod, group["class"], None)
                    if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)):
                        setup_failure = (FAIL, "collect", f"{group['class']} is not a TestCase in {modname}")
                        cls = None
                if setup_failure is None and not getattr(cls, "__unittest_skip__", False):
                    try:
                        cls._classSetupFailed = False
                    except TypeError:
                        pass
                    try:
                        cls.setUpClass()
                        class_set_up = True
                    except unittest.SkipTest as e:
                        setup_failure = (SKIP, "setUpClass", f"skip: {e}")
                    except KeyboardInterrupt:
                        raise
                    except BaseException:
                        detail = _exc_text()
                        try:
                            cls._classSetupFailed = True
                        except TypeError:
                            pass
                        try:
                            cls.doClassCleanups()
                            for exc_info in getattr(cls, "tearDown_exceptions", []):
                                detail += "\n" + "".join(traceback.format_exception(*exc_info))
                        except Exception:
                            detail += "\n" + _exc_text()
                        setup_failure = (FAIL, "setUpClass", detail)
        except KeyboardInterrupt:
            raise
        except BaseException:  # bug in the runner itself: report, never swallow
            detail = "runner error during group setup:\n" + _exc_text()
            _log(detail)
            setup_failure = (FAIL, "runner", detail)

        # Tests.
        for ti, entry in enumerate(gentries):
            last = ti + 1 == len(gentries)
            if setup_failure is not None:
                outcome, phase, detail = setup_failure
                v = _verdict(entry, outcome, phase, detail)
                if before_test is not None:
                    before_test(entry)
                if after_test is not None:
                    after_test(entry, v)
            else:
                if before_test is not None:
                    before_test(entry)
                t0, c0 = time.perf_counter(), time.process_time()
                try:
                    method = parse_id(entry["id"])[2]
                    try:
                        case = cls(method)
                    except (ValueError, AttributeError) as e:
                        v = _verdict(entry, FAIL, "collect", f"cannot instantiate test: {e!r}")
                    else:
                        result.reset()
                        case.run(result)
                        outcome = result.verdict()
                        del case
                        if outcome is None:
                            v = _verdict(entry, FAIL, "runner", "TestCase.run reported no outcome")
                            _log(f"{entry['id']}: TestCase.run reported no outcome")
                        else:
                            v = _verdict(entry, outcome, "call", result.detail)
                except KeyboardInterrupt:
                    raise
                except BaseException:
                    detail = "runner error while running the test:\n" + _exc_text()
                    _log(f"{entry['id']}: {detail}")
                    v = _verdict(entry, FAIL, "runner", detail)
                v["wall"] = time.perf_counter() - t0
                v["cpu"] = time.process_time() - c0
                if after_test is not None:
                    after_test(entry, v)
            gverdicts.append(v)
            verdicts.append(v)
            if not last:
                emit(v)

        # Teardown (errors go to the last test of the class / module, like pytest).
        if before_teardown is not None:
            before_teardown(group)
        last_v = gverdicts[-1]
        try:
            if class_set_up and cls is not None:
                try:
                    cls.tearDownClass()
                except KeyboardInterrupt:
                    raise
                except BaseException:
                    _fail_verdict(last_v, "tearDownClass", _exc_text())
                cls.doClassCleanups()
                for exc_info in getattr(cls, "tearDown_exceptions", []):
                    _fail_verdict(last_v, "tearDownClass", "".join(traceback.format_exception(*exc_info)))
            if is_last_of_module:
                module_teardown(last_v)
        except KeyboardInterrupt:
            raise
        except BaseException:
            detail = "runner error during group teardown:\n" + _exc_text()
            _log(detail)
            _fail_verdict(last_v, "runner", detail)
        emit(last_v)
        if after_group is not None:
            after_group(group, gverdicts)

    return verdicts


def silence_std_fds(path=os.devnull):
    """Points fds 1 and 2 at path (for forked children whose parent owns stdout)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)


def _main(argv=None):
    """python -m br.unitrun --tests tests.json --root /opt/sqlglot [--inline-pool] [ids...]"""
    import argparse

    p = argparse.ArgumentParser(description=_main.__doc__)
    p.add_argument("--tests", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--inline-pool", action="store_true")
    p.add_argument("--json", help="write verdicts here")
    p.add_argument("ids", nargs="*", help="test ids or numbers (default: all)")
    a = p.parse_args(argv)
    tests, _ = load_tests_json(a.tests)
    by_id = {t["id"]: t for t in tests}
    by_n = {str(t["number"]): t for t in tests}
    sel = [by_n.get(i) or by_id.get(i) or {"id": i} for i in a.ids] if a.ids else tests
    warm = build_warm_state(tests, a.root)
    if a.inline_pool:
        try:
            from br import inlinepool  # noqa: PLC0415
        except ImportError:
            import inlinepool  # noqa: PLC0415  (run as a script from br/)

        inlinepool.install(warm.modules)
    t0, c0 = time.perf_counter(), time.process_time()
    verdicts = run_tests(sel, root=a.root, index=by_id, warm=warm)
    wall, cpu = time.perf_counter() - t0, time.process_time() - c0
    counts = {}
    for v in verdicts:
        counts[v["outcome"]] = counts.get(v["outcome"], 0) + 1
        if v["outcome"] == FAIL:
            print(f"FAIL {v['n']} {v['id']} [{v['phase']}]\n{v['detail']}", file=sys.stderr)
    summary = {
        "counts": counts,
        "warm_wall": warm.wall,
        "warm_cpu": warm.cpu,
        "run_wall": wall,
        "run_cpu": cpu,
        "import_errors": sorted(warm.import_errors),
    }
    print(json.dumps(summary), file=sys.stderr)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "verdicts": verdicts}, f, indent=0)
    return 0 if counts.get(FAIL, 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(_main())
