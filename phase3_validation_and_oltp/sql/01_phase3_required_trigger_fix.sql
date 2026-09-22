/*
  Required narrow implementation correction discovered at Phase 3 preflight.

  Phase 1 defines Season.StartDate and EndDate as calendar dates and says a
  match timestamp is valid anywhere on those inclusive dates. The original
  trigger compared the full timestamp to EndDate at 00:00:00, which would
  reject 32 clean Phase 2 matches later on their valid final season date.

  This replacement changes only the comparison expression: it compares the
  calendar date of MatchDateTime to the inclusive season dates. No table,
  relationship, source rule, or project scope is changed.
*/

CREATE OR REPLACE TRIGGER TRG_Match_Season_Window
BEFORE INSERT OR UPDATE OF SeasonID, MatchDateTime ON Match
FOR EACH ROW
DECLARE
    v_StartDate Season.StartDate%TYPE;
    v_EndDate   Season.EndDate%TYPE;
BEGIN
    SELECT StartDate, EndDate
      INTO v_StartDate, v_EndDate
      FROM Season
     WHERE SeasonID = :NEW.SeasonID;

    IF TRUNC(CAST(:NEW.MatchDateTime AS DATE)) < TRUNC(v_StartDate)
       OR TRUNC(CAST(:NEW.MatchDateTime AS DATE)) > TRUNC(v_EndDate) THEN
        RAISE_APPLICATION_ERROR(
            -20010,
            'MatchDateTime calendar date must fall within the referenced season.'
        );
    END IF;
END;


SELECT Line, Position, Text
  FROM User_Errors
 WHERE Name = 'TRG_MATCH_SEASON_WINDOW'
   AND Type = 'TRIGGER'
 ORDER BY Sequence;

SELECT Trigger_Name, Status
  FROM User_Triggers
 WHERE Trigger_Name = 'TRG_MATCH_SEASON_WINDOW';

SELECT Object_Name, Status
FROM User_Objects
WHERE Object_Name = 'TRG_MATCH_SEASON_WINDOW'
  AND Object_Type = 'TRIGGER';

SELECT Object_Name, Status
FROM User_Objects
WHERE Object_Name = 'TRG_MATCH_SEASON_WINDOW'
  AND Object_Type = 'TRIGGER';
