#!/usr/bin/env python3
"""Convert version-1 Blast Radius maps to integer-indexed version 2."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("usage: python3 compact_maps.py maps.json.gz [maps.v2.json.gz]", file=sys.stderr)
        return 2
    source = Path(sys.argv[1])
    target = Path(sys.argv[2]) if len(sys.argv) == 3 else source.with_name("maps.v2.json.gz")

    with gzip.open(source, "rt", encoding="utf-8") as stream:
        maps = json.load(stream)
    if maps.get("version") != 1:
        raise SystemExit("expected a version-1 map")

    references: set[str] = set()
    for lines in maps.get("coverage", {}).values():
        for refs in lines.values():
            references.update(refs)
    for refs in maps.get("file_access", {}).values():
        references.update(refs)
    for scopes in maps.get("function_scopes", {}).values():
        for scope in scopes:
            references.update(scope.get("tests", ()))

    nodes = sorted(references)
    index = {value: number for number, value in enumerate(nodes)}

    for lines in maps.get("coverage", {}).values():
        for line, refs in lines.items():
            lines[line] = [index[value] for value in refs]
    for path, refs in maps.get("file_access", {}).items():
        maps["file_access"][path] = [index[value] for value in refs]
    for scopes in maps.get("function_scopes", {}).values():
        for scope in scopes:
            if "tests" in scope:
                scope["tests"] = [index[value] for value in scope["tests"]]

    maps["version"] = 2
    maps["nodes"] = nodes
    with gzip.open(target, "wt", encoding="utf-8", compresslevel=6) as stream:
        json.dump(maps, stream, separators=(",", ":"), ensure_ascii=True)
    print(f"wrote {target} ({target.stat().st_size:,} bytes; {len(nodes):,} indexed references)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
