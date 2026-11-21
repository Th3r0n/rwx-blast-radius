"""Item slices: prove a loop-over-fixture test passing by running only the items a patch reaches.

Some expensive tests loop over the entries ("items") of one fixture file: TPC-H/TPC-DS
queries (tests/test_executor.py), optimizer fixture pairs (TestOptimizer.check_file,
test_annotate_types, test_annotate_funcs). lab/trace/slices_trace.py ran each such
test T at the pinned commit once per item (T restricted to that single item, forked
from the warm state) and once with zero items, under the tracer's coverage recording.
lab/build_slices.py stored the result in maps/slices.marshal:

  {"version": 1, "tests": {test_id: entry}}
  entry:
    module, cls, method      where the test lives
    sources   [(kind, name, loader, file, n, offset), ...]  what T loops over:
              kind "call": T calls module.<name>(file) (name == loader) while it runs;
              kind "attr": setUpClass stored list(<loader>(file)) as cls.<name> and T
              reads self.<name>. Items of a source are numbered offset .. offset+n-1
              in yield order; `loader` is load_sql_fixture_pairs or load_sql_fixtures.
    n         number of items (all sources)
    opens     files T opened as data (not through the import system) in any run
    cpu_base  CPU seconds of T with zero items in a forked child (no tracing)
    cpu_items per item: CPU seconds T needs on top of cpu_base for that item
    seconds   recorded duration of the full test (tests.json)
    cov       zlib(marshal({"sets": [bitset, ...], "files": {path: {"c": {(qualname,
              firstlineno): sid}, "l": {(qualname, firstlineno): {line: sid}}}}}))
              sid indexes "sets" (bit i = item i ran it); sid -1 = "common": T ran it
              outside any single item (class setup, loop scaffolding, or only when
              several items run in sequence) - touching it affects every item.
    rest      same layout for test_execute_tpcds only: coverage outside the "front"
              phase (parse_one + the call-item pickle round trip, executor.optimize),
              see the front proof below; None otherwise.

maps/front.marshal (build_front; `python -m br.slices --build-front OUT ROOT MAPS`):
  {"version": FRONT_VERSION, "test": TPCDS_TEST, "n": 99, "digests": [bytes | None],
   "cpu": [per-item CPU of the front recomputation], "unstable": [...], "failed": [...]}
  digests of the pristine front of every item, built in a warm state under two hash
  seeds (an item whose digests differ is None, never equal).

Soundness model: a patch whose changed code objects/lines are not in T's common
coverage reaches item i only if item i ran a changed line (or started a code object
that changed structurally). The unaffected items run exactly the pristine code, and
every item passes at the pinned commit (checked by the build). Running T on the
affected items alone with the patch active therefore decides T's verdict, up to one
accepted gap: patched code run by an affected item that leaves different shared state
behind (a cache entry, a mutated module global) read by an unaffected item later in the
full run. Everything not understood returns None (= run the whole test).

Runtime integration (all stdlib). Never slice when the static stage widened to ALL,
when the patch creates/deletes/renames files, or when the patch is not hot-swappable.

(a) Item-sliced test T (any test in the map):
    parent:  smap = load_default()
             items = affected(smap, T, res.changes, res.data_changes)
               None -> T is not sliceable for this patch (run it whole or keep it)
               []   -> no item and no shared code reaches the patch: T passes unrun
               else -> cost estimate_cpu(smap, T, items); job.slices[T] = items
    child (verify._child, in this order):
             1. proof = verify._activate(warm, job)            patch live
             2. inlinepool.install(<loaded tests.* modules>)   pickling pool stand-in
             3. duckdb_single_thread()
             4. install_filter(T, items, smap)                 raises -> no proof
             5. write the proof, then unitrun.run_tests(job.tests): T sees only items

(b) test_execute_tpcds with the front proof (TPCDS_TEST):
    parent:  items, rest = affected_split(smap, TPCDS_TEST, res.changes, res.data_changes)
               items None / [] -> as in (a)
               else -> cost: check, worst = estimate_front_cpu(smap, items, rest)
                       (expected about check + estimate_cpu(smap, T, rest) if rest);
                       job.front[TPCDS_TEST] = (items, rest)
             front = load_front()      (maps/front.marshal; None -> use (a) instead)
    child:   1-3 as in (a), then, before any test runs:
             4. to_run = plan_in_child(TPCDS_TEST, items, rest, front, smap)
                  recomputes the patched front of the candidates (items - rest),
                  drops those whose digest equals the pristine one, and installs the
                  filter for the rest (raises -> treat T as unverified, keep it)
             5. write the proof; if to_run == []: report T "pass" and remove it from
                job.tests; else run job.tests (T sees only to_run).
    plan_in_child must run after _activate (it must see the patched code) and before
    run_tests (setUpClass state and other tests' side effects are not wanted in it).
    The child's CPU limit must allow the check (front cpu of the candidates, <= ~4 s).
"""

from __future__ import annotations

import functools
import importlib
import marshal
import os
import sys
import zlib

from . import fixtures as _fixtures

__all__ = [
    "SliceMap",
    "load_default",
    "affected",
    "affected_split",
    "install_filter",
    "uninstall_filters",
    "duckdb_single_thread",
    "estimate_cpu",
    "estimate_front_cpu",
    "fingerprint",
    "FrontMismatch",
    "front_equal",
    "front_digests",
    "load_front",
    "plan_in_child",
    "build_front",
]

LOADERS = ("load_sql_fixture_pairs", "load_sql_fixtures")
COMMON = -1
DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maps", "slices.marshal")


class SliceTest:
    """One sliceable test of the map (coverage decompressed on first use)."""

    def __init__(self, test_id, entry):
        self.id = test_id
        self.module = entry["module"]
        self.cls = entry["cls"]
        self.method = entry["method"]
        self.sources = [tuple(s) for s in entry["sources"]]
        self.n = entry["n"]
        self.opens = frozenset(entry.get("opens", ()))
        self.cpu_base = entry.get("cpu_base")
        self.cpu_items = entry.get("cpu_items")
        self.seconds = entry.get("seconds")
        self._blobs = {"cov": entry.get("cov"), "rest": entry.get("rest")}
        self._cache = {}

    def coverage(self, which="cov"):
        if which not in self._cache:
            blob = self._blobs.get(which)
            self._cache[which] = None if blob is None else marshal.loads(zlib.decompress(blob))
        return self._cache[which]

    def fixture_paths(self):
        """{repo path of a looped fixture: [source, ...]}."""
        out = {}
        for src in self.sources:
            out.setdefault("tests/fixtures/" + src[3], []).append(src)
        return out


class SliceMap:
    def __init__(self, path=DEFAULT_PATH):
        with open(path, "rb") as f:
            data = marshal.load(f)
        if data.get("version") != 1:
            raise ValueError(f"unsupported slices map version {data.get('version')!r}")
        self.path = path
        self._raw = data["tests"]
        self._tests = {}

    def __contains__(self, test_id):
        return test_id in self._raw

    def ids(self):
        return sorted(self._raw)

    def get(self, test_id):
        t = self._tests.get(test_id)
        if t is None and test_id in self._raw:
            t = self._tests[test_id] = SliceTest(test_id, self._raw[test_id])
        return t


_default = []


def load_default():
    """The map next to br/ (maps/slices.marshal) or $BR_SLICES; None if absent."""
    if not _default:
        path = os.environ.get("BR_SLICES") or DEFAULT_PATH
        try:
            _default.append(SliceMap(path))
        except (OSError, ValueError, EOFError, TypeError):
            _default.append(None)
    return _default[0]


# ---------------------------------------------------------------------------
# Which items a patch reaches


def _bits_to_list(bits):
    out = []
    i = 0
    while bits:
        if bits & 1:
            out.append(i)
        bits >>= 1
        i += 1
    return out


def _code_items(cov, path, change):
    """Bitset of items that ran the change, or None if common coverage is touched."""
    block = cov["files"].get(path)
    if block is None:
        return 0  # T never ran any code of this file
    sets = cov["sets"]
    key = (change.code.co_qualname, change.code.co_firstlineno)
    lines = block["l"].get(key)
    out = 0
    if change.whole:
        sid = block["c"].get(key)
        if sid is not None:
            if sid == COMMON:
                return None
            out |= sets[sid]
        if lines:
            for sid in lines.values():
                if sid == COMMON:
                    return None
                out |= sets[sid]
        return out
    if not lines:
        return 0
    for line in change.lines:
        sid = lines.get(line)
        if sid is None:
            continue
        if sid == COMMON:
            return None
        out |= sets[sid]
    return out


def _changed_entries(loader, old, new):
    """Changed 0-based entry indexes of a fixture for `loader`, or None (not alignable)."""
    if loader == "load_sql_fixture_pairs":
        return _fixtures.changed_pair_indexes(old, new)
    if loader == "load_sql_fixtures":
        try:
            a = _fixtures.fixtures(old)
            b = _fixtures.fixtures(new)
        except _fixtures.FixtureDecodeError:
            return None
        if len(a) != len(b):
            return None
        return [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    return None


def _affected_bits(t, changes, data_changes, which):
    cov = t.coverage(which)
    if cov is None:
        return None
    bits = 0
    for path, change in changes:
        if change.import_time:
            return None  # module/class body: cannot be reasoned about per item
        if path in t.opens:
            return None  # T reads the changed source as data
        got = _code_items(cov, path, change)
        if got is None:
            return None
        bits |= got
    fixture_paths = t.fixture_paths()
    for entry in data_changes or ():
        path, old, new = entry[0], entry[1], entry[2]
        srcs = fixture_paths.get(path)
        if srcs is None:
            if path in t.opens:
                return None  # some other file T reads
            continue
        if old is None or new is None:
            return None
        for src in srcs:
            idx = _changed_entries(src[2], old, new)
            if idx is None:
                return None
            n, offset = src[4], src[5]
            for i in idx:
                if not 0 <= i < n:
                    return None
                bits |= 1 << (offset + i)
    return bits


def affected(slice_map, test_id, changes, data_changes=None):
    """Sorted item indexes of test_id that the patch can reach, or None (run all of T).

    changes: [(path, codediff.Change)] of the patch (static.Result.changes).
    data_changes: [(path, old_bytes, new_bytes)] (static.Result.data_changes); a
    looped fixture contributes its changed entries, any other file T opened as data
    returns None. The caller must not slice when the patch creates, deletes or
    renames files, or when the static stage widened to ALL.
    """
    t = slice_map.get(test_id) if slice_map is not None else None
    if t is None:
        return None
    try:
        bits = _affected_bits(t, changes, data_changes, "cov")
    except Exception:  # an analysis bug must never drop a test
        return None
    if bits is None:
        return None
    if bits >> t.n:
        return None  # map inconsistency
    return _bits_to_list(bits)


def affected_split(slice_map, test_id, changes, data_changes=None):
    """(affected, rest_affected) for a test with a front/rest split (else rest = affected).

    rest_affected: the items whose non-front coverage the patch reaches; items in
    affected but not in rest_affected may be dropped when front_equal() says their
    front output is unchanged. (None, None) when T cannot be sliced.
    """
    items = affected(slice_map, test_id, changes, data_changes)
    if items is None:
        return None, None
    t = slice_map.get(test_id)
    if t.coverage("rest") is None:
        return items, items
    try:
        bits = _affected_bits(t, changes, data_changes, "rest")
    except Exception:
        return items, items
    if bits is None:
        return items, items
    rest = _bits_to_list(bits)
    return items, sorted(set(rest) & set(items)) if items is not None else rest


def estimate_cpu(slice_map, test_id, items):
    """Estimated child CPU seconds (no tracing) of T restricted to items, or None."""
    t = slice_map.get(test_id)
    if t is None or t.cpu_base is None or t.cpu_items is None:
        return None
    return t.cpu_base + sum(t.cpu_items[i] for i in items)


# ---------------------------------------------------------------------------
# Running T on a subset of its items (inside a verification child)

_installed = []  # (owner, name, previous value)


def _spec_of(test_id, slice_map, spec):
    if spec is not None:
        return spec
    smap = slice_map if slice_map is not None else load_default()
    t = smap.get(test_id) if smap is not None else None
    if t is None:
        raise KeyError(f"no slice map entry for {test_id}")
    return {"module": t.module, "cls": t.cls, "method": t.method, "sources": t.sources, "n": t.n}


def install_filter(test_id, items, slice_map=None, spec=None):
    """Makes test_id iterate only over `items` (global item indexes) when it runs.

    Replaces the test method on its class by a wrapper that, for the duration of each
    call, rebinds the loader names in the test's module to a filtering generator
    ("call" sources) and shadows the class attributes setUpClass loaded with filtered
    copies on the instance ("attr" sources). Works whether or not setUpClass has run.
    The filtering loader / attribute check fails the test (AssertionError) when a
    fixture does not have the number of entries the map recorded, so a mismatch never
    turns into a pass. Raises if the test cannot be found; the caller must then run
    the test unfiltered or keep it selected.
    """
    spec = _spec_of(test_id, slice_map, spec)
    module = sys.modules.get(spec["module"]) or importlib.import_module(spec["module"])
    cls = getattr(module, spec["cls"])
    name = spec["method"]
    original = getattr(cls, name)
    if getattr(original, "__br_slice_original__", None) is not None:
        original = original.__br_slice_original__  # re-filtering: wrap the real method
    wanted = sorted(set(int(i) for i in items))
    calls = {}  # (loader name, file) -> (n, allowed local indexes)
    attrs = []  # (attr, n, [local indexes])
    for kind, sname, loader, file, n, offset in (tuple(s) for s in spec["sources"]):
        local = [i - offset for i in wanted if offset <= i < offset + n]
        if kind == "call":
            calls[(sname, file)] = (n, frozenset(local))
        elif kind == "attr":
            attrs.append((sname, n, local))
        else:
            raise ValueError(f"unknown source kind {kind!r}")
    outside = [i for i in wanted if not 0 <= i < spec["n"]]
    if outside:
        raise ValueError(f"items out of range for {test_id}: {outside[:5]}")
    loader_names = sorted({sname for sname, _ in calls})

    def make_filtered(real, lname):
        def filtered(filename, *args, **kwargs):
            spec_ = calls.get((lname, filename))
            if spec_ is None:  # not a looped file: unfiltered
                yield from real(filename, *args, **kwargs)
                return
            n, allowed = spec_
            count = 0
            for index, entry in enumerate(real(filename, *args, **kwargs)):
                count += 1
                if index in allowed:
                    yield entry
            if count != n:
                raise AssertionError(f"br.slices: {filename} has {count} entries, the slice map {n}")

        filtered.__br_slice_real__ = real
        return filtered

    @functools.wraps(original)
    def sliced(self, *args, **kwargs):
        saved = []
        try:
            for lname in loader_names:
                real = module.__dict__[lname]
                real = getattr(real, "__br_slice_real__", real)
                saved.append((lname, module.__dict__[lname]))
                setattr(module, lname, make_filtered(real, lname))
            for attr, n, local in attrs:
                full = getattr(type(self), attr)
                if len(full) != n:
                    raise AssertionError(f"br.slices: {attr} has {len(full)} entries, the slice map {n}")
                self.__dict__[attr] = [full[i] for i in local]
            return original(self, *args, **kwargs)
        finally:
            for lname, value in reversed(saved):
                setattr(module, lname, value)
            for attr, _n, _local in attrs:
                self.__dict__.pop(attr, None)

    sliced.__br_slice_original__ = original
    sliced.__br_slice_items__ = tuple(wanted)
    previous = cls.__dict__.get(name, _MISSING)
    _installed.append((cls, name, previous))
    setattr(cls, name, sliced)
    return sliced


_MISSING = object()


def uninstall_filters():
    while _installed:
        owner, name, previous = _installed.pop()
        if previous is _MISSING:
            try:
                delattr(owner, name)
            except AttributeError:
                pass
        else:
            setattr(owner, name, previous)


def duckdb_single_thread():
    """Makes later duckdb.connect() calls default to one worker thread.

    duckdb starts one OS thread per core by default; a forked verification child
    must stay within the container's process/thread limit. Results are identical
    (checked on the pristine suite by lab/trace/slices_trace.py). Returns True when
    patched.
    """
    duckdb = sys.modules.get("duckdb")
    if duckdb is None:
        return False
    real = duckdb.connect
    if getattr(real, "__br_single_thread__", False):
        return True

    @functools.wraps(real)
    def connect(*args, **kwargs):
        config = kwargs.get("config")
        if len(args) >= 3:  # positional config
            config = args[2]
        config = dict(config or {})
        config.setdefault("threads", 1)
        if len(args) >= 3:
            args = args[:2] + (config,) + args[3:]
        else:
            kwargs["config"] = config
        return real(*args, **kwargs)

    connect.__br_single_thread__ = True
    duckdb.connect = connect
    return True


# ---------------------------------------------------------------------------
# test_execute_tpcds: the front proof
#
# For item i (pair (meta, sql, _) of tpc-ds.sql) the test does, in this process (the
# inline pool runs the worker in-process; tests/test_executor.py at the pinned commit):
#   main:   expr = parse_one(sql); payload = dumps((mp_execute, (expr, meta), {}))
#   worker: fn, (expr2, meta2), kw = loads(payload); mp_execute(expr2, meta2):
#           not meta2.get("execute") -> return None (the main then skips the item)
#           tables = {t.name: _tables[t.name] for t in find_tables(expr2)}
#           execute(expr2, schema=_schema, tables=tables):          (sqlglot/executor)
#             tables_ = ensure_tables(tables, dialect=None)
#             schema = ensure_schema(_schema, dialect=None)          (_schema is truthy)
#             tables_.supported_table_args != schema.supported_table_args -> ExecuteError
#             expression = optimize(expr2, schema, leave_tables_isolated=True,
#                                   dialect=None)                       <- front
#             expression.sql(pretty=True)        (logger.debug argument, always evaluated)
#             Plan(expression)                   (starts with expression.copy())
#             PythonExecutor(tables=tables_).execute(plan); result pickled back;
#   main:   subtestHelper: transpile(sql, write="duckdb") + duckdb + assert_frame_equal
# The traced front phase (lab/trace/slices_trace.py install_phases) is parse_one, the
# call-item dumps/loads and optimize; everything else is the "rest" (entry["rest"]).
#
# Claim: if the patch does not reach item i's rest coverage, the rest of item i runs
# pristine code, and its only inputs from the front are (a) expr2 (read by find_tables
# before optimize, which copies its input) and (b) the optimized tree (read by .sql()
# and by Plan through .copy(): node classes, args with every non-Expr value, parent /
# arg_key / index, comments, _type, _meta, cached _hash values, any instance
# __dict__). front_digests() recomputes exactly that sequence (find_tables, ensure_tables
# on stand-in Tables with the real column names, ensure_schema, the table-args check,
# optimize) with the patch active and digests (a) and (b) with fingerprint(); equal to
# the pristine digest => item i behaves as at the pinned commit, where it passes. A
# raising front never compares equal (the real item would fail). For items with
# execute = false only parse_one + the pickle round trip matter: they must not raise.
#
# Accepted gaps (same class as item slicing): module/class state that patched front code
# leaves behind and pristine rest code (of this or a later item) reads - a lazily built
# cache, a mutated global, the shared _schema dict; and the inline pool vs. a spawned
# worker (validated on the suite).

TPCDS_TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TPCDS_FIXTURE = "optimizer/tpc-ds/tpc-ds.sql"
FRONT_VERSION = 2
FRONT_PATH = os.path.join(os.path.dirname(DEFAULT_PATH), "front.marshal")


class FrontMismatch(Exception):
    """The front output cannot be compared faithfully: never equal."""


class _Token:
    __slots__ = ("tok",)

    def __init__(self, tok):
        self.tok = tok


class _Hashed:
    """Stands for a child expression inside a hash() tuple, with a known hash."""

    __slots__ = ("h",)

    def __init__(self, h):
        self.h = h

    def __hash__(self):
        return self.h


def _node_hash(n, memo, expr_cls):
    """Expression.__hash__ of one node at the pinned commit (children from memo)."""
    h = hash(n.key)
    args = n.args
    if n._hash_raw_args:
        for k in sorted(args):
            v = args[k]
            if v:
                if type(v) is list:
                    v = tuple(_Hashed(memo[id(x)]) if isinstance(x, expr_cls) else x for x in v)
                elif isinstance(v, expr_cls):
                    v = _Hashed(memo[id(v)])
                h = hash((h, k, v))
        return h
    for k in sorted(args):
        v = args[k]
        vt = type(v)
        if vt is list:
            for x in v:
                if x is not None and x is not False:
                    if type(x) is str:
                        x = x.lower()
                    elif isinstance(x, expr_cls):
                        x = _Hashed(memo[id(x)])
                    h = hash((h, k, x))
                else:
                    h = hash((h, k))
        elif v is not None and v is not False:
            if vt is str:
                v = v.lower()
            elif isinstance(v, expr_cls):
                v = _Hashed(memo[id(v)])
            h = hash((h, k, v))
    return h


def _replica_hash(node, memo, expr_cls):
    """What the pinned Expression.__hash__ returns for node (iterative post-order).

    Own copy of the algorithm: the patch may change sqlglot's. Raises FrontMismatch on
    a cycle (the pinned code would not terminate).
    """
    got = memo.get(id(node))
    if got is not None:
        return got
    visiting = set()
    stack = [(node, False)]
    while stack:
        n, expanded = stack.pop()
        nid = id(n)
        if expanded:
            memo[nid] = _node_hash(n, memo, expr_cls)
            visiting.discard(nid)
            continue
        if nid in memo:
            continue
        if nid in visiting:
            raise FrontMismatch("cycle in an expression tree")
        visiting.add(nid)
        stack.append((n, True))
        for v in n.args.values():
            if isinstance(v, expr_cls):
                if id(v) not in memo:
                    stack.append((v, False))
            elif type(v) is list:
                for x in v:
                    if isinstance(x, expr_cls) and id(x) not in memo:
                        stack.append((x, False))
    return memo[id(node)]


_SCALARS = (type(None), str, int, bool, float, bytes)


def _tokens(root, expr_cls):
    """Canonical pre-order token list of everything reachable from root.

    Per expression node: class, arg_key, index, whether _hash is cached, the parent (as
    the ordinal of a node of this walk), then every args entry in insertion order,
    comments, _type, _meta and any instance __dict__, recursively; a node met again is
    a back reference. Values other than expressions, None/str/int/bool/float/bytes,
    list/tuple/dict and enum members raise FrontMismatch, and so does a parent outside
    the walk or a cached _hash that the pinned __hash__ would not compute.
    """
    import enum

    toks = []
    append = toks.append
    index = {}
    nodes = []
    fix = []
    stack = [root]
    pop = stack.pop
    while stack:
        v = pop()
        t = type(v)
        if t is _Token:
            append(v.tok)
            continue
        if t in _SCALARS:
            append((t.__name__, v))
            continue
        if isinstance(v, expr_cls):
            j = index.get(id(v))
            if j is not None:
                append(("ref", j))
                continue
            index[id(v)] = len(nodes)
            nodes.append(v)
            append(("N", t.__module__, t.__qualname__, v.arg_key, v.index, v._hash is not None))
            fix.append((len(toks), v.parent))
            append(None)
            push = []
            for k, val in v.args.items():
                push.append(_Token(("K", k)))
                push.append(val)
            push.append(_Token(("C",)))
            push.append(v.comments)
            push.append(_Token(("T",)))
            push.append(v._type)
            push.append(_Token(("M",)))
            push.append(v._meta)
            extra = getattr(v, "__dict__", None)
            if extra:
                push.append(_Token(("D",)))
                push.append(dict(sorted(extra.items())))
            push.reverse()
            stack.extend(push)
            continue
        if t is list or t is tuple:
            append((t.__name__, len(v)))
            stack.extend(reversed(v))
        elif t is dict:
            append(("dict", len(v)))
            for k2, val in reversed(list(v.items())):
                stack.append(val)
                stack.append(k2)
        elif isinstance(v, enum.Enum):
            append(("enum", t.__module__, t.__qualname__, v.name))
        else:
            raise FrontMismatch(f"cannot fingerprint a {t.__module__}.{t.__qualname__}")
    for pos, parent in fix:
        if parent is None:
            toks[pos] = ("P",)
        else:
            j = index.get(id(parent))
            if j is None:
                raise FrontMismatch("parent outside the tree")
            toks[pos] = ("P", j)
    memo = {}
    for n in nodes:
        if n._hash is not None and n._hash != _replica_hash(n, memo, expr_cls):
            raise FrontMismatch("cached _hash differs from the pinned __hash__")
    return toks


def fingerprint(expr):
    """Structural digest (sha256 bytes) of an expression tree; raises FrontMismatch.

    Independent of PYTHONHASHSEED and of object addresses (see _tokens).
    """
    import hashlib

    from sqlglot.expressions import Expr

    toks = _tokens(expr, Expr)
    return hashlib.sha256("\x00".join(map(repr, toks)).encode("utf-8", "backslashreplace")).digest()


def _front_env():
    """What the test's front phase uses, as bound in the modules the test runs."""
    import tests.helpers as helpers
    import tests.test_executor as te
    import sqlglot.executor as ex

    from . import inlinepool

    return helpers, te, ex, inlinepool


def _front_item(env, schema, meta, sql):
    """Digest of one item's front phase (raises when the real item would raise)."""
    import hashlib

    helpers, te, ex, inlinepool = env
    h = hashlib.sha256()
    h.update(repr((FRONT_VERSION, sorted(meta.items()), sql)).encode("utf-8", "backslashreplace"))
    expr = te.parse_one(sql)
    payload = inlinepool._dumps((te.mp_execute, (expr, meta), {}))
    fn2, args2, kwargs2 = inlinepool._loads(payload)
    if fn2 is not te.mp_execute or kwargs2 or len(args2) != 2:
        raise FrontMismatch("call item did not round-trip")
    expr2, meta2 = args2
    if not meta2.get("execute"):
        h.update(b"noexec")
        return h.digest()
    h.update(b"in")
    h.update(fingerprint(expr2))  # as find_tables sees it (optimize copies its input)
    tables = {}
    for t in te.find_tables(expr2):
        name = t.name
        if name not in schema:  # _tables has exactly the TPCDS_SCHEMA tables
            raise KeyError(name)
        tables[name] = ex.Table(columns=schema[name])  # ensure_tables reads no rows
    tables_ = ex.ensure_tables(tables, dialect=None)
    if not schema:
        raise FrontMismatch("empty schema")  # execute() would infer one from the rows
    schema_obj = ex.ensure_schema(schema, dialect=None)
    if tables_.supported_table_args and tables_.supported_table_args != schema_obj.supported_table_args:
        raise FrontMismatch("table args differ (ExecuteError)")
    optimized = ex.optimize(expr2, schema_obj, leave_tables_isolated=True, dialect=None)
    h.update(b"opt")
    h.update(fingerprint(optimized))
    return h.digest()


def front_digests(items=None, pairs=None, cpu=None):
    """{item: digest or None} of test_execute_tpcds' front phase, computed here.

    Uses the functions the test reaches, so in a verification child with the patch
    active this is the patched front. None = the recomputation raised (the real item
    would fail or could not be compared). cpu: optional dict filled with per-item CPU
    seconds.
    """
    import time

    env = _front_env()
    helpers, te, ex, inlinepool = env
    if pairs is None:
        pairs = list(helpers.load_sql_fixture_pairs(TPCDS_FIXTURE))
    if items is None:
        items = range(len(pairs))
    # The worker's _schema: the initializer's arguments after a pickle round trip,
    # one object shared by all items (as in the worker).
    schema = inlinepool._loads(inlinepool._dumps(helpers.TPCDS_SCHEMA))
    out = {}
    for i in items:
        meta, sql, _expected = pairs[i]
        c0 = time.process_time()
        try:
            out[i] = _front_item(env, schema, meta, sql)
        except Exception:  # the real test would fail (or differ): never equal
            out[i] = None
        if cpu is not None:
            cpu[i] = time.process_time() - c0
    return out


def load_front(path=None):
    """Pristine front digests (build_front / --build-front), or None."""
    if path is None:
        path = os.environ.get("BR_FRONT") or FRONT_PATH
    try:
        with open(path, "rb") as f:
            data = marshal.load(f)
    except (OSError, ValueError, EOFError, TypeError):
        return None
    if type(data) is not dict or data.get("version") != FRONT_VERSION or data.get("test") != TPCDS_TEST:
        return None
    if len(data.get("digests") or ()) != data.get("n"):
        return None
    return data


def front_equal(items, pristine):
    """(equal, differing) among items of test_execute_tpcds: front output == pristine?

    Call inside the verification child with the patch active, before the test runs.
    `pristine` is load_front(). Items without a stable pristine digest, or whose
    recomputation raises, are differing; so is everything when the fixture no longer
    has the recorded number of entries. An equal item still needs its rest coverage
    untouched (affected_split()[1]) to be skipped: plan_in_child() does both.
    """
    items = sorted(set(items))
    if pristine is None or not items:
        return [], items
    digests = pristine["digests"]
    try:
        helpers = _front_env()[0]
        pairs = list(helpers.load_sql_fixture_pairs(TPCDS_FIXTURE))
        if len(pairs) != pristine["n"] or items[-1] >= len(pairs) or items[0] < 0:
            return [], items
        now = front_digests(items, pairs)
    except Exception:
        return [], items
    equal, differing = [], []
    for i in items:
        d = now.get(i)
        if d is not None and digests[i] is not None and d == digests[i]:
            equal.append(i)
        else:
            differing.append(i)
    return equal, differing


def estimate_front_cpu(slice_map, items, rest_items, pristine=None, test_id=TPCDS_TEST):
    """(front check CPU, worst-case CPU) for test_execute_tpcds, or None.

    front check = recomputing the front of the candidates (items not in rest_items);
    worst case = the check + running every item (nothing proven equal); best case is
    the check + estimate_cpu(rest_items) (cpu_base included only if rest_items).
    """
    pristine = load_front() if pristine is None else pristine
    full = estimate_cpu(slice_map, test_id, items)
    if pristine is None or full is None or not pristine.get("cpu"):
        return None
    cand = sorted(set(items) - set(rest_items or ()))
    check = sum(pristine["cpu"][i] for i in cand)
    return check, check + full


def plan_in_child(test_id, items, rest_items=None, front=None, slice_map=None, report=None):
    """Inside a verification child with the patch active, before run_tests().

    items: affected() (or affected_split()[0]); rest_items: affected_split()[1].
    For test_execute_tpcds with pristine front digests (load_front()), the items whose
    only contact with the patch is the front phase and whose front output is unchanged
    are dropped. Installs the filter for the remaining items and returns them; an empty
    list means T passes without running (do not run it). Raises on any problem (the
    caller must then keep T selected). report: optional dict, gets "candidates",
    "equal", "differing", "front_cpu".
    """
    import time

    to_run = set(items)
    if test_id == TPCDS_TEST and front is not None and rest_items is not None:
        if not set(rest_items) <= to_run:
            raise ValueError("rest_items must be a subset of items")
        candidates = sorted(to_run - set(rest_items))
        c0 = time.process_time()
        equal, differing = front_equal(candidates, front) if candidates else ([], [])
        to_run -= set(equal)
        if report is not None:
            report.update(candidates=candidates, equal=equal, differing=differing,
                          front_cpu=time.process_time() - c0)
    to_run = sorted(to_run)
    if to_run:
        install_filter(test_id, to_run, slice_map)
    return to_run


def build_front(out, root, maps_dir, seeds=(0, 1), items=None):
    """Build step: pristine front digests under several hash seeds (unstable -> None)."""
    import subprocess
    import tempfile

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runs = []
    with tempfile.TemporaryDirectory() as tmp:
        for seed in seeds:
            path = os.path.join(tmp, f"front-{seed}.marshal")
            env = dict(os.environ, PYTHONHASHSEED=str(seed), PYTHONPATH=here, SKIP_INTEGRATION="0")
            cmd = [sys.executable, "-m", "br.slices", "--front-child", path, root, maps_dir]
            if items is not None:
                cmd.append(",".join(str(i) for i in items))
            subprocess.run(cmd, check=True, env=env, cwd=here)
            with open(path, "rb") as f:
                runs.append(marshal.load(f))
    n = runs[0]["n"]
    digests = []
    for i in range(n):
        vals = {r["digests"][i] for r in runs}
        digests.append(vals.pop() if len(vals) == 1 else None)
    cpu = [max(r["cpu"][i] for r in runs) for i in range(n)]
    data = {"version": FRONT_VERSION, "test": TPCDS_TEST, "n": n, "digests": digests, "cpu": cpu,
            "unstable": [i for i in range(n) if digests[i] is None and any(r["digests"][i] for r in runs)],
            "failed": [i for i in range(n) if all(r["digests"][i] is None for r in runs)],
            "seeds": list(seeds), "partial": items is not None}
    if out:
        with open(out, "wb") as f:
            marshal.dump(data, f)
    return data


def _front_child(out, root, maps_dir, items=None):
    from . import buildstate, unitrun  # unitrun imports unittest before any sqlglot import

    tests = buildstate._tests_from_maps(maps_dir)
    unitrun.build_warm_state(tests, root)
    helpers = _front_env()[0]
    n = len(list(helpers.load_sql_fixture_pairs(TPCDS_FIXTURE)))
    cpu = {}
    d = front_digests(items, cpu=cpu)
    with open(out, "wb") as f:
        marshal.dump({"n": n, "digests": [d.get(i) for i in range(n)],
                      "cpu": [round(cpu.get(i, 0.0), 6) for i in range(n)]}, f)


def _main(argv):
    if argv[:1] == ["--build-front"]:
        out = argv[1]
        root = argv[2] if len(argv) > 2 else os.environ.get("BR_ROOT", "/opt/sqlglot")
        maps_dir = argv[3] if len(argv) > 3 else os.path.dirname(DEFAULT_PATH)
        data = build_front(out, root, maps_dir)
        print(f"front digests: {data['n']} items, unstable {data['unstable']}, failed {data['failed']}",
              file=sys.stderr)
        return 0
    if argv[:1] == ["--front-child"]:
        items = [int(x) for x in argv[4].split(",")] if len(argv) > 4 and argv[4] else None
        _front_child(argv[1], argv[2], argv[3], items)
        return 0
    print("usage: python -m br.slices --build-front OUT [ROOT] [MAPS]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
