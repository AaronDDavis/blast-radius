"""Fetch exactly the sqlglot source revision permitted by the challenge."""

from __future__ import annotations

import io
import os
import tarfile
import urllib.request
from pathlib import Path

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{COMMIT}"
DEST = Path("/opt/sqlglot")


def main() -> None:
    request = urllib.request.Request(URL, headers={"User-Agent": "blast-radius-build/1.0"})
    with urllib.request.urlopen(request, timeout=180) as response:
        archive = response.read()
    DEST.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getmembers()
        roots = {member.name.split("/", 1)[0] for member in members if member.name}
        if len(roots) != 1:
            raise RuntimeError("unexpected sqlglot archive layout")
        root = next(iter(roots))
        # GitHub's archive has a single top-level directory.  Extract only
        # members beneath it and reject absolute/traversal paths.
        safe = []
        for member in members:
            if not member.name.startswith(root + "/"):
                continue
            relative = member.name[len(root) + 1:]
            if not relative or relative.startswith("/") or ".." in Path(relative).parts:
                continue
            member.name = relative
            safe.append(member)
        tar.extractall(DEST, members=safe, filter="data")
    marker = DEST / "pyproject.toml"
    if not marker.is_file():
        raise RuntimeError("sqlglot checkout failed verification")
    print(f"fetched sqlglot at {COMMIT}")


if __name__ == "__main__":
    main()
