"""Build a selector image and score it like the board does (host side, stdlib).

    python -I lab/eval.py [--context .] [--dockerfile Dockerfile] [--dataset kit/sample]
                          [--answers DATASET/answers.json] [--runs 3] [--tag br-eval]
                          [--no-build] [--cpuset 0-3] [--misses-only] [--out lab/out/eval/<stamp>]

Steps:
  1. docker build --platform linux/amd64 -t TAG CONTEXT (unless --no-build)
  2. RUN_FLAGS are parsed from kit/Makefile (so they stay in sync), with
     $(SAMPLE) replaced by the dataset dir; nothing is dropped.
  3. The entrypoint is replaced by
        sh -c '<image ENTRYPOINT+CMD>; rc=$?; cat /sys/fs/cgroup/cpu.stat >&2; exit $rc'
     and usage_usec is parsed from stderr: the CPU time of every process in
     the container, user + system. The selector's stderr goes to runN.stderr.
  4. Each run's stdout is scored with kit/score.py (simulated seconds, misses).
Prints per run simulated / misses / CPU / total / wall, the median total
rounded like SCORING.md, whether all stdouts are byte-identical, and a
per-commit table against the dataset answers (cost, selection size, oracle
cost = only the failing tests on the best worker count, missed tests).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

LAB = Path(__file__).resolve().parent
REPO = LAB.parent
KIT = REPO / "kit"


def docker_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def docker_env() -> dict:
    env = dict(os.environ)
    env["MSYS_NO_PATHCONV"] = "1"
    return env


def load_score_module():
    spec = importlib.util.spec_from_file_location("kit_score", KIT / "score.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_run_flags(makefile: Path, dataset: Path) -> list[str]:
    text = makefile.read_text("utf-8").replace("\\\n", " ")
    m = re.search(r"^RUN_FLAGS\s*:?=\s*(.*)$", text, re.M)
    if not m:
        raise SystemExit("RUN_FLAGS not found in " + str(makefile))
    flags = m.group(1)
    flags = flags.replace("$(SAMPLE)", docker_path(dataset)).replace("$(CURDIR)", docker_path(KIT))
    if "$(" in flags:
        raise SystemExit("unresolved make variable in RUN_FLAGS: " + flags)
    return shlex.split(flags)


def image_command(tag: str) -> list[str]:
    out = subprocess.run(
        ["docker", "image", "inspect", tag, "--format", "{{json .Config}}"],
        check=True, capture_output=True, text=True, env=docker_env(),
    ).stdout
    cfg = json.loads(out)
    return list(cfg.get("Entrypoint") or []) + list(cfg.get("Cmd") or [])


def oracle_ms(failing: list[int], dur: dict[int, int], score) -> int:
    if not failing:
        return 0
    loads_sorted = sorted((dur[n] for n in failing), reverse=True)
    best = None
    for k in range(1, min(score.MAXIMUM_WORKERS, len(failing)) + 1):
        loads = [0] * k
        for d in loads_sorted:
            i = loads.index(min(loads))
            loads[i] += d
        cost = max(loads) + score.WORKER_COST_MILLISECONDS * k
        best = cost if best is None else min(best, cost)
    return best


def per_commit(stdout: bytes, tests: list[dict], answers: dict, score) -> dict[str, dict]:
    dur = {t["number"]: score._duration_milliseconds(t["seconds"]) for t in tests}
    rows = {}
    for line in stdout.decode("utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        workers = [w for w in obj["workers"] if w]
        sel = {n for w in workers for n in w}
        cost = (max(sum(dur[n] for n in w) for w in workers) + score.WORKER_COST_MILLISECONDS * len(workers)) if workers else 0
        fail = answers.get(obj["commit"], {}).get("failing", [])
        rows[obj["commit"]] = {
            "cost": cost,
            "size": len(sel),
            "workers": len(workers),
            "missed": sorted(set(fail) - sel),
            "oracle": oracle_ms(fail, dur, score),
            "nfail": len(fail),
            "kind": answers.get(obj["commit"], {}).get("kind", ""),
        }
    return rows


def fmt_ms(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def board_round(us: int) -> float:
    return ((us + 50000) // 100000) / 10


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", type=Path, default=REPO, help="docker build context (has the Dockerfile)")
    ap.add_argument("--dockerfile", default=None, help="Dockerfile path relative to the context")
    ap.add_argument("--dataset", type=Path, default=KIT / "sample", help="dir with commits/ and tests.json")
    ap.add_argument("--answers", type=Path, help="default: DATASET/answers.json")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--tag", default="br-eval")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--cpuset", help="optional extra --cpuset-cpus (not part of RUN_FLAGS)")
    ap.add_argument("--makefile", type=Path, default=KIT / "Makefile")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--misses-only", action="store_true", help="per-commit table: only commits with misses")
    args = ap.parse_args(argv)

    score = load_score_module()
    dataset = args.dataset.resolve()
    answers_path = args.answers or dataset / "answers.json"
    tests = json.loads((dataset / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads(answers_path.read_text("utf-8"))["commits"]
    out_dir = args.out or LAB / "out" / "eval" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.no_build:
        cmd = ["docker", "build", "--platform", "linux/amd64", "--tag", args.tag]
        if args.dockerfile:
            cmd += ["--file", str(args.context / args.dockerfile)]
        cmd.append(str(args.context))
        t0 = time.monotonic()
        b = subprocess.run(cmd, capture_output=True, text=True, env=docker_env())
        (out_dir / "build.log").write_text(b.stdout + b.stderr, "utf-8")
        if b.returncode:
            print(b.stdout[-3000:], b.stderr[-3000:])
            print("build failed, log in", out_dir / "build.log")
            return 1
        print(f"built {args.tag} in {time.monotonic() - t0:.1f}s")

    flags = parse_run_flags(args.makefile, dataset)
    if args.cpuset:
        flags = ["--cpuset-cpus", args.cpuset] + flags
    inner = shlex.join(image_command(args.tag))
    script = f"{inner}; rc=$?; cat /sys/fs/cgroup/cpu.stat >&2; exit $rc"
    (out_dir / "run_cmd.txt").write_text(
        shlex.join(["docker", "run", *flags, "--entrypoint", "sh", args.tag, "-c", script]) + "\n", "utf-8"
    )

    results = []
    outputs = []
    for i in range(1, args.runs + 1):
        cmd = ["docker", "run", *flags, "--entrypoint", "sh", args.tag, "-c", script]
        t0 = time.monotonic()
        r = subprocess.run(cmd, capture_output=True, env=docker_env())
        wall = time.monotonic() - t0
        (out_dir / f"run{i}.stdout").write_bytes(r.stdout)
        (out_dir / f"run{i}.stderr").write_bytes(r.stderr)
        err = r.stderr.decode("utf-8", "replace")
        usage = re.findall(r"^usage_usec (\d+)$", err, re.M)
        cpu_us = int(usage[-1]) if usage else None
        ev = score.evaluate_run(r.stdout, tests, answers)
        res = {"run": i, "rc": r.returncode, "wall": wall, "cpu_us": cpu_us, "eval": ev}
        if ev["ok"] and cpu_us is not None:
            res["total_us"] = score.run_total_microseconds(ev["simulated_milliseconds"], cpu_us)
        results.append(res)
        outputs.append(r.stdout)
        if ev["ok"]:
            sim = ev["simulated_milliseconds"]
            print(
                f"run {i}: simulated {fmt_ms(sim)} s  misses {len(ev['missed_commits'])}  "
                f"CPU {cpu_us / 1e6 if cpu_us is not None else float('nan'):.3f} s  "
                f"total {res.get('total_us', 0) / 1e6:.3f} s  wall {wall:.2f} s  rc {r.returncode}"
            )
        else:
            print(f"run {i}: REJECTED {ev['code']} line {ev.get('line')}  rc {r.returncode}  wall {wall:.2f}s")

    totals = [r["total_us"] for r in results if "total_us" in r]
    summary = {"runs": results, "identical_outputs": len(set(outputs)) == 1}
    if len(totals) == len(results) and totals:
        med = statistics.median_low(totals)  # the middle of 3 runs
        summary["median_total_us"] = med
        summary["board_score"] = board_round(med)
        print(f"median total: {board_round(med):.1f} s (board rounding)")
    else:
        print("not all runs valid: no board score")
    print(f"stdout byte-identical across runs: {summary['identical_outputs']}")

    first_ok = next((o for o, r in zip(outputs, results) if r["eval"]["ok"]), None)
    if first_ok is not None:
        rows = per_commit(first_ok, tests, answers, score)
        summary["per_commit"] = rows
        print(f"\n{'commit':<10}{'kind':<9}{'cost':>9}{'oracle':>9}{'sel':>6}{'wk':>4}{'fail':>5}  missed")
        tot_cost = tot_oracle = 0
        for cid in sorted(rows):
            row = rows[cid]
            tot_cost += row["cost"]
            tot_oracle += row["oracle"]
            if args.misses_only and not row["missed"]:
                continue
            missed = " ".join(map(str, row["missed"][:12])) + (" ..." if len(row["missed"]) > 12 else "")
            print(
                f"{cid:<10}{row['kind']:<9}{fmt_ms(row['cost']):>9}{fmt_ms(row['oracle']):>9}"
                f"{row['size']:>6}{row['workers']:>4}{row['nfail']:>5}  {missed}"
            )
        print(f"{'sum':<19}{fmt_ms(tot_cost):>9}{fmt_ms(tot_oracle):>9}")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1) + "\n", "utf-8")
    print(f"\nlogs and outputs in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
