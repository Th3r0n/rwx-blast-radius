# Results

Local numbers come from `lab/eval.py` (scorer's docker flags, i7-12700K host shared
with other workloads, so local CPU is noisy). Board numbers are the practice board's
(40 hidden commits, median of 3 runs).

| Tag | Commit | What | Sample (30) local: simulated / CPU | Board: simulated / CPU / misses / score |
|---|---|---|---|---|
| submit-4 | 1b816de | static maps, import-time state diff with measured readers, lazy-module diff, item slices in verification | 152.020 / ~7.5 | 150.933 / 15.2-15.7 / 0 / **166.4** |
| submit-7 | 8abe147 | + TPC-DS front proof, readers of unmeasured cells, cheaper diffs, calibrated estimates, heavy tests verifiable | 131.832 / ~9 | 144.571 / 11.3-12.0 / 0 / **155.9** |
| submit-10 | f20679f | + subprocess tests via patched overlay, best-prefix plan in clean mode, TPC-DS front proof under import-time changes, faster static stage | 129.386 / ~9-15 (noisy host) | pending |

Labeled-mutant validation of submit-10 (40-commit batches, like the board):
- dev corpus (dev1, ood1, targeted1, state1; 620 commits): 0 misses, simulated 4695.0 s
  (oracle 2026.6 s).
- holdout1 (never used for tuning; 151 labeled so far): 0 misses.

## Verification threshold sweep (2026-11-22)
Local, scorer's docker flags, cgroup CPU of the whole container. Totals = simulated + CPU.

| BR_CPU_FACTOR / BR_SAFETY | dev (620) | holdout (193) | sample (30) |
|---|---|---|---|
| 1.5 / 0.7 (previous) | 4573.1 + 310.2 = 4883.4 | 1061.2 + 70.8 = 1132.0 | 129.386 + 6.8 = 136.2 |
| 1.0 / 1.0 | 4408.7 + 399.6 = 4808.3 | | 127.315 + 9.9 = 137.2 |
| 0.85 / 1.0 (chosen) | 4386.2 + 393.5 = 4779.7 | 1005.8 + 102.8 = 1108.6 | 127.315 + 10.0 = 137.4 |
| 0.7 / 1.0 | 4308.7 + 456.3 = 4764.9 | 1003.7 + 116.5 = 1120.1 | 127.315 + 10.4 = 137.7 |
| 0.5 / 1.0 | 4093.6 + 642.9 = 4736.4 | | |

0 misses everywhere. 0.85 keeps most of the gain if the scoring machine charges more CPU than ours.
