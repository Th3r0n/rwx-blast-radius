"""Run the selector; if it dies before printing, print "run everything" for every commit."""

import json
import os
import sys


def _fallback() -> None:
    data = os.environ.get("BR_DATA", "/data")
    with open(os.path.join(data, "tests.json"), "rb") as f:
        tests = json.load(f)["tests"]
    loads = [0] * 4
    workers = [[] for _ in range(4)]
    for t in sorted(tests, key=lambda t: (-round(t["seconds"] * 1000), t["number"])):
        i = loads.index(min(loads))
        workers[i].append(t["number"])
        loads[i] += round(t["seconds"] * 1000)
    names = sorted(n[:-6] for n in os.listdir(os.path.join(data, "commits")) if n.endswith(".patch"))
    sys.stdout.write("".join(json.dumps({"commit": c, "workers": workers}, separators=(",", ":")) + "\n" for c in names))


try:
    from . import main as _main

    _main.main()
except BaseException as exc:  # last resort: never die without an answer
    print("[br] fatal:", repr(exc), file=sys.stderr, flush=True)
    try:
        from . import main as _main  # noqa: F811

        written = getattr(_main, "WROTE", False)
    except BaseException:
        written = False
    if not written:
        try:
            _fallback()
        except BaseException as exc2:
            print("[br] fallback failed:", repr(exc2), file=sys.stderr, flush=True)
sys.stdout.flush()
os._exit(0)
