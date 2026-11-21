# Results

Local numbers come from `lab/eval.py` (scorer's docker flags, i7-12700K host).
Board numbers are reported by the team after each submission.

| Tag | Commit | What | Sample (30) local: simulated / CPU / misses | Dev corpus local | Board: simulated / CPU / misses / score |
|---|---|---|---|---|---|
| submit-1 | c402c58 | static selector only | 243.871 / 0.4 / 0 | 0 misses on 52 labeled | pending |
| submit-2 | 6c81c81 | + import-time state diff with measured readers, verification of cheap candidates | 164.353 / 4.8-7.9 / 0 | 56 labeled: 328.6 s simulated (oracle 151.1), 24.3 s CPU, 0 misses | pending |
