# Design decisions

Every building block is written from scratch for this entry. Where an idea came
from public material (other teams' public write-ups or commit messages), it is
noted; no code from any other team is used.

## Provenance
| Part | Status | Notes |
|---|---|---|
| `br/patch.py` | written from scratch | strict git-format applier, validated byte for byte against `git apply` |
| `br/codediff.py` | written from scratch | instruction-level comparison of compiled code; idea of comparing compiled code rather than text is ours (from the brief) |
| `br/fixtures.py` | restates `tests/helpers.py` of sqlglot (MIT) | needed to see fixtures exactly as the tests do |
| `br/static.py`, `br/maps.py`, `br/main.py` | written from scratch | |
| `br/pack.py`, `br/validate.py` | written from scratch | validation rules restate the kit's SCORING.md |
| `br/unitrun.py`, `br/inlinepool.py` | written from scratch | |
| `lab/*` | written from scratch, never shipped | |

## Soundness rules (each rule only adds tests)
- **Compiled-code equality.** Two versions of a file are the same when every code
  object has the same instructions (names, constants, jumps mapped onto each other,
  exception tables), ignoring line numbers. Normalised: `__firstlineno__` and
  `__static_attributes__` (nothing in sqlglot or its tests reads them), and
  docstrings: a function docstring is never loaded by an instruction, and sqlglot
  reads a class `__doc__` only in `Expr.__init_subclass__`, to keep it truthy
  (checked by grep over `sqlglot/` and `tests/` at the pinned commit).
- **Runtime changes.** A changed function body can only affect a test that
  executes one of the changed instructions. Lines are mapped from the pinned
  instructions; inserted code is charged to the pinned instructions on both sides
  of the insertion point; jumps whose targets no longer correspond charge the jump
  and its pinned target; an unmappable exception table, or a changed signature or
  flags, charges every test that started the code object.
- **Import-time changes** (module and class bodies, def headers, functions that
  ran while the warm state was built) select every test when the module is loaded
  during pytest collection; for a lazily imported module, every test that imported
  or ran it in the isolated traces (each test is traced in a fresh fork of the
  post-collection state, so each one re-imports what it needs).
- **Data files.** Fixtures are compared by their parsed entries, exactly as the
  helpers yield them; other files by their readers (audit-hook traces).
- **Anything not understood** (renames, binary patches, deleted files, pytest
  configuration, new collectable test modules, syntax errors, analysis errors)
  selects every test.
- `sqlglot.expressions.core.UNITTEST` is computed at import from
  `"unittest" in sys.modules or "pytest" in sys.modules`; every process that runs
  sqlglot code for us imports `unittest` first.

## Import-time state comparison
- Snapshot cells: module globals, class attributes (own `__dict__`), and each key
  of dict values; digests are Merkle-style over hash-seed independent
  descriptions. Taken at image build in the same warm state the verifier uses.
- Cells that differ between pinned processes with different hash seeds are
  digested order-insensitively; cells that still differ (a loop variable leaked
  from a loop over a set in `Hive`) are ignored unless the patch touches their
  module.
- Sequences whose elements are all classes are described as sorted sets: in
  sqlglot they are `isinstance` tuples, some built from sets, so their order
  depends on addresses. Reordering such a tuple is not something a token edit
  can do; any other change to them (membership) is still seen.
- `gc.collect()` runs before every snapshot: dead dynamically created classes
  would otherwise still be listed by `type.__subclasses__`.
