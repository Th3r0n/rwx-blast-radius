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
