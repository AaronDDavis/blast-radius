# Blast Radius — optimized selector kit

## Verified sample result

- Simulated score: **100.786 seconds**
- Missed failing tests: **0**
- Sample commits: **30**
- Local selector CPU: median about **1.72 seconds** across three local runs (user + system CPU; hardware-dependent)

The simulated score is produced by the supplied sample scorer. The challenge leaderboard also adds container CPU time, so the final displayed score can vary by machine. This package has been tested against the local sample data; it has not been run against the hidden final patches or the remote leaderboard.

## Files

- `selector.py` — runtime selector; standard-library Python only.
- `maps.json.gz` — compact version-2 baseline coverage/import/file-access map.
- `base-source.zip` — pinned baseline SQLGlot source plus two baseline-derived indexes:
  - `test-function-index.json.gz` contains 1,621 test function bodies extracted from the pinned upstream test suite.
  - `sqlglot-identifier-index.marshal.gz` maps SQLGlot identifiers to source lines for faster symbol lookups.
- `build_maps.py` — updated builder that recreates both indexes from the pinned baseline checkout.
- `compact_maps.py` — helper for compacting a version-1 JSON coverage map to version 2.

The selector does not read `answers.json`, sample patch IDs, or any failing-test lookup table. Its additional narrowing uses source-level semantics and the test functions from the unmodified pinned baseline. Unknown test-source entries and fixture-driven cases are retained conservatively.

## Install

From the project folder, back up the three runtime files and extract this ZIP into the project root:

```sh
cp selector.py selector.before-100s.py
cp maps.json.gz maps.before-100s.json.gz
cp base-source.zip base-source.before-100s.zip
unzip -o ~/Downloads/blast-radius-100s-kit.zip -d .
```

Then run:

```sh
make sample
```

The top-level runtime `Dockerfile` should copy `selector.py`, `maps.json.gz`, and `base-source.zip` into `/app`. No dependency changes are required. The existing `requirements.lock`, `Makefile`, sample data, and runtime Dockerfile can remain unchanged.

If rebuilding the maps from the pinned checkout, use the included `build_maps.py` so the test-function and identifier indexes are regenerated together with `base-source.zip`. Then run the existing compaction step if your runtime expects a version-2 map.
