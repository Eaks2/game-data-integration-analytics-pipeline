/*
  CS779 Term Project - Phase 1
  Finalized Oracle dimensional model

  The model uses two facts because match participation and player-season
  ranking snapshots have different grains. Keeping them separate prevents
  season-level ratings and ranks from being duplicated once per match.
*/

CREATE TABLE DimDate (
    DateKey          NUMBER(10)       NOT NULL,
    FullDate         DATE             NOT NULL,
    DayOfMonth       NUMBER(2)        NOT NULL,
    DayOfWeekNumber  NUMBER(1)        NOT NULL,
    DayName          VARCHAR2(10)     NOT NULL,
    WeekOfYear       NUMBER(2)        NOT NULL,
    MonthNumber      NUMBER(2)        NOT NULL,
    MonthName        VARCHAR2(10)     NOT NULL,
    QuarterNumber    NUMBER(1)        NOT NULL,
    YearNumber       NUMBER(4)        NOT NULL,
    IsWeekend        CHAR(1)          NOT NULL,
    CONSTRAINT PK_DimDate PRIMARY KEY (DateKey),
    CONSTRAINT UQ_DimDate_FullDate UNIQUE (FullDate),
    CONSTRAINT CK_DimDate_Day CHECK (DayOfMonth BETWEEN 1 AND 31),
    CONSTRAINT CK_DimDate_DayOfWeek CHECK (DayOfWeekNumber BETWEEN 1 AND 7),
    CONSTRAINT CK_DimDate_Week CHECK (WeekOfYear BETWEEN 1 AND 53),
    CONSTRAINT CK_DimDate_Month CHECK (MonthNumber BETWEEN 1 AND 12),
    CONSTRAINT CK_DimDate_Quarter CHECK (QuarterNumber BETWEEN 1 AND 4),
    CONSTRAINT CK_DimDate_Weekend CHECK (IsWeekend IN ('Y', 'N'))
);

CREATE TABLE DimTime (
    TimeKey             NUMBER(10)       NOT NULL,
    TimeValue           VARCHAR2(8)      NOT NULL,
    SecondsSinceMidnight NUMBER(5)       NOT NULL,
    HourNumber          NUMBER(2)        NOT NULL,
    MinuteNumber        NUMBER(2)        NOT NULL,
    SecondNumber        NUMBER(2)        NOT NULL,
    DayPart             VARCHAR2(20)     NOT NULL,
    CONSTRAINT PK_DimTime PRIMARY KEY (TimeKey),
    CONSTRAINT UQ_DimTime_Value UNIQUE (TimeValue),
    CONSTRAINT UQ_DimTime_Second UNIQUE (SecondsSinceMidnight),
    CONSTRAINT CK_DimTime_Hour CHECK (HourNumber BETWEEN 0 AND 23),
    CONSTRAINT CK_DimTime_Minute CHECK (MinuteNumber BETWEEN 0 AND 59),
    CONSTRAINT CK_DimTime_SecondPart CHECK (SecondNumber BETWEEN 0 AND 59),
    CONSTRAINT CK_DimTime_Seconds CHECK (
        SecondsSinceMidnight BETWEEN 0 AND 86399
    ),
    CONSTRAINT CK_DimTime_Components CHECK (
        SecondsSinceMidnight =
            (HourNumber * 3600) + (MinuteNumber * 60) + SecondNumber
    )
);

/* Type 2 dimension: one natural player may have multiple dated versions. */
CREATE TABLE DimPlayer (
    PlayerKey               NUMBER(12)       NOT NULL,
    PlayerID                NUMBER(12)       NOT NULL,
    Username                VARCHAR2(50)     NOT NULL,
    Region                  VARCHAR2(50)     NOT NULL,
    AccountLevel            NUMBER(4)        NOT NULL,
    AccountStatus           VARCHAR2(20)     NOT NULL,
    EffectiveStartDateTime  TIMESTAMP(0)     NOT NULL,
    EffectiveEndDateTime    TIMESTAMP(0)     NOT NULL,
    IsCurrent               CHAR(1)          NOT NULL,
    CONSTRAINT PK_DimPlayer PRIMARY KEY (PlayerKey),
    CONSTRAINT UQ_DP_Version UNIQUE (PlayerID, EffectiveStartDateTime),
    CONSTRAINT CK_DP_Dates CHECK (
        EffectiveEndDateTime > EffectiveStartDateTime
    ),
    CONSTRAINT CK_DP_Level CHECK (AccountLevel >= 0),
    CONSTRAINT CK_DP_Status CHECK (
        AccountStatus IN ('ACTIVE', 'SUSPENDED', 'BANNED')
    ),
    CONSTRAINT CK_DP_Current CHECK (IsCurrent IN ('Y', 'N'))
);

/* Only one open/current Type 2 row is permitted for a natural PlayerID. */
CREATE UNIQUE INDEX UX_DP_Current
    ON DimPlayer (CASE WHEN IsCurrent = 'Y' THEN PlayerID END);

CREATE TABLE DimRole (
    RoleKey          NUMBER(12)       NOT NULL,
    RoleID           NUMBER(12)       NOT NULL,
    RoleName         VARCHAR2(30)     NOT NULL,
    RoleDescription  VARCHAR2(255),
    CONSTRAINT PK_DimRole PRIMARY KEY (RoleKey),
    CONSTRAINT UQ_DimRole_ID UNIQUE (RoleID),
    CONSTRAINT UQ_DimRole_Name UNIQUE (RoleName)
);

CREATE TABLE DimCharacter (
    CharacterKey     NUMBER(12)       NOT NULL,
    CharacterID      NUMBER(12)       NOT NULL,
    CharacterName    VARCHAR2(50)     NOT NULL,
    CONSTRAINT PK_DimCharacter PRIMARY KEY (CharacterKey),
    CONSTRAINT UQ_DimCharacter_ID UNIQUE (CharacterID),
    CONSTRAINT UQ_DimCharacter_Name UNIQUE (CharacterName)
);

CREATE TABLE DimSeason (
    SeasonKey        NUMBER(12)       NOT NULL,
    SeasonID         NUMBER(12)       NOT NULL,
    SeasonName       VARCHAR2(100)    NOT NULL,
    StartDate        DATE             NOT NULL,
    EndDate          DATE             NOT NULL,
    SeasonStatus     VARCHAR2(20)     NOT NULL,
    CONSTRAINT PK_DimSeason PRIMARY KEY (SeasonKey),
    CONSTRAINT UQ_DimSeason_ID UNIQUE (SeasonID),
    CONSTRAINT CK_DimSeason_Dates CHECK (EndDate > StartDate),
    CONSTRAINT CK_DimSeason_Status CHECK (
        SeasonStatus IN ('PLANNED', 'ACTIVE', 'COMPLETED')
    )
);

CREATE TABLE DimRankTier (
    RankTierKey      NUMBER(12)       NOT NULL,
    RankTierName     VARCHAR2(30)     NOT NULL,
    RankTierOrder    NUMBER(2)        NOT NULL,
    CONSTRAINT PK_DimRankTier PRIMARY KEY (RankTierKey),
    CONSTRAINT UQ_DimRankTier_Name UNIQUE (RankTierName),
    CONSTRAINT UQ_DimRankTier_Order UNIQUE (RankTierOrder),
    CONSTRAINT CK_DimRankTier_Order CHECK (RankTierOrder > 0)
);

/*
  Transaction fact grain: one row for one player's participation in one match.
*/
CREATE TABLE FactMatchParticipation (
    MatchParticipationFactKey  NUMBER(12)       NOT NULL,
    SourceMatchParticipationID NUMBER(12)       NOT NULL,
    MatchID                    NUMBER(12)       NOT NULL,
    PlayerKey                  NUMBER(12)       NOT NULL,
    RoleKey                    NUMBER(12)       NOT NULL,
    CharacterKey               NUMBER(12)       NOT NULL,
    SeasonKey                  NUMBER(12)       NOT NULL,
    DateKey                    NUMBER(10)       NOT NULL,
    TimeKey                    NUMBER(10)       NOT NULL,
    TeamAssignment             VARCHAR2(10)     NOT NULL,
    ParticipationCount         NUMBER(1)        DEFAULT 1 NOT NULL,
    WinFlag                    NUMBER(1)        NOT NULL,
    RatingChange               NUMBER(7,2)      DEFAULT 0 NOT NULL,
    SourceLoadBatchID          NUMBER(12)       NOT NULL,
    LoadedAt                   TIMESTAMP(0)     DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT PK_FactMatchParticipation
        PRIMARY KEY (MatchParticipationFactKey),
    CONSTRAINT UQ_FMP_Source UNIQUE (SourceMatchParticipationID),
    CONSTRAINT FK_FMP_Player FOREIGN KEY (PlayerKey)
        REFERENCES DimPlayer (PlayerKey),
    CONSTRAINT FK_FMP_Role FOREIGN KEY (RoleKey)
        REFERENCES DimRole (RoleKey),
    CONSTRAINT FK_FMP_Character FOREIGN KEY (CharacterKey)
        REFERENCES DimCharacter (CharacterKey),
    CONSTRAINT FK_FMP_Season FOREIGN KEY (SeasonKey)
        REFERENCES DimSeason (SeasonKey),
    CONSTRAINT FK_FMP_Date FOREIGN KEY (DateKey)
        REFERENCES DimDate (DateKey),
    CONSTRAINT FK_FMP_Time FOREIGN KEY (TimeKey)
        REFERENCES DimTime (TimeKey),
    CONSTRAINT CK_FMP_Team CHECK (TeamAssignment IN ('TEAM_A', 'TEAM_B')),
    CONSTRAINT CK_FMP_Count CHECK (ParticipationCount = 1),
    CONSTRAINT CK_FMP_Win CHECK (WinFlag IN (0, 1)),
    CONSTRAINT CK_FMP_RatingChange CHECK (
        RatingChange BETWEEN -100.00 AND 100.00
    ),
    CONSTRAINT CK_FMP_WinRating CHECK (
        (WinFlag = 1 AND RatingChange > 0)
        OR (WinFlag = 0 AND RatingChange <= 0)
    )
);

/*
  Periodic snapshot grain: one final/current snapshot for one player in one
  season. The separate grain prevents rating and rank values from being
  repeated for every match participation.
*/
CREATE TABLE FactPlayerSeasonSnapshot (
    PlayerSeasonSnapshotFactKey NUMBER(12)       NOT NULL,
    SourceSeasonRankingID       NUMBER(12)       NOT NULL,
    PlayerKey                   NUMBER(12)       NOT NULL,
    SeasonKey                   NUMBER(12)       NOT NULL,
    RankTierKey                 NUMBER(12)       NOT NULL,
    SnapshotDateKey             NUMBER(10)       NOT NULL,
    StartingRating              NUMBER(7,2)      NOT NULL,
    EndingRating                NUMBER(7,2)      NOT NULL,
    NetRatingChange             NUMBER(8,2)
        GENERATED ALWAYS AS (EndingRating - StartingRating) VIRTUAL,
    ParticipationCount          NUMBER(10)       DEFAULT 0 NOT NULL,
    WinCount                    NUMBER(10)       DEFAULT 0 NOT NULL,
    SourceLoadBatchID           NUMBER(12)       NOT NULL,
    LoadedAt                    TIMESTAMP(0)      DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT PK_FactPlayerSeasonSnapshot
        PRIMARY KEY (PlayerSeasonSnapshotFactKey),
    CONSTRAINT UQ_FPSS_Source UNIQUE (SourceSeasonRankingID),
    CONSTRAINT UQ_FPSS_Player_Season UNIQUE (PlayerKey, SeasonKey),
    CONSTRAINT FK_FPSS_Player FOREIGN KEY (PlayerKey)
        REFERENCES DimPlayer (PlayerKey),
    CONSTRAINT FK_FPSS_Season FOREIGN KEY (SeasonKey)
        REFERENCES DimSeason (SeasonKey),
    CONSTRAINT FK_FPSS_RankTier FOREIGN KEY (RankTierKey)
        REFERENCES DimRankTier (RankTierKey),
    CONSTRAINT FK_FPSS_Date FOREIGN KEY (SnapshotDateKey)
        REFERENCES DimDate (DateKey),
    CONSTRAINT CK_FPSS_StartRating CHECK (
        StartingRating BETWEEN 0.00 AND 5000.00
    ),
    CONSTRAINT CK_FPSS_EndRating CHECK (
        EndingRating BETWEEN 0.00 AND 5000.00
    ),
    CONSTRAINT CK_FPSS_Counts CHECK (
        ParticipationCount >= 0
        AND WinCount >= 0
        AND WinCount <= ParticipationCount
    )
);

CREATE SEQUENCE DimDate_Seq START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE DimTime_Seq START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE DimPlayer_Seq START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE DimRole_Seq START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE DimCharacter_Seq START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE DimSeason_Seq START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE DimRankTier_Seq START WITH 1000 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE FactMatchParticipation_Seq
    START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE FactPlayerSeasonSnapshot_Seq
    START WITH 1 INCREMENT BY 1 CACHE 1000;

CREATE INDEX IX_FMP_Date_Time
    ON FactMatchParticipation (DateKey, TimeKey);
CREATE INDEX IX_FMP_Season_Player
    ON FactMatchParticipation (SeasonKey, PlayerKey);
CREATE INDEX IX_FMP_Role_Character
    ON FactMatchParticipation (RoleKey, CharacterKey);
CREATE INDEX IX_FMP_Match
    ON FactMatchParticipation (MatchID);
CREATE INDEX IX_FPSS_Season_Tier
    ON FactPlayerSeasonSnapshot (SeasonKey, RankTierKey);

COMMENT ON COLUMN FactPlayerSeasonSnapshot.StartingRating IS
    'Non-additive player rating state; compare or average, never sum across players or time';

COMMENT ON COLUMN FactPlayerSeasonSnapshot.EndingRating IS
    'Non-additive player rating state; compare or average, never sum across players or time';

COMMENT ON COLUMN FactMatchParticipation.RatingChange IS
    'Additive rating flow; valid per-match domain is -100.00 through +100.00 inclusive';

/* Static rank-tier seed used by the original CS669 data vocabulary. */
INSERT INTO DimRankTier (RankTierKey, RankTierName, RankTierOrder)
VALUES (1, 'BRONZE', 1);
INSERT INTO DimRankTier (RankTierKey, RankTierName, RankTierOrder)
VALUES (2, 'SILVER', 2);
INSERT INTO DimRankTier (RankTierKey, RankTierName, RankTierOrder)
VALUES (3, 'GOLD', 3);
INSERT INTO DimRankTier (RankTierKey, RankTierName, RankTierOrder)
VALUES (4, 'PLATINUM', 4);
INSERT INTO DimRankTier (RankTierKey, RankTierName, RankTierOrder)
VALUES (5, 'DIAMOND', 5);
