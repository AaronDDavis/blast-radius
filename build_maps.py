"""Build compact, baseline-derived test selection maps.

Run after the pinned sqlglot test suite has been executed once under
pytest-cov with --cov-context=test and blast_plugin enabled.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import marshal
import re
import zipfile
from collections import defaultdict, deque
from pathlib import Path


def norm_path(path: str | Path, root: Path) -> str | None:
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return None


def clean_context(context: str) -> str | None:
    if not context:
        return None
    # pytest-cov versions differ on whether they append a phase suffix.
    candidate = context.split("|", 1)[0]
    if candidate.startswith("tests/") and "::" in candidate:
        return candidate
    return None


def parse_import_name(module_name: str, file_path: str, level: int = 0) -> str:
    if level <= 0:
        return module_name
    current = file_path[:-3].replace("/", ".")
    if current.endswith(".__init__"):
        current = current[: -len(".__init__")]
    else:
        current = current.rpartition(".")[0]
    parts = current.split(".") if current else []
    keep = max(0, len(parts) - level + 1)
    base = parts[:keep]
    if module_name:
        base.extend(module_name.split("."))
    return ".".join(base)


def module_name_for_file(rel: str) -> str:
    no_ext = rel[:-3] if rel.endswith(".py") else rel
    pieces = no_ext.split("/")
    if pieces[-1] == "__init__":
        pieces = pieces[:-1]
    return ".".join(pieces)


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def candidates_for_import(node: ast.AST, rel: str) -> set[str]:
    out: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.startswith("sqlglot") or alias.name.startswith("tests"):
                out.add(alias.name)
    elif isinstance(node, ast.ImportFrom):
        base = parse_import_name(node.module or "", rel, node.level)
        if base.startswith("sqlglot") or base.startswith("tests"):
            out.add(base)
            for alias in node.names:
                if alias.name != "*":
                    out.add(base + "." + alias.name)
    elif isinstance(node, ast.Call):
        # Also resolve literal lazy imports such as
        # importlib.import_module("sqlglot.dialects.foo").
        function_name = _dotted_name(node.func)
        is_dynamic_import = function_name == "__import__" or function_name.endswith(".import_module")
        if is_dynamic_import and node.args and isinstance(node.args[0], ast.Constant):
            module_name = node.args[0].value
            if isinstance(module_name, str) and (
                module_name.startswith("sqlglot") or module_name.startswith("tests")
            ):
                out.add(module_name)
    return out


def import_graph(root: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return module-name -> file and file -> imported-file graph."""
    files: dict[str, str] = {}
    tree_by_file: dict[str, ast.AST] = {}
    scan_roots = [root / "sqlglot", root / "tests"]
    if (root / "conftest.py").is_file():
        scan_roots.append(root)
    for base in scan_roots:
        if not base.exists():
            continue
        paths = [base / "conftest.py"] if base == root else list(base.rglob("*.py"))
        for path in paths:
            if not path.is_file() or path.suffix != ".py":
                continue
            rel = path.relative_to(root).as_posix()
            try:
                tree_by_file[rel] = ast.parse(path.read_text(encoding="utf-8"))
                files[module_name_for_file(rel)] = rel
            except Exception:
                continue

    graph: dict[str, set[str]] = defaultdict(set)
    for rel, tree in tree_by_file.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Call)):
                for name in candidates_for_import(node, rel):
                    # Importing ``pkg.submodule`` always executes each
                    # available parent package's __init__.py first. Include
                    # those implicit imports so edits to package initializers
                    # select their true importers too.
                    parts = name.split(".")
                    for end in range(1, len(parts) + 1):
                        target = files.get(".".join(parts[:end]))
                        if target:
                            graph[rel].add(target)
    return files, graph


def build_import_dependents(root: Path) -> dict[str, list[str]]:
    _, graph = import_graph(root)
    test_files = sorted(
        p.relative_to(root).as_posix()
        for p in (root / "tests").rglob("*.py")
        if p.is_file()
    )
    out: dict[str, set[str]] = defaultdict(set)
    for test_file in test_files:
        # pytest loads the nearest conftest.py files automatically; their
        # imports and fixtures are dependencies of every test below them.
        initial: set[str] = {test_file}
        parent = Path(test_file).parent
        while True:
            candidate = (parent / "conftest.py").as_posix()
            if (root / candidate).is_file():
                initial.add(candidate)
            if parent == Path("."):
                break
            parent = parent.parent
        if (root / "conftest.py").is_file():
            initial.add("conftest.py")

        seen: set[str] = set()
        todo: deque[str] = deque(initial)
        while todo:
            current = todo.popleft()
            if current in seen:
                continue
            seen.add(current)
            for dep in graph.get(current, ()):
                if dep not in seen:
                    todo.append(dep)
        for dep in seen:
            if dep != test_file and (
                dep.startswith("sqlglot/")
                or dep.startswith("tests/")
                or dep == "conftest.py"
            ):
                out[dep].add(test_file)
    return {key: sorted(value) for key, value in sorted(out.items())}


def build_function_scopes(root: Path, coverage_by_file: dict[str, dict[int, set[str]]]):
    result: dict[str, list[dict]] = {}
    for rel, lines in coverage_by_file.items():
        path = root / rel
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        scopes: list[dict] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            end = getattr(node, "end_lineno", node.lineno)
            body_start = min((getattr(child, "lineno", node.lineno) for child in node.body), default=node.lineno)
            body_start = min(body_start, end)
            # A docstring is evaluated when the function object is created, not
            # when the function is called. Track its lines separately so edits
            # there trigger the conservative import-dependent fallback.
            doc_start = doc_end = None
            if node.body and isinstance(node.body[0], ast.Expr):
                value = node.body[0].value
                if isinstance(value, (ast.Str, ast.Constant)) and isinstance(getattr(value, "value", None), str):
                    doc_start = getattr(value, "lineno", None)
                    doc_end = getattr(value, "end_lineno", doc_start)
            tests: set[str] = set()
            for line_no, contexts in lines.items():
                if body_start <= line_no <= end:
                    tests.update(contexts)
            scopes.append({
                "header_start": min([node.lineno] + [d.lineno for d in node.decorator_list]),
                "header_end": max(node.lineno, body_start - 1),
                "body_start": body_start,
                "body_indent": getattr(node.body[0], "col_offset", 0) if node.body else 0,
                "end": end,
                "doc_start": doc_start,
                "doc_end": doc_end,
                "tests": sorted(tests),
            })
        scopes.sort(key=lambda x: (x["body_start"], x["end"]))
        result[rel] = scopes
    return result


def build_test_function_index(root: Path) -> dict[str, list[str]]:
    """Index test function source from the pinned baseline for runtime semantic filtering."""
    test_root = root / "tests"
    index: dict[str, list[str]] = {}
    if not test_root.is_dir():
        return index

    for path in sorted(test_root.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except Exception:
            continue
        rel = path.relative_to(root).as_posix()
        lines = source.splitlines()

        def walk(node: ast.AST, scopes: list[str]) -> None:
            for child in getattr(node, "body", ()):
                if isinstance(child, ast.ClassDef):
                    walk(child, scopes + [child.name])
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name.startswith("test_"):
                        key = "::".join([rel, *scopes, child.name])
                        start = min(
                            [child.lineno]
                            + [getattr(d, "lineno", child.lineno) for d in child.decorator_list]
                        )
                        index[key] = [rel, "\n".join(lines[start - 1 : child.end_lineno])]
                    walk(child, scopes + [child.name])

        walk(tree, [])
    return index


def build_sqlglot_identifier_index(root: Path) -> dict[str, list[tuple[str, int]]]:
    """Build a source identifier-to-line index for fast runtime symbol lookups."""
    token_re = re.compile(r"\b[A-Za-z_]\w*\b")
    index: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path in sorted((root / "sqlglot").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line_no, text in enumerate(lines, 1):
            if text.lstrip().startswith("#"):
                continue
            for token in set(token_re.findall(text)):
                index[token].append((rel, line_no))
    return dict(index)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--coverage", required=True, type=Path)
    parser.add_argument("--file-access", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.repo.resolve()

    coverage_by_file: dict[str, dict[int, set[str]]] = {}
    try:
        from coverage import CoverageData
        data = CoverageData(basename=str(args.coverage))
        data.read()
        for filename in data.measured_files():
            rel = norm_path(filename, root)
            if not rel or not rel.startswith("sqlglot/") or not rel.endswith(".py"):
                continue
            try:
                raw = data.contexts_by_lineno(filename)
            except Exception:
                raw = {}
            per_line: dict[int, set[str]] = defaultdict(set)
            for line_no, contexts in raw.items():
                for context in contexts:
                    test_id = clean_context(context)
                    if test_id:
                        per_line[int(line_no)].add(test_id)
            if per_line:
                coverage_by_file[rel] = per_line
    except Exception as exc:
        print(f"coverage map warning: {type(exc).__name__}: {exc}", file=__import__("sys").stderr)

    coverage_map = {
        rel: {str(line): sorted(ids) for line, ids in sorted(lines.items())}
        for rel, lines in sorted(coverage_by_file.items())
    }
    file_tests: dict[str, set[str]] = defaultdict(set)
    test_imports: dict[str, set[str]] = defaultdict(set)
    try:
        raw_access = json.loads(args.file_access.read_text(encoding="utf-8"))
        for rel, contexts in raw_access.get("files", {}).items():
            for context in contexts:
                if context.startswith("@file:"):
                    file_tests[rel].add(context[len("@file:"):])
                elif context.startswith("tests/") and "::" in context:
                    # The test's data/fixture access occurred while this test
                    # itself was executing (setup, call, or teardown).
                    file_tests[rel].add(context)
        for test_file, imported in raw_access.get("test_imports", {}).items():
            for rel in imported:
                if rel.startswith("sqlglot/"):
                    test_imports[test_file].add(rel)
    except Exception as exc:
        print(f"file-access map warning: {type(exc).__name__}: {exc}", file=__import__("sys").stderr)

    import_dependents = build_import_dependents(root)
    # Merge observed imports with the static transitive dependency map.
    for test_file, imported in test_imports.items():
        for rel in imported:
            # This direct observation is useful when a module is imported
            # dynamically and isn't visible to the AST dependency walk.
            import_dependents.setdefault(rel, [])
            if test_file not in import_dependents[rel]:
                import_dependents[rel].append(test_file)
    import_dependents = {k: sorted(set(v)) for k, v in sorted(import_dependents.items())}

    function_scopes = build_function_scopes(root, coverage_by_file)

    # Keep exact baseline source for safe, in-memory syntax checking of patched
    # Python modules at runtime. No test answers or patch-specific facts enter it.
    source_zip = args.output.with_name("base-source.zip")
    with zipfile.ZipFile(source_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted((root / "sqlglot").rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            try:
                zf.write(path, rel)
            except OSError:
                pass
        # Store exact baseline test-function inputs, not answers or patch mappings.
        # The selector uses this index to inspect candidate test semantics without
        # reparsing whole test modules during the timed run.
        test_index = build_test_function_index(root)
        packed_index = gzip.compress(
            json.dumps(test_index, separators=(",", ":")).encode("utf-8"),
            compresslevel=9,
            mtime=0,
        )
        zf.writestr("test-function-index.json.gz", packed_index)
        identifier_index = build_sqlglot_identifier_index(root)
        packed_identifiers = gzip.compress(
            marshal.dumps(identifier_index), compresslevel=6, mtime=0
        )
        zf.writestr("sqlglot-identifier-index.marshal.gz", packed_identifiers)

    if not coverage_map:
        raise RuntimeError("no per-test source coverage was recorded; refusing to build an unsafe selector")
    if not import_dependents:
        raise RuntimeError("no import dependency map was generated; refusing to build an unsafe selector")

    payload = {
        "version": 1,
        "coverage": coverage_map,
        "file_access": {k: sorted(v) for k, v in sorted(file_tests.items())},
        "import_dependents": import_dependents,
        "function_scopes": function_scopes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=9) as stream:
        json.dump(payload, stream, separators=(",", ":"))
    print(
        "built maps: "
        f"{len(coverage_map)} covered modules, "
        f"{sum(len(v) for v in coverage_map.values())} covered lines, "
        f"{len(file_tests)} accessed files, "
        f"{len(import_dependents)} import dependencies",
        file=__import__("sys").stderr,
    )


if __name__ == "__main__":
    main()
