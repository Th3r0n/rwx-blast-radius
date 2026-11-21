"""In-container worker loop of the labeling farm (see farm.py).

    python /lab/farm_worker.py --index I --count N [--slots S] [--corpus /lab/corpus]
        [--timeout 120] [--session-timeout 1500] [--xdist 0] [--rescan 30]

With --slots S > 1 the container runs S labelers side by side: slot j becomes
worker I*S+j of N*S (its own partition, heartbeat and log), and the
memory-heavy tests are serialized across slots through a flock
($LABEL_HEAVY_LOCK, see label_plugin.py) so peaks never stack.

Worker I statically owns, inside every set, the patches whose position in the
sorted id list satisfies (position + crc32(set)) % N == I, so no locking is
needed. After every label it rescans the corpus, so sets and patches dropped
in later are picked up without a restart. Sets are served in the order of
<corpus>/priority.txt (one set name per line); unlisted sets come after it,
alphabetically. A set directory containing a file named PAUSED is skipped.

Labels go to <corpus>/<set>/labels/<id>.json (atomic replace). Heartbeats go
to <corpus>/_farm/worker-<I>.json and one line per finished label is appended
to <corpus>/_farm/worker-<I>.log. The worker exits when <corpus>/_farm/STOP
exists or on SIGTERM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import time
import traceback
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import label_run  # noqa: E402

STOPPING = False


def _on_term(signum, frame):  # pragma: no cover - signal path
    global STOPPING
    STOPPING = True
    raise KeyboardInterrupt


def set_order(corpus: Path) -> list[str]:
    sets = sorted(
        p.name
        for p in corpus.iterdir()
        if p.is_dir() and not p.name.startswith(("_", ".")) and (p / "patches").is_dir()
    )
    prio_file = corpus / "priority.txt"
    prio: list[str] = []
    if prio_file.exists():
        for line in prio_file.read_text("utf-8").splitlines():
            name = line.split("#", 1)[0].strip()
            if name and name in sets and name not in prio:
                prio.append(name)
    return prio + [s for s in sets if s not in prio]


def owned_pending(corpus: Path, index: int, count: int) -> list[tuple[str, str]]:
    todo: list[tuple[str, str]] = []
    for name in set_order(corpus):
        set_dir = corpus / name
        if (set_dir / "PAUSED").exists():
            continue
        ids = sorted(p.name[: -len(".patch")] for p in (set_dir / "patches").glob("*.patch"))
        offset = zlib.crc32(name.encode())
        labels = set_dir / "labels"
        done = {p.name[: -len(".json")] for p in labels.glob("*.json")} if labels.is_dir() else set()
        for pos, cid in enumerate(ids):
            if (pos + offset) % count == index and cid not in done:
                todo.append((name, cid))
    return todo


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, sort_keys=True) + "\n", "utf-8")
    os.replace(tmp, path)


def run_slots(args) -> int:
    import subprocess

    env = dict(os.environ)
    env.setdefault("LABEL_HEAVY_LOCK", "/tmp/label-heavy.lock")
    children = []
    for j in range(args.slots):
        cmd = [
            sys.executable, str(Path(__file__).resolve()),
            "--index", str(args.index * args.slots + j),
            "--count", str(args.count * args.slots),
            "--corpus", str(args.corpus), "--tests", str(args.tests),
            "--timeout", str(args.timeout), "--session-timeout", str(args.session_timeout),
            "--xdist", str(args.xdist), "--rescan", str(args.rescan),
        ]
        children.append(subprocess.Popen(cmd, env=env))

    def forward(signum, frame):
        for c in children:
            if c.poll() is None:
                c.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, forward)
    rc = 0
    for c in children:
        while True:
            try:
                rc = max(rc, c.wait())
                break
            except KeyboardInterrupt:
                forward(None, None)
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, required=True)
    ap.add_argument("--count", type=int, required=True)
    ap.add_argument("--corpus", type=Path, default=Path("/lab/corpus"))
    ap.add_argument("--tests", type=Path, default=label_run.DEFAULT_TESTS)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--session-timeout", type=float, default=1500)
    ap.add_argument("--xdist", type=int, default=0)
    ap.add_argument("--rescan", type=float, default=30)
    ap.add_argument("--slots", type=int, default=1)
    args = ap.parse_args(argv)
    if args.slots > 1:
        return run_slots(args)

    signal.signal(signal.SIGTERM, _on_term)
    farm_dir = args.corpus / "_farm"
    farm_dir.mkdir(parents=True, exist_ok=True)
    beat_path = farm_dir / f"worker-{args.index}.json"
    log_path = farm_dir / f"worker-{args.index}.log"
    started = time.time()
    done_count = 0
    beat = {
        "index": args.index,
        "count": args.count,
        "started": started,
        "host": os.uname().nodename,
        "config": {
            "timeout": args.timeout,
            "session_timeout": args.session_timeout,
            "xdist": args.xdist,
        },
    }

    try:
        while not STOPPING:
            if (farm_dir / "STOP").exists():
                break
            todo = owned_pending(args.corpus, args.index, args.count)
            if not todo:
                beat.update(state="idle", current=None, now=time.time(), done=done_count)
                write_json(beat_path, beat)
                time.sleep(args.rescan)
                continue
            set_name, cid = todo[0]
            patch = args.corpus / set_name / "patches" / f"{cid}.patch"
            beat.update(
                state="busy",
                current=f"{set_name}/{cid}",
                current_since=time.time(),
                now=time.time(),
                done=done_count,
                pending_owned=len(todo),
            )
            write_json(beat_path, beat)
            t0 = time.time()
            try:
                patch_bytes = patch.read_bytes()
                res = label_run.label(
                    str(patch),
                    args.tests,
                    cid,
                    timeout=args.timeout,
                    session_timeout=args.session_timeout,
                    xdist=args.xdist,
                )
                res["patch_sha256"] = hashlib.sha256(patch_bytes).hexdigest()
            except KeyboardInterrupt:
                break
            except FileNotFoundError:
                continue  # patch vanished between scan and read
            except Exception:
                res = {
                    "commit": cid,
                    "failing": [],
                    "unknown_failing": [],
                    "collection_errors": [],
                    "wall": round(time.time() - t0, 3),
                    "status": "error",
                    "error": traceback.format_exc()[-4000:],
                }
            res["set"] = set_name
            res["worker"] = args.index
            res["finished_at"] = time.time()
            res["label_config"] = beat["config"]
            write_json(args.corpus / set_name / "labels" / f"{cid}.json", res)
            done_count += 1
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(
                    json.dumps(
                        {
                            "t": round(res["finished_at"], 1),
                            "set": set_name,
                            "id": cid,
                            "status": res["status"],
                            "nfail": len(res["failing"]),
                            "wall": res["wall"],
                        }
                    )
                    + "\n"
                )
    except KeyboardInterrupt:
        pass
    beat.update(state="stopped", current=None, now=time.time(), done=done_count)
    write_json(beat_path, beat)
    return 0


if __name__ == "__main__":
    sys.exit(main())
