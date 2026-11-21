"""br.validate must agree with kit/score.py (run: python -I selftest/test_validate.py)."""

from __future__ import annotations

import importlib.util
import json
import os
import random
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from br import validate as bv  # noqa: E402


def load_score():
    spec = importlib.util.spec_from_file_location("kit_score", os.path.join(REPO, "kit", "score.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCORE = load_score()
with open(os.path.join(REPO, "kit", "sample", "tests.json"), encoding="utf-8") as _handle:
    TESTS = json.load(_handle)["tests"]
with open(os.path.join(REPO, "kit", "sample", "answers.json"), encoding="utf-8") as _handle:
    ANSWERS = json.load(_handle)["commits"]
COMMITS = set(ANSWERS)
N_TESTS = len(TESTS)
FIRST = sorted(COMMITS)[0]

GOOD = [
    '{"commit":"%s","workers":[]}',
    '{"commit": "%s", "workers": [[1, 2], [3]]}',
    '{"workers":[[0]],"commit":"%s"}',
    '{"commit":"%s","workers":[[1619]]}',
    '{"commit":"%s","workers":[[3.0]]}',
    '{"commit":"%s","workers":[[1e2, 1E0]]}',
    '{"commit":"%s","workers":[[-0], [0.5e1], [20.0e-1]]}',
    '{"commit":"%s","workers":[[],[],[],[],[],[],[],[],[],[1]]}',
    '{"commit":"%s","workers":[[1],[2],[3],[4],[5],[6],[7],[8]]}',
    ' \t{"commit":"%s","workers":[]} ',
    '{"commit":"%s","workers":[]}\r',
    '{"commit":"nope","commit":"%s","workers":[]}',
    '{"commit":"%s","workers":[[7, 1.0e3, 10E-1]]}',
]
BAD = [
    "",
    " ",
    "null",
    "[]",
    "1",
    '"x"',
    '{"commit":"%s","workers":[[NaN]]}',
    '{"commit":"%s","workers":[[Infinity]]}',
    '{"commit":"%s","workers":[[-Infinity]]}',
    '{"commit":"%s","workers":[[1e400]]}',
    '{"commit":"%s"}',
    '{"commit":"%s","workers":[],"x":1}',
    '{"commit":2,"workers":[]}',
    '{"commit":"%s","workers":{}}',
    '{"commit":"%s","workers":null}',
    '{"commit":"zzzz","workers":[]}',
    '{"commit":"zzzz","workers":[["a"]]}',
    '{"commit":"%s","workers":[1]}',
    '{"commit":"%s","workers":[[true]]}',
    '{"commit":"%s","workers":[[false]]}',
    '{"commit":"%s","workers":[["3"]]}',
    '{"commit":"%s","workers":[[3.5]]}',
    '{"commit":"%s","workers":[[null]]}',
    '{"commit":"%s","workers":[[[1]]]}',
    '{"commit":"%s","workers":[[1620]]}',
    '{"commit":"%s","workers":[[-1]]}',
    '{"commit":"%s","workers":[[1],[1]]}',
    '{"commit":"%s","workers":[[1,1]]}',
    '{"commit":"%s","workers":[[1.0,1]]}',
    '{"commit":"%s","workers":[[1],[2],[3],[4],[5],[6],[7],[8],[9]]}',
    '{"commit":"%s","workers":[[1620,1,1]]}',
    '{"commit":"%s","workers":[[1,1],[2],[3],[4],[5],[6],[7],[8],[9]]}',
    '{"commit":"%s","workers":[[1620],["x"]]}',
    '{"commit":"%s","workers":[[' + "1" * 5000 + ']]}',
    '{"commit":"%s","workers":[[' + "1" * 4300 + ']]}',
    '{"commit":"%s","workers":[[-' + "1" * 4300 + ']]}',
    '﻿{"commit":"%s","workers":[]}',
    "[" * 100000,
    '{"commit":"%s","workers":[[1]]} x',
    "{'commit':'%s','workers':[]}",
    '{"commit":"%s","workers":[[01]]}',
    '{"commit":"\\ud800","workers":[]}',
    '{"commit":"%s","workers":[[1]],}',
    '{"commit":"%s\x01","workers":[]}',
    '{"commit":"%s","workers":[[1]]}\x0b',
]


def score_verdict(stdout: bytes, answers: dict = ANSWERS):
    result = SCORE.evaluate_run(stdout, TESTS, answers)
    return None if result["ok"] else (result["code"], result["line"])


def fill(template: str) -> str:
    return template.replace("%s", FIRST) if "%s" in template else template


class TestValidate(unittest.TestCase):
    def compare_line(self, line: str) -> None:
        """Compare check_line and check_output with the scorer for `line` as line 1."""
        ours = bv.check_line(line, COMMITS, N_TESTS, set())
        rest = [bv.encode_line(commit, []) for commit in sorted(COMMITS - {FIRST})]
        others = [bv.encode_line(FIRST, [])] if ours is not None else []
        stdout = ("\n".join([line] + rest + others) + "\n").encode("utf-8", "surrogatepass")
        expected = score_verdict(stdout)
        self.assertEqual(None if ours is None else (ours, 1), expected, repr(line[:80]))
        self.assertEqual(bv.check_output(stdout, COMMITS, N_TESTS), expected, repr(line[:80]))

    def test_good_lines(self):
        for template in GOOD:
            line = fill(template)
            self.assertIsNone(bv.check_line(line, COMMITS, N_TESTS, set()), line)
            self.compare_line(line)

    def test_bad_lines(self):
        for template in BAD:
            line = fill(template)
            self.assertIsNotNone(bv.check_line(line, COMMITS, N_TESTS, set()), line[:80])
            self.compare_line(line)

    def test_fuzzed_lines(self):
        rng = random.Random(9)
        alphabet = list('{}[]",:. -+0123456789eE') + ["true", "null", "NaN", FIRST, "commit", "workers"]
        seeds = [fill(t) for t in GOOD + BAD if len(t) < 200]
        for _ in range(3000):
            line = rng.choice(seeds)
            for _ in range(rng.randint(1, 3)):
                at = rng.randint(0, len(line))
                if rng.random() < 0.5 and line:
                    line = line[:at] + line[at + 1:]
                else:
                    line = line[:at] + rng.choice(alphabet) + line[at:]
            self.compare_line(line)

    def test_whole_output(self):
        good = [bv.encode_line(commit, [[i]]) for i, commit in enumerate(sorted(COMMITS))]
        cases = [
            b"",
            b"\n",
            ("\n".join(good) + "\n").encode(),
            "\n".join(good).encode(),
            ("\n".join(good) + "\n\n").encode(),
            ("\r\n".join(good) + "\r\n").encode(),
            ("\n".join(good[:-1]) + "\n").encode(),
            ("\n".join(good + good[:1]) + "\n").encode(),
            ("\n".join(good[:3] + [""] + good[3:]) + "\n").encode(),
            b"\xef\xbb\xbf" + ("\n".join(good) + "\n").encode(),
            ("\n".join(good) + "\n").encode() + b"\xff",
            ("\n".join(good) + "\n").encode().replace(b"workers", b"w\xc3\xb6rkers", 1),
            ("\n".join(reversed(good)) + "\n").encode(),
        ]
        for stdout in cases:
            self.assertEqual(bv.check_output(stdout, COMMITS, N_TESTS), score_verdict(stdout), stdout[:60])
        self.assertEqual(bv.check_output(b" " * (bv.MAX_OUTPUT_BYTES + 1), COMMITS, N_TESTS), ("too_large", None))

    def test_seen_and_repeat(self):
        seen: set[str] = set()
        line = bv.encode_line(FIRST, [[1, 2], [], [3]])
        self.assertIsNone(bv.check_line(line, COMMITS, N_TESTS, seen))
        self.assertEqual(seen, {FIRST})
        self.assertEqual(bv.check_line(line, COMMITS, N_TESTS, seen), "repeated_commit")
        self.assertEqual(bv.check_line(bv.encode_line(FIRST, [[1620]]), COMMITS, N_TESTS, set()), "unknown_test")

    def test_encode_line(self):
        self.assertEqual(bv.encode_line("ab12", [[3, 1], [], [2]]), '{"commit":"ab12","workers":[[3,1],[2]]}')
        self.assertEqual(bv.encode_line("ab12", []), '{"commit":"ab12","workers":[]}')
        self.assertEqual(bv.encode_line("ab12", [[], []]), '{"commit":"ab12","workers":[]}')
        for bad in ([[True]], [[1.0]], [["1"]]):
            with self.assertRaises(TypeError):
                bv.encode_line("ab12", bad)


if __name__ == "__main__":
    unittest.main()
