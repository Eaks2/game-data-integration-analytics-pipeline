#!/usr/bin/env python3
"""Produce machine-readable Phase 3 staging-to-OLTP reconciliation."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from manifest_checks import expected_rows, manifest_preflight
from oracle_support import connect, database_preflight, get_load_batches
from phase3_model import SOURCE_SPECS, read_csv, read_json, write_json


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
        default=root / "run_outputs" / "phase3_verification.json",
    )
    return parser.parse_args()


def query_scalar(connection: Any, sql: str, **binds: Any) -> int:
    cursor = connection.cursor()
    cursor.execute(sql, binds)
    return int(cursor.fetchone()[0])


def query_dicts(connection: Any, sql: str, **binds: Any) -> list[dict[str, Any]]:
    cursor = connection.cursor()
    cursor.execute(sql, binds)
    columns = [item[0].lower() for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor]


def safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        decimal = Decimal(str(value))
    except InvalidOperation:
        return None
    if not decimal.is_finite() or decimal != decimal.to_integral_value():
        return None
    return int(decimal)


def add_check(
    checks: list[dict[str, Any]],
    name: str,
    passed: bool,
    actual: Any,
    expected: Any,
    failures: list[str],
) -> None:
    checks.append(
        {
            "name": name,
            "status": "PASS" if passed else "FAIL",
            "actual": actual,
            "expected": expected,
        }
    )
    if not passed:
        failures.append(f"{name}: actual={actual!r}, expected={expected!r}")


def fetch_staging_file(
    connection: Any, file_name: str, batch_id: int
) -> list[dict[str, Any]]:
    spec = SOURCE_SPECS[file_name]
    raw_columns = list(spec["fields"].values())
    sql = f"""
        SELECT {spec['id_column']} AS StagingID,
               SourceRowNumber, ProcessingStatus, TargetRecordID,
               {', '.join(raw_columns)}
          FROM {spec['stage_table']}
         WHERE LoadBatchID = :batch_id
           AND SourceFileName = :file_name
         ORDER BY SourceRowNumber
    """
    return query_dicts(connection, sql, batch_id=batch_id, file_name=file_name)


def manifest_reason_comparison(
    manifest_dir: Path,
    error_code_map: dict[str, list[str]],
    rejection_by_row: dict[tuple[str, int], list[dict[str, Any]]],
) -> dict[str, Any]:
    cases = read_csv(manifest_dir / "error_manifest.csv")
    mismatches = []
    case_keys = {
        (case["source_file"], int(case["source_row_number"])) for case in cases
    }
    for case in cases:
        key = (case["source_file"], int(case["source_row_number"]))
        actual = rejection_by_row.get(key, [])
        actual_codes = {str(row["errorcode"]) for row in actual}
        allowed = set(error_code_map.get(case["error_type"], []))
        if case["multi_error"] == "Y":
            missing_codes = allowed - actual_codes
            expected_fields = set(case["affected_fields"].split("|"))
            actual_fields = {
                str(row["errorfieldname"])
                for row in actual
                if row["errorfieldname"] is not None
            }
            missing_fields = expected_fields - actual_fields
            if missing_codes or missing_fields:
                mismatches.append(
                    {
                        "source_file": key[0],
                        "source_row_number": key[1],
                        "error_type": case["error_type"],
                        "missing_codes": sorted(missing_codes),
                        "missing_fields": sorted(missing_fields),
                        "actual_codes": sorted(actual_codes),
                    }
                )
        elif not actual_codes.intersection(allowed):
            mismatches.append(
                {
                    "source_file": key[0],
                    "source_row_number": key[1],
                    "error_type": case["error_type"],
                    "allowed_codes": sorted(allowed),
                    "actual_codes": sorted(actual_codes),
                }
            )
    dependent_parents = [
        item
        for item in read_csv(manifest_dir / "match_row_expectations.csv")
        if item["expected_disposition"] == "REJECT"
        and ("match.csv", int(item["source_row_number"])) not in case_keys
    ]
    for item in dependent_parents:
        key = ("match.csv", int(item["source_row_number"]))
        actual_codes = {
            str(row["errorcode"]) for row in rejection_by_row.get(key, [])
        }
        if "MAT_GROUP_INVALID" not in actual_codes:
            mismatches.append(
                {
                    "source_file": key[0],
                    "source_row_number": key[1],
                    "error_type": "DEPENDENT_INVALID_MATCH_PARENT",
                    "required_code": "MAT_GROUP_INVALID",
                    "actual_codes": sorted(actual_codes),
                }
            )
    return {
        "status": "PASS" if not mismatches else "FAIL",
        "expected_error_cases": len(cases),
        "dependent_invalid_parent_cases": len(dependent_parents),
        "reason_mismatch_count": len(mismatches),
        "reason_mismatches": mismatches[:100],
    }


def rejected_business_key_leaks(
    staged: dict[str, list[dict[str, Any]]],
    oltp_keys: dict[str, set[Any]],
) -> list[dict[str, Any]]:
    leaks = []
    key_fields = {
        "player.csv": ("rawplayerid",),
        "match.csv": ("rawmatchid",),
        "participation.csv": ("rawmatchid", "rawplayerid"),
        "season_ranking.csv": ("rawplayerid", "rawseasonid"),
    }
    for file_name, fields in key_fields.items():
        accepted_keys = set()
        rejected = []
        for row in staged[file_name]:
            parsed = tuple(safe_int(row[field]) for field in fields)
            if any(value is None for value in parsed):
                continue
            key: Any = parsed[0] if len(parsed) == 1 else parsed
            if row["processingstatus"] == "LOADED":
                accepted_keys.add(key)
            elif row["processingstatus"] == "REJECTED":
                rejected.append((row, key))
        for row, key in rejected:
            if key not in accepted_keys and key in oltp_keys[file_name]:
                leaks.append(
                    {
                        "source_file": file_name,
                        "source_row_number": int(row["sourcerownumber"]),
                        "business_key": key,
                    }
                )
    return leaks


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    config = read_json(args.phase3_config.resolve())
    oracle_config = read_json(args.oracle_config.resolve())
    manifest_path = Path(config["manifest_directory"])
    manifest_dir = (
        manifest_path.resolve()
        if manifest_path.is_absolute()
        else (root / manifest_path).resolve()
    )
    manifest_check = manifest_preflight(manifest_dir, config)
    generation = manifest_check["generation_manifest"]
    references = read_json(manifest_dir / "reference_prerequisites.json")
    error_code_map = read_json(root / "config" / "error_code_map.json")
    initial_batch_id = int(config["initial_load_batch_id"])
    scd2_batch_id = int(config["scd2_load_batch_id"])
    checks: list[dict[str, Any]] = []
    failures: list[str] = list(manifest_check["failures"])
    result: dict[str, Any] = {
        "phase": 3,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generation_id": generation["generation_id"],
    }

    connection = None
    try:
        connection = connect(oracle_config)
        database_check = database_preflight(connection, references)
        result["database_preflight"] = {
            "status": database_check["status"],
            "failures": database_check["failures"],
        }
        failures.extend(database_check["failures"])

        batches = get_load_batches(connection, [initial_batch_id, scd2_batch_id])
        result["load_batches"] = batches
        for batch_id, batch_name in ((initial_batch_id, "initial"), (scd2_batch_id, "scd2")):
            expected_batch_rows = int(generation["batches"][batch_name]["expected_rows"])
            expected_rejected = 298 if batch_name == "initial" else 0
            expected_accepted = expected_batch_rows - expected_rejected
            batch = batches.get(batch_id, {})
            add_check(checks, f"LoadBatch {batch_id} status", batch.get("load_status") == "COMPLETED", batch.get("load_status"), "COMPLETED", failures)
            add_check(checks, f"LoadBatch {batch_id} RowsRead", batch.get("rows_read") == expected_batch_rows, batch.get("rows_read"), expected_batch_rows, failures)
            add_check(checks, f"LoadBatch {batch_id} RowsAccepted", batch.get("rows_accepted") == expected_accepted, batch.get("rows_accepted"), expected_accepted, failures)
            add_check(checks, f"LoadBatch {batch_id} RowsRejected", batch.get("rows_rejected") == expected_rejected, batch.get("rows_rejected"), expected_rejected, failures)

        staged = {
            "player.csv": fetch_staging_file(connection, "player.csv", initial_batch_id),
            "player_scd2_update.csv": fetch_staging_file(connection, "player_scd2_update.csv", scd2_batch_id),
            "match.csv": fetch_staging_file(connection, "match.csv", initial_batch_id),
            "participation.csv": fetch_staging_file(connection, "participation.csv", initial_batch_id),
            "season_ranking.csv": fetch_staging_file(connection, "season_ranking.csv", initial_batch_id),
        }
        status_report = {}
        table_status_counts: dict[str, Counter[str]] = defaultdict(Counter)
        for file_name, rows in staged.items():
            counts = Counter(str(row["processingstatus"]) for row in rows)
            status_report[file_name] = dict(sorted(counts.items()))
            table_status_counts[SOURCE_SPECS[file_name]["stage_table"]].update(counts)
            expected_count = int(generation["files"][file_name]["row_count"])
            add_check(checks, f"{file_name} staged count", len(rows) == expected_count, len(rows), expected_count, failures)
            bad_target = sum(
                (row["processingstatus"] == "LOADED" and row["targetrecordid"] is None)
                or (row["processingstatus"] == "REJECTED" and row["targetrecordid"] is not None)
                for row in rows
            )
            add_check(checks, f"{file_name} target/status integrity", bad_target == 0, bad_target, 0, failures)
        result["staging_by_file_and_status"] = status_report
        result["staging_by_table_and_status"] = {
            table_name: dict(sorted(counts.items()))
            for table_name, counts in sorted(table_status_counts.items())
        }

        expected = expected_rows(manifest_dir)
        disposition_mismatches = []
        for file_name in ("player.csv", "match.csv", "participation.csv", "season_ranking.csv"):
            actual_by_row = {int(row["sourcerownumber"]): row for row in staged[file_name]}
            for (expected_file, row_number), expectation in expected.items():
                if expected_file != file_name:
                    continue
                actual = actual_by_row.get(row_number)
                wanted = "LOADED" if expectation["expected_disposition"] == "ACCEPT" else "REJECTED"
                if actual is None or actual["processingstatus"] != wanted:
                    disposition_mismatches.append(
                        {
                            "source_file": file_name,
                            "source_row_number": row_number,
                            "expected": wanted,
                            "actual": None if actual is None else actual["processingstatus"],
                        }
                    )
        for row in staged["player_scd2_update.csv"]:
            if row["processingstatus"] != "LOADED":
                disposition_mismatches.append(
                    {
                        "source_file": "player_scd2_update.csv",
                        "source_row_number": int(row["sourcerownumber"]),
                        "expected": "LOADED",
                        "actual": row["processingstatus"],
                    }
                )
        result["expected_vs_actual_disposition"] = {
            "status": "PASS" if not disposition_mismatches else "FAIL",
            "mismatch_count": len(disposition_mismatches),
            "mismatches": disposition_mismatches[:100],
        }
        add_check(checks, "Phase 2 expected dispositions", not disposition_mismatches, len(disposition_mismatches), 0, failures)

        rejection_rows = query_dicts(
            connection,
            """
            SELECT LoadBatchID, SourceFileName, SourceTable, SourceRowNumber,
                   StagingRecordID, ErrorCode, ErrorFieldName, ErrorReason, RawValue
              FROM RejectedRecord
             WHERE LoadBatchID IN (:b1, :b2)
             ORDER BY SourceFileName, SourceRowNumber, ErrorCode
            """,
            b1=initial_batch_id,
            b2=scd2_batch_id,
        )
        rejection_by_row: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        rejection_counts_file = Counter()
        rejection_counts_code = Counter()
        for row in rejection_rows:
            key = (str(row["sourcefilename"]), int(row["sourcerownumber"]))
            rejection_by_row[key].append(row)
            rejection_counts_file[str(row["sourcefilename"])] += 1
            rejection_counts_code[str(row["errorcode"])] += 1
        result["rejections"] = {
            "rejected_record_rows": len(rejection_rows),
            "distinct_rejected_staging_rows": len(rejection_by_row),
            "reason_rows_by_source_file": dict(sorted(rejection_counts_file.items())),
            "reason_rows_by_error_code": dict(sorted(rejection_counts_code.items())),
        }
        add_check(checks, "distinct rejected staging rows", len(rejection_by_row) == 298, len(rejection_by_row), 298, failures)
        add_check(checks, "RejectedRecord reason rows", len(rejection_rows) == 307, len(rejection_rows), 307, failures)
        reason_comparison = manifest_reason_comparison(manifest_dir, error_code_map, rejection_by_row)
        result["expected_vs_actual_rejection_reasons"] = reason_comparison
        add_check(checks, "Phase 2 expected rejection reasons", reason_comparison["status"] == "PASS", reason_comparison["reason_mismatch_count"], 0, failures)

        target_mapping_checks = {
            "player": query_scalar(connection, """
                SELECT COUNT(*) FROM StgPlayer sp
                 WHERE sp.LoadBatchID IN (:b1, :b2)
                   AND sp.ProcessingStatus = 'LOADED'
                   AND NOT EXISTS (
                       SELECT 1 FROM Player p
                        WHERE p.PlayerID = sp.TargetRecordID
                          AND TO_CHAR(p.PlayerID) = TRIM(sp.RawPlayerID)
                   )
            """, b1=initial_batch_id, b2=scd2_batch_id),
            "match": query_scalar(connection, """
                SELECT COUNT(*) FROM StgMatch sm
                 WHERE sm.LoadBatchID = :b1 AND sm.ProcessingStatus = 'LOADED'
                   AND NOT EXISTS (
                       SELECT 1 FROM Match m
                        WHERE m.MatchID = sm.TargetRecordID
                          AND TO_CHAR(m.MatchID) = TRIM(sm.RawMatchID)
                   )
            """, b1=initial_batch_id),
            "participation": query_scalar(connection, """
                SELECT COUNT(*) FROM StgParticipation sp
                 WHERE sp.LoadBatchID = :b1 AND sp.ProcessingStatus = 'LOADED'
                   AND NOT EXISTS (
                       SELECT 1 FROM MatchParticipation mp
                        WHERE mp.MatchParticipationID = sp.TargetRecordID
                          AND TO_CHAR(mp.MatchID) = TRIM(sp.RawMatchID)
                          AND TO_CHAR(mp.PlayerID) = TRIM(sp.RawPlayerID)
                   )
            """, b1=initial_batch_id),
            "season_ranking": query_scalar(connection, """
                SELECT COUNT(*) FROM StgSeasonRanking ssr
                 WHERE ssr.LoadBatchID = :b1 AND ssr.ProcessingStatus = 'LOADED'
                   AND NOT EXISTS (
                       SELECT 1 FROM SeasonRanking sr
                        WHERE sr.SeasonRankingID = ssr.TargetRecordID
                          AND TO_CHAR(sr.PlayerID) = TRIM(ssr.RawPlayerID)
                          AND TO_CHAR(sr.SeasonID) = TRIM(ssr.RawSeasonID)
                   )
            """, b1=initial_batch_id),
        }
        result["invalid_target_mappings"] = target_mapping_checks
        add_check(checks, "accepted TargetRecordID mappings", sum(target_mapping_checks.values()) == 0, sum(target_mapping_checks.values()), 0, failures)

        oltp_counts = {
            table: query_scalar(connection, f"SELECT COUNT(*) FROM {table}")
            for table in (
                "Player",
                "Match",
                "MatchParticipation",
                "MatchResult",
                "SeasonRanking",
                "SeasonRatingChange",
            )
        }
        result["oltp_total_row_counts"] = oltp_counts
        source_target_counts = {
            "players": len({int(row["targetrecordid"]) for row in staged["player.csv"] if row["processingstatus"] == "LOADED"}),
            "matches": len({int(row["targetrecordid"]) for row in staged["match.csv"] if row["processingstatus"] == "LOADED"}),
            "participation": len({int(row["targetrecordid"]) for row in staged["participation.csv"] if row["processingstatus"] == "LOADED"}),
            "rankings": len({int(row["targetrecordid"]) for row in staged["season_ranking.csv"] if row["processingstatus"] == "LOADED"}),
        }
        result["distinct_source_target_counts"] = source_target_counts
        expected_source_targets = {"players": 2560, "matches": 3624, "participation": 36000, "rankings": 7500}
        add_check(checks, "distinct accepted OLTP targets", source_target_counts == expected_source_targets, source_target_counts, expected_source_targets, failures)

        orphan_checks = {
            "participation_match": query_scalar(connection, "SELECT COUNT(*) FROM MatchParticipation mp LEFT JOIN Match m ON m.MatchID=mp.MatchID WHERE m.MatchID IS NULL"),
            "participation_player": query_scalar(connection, "SELECT COUNT(*) FROM MatchParticipation mp LEFT JOIN Player p ON p.PlayerID=mp.PlayerID WHERE p.PlayerID IS NULL"),
            "participation_role": query_scalar(connection, "SELECT COUNT(*) FROM MatchParticipation mp LEFT JOIN Role r ON r.RoleID=mp.RoleID WHERE r.RoleID IS NULL"),
            "participation_character": query_scalar(connection, "SELECT COUNT(*) FROM MatchParticipation mp LEFT JOIN Character c ON c.CharacterID=mp.CharacterID WHERE c.CharacterID IS NULL"),
            "result_participation": query_scalar(connection, "SELECT COUNT(*) FROM MatchResult mr LEFT JOIN MatchParticipation mp ON mp.MatchParticipationID=mr.MatchParticipationID WHERE mp.MatchParticipationID IS NULL"),
            "ranking_player": query_scalar(connection, "SELECT COUNT(*) FROM SeasonRanking sr LEFT JOIN Player p ON p.PlayerID=sr.PlayerID WHERE p.PlayerID IS NULL"),
            "ranking_season": query_scalar(connection, "SELECT COUNT(*) FROM SeasonRanking sr LEFT JOIN Season s ON s.SeasonID=sr.SeasonID WHERE s.SeasonID IS NULL"),
            "history_ranking": query_scalar(connection, "SELECT COUNT(*) FROM SeasonRatingChange src LEFT JOIN SeasonRanking sr ON sr.SeasonRankingID=src.SeasonRankingID WHERE sr.SeasonRankingID IS NULL"),
        }
        result["orphan_fk_checks"] = orphan_checks
        add_check(checks, "orphan foreign keys", sum(orphan_checks.values()) == 0, sum(orphan_checks.values()), 0, failures)

        duplicate_checks = {
            "player_username_ci": query_scalar(connection, "SELECT COUNT(*) FROM (SELECT LOWER(Username) FROM Player GROUP BY LOWER(Username) HAVING COUNT(*)>1)"),
            "player_email_ci": query_scalar(connection, "SELECT COUNT(*) FROM (SELECT LOWER(Email) FROM Player GROUP BY LOWER(Email) HAVING COUNT(*)>1)"),
            "participation_match_player": query_scalar(connection, "SELECT COUNT(*) FROM (SELECT MatchID,PlayerID FROM MatchParticipation GROUP BY MatchID,PlayerID HAVING COUNT(*)>1)"),
            "ranking_player_season": query_scalar(connection, "SELECT COUNT(*) FROM (SELECT PlayerID,SeasonID FROM SeasonRanking GROUP BY PlayerID,SeasonID HAVING COUNT(*)>1)"),
        }
        result["duplicate_business_key_checks"] = duplicate_checks
        add_check(checks, "duplicate natural/business keys", sum(duplicate_checks.values()) == 0, sum(duplicate_checks.values()), 0, failures)

        completed_inconsistency = query_scalar(connection, """
            SELECT COUNT(*)
              FROM (
                    SELECT m.MatchID,
                           COUNT(DISTINCT mp.MatchParticipationID) AS ParticipantCount,
                           SUM(CASE WHEN mp.TeamAssignment='TEAM_A' THEN 1 ELSE 0 END) AS TeamA,
                           SUM(CASE WHEN mp.TeamAssignment='TEAM_B' THEN 1 ELSE 0 END) AS TeamB,
                           COUNT(mr.MatchParticipationID) AS ResultCount,
                           SUM(CASE WHEN mp.TeamAssignment='TEAM_A' AND mr.PlayerResult='WIN' THEN 1 ELSE 0 END) AS TAW,
                           SUM(CASE WHEN mp.TeamAssignment='TEAM_A' AND mr.PlayerResult='LOSS' THEN 1 ELSE 0 END) AS TAL,
                           SUM(CASE WHEN mp.TeamAssignment='TEAM_A' AND mr.PlayerResult='DRAW' THEN 1 ELSE 0 END) AS TAD,
                           SUM(CASE WHEN mp.TeamAssignment='TEAM_B' AND mr.PlayerResult='WIN' THEN 1 ELSE 0 END) AS TBW,
                           SUM(CASE WHEN mp.TeamAssignment='TEAM_B' AND mr.PlayerResult='LOSS' THEN 1 ELSE 0 END) AS TBL,
                           SUM(CASE WHEN mp.TeamAssignment='TEAM_B' AND mr.PlayerResult='DRAW' THEN 1 ELSE 0 END) AS TBD
                      FROM Match m
                      JOIN StgMatch sm ON sm.TargetRecordID=m.MatchID
                                      AND sm.LoadBatchID=:b1
                                      AND sm.ProcessingStatus='LOADED'
                      LEFT JOIN MatchParticipation mp ON mp.MatchID=m.MatchID
                      LEFT JOIN MatchResult mr ON mr.MatchParticipationID=mp.MatchParticipationID
                     WHERE m.MatchStatus='COMPLETED'
                     GROUP BY m.MatchID
                   ) q
             WHERE ParticipantCount<>10 OR TeamA<>5 OR TeamB<>5 OR ResultCount<>10
                OR NOT (
                    (TAW=5 AND TAL=0 AND TAD=0 AND TBW=0 AND TBL=5 AND TBD=0)
                    OR (TAW=0 AND TAL=5 AND TAD=0 AND TBW=5 AND TBL=0 AND TBD=0)
                    OR (TAW=0 AND TAL=0 AND TAD=5 AND TBW=0 AND TBL=0 AND TBD=5)
                )
        """, b1=initial_batch_id)
        result["completed_match_inconsistency_count"] = completed_inconsistency
        add_check(checks, "completed 10-player/5v5/result consistency", completed_inconsistency == 0, completed_inconsistency, 0, failures)

        rating_reconciliation = query_dicts(connection, """
            WITH accepted_rankings AS (
                SELECT ssr.TargetRecordID AS SeasonRankingID,
                       TO_NUMBER(ssr.RawCurrentRating) AS ExpectedFinalRating,
                       UPPER(TRIM(ssr.RawRankTier)) AS ExpectedTier
                  FROM StgSeasonRanking ssr
                 WHERE ssr.LoadBatchID=:b1
                   AND ssr.ProcessingStatus='LOADED'
            ),
            source_events AS (
                SELECT mp.PlayerID, m.SeasonID, mr.RatingChange
                  FROM StgParticipation sp
                  JOIN MatchParticipation mp
                    ON mp.MatchParticipationID=sp.TargetRecordID
                  JOIN Match m ON m.MatchID=mp.MatchID
                  JOIN MatchResult mr
                    ON mr.MatchParticipationID=mp.MatchParticipationID
                 WHERE sp.LoadBatchID=:b1
                   AND sp.ProcessingStatus='LOADED'
            ),
            event_totals AS (
                SELECT sr.SeasonRankingID,
                       NVL(SUM(se.RatingChange),0) AS EventDelta,
                       SUM(CASE WHEN se.RatingChange<>0 THEN 1 ELSE 0 END) AS NonzeroEvents
                  FROM SeasonRanking sr
                  JOIN accepted_rankings ar ON ar.SeasonRankingID=sr.SeasonRankingID
                  LEFT JOIN source_events se
                    ON se.PlayerID=sr.PlayerID AND se.SeasonID=sr.SeasonID
                 GROUP BY sr.SeasonRankingID
            ),
            history_totals AS (
                SELECT sr.SeasonRankingID,
                       NVL(SUM(src.RatingDelta),0) AS HistoryDelta,
                       COUNT(src.SeasonRatingChangeID) AS HistoryRows
                  FROM SeasonRanking sr
                  JOIN accepted_rankings ar ON ar.SeasonRankingID=sr.SeasonRankingID
                  LEFT JOIN SeasonRatingChange src ON src.SeasonRankingID=sr.SeasonRankingID
                 GROUP BY sr.SeasonRankingID
            )
            SELECT COUNT(*) AS MismatchCount,
                   SUM(et.NonzeroEvents) AS ExpectedHistoryRows,
                   SUM(ht.HistoryRows) AS ActualHistoryRows
              FROM accepted_rankings ar
              JOIN SeasonRanking sr ON sr.SeasonRankingID=ar.SeasonRankingID
              JOIN event_totals et ON et.SeasonRankingID=ar.SeasonRankingID
              JOIN history_totals ht ON ht.SeasonRankingID=ar.SeasonRankingID
             WHERE sr.SeasonCurrentRating<>ar.ExpectedFinalRating
                OR sr.SeasonRankTier<>ar.ExpectedTier
                OR et.EventDelta<>ht.HistoryDelta
                OR et.NonzeroEvents<>ht.HistoryRows
                OR ar.ExpectedFinalRating-et.EventDelta NOT BETWEEN 0 AND 5000
        """, b1=initial_batch_id)[0]
        # Aggregate queries with a WHERE return NULL sums when there are no mismatches;
        # get global expected/actual history counts separately for a complete report.
        history_totals = query_dicts(connection, """
            SELECT
                (SELECT COUNT(*)
                   FROM StgParticipation sp
                  WHERE sp.LoadBatchID=:b1 AND sp.ProcessingStatus='LOADED'
                    AND TO_NUMBER(sp.RawRatingChange)<>0) AS ExpectedRows,
                (SELECT COUNT(*)
                   FROM SeasonRatingChange src
                   JOIN StgSeasonRanking ssr ON ssr.TargetRecordID=src.SeasonRankingID
                                             AND ssr.LoadBatchID=:b1
                                             AND ssr.ProcessingStatus='LOADED') AS ActualRows
              FROM dual
        """, b1=initial_batch_id)[0]
        rating_mismatches = int(rating_reconciliation["mismatchcount"])
        result["season_rating_reconciliation"] = {
            "mismatch_count": rating_mismatches,
            "expected_nonzero_history_rows": int(history_totals["expectedrows"]),
            "actual_history_rows": int(history_totals["actualrows"]),
        }
        add_check(checks, "season rating and history reconciliation", rating_mismatches == 0 and int(history_totals["expectedrows"]) == int(history_totals["actualrows"]), result["season_rating_reconciliation"], {"mismatch_count": 0, "history_counts_equal": True}, failures)

        scd2_mismatches = query_scalar(connection, """
            SELECT COUNT(*)
              FROM StgPlayer update_row
              LEFT JOIN StgPlayer initial_row
                ON initial_row.LoadBatchID=:b1
               AND initial_row.SourceFileName='player.csv'
               AND initial_row.ProcessingStatus='LOADED'
               AND initial_row.RawPlayerID=update_row.RawPlayerID
              LEFT JOIN Player p ON p.PlayerID=update_row.TargetRecordID
             WHERE update_row.LoadBatchID=:b2
               AND update_row.SourceFileName='player_scd2_update.csv'
               AND (
                   update_row.ProcessingStatus<>'LOADED'
                   OR initial_row.StgPlayerID IS NULL
                   OR p.PlayerID IS NULL
                   OR p.Region<>TRIM(update_row.RawRegion)
                   OR p.AccountLevel<>TO_NUMBER(update_row.RawAccountLevel)
                   OR p.AccountStatus<>UPPER(TRIM(update_row.RawAccountStatus))
                   OR NOT (
                       TRIM(initial_row.RawUsername)<>TRIM(update_row.RawUsername)
                       OR TRIM(initial_row.RawRegion)<>TRIM(update_row.RawRegion)
                       OR TO_NUMBER(initial_row.RawAccountLevel)<>TO_NUMBER(update_row.RawAccountLevel)
                       OR UPPER(TRIM(initial_row.RawAccountStatus))<>UPPER(TRIM(update_row.RawAccountStatus))
                   )
               )
        """, b1=initial_batch_id, b2=scd2_batch_id)
        result["scd2_update_preservation_mismatch_count"] = scd2_mismatches
        add_check(checks, "25 legitimate later player versions preserved and applied", scd2_mismatches == 0, scd2_mismatches, 0, failures)

        scd2_expected = {
            int(item["PlayerID"]): item
            for item in read_csv(manifest_dir / "scd2_update_manifest.csv")
        }
        initial_versions = {
            safe_int(row["rawplayerid"]): row
            for row in staged["player.csv"]
            if row["processingstatus"] == "LOADED"
            and safe_int(row["rawplayerid"]) is not None
        }
        later_versions = {
            safe_int(row["rawplayerid"]): row
            for row in staged["player_scd2_update.csv"]
            if row["processingstatus"] == "LOADED"
            and safe_int(row["rawplayerid"]) is not None
        }
        scd2_fixture_mismatches = []
        if set(later_versions) != set(scd2_expected):
            scd2_fixture_mismatches.append(
                {
                    "problem": "PlayerID set differs from scd2_update_manifest.csv",
                    "missing_player_ids": sorted(set(scd2_expected) - set(later_versions)),
                    "unexpected_player_ids": sorted(set(later_versions) - set(scd2_expected)),
                }
            )
        for player_id in sorted(set(scd2_expected).intersection(later_versions)):
            expected_version = scd2_expected[player_id]
            initial_row = initial_versions.get(player_id)
            later_row = later_versions[player_id]
            comparisons = {
                "Username": (
                    None if initial_row is None else str(initial_row["rawusername"]).strip(),
                    str(later_row["rawusername"]).strip(),
                    expected_version["Username"],
                    expected_version["Username"],
                ),
                "Region": (
                    None if initial_row is None else str(initial_row["rawregion"]).strip(),
                    str(later_row["rawregion"]).strip(),
                    expected_version["OldRegion"],
                    expected_version["NewRegion"],
                ),
                "AccountLevel": (
                    None if initial_row is None else safe_int(initial_row["rawaccountlevel"]),
                    safe_int(later_row["rawaccountlevel"]),
                    int(expected_version["OldAccountLevel"]),
                    int(expected_version["NewAccountLevel"]),
                ),
                "AccountStatus": (
                    None if initial_row is None else str(initial_row["rawaccountstatus"]).strip().upper(),
                    str(later_row["rawaccountstatus"]).strip().upper(),
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
                scd2_fixture_mismatches.append(
                    {
                        "player_id": player_id,
                        "initial_source_row_found": initial_row is not None,
                        "field_mismatches": field_mismatches,
                    }
                )
        result["scd2_manifest_comparison"] = {
            "status": "PASS" if not scd2_fixture_mismatches else "FAIL",
            "expected_players": len(scd2_expected),
            "actual_later_players": len(later_versions),
            "mismatch_count": len(scd2_fixture_mismatches),
            "mismatches": scd2_fixture_mismatches[:100],
        }
        add_check(checks, "staged old/new Player versions match SCD2 fixture", not scd2_fixture_mismatches, len(scd2_fixture_mismatches), 0, failures)

        oltp_keys = {
            "player.csv": {int(row["playerid"]) for row in query_dicts(connection, "SELECT PlayerID FROM Player")},
            "match.csv": {int(row["matchid"]) for row in query_dicts(connection, "SELECT MatchID FROM Match")},
            "participation.csv": {(int(row["matchid"]), int(row["playerid"])) for row in query_dicts(connection, "SELECT MatchID,PlayerID FROM MatchParticipation")},
            "season_ranking.csv": {(int(row["playerid"]), int(row["seasonid"])) for row in query_dicts(connection, "SELECT PlayerID,SeasonID FROM SeasonRanking")},
        }
        leaks = rejected_business_key_leaks(staged, oltp_keys)
        result["rejected_unique_business_keys_found_in_oltp"] = leaks[:100]
        add_check(checks, "rejected-only business keys absent from OLTP", not leaks, len(leaks), 0, failures)

        result["checks"] = checks
        result["failure_count"] = len(failures)
        result["failures"] = failures
        result["status"] = "PASS" if not failures else "FAIL"
        write_json(args.output.resolve(), result)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["status"] == "PASS" else 1
    except Exception as exc:
        failures.append(f"verification execution failed: {type(exc).__name__}: {exc}")
        result.setdefault("checks", checks)
        result["failure_count"] = len(failures)
        result["failures"] = failures
        result["status"] = "FAIL"
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        write_json(args.output.resolve(), result)
        print(json.dumps(result, indent=2, default=str), file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
