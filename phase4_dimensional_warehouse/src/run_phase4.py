#!/usr/bin/env python3
"""Load the finalized CS779 dimensional model from the completed Phase 3 state."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oracle_support import (
    calendar_bounds,
    database_preflight,
    desired_player_versions,
    fact_source_mapping_counts,
    lightweight_postload_check,
    load_date_dimension,
    load_match_participation_fact,
    load_player_season_snapshot_fact,
    load_time_dimension,
    load_type1_dimensions,
    sync_dim_player,
)
from phase4_common import connect, read_json, table_counts, write_json


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-config", type=Path, required=True)
    parser.add_argument(
        "--phase4-config",
        type=Path,
        default=root / "config" / "phase4_config.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "run_outputs" / "phase4_run_result.json",
    )
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    config = read_json(args.phase4_config.resolve())
    oracle_config = read_json(args.oracle_config.resolve())
    output_path = args.output.resolve()
    result: dict[str, Any] = {
        "phase": 4,
        "status": "STARTED",
        "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "transaction_policy": (
            "All dimensional MERGE/INSERT operations run in one Oracle transaction; "
            "any failure rolls back Phase 4 DML only. OLTP and staging are read-only."
        ),
    }
    connection = None
    try:
        ddl_path = root / "sql" / "00_authoritative_dimensional_ddl.sql"
        actual_ddl_hash = file_sha256(ddl_path)
        expected_ddl_hash = str(config["authoritative_dimensional_ddl_sha256"])
        result["ddl_identity"] = {
            "file": str(ddl_path.relative_to(root)),
            "actual_sha256": actual_ddl_hash,
            "expected_sha256": expected_ddl_hash,
            "status": "PASS" if actual_ddl_hash == expected_ddl_hash else "FAIL",
        }
        if actual_ddl_hash != expected_ddl_hash:
            raise RuntimeError("Packaged dimensional DDL differs from corrected Phase 1")

        connection = connect(oracle_config)
        preflight = database_preflight(connection, config)
        result["database_preflight"] = preflight
        if preflight["status"] != "PASS":
            raise RuntimeError(
                "Oracle preflight failed; no Phase 4 DML was committed"
            )

        before = table_counts(connection)
        result["warehouse_counts_before"] = before

        calendar_start, calendar_end = calendar_bounds(connection)
        load_type1_dimensions(connection)
        load_date_dimension(connection, calendar_start, calendar_end)
        load_time_dimension(connection)

        desired_versions, effective_policy = desired_player_versions(
            connection, config
        )
        result["effective_dating_policy"] = effective_policy
        result["dim_player_dml"] = sync_dim_player(connection, desired_versions)

        mappings = fact_source_mapping_counts(connection, config)
        result["pre_fact_dimension_resolution"] = mappings
        if (
            mappings["participation_source_rows"]
            != mappings["participation_rows_with_one_dimension_path"]
        ):
            raise RuntimeError(
                "Not every loaded participation resolves to exactly one dimension path"
            )
        if (
            mappings["ranking_source_rows"]
            != mappings["ranking_rows_with_one_dimension_path"]
        ):
            raise RuntimeError(
                "Not every loaded season ranking resolves to exactly one dimension path"
            )

        load_match_participation_fact(connection, config)
        load_player_season_snapshot_fact(connection, config)

        postload = lightweight_postload_check(connection, config)
        result["postload_check"] = postload
        if postload["status"] != "PASS":
            raise RuntimeError(
                "Post-load dimensional counts failed; Phase 4 DML was rolled back"
            )

        after = postload["actual_counts"]
        result["warehouse_counts_after"] = after
        result["row_count_changes"] = {
            name: int(after[name]) - int(before[name]) for name in sorted(after)
        }
        connection.commit()
        result["committed"] = True
        result["status"] = (
            "ALREADY_CURRENT" if before == after else "COMPLETED"
        )
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        write_json(output_path, result)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        if connection is not None:
            connection.rollback()
        result["committed"] = False
        result["status"] = "FAIL"
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        write_json(output_path, result)
        print(json.dumps(result, indent=2, default=str), file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
