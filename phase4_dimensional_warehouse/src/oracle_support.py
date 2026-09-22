"""Oracle preflight, dimensional loading, and source-resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from phase4_common import scalar, table_counts


SOURCE_TABLES = {
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

WAREHOUSE_TABLES = {
    "DIMDATE",
    "DIMTIME",
    "DIMPLAYER",
    "DIMROLE",
    "DIMCHARACTER",
    "DIMSEASON",
    "DIMRANKTIER",
    "FACTMATCHPARTICIPATION",
    "FACTPLAYERSEASONSNAPSHOT",
}

WAREHOUSE_SEQUENCES = {
    "DIMDATE_SEQ",
    "DIMTIME_SEQ",
    "DIMPLAYER_SEQ",
    "DIMROLE_SEQ",
    "DIMCHARACTER_SEQ",
    "DIMSEASON_SEQ",
    "DIMRANKTIER_SEQ",
    "FACTMATCHPARTICIPATION_SEQ",
    "FACTPLAYERSEASONSNAPSHOT_SEQ",
}

CURRENT_END = datetime(9999, 12, 31, 23, 59, 59)


@dataclass(frozen=True)
class PlayerVersion:
    player_id: int
    username: str
    region: str
    account_level: int
    account_status: str
    effective_start: datetime
    effective_end: datetime
    is_current: str


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    raise TypeError(f"Expected Oracle DATE/TIMESTAMP, received {type(value).__name__}")


def database_preflight(connection: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Require the successful Phase 3 state and the create-only DW objects."""
    cursor = connection.cursor()
    cursor.execute(
        "SELECT Object_Name, Object_Type, Status FROM User_Objects "
        "WHERE Object_Type IN ('TABLE', 'SEQUENCE')"
    )
    objects = {(str(name), str(kind)): str(status) for name, kind, status in cursor}
    failures: list[str] = []

    for table in sorted(SOURCE_TABLES | WAREHOUSE_TABLES):
        if (table, "TABLE") not in objects:
            failures.append(f"missing required table {table}")
    for sequence in sorted(WAREHOUSE_SEQUENCES):
        if (sequence, "SEQUENCE") not in objects:
            failures.append(f"missing required sequence {sequence}")

    if failures:
        return {"status": "FAIL", "failures": failures}

    initial_batch = int(config["initial_load_batch_id"])
    update_batch = int(config["scd2_load_batch_id"])
    expected = config["expected_phase3"]
    cursor.execute(
        """
        SELECT LoadBatchID, SourceSystem, LoadStartTime, LoadEndTime,
               RowsRead, RowsAccepted, RowsRejected, LoadStatus
          FROM LoadBatch
         WHERE LoadBatchID IN (:initial_batch, :update_batch)
         ORDER BY LoadBatchID
        """,
        initial_batch=initial_batch,
        update_batch=update_batch,
    )
    batches = {
        int(row[0]): {
            "load_batch_id": int(row[0]),
            "source_system": str(row[1]),
            "load_start_time": row[2],
            "load_end_time": row[3],
            "rows_read": int(row[4]),
            "rows_accepted": int(row[5]),
            "rows_rejected": int(row[6]),
            "load_status": str(row[7]),
        }
        for row in cursor
    }
    expected_batches = {
        initial_batch: (
            int(expected["initial_staged_rows"]),
            int(expected["initial_valid_rows"]),
            int(expected["rejected_staging_rows"]),
        ),
        update_batch: (
            int(expected["valid_player_updates"]),
            int(expected["valid_player_updates"]),
            0,
        ),
    }
    for batch_id, expected_counts in expected_batches.items():
        actual = batches.get(batch_id)
        if actual is None:
            failures.append(f"LoadBatchID {batch_id} is missing")
            continue
        if actual["load_status"] != "COMPLETED":
            failures.append(f"LoadBatchID {batch_id} is not COMPLETED")
        actual_counts = (
            actual["rows_read"],
            actual["rows_accepted"],
            actual["rows_rejected"],
        )
        if actual_counts != expected_counts:
            failures.append(
                f"LoadBatchID {batch_id} counts {actual_counts} do not match {expected_counts}"
            )

    stage_total = int(
        scalar(
            connection,
            """
            SELECT (SELECT COUNT(*) FROM StgPlayer)
                 + (SELECT COUNT(*) FROM StgMatch)
                 + (SELECT COUNT(*) FROM StgParticipation)
                 + (SELECT COUNT(*) FROM StgSeasonRanking)
              FROM dual
            """,
        )
    )
    if stage_total != int(expected["total_staging_rows"]):
        failures.append(
            f"total staging rows {stage_total} do not match {expected['total_staging_rows']}"
        )

    pending_rows = int(
        scalar(
            connection,
            """
            SELECT (SELECT COUNT(*) FROM StgPlayer WHERE ProcessingStatus IN ('NEW','VALID'))
                 + (SELECT COUNT(*) FROM StgMatch WHERE ProcessingStatus IN ('NEW','VALID'))
                 + (SELECT COUNT(*) FROM StgParticipation WHERE ProcessingStatus IN ('NEW','VALID'))
                 + (SELECT COUNT(*) FROM StgSeasonRanking WHERE ProcessingStatus IN ('NEW','VALID'))
              FROM dual
            """,
        )
    )
    if pending_rows:
        failures.append(f"{pending_rows} staging rows are still NEW or VALID")

    rejected_rows = int(
        scalar(
            connection,
            """
            SELECT (SELECT COUNT(*) FROM StgPlayer WHERE ProcessingStatus='REJECTED')
                 + (SELECT COUNT(*) FROM StgMatch WHERE ProcessingStatus='REJECTED')
                 + (SELECT COUNT(*) FROM StgParticipation WHERE ProcessingStatus='REJECTED')
                 + (SELECT COUNT(*) FROM StgSeasonRanking WHERE ProcessingStatus='REJECTED')
              FROM dual
            """,
        )
    )
    rejection_reasons = int(scalar(connection, "SELECT COUNT(*) FROM RejectedRecord"))
    if rejected_rows != int(expected["rejected_staging_rows"]):
        failures.append(
            f"rejected staging rows {rejected_rows} do not match {expected['rejected_staging_rows']}"
        )
    if rejection_reasons != int(expected["rejection_reason_rows"]):
        failures.append(
            f"rejection reason rows {rejection_reasons} do not match {expected['rejection_reason_rows']}"
        )

    oltp_queries = {
        "players": "SELECT COUNT(*) FROM Player",
        "matches": "SELECT COUNT(*) FROM Match",
        "participation": "SELECT COUNT(*) FROM MatchParticipation",
        "results": "SELECT COUNT(*) FROM MatchResult",
        "rankings": "SELECT COUNT(*) FROM SeasonRanking",
        "rating_history": "SELECT COUNT(*) FROM SeasonRatingChange",
    }
    oltp_counts = {
        name: int(scalar(connection, sql)) for name, sql in oltp_queries.items()
    }
    for name, actual in oltp_counts.items():
        expected_value = int(expected[name])
        if actual != expected_value:
            failures.append(
                f"OLTP {name} count {actual} does not match {expected_value}"
            )

    reference_counts = {
        "seasons": int(scalar(connection, "SELECT COUNT(*) FROM Season")),
        "roles": int(scalar(connection, "SELECT COUNT(*) FROM Role")),
        "characters": int(scalar(connection, "SELECT COUNT(*) FROM Character")),
    }
    expected_reference_counts = {
        "seasons": int(config["expected_phase4"]["dim_season_rows"]),
        "roles": int(config["expected_phase4"]["dim_role_rows"]),
        "characters": int(config["expected_phase4"]["dim_character_rows"]),
    }
    for name, actual in reference_counts.items():
        expected_value = expected_reference_counts[name]
        if actual != expected_value:
            failures.append(
                f"controlled {name} count {actual} does not match {expected_value}"
            )

    tier_rows = []
    cursor.execute(
        "SELECT RankTierKey, RankTierName, RankTierOrder "
        "FROM DimRankTier ORDER BY RankTierOrder"
    )
    tier_rows = [(int(row[0]), str(row[1]), int(row[2])) for row in cursor]
    required_tiers = [
        (1, "BRONZE", 1),
        (2, "SILVER", 2),
        (3, "GOLD", 3),
        (4, "PLATINUM", 4),
        (5, "DIAMOND", 5),
    ]
    if tier_rows != required_tiers:
        failures.append(
            "DimRankTier does not contain the five authoritative seeded rows; rerun the DDL as a script and COMMIT"
        )

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "batches": batches,
        "stage_total": stage_total,
        "rejected_staging_rows": rejected_rows,
        "rejection_reason_rows": rejection_reasons,
        "oltp_counts": oltp_counts,
        "reference_counts": reference_counts,
    }


def calendar_bounds(connection: Any) -> tuple[date, date]:
    cursor = connection.cursor()
    cursor.execute("SELECT MIN(StartDate), MAX(EndDate) FROM Season")
    start_value, end_value = cursor.fetchone()
    if start_value is None or end_value is None:
        raise RuntimeError("Season has no rows; DimDate cannot be populated")
    start_date = start_value.date() if isinstance(start_value, datetime) else start_value
    end_date = end_value.date() if isinstance(end_value, datetime) else end_value
    return start_date, end_date


def load_type1_dimensions(connection: Any) -> None:
    cursor = connection.cursor()
    cursor.execute(
        """
        MERGE INTO DimRole d
        USING (SELECT RoleID, RoleName, RoleDescription FROM Role) s
           ON (d.RoleID = s.RoleID)
        WHEN MATCHED THEN UPDATE SET
             d.RoleName = s.RoleName,
             d.RoleDescription = s.RoleDescription
        WHEN NOT MATCHED THEN INSERT (
             RoleKey, RoleID, RoleName, RoleDescription
        ) VALUES (
             DimRole_Seq.NEXTVAL, s.RoleID, s.RoleName, s.RoleDescription
        )
        """
    )
    cursor.execute(
        """
        MERGE INTO DimCharacter d
        USING (SELECT CharacterID, CharacterName FROM Character) s
           ON (d.CharacterID = s.CharacterID)
        WHEN MATCHED THEN UPDATE SET
             d.CharacterName = s.CharacterName
        WHEN NOT MATCHED THEN INSERT (
             CharacterKey, CharacterID, CharacterName
        ) VALUES (
             DimCharacter_Seq.NEXTVAL, s.CharacterID, s.CharacterName
        )
        """
    )
    cursor.execute(
        """
        MERGE INTO DimSeason d
        USING (
            SELECT SeasonID, SeasonName, StartDate, EndDate, SeasonStatus
              FROM Season
        ) s
           ON (d.SeasonID = s.SeasonID)
        WHEN MATCHED THEN UPDATE SET
             d.SeasonName = s.SeasonName,
             d.StartDate = s.StartDate,
             d.EndDate = s.EndDate,
             d.SeasonStatus = s.SeasonStatus
        WHEN NOT MATCHED THEN INSERT (
             SeasonKey, SeasonID, SeasonName, StartDate, EndDate, SeasonStatus
        ) VALUES (
             DimSeason_Seq.NEXTVAL, s.SeasonID, s.SeasonName,
             s.StartDate, s.EndDate, s.SeasonStatus
        )
        """
    )


def _date_rows(start_date: date, end_date: date) -> list[dict[str, Any]]:
    day_names = (
        "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
        "FRIDAY", "SATURDAY", "SUNDAY",
    )
    month_names = (
        "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
        "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
    )
    rows: list[dict[str, Any]] = []
    current = start_date
    while current <= end_date:
        iso = current.isocalendar()
        rows.append(
            {
                "full_date": current,
                "day_of_month": current.day,
                "day_of_week": current.isoweekday(),
                "day_name": day_names[current.weekday()],
                "week_of_year": iso.week,
                "month_number": current.month,
                "month_name": month_names[current.month - 1],
                "quarter_number": ((current.month - 1) // 3) + 1,
                "year_number": current.year,
                "is_weekend": "Y" if current.isoweekday() >= 6 else "N",
            }
        )
        current += timedelta(days=1)
    return rows


def load_date_dimension(connection: Any, start_date: date, end_date: date) -> None:
    rows = _date_rows(start_date, end_date)
    cursor = connection.cursor()
    cursor.executemany(
        """
        MERGE INTO DimDate d
        USING (SELECT :full_date AS FullDate FROM dual) s
           ON (d.FullDate = s.FullDate)
        WHEN MATCHED THEN UPDATE SET
             d.DayOfMonth = :day_of_month,
             d.DayOfWeekNumber = :day_of_week,
             d.DayName = :day_name,
             d.WeekOfYear = :week_of_year,
             d.MonthNumber = :month_number,
             d.MonthName = :month_name,
             d.QuarterNumber = :quarter_number,
             d.YearNumber = :year_number,
             d.IsWeekend = :is_weekend
        WHEN NOT MATCHED THEN INSERT (
             DateKey, FullDate, DayOfMonth, DayOfWeekNumber, DayName,
             WeekOfYear, MonthNumber, MonthName, QuarterNumber,
             YearNumber, IsWeekend
        ) VALUES (
             DimDate_Seq.NEXTVAL, :full_date, :day_of_month, :day_of_week,
             :day_name, :week_of_year, :month_number, :month_name,
             :quarter_number, :year_number, :is_weekend
        )
        """,
        rows,
    )


def load_time_dimension(connection: Any) -> None:
    """Populate all 86,400 second-of-day members in one set operation."""
    cursor = connection.cursor()
    cursor.execute(
        """
        MERGE INTO DimTime d
        USING (
            SELECT SecondsSinceMidnight,
                   TO_CHAR(HourNumber, 'FM00') || ':' ||
                   TO_CHAR(MinuteNumber, 'FM00') || ':' ||
                   TO_CHAR(SecondNumber, 'FM00') AS TimeValue,
                   HourNumber,
                   MinuteNumber,
                   SecondNumber,
                   CASE
                       WHEN HourNumber BETWEEN 0 AND 5 THEN 'NIGHT'
                       WHEN HourNumber BETWEEN 6 AND 11 THEN 'MORNING'
                       WHEN HourNumber BETWEEN 12 AND 17 THEN 'AFTERNOON'
                       ELSE 'EVENING'
                   END AS DayPart
              FROM (
                    SELECT second_value AS SecondsSinceMidnight,
                           TRUNC(second_value / 3600) AS HourNumber,
                           TRUNC(MOD(second_value, 3600) / 60) AS MinuteNumber,
                           MOD(second_value, 60) AS SecondNumber
                      FROM (
                            SELECT LEVEL - 1 AS second_value
                              FROM dual
                            CONNECT BY LEVEL <= 86400
                           )
                   )
        ) s
           ON (d.SecondsSinceMidnight = s.SecondsSinceMidnight)
        WHEN MATCHED THEN UPDATE SET
             d.TimeValue = s.TimeValue,
             d.HourNumber = s.HourNumber,
             d.MinuteNumber = s.MinuteNumber,
             d.SecondNumber = s.SecondNumber,
             d.DayPart = s.DayPart
        WHEN NOT MATCHED THEN INSERT (
             TimeKey, TimeValue, SecondsSinceMidnight, HourNumber,
             MinuteNumber, SecondNumber, DayPart
        ) VALUES (
             DimTime_Seq.NEXTVAL, s.TimeValue, s.SecondsSinceMidnight,
             s.HourNumber, s.MinuteNumber, s.SecondNumber, s.DayPart
        )
        """
    )


def _fetch_player_stage(
    connection: Any, batch_id: int, file_name: str
) -> dict[int, dict[str, Any]]:
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT TO_NUMBER(TRIM(RawPlayerID)), TRIM(RawUsername), TRIM(RawRegion),
               TO_NUMBER(TRIM(RawAccountLevel)), UPPER(TRIM(RawAccountStatus)),
               StgPlayerID, TargetRecordID
          FROM StgPlayer
         WHERE LoadBatchID = :batch_id
           AND SourceFileName = :file_name
           AND ProcessingStatus = 'LOADED'
         ORDER BY SourceRowNumber
        """,
        batch_id=batch_id,
        file_name=file_name,
    )
    result: dict[int, dict[str, Any]] = {}
    for row in cursor:
        player_id = int(row[0])
        if player_id in result:
            raise RuntimeError(
                f"Loaded staging contains duplicate PlayerID {player_id} in {file_name}"
            )
        if int(row[6]) != player_id:
            raise RuntimeError(
                f"{file_name} PlayerID {player_id} maps to unexpected OLTP target {row[6]}"
            )
        result[player_id] = {
            "player_id": player_id,
            "username": str(row[1]),
            "region": str(row[2]),
            "account_level": int(row[3]),
            "account_status": str(row[4]),
            "staging_id": int(row[5]),
            "target_record_id": int(row[6]),
        }
    return result


def desired_player_versions(
    connection: Any, config: dict[str, Any]
) -> tuple[list[PlayerVersion], dict[str, Any]]:
    initial_batch = int(config["initial_load_batch_id"])
    update_batch = int(config["scd2_load_batch_id"])
    initial = _fetch_player_stage(connection, initial_batch, "player.csv")
    updates = _fetch_player_stage(
        connection, update_batch, "player_scd2_update.csv"
    )
    if len(initial) != int(config["expected_phase3"]["players"]):
        raise RuntimeError("Initial loaded Player staging count is not authoritative")
    if len(updates) != int(config["expected_phase3"]["valid_player_updates"]):
        raise RuntimeError("Later loaded Player staging count is not authoritative")
    if not set(updates).issubset(initial):
        raise RuntimeError("A later Player row has no initial loaded version")

    start_date, _ = calendar_bounds(connection)
    initial_start = datetime.combine(start_date, time.min)
    update_start_value = scalar(
        connection,
        "SELECT LoadStartTime FROM LoadBatch WHERE LoadBatchID=:batch_id",
        batch_id=update_batch,
    )
    update_start = _as_datetime(update_start_value)
    if update_start <= initial_start:
        raise RuntimeError("Batch 2 effective time does not follow analytical coverage start")

    tracked = ("username", "region", "account_level", "account_status")
    unchanged_update_ids = [
        player_id
        for player_id, later in updates.items()
        if all(later[name] == initial[player_id][name] for name in tracked)
    ]
    if unchanged_update_ids:
        raise RuntimeError(
            "Later Player batch contains unchanged Type 2 candidates: "
            + ", ".join(map(str, unchanged_update_ids[:10]))
        )

    versions: list[PlayerVersion] = []
    for player_id, original in sorted(initial.items()):
        later = updates.get(player_id)
        versions.append(
            PlayerVersion(
                player_id=player_id,
                username=original["username"],
                region=original["region"],
                account_level=original["account_level"],
                account_status=original["account_status"],
                effective_start=initial_start,
                effective_end=update_start if later else CURRENT_END,
                is_current="N" if later else "Y",
            )
        )
        if later:
            versions.append(
                PlayerVersion(
                    player_id=player_id,
                    username=later["username"],
                    region=later["region"],
                    account_level=later["account_level"],
                    account_status=later["account_status"],
                    effective_start=update_start,
                    effective_end=CURRENT_END,
                    is_current="Y",
                )
            )
    return versions, {
        "initial_effective_start": initial_start,
        "update_effective_start": update_start,
        "current_effective_end": CURRENT_END,
        "updated_player_ids": sorted(updates),
    }


def sync_dim_player(
    connection: Any, desired: list[PlayerVersion]
) -> dict[str, int]:
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT PlayerKey, PlayerID, Username, Region, AccountLevel,
               AccountStatus, EffectiveStartDateTime, EffectiveEndDateTime,
               IsCurrent
          FROM DimPlayer
         ORDER BY PlayerID, EffectiveStartDateTime
        """
    )
    existing: dict[tuple[int, datetime], dict[str, Any]] = {}
    for row in cursor:
        key = (int(row[1]), _as_datetime(row[6]))
        existing[key] = {
            "player_key": int(row[0]),
            "player_id": int(row[1]),
            "username": str(row[2]),
            "region": str(row[3]),
            "account_level": int(row[4]),
            "account_status": str(row[5]),
            "effective_start": _as_datetime(row[6]),
            "effective_end": _as_datetime(row[7]),
            "is_current": str(row[8]),
        }

    desired_map = {(row.player_id, row.effective_start): row for row in desired}
    extras = sorted(set(existing) - set(desired_map))
    if extras:
        preview = ", ".join(f"{pid}@{start.isoformat()}" for pid, start in extras[:10])
        raise RuntimeError(
            "DimPlayer contains versions outside the authoritative two-batch history: "
            + preview
        )

    updated = 0
    inserted = 0

    def bind(row: PlayerVersion) -> dict[str, Any]:
        return {
            "player_id": row.player_id,
            "username": row.username,
            "region": row.region,
            "account_level": row.account_level,
            "account_status": row.account_status,
            "effective_start": row.effective_start,
            "effective_end": row.effective_end,
            "is_current": row.is_current,
        }

    # Close old current versions first so the conditional unique index never
    # sees two current rows for one PlayerID.
    for row in [item for item in desired if item.is_current == "N"]:
        current = existing.get((row.player_id, row.effective_start))
        if current:
            cursor.execute(
                """
                UPDATE DimPlayer
                   SET PlayerID=:player_id,
                       Username=:username, Region=:region,
                       AccountLevel=:account_level, AccountStatus=:account_status,
                       EffectiveStartDateTime=:effective_start,
                       EffectiveEndDateTime=:effective_end, IsCurrent=:is_current
                 WHERE PlayerKey=:player_key
                """,
                {**bind(row), "player_key": current["player_key"]},
            )
            updated += int(cursor.rowcount)
        else:
            cursor.execute(
                """
                INSERT INTO DimPlayer (
                    PlayerKey, PlayerID, Username, Region, AccountLevel,
                    AccountStatus, EffectiveStartDateTime,
                    EffectiveEndDateTime, IsCurrent
                ) VALUES (
                    DimPlayer_Seq.NEXTVAL, :player_id, :username, :region,
                    :account_level, :account_status, :effective_start,
                    :effective_end, :is_current
                )
                """,
                bind(row),
            )
            inserted += int(cursor.rowcount)

    for row in [item for item in desired if item.is_current == "Y"]:
        current = existing.get((row.player_id, row.effective_start))
        if current:
            cursor.execute(
                """
                UPDATE DimPlayer
                   SET PlayerID=:player_id,
                       Username=:username, Region=:region,
                       AccountLevel=:account_level, AccountStatus=:account_status,
                       EffectiveStartDateTime=:effective_start,
                       EffectiveEndDateTime=:effective_end, IsCurrent=:is_current
                 WHERE PlayerKey=:player_key
                """,
                {**bind(row), "player_key": current["player_key"]},
            )
            updated += int(cursor.rowcount)
        else:
            cursor.execute(
                """
                INSERT INTO DimPlayer (
                    PlayerKey, PlayerID, Username, Region, AccountLevel,
                    AccountStatus, EffectiveStartDateTime,
                    EffectiveEndDateTime, IsCurrent
                ) VALUES (
                    DimPlayer_Seq.NEXTVAL, :player_id, :username, :region,
                    :account_level, :account_status, :effective_start,
                    :effective_end, :is_current
                )
                """,
                bind(row),
            )
            inserted += int(cursor.rowcount)

    return {"inserted": inserted, "refreshed": updated}


def fact_source_mapping_counts(connection: Any, config: dict[str, Any]) -> dict[str, int]:
    initial_batch = int(config["initial_load_batch_id"])
    participation_source = int(
        scalar(
            connection,
            """
            SELECT COUNT(*)
              FROM StgParticipation
             WHERE LoadBatchID=:batch_id
               AND SourceFileName='participation.csv'
               AND ProcessingStatus='LOADED'
            """,
            batch_id=initial_batch,
        )
    )
    participation_mapped = int(
        scalar(
            connection,
            """
            SELECT COUNT(*)
              FROM StgParticipation sp
              JOIN MatchParticipation mp
                ON mp.MatchParticipationID=sp.TargetRecordID
              JOIN Match m ON m.MatchID=mp.MatchID
              JOIN DimPlayer dp
                ON dp.PlayerID=mp.PlayerID
               AND m.MatchDateTime>=dp.EffectiveStartDateTime
               AND m.MatchDateTime<dp.EffectiveEndDateTime
              JOIN DimRole dr ON dr.RoleID=mp.RoleID
              JOIN DimCharacter dc ON dc.CharacterID=mp.CharacterID
              JOIN DimSeason ds ON ds.SeasonID=m.SeasonID
              JOIN DimDate dd
                ON dd.FullDate=TRUNC(CAST(m.MatchDateTime AS DATE))
              JOIN DimTime dt
                ON dt.SecondsSinceMidnight=
                   TO_NUMBER(TO_CHAR(m.MatchDateTime,'HH24'))*3600
                 + TO_NUMBER(TO_CHAR(m.MatchDateTime,'MI'))*60
                 + TO_NUMBER(TO_CHAR(m.MatchDateTime,'SS'))
             WHERE sp.LoadBatchID=:batch_id
               AND sp.SourceFileName='participation.csv'
               AND sp.ProcessingStatus='LOADED'
            """,
            batch_id=initial_batch,
        )
    )
    ranking_source = int(
        scalar(
            connection,
            """
            SELECT COUNT(*) FROM StgSeasonRanking
             WHERE LoadBatchID=:batch_id
               AND SourceFileName='season_ranking.csv'
               AND ProcessingStatus='LOADED'
            """,
            batch_id=initial_batch,
        )
    )
    ranking_mapped = int(
        scalar(
            connection,
            """
            SELECT COUNT(*)
              FROM StgSeasonRanking ssr
              JOIN SeasonRanking sr ON sr.SeasonRankingID=ssr.TargetRecordID
              JOIN Season s ON s.SeasonID=sr.SeasonID
              JOIN DimSeason ds ON ds.SeasonID=sr.SeasonID
              JOIN DimRankTier rt ON rt.RankTierName=sr.SeasonRankTier
              JOIN DimDate dd ON dd.FullDate=
                   CASE WHEN s.SeasonStatus='COMPLETED' THEN s.EndDate
                        ELSE LEAST(GREATEST(TRUNC(SYSDATE),s.StartDate),s.EndDate)
                   END
              JOIN DimPlayer dp
                ON dp.PlayerID=sr.PlayerID
               AND CAST(CASE WHEN s.SeasonStatus='COMPLETED' THEN s.EndDate
                             ELSE LEAST(GREATEST(TRUNC(SYSDATE),s.StartDate),s.EndDate)
                        END AS TIMESTAMP)>=dp.EffectiveStartDateTime
               AND CAST(CASE WHEN s.SeasonStatus='COMPLETED' THEN s.EndDate
                             ELSE LEAST(GREATEST(TRUNC(SYSDATE),s.StartDate),s.EndDate)
                        END AS TIMESTAMP)<dp.EffectiveEndDateTime
             WHERE ssr.LoadBatchID=:batch_id
               AND ssr.SourceFileName='season_ranking.csv'
               AND ssr.ProcessingStatus='LOADED'
            """,
            batch_id=initial_batch,
        )
    )
    return {
        "participation_source_rows": participation_source,
        "participation_rows_with_one_dimension_path": participation_mapped,
        "ranking_source_rows": ranking_source,
        "ranking_rows_with_one_dimension_path": ranking_mapped,
    }


def load_match_participation_fact(connection: Any, config: dict[str, Any]) -> None:
    initial_batch = int(config["initial_load_batch_id"])
    cursor = connection.cursor()
    cursor.execute(
        """
        MERGE INTO FactMatchParticipation f
        USING (
            SELECT mp.MatchParticipationID AS SourceMatchParticipationID,
                   m.MatchID,
                   dp.PlayerKey,
                   dr.RoleKey,
                   dc.CharacterKey,
                   ds.SeasonKey,
                   dd.DateKey,
                   dt.TimeKey,
                   mp.TeamAssignment,
                   CASE WHEN mr.PlayerResult='WIN' THEN 1 ELSE 0 END AS WinFlag,
                   mr.RatingChange,
                   sp.LoadBatchID AS SourceLoadBatchID
              FROM StgParticipation sp
              JOIN MatchParticipation mp
                ON mp.MatchParticipationID=sp.TargetRecordID
              JOIN MatchResult mr
                ON mr.MatchParticipationID=mp.MatchParticipationID
              JOIN Match m ON m.MatchID=mp.MatchID
              JOIN DimPlayer dp
                ON dp.PlayerID=mp.PlayerID
               AND m.MatchDateTime>=dp.EffectiveStartDateTime
               AND m.MatchDateTime<dp.EffectiveEndDateTime
              JOIN DimRole dr ON dr.RoleID=mp.RoleID
              JOIN DimCharacter dc ON dc.CharacterID=mp.CharacterID
              JOIN DimSeason ds ON ds.SeasonID=m.SeasonID
              JOIN DimDate dd
                ON dd.FullDate=TRUNC(CAST(m.MatchDateTime AS DATE))
              JOIN DimTime dt
                ON dt.SecondsSinceMidnight=
                   TO_NUMBER(TO_CHAR(m.MatchDateTime,'HH24'))*3600
                 + TO_NUMBER(TO_CHAR(m.MatchDateTime,'MI'))*60
                 + TO_NUMBER(TO_CHAR(m.MatchDateTime,'SS'))
             WHERE sp.LoadBatchID=:initial_batch
               AND sp.SourceFileName='participation.csv'
               AND sp.ProcessingStatus='LOADED'
        ) s
           ON (f.SourceMatchParticipationID=s.SourceMatchParticipationID)
        WHEN MATCHED THEN UPDATE SET
             f.MatchID=s.MatchID,
             f.PlayerKey=s.PlayerKey,
             f.RoleKey=s.RoleKey,
             f.CharacterKey=s.CharacterKey,
             f.SeasonKey=s.SeasonKey,
             f.DateKey=s.DateKey,
             f.TimeKey=s.TimeKey,
             f.TeamAssignment=s.TeamAssignment,
             f.ParticipationCount=1,
             f.WinFlag=s.WinFlag,
             f.RatingChange=s.RatingChange,
             f.SourceLoadBatchID=s.SourceLoadBatchID
        WHEN NOT MATCHED THEN INSERT (
             MatchParticipationFactKey, SourceMatchParticipationID, MatchID,
             PlayerKey, RoleKey, CharacterKey, SeasonKey, DateKey, TimeKey,
             TeamAssignment, ParticipationCount, WinFlag, RatingChange,
             SourceLoadBatchID, LoadedAt
        ) VALUES (
             FactMatchParticipation_Seq.NEXTVAL,
             s.SourceMatchParticipationID, s.MatchID, s.PlayerKey, s.RoleKey,
             s.CharacterKey, s.SeasonKey, s.DateKey, s.TimeKey,
             s.TeamAssignment, 1, s.WinFlag, s.RatingChange,
             s.SourceLoadBatchID, SYSTIMESTAMP
        )
        """,
        initial_batch=initial_batch,
    )


def load_player_season_snapshot_fact(connection: Any, config: dict[str, Any]) -> None:
    initial_batch = int(config["initial_load_batch_id"])
    cursor = connection.cursor()
    cursor.execute(
        """
        MERGE INTO FactPlayerSeasonSnapshot f
        USING (
            WITH measures AS (
                SELECT mp.PlayerID,
                       m.SeasonID,
                       COUNT(*) AS ParticipationCount,
                       SUM(CASE WHEN mr.PlayerResult='WIN' THEN 1 ELSE 0 END)
                           AS WinCount,
                       SUM(mr.RatingChange) AS RatingChangeTotal
                  FROM MatchParticipation mp
                  JOIN Match m ON m.MatchID=mp.MatchID
                  JOIN MatchResult mr
                    ON mr.MatchParticipationID=mp.MatchParticipationID
                 GROUP BY mp.PlayerID, m.SeasonID
            ), ranking_source AS (
                SELECT sr.SeasonRankingID,
                       sr.PlayerID,
                       sr.SeasonID,
                       sr.SeasonCurrentRating,
                       sr.SeasonRankTier,
                       CASE WHEN s.SeasonStatus='COMPLETED' THEN s.EndDate
                            ELSE LEAST(GREATEST(TRUNC(SYSDATE),s.StartDate),s.EndDate)
                       END AS SnapshotDate,
                       NVL(measures.ParticipationCount,0) AS ParticipationCount,
                       NVL(measures.WinCount,0) AS WinCount,
                       NVL(measures.RatingChangeTotal,0) AS RatingChangeTotal,
                       ssr.LoadBatchID AS SourceLoadBatchID
                  FROM StgSeasonRanking ssr
                  JOIN SeasonRanking sr
                    ON sr.SeasonRankingID=ssr.TargetRecordID
                  JOIN Season s ON s.SeasonID=sr.SeasonID
                  LEFT JOIN measures
                    ON measures.PlayerID=sr.PlayerID
                   AND measures.SeasonID=sr.SeasonID
                 WHERE ssr.LoadBatchID=:initial_batch
                   AND ssr.SourceFileName='season_ranking.csv'
                   AND ssr.ProcessingStatus='LOADED'
            )
            SELECT rs.SeasonRankingID AS SourceSeasonRankingID,
                   dp.PlayerKey,
                   ds.SeasonKey,
                   rt.RankTierKey,
                   dd.DateKey AS SnapshotDateKey,
                   rs.SeasonCurrentRating-rs.RatingChangeTotal AS StartingRating,
                   rs.SeasonCurrentRating AS EndingRating,
                   rs.ParticipationCount,
                   rs.WinCount,
                   rs.SourceLoadBatchID
              FROM ranking_source rs
              JOIN DimPlayer dp
                ON dp.PlayerID=rs.PlayerID
               AND CAST(rs.SnapshotDate AS TIMESTAMP)>=dp.EffectiveStartDateTime
               AND CAST(rs.SnapshotDate AS TIMESTAMP)<dp.EffectiveEndDateTime
              JOIN DimSeason ds ON ds.SeasonID=rs.SeasonID
              JOIN DimRankTier rt ON rt.RankTierName=rs.SeasonRankTier
              JOIN DimDate dd ON dd.FullDate=rs.SnapshotDate
        ) s
           ON (f.SourceSeasonRankingID=s.SourceSeasonRankingID)
        WHEN MATCHED THEN UPDATE SET
             f.PlayerKey=s.PlayerKey,
             f.SeasonKey=s.SeasonKey,
             f.RankTierKey=s.RankTierKey,
             f.SnapshotDateKey=s.SnapshotDateKey,
             f.StartingRating=s.StartingRating,
             f.EndingRating=s.EndingRating,
             f.ParticipationCount=s.ParticipationCount,
             f.WinCount=s.WinCount,
             f.SourceLoadBatchID=s.SourceLoadBatchID
        WHEN NOT MATCHED THEN INSERT (
             PlayerSeasonSnapshotFactKey, SourceSeasonRankingID, PlayerKey,
             SeasonKey, RankTierKey, SnapshotDateKey, StartingRating,
             EndingRating, ParticipationCount, WinCount,
             SourceLoadBatchID, LoadedAt
        ) VALUES (
             FactPlayerSeasonSnapshot_Seq.NEXTVAL,
             s.SourceSeasonRankingID, s.PlayerKey, s.SeasonKey,
             s.RankTierKey, s.SnapshotDateKey, s.StartingRating,
             s.EndingRating, s.ParticipationCount, s.WinCount,
             s.SourceLoadBatchID, SYSTIMESTAMP
        )
        """,
        initial_batch=initial_batch,
    )


def lightweight_postload_check(
    connection: Any, config: dict[str, Any]
) -> dict[str, Any]:
    counts = table_counts(connection)
    expected = config["expected_phase4"]
    start_date, end_date = calendar_bounds(connection)
    expected_date_rows = (end_date - start_date).days + 1
    expected_counts = {
        "DimDate": expected_date_rows,
        "DimTime": 86400,
        "DimPlayer": int(expected["dim_player_rows"]),
        "DimRole": int(expected["dim_role_rows"]),
        "DimCharacter": int(expected["dim_character_rows"]),
        "DimSeason": int(expected["dim_season_rows"]),
        "DimRankTier": int(expected["dim_rank_tier_rows"]),
        "FactMatchParticipation": int(expected["match_participation_fact_rows"]),
        "FactPlayerSeasonSnapshot": int(expected["player_season_snapshot_fact_rows"]),
    }
    failures = [
        f"{name} count {counts[name]} does not match {expected_value}"
        for name, expected_value in expected_counts.items()
        if counts[name] != expected_value
    ]
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual_counts": counts,
        "expected_counts": expected_counts,
        "calendar_start": start_date,
        "calendar_end": end_date,
    }
