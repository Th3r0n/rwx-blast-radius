#!/bin/sh
# Runs one tracer pass inside br-lab (repo mounted at /src).
# usage: sh /src/lab/trace/run.sh <isolated|inorder|calib> <out-subdir> [extra tracer args]
set -e
mode="$1"; sub="$2"; shift 2
# The image sets PYTHONDONTWRITEBYTECODE=1 and ships no .pyc for sqlglot/stdlib parts:
# compile once so imports do not recompile (would inflate CPU numbers).
python -m compileall -q -j 8 /usr/local/lib/python3.13 /opt/sqlglot >/dev/null 2>&1 || true
cd /opt/sqlglot
exec python /src/lab/trace/tracer.py --tests /src/kit/sample/tests.json --root /opt/sqlglot \
    --out "/src/lab/out/trace/$sub" --pass "$mode" "$@"
