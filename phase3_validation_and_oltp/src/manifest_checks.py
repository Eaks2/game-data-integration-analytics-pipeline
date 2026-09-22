"""Compare independent validation results with immutable Phase 2 fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from phase3_model import StageRow, ValidationBundle, read_csv, read_json, sha256_file


def manifest_preflight(
    manifest_dir: Path, phase3_config: dict[str, Any]
) -> dict[str, Any]:
    generation = read_json(manifest_dir / "generation_manifest.json")
    failures: list[str] = []
    if generation["generation_id"] != phase3_config["phase2_generation_id"]:
        failures.append("Phase 2 generation ID differs from phase3_config.json")
    error_metadata = generation["error_manifest"]
    if sha256_file(manifest_dir / error_metadata["file"]) != error_metadata["sha256"]:
        failures.append("error_manifest.csv SHA-256 mismatch")
    for metadata in generation["row_expectation_manifests"].values():
        if sha256_file(manifest_dir / metadata["file"]) != metadata["sha256"]:
            failures.append(f"{metadata['file']} SHA-256 mismatch")
    scd2_metadata = generation["scd2_update_manifest"]
    if sha256_file(manifest_dir / scd2_metadata["file"]) != scd2_metadata["sha256"]:
        failures.append("scd2_update_manifest.csv SHA-256 mismatch")
    return {
        "status": "PASS" if not failures else "FAIL",
        "generation_id": generation["generation_id"],
        "failures": failures,
        "generation_manifest": generation,
    }


def expected_rows(manifest_dir: Path) -> dict[tuple[str, int], dict[str, str]]:
    result: dict[tuple[str, int], dict[str, str]] = {}
    for source_file in (
        "player.csv",
        "match.csv",
        "participation.csv",
        "season_ranking.csv",
    ):
        expectation_file = manifest_dir / f"{Path(source_file).stem}_row_expectations.csv"
        for item in read_csv(expectation_file):
            result[(source_file, int(item["source_row_number"]))] = item
    return result


def compare_validation_to_manifests(
    bundle: ValidationBundle,
    manifest_dir: Path,
    error_code_map_path: Path,
) -> dict[str, Any]:
    expected = expected_rows(manifest_dir)
    actual_initial = {
        (row.source_file_name, row.source_row_number): row
        for row in bundle.all_rows()
        if row.load_batch_id == 1
    }
    disposition_mismatches: list[dict[str, Any]] = []
    for key, expectation in expected.items():
        row = actual_initial.get(key)
        if row is None:
            disposition_mismatches.append(
                {"source_file": key[0], "source_row_number": key[1], "problem": "missing staged row"}
            )
            continue
        actual = "ACCEPT" if row.valid else "REJECT"
        if actual != expectation["expected_disposition"]:
            disposition_mismatches.append(
                {
                    "source_file": key[0],
                    "source_row_number": key[1],
                    "expected": expectation["expected_disposition"],
                    "actual": actual,
                    "actual_error_codes": [error.code for error in row.errors],
                }
            )
    for key in set(actual_initial) - set(expected):
        disposition_mismatches.append(
            {"source_file": key[0], "source_row_number": key[1], "problem": "unexpected staged row"}
        )

    error_code_map = read_json(error_code_map_path)
    reason_mismatches: list[dict[str, Any]] = []
    error_cases = read_csv(manifest_dir / "error_manifest.csv")
    error_case_keys = {
        (case["source_file"], int(case["source_row_number"])) for case in error_cases
    }
    for case in error_cases:
        key = (case["source_file"], int(case["source_row_number"]))
        row = actual_initial.get(key)
        if row is None:
            continue
        actual_codes = {error.code for error in row.errors}
        allowed_codes = set(error_code_map.get(case["error_type"], []))
        if case["multi_error"] == "Y":
            missing_codes = allowed_codes - actual_codes
            affected_fields = set(case["affected_fields"].split("|"))
            actual_fields = {error.field_name for error in row.errors}
            missing_fields = affected_fields - actual_fields
            if missing_codes or missing_fields:
                reason_mismatches.append(
                    {
                        "source_file": key[0],
                        "source_row_number": key[1],
                        "error_type": case["error_type"],
                        "missing_codes": sorted(missing_codes),
                        "missing_fields": sorted(missing_fields),
                        "actual_codes": sorted(actual_codes),
                    }
                )
        elif not actual_codes.intersection(allowed_codes):
            reason_mismatches.append(
                {
                    "source_file": key[0],
                    "source_row_number": key[1],
                    "error_type": case["error_type"],
                    "allowed_codes": sorted(allowed_codes),
                    "actual_codes": sorted(actual_codes),
                }
            )

    dependent_parent_keys = [
        key
        for key, expectation in expected.items()
        if key[0] == "match.csv"
        and expectation["expected_disposition"] == "REJECT"
        and key not in error_case_keys
    ]
    for key in dependent_parent_keys:
        row = actual_initial.get(key)
        actual_codes = set() if row is None else {error.code for error in row.errors}
        if "MAT_GROUP_INVALID" not in actual_codes:
            reason_mismatches.append(
                {
                    "source_file": key[0],
                    "source_row_number": key[1],
                    "error_type": "DEPENDENT_INVALID_MATCH_PARENT",
                    "required_code": "MAT_GROUP_INVALID",
                    "actual_codes": sorted(actual_codes),
                }
            )

    update_rejections = [
        {
            "source_row_number": row.source_row_number,
            "error_codes": [error.code for error in row.errors],
        }
        for row in bundle.update_players
        if not row.valid
    ]
    scd2_expected = {
        int(item["PlayerID"]): item
        for item in read_csv(manifest_dir / "scd2_update_manifest.csv")
    }
    initial_by_player = {
        row.canonical["PlayerID"]: row
        for row in bundle.initial_players
        if row.valid and row.canonical.get("PlayerID") is not None
    }
    update_by_player = {
        row.canonical["PlayerID"]: row
        for row in bundle.update_players
        if row.canonical.get("PlayerID") is not None
    }
    scd2_manifest_mismatches: list[dict[str, Any]] = []
    if set(update_by_player) != set(scd2_expected):
        scd2_manifest_mismatches.append(
            {
                "problem": "PlayerID set differs from scd2_update_manifest.csv",
                "missing_player_ids": sorted(set(scd2_expected) - set(update_by_player)),
                "unexpected_player_ids": sorted(set(update_by_player) - set(scd2_expected)),
            }
        )
    for player_id in sorted(set(scd2_expected).intersection(update_by_player)):
        expected_version = scd2_expected[player_id]
        initial_row = initial_by_player.get(player_id)
        update_row = update_by_player[player_id]
        comparisons = {
            "Username": (
                None if initial_row is None else initial_row.canonical.get("Username"),
                update_row.canonical.get("Username"),
                expected_version["Username"],
                expected_version["Username"],
            ),
            "Region": (
                None if initial_row is None else initial_row.canonical.get("Region"),
                update_row.canonical.get("Region"),
                expected_version["OldRegion"],
                expected_version["NewRegion"],
            ),
            "AccountLevel": (
                None if initial_row is None else initial_row.canonical.get("AccountLevel"),
                update_row.canonical.get("AccountLevel"),
                int(expected_version["OldAccountLevel"]),
                int(expected_version["NewAccountLevel"]),
            ),
            "AccountStatus": (
                None if initial_row is None else initial_row.canonical.get("AccountStatus"),
                update_row.canonical.get("AccountStatus"),
                expected_version["OldAccountStatus"],
                expected_version["NewAccountStatus"],
            ),
        }
        field_mismatches = {
            field: {
                "actual_old": values[0],
                "actual_new": values[1],
                "expected_old": values[2],
                "expected_new": values[3],
            }
            for field, values in comparisons.items()
            if values[0] != values[2] or values[1] != values[3]
        }
        if initial_row is None or field_mismatches:
            scd2_manifest_mismatches.append(
                {
                    "player_id": player_id,
                    "initial_source_row_found": initial_row is not None,
                    "field_mismatches": field_mismatches,
                }
            )
    status = (
        "PASS"
        if not disposition_mismatches
        and not reason_mismatches
        and not update_rejections
        and not scd2_manifest_mismatches
        else "FAIL"
    )
    return {
        "status": status,
        "expected_initial_row_count": len(expected),
        "actual_initial_row_count": len(actual_initial),
        "disposition_mismatch_count": len(disposition_mismatches),
        "disposition_mismatches": disposition_mismatches[:100],
        "error_manifest_case_count": len(error_cases),
        "dependent_invalid_parent_case_count": len(dependent_parent_keys),
        "rejection_reason_mismatch_count": len(reason_mismatches),
        "rejection_reason_mismatches": reason_mismatches[:100],
        "scd2_update_row_count": len(bundle.update_players),
        "scd2_update_rejection_count": len(update_rejections),
        "scd2_update_rejections": update_rejections,
        "scd2_manifest_mismatch_count": len(scd2_manifest_mismatches),
        "scd2_manifest_mismatches": scd2_manifest_mismatches[:100],
    }
