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
    __slots__ = ("bits", "full", "reload", "notes", "src", "data", "runnable")

    def __init__(self):
        self.bits = 0
        self.full = False
        self.reload = {}  # module name -> (path, new source or None): import-time changes
        self.notes = []
        self.src = {}  # every patched python module -> (path, new source)
        self.data = {}  # every edited file -> new text, for code that opens it
        self.runnable = True  # tests can be run against the patched tree


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
        if moved or fp.binary:
            plan.runnable = False
        elif fp.hunks:
            old = read_source(fp.old)
            try:
                if old is None:
                    raise patchfmt.PatchError("missing")
                plan.data[fp.old] = patchfmt.apply(old, fp.hunks)
            except patchfmt.PatchError:
                plan.runnable = False
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
    plan.runnable = False
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
    plan.src[name] = (rel, new)
    oi = old_info(rel, old)
    try:
        ni = PyInfo(new.encode("utf-8", "surrogateescape"))
    except (SyntaxError, ValueError, RecursionError) as e:
        plan.notes.append(f"{rel} no longer compiles")
        if is_test:
            plan.bits |= maps.tests_importing(name)
        else:
            plan.full = True
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
        if max(loads) <= ms[order[0]]:
            break
    return [g for g in best[1] if g]


def pack_cost(tests, ms):
    groups = pack(tests, ms)
    if not groups:
        return 0.0
    return (max(sum(ms[t] for t in g) for g in groups) + WORKER_MS * len(groups)) / 1000.0


# ---------------------------------------------------------------- running tests
HEAVY_CPU = float(os.environ.get("BLAST_HEAVY", "2.5"))  # never run a test estimated above this
MARGIN = float(os.environ.get("BLAST_MARGIN", "1.0"))
VERIFY = os.environ.get("BLAST_VERIFY", "1") == "1"
VERIFY_UNTIL = float(os.environ.get("BLAST_VERIFY_UNTIL", "110"))  # no new test runs after this many seconds
REF_WARM = float(os.environ.get("BLAST_REF_WARM", "0.55"))  # warm-up CPU where the costs were measured
TMP = os.environ.get("TMPDIR", "/tmp")


class Ctx:
    """What the parent knows about the batch, shared with its children by fork."""

    def __init__(self, maps, tj):
        self.maps = maps
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in tj}
        self.number = {t["id"]: t["number"] for t in tj}
        self.id_of = {t["number"]: t["id"] for t in tj}
        self.everything = [t["number"] for t in tj]
        self.bit_number = [self.number.get(t) for t in maps.tests]
        self.bit_of = {t: i for i, t in enumerate(maps.tests)}
        known = {n for n in self.bit_number if n is not None}
        self.unmapped = [n for n in self.everything if n not in known]
        self.speed = 2.0
        try:
            self.calib = _load(os.path.join(MAPS, "calib.bin"))
        except OSError:
            self.calib = None

    def numbers(self, bits):
        out = []
        bn = self.bit_number
        while bits:
            low = bits & -bits
            n = bn[low.bit_length() - 1]
            if n is not None:
                out.append(n)
            bits ^= low
        if out and self.unmapped:
            out.extend(self.unmapped)
        return out

    def est(self, test_id):
        c = self.calib["test_cpu"].get(test_id)
        return None if c is None else self.speed * c + 0.003

    def run_set(self, bits, fixed):
        """Which candidate tests to run, and the CPU that is expected to cost.

        Running pays when the tests it may drop cost more to select than to run."""
        if self.calib is None:
            return [], 0.0
        nums = self.numbers(bits)
        if not nums:
            return [], 0.0
        light, heavy = [], []
        cpu = fixed
        classes = set()
        for n in nums:
            tid = self.id_of[n]
            e = self.est(tid)
            if e is None or e > HEAVY_CPU:
                heavy.append(n)
            else:
                light.append(n)
                cpu += e
                cls = tid.rsplit("::", 1)[0]
                if cls not in classes:
                    classes.add(cls)
                    cpu += self.speed * self.calib["class_cpu"].get(cls, 0.05)
        if not light:
            return [], 0.0
        gain = pack_cost(nums, self.ms) - pack_cost(heavy, self.ms)
        if gain <= cpu * MARGIN:
            return [], 0.0
        return [self.id_of[n] for n in light], cpu


def _send(fd, obj):
    b = marshal.dumps(obj)
    os.write(fd, len(b).to_bytes(4, "little") + b)


def child_main(ctx, warm, plan, verify, w):
    import engine

    sq = {m: v for m, v in plan.src.items() if m.startswith("sqlglot")}
    sq.update(plan.reload)
    tsrc = {m: v for m, v in plan.src.items() if m.startswith("tests")}
    bits, notes = plan.bits, []
    if sq:
        dbits, dnotes, broken, _ = engine.state_diff(warm, sq, diff=bool(plan.reload))
        bits |= dbits
        notes += dnotes
        if broken:
            _send(w, (bits, notes))
            return
    _send(w, (bits, notes))
    if not verify:
        return
    fixed = 0.0 if not sq else 0.0
    ids_run, cpu = ctx.run_set(bits, fixed)
    if not ids_run:
        return
    import tempfile

    import runner

    t0 = time.process_time()
    tmp = tempfile.mkdtemp(prefix="blast", dir=TMP)
    runner.prepare(SRC, tsrc, plan.data, tmp)
    speed = ctx.speed
    cpu_of = ctx.calib["test_cpu"]
    failed, _ = runner.run(SRC, ids_run, lambda tid: max(3.0, 6.0 * speed * cpu_of.get(tid, 1.0) + 2.0))
    for tid in ids_run:
        if tid not in failed:
            bits &= ~(1 << ctx.bit_of[tid])
    notes.append(f"ran {len(ids_run)} est {cpu:.2f}s took {time.process_time() - t0:.2f}s, {len(failed)} failed")
    _send(w, (bits, notes))


def start_child(ctx, warm, plan, verify):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            child_main(ctx, warm, plan, verify, w)
        except BaseException as e:  # noqa
            import traceback

            try:
                os.write(2, f"child failed: {e!r}\n{traceback.format_exc()[-1500:]}\n".encode())
            except Exception:
                pass
        finally:
            os._exit(0)
    os.close(w)
    return pid, r


def _frames(buf):
    out = []
    i = 0
    while i + 4 <= len(buf):
        n = int.from_bytes(buf[i : i + 4], "little")
        if i + 4 + n > len(buf):
            break
        out.append(marshal.loads(bytes(buf[i + 4 : i + 4 + n])))
        i += 4 + n
    return out


def run_children(ctx, warm, plans, jobs):
    """jobs: list of (cid, verify). Two at a time, one per core."""
    import select

    queue = list(jobs)
    running = {}  # fd -> [cid, pid, buffer, deadline]
    while queue or running:
        while queue and len(running) < 2:
            cid, verify = queue.pop(0)
            plan = plans[cid]
            elapsed = time.monotonic() - T0
            if elapsed > DEADLINE:
                if plan.reload:
                    plan.full = True
                    plan.notes.append("out of time")
                continue
            verify = verify and VERIFY and elapsed < VERIFY_UNTIL
            try:
                pid, r = start_child(ctx, warm, plan, verify)
            except Exception as e:
                if plan.reload:
                    plan.full = True
                plan.notes.append(f"fork failed: {e!r}")
                continue
            limit = min(60.0, max(5.0, DEADLINE + 20 - elapsed))
            running[r] = [cid, pid, bytearray(), time.monotonic() + limit]
        if not running:
            continue
        now = time.monotonic()
        wait = max(0.0, min(v[3] for v in running.values()) - now)
        ready, _, _ = select.select(list(running), [], [], wait)
        for r in ready:
            b = os.read(r, 1 << 20)
            if b:
                running[r][2] += b
                continue
            _finish(plans, running.pop(r), r, False)
        now = time.monotonic()
        for r in [r for r, v in running.items() if v[3] <= now]:
            _finish(plans, running.pop(r), r, True)


def _finish(plans, entry, r, killed):
    cid, pid, buf, _ = entry
    os.close(r)
    if killed:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    plan = plans[cid]
    frames = []
    try:
        frames = _frames(buf)
    except Exception as e:
        plan.notes.append(f"bad frames {e!r}")
    if killed:
        plan.notes.append("child timed out")
    if not frames:
        if plan.reload:
            plan.full = True  # what changed at import time is unknown
        plan.notes.append("child gave no answer")
        return
    bits, notes = frames[-1]
    plan.bits = bits
    plan.notes.extend(notes)
    if any(n.startswith("broken sqlglot") for n in notes):
        # a module that no longer imports breaks whatever loads it, often lazily
        # through the dialect registry: far more tests than any map shows
        plan.full = True


# ---------------------------------------------------------------- driver
def main():
    with open(os.path.join(DATA, "tests.json"), "rb") as f:
        tj = json.load(f)["tests"]
    maps = Maps(MAPS)
    ctx = Ctx(maps, tj)

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

    jobs = []
    for cid in ids:
        p = plans[cid]
        if p.full:
            continue
        if p.reload:
            jobs.append((cid, p.runnable))
        elif p.runnable and p.bits and VERIFY and ctx.calib is not None:
            jobs.append((cid, True))
    if jobs:
        warm = None
        try:
            import engine

            t0 = time.process_time()
            _preload_test_deps()  # before sqlglot: it checks whether unittest is loaded
            warm = engine.Warm(SRC, maps)
            warm_cpu = time.process_time() - t0
            ctx.speed = min(4.0, max(1.0, warm_cpu / REF_WARM))
            log(f"warm-up {warm_cpu:.2f}s cpu, speed factor {ctx.speed:.2f}")
            maps.reads  # load before forking so children share it
        except Exception as e:
            log(f"warm-up failed: {e!r}")
        if warm is None:
            for cid, _ in jobs:
                if plans[cid].reload:
                    plans[cid].full = True
        else:
            # verification-only jobs: skip those whose static set is not worth running
            kept = []
            for cid, verify in jobs:
                p = plans[cid]
                if p.reload:
                    kept.append((cid, verify))
                    continue
                sq = [m for m in p.src if m.startswith("sqlglot")]
                fixed = 0.002 * len(warm.dependents(sq)) * ctx.speed + 0.01
                if ctx.run_set(p.bits, fixed)[0]:
                    kept.append((cid, True))
            # import-time changes first: they matter most if time runs short
            kept.sort(key=lambda j: not plans[j[0]].reload)
            run_children(ctx, warm, plans, kept)

    out = []
    for cid in ids:
        p = plans[cid]
        tests = ctx.everything if p.full else ctx.numbers(p.bits)
        workers = pack(tests, ctx.ms)
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
        if DEBUG:
            log(cid, "FULL" if p.full else len(tests), "; ".join(p.notes)[:300])
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    log(f"done {len(ids)} commits in {time.monotonic() - T0:.2f}s wall, {time.process_time():.2f}s cpu (parent)")


def _preload_test_deps():
    """Third-party modules the tests import, loaded once before forking."""
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    for m in ("unittest", "unittest.mock", "sqlite3", "decimal", "tempfile", "concurrent.futures",
              "multiprocessing", "tracemalloc", "pytz", "duckdb", "numpy", "pandas", "pandas.testing"):
        try:
            __import__(m)
        except Exception:
            pass


if __name__ == "__main__":
    main()
