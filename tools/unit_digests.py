"""Reference digests for the list-driven executor tests (see unitcheck.py).

    SKIP_INTEGRATION=0 PYTHONHASHSEED=<n> PYTHONPATH=<tools>:. python tools/unit_digests.py OUT.bin

Run from the sqlglot checkout at the pinned commit, once per hash seed;
tools/pack_digests.py keeps an item's digests only when every run agrees.
"""
import importlib
import marshal
import os
import sys

os.environ["SKIP_INTEGRATION"] = "0"
import unittest  # noqa: sqlglot checks for it at import, as under pytest

import unitcheck

helpers = importlib.import_module("tests.helpers")
out = {}
for test_id in unitcheck.SPECS:
    mod = importlib.import_module(test_id.split("::")[0][:-3].replace("/", "."))
    kind, items = unitcheck.prepare(mod, helpers, test_id)
    out[test_id] = [unitcheck.item_digests(kind, mod, it) for it in items]
marshal.dump(out, open(sys.argv[1], "wb"))
print({t.split("::")[-1]: len(v) for t, v in out.items()})
