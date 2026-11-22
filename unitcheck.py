"""Digests of what sqlglot produces for one item of a list-driven executor test.

The TPC executor tests do the same few things for every query: parse it, hand
it to sqlglot.executor.execute (which optimizes, plans and runs it), transpile
it to DuckDB SQL for the reference answer, and compare the two results.  The
calls that turn the query text into something else are pure functions of that
text:

    parse_one(sql)                      -> a tree
    optimize(tree, schema, ...)         -> a tree      (inside execute)
    transpile(sql, write="duckdb")      -> SQL text

`item_digests` makes those calls exactly as the test does and returns a digest
of each result.  When a patch does not touch anything an item runs outside
those calls, and all digests equal the ones taken at the pinned commit, the
rest of the item runs unchanged code on unchanged input, so its outcome is
unchanged.  Used by tools/mapplugin.py (to take the reference digests) and by
runner.py (to compare).
"""

import hashlib
import pickle

from blastkeys import fingerprint

# test id -> (kind, schema name in tests.helpers, fixture path)
SPECS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": ("execute", "TPCDS_SCHEMA", "optimizer/tpc-ds/tpc-ds.sql"),
    "tests/test_executor.py::TestExecutor::test_execute_tpch": ("execute", "TPCH_SCHEMA", "optimizer/tpc-h/tpc-h.sql"),
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": ("optimized", "TPCH_SCHEMA", "optimizer/tpc-h/tpc-h.sql"),
}


class Stop(BaseException):
    pass


def _text(strings):
    return hashlib.md5("\0".join(strings).encode("utf-8", "replace")).hexdigest()


def prepare(mod, helpers, test_id):
    """(kind, items) for a test, with the test module set up as its pool initializer would."""
    from sqlglot.executor.table import Table

    kind, schema_name, fixture = SPECS[test_id]
    schema = getattr(helpers, schema_name)
    items = list(helpers.load_sql_fixture_pairs(fixture))
    if kind == "execute":
        mod.initializer(schema, {name: Table(columns=cols, rows=[]) for name, cols in schema.items()})
    return kind, items


def item_digests(kind, mod, item):
    """Digests for one item, in the order the test makes the calls."""
    import sqlglot.executor as ex

    meta, sql, expected = item
    out = []
    if kind == "execute":
        tree = mod.parse_one(sql)
        out.append(fingerprint(tree))
        if meta.get("execute"):
            real = ex.optimize
            got = []

            def optimize(*a, **k):
                got.append(fingerprint(real(*a, **k)))
                raise Stop()

            ex.optimize = optimize
            try:
                mod.mp_execute(pickle.loads(pickle.dumps(tree)), meta)  # as the pool would pass it
            except Stop:
                pass
            finally:
                ex.optimize = real
            out.append(got[0] if len(got) == 1 else "?")
            out.append(_text(mod.transpile(sql, write="duckdb")))
    else:
        out.append(_text(mod.transpile(sql, write="duckdb")))
        out.append(_text(mod.transpile(expected, write="duckdb")))
    return out
