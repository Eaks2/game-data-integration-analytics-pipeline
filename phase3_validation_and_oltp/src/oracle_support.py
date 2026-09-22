"""Oracle access, staging state changes, and normalized OLTP loading."""

from __future__ import annotations

import os
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from phase3_model import (
    ENTITY_STAGE_SPEC,
    SOURCE_SPECS,
    PlayerState,
    SeasonRef,
    StageRow,
    ValidationBundle,
)


REQUIRED_TABLES = {
    "LOADBATCH",
    "STGPLAYER",
    "STGMATCH",
    "STGPARTICIPATION",
    "STGSEASONRANKING",
    "REJECTEDRECORD",
    "PLAYER",
    "SEASON",
    "ROLE",
    "CHARACTER",
    "MATCH",
    "MATCHPARTICIPATION",
    "MATCHRESULT",
    "SEASONRANKING",
    "SEASONRATINGCHANGE",
}

REQUIRED_TRIGGERS = {
    "TRG_MATCH_SEASON_WINDOW",
    "TRG_MP_5V5_LIMIT",
    "TRG_MP_TERMINAL_GUARD",
    "TRG_MR_TERMINAL_GUARD",
    "TRG_MATCH_STATUS_TRANSITION",
    "TRG_MATCH_COMPLETION",
    "TRG_SEASON_RATING_HISTORY",
}


def connect(config: dict[str, Any]):
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt before using Oracle") from exc
    thick_dir = str(config.get("thick_mode_lib_dir", "")).strip()
    if thick_dir:
        oracledb.init_oracle_client(lib_dir=thick_dir)
    password_name = config.get("password_env", "CS779_ORACLE_PASSWORD")
    password = os.environ.get(password_name)
    if not password:
        raise RuntimeError(f"Set environment variable {password_name} to the Oracle password")
    return oracledb.connect(
        user=config["user"], password=password, dsn=config["dsn"]
    )


def as_date(value: Any) -> date:
    return value.date() if isinstance(value, datetime) else value


def database_preflight(
    connection: Any, expected_references: dict[str, Any]
) -> dict[str, Any]:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT Object_Name, Object_Type, Status FROM User_Objects "
        "WHERE Object_Type IN ('TABLE', 'TRIGGER')"
    )
    objects = {(str(name), str(kind)): str(status) for name, kind, status in cursor}
    failures: list[str] = []
    for table in sorted(REQUIRED_TABLES):
        if (table, "TABLE") not in objects:
            failures.append(f"missing required table {table}")
    for trigger in sorted(REQUIRED_TRIGGERS):
        status = objects.get((trigger, "TRIGGER"))
        if status is None:
            failures.append(f"missing required trigger {trigger}")
        elif status != "VALID":
            failures.append(f"trigger {trigger} is {status}")

    cursor.execute(
        "SELECT Text FROM User_Source "
        "WHERE Name = 'TRG_MATCH_SEASON_WINDOW' AND Type = 'TRIGGER' ORDER BY Line"
    )
    trigger_source = "".join(str(row[0]) for row in cursor).upper()
    compact_source = "".join(trigger_source.split())
    if "TRUNC(CAST(:NEW.MATCHDATETIMEASDATE))" not in compact_source:
        failures.append(
            "TRG_MATCH_SEASON_WINDOW does not contain the required inclusive calendar-date correction; run sql/01_phase3_required_trigger_fix.sql"
        )

    seasons: dict[int, SeasonRef] = {}
    roles: dict[int, tuple[str, str | None]] = {}
    characters: dict[str, int] = {}
    if not failures or all(not item.startswith("missing required table") for item in failures):
        cursor.execute(
            "SELECT SeasonID, SeasonName, StartDate, EndDate, SeasonStatus FROM Season"
        )
        seasons = {
            int(row[0]): SeasonRef(
                season_id=int(row[0]),
                name=str(row[1]),
                start_date=as_date(row[2]),
                end_date=as_date(row[3]),
                status=str(row[4]),
            )
            for row in cursor
        }
        cursor.execute("SELECT RoleID, RoleName, RoleDescription FROM Role")
        roles = {
            int(row[0]): (str(row[1]), None if row[2] is None else str(row[2]))
            for row in cursor
        }
        cursor.execute("SELECT CharacterID, CharacterName FROM Character")
        characters = {str(row[1]).upper(): int(row[0]) for row in cursor}

        for expected in expected_references["seasons"]:
            season_id = int(expected["season_id"])
            actual = seasons.get(season_id)
            expected_tuple = (
                expected["season_name"],
                date.fromisoformat(expected["start_date"]),
                date.fromisoformat(expected["end_date"]),
                expected["season_status"],
            )
            actual_tuple = (
                actual.name,
                actual.start_date,
                actual.end_date,
                actual.status,
            ) if actual else None
            if actual_tuple != expected_tuple:
                failures.append(f"SeasonID {season_id} does not match reference_prerequisites.json")
        for expected in expected_references["roles"]:
            role_id = int(expected["role_id"])
            actual = roles.get(role_id)
            if actual is None or actual[0] != expected["role_name"]:
                failures.append(f"RoleID {role_id} does not match reference_prerequisites.json")
        for expected in expected_references["characters"]:
            if characters.get(expected["character_name"].upper()) != int(expected["character_id"]):
                failures.append(
                    f"Character {expected['character_name']} does not match reference_prerequisites.json"
                )

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "seasons": seasons,
        "role_ids": set(roles),
        "characters_by_name": characters,
    }


def get_load_batches(connection: Any, batch_ids: list[int]) -> dict[int, dict[str, Any]]:
    cursor = connection.cursor()
    binds = ",".join(f":b{index}" for index in range(len(batch_ids)))
    parameters = {f"b{index}": value for index, value in enumerate(batch_ids)}
    cursor.execute(
        f"""
        SELECT LoadBatchID, SourceSystem, RowsRead, RowsAccepted, RowsRejected, LoadStatus
          FROM LoadBatch
         WHERE LoadBatchID IN ({binds})
        """,
        parameters,
    )
    return {
        int(row[0]): {
            "load_batch_id": int(row[0]),
            "source_system": str(row[1]),
            "rows_read": int(row[2]),
            "rows_accepted": int(row[3]),
            "rows_rejected": int(row[4]),
            "load_status": str(row[5]),
        }
        for row in cursor
    }


def fetch_stage_rows(
    connection: Any, file_name: str, batch_id: int
) -> list[StageRow]:
    spec = SOURCE_SPECS[file_name]
    raw_columns = list(spec["fields"].values())
    select_columns = [
        spec["id_column"],
        "LoadBatchID",
        "SourceFileName",
        "SourceRowNumber",
        *raw_columns,
        "ProcessingStatus",
        "TargetRecordID",
    ]
    cursor = connection.cursor()
    cursor.execute(
        f"""
        SELECT {', '.join(select_columns)}
          FROM {spec['stage_table']}
         WHERE LoadBatchID = :batch_id
           AND SourceFileName = :source_file_name
         ORDER BY SourceRowNumber
        """,
        batch_id=batch_id,
        source_file_name=file_name,
    )
    result = []
    for values in cursor:
        record = dict(zip(select_columns, values))
        raw = {
            field: None if record[column] is None else str(record[column])
            for field, column in spec["fields"].items()
        }
        row = StageRow(
            entity=spec["entity"],
            source_table=spec["source_table"],
            staging_id=int(record[spec["id_column"]]),
            load_batch_id=int(record["LoadBatchID"]),
            source_file_name=str(record["SourceFileName"]),
            source_row_number=int(record["SourceRowNumber"]),
            raw=raw,
        )
        row.canonical["_database_processing_status"] = str(record["ProcessingStatus"])
        row.canonical["_database_target_record_id"] = record["TargetRecordID"]
        result.append(row)
    return result


def fetch_existing_players(connection: Any) -> dict[int, PlayerState]:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT PlayerID, Username, Email, Region, AccountLevel, AccountStatus FROM Player"
    )
    return {
        int(row[0]): PlayerState(
            player_id=int(row[0]),
            username=str(row[1]),
            email=str(row[2]),
            region=str(row[3]),
            account_level=int(row[4]),
            account_status=str(row[5]),
        )
        for row in cursor
    }


def staging_input_preflight(
    connection: Any,
    bundle: ValidationBundle,
    generation_manifest: dict[str, Any],
    initial_batch_id: int,
    scd2_batch_id: int,
) -> dict[str, Any]:
    failures: list[str] = []
    batches = get_load_batches(connection, [initial_batch_id, scd2_batch_id])
    if set(batches) != {initial_batch_id, scd2_batch_id}:
        failures.append("LoadBatchID 1 and/or 2 is missing")
        return {"status": "FAIL", "failures": failures, "batches": batches}
    expected_batch_names = {
        initial_batch_id: "initial",
        scd2_batch_id: "scd2",
    }
    for batch_id, name in expected_batch_names.items():
        expected = generation_manifest["batches"][name]
        actual = batches[batch_id]
        if actual["source_system"] != expected["run_key"]:
            failures.append(f"LoadBatchID {batch_id} SourceSystem does not match the Phase 2 manifest")
        if actual["rows_read"] != int(expected["expected_rows"]):
            failures.append(f"LoadBatchID {batch_id} RowsRead does not match the Phase 2 manifest")
        if actual["load_status"] not in {"STAGED", "COMPLETED"}:
            failures.append(f"LoadBatchID {batch_id} has unsupported status {actual['load_status']}")

    if all(item["load_status"] == "COMPLETED" for item in batches.values()):
        return {
            "status": "ALREADY_COMPLETED",
            "failures": failures,
            "batches": batches,
        }
    if any(item["load_status"] == "COMPLETED" for item in batches.values()):
        failures.append("Only one Phase 2 batch is COMPLETED; automatic mixed-state recovery is intentionally refused")

    file_counts = Counter(row.source_file_name for row in bundle.all_rows())
    for file_name, metadata in generation_manifest["files"].items():
        if file_counts.get(file_name, 0) != int(metadata["row_count"]):
            failures.append(
                f"{file_name} staged row count {file_counts.get(file_name, 0)} does not match manifest {metadata['row_count']}"
            )
    for row in bundle.all_rows():
        if row.canonical.get("_database_processing_status") != "NEW":
            failures.append(
                f"{row.source_file_name} row {row.source_row_number} is not NEW"
            )
            break
        if row.canonical.get("_database_target_record_id") is not None:
            failures.append(
                f"{row.source_file_name} row {row.source_row_number} already has TargetRecordID"
            )
            break
    cursor = connection.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM RejectedRecord WHERE LoadBatchID IN (:b1, :b2)",
        b1=initial_batch_id,
        b2=scd2_batch_id,
    )
    if int(cursor.fetchone()[0]) != 0:
        failures.append("RejectedRecord is not empty for the two input batches")
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "batches": batches,
        "staged_file_counts": dict(sorted(file_counts.items())),
    }


def apply_validation_results(connection: Any, bundle: ValidationBundle) -> dict[str, int]:
    cursor = connection.cursor()
    rejected_records = 0
    status_counts: Counter[str] = Counter()
    for entity_rows in (
        bundle.initial_players,
        bundle.update_players,
        bundle.matches,
        bundle.participation,
        bundle.rankings,
    ):
        if not entity_rows:
            continue
        spec = ENTITY_STAGE_SPEC[entity_rows[0].entity]
        valid_binds = []
        rejected_binds = []
        rejection_binds = []
        for row in entity_rows:
            if row.valid:
                valid_binds.append({"staging_id": row.staging_id})
                status_counts["VALID"] += 1
            else:
                rejected_binds.append({"staging_id": row.staging_id})
                status_counts["REJECTED"] += 1
                for error in row.errors:
                    rejection_binds.append(
                        {
                            "load_batch_id": row.load_batch_id,
                            "source_file_name": row.source_file_name,
                            "source_table": row.source_table,
                            "source_row_number": row.source_row_number,
                            "staging_record_id": row.staging_id,
                            "error_code": error.code,
                            "error_field_name": error.field_name,
                            "error_reason": error.reason,
                            "raw_value": error.raw_value,
                        }
                    )
        if valid_binds:
            cursor.executemany(
                f"""
                UPDATE {spec['stage_table']}
                   SET ProcessingStatus = 'VALID', TargetRecordID = NULL
                 WHERE {spec['id_column']} = :staging_id
                   AND ProcessingStatus = 'NEW'
                """,
                valid_binds,
            )
        if rejected_binds:
            cursor.executemany(
                f"""
                UPDATE {spec['stage_table']}
                   SET ProcessingStatus = 'REJECTED', TargetRecordID = NULL
                 WHERE {spec['id_column']} = :staging_id
                   AND ProcessingStatus = 'NEW'
                """,
                rejected_binds,
            )
        if rejection_binds:
            cursor.executemany(
                """
                INSERT INTO RejectedRecord (
                    RejectedRecordID, LoadBatchID, SourceFileName, SourceTable,
                    SourceRowNumber, StagingRecordID, ErrorCode,
                    ErrorFieldName, ErrorReason, RawValue, RejectedDateTime
                )
                SELECT RejectedRecord_Seq.NEXTVAL,
                       :load_batch_id, :source_file_name, :source_table,
                       :source_row_number, :staging_record_id, :error_code,
                       :error_field_name, :error_reason, :raw_value, SYSTIMESTAMP
                  FROM dual
                 WHERE NOT EXISTS (
                       SELECT 1
                         FROM RejectedRecord existing_error
                        WHERE existing_error.LoadBatchID = :load_batch_id
                          AND existing_error.SourceTable = :source_table
                          AND existing_error.StagingRecordID = :staging_record_id
                          AND existing_error.ErrorCode = :error_code
                          AND (
                              existing_error.ErrorFieldName = :error_field_name
                              OR (existing_error.ErrorFieldName IS NULL AND :error_field_name IS NULL)
                          )
                 )
                """,
                rejection_binds,
            )
            rejected_records += len(rejection_binds)
    return {
        "valid_staging_rows": status_counts["VALID"],
        "rejected_staging_rows": status_counts["REJECTED"],
        "rejection_reason_rows": rejected_records,
    }


def load_initial_players(
    connection: Any,
    rows: list[StageRow],
    existing: dict[int, PlayerState],
) -> dict[int, int]:
    cursor = connection.cursor()
    inserts = []
    for row in rows:
        if not row.valid:
            continue
        player_id = row.canonical["PlayerID"]
        if player_id not in existing:
            inserts.append(
                {
                    "player_id": player_id,
                    "username": row.canonical["Username"],
                    "email": row.canonical["Email"],
                    "region": row.canonical["Region"],
                    "account_level": row.canonical["AccountLevel"],
                    "account_status": row.canonical["AccountStatus"],
                }
            )
    if inserts:
        cursor.executemany(
            """
            INSERT INTO Player (
                PlayerID, Username, Email, Region, AccountLevel,
                AccountStatus, CreatedAt
            ) VALUES (
                :player_id, :username, :email, :region, :account_level,
                :account_status, SYSTIMESTAMP
            )
            """,
            inserts,
        )
    return {row.staging_id: row.canonical["PlayerID"] for row in rows if row.valid}


def fetch_existing_matches(connection: Any) -> dict[int, dict[str, Any]]:
    cursor = connection.cursor()
    cursor.execute("SELECT MatchID, SeasonID, MatchStatus, MatchDateTime FROM Match")
    return {
        int(row[0]): {
            "SeasonID": int(row[1]),
            "MatchStatus": str(row[2]),
            "MatchDateTime": row[3],
        }
        for row in cursor
    }


def load_matches(connection: Any, rows: list[StageRow]) -> dict[int, int]:
    cursor = connection.cursor()
    existing = fetch_existing_matches(connection)
    inserts = []
    for row in rows:
        if not row.valid:
            continue
        match_id = row.canonical["MatchID"]
        desired_status = row.canonical["MatchStatus"]
        initial_status = "IN_PROGRESS" if desired_status == "COMPLETED" else desired_status
        actual = existing.get(match_id)
        if actual is None:
            inserts.append(
                {
                    "match_id": match_id,
                    "season_id": row.canonical["SeasonID"],
                    "match_status": initial_status,
                    "match_time": row.canonical["MatchDateTime"],
                }
            )
        else:
            allowed_status = {desired_status, initial_status}
            actual_time = actual["MatchDateTime"]
            if (
                actual["SeasonID"] != row.canonical["SeasonID"]
                or actual["MatchStatus"] not in allowed_status
                or actual_time != row.canonical["MatchDateTime"]
            ):
                raise RuntimeError(f"Existing MatchID {match_id} conflicts with accepted source data")
    if inserts:
        cursor.executemany(
            """
            INSERT INTO Match (MatchID, SeasonID, MatchStatus, MatchDateTime)
            VALUES (:match_id, :season_id, :match_status, :match_time)
            """,
            inserts,
        )
    return {row.staging_id: row.canonical["MatchID"] for row in rows if row.valid}


def fetch_existing_participation(connection: Any) -> dict[tuple[int, int], dict[str, Any]]:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT MatchParticipationID, MatchID, PlayerID, RoleID, CharacterID, TeamAssignment "
        "FROM MatchParticipation"
    )
    return {
        (int(row[1]), int(row[2])): {
            "MatchParticipationID": int(row[0]),
            "RoleID": int(row[3]),
            "CharacterID": int(row[4]),
            "TeamAssignment": str(row[5]),
        }
        for row in cursor
    }


def load_participation(connection: Any, rows: list[StageRow]) -> dict[int, int]:
    cursor = connection.cursor()
    existing = fetch_existing_participation(connection)
    inserts = []
    for row in rows:
        if not row.valid:
            continue
        key = (row.canonical["MatchID"], row.canonical["PlayerID"])
        actual = existing.get(key)
        expected = (
            row.canonical["RoleID"],
            row.canonical["CharacterID"],
            row.canonical["TeamAssignment"],
        )
        if actual is None:
            inserts.append(
                {
                    "match_id": key[0],
                    "player_id": key[1],
                    "role_id": expected[0],
                    "character_id": expected[1],
                    "team_assignment": expected[2],
                }
            )
        elif (
            actual["RoleID"],
            actual["CharacterID"],
            actual["TeamAssignment"],
        ) != expected:
            raise RuntimeError(f"Existing participation {key} conflicts with accepted source data")
    if inserts:
        cursor.executemany(
            """
            INSERT INTO MatchParticipation (
                MatchParticipationID, MatchID, PlayerID, RoleID,
                CharacterID, TeamAssignment
            ) VALUES (
                MatchParticipation_Seq.NEXTVAL, :match_id, :player_id,
                :role_id, :character_id, :team_assignment
            )
            """,
            inserts,
        )
    mapping = fetch_existing_participation(connection)
    return {
        row.staging_id: mapping[(row.canonical["MatchID"], row.canonical["PlayerID"])]["MatchParticipationID"]
        for row in rows
        if row.valid
    }


def load_match_results(
    connection: Any,
    rows: list[StageRow],
    participation_targets: dict[int, int],
) -> None:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT MatchParticipationID, PlayerResult, RatingChange FROM MatchResult"
    )
    existing = {
        int(row[0]): (str(row[1]), Decimal(str(row[2]))) for row in cursor
    }
    inserts = []
    for row in rows:
        if not row.valid:
            continue
        participation_id = participation_targets[row.staging_id]
        expected = (row.canonical["PlayerResult"], row.canonical["RatingChange"])
        if participation_id not in existing:
            inserts.append(
                {
                    "participation_id": participation_id,
                    "player_result": expected[0],
                    "rating_change": expected[1],
                }
            )
        elif existing[participation_id] != expected:
            raise RuntimeError(
                f"Existing MatchResult for MatchParticipationID {participation_id} conflicts with source"
            )
    if inserts:
        cursor.executemany(
            """
            INSERT INTO MatchResult (
                MatchParticipationID, PlayerResult, RatingChange
            ) VALUES (
                :participation_id, :player_result, :rating_change
            )
            """,
            inserts,
        )


def fetch_existing_rankings(connection: Any) -> dict[tuple[int, int], dict[str, Any]]:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT SeasonRankingID, PlayerID, SeasonID, SeasonCurrentRating, SeasonRankTier "
        "FROM SeasonRanking"
    )
    return {
        (int(row[1]), int(row[2])): {
            "SeasonRankingID": int(row[0]),
            "SeasonCurrentRating": Decimal(str(row[3])),
            "SeasonRankTier": str(row[4]),
        }
        for row in cursor
    }


def load_rankings_and_history(
    connection: Any,
    bundle: ValidationBundle,
) -> tuple[dict[int, int], dict[str, int]]:
    cursor = connection.cursor()
    existing = fetch_existing_rankings(connection)
    inserts = []
    keys_to_replay: set[tuple[int, int]] = set()
    valid_rows = [row for row in bundle.rankings if row.valid]
    for row in valid_rows:
        key = (row.canonical["PlayerID"], row.canonical["SeasonID"])
        starting = bundle.rating_start[key]
        final = row.canonical["CurrentRating"]
        actual = existing.get(key)
        if actual is None:
            inserts.append(
                {
                    "player_id": key[0],
                    "season_id": key[1],
                    "starting_rating": starting,
                    "rank_tier": row.canonical["RankTier"],
                }
            )
            keys_to_replay.add(key)
        elif actual["SeasonCurrentRating"] == starting:
            keys_to_replay.add(key)
        elif (
            actual["SeasonCurrentRating"] != final
            or actual["SeasonRankTier"] != row.canonical["RankTier"]
        ):
            raise RuntimeError(f"Existing SeasonRanking {key} conflicts with accepted source data")
    if inserts:
        cursor.executemany(
            """
            INSERT INTO SeasonRanking (
                SeasonRankingID, PlayerID, SeasonID,
                SeasonCurrentRating, SeasonRankTier
            ) VALUES (
                SeasonRanking_Seq.NEXTVAL, :player_id, :season_id,
                :starting_rating, :rank_tier
            )
            """,
            inserts,
        )
    mapping = fetch_existing_rankings(connection)
    replay_binds = []
    nonzero_events = 0
    for row in valid_rows:
        key = (row.canonical["PlayerID"], row.canonical["SeasonID"])
        if key not in keys_to_replay:
            continue
        ranking_id = mapping[key]["SeasonRankingID"]
        for event_time, staging_id, delta in bundle.rating_replay.get(key, []):
            replay_binds.append(
                {
                    "rating_delta": delta,
                    "season_ranking_id": ranking_id,
                }
            )
            nonzero_events += delta != 0
    if replay_binds:
        cursor.executemany(
            """
            UPDATE SeasonRanking
               SET SeasonCurrentRating = SeasonCurrentRating + :rating_delta
             WHERE SeasonRankingID = :season_ranking_id
            """,
            replay_binds,
        )

    final_rows = fetch_existing_rankings(connection)
    tier_updates = []
    for row in valid_rows:
        key = (row.canonical["PlayerID"], row.canonical["SeasonID"])
        actual = final_rows[key]
        if actual["SeasonCurrentRating"] != row.canonical["CurrentRating"]:
            raise RuntimeError(
                f"SeasonRanking {key} replay ended at {actual['SeasonCurrentRating']}, expected {row.canonical['CurrentRating']}"
            )
        if actual["SeasonRankTier"] != row.canonical["RankTier"]:
            tier_updates.append(
                {
                    "rank_tier": row.canonical["RankTier"],
                    "season_ranking_id": actual["SeasonRankingID"],
                }
            )
    if tier_updates:
        cursor.executemany(
            "UPDATE SeasonRanking SET SeasonRankTier = :rank_tier "
            "WHERE SeasonRankingID = :season_ranking_id",
            tier_updates,
        )
    target_map = {
        row.staging_id: final_rows[(row.canonical["PlayerID"], row.canonical["SeasonID"])]["SeasonRankingID"]
        for row in valid_rows
    }
    return target_map, {
        "rating_events_replayed": len(replay_binds),
        "nonzero_rating_history_events_expected": nonzero_events,
    }


def complete_matches(connection: Any, rows: list[StageRow]) -> None:
    completed = [
        {"match_id": row.canonical["MatchID"]}
        for row in rows
        if row.valid and row.canonical["MatchStatus"] == "COMPLETED"
    ]
    cursor = connection.cursor()
    if completed:
        cursor.executemany(
            """
            UPDATE Match
               SET MatchStatus = 'COMPLETED'
             WHERE MatchID = :match_id
               AND MatchStatus = 'IN_PROGRESS'
            """,
            completed,
        )
    final = fetch_existing_matches(connection)
    for row in rows:
        if row.valid and final[row.canonical["MatchID"]]["MatchStatus"] != row.canonical["MatchStatus"]:
            raise RuntimeError(
                f"MatchID {row.canonical['MatchID']} did not reach source status {row.canonical['MatchStatus']}"
            )


def apply_player_updates(
    connection: Any, rows: list[StageRow]
) -> dict[int, int]:
    binds = [
        {
            "player_id": row.canonical["PlayerID"],
            "username": row.canonical["Username"],
            "email": row.canonical["Email"],
            "region": row.canonical["Region"],
            "account_level": row.canonical["AccountLevel"],
            "account_status": row.canonical["AccountStatus"],
        }
        for row in rows
        if row.valid
    ]
    cursor = connection.cursor()
    if binds:
        cursor.executemany(
            """
            UPDATE Player
               SET Username = :username,
                   Email = :email,
                   Region = :region,
                   AccountLevel = :account_level,
                   AccountStatus = :account_status
             WHERE PlayerID = :player_id
            """,
            binds,
        )
    current = fetch_existing_players(connection)
    for row in rows:
        if row.valid:
            expected = PlayerState(
                player_id=row.canonical["PlayerID"],
                username=row.canonical["Username"],
                email=row.canonical["Email"],
                region=row.canonical["Region"],
                account_level=row.canonical["AccountLevel"],
                account_status=row.canonical["AccountStatus"],
            )
            if current.get(expected.player_id) != expected:
                raise RuntimeError(f"Player update failed for PlayerID {expected.player_id}")
    return {row.staging_id: row.canonical["PlayerID"] for row in rows if row.valid}


def mark_loaded(
    connection: Any,
    rows: list[StageRow],
    target_map: dict[int, int],
) -> None:
    if not rows:
        return
    spec = ENTITY_STAGE_SPEC[rows[0].entity]
    binds = [
        {"target_record_id": target_map[row.staging_id], "staging_id": row.staging_id}
        for row in rows
        if row.valid
    ]
    if binds:
        cursor = connection.cursor()
        cursor.executemany(
            f"""
            UPDATE {spec['stage_table']}
               SET ProcessingStatus = 'LOADED',
                   TargetRecordID = :target_record_id
             WHERE {spec['id_column']} = :staging_id
               AND ProcessingStatus = 'VALID'
            """,
            binds,
        )


def finalize_batches(
    connection: Any, batch_ids: list[int]
) -> dict[int, dict[str, int]]:
    cursor = connection.cursor()
    stage_tables = (
        "StgPlayer",
        "StgMatch",
        "StgParticipation",
        "StgSeasonRanking",
    )
    results = {}
    for batch_id in batch_ids:
        status_counts: Counter[str] = Counter()
        for table in stage_tables:
            cursor.execute(
                f"SELECT ProcessingStatus, COUNT(*) FROM {table} "
                "WHERE LoadBatchID = :batch_id GROUP BY ProcessingStatus",
                batch_id=batch_id,
            )
            for status, count in cursor:
                status_counts[str(status)] += int(count)
        rows_read = sum(status_counts.values())
        rows_accepted = status_counts["LOADED"]
        rows_rejected = status_counts["REJECTED"]
        if status_counts["NEW"] or status_counts["VALID"]:
            raise RuntimeError(
                f"LoadBatchID {batch_id} still has NEW/VALID rows and cannot be completed"
            )
        cursor.execute(
            """
            UPDATE LoadBatch
               SET RowsRead = :rows_read,
                   RowsAccepted = :rows_accepted,
                   RowsRejected = :rows_rejected,
                   LoadStatus = 'COMPLETED',
                   LoadEndTime = SYSTIMESTAMP
             WHERE LoadBatchID = :batch_id
            """,
            rows_read=rows_read,
            rows_accepted=rows_accepted,
            rows_rejected=rows_rejected,
            batch_id=batch_id,
        )
        results[batch_id] = {
            "rows_read": rows_read,
            "rows_acce  ted": rows_accepted,
            "rows_rejected": rows_rejected,
        }
    return results


def load_oltp(
    connection: Any,
    bundle: ValidationBundle,
    existing_players: dict[int, PlayerState],
    batch_ids: list[int],
) -> dict[str, Any]:
    validation_write = apply_validation_results(connection, bundle)
    player_targets = load_initial_players(
        connection, bundle.initial_players, existing_players
    )
    match_targets = load_matches(connection, bundle.matches)
    participation_targets = load_participation(connection, bundle.participation)
    load_match_results(connection, bundle.participation, participation_targets)
    ranking_targets, rating_metrics = load_rankings_and_history(connection, bundle)
    complete_matches(connection, bundle.matches)
    update_targets = apply_player_updates(connection, bundle.update_players)

    mark_loaded(connection, bundle.initial_players, player_targets)
    mark_loaded(connection, bundle.update_players, update_targets)
    mark_loaded(connection, bundle.matches, match_targets)
    mark_loaded(connection, bundle.participation, participation_targets)
    mark_loaded(connection, bundle.rankings, ranking_targets)
    batch_results = finalize_batches(connection, batch_ids)
    return {
        "validation_write": validation_write,
        "target_mappings": {
            "initial_players": len(player_targets),
            "player_updates": len(update_targets),
            "matches": len(match_targets),
            "participation_and_results": len(participation_targets),
            "season_rankings": len(ranking_targets),
        },
        "rating_history": rating_metrics,
        "batches": batch_results,
    }
