/*
  Read-only Phase 3 summary. The Python verify_phase3.py script performs the
  authoritative row-by-row manifest and TargetRecordID reconciliation.

  SQL*Plus / SQLcl:
    @03_phase3_verification.sql 1 2
*/

SET PAGESIZE 500
SET LINESIZE 240
SET VERIFY OFF

DEFINE INITIAL_BATCH_ID = '&1'
DEFINE SCD2_BATCH_ID = '&2'

PROMPT === Batch reconciliation ===
SELECT LoadBatchID, SourceSystem, RowsRead, RowsAccepted, RowsRejected,
       LoadStatus, LoadStartTime, LoadEndTime
  FROM LoadBatch
 WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
 ORDER BY LoadBatchID;

PROMPT === Staging rows by source and status ===
SELECT SourceFileName, ProcessingStatus, COUNT(*) AS RowCount
  FROM (
        SELECT SourceFileName, ProcessingStatus FROM StgPlayer
         WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
        UNION ALL
        SELECT SourceFileName, ProcessingStatus FROM StgMatch
         WHERE LoadBatchID = &INITIAL_BATCH_ID
        UNION ALL
        SELECT SourceFileName, ProcessingStatus FROM StgParticipation
         WHERE LoadBatchID = &INITIAL_BATCH_ID
        UNION ALL
        SELECT SourceFileName, ProcessingStatus FROM StgSeasonRanking
         WHERE LoadBatchID = &INITIAL_BATCH_ID
       )
 GROUP BY SourceFileName, ProcessingStatus
 ORDER BY SourceFileName, ProcessingStatus;

PROMPT === Rejection reason rows by source file ===
SELECT SourceFileName,
       COUNT(*) AS ReasonRows,
       COUNT(DISTINCT SourceTable || ':' || TO_CHAR(StagingRecordID)) AS RejectedStageRows
  FROM RejectedRecord
 WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
 GROUP BY SourceFileName
 ORDER BY SourceFileName;

PROMPT === Rejection reason rows by deterministic error code ===
SELECT ErrorCode, COUNT(*) AS ReasonRows
  FROM RejectedRecord
 WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
 GROUP BY ErrorCode
 ORDER BY ErrorCode;

PROMPT === Target mapping exceptions (all counts must be zero) ===
SELECT 'Loaded StgPlayer missing target' AS ExceptionName, COUNT(*) AS ExceptionCount
  FROM StgPlayer
 WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
   AND ProcessingStatus = 'LOADED' AND TargetRecordID IS NULL
UNION ALL
SELECT 'Rejected StgPlayer has target', COUNT(*)
  FROM StgPlayer
 WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
   AND ProcessingStatus = 'REJECTED' AND TargetRecordID IS NOT NULL
UNION ALL
SELECT 'Loaded StgMatch missing target', COUNT(*)
  FROM StgMatch
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND ProcessingStatus = 'LOADED' AND TargetRecordID IS NULL
UNION ALL
SELECT 'Rejected StgMatch has target', COUNT(*)
  FROM StgMatch
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND ProcessingStatus = 'REJECTED' AND TargetRecordID IS NOT NULL
UNION ALL
SELECT 'Loaded StgParticipation missing target', COUNT(*)
  FROM StgParticipation
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND ProcessingStatus = 'LOADED' AND TargetRecordID IS NULL
UNION ALL
SELECT 'Rejected StgParticipation has target', COUNT(*)
  FROM StgParticipation
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND ProcessingStatus = 'REJECTED' AND TargetRecordID IS NOT NULL
UNION ALL
SELECT 'Loaded StgSeasonRanking missing target', COUNT(*)
  FROM StgSeasonRanking
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND ProcessingStatus = 'LOADED' AND TargetRecordID IS NULL
UNION ALL
SELECT 'Rejected StgSeasonRanking has target', COUNT(*)
  FROM StgSeasonRanking
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND ProcessingStatus = 'REJECTED' AND TargetRecordID IS NOT NULL;

PROMPT === OLTP row counts ===
SELECT 'Player' AS TableName, COUNT(*) AS RowCount FROM Player
UNION ALL SELECT 'Match', COUNT(*) FROM Match
UNION ALL SELECT 'MatchParticipation', COUNT(*) FROM MatchParticipation
UNION ALL SELECT 'MatchResult', COUNT(*) FROM MatchResult
UNION ALL SELECT 'SeasonRanking', COUNT(*) FROM SeasonRanking
UNION ALL SELECT 'SeasonRatingChange', COUNT(*) FROM SeasonRatingChange;

PROMPT === Source-associated distinct OLTP targets ===
SELECT 'Player initial' AS SourceName, COUNT(DISTINCT TargetRecordID) AS TargetCount
  FROM StgPlayer WHERE LoadBatchID=&INITIAL_BATCH_ID AND ProcessingStatus='LOADED'
UNION ALL
SELECT 'Player SCD2 update', COUNT(DISTINCT TargetRecordID)
  FROM StgPlayer WHERE LoadBatchID=&SCD2_BATCH_ID AND ProcessingStatus='LOADED'
UNION ALL
SELECT 'Match', COUNT(DISTINCT TargetRecordID)
  FROM StgMatch WHERE LoadBatchID=&INITIAL_BATCH_ID AND ProcessingStatus='LOADED'
UNION ALL
SELECT 'MatchParticipation/Result', COUNT(DISTINCT TargetRecordID)
  FROM StgParticipation WHERE LoadBatchID=&INITIAL_BATCH_ID AND ProcessingStatus='LOADED'
UNION ALL
SELECT 'SeasonRanking', COUNT(DISTINCT TargetRecordID)
  FROM StgSeasonRanking WHERE LoadBatchID=&INITIAL_BATCH_ID AND ProcessingStatus='LOADED';

PROMPT === Orphan FK checks (all counts must be zero) ===
SELECT 'MatchParticipation -> Match' AS CheckName, COUNT(*) AS OrphanCount
  FROM MatchParticipation mp LEFT JOIN Match m ON m.MatchID=mp.MatchID
 WHERE m.MatchID IS NULL
UNION ALL
SELECT 'MatchParticipation -> Player', COUNT(*)
  FROM MatchParticipation mp LEFT JOIN Player p ON p.PlayerID=mp.PlayerID
 WHERE p.PlayerID IS NULL
UNION ALL
SELECT 'MatchParticipation -> Role', COUNT(*)
  FROM MatchParticipation mp LEFT JOIN Role r ON r.RoleID=mp.RoleID
 WHERE r.RoleID IS NULL
UNION ALL
SELECT 'MatchParticipation -> Character', COUNT(*)
  FROM MatchParticipation mp LEFT JOIN Character c ON c.CharacterID=mp.CharacterID
 WHERE c.CharacterID IS NULL
UNION ALL
SELECT 'MatchResult -> MatchParticipation', COUNT(*)
  FROM MatchResult mr LEFT JOIN MatchParticipation mp
    ON mp.MatchParticipationID=mr.MatchParticipationID
 WHERE mp.MatchParticipationID IS NULL
UNION ALL
SELECT 'SeasonRanking -> Player', COUNT(*)
  FROM SeasonRanking sr LEFT JOIN Player p ON p.PlayerID=sr.PlayerID
 WHERE p.PlayerID IS NULL
UNION ALL
SELECT 'SeasonRanking -> Season', COUNT(*)
  FROM SeasonRanking sr LEFT JOIN Season s ON s.SeasonID=sr.SeasonID
 WHERE s.SeasonID IS NULL
UNION ALL
SELECT 'SeasonRatingChange -> SeasonRanking', COUNT(*)
  FROM SeasonRatingChange src LEFT JOIN SeasonRanking sr
    ON sr.SeasonRankingID=src.SeasonRankingID
 WHERE sr.SeasonRankingID IS NULL;

PROMPT === Duplicate natural/business-key checks (all counts must be zero) ===
SELECT 'Player username case-insensitive' AS CheckName, COUNT(*) AS DuplicateGroups
  FROM (SELECT LOWER(Username) FROM Player GROUP BY LOWER(Username) HAVING COUNT(*)>1)
UNION ALL
SELECT 'Player email case-insensitive', COUNT(*)
  FROM (SELECT LOWER(Email) FROM Player GROUP BY LOWER(Email) HAVING COUNT(*)>1)
UNION ALL
SELECT 'MatchParticipation MatchID/PlayerID', COUNT(*)
  FROM (SELECT MatchID,PlayerID FROM MatchParticipation GROUP BY MatchID,PlayerID HAVING COUNT(*)>1)
UNION ALL
SELECT 'SeasonRanking PlayerID/SeasonID', COUNT(*)
  FROM (SELECT PlayerID,SeasonID FROM SeasonRanking GROUP BY PlayerID,SeasonID HAVING COUNT(*)>1);

PROMPT === Legitimate player update preservation ===
SELECT COUNT(*) AS UpdateRows,
       COUNT(DISTINCT update_row.RawPlayerID) AS DistinctPlayers,
       SUM(CASE WHEN initial_row.StgPlayerID IS NOT NULL THEN 1 ELSE 0 END) AS InitialVersionsFound,
       SUM(CASE WHEN p.PlayerID IS NOT NULL THEN 1 ELSE 0 END) AS CurrentPlayersFound
  FROM StgPlayer update_row
  LEFT JOIN StgPlayer initial_row
    ON initial_row.LoadBatchID=&INITIAL_BATCH_ID
   AND initial_row.SourceFileName='player.csv'
   AND initial_row.ProcessingStatus='LOADED'
   AND initial_row.RawPlayerID=update_row.RawPlayerID
  LEFT JOIN Player p ON p.PlayerID=update_row.TargetRecordID
 WHERE update_row.LoadBatchID=&SCD2_BATCH_ID
   AND update_row.SourceFileName='player_scd2_update.csv'
   AND update_row.ProcessingStatus='LOADED';

PROMPT === Season rating/history counts ===
SELECT
    (SELECT COUNT(*)
       FROM StgParticipation
      WHERE LoadBatchID=&INITIAL_BATCH_ID
        AND ProcessingStatus='LOADED'
        AND TO_NUMBER(RawRatingChange)<>0) AS ExpectedNonzeroHistoryRows,
    (SELECT COUNT(*)
       FROM SeasonRatingChange src
       JOIN StgSeasonRanking ssr
         ON ssr.TargetRecordID=src.SeasonRankingID
        AND ssr.LoadBatchID=&INITIAL_BATCH_ID
        AND ssr.ProcessingStatus='LOADED') AS ActualHistoryRows
  FROM dual;

UNDEFINE INITIAL_BATCH_ID
UNDEFINE SCD2_BATCH_ID
