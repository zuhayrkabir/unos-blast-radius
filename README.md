# unos — Blast Radius selector

For each patch to sqlglot@eb11aec, pick the tests that can fail and split them over workers.

- `map.json.gz`: per-test line coverage of `sqlglot/` recorded on the pinned commit (pytest + coverage
  dynamic contexts, process pools run in-process with the same pickling so their work is attributed),
  plus which fixture files each test reads. Keyed by test id.
- `selector.py`: parses each diff, applies it to the vendored pinned sources, and selects:
  syntax error → everything; identical AST (comments/whitespace) → nothing; function-body lines →
  tests that reached the enclosing statement; lambdas → their coverage; class/module attributes →
  tests that ran a line reading that name (scoped to the dialect family for dialect classes);
  docstrings → doctests; fixtures → tests that opened them; test files → the edited test functions;
  anything unknown → everything. Then LPT split choosing the worker count that minimises
  busiest-worker seconds + 2 s per worker.
- `sqlglot_src.tar.gz`: the pinned sqlglot sources (.py only), unpacked at build and read for AST analysis. Apache-2.0 / MIT, © the sqlglot authors.
- `tool_covplugin.py`, `tool_buildmap.py`: how the map was built.

No table from patches or kinds of edit to tests: everything is derived from the pinned commit.
MIT licensed.
