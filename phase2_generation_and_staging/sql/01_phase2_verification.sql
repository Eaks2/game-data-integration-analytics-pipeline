/*
  CS779 Phase 2 read-only staging verification.

  SQL*Plus / SQLcl usage:
    @01_phase2_verification.sql <INITIAL_LOAD_BATCH_ID> <SCD2_LOAD_BATCH_ID>

  This script does not validate, reject, update staging statuses, or load a
  target. The Python verify_staging.py program performs the stronger all-cell
  CSV-to-staging comparison and error-manifest check.
*/

SET PAGESIZE 200
SET LINESIZE 220
SET VERIFY OFF

DEFINE INITIAL_BATCH_ID = '&1'
DEFINE SCD2_BATCH_ID = '&2'

PROMPT === LoadBatch control rows (must be STAGED with zero accepted/rejected) ===
SELECT LoadBatchID,
       SourceSystem,
       LoadStartTime,
       LoadEndTime,
       RowsRead,
       RowsAccepted,
       RowsRejected,
       LoadStatus
  FROM LoadBatch
 WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
 ORDER BY LoadBatchID;

PROMPT === Generated-to-staged row counts ===
SELECT 'INITIAL player.csv' AS SourceFile,
       2592 AS GeneratedRows,
       COUNT(*) AS StagedRows
  FROM StgPlayer
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND SourceFileName = 'player.csv'
UNION ALL
SELECT 'INITIAL match.csv', 3658, COUNT(*)
  FROM StgMatch
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND SourceFileName = 'match.csv'
UNION ALL
SELECT 'INITIAL participation.csv', 36172, COUNT(*)
  FROM StgParticipation
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND SourceFileName = 'participation.csv'
UNION ALL
SELECT 'INITIAL season_ranking.csv', 7560, COUNT(*)
  FROM StgSeasonRanking
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND SourceFileName = 'season_ranking.csv'
UNION ALL
SELECT 'SCD2 player_scd2_update.csv', 25, COUNT(*)
  FROM StgPlayer
 WHERE LoadBatchID = &SCD2_BATCH_ID
   AND SourceFileName = 'player_scd2_update.csv';

PROMPT === Lineage bounds and distinct source-row counts ===
SELECT SourceFileName,
       COUNT(*) AS RowCount,
       COUNT(DISTINCT SourceRowNumber) AS DistinctRowNumbers,
       MIN(SourceRowNumber) AS FirstRow,
       MAX(SourceRowNumber) AS LastRow
  FROM (
        SELECT SourceFileName, SourceRowNumber FROM StgPlayer
         WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
        UNION ALL
        SELECT SourceFileName, SourceRowNumber FROM StgMatch
         WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
        UNION ALL
        SELECT SourceFileName, SourceRowNumber FROM StgParticipation
         WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
        UNION ALL
        SELECT SourceFileName, SourceRowNumber FROM StgSeasonRanking
         WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
       )
 GROUP BY SourceFileName
 ORDER BY SourceFileName;

PROMPT === Phase 3 work must not have happened (every exception count must be zero) ===
SELECT 'StgPlayer non-NEW or target populated' AS ExceptionName, COUNT(*) AS ExceptionCount
  FROM StgPlayer
 WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
   AND (ProcessingStatus <> 'NEW' OR TargetRecordID IS NOT NULL)
UNION ALL
SELECT 'StgMatch non-NEW or target populated', COUNT(*)
  FROM StgMatch
 WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
   AND (ProcessingStatus <> 'NEW' OR TargetRecordID IS NOT NULL)
UNION ALL
SELECT 'StgParticipation non-NEW or target populated', COUNT(*)
  FROM StgParticipation
 WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
   AND (ProcessingStatus <> 'NEW' OR TargetRecordID IS NOT NULL)
UNION ALL
SELECT 'StgSeasonRanking non-NEW or target populated', COUNT(*)
  FROM StgSeasonRanking
 WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID)
   AND (ProcessingStatus <> 'NEW' OR TargetRecordID IS NOT NULL)
UNION ALL
SELECT 'RejectedRecord rows created', COUNT(*)
  FROM RejectedRecord
 WHERE LoadBatchID IN (&INITIAL_BATCH_ID, &SCD2_BATCH_ID);

PROMPT === Representative malformed raw text that must survive ingestion ===
SELECT 'PLAYER_EMAIL' AS Fixture,
       SourceFileName,
       SourceRowNumber,
       RawEmail AS RawValue
  FROM StgPlayer
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND RawEmail = 'not-an-email'
UNION ALL
SELECT 'MATCH_TIMESTAMP', SourceFileName, SourceRowNumber, RawMatchDateTime
  FROM StgMatch
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND RawMatchDateTime = 'not-a-timestamp'
UNION ALL
SELECT 'PARTICIPATION_NUMBER', SourceFileName, SourceRowNumber, RawRatingChange
  FROM StgParticipation
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND RawRatingChange = 'NOT_A_NUMBER'
UNION ALL
SELECT 'RANKING_NUMBER', SourceFileName, SourceRowNumber, RawCurrentRating
  FROM StgSeasonRanking
 WHERE LoadBatchID = &INITIAL_BATCH_ID
   AND RawCurrentRating = 'NOT_A_NUMBER'
 ORDER BY Fixture, SourceRowNumber;

PROMPT === Legitimate later player versions (all 25 rows are valid updates) ===
SELECT SourceFileName,
       COUNT(*) AS UpdateRows,
       MIN(SourceRowNumber) AS FirstRow,
       MAX(SourceRowNumber) AS LastRow,
       MIN(ProcessingStatus) AS MinStatus,
       MAX(ProcessingStatus) AS MaxStatus
  FROM StgPlayer
 WHERE LoadBatchID = &SCD2_BATCH_ID
   AND SourceFileName = 'player_scd2_update.csv'
 GROUP BY SourceFileName;

UNDEFINE INITIAL_BATCH_ID
UNDEFINE SCD2_BATCH_ID
