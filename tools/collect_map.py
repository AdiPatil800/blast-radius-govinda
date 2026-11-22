"""Which sqlglot function lines run while each test file is imported.

    SKIP_INTEGRATION=0 python tools/collect_map.py OUT.bin      (from the sqlglot checkout)

A test file can run sqlglot code at import, before any test: defining a
subclass of a sqlglot class runs its metaclass, asking a package for a lazily
loaded name runs the package's __getattr__.  No test's coverage shows that, so
it is recorded here per test file, with every sqlglot module already imported
(their own import-time lines are a separate map).
"""
import collections
import glob
import importlib
import marshal
import os
import pkgutil
import sys

os.environ["SKIP_INTEGRATION"] = "0"
import unittest  # noqa

import sqlglot

for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if not info.name.endswith("__main__"):
        try:
            importlib.import_module(info.name)
        except Exception:
            pass
import sqlglot.dialects as D

for n in list(getattr(D, "DIALECTS", [])):
    try:
        getattr(D, n)
    except Exception:
        pass
root = os.path.join(os.getcwd(), "sqlglot") + os.sep
mon = sys.monitoring
TOOL = 3
mon.use_tool_id(TOOL, "collect")
cur = None
hits = collections.defaultdict(lambda: collections.defaultdict(set))


def on_line(code, line):
    f = code.co_filename
    if f.startswith(root) and code.co_name != "<module>":
        hits[cur][os.path.relpath(f, os.getcwd())].add(line)
    return mon.DISABLE


mon.register_callback(TOOL, mon.events.LINE, on_line)
files = sorted(p for p in glob.glob("tests/**/*.py", recursive=True) if not p.endswith("__init__.py"))
files.sort(key=lambda p: p != "tests/helpers.py")
for p in files:
    cur = p
    mon.restart_events()
    mon.set_events(TOOL, mon.events.LINE)
    try:
        importlib.import_module(p[:-3].replace("/", "."))
    except Exception:
        pass
    mon.set_events(TOOL, 0)
out = {t: {f: sorted(ls) for f, ls in d.items()} for t, d in hits.items() if d}
marshal.dump(out, open(sys.argv[1], "wb"))
print({t: {f: len(ls) for f, ls in d.items()} for t, d in out.items()})
