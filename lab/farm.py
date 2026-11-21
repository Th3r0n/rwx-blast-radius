"""Labeling farm orchestrator (host side, stdlib only).

    python -I lab/farm.py start  [--workers 4] [--cpus 12-19] [--memory 3500m]
                                 [--slots 1] [--timeout 120] [--session-timeout 1500]
                                 [--xdist 0] [--rescan 30] [--image br-lab]
    python -I lab/farm.py status [--window 3600]
    python -I lab/farm.py stop
    python -I lab/farm.py restart [start options]   (stop + start; picks up code changes)
    python -I lab/farm.py requeue --set S [--status crash,error,timeout]

The farm is N detached containers `br-farm-<i>` running lab/farm_worker.py.
The CPU range is split evenly between them (--cpuset-cpus). Worker i owns a
static partition of every set, so they never coordinate. The lab directory is
mounted read-only at /lab and lab/corpus read-write at /lab/corpus. Drop new
patches into lab/corpus/<set>/patches/ at any time; list set names in
lab/corpus/priority.txt to order them.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

LAB = Path(__file__).resolve().parent
CORPUS = LAB / "corpus"
FARM_DIR = CORPUS / "_farm"
PREFIX = "br-farm-"


def docker_path(path: Path) -> str:
    """C:\\x\\y -> C:/x/y, which Docker Desktop accepts for -v."""
    return str(path.resolve()).replace("\\", "/")


def docker(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["MSYS_NO_PATHCONV"] = "1"
    return subprocess.run(
        ["docker", *args], check=check, text=True, capture_output=capture, env=env
    )


def parse_cpus(spec: str) -> list[int]:
    cpus: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-")
            cpus.extend(range(int(lo), int(hi) + 1))
        elif part:
            cpus.append(int(part))
    return cpus


def farm_containers() -> list[str]:
    out = docker("ps", "-a", "--filter", f"name=^{PREFIX}", "--format", "{{.Names}}\t{{.Status}}").stdout
    return [line for line in out.splitlines() if line.strip()]


def cmd_start(args) -> int:
    existing = farm_containers()
    if existing:
        print("farm containers already exist (use stop or restart):", *existing, sep="\n  ")
        return 1
    cpus = parse_cpus(args.cpus)
    n = args.workers
    if len(cpus) < n:
        print(f"need at least {n} CPUs, got {cpus}")
        return 1
    per = len(cpus) // n
    CORPUS.mkdir(exist_ok=True)
    FARM_DIR.mkdir(exist_ok=True)
    stop_file = FARM_DIR / "STOP"
    if stop_file.exists():
        stop_file.unlink()
    for i in range(n):
        mine = cpus[i * per : (i + 1) * per]
        name = f"{PREFIX}{i}"
        cmd = [
            "run", "-d", "--name", name,
            "--restart", "unless-stopped",
            "--cpuset-cpus", ",".join(map(str, mine)),
            "--memory", args.memory, "--memory-swap", args.memory,
            "--label", "br-farm=1",
            "-v", f"{docker_path(LAB)}:/lab:ro",
            "-v", f"{docker_path(CORPUS)}:/lab/corpus",
            args.image,
            "python", "/lab/farm_worker.py",
            "--index", str(i), "--count", str(n),
            "--timeout", str(args.timeout),
            "--session-timeout", str(args.session_timeout),
            "--xdist", str(args.xdist),
            "--rescan", str(args.rescan),
            "--slots", str(args.slots),
        ]
        cid = docker(*cmd).stdout.strip()
        print(f"{name} cpus={','.join(map(str, mine))} {cid[:12]}")
    return 0


def cmd_stop(args) -> int:
    FARM_DIR.mkdir(parents=True, exist_ok=True)
    names = [line.split("\t")[0] for line in farm_containers()]
    if not names:
        print("no farm containers")
        return 0
    docker("stop", "-t", "15", *names, check=False)
    docker("rm", *names, check=False)
    print("stopped and removed:", " ".join(names))
    return 0


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def cmd_status(args) -> int:
    now = time.time()
    print("containers:")
    for line in farm_containers() or ["  (none)"]:
        print("  " + line)
    order = []
    prio = CORPUS / "priority.txt"
    if prio.exists():
        order = [l.split("#")[0].strip() for l in prio.read_text("utf-8").splitlines()]
    sets = sorted(
        (p for p in CORPUS.iterdir() if p.is_dir() and (p / "patches").is_dir()),
        key=lambda p: (order.index(p.name) if p.name in order else len(order), p.name),
    ) if CORPUS.exists() else []
    print(f"\n{'set':<14}{'patches':>8}{'done':>6}{'pend':>6}  statuses / fail stats")
    total_pending = 0
    walls: list[float] = []
    for s in sets:
        ids = {p.name[:-6] for p in (s / "patches").glob("*.patch")}
        labels = {p.name[:-5]: p for p in (s / "labels").glob("*.json")} if (s / "labels").is_dir() else {}
        done = ids & set(labels)
        statuses: Counter = Counter()
        nonzero = 0
        for cid in done:
            lab = read_json(labels[cid]) or {}
            statuses[lab.get("status", "?")] += 1
            if lab.get("failing") or lab.get("unknown_failing"):
                nonzero += 1
        pending = len(ids - done)
        total_pending += pending
        paused = " PAUSED" if (s / "PAUSED").exists() else ""
        st = " ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
        print(f"{s.name:<14}{len(ids):>8}{len(done):>6}{pending:>6}  {st} breaking={nonzero}{paused}")

    print("\nworkers:")
    recent = 0
    live_since = now
    beats = [read_json(p) or {} for p in FARM_DIR.glob("worker-*.json")] if FARM_DIR.exists() else []
    for beat in sorted(beats, key=lambda b: b.get("index", -1)):
        idx = beat.get("index")
        if beat.get("state") == "stopped" and now - beat.get("now", 0) > 600:
            continue  # heartbeat of an older farm layout
        log_path = FARM_DIR / f"worker-{idx}.log"
        n_recent = 0
        if log_path.exists():
            for line in log_path.read_text("utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if now - rec["t"] <= args.window:
                    n_recent += 1
                    walls.append(rec["wall"])
                    live_since = min(live_since, rec["t"] - rec["wall"])
        recent += n_recent
        cur = beat.get("current")
        since = beat.get("current_since")
        busy = f" for {now - since:.0f}s" if cur and since else ""
        age = now - beat.get("now", 0)
        print(
            f"  worker {idx}: {beat.get('state')} {cur or ''}{busy} done={beat.get('done')} "
            f"owned_pending={beat.get('pending_owned', '-')} last_beat={age:.0f}s ago "
            f"last{args.window // 60}min={n_recent}"
        )
    if walls:
        walls.sort()
        med = walls[len(walls) // 2]
        span = max(60.0, now - max(now - args.window, live_since))
        rate = recent / span * 3600
        eta = total_pending / rate if rate else float("inf")
        print(
            f"\nthroughput (last {span / 60:.0f} min): {recent} labels = {rate:.1f}/h, "
            f"median wall {med:.0f}s; pending {total_pending}, ETA {eta:.1f} h"
        )
    return 0


def cmd_requeue(args) -> int:
    wanted = set(args.status.split(","))
    labels = CORPUS / args.set / "labels"
    removed = 0
    for p in labels.glob("*.json"):
        lab = read_json(p) or {}
        if lab.get("status") in wanted:
            p.unlink()
            removed += 1
    print(f"requeued {removed} labels of {args.set} with status in {sorted(wanted)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_start_opts(p):
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--cpus", default="12-19", help="host CPUs split evenly between workers")
        p.add_argument("--memory", default="3500m")
        p.add_argument("--timeout", type=float, default=120, help="per-test pytest-timeout")
        p.add_argument("--session-timeout", type=float, default=1500)
        p.add_argument("--xdist", type=int, default=0)
        p.add_argument("--rescan", type=float, default=30)
        p.add_argument("--slots", type=int, default=1,
                       help="labelers per container (heavy tests serialized by a flock)")
        p.add_argument("--image", default="br-lab")

    add_start_opts(sub.add_parser("start"))
    add_start_opts(sub.add_parser("restart"))
    p = sub.add_parser("status")
    p.add_argument("--window", type=int, default=3600)
    sub.add_parser("stop")
    p = sub.add_parser("requeue")
    p.add_argument("--set", required=True)
    p.add_argument("--status", default="crash,error,timeout")
    args = ap.parse_args(argv)
    if args.cmd == "start":
        return cmd_start(args)
    if args.cmd == "restart":
        cmd_stop(args)
        return cmd_start(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "stop":
        return cmd_stop(args)
    if args.cmd == "requeue":
        return cmd_requeue(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
