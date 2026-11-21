"""Split a kit-format dataset into chunks of N commits (realistic batch sizes)."""
import json, os, shutil, sys
src, out, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
ans = json.load(open(os.path.join(src, "answers.json")))["commits"]
ids = sorted(ans)
shutil.rmtree(out, ignore_errors=True)
for k in range(0, len(ids), n):
    d = os.path.join(out, f"chunk{k // n:02d}")
    os.makedirs(os.path.join(d, "commits"))
    shutil.copy(os.path.join(src, "tests.json"), d)
    part = {c: ans[c] for c in ids[k:k + n]}
    json.dump({"commits": part}, open(os.path.join(d, "answers.json"), "w"))
    for c in part:
        shutil.copy(os.path.join(src, "commits", c + ".patch"), os.path.join(d, "commits"))
print(len(ids), "commits ->", (len(ids) + n - 1) // n, "chunks")
