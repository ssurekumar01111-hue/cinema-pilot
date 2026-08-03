"""
infra/cleanup_orphaned_props.py

One-time cleanup: delete the 10 orphaned prop rows left in
cinemapilot-2026.production_graph.props after the first (pre-normalization)
ingest run. These rows were created with non-canonical slugs (e.g.
prop_battered_transistor_radio, prop_broken_office_chair) that were never
referenced by any scene and will never be updated by future ingestion runs.

Run once with the venv activated:
    python infra/cleanup_orphaned_props.py

Safe to re-run: a second execution will report 0 rows deleted.
"""

import sys
from pathlib import Path

# Resolve repo root for imports
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from google.cloud import bigquery

PROJECT   = "cinemapilot-2026"
DATASET   = "production_graph"
TABLE     = f"`{PROJECT}.{DATASET}.props`"

# The exact orphaned prop_ids created by the first (pre-normalization) run.
# These all have version=1 and were never updated by subsequent runs because
# their slugs no longer match any canonical prop name.
ORPHANED_IDS: list[str] = [
    "prop_bare_bulb",
    "prop_battered_transistor_radio",
    "prop_black_suv",
    "prop_broken_office_chair",
    "prop_burner_phone",
    "prop_frequency_printouts",
    "prop_granola_bars",
    "prop_motivational_poster_hang_in_there_kitten_on_a_branch",
    "prop_printed_image_of_the_observatory",
    "prop_rolled_up_rug",
]

DELETE_SQL = f"""
DELETE FROM {TABLE}
WHERE prop_id IN UNNEST(@orphaned_ids)
  AND version = 1
"""
# The `AND version = 1` guard is belt-and-suspenders: if any of these IDs
# somehow got a legitimate write later (version > 1), we skip them.

COUNT_SQL = f"SELECT COUNT(*) AS total FROM {TABLE}"


def main() -> None:
    print("=" * 60)
    print("  Orphaned prop cleanup — cinemapilot-2026.production_graph")
    print("=" * 60)

    client = bigquery.Client(project=PROJECT)

    # Count before
    before = list(client.query(COUNT_SQL).result())[0]["total"]
    print(f"\n  Prop rows before cleanup: {before}")
    print(f"  Targeting {len(ORPHANED_IDS)} orphaned ID(s):")
    for oid in ORPHANED_IDS:
        print(f"    - {oid}")

    # Delete
    print("\n  Executing DELETE...")
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("orphaned_ids", "STRING", ORPHANED_IDS)
        ]
    )
    job = client.query(DELETE_SQL, job_config=cfg)
    job.result()
    deleted = job.num_dml_affected_rows
    print(f"  Deleted: {deleted} row(s)")

    # Count after
    after = list(client.query(COUNT_SQL).result())[0]["total"]
    print(f"  Prop rows after cleanup:  {after}")

    # Remaining rows
    rows = list(client.query(
        f"SELECT prop_id, name, version FROM {TABLE} ORDER BY name ASC"
    ).result())

    print(f"\n  Remaining props ({len(rows)}):")
    for r in rows:
        print(f"    v{r['version']}  {r['prop_id']:<55}  {r['name']}")

    if deleted == len(ORPHANED_IDS):
        print(f"\n  SUCCESS: All {deleted} orphaned rows deleted.")
    elif deleted == 0:
        print("\n  NOTE: 0 rows deleted — cleanup may have already run.")
    else:
        print(f"\n  PARTIAL: {deleted}/{len(ORPHANED_IDS)} rows deleted.")

    print("=" * 60)


if __name__ == "__main__":
    main()
