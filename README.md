# Blast Radius: proving which sqlglot tests a patch breaks

Entry for the RWX *Blast Radius* track. For each patch against sqlglot
`eb11aec`, the image prints which tests to run and how to split them over at
most 8 workers. The score charges simulated test time, 300 s per missed
failing test, and the CPU our container uses, so the selector tries to
*prove*, cheaply, which tests fail: static maps bound the candidates, and the
cheap candidates are run against the patched code in forked children.

## How a commit is decided
1. **Patch.** A strict in-memory applier (`br/patch.py`) rebuilds every touched
   file. Anything it does not understand (binary, rename, mismatching context)
   selects every test.
2. **What changed** (`br/codediff.py`, `br/fixtures.py`). Python files are
   compared as compiled code, instruction by instruction, ignoring line numbers:
   comments and formatting change nothing; each differing code object is
   reported with the pinned lines of its differing instructions, and classified
   as run time (a function body) or import time (module or class body, def
   header). SQL fixtures are compared by the entries the test helpers yield.
3. **Candidates** (`br/static.py`, `br/maps.py`). A run-time change selects the
   tests that executed one of those lines; an import-time change selects the
   tests that imported the module (every test, if pytest imports it while
   collecting); a data file selects its readers. Maps come from tracing every
   test of the pinned commit with `sys.monitoring` (line and function-start
   events per code object), each in a fresh fork of the post-collection state,
   plus one in-order pass of the whole suite; the union is used.
4. **Import-time state** (`br/statediff.py`, `br/buildstate.py`). A change that
   acts at import time is measured, not guessed: a child imports the patched tree
   (every test module, like pytest's collection, for modules loaded then; only the
   patched modules and their importers for lazily imported ones) and digests every
   module global, class attribute and dict key; the image build stored the same
   digests for the pinned commit. Each changed cell selects its readers, measured
   per test at the pinned commit with recording descriptors and recording dicts
   (`maps/attrs.bin`), or, for functions, the tests that ran the pinned function.
5. **Item slices** (`br/slices.py`). Tests that loop over a fixture file (TPC
   queries, optimizer fixture pairs) are run on the items the patch can reach only:
   per-item coverage was recorded at the pinned commit.
6. **Verification** (`br/verify.py`, `br/policy.py`). When it is cheaper than
   selecting them, candidates are run in a child forked from a warm process that
   imported every test module like pytest's collection. The patch is made live
   by swapping the code objects of the changed functions, serving patched
   sources to modules imported later and patched bytes to file reads. Process
   pools run inline with real pickle round trips. A test is dropped only after
   it passed in a child that proved the patch active.
7. **Packing** (`br/pack.py`): 1 to 8 workers, longest first plus local search,
   minimising busiest worker + 2 s per worker, in whole milliseconds.

Any exception, timeout or dead child keeps the tests concerned; a crash of the
whole program still prints "run everything" for every commit.

## What is precomputed, and from what
Only facts about the pinned commit: coverage per test and code object,
files and directories each test read, which tests start subprocesses, which
code ran while the test modules were imported, per-test CPU of an isolated
run, and (at image build) a digest of the import-time state. There is no
table from patches, or from kinds of edits, to tests.

Rebuild the maps (in an image with the pinned base, `requirements.lock` and
sqlglot at `eb11aec`):

```sh
python lab/trace/tracer.py --pass isolated ...   # see lab/trace/tracer.py --help
python lab/trace/tracer.py --pass inorder ...
python lab/trace/tracer.py --pass calib ...
python lab/build_maps.py --tests tests.json --trace out/isolated --trace out/inorder \
    --preload out/isolated/preload.json --calib out/calib.json --out maps
```

## Validation
- `lab/label_run.py` labels a patch by running the full suite with pytest in the
  labeling environment; it reproduces the kit's `sample/answers.json` exactly.
- `lab/mutate.py` generates mutants in the style of the sample (and out-of-style
  ones); `lab/farm.py` labels them; `lab/eval.py` scores the image under the
  scorer's limits. Results are in `docs/RESULTS.md`.

## Third-party code
None in the selector. `br/fixtures.py` restates the fixture parsing of sqlglot's
own `tests/helpers.py` (MIT). `br/validate.py` restates the output rules of the
track's `SCORING.md`. The image installs the track's `requirements.lock` from
PyPI and sqlglot from its own repository at the pinned commit.

## License
MIT
