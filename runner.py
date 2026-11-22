"""Running sqlglot's own tests inside the selector's process.

Used on a patched copy of sqlglot loaded in a forked child: a candidate test
that passes there cannot fail on that commit and is dropped from the selection.
"""

import concurrent.futures as cf
import contextlib
import importlib
import os
import pickle
import signal
import sys
import time
import unittest


class TestTimeout(BaseException):
    """Not an Exception, so code under test cannot swallow it."""


class InlineExecutor:
    """Stands in for the process pools two test modules create.  A spawned worker
    would import sqlglot from disk and miss the patch, so the work runs here,
    with the same pickling round trip arguments and results would go through."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        fut = cf.Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            res = pickle.loads(pickle.dumps(fn(*args, **kwargs)))
            fut.set_result(res)
        except TestTimeout:
            raise
        except BaseException as e:  # noqa
            fut.set_exception(e)
        return fut

    def shutdown(self, wait=True, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@contextlib.contextmanager
def _sub_test(self, msg=None, **params):
    # the first failing subtest fails the test, so the rest need not run;
    # a skip inside a subtest skips only that subtest, as unittest does
    try:
        yield
    except unittest.SkipTest:
        pass


class Result(unittest.TestResult):
    def __init__(self, limit_for):
        super().__init__()
        self.failed = set()
        self.limit_for = limit_for
        self.group = ()
        self.times = {}
        self._t = 0.0
        self.armed = False
        self.done = set()
        self.keep_going = None
        self.timed_out = False

    def _key(self, test):
        return (type(test).__module__, type(test).__qualname__, test._testMethodName)

    def startTest(self, test):
        self.timed_out = False
        super().startTest(test)
        self._t = time.process_time()
        self.armed = True
        signal.setitimer(signal.ITIMER_REAL, self.limit_for(self._key(test)), 0.25)

    def stopTest(self, test):
        self.armed = False
        signal.setitimer(signal.ITIMER_REAL, 0)
        self.times[self._key(test)] = time.process_time() - self._t
        if self.timed_out:
            self.failed.add(self._key(test))  # however it ended, a test that ran out of time fails
        super().stopTest(test)
        self.done.add(self._key(test))
        if self.keep_going is not None and not self.keep_going(self):
            self.shouldStop = True

    def _bad(self, test):
        if isinstance(test, unittest.TestCase) and hasattr(test, "_testMethodName") and type(test).__name__ != "_ErrorHolder":
            self.failed.add(self._key(test))
        else:  # setUpClass / tearDownClass / module fixture failed
            self.failed.update(self.group)

    def addError(self, test, err):
        self._bad(test)

    def addFailure(self, test, err):
        self._bad(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._bad(test)

    def addUnexpectedSuccess(self, test):
        self._bad(test)


def prepare(src, test_sources, data, tmpdir):
    """Make the tests see the patched tree: fresh imports of every test module
    (patched ones from `test_sources`), and `open` of an edited data file
    returning its patched text."""
    import builtins
    import io
    import linecache
    import tokenize

    import engine

    for name in [m for m in sys.modules if m == "tests" or m.startswith("tests.")]:
        del sys.modules[name]
    if test_sources:
        sys.meta_path.insert(0, engine.Overlay(src, test_sources))
    importlib.invalidate_caches()
    if not data:
        return
    redirect = {}
    for i, (rel, text) in enumerate(sorted(data.items())):
        full = os.path.join(src, rel)
        tmp = os.path.join(tmpdir, f"{i}_{os.path.basename(rel)}")
        with open(tmp, "wb") as f:
            f.write(text.encode("utf-8", "surrogateescape"))
        redirect[full] = tmp
        if rel.endswith(".py"):
            lines = text.splitlines(True)
            linecache.cache[full] = (len(text), None, lines, full)
    real_open = builtins.open

    def _map(file):
        if isinstance(file, (str, bytes, os.PathLike)):
            try:
                p = os.fsdecode(file)
            except Exception:
                return file
            if not os.path.isabs(p):
                p = os.path.join(os.getcwd(), p)
            return redirect.get(os.path.normpath(p), file)
        return file

    def patched_open(file, *a, **k):
        return real_open(_map(file), *a, **k)

    builtins.open = patched_open
    io.open = patched_open
    tokenize._builtin_open = patched_open


def split_id(test_id):
    path, klass, method = test_id.split("::")
    return path[:-3].replace("/", "."), klass, method


def run(src, test_ids, limit_for, patch_pools=True, class_times=None, keep_going=None):
    """Run tests by id, in the order given (grouped by class, classes in order of
    first appearance). Returns (failing ids, {id: cpu seconds}, ids run).

    keep_going(failed ids, ids run so far) is asked after every test; once it
    says no, the remaining tests are not run."""
    os.chdir(src)
    unittest.TestCase.subTest = _sub_test
    res = Result(lambda key: limit_for(by_key[key]))
    if keep_going is not None:
        res.keep_going = lambda r: keep_going(
            {by_key[k] for k in r.failed if k in by_key}, {by_key[k] for k in r.done if k in by_key}
        )
    by_key = {}
    groups = {}
    for tid in test_ids:
        mod, klass, method = split_id(tid)
        by_key[(mod, klass, method)] = tid
        groups.setdefault((mod, klass), []).append(method)

    def on_alarm(signum, frame):
        if res.armed:
            res.timed_out = True
            raise TestTimeout()

    signal.signal(signal.SIGALRM, on_alarm)
    failed = set()
    for (modname, klass), methods in groups.items():
        if res.shouldStop:
            break
        keys = [(modname, klass, m) for m in methods]
        t_class = time.process_time()
        try:
            mod = importlib.import_module(modname)
            if patch_pools and hasattr(mod, "ProcessPoolExecutor"):
                mod.ProcessPoolExecutor = InlineExecutor
            cls = getattr(mod, klass)
            cases = [cls(m) for m in methods]
        except BaseException:  # noqa: the file cannot be imported, or the test is gone
            failed.update(by_key[k] for k in keys)
            continue
        res.group = keys
        try:
            unittest.TestSuite(cases).run(res)
        except BaseException:  # noqa
            failed.update(by_key[k] for k in keys)
        finally:
            res.armed = False
            signal.setitimer(signal.ITIMER_REAL, 0)
        if class_times is not None:
            class_times[(modname, klass)] = time.process_time() - t_class
    failed.update(by_key[k] for k in res.failed if k in by_key)
    ran = {by_key[k] for k in res.done if k in by_key} | failed
    return failed, {by_key[k]: v for k, v in res.times.items() if k in by_key}, ran


def run_units(src, jobs, limit_s, patch_pools=True):
    """Run list-driven tests one item at a time.

    jobs: [(test id, class attribute holding the list, [item indices])], all of one
    class. The class fixture is set up once; each chosen item is run as the whole
    test with the list cut down to that item, stopping a test at its first failing
    item. Returns {test id: True if every chosen item passed}, or None when the
    class could not be set up (nothing is known then).
    """
    os.chdir(src)
    unittest.TestCase.subTest = _sub_test
    modname, klass, _ = split_id(jobs[0][0])
    state = {"res": None}

    def on_alarm(signum, frame):
        res = state["res"]
        if res is not None and res.armed:
            res.timed_out = True
            raise TestTimeout()

    signal.signal(signal.SIGALRM, on_alarm)
    try:
        mod = importlib.import_module(modname)
        if patch_pools and hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = InlineExecutor
        cls = getattr(mod, klass)
        cls.setUpClass()
    except BaseException:  # noqa
        return None
    out = {}
    try:
        for test_id, attr, indices in jobs:
            method = split_id(test_id)[2]
            full = list(getattr(cls, attr))
            ok = True
            try:
                for i in indices:
                    if i >= len(full):
                        ok = False
                        break
                    setattr(cls, attr, [full[i]])
                    res = Result(lambda key: limit_s)
                    state["res"] = res
                    try:
                        cls(method).run(res)
                    except BaseException:  # noqa
                        ok = False
                    finally:
                        res.armed = False
                        signal.setitimer(signal.ITIMER_REAL, 0)
                    if res.failed or res.errors or res.failures:
                        ok = False
                    if not ok:
                        break
            finally:
                setattr(cls, attr, full)
            out[test_id] = ok
    finally:
        state["res"] = None
        try:
            cls.tearDownClass()
            cls.doClassCleanups()
        except BaseException:  # noqa
            pass
    return out


# Tests that hand each query to sqlglot.executor.execute through a process pool:
# where their schema and queries come from, as their own setUpClass loads them.
OPTIMIZER_STAGE = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": ("TPCDS_SCHEMA", "optimizer/tpc-ds/tpc-ds.sql"),
    "tests/test_executor.py::TestExecutor::test_execute_tpch": ("TPCH_SCHEMA", "optimizer/tpc-h/tpc-h.sql"),
}


class _Stop(BaseException):
    pass


def optimizer_changed(src, test_id, indices, digests, give_up=6):
    """For the chosen queries of an executor test: the ones for which the
    optimizer (as sqlglot.executor.execute calls it) no longer returns exactly
    what it returned at the pinned commit.  None when that cannot be told, or
    when more than `give_up` differ (the caller then keeps the whole test).
    The test's own worker function is driven up to the optimizer call and
    stopped there, so no data is loaded and nothing is executed."""
    spec = OPTIMIZER_STAGE.get(test_id)
    if spec is None:
        return None
    os.chdir(src)
    try:
        import sqlglot.executor as ex
        from sqlglot import parse_one
        from sqlglot.executor.table import Table

        from blastkeys import fingerprint

        modname = split_id(test_id)[0]
        mod = importlib.import_module(modname)
        helpers = importlib.import_module("tests.helpers")
        schema = getattr(helpers, spec[0])
        sqls = list(helpers.load_sql_fixture_pairs(spec[1]))
        mod.initializer(schema, {name: Table(columns=cols, rows=[]) for name, cols in schema.items()})
        real = ex.optimize
        got = []

        def optimize(*a, **k):
            got.append(fingerprint(real(*a, **k)))
            raise _Stop()

        ex.optimize = optimize
        changed = []
        try:
            for i in indices:
                if i >= len(sqls) or i >= len(digests) or not digests[i]:
                    return None
                meta, sql, _ = sqls[i]
                del got[:]
                try:
                    expression = pickle.loads(pickle.dumps(parse_one(sql)))  # as the pool would pass it
                    mod.mp_execute(expression, meta)
                except _Stop:
                    pass
                except Exception:
                    pass  # the item now raises before or inside the optimizer: it differs
                if got != [digests[i]]:
                    changed.append(i)
                    if len(changed) > give_up:
                        return None
            return changed
        finally:
            ex.optimize = real
    except BaseException:  # noqa
        return None
