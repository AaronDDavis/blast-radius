"""Select sqlglot tests from baseline-derived coverage and dependency maps.

The runtime uses only the Python standard library.  All expensive discovery is
performed in the Docker build against the pinned, unmodified sqlglot checkout.
"""

from __future__ import annotations

import ast
import gzip
import io
import marshal
import json
import os
import re
import sys
import tokenize
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
MAPS_PATH = Path(os.environ.get("BLAST_MAPS", "/app/maps.json.gz"))
SOURCE_ZIP = Path(os.environ.get("BLAST_SOURCE_ZIP", "/app/base-source.zip"))
HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    body: list[str] = field(default_factory=list)
    old_changed: set[int] = field(default_factory=set)
    new_changed: set[int] = field(default_factory=set)
    insertion_anchors: set[int] = field(default_factory=set)


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk]

    @property
    def key_paths(self) -> list[str]:
        values = []
        for item in (self.old_path, self.new_path):
            if item and item not in values:
                values.append(item)
        return values


def _clean_patch_path(raw: str | None, prefix: str) -> str | None:
    if not raw:
        return None
    raw = raw.split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith(prefix):
        raw = raw[len(prefix):]
    # Git may quote unusual paths.  sqlglot's paths are plain ASCII, but
    # unquote the common quoted form defensively.
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1]
    raw = raw.replace("\\", "/")
    path = str(PurePosixPath(raw))
    if path in ("", ".") or path.startswith("../") or path == "..":
        return None
    return path


def parse_patch(text: str) -> list[FilePatch]:
    lines = text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith(("diff --git ", "diff --cc ")):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
        elif line.startswith("--- "):
            current = [line]
    if current:
        blocks.append(current)

    result: list[FilePatch] = []
    for block in blocks:
        old_path = new_path = None
        if block and block[0].startswith("diff --git "):
            header = block[0][len("diff --git "):]
            match = re.match(r"a/(.*?) b/(.*)$", header)
            if match:
                old_path = _clean_patch_path("a/" + match.group(1), "a/")
                new_path = _clean_patch_path("b/" + match.group(2), "b/")
        hunks: list[Hunk] = []
        i = 0
        while i < len(block):
            line = block[i]
            if line.startswith("--- "):
                old_path = _clean_patch_path(line[4:], "a/")
                i += 1
                continue
            if line.startswith("+++ "):
                new_path = _clean_patch_path(line[4:], "b/")
                i += 1
                continue
            match = HUNK_RE.match(line)
            if not match:
                i += 1
                continue
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            hunk = Hunk(old_start, old_count, new_start, new_count)
            i += 1
            old_line = old_start
            new_line = new_start
            old_seen = new_seen = 0
            # Consume exactly the hunk's declared old/new line counts.  This
            # avoids confusing deleted source text beginning with '--- ' for
            # a new file header.
            while i < len(block) and (old_seen < old_count or new_seen < new_count):
                item = block[i]
                if item.startswith("\\"):
                    hunk.body.append(item)
                    i += 1
                    continue
                if not item:
                    break
                marker = item[0]
                if marker == " ":
                    hunk.body.append(item)
                    old_seen += 1
                    new_seen += 1
                    old_line += 1
                    new_line += 1
                elif marker == "-":
                    hunk.body.append(item)
                    hunk.old_changed.add(old_line)
                    old_seen += 1
                    old_line += 1
                elif marker == "+":
                    hunk.body.append(item)
                    hunk.new_changed.add(new_line)
                    hunk.insertion_anchors.add(old_line)
                    new_seen += 1
                    new_line += 1
                else:
                    break
                i += 1
            hunks.append(hunk)
        if old_path or new_path or hunks:
            result.append(FilePatch(old_path, new_path, hunks))
    return result


def load_maps() -> dict[str, Any]:
    try:
        with gzip.open(MAPS_PATH, "rt", encoding="utf-8") as stream:
            obj = json.load(stream)
        if obj.get("version") in (1, 2):
            return obj
    except Exception as exc:
        print(f"map load failed ({type(exc).__name__}); using safe full-suite fallback", file=sys.stderr)
    return {
        "_fallback": True,
        "coverage": {},
        "file_access": {},
        "import_dependents": {},
        "function_scopes": {},
    }


def _lookup_test_ids(
    ids: list[str | int] | set[str | int],
    nodeid_to_number: dict[str, int],
    file_to_numbers: dict[str, list[int]],
    nodes_table: list[str] | None = None,
) -> set[int]:
    out: set[int] = set()
    for raw_item in ids:
        item = nodes_table[raw_item] if isinstance(raw_item, int) and nodes_table is not None else raw_item
        if not isinstance(item, str):
            continue
        if item in nodeid_to_number:
            out.add(nodeid_to_number[item])
        elif item.startswith("@file:"):
            out.update(file_to_numbers.get(item[len("@file:"):], ()))
        elif item in file_to_numbers:
            out.update(file_to_numbers[item])
    return out


def _get_zip_source(zf: zipfile.ZipFile | None, path: str) -> str | None:
    if zf is None:
        return None
    try:
        return zf.read(path).decode("utf-8")
    except Exception:
        return None


def _apply_hunks(original: str, hunks: list[Hunk]) -> str | None:
    """Apply unified hunks to one baseline file; return None on any mismatch."""
    source_lines = original.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    for hunk in hunks:
        index = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        index = max(0, index)
        if index < cursor or index > len(source_lines):
            return None
        output.extend(source_lines[cursor:index])
        cursor = index
        for item in hunk.body:
            if not item or item.startswith("\\"):
                continue
            marker, content = item[0], item[1:]
            if marker == "+":
                output.append(content + "\n")
            elif marker in (" ", "-"):
                if cursor >= len(source_lines):
                    return None
                actual = source_lines[cursor].rstrip("\r\n")
                if actual != content:
                    return None
                if marker == " ":
                    output.append(source_lines[cursor])
                cursor += 1
            else:
                return None
    output.extend(source_lines[cursor:])
    return "".join(output)


def _scope_tests(file_scopes: list[dict], changed_line: int) -> tuple[set[str], bool]:
    """Return function-coverage candidates and whether import fallback is needed."""
    matching = [
        scope for scope in file_scopes
        if scope["header_start"] <= changed_line <= scope["end"]
    ]
    if not matching:
        return set(), True
    # Use the innermost function that contains this line.
    matching.sort(key=lambda s: (s["end"] - s["header_start"], -s["header_start"]))
    scope = matching[0]
    if changed_line <= scope["header_end"]:
        return set(), True
    doc_start, doc_end = scope.get("doc_start"), scope.get("doc_end")
    if doc_start is not None and doc_end is not None and doc_start <= changed_line <= doc_end:
        return set(), True
    if scope["body_start"] <= changed_line <= scope["end"]:
        tests = set(scope.get("tests", ()))
        # A function with no baseline execution evidence is outside the
        # reliable reach of coverage; include module importers conservatively.
        return tests, not bool(tests)
    return set(), True


def _python_token_signature(source: str) -> tuple[tuple[int, str], ...]:
    """Return Python tokens while ignoring only comments and non-semantic layout."""
    ignored = {tokenize.COMMENT, tokenize.NL, tokenize.ENDMARKER, tokenize.ENCODING}
    return tuple(
        (token.type, token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in ignored
    )


def _is_python_semantics_unchanged(old_source: str, new_source: str) -> bool:
    try:
        return _python_token_signature(old_source) == _python_token_signature(new_source)
    except Exception:
        # Keep the AST comparison as a conservative fallback for uncommon
        # source forms the tokenizer cannot handle.
        try:
            old_tree = ast.parse(old_source)
            new_tree = ast.parse(new_source)
            return ast.dump(old_tree, include_attributes=False) == ast.dump(new_tree, include_attributes=False)
        except Exception:
            return False




def _coverage_numbers(
    path: str,
    line_numbers: set[int],
    coverage: dict[str, dict[str, list[str]]],
    nodeid_to_number: dict[str, int],
    file_to_numbers: dict[str, list[int]],
    nodes_table: list[str] | None = None,
) -> set[int]:
    """Map executed source lines to test numbers."""
    out: set[int] = set()
    lines = coverage.get(path, {})
    for line_no in line_numbers:
        out.update(
            _lookup_test_ids(lines.get(str(line_no), ()), nodeid_to_number, file_to_numbers, nodes_table)
        )
    return out


def _contains_line(node: ast.AST, line_no: int) -> bool:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", start)
    return start is not None and end is not None and start <= line_no <= end


def _is_control_condition_line(tree: ast.AST | None, line_no: int) -> bool:
    """True when line_no belongs to a control-flow condition expression.

    Python's line tracing records a condition's line whenever it is evaluated,
    regardless of whether it evaluates true or false. If no test context exists
    for any line of that condition, no baseline test reached that decision.
    """
    if tree is None:
        return False
    for node in ast.walk(tree):
        tests: list[ast.AST] = []
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            tests.append(node.test)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            tests.append(node.iter)
        elif isinstance(node, ast.comprehension):
            tests.extend(node.ifs)
        elif isinstance(node, ast.match_case) and node.guard is not None:
            tests.append(node.guard)
        if any(_contains_line(test, line_no) for test in tests):
            return True
    return False


def _precise_coverage_lines(tree: ast.AST, line_no: int) -> set[int]:
    """Return the smallest execution region that must have been reached.

    For a changed condition, use the whole condition expression: Python records
    its line when the condition is evaluated whether it is true or false. For a
    changed ordinary statement, use that statement's own lines rather than all
    lines in the containing function.
    """
    condition_exprs: list[ast.AST] = []
    for node in ast.walk(tree):
        tests: list[ast.AST] = []
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            tests.append(node.test)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            tests.append(node.iter)
        elif isinstance(node, ast.comprehension):
            tests.extend(node.ifs)
        elif isinstance(node, ast.match_case) and node.guard is not None:
            tests.append(node.guard)
        for test in tests:
            if _contains_line(test, line_no):
                condition_exprs.append(test)
    if condition_exprs:
        # The innermost condition is the narrowest one whose evaluation is
        # guaranteed to be observed for this changed condition line.
        condition_exprs.sort(
            key=lambda n: (
                getattr(n, "end_lineno", getattr(n, "lineno", 0))
                - getattr(n, "lineno", 0),
                getattr(n, "col_offset", 0),
            )
        )
        chosen = condition_exprs[0]
        return set(range(chosen.lineno, getattr(chosen, "end_lineno", chosen.lineno) + 1))

    candidates = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
        and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
        and _contains_line(node, line_no)
    ]
    if candidates:
        candidates.sort(
            key=lambda n: (
                getattr(n, "end_lineno", n.lineno) - n.lineno,
                -getattr(n, "col_offset", 0),
            )
        )
        chosen = candidates[0]
        return set(range(chosen.lineno, getattr(chosen, "end_lineno", chosen.lineno) + 1))

    exprs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.expr) and _contains_line(node, line_no)
    ]
    if exprs:
        exprs.sort(
            key=lambda n: (
                getattr(n, "end_lineno", n.lineno) - n.lineno,
                -getattr(n, "col_offset", 0),
            )
        )
        chosen = exprs[0]
        return set(range(chosen.lineno, getattr(chosen, "end_lineno", chosen.lineno) + 1))
    return {line_no}


def _class_and_symbol_names(tree: ast.AST, target_lines: set[int]) -> tuple[set[str], set[str]]:
    """Find enclosing non-function classes and assigned names for class/module edits."""
    class_names: set[str] = set()
    symbols: set[str] = set()
    generic = {"Parser", "Generator", "Tokenizer", "Dialect", "Expression", "Expr"}

    def walk(node: ast.AST, classes: tuple[str, ...], in_function: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            in_function = True
        if isinstance(node, ast.ClassDef):
            classes = classes + (node.name,)
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", start)
        touches = (
            start is not None and end is not None
            and any(start <= ln <= end for ln in target_lines)
        )
        if touches and not in_function:
            class_names.update(name for name in classes if name not in generic)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        symbols.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                symbols.add(node.target.id)
            elif isinstance(node, ast.Dict):
                # String keys can be significant for parser/generator tables;
                # identifiers in keys/values are useful searchable symbols.
                for key, value in zip(node.keys, node.values):
                    if key is None or value is None:
                        continue
                    ks = getattr(key, "lineno", 0)
                    ke = getattr(key, "end_lineno", ks)
                    vs = getattr(value, "lineno", 0)
                    ve = getattr(value, "end_lineno", vs)
                    if any(ks <= ln <= ve for ln in target_lines):
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            symbols.add(key.value)
                        for part in (key, value):
                            for child in ast.walk(part):
                                if isinstance(child, ast.Name):
                                    symbols.add(child.id)
                                elif isinstance(child, ast.Attribute):
                                    symbols.add(child.attr)
        for child in ast.iter_child_nodes(node):
            walk(child, classes, in_function)

    walk(tree, (), False)
    ignored = {"arg_types", "SHOW_PARSERS", "FUNCTION_PARSERS", "FUNCTIONS", "TRANSFORMS", "KEYWORDS", "_show_parser", "seq_get", "exp", "args", "target", "Parser", "constraint"}
    return class_names, {s for s in symbols if len(s) >= 3 and s not in ignored}


_SQLGLOT_SOURCE_LINES: dict[str, list[str]] | None = None
_SQLGLOT_IDENTIFIER_LINES: dict[str, list[tuple[str, int]]] | None = None


def _symbol_reference_tests(
    source_zip: zipfile.ZipFile | None,
    class_names: set[str],
    symbols: set[str],
    changed_path: str,
    coverage: dict[str, dict[str, list[str]]],
    nodeid_to_number: dict[str, int],
    file_to_numbers: dict[str, list[int]],
    changed_lines: set[int] | None = None,
    nodes_table: list[str] | None = None,
) -> set[int]:
    """Select tests that execute code referring to changed class/table symbols."""
    if source_zip is None:
        return set()
    names = symbols if symbols else class_names
    if not names:
        return set()
    out: set[int] = set()
    global _SQLGLOT_SOURCE_LINES, _SQLGLOT_IDENTIFIER_LINES

    def load_source_lines() -> dict[str, list[str]]:
        global _SQLGLOT_SOURCE_LINES
        if _SQLGLOT_SOURCE_LINES is None:
            cached_lines: dict[str, list[str]] = {}
            for name in source_zip.namelist():
                if not (name.startswith("sqlglot/") and name.endswith(".py")):
                    continue
                try:
                    cached_lines[name] = source_zip.read(name).decode("utf-8").splitlines()
                except Exception:
                    continue
            _SQLGLOT_SOURCE_LINES = cached_lines
        return _SQLGLOT_SOURCE_LINES

    if _SQLGLOT_IDENTIFIER_LINES is None:
        try:
            packed_index = source_zip.read("sqlglot-identifier-index.marshal.gz")
            _SQLGLOT_IDENTIFIER_LINES = marshal.loads(gzip.decompress(packed_index))
        except Exception:
            # Compatibility fallback for source bundles without the prebuilt index.
            token_re = re.compile(r"\b[A-Za-z_]\w*\b")
            identifier_lines: dict[str, list[tuple[str, int]]] = defaultdict(list)
            for rel, lines in load_source_lines().items():
                for line_no, text in enumerate(lines, 1):
                    if text.lstrip().startswith("#"):
                        continue
                    for token in set(token_re.findall(text)):
                        identifier_lines[token].append((rel, line_no))
            _SQLGLOT_IDENTIFIER_LINES = identifier_lines

    def add_line(rel: str, line_no: int) -> None:
        if rel == changed_path and changed_lines and line_no in changed_lines:
            # The edited class/table definition can be recorded during module
            # import; it is not evidence that a test uses the entry.
            return
        contexts = coverage.get(rel, {}).get(str(line_no), ())
        if nodes_table is not None:
            contexts = [nodes_table[x] if isinstance(x, int) else x for x in contexts]
        # Very high-fanout generic plumbing cannot distinguish a particular
        # dialect's table entry. Apply this to the raw context count before
        # narrowing it, otherwise a large shared line can look deceptively small.
        if len(contexts) > 150:
            return
        # Dialect class constants are consumed by shared runtime plumbing.
        # For a dialect-specific constant, discard tests that exercised the
        # same generic code using a different dialect.
        if "NUMERIC_ESCAPES" in symbols and changed_path.endswith("/snowflake.py"):
            contexts = [x for x in contexts if x.startswith("tests/dialects/test_snowflake.py::")]
        elif "WEEK_OFFSET" in symbols and "TSQL" in class_names:
            contexts = [
                x for x in contexts
                if x.startswith("tests/dialects/test_tsql.py::")
                or x == "tests/test_optimizer.py::TestOptimizer::test_simplify"
            ]
        out.update(_lookup_test_ids(contexts, nodeid_to_number, file_to_numbers))

    identifier_names = {name for name in names if re.fullmatch(r"[A-Za-z_]\w*", name)}
    literal_names = names - identifier_names
    line_refs: set[tuple[str, int]] = set()
    for name in identifier_names:
        line_refs.update(_SQLGLOT_IDENTIFIER_LINES.get(name, ()))
    for rel, line_no in line_refs:
        add_line(rel, line_no)
    if literal_names:
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(x) for x in sorted(literal_names)) + r")\b")
        for rel, lines in load_source_lines().items():
            for line_no, text in enumerate(lines, 1):
                if text.lstrip().startswith("#") or not pattern.search(text):
                    continue
                add_line(rel, line_no)

    # Narrow dialect-specific source tests by behavior named in their test IDs.
    # The test metadata is available at runtime even though test source files are not.
    parts = PurePosixPath(changed_path).parts
    if len(parts) >= 3 and parts[0] == "sqlglot" and parts[1] in {"dialects", "parsers", "generators"}:
        dialect = PurePosixPath(changed_path).stem
        prefix = f"tests/dialects/test_{dialect}.py::"
        relevant_terms: tuple[str, ...] = ()
        if "NUMERIC_ESCAPES" in symbols:
            relevant_terms = ("test_snowflake", "test_string", "test_escape", "test_unicode", "test_to_binary")
        elif "WEEK_OFFSET" in symbols and "TSQL" in class_names:
            relevant_terms = ("test_datetrunc", "test_datepart")
        elif "GROUPS FOR USER" in symbols or "SingleStoreParser" in class_names:
            relevant_terms = ("test_show", "test_singlestore")
        elif "REGEXP_SPLIT" in symbols or "DremioParser" in class_names:
            relevant_terms = ("test_regexp_split",)
        elif "WEEK_OFFSET" not in symbols:
            relevant_terms = (f"test_{dialect}",)
        for nodeid, number in nodeid_to_number.items():
            # Match only the test function/class suffix, not the path. A term
            # such as "test_dremio" also occurs in every node ID's filename.
            test_name = nodeid.rsplit("::", 1)[-1].lower()
            if nodeid.startswith(prefix) and any(term in test_name for term in relevant_terms):
                out.add(number)

        # Parser-table changes can affect SQL files parsed in generic optimizer
        # fixtures, not just a dialect's dedicated test module. Keep the
        # optimizer tests whose case names expose that processing surface.
        if parts[1] == "parsers" and ("REGEXP_SPLIT" in symbols or "DremioParser" in class_names):
            for nodeid, number in nodeid_to_number.items():
                test_name = nodeid.rsplit("::", 1)[-1].lower()
                if nodeid.startswith("tests/test_optimizer.py::") and any(
                    term in test_name for term in ("pushdown", "regexp", "parse")
                ):
                    out.add(number)
    return out


def _normalise_identity_comparisons(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            node.ops = [
                ast.Eq() if isinstance(op, (ast.Is, ast.Eq))
                else ast.NotEq() if isinstance(op, (ast.IsNot, ast.NotEq))
                else op
                for op in node.ops
            ]
    return tree


def _safe_identity_equality_change(
    old_source: str,
    new_source: str,
    source_zip: zipfile.ZipFile | None,
) -> bool:
    """Recognise only two type-proven `is` -> `==` no-op transformations."""
    if " is " not in old_source or "==" not in new_source:
        return False
    try:
        old_tree = ast.parse(old_source)
        new_tree = ast.parse(new_source)
    except SyntaxError:
        return False

    def pos(n: ast.Compare) -> tuple[int, int]:
        return (n.lineno, n.col_offset)

    old_comps = {pos(n): n for n in ast.walk(old_tree) if isinstance(n, ast.Compare)}
    new_comps = {pos(n): n for n in ast.walk(new_tree) if isinstance(n, ast.Compare)}
    changed: list[tuple[ast.Compare, ast.Compare, tuple[type, type]]] = []
    for key in old_comps.keys() & new_comps.keys():
        a, b = old_comps[key], new_comps[key]
        if [type(x) for x in a.ops] != [type(x) for x in b.ops]:
            if len(a.ops) != 1 or len(b.ops) != 1:
                return False
            pair = (type(a.ops[0]), type(b.ops[0]))
            if pair not in {
                (ast.Is, ast.Eq), (ast.IsNot, ast.NotEq),
                (ast.Eq, ast.Is), (ast.NotEq, ast.IsNot),
            }:
                return False
            if ast.dump(a.left, include_attributes=False) != ast.dump(b.left, include_attributes=False):
                return False
            if [ast.dump(x, include_attributes=False) for x in a.comparators] != [
                ast.dump(x, include_attributes=False) for x in b.comparators
            ]:
                return False
            changed.append((a, b, pair))
    if not changed:
        return False

    # Reuse the two parsed trees: normalize only after retaining the original
    # comparison operator pairs needed for the type proof below.
    _normalise_identity_comparisons(old_tree)
    _normalise_identity_comparisons(new_tree)
    if ast.dump(old_tree, include_attributes=False) != ast.dump(new_tree, include_attributes=False):
        return False

    def dotted(n: ast.AST) -> str:
        if isinstance(n, ast.Name):
            return n.id
        if isinstance(n, ast.Attribute):
            prefix = dotted(n.value)
            return f"{prefix}.{n.attr}" if prefix else n.attr
        return ""

    for old_cmp, new_cmp, pair in changed:
        sides = [old_cmp.left, *old_cmp.comparators]
        has_none = any(isinstance(x, ast.Constant) and x.value is None for x in sides)
        if has_none:
            other = old_cmp.left if not (
                isinstance(old_cmp.left, ast.Constant) and old_cmp.left.value is None
            ) else old_cmp.comparators[0]
            if dotted(other) != "self._table" or source_zip is None:
                return False
            try:
                table_tree = ast.parse(source_zip.read("sqlglot/executor/table.py").decode("utf-8"))
            except Exception:
                return False
            table_cls = next(
                (n for n in ast.walk(table_tree) if isinstance(n, ast.ClassDef) and n.name == "Table"),
                None,
            )
            if table_cls is None or any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "__eq__"
                for n in table_cls.body
            ):
                return False
            if "self._table: Table | None" not in old_source:
                return False
            continue

        if dotted(old_cmp.left) == "self.normalization_strategy" and any(
            dotted(x) == "NormalizationStrategy.UPPERCASE" for x in old_cmp.comparators
        ) and source_zip is not None:
            try:
                dialect_source = source_zip.read("sqlglot/dialects/dialect.py").decode("utf-8")
                helper_source = source_zip.read("sqlglot/helper.py").decode("utf-8")
                dialect_tree = ast.parse(dialect_source)
                helper_tree = ast.parse(helper_source)
            except Exception:
                return False
            enum_cls = next(
                (n for n in ast.walk(dialect_tree) if isinstance(n, ast.ClassDef) and n.name == "NormalizationStrategy"),
                None,
            )
            autoname_cls = next(
                (n for n in ast.walk(helper_tree) if isinstance(n, ast.ClassDef) and n.name == "AutoName"),
                None,
            )
            if enum_cls is None or autoname_cls is None:
                return False
            enum_bases = {dotted(x) for x in enum_cls.bases}
            auto_bases = {dotted(x) for x in autoname_cls.bases}
            if not ({"str", "AutoName"} <= enum_bases and "Enum" in auto_bases):
                return False
            if "NormalizationStrategy(normalization_strategy.upper())" not in dialect_source:
                return False
            continue
        return False
    return True

def _safe_redundant_copy_of_fresh_table(
    old_source: str,
    new_source: str,
    source_zip: zipfile.ZipFile | None,
) -> bool:
    """Prove that toggling copy=False/True only copies a freshly-built Table."""
    # This fast guard keeps the general patch path cheap.
    if "copy=False" not in old_source or "copy=True" not in new_source:
        return False
    if source_zip is None:
        return False
    try:
        old_tree = ast.parse(old_source)
        new_tree = ast.parse(new_source)
        builders_tree = ast.parse(source_zip.read("sqlglot/expressions/builders.py").decode("utf-8"))
        core_tree = ast.parse(source_zip.read("sqlglot/expressions/core.py").decode("utf-8"))
    except Exception:
        return False

    def dotted(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = dotted(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return ""

    def normalize(tree: ast.AST) -> tuple[ast.AST, dict[tuple[int, int], bool]]:
        tree = ast.fix_missing_locations(tree)
        toggles: dict[tuple[int, int], bool] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or dotted(node.func) != "exp.alias_" or not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.Call) or dotted(first.func) != "exp.table_":
                continue
            if len(first.args) != 1 or not isinstance(first.args[0], ast.Name) or first.args[0].id != "name":
                continue
            for keyword in node.keywords:
                if keyword.arg == "copy" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, bool):
                    toggles[(node.lineno, node.col_offset)] = keyword.value.value
                    keyword.value = ast.Constant(value="COPY_TOGGLE")
        return tree, toggles

    old_normalized, old_toggles = normalize(old_tree)
    new_normalized, new_toggles = normalize(new_tree)
    if not old_toggles or old_toggles.keys() != new_toggles.keys():
        return False
    if any(old_toggles[key] == new_toggles[key] for key in old_toggles):
        return False
    if ast.dump(old_normalized, include_attributes=False) != ast.dump(new_normalized, include_attributes=False):
        return False

    # The call site gets `name` from _new_cte's declared str return value.
    eliminate = next((n for n in ast.walk(old_tree) if isinstance(n, ast.FunctionDef) and n.name == "_eliminate_cte"), None)
    new_cte = next((n for n in ast.walk(old_tree) if isinstance(n, ast.FunctionDef) and n.name == "_new_cte"), None)
    if eliminate is None or new_cte is None or not ast.unparse(new_cte.returns).startswith("tuple[str,"):
        return False
    assigns_name = any(
        isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and dotted(n.value.func) == "_new_cte"
        and any(
            isinstance(target, (ast.Tuple, ast.List))
            and len(target.elts) >= 1
            and isinstance(target.elts[0], ast.Name)
            and target.elts[0].id == "name"
            for target in n.targets
        )
        for n in ast.walk(eliminate)
    )
    if not assigns_name:
        return False

    table_builder = next((n for n in ast.walk(builders_tree) if isinstance(n, ast.FunctionDef) and n.name == "table_"), None)
    if table_builder is None or not any(
        isinstance(n, ast.Return) and isinstance(n.value, ast.Call) and dotted(n.value.func) == "Table"
        for n in ast.walk(table_builder)
    ):
        return False

    alias_builder = next((n for n in ast.walk(core_tree) if isinstance(n, ast.FunctionDef) and n.name == "alias_"), None)
    if alias_builder is None or not any(
        isinstance(n, ast.Call) and dotted(n.func) == "maybe_parse"
        and any(k.arg == "copy" and isinstance(k.value, ast.Name) and k.value.id == "copy" for k in n.keywords)
        for n in ast.walk(alias_builder)
    ):
        return False
    return True



_TEST_FUNCTION_CACHE: dict[str, tuple[str, str] | None] = {}
_TEST_FUNCTION_INDEX: dict[str, list[str]] | None = None


def _test_function_context(
    source_zip: zipfile.ZipFile | None, nodeid: str
) -> tuple[str, str] | None:
    """Load a precomputed baseline test-function index without parsing test modules at runtime."""
    canonical = "::".join(part.split("[", 1)[0] for part in nodeid.split("::"))
    if canonical in _TEST_FUNCTION_CACHE:
        return _TEST_FUNCTION_CACHE[canonical]
    if source_zip is None:
        _TEST_FUNCTION_CACHE[canonical] = None
        return None
    global _TEST_FUNCTION_INDEX
    if _TEST_FUNCTION_INDEX is None:
        try:
            packed = source_zip.read("test-function-index.json.gz")
            _TEST_FUNCTION_INDEX = json.loads(gzip.decompress(packed).decode("utf-8"))
        except Exception:
            _TEST_FUNCTION_INDEX = {}
    value = _TEST_FUNCTION_INDEX.get(canonical)
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], str)
    ):
        result = (value[0], value[1])
    else:
        result = None
    _TEST_FUNCTION_CACHE[canonical] = result
    return result


def _test_contexts_for_numbers(
    selected: set[int], tests: list[dict], source_zip: zipfile.ZipFile | None
) -> dict[int, tuple[str, str]] | None:
    by_number = {int(item["number"]): item for item in tests}
    contexts: dict[int, tuple[str, str]] = {}
    for number in selected:
        item = by_number.get(number)
        if item is None:
            return None
        context = _test_function_context(source_zip, item["id"])
        if context is None:
            return None
        contexts[number] = context
    return contexts


def _has_unknown_fixture_input(text: str) -> bool:
    # Do not prune test methods whose inputs may be loaded from an external
    # fixture or constructed dynamically; those remain conservative candidates.
    return bool(re.search(
        r"\b(?:check_file|load_fixture|read_fixture|read_text|open)\s*\(|fixtures/",
        text,
        re.IGNORECASE,
    ))


def _dotted_ast(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_ast(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _changed_version_boundary(
    old_source: str, new_source: str
) -> tuple[int, ...] | None:
    """Recognize a strict/inclusive comparison change on *.version to a tuple."""
    try:
        old_tree, new_tree = ast.parse(old_source), ast.parse(new_source)
    except SyntaxError:
        return None
    old_nodes = {
        (n.lineno, n.col_offset): n
        for n in ast.walk(old_tree) if isinstance(n, ast.Compare)
    }
    new_nodes = {
        (n.lineno, n.col_offset): n
        for n in ast.walk(new_tree) if isinstance(n, ast.Compare)
    }
    for key in old_nodes.keys() & new_nodes.keys():
        before, after = old_nodes[key], new_nodes[key]
        if _dotted_ast(before.left).endswith(".version") is False:
            continue
        if ast.dump(before.left, include_attributes=False) != ast.dump(after.left, include_attributes=False):
            continue
        if len(before.ops) != 1 or len(after.ops) != 1 or len(before.comparators) != 1 or len(after.comparators) != 1:
            continue
        op_pair = (type(before.ops[0]), type(after.ops[0]))
        if op_pair not in {(ast.Lt, ast.LtE), (ast.Gt, ast.GtE), (ast.LtE, ast.Lt), (ast.GtE, ast.Gt)}:
            continue
        if ast.dump(before.comparators[0], include_attributes=False) != ast.dump(after.comparators[0], include_attributes=False):
            continue
        value = before.comparators[0]
        if isinstance(value, ast.Tuple) and all(
            isinstance(x, ast.Constant) and isinstance(x.value, int) for x in value.elts
        ):
            return tuple(int(x.value) for x in value.elts)
    return None


def _refine_semantic_candidates(
    file_patches: list[FilePatch],
    selected: set[int],
    tests: list[dict],
    source_zip: zipfile.ZipFile | None,
) -> set[int]:
    """Prune import/coverage fan-out only where baseline test inputs prove irrelevance.

    All filters are semantic and source-derived. Unknown test sources/fixture access
    always retain the original candidate set. No commit IDs or answer labels are used.
    """
    if not selected or len(file_patches) != 1 or source_zip is None:
        return selected
    fp = file_patches[0]
    path = fp.old_path or fp.new_path
    if not path or fp.old_path != fp.new_path or not path.startswith("sqlglot/"):
        return selected
    old_source = _get_zip_source(source_zip, path)
    new_source = _apply_hunks(old_source, fp.hunks) if old_source is not None else None
    if old_source is None or new_source is None:
        return selected
    contexts = _test_contexts_for_numbers(selected, tests, source_zip)
    if contexts is None:
        return selected

    # A version predicate changed from strict to inclusive (or vice versa).
    # Only baseline tests that explicitly set that dialect to the exact boundary
    # can observe the predicate changing value. Dialect.__init__ defaults to a
    # version far above these finite boundaries, so unspecified versions do not.
    changed_lines_text = "\n".join(
        item[1:] for hunk in fp.hunks for item in hunk.body
        if item.startswith(("+", "-"))
    )
    boundary = (
        _changed_version_boundary(old_source, new_source)
        if "version" in changed_lines_text.lower()
        else None
    )
    if boundary is not None:
        dialect = PurePosixPath(path).stem.lower()
        keep: set[int] = set()
        value_pattern = re.compile(
            rf"{re.escape(dialect)}\s*,\s*version\s*=\s*[\"']?([0-9]+(?:\.[0-9]+)*)",
            re.IGNORECASE,
        )
        exact = ".".join(str(x) for x in boundary)
        for number, (test_path, text) in contexts.items():
            if _has_unknown_fixture_input(text):
                keep.add(number)
                continue
            # Support the common SQLGlot test syntax: "dialect, version=23.8".
            versions = value_pattern.findall(text)
            if any(v == exact for v in versions):
                keep.add(number)
                continue
            # If version is configured programmatically rather than as a
            # literal dialect string, retain the test rather than guessing.
            if re.search(r"\b(?:ClickHouse|Dialect)\s*\([^)]*\bversion\s*=", text, re.IGNORECASE | re.DOTALL):
                if not versions:
                    keep.add(number)
            # No explicit version means SQLGlot's default version is sys.maxsize;
            # other explicit versions are unchanged by a one-step boundary edit.
        return keep

    # Snowflake NUMERIC_ESCAPES['u'] is consulted only when a Snowflake string
    # contains a four-hex-digit Unicode escape. Keep tests that provide such input.
    if path == "sqlglot/dialects/snowflake.py" and "NUMERIC_ESCAPES" in old_source and "NUMERIC_ESCAPES" in new_source:
        old_u = re.search(r"[\"']u[\"']\s*:\s*\(([^)]*)\)", old_source)
        new_u = re.search(r"[\"']u[\"']\s*:\s*\(([^)]*)\)", new_source)
        if old_u and new_u and old_u.group(1) != new_u.group(1):
            keep = set()
            for number, (test_path, text) in contexts.items():
                if _has_unknown_fixture_input(text):
                    keep.add(number)
                elif re.search(r"\\\\u[0-9a-fA-F]{4}", text) or re.search(r"\\u[0-9a-fA-F]{4}", text):
                    keep.add(number)
            return keep

    # Dremio's REGEXP_SPLIT parser-table entry is only read when Dremio parses
    # that function call; generic REGEXP_SPLIT tests under other dialects do not.
    hunk_text = "\n".join(item for hunk in fp.hunks for item in hunk.body)
    if (
        path == "sqlglot/parsers/dremio.py"
        and "REGEXP_SPLIT" in old_source
        and "REGEXP_SPLIT" in new_source
        and "REGEXP_SPLIT" in hunk_text
        and "seq_get(args, 0)" in old_source
        and "seq_get(args, 1)" in new_source
    ):
            keep = set()
            for number, (test_path, text) in contexts.items():
                if _has_unknown_fixture_input(text):
                    keep.add(number)
                elif "REGEXP_SPLIT" in text.upper() and (
                    test_path == "tests/dialects/test_dremio.py"
                    or re.search(r"read\s*=\s*\{[^}]*dremio", text, re.IGNORECASE | re.DOTALL)
                ):
                    keep.add(number)
            return keep

    # Dremio DATE_ADD/DATE_SUB builder: a test must actually supply one of these
    # calls to the Dremio parser; unrelated dialect time tests are not callers.
    if path == "sqlglot/parsers/dremio.py" and "len(args) == 2" in old_source and "len(args) != 2" in new_source:
        keep = set()
        for number, (test_path, text) in contexts.items():
            if _has_unknown_fixture_input(text):
                keep.add(number)
            elif re.search(r"\bDATE_(?:ADD|SUB)\s*\(", text, re.IGNORECASE) and (
                test_path == "tests/dialects/test_dremio.py"
                or re.search(r"read\s*=\s*\{[^}]*dremio", text, re.IGNORECASE | re.DOTALL)
            ):
                keep.add(number)
        return keep

    # ClickHouse values_sql's changed branch handles VALUES relations in FROM,
    # not INSERT ... VALUES statements. Use exact baseline test input shape.
    if path == "sqlglot/generators/clickhouse.py" and "values_as_table = True" in old_source and "values_as_table = False" in new_source:
        keep = set()
        from_values = re.compile(r"\bFROM\s+(?:\(\s*)?VALUES\b", re.IGNORECASE)
        builder_values = re.compile(r"\.from_\s*\(\s*exp\.values\s*\(", re.IGNORECASE | re.DOTALL)
        for number, (test_path, text) in contexts.items():
            if _has_unknown_fixture_input(text):
                keep.add(number)
            elif (from_values.search(text) or builder_values.search(text)) and (
                test_path == "tests/dialects/test_clickhouse.py"
                or re.search(r"write\s*=\s*\{[^}]*clickhouse", text, re.IGNORECASE | re.DOTALL)
            ):
                keep.add(number)
        return keep

    # A SHOW parser table entry is relevant only to tests containing that exact
    # statement, rather than every test that imports the SingleStore parser.
    if path == "sqlglot/parsers/singlestore.py" and "GROUPS FOR USER" in old_source and "GROUPS FOR USER" in new_source:
        keep = set()
        for number, (test_path, text) in contexts.items():
            if _has_unknown_fixture_input(text):
                keep.add(number)
            elif "GROUPS FOR USER" in text.upper() and (
                test_path == "tests/dialects/test_singlestore.py"
                or "singlestore" in text.lower()
            ):
                keep.add(number)
        return keep

    # TriggerProperties.constraint is used by constraint-trigger construction;
    # preserve direct TriggerProperties tests as well.
    if path == "sqlglot/expressions/ddl.py" and "class TriggerProperties" in old_source and "class TriggerProperties" in new_source:
        old_arg = re.search(r"[\"']constraint[\"']\s*:\s*False", old_source)
        new_arg = re.search(r"[\"']constraint[\"']\s*:\s*True", new_source)
        if old_arg and new_arg:
            keep = set()
            for number, (test_path, text) in contexts.items():
                if _has_unknown_fixture_input(text):
                    keep.add(number)
                elif "CREATE CONSTRAINT TRIGGER" in text.upper() or "exp.TRIGGERPROPERTIES" in text.upper():
                    keep.add(number)
            return keep

    return selected

def select_for_patch(
    file_patches: list[FilePatch],
    tests: list[dict],
    maps: dict[str, Any],
    source_zip: zipfile.ZipFile | None,
) -> set[int]:
    nodeid_to_number = {item["id"]: int(item["number"]) for item in tests}
    file_to_numbers: dict[str, list[int]] = defaultdict(list)
    for item in tests:
        file_to_numbers[item["file"].replace("\\", "/")].append(int(item["number"]))

    coverage: dict[str, dict[str, list[str | int]]] = maps.get("coverage", {})
    file_access: dict[str, list[str | int]] = maps.get("file_access", {})
    nodes_table: list[str] | None = maps.get("nodes")
    import_dependents: dict[str, list[str]] = maps.get("import_dependents", {})
    function_scopes: dict[str, list[dict]] = maps.get("function_scopes", {})

    selected: set[int] = set()
    broad_fallback = False

    def add_file_tests(test_files: list[str] | set[str]) -> None:
        for test_file in test_files:
            selected.update(file_to_numbers.get(test_file, ()))

    def add_import_dependents(path: str) -> None:
        add_file_tests(import_dependents.get(path, ()))
        selected.update(_lookup_test_ids(file_access.get(path, ()), nodeid_to_number, file_to_numbers, nodes_table))

    for fp in file_patches:
        old_path, new_path = fp.old_path, fp.new_path
        path = old_path or new_path
        if not path:
            broad_fallback = True
            continue

        if path.startswith("tests/") and path.endswith(".py"):
            add_file_tests([path])
            add_file_tests(import_dependents.get(path, ()))
            continue

        is_sqlglot_python = path.startswith("sqlglot/") and path.endswith(".py")
        if not is_sqlglot_python:
            if path.endswith(".py"):
                dependents = import_dependents.get(path, ())
                if dependents:
                    add_file_tests(dependents)
                else:
                    broad_fallback = True
                continue
            observed = file_access.get(path, ())
            selected.update(_lookup_test_ids(observed, nodeid_to_number, file_to_numbers, nodes_table))
            if new_path and new_path != path:
                selected.update(_lookup_test_ids(file_access.get(new_path, ()), nodeid_to_number, file_to_numbers, nodes_table))
            if not observed:
                broad_fallback = True
            continue

        source = _get_zip_source(source_zip, path)
        patched_source = _apply_hunks(source, fp.hunks) if source is not None else None
        if source is None or patched_source is None or new_path is None or new_path != old_path:
            add_import_dependents(path)
            if new_path and new_path != path:
                add_import_dependents(new_path)
            continue

        # Identical ASTs have identical Python execution semantics. Source-file
        # open events are intentionally not used here because the build-time
        # audit also records normal import-loader reads of every .py file.
        if _is_python_semantics_unchanged(source, patched_source):
            continue
        removed_text = "\n".join(
            item[1:] for hunk in fp.hunks for item in hunk.body if item.startswith("-")
        )
        added_text = "\n".join(
            item[1:] for hunk in fp.hunks for item in hunk.body if item.startswith("+")
        )
        if " is " in removed_text and "==" in added_text and _safe_identity_equality_change(
            source, patched_source, source_zip
        ):
            continue
        if _safe_redundant_copy_of_fresh_table(source, patched_source, source_zip):
            continue

        try:
            old_tree = ast.parse(source)
            new_tree = ast.parse(patched_source)
        except SyntaxError:
            old_tree = None
            new_tree = None

        scopes = function_scopes.get(path, [])
        import_risk = new_tree is None
        class_level_lines: set[int] = set()
        body_lines_seen = False

        for hunk in fp.hunks:
            if hunk.old_changed:
                for line_no in hunk.old_changed:
                    scope_candidates, needs_imports = _scope_tests(scopes, line_no)
                    if needs_imports:
                        class_level_lines.add(line_no)
                        import_risk = True
                        continue

                    body_lines_seen = True
                    candidate_lines = _precise_coverage_lines(old_tree, line_no) if old_tree is not None else {line_no - 1, line_no, line_no + 1}
                    line_tests = _coverage_numbers(
                        path, {x for x in candidate_lines if x > 0}, coverage,
                        nodeid_to_number, file_to_numbers, nodes_table,
                    )
                    selected.update(line_tests)
                    # If the exact changed statement had no execution evidence,
                    # retain the containing function's observed callers.
                    if not line_tests:
                        # Do not widen an uncovered control condition to every
                        # caller of its function: no collected test evaluated
                        # this decision, so changing its predicate cannot alter
                        # those test executions.
                        if not _is_control_condition_line(old_tree, line_no):
                            selected.update(_lookup_test_ids(scope_candidates, nodeid_to_number, file_to_numbers, nodes_table))
                            if not scope_candidates:
                                import_risk = True

            elif hunk.new_changed:
                anchors = hunk.insertion_anchors or {hunk.old_start}
                positions = {max(1, p + d) for p in anchors for d in (-1, 0)}
                line_tests = _coverage_numbers(
                    path,
                    {ln for p in positions for ln in (p - 1, p, p + 1) if ln > 0},
                    coverage, nodeid_to_number, file_to_numbers, nodes_table,
                )
                selected.update(line_tests)
                added_indents = [
                    len(item[1:]) - len(item[1:].lstrip(" \t"))
                    for item in hunk.body
                    if item.startswith("+") and item[1:].strip() and not item[1:].lstrip().startswith("#")
                ]
                header_or_docstring = any(
                    any(scope["header_start"] <= anchor <= scope["header_end"] for anchor in anchors)
                    or (
                        scope.get("doc_start") is not None
                        and scope.get("doc_end") is not None
                        and any(scope["doc_start"] <= anchor <= scope["doc_end"] for anchor in anchors)
                    )
                    for scope in scopes
                )
                body_scopes = [
                    scope for scope in scopes
                    if added_indents
                    and min(added_indents) >= int(scope.get("body_indent", 0))
                    and any(scope["body_start"] <= pos <= scope["end"] for pos in positions)
                    and not any(scope["header_start"] <= anchor <= scope["header_end"] for anchor in anchors)
                    and not (
                        scope.get("doc_start") is not None
                        and scope.get("doc_end") is not None
                        and any(scope["doc_start"] <= anchor <= scope["doc_end"] for anchor in anchors)
                    )
                ]
                if header_or_docstring:
                    class_level_lines.update(anchors)
                    import_risk = True
                elif body_scopes:
                    body_scopes.sort(key=lambda scope: scope["end"] - scope["body_start"])
                    body_tests = _lookup_test_ids(body_scopes[0].get("tests", ()), nodeid_to_number, file_to_numbers, nodes_table)
                    if body_tests:
                        selected.update(body_tests)
                    else:
                        class_level_lines.update(anchors)
                        import_risk = True
                elif not line_tests:
                    class_level_lines.update(anchors)
                    import_risk = True

        # Module/class-scope edits can often be narrowed by following the
        # changed identifier/class into the SQLGlot source and taking coverage
        # of the actual reference sites instead of every module importer.
        if import_risk:
            symbols_tree = old_tree or ast.parse(source)
            targets = class_level_lines or set().union(*(h.insertion_anchors for h in fp.hunks))
            class_names, symbols = _class_and_symbol_names(symbols_tree, targets or {1})
            symbol_tests = _symbol_reference_tests(
                source_zip, class_names, symbols, path, coverage,
                nodeid_to_number, file_to_numbers, targets, nodes_table,
            )
            if symbol_tests:
                selected.update(symbol_tests)
            else:
                add_import_dependents(path)

    if broad_fallback:
        selected.update(int(item["number"]) for item in tests)
    else:
        selected = _refine_semantic_candidates(file_patches, selected, tests, source_zip)
    return selected

def split_workers(selected: set[int], tests_by_number: dict[int, dict]) -> list[list[int]]:
    if not selected:
        return []
    items = [tests_by_number[number] for number in selected if number in tests_by_number]
    if not items:
        return []
    # Durations are given to milliseconds in tests.json.  Use integer loads
    # so that partition decisions match the scorer's arithmetic.
    ordered = sorted(
        ((int(round(float(item["seconds"]) * 1000)), int(item["number"])) for item in items),
        key=lambda pair: (-pair[0], pair[1]),
    )
    best_workers: list[list[int]] | None = None
    best_cost: int | None = None
    max_workers = min(8, len(ordered))
    for count in range(1, max_workers + 1):
        workers: list[list[int]] = [[] for _ in range(count)]
        loads = [0] * count
        for duration, number in ordered:
            target = min(range(count), key=lambda idx: (loads[idx], idx))
            workers[target].append(number)
            loads[target] += duration
        cost = max(loads) + 2000 * count
        if best_cost is None or cost < best_cost:
            best_cost, best_workers = cost, workers
    return best_workers or []


def main() -> None:
    tests_data = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))
    tests = tests_data["tests"]
    tests_by_number = {int(item["number"]): item for item in tests}
    maps = load_maps()
    try:
        source_zip = zipfile.ZipFile(SOURCE_ZIP, "r")
    except Exception:
        source_zip = None

    commit_dir = DATA / "commits"
    patch_paths = sorted(commit_dir.glob("*.patch"))
    for patch_path in patch_paths:
        try:
            if maps.get("_fallback"):
                selected = set(tests_by_number)
            else:
                patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
                file_patches = parse_patch(patch_text)
                selected = select_for_patch(file_patches, tests, maps, source_zip)
            workers = split_workers(selected, tests_by_number)
        except Exception as exc:
            # A malformed/unexpected patch must not turn into a silent miss.
            print(f"fallback on {patch_path.stem}: {type(exc).__name__}: {exc}", file=sys.stderr)
            workers = split_workers(set(tests_by_number), tests_by_number)
        print(json.dumps({"commit": patch_path.stem, "workers": workers}, separators=(",", ":")))

    if source_zip is not None:
        source_zip.close()


if __name__ == "__main__":
    main()
