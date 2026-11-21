# Blast Radius entry — team Govinda

Given a patch against sqlglot at `eb11aec4`, print the tests that can fail.

## How it decides

Everything is derived from one instrumented run of sqlglot's own test suite at the
pinned commit (`tools/mapplugin.py`). That run records, per test:

- the source lines it executes,
- the repository files it opens and directories it lists,
- the class attributes it reads and the keys it looks up in class-level and
  module-level dicts and sets,
- which lines run while each module is imported.

`tools/pack_maps.py` packs the recording into `mapsgz/`. No patch is involved in
building the maps.

At run time (`selector.py`), for each patch:

1. **Data files** (fixtures and the like): the tests that open that file.
2. **Edits inside a function body**: the tests that execute the edited statement
   (or the whole function when the edit is not a line-for-line replacement).
   Layout and comment-only edits are recognized by comparing syntax trees.
3. **Edits that take effect at import time** (class attributes, table entries,
   decorators, module constants, functions called during import): a forked
   child re-imports the patched module and its dependents (`engine.py`),
   compares every class and module global with the original, and selects the
   tests that read the attributes or look up the keys that actually differ.
4. **Anything else** (unparseable patches, test configuration, failures of the
   analysis itself): every test.

The selected tests are split over workers to minimize busiest load + 2 s per worker.

## Rebuilding the maps

```sh
git clone https://github.com/tobymao/sqlglot && cd sqlglot && git checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
pip install -r ../requirements.lock
SKIP_INTEGRATION=0 MAP_OUT=/tmp/raw.pickle PYTHONPATH=../tools:. python -m pytest tests -p mapplugin -p no:cacheprovider -q
python ../tools/pack_maps.py /tmp/raw.pickle . ../maps
```
