#!/usr/bin/env python3
"""Compare every generated raw value and lineage key with Oracle staging."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from load_staging import connect, load_json
from phase2_common import SOURCE_DEFINITIONS, oracle_text, read_csv, write_json


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=root / "data" / "generated")
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "run_outputs" / "staging_verification.json",
    )
    return parser.parse_args()


def fail(message: str) -> None:
    raise ValueError(message)


def get_batch(connection: Any, run_key: str) -> dict[str, Any]:
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT LoadBatchID, SourceSystem, RowsRead, RowsAccepted, RowsRejected, LoadStatus
          FROM LoadBatch
         WHERE SourceSystem = :run_key
        """,
        run_key=run_key,
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        fail(f"Expected one LoadBatch for {run_key!r}; found {len(rows)}")
    row = rows[0]
    return {
        "load_batch_id": int(row[0]),
        "source_system": str(row[1]),
        "rows_read": int(row[2]),
        "rows_accepted": int(row[3]),
        "rows_rejected": int(row[4]),
        "load_status": str(row[5]),
    }


def staged_file_rows(
    connection: Any, file_name: str, batch_id: int
) -> dict[int, dict[str, Any]]:
    definition = SOURCE_DEFINITIONS[file_name]
    select_columns = [
        definition["id_column"],
        "SourceFileName",
        "SourceRowNumber",
        *definition["raw_columns"],
        "ProcessingStatus",
        "TargetRecordID",
    ]
    cursor = connection.cursor()
    cursor.execute(
        f"""
        SELECT {', '.join(select_columns)}
          FROM {definition['table']}
         WHERE LoadBatchID = :batch_id
           AND SourceFileName = :file_name
         ORDER BY SourceRowNumber
        """,
        batch_id=batch_id,
        file_name=file_name,
    )
    result = {}
    for values in cursor:
        item = dict(zip(select_columns, values))
        result[int(item["SourceRowNumber"])] = item
    return result


def compare_file(
    connection: Any, data_dir: Path, file_name: str, batch_id: int
) -> dict[str, int]:
    definition = SOURCE_DEFINITIONS[file_name]
    source = read_csv(data_dir / file_name, definition["headers"])
    staged = staged_file_rows(connection, file_name, batch_id)
    if len(staged) != len(source):
        fail(f"{file_name}: generated {len(source)} rows but staged {len(staged)}")
    if set(staged) != set(range(1, len(source) + 1)):
        fail(f"{file_name}: SourceRowNumber is not the complete 1..N lineage range")
    for source_row_number, source_row in enumerate(source, start=1):
        staged_row = staged[source_row_number]
        if staged_row["SourceFileName"] != file_name:
            fail(f"{file_name} row {source_row_number}: SourceFileName was not preserved")
        for header, raw_column in zip(definition["headers"], definition["raw_columns"]):
            expected = oracle_text(source_row[header])
            actual = staged_row[raw_column]
            if actual != expected:
                fail(
                    f"{file_name} row {source_row_number} field {header}: "
                    f"generated {expected!r}, staged {actual!r}"
                )
        if staged_row["ProcessingStatus"] != "NEW":
            fail(f"{file_name} row {source_row_number}: ProcessingStatus is not NEW")
        if staged_row["TargetRecordID"] is not None:
            fail(f"{file_name} row {source_row_number}: TargetRecordID was populated during Phase 2")
        if staged_row[definition["id_column"]] is None:
            fail(f"{file_name} row {source_row_number}: staging record ID was not captured")
    return {"generated_rows": len(source), "staged_rows": len(staged), "raw_values_compared": len(source) * len(definition["headers"])}


def main() -> int:
    args = parse_args()
    data_dir = args.data_dir.resolve()
    manifest = load_json(data_dir / "generation_manifest.json")
    oracle_config = load_json(args.oracle_config.resolve())
    connection = connect(oracle_config)
    try:
        batches = {
            name: get_batch(connection, metadata["run_key"])
            for name, metadata in manifest["batches"].items()
        }
        file_results: dict[str, dict[str, int]] = {}
        for batch_name, metadata in manifest["batches"].items():
            batch = batches[batch_name]
            if batch["load_status"] != "STAGED":
                fail(f"{batch_name} LoadBatch is {batch['load_status']}, expected STAGED")
            if batch["rows_accepted"] != 0 or batch["rows_rejected"] != 0:
                fail(f"{batch_name} LoadBatch has Phase 3 acceptance/rejection counts")
            expected_rows = int(metadata["expected_rows"])
            if batch["rows_read"] != expected_rows:
                fail(f"{batch_name} LoadBatch RowsRead {batch['rows_read']} != {expected_rows}")
            for file_name in metadata["files"]:
                file_results[file_name] = compare_file(
                    connection, data_dir, file_name, batch["load_batch_id"]
                )

        batch_ids = [item["load_batch_id"] for item in batches.values()]
        bind_names = ",".join(f":batch_{index}" for index in range(len(batch_ids)))
        cursor = connection.cursor()
        cursor.execute(
            f"SELECT COUNT(*) FROM RejectedRecord WHERE LoadBatchID IN ({bind_names})",
            {f"batch_{index}": value for index, value in enumerate(batch_ids)},
        )
        rejection_count = int(cursor.fetchone()[0])
        if rejection_count != 0:
            fail("RejectedRecord contains rows for a Phase 2 batch")

        with (data_dir / "error_manifest.csv").open("r", encoding="utf-8", newline="") as handle:
            errors = list(csv.DictReader(handle))
        staged_cache: dict[str, dict[int, dict[str, Any]]] = {}
        initial_batch_id = batches["initial"]["load_batch_id"]
        for file_name in {row["source_file"] for row in errors}:
            staged_cache[file_name] = staged_file_rows(connection, file_name, initial_batch_id)
        for error in errors:
            source_file = error["source_file"]
            source_row = int(error["source_row_number"])
            staged = staged_cache[source_file].get(source_row)
            if staged is None:
                fail(f"Injected-error fixture is not staged: {source_file} row {source_row}")
            expected_values = json.loads(error["field_values_json"])
            definition = SOURCE_DEFINITIONS[source_file]
            raw_by_header = dict(zip(definition["headers"], definition["raw_columns"]))
            for field, expected in expected_values.items():
                if staged[raw_by_header[field]] != oracle_text(expected):
                    fail(f"Injected raw value did not survive staging: {source_file} row {source_row} {field}")

        error_counts = Counter(row["source_file"] for row in errors)
        result = {
            "status": "PASS",
            "batches": batches,
            "files": file_results,
            "all_generated_rows_reached_staging": True,
            "all_raw_source_values_compared": True,
            "lineage_complete": True,
            "all_processing_statuses_new": True,
            "all_target_record_ids_null": True,
            "rejected_record_count": rejection_count,
            "error_manifest_case_count": len(errors),
            "staged_error_fixture_counts_by_file": dict(sorted(error_counts.items())),
            "scd2_update_batch_identified": {
                "load_batch_id": batches["scd2"]["load_batch_id"],
                "source_file": "player_scd2_update.csv",
                "valid_update_rows": file_results["player_scd2_update.csv"]["staged_rows"],
            },
        }
        write_json(args.output.resolve(), result)
        print(json.dumps(result, indent=2))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
