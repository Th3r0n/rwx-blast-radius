"""Turn a labeled corpus set into a kit-format dataset dir (host side, stdlib).

    python -I lab/corpus_to_kit.py SET [SET ...] [--out lab/out/kit-SET]
        [--tests lab/data/tests.json] [--status ok] [--compare kit/sample/answers.json]

Output dir: tests.json (copy), commits/<id>.patch, answers.json
{"commits": {id: {"failing": [...], "kind": kind}}}. Only labels whose status
is in --status (default: ok) are exported; the rest are listed on stderr.
The kind comes from the lab-only meta sidecar (or, for the sample set, from
--compare answers). The dataset is what eval.py and kit/score.py consume;
only commits/ and tests.json are ever mounted into a selector.

--compare ANSWERS checks the labels against a reference answers.json and
prints every mismatch (exit 1 if any).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent
CORPUS = LAB / "corpus"


def load_labels(set_name: str) -> dict[str, dict]:
    out = {}
    for p in sorted((CORPUS / set_name / "labels").glob("*.json")):
        out[p.stem] = json.loads(p.read_text("utf-8"))
    return out


def compare(labels: dict[str, dict], answers_path: Path) -> int:
    ref = json.loads(answers_path.read_text("utf-8"))["commits"]
    bad = 0
    missing = [cid for cid in ref if cid not in labels]
    for cid in sorted(ref):
        if cid not in labels:
            continue
        mine = labels[cid]
        want = sorted(ref[cid]["failing"])
        got = sorted(mine.get("failing", []))
        if got != want or mine.get("status") != "ok":
            bad += 1
            print(
                f"MISMATCH {cid} ({ref[cid].get('kind')}): status={mine.get('status')} "
                f"extra={sorted(set(got) - set(want))} missing={sorted(set(want) - set(got))} "
                f"unknown={mine.get('unknown_failing')}"
            )
    print(f"compare: {len(ref) - len(missing) - bad}/{len(ref)} match, {bad} mismatch, {len(missing)} unlabeled")
    return 1 if bad or missing else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sets", nargs="+")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--tests", type=Path, default=LAB / "data" / "tests.json")
    ap.add_argument("--status", default="ok", help="comma list of label statuses to export")
    ap.add_argument("--compare", type=Path, help="reference answers.json to check the labels against")
    args = ap.parse_args(argv)

    labels: dict[str, dict] = {}
    origin: dict[str, str] = {}
    for s in args.sets:
        for cid, lab in load_labels(s).items():
            if cid in labels:
                print(f"duplicate id {cid} in {origin[cid]} and {s}", file=sys.stderr)
                return 2
            labels[cid] = lab
            origin[cid] = s

    if args.compare:
        rc = compare(labels, args.compare)
        if not args.out:
            return rc

    ref_kinds = {}
    if args.compare:
        ref_kinds = {k: v.get("kind") for k, v in json.loads(args.compare.read_text("utf-8"))["commits"].items()}
    out = args.out or (LAB / "out" / ("kit-" + "+".join(args.sets)))
    if out.exists():
        shutil.rmtree(out)
    (out / "commits").mkdir(parents=True)
    shutil.copy2(args.tests, out / "tests.json")
    wanted = set(args.status.split(","))
    answers = {}
    skipped = []
    for cid, lab in sorted(labels.items()):
        if lab.get("status") not in wanted:
            skipped.append(f"{origin[cid]}/{cid}:{lab.get('status')}")
            continue
        s = origin[cid]
        meta_path = CORPUS / s / "meta" / f"{cid}.meta.json"
        kind = ref_kinds.get(cid)
        if kind is None and meta_path.exists():
            kind = json.loads(meta_path.read_text("utf-8")).get("kind")
        shutil.copy2(CORPUS / s / "patches" / f"{cid}.patch", out / "commits" / f"{cid}.patch")
        answers[cid] = {"failing": sorted(lab["failing"]), "kind": kind or "unknown"}
    (out / "answers.json").write_text(json.dumps({"commits": answers}, indent=1, sort_keys=True) + "\n", "utf-8")
    print(f"wrote {len(answers)} commits to {out}")
    if skipped:
        print(f"skipped {len(skipped)} labels not in status {sorted(wanted)}: {' '.join(skipped[:20])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
