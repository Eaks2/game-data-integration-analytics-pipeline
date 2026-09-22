#!/usr/bin/env python3
"""Load CSV text verbatim into the authoritative Phase 1 raw staging tables."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phase2_common import SOURCE_DEFINITIONS, oracle_text, read_csv, sha256_file, write_json


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=root / "data" / "generated")
    parser.add_argument("--batch", choices=("initial", "scd2"), required=True)
    parser.add_argument(
        "--result-file",
        type=Path,
        help="Default: run_outputs/<batch>_load_result.json",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def connect(config: dict[str, Any]):
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt before running the Oracle loader") from exc
    thick_dir = str(config.get("thick_mode_lib_dir", "")).strip()
    if thick_dir:
        oracledb.init_oracle_client(lib_dir=thick_dir)
    password_variable = config.get("password_env", "CS779_ORACLE_PASSWORD")
    password = os.environ.get(password_variable)
    if not password:
        raise RuntimeError(f"Set environment variable {password_variable} to the Oracle password")
    return oracledb.connect(user=config["user"], password=password, dsn=config["dsn"])


def scalar_out_value(variable: Any) -> int:
    value = variable.getvalue()
    if isinstance(value, list):
        value = value[0]
    return int(value)


def find_or_open_batch(connection: Any, run_key: str) -> tuple[int, bool, str | None]:
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT LoadBatchID, LoadStatus
          FROM LoadBatch
         WHERE SourceSystem = :source_system
         ORDER BY LoadBatchID
        """,
        source_system=run_key,
    )
    existing = cursor.fetchall()
    if len(existing) > 1:
        raise RuntimeError(
            f"More than one LoadBatch has SourceSystem={run_key!r}; resolve this manually before continuing"
        )
    if existing:
        batch_id, status = int(existing[0][0]), str(existing[0][1])
        if status not in {"STARTED", "STAGED", "FAILED"}:
            raise RuntimeError(
                f"LoadBatch {batch_id} is {status}; it may have entered Phase 3 and cannot be resumed by this loader"
            )
        cursor.execute(
            """
            UPDATE LoadBatch
               SET LoadStatus = 'STARTED', LoadEndTime = NULL
             WHERE LoadBatchID = :batch_id
            """,
            batch_id=batch_id,
        )
        connection.commit()
        return batch_id, True, status

    batch_var = cursor.var(int)
    cursor.execute(
        """
        INSERT INTO LoadBatch (
            LoadBatchID, SourceSystem, LoadStartTime, RowsRead,
            RowsAccepted, RowsRejected, LoadStatus
        ) VALUES (
            LoadBatch_Seq.NEXTVAL, :source_system, SYSTIMESTAMP, 0, 0, 0, 'STARTED'
        )
        RETURNING LoadBatchID INTO :batch_id
        """,
        source_system=run_key,
        batch_id=batch_var,
    )
    batch_id = scalar_out_value(batch_var)
    connection.commit()
    return batch_id, False, None


def total_staged_rows(connection: Any, batch_id: int) -> int:
    cursor = connection.cursor()
    total = 0
    for table in ("StgPlayer", "StgMatch", "StgParticipation", "StgSeasonRanking"):
        cursor.execute(
            f"SELECT COUNT(*) FROM {table} WHERE LoadBatchID = :batch_id",
            batch_id=batch_id,
        )
        total += int(cursor.fetchone()[0])
    return total


def update_progress(connection: Any, batch_id: int, status: str) -> int:
    rows_read = total_staged_rows(connection, batch_id)
    cursor = connection.cursor()
    if status == "STAGED":
        cursor.execute(
            """
            UPDATE LoadBatch
               SET RowsRead = :rows_read,
                   RowsAccepted = 0,
                   RowsRejected = 0,
                   LoadStatus = 'STAGED',
                   LoadEndTime = SYSTIMESTAMP
             WHERE LoadBatchID = :batch_id
            """,
            rows_read=rows_read,
            batch_id=batch_id,
        )
    else:
        cursor.execute(
            """
            UPDATE LoadBatch
               SET RowsRead = :rows_read,
                   RowsAccepted = 0,
                   RowsRejected = 0,
                   LoadStatus = :load_status,
                   LoadEndTime = CASE WHEN :load_status = 'FAILED' THEN SYSTIMESTAMP ELSE NULL END
             WHERE LoadBatchID = :batch_id
            """,
            rows_read=rows_read,
            load_status=status,
            batch_id=batch_id,
        )
    connection.commit()
    return rows_read


def existing_rows(
    connection: Any,
    definition: dict[str, Any],
    batch_id: int,
    file_name: str,
) -> dict[int, tuple[Any, ...]]:
    raw_columns = definition["raw_columns"]
    columns = ", ".join(["SourceRowNumber", *raw_columns, "ProcessingStatus", "TargetRecordID"])
    cursor = connection.cursor()
    cursor.execute(
        f"""
        SELECT {columns}
          FROM {definition['table']}
         WHERE LoadBatchID = :batch_id
           AND SourceFileName = :file_name
        """,
        batch_id=batch_id,
        file_name=file_name,
    )
    return {int(row[0]): tuple(row[1:]) for row in cursor}


def load_file(
    connection: Any,
    data_dir: Path,
    file_name: str,
    batch_id: int,
    batch_size: int,
) -> dict[str, int]:
    definition = SOURCE_DEFINITIONS[file_name]
    rows = read_csv(data_dir / file_name, definition["headers"])
    staged = existing_rows(connection, definition, batch_id, file_name)
    source_row_numbers = set(range(1, len(rows) + 1))
    extra_staged = set(staged) - source_row_numbers
    if extra_staged:
        raise RuntimeError(
            f"{file_name}: batch contains lineage rows no longer present in the source: {sorted(extra_staged)[:5]}"
        )

    pending: list[dict[str, Any]] = []
    for source_row_number, source in enumerate(rows, start=1):
        raw_values = tuple(oracle_text(source[header]) for header in definition["headers"])
        if source_row_number in staged:
            stored = staged[source_row_number]
            if stored[:-2] != raw_values:
                raise RuntimeError(
                    f"{file_name} row {source_row_number}: source changed after it was staged; use a new run key"
                )
            if stored[-2] != "NEW" or stored[-1] is not None:
                raise RuntimeError(
                    f"{file_name} row {source_row_number}: Phase 2 requires ProcessingStatus=NEW and TargetRecordID=NULL"
                )
            continue
        bind = {
            "batch_id": batch_id,
            "file_name": file_name,
            "source_row_number": source_row_number,
        }
        for index, value in enumerate(raw_values):
            bind[f"raw_{index}"] = value
        pending.append(bind)

    raw_bind_names = [f":raw_{index}" for index in range(len(definition["raw_columns"]))]
    insert_sql = f"""
        INSERT INTO {definition['table']} (
            {definition['id_column']}, LoadBatchID, SourceFileName, SourceRowNumber,
            {', '.join(definition['raw_columns'])}, ProcessingStatus, TargetRecordID
        ) VALUES (
            {definition['sequence']}.NEXTVAL, :batch_id, :file_name, :source_row_number,
            {', '.join(raw_bind_names)}, 'NEW', NULL
        )
    """
    cursor = connection.cursor()
    inserted = 0
    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        cursor.executemany(insert_sql, chunk)
        connection.commit()
        inserted += len(chunk)
        update_progress(connection, batch_id, "STARTED")
    return {"source_rows": len(rows), "inserted_rows": inserted, "already_staged_rows": len(rows) - inserted}


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    data_dir = args.data_dir.resolve()
    oracle_config = load_json(args.oracle_config.resolve())
    manifest = load_json(data_dir / "generation_manifest.json")
    batch_metadata = manifest["batches"][args.batch]
    run_key = batch_metadata["run_key"]
    if len(run_key) > 50:
        raise ValueError("Run key exceeds LoadBatch.SourceSystem VARCHAR2(50)")

    for file_name in batch_metadata["files"]:
        expected_hash = manifest["files"][file_name]["sha256"]
        actual_hash = sha256_file(data_dir / file_name)
        if actual_hash != expected_hash:
            raise ValueError(f"{file_name}: hash differs from generation_manifest.json; regenerate or use the correct file")

    result_file = args.result_file or root / "run_outputs" / f"{args.batch}_load_result.json"
    connection = connect(oracle_config)
    batch_id: int | None = None
    try:
        batch_id, resumed, previous_status = find_or_open_batch(connection, run_key)
        details = {}
        for file_name in batch_metadata["files"]:
            details[file_name] = load_file(
                connection,
                data_dir,
                file_name,
                batch_id,
                int(oracle_config.get("batch_size", 1000)),
            )
        staged_total = update_progress(connection, batch_id, "STAGED")
        if staged_total != int(batch_metadata["expected_rows"]):
            raise RuntimeError(
                f"Batch {batch_id}: staged {staged_total}, expected {batch_metadata['expected_rows']}"
            )
        result = {
            "status": "STAGED",
            "batch_name": args.batch,
            "load_batch_id": batch_id,
            "run_key": run_key,
            "resumed_existing_batch": resumed,
            "previous_status": previous_status,
            "files": details,
            "staged_rows": staged_total,
            "rows_accepted": 0,
            "rows_rejected": 0,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        write_json(result_file.resolve(), result)
        print(json.dumps(result, indent=2))
    except Exception:
        if batch_id is not None:
            try:
                connection.rollback()
                update_progress(connection, batch_id, "FAILED")
            except Exception:
                connection.rollback()
        raise
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
