"""Blast Radius selector.

For each patch against the pinned sqlglot commit, print the tests that can fail.

How a patch is turned into tests:

  1. Fixture and other data files: the tests recorded as opening that file.
  2. A change inside a function body: the tests recorded as executing the
     changed statement (or the whole function when the edit is not a plain
     line-for-line replacement).
  3. A change that takes effect at import time (class attributes, table
     entries, decorators, module constants, functions called while importing):
     a forked child imports the patched module and its dependents, compares
     every class and module global with the original, and selects the tests
     recorded as reading the attributes or looking up the keys that differ.
  4. Anything not understood: every test.

All records come from running the suite once at the pinned commit
(tools/mapplugin.py).  Nothing here knows about any particular patch.
"""

import json
import marshal
import os
import sys
import time

import patchfmt
from pyscope import PyInfo

T0 = time.monotonic()
DATA = os.environ.get("BLAST_DATA", "/data")
SRC = os.environ.get("BLAST_SRC", "/src/sqlglot")
MAPS = os.environ.get("BLAST_MAPS", "/app/maps")
DEBUG = os.environ.get("BLAST_DEBUG") == "1"
DEADLINE = float(os.environ.get("BLAST_DEADLINE", "150"))  # seconds of wall clock for the batch
WORKER_MS = 2000
MAX_WORKERS = 8
PYTEST_CONFIG = {"pytest.ini", "tox.ini", "conftest.py", ".pytest.ini", "pytest.toml", ".pytest.toml"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Maps:
    def __init__(self, path):
        self.path = path
        self.head = _load(os.path.join(path, "head.bin"))
        self.tests = self.head["tests"]
        self.n = len(self.tests)
        self.all_bits = (1 << self.n) - 1
        self._index = _load(os.path.join(path, "lines.idx"))
        self._blob = None
        self._lines = {}
        self._reads = None
        self._subs = None
        self._file_bits = None
        self.opens = {os.path.normpath(k) if not k.startswith("dir:") else "dir:" + os.path.normpath(k[4:] or "."): 0
                      for k in self.head["opens"]}
        for k, v in self.head["opens"].items():
            nk = os.path.normpath(k) if not k.startswith("dir:") else "dir:" + os.path.normpath(k[4:] or ".")
            self.opens[nk] |= v

    def lines(self, rel):
        d = self._lines.get(rel)
        if d is None:
            hit = self._index.get(rel)
            if hit is None:
                d = {}
            else:
                if self._blob is None:
                    with open(os.path.join(self.path, "lines.bin"), "rb") as f:
                        self._blob = f.read()
                d = marshal.loads(self._blob[hit[0] : hit[0] + hit[1]])
            self._lines[rel] = d
        return d

    @property
    def reads(self):
        if self._reads is None:
            self._reads = _load(os.path.join(self.path, "reads.bin"))
        return self._reads

    def subclasses(self, c):
        if self._subs is None:
            subs = {}
            for x, info in self.head["class_info"].items():
                for b in info["mro"]:
                    subs.setdefault(b, []).append(x)
            self._subs = subs
        return self._subs.get(c) or [c]

    def file_bits(self, rel):
        if self._file_bits is None:
            fb = {}
            for i, t in enumerate(self.tests):
                f = t.split("::", 1)[0]
                fb[f] = fb.get(f, 0) | (1 << i)
            self._file_bits = fb
        return self._file_bits.get(rel, 0)

    def tests_importing(self, module):
        """Tests whose file imports `module`, directly or through other modules."""
        deps = self.head["deps"]
        rdeps = getattr(self, "_rdeps", None)
        if rdeps is None:
            rdeps = {}
            for m, ds in deps["deps"].items():
                for d in ds:
                    rdeps.setdefault(d, set()).add(m)
            self._rdeps = rdeps
        seen, stack, bits = set(), [module], 0
        while stack:
            m = stack.pop()
            if m in seen:
                continue
            seen.add(m)
            rel = deps["files"].get(m)
            if rel:
                bits |= self.file_bits(rel)
            stack.extend(rdeps.get(m, ()))
        return bits


def _load(path):
    with open(path, "rb") as f:
        return marshal.load(f)


# ---------------------------------------------------------------- one patch
class Plan:
    __slots__ = ("bits", "full", "reload", "notes")

    def __init__(self):
        self.bits = 0
        self.full = False
        self.reload = {}  # module name -> (relative path, new source or None)
        self.notes = []


def read_source(rel):
    try:
        with open(os.path.join(SRC, rel), "rb") as f:
            return f.read().decode("utf-8", "surrogateescape")
    except FileNotFoundError:
        return None


_info_cache = {}


def old_info(rel, text):
    hit = _info_cache.get(rel)
    if hit is None:
        hit = _info_cache[rel] = PyInfo(text.encode("utf-8", "surrogateescape"))
    return hit


def module_name(rel):
    name = rel[:-3].replace("/", ".")
    if name.endswith(".__init__"):
        name = name[: -len(".__init__")]
    return name


def analyze(maps, text):
    plan = Plan()
    files = patchfmt.parse(text)
    if not files:
        if text.strip():
            plan.full = True
            plan.notes.append("unparsed patch")
        return plan
    for fp in files:
        if fp.mode_only and fp.old == fp.new:
            continue
        paths = [p for p in (fp.old, fp.new) if p]
        moved = fp.old != fp.new
        for p in paths:
            base = os.path.basename(p)
            if base in PYTEST_CONFIG or (base == "__init__.py" and moved and not p.startswith("sqlglot/")):
                plan.full = True
                plan.notes.append(f"test configuration {p}")
            if base in ("pyproject.toml", "setup.cfg") and "/" not in p:
                if moved or any("pytest" in t for h in fp.hunks for tag, t in h[4] if tag == "+"):
                    plan.full = True
                    plan.notes.append(f"test configuration {p}")
            if base in ("sitecustomize.py", "usercustomize.py"):
                plan.full = True
        if plan.full:
            return plan
        # whoever opens the file, or lists its directory when it appears or disappears
        for p in paths:
            plan.bits |= maps.opens.get(os.path.normpath(p), 0)
            if moved:
                plan.bits |= maps.opens.get("dir:" + (os.path.dirname(p) or "."), 0)
        for p in paths:
            if p.endswith(".py") and p.startswith(("sqlglot/", "tests/")):
                break
        else:
            continue
        if fp.binary:
            plan.full = True
            plan.notes.append("binary patch of a python file")
            return plan
        if moved:
            _moved_python(maps, plan, fp)
        else:
            _edited_python(maps, plan, fp)
        if plan.full:
            return plan
    return plan


def _moved_python(maps, plan, fp):
    """A python file created, deleted or renamed."""
    if fp.old and fp.old.endswith(".py") and fp.old.startswith(("sqlglot/", "tests/")):
        name = module_name(fp.old)
        for b in maps.lines(fp.old).values():
            plan.bits |= b
        plan.bits |= maps.file_bits(fp.old)
        plan.bits |= maps.tests_importing(name)
        if fp.old.startswith("sqlglot/"):
            plan.reload[name] = (fp.old, None)
        plan.notes.append(f"removed {fp.old}")
    if fp.new and fp.new.endswith(".py") and fp.new.startswith(("sqlglot/", "tests/")):
        name = module_name(fp.new)
        files = maps.head["deps"]["files"]
        old = read_source(fp.old) if fp.old else ""
        try:
            new = patchfmt.apply(old or "", fp.hunks)
        except patchfmt.PatchError:
            plan.full = True
            return
        pkg_dir = fp.new[:-3]
        shadows = name in files or any(f.startswith(pkg_dir + "/") for f in files.values())
        parent_pkg = os.path.dirname(fp.new) + "/__init__.py"
        if fp.new.startswith("sqlglot/"):
            if shadows or os.path.basename(fp.new) == "__init__.py":
                plan.full = True
                plan.notes.append(f"new module shadows {name}")
                return
            plan.reload[name] = (fp.new, new)
        elif shadows:
            plan.full = True
        plan.notes.append(f"added {fp.new}")


def _edited_python(maps, plan, fp):
    rel = fp.old
    old = read_source(rel)
    if old is None:
        plan.full = True
        plan.notes.append(f"{rel} is not in the pinned tree")
        return
    try:
        new = patchfmt.apply(old, fp.hunks)
    except patchfmt.PatchError as e:
        plan.full = True
        plan.notes.append(f"cannot apply to {rel}: {e}")
        return
    if new == old:
        return
    is_test = rel.startswith("tests/")
    name = module_name(rel)
    oi = old_info(rel, old)
    try:
        ni = PyInfo(new.encode("utf-8", "surrogateescape"))
    except (SyntaxError, ValueError, RecursionError) as e:
        plan.notes.append(f"{rel} no longer compiles")
        if is_test:
            plan.bits |= maps.tests_importing(name)
        else:
            plan.reload[name] = (rel, new)
        return
    if oi.dump() == ni.dump():
        return  # comments and layout only
    lines = maps.lines(rel)
    imp = maps.head["import_lines"].get(rel, frozenset())
    need = False
    if oi.names != ni.names:
        need = True

    def cover(rng):
        bits = 0
        for ln in rng:
            b = lines.get(ln)
            if b:
                bits |= b
        return bits

    def runs_at_import(sc):
        for ln in range(sc.start, sc.end + 1):
            if ln in imp and sc.owns_body(ln):
                return True
        return False

    bits = 0
    for os_, ol, ns, nl, body in fp.hunks:
        o, n = (os_ if ol else os_ + 1), (ns if nl else ns + 1)
        block_old, block_new = [], []
        for tag, t in body + [(" ", "")]:
            if tag == "-":
                block_old.append((o, t))
                o += 1
            elif tag == "+":
                block_new.append((n, t))
                n += 1
            elif tag == " ":
                if block_old or block_new:
                    r = [(ln, t2) for ln, t2 in block_old if _matters(oi, ln, t2)]
                    a = [(ln, t2) for ln, t2 in block_new if _matters(ni, ln, t2)]
                    aligned = (
                        len(block_old) == len(block_new)
                        and oi.names == ni.names
                        and all(_indent(x[1]) == _indent(y[1]) for x, y in zip(block_old, block_new))
                    )
                    for ln, _ in r:
                        bits |= lines.get(ln, 0)
                        sc = oi.scope(ln)
                        if sc is None or not sc.owns_body(ln):
                            need = True
                            if sc is not None:
                                bits |= cover(range(sc.start, sc.end + 1))
                            continue
                        if runs_at_import(sc):
                            need = True
                        if sc.kind == "lambda":
                            bits |= cover(range(sc.start, sc.end + 1))
                            continue
                        hdr = oi.statement_lines(sc, ln) if aligned else None
                        bits |= cover(hdr if hdr is not None else range(sc.start, sc.end + 1))
                    if not aligned:
                        for ln, _ in a:
                            sc = ni.scope(ln)
                            if sc is None or not sc.owns_body(ln):
                                need = True
                                continue
                            named = sc.named()
                            oc = oi.by_qual.get(named.qual) if named is not None else None
                            if oc is None:
                                need = True
                                continue
                            if runs_at_import(oc):
                                need = True
                            bits |= cover(range(oc.start, oc.end + 1))
                    block_old, block_new = [], []
                o += 1
                n += 1
    plan.bits |= bits
    if need:
        if is_test:
            plan.bits |= maps.tests_importing(name)
            plan.notes.append(f"{rel}: test module changed at import level")
        else:
            plan.reload[name] = (rel, new)
            plan.notes.append(f"{rel}: import-time change")


def _indent(t):
    return len(t) - len(t.lstrip())


def _matters(info, line, text):
    s = text.strip()
    if s and not s.startswith("#"):
        return True
    return info.in_string(line)


# ---------------------------------------------------------------- packing
def pack(tests, ms):
    """Split tests over workers to minimize busiest load + 2 s per worker."""
    if not tests:
        return []
    order = sorted(tests, key=lambda t: -ms[t])
    total = sum(ms[t] for t in tests)
    best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads = [0] * k
        groups = [[] for _ in range(k)]
        for t in order:
            i = loads.index(min(loads))
            loads[i] += ms[t]
            groups[i].append(t)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, groups)
        if max(loads) <= ms[order[0]] or total / (k + 1) + WORKER_MS * (k + 1) > best[0] + WORKER_MS:
            if max(loads) <= ms[order[0]]:
                break
    return [g for g in best[1] if g]


# ---------------------------------------------------------------- driver
def run_child(warm, plan):
    """Fork, import the patched modules, report what changed as a test set."""
    import engine

    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(r)
            bits, notes, broken, _ = engine.state_diff(warm, plan.reload)
            payload = marshal.dumps((bits, notes))
        except BaseException as e:  # noqa
            import traceback

            payload = marshal.dumps((None, [f"child failed: {e!r}", traceback.format_exc()[-1500:]]))
        try:
            with os.fdopen(w, "wb") as f:
                f.write(payload)
        finally:
            os._exit(code)
    os.close(w)
    return pid, r


def collect_child(pid, r, timeout):
    import select

    chunks = []
    end = time.monotonic() + timeout
    ok = True
    while True:
        left = end - time.monotonic()
        if left <= 0:
            ok = False
            break
        ready, _, _ = select.select([r], [], [], left)
        if not ready:
            ok = False
            break
        b = os.read(r, 1 << 20)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    if not ok:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    if not ok:
        return None, ["child timed out"]
    try:
        return marshal.loads(b"".join(chunks))
    except Exception as e:
        return None, [f"child sent nothing usable: {e!r}"]


def main():
    with open(os.path.join(DATA, "tests.json"), "rb") as f:
        tj = json.load(f)["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tj}
    number = {t["id"]: t["number"] for t in tj}
    everything = [t["number"] for t in tj]
    maps = Maps(MAPS)
    bit_number = [number.get(t) for t in maps.tests]
    known = {n for n in bit_number if n is not None}
    unmapped = [n for n in everything if n not in known]  # tests the maps never saw

    def numbers(bits):
        out = []
        i = 0
        while bits:
            low = bits & -bits
            i = low.bit_length() - 1
            n = bit_number[i]
            if n is not None:
                out.append(n)
            bits ^= low
        if out and unmapped:
            out.extend(unmapped)
        return out

    commits_dir = os.path.join(DATA, "commits")
    ids = sorted(f[:-6] for f in os.listdir(commits_dir) if f.endswith(".patch"))
    plans = {}
    for cid in ids:
        try:
            with open(os.path.join(commits_dir, cid + ".patch"), "rb") as f:
                text = f.read().decode("utf-8", "surrogateescape")
            plans[cid] = analyze(maps, text)
        except Exception as e:  # anything unexpected: run everything for this commit
            p = Plan()
            p.full = True
            p.notes.append(f"analysis failed: {e!r}")
            plans[cid] = p

    todo = [cid for cid in ids if plans[cid].reload and not plans[cid].full]
    if todo:
        warm = None
        try:
            import engine

            warm = engine.Warm(SRC, maps)
            maps.reads  # load before forking so children share it
        except Exception as e:
            log(f"warm-up failed: {e!r}")
        running = []
        queue = list(todo)
        slots = 2
        while queue or running:
            while queue and len(running) < slots:
                cid = queue.pop(0)
                if warm is None or time.monotonic() - T0 > DEADLINE:
                    plans[cid].full = True
                    plans[cid].notes.append("no time or no warm state")
                    continue
                try:
                    pid, r = run_child(warm, plans[cid])
                    running.append((cid, pid, r))
                except Exception as e:
                    plans[cid].full = True
                    plans[cid].notes.append(f"fork failed: {e!r}")
            if running:
                cid, pid, r = running.pop(0)
                bits, notes = collect_child(pid, r, 60)
                plans[cid].notes.extend(notes)
                if bits is None:
                    plans[cid].full = True
                else:
                    plans[cid].bits |= bits

    out = []
    for cid in ids:
        p = plans[cid]
        tests = everything if p.full else numbers(p.bits)
        workers = pack(tests, ms)
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
        if DEBUG:
            log(cid, "FULL" if p.full else len(tests), "; ".join(p.notes)[:300])
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    log(f"done {len(ids)} commits in {time.monotonic() - T0:.2f}s wall, {time.process_time():.2f}s cpu (parent)")


if __name__ == "__main__":
    main()
