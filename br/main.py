"""Entry point: read the batch, decide every commit, print one JSON line each.

Order of work, so that a valid and safe answer always exists:
1. read tests.json and the patch list; the fallback for every commit is ALL;
2. static selection per commit (sound superset, see static.py);
3. (later stages refine the static answer; any failure keeps it);
4. pack, validate every line with the scorer's rules, print everything at once.
Logs go to stderr only. The exit code is always 0.
"""

from __future__ import annotations

import json
import os
import sys
import time


WROTE = False


def log(*parts):
    print("[br]", *parts, file=sys.stderr, flush=True)


def main() -> int:
    started = time.monotonic()
    data = os.environ.get("BR_DATA", "/data")
    root = os.environ.get("BR_ROOT", "/opt/sqlglot")
    here = os.path.dirname(os.path.abspath(__file__))
    maps_dir = os.environ.get("BR_MAPS", os.path.join(os.path.dirname(here), "maps"))

    with open(os.path.join(data, "tests.json"), "rb") as f:
        tests = json.load(f)["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    number_of = {t["id"]: t["number"] for t in tests}
    every = sorted(ms)
    commit_dir = os.path.join(data, "commits")
    commits = sorted(name[: -len(".patch")] for name in os.listdir(commit_dir) if name.endswith(".patch"))

    from . import pack, validate

    answers: dict[str, list[int]] = {cid: every for cid in commits}
    statics: dict = {}
    maps = None

    try:
        from .maps import Maps
        from .static import StaticSelector

        maps = Maps(maps_dir)
        bit_numbers = [number_of.get(test_id) for test_id in maps.tests]
        known = {n for n in bit_numbers if n is not None}
        unknown = [n for n in every if n not in known]  # no coverage: always run
        if unknown:
            log(f"{len(unknown)} tests have no map entry; they are added to every commit")
        selector = StaticSelector(maps, root)
    except Exception as exc:  # no maps: everything runs
        log("maps unavailable, running everything:", repr(exc))
        selector = None

    if selector is not None:
        for cid in commits:
            try:
                with open(os.path.join(commit_dir, cid + ".patch"), "rb") as f:
                    patch_bytes = f.read()
                res = selector.select(patch_bytes)
                if res.all:
                    chosen = every
                else:
                    chosen = set(unknown)
                    bits = res.bits
                    i = 0
                    while bits:
                        if bits & 1:
                            n = bit_numbers[i]
                            if n is not None:
                                chosen.add(n)
                        bits >>= 1
                        i += 1
                    chosen = sorted(chosen)
                answers[cid] = chosen
                statics[cid] = res
                log(cid, len(chosen), "tests;", "; ".join(res.reasons)[:300])
            except Exception as exc:
                log(cid, "error, running everything:", repr(exc))
                answers[cid] = every

    if selector is not None and os.environ.get("BR_VERIFY", "1") == "1":
        try:
            from . import policy

            policy.refine(
                answers,
                statics,
                tests,
                ms,
                maps,
                number_of,
                deadline=started + float(os.environ.get("BR_DEADLINE", "110")),
                log=log,
            )
        except Exception as exc:  # keep the static answers
            log("verification stage failed, keeping static answers:", repr(exc))

    lines = []
    seen: set = set()
    commit_set = set(commits)
    n_tests = len(tests)
    for cid in commits:
        chosen = answers[cid]
        try:
            _, workers = pack.pack([(n, ms[n]) for n in chosen])
            line = validate.encode_line(cid, workers)
            if validate.check_line(line, commit_set, n_tests, set(seen)) is not None:
                raise ValueError("line failed validation")
        except Exception as exc:
            log(cid, "packing failed, running everything:", repr(exc))
            _, workers = pack.pack([(n, ms[n]) for n in every])
            line = validate.encode_line(cid, workers)
        seen.add(cid)
        lines.append(line)
    out = ("\n".join(lines) + "\n") if lines else ""
    global WROTE
    WROTE = True
    sys.stdout.write(out)
    sys.stdout.flush()
    log(f"done: {len(commits)} commits in {time.monotonic() - started:.2f}s wall, cpu {time.process_time():.2f}s")
    return 0
