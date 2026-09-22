/*
  Controlled reference prerequisites copied from the authoritative Phase 1
  decisions. Season, Role, and Character are required controlled prerequisites
  before the Phase 3 normalized load. Run this after the finalized OLTP tables
  exist; it is safe when the expected rows are already present.

  The MERGE statements are restart-safe and do not alter a matching reference
  row. A conflicting existing value is intentionally left for manual review.
*/

MERGE INTO Season target
USING (
    SELECT 1 SeasonID, '2024 Season 1' SeasonName,
           DATE '2024-01-01' StartDate, DATE '2024-04-30' EndDate,
           'COMPLETED' SeasonStatus FROM dual
    UNION ALL
    SELECT 2, '2024 Season 2', DATE '2024-05-15', DATE '2024-09-15',
           'COMPLETED' FROM dual
    UNION ALL
    SELECT 3, '2024-25 Season 3', DATE '2024-10-01', DATE '2025-02-15',
           'COMPLETED' FROM dual
) source
ON (target.SeasonID = source.SeasonID)
WHEN NOT MATCHED THEN INSERT (
    SeasonID, SeasonName, StartDate, EndDate, SeasonStatus
) VALUES (
    source.SeasonID, source.SeasonName, source.StartDate, source.EndDate,
    source.SeasonStatus
);

MERGE INTO Role target
USING (
    SELECT 1 RoleID, 'TANK' RoleName,
           'Absorbs damage and initiates team fights' RoleDescription FROM dual
    UNION ALL SELECT 2, 'DAMAGE', 'Primary damage dealer' FROM dual
    UNION ALL SELECT 3, 'SUPPORT', 'Heals, shields, or enables teammates' FROM dual
    UNION ALL SELECT 4, 'ASSASSIN', 'High burst damage and flanking specialist' FROM dual
    UNION ALL SELECT 5, 'CONTROLLER', 'Provides crowd control and zoning' FROM dual
) source
ON (target.RoleID = source.RoleID)
WHEN NOT MATCHED THEN INSERT (RoleID, RoleName, RoleDescription)
VALUES (source.RoleID, source.RoleName, source.RoleDescription);

MERGE INTO Character target
USING (
    SELECT 1 CharacterID, 'ATLAS' CharacterName FROM dual
    UNION ALL SELECT 2, 'SERAPH' FROM dual
    UNION ALL SELECT 3, 'PYRO' FROM dual
    UNION ALL SELECT 4, 'VEX' FROM dual
    UNION ALL SELECT 5, 'NYX' FROM dual
) source
ON (target.CharacterID = source.CharacterID)
WHEN NOT MATCHED THEN INSERT (CharacterID, CharacterName)
VALUES (source.CharacterID, source.CharacterName);

COMMIT;

SELECT SeasonID, SeasonName, StartDate, EndDate, SeasonStatus
  FROM Season
 WHERE SeasonID IN (1, 2, 3)
 ORDER BY SeasonID;

SELECT RoleID, RoleName, RoleDescription
  FROM Role
 WHERE RoleID BETWEEN 1 AND 5
 ORDER BY RoleID;

SELECT CharacterID, CharacterName
  FROM Character
 WHERE CharacterID BETWEEN 1 AND 5
 ORDER BY CharacterID;
