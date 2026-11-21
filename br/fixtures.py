"""Parse sqlglot's SQL fixtures exactly like tests/helpers.py at the pinned commit.

The tests read `tests/fixtures/**/*.sql` only through `load_sql_fixtures` and
`load_sql_fixture_pairs` (checked by grep and by the file-read traces). Two
versions of a fixture file whose parsed entries are equal therefore look the
same to every test. The functions below restate those helpers' logic (sqlglot,
MIT licence) on in-memory bytes, including universal-newline decoding and
`str.splitlines()`, which the helpers use.
"""

from __future__ import annotations


class FixtureDecodeError(Exception):
    pass


def _text(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:  # the helpers would raise too
        raise FixtureDecodeError(str(exc)) from exc
    # open(..., encoding="utf-8") translates newlines on read.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _filter_comments(s: str) -> str:
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _extract_meta(sql: str):
    meta = {}
    sql_lines = sql.split("\n")
    i = 0
    while sql_lines[i].startswith("#"):
        key, val = sql_lines[i].split(":", maxsplit=1)
        meta[key.lstrip("#").strip()] = val.strip()
        i += 1
    return "\n".join(sql_lines[i:]), meta


def fixtures(data: bytes) -> list[str]:
    """What `load_sql_fixtures` yields."""
    out = []
    lines: list[str] = []
    for line in _filter_comments(_text(data)).splitlines():
        lines.append(line)
        if not line.startswith("#"):
            out.append("\n".join(lines))
            lines.clear()
    return out


def fixture_pairs(data: bytes) -> list:
    """What `load_sql_fixture_pairs` yields, or an exception instance it would raise."""
    statements = _filter_comments(_text(data)).split(";")
    out = []
    size = len(statements)
    for i in range(0, size, 2):
        if i + 1 < size:
            sql = statements[i].strip()
            try:
                sql, meta = _extract_meta(sql)
            except Exception as exc:  # e.g. a malformed "#" line: the generator raises here
                out.append(("raise", type(exc).__name__))
                break
            expected = statements[i + 1].strip()
            out.append((tuple(sorted(meta.items())), sql, expected))
    return out


def entries_equal(old: bytes, new: bytes) -> bool:
    """True when both helpers yield exactly the same entries for both versions."""
    try:
        return fixtures(old) == fixtures(new) and fixture_pairs(old) == fixture_pairs(new)
    except FixtureDecodeError:
        return False


def changed_pair_indexes(old: bytes, new: bytes):
    """Indexes (0-based, in yield order) of pairs that differ, or None if not comparable.

    None means the pair lists cannot be aligned (different length, a raise),
    so every entry must be treated as changed.
    """
    try:
        a = fixture_pairs(old)
        b = fixture_pairs(new)
    except FixtureDecodeError:
        return None
    if len(a) != len(b) or any(entry[0] == "raise" for entry in a + b):
        return None
    return [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
