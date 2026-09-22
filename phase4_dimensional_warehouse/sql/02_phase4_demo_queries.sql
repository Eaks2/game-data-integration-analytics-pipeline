/*
  Phase 4 presentation/demo queries. These are read-only and contain no slash
  terminators. Run the complete file as a script or select one statement at a
  time in DBeaver.
*/

/* 1. Dimensional row counts. */
SELECT 'DimDate' AS TableName, COUNT(*) AS RowCount FROM DimDate
UNION ALL SELECT 'DimTime', COUNT(*) FROM DimTime
UNION ALL SELECT 'DimPlayer', COUNT(*) FROM DimPlayer
UNION ALL SELECT 'DimRole', COUNT(*) FROM DimRole
UNION ALL SELECT 'DimCharacter', COUNT(*) FROM DimCharacter
UNION ALL SELECT 'DimSeason', COUNT(*) FROM DimSeason
UNION ALL SELECT 'DimRankTier', COUNT(*) FROM DimRankTier
UNION ALL SELECT 'FactMatchParticipation', COUNT(*) FROM FactMatchParticipation
UNION ALL SELECT 'FactPlayerSeasonSnapshot', COUNT(*) FROM FactPlayerSeasonSnapshot;

/* 2. One of the 25 players with an old closed row and a new current row. */
SELECT PlayerKey, PlayerID, Username, Region, AccountLevel, AccountStatus,
       EffectiveStartDateTime, EffectiveEndDateTime, IsCurrent
  FROM DimPlayer
 WHERE PlayerID = (
       SELECT MIN(PlayerID)
         FROM (
               SELECT PlayerID
                 FROM DimPlayer
                GROUP BY PlayerID
               HAVING COUNT(*) = 2
                  AND SUM(CASE WHEN IsCurrent='Y' THEN 1 ELSE 0 END) = 1
              )
       )
 ORDER BY EffectiveStartDateTime;

/* 3. One fact traced through staging, OLTP, and its historical dimensions. */
SELECT f.MatchParticipationFactKey,
       sp.LoadBatchID,
       sp.SourceFileName,
       sp.SourceRowNumber,
       f.SourceMatchParticipationID,
       f.MatchID,
       dp.PlayerKey,
       dp.PlayerID,
       dp.Region AS HistoricalRegion,
       dr.RoleName,
       dc.CharacterName,
       ds.SeasonName,
       dd.FullDate,
       dt.TimeValue,
       f.TeamAssignment,
       f.WinFlag,
       f.RatingChange
  FROM FactMatchParticipation f
  JOIN StgParticipation sp
    ON sp.TargetRecordID=f.SourceMatchParticipationID
   AND sp.LoadBatchID=f.SourceLoadBatchID
   AND sp.ProcessingStatus='LOADED'
  JOIN DimPlayer dp ON dp.PlayerKey=f.PlayerKey
  JOIN DimRole dr ON dr.RoleKey=f.RoleKey
  JOIN DimCharacter dc ON dc.CharacterKey=f.CharacterKey
  JOIN DimSeason ds ON ds.SeasonKey=f.SeasonKey
  JOIN DimDate dd ON dd.DateKey=f.DateKey
  JOIN DimTime dt ON dt.TimeKey=f.TimeKey
 ORDER BY f.MatchParticipationFactKey
 FETCH FIRST 1 ROW ONLY;

/* 4. Useful aggregation: selection volume and derived win rate by season/role/character. */
SELECT ds.SeasonName,
       dr.RoleName,
       dc.CharacterName,
       SUM(f.ParticipationCount) AS Selections,
       SUM(f.WinFlag) AS Wins,
       ROUND(100 * SUM(f.WinFlag) / NULLIF(SUM(f.ParticipationCount), 0), 2)
           AS WinRatePercent,
       SUM(f.RatingChange) AS NetRatingChange
  FROM FactMatchParticipation f
  JOIN DimSeason ds ON ds.SeasonKey=f.SeasonKey
  JOIN DimRole dr ON dr.RoleKey=f.RoleKey
  JOIN DimCharacter dc ON dc.CharacterKey=f.CharacterKey
 GROUP BY ds.SeasonName, dr.RoleName, dc.CharacterName
 ORDER BY ds.SeasonName, Selections DESC, dr.RoleName, dc.CharacterName;

COMMIT;

