"""Pytest plugin used only during the image build to map file access to tests.

It records (a) files opened while a test runs, and (b) files imported while a
pytest test module is collected.  The resulting map is derived solely from the
pinned, unmodified sqlglot checkout.
"""

from __future__ import annotations

import builtins
import importlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("BLAST_SQLGLOT_ROOT", os.getcwd())).resolve()
OUT = Path(os.environ.get("BLAST_FILE_ACCESS_OUT", "/app/file-access.json"))
_CURRENT: list[str | None] = [None]
_FILES: dict[str, set[str]] = defaultdict(set)
_TEST_IMPORTS: dict[str, set[str]] = defaultdict(set)
_FIXTURE_FILES: dict[int, set[str]] = defaultdict(set)
_CURRENT_FIXTURE: list[int | None] = [None]
_ORIGINAL_IMPORT = builtins.__import__
_ORIGINAL_IMPORT_MODULE = importlib.import_module


def _relative(path: Any) -> str | None:
    if isinstance(path, bytes):
        try:
            path = os.fsdecode(path)
        except Exception:
            return None
    if not isinstance(path, (str, os.PathLike)):
        return None
    try:
        raw = os.fspath(path)
        absolute = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(os.getcwd(), raw))
        root = os.fspath(ROOT)
        if absolute == root:
            return "."
        if not absolute.startswith(root + os.sep):
            return None
        rel = os.path.relpath(absolute, root).replace(os.sep, "/")
    except Exception:
        return None
    if not rel or rel.startswith((".git/", "__pycache__/")) or "/__pycache__/" in rel:
        return None
    if rel.endswith((".pyc", ".pyo")):
        return None
    return rel


def _record_import_module(module: Any) -> None:
    current = _CURRENT[0]
    if not current:
        return
    filename = getattr(module, "__file__", None)
    rel = _relative(filename) if filename else None
    if not rel:
        return
    if current.startswith("@file:"):
        _TEST_IMPORTS[current[6:]].add(rel)
    else:
        # This also catches importlib.import_module() calls made after test
        # collection, including imports of modules already in sys.modules.
        _FILES[rel].add(current)


def _tracking_import(name, globals=None, locals=None, fromlist=(), level=0):
    result = _ORIGINAL_IMPORT(name, globals, locals, fromlist, level)
    current = _CURRENT[0]
    if current:
        # Record the module actually requested, including cached imports.
        if level and globals:
            package = (globals.get("__package__") or "").split(".")
            keep = max(0, len(package) - level + 1)
            parts = package[:keep]
            if name:
                parts.extend(name.split("."))
            absolute = ".".join(parts)
        else:
            absolute = name
        module = sys.modules.get(absolute)
        if module is not None:
            _record_import_module(module)
        # `from package import child` may import a submodule, or access an
        # attribute. Record a submodule only when Python actually loaded it.
        for child in fromlist or ():
            if child == "*":
                continue
            child_module = sys.modules.get(f"{absolute}.{child}")
            if child_module is not None:
                _record_import_module(child_module)
    return result



def _tracking_import_module(name, package=None):
    module = _ORIGINAL_IMPORT_MODULE(name, package)
    _record_import_module(module)
    return module


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if event != "open" or not args:
        return
    current = _CURRENT[0]
    if not current:
        return
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else None
    if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
        return
    if isinstance(flags, int):
        import os as _os
        if flags & (_os.O_WRONLY | _os.O_RDWR):
            return
    rel = _relative(args[0])
    if rel:
        _FILES[rel].add(current)
        fixture_id = _CURRENT_FIXTURE[0]
        if fixture_id is not None:
            _FIXTURE_FILES[fixture_id].add(rel)


class _InlineProcessPool:
    """Stand-in for ProcessPoolExecutor, used only while building the maps.

    sqlglot's tests farm work out to spawned worker processes, which coverage
    does not trace.  Every test whose real work happens in those workers then
    looks as if it never touches the code under test, and the coverage map
    misses it.  Running the submitted callables in the calling process keeps
    the work inside the test that submitted it, so coverage contexts and the
    file-access hook attribute it correctly.  Results are unchanged.
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_kw):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        from concurrent.futures import Future

        import pickle

        def _roundtrip(obj):
            # A real worker receives its arguments and returns its result
            # through pickle, and sqlglot pickles expressions via sqlglot.serde.
            # Do the same here so that code is attributed to the right test.
            try:
                return pickle.loads(pickle.dumps(obj))
            except Exception:
                return obj

        future: Future = Future()
        try:
            fn, args, kwargs = _roundtrip((fn, args, kwargs))
            future.set_result(_roundtrip(fn(*args, **kwargs)))
        except BaseException as exc:  # noqa: BLE001 - mirror executor semantics
            future.set_exception(exc)
        return future

    def map(self, fn, *iterables, **_kw):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, *args, **kwargs) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def pytest_configure(config) -> None:
    import concurrent.futures as _futures

    _futures.ProcessPoolExecutor = _InlineProcessPool  # type: ignore[misc,assignment]
    builtins.__import__ = _tracking_import
    importlib.import_module = _tracking_import_module
    sys.addaudithook(_audit)



# Keep collection active while pytest imports each test module so the audit
# hook can associate import-time fixture/source reads with that module.
try:
    import pytest

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_make_collect_report(collector):  # type: ignore[no-redef]
        path = getattr(collector, "path", None)
        rel = _relative(path) if path is not None else None
        old = _CURRENT[0]
        if rel and rel.startswith("tests/") and rel.endswith(".py"):
            _CURRENT[0] = "@file:" + rel
        try:
            yield
        finally:
            _CURRENT[0] = old

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(item, nextitem):
        old = _CURRENT[0]
        _CURRENT[0] = item.nodeid
        try:
            yield
        finally:
            _CURRENT[0] = old

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_fixture_setup(fixturedef, request):
        old_test = _CURRENT[0]
        old_fixture = _CURRENT_FIXTURE[0]
        # Session-scoped fixtures have the Session as request.node, whose
        # nodeid is empty.  The enclosing runtest protocol still identifies
        # the first requesting test; sessionfinish later propagates this
        # fixture's file accesses to every collected dependent test.
        _CURRENT[0] = request.node.nodeid or old_test
        _CURRENT_FIXTURE[0] = id(fixturedef)
        try:
            yield
        finally:
            _CURRENT[0] = old_test
            _CURRENT_FIXTURE[0] = old_fixture
except Exception:
    # pytest is installed in the builder; this keeps the module importable in
    # lightweight tooling that only performs static inspection.
    pass


def pytest_sessionfinish(session, exitstatus) -> None:
    # A session- or module-scoped fixture is opened only once, but every test
    # depending on it can be affected by a fixture-file change.  Expand those
    # accesses to all collected items that use the same fixture definition.
    for item in getattr(session, "items", ()):
        fixture_info = getattr(item, "_fixtureinfo", None)
        name_to_defs = getattr(fixture_info, "name2fixturedefs", {})
        for definitions in name_to_defs.values():
            for fixturedef in definitions:
                for rel in _FIXTURE_FILES.get(id(fixturedef), ()):
                    _FILES[rel].add(item.nodeid)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "files": {key: sorted(values) for key, values in sorted(_FILES.items())},
        "test_imports": {
            key: sorted(values) for key, values in sorted(_TEST_IMPORTS.items())
        },
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
