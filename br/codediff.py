"""Compare the compiled code of the pinned and the patched version of a file.

Two versions of a Python file behave the same when their code objects match
instruction for instruction, ignoring line numbers. This module reports the
pinned code objects whose own instructions differ, and for each one the pinned
line numbers of the differing instructions, which is what line coverage was
recorded against.

Normalised away (see docs/DECISIONS.md):
- line numbers and positions (co_firstlineno, co_linetable);
- the `__firstlineno__` and `__static_attributes__` constants of class bodies;
- docstrings (function co_consts[0] is never loaded by an instruction, and a
  class `__doc__` is only read by sqlglot to keep it truthy).
"""

from __future__ import annotations

import dis
import types
from difflib import SequenceMatcher

CO_OPTIMIZED = 0x0001

_JUMPS = frozenset(dis.hasjump) if hasattr(dis, "hasjump") else frozenset(dis.hasjrel + dis.hasjabs)
_NORMALISED_STORES = {"__firstlineno__": "firstlineno", "__static_attributes__": "static", "__doc__": "doc"}


class Change:
    """One pinned code object whose own instructions differ in the patched file."""

    __slots__ = ("code", "kind", "import_time", "lines", "whole")

    def __init__(self, code, kind, import_time, lines, whole):
        self.code = code  # pinned code object
        self.kind = kind  # "module" | "class" | "function"
        self.import_time = import_time  # runs while the module is imported
        self.lines = lines  # pinned lines of differing instructions
        self.whole = whole  # structure changed: treat every line as touched

    @property
    def key(self):
        return (self.code.co_qualname, self.code.co_firstlineno)

    def __repr__(self):
        return (
            f"Change({self.kind} {self.code.co_qualname}@{self.code.co_firstlineno}"
            f" import={self.import_time} whole={self.whole} lines={sorted(self.lines)})"
        )


def compile_bytes(data: bytes, filename: str):
    """Compile like the import system does (honours coding cookies and BOMs)."""
    return compile(data, filename, "exec", dont_inherit=True, optimize=0)


def _kind(co) -> str:
    if co.co_name == "<module>":
        return "module"
    if co.co_flags & CO_OPTIMIZED:
        return "function"
    return "class"


def _canon(value):
    """A hashable, hash-seed independent description of a constant."""
    kind = type(value)
    if kind is types.CodeType:
        return ("code", value.co_qualname)
    if kind is tuple:
        return ("tuple", tuple(_canon(item) for item in value))
    if kind is frozenset:
        return ("frozenset", tuple(sorted(repr(_canon(item)) for item in value)))
    if kind is float:
        return ("float", repr(value))
    return (kind.__name__, repr(value))


class _Instr:
    __slots__ = ("key", "line", "target", "offset")

    def __init__(self, key, line, target, offset):
        self.key = key
        self.line = line
        self.target = target  # jump target offset, or None
        self.offset = offset


def _instructions(co):
    raw = list(dis.get_instructions(co))
    out = []
    last_line = co.co_firstlineno
    for index, ins in enumerate(raw):
        line = ins.positions.lineno if ins.positions is not None else None
        if line is None:
            line = last_line
        else:
            last_line = line
        target = None
        if ins.opcode in _JUMPS:
            key = (ins.opname,)
            target = ins.argval
        elif ins.opname in ("LOAD_CONST", "RETURN_CONST"):
            nxt = raw[index + 1] if index + 1 < len(raw) else None
            if (
                ins.opname == "LOAD_CONST"
                and nxt is not None
                and nxt.opname in ("STORE_NAME", "STORE_GLOBAL")
                and nxt.argval in _NORMALISED_STORES
            ):
                key = (ins.opname, _NORMALISED_STORES[nxt.argval])
            else:
                key = (ins.opname, _canon(ins.argval))
        elif ins.argrepr:
            key = (ins.opname, ins.argrepr)
        else:
            key = (ins.opname, ins.arg)
        out.append(_Instr(key, line, target, ins.offset))
    return out


def _exception_entries(co, index_of):
    try:
        entries = dis._parse_exception_table(co)  # noqa: SLF001 - stdlib helper
    except Exception:
        return None
    out = []
    for entry in entries:
        out.append(
            (
                index_of.get(entry.start),
                index_of.get(entry.end - 2) if entry.end - 2 in index_of else index_of.get(entry.end),
                index_of.get(entry.target),
                entry.depth,
                entry.lasti,
            )
        )
    return out


def _signature(co):
    return (
        co.co_argcount,
        co.co_posonlyargcount,
        co.co_kwonlyargcount,
        co.co_flags,
        co.co_varnames,
        co.co_cellvars,
        co.co_freevars,
        co.co_name,
        co.co_qualname,
    )


def _divergent(old, new):
    """(lines, whole) for two code objects with different instructions."""
    if _signature(old) != _signature(new):
        return set(), True
    oi = _instructions(old)
    ni = _instructions(new)
    okeys = [ins.key for ins in oi]
    nkeys = [ins.key for ins in ni]
    if okeys == nkeys and all((a.target is None) == (b.target is None) for a, b in zip(oi, ni)):
        mapping = {i: i for i in range(len(oi))}
        lines: set[int] = set()
    else:
        matcher = SequenceMatcher(None, okeys, nkeys, autojunk=False)
        mapping = {}
        lines = set()
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for d in range(i2 - i1):
                    mapping[i1 + d] = j1 + d
                continue
            for i in range(i1, i2):
                lines.add(oi[i].line)
            if tag == "replace" and i2 - i1 == j2 - j1:
                # Same-length replacement: positions still correspond, so a
                # jump landing here lands on the (already flagged) new code.
                for d in range(i2 - i1):
                    mapping[i1 + d] = j1 + d
            if i1 == i2:
                # Code inserted between two pinned instructions runs only after
                # one of them (fall-through) or instead of the second (jump).
                if i1 > 0:
                    lines.add(oi[i1 - 1].line)
                if i1 < len(oi):
                    lines.add(oi[i1].line)
                elif oi:
                    lines.add(oi[-1].line)
    # Jumps whose targets no longer correspond.
    o_index = {ins.offset: i for i, ins in enumerate(oi)}
    n_index = {ins.offset: j for j, ins in enumerate(ni)}
    for i, j in mapping.items():
        a, b = oi[i], ni[j]
        if a.target is None and b.target is None:
            continue
        ot = o_index.get(a.target)
        nt = n_index.get(b.target)
        if ot is None or nt is None or mapping.get(ot) != nt:
            lines.add(a.line)
            if ot is not None:
                lines.add(oi[ot].line)
    # Exception handlers must map onto each other as well.
    if old.co_exceptiontable != new.co_exceptiontable:
        oe = _exception_entries(old, o_index)
        ne = _exception_entries(new, n_index)
        if oe is None or ne is None or len(oe) != len(ne):
            return lines, True
        for (s1, e1, t1, d1, l1), (s2, e2, t2, d2, l2) in zip(oe, ne):
            if None in (s1, e1, t1, s2, e2, t2) or (d1, l1) != (d2, l2):
                return lines, True
            if mapping.get(s1) != s2 or mapping.get(e1) != e2 or mapping.get(t1) != t2:
                return lines, True
    return lines, False


def _same(old, new) -> bool:
    if old.co_code == new.co_code and old.co_names == new.co_names and _signature(old) == _signature(new):
        if old.co_exceptiontable == new.co_exceptiontable and len(old.co_consts) == len(new.co_consts):
            if all(_canon(a) == _canon(b) for a, b in zip(old.co_consts, new.co_consts)):
                return True
    return False


def _children(co):
    seen: dict[str, int] = {}
    out = {}
    for const in co.co_consts:
        if type(const) is types.CodeType:
            n = seen.get(const.co_qualname, 0)
            seen[const.co_qualname] = n + 1
            out[(const.co_qualname, n)] = const
    return out


def _walk(old, new, import_ctx, out):
    kind = _kind(old)
    import_time = import_ctx and kind != "function"
    if not _same(old, new):
        lines, whole = _divergent(old, new)
        if whole or lines:
            out.append(Change(old, kind, import_time, lines, whole))
    new_children = _children(new)
    for key, child in _children(old).items():
        other = new_children.get(key)
        if other is not None:
            _walk(child, other, import_time, out)


def diff_code(old_module_code, new_module_code) -> list[Change]:
    """Changed pinned code objects, outermost first."""
    out: list[Change] = []
    _walk(old_module_code, new_module_code, True, out)
    return out


def iter_codes(co):
    """Every code object nested in `co`, `co` included."""
    stack = [co]
    while stack:
        current = stack.pop()
        yield current
        for const in current.co_consts:
            if type(const) is types.CodeType:
                stack.append(const)
