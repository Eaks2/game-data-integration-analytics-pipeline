/*
  Concise read-only SQL evidence. src/verify_phase4.py is the authoritative
  automated reconciliation and produces phase4_verification.json.
*/

/* Expected counts: 412 dates for the current source range, 86,400 times,
   2,585 player versions, 36,000 transaction facts, 7,500 snapshots. */
SELECT 'DimDate' AS CheckName, COUNT(*) AS ActualCount FROM DimDate
UNION ALL SELECT 'DimTime', COUNT(*) FROM DimTime
UNION ALL SELECT 'DimPlayer', COUNT(*) FROM DimPlayer
UNION ALL SELECT 'DimPlayer current', COUNT(*) FROM DimPlayer WHERE IsCurrent='Y'
UNION ALL SELECT 'DimRole', COUNT(*) FROM DimRole
UNION ALL SELECT 'DimCharacter', COUNT(*) FROM DimCharacter
UNION ALL SELECT 'DimSeason', COUNT(*) FROM DimSeason
UNION ALL SELECT 'DimRankTier', COUNT(*) FROM DimRankTier
UNION ALL SELECT 'FactMatchParticipation', COUNT(*) FROM FactMatchParticipation
UNION ALL SELECT 'FactPlayerSeasonSnapshot', COUNT(*) FROM FactPlayerSeasonSnapshot;

/* Expected: 25 two-version players and 2,535 one-version players. */
SELECT VersionCount, COUNT(*) AS PlayerCount
  FROM (
        SELECT PlayerID, COUNT(*) AS VersionCount
          FROM DimPlayer
         GROUP BY PlayerID
       )
 GROUP BY VersionCount
 ORDER BY VersionCount;

/* All counts below must be zero. */
SELECT 'FMP missing accepted staging lineage' AS CheckName, COUNT(*) AS FailureCount
  FROM FactMatchParticipation f
 WHERE NOT EXISTS (
       SELECT 1 FROM StgParticipation sp
        WHERE sp.TargetRecordID=f.SourceMatchParticipationID
          AND sp.LoadBatchID=f.SourceLoadBatchID
          AND sp.ProcessingStatus='LOADED')
UNION ALL
SELECT 'FPSS missing accepted staging lineage', COUNT(*)
  FROM FactPlayerSeasonSnapshot f
 WHERE NOT EXISTS (
       SELECT 1 FROM StgSeasonRanking ssr
        WHERE ssr.TargetRecordID=f.SourceSeasonRankingID
          AND ssr.LoadBatchID=f.SourceLoadBatchID
          AND ssr.ProcessingStatus='LOADED')
UNION ALL
SELECT 'FMP historical PlayerKey mismatch', COUNT(*)
  FROM FactMatchParticipation f
  JOIN MatchParticipation mp
    ON mp.MatchParticipationID=f.SourceMatchParticipationID
  JOIN Match m ON m.MatchID=mp.MatchID
  JOIN DimPlayer dp ON dp.PlayerKey=f.PlayerKey
 WHERE dp.PlayerID<>mp.PlayerID
    OR m.MatchDateTime<dp.EffectiveStartDateTime
    OR m.MatchDateTime>=dp.EffectiveEndDateTime
UNION ALL
SELECT 'Snapshot rating equation mismatch', COUNT(*)
  FROM FactPlayerSeasonSnapshot
 WHERE StartingRating+NetRatingChange<>EndingRating
UNION ALL
SELECT 'FMP source duplicates', COUNT(*)
  FROM (
        SELECT SourceMatchParticipationID
          FROM FactMatchParticipation
         GROUP BY SourceMatchParticipationID
        HAVING COUNT(*)>1
       )
UNION ALL
SELECT 'FPSS source duplicates', COUNT(*)
  FROM (
        SELECT SourceSeasonRankingID
          FROM FactPlayerSeasonSnapshot
         GROUP BY SourceSeasonRankingID
        HAVING COUNT(*)>1
       );

/* Fact totals should match each other and the OLTP sources. */
SELECT
    (SELECT SUM(ParticipationCount) FROM FactMatchParticipation) AS FactParticipations,
    (SELECT SUM(ParticipationCount) FROM FactPlayerSeasonSnapshot) AS SnapshotParticipations,
    (SELECT SUM(WinFlag) FROM FactMatchParticipation) AS FactWins,
    (SELECT SUM(WinCount) FROM FactPlayerSeasonSnapshot) AS SnapshotWins,
    (SELECT SUM(RatingChange) FROM FactMatchParticipation) AS FactNetRating,
    (SELECT SUM(NetRatingChange) FROM FactPlayerSeasonSnapshot) AS SnapshotNetRating
  FROM dual;

