Synthetic test drawings for the cad-drawings skill
===================================================

Everything here is invented. No real project, client or vendor data is used, and no
drawing is ever committed: the DXF files are generated on demand into evals/fixtures/
(git-ignored).

Regenerate
----------
    python skills/cad-drawings/evals/make_fixtures.py --out <dir> [--large]

Requires ezdxf (>= 1.4.4). Without --out the files go to evals/fixtures/ next to the script.
--large also writes plan_large.dxf (about 30 000 entities, roughly 5 MB, a few seconds).

The output is deterministic: same arguments, byte-identical files (fixed seed, fixed
metadata, sequential handles, independent of PYTHONHASHSEED).

Files
-----
plan_cm_v1.dxf, plan_mm_v1.dxf  Same plan in centimetres ($INSUNITS=5) and millimetres
                                ($INSUNITS=4); the second has its origin shifted by a known vector.
sheet_set.dxf                   Model space plus layouts "Sheet-A" and "Sheet-B": title blocks made
                                of TEXT, one viewport each (Sheet-B: twist 90, one layer frozen in
                                that viewport only) and the word FIRE placed in six different ways.
mtext_cases.dxf                 MTEXT with inline codes, and MTEXT with two columns.
blocks_attribs.dxf              Block with attributes (several inserts) and an anonymous block.
hatch_assoc.dxf                 Associative hatch on a closed polyline, plus an unhatched decoy.
xref_missing.dxf                External reference to a file that does not exist.
sheet_set_v1.dxf, plan_v2.dxf   Base drawing and its re-saved, changed counterpart: three real
                                changes plus re-save noise (see changes.json).
plan_large.dxf                  Performance fixture (only with --large).

Ground truth
------------
truth.json    Per-file claims, keyed by file name under "files"; handles are strings.
changes.json  The v1 -> v2 comparison: "real_changes", declared "noise", "entity_map" (v1 handle
              to v2 handle for every surviving entity) and the full entity lists of both files.

Tests
-----
    python -m pytest skills/cad-drawings/tests -q

tests/test_fixtures.py regenerates everything into a temporary directory, re-reads the files
and checks every claim in truth.json. The large variant is marked "slow" and skipped unless
CAD_DRAWINGS_RUN_SLOW=1 is set.
tests/check_forbidden.py scans the repository for forbidden strings and vendor files.
