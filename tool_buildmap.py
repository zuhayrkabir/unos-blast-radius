"""Combine coverage data into map.json: {file: {line: [test numbers]}} plus import-only lines and fixture opens."""
import glob, json, os, sys
import coverage

root = sys.argv[1]; tests_json = sys.argv[2]; out = sys.argv[3]
tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}
lines = {}
imp = {}
for f in glob.glob("/tmp/cov/.cov.*"):
    d = coverage.CoverageData(basename=f); d.read()
    for path in d.measured_files():
        rel = os.path.relpath(path, root)
        ctxs = d.contexts_by_lineno(path)
        for ln, cs in ctxs.items():
            for c in cs:
                if c in num:
                    lines.setdefault(rel, {}).setdefault(ln, set()).add(num[c])
                elif c == "" :
                    imp.setdefault(rel, set()).add(ln)
opens = {}
for f in glob.glob("/tmp/cov/opens.*.json"):
    for nid, files in json.load(open(f)).items():
        if nid in num:
            for p in files:
                opens.setdefault(p, set()).add(num[nid])
ids = [t["id"] for t in tests]
res = {"ids": ids, "lines": {f: {str(l): sorted(s) for l, s in d.items()} for f, d in lines.items()},
       "import_lines": {f: sorted(s) for f, s in imp.items()},
       "opens": {p: sorted(s) for p, s in opens.items()}}
json.dump(res, open(out, "w"), separators=(",", ":"))
print("files", len(lines), "fixtures opened", len(opens), "size", os.path.getsize(out), file=sys.stderr)
