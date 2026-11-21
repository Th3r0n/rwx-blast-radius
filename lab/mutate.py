"""Generate mutant patches against pristine sqlglot. Runs inside br-lab.

    python /lab/mutate.py gen --set dev1 --n 300 --seed 101 --mix default
    python /lab/mutate.py gen --set ood1 --n 100 --seed 202 --mix ood
    python /lab/mutate.py gen --set targeted1 --n 150 --seed 303 --mix targeted
    python /lab/mutate.py gen --set holdout1 --n 200 --seed 909 --mix default --holdout
    python /lab/mutate.py copy --set sample --src /kit/sample/commits
    python /lab/mutate.py selftest --src /kit/sample/commits

Every mutant is made in a scratch copy of /opt/sqlglot (a git repo), the patch
is `git add -A && git diff --cached` (so new and deleted files work too),
the patch must pass `git apply --check` against /opt/sqlglot and change at
least one file. Patches go to <corpus>/<set>/patches/<id>.patch (written
atomically, so the farm never reads half a patch) and a lab-only sidecar to
<corpus>/<set>/meta/<id>.meta.json. The selector must never read meta.

Mixes (kind shares follow the 30 sample commits):
  default   code 53%, fixture 27%, comment 10%, multi 10%
  targeted  TPC-path code, import-time state, dialect registry/metaclass,
            fixtures outside optimizer/, sqlglot/typing, serde, ';' in fixtures
  ood       multi-line edits, function add/remove/rename, new imports, new and
            deleted files, tests/*.py edits, sqlglotc/, docs, __init__.py
Deterministic for a given --seed (and corpus ids already taken).
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import warnings
from pathlib import Path

PRISTINE = Path("/opt/sqlglot")
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

COMMENTS = [
    "# Cheap check first",
    "# Intentionally not cached",
    "# Kept for backwards compatibility",
    "# Keep in sync with the generator",
    "# Mirrors the behavior of the reference implementation",
    "# Fast path for the common case",
    "# Order matters here",
    "# See the dialect documentation",
    "# NOTE: this may be None",
    "# TODO: revisit once the parser handles this",
    "# Defensive: should not happen in practice",
    "# Avoid an extra copy",
    "# Matches the upstream behavior",
    "# Preserve the original casing",
    "# This is hot, keep it simple",
]
FIXTURE_TOKENS = ["0", "1", "42", "x", "a", "NULL"]

CODE_OPS = {
    # operator: weight, calibrated on the 17 code edits of the sample set
    "cmp_is": 2.0,
    "cmp_eq": 2.0,
    "cmp_rel": 1.5,
    "bool_op": 3.5,
    "bool_const": 2.0,
    "int_lit": 2.0,
    "index": 1.0,
    "copy_flag": 1.0,
    "not_remove": 1.0,
    "dict_value": 1.5,
    "default_value": 1.0,
}


class Skip(Exception):
    """This attempt produced no usable mutant; try another."""


# ---------------------------------------------------------------- sources


def char_col(line: str, byte_col: int) -> int:
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", "ignore"))


class Source:
    def __init__(self, rel: str, text: str) -> None:
        self.rel = rel
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.starts = [0]
        for line in self.lines:
            self.starts.append(self.starts[-1] + len(line))
        self._tokens = None
        self._sig = None
        self._tree = None
        self._string_rows = None

    def off(self, row: int, col: int) -> int:
        """tokenize (1-based row, char col) -> offset"""
        return self.starts[row - 1] + col

    def aoff(self, lineno: int, col: int) -> int:
        """ast (1-based line, utf-8 byte col) -> offset"""
        return self.starts[lineno - 1] + char_col(self.lines[lineno - 1], col)

    @property
    def tokens(self):
        if self._tokens is None:
            self._tokens = list(tokenize.generate_tokens(io.StringIO(self.text).readline))
            skip = {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT,
                    tokenize.DEDENT, tokenize.ENDMARKER}
            self._sig = [t for t in self._tokens if t.type not in skip]
        return self._tokens

    @property
    def sig(self):
        self.tokens
        return self._sig

    @property
    def tree(self):
        if self._tree is None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self._tree = ast.parse(self.text)
        return self._tree

    @property
    def string_rows(self) -> set[int]:
        """1-based rows whose start lies inside a multi-line string token."""
        if self._string_rows is None:
            rows: set[int] = set()
            stack = []
            for t in self.tokens:
                if t.type == tokenize.STRING and t.end[0] > t.start[0]:
                    rows.update(range(t.start[0] + 1, t.end[0] + 1))
                elif t.type == getattr(tokenize, "FSTRING_START", -1):
                    stack.append(t.start[0])
                elif t.type == getattr(tokenize, "FSTRING_END", -1) and stack:
                    s = stack.pop()
                    if t.end[0] > s:
                        rows.update(range(s + 1, t.end[0] + 1))
            self._string_rows = rows
        return self._string_rows


def apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    for start, end, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        text = text[:start] + new + text[end:]
    return text


def compiles(text: str, rel: str) -> bool:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            compile(text, rel, "exec")
            return True
        except (SyntaxError, ValueError):
            return False


# ---------------------------------------------------------------- code sites
# A site is (description, [edit-list choices]); an edit is (start, end, new).


def token_sites(src: Source, op: str) -> list[tuple[str, list[list[tuple[int, int, str]]]]]:
    sig = src.sig
    out = []
    for i, t in enumerate(sig):
        s, e = src.off(*t.start), src.off(*t.end)
        nxt = sig[i + 1] if i + 1 < len(sig) else None
        prv = sig[i - 1] if i > 0 else None
        desc = f"L{t.start[0]} {t.string!r}"
        if op == "cmp_is":
            if t.type == tokenize.NAME and t.string == "is":
                if nxt is not None and nxt.string == "not":
                    out.append((desc + " not->!=", [[(s, src.off(*nxt.end), "!=")]]))
                else:
                    out.append((desc + "->==", [[(s, e, "==")]]))
            elif t.type == tokenize.OP and t.string in ("==", "!="):
                # the reverse direction, rarer: weight it down by sampling later
                out.append((desc + "->is", [[(s, e, "is" if t.string == "==" else "is not")]]))
        elif op == "cmp_eq" and t.type == tokenize.OP and t.string in ("==", "!="):
            out.append((desc, [[(s, e, "!=" if t.string == "==" else "==")]]))
        elif op == "cmp_rel" and t.type == tokenize.OP and t.string in ("<", "<=", ">", ">="):
            new = {"<": "<=", "<=": "<", ">": ">=", ">=": ">"}[t.string]
            out.append((desc, [[(s, e, new)]]))
        elif op == "bool_op" and t.type == tokenize.NAME and t.string in ("and", "or"):
            out.append((desc, [[(s, e, "or" if t.string == "and" else "and")]]))
        elif op == "not_remove" and t.type == tokenize.NAME and t.string == "not" and nxt is not None:
            out.append((desc, [[(s, src.off(*nxt.start), "")]]))
        elif op == "bool_const" and t.type == tokenize.NAME and t.string in ("True", "False"):
            if prv is not None and prv.string == "=" and i >= 2 and sig[i - 2].string == "copy":
                continue
            out.append((desc, [[(s, e, "False" if t.string == "True" else "True")]]))
        elif op == "copy_flag" and t.type == tokenize.NAME and t.string in ("True", "False"):
            if prv is not None and prv.string == "=" and i >= 2 and sig[i - 2].string == "copy":
                out.append((desc, [[(s, e, "False" if t.string == "True" else "True")]]))
        elif op in ("int_lit", "index") and t.type == tokenize.NUMBER:
            m = re.fullmatch(r"(0[xX][0-9a-fA-F]+|[0-9]+)", t.string)
            if not m:
                continue
            value = int(t.string, 0) if t.string.lower().startswith("0x") else int(t.string)
            is_index = prv is not None and nxt is not None and (
                (prv.string == "[" and nxt.string in ("]", ":", ","))
                or (prv.string == ":" and nxt.string == "]")
                or (prv.string == "," and nxt.string == ")"
                    and any(x.string == "seq_get" for x in sig[max(0, i - 12):i]))
            )
            if (op == "index") != bool(is_index):
                continue

            def fmt(v: int, lit: str = t.string) -> str:
                if not lit.lower().startswith("0x"):
                    return str(v)
                digits = lit[2:]
                return lit[:2] + format(v, "X" if digits.upper() == digits else "x").zfill(len(digits))

            if op == "index":
                news = [1] if value == 0 else [value - 1, value + 1]
            else:
                news = [1] if value == 0 else [value - 1, value + 1]
            out.append((desc, [[(s, e, fmt(v))] for v in news]))
    return out


def _literal_alternatives(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return [str(not v)]
        if isinstance(v, int):
            return [str(v + 1)] if v == 0 else [str(v - 1), str(v + 1)]
        if v is None:
            return ["True", "False"]
    return []


def dict_sites(src: Source, module_level: bool = True):
    out = []
    bodies = []
    for node in src.tree.body:
        if isinstance(node, ast.ClassDef):
            bodies.append(("class", node.body))
            for sub in node.body:
                if isinstance(sub, ast.ClassDef):
                    bodies.append(("class", sub.body))
    if module_level:
        bodies.append(("module", src.tree.body))
    for where, body in bodies:
        for stmt in body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or not isinstance(stmt.value, ast.Dict):
                continue
            d = stmt.value
            entries = [(k, v) for k, v in zip(d.keys, d.values) if k is not None]
            if len(entries) < 2:
                continue
            for idx, (k, v) in enumerate(entries):
                vs, ve = src.aoff(v.lineno, v.col_offset), src.aoff(v.end_lineno, v.end_col_offset)
                vtext = src.text[vs:ve]
                choices = []
                for alt in _literal_alternatives(v):
                    choices.append([(vs, ve, alt)])
                others = [
                    (o_k, o_v) for j, (o_k, o_v) in enumerate(entries) if j != idx
                ]
                swaps = []
                for _, ov in others:
                    os_, oe = src.aoff(ov.lineno, ov.col_offset), src.aoff(ov.end_lineno, ov.end_col_offset)
                    otext = src.text[os_:oe]
                    if otext != vtext and "\n" not in otext and "\n" not in vtext:
                        swaps.append([(vs, ve, otext), (os_, oe, vtext)])
                if swaps:
                    choices.append(swaps[hash_choice(len(swaps), vs)])
                if choices:
                    out.append((f"{where} dict L{v.lineno} {vtext[:40]!r}", choices))
    return out


def hash_choice(n: int, salt: int) -> int:
    return (salt * 2654435761) % n


def default_sites(src: Source):
    out = []
    for node in ast.walk(src.tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = node.args
        pos = a.posonlyargs + a.args
        pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults))
        kw = [(arg, d) for arg, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
        for j, (arg, d) in enumerate(pairs + kw):
            ds, de = src.aoff(d.lineno, d.col_offset), src.aoff(d.end_lineno, d.end_col_offset)
            ae = src.aoff(arg.end_lineno, arg.end_col_offset)
            choices = [[(ds, de, alt)] for alt in _literal_alternatives(d)]
            removable = (j == 0 and pairs) or j >= len(pairs)
            if removable and "=" in src.text[ae:ds] and "\n" not in src.text[ae:ds]:
                choices.append([(ae, de, "")])
            if choices:
                out.append((f"default L{d.lineno} {node.name}.{arg.arg}", choices))
    return out


# ---------------------------------------------------------------- generator


class Generator:
    def __init__(self, rng: random.Random, tree: Path) -> None:
        self.rng = rng
        self.tree = tree
        self.sources: dict[str, Source] = {}
        self.site_cache: dict[tuple[str, str], list] = {}
        self.py_files = sorted(
            str(p.relative_to(PRISTINE)) for p in (PRISTINE / "sqlglot").rglob("*.py")
        )
        self.test_py = sorted(
            str(p.relative_to(PRISTINE)) for p in (PRISTINE / "tests").rglob("*.py")
        )
        self.opt_fixtures = sorted(
            str(p.relative_to(PRISTINE)) for p in (PRISTINE / "tests/fixtures/optimizer").glob("*.sql")
        )
        self.other_fixtures = sorted(
            str(p.relative_to(PRISTINE))
            for p in (PRISTINE / "tests/fixtures").rglob("*")
            if p.is_file() and p.suffix in (".sql", ".json") and str(p.relative_to(PRISTINE)) not in self.opt_fixtures
        )

    # -- helpers
    def src(self, rel: str) -> Source:
        if rel not in self.sources:
            self.sources[rel] = Source(rel, (PRISTINE / rel).read_bytes().decode("utf-8"))
        return self.sources[rel]

    def sites(self, rel: str, op: str):
        key = (rel, op)
        if key not in self.site_cache:
            s = self.src(rel)
            try:
                if op == "dict_value":
                    found = dict_sites(s)
                elif op == "default_value":
                    found = default_sites(s)
                else:
                    found = token_sites(s, op)
            except (SyntaxError, tokenize.TokenError):
                found = []
            self.site_cache[key] = found
        return self.site_cache[key]

    def pick_weighted(self, weights: dict[str, float]) -> str:
        names = list(weights)
        return self.rng.choices(names, weights=[weights[n] for n in names])[0]

    # -- parts: dicts {file, operator, edits | create | delete, compile, desc}
    def code_part(self, files: list[str], ops: dict[str, float] | None = None,
                  line_filter=None) -> dict:
        ops = ops or CODE_OPS
        for _ in range(60):
            op = self.pick_weighted(ops)
            cands = []
            for rel in files:
                for site in self.sites(rel, op):
                    cands.append((rel, site))
            if line_filter is not None:
                cands = [(r, s) for r, s in cands if line_filter(r, s)]
            if not cands:
                continue
            rel, (desc, choices) = self.rng.choice(cands)
            if op == "cmp_is" and desc.endswith("->is") and self.rng.random() < 0.6:
                continue  # keep the is->== direction dominant
            edits = self.rng.choice(choices)
            src = self.src(rel)
            new = apply_edits(src.text, edits)
            if new == src.text or not compiles(new, rel):
                continue
            return {"file": rel, "operator": op, "edits": edits, "desc": desc,
                    "line": src.text.count("\n", 0, edits[0][0]) + 1}
        raise Skip("no code site")

    def comment_part(self, files: list[str], in_string: bool | None = None) -> dict:
        if in_string is None:
            in_string = self.rng.random() < 0.12
        for _ in range(60):
            rel = self.rng.choice(files)
            src = self.src(rel)
            try:
                srows = src.string_rows
            except tokenize.TokenError:
                continue
            rows = [
                r for r in range(1, len(src.lines) + 1)
                if src.lines[r - 1].strip() and ((r in srows) == in_string)
            ]
            if not rows:
                continue
            r = self.rng.choice(rows)
            line = src.lines[r - 1]
            indent = line[: len(line) - len(line.lstrip())]
            text = indent + self.rng.choice(COMMENTS) + "\n"
            off = src.starts[r - 1]
            new = apply_edits(src.text, [(off, off, text)])
            if not compiles(new, rel):
                continue
            return {"file": rel, "operator": "comment_in_string" if in_string else "comment",
                    "edits": [(off, off, text)], "desc": f"before L{r}", "line": r}
        raise Skip("no comment site")

    def fixture_part(self, files: list[str], semicolon: bool = False) -> dict:
        for _ in range(60):
            # half the time uniform over files, half over lines
            if self.rng.random() < 0.5:
                rel = self.rng.choice(files)
            else:
                rel = self.rng.choices(files, weights=[len(self.src(f).lines) for f in files])[0]
            src = self.src(rel)
            u = self.rng.random()
            cat = "sql" if u < 0.85 else ("meta" if u < 0.95 else "dashdash")
            rows = []
            for r, line in enumerate(src.lines, 1):
                s = line.strip()
                if not s:
                    continue
                c = "meta" if s.startswith("#") else ("dashdash" if s.startswith("--") else "sql")
                if c == cat:
                    rows.append(r)
            if not rows:
                continue
            r = self.rng.choice(rows)
            line = src.lines[r - 1]
            body = line.rstrip("\r\n")
            base = src.starts[r - 1]
            toks = [m for m in re.finditer(r"\w+|[^\w\s]", body)]
            if not toks:
                continue
            if semicolon:
                semis = [m for m in toks if m.group() == ";"]
                mode = self.rng.choice(["remove", "add_end", "insert"]) if semis else self.rng.choice(["add_end", "insert"])
                if mode == "remove":
                    m = self.rng.choice(semis)
                    edits = [(base + m.start(), base + m.end(), "")]
                elif mode == "add_end":
                    edits = [(base + len(body), base + len(body), ";")]
                else:
                    m = self.rng.choice(toks)
                    edits = [(base + m.start(), base + m.start(), "; ")]
                op = "fixture_semicolon_" + mode
            elif self.rng.random() < 0.8:
                m = self.rng.choice(toks)
                tok = self.rng.choice(FIXTURE_TOKENS)
                edits = [(base + m.start(), base + m.start(), tok + " ")]
                op = "fixture_insert"
            else:
                m = self.rng.choice(toks)
                edits = [(base + m.start(), base + m.end(), "")]
                op = "fixture_remove"
            if apply_edits(src.text, edits) == src.text:
                continue
            return {"file": rel, "operator": op + ("" if cat == "sql" else "_" + cat),
                    "edits": edits, "desc": f"L{r}", "line": r}
        raise Skip("no fixture site")

    def text_part(self, files: list[str]) -> dict:
        """Generic one-line edit for non-Python files (docs, config)."""
        for _ in range(40):
            rel = self.rng.choice(files)
            src = self.src(rel)
            rows = [r for r, l in enumerate(src.lines, 1) if l.strip()]
            if not rows:
                continue
            r = self.rng.choice(rows)
            base = src.starts[r - 1]
            body = src.lines[r - 1].rstrip("\r\n")
            mode = self.rng.choice(["insert_line", "delete_line", "word"])
            if mode == "insert_line":
                edits = [(base, base, "Lab note: see the documentation.\n")]
            elif mode == "delete_line":
                edits = [(base, src.starts[r], "")]
            else:
                words = list(re.finditer(r"[A-Za-z]{3,}", body))
                if not words:
                    continue
                m = self.rng.choice(words)
                edits = [(base + m.start(), base + m.end(), m.group()[::-1])]
            return {"file": rel, "operator": "text_" + mode, "edits": edits, "desc": f"L{r}", "line": r}
        raise Skip("no text site")

    # -- whole-mutant styles
    def make(self, style: str) -> tuple[str, list[dict], dict]:
        """Returns (kind, parts, extra meta)."""
        R = self.rng
        py = self.py_files
        if style == "code":
            return "code", [self.code_part(py)], {}
        if style == "fixture":
            return "fixture", [self.fixture_part(self.opt_fixtures)], {}
        if style == "comment":
            return "comment", [self.comment_part(py)], {}
        if style == "multi":
            n = 2 if R.random() < 0.7 else 3
            parts: list[dict] = []
            used: set[str] = set()
            while len(parts) < n:
                sub = R.choices(["comment", "fixture", "code"], weights=[0.45, 0.35, 0.2])[0]
                if sub == "comment":
                    p = self.comment_part([f for f in py if f not in used])
                elif sub == "fixture":
                    p = self.fixture_part([f for f in self.opt_fixtures if f not in used])
                else:
                    p = self.code_part([f for f in py if f not in used])
                used.add(p["file"])
                parts.append(p)
            return "multi", parts, {}
        # ---- targeted lots
        if style == "t_tpc":
            files = [f for f in py if f.startswith(("sqlglot/optimizer/", "sqlglot/executor/"))
                     or f in ("sqlglot/planner.py", "sqlglot/generators/duckdb.py", "sqlglot/dialects/duckdb.py")]
            return "code", [self.code_part(files)], {"lot": "tpc"}
        if style == "t_import_state":
            ops = {"dict_value": 3.0, "default_value": 2.0, "int_lit": 1.0, "bool_const": 1.0}
            p = self.code_part(py, ops)
            # int/bool literals only count when they sit in class-body or module-level statements
            if p["operator"] in ("int_lit", "bool_const"):
                p = self.import_time_token_part(py)
            return "code", [p], {"lot": "import_state"}
        if style == "t_registry":
            files = ["sqlglot/dialects/__init__.py", "sqlglot/dialects/dialect.py"]
            lo, hi = self.dialect_metaclass_lines()
            def in_registry(rel, site):
                if rel == "sqlglot/dialects/__init__.py":
                    return True
                line = site_line(site[0])
                return line is not None and lo <= line <= hi
            if R.random() < 0.2:
                return "comment", [self.comment_part(files)], {"lot": "registry"}
            return "code", [self.code_part(files, line_filter=in_registry)], {"lot": "registry"}
        if style == "t_fixture_other":
            return "fixture", [self.fixture_part(self.other_fixtures)], {"lot": "fixture_other"}
        if style == "t_typing":
            files = [f for f in py if f.startswith("sqlglot/typing/")]
            return "code", [self.code_part(files)], {"lot": "typing"}
        if style == "t_serde":
            return "code", [self.code_part(["sqlglot/serde.py"])], {"lot": "serde"}
        if style == "t_semicolon":
            files = self.opt_fixtures + [f for f in self.other_fixtures if f.endswith(".sql")]
            return "fixture", [self.fixture_part(files, semicolon=True)], {"lot": "semicolon"}
        # ---- out of distribution
        if style.startswith("o_"):
            return "ood", self.ood_parts(style[2:]), {"lot": style[2:]}
        raise ValueError(style)

    def dialect_metaclass_lines(self) -> tuple[int, int]:
        src = self.src("sqlglot/dialects/dialect.py")
        for node in src.tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "_Dialect":
                return node.lineno, node.end_lineno
        return 0, 0

    def import_time_token_part(self, files: list[str]) -> dict:
        """int/bool literal in a class-body or module-level statement."""
        for _ in range(40):
            rel = self.rng.choice(files)
            src = self.src(rel)
            spans = []
            for node in src.tree.body:
                if isinstance(node, ast.ClassDef):
                    spans += [(s.lineno, s.end_lineno) for s in node.body
                              if isinstance(s, (ast.Assign, ast.AnnAssign))]
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    spans.append((node.lineno, node.end_lineno))
            sites = []
            for op in ("int_lit", "bool_const"):
                for desc, choices in self.sites(rel, op):
                    ln = site_line(desc)
                    if any(a <= ln <= b for a, b in spans):
                        sites.append((op, desc, choices))
            if not sites:
                continue
            op, desc, choices = self.rng.choice(sites)
            edits = self.rng.choice(choices)
            new = apply_edits(src.text, edits)
            if new != src.text and compiles(new, rel):
                return {"file": rel, "operator": op + "_import_time", "edits": edits, "desc": desc,
                        "line": site_line(desc)}
        raise Skip("no import-time site")

    def ood_parts(self, what: str) -> list[dict]:
        R = self.rng
        py = self.py_files
        if what == "multiline":
            mode = R.choice(["delete_block", "two_tokens", "swap_lines"])
            if mode == "two_tokens":
                a = self.code_part(py)
                src = self.src(a["file"])
                ln = a["line"]
                for _ in range(30):
                    b = self.code_part([a["file"]])
                    if b["line"] != ln and abs(b["line"] - ln) <= 12 and not overlaps(a["edits"], b["edits"]):
                        edits = a["edits"] + b["edits"]
                        if compiles(apply_edits(src.text, edits), a["file"]):
                            return [{"file": a["file"], "operator": "multiline_two_tokens", "edits": edits,
                                     "desc": a["desc"] + " + " + b["desc"], "line": ln}]
                raise Skip("no second token")
            for _ in range(40):
                rel = R.choice(py)
                src = self.src(rel)
                funcs = [n for n in ast.walk(src.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and len(n.body) >= 3]
                if not funcs:
                    continue
                fn = R.choice(funcs)
                body = fn.body
                if mode == "delete_block":
                    i = R.randrange(1, len(body))
                    j = min(len(body), i + R.randint(1, 2))
                    first, last = body[i], body[j - 1]
                    if last.end_lineno == first.lineno and j < len(body):
                        last = body[j]
                    if last.end_lineno == first.lineno:
                        continue
                    s, e = src.starts[first.lineno - 1], src.starts[last.end_lineno]
                    edits = [(s, e, "")]
                else:
                    pairs = [(x, y) for x, y in zip(body, body[1:])
                             if x.lineno == x.end_lineno and y.lineno == y.end_lineno == x.lineno + 1]
                    if not pairs:
                        continue
                    x, y = R.choice(pairs)
                    lx, ly = src.lines[x.lineno - 1], src.lines[y.lineno - 1]
                    if lx == ly:
                        continue
                    edits = [(src.starts[x.lineno - 1], src.starts[y.lineno], ly + lx)]
                new = apply_edits(src.text, edits)
                if new != src.text and compiles(new, rel):
                    return [{"file": rel, "operator": "multiline_" + mode, "edits": edits,
                             "desc": f"{fn.name}", "line": body[0].lineno}]
            raise Skip("no multiline site")
        if what in ("func_add", "func_remove", "func_rename", "func_rename_callers"):
            for _ in range(40):
                rel = R.choice(py)
                src = self.src(rel)
                tops = [n for n in src.tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
                defs = [n for n in ast.walk(src.tree) if isinstance(n, ast.FunctionDef)]
                if what == "func_add":
                    if not tops:
                        continue
                    n = R.choice(tops)
                    start = min([n.lineno] + [d.lineno for d in n.decorator_list])
                    off = src.starts[start - 1]
                    name = "_lab_helper_" + "".join(R.choice("abcdefgh") for _ in range(4))
                    text = f"def {name}(value):\n    return value\n\n\n"
                    edits = [(off, off, text)]
                    desc = f"before {n.name}"
                elif what == "func_remove":
                    if not defs:
                        continue
                    n = R.choice(defs)
                    start = min([n.lineno] + [d.lineno for d in n.decorator_list])
                    s, e = src.starts[start - 1], src.starts[n.end_lineno]
                    # swallow one following blank line
                    if n.end_lineno < len(src.lines) and not src.lines[n.end_lineno].strip():
                        e = src.starts[n.end_lineno + 1]
                    edits = [(s, e, "")]
                    desc = n.name
                else:
                    if not defs:
                        continue
                    n = R.choice(defs)
                    if n.name.startswith("__"):
                        continue
                    new_name = n.name + "_v2"
                    line = src.lines[n.lineno - 1]
                    m = re.search(r"def\s+(" + re.escape(n.name) + r")\b", line)
                    if not m:
                        continue
                    s = src.starts[n.lineno - 1] + m.start(1)
                    if what == "func_rename":
                        edits = [(s, s + len(n.name), new_name)]
                        parts_out = [{"file": rel, "operator": "func_rename", "edits": edits,
                                      "desc": n.name, "line": n.lineno}]
                    else:
                        pat = re.compile(r"\b" + re.escape(n.name) + r"\b")
                        parts_out = []
                        for other in py:
                            osrc = self.src(other)
                            hits = [(m2.start(), m2.end(), new_name) for m2 in pat.finditer(osrc.text)]
                            if hits:
                                parts_out.append({"file": other, "operator": "func_rename_callers",
                                                  "edits": hits, "desc": n.name, "line": 0})
                        if len(parts_out) > 6 or sum(len(p["edits"]) for p in parts_out) > 25:
                            continue
                    ok = all(compiles(apply_edits(self.src(p["file"]).text, p["edits"]), p["file"]) for p in parts_out)
                    if ok:
                        return parts_out
                    continue
                new = apply_edits(src.text, edits)
                if new != src.text and compiles(new, rel):
                    return [{"file": rel, "operator": what, "edits": edits, "desc": desc,
                             "line": src.text.count("\n", 0, edits[0][0]) + 1}]
            raise Skip("no func site")
        if what == "import_add":
            for _ in range(40):
                rel = R.choice(py)
                src = self.src(rel)
                imports = [n for n in src.tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
                if not imports:
                    continue
                last = imports[-1] if R.random() < 0.6 else R.choice(imports)
                line = R.choice(["import os\n", "import re\n", "import json\n", "import typing as t\n",
                                 "from sqlglot import exp\n", "import sqlglot\n",
                                 "from sqlglot.helper import seq_get\n", "import itertools\n",
                                 "from sqlglot.optimizer import optimize\n"])
                off = src.starts[last.end_lineno]
                edits = [(off, off, line)]
                new = apply_edits(src.text, edits)
                if new != src.text and compiles(new, rel):
                    return [{"file": rel, "operator": "import_add", "edits": edits, "desc": line.strip(),
                             "line": last.end_lineno + 1}]
            raise Skip("no import site")
        if what == "new_file":
            choice = R.choice(["sqlglot_unused", "tests_helper", "dialect_module", "tests_module"])
            tag = "".join(R.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(4))
            if choice == "sqlglot_unused":
                rel, body = f"sqlglot/lab_{tag}.py", '"""Unused helper module."""\n\n\ndef helper(value):\n    return value\n'
            elif choice == "tests_helper":
                rel, body = f"tests/lab_{tag}_helpers.py", "def make_rows(n):\n    return [{\"a\": i} for i in range(n)]\n"
            elif choice == "dialect_module":
                rel, body = (f"sqlglot/dialects/lab{tag}.py",
                             "from sqlglot.dialects.dialect import Dialect\n\n\nclass Lab" + tag.capitalize() + "(Dialect):\n    pass\n")
            else:
                rel, body = (f"tests/test_lab_{tag}.py",
                             "import unittest\n\n\nclass TestLab(unittest.TestCase):\n    def test_noop(self):\n        self.assertTrue(True)\n")
            return [{"file": rel, "operator": "new_file_" + choice, "create": body, "desc": rel, "line": 1}]
        if what == "delete_file":
            choice = R.choice(["fixture", "sqlglot_module", "test_file", "doc"])
            if choice == "fixture":
                rel = R.choice(self.opt_fixtures + [f for f in self.other_fixtures if f.endswith(".sql")])
            elif choice == "sqlglot_module":
                rel = R.choice([f for f in py if not f.endswith("__init__.py")])
            elif choice == "test_file":
                rel = R.choice([f for f in self.test_py if "/test_" in f])
            else:
                rel = R.choice(["CHANGELOG.md", "posts/ast_primer.md", "CONTRIBUTING.md", "AGENTS.md"])
            return [{"file": rel, "operator": "delete_file_" + choice, "delete": True, "desc": rel, "line": 0}]
        if what == "tests_edit":
            mode = R.choice(["code", "code", "string", "helpers", "class_body"])
            files = [f for f in self.test_py if f.endswith(".py")]
            if mode == "helpers":
                return [self.code_part(["tests/helpers.py"]) | {"operator": "tests_helpers"}]
            if mode == "code":
                p = self.code_part(files)
                p["operator"] = "tests_" + p["operator"]
                return [p]
            if mode == "class_body":
                p = self.import_time_token_part([f for f in files if "/test_" in f])
                p["operator"] = "tests_class_body"
                return [p]
            for _ in range(60):
                rel = R.choice([f for f in files if "/test_" in f])
                src = self.src(rel)
                strs = [t for t in src.sig if t.type == tokenize.STRING and t.start[0] == t.end[0]
                        and len(t.string) > 4]
                if not strs:
                    continue
                t = R.choice(strs)
                s, e = src.off(*t.start), src.off(*t.end)
                q = 1 if t.string[0] in "\"'" else 2
                pos = s + q + R.randrange(max(1, len(t.string) - 2 * q - 1))
                edits = [(pos, pos, R.choice([" ", "x", "1"]))]
                new = apply_edits(src.text, edits)
                if new != src.text and compiles(new, rel):
                    return [{"file": rel, "operator": "tests_string", "edits": edits,
                             "desc": f"L{t.start[0]}", "line": t.start[0]}]
            raise Skip("no test string")
        if what == "sqlglotc":
            if R.random() < 0.5:
                p = self.code_part(["sqlglotc/setup.py"])
                p["operator"] = "sqlglotc_" + p["operator"]
                return [p]
            p = self.text_part(["sqlglotc/pyproject.toml", "sqlglotc/MANIFEST.in"])
            p["operator"] = "sqlglotc_" + p["operator"]
            return [p]
        if what == "docs":
            files = ["README.md", "CHANGELOG.md", "CONTRIBUTING.md", "AGENTS.md", "posts/ast_primer.md",
                     "posts/onboarding.md", "setup.cfg", "Makefile", "pyproject.toml", "MANIFEST.in"]
            files = [f for f in files if (PRISTINE / f).exists()]
            p = self.text_part(files)
            p["operator"] = "docs_" + p["operator"]
            return [p]
        if what == "init":
            inits = [f for f in py if f.endswith("__init__.py")]
            u = R.random()
            if u < 0.5:
                p = self.code_part(inits)
            elif u < 0.75:
                p = self.comment_part(inits, in_string=False)
            else:
                src_files = inits
                for _ in range(20):
                    rel = R.choice(src_files)
                    src = self.src(rel)
                    imports = [n for n in src.tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
                    if imports:
                        off = src.starts[imports[-1].end_lineno]
                        edits = [(off, off, "import os\n")]
                        p = {"file": rel, "operator": "import_add", "edits": edits, "desc": "import os",
                             "line": imports[-1].end_lineno + 1}
                        break
                else:
                    raise Skip("no init import")
            p["operator"] = "init_" + p["operator"]
            return [p]
        raise ValueError(what)


def site_line(desc: str) -> int | None:
    m = re.search(r"(?<![A-Za-z])L(\d+)", desc)
    return int(m.group(1)) if m else None


def overlaps(a, b) -> bool:
    return any(x[0] < y[1] and y[0] < x[1] for x in a for y in b)


MIXES = {
    "default": {"code": 53, "fixture": 27, "comment": 10, "multi": 10},
    "targeted": {"t_tpc": 35, "t_import_state": 30, "t_registry": 15, "t_fixture_other": 20,
                 "t_typing": 20, "t_serde": 12, "t_semicolon": 18},
    "ood": {"o_multiline": 15, "o_func_add": 7, "o_func_remove": 8, "o_func_rename": 6,
            "o_func_rename_callers": 5, "o_import_add": 8, "o_new_file": 8, "o_delete_file": 8,
            "o_tests_edit": 15, "o_sqlglotc": 5, "o_docs": 8, "o_init": 7},
}


def quota(mix: dict[str, int], n: int) -> list[str]:
    """Exact per-style counts by largest remainder, so shares match the mix."""
    total = sum(mix.values())
    raw = {k: n * v / total for k, v in mix.items()}
    counts = {k: int(x) for k, x in raw.items()}
    rest = n - sum(counts.values())
    for k in sorted(raw, key=lambda k: raw[k] - counts[k], reverse=True)[:rest]:
        counts[k] += 1
    styles = [k for k in mix for _ in range(counts[k])]
    return styles


# ---------------------------------------------------------------- git plumbing


def git(tree: Path, *args: str, input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=tree, input=input_bytes, capture_output=True, check=check)


def fresh_tree() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="mut-"))
    tree = tmp / "sqlglot"
    subprocess.run(["cp", "-a", str(PRISTINE), str(tree)], check=True)
    return tree


def reset_tree(tree: Path) -> None:
    git(tree, "reset", "-q", "--hard")
    git(tree, "clean", "-fdq")


def render(tree: Path, parts: list[dict]) -> bytes:
    for p in parts:
        path = tree / p["file"]
        if p.get("delete"):
            path.unlink()
        elif "create" in p:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(p["create"], "utf-8")
        else:
            text = path.read_bytes().decode("utf-8")
            path.write_bytes(apply_edits(text, p["edits"]).encode("utf-8"))
    git(tree, "add", "-A")
    diff = git(tree, "diff", "--cached", "--no-color", "--no-ext-diff").stdout
    reset_tree(tree)
    return diff


def check_applies(patch: bytes) -> bool:
    r = subprocess.run(["git", "apply", "--check", "-"], cwd=PRISTINE, input=patch, capture_output=True)
    return r.returncode == 0


def new_id(rng: random.Random, taken: set[str]) -> str:
    while True:
        cid = "".join(rng.choice(ID_ALPHABET) for _ in range(4))
        if cid not in taken:
            taken.add(cid)
            return cid


def taken_ids(corpus: Path) -> set[str]:
    out = set()
    if corpus.exists():
        for p in corpus.glob("*/patches/*.patch"):
            out.add(p.stem)
    return out


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def cmd_gen(args) -> int:
    rng = random.Random(f"{args.set}:{args.seed}")
    corpus = args.corpus
    set_dir = corpus / args.set
    if (set_dir / "patches").exists() and any((set_dir / "patches").glob("*.patch")) and not args.append:
        print(f"{set_dir}/patches already has patches; use --append to add more", file=sys.stderr)
        return 1
    taken = taken_ids(corpus)
    tree = fresh_tree()
    gen = Generator(rng, tree)
    styles = quota(MIXES[args.mix], args.n)
    rng.shuffle(styles)
    made = 0
    failures = 0
    seen_patches: set[bytes] = set()
    from collections import Counter
    stats: Counter = Counter()
    for style in styles:
        for attempt in range(25):
            try:
                kind, parts, extra = gen.make(style)
            except Skip:
                failures += 1
                continue
            patch = render(tree, parts)
            if not patch.strip() or patch in seen_patches:
                failures += 1
                continue
            if not check_applies(patch):
                failures += 1
                continue
            seen_patches.add(patch)
            cid = new_id(rng, taken)
            meta = {
                "id": cid,
                "set": args.set,
                "kind": kind,
                "style": style,
                "operator": "+".join(p["operator"] for p in parts),
                "files": sorted({p["file"] for p in parts}),
                "parts": [
                    {k: v for k, v in p.items() if k in ("file", "operator", "desc", "line")}
                    | {"action": "delete" if p.get("delete") else ("create" if "create" in p else "modify")}
                    for p in parts
                ],
                "seed": args.seed,
                "mix": args.mix,
                "holdout": bool(args.holdout),
                "lab_only": "never read by the selector",
            } | extra
            write_atomic(set_dir / "meta" / f"{cid}.meta.json", (json.dumps(meta, indent=1, sort_keys=True) + "\n").encode())
            write_atomic(set_dir / "patches" / f"{cid}.patch", patch)
            stats[kind] += 1
            made += 1
            break
        else:
            print(f"gave up on style {style}", file=sys.stderr)
    if args.holdout:
        (set_dir / "NEVER_TUNE.txt").write_text(
            "Holdout set: never inspect failures, never tune the selector on it.\n"
            "Use only for a final go/no-go estimate.\n", "utf-8")
    shutil.rmtree(tree.parent, ignore_errors=True)
    print(json.dumps({"set": args.set, "made": made, "rejected_attempts": failures, "kinds": stats}))
    return 0


def cmd_copy(args) -> int:
    dst = args.corpus / args.set / "patches"
    n = 0
    for p in sorted(Path(args.src).glob("*.patch")):
        write_atomic(dst / p.name, p.read_bytes())
        n += 1
    print(f"copied {n} patches into {dst}")
    return 0


def cmd_selftest(args) -> int:
    """Apply each sample patch and regenerate it: must be byte-identical."""
    tree = fresh_tree()
    ok = bad = 0
    for p in sorted(Path(args.src).glob("*.patch")):
        original = p.read_bytes()
        r = git(tree, "apply", str(p), check=False)
        if r.returncode:
            print("apply failed", p.name, r.stderr.decode())
            bad += 1
            continue
        git(tree, "add", "-A")
        regen = git(tree, "diff", "--cached", "--no-color", "--no-ext-diff").stdout
        reset_tree(tree)
        if regen == original:
            ok += 1
        else:
            bad += 1
            print("DIFFERS", p.name)
    shutil.rmtree(tree.parent, ignore_errors=True)
    print(f"selftest: {ok} identical, {bad} different")
    return 0 if bad == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--set", required=True)
    g.add_argument("--n", type=int, required=True)
    g.add_argument("--seed", type=int, required=True)
    g.add_argument("--mix", choices=sorted(MIXES), default="default")
    g.add_argument("--holdout", action="store_true")
    g.add_argument("--append", action="store_true")
    g.add_argument("--corpus", type=Path, default=Path("/lab/corpus"))
    c = sub.add_parser("copy")
    c.add_argument("--set", required=True)
    c.add_argument("--src", required=True)
    c.add_argument("--corpus", type=Path, default=Path("/lab/corpus"))
    s = sub.add_parser("selftest")
    s.add_argument("--src", required=True)
    args = ap.parse_args(argv)
    return {"gen": cmd_gen, "copy": cmd_copy, "selftest": cmd_selftest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
