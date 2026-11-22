Blast Radius optimized selector kit
===================================

Verified against the supplied 30-commit sample:
  Simulated score: 108.408 seconds
  Misses:          0

The map is version 2 (integer-indexed references), compressed to about 751 KB.
The selector accepts both version 1 and version 2 maps. The sample output is
identical with the original version 1 map and this compact version 2 map.

Install on macOS
----------------
1. In Terminal, go to your existing project folder:
     cd ~/Downloads/BlastRadius

2. Make backups before replacing anything:
     cp selector.py selector.before-optimized.py
     cp maps.json.gz maps.before-optimized.json.gz

3. Download blast-radius-optimized-kit.zip to your Downloads folder, then run:
     unzip -o ~/Downloads/blast-radius-optimized-kit.zip -d ~/Downloads/BlastRadius

   This replaces only selector.py and maps.json.gz, and adds compact_maps.py.
   It does not replace your Dockerfile, Makefile, requirements, sample data, or
   base-source.zip.

4. Run the sample:
     make sample

Dockerfile compatibility
------------------------
The existing runtime Dockerfile must copy selector.py and maps.json.gz into
/app, and base-source.zip must remain at /app/base-source.zip. The selector's
default paths are /app/maps.json.gz and /app/base-source.zip, matching that
layout. No DuckDB or pytest dependency is needed at selector runtime.

If maps are regenerated later
-----------------------------
If the builder produces a fresh version 1 maps.json.gz, convert it with:
     python3 compact_maps.py maps.json.gz maps.v2.json.gz
     mv maps.v2.json.gz maps.json.gz

Important
---------
108.408 seconds is the local simulated sample score, not a guarantee of the
hidden final score. The supplied sample was checked with zero missed failing
tests. CPU time is additional on the leaderboard and can vary by machine.
