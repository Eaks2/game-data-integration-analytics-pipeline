/*
  CS779 Term Project - Phase 1
  Finalized Oracle OLTP design

  This is create-only DDL for the revised design. It intentionally does not
  drop or overwrite the existing CS669 schema. Phase 2 should migrate or
  recreate the schema in a separate Oracle user after the design is approved.

  Source-ID policy for the proof of concept:
  - Python-generated PlayerID, MatchID, and SeasonID values are authoritative
    operational identifiers and are loaded unchanged.
  - Oracle sequences begin at 1,000,000,001 for records created inside Oracle,
    keeping the application-generated range separate from the synthetic range.

  Rating-domain policy:
  - The CS669 source used DECIMAL(7,2) and examples but no range constraints.
  - This proof of concept standardizes current ratings at 0.00..5000.00 and
    per-match rating changes at -100.00..+100.00 inclusive.
*/

CREATE TABLE Player (
    PlayerID        NUMBER(12)       NOT NULL,
    Username        VARCHAR2(50)     NOT NULL,
    Email           VARCHAR2(255)    NOT NULL,
    Region          VARCHAR2(50)     NOT NULL,
    AccountLevel    NUMBER(4)        NOT NULL,
    AccountStatus   VARCHAR2(20)     NOT NULL,
    CreatedAt       TIMESTAMP(0)     DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT PK_Player PRIMARY KEY (PlayerID),
    CONSTRAINT CK_Player_Level CHECK (AccountLevel >= 0),
    CONSTRAINT CK_Player_Status CHECK (
        AccountStatus IN ('ACTIVE', 'SUSPENDED', 'BANNED')
    )
);

CREATE UNIQUE INDEX UX_Player_Username_CI ON Player (LOWER(Username));
CREATE UNIQUE INDEX UX_Player_Email_CI ON Player (LOWER(Email));

CREATE TABLE Season (
    SeasonID        NUMBER(12)       NOT NULL,
    SeasonName      VARCHAR2(100)    NOT NULL,
    StartDate       DATE             NOT NULL,
    EndDate         DATE             NOT NULL,
    SeasonStatus    VARCHAR2(20)     NOT NULL,
    CONSTRAINT PK_Season PRIMARY KEY (SeasonID),
    CONSTRAINT UQ_Season_Name UNIQUE (SeasonName),
    CONSTRAINT CK_Season_Dates CHECK (EndDate > StartDate),
    CONSTRAINT CK_Season_Status CHECK (
        SeasonStatus IN ('PLANNED', 'ACTIVE', 'COMPLETED')
    )
);

CREATE TABLE Role (
    RoleID              NUMBER(12)       NOT NULL,
    RoleName            VARCHAR2(30)     NOT NULL,
    RoleDescription     VARCHAR2(255),
    CONSTRAINT PK_Role PRIMARY KEY (RoleID),
    CONSTRAINT UQ_Role_Name UNIQUE (RoleName)
);

CREATE TABLE Character (
    CharacterID         NUMBER(12)       NOT NULL,
    CharacterName       VARCHAR2(50)     NOT NULL,
    CONSTRAINT PK_Character PRIMARY KEY (CharacterID),
    CONSTRAINT UQ_Character_Name UNIQUE (CharacterName)
);

CREATE TABLE Match (
    MatchID          NUMBER(12)       NOT NULL,
    SeasonID         NUMBER(12)       NOT NULL,
    MatchStatus      VARCHAR2(20)     NOT NULL,
    MatchDateTime    TIMESTAMP(0)     NOT NULL,
    CONSTRAINT PK_Match PRIMARY KEY (MatchID),
    CONSTRAINT FK_Match_Season FOREIGN KEY (SeasonID)
        REFERENCES Season (SeasonID),
    CONSTRAINT CK_Match_Status CHECK (
        MatchStatus IN ('SCHEDULED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED')
    )
);

CREATE TABLE SeasonRanking (
    SeasonRankingID      NUMBER(12)       NOT NULL,
    PlayerID             NUMBER(12)       NOT NULL,
    SeasonID             NUMBER(12)       NOT NULL,
    SeasonCurrentRating  NUMBER(7,2)      NOT NULL,
    SeasonRankTier       VARCHAR2(30)     NOT NULL,
    CONSTRAINT PK_SeasonRanking PRIMARY KEY (SeasonRankingID),
    CONSTRAINT FK_SR_Player FOREIGN KEY (PlayerID)
        REFERENCES Player (PlayerID),
    CONSTRAINT FK_SR_Season FOREIGN KEY (SeasonID)
        REFERENCES Season (SeasonID),
    CONSTRAINT UQ_SR_Player_Season UNIQUE (PlayerID, SeasonID),
    CONSTRAINT CK_SR_Rating CHECK (
        SeasonCurrentRating BETWEEN 0.00 AND 5000.00
    ),
    CONSTRAINT CK_SR_Tier CHECK (
        SeasonRankTier IN ('BRONZE', 'SILVER', 'GOLD', 'PLATINUM', 'DIAMOND')
    )
);

CREATE TABLE MatchParticipation (
    MatchParticipationID  NUMBER(12)       NOT NULL,
    MatchID               NUMBER(12)       NOT NULL,
    PlayerID              NUMBER(12)       NOT NULL,
    RoleID                NUMBER(12)       NOT NULL,
    CharacterID           NUMBER(12)       NOT NULL,
    TeamAssignment        VARCHAR2(10)     NOT NULL,
    CONSTRAINT PK_MatchParticipation PRIMARY KEY (MatchParticipationID),
    CONSTRAINT FK_MP_Match FOREIGN KEY (MatchID)
        REFERENCES Match (MatchID),
    CONSTRAINT FK_MP_Player FOREIGN KEY (PlayerID)
        REFERENCES Player (PlayerID),
    CONSTRAINT FK_MP_Role FOREIGN KEY (RoleID)
        REFERENCES Role (RoleID),
    CONSTRAINT FK_MP_Character FOREIGN KEY (CharacterID)
        REFERENCES Character (CharacterID),
    CONSTRAINT UQ_MP_Match_Player UNIQUE (MatchID, PlayerID),
    CONSTRAINT CK_MP_Team CHECK (TeamAssignment IN ('TEAM_A', 'TEAM_B'))
);

/*
  A result is optional while a match is in progress and becomes mandatory
  before the match can be marked COMPLETED. Re-keying the table to the
  participation removes the repeated MatchID, PlayerID, and SeasonID values.
*/
CREATE TABLE MatchResult (
    MatchParticipationID  NUMBER(12)       NOT NULL,
    PlayerResult          VARCHAR2(10)     NOT NULL,
    RatingChange          NUMBER(7,2)      NOT NULL,
    CONSTRAINT PK_MatchResult PRIMARY KEY (MatchParticipationID),
    CONSTRAINT FK_MR_Participation FOREIGN KEY (MatchParticipationID)
        REFERENCES MatchParticipation (MatchParticipationID),
    CONSTRAINT CK_MR_Result CHECK (PlayerResult IN ('WIN', 'LOSS', 'DRAW')),
    CONSTRAINT CK_MR_RatingChange CHECK (
        RatingChange BETWEEN -100.00 AND 100.00
    ),
    CONSTRAINT CK_MR_ResultRating CHECK (
        (PlayerResult = 'WIN' AND RatingChange > 0)
        OR (PlayerResult = 'LOSS' AND RatingChange < 0)
        OR (PlayerResult = 'DRAW' AND RatingChange = 0)
    )
);

/*
  SeasonCurrentRating is intentionally retained as the current-state value.
  SeasonRatingChange is the audit history maintained by a trigger below.
*/
CREATE TABLE SeasonRatingChange (
    SeasonRatingChangeID  NUMBER(12)       NOT NULL,
    SeasonRankingID       NUMBER(12)       NOT NULL,
    OldRating             NUMBER(7,2)      NOT NULL,
    NewRating             NUMBER(7,2)      NOT NULL,
    RatingDelta           NUMBER(8,2)
        GENERATED ALWAYS AS (NewRating - OldRating) VIRTUAL,
    RecordedAt            TIMESTAMP(0)     DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT PK_SeasonRatingChange PRIMARY KEY (SeasonRatingChangeID),
    CONSTRAINT FK_SRC_SeasonRanking FOREIGN KEY (SeasonRankingID)
        REFERENCES SeasonRanking (SeasonRankingID),
    CONSTRAINT CK_SRC_OldRating CHECK (OldRating BETWEEN 0.00 AND 5000.00),
    CONSTRAINT CK_SRC_NewRating CHECK (NewRating BETWEEN 0.00 AND 5000.00),
    CONSTRAINT CK_SRC_DeltaRange CHECK (
        NewRating - OldRating BETWEEN -100.00 AND 100.00
    ),
    CONSTRAINT CK_SRC_Changed CHECK (OldRating <> NewRating)
);

/*
  Both players must have participated in the reported match. The two
  composite foreign keys use the MatchParticipation candidate key.
*/
CREATE TABLE Report (
    ReportID             NUMBER(12)       NOT NULL,
    MatchID              NUMBER(12)       NOT NULL,
    ReporterPlayerID     NUMBER(12)       NOT NULL,
    ReportedPlayerID     NUMBER(12)       NOT NULL,
    ReportReason         VARCHAR2(255)    NOT NULL,
    ReportDateTime       TIMESTAMP(0)     DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT PK_Report PRIMARY KEY (ReportID),
    CONSTRAINT FK_Report_ReporterMP FOREIGN KEY (MatchID, ReporterPlayerID)
        REFERENCES MatchParticipation (MatchID, PlayerID),
    CONSTRAINT FK_Report_ReportedMP FOREIGN KEY (MatchID, ReportedPlayerID)
        REFERENCES MatchParticipation (MatchID, PlayerID),
    CONSTRAINT CK_Report_DifferentPlayers CHECK (
        ReporterPlayerID <> ReportedPlayerID
    )
);

/*
  The CS669 subtype tables are replaced by an enforced discriminator. The
  affected player is obtained through Report.ReportedPlayerID and therefore
  is no longer stored redundantly here.
*/
CREATE TABLE EnforcementAction (
    EnforcementActionID    NUMBER(12)       NOT NULL,
    ReportID               NUMBER(12)       NOT NULL,
    ActionType             VARCHAR2(30)     NOT NULL,
    EnforcementReason      VARCHAR2(255)    NOT NULL,
    EnforcementDateTime    TIMESTAMP(0)     DEFAULT SYSTIMESTAMP NOT NULL,
    RestrictionEndDateTime TIMESTAMP(0),
    CONSTRAINT PK_EnforcementAction PRIMARY KEY (EnforcementActionID),
    CONSTRAINT FK_EA_Report FOREIGN KEY (ReportID)
        REFERENCES Report (ReportID),
    CONSTRAINT UQ_EA_Report UNIQUE (ReportID),
    CONSTRAINT CK_EA_Type CHECK (
        ActionType IN ('WARNING', 'TEMPORARY_RESTRICTION', 'BAN')
    ),
    CONSTRAINT CK_EA_RestrictionEnd CHECK (
        (ActionType = 'TEMPORARY_RESTRICTION'
         AND RestrictionEndDateTime IS NOT NULL
         AND RestrictionEndDateTime > EnforcementDateTime)
        OR
        (ActionType IN ('WARNING', 'BAN')
         AND RestrictionEndDateTime IS NULL)
    )
);

/* Sequences for Oracle-created rows; synthetic IDs remain below this range. */
CREATE SEQUENCE Player_Seq
    START WITH 1000000001 INCREMENT BY 1 CACHE 1000 NOORDER NOCYCLE;
CREATE SEQUENCE Season_Seq
    START WITH 1000000001 INCREMENT BY 1 CACHE 1000 NOORDER NOCYCLE;
CREATE SEQUENCE Role_Seq
    START WITH 1000000001 INCREMENT BY 1 CACHE 1000 NOORDER NOCYCLE;
CREATE SEQUENCE Character_Seq
    START WITH 1000000001 INCREMENT BY 1 CACHE 1000 NOORDER NOCYCLE;
CREATE SEQUENCE SeasonRanking_Seq
    START WITH 1000000001 INCREMENT BY 1 CACHE 1000 NOORDER NOCYCLE;
CREATE SEQUENCE Match_Seq
    START WITH 1000000001 INCREMENT BY 1 CACHE 1000 NOORDER NOCYCLE;
CREATE SEQUENCE MatchParticipation_Seq
    START WITH 1000000001 INCREMENT BY 1 CACHE 1000 NOORDER NOCYCLE;
CREATE SEQUENCE SeasonRatingChange_Seq
    START WITH 1000000001 INCREMENT BY 1 CACHE 1000 NOORDER NOCYCLE;
CREATE SEQUENCE Report_Seq
    START WITH 1000000001 INCREMENT BY 1 CACHE 1000 NOORDER NOCYCLE;
CREATE SEQUENCE EnforcementAction_Seq
    START WITH 1000000001 INCREMENT BY 1 CACHE 1000 NOORDER NOCYCLE;

/* Controlled reference values carried forward from the CS669 sample data. */
INSERT INTO Role (RoleID, RoleName, RoleDescription)
VALUES (1, 'TANK', 'Absorbs damage and initiates team fights');
INSERT INTO Role (RoleID, RoleName, RoleDescription)
VALUES (2, 'DAMAGE', 'Primary damage dealer');
INSERT INTO Role (RoleID, RoleName, RoleDescription)
VALUES (3, 'SUPPORT', 'Heals, shields, or enables teammates');
INSERT INTO Role (RoleID, RoleName, RoleDescription)
VALUES (4, 'ASSASSIN', 'High burst damage and flanking specialist');
INSERT INTO Role (RoleID, RoleName, RoleDescription)
VALUES (5, 'CONTROLLER', 'Provides crowd control and zoning');

INSERT INTO Character (CharacterID, CharacterName) VALUES (1, 'ATLAS');
INSERT INTO Character (CharacterID, CharacterName) VALUES (2, 'SERAPH');
INSERT INTO Character (CharacterID, CharacterName) VALUES (3, 'PYRO');
INSERT INTO Character (CharacterID, CharacterName) VALUES (4, 'VEX');
INSERT INTO Character (CharacterID, CharacterName) VALUES (5, 'NYX');

/* Foreign-key and query-path indexes. */
CREATE INDEX IX_Match_Season_Status_Time
    ON Match (SeasonID, MatchStatus, MatchDateTime);
CREATE INDEX IX_SR_Season_Tier
    ON SeasonRanking (SeasonID, SeasonRankTier);
CREATE INDEX IX_MP_Player
    ON MatchParticipation (PlayerID);
CREATE INDEX IX_MP_Role
    ON MatchParticipation (RoleID);
CREATE INDEX IX_MP_Character
    ON MatchParticipation (CharacterID);
CREATE INDEX IX_SRC_Ranking_Time
    ON SeasonRatingChange (SeasonRankingID, RecordedAt);
CREATE INDEX IX_Report_Reported
    ON Report (ReportedPlayerID, ReportDateTime);
CREATE INDEX IX_EA_Time
    ON EnforcementAction (EnforcementDateTime);

/* Enforce that a match timestamp falls within its referenced season. */
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

    IF CAST(:NEW.MatchDateTime AS DATE) < v_StartDate
       OR CAST(:NEW.MatchDateTime AS DATE) > v_EndDate THEN
        RAISE_APPLICATION_ERROR(
            -20010,
            'MatchDateTime must fall within the referenced season.'
        );
    END IF;
END;

/* Prevent more than ten participants or more than five on either team. */
CREATE OR REPLACE TRIGGER TRG_MP_5V5_Limit
FOR INSERT OR UPDATE OF MatchID, TeamAssignment ON MatchParticipation
COMPOUND TRIGGER
    TYPE t_MatchSet IS TABLE OF PLS_INTEGER INDEX BY VARCHAR2(40);
    g_MatchSet t_MatchSet;

    BEFORE EACH ROW IS
        v_Dummy NUMBER;
    BEGIN
        SELECT 1
          INTO v_Dummy
          FROM Match
         WHERE MatchID = :NEW.MatchID
           FOR UPDATE;
        g_MatchSet(TO_CHAR(:NEW.MatchID)) := 1;
    END BEFORE EACH ROW;

    AFTER STATEMENT IS
        v_Key        VARCHAR2(40);
        v_Total      NUMBER;
        v_TeamA      NUMBER;
        v_TeamB      NUMBER;
    BEGIN
        v_Key := g_MatchSet.FIRST;
        WHILE v_Key IS NOT NULL LOOP
            SELECT COUNT(*),
                   NVL(SUM(CASE WHEN TeamAssignment = 'TEAM_A' THEN 1 END), 0),
                   NVL(SUM(CASE WHEN TeamAssignment = 'TEAM_B' THEN 1 END), 0)
              INTO v_Total, v_TeamA, v_TeamB
              FROM MatchParticipation
             WHERE MatchID = TO_NUMBER(v_Key);

            IF v_Total > 10 OR v_TeamA > 5 OR v_TeamB > 5 THEN
                RAISE_APPLICATION_ERROR(
                    -20011,
                    'A 5v5 match permits at most ten players and five per team.'
                );
            END IF;
            v_Key := g_MatchSet.NEXT(v_Key);
        END LOOP;
    END AFTER STATEMENT;
END;

/* Preserve the participant set after a match reaches a terminal state. */
CREATE OR REPLACE TRIGGER TRG_MP_Terminal_Guard
BEFORE INSERT OR UPDATE OR DELETE ON MatchParticipation
FOR EACH ROW
DECLARE
    v_Status Match.MatchStatus%TYPE;
BEGIN
    IF INSERTING OR UPDATING THEN
        SELECT MatchStatus
          INTO v_Status
          FROM Match
         WHERE MatchID = :NEW.MatchID;

        IF v_Status IN ('COMPLETED', 'CANCELLED') THEN
            RAISE_APPLICATION_ERROR(
                -20013,
                'Participants cannot be changed for a completed or cancelled match.'
            );
        END IF;
    END IF;

    IF UPDATING OR DELETING THEN
        SELECT MatchStatus
          INTO v_Status
          FROM Match
         WHERE MatchID = :OLD.MatchID;

        IF v_Status IN ('COMPLETED', 'CANCELLED') THEN
            RAISE_APPLICATION_ERROR(
                -20013,
                'Participants cannot be changed for a completed or cancelled match.'
            );
        END IF;
    END IF;
END;

/* Preserve result completeness after the parent match becomes terminal. */
CREATE OR REPLACE TRIGGER TRG_MR_Terminal_Guard
BEFORE INSERT OR UPDATE OR DELETE ON MatchResult
FOR EACH ROW
DECLARE
    v_Status Match.MatchStatus%TYPE;
BEGIN
    IF INSERTING OR UPDATING THEN
        SELECT m.MatchStatus
          INTO v_Status
          FROM MatchParticipation mp
          JOIN Match m ON m.MatchID = mp.MatchID
         WHERE mp.MatchParticipationID = :NEW.MatchParticipationID;

        IF v_Status IN ('COMPLETED', 'CANCELLED') THEN
            RAISE_APPLICATION_ERROR(
                -20014,
                'Results cannot be changed for a completed or cancelled match.'
            );
        END IF;
    END IF;

    IF UPDATING OR DELETING THEN
        SELECT m.MatchStatus
          INTO v_Status
          FROM MatchParticipation mp
          JOIN Match m ON m.MatchID = mp.MatchID
         WHERE mp.MatchParticipationID = :OLD.MatchParticipationID;

        IF v_Status IN ('COMPLETED', 'CANCELLED') THEN
            RAISE_APPLICATION_ERROR(
                -20014,
                'Results cannot be changed for a completed or cancelled match.'
            );
        END IF;
    END IF;
END;

/* Terminal matches are not reopened; corrections require an explicit process. */
CREATE OR REPLACE TRIGGER TRG_Match_Status_Transition
BEFORE UPDATE OF MatchStatus ON Match
FOR EACH ROW
BEGIN
    IF :OLD.MatchStatus IN ('COMPLETED', 'CANCELLED')
       AND :NEW.MatchStatus <> :OLD.MatchStatus THEN
        RAISE_APPLICATION_ERROR(
            -20015,
            'Completed or cancelled matches cannot be reopened.'
        );
    END IF;
END;

/* A completed match must have ten participants, five per team, and ten results. */
CREATE OR REPLACE TRIGGER TRG_Match_Completion
BEFORE INSERT OR UPDATE OF MatchStatus ON Match
FOR EACH ROW
WHEN (NEW.MatchStatus = 'COMPLETED')
DECLARE
    v_TotalParticipants NUMBER;
    v_TeamA             NUMBER;
    v_TeamB             NUMBER;
    v_TotalResults      NUMBER;
    v_TeamAWins         NUMBER;
    v_TeamALosses       NUMBER;
    v_TeamADraws        NUMBER;
    v_TeamBWins         NUMBER;
    v_TeamBLosses       NUMBER;
    v_TeamBDraws        NUMBER;
BEGIN
    SELECT COUNT(*),
           NVL(SUM(CASE WHEN TeamAssignment = 'TEAM_A' THEN 1 END), 0),
           NVL(SUM(CASE WHEN TeamAssignment = 'TEAM_B' THEN 1 END), 0)
      INTO v_TotalParticipants, v_TeamA, v_TeamB
      FROM MatchParticipation
     WHERE MatchID = :NEW.MatchID;

    SELECT COUNT(*)
      INTO v_TotalResults
      FROM MatchResult mr
      JOIN MatchParticipation mp
        ON mp.MatchParticipationID = mr.MatchParticipationID
     WHERE mp.MatchID = :NEW.MatchID;

    IF v_TotalParticipants <> 10 OR v_TeamA <> 5 OR v_TeamB <> 5
       OR v_TotalResults <> 10 THEN
        RAISE_APPLICATION_ERROR(
            -20012,
            'A completed match requires ten participants, five per team, and ten results.'
        );
    END IF;

    SELECT
        NVL(SUM(CASE WHEN mp.TeamAssignment = 'TEAM_A'
                      AND mr.PlayerResult = 'WIN' THEN 1 ELSE 0 END), 0),
        NVL(SUM(CASE WHEN mp.TeamAssignment = 'TEAM_A'
                      AND mr.PlayerResult = 'LOSS' THEN 1 ELSE 0 END), 0),
        NVL(SUM(CASE WHEN mp.TeamAssignment = 'TEAM_A'
                      AND mr.PlayerResult = 'DRAW' THEN 1 ELSE 0 END), 0),
        NVL(SUM(CASE WHEN mp.TeamAssignment = 'TEAM_B'
                      AND mr.PlayerResult = 'WIN' THEN 1 ELSE 0 END), 0),
        NVL(SUM(CASE WHEN mp.TeamAssignment = 'TEAM_B'
                      AND mr.PlayerResult = 'LOSS' THEN 1 ELSE 0 END), 0),
        NVL(SUM(CASE WHEN mp.TeamAssignment = 'TEAM_B'
                      AND mr.PlayerResult = 'DRAW' THEN 1 ELSE 0 END), 0)
      INTO v_TeamAWins, v_TeamALosses, v_TeamADraws,
           v_TeamBWins, v_TeamBLosses, v_TeamBDraws
      FROM MatchParticipation mp
      JOIN MatchResult mr
        ON mr.MatchParticipationID = mp.MatchParticipationID
     WHERE mp.MatchID = :NEW.MatchID;

    IF NOT (
        (v_TeamAWins = 5 AND v_TeamALosses = 0 AND v_TeamADraws = 0
         AND v_TeamBWins = 0 AND v_TeamBLosses = 5 AND v_TeamBDraws = 0)
        OR
        (v_TeamAWins = 0 AND v_TeamALosses = 5 AND v_TeamADraws = 0
         AND v_TeamBWins = 5 AND v_TeamBLosses = 0 AND v_TeamBDraws = 0)
        OR
        (v_TeamAWins = 0 AND v_TeamALosses = 0 AND v_TeamADraws = 5
         AND v_TeamBWins = 0 AND v_TeamBLosses = 0 AND v_TeamBDraws = 5)
    ) THEN
        RAISE_APPLICATION_ERROR(
            -20016,
            'Results must be opposing 5 WIN/5 LOSS teams or ten DRAW results.'
        );
    END IF;
END;

/*
  Preserve every change to the controlled current-rating summary.
  RecordedAt is the database recording timestamp, not the match event time.
  Match.MatchDateTime remains authoritative for event-time analytics.
*/
CREATE OR REPLACE TRIGGER TRG_Season_Rating_History
AFTER UPDATE OF SeasonCurrentRating ON SeasonRanking
FOR EACH ROW
WHEN (OLD.SeasonCurrentRating <> NEW.SeasonCurrentRating)
BEGIN
    INSERT INTO SeasonRatingChange (
        SeasonRatingChangeID,
        SeasonRankingID,
        OldRating,
        NewRating,
        RecordedAt
    ) VALUES (
        SeasonRatingChange_Seq.NEXTVAL,
        :NEW.SeasonRankingID,
        :OLD.SeasonCurrentRating,
        :NEW.SeasonCurrentRating,
        SYSTIMESTAMP
    );
END;

/* Existing CS669 views can be preserved with revised joins. */
CREATE OR REPLACE VIEW VW_Active_Season_Rankings AS
SELECT
    p.PlayerID,
    p.Username,
    p.AccountStatus,
    s.SeasonID,
    s.SeasonName,
    s.SeasonStatus,
    sr.SeasonCurrentRating,
    sr.SeasonRankTier
FROM Player p
JOIN SeasonRanking sr ON sr.PlayerID = p.PlayerID
JOIN Season s ON s.SeasonID = sr.SeasonID;

CREATE OR REPLACE VIEW VW_Completed_Match_Detail AS
SELECT
    p.Username,
    s.SeasonName,
    m.MatchID,
    m.MatchDateTime,
    mp.TeamAssignment,
    r.RoleName,
    c.CharacterName,
    mr.PlayerResult,
    mr.RatingChange
FROM MatchParticipation mp
JOIN Player p ON p.PlayerID = mp.PlayerID
JOIN Match m ON m.MatchID = mp.MatchID
JOIN Season s ON s.SeasonID = m.SeasonID
JOIN Role r ON r.RoleID = mp.RoleID
JOIN Character c ON c.CharacterID = mp.CharacterID
JOIN MatchResult mr
  ON mr.MatchParticipationID = mp.MatchParticipationID
WHERE m.MatchStatus = 'COMPLETED';
