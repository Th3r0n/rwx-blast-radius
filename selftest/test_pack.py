"""Tests for br.pack (run: python -I selftest/test_pack.py)."""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import random
import statistics
import sys
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from br import pack as bk  # noqa: E402


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
MS = {test["number"]: bk.duration_ms(test["seconds"]) for test in TESTS}


def partitions(items: list):
    """All set partitions of items."""
    if not items:
        yield []
        return
    first, rest = items[0], items[1:]
    for partition in partitions(rest):
        for index in range(len(partition)):
            yield partition[:index] + [[first] + partition[index]] + partition[index + 1:]
        yield [[first]] + partition


def brute_force(durations: dict[int, int]) -> int:
    best = None
    for partition in partitions(sorted(durations)):
        if len(partition) <= bk.MAX_WORKERS:
            value = bk.cost(partition, durations)
            best = value if best is None else min(best, value)
    return best or 0


class TestPack(unittest.TestCase):
    def assert_valid(self, durations: dict[int, int], result: tuple[int, list[list[int]]]) -> None:
        value, workers = result
        self.assertLessEqual(len(workers), bk.MAX_WORKERS)
        self.assertTrue(all(workers))
        flat = [test for worker in workers for test in worker]
        self.assertEqual(sorted(flat), sorted(durations))
        self.assertEqual(value, bk.cost(workers, durations))

    def test_durations_match_scorer(self):
        for test in TESTS:
            self.assertEqual(bk.duration_ms(test["seconds"]), SCORE._duration_milliseconds(test["seconds"]))
        for seconds in (0.0005, 0.0015, 0.0025, 1.0005, 2.675, 0.1235):
            self.assertEqual(bk.duration_ms(seconds), SCORE._duration_milliseconds(seconds))

    def test_full_set(self):
        result = bk.pack(MS)
        self.assert_valid(MS, result)
        self.assertEqual(result[0], 36_817)
        timings = []
        for _ in range(25):
            start = time.perf_counter()
            bk.pack(MS)
            timings.append((time.perf_counter() - start) * 1000)
        print(f"\npack(all {len(MS)} tests): min {min(timings):.2f} ms, median "
              f"{statistics.median(timings):.2f} ms, max {max(timings):.2f} ms", file=sys.stderr)
        self.assertLess(statistics.median(timings), 30)

    def test_sample_commits_and_scorer(self):
        lines, total = [], 0
        for commit, answer in sorted(ANSWERS.items()):
            durations = {test: MS[test] for test in answer["failing"]}
            value, workers = bk.pack(durations)
            self.assert_valid(durations, (value, workers))
            total += value
            lines.append(json.dumps({"commit": commit, "workers": workers}))
        self.assertEqual(total, 94_351)
        evaluation = SCORE.evaluate_run(("\n".join(lines) + "\n").encode(), TESTS, ANSWERS)
        self.assertEqual(evaluation, {"ok": True, "simulated_milliseconds": total, "missed_commits": []})

    def test_cost_matches_scorer(self):
        rng = random.Random(7)
        numbers = sorted(MS)
        for _ in range(200):
            chosen = rng.sample(numbers, rng.randint(0, 60))
            workers = [[] for _ in range(rng.randint(1, 8))]
            for test in chosen:
                rng.choice(workers).append(test)
            commit = next(iter(ANSWERS))
            answers = {commit: {"failing": [], "kind": "code"}}
            stdout = (json.dumps({"commit": commit, "workers": workers}) + "\n").encode()
            evaluation = SCORE.evaluate_run(stdout, TESTS, answers)
            self.assertEqual(evaluation["simulated_milliseconds"], bk.cost(workers, MS))

    def test_exact_small_sets(self):
        rng = random.Random(11)
        for trial in range(300):
            count = rng.randint(1, 9)
            pool = rng.choice([[1, 2, 3, 5, 8], list(range(0, 5000, 7)), list(range(1000, 30000, 997)), [0, 0, 1]])
            durations = {rng.randrange(10_000) * 10 + i: rng.choice(pool) for i in range(count)}
            result = bk.pack(durations)
            self.assert_valid(durations, result)
            self.assertEqual(result[0], brute_force(durations), durations)

    def test_larger_sets(self):
        rng = random.Random(3)
        numbers = sorted(MS)
        for count in (15, 20, 40, 100, 400, 1000):
            for _ in range(5):
                durations = {test: MS[test] for test in rng.sample(numbers, count)}
                if rng.random() < 0.5:
                    durations = {test: rng.randint(0, 20_000) for test in durations}
                result = bk.pack(durations)
                self.assert_valid(durations, result)
                total, biggest = sum(durations.values()), max(durations.values())
                bound = min(max(biggest, -(-total // k)) + 2000 * k for k in range(1, 9))
                self.assertGreaterEqual(result[0], bound)

    def test_determinism_and_input_forms(self):
        rng = random.Random(5)
        durations = {test: MS[test] for test in rng.sample(sorted(MS), 300)}
        expected = bk.pack(durations)
        pairs = list(durations.items())
        for _ in range(5):
            rng.shuffle(pairs)
            self.assertEqual(bk.pack(pairs), expected)
            self.assertEqual(bk.pack(dict(pairs)), expected)

    def test_edges(self):
        self.assertEqual(bk.pack({}), (0, []))
        self.assertEqual(bk.pack([]), (0, []))
        self.assertEqual(bk.pack({5: 0}), (2000, [[5]]))
        self.assertEqual(bk.pack({1: 0, 2: 0, 3: 0}), (2000, [[1, 2, 3]]))
        self.assertEqual(bk.pack({1: 30000, 2: 30000}), (34000, [[1], [2]]))
        self.assertEqual(bk.pack({1: 100, 2: 100}), (2200, [[1, 2]]))
        self.assertEqual(bk.cost([], MS), 0)
        self.assertEqual(bk.cost([[], []], MS), 0)
        with self.assertRaises(ValueError):
            bk.pack([(1, 5), (1, 6)])
        with self.assertRaises(ValueError):
            bk.pack({1: 2.5})
        with self.assertRaises(ValueError):
            bk.pack({1: True})


if __name__ == "__main__":
    unittest.main()
