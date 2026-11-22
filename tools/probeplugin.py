"""pytest plugin: for every single-operator comparison in sqlglot, record over the
whole test suite at the pinned commit whether its sibling operator would ever
have given a different answer.

    is      vs ==        is not  vs !=
    <       vs <=        >       vs >=

sqlglot modules are compiled with each such comparison replaced by a call that
computes the original result (returned unchanged) and also evaluates the
sibling, noting any execution where the two disagree.  The result maps
(file, line, column) to flags; a patch that only swaps one of these operators
where they never disagreed cannot change what any test sees.

    SKIP_INTEGRATION=0 PROBE_OUT=/tmp/probe PYTHONPATH=<tools>:. python -m pytest tests -p probeplugin -n 8
"""

import ast
import concurrent.futures as cf
import importlib.abc
import importlib.machinery
import marshal
import operator
import os
import pickle
import sys

REPO = os.getcwd()
OUT = os.environ.get("PROBE_OUT", "/tmp/probe")

EXECUTED, IS_EQ, ISNOT_NE, LT_LE, GT_GE = 1, 2, 4, 8, 16
OPS = {
    "Is": operator.is_, "IsNot": operator.is_not, "Eq": operator.eq, "NotEq": operator.ne,
    "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge,
}
NODES = []  # node id -> (relpath, line, col, op)
FLAGS = []  # node id -> int


def _same(x, y):
    return type(x) is bool and type(y) is bool and x == y


def probe(nid, a, b):
    op = NODES[nid][3]
    r = OPS[op](a, b)
    f = FLAGS[nid] | EXECUTED
    try:
        if op in ("Is", "Eq"):
            if not (f & IS_EQ) and not _same(a is b, a == b):
                f |= IS_EQ
        elif op in ("IsNot", "NotEq"):
            if not (f & ISNOT_NE) and not _same(a is not b, a != b):
                f |= ISNOT_NE
        elif op in ("Lt", "LtE"):
            if not (f & LT_LE) and not _same(a < b, a <= b):
                f |= LT_LE
        else:
            if not (f & GT_GE) and not _same(a > b, a >= b):
                f |= GT_GE
    except BaseException:  # noqa: the sibling cannot even be evaluated: never equivalent
        f |= IS_EQ | ISNOT_NE | LT_LE | GT_GE
    FLAGS[nid] = f
    return r


class Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        op = type(node.ops[0]).__name__
        if op not in OPS:
            return node
        nid = len(NODES)
        NODES.append((self.rel, node.lineno, node.col_offset, op))
        FLAGS.append(0)
        call = ast.Call(
            func=ast.Name("__blast_probe__", ast.Load()),
            args=[ast.Constant(nid), node.left, node.comparators[0]],
            keywords=[],
        )
        return ast.copy_location(call, node)


class Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        rel = os.path.relpath(path, REPO)
        tree = ast.parse(data, filename=path)
        tree = ast.fix_missing_locations(Rewrite(rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True)

    def exec_module(self, module):
        module.__dict__["__blast_probe__"] = probe
        return super().exec_module(module)

    def get_code(self, fullname):
        # never use cached bytecode: it lacks the probes
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name != "sqlglot" and not name.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not (spec.origin or "").endswith(".py"):
            return spec
        if not os.path.abspath(spec.origin).startswith(os.path.join(REPO, "sqlglot") + os.sep):
            return spec
        spec.loader = Loader(name, spec.origin)
        return spec


assert "sqlglot" not in sys.modules, "probeplugin must load before sqlglot"
sys.meta_path.insert(0, Finder())


class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        fut = cf.Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            fut.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as e:  # noqa
            fut.set_exception(e)
        return fut

    def shutdown(self, wait=True, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def pytest_configure(config):
    os.environ["SKIP_INTEGRATION"] = "0"


def pytest_collection_finish(session):
    for modname in ("tests.test_optimizer", "tests.test_executor"):
        mod = sys.modules.get(modname)
        if mod is not None and hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = InlineExecutor


def pytest_sessionfinish(session):
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    out = {}
    for (rel, line, col, op), f in zip(NODES, FLAGS):
        key = (rel, line, col)
        out[key] = out.get(key, 0) | f
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"probe_{wid}.bin"), "wb") as fh:
        marshal.dump(out, fh)
    print(f"probeplugin: {len(NODES)} comparisons, {sum(1 for f in FLAGS if f & EXECUTED)} executed", file=sys.stderr)
