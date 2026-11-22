"""Lines of sqlglot that run only once per process (tools/once_map.py OUT.bin).

    SKIP_INTEGRATION=0 PYTHONPATH=<blast>:. python <blast>/tools/once_map.py OUT.bin

Caches and lazy loading make some code run for the first test that needs it and
never again: the dispatch table a generator class builds, a dialect module
loaded on demand, a package resolving a name through __getattr__.  Per-test
coverage shows such a line for whichever test came first, although every later
user depends on what it computed.  Here the whole suite is run twice in one
process; a function line the first pass runs and the second does not is one of
those.  A patch touching a function that holds such a line selects every test.
"""
import collections
import importlib
import marshal
import os
import sys
import unittest

os.environ["SKIP_INTEGRATION"] = "0"
import runner  # the selector's own in-process runner pieces

import sqlglot  # noqa

root = os.path.join(os.getcwd(), "sqlglot") + os.sep
mon = sys.monitoring
TOOL = 3
mon.use_tool_id(TOOL, "once")
seen = None


def on_line(code, line):
    f = code.co_filename
    if f.startswith(root) and code.co_name != "<module>":
        seen.add((os.path.relpath(f, os.getcwd()), line, code.co_qualname))
    return mon.DISABLE


mon.register_callback(TOOL, mon.events.LINE, on_line)


def load():
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    files = sorted(
        os.path.join(dp, n)
        for dp, _, fn in os.walk("tests")
        for n in fn
        if n.startswith("test_") and n.endswith(".py")
    )
    for p in files:
        name = p[:-3].replace(os.sep, ".")
        try:
            mod = importlib.import_module(name)
        except Exception as e:  # noqa
            print("skip", name, repr(e)[:80], file=sys.stderr)
            continue
        if hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = runner.InlineExecutor
        for v in vars(mod).values():
            if isinstance(v, type) and issubclass(v, unittest.TestCase) and v.__module__ == name:
                suite.addTests(loader.loadTestsFromTestCase(v))
    return suite


passes = []
suite = load()
for i in range(2):
    seen = set()
    mon.restart_events()
    mon.set_events(TOOL, mon.events.LINE)
    res = unittest.TestResult()
    suite.run(res)
    mon.set_events(TOOL, 0)
    passes.append(seen)
    print(f"pass {i + 1}: {res.testsRun} tests, {len(res.failures) + len(res.errors)} not passing, {len(seen)} lines", file=sys.stderr)
    suite = load()  # fresh test objects (unittest drops them after a run)
once = passes[0] - passes[1]
out = collections.defaultdict(set)
funcs = collections.Counter()
for rel, line, qual in once:
    out[rel].add(line)
    funcs[(rel, qual)] += 1
marshal.dump({k: sorted(v) for k, v in out.items()}, open(sys.argv[1], "wb"))
print(len(once), "lines in", len(funcs), "functions of", len(out), "files", file=sys.stderr)
for (rel, qual), n in sorted(funcs.items()):
    print(f"  {rel}::{qual} {n}", file=sys.stderr)
