#!/usr/bin/env python3
"""Validate Phase 2 CSV fixtures without Oracle; this performs no loading."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from phase3_model import SOURCE_SPECS, SeasonRef, StageRow, read_csv, write_json
from manifest_checks import compare_validation_to_manifests
from validator import validate_all


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase2-data-dir", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "run_outputs" / "offline_fixture_verification.json",
    )
    return parser.parse_args()


def fixture_rows(data_dir: Path, file_name: str, batch_id: int) -> list[StageRow]:
    spec = SOURCE_SPECS[file_name]
    rows = read_csv(data_dir / file_name)
    result = []
    for row_number, raw in enumerate(rows, start=1):
        result.append(
            StageRow(
                entity=spec["entity"],
                source_table=spec["source_table"],
                staging_id=(batch_id * 1_000_000) + row_number,
                load_batch_id=batch_id,
                source_file_name=file_name,
                source_row_number=row_number,
                raw={field: (value if value != "" else None) for field, value in raw.items()},
            )
        )
    return result


def expected_dispositions(data_dir: Path, file_name: str) -> dict[int, str]:
    stem = Path(file_name).stem
    return {
        int(row["source_row_number"]): row["expected_disposition"]
        for row in read_csv(data_dir / f"{stem}_row_expectations.csv")
    }


def main() -> int:
    args = parse_args()
    data_dir = args.phase2_data_dir.resolve()
    references = json.loads((data_dir / "reference_prerequisites.json").read_text(encoding="utf-8"))
    seasons = {
        int(item["season_id"]): SeasonRef(
            season_id=int(item["season_id"]),
            name=item["season_name"],
            start_date=date.fromisoformat(item["start_date"]),
            end_date=date.fromisoformat(item["end_date"]),
            status=item["season_status"],
        )
        for item in references["seasons"]
    }
    role_ids = {int(item["role_id"]) for item in references["roles"]}
    characters = {
        item["character_name"].upper(): int(item["character_id"])
        for item in references["characters"]
    }
    bundle = validate_all(
        fixture_rows(data_dir, "player.csv", 1),
        fixture_rows(data_dir, "player_scd2_update.csv", 2),
        fixture_rows(data_dir, "match.csv", 1),
        fixture_rows(data_dir, "participation.csv", 1),
        fixture_rows(data_dir, "season_ranking.csv", 1),
        seasons,
        role_ids,
        characters,
    )

    mismatches = []
    rows_by_file = {
        "player.csv": bundle.initial_players,
        "match.csv": bundle.matches,
        "participation.csv": bundle.participation,
        "season_ranking.csv": bundle.rankings,
    }
    counts = {}
    for file_name, rows in rows_by_file.items():
        expected = expected_dispositions(data_dir, file_name)
        accepted = rejected = 0
        for row in rows:
            actual = "ACCEPT" if row.valid else "REJECT"
            accepted += actual == "ACCEPT"
            rejected += actual == "REJECT"
            if expected[row.source_row_number] != actual:
                mismatches.append(
                    {
                        "source_file": file_name,
                        "source_row_number": row.source_row_number,
                        "expected": expected[row.source_row_number],
                        "actual": actual,
                        "errors": [error.code for error in row.errors],
                        "raw": row.raw,
                    }
                )
        counts[file_name] = {"accepted": accepted, "rejected": rejected}
    update_rejected = [row for row in bundle.update_players if not row.valid]
    rejection_reason_rows = sum(len(row.errors) for row in bundle.all_rows())
    rejected_staging_rows = sum(not row.valid for row in bundle.all_rows())
    root = Path(__file__).resolve().parents[1]
    expectation_check = compare_validation_to_manifests(
        bundle,
        data_dir,
        root / "config" / "error_code_map.json",
    )
    result = {
        "status": (
            "PASS"
            if not mismatches
            and not update_rejected
            and rejected_staging_rows == 298
            and rejection_reason_rows == 307
            and expectation_check["status"] == "PASS"
            else "FAIL"
        ),
        "counts": counts,
        "scd2_update_rows": len(bundle.update_players),
        "scd2_update_rejected": len(update_rejected),
        "rejected_staging_rows": rejected_staging_rows,
        "rejection_reason_rows": rejection_reason_rows,
        "disposition_mismatch_count": len(mismatches),
        "disposition_mismatches": mismatches[:100],
        "rating_plan_count": len(bundle.rating_start),
        "expectation_comparison": expectation_check,
    }
    write_json(args.output.resolve(), result)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
