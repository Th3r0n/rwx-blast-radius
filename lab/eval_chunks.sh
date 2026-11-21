#!/bin/sh
# Evaluate an image on every chunk dir under $2 (one run each); print one line per chunk.
tag=$1; base=$2
for d in "$base"/chunk*; do
  python -I lab/eval.py --no-build --tag "$tag" --runs 1 --dataset "$d" > /dev/null 2>&1
  last=$(ls -d lab/out/eval/*/ | tail -1)
  python -I -c "
import json,sys; s=json.load(open('${last}summary.json')); r=s['runs'][0]
print('$d', r['eval']['simulated_milliseconds']/1000, len(r['eval'].get('missed_commits') or []), r['eval'].get('missed_commits'), r['cpu_us']/1e6, '${last}')"
done
