"""Tests for br.slices (run in br-lab: cd /opt/sqlglot && python /src/selftest/test_slices.py).

Pure tests (synthetic slice maps, the filter on a synthetic test class) also run on the
host; tests needing sqlglot skip unless BR_ROOT (default /opt/sqlglot) holds the checkout.
"""

from __future__ import annotations

import unittest  # noqa: F401  (before sqlglot, in every process)

import marshal
import os
import subprocess
import sys
import tempfile
import types
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from br import slices  # noqa: E402
from br.codediff import Change  # noqa: E402

ROOT = os.environ.get("BR_ROOT", "/opt/sqlglot")
HAVE_SQLGLOT = os.path.isdir(os.path.join(ROOT, "sqlglot")) and os.path.isdir(os.path.join(ROOT, "tests"))
MAPS = os.path.join(REPO, "maps")


def code(qualname, firstlineno):
    return types.SimpleNamespace(co_qualname=qualname, co_firstlineno=firstlineno)


def change(qualname, first, lines=(), whole=False, import_time=False):
    return Change(code(qualname, first), "function", import_time, set(lines), whole)


FIX = "optimizer/demo.sql"
FIX_PATH = "tests/fixtures/" + FIX


def blob(sets, files):
    return zlib.compress(marshal.dumps({"sets": sets, "files": files}))


def synthetic_map(path):
    """One test T with 4 items over FIX ("call" source) and a front/rest split.

    sqlglot/a.py:
      f@10: line 11 items {0}, line 12 items {1, 2}, line 13 common
      g@20: code entry items {3}, line 21 items {3}
    rest coverage: only f line 12 for item 2 (so items 0, 1 are front-only for f).
    """
    sets = [0b0001, 0b0110, 0b1000]
    files = {"sqlglot/a.py": {"c": {("f", 10): 0, ("g", 20): 2},
                              "l": {("f", 10): {11: 0, 12: 1, 13: slices.COMMON}, ("g", 20): {21: 2}}}}
    rest_sets = [0b0100]
    rest_files = {"sqlglot/a.py": {"c": {}, "l": {("f", 10): {12: 0, 13: slices.COMMON}}}}
    entry = {"module": "demo", "cls": "Demo", "method": "test_it",
             "sources": [("call", "load_sql_fixture_pairs", "load_sql_fixture_pairs", FIX, 4, 0)],
             "n": 4, "opens": [FIX_PATH, "tests/fixtures/other.csv"], "cpu_base": 1.0,
             "cpu_items": [0.1, 0.2, 0.3, 0.4], "seconds": 5.0,
             "cov": blob(sets, files), "rest": blob(rest_sets, rest_files)}
    with open(path, "wb") as f:
        marshal.dump({"version": 1, "tests": {"T": entry}}, f)
    return slices.SliceMap(path)


def pairs_text(n, changed=None):
    out = []
    for i in range(n):
        sql = f"SELECT {i}" if i != changed else f"SELECT {i} + 100"
        out.append(f"# title: q{i}\n{sql};\nSELECT {i};\n")
    return "\n".join(out).encode()


class AffectedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.smap = synthetic_map(os.path.join(cls.tmp.name, "slices.marshal"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def aff(self, changes, data=None):
        return slices.affected(self.smap, "T", [("sqlglot/a.py", c) for c in changes], data)

    def test_lines(self):
        self.assertEqual(self.aff([change("f", 10, [11])]), [0])
        self.assertEqual(self.aff([change("f", 10, [12])]), [1, 2])
        self.assertEqual(self.aff([change("f", 10, [11, 12])]), [0, 1, 2])
        self.assertEqual(self.aff([change("f", 10, [99])]), [])  # line never ran

    def test_common_and_unknowable(self):
        self.assertIsNone(self.aff([change("f", 10, [13])]))
        self.assertIsNone(self.aff([change("f", 10, whole=True)]))  # whole f includes line 13
        self.assertIsNone(self.aff([change("f", 10, [11], import_time=True)]))
        self.assertIsNone(slices.affected(self.smap, "nope", [], None))
        self.assertIsNone(slices.affected(None, "T", [], None))

    def test_whole_code_object(self):
        self.assertEqual(self.aff([change("g", 20, whole=True)]), [3])
        self.assertEqual(self.aff([change("h", 30, whole=True)]), [])  # never ran

    def test_other_files(self):
        self.assertEqual(slices.affected(self.smap, "T", [("sqlglot/b.py", change("f", 10, [11]))]), [])
        # T reads this source as data
        self.assertIsNone(slices.affected(self.smap, "T", [(FIX_PATH, change("f", 10, [11]))]))

    def test_data_changes(self):
        old = pairs_text(4)
        self.assertEqual(self.aff([], [(FIX_PATH, old, pairs_text(4, changed=2))]), [2])
        self.assertEqual(self.aff([change("f", 10, [11])], [(FIX_PATH, old, pairs_text(4, changed=3))]), [0, 3])
        self.assertIsNone(self.aff([], [("tests/fixtures/other.csv", b"a", b"b")]))  # opened data
        self.assertEqual(self.aff([], [("tests/fixtures/unread.sql", b"a", b"b")]), [])
        self.assertIsNone(self.aff([], [(FIX_PATH, None, old)]))  # created
        self.assertIsNone(self.aff([], [(FIX_PATH, old, pairs_text(5))]))  # entry added

    def test_split_and_cpu(self):
        ch = [("sqlglot/a.py", change("f", 10, [11, 12]))]
        items, rest = slices.affected_split(self.smap, "T", ch)
        self.assertEqual((items, rest), ([0, 1, 2], [2]))
        self.assertEqual(slices.affected_split(self.smap, "T", [("sqlglot/a.py", change("f", 10, [13]))]),
                         (None, None))
        self.assertAlmostEqual(slices.estimate_cpu(self.smap, "T", [0, 3]), 1.5)
        front = {"cpu": [0.01, 0.02, 0.03, 0.04]}
        check, worst = slices.estimate_front_cpu(self.smap, items, rest, front, test_id="T")
        self.assertAlmostEqual(check, 0.03)
        self.assertAlmostEqual(worst, 0.03 + 1.6)


def make_demo_module(name):
    """A test module shaped like the sliced sqlglot tests ("call" and "attr" sources)."""
    mod = types.ModuleType(name)
    mod.loaded = []
    mod.seen = []

    def load_sql_fixture_pairs(filename):
        mod.loaded.append(filename)
        n = {"a.sql": 5, "b.sql": 3}[filename]
        for i in range(n):
            yield ({}, f"{filename}:{i}", "")

    mod.load_sql_fixture_pairs = load_sql_fixture_pairs

    class Demo(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.rows = list(mod.load_sql_fixture_pairs("b.sql"))

        def test_it(self):
            for _meta, sql, _ in mod.load_sql_fixture_pairs("a.sql"):
                mod.seen.append(sql)
            for _meta, sql, _ in self.rows:
                mod.seen.append(sql)

    Demo.__module__ = name
    mod.Demo = Demo
    sys.modules[name] = mod
    return mod


SPEC = {"module": "br_demo_tests", "cls": "Demo", "method": "test_it", "n": 8,
        "sources": [("call", "load_sql_fixture_pairs", "load_sql_fixture_pairs", "a.sql", 5, 0),
                    ("attr", "rows", "load_sql_fixture_pairs", "b.sql", 3, 5)]}


class FilterSyntheticTest(unittest.TestCase):
    def setUp(self):
        self.mod = make_demo_module("br_demo_tests")
        self.mod.Demo.setUpClass()

    def tearDown(self):
        slices.uninstall_filters()
        sys.modules.pop("br_demo_tests", None)

    def run_it(self):
        self.mod.seen.clear()
        result = unittest.TestResult()
        self.mod.Demo("test_it").run(result)
        return result

    def test_round_trip(self):
        original = self.mod.Demo.__dict__["test_it"]
        sliced = slices.install_filter("demo", [1, 3, 6], spec=SPEC)
        self.assertEqual(sliced.__br_slice_items__, (1, 3, 6))
        r = self.run_it()
        self.assertTrue(r.wasSuccessful(), r.errors + r.failures)
        self.assertEqual(self.mod.seen, ["a.sql:1", "a.sql:3", "b.sql:1"])
        # the loader binding and the class attribute are back after the call
        self.assertEqual(self.mod.load_sql_fixture_pairs.__name__, "load_sql_fixture_pairs")
        self.assertFalse(hasattr(self.mod.load_sql_fixture_pairs, "__br_slice_real__"))
        self.assertEqual(len(self.mod.Demo.rows), 3)
        # re-filtering wraps the real method, not the previous wrapper
        again = slices.install_filter("demo", [0], spec=SPEC)
        self.assertIs(again.__br_slice_original__, original)
        self.run_it()
        self.assertEqual(self.mod.seen, ["a.sql:0"])
        slices.uninstall_filters()
        self.assertIs(self.mod.Demo.__dict__["test_it"], original)
        self.run_it()
        self.assertEqual(len(self.mod.seen), 8)

    def test_zero_items(self):
        slices.install_filter("demo", [], spec=SPEC)
        r = self.run_it()
        self.assertTrue(r.wasSuccessful())
        self.assertEqual(self.mod.seen, [])

    def test_count_mismatch_fails(self):
        bad = dict(SPEC, sources=[("call", "load_sql_fixture_pairs", "load_sql_fixture_pairs", "a.sql", 4, 0),
                                  ("attr", "rows", "load_sql_fixture_pairs", "b.sql", 3, 4)], n=7)
        slices.install_filter("demo", [0], spec=bad)
        r = self.run_it()
        self.assertFalse(r.wasSuccessful())
        self.assertIn("br.slices", "".join(t for _, t in r.errors + r.failures))
        bad2 = dict(SPEC, sources=[SPEC["sources"][0], ("attr", "rows", "load_sql_fixture_pairs", "b.sql", 2, 5)], n=7)
        slices.install_filter("demo", [6], spec=bad2)
        self.assertFalse(self.run_it().wasSuccessful())

    def test_out_of_range(self):
        with self.assertRaises(ValueError):
            slices.install_filter("demo", [8], spec=SPEC)


# ---------------------------------------------------------------------------
# With sqlglot (br-lab)

FP_CHILD = r"""
import unittest, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
from br import slices
from sqlglot import parse_one
from sqlglot.optimizer import optimize
schema = {"t": {"a": "INT", "b": "TEXT", "c": "DOUBLE"}, "u": {"a": "INT", "d": "DATE"}}
sqls = ["SELECT a, SUM(c) AS s FROM t WHERE b IN ('x', 'y') GROUP BY a HAVING SUM(c) > 1 ORDER BY 2",
        "WITH w AS (SELECT a FROM u WHERE d > DATE '2020-01-01') SELECT t.a, COUNT(*) FROM t JOIN w ON t.a = w.a GROUP BY 1",
        "SELECT /* hi */ CASE WHEN a > 1 THEN 'p' ELSE b END AS k FROM t UNION ALL SELECT CAST(a AS TEXT) FROM u"]
out = []
for sql in sqls:
    e = parse_one(sql)
    out.append(slices.fingerprint(e).hex())
    o = optimize(e, schema, leave_tables_isolated=True)
    hash(o)  # caches _hash on every node
    out.append(slices.fingerprint(o).hex())
print(" ".join(out))
"""


@unittest.skipUnless(HAVE_SQLGLOT, "needs the sqlglot checkout (br-lab)")
class FingerprintTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from sqlglot import exp, parse_one
        from sqlglot.optimizer import optimize

        cls.exp = exp
        cls.parse_one = staticmethod(parse_one)
        cls.optimize = staticmethod(optimize)

    def opt(self, sql="SELECT a, b FROM t WHERE a > 1"):
        o = self.optimize(self.parse_one(sql), {"t": {"a": "INT", "b": "TEXT"}}, leave_tables_isolated=True)
        hash(o)
        return o

    def test_across_hash_seeds(self):
        got = set()
        for seed in ("0", "1", "4242"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            r = subprocess.run([sys.executable, "-c", FP_CHILD, REPO, ROOT], env=env, cwd=ROOT,
                               capture_output=True, text=True, check=True)
            got.add(r.stdout.strip())
        self.assertEqual(len(got), 1, got)
        self.assertEqual(len(got.pop().split()), 6)

    def test_copy_equal_and_sensitive(self):
        a = self.opt()
        self.assertEqual(slices.fingerprint(self.opt()), slices.fingerprint(a))
        # copy() keeps _hash, types and meta but unshares shared nodes (shared DataType
        # objects): the digest sees sharing, so compare copies with copies.
        fa = slices.fingerprint(a.copy())
        self.assertEqual(slices.fingerprint(a.copy()), fa)
        b = a.copy()
        b.find(self.exp.Literal).set("this", "2")
        self.assertNotEqual(slices.fingerprint(b), fa)
        c = a.copy()
        c.find(self.exp.Column).add_comments(["x"])
        self.assertNotEqual(slices.fingerprint(c), fa)
        d = a.copy()
        d.find(self.exp.Column).type = "BIGINT"
        self.assertNotEqual(slices.fingerprint(d), fa)
        e = a.copy()
        e.find(self.exp.Column).meta["k"] = 1
        self.assertNotEqual(slices.fingerprint(e), fa)
        f = a.copy()
        leaf = f.find(self.exp.Identifier)  # copy() keeps cached hashes on leaves only
        self.assertIsNotNone(leaf._hash)
        leaf._hash = None  # cache presence is part of the digest
        self.assertNotEqual(slices.fingerprint(f), fa)
        g = a.copy()
        g.find(self.exp.Column).args["extra"] = False
        self.assertNotEqual(slices.fingerprint(g), fa)

    def test_never_equal_cases(self):
        a = self.opt()
        bad_hash = a.copy()
        bad_hash.find(self.exp.Column)._hash = 12345
        with self.assertRaises(slices.FrontMismatch):
            slices.fingerprint(bad_hash)
        stale = a.copy()
        stale.find(self.exp.Literal).args["this"] = "7"  # direct mutation: cached hashes stale
        with self.assertRaises(slices.FrontMismatch):
            slices.fingerprint(stale)
        ext = a.copy()
        ext.find(self.exp.Column).parent = self.exp.column("z")
        with self.assertRaises(slices.FrontMismatch):
            slices.fingerprint(ext)
        odd = a.copy()
        odd.find(self.exp.Column).meta["obj"] = object()
        with self.assertRaises(slices.FrontMismatch):
            slices.fingerprint(odd)
        extra = a.copy()
        node = extra.find(self.exp.Column)
        node.__dict__["note"] = "x"  # instance attributes are covered too
        self.assertNotEqual(slices.fingerprint(extra), slices.fingerprint(a))

    def test_replica_hash_matches_sqlglot(self):
        from sqlglot.expressions import Expr

        o = self.opt("SELECT a, COUNT(DISTINCT b) FROM t WHERE b LIKE 'x%' AND a IN (1, 2) GROUP BY a")
        memo = {}
        for node in o.walk():
            self.assertEqual(slices._replica_hash(node, memo, Expr), hash(node))


@unittest.skipUnless(HAVE_SQLGLOT, "needs the sqlglot checkout (br-lab)")
class RealSlicedTest(unittest.TestCase):
    """install_filter() on a real sliced test of the map, run like the runtime does."""

    TEST = "tests/test_optimizer.py::TestOptimizer::test_eliminate_ctes"

    @classmethod
    def setUpClass(cls):
        from br import inlinepool, unitrun

        cls.unitrun = unitrun
        cls.smap = slices.SliceMap()
        cls.t = cls.smap.get(cls.TEST)
        os.chdir(ROOT)
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import importlib

        importlib.import_module(cls.t.module)
        inlinepool.install([m for n, m in list(sys.modules.items()) if n.startswith("tests") and m is not None])
        cls.row = {"id": cls.TEST, "number": 0, "file": cls.TEST.split("::")[0]}

    @classmethod
    def tearDownClass(cls):
        from br import inlinepool

        slices.uninstall_filters()
        inlinepool.uninstall()

    def run_test(self):
        return self.unitrun.run_tests([self.row], root=ROOT)[0]

    def test_round_trip(self):
        self.assertIsNotNone(self.t)
        cls = getattr(sys.modules[self.t.module], self.t.cls)
        original = cls.__dict__[self.t.method]
        slices.install_filter(self.TEST, [0, self.t.n - 1], self.smap)
        self.assertEqual(self.run_test()["outcome"], "pass")
        slices.install_filter(self.TEST, [], self.smap)
        self.assertEqual(self.run_test()["outcome"], "pass")
        spec = slices._spec_of(self.TEST, self.smap, None)
        src = list(spec["sources"][0])
        src[4] += 1  # the map claims one entry more than the fixture has
        slices.install_filter(self.TEST, [0], spec=dict(spec, sources=[tuple(src)], n=spec["n"] + 1))
        v = self.run_test()
        self.assertEqual(v["outcome"], "fail")
        self.assertIn("br.slices", v["detail"] or "")
        slices.uninstall_filters()
        self.assertIs(cls.__dict__[self.t.method], original)
        self.assertEqual(self.run_test()["outcome"], "pass")


@unittest.skipUnless(HAVE_SQLGLOT and os.environ.get("BR_FAST") != "1", "needs br-lab (set BR_FAST=1 to skip)")
class FrontBuildTest(unittest.TestCase):
    """Pristine front digests: equal across two build runs (different hash seeds), and for
    a subset of items equal to the full in-order build in maps/front.marshal."""

    ITEMS = [0, 3, 17, 42, 98]

    def test_stable(self):
        shipped = slices.load_front()
        self.assertIsNotNone(shipped, "maps/front.marshal missing or of another version")
        self.assertEqual(shipped["unstable"], [])
        a = slices.build_front(None, ROOT, MAPS, seeds=(0,), items=self.ITEMS)
        b = slices.build_front(None, ROOT, MAPS, seeds=(7,), items=self.ITEMS)
        self.assertEqual(a["n"], shipped["n"])
        for i in self.ITEMS:
            self.assertIsNotNone(a["digests"][i])
            self.assertEqual(a["digests"][i], b["digests"][i])
            self.assertEqual(a["digests"][i], shipped["digests"][i])


if __name__ == "__main__":
    unittest.main(verbosity=2)
