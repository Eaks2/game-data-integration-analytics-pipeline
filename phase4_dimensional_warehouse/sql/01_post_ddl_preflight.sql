/*
  Run after 00_authoritative_dimensional_ddl.sql.
  There are no PL/SQL blocks or slash terminators in either file.
*/

COMMIT;

SELECT ObjectName, ObjectType, Status
  FROM (
        SELECT Object_Name AS ObjectName, Object_Type AS ObjectType, Status
          FROM User_Objects
         WHERE Object_Name IN (
             'DIMDATE','DIMTIME','DIMPLAYER','DIMROLE','DIMCHARACTER',
             'DIMSEASON','DIMRANKTIER','FACTMATCHPARTICIPATION',
             'FACTPLAYERSEASONSNAPSHOT'
         )
       )
 ORDER BY ObjectType, ObjectName;

SELECT RankTierKey, RankTierName, RankTierOrder
  FROM DimRankTier
 ORDER BY RankTierOrder;

SELECT LoadBatchID, RowsRead, RowsAccepted, RowsRejected, LoadStatus,
       LoadStartTime, LoadEndTime
  FROM LoadBatch
 WHERE LoadBatchID IN (1, 2)
 ORDER BY LoadBatchID;

SELECT 'Player' AS SourceTable, COUNT(*) AS RowCount FROM Player
UNION ALL SELECT 'Match', COUNT(*) FROM Match
UNION ALL SELECT 'MatchParticipation', COUNT(*) FROM MatchParticipation
UNION ALL SELECT 'MatchResult', COUNT(*) FROM MatchResult
UNION ALL SELECT 'SeasonRanking', COUNT(*) FROM SeasonRanking
UNION ALL SELECT 'SeasonRatingChange', COUNT(*) FROM SeasonRatingChange;

