"""Pack selected tests onto at most 8 workers at minimum commit cost.

Cost model (kit/SCORING.md): a test lasts round-half-up(seconds * 1000) ms; a
commit with no worker costs 0, otherwise its largest worker load plus 2000 ms
per non-empty worker. pack() tries every worker count k: longest-processing-
time-first, then a move/swap local search on the most loaded worker, and an
exact branch-and-bound when there are few tests. Fully deterministic.
"""

from __future__ import annotations

import bisect
import heapq
import math
from collections.abc import Iterable, Mapping, Sequence

__all__ = ["WORKER_COST_MS", "MAX_WORKERS", "EXACT_LIMIT", "duration_ms", "cost", "pack"]

WORKER_COST_MS = 2_000
MAX_WORKERS = 8
EXACT_LIMIT = 14
_EXACT_NODE_LIMIT = 300_000

Item = tuple[int, int]  # (milliseconds, test)


def duration_ms(seconds: float) -> int:
    """A test's duration in whole ms, rounded half up exactly like kit/score.py."""
    return math.floor(seconds * 1000 + 0.5)


def cost(workers: Iterable[Iterable[int]], ms: Mapping[int, int] | Sequence[int]) -> int:
    """The scorer's cost of one commit; ms maps test number to milliseconds."""
    occupied = [list(worker) for worker in workers]
    occupied = [worker for worker in occupied if worker]
    if not occupied:
        return 0
    return max(sum(ms[test] for test in worker) for worker in occupied) + WORKER_COST_MS * len(occupied)


def pack(
    durations: Mapping[int, int] | Iterable[tuple[int, int]], max_workers: int = MAX_WORKERS
) -> tuple[int, list[list[int]]]:
    """Return (cost_ms, workers) minimizing the commit cost; workers are non-empty lists."""
    items = _items(durations)
    if not items:
        return 0, []
    if not 1 <= max_workers:
        raise ValueError("max_workers must be at least 1")
    total = sum(ms for ms, _test in items)
    biggest = items[0][0]
    count = len(items)

    def lower_bound(k: int) -> int:
        return max(biggest, -(-total // k)) + WORKER_COST_MS * k

    best_cost: int | None = None
    best_bins: list[list[Item]] = []
    for k in sorted(range(1, min(max_workers, count) + 1), key=lambda k: (lower_bound(k), k)):
        if best_cost is not None and lower_bound(k) >= best_cost:
            break
        floor_makespan = lower_bound(k) - WORKER_COST_MS * k
        bins, loads = _lpt(items, k)
        if max(loads) > floor_makespan:
            _local_search(bins, loads)
        if count <= EXACT_LIMIT and max(loads) > floor_makespan:
            exact = _exact(items, k, max(loads), floor_makespan)
            if exact is not None:
                bins, loads = exact
        occupied = sum(1 for load_bin in bins if load_bin)
        candidate = max(loads) + WORKER_COST_MS * occupied
        if best_cost is None or candidate < best_cost:
            best_cost, best_bins = candidate, bins
    assert best_cost is not None
    occupied_bins = [load_bin for load_bin in best_bins if load_bin]
    occupied_bins.sort(key=lambda load_bin: (-sum(ms for ms, _test in load_bin), min(t for _ms, t in load_bin)))
    return best_cost, [sorted(test for _ms, test in load_bin) for load_bin in occupied_bins]


def _items(durations: Mapping[int, int] | Iterable[tuple[int, int]]) -> list[Item]:
    pairs = durations.items() if isinstance(durations, Mapping) else durations
    seen: set[int] = set()
    items: list[Item] = []
    for test, ms in pairs:
        if isinstance(ms, bool) or not isinstance(ms, int) or ms < 0:
            raise ValueError(f"duration of test {test!r} must be a non-negative int of ms")
        if test in seen:
            raise ValueError(f"test {test!r} listed twice")
        seen.add(test)
        items.append((ms, test))
    items.sort(key=lambda item: (-item[0], item[1]))
    return items


def _lpt(items: list[Item], k: int) -> tuple[list[list[Item]], list[int]]:
    """Longest first onto the least loaded worker (ties: lowest index)."""
    heap = [(0, j) for j in range(k)]
    bins: list[list[Item]] = [[] for _ in range(k)]
    for item in items:
        load, j = heapq.heappop(heap)
        bins[j].append(item)
        heapq.heappush(heap, (load + item[0], j))
    loads = [0] * k
    for load, j in heap:
        loads[j] = load
    for load_bin in bins:
        load_bin.sort()
    return bins, loads


def _local_search(bins: list[list[Item]], loads: list[int]) -> None:
    """Moves and swaps out of the most loaded worker while its pair max drops."""
    k = len(bins)
    if k < 2:
        return
    values = [[ms for ms, _test in load_bin] for load_bin in bins]
    while True:
        top = max(loads)
        hi = loads.index(top)
        source = values[hi]
        best: tuple | None = None  # (pair max, kind, other, index in hi, index in other)
        lo = min((j for j in range(k) if j != hi), key=lambda j: (loads[j], j))
        gap = top - loads[lo]
        if gap > 1:
            # move x with 0 < x < gap, as close to gap / 2 as possible
            at = bisect.bisect_left(source, gap // 2)
            for index in _around(source, at):
                x = source[index]
                if 0 < x < gap:
                    pair = max(top - x, loads[lo] + x)
                    key = (pair, 0, lo, bins[hi][index][1], -1)
                    if best is None or key < best[:5]:
                        best = key + (index, -1)
        distinct = sorted(set(source))
        for other in range(k):
            if other == hi:
                continue
            gap = top - loads[other]
            target_values = values[other]
            if gap <= 1 or not target_values:
                continue
            for a in distinct:
                if a == 0:
                    continue
                # b with 0 < a - b < gap, a - b close to gap / 2
                at = bisect.bisect_left(target_values, a - gap // 2)
                for index in _around(target_values, at):
                    b = target_values[index]
                    if 0 < a - b < gap:
                        pair = max(top - a + b, loads[other] + a - b)
                        a_index = bisect.bisect_left(source, a)
                        key = (pair, 1, other, bins[hi][a_index][1], bins[other][index][1])
                        if best is None or key < best[:5]:
                            best = key + (a_index, index)
        if best is None or best[0] >= top:
            return
        _pair, kind, other, _t1, _t2, hi_index, other_index = best
        item = bins[hi].pop(hi_index)
        source.pop(hi_index)
        loads[hi] -= item[0]
        if kind == 1:
            swapped = bins[other].pop(other_index)
            values[other].pop(other_index)
            loads[other] -= swapped[0]
            _insert(bins, values, loads, hi, swapped)
        _insert(bins, values, loads, other, item)


def _around(sorted_values: list[int], at: int) -> range:
    return range(max(0, at - 2), min(len(sorted_values), at + 2))


def _insert(bins: list[list[Item]], values: list[list[int]], loads: list[int], j: int, item: Item) -> None:
    index = bisect.bisect_left(bins[j], item)
    bins[j].insert(index, item)
    values[j].insert(index, item[0])
    loads[j] += item[0]


def _exact(
    items: list[Item], k: int, upper: int, floor_makespan: int
) -> tuple[list[list[Item]], list[int]] | None:
    """Branch and bound for a makespan below `upper`; None when none is found."""
    count = len(items)
    sizes = [ms for ms, _test in items]
    suffix = [0] * (count + 1)
    for i in range(count - 1, -1, -1):
        suffix[i] = suffix[i + 1] + sizes[i]
    loads = [0] * k
    assign = [0] * count
    best = {"makespan": upper, "assign": None}
    nodes = 0

    def search(i: int, current: int) -> bool:
        nonlocal nodes
        if i == count:
            best["makespan"], best["assign"] = current, assign[:]
            return current <= floor_makespan
        nodes += 1
        if nodes > _EXACT_NODE_LIMIT:
            return True
        cap = best["makespan"] - 1
        if sum(cap - load for load in loads if load < cap) < suffix[i]:
            return False
        size = sizes[i]
        tried: set[int] = set()
        for j in range(k):
            load = loads[j]
            if load in tried or load + size > cap:
                continue
            tried.add(load)
            loads[j] = load + size
            assign[i] = j
            stop = search(i + 1, max(current, load + size))
            loads[j] = load
            if stop:
                return True
            cap = best["makespan"] - 1
        return False

    search(0, 0)
    if best["assign"] is None:
        return None
    bins: list[list[Item]] = [[] for _ in range(k)]
    result_loads = [0] * k
    for item, j in zip(items, best["assign"]):
        bins[j].append(item)
        result_loads[j] += item[0]
    return bins, result_loads
