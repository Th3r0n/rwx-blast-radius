"""Decide which candidates to prove passing by running them (verify-or-select).

Verifying costs CPU, which is score; selecting costs simulated seconds. A job
is worth running when its estimated CPU is clearly below what it saves if the
tests pass: the cost of the commit with them minus the cost without them
(packing slack included, so tests that fit beside a long selected test are
never worth verifying). Estimates are deterministic (calibrated costs, a fixed
machine factor), so the three scored runs take the same decisions.

A test is dropped only with a "pass" or "skip" verdict from a child that
proved the patch active. Everything else stays selected.
"""

from __future__ import annotations

import os
import time

from . import pack, verify

HEAVY_MS = 1000
SAFETY = 0.7  # verify only when CPU estimate <= SAFETY * gain


def _cost(numbers, ms):
    if not numbers:
        return 0
    return pack.pack([(n, ms[n]) for n in numbers])[0]


def _hot_eligible(res, maps):
    if res.all:
        return False
    for path, change in res.changes:
        loaded = path in maps.preload_mods
        if loaded and (change.import_time or change.kind != "function"):
            return False  # needs a clean import of the patched tree
        if loaded and change.key in maps.preload.get(path, ()):
            return False  # ran while the warm state was built
    return True


def refine(answers, statics, tests, ms, maps, number_of, deadline, log):
    factor = float(os.environ.get("BR_CPU_FACTOR", "2.5"))
    rows = {t["number"]: t for t in tests}
    bit_numbers = [number_of.get(test_id) for test_id in maps.tests]
    est = {}
    cpu_list = getattr(maps, "cpu", None)
    for i, n in enumerate(bit_numbers):
        if n is not None and cpu_list:
            est[n] = cpu_list[i]
    never = set()
    bits = maps.subprocs
    i = 0
    while bits:
        if bits & 1 and bit_numbers[i] is not None:
            never.add(bit_numbers[i])
        bits >>= 1
        i += 1
    for t in tests:
        if t["file"] == "tests/test_executor.py" or ms[t["number"]] >= HEAVY_MS:
            never.add(t["number"])

    plans = []
    for cid, res in statics.items():
        chosen = answers[cid]
        if not chosen or res.all or not _hot_eligible(res, maps):
            continue
        light = [n for n in chosen if n not in never]
        if not light:
            continue
        keep = [n for n in chosen if n in never]
        gain_ms = _cost(chosen, ms) - _cost(keep, ms)
        cpu = sum(est.get(n, ms[n] / 1000.0) for n in light) * factor
        if cpu * 1000 > SAFETY * gain_ms:
            log(cid, f"not verified: est {cpu:.2f}s cpu vs gain {gain_ms / 1000:.2f}s")
            continue
        plans.append((gain_ms - cpu * 1000, cid, light, keep, cpu))
    if not plans:
        return
    plans.sort(key=lambda p: (-p[0], p[1]))
    warm = verify.Warm(tests)
    for _, cid, light, keep, cpu in plans:
        if time.monotonic() > deadline:
            log("deadline reached; remaining commits keep their static answer")
            break
        res = statics[cid]
        job = verify.Job(
            cid,
            [rows[n] for n in sorted(light)],
            dict(res.py_files),
            {path: new for path, _old, new in res.data_changes},
            cpu_limit=max(5.0, cpu / factor * 4 + 3),
            wall_limit=max(10.0, cpu / factor * 6 + 5),
        )
        t0 = time.monotonic()
        verdicts, info = verify.run_job(warm, job)
        failing = []
        unverified = []
        for n in light:
            outcome = verdicts.get(rows[n]["id"])
            if outcome in ("pass", "skip"):
                continue
            (failing if outcome == "fail" else unverified).append(n)
        answers[cid] = sorted(set(keep) | set(failing) | set(unverified))
        log(
            cid,
            f"verified {len(light)}: {len(failing)} fail, {len(unverified)} unverified, kept {len(keep)};"
            f" child cpu {info.get('cpu', 0):.2f}s wall {time.monotonic() - t0:.2f}s"
            f" proof={info.get('proof')} err={info.get('error')}",
        )
