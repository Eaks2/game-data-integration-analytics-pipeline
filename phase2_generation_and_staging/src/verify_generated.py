#!/usr/bin/env python3
"""Verify generated file hashes, counts, error metadata, and the SCD2 batch."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from phase2_common import SOURCE_DEFINITIONS, read_csv, sha256_file, write_json


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=root / "data" / "generated")
    parser.add_argument("--output", type=Path, default=root / "run_outputs" / "generated_verification.json")
    return parser.parse_args()


def fail(message: str) -> None:
    raise ValueError(message)


def main() -> int:
    args = parse_args()
    data_dir = args.data_dir.resolve()
    manifest_path = data_dir / "generation_manifest.json"
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)

    loaded: dict[str, list[dict[str, str]]] = {}
    checks: list[str] = []
    for file_name, metadata in manifest["files"].items():
        path = data_dir / file_name
        rows = read_csv(path, SOURCE_DEFINITIONS[file_name]["headers"])
        loaded[file_name] = rows
        if len(rows) != metadata["row_count"]:
            fail(f"{file_name}: manifest count {metadata['row_count']} != actual {len(rows)}")
        if sha256_file(path) != metadata["sha256"]:
            fail(f"{file_name}: SHA-256 does not match generation manifest")
    checks.append("all source-file row counts and SHA-256 hashes match generation_manifest.json")

    error_path = data_dir / manifest["error_manifest"]["file"]
    if sha256_file(error_path) != manifest["error_manifest"]["sha256"]:
        fail("error_manifest.csv SHA-256 does not match generation manifest")
    with error_path.open("r", encoding="utf-8", newline="") as handle:
        errors = list(csv.DictReader(handle))
    if len(errors) != manifest["error_manifest"]["error_case_count"]:
        fail("Error-manifest case count does not match generation manifest")
    counts = Counter(row["source_file"] for row in errors)
    if dict(sorted(counts.items())) != manifest["error_manifest"]["counts_by_source_file"]:
        fail("Error counts by source file do not match generation manifest")
    for error in errors:
        file_name = error["source_file"]
        source_row = int(error["source_row_number"])
        if not 1 <= source_row <= len(loaded[file_name]):
            fail(f"Error manifest points outside {file_name}: row {source_row}")
        source = loaded[file_name][source_row - 1]
        expected_values: dict[str, Any] = json.loads(error["field_values_json"])
        actual_values = {field: source[field] for field in error["affected_fields"].split("|")}
        if actual_values != expected_values:
            fail(f"Error fixture mismatch for {file_name} row {source_row}")
    checks.append("every injected-error case points to the expected source row and raw field value")

    expected_reject_counts: dict[str, int] = {}
    for file_name, metadata in manifest["row_expectation_manifests"].items():
        expectation_path = data_dir / metadata["file"]
        if sha256_file(expectation_path) != metadata["sha256"]:
            fail(f"{metadata['file']}: SHA-256 does not match generation manifest")
        with expectation_path.open("r", encoding="utf-8", newline="") as handle:
            expectations = list(csv.DictReader(handle))
        if len(expectations) != len(loaded[file_name]):
            fail(f"{metadata['file']}: expectation count does not match source row count")
        if [int(row["source_row_number"]) for row in expectations] != list(range(1, len(expectations) + 1)):
            fail(f"{metadata['file']}: SourceRowNumber coverage is incomplete")
        disposition_counts = Counter(row["expected_disposition"] for row in expectations)
        if set(disposition_counts) - {"ACCEPT", "REJECT"}:
            fail(f"{metadata['file']}: invalid expected disposition")
        if disposition_counts["ACCEPT"] != metadata["expected_accept_rows"] or disposition_counts["REJECT"] != metadata["expected_reject_rows"]:
            fail(f"{metadata['file']}: expectation totals do not match generation manifest")
        expected_reject_counts[file_name] = disposition_counts["REJECT"]
    if expected_reject_counts != manifest["error_manifest"]["expected_reject_rows_by_source_file"]:
        fail("Row-expectation reject counts do not match generation manifest")
    if sum(expected_reject_counts.values()) != len(errors) + 4:
        fail("Expected four invalid parent Match rows in addition to error-manifest source rows")
    checks.append("one disposition expectation exists for every initial source row; four invalid result groups also reject their parent Match rows")

    scd2_path = data_dir / manifest["scd2_update_manifest"]["file"]
    if sha256_file(scd2_path) != manifest["scd2_update_manifest"]["sha256"]:
        fail("SCD2 update-manifest SHA-256 does not match")
    with scd2_path.open("r", encoding="utf-8", newline="") as handle:
        scd2_documentation = list(csv.DictReader(handle))
    initial_by_id = {}
    clean_player_count = manifest["clean_baseline"]["row_counts"]["player.csv"]
    for row in loaded["player.csv"][:clean_player_count]:
        initial_by_id[row["PlayerID"]] = row
    update_by_id = {row["PlayerID"]: row for row in loaded["player_scd2_update.csv"]}
    if len(update_by_id) != manifest["scd2_update_manifest"]["row_count"]:
        fail("SCD2 update file does not contain the documented number of unique players")
    for row in scd2_documentation:
        player_id = row["PlayerID"]
        if player_id not in initial_by_id or player_id not in update_by_id:
            fail(f"Documented SCD2 PlayerID {player_id} is missing")
        initial = initial_by_id[player_id]
        update = update_by_id[player_id]
        if update["Username"] != initial["Username"] or update["Email"] != initial["Email"]:
            fail(f"SCD2 fixture {player_id} changed non-tracked identity attributes")
        if not any(update[field] != initial[field] for field in ("Region", "AccountLevel", "AccountStatus")):
            fail(f"SCD2 fixture {player_id} did not change a tracked attribute")
        if (
            row["OldRegion"] != initial["Region"]
            or row["NewRegion"] != update["Region"]
            or row["OldAccountLevel"] != initial["AccountLevel"]
            or row["NewAccountLevel"] != update["AccountLevel"]
            or row["OldAccountStatus"] != initial["AccountStatus"]
            or row["NewAccountStatus"] != update["AccountStatus"]
        ):
            fail(f"SCD2 documentation does not match data for PlayerID {player_id}")
    checks.append("the separate player update batch contains valid existing players with documented tracked-attribute changes")

    if manifest["clean_baseline"]["status"] != "PASSED before error injection":
        fail("Generator did not record a passing clean-baseline verification")
    checks.append("generator verified all clean business rules before injecting dirty data")

    result = {
        "status": "PASS",
        "generation_id": manifest["generation_id"],
        "checks": checks,
        "source_row_counts": {name: len(rows) for name, rows in loaded.items()},
        "injected_error_case_count": len(errors),
        "error_counts_by_file": dict(sorted(counts.items())),
        "scd2_valid_update_count": len(update_by_id),
    }
    write_json(args.output.resolve(), result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
