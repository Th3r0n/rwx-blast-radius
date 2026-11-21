"""Import-time state fingerprints: what a patched import changed.

A *cell* is one module global, one class attribute, or one key of a
dict-valued class attribute or global. `snapshot()` digests every cell of the
given modules and of every class reachable from them; `diff()` lists the cells
whose digest differs between the pinned snapshot (taken at build time, in the
same warm state) and the patched one.

Digests are computed from a hash-seed independent description of each value:
numbers and strings by type and repr, containers recursively (sets and dict
keys sorted), functions by module, qualname, defaults, keyword defaults,
closure contents and, for anonymous functions, their instructions; classes and
modules by name (classes are cells of their own). Any value that cannot be
described faithfully is marked opaque; an opaque cell compares unequal to
everything, so it can only widen the result.
"""

from __future__ import annotations

import enum
import functools
import hashlib
import re
import sys
import types

OPAQUE = b"?"
SKIP_CLASS_ATTRS = frozenset(
    {"__dict__", "__weakref__", "__module__", "__doc__", "__firstlineno__", "__static_attributes__", "__qualname__"}
)
SKIP_GLOBALS = frozenset({"__builtins__", "__cached__", "__file__", "__loader__", "__spec__", "__doc__", "__path__"})
MAX_DEPTH = 64

_FUNC_TYPES = (types.FunctionType,)
_BUILTIN_TYPES = (types.BuiltinFunctionType, types.BuiltinMethodType, types.WrapperDescriptorType,
                  types.MethodDescriptorType, types.ClassMethodDescriptorType, types.MethodWrapperType)


class _Opaque(Exception):
    pass


def _code_desc(co):
    out = [co.co_name, co.co_argcount, co.co_kwonlyargcount, co.co_flags, co.co_code, co.co_names, co.co_varnames]
    for const in co.co_consts:
        if isinstance(const, types.CodeType):
            out.append(_code_desc(const))
        else:
            out.append((type(const).__name__, repr(const)))
    return tuple(out)


class Describer:
    """Turns values into hashable, seed-independent descriptions (memoised by id)."""

    def __init__(self):
        self.memo: dict[int, object] = {}
        self.keep: list = []  # keep described objects alive so ids stay unique

    def __call__(self, value, depth=0):
        kind = type(value)
        if value is None or kind in (bool, int, float, complex, str, bytes):
            return (kind.__name__, repr(value))
        if depth > MAX_DEPTH:
            raise _Opaque()
        key = id(value)
        hit = self.memo.get(key)
        if hit is not None:
            return hit
        self.memo[key] = ("cycle",)
        self.keep.append(value)
        # Merkle style: containers are summarised by a digest of their
        # children's summaries, so each object is described once.
        desc = _digest(self._describe(value, kind, depth + 1))
        self.memo[key] = desc
        return desc

    def _describe(self, value, kind, depth):
        if isinstance(value, type):
            return ("type", value.__module__, value.__qualname__)
        if kind is types.ModuleType:
            return ("module", value.__name__)
        if isinstance(value, enum.Enum):
            return ("enum", type(value).__module__, type(value).__qualname__, value.name, self(value._value_, depth))
        if kind in (tuple, list) or isinstance(value, (tuple, list)):
            if value and all(isinstance(v, type) for v in value):
                # Sequences of classes are isinstance/issubclass tuples in
                # sqlglot, often built from sets (address-dependent order).
                return (kind.__name__, "classes", tuple(sorted(f"{v.__module__}:{v.__qualname__}" for v in value)))
            return (kind.__name__, tuple(self(v, depth) for v in value))
        if isinstance(value, dict):
            items = sorted((repr(self(k, depth)), self(v, depth)) for k, v in value.items())
            return ("dict", kind.__name__, tuple(items))
        if isinstance(value, (set, frozenset)):
            return ("set", kind.__name__, tuple(sorted(repr(self(v, depth)) for v in value)))
        if kind in _FUNC_TYPES:
            return self._function(value, depth)
        if kind is types.MethodType:
            return ("method", self(value.__func__, depth), self._ref(value.__self__))
        if kind in (classmethod, staticmethod):
            return (kind.__name__, self(value.__func__, depth))
        if kind is property:
            return ("property", self(value.fget, depth), self(value.fset, depth), self(value.fdel, depth))
        if kind in _BUILTIN_TYPES:
            owner = getattr(value, "__self__", None)
            return ("builtin", getattr(value, "__module__", None), getattr(value, "__qualname__", None), self._ref(owner))
        if kind is functools.partial:
            return ("partial", self(value.func, depth), self(value.args, depth), self(value.keywords, depth))
        if kind is re.Pattern:
            return ("re", value.pattern, value.flags)
        if kind is types.SimpleNamespace or hasattr(value, "__dict__") or hasattr(type(value), "__slots__"):
            state = []
            d = getattr(value, "__dict__", None)
            if isinstance(d, dict):
                state.extend((k, self(v, depth)) for k, v in sorted(d.items()))
            for cls in type(value).__mro__:
                for slot in getattr(cls, "__slots__", ()) if isinstance(getattr(cls, "__slots__", ()), (tuple, list)) else ():
                    if slot in ("__dict__", "__weakref__"):
                        continue
                    if hasattr(value, slot):
                        state.append((slot, self(getattr(value, slot), depth)))
            if state or not _has_address_repr(value):
                return ("obj", type(value).__module__, type(value).__qualname__, tuple(state),
                        None if _has_address_repr(value) else repr(value))
        try:
            r = repr(value)
        except Exception:
            raise _Opaque()
        if " at 0x" in r or "object at" in r:
            raise _Opaque()
        return ("repr", kind.__module__, kind.__qualname__, r)

    def _ref(self, owner):
        if owner is None:
            return None
        if isinstance(owner, type):
            return ("type", owner.__module__, owner.__qualname__)
        if isinstance(owner, types.ModuleType):
            return ("module", owner.__name__)
        return ("instance", type(owner).__module__, type(owner).__qualname__)

    def _function(self, fn, depth):
        qual = fn.__qualname__
        code = _code_desc(fn.__code__) if ("<" in qual) else None
        closure = None
        if fn.__closure__:
            cells = []
            for cell in fn.__closure__:
                try:
                    cells.append(self(cell.cell_contents, depth))
                except ValueError:  # empty cell
                    cells.append(("empty",))
            closure = tuple(cells)
        return (
            "function",
            fn.__module__,
            qual,
            code,
            self(fn.__defaults__, depth),
            self(fn.__kwdefaults__, depth),
            closure,
            tuple(sorted((k, self(v, depth)) for k, v in fn.__dict__.items())) if fn.__dict__ else None,
        )


def _has_address_repr(value) -> bool:
    try:
        r = object.__repr__(value)
        return type(value).__repr__ is object.__repr__ or " at 0x" in repr(value)
    except Exception:
        return True


def key_repr(k) -> str:
    """Readable, seed-independent name of a dict key (shared with the tracer)."""
    if isinstance(k, type):
        return f"T:{k.__module__}:{k.__qualname__}"
    if isinstance(k, enum.Enum):
        return f"E:{type(k).__module__}:{type(k).__qualname__}.{k.name}"
    if k is None or type(k) in (bool, int, float, str, bytes):
        return f"V:{type(k).__name__}:{k!r}"
    if type(k) is tuple:
        return "U:(" + ",".join(key_repr(x) for x in k) + ")"
    return f"O:{type(k).__module__}:{type(k).__qualname__}:{k!r}"


def _digest(desc) -> bytes:
    return hashlib.blake2b(repr(desc).encode("utf-8", "surrogatepass"), digest_size=12).digest()


def _unordered_digest(describe, value):
    """Digest that ignores the order of a sequence's elements (for unstable cells)."""
    if isinstance(value, (tuple, list)):
        return _digest(("unordered", type(value).__name__, tuple(sorted(repr(describe(v)) for v in value))))
    return _digest(describe(value))


def snapshot(module_names, prefix=("sqlglot",), unordered=frozenset()) -> dict:
    """{cell: digest}. Cells: ("g", module, name), ("c", class_id, name),
    ("gk"|"ck", owner, name, key_repr) for keys of dict values."""
    describe = Describer()
    cells: dict = {}
    classes: dict[str, type] = {}
    seen_classes: set[int] = set()

    def cell(kind, owner, name, value):
        try:
            if (kind, owner, name) in unordered:
                cells[(kind, owner, name)] = _unordered_digest(describe, value)
            else:
                cells[(kind, owner, name)] = _digest(describe(value))
        except _Opaque:
            cells[(kind, owner, name)] = OPAQUE
        except RecursionError:
            cells[(kind, owner, name)] = OPAQUE
        if isinstance(value, dict):
            for k, v in list(value.items()):
                kd = key_repr(k)
                try:
                    cells[(kind + "k", owner, name, kd)] = _digest(describe(v))
                except (_Opaque, RecursionError):
                    cells[(kind + "k", owner, name, kd)] = OPAQUE

    def note_class(cls):
        if id(cls) in seen_classes:
            return
        mod = getattr(cls, "__module__", "")
        if not isinstance(mod, str) or not mod.startswith(prefix):
            return
        seen_classes.add(id(cls))
        base = f"{mod}:{cls.__qualname__}"
        cid = base
        n = 1
        while cid in classes:
            n += 1
            cid = f"{base}#{n}"
        classes[cid] = cls

    for name in sorted(module_names):
        module = sys.modules.get(name)
        if module is None:
            continue
        for gname, value in sorted(vars(module).items(), key=lambda kv: kv[0]):
            if gname in SKIP_GLOBALS:
                continue
            if isinstance(value, type):
                note_class(value)
            cell("g", name, gname, value)

    # Classes reachable through attributes and subclassing.
    queue = list(classes.values())
    while queue:
        cls = queue.pop()
        try:
            members = list(cls.__dict__.items())
            subs = type.__subclasses__(cls)
        except Exception:
            continue
        for _, value in members:
            if isinstance(value, type) and id(value) not in seen_classes:
                note_class(value)
                if id(value) in seen_classes:
                    queue.append(value)
        for sub in subs:
            if id(sub) not in seen_classes:
                note_class(sub)
                if id(sub) in seen_classes:
                    queue.append(sub)

    for cid, cls in sorted(classes.items()):
        cells[("mro", cid, "")] = _digest(tuple(f"{c.__module__}:{c.__qualname__}" for c in cls.__mro__))
        for aname, value in sorted(cls.__dict__.items(), key=lambda kv: kv[0]):
            if aname in SKIP_CLASS_ATTRS:
                continue
            cell("c", cid, aname, value)
    return cells


def cell_module(key) -> str:
    owner = key[1]
    return owner.split(":", 1)[0] if key[0] in ("c", "ck", "mro") else owner


def function_keys(cells, root) -> dict:
    """{cell: (path, qualname, firstlineno)} for ("c"/"g") cells whose live value is
    a function (or classmethod/staticmethod/property/wrapper of one) defined under root."""
    out = {}
    prefix = root.rstrip("/") + "/"
    for cell in cells:
        if cell[0] not in ("c", "g"):
            continue
        if cell[0] == "c":
            mod, _, qual = cell[1].partition(":")
            if "#" in qual:
                continue
            obj = sys.modules.get(mod)
            for part in qual.split("."):
                obj = getattr(obj, part, None) if obj is not None else None
            if not isinstance(obj, type):
                continue
            value = obj.__dict__.get(cell[2])
        else:
            module = sys.modules.get(cell[1])
            value = vars(module).get(cell[2]) if module is not None else None
        fn = value
        if isinstance(fn, (classmethod, staticmethod)):
            fn = fn.__func__
        elif isinstance(fn, property):
            fn = fn.fget
        seen = 0
        while hasattr(fn, "__wrapped__") and seen < 10:
            fn = fn.__wrapped__
            seen += 1
        if isinstance(fn, types.FunctionType) and fn.__code__.co_filename.startswith(prefix):
            co = fn.__code__
            out[cell] = (co.co_filename[len(prefix):], co.co_qualname, co.co_firstlineno)
    return out


def diff(old: dict, new: dict, touched_modules=()) -> list:
    """Cells that differ: present in only one snapshot, or digests differ.

    An opaque cell (a value we cannot describe) counts as changed when its
    owner module is one the patch touched; elsewhere it cannot have changed
    unless something it holds did, which shows up in other cells.
    """
    touched = set(touched_modules)
    out = []
    for key, value in new.items():
        before = old.get(key)
        if before is None or before != value:
            out.append(key)
        elif value == OPAQUE and cell_module(key) in touched:
            out.append(key)
    for key in old:
        if key not in new:
            out.append(key)
    return out
