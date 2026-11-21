"""pytest plugin: per-test line coverage of sqlglot/ plus which files under tests/ each test opens."""
import builtins, io, json, os
import coverage

_cov = None
_cur = [""]
_opens = {}
_root = os.path.abspath(os.getcwd())
_orig_open = builtins.open
_orig_io_open = io.open


def _rec(path):
    try:
        p = os.path.abspath(os.fspath(path))
    except Exception:
        return
    if p.startswith(_root + os.sep):
        rel = os.path.relpath(p, _root)
        if not rel.startswith("sqlglot" + os.sep) or rel.endswith((".sql", ".json", ".csv", ".yaml", ".yml", ".txt", ".tsv")):
            _opens.setdefault(_cur[0], set()).add(rel)


def _open(file, *a, **k):
    _rec(file)
    return _orig_open(file, *a, **k)


class _SyncExecutor:
    """Stand-in for ProcessPoolExecutor so pooled work runs (and is covered) in the test itself."""
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *a, **k):
        import concurrent.futures as cf
        import pickle
        f = cf.Future()
        try:
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, a, k)))   # same round trip as a real pool
            f.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def map(self, fn, *its, **k):
        return map(fn, *its)

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def pytest_configure(config):
    global _cov
    import concurrent.futures as cf
    cf.ProcessPoolExecutor = _SyncExecutor
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    _cov = coverage.Coverage(data_file=f"/tmp/cov/.cov.{wid}", source=["sqlglot"], branch=False)
    _cov.start()
    builtins.open = _open
    io.open = _open


def pytest_runtest_setup(item):
    _cur[0] = item.nodeid
    _cov.switch_context(item.nodeid)


def pytest_runtest_teardown(item, nextitem):
    pass


def pytest_unconfigure(config):
    builtins.open = _orig_open
    io.open = _orig_io_open
    _cov.stop()
    _cov.save()
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with _orig_open(f"/tmp/cov/opens.{wid}.json", "w") as f:
        json.dump({k: sorted(v) for k, v in _opens.items()}, f)
