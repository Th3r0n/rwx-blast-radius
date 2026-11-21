"""The scorer's output rules (kit/SCORING.md, kit/score.py), to check lines before printing.

check_line() applies the per-line rules in the scorer's order and returns its
rejection code; check_output() adds the whole-output rules; encode_line()
writes a compact line with plain ints and empty workers dropped.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

__all__ = ["MAX_WORKERS", "MAX_OUTPUT_BYTES", "check_line", "check_output", "encode_line"]

MAX_WORKERS = 8
MAX_OUTPUT_BYTES = 2_000_000  # "stdout may hold 2 MB": the stricter reading
# Longer integer literals exceed CPython's default int-conversion limit; the
# scorer reads them as floats, so this module does too.
_LONGEST_INTEGER_LITERAL = 4300


class _NotJson(Exception):
    pass


def _reject_constant(name: str) -> object:
    raise _NotJson(name)


def _parse_int(literal: str) -> int | float:
    if len(literal) > _LONGEST_INTEGER_LITERAL:
        return float(literal)
    return int(literal)


def _whole_number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def check_line(line: str, commits: set[str], n_tests: int, seen: set[str]) -> str | None:
    """Return the scorer's rejection code for one output line, or None if it is valid.

    commits are the known commit ids, tests are numbered 0..n_tests-1, and seen
    holds the commits of earlier lines; a valid line's commit is added to seen.
    """
    try:
        value = json.loads(line, parse_constant=_reject_constant, parse_int=_parse_int)
    except (_NotJson, ValueError, RecursionError):
        return "not_json"
    if not (
        isinstance(value, dict)
        and set(value.keys()) == {"commit", "workers"}
        and isinstance(value["commit"], str)
        and isinstance(value["workers"], list)
    ):
        return "wrong_shape"
    commit = value["commit"]
    if commit not in commits:
        return "unknown_commit"
    if commit in seen:
        return "repeated_commit"
    numbers: list[int] = []
    occupied = 0
    for worker in value["workers"]:
        if not isinstance(worker, list):
            return "wrong_shape"
        for item in worker:
            number = _whole_number(item)
            if number is None:
                return "wrong_shape"
            numbers.append(number)
        occupied += bool(worker)
    if any(not 0 <= number < n_tests for number in numbers):
        return "unknown_test"
    if len(set(numbers)) != len(numbers):
        return "repeated_test"
    if occupied > MAX_WORKERS:
        return "too_many_workers"
    seen.add(commit)
    return None


def check_output(stdout: bytes, commits: set[str], n_tests: int) -> tuple[str, int | None] | None:
    """Check a whole stdout; return (code, 1-based line or None) or None if valid.

    Codes are the scorer's, plus "too_large" for more than MAX_OUTPUT_BYTES.
    """
    if len(stdout) > MAX_OUTPUT_BYTES:
        return "too_large", None
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return "not_utf8", None
    pieces = text.split("\n") if text else []
    if text.endswith("\n"):
        pieces.pop()
    seen: set[str] = set()
    for number, line in enumerate(pieces, start=1):
        code = check_line(line, commits, n_tests, seen)
        if code is not None:
            return code, number
    if any(commit not in seen for commit in commits):
        return "missing_commit", None
    return None


def encode_line(commit: str, workers: Iterable[Iterable[int]]) -> str:
    """Compact JSON line (no trailing newline) with int test numbers; empty workers dropped."""
    if not isinstance(commit, str):
        raise TypeError("commit must be a str")
    out: list[list[int]] = []
    for worker in workers:
        tests = []
        for test in worker:
            if isinstance(test, bool) or not isinstance(test, int):
                raise TypeError(f"test number must be an int, not {test!r}")
            tests.append(int(test))
        if tests:
            out.append(tests)
    return json.dumps({"commit": commit, "workers": out}, separators=(",", ":"), ensure_ascii=True)
