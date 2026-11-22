# Blast Radius — updated candidate (source-aware)

This folder is a replacement runtime package for the Blast Radius challenge.

## Practice-set validation

The selector was run against the supplied 30-commit practice package using the supplied scoring script:

- Simulated score: **101.632 seconds**
- Missed failing tests: **0**
- Commits: **30**

This is a local practice result, not an official hidden-set or leaderboard result. It is 0.846 seconds slower on the practice set than the earlier 100.786-second version. The main reason to prefer this candidate is that it removes the earlier hand-written dialect/edit-specific test filters and adds a generic fixture-directory fallback. Whether it improves the hidden-set score has not been established.

## Runtime files

- `Dockerfile` — minimal Python 3.13 runtime image.
- `selector.py` — reads `/data/tests.json` and `/data/commits/*.patch`; writes one JSON line per commit to stdout.
- `maps.json.gz` — baseline-derived coverage, imports, fixture-access and function-scope maps.
- `base-source.zip` — pinned SQLGlot source and baseline-derived indexes.

`build_maps.py` and `compact_maps.py` are included as optional reproducibility utilities; they are not needed at runtime.

## What changed from the previous submission

1. Removed explicit dialect/edit-specific filtering rules that narrowed selected tests based on particular identifiers and dialect paths.
2. Changed Python unchanged-file detection to compare parsed ASTs including type comments.
3. For an unseen non-Python fixture under `tests/fixtures/`, use baseline fixture-access evidence from the nearest observed parent directory instead of immediately selecting the entire suite. Unfamiliar paths outside a known fixture tree still use the conservative full-suite fallback.
4. Retained up to eight workers and the duration-based load-balancing routine.
5. Kept two generic source-aware refinements: detection of a changed strict/inclusive `*.version` boundary and changed one-character tuple-mapping keys. They compare the actual changed AST values with the unmodified baseline test-function source index; they do not use commit IDs, answer files, sample patch IDs, or a precomputed patch-to-test table.

## Challenge-rule transparency

`test-function-index.json.gz` inside `base-source.zip` contains test-function source extracted from the pinned, unmodified baseline. The selector reads it only for the two generic source-aware refinements listed above. This is not a table mapping patch types or patch IDs to failing tests. However, the challenge text explicitly names coverage, imports and fixtures as examples of permitted baseline-derived maps; it does not expressly discuss a test-function-source index. I have not received an organizer ruling on that distinction. The companion conservative package avoids this index entirely if you prefer the narrower interpretation.

## Install in the existing project

From the existing project directory on macOS, back up the runtime files and extract this ZIP into the project root:

```sh
cd ~/Downloads/BlastRadius
cp selector.py selector.before-updated.py
cp maps.json.gz maps.before-updated.json.gz
cp base-source.zip base-source.before-updated.zip
cp Dockerfile Dockerfile.before-updated
unzip -o ~/Downloads/blast-radius-updated-source-aware.zip -d .
```

Then run the project's existing local sample command:

```sh
make sample
```

Expected simulated result is approximately **101.632 seconds with 0 misses**, plus the local runtime CPU time. Do not submit unless the command completes successfully and reports zero misses.

## Commit and submit

Review the changed files, then commit only the runtime and its supporting source/build files. Do not add `sample/answers.json` or other practice answer material to the public repository.

```sh
git status
git add Dockerfile selector.py maps.json.gz base-source.zip README.md LICENSE build_maps.py compact_maps.py
git commit -m "Refine baseline-driven test selection"
git push
git rev-parse HEAD
```

Submit the public repository URL and the full SHA printed by `git rev-parse HEAD` on the challenge page. Keep the previous submitted commit intact.

## Limitations

- The hidden final patches are unavailable to this local test, so no new official score can be predicted or guaranteed.
- The Docker image was not built in this preparation environment because Docker is unavailable here. The selector itself was run with Python 3.13 against all 30 practice commits and scored with the supplied scorer.
