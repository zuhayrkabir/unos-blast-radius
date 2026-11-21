"""unos Blast Radius selector.

For each patch, map the changed lines of the pinned sqlglot to the tests that execute them
(per-test line coverage recorded on the pinned commit), plus fixture files to the tests that
read them, with conservative fallbacks for import-time code, docstrings, syntax errors and
anything the map can't see. Then split the chosen tests over workers to minimise
busiest-worker seconds + 2 s per worker.
"""
import ast
import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SQLGLOT_SRC", str(HERE / "sqlglot")))  # pinned checkout (sources only)
MAP = HERE / "map.json.gz"
WORKER_COST = 2.0


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ patch parsing
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """-> {path: {"old": path_or_None, "new": path_or_None, "removed": set(old lines),
                  "anchors": set(old lines next to insertions), "added": [str], "removed_text": [str],
                  "new_file": bool, "deleted": bool}}"""
    files = {}
    cur = None
    old_ln = 0
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            path = m.group(2) if m else ln.split()[-1][2:]
            cur = files.setdefault(path, {"old": m.group(1) if m else path, "new": path, "removed": set(),
                                          "anchors": set(), "added": [], "removed_text": [],
                                          "new_file": False, "deleted": False, "binary": False, "rename": False})
        elif cur is not None and ln.startswith("new file mode"):
            cur["new_file"] = True
        elif cur is not None and ln.startswith("deleted file mode"):
            cur["deleted"] = True
        elif cur is not None and (ln.startswith("rename from") or ln.startswith("rename to") or ln.startswith("similarity index")):
            cur["rename"] = True
        elif cur is not None and (ln.startswith("Binary files") or ln.startswith("GIT binary patch")):
            cur["binary"] = True
        elif ln.startswith("--- ") or ln.startswith("+++ "):
            pass
        elif cur is not None and ln.startswith("@@"):
            m = HUNK.match(ln)
            old_ln = int(m.group(1)) if m else 0
        elif cur is not None and ln.startswith("-"):
            cur["removed"].add(old_ln)
            cur["removed_text"].append(ln[1:])
            old_ln += 1
        elif cur is not None and ln.startswith("+"):
            cur["anchors"].add(max(1, old_ln - 1))
            cur["anchors"].add(old_ln)
            cur["added"].append(ln[1:])
        elif cur is not None and (ln.startswith(" ") or ln == ""):
            if ln.startswith(" "):
                old_ln += 1
        elif ln.startswith("\\"):
            pass
        i += 1
    return files


def apply_patch_text(orig_lines, patch_text, path):
    """Return the new file text for `path` by applying the hunks (best effort, no fuzz)."""
    out = []
    pos = 0  # index into orig_lines (0-based)
    in_file = False
    lines = patch_text.split("\n")
    for ln in lines:
        if ln.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            in_file = bool(m) and m.group(2) == path
            continue
        if not in_file:
            continue
        if ln.startswith("--- ") or ln.startswith("+++ ") or ln.startswith("index ") or ln.startswith("new file") or ln.startswith("deleted file") or ln.startswith("similarity") or ln.startswith("rename "):
            continue
        if ln.startswith("@@"):
            m = HUNK.match(ln)
            start = int(m.group(1)) - 1 if m else pos
            if m and m.group(2) == "0":
                start += 1
            out.extend(orig_lines[pos:start])
            pos = start
        elif ln.startswith("-"):
            pos += 1
        elif ln.startswith("+"):
            out.append(ln[1:])
        elif ln.startswith(" "):
            out.append(orig_lines[pos] if pos < len(orig_lines) else ln[1:])
            pos += 1
    out.extend(orig_lines[pos:])
    return "\n".join(out)


# ------------------------------------------------------------------ behaviour-preserving rewrites
def _singleton(node):
    """None, or an enum-style constant such as TokenType.SELECT / NormalizationStrategy.UPPERCASE.
    (True/False are excluded: 1 == True but 1 is not True.)"""
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    if isinstance(node, ast.Attribute) and node.attr.isupper() and len(node.attr) > 1 \
            and isinstance(node.value, ast.Name) and node.value.id[:1].isupper() and not node.value.id.isupper():
        return True
    return False


class _CmpNorm(ast.NodeTransformer):
    """x is C  ==  x == C, and x is not C  ==  x != C, when C is None/True/False or an enum member."""

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and (_singleton(node.comparators[0]) or _singleton(node.left)):
            op = node.ops[0]
            if isinstance(op, ast.Eq):
                node.ops = [ast.Is()]
            elif isinstance(op, ast.NotEq):
                node.ops = [ast.IsNot()]
        return node


def _norm_dump(tree):
    import copy
    return ast.dump(_CmpNorm().visit(copy.deepcopy(tree)))


# ------------------------------------------------------------------ static structure of sources
_struct_cache = {}


def structure(path):
    """For a python source: list of (start, end, kind, name, parent_class) for every def/class,
    and the set of docstring lines."""
    if path in _struct_cache:
        return _struct_cache[path]
    p = SRC / path
    res = ([], set(), None)
    try:
        text = p.read_text()
        tree = ast.parse(text)
        spans = []
        doc = set()

        def walk(node, cls=None):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start = min([d.lineno for d in ch.decorator_list] + [ch.lineno])
                    spans.append((start, ch.end_lineno, "class" if isinstance(ch, ast.ClassDef) else "def", ch.name, cls))
                    body = ch.body
                    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                        doc.update(range(body[0].lineno, body[0].end_lineno + 1))
                    walk(ch, ch.name if isinstance(ch, ast.ClassDef) else cls)
                else:
                    walk(ch, cls)
        if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None), ast.Constant):
            doc.update(range(tree.body[0].lineno, tree.body[0].end_lineno + 1))
        walk(tree)
        res = (spans, doc, text.split("\n"))
    except Exception as e:  # noqa
        log("structure failed", path, e)
    _struct_cache[path] = res
    return res


def innermost(spans, line, kind=None):
    best = None
    for s in spans:
        if s[0] <= line <= s[1] and (kind is None or s[2] == kind):
            if best is None or s[0] >= best[0]:
                best = s
    return best


# ------------------------------------------------------------------ selection
class Selector:
    def __init__(self):
        self.tests = json.loads((DATA / "tests.json").read_text())["tests"]
        self.by_num = {t["number"]: t for t in self.tests}
        self.by_file = {}
        for t in self.tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])
        self.all = set(self.by_num)
        import gzip
        with gzip.open(MAP, "rt") as f:
            m = json.load(f)
        # the map is keyed by test id so it survives renumbering
        ids = m["ids"]
        id2num = {t["id"]: t["number"] for t in self.tests}
        self.idx = [id2num.get(i) for i in ids]
        self.lines = m["lines"]
        self.imp = {f: set(v) for f, v in m["import_lines"].items()}
        self.opens = m["opens"]
        self.dialect_users = m.get("dialect_users", {})
        self.expr_users = m.get("expr_users", {})
        self.filetests = {}
        self._trees = {}
        self._src_cache = None

    def nums(self, idxs):
        return {self.idx[i] for i in idxs if self.idx[i] is not None}

    # Tests that read only the *input* statements (even positions) of a load_sql_fixture_pairs file:
    # test_executor runs `sql` and compares it with duckdb; the expected (optimized) text is unused.
    INPUT_ONLY = {
        "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": {"tests/test_executor.py::TestExecutor::test_execute_tpcds"},
        "tests/fixtures/optimizer/tpc-h/tpc-h.sql": {"tests/test_executor.py::TestExecutor::test_execute_tpch"},
    }

    def only_expected_changed(self, path, info):
        """True if every changed line of a fixture-pairs file lies in an odd (expected) statement and
        no ';' (statement boundary) was added or removed."""
        try:
            old = (SRC / path).read_text(encoding="utf-8").split("\n")
        except Exception:
            return False
        if any(";" in l for l in info["added"]) or any(";" in l for l in info["removed_text"]):
            return False
        # statement index at the start of each line, counting ';' only on lines the loader keeps
        idx_at = []
        n = 0
        for line in old:
            idx_at.append(n)
            if line and not line.startswith("--"):
                n += line.count(";")
        touched = set(info["removed"]) | set(info["anchors"])
        if not touched:
            return False
        for ln in touched:
            if not (1 <= ln <= len(old)):
                return False
            if idx_at[ln - 1] % 2 != 1:
                return False
            # an anchor next to a boundary line could belong to either statement
            line = old[ln - 1]
            if ";" in line and ln in info["anchors"]:
                return False
        return True

    def tests_of_file(self, f):
        """Every test that executed any line of source file f."""
        if f not in self.filetests:
            s = set()
            for v in self.lines.get(f, {}).values():
                s.update(v)
            self.filetests[f] = self.nums(s)
        return self.filetests[f]

    def tests_of_span(self, f, a, b):
        s = set()
        d = self.lines.get(f, {})
        for ln in range(a, b + 1):
            v = d.get(str(ln))
            if v:
                s.update(v)
        return self.nums(s)

    def select(self, patch_text):
        files = parse_patch(patch_text)
        sel = set()
        reasons = []
        for path, info in files.items():
            s, why = self.for_file(path, info, patch_text)
            sel |= s
            reasons.append(f"{path}:{why}:{len(s)}")
        return sel, reasons

    def for_file(self, path, info, patch_text):
        if info["binary"] or info["rename"]:
            return set(self.all), "binary/rename"
        if path.startswith("sqlglot/") and path.endswith(".py"):
            return self.for_source(path, info, patch_text)
        if path.startswith("tests/") and path.endswith(".py"):
            return self.for_testfile(path, info)
        if path.startswith("tests/"):
            # fixture data: tests that opened it (or anything in its folder if unseen)
            hit = set()
            for p, idxs in self.opens.items():
                if p == path:
                    hit |= self.nums(idxs)
            if hit:
                if path in self.INPUT_ONLY and self.only_expected_changed(path, info):
                    drop = {t["number"] for t in self.tests if t["id"] in self.INPUT_ONLY[path]}
                    return hit - drop, "fixture-expected-only"
                return hit, "fixture"
            folder = os.path.dirname(path)
            for p, idxs in self.opens.items():
                if p.startswith(folder + "/"):
                    hit |= self.nums(idxs)
            if hit:
                return hit, "fixture-folder"
            return set(self.all), "fixture-unknown"
        if path.endswith(".py") or path in ("setup.py", "pyproject.toml", "setup.cfg"):
            return set(self.all), "other-py"
        # docs, markdown, CI config...: no test reads them
        if path.endswith((".md", ".rst", ".txt", ".yml", ".yaml", ".html", ".png", ".svg")) and not path.startswith("sqlglot/"):
            return set(), "non-code"
        return set(self.all), "unknown"

    def for_testfile(self, path, info):
        own = set(self.by_file.get(path, []))
        if info["new_file"]:
            return set(self.all), "new-testfile"
        spans, doc, src = structure(path)
        if not spans:
            return own or set(self.all), "testfile-unparsed"
        out = set()
        touched = sorted(info["removed"] | info["anchors"])
        for ln in touched:
            fn = innermost(spans, ln, "def")
            if fn is None or not fn[3].startswith("test"):
                # helper, setUp, class/module level: every test in the file (and importers, e.g. helpers.py)
                return own if own else set(self.all), "testfile-shared"
            cls = fn[4]
            tid_suffix = f"::{cls}::{fn[3]}" if cls else f"::{fn[3]}"
            for n in own:
                if self.by_num[n]["id"].endswith(tid_suffix):
                    out.add(n)
        if not out:
            return own, "testfile-nomatch"
        return out, "testfile-fn"

    def for_source(self, path, info, patch_text):
        if info["new_file"] or info["deleted"]:
            return set(self.all), "new/deleted-source"
        # 1. does the new file still compile?  If not, everything importing it fails.
        spans, doc, src = structure(path)
        if src is not None:
            new_text = apply_patch_text(src, patch_text, path)
            try:
                new_tree = ast.parse(new_text)
                compile(new_text, path, "exec")
            except SyntaxError:
                return set(self.all), "syntax-error"
            try:
                old_tree = self._tree(path)
                if old_tree is not None and ast.dump(old_tree) == ast.dump(new_tree):
                    return set(), "ast-same"
                if old_tree is not None and _norm_dump(old_tree) == _norm_dump(new_tree):
                    return set(), "equiv-compare"
            except Exception:
                pass
        covered = self.lines.get(path, {})
        imp = self.imp.get(path, set())
        out = set()
        why = set()
        # anchors from added comment/blank lines carry no code; removed comment/blank lines likewise
        touched = set()
        for ln in info["removed"]:
            t = src[ln - 1].strip() if src and 0 < ln <= len(src) else "x"
            if t and not t.startswith("#"):
                touched.add(ln)
        code_added = [a for a in info["added"] if a.strip() and not a.strip().startswith("#")]
        if code_added:
            touched |= info["anchors"]
        elif info["added"] and not touched:
            # only comments/blank lines were added but the AST changed (e.g. inside a string): be careful
            touched |= info["anchors"]
        touched = sorted(touched)
        lam = self._lambda_spans(path)
        for ln in touched:
            v = covered.get(str(ln))
            in_lambda = any(a <= ln <= b for a, b in lam)
            if in_lambda:
                if v is not None:
                    out |= self.nums(v)
                    why.add("lambda")
                else:
                    a, b = next((a, b) for a, b in lam if a <= ln <= b)
                    hit = self.tests_of_span(path, a, b)
                    out |= hit
                    why.add("lambda-unrun")
                continue
            if ln in doc:
                # docstrings feed doctests (test_docs) and help text
                out |= set(self.by_file.get("tests/test_docs.py", []))
                why.add("doc")
                continue
            fn_here = innermost(spans, ln, "def")
            in_body = fn_here is not None and fn_here[0] < ln <= fn_here[1] and not self._is_def_header(path, fn_here, ln)
            if not in_body and src is not None and 0 < ln <= len(src) and src[ln - 1].strip() and not src[ln - 1].strip().startswith("#"):
                # runs at import time (class body, module constant, decorator, def line)
                cls = innermost(spans, ln, "class")
                fn = innermost(spans, ln, "def")
                if fn is not None and fn[0] < ln <= fn[1] and ln not in imp:
                    out |= self.tests_of_span(path, fn[0], fn[1])
                    why.add("fn")
                elif cls is not None:
                    names = self.assigned_names(path, ln)
                    fam = self.family(path)
                    if names and not fam and cls[3] in self.expr_users and not (names & self.REGISTRY_NAMES):
                        out |= self.nums(self.expr_users[cls[3]])
                        why.add("expr-attr")
                        continue
                    out |= self.tests_of_span(path, cls[0], cls[1]) | self.tests_of_file(path)
                    if names and fam:
                        users = self.family_users(fam)
                        out |= self.readers(names) & users
                        why.add("dialect-attr")
                    elif names and cls[3] in self.expr_users and not (names & self.REGISTRY_NAMES):
                        # attribute of an expression type: only tests that built that type (or a subclass) can see it
                        users = self.nums(self.expr_users[cls[3]])
                        out |= self.readers(names) & users
                        why.add("expr-attr")
                    elif names:
                        out |= self.readers(names)
                        why.add("class-attr")
                    else:
                        out |= self.tests_of_class_users(path, cls)
                        why.add("class-body")
                else:
                    names = self.assigned_names(path, ln)
                    if names:
                        out |= self.tests_of_file(path) | self.readers(names)
                        why.add("module-attr")
                    else:
                        out |= self.tests_of_file(path) | self.importers(path)
                        why.add("module")
                continue
            if v is not None:
                a, b = self.stmt_span(path, ln)
                out |= self.nums(v) | self.tests_of_span(path, a, b)
                why.add("lines")
                continue
            # never executed: inside a function body but on an un-run line (or blank/comment)
            fn = innermost(spans, ln, "def")
            text = src[ln - 1] if src and 0 < ln <= len(src) else ""
            if fn is not None:
                # tests that entered the function: a new/changed branch may run for them
                out |= self.tests_of_span(path, fn[0], fn[1])
                why.add("fn-unrun")
            elif text.strip() == "" or text.strip().startswith("#"):
                why.add("blank")
            else:
                out |= self.tests_of_file(path) | self.importers(path)
                why.add("module-unrun")
        return out, "+".join(sorted(why)) or "none"

    def _tree(self, path):
        tree = self._trees.get(path)
        if tree is None:
            try:
                tree = ast.parse((SRC / path).read_text())
            except Exception:
                return None
            self._trees[path] = tree
        return tree

    def stmt_span(self, path, ln):
        """Lines of the innermost statement containing ln (for compound statements, just the header),
        so a change inside a multi-line condition counts every test that reached the statement."""
        tree = self._tree(path)
        if tree is None:
            return ln, ln
        best = None
        for n in ast.walk(tree):
            if isinstance(n, ast.stmt) and n.lineno <= ln <= (n.end_lineno or n.lineno):
                body = getattr(n, "body", None)
                if isinstance(body, list) and body and not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    hdr_end = body[0].lineno - 1
                    if ln <= hdr_end:
                        span = (n.lineno, hdr_end)
                    else:
                        continue
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                else:
                    span = (n.lineno, n.end_lineno or n.lineno)
                if best is None or span[0] >= best[0]:
                    best = span
        return best or (ln, ln)

    def _lambda_spans(self, path):
        tree = self._tree(path)
        if tree is None:
            return []
        return [(n.lineno, n.end_lineno) for n in ast.walk(tree) if isinstance(n, ast.Lambda)]

    # attributes that decide which expression type the parser builds for a name: changing them can
    # affect tests that never built this type, so they keep the broad treatment
    REGISTRY_NAMES = {"_sql_names", "key", "sql_names", "_sql_name"}

    FAMILY_DIRS = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/tokenizers/", "sqlglot/typing/")
    GENERIC = {"dialect", "__init__", "base"}

    def family(self, path):
        for d in self.FAMILY_DIRS:
            if path.startswith(d):
                name = path[len(d):].split("/")[0].replace(".py", "")
                if name and name not in self.GENERIC:
                    return name
        return None

    def family_users(self, fam):
        """Tests that ran function-body code of dialect `fam`, or of any dialect whose sources mention it
        (subclasses such as Redshift(Postgres))."""
        key = ("fam", fam)
        if key in self.filetests:
            return self.filetests[key]
        fams = {fam}
        pat = re.compile(r"\b" + re.escape(fam) + r"\b", re.I)
        changed = True
        while changed:
            changed = False
            for f, src in self._sources().items():
                ff = self.family(f)
                if ff and ff not in fams and any(pat.search(l) for l in src if ("import" in l or "class " in l)):
                    fams.add(ff)
                    changed = True
            pat = re.compile(r"\b(" + "|".join(map(re.escape, sorted(fams))) + r")\b", re.I)
        users = set()
        for f in self.lines:
            if self.family(f) in fams:
                users |= self.tests_of_file(f)
        # tests that actually instantiated the dialect (or a subclass of it), recorded on the pinned commit
        for fm in fams:
            users |= self.nums(self.dialect_users.get(fm, []))
        # tests named after the dialect (tests/dialects/test_<fam>.py)
        for fm in fams:
            users |= set(self.by_file.get(f"tests/dialects/test_{fm}.py", []))
        self.filetests[key] = users
        return users

    def assigned_names(self, path, ln):
        """Names bound by the class-body / module-level statement that contains line ln
        (an assignment, possibly a multi-line dict/list literal).  Empty if not a plain assignment."""
        try:
            tree = self._trees.get(path)
            if tree is None:
                tree = ast.parse((SRC / path).read_text())
                self._trees[path] = tree
        except Exception:
            return set()
        best = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.lineno <= ln <= node.end_lineno:
                if best is None or node.lineno >= best.lineno:
                    best = node
        if best is None:
            return set()
        targets = best.targets if isinstance(best, ast.Assign) else [best.target]
        names = set()
        for tg in targets:
            for n in ast.walk(tg):
                if isinstance(n, ast.Name):
                    names.add(n.id)
                elif isinstance(n, ast.Attribute):
                    names.add(n.attr)
        return {n for n in names if len(n) > 2}

    def _assignments(self):
        """(targets, referenced names) for every assignment statement in sqlglot (parsed once)."""
        if getattr(self, "_asg", None) is None:
            out = []
            for f in self._sources():
                tree = self._tree(f)
                if tree is None:
                    continue
                for n in ast.walk(tree):
                    if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n.value is not None:
                        tg = n.targets if isinstance(n, ast.Assign) else [n.target]
                        tnames = set()
                        for t in tg:
                            for x in ast.walk(t):
                                if isinstance(x, ast.Name):
                                    tnames.add(x.id)
                                elif isinstance(x, ast.Attribute):
                                    tnames.add(x.attr)
                        refs = set()
                        for x in ast.walk(n.value):
                            if isinstance(x, ast.Name):
                                refs.add(x.id)
                            elif isinstance(x, ast.Attribute):
                                refs.add(x.attr)
                        out.append((tnames, refs))
            self._asg = out
        return self._asg

    def derived(self, names, rounds=3):
        """Names computed from `names` anywhere in sqlglot, e.g. the metaclass builds
        INVERSE_TIME_MAPPING / TIME_TRIE from TIME_MAPPING."""
        names = set(names)
        for _ in range(rounds):
            new = set()
            for tnames, refs in self._assignments():
                if refs & names:
                    # only class-constant style names (TIME_TRIE, INVERSE_TIME_MAPPING, ...)
                    new |= {t for t in tnames if len(t) > 2 and t not in names and re.fullmatch(r"_?[A-Z][A-Z0-9_]+", t)}
            if not new:
                break
            names |= new
        return names

    def readers(self, names):
        """Tests that ran any sqlglot source line mentioning one of `names` (or a name derived from them)."""
        names = self.derived(names)
        pat = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names))) + r")\b")
        out = set()
        for f, src in self._sources().items():
            d = self.lines.get(f)
            if not d:
                continue
            for i, line in enumerate(src, 1):
                if pat.search(line):
                    v = d.get(str(i))
                    if v:
                        out |= self.nums(v)
        return out

    def _sources(self):
        if self._src_cache is None:
            self._src_cache = {}
            for p in (SRC / "sqlglot").rglob("*.py"):
                rel = str(p.relative_to(SRC))
                try:
                    self._src_cache[rel] = p.read_text().split("\n")
                except Exception:
                    pass
        return self._src_cache

    def _is_def_header(self, path, fn, ln):
        """Decorators and the def line itself (through the closing paren of the signature) run at import."""
        try:
            tree = self._trees.get(path)
            if tree is None:
                tree = ast.parse((SRC / path).read_text())
                self._trees[path] = tree
        except Exception:
            return False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn[3] and node.end_lineno == fn[1]:
                first_body = node.body[0].lineno if node.body else node.lineno
                if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
                    first_body = node.body[0].lineno
                return ln < first_body
        return False

    def _is_header(self, spans, ln):
        for s in spans:
            if s[0] <= ln and (ln == s[0] or ln == s[0]):
                return True
        return False

    def tests_of_class_users(self, path, cls):
        # class-level attributes are read by methods of the class and its subclasses elsewhere;
        # approximate with every test that ran any line of this file
        return self.tests_of_file(path)

    def importers(self, path):
        return set(self.all)


# ------------------------------------------------------------------ worker split
def split(nums, by_num):
    if not nums:
        return []
    ms = sorted(nums, key=lambda n: -round(by_num[n]["seconds"] * 1000))
    durs = [round(by_num[n]["seconds"] * 1000) for n in ms]
    best = None
    for k in range(1, 9):
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n, d in zip(ms, durs):
            i = loads.index(min(loads))
            loads[i] += d
            ws[i].append(n)
        cost = max(loads) + 2000 * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best[1]


def main():
    s = Selector()
    for p in sorted((DATA / "commits").glob("*.patch")):
        try:
            sel, why = s.select(p.read_text(errors="replace"))
        except Exception as e:  # never crash: run everything for that commit
            log("error", p.stem, repr(e))
            sel, why = set(s.all), ["error"]
        ws = split(sel, s.by_num)
        print(json.dumps({"commit": p.stem, "workers": ws}))
        log(p.stem, len(sel), " ".join(why))


if __name__ == "__main__":
    main()
