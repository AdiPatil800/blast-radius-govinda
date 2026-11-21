"""Where a source line sits: which function, which statement, or import-time code."""

import ast

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


class Scope:
    __slots__ = ("kind", "start", "end", "body_start", "qual", "node", "parent")

    def __init__(self, kind, start, end, body_start, qual, node, parent):
        self.kind = kind  # "def" or "lambda"
        self.start = start  # first line, decorators included
        self.end = end
        self.body_start = body_start
        self.qual = qual
        self.node = node
        self.parent = parent  # enclosing Scope or None

    def owns_body(self, line):
        """True when `line` only runs when the function is called."""
        if self.kind == "def":
            return self.body_start > self.node.lineno and self.body_start <= line <= self.end
        return self.node.lineno < line < self.end

    def named(self):
        s = self
        while s is not None and s.kind != "def":
            s = s.parent
        return s


class PyInfo:
    def __init__(self, source):
        self.tree = ast.parse(source)
        self.scopes = []
        self.by_qual = {}
        self.names = []  # qualified names of every def and class, in order
        self.strings = []  # (start, end) of string literals spanning lines
        self._walk(self.tree, "", None)
        self.scopes.sort(key=lambda s: (s.end - s.start, s.start))
        self._dump = None

    def dump(self):
        if self._dump is None:
            self._dump = ast.dump(self.tree)
        return self._dump

    def _walk(self, node, prefix, parent):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FUNC):
                qual = prefix + child.name
                start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                sc = Scope("def", start, child.end_lineno, child.body[0].lineno, qual, child, parent)
                self.scopes.append(sc)
                self.by_qual.setdefault(qual, sc)
                self.names.append("def " + qual)
                self._walk(child, qual + ".", sc)
            elif isinstance(child, ast.Lambda):
                sc = Scope("lambda", child.lineno, child.end_lineno, child.body.lineno, prefix + "<lambda>", child, parent)
                self.scopes.append(sc)
                self._walk(child, prefix, sc)
            elif isinstance(child, ast.ClassDef):
                qual = prefix + child.name
                self.names.append("class " + qual)
                self._walk(child, qual + ".", parent)
            else:
                if isinstance(child, (ast.Constant, ast.JoinedStr)) and child.end_lineno > child.lineno:
                    if not isinstance(child, ast.Constant) or isinstance(child.value, (str, bytes)):
                        self.strings.append((child.lineno, child.end_lineno))
                self._walk(child, prefix, parent)

    def scope(self, line):
        for sc in self.scopes:  # smallest span first
            if sc.start <= line <= sc.end:
                return sc
        return None

    def in_string(self, line):
        for a, b in self.strings:
            if a <= line <= b and (a < line or b > line):
                return True
        return False

    def statement_lines(self, sc, line):
        """Lines of the innermost statement of `sc` holding `line` (only its
        header when it has a body), or None when that cannot be pinned down."""
        try:
            return _find(sc.node.body, line)
        except Exception:
            return None


def _span(node):
    if isinstance(node, ast.match_case):
        return node.pattern.lineno, node.body[-1].end_lineno
    start = node.lineno
    for d in getattr(node, "decorator_list", ()):
        start = min(start, d.lineno)
    return start, node.end_lineno


def _find(stmts, line):
    for s in stmts:
        a, b = _span(s)
        if not (a <= line <= b):
            continue
        children = []
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            v = getattr(s, field, None)
            if isinstance(v, list):
                children.extend(v)
        if not children:
            return range(a, b + 1)
        inner = _find(children, line)
        if inner is not None:
            return inner
        first = min(_span(c)[0] for c in children)
        if line < first:
            return range(a, first)
        return None
    return None
