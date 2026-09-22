#!/usr/bin/env python3
"""Validate both Phase 2 batches and atomically load accepted rows into OLTP."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from manifest_checks import compare_validation_to_manifests, manifest_preflight
from oracle_support import (
    connect,
    database_preflight,
    fetch_existing_players,
    fetch_stage_rows,
    load_oltp,
    staging_input_preflight,
)
from phase3_model import read_json, write_json
from validator import validate_all


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-config", type=Path, required=True)
    parser.add_argument(
        "--phase3-config",
        type=Path,
        default=root / "config" / "phase3_config.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "run_outputs" / "phase3_run_result.json",
    )
    return parser.parse_args()


def resolve_manifest_dir(root: Path, config: dict[str, Any]) -> Path:
    path = Path(config["manifest_directory"])
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def validation_summary(bundle) -> dict[str, Any]:
    rows_by_file = {
        "player.csv": bundle.initial_players,
        "player_scd2_update.csv": bundle.update_players,
        "match.csv": bundle.matches,
        "participation.csv": bundle.participation,
        "season_ranking.csv": bundle.rankings,
    }
    summary = {}
    errors = Counter()
    rejection_reasons = 0
    for file_name, rows in rows_by_file.items():
        accepted = sum(row.valid for row in rows)
        rejected = len(rows) - accepted
        summary[file_name] = {
            "rows": len(rows),
            "accepted": accepted,
            "rejected": rejected,
        }
        for row in rows:
            for error in row.errors:
                errors[error.code] += 1
                rejection_reasons += 1
    return {
        "files": summary,
        "rejected_staging_rows": sum(item["rejected"] for item in summary.values()),
        "rejection_reason_rows": rejection_reasons,
        "error_counts_by_code": dict(sorted(errors.items())),
        "rating_plan_player_seasons": len(bundle.rating_start),
    }


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    output_path = args.output.resolve()
    config = read_json(args.phase3_config.resolve())
    oracle_config = read_json(args.oracle_config.resolve())
    manifest_dir = resolve_manifest_dir(root, config)
    result: dict[str, Any] = {
        "phase": 3,
        "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "STARTED",
        "transaction_policy": "Both LoadBatchID values are processed in one Oracle transaction; failures roll back all Phase 3 DML.",
    }
    connection = None
    try:
        manifest_check = manifest_preflight(manifest_dir, config)
        result["manifest_preflight"] = {
            key: value for key, value in manifest_check.items() if key != "generation_manifest"
        }
        if manifest_check["status"] != "PASS":
            raise RuntimeError("Phase 2 manifest preflight failed")
        generation = manifest_check["generation_manifest"]
        references = read_json(manifest_dir / "reference_prerequisites.json")
        initial_batch_id = int(config["initial_load_batch_id"])
        scd2_batch_id = int(config["scd2_load_batch_id"])

        connection = connect(oracle_config)
        database_check = database_preflight(connection, references)
        result["database_preflight"] = {
            "status": database_check["status"],
            "failures": database_check["failures"],
        }
        if database_check["status"] != "PASS":
            raise RuntimeError("Oracle schema/reference preflight failed")

        initial_players = fetch_stage_rows(connection, "player.csv", initial_batch_id)
        update_players = fetch_stage_rows(
            connection, "player_scd2_update.csv", scd2_batch_id
        )
        matches = fetch_stage_rows(connection, "match.csv", initial_batch_id)
        participation = fetch_stage_rows(
            connection, "participation.csv", initial_batch_id
        )
        rankings = fetch_stage_rows(
            connection, "season_ranking.csv", initial_batch_id
        )

        # Build a temporary unvalidated bundle for the input-state checks.
        from phase3_model import ValidationBundle

        input_bundle = ValidationBundle(
            initial_players,
            update_players,
            matches,
            participation,
            rankings,
            {},
            {},
        )
        input_check = staging_input_preflight(
            connection,
            input_bundle,
            generation,
            initial_batch_id,
            scd2_batch_id,
        )
        result["staging_input_preflight"] = input_check
        if input_check["status"] == "ALREADY_COMPLETED":
            result.update(
                {
                    "status": "ALREADY_COMPLETED",
                    "message": "Both batches are already COMPLETED. No DML was executed; run verify_phase3.py for reconciliation.",
                    "finished_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            )
            write_json(output_path, result)
            print(json.dumps(result, indent=2, default=str))
            return 0
        if input_check["status"] != "PASS":
            raise RuntimeError("Staging input-state preflight failed")

        existing_players = fetch_existing_players(connection)
        bundle = validate_all(
            initial_players,
            update_players,
            matches,
            participation,
            rankings,
            database_check["seasons"],
            database_check["role_ids"],
            database_check["characters_by_name"],
            existing_players,
        )
        summary = validation_summary(bundle)
        result["validation_summary"] = summary
        expected_validation_totals = {
            "rejected_staging_rows": 298,
            "rejection_reason_rows": 307,
        }
        result["expected_validation_totals"] = expected_validation_totals
        if any(
            summary[name] != expected
            for name, expected in expected_validation_totals.items()
        ):
            raise RuntimeError(
                "Validation totals differ from the fixed Phase 2 reconciliation; no database changes were made"
            )
        expectation_check = compare_validation_to_manifests(
            bundle,
            manifest_dir,
            root / "config" / "error_code_map.json",
        )
        result["preload_expectation_comparison"] = expectation_check
        if expectation_check["status"] != "PASS":
            raise RuntimeError(
                "Deliberate validation does not match immutable Phase 2 expectations; no database changes were made"
            )

        result["load"] = load_oltp(
            connection,
            bundle,
            existing_players,
            [initial_batch_id, scd2_batch_id],
        )
        connection.commit()
        result["status"] = "COMPLETED"
        result["committed"] = True
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_json(output_path, result)
        print(json.dumps(result, indent=2, default=str))
        return 0
    except Exception as exc:
        if connection is not None:
            connection.rollback()
        result["status"] = "FAIL"
        result["committed"] = False
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_json(output_path, result)
        print(json.dumps(result, indent=2, default=str), file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
