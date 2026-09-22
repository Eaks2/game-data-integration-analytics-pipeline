#!/usr/bin/env python3
"""Read-only automated reconciliation for the CS779 Phase 4 warehouse."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from oracle_support import (
    PlayerVersion,
    _as_datetime,
    calendar_bounds,
    database_preflight,
    desired_player_versions,
)
from phase4_common import connect, read_json, scalar, table_counts, write_json


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
        default=root / "run_outputs" / "phase4_verification.json",
    )
    return parser.parse_args()


def int_scalar(connection: Any, sql: str, **binds: Any) -> int:
    return int(scalar(connection, sql, **binds))


def fetch_one(connection: Any, sql: str, **binds: Any) -> tuple[Any, ...]:
    cursor = connection.cursor()
    cursor.execute(sql, binds)
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("Verification query returned no row")
    return tuple(row)


def main() -> int:
    args = parse_args()
    config = read_json(args.phase4_config.resolve())
    oracle_config = read_json(args.oracle_config.resolve())
    output_path = args.output.resolve()
    result: dict[str, Any] = {
        "phase": 4,
        "status": "STARTED",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks": [],
        "failures": [],
    }
    connection = None

    def add_check(name: str, actual: Any, expected: Any) -> None:
        status = "PASS" if actual == expected else "FAIL"
        result["checks"].append(
            {"name": name, "actual": actual, "expected": expected, "status": status}
        )
        if status == "FAIL":
            result["failures"].append(
                f"{name}: actual={actual!r}, expected={expected!r}"
            )

    try:
        connection = connect(oracle_config)
        preflight = database_preflight(connection, config)
        result["database_preflight"] = preflight
        add_check("Phase 3 source state and dimensional objects", preflight["status"], "PASS")
        if preflight["status"] != "PASS":
            raise RuntimeError("Database preflight failed")

        initial_batch = int(config["initial_load_batch_id"])
        update_batch = int(config["scd2_load_batch_id"])
        expected4 = config["expected_phase4"]
        calendar_start, calendar_end = calendar_bounds(connection)
        expected_date_rows = (calendar_end - calendar_start).days + 1
        counts = table_counts(connection)
        result["dimension_and_fact_counts"] = counts
        expected_counts = {
            "DimDate": expected_date_rows,
            "DimTime": 86400,
            "DimPlayer": int(expected4["dim_player_rows"]),
            "DimRole": int(expected4["dim_role_rows"]),
            "DimCharacter": int(expected4["dim_character_rows"]),
            "DimSeason": int(expected4["dim_season_rows"]),
            "DimRankTier": int(expected4["dim_rank_tier_rows"]),
            "FactMatchParticipation": int(expected4["match_participation_fact_rows"]),
            "FactPlayerSeasonSnapshot": int(expected4["player_season_snapshot_fact_rows"]),
        }
        for name, expected_count in expected_counts.items():
            add_check(f"{name} row count", counts[name], expected_count)

        # Static dimension completeness and component correctness.
        raw_date_bounds = fetch_one(
            connection, "SELECT MIN(FullDate), MAX(FullDate) FROM DimDate"
        )
        actual_date_bounds = tuple(
            value.date() if isinstance(value, datetime) else value
            for value in raw_date_bounds
        )
        add_check(
            "DimDate calendar boundaries",
            actual_date_bounds,
            (calendar_start, calendar_end),
        )
        add_check(
            "DimDate attribute mismatches",
            int_scalar(
                connection,
                """
                SELECT COUNT(*) FROM DimDate
                 WHERE DayOfMonth<>TO_NUMBER(TO_CHAR(FullDate,'DD'))
                    OR DayOfWeekNumber<>(TRUNC(FullDate)-TRUNC(FullDate,'IW')+1)
                    OR WeekOfYear<>TO_NUMBER(TO_CHAR(FullDate,'IW'))
                    OR MonthNumber<>TO_NUMBER(TO_CHAR(FullDate,'MM'))
                    OR QuarterNumber<>TO_NUMBER(TO_CHAR(FullDate,'Q'))
                    OR YearNumber<>TO_NUMBER(TO_CHAR(FullDate,'YYYY'))
                    OR IsWeekend<>CASE WHEN (TRUNC(FullDate)-TRUNC(FullDate,'IW')+1)>=6
                                      THEN 'Y' ELSE 'N' END
                """,
            ),
            0,
        )
        add_check(
            "DimTime second coverage",
            fetch_one(
                connection,
                "SELECT MIN(SecondsSinceMidnight), MAX(SecondsSinceMidnight) FROM DimTime",
            ),
            (0, 86399),
        )
        add_check(
            "DimTime attribute mismatches",
            int_scalar(
                connection,
                """
                SELECT COUNT(*) FROM DimTime
                 WHERE TimeValue<>TO_CHAR(HourNumber,'FM00')||':'||
                                  TO_CHAR(MinuteNumber,'FM00')||':'||
                                  TO_CHAR(SecondNumber,'FM00')
                    OR DayPart<>CASE
                         WHEN HourNumber BETWEEN 0 AND 5 THEN 'NIGHT'
                         WHEN HourNumber BETWEEN 6 AND 11 THEN 'MORNING'
                         WHEN HourNumber BETWEEN 12 AND 17 THEN 'AFTERNOON'
                         ELSE 'EVENING' END
                """,
            ),
            0,
        )

        # Natural keys, source keys, and conditional current-row uniqueness.
        duplicate_queries = {
            "DimDate surrogate-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT DateKey FROM DimDate GROUP BY DateKey HAVING COUNT(*)>1)",
            "DimDate FullDate duplicate groups":
                "SELECT COUNT(*) FROM (SELECT FullDate FROM DimDate GROUP BY FullDate HAVING COUNT(*)>1)",
            "DimTime surrogate-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT TimeKey FROM DimTime GROUP BY TimeKey HAVING COUNT(*)>1)",
            "DimTime TimeValue duplicate groups":
                "SELECT COUNT(*) FROM (SELECT TimeValue FROM DimTime GROUP BY TimeValue HAVING COUNT(*)>1)",
            "DimTime second duplicate groups":
                "SELECT COUNT(*) FROM (SELECT SecondsSinceMidnight FROM DimTime GROUP BY SecondsSinceMidnight HAVING COUNT(*)>1)",
            "DimPlayer version duplicate groups":
                "SELECT COUNT(*) FROM (SELECT PlayerID,EffectiveStartDateTime FROM DimPlayer GROUP BY PlayerID,EffectiveStartDateTime HAVING COUNT(*)>1)",
            "DimPlayer surrogate-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT PlayerKey FROM DimPlayer GROUP BY PlayerKey HAVING COUNT(*)>1)",
            "DimPlayer multiple-current groups":
                "SELECT COUNT(*) FROM (SELECT PlayerID FROM DimPlayer WHERE IsCurrent='Y' GROUP BY PlayerID HAVING COUNT(*)>1)",
            "DimRole natural-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT RoleID FROM DimRole GROUP BY RoleID HAVING COUNT(*)>1)",
            "DimRole surrogate-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT RoleKey FROM DimRole GROUP BY RoleKey HAVING COUNT(*)>1)",
            "DimCharacter natural-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT CharacterID FROM DimCharacter GROUP BY CharacterID HAVING COUNT(*)>1)",
            "DimCharacter surrogate-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT CharacterKey FROM DimCharacter GROUP BY CharacterKey HAVING COUNT(*)>1)",
            "DimSeason natural-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT SeasonID FROM DimSeason GROUP BY SeasonID HAVING COUNT(*)>1)",
            "DimSeason surrogate-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT SeasonKey FROM DimSeason GROUP BY SeasonKey HAVING COUNT(*)>1)",
            "DimRankTier natural-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT RankTierName FROM DimRankTier GROUP BY RankTierName HAVING COUNT(*)>1)",
            "DimRankTier order duplicate groups":
                "SELECT COUNT(*) FROM (SELECT RankTierOrder FROM DimRankTier GROUP BY RankTierOrder HAVING COUNT(*)>1)",
            "DimRankTier surrogate-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT RankTierKey FROM DimRankTier GROUP BY RankTierKey HAVING COUNT(*)>1)",
            "FactMatchParticipation surrogate-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT MatchParticipationFactKey FROM FactMatchParticipation GROUP BY MatchParticipationFactKey HAVING COUNT(*)>1)",
            "FactMatchParticipation source duplicate groups":
                "SELECT COUNT(*) FROM (SELECT SourceMatchParticipationID FROM FactMatchParticipation GROUP BY SourceMatchParticipationID HAVING COUNT(*)>1)",
            "FactMatchParticipation grain duplicate groups":
                "SELECT COUNT(*) FROM (SELECT MatchID,PlayerKey FROM FactMatchParticipation GROUP BY MatchID,PlayerKey HAVING COUNT(*)>1)",
            "FactPlayerSeasonSnapshot surrogate-key duplicate groups":
                "SELECT COUNT(*) FROM (SELECT PlayerSeasonSnapshotFactKey FROM FactPlayerSeasonSnapshot GROUP BY PlayerSeasonSnapshotFactKey HAVING COUNT(*)>1)",
            "FactPlayerSeasonSnapshot source duplicate groups":
                "SELECT COUNT(*) FROM (SELECT SourceSeasonRankingID FROM FactPlayerSeasonSnapshot GROUP BY SourceSeasonRankingID HAVING COUNT(*)>1)",
            "FactPlayerSeasonSnapshot grain duplicate groups":
                "SELECT COUNT(*) FROM (SELECT PlayerKey,SeasonKey FROM FactPlayerSeasonSnapshot GROUP BY PlayerKey,SeasonKey HAVING COUNT(*)>1)",
        }
        duplicate_results = {
            name: int_scalar(connection, sql) for name, sql in duplicate_queries.items()
        }
        result["duplicate_key_checks"] = duplicate_results
        for name, actual in duplicate_results.items():
            add_check(name, actual, 0)

        # Compare the entire Type 2 dimension with the two accepted staging versions.
        desired, policy = desired_player_versions(connection, config)
        result["effective_dating_policy"] = policy
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT PlayerKey, PlayerID, Username, Region, AccountLevel,
                   AccountStatus, EffectiveStartDateTime,
                   EffectiveEndDateTime, IsCurrent
              FROM DimPlayer
             ORDER BY PlayerID, EffectiveStartDateTime
            """
        )
        actual_versions: dict[tuple[int, datetime], tuple[Any, ...]] = {}
        player_keys: dict[tuple[int, datetime], int] = {}
        for row in cursor:
            natural_key = (int(row[1]), _as_datetime(row[6]))
            player_keys[natural_key] = int(row[0])
            actual_versions[natural_key] = (
                str(row[2]),
                str(row[3]),
                int(row[4]),
                str(row[5]),
                _as_datetime(row[7]),
                str(row[8]),
            )
        desired_versions = {
            (row.player_id, row.effective_start): (
                row.username,
                row.region,
                row.account_level,
                row.account_status,
                row.effective_end,
                row.is_current,
            )
            for row in desired
        }
        scd_mismatches = sorted(
            key
            for key in set(actual_versions) | set(desired_versions)
            if actual_versions.get(key) != desired_versions.get(key)
        )
        add_check("DimPlayer staged-version mismatch count", len(scd_mismatches), 0)
        result["scd2_mismatch_preview"] = [
            {"player_id": key[0], "effective_start": key[1]}
            for key in scd_mismatches[:20]
        ]
        add_check(
            "Players with exactly two intended Type 2 versions",
            int_scalar(
                connection,
                """
                SELECT COUNT(*) FROM (
                    SELECT PlayerID
                      FROM DimPlayer
                     GROUP BY PlayerID
                    HAVING COUNT(*)=2
                       AND SUM(CASE WHEN IsCurrent='Y' THEN 1 ELSE 0 END)=1
                       AND SUM(CASE WHEN IsCurrent='N' THEN 1 ELSE 0 END)=1
                )
                """,
            ),
            int(expected4["scd2_updated_players"]),
        )
        add_check(
            "Players with one current version",
            int_scalar(
                connection,
                """
                SELECT COUNT(*) FROM (
                    SELECT PlayerID
                      FROM DimPlayer
                     GROUP BY PlayerID
                    HAVING COUNT(*)=1
                       AND SUM(CASE WHEN IsCurrent='Y' THEN 1 ELSE 0 END)=1
                )
                """,
            ),
            int(expected4["single_version_players"]),
        )
        add_check(
            "DimPlayer overlapping version pairs",
            int_scalar(
                connection,
                """
                SELECT COUNT(*)
                  FROM DimPlayer a
                  JOIN DimPlayer b
                    ON b.PlayerID=a.PlayerID
                   AND b.PlayerKey>a.PlayerKey
                   AND a.EffectiveStartDateTime<b.EffectiveEndDateTime
                   AND b.EffectiveStartDateTime<a.EffectiveEndDateTime
                """,
            ),
            0,
        )
        add_check(
            "Current DimPlayer attributes differ from OLTP Player",
            int_scalar(
                connection,
                """
                SELECT COUNT(*)
                  FROM Player p
                  LEFT JOIN DimPlayer dp
                    ON dp.PlayerID=p.PlayerID AND dp.IsCurrent='Y'
                 WHERE dp.PlayerKey IS NULL
                    OR dp.Username<>p.Username
                    OR dp.Region<>p.Region
                    OR dp.AccountLevel<>p.AccountLevel
                    OR dp.AccountStatus<>p.AccountStatus
                """,
            ),
            0,
        )

        updated_ids = policy["updated_player_ids"]
        example_id = int(updated_ids[0])
        cursor.execute(
            """
            SELECT PlayerKey, PlayerID, Username, Region, AccountLevel,
                   AccountStatus, EffectiveStartDateTime,
                   EffectiveEndDateTime, IsCurrent
              FROM DimPlayer
             WHERE PlayerID=:player_id
             ORDER BY EffectiveStartDateTime
            """,
            player_id=example_id,
        )
        result["scd2_demo_player"] = [
            {
                "player_key": int(row[0]),
                "player_id": int(row[1]),
                "username": str(row[2]),
                "region": str(row[3]),
                "account_level": int(row[4]),
                "account_status": str(row[5]),
                "effective_start": row[6],
                "effective_end": row[7],
                "is_current": str(row[8]),
            }
            for row in cursor
        ]

        orphan_queries = {
            "FMP PlayerKey": "SELECT COUNT(*) FROM FactMatchParticipation f LEFT JOIN DimPlayer d ON d.PlayerKey=f.PlayerKey WHERE d.PlayerKey IS NULL",
            "FMP RoleKey": "SELECT COUNT(*) FROM FactMatchParticipation f LEFT JOIN DimRole d ON d.RoleKey=f.RoleKey WHERE d.RoleKey IS NULL",
            "FMP CharacterKey": "SELECT COUNT(*) FROM FactMatchParticipation f LEFT JOIN DimCharacter d ON d.CharacterKey=f.CharacterKey WHERE d.CharacterKey IS NULL",
            "FMP SeasonKey": "SELECT COUNT(*) FROM FactMatchParticipation f LEFT JOIN DimSeason d ON d.SeasonKey=f.SeasonKey WHERE d.SeasonKey IS NULL",
            "FMP DateKey": "SELECT COUNT(*) FROM FactMatchParticipation f LEFT JOIN DimDate d ON d.DateKey=f.DateKey WHERE d.DateKey IS NULL",
            "FMP TimeKey": "SELECT COUNT(*) FROM FactMatchParticipation f LEFT JOIN DimTime d ON d.TimeKey=f.TimeKey WHERE d.TimeKey IS NULL",
            "FPSS PlayerKey": "SELECT COUNT(*) FROM FactPlayerSeasonSnapshot f LEFT JOIN DimPlayer d ON d.PlayerKey=f.PlayerKey WHERE d.PlayerKey IS NULL",
            "FPSS SeasonKey": "SELECT COUNT(*) FROM FactPlayerSeasonSnapshot f LEFT JOIN DimSeason d ON d.SeasonKey=f.SeasonKey WHERE d.SeasonKey IS NULL",
            "FPSS RankTierKey": "SELECT COUNT(*) FROM FactPlayerSeasonSnapshot f LEFT JOIN DimRankTier d ON d.RankTierKey=f.RankTierKey WHERE d.RankTierKey IS NULL",
            "FPSS SnapshotDateKey": "SELECT COUNT(*) FROM FactPlayerSeasonSnapshot f LEFT JOIN DimDate d ON d.DateKey=f.SnapshotDateKey WHERE d.DateKey IS NULL",
        }
        orphan_results = {
            name: int_scalar(connection, sql) for name, sql in orphan_queries.items()
        }
        result["orphan_dimension_fk_checks"] = orphan_results
        add_check("Total orphan dimensional foreign keys", sum(orphan_results.values()), 0)

        # Every accepted source target must occur exactly once and no fact may
        # originate from a rejected/non-loaded staging row.
        add_check(
            "Loaded participation rows not represented exactly once",
            int_scalar(
                connection,
                """
                SELECT COUNT(*)
                  FROM StgParticipation sp
                 WHERE sp.LoadBatchID=:batch_id
                   AND sp.SourceFileName='participation.csv'
                   AND sp.ProcessingStatus='LOADED'
                   AND (SELECT COUNT(*) FROM FactMatchParticipation f
                         WHERE f.SourceMatchParticipationID=sp.TargetRecordID)<>1
                """,
                batch_id=initial_batch,
            ),
            0,
        )
        add_check(
            "Loaded ranking rows not represented exactly once",
            int_scalar(
                connection,
                """
                SELECT COUNT(*)
                  FROM StgSeasonRanking ssr
                 WHERE ssr.LoadBatchID=:batch_id
                   AND ssr.SourceFileName='season_ranking.csv'
                   AND ssr.ProcessingStatus='LOADED'
                   AND (SELECT COUNT(*) FROM FactPlayerSeasonSnapshot f
                         WHERE f.SourceSeasonRankingID=ssr.TargetRecordID)<>1
                """,
                batch_id=initial_batch,
            ),
            0,
        )
        add_check(
            "Participation facts without accepted staging lineage",
            int_scalar(
                connection,
                """
                SELECT COUNT(*)
                  FROM FactMatchParticipation f
                 WHERE NOT EXISTS (
                       SELECT 1 FROM StgParticipation sp
                        WHERE sp.TargetRecordID=f.SourceMatchParticipationID
                          AND sp.LoadBatchID=f.SourceLoadBatchID
                          AND sp.ProcessingStatus='LOADED')
                """,
            ),
            0,
        )
        add_check(
            "Snapshot facts without accepted staging lineage",
            int_scalar(
                connection,
                """
                SELECT COUNT(*)
                  FROM FactPlayerSeasonSnapshot f
                 WHERE NOT EXISTS (
                       SELECT 1 FROM StgSeasonRanking ssr
                        WHERE ssr.TargetRecordID=f.SourceSeasonRankingID
                          AND ssr.LoadBatchID=f.SourceLoadBatchID
                          AND ssr.ProcessingStatus='LOADED')
                """,
            ),
            0,
        )

        # Row-level transaction fact reconciliation, including Date/Time and
        # the historical DimPlayer version valid at MatchDateTime.
        add_check(
            "FactMatchParticipation row-level mismatches",
            int_scalar(
                connection,
                """
                WITH expected AS (
                    SELECT mp.MatchParticipationID AS SourceID,
                           m.MatchID, dp.PlayerKey, dr.RoleKey, dc.CharacterKey,
                           ds.SeasonKey, dd.DateKey, dt.TimeKey,
                           mp.TeamAssignment,
                           CASE WHEN mr.PlayerResult='WIN' THEN 1 ELSE 0 END AS WinFlag,
                           mr.RatingChange, sp.LoadBatchID
                      FROM StgParticipation sp
                      JOIN MatchParticipation mp ON mp.MatchParticipationID=sp.TargetRecordID
                      JOIN MatchResult mr ON mr.MatchParticipationID=mp.MatchParticipationID
                      JOIN Match m ON m.MatchID=mp.MatchID
                      JOIN DimPlayer dp
                        ON dp.PlayerID=mp.PlayerID
                       AND m.MatchDateTime>=dp.EffectiveStartDateTime
                       AND m.MatchDateTime<dp.EffectiveEndDateTime
                      JOIN DimRole dr ON dr.RoleID=mp.RoleID
                      JOIN DimCharacter dc ON dc.CharacterID=mp.CharacterID
                      JOIN DimSeason ds ON ds.SeasonID=m.SeasonID
                      JOIN DimDate dd ON dd.FullDate=TRUNC(CAST(m.MatchDateTime AS DATE))
                      JOIN DimTime dt ON dt.SecondsSinceMidnight=
                           TO_NUMBER(TO_CHAR(m.MatchDateTime,'HH24'))*3600+
                           TO_NUMBER(TO_CHAR(m.MatchDateTime,'MI'))*60+
                           TO_NUMBER(TO_CHAR(m.MatchDateTime,'SS'))
                     WHERE sp.LoadBatchID=:batch_id
                       AND sp.SourceFileName='participation.csv'
                       AND sp.ProcessingStatus='LOADED'
                )
                SELECT COUNT(*)
                  FROM expected e
                  LEFT JOIN FactMatchParticipation f
                    ON f.SourceMatchParticipationID=e.SourceID
                 WHERE f.MatchParticipationFactKey IS NULL
                    OR f.MatchID<>e.MatchID
                    OR f.PlayerKey<>e.PlayerKey
                    OR f.RoleKey<>e.RoleKey
                    OR f.CharacterKey<>e.CharacterKey
                    OR f.SeasonKey<>e.SeasonKey
                    OR f.DateKey<>e.DateKey
                    OR f.TimeKey<>e.TimeKey
                    OR f.TeamAssignment<>e.TeamAssignment
                    OR f.ParticipationCount<>1
                    OR f.WinFlag<>e.WinFlag
                    OR f.RatingChange<>e.RatingChange
                    OR f.SourceLoadBatchID<>e.LoadBatchID
                """,
                batch_id=initial_batch,
            ),
            0,
        )
        add_check(
            "Participation facts outside their DimPlayer effective interval",
            int_scalar(
                connection,
                """
                SELECT COUNT(*)
                  FROM FactMatchParticipation f
                  JOIN MatchParticipation mp
                    ON mp.MatchParticipationID=f.SourceMatchParticipationID
                  JOIN Match m ON m.MatchID=mp.MatchID
                  JOIN DimPlayer dp ON dp.PlayerKey=f.PlayerKey
                 WHERE dp.PlayerID<>mp.PlayerID
                    OR m.MatchDateTime<dp.EffectiveStartDateTime
                    OR m.MatchDateTime>=dp.EffectiveEndDateTime
                """,
            ),
            0,
        )
        add_check(
            "Historical facts for updated players using their new current version",
            int_scalar(
                connection,
                """
                SELECT COUNT(*)
                  FROM FactMatchParticipation f
                  JOIN DimPlayer dp ON dp.PlayerKey=f.PlayerKey
                  JOIN StgPlayer update_row
                    ON TO_NUMBER(TRIM(update_row.RawPlayerID))=dp.PlayerID
                 WHERE update_row.LoadBatchID=:update_batch
                   AND update_row.SourceFileName='player_scd2_update.csv'
                   AND update_row.ProcessingStatus='LOADED'
                   AND dp.IsCurrent='Y'
                """,
                update_batch=update_batch,
            ),
            0,
        )

        # Snapshot row-level reconciliation and measure relationships.
        add_check(
            "FactPlayerSeasonSnapshot row-level mismatches",
            int_scalar(
                connection,
                """
                WITH measures AS (
                    SELECT mp.PlayerID,
                           m.SeasonID,
                           COUNT(*) AS ParticipationCount,
                           SUM(CASE WHEN mr.PlayerResult='WIN' THEN 1 ELSE 0 END) AS WinCount,
                           SUM(mr.RatingChange) AS RatingChangeTotal
                      FROM MatchParticipation mp
                      JOIN Match m ON m.MatchID=mp.MatchID
                      JOIN MatchResult mr
                        ON mr.MatchParticipationID=mp.MatchParticipationID
                     GROUP BY mp.PlayerID, m.SeasonID
                ), expected AS (
                    SELECT sr.SeasonRankingID AS SourceID,
                           dp.PlayerKey, ds.SeasonKey, rt.RankTierKey,
                           dd.DateKey,
                           sr.SeasonCurrentRating-NVL(measures.RatingChangeTotal,0) AS StartingRating,
                           sr.SeasonCurrentRating AS EndingRating,
                           NVL(measures.ParticipationCount,0) AS ParticipationCount,
                           NVL(measures.WinCount,0) AS WinCount,
                           ssr.LoadBatchID
                      FROM StgSeasonRanking ssr
                      JOIN SeasonRanking sr ON sr.SeasonRankingID=ssr.TargetRecordID
                      JOIN Season s ON s.SeasonID=sr.SeasonID
                      LEFT JOIN measures
                        ON measures.PlayerID=sr.PlayerID
                       AND measures.SeasonID=sr.SeasonID
                      JOIN DimSeason ds ON ds.SeasonID=sr.SeasonID
                      JOIN DimRankTier rt ON rt.RankTierName=sr.SeasonRankTier
                      JOIN DimDate dd ON dd.FullDate=
                           CASE WHEN s.SeasonStatus='COMPLETED' THEN s.EndDate
                                ELSE LEAST(GREATEST(TRUNC(SYSDATE),s.StartDate),s.EndDate) END
                      JOIN DimPlayer dp
                        ON dp.PlayerID=sr.PlayerID
                       AND CAST(CASE WHEN s.SeasonStatus='COMPLETED' THEN s.EndDate
                                     ELSE LEAST(GREATEST(TRUNC(SYSDATE),s.StartDate),s.EndDate) END AS TIMESTAMP)
                           >=dp.EffectiveStartDateTime
                       AND CAST(CASE WHEN s.SeasonStatus='COMPLETED' THEN s.EndDate
                                     ELSE LEAST(GREATEST(TRUNC(SYSDATE),s.StartDate),s.EndDate) END AS TIMESTAMP)
                           <dp.EffectiveEndDateTime
                     WHERE ssr.LoadBatchID=:batch_id
                       AND ssr.SourceFileName='season_ranking.csv'
                       AND ssr.ProcessingStatus='LOADED'
                )
                SELECT COUNT(*)
                  FROM expected e
                  LEFT JOIN FactPlayerSeasonSnapshot f
                    ON f.SourceSeasonRankingID=e.SourceID
                 WHERE f.PlayerSeasonSnapshotFactKey IS NULL
                    OR f.PlayerKey<>e.PlayerKey
                    OR f.SeasonKey<>e.SeasonKey
                    OR f.RankTierKey<>e.RankTierKey
                    OR f.SnapshotDateKey<>e.DateKey
                    OR f.StartingRating<>e.StartingRating
                    OR f.EndingRating<>e.EndingRating
                    OR f.ParticipationCount<>e.ParticipationCount
                    OR f.WinCount<>e.WinCount
                    OR f.SourceLoadBatchID<>e.LoadBatchID
                """,
                batch_id=initial_batch,
            ),
            0,
        )
        add_check(
            "Snapshot Starting + Net = Ending violations",
            int_scalar(
                connection,
                """
                SELECT COUNT(*) FROM FactPlayerSeasonSnapshot
                 WHERE StartingRating+NetRatingChange<>EndingRating
                """,
            ),
            0,
        )
        add_check(
            "Snapshot RankTier resolution mismatches",
            int_scalar(
                connection,
                """
                SELECT COUNT(*)
                  FROM FactPlayerSeasonSnapshot f
                  JOIN SeasonRanking sr ON sr.SeasonRankingID=f.SourceSeasonRankingID
                  JOIN DimRankTier rt ON rt.RankTierKey=f.RankTierKey
                 WHERE rt.RankTierName<>sr.SeasonRankTier
                """,
            ),
            0,
        )
        add_check(
            "Snapshot Date resolution mismatches",
            int_scalar(
                connection,
                """
                SELECT COUNT(*)
                  FROM FactPlayerSeasonSnapshot f
                  JOIN SeasonRanking sr ON sr.SeasonRankingID=f.SourceSeasonRankingID
                  JOIN Season s ON s.SeasonID=sr.SeasonID
                  JOIN DimDate dd ON dd.DateKey=f.SnapshotDateKey
                 WHERE dd.FullDate<>CASE WHEN s.SeasonStatus='COMPLETED' THEN s.EndDate
                                        ELSE LEAST(GREATEST(TRUNC(SYSDATE),s.StartDate),s.EndDate) END
                """,
            ),
            0,
        )

        source_totals = fetch_one(
            connection,
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN mr.PlayerResult='WIN' THEN 1 ELSE 0 END),
                   SUM(mr.RatingChange)
              FROM StgParticipation sp
              JOIN MatchParticipation mp ON mp.MatchParticipationID=sp.TargetRecordID
              JOIN MatchResult mr ON mr.MatchParticipationID=mp.MatchParticipationID
             WHERE sp.LoadBatchID=:batch_id
               AND sp.SourceFileName='participation.csv'
               AND sp.ProcessingStatus='LOADED'
            """,
            batch_id=initial_batch,
        )
        fact_totals = fetch_one(
            connection,
            """
            SELECT SUM(ParticipationCount), SUM(WinFlag), SUM(RatingChange)
              FROM FactMatchParticipation
            """,
        )
        result["participation_measure_totals"] = {
            "oltp": source_totals,
            "warehouse": fact_totals,
        }
        add_check("Participation/win/rating totals reconcile", fact_totals, source_totals)

        snapshot_totals = fetch_one(
            connection,
            """
            SELECT SUM(ParticipationCount), SUM(WinCount), SUM(NetRatingChange)
              FROM FactPlayerSeasonSnapshot
            """,
        )
        expected_snapshot_totals = (
            source_totals[0],
            source_totals[1],
            source_totals[2],
        )
        result["snapshot_measure_totals"] = {
            "warehouse": snapshot_totals,
            "expected_from_oltp": expected_snapshot_totals,
        }
        add_check(
            "Snapshot participation/win/net-rating totals reconcile",
            snapshot_totals,
            expected_snapshot_totals,
        )

        result["failure_count"] = len(result["failures"])
        result["status"] = "PASS" if not result["failures"] else "FAIL"
        result["verified_at_utc"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        write_json(output_path, result)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["status"] == "PASS" else 1
    except Exception as exc:
        result["status"] = "FAIL"
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        result["failure_count"] = max(1, len(result["failures"]))
        result["verified_at_utc"] = datetime.now(timezone.utc).isoformat(
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
