"""Label one patch: which numbered tests fail when it is applied to sqlglot.

Runs inside the br-lab image (pristine sqlglot at /opt/sqlglot, a git repo).

    python label_run.py --patch X.patch --tests tests.json [--out X.json]
                        [--id X] [--timeout 120] [--session-timeout 1500]
                        [--xdist 0] [--keep]

An empty --patch (or --patch "") labels the pristine tree.

Steps: copy /opt/sqlglot to a fresh temp dir, `git apply` the patch, then run
    SKIP_INTEGRATION=0 PYTHONPATH=<tree>:<plugin dir> python -m pytest tests
        --continue-on-collection-errors -p no:cacheprovider -q -p label_plugin
        [--timeout T] [-n N]
from the tree root. Output JSON:
    {"commit", "failing", "unknown_failing", "collection_errors", "wall",
     "status": "ok|apply_failed|crash|timeout", ...extra diagnostics}
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PRISTINE = Path("/opt/sqlglot")
HERE = Path(__file__).resolve().parent
PLUGIN = HERE / "label_plugin.py"
DEFAULT_TESTS = HERE / "data" / "tests.json"
LOG_TAIL = 6000


def load_tests(path: Path) -> tuple[dict[str, int], dict[str, list[int]], list[str]]:
    data = json.loads(Path(path).read_text("utf-8"))
    by_id = {t["id"]: t["number"] for t in data["tests"]}
    by_file: dict[str, list[int]] = {}
    for t in data["tests"]:
        by_file.setdefault(t["file"], []).append(t["number"])
    return by_id, by_file, list(data.get("skipped", []))


def expand_collection_error(nodeid: str, by_id: dict[str, int], by_file: dict[str, list[int]]) -> set[int]:
    """Numbers of the tests covered by a failed collector (module, package, class)."""
    nodeid = nodeid.strip("/")
    if nodeid in ("", ".", "tests"):
        return set(by_id.values())
    hit: set[int] = set()
    for file, numbers in by_file.items():
        if file == nodeid or file.startswith(nodeid + "/"):
            hit.update(numbers)
    for test_id, number in by_id.items():
        if test_id.startswith(nodeid + "::"):
            hit.add(number)
    return hit


def _tail(path: Path, limit: int = LOG_TAIL) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace")


def label(
    patch: str | None,
    tests_json: Path,
    commit: str,
    timeout: float = 120,
    session_timeout: float = 1500,
    xdist: int = 0,
    keep: bool = False,
    workroot: str | None = None,
    log_path: Path | None = None,
) -> dict:
    start = time.monotonic()
    by_id, by_file, skipped_ids = load_tests(tests_json)
    result: dict = {
        "commit": commit,
        "failing": [],
        "unknown_failing": [],
        "collection_errors": [],
        "wall": 0.0,
        "status": "ok",
    }
    tmp = Path(tempfile.mkdtemp(prefix="lab-", dir=workroot))
    try:
        tree = tmp / "sqlglot"
        plugdir = tmp / "plug"
        plugdir.mkdir()
        shutil.copy2(PLUGIN, plugdir / "label_plugin.py")
        subprocess.run(["cp", "-a", str(PRISTINE), str(tree)], check=True)

        patch_text = Path(patch).read_bytes() if patch else b""
        if patch_text.strip():
            patch_file = tmp / "change.patch"
            patch_file.write_bytes(patch_text)
            applied = subprocess.run(
                ["git", "apply", str(patch_file)],
                cwd=tree,
                capture_output=True,
                text=True,
            )
            if applied.returncode != 0:
                result["status"] = "apply_failed"
                result["apply_error"] = (applied.stderr or applied.stdout)[-2000:]
                result["wall"] = round(time.monotonic() - start, 3)
                return result

        events_path = tmp / "events.jsonl"
        env = dict(os.environ)
        env["SKIP_INTEGRATION"] = "0"
        env["PYTHONPATH"] = f"{tree}{os.pathsep}{plugdir}"
        env["LABEL_PLUGIN_OUT"] = str(events_path)
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTEST_PLUGINS", None)
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "--continue-on-collection-errors",
            "-p",
            "no:cacheprovider",
            "-q",
            "-p",
            "label_plugin",
        ]
        if timeout and timeout > 0:
            cmd += ["--timeout", str(timeout)]
        if xdist and xdist > 0:
            cmd += ["-n", str(xdist)]
        result["cmd"] = " ".join(cmd[1:])

        log_file = log_path or (tmp / "pytest.log")
        t0 = time.monotonic()
        with open(log_file, "wb") as log:
            proc = subprocess.Popen(cmd, cwd=tree, env=env, stdout=log, stderr=subprocess.STDOUT)
            timed_out = False
            try:
                returncode = proc.wait(timeout=session_timeout if session_timeout > 0 else None)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                returncode = proc.wait()
        result["pytest_wall"] = round(time.monotonic() - t0, 3)
        result["returncode"] = returncode

        failed_ids: set[str] = set()
        timeouts: set[str] = set()
        phases: dict[str, set[str]] = {}
        collection_errors: list[str] = []
        done = None
        if events_path.exists():
            for line in events_path.read_text("utf-8", "replace").splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev["ev"] == "fail":
                    failed_ids.add(ev["nodeid"])
                    phases.setdefault(ev["nodeid"], set()).add(
                        "subtest" if ev.get("subtest") else ev.get("when", "?")
                    )
                    if ev.get("timeout"):
                        timeouts.add(ev["nodeid"])
                elif ev["ev"] == "collect_error":
                    if ev["nodeid"] not in collection_errors:
                        collection_errors.append(ev["nodeid"])
                elif ev["ev"] == "done":
                    done = ev

        failing: set[int] = set()
        unknown: set[str] = set()
        for nodeid in failed_ids:
            if nodeid in by_id:
                failing.add(by_id[nodeid])
            else:
                unknown.add(nodeid)
        for nodeid in collection_errors:
            failing.update(expand_collection_error(nodeid, by_id, by_file))

        result["failing"] = sorted(failing)
        result["unknown_failing"] = sorted(unknown)
        result["collection_errors"] = collection_errors
        result["timeouts"] = sorted(timeouts)
        # tests that failed only through subtests (parent call report passed)
        result["subtest_only"] = sorted(n for n, ph in phases.items() if ph == {"subtest"})
        if done is not None:
            result["reported"] = done.get("reported")
            result["testscollected"] = done.get("testscollected")
            known_skips = set(skipped_ids)
            result["unexpected_skips"] = [s for s in done.get("skipped", []) if s not in known_skips]
        if timed_out:
            result["status"] = "timeout"
        elif done is None or returncode not in (0, 1):
            result["status"] = "crash"
        if result["status"] != "ok" or unknown:
            result["log_tail"] = _tail(log_file)
        return result
    finally:
        result["wall"] = round(time.monotonic() - start, 3)
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            result["workdir"] = str(tmp)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patch", default="", help="patch file; empty labels the pristine tree")
    ap.add_argument("--tests", type=Path, default=DEFAULT_TESTS)
    ap.add_argument("--out", type=Path, help="write JSON here (default stdout)")
    ap.add_argument("--id", help="commit id (default: patch file stem, or 'pristine')")
    ap.add_argument("--timeout", type=float, default=120, help="pytest-timeout seconds per test (0=off)")
    ap.add_argument("--session-timeout", type=float, default=1500, help="kill pytest after this many seconds")
    ap.add_argument("--xdist", type=int, default=0, help="pytest-xdist -n value (0 = no xdist)")
    ap.add_argument("--keep", action="store_true", help="keep the temp tree")
    ap.add_argument("--log", type=Path, help="write the pytest output here")
    args = ap.parse_args(argv)
    commit = args.id or (Path(args.patch).stem if args.patch else "pristine")
    res = label(
        args.patch or None,
        args.tests,
        commit,
        timeout=args.timeout,
        session_timeout=args.session_timeout,
        xdist=args.xdist,
        keep=args.keep,
        log_path=args.log,
    )
    text = json.dumps(res, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.out.with_suffix(args.out.suffix + ".tmp")
        tmp.write_text(text + "\n", "utf-8")
        os.replace(tmp, args.out)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
