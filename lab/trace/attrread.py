"""Class-attribute read recording for the tracer's "attrs" pass.

Every non-callable, non-descriptor, non-dunder entry of the __dict__ of every sqlglot
class (discovered like br.statediff.snapshot: module globals of sqlglot modules, then
nested classes and the __subclasses__ closure) is replaced by a non-data descriptor
(Rec) whose __get__ records the class through which it was read and returns the value.
Dict values (exact dict, or defaultdict) are replaced, once per distinct dict object,
by RecDict: a dict subclass holding the same items that records key-level reads
(__getitem__, get, __contains__, setdefault, pop, __delitem__) and whole-dict reads
("*": iteration, keys/values/items, len/bool, copy, ==, |, repr, **-unpacking,
dict(x), pickling/copying). Aliases of the same dict (other class attributes, sqlglot
module globals) are pointed at the same RecDict so mutations stay shared.

Classes defined outside sqlglot (tests) that subclass sqlglot classes get their own
attributes wrapped too when a sqlglot ancestor defines the same name; those reads are
attributed to every such ancestor (the test value usually derives from it).

Classes created later (lazy imports inside a test) are instrumented right after their
module finishes executing (hook on importlib's _LoaderBasics.exec_module), so reads
from then on are recorded; reads made by their own class bodies of already
instrumented classes are recorded as usual.

Cell names match br.statediff: class_id = f"{module}:{qualname}" with "#2", "#3"
suffixes for duplicates in discovery order, key names from statediff.key_repr.
"""

from __future__ import annotations

import collections
import enum
import sys
import types

_SKIP_NAMES = frozenset({"__dict__", "__weakref__", "__module__", "__doc__", "__qualname__"})
_dget = dict.get
_dgetitem = dict.__getitem__
_dcontains = dict.__contains__
_dsetdefault = dict.setdefault
_dpop = dict.pop
_ddelitem = dict.__delitem__
_diter = dict.__iter__
_dkeys = dict.keys
_dvalues = dict.values
_ditems = dict.items
_dlen = dict.__len__
_deq = dict.__eq__
_dne = dict.__ne__
_dor = dict.__or__
_dror = dict.__ror__
_dcopy = dict.copy
_drepr = dict.__repr__
_dreversed = dict.__reversed__
_dpopitem = dict.popitem


class Rec:
    """Non-data descriptor standing in for a class attribute value."""

    __slots__ = ("value", "vias", "owners", "attr")

    def __init__(self, value, owners, attr):
        self.value = value
        self.vias = set()
        self.owners = owners  # list of owner class ids
        self.attr = attr

    def __get__(self, obj, objtype=None):
        self.vias.add(objtype if objtype is not None else type(obj))
        return self.value

    def __repr__(self):  # shows up if someone looks at cls.__dict__ directly
        return f"<Rec {self.owners}.{self.attr}={self.value!r}>"


class _RecMixin:
    __slots__ = ()

    def __getitem__(self, k):
        self._kr.add(k)
        return _dgetitem(self, k)

    def get(self, k, default=None):
        self._kr.add(k)
        return _dget(self, k, default)

    def __contains__(self, k):
        self._kr.add(k)
        return _dcontains(self, k)

    def setdefault(self, k, default=None):
        self._kr.add(k)
        return _dsetdefault(self, k, default)

    def pop(self, k, *default):
        self._kr.add(k)
        return _dpop(self, k, *default)

    def __delitem__(self, k):
        self._kr.add(k)
        _ddelitem(self, k)

    def __iter__(self):
        self._all = True
        return _diter(self)

    def keys(self):
        self._all = True
        return _dkeys(self)

    def values(self):
        self._all = True
        return _dvalues(self)

    def items(self):
        self._all = True
        return _ditems(self)

    def __len__(self):
        self._all = True
        return _dlen(self)

    def __eq__(self, other):
        self._all = True
        return _deq(self, other)

    def __ne__(self, other):
        self._all = True
        return _dne(self, other)

    __hash__ = None

    def __or__(self, other):
        self._all = True
        return _dor(self, other)

    def __ror__(self, other):
        self._all = True
        return _dror(self, other)

    def copy(self):
        self._all = True
        return _dcopy(self)

    def __repr__(self):
        self._all = True
        return _drepr(self)

    def __reversed__(self):
        self._all = True
        return _dreversed(self)

    def popitem(self):
        self._all = True
        return _dpopitem(self)

    def __reduce__(self):  # pickle/copy/deepcopy produce what the plain dict would
        self._all = True
        return (self._plain_type(), (self._plain_args(),))


class RecDict(_RecMixin, dict):
    __slots__ = ("_kr", "_all", "owners")

    def __init__(self, *args, **kwargs):  # dict-compatible: type(d)(...) still works
        dict.__init__(self, *args, **kwargs)
        self._kr = set()
        self._all = False
        self.owners = []  # list of (owner class id, attr)

    def _plain_type(self):
        return dict

    def _plain_args(self):
        return _dcopy(self)


class RecDefaultDict(_RecMixin, collections.defaultdict):
    __slots__ = ("_kr", "_all", "owners")

    def __init__(self, *args, **kwargs):
        collections.defaultdict.__init__(self, *args, **kwargs)
        self._kr = set()
        self._all = False
        self.owners = []

    def __reduce__(self):
        self._all = True
        return (collections.defaultdict, (self.default_factory, _dcopy(self)))


def _is_wrappable(name, value, cls):
    if name in _SKIP_NAMES or (name.startswith("__") and name.endswith("__")):
        return False
    if len(name) > 2 and name[0] == "_" and name[-1] == "_" and name[1] != "_" and name[-2] != "_":
        return False  # enum _sunder_ names
    if isinstance(value, type) or callable(value):
        return False
    if isinstance(value, (classmethod, staticmethod, property, Rec)):
        return False
    if hasattr(type(value), "__get__") or hasattr(type(value), "__set__"):
        return False  # other descriptors (slots, cached_property, enum.property, ...)
    return True


class Instrumenter:
    def __init__(self, key_repr, prefix=("sqlglot",)):
        self.key_repr = key_repr
        self.prefix = prefix
        self.cids = {}  # id(cls) -> class id
        self.classes = {}  # class id -> cls
        self.keep = []
        self.done = set()  # ids of instrumented classes (sqlglot and foreign)
        self.recs = []
        self.recdicts = {}  # id(original dict) -> RecDict
        self.dicts_by_id = {}  # id(RecDict) -> RecDict
        self.duplicates = 0
        self.stats = collections.Counter()
        self._hooked = None

    # discovery (mirrors br.statediff.snapshot) --------------------------------------

    def _note(self, cls):
        if id(cls) in self.cids:
            return
        mod = getattr(cls, "__module__", "")
        if not isinstance(mod, str) or not mod.startswith(self.prefix):
            return
        base = f"{mod}:{cls.__qualname__}"
        cid = base
        n = 1
        while cid in self.classes:
            n += 1
            cid = f"{base}#{n}"
        if n > 1:
            self.duplicates += 1
        self.cids[id(cls)] = cid
        self.classes[cid] = cls
        self.keep.append(cls)

    def discover(self):
        """Notes every reachable sqlglot class (new ones get ids after existing ones)."""
        before = set(self.cids)
        for name in sorted(m for m in sys.modules if m.startswith(self.prefix)):
            module = sys.modules.get(name)
            if module is None:
                continue
            for _gname, value in sorted(vars(module).items(), key=lambda kv: kv[0]):
                if isinstance(value, type):
                    self._note(value)
        queue = [c for c in self.classes.values()]
        seen = set(self.cids)
        while queue:
            cls = queue.pop()
            try:
                members = list(cls.__dict__.items())
                subs = type.__subclasses__(cls)
            except Exception:
                continue
            for _, value in members:
                if isinstance(value, type) and id(value) not in seen:
                    self._note(value)
                    if id(value) in self.cids:
                        seen.add(id(value))
                        queue.append(value)
            for sub in subs:
                if id(sub) not in seen:
                    self._note(sub)
                    if id(sub) in self.cids:
                        seen.add(id(sub))
                        queue.append(sub)
        return [c for k, c in self.classes.items() if id(c) not in before]

    def foreign_subclasses(self):
        """Non-sqlglot classes reachable by subclassing from sqlglot classes."""
        out = []
        seen = set()
        queue = list(self.classes.values())
        while queue:
            cls = queue.pop()
            try:
                subs = type.__subclasses__(cls)
            except Exception:
                continue
            for sub in subs:
                if id(sub) in seen:
                    continue
                seen.add(id(sub))
                queue.append(sub)
                if id(sub) not in self.cids:
                    out.append(sub)
        return out

    # wrapping ----------------------------------------------------------------------

    def _recdict(self, value, owner_pairs):
        rd = self.recdicts.get(id(value))
        if rd is None:
            if type(value) is dict:
                rd = RecDict(value)
            elif type(value) is collections.defaultdict:
                rd = RecDefaultDict(value.default_factory, value)
            else:
                return None
            self.recdicts[id(value)] = rd
            self.dicts_by_id[id(rd)] = rd
            self.keep.append(value)
        for pair in owner_pairs:
            if pair not in rd.owners:
                rd.owners.append(pair)
        return rd

    def _wrap_class(self, cls, owners_for):
        """owners_for(name) -> list of owner class ids (empty: leave the entry alone)."""
        try:
            items = list(cls.__dict__.items())
        except Exception:
            return
        for name, value in items:
            if not _is_wrappable(name, value, cls):
                continue
            owners = owners_for(name)
            if not owners:
                continue
            if isinstance(value, dict):
                rd = self._recdict(value, [(o, name) for o in owners])
                if rd is not None:
                    value = rd
                    self.stats["dict"] += 1
                else:
                    self.stats["dict_subclass_attr_only"] += 1
            rec = Rec(value, owners, name)
            try:
                type.__setattr__(cls, name, rec)
            except (TypeError, AttributeError):
                self.stats["unsettable"] += 1
                continue
            self.recs.append(rec)
            self.stats["attr"] += 1
            if isinstance(value, enum.Enum):
                self.stats["enum_member"] += 1

    def instrument(self):
        """Instruments every not yet instrumented sqlglot class and foreign subclass."""
        self.discover()
        for cls in list(self.classes.values()):
            if id(cls) in self.done:
                continue
            self.done.add(id(cls))
            cid = self.cids[id(cls)]
            self._wrap_class(cls, lambda name, cid=cid: [cid])
        for cls in self.foreign_subclasses():
            if id(cls) in self.done:
                continue
            self.done.add(id(cls))
            self.keep.append(cls)

            def owners_for(name, cls=cls):
                out = []
                for base in cls.__mro__[1:]:
                    cid = self.cids.get(id(base))
                    if cid is not None and name in base.__dict__:
                        out.append(cid)
                return out

            self._wrap_class(cls, owners_for)
            self.stats["foreign_classes"] += 1
        self._alias_globals()

    def _alias_globals(self):
        """Points sqlglot module globals that alias a wrapped dict at its RecDict."""
        if not self.recdicts:
            return
        for name in [m for m in sys.modules if m.startswith(self.prefix)]:
            module = sys.modules.get(name)
            d = getattr(module, "__dict__", None)
            if not isinstance(d, dict):
                continue
            for gname, value in list(d.items()):
                if type(value) in (dict, collections.defaultdict):
                    rd = self.recdicts.get(id(value))
                    if rd is not None:
                        d[gname] = rd
                        self.stats["aliased_global"] += 1

    def install_import_hook(self):
        """Instruments classes of modules imported from now on, right after they run."""
        import importlib._bootstrap_external as be

        original = be._LoaderBasics.exec_module
        inst = self

        def exec_module(loader, module):
            original(loader, module)
            if getattr(module, "__name__", "").startswith(inst.prefix):
                inst.instrument()

        be._LoaderBasics.exec_module = exec_module
        self._hooked = (be._LoaderBasics, original)

    def reset(self):
        for rec in self.recs:
            rec.vias.clear()
        for rd in self.dicts_by_id.values():
            rd._kr.clear()
            rd._all = False

    # results -----------------------------------------------------------------------

    def _cid(self, cls):
        cid = self.cids.get(id(cls))
        if cid is None:
            cid = f"{getattr(cls, '__module__', '?')}:{getattr(cls, '__qualname__', '?')}"
        return cid

    def collect(self):
        reads = set()
        for rec in self.recs:
            if rec.vias:
                vias = [self._cid(v) for v in rec.vias]
                for owner in rec.owners:
                    for via in vias:
                        reads.add((owner, rec.attr, via))
        keyreads = set()
        key_errors = 0
        for rd in self.dicts_by_id.values():
            if not rd._kr and not rd._all:
                continue
            names = []
            for k in rd._kr:
                try:
                    names.append(self.key_repr(k))
                except Exception:
                    key_errors += 1
                    names.append("?")
            if rd._all:
                names.append("*")
            for owner, attr in rd.owners:
                for kn in names:
                    keyreads.add((owner, attr, kn))
        return sorted(reads), sorted(keyreads), key_errors
