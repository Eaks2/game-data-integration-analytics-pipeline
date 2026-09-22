/*
  CS779 Term Project - Phase 1
  Finalized Oracle staging, load-control, and rejection design

  Raw source fields remain VARCHAR2 so malformed values can be loaded first
  and rejected by validation instead of failing during file ingestion.
*/

CREATE TABLE LoadBatch (
    LoadBatchID       NUMBER(12)       NOT NULL,
    SourceSystem      VARCHAR2(50)     DEFAULT 'PYTHON_GENERATOR' NOT NULL,
    LoadStartTime     TIMESTAMP(0)     DEFAULT SYSTIMESTAMP NOT NULL,
    LoadEndTime       TIMESTAMP(0),
    RowsRead          NUMBER(12)       DEFAULT 0 NOT NULL,
    RowsAccepted      NUMBER(12)       DEFAULT 0 NOT NULL,
    RowsRejected      NUMBER(12)       DEFAULT 0 NOT NULL,
    LoadStatus        VARCHAR2(20)     DEFAULT 'STARTED' NOT NULL,
    CONSTRAINT PK_LoadBatch PRIMARY KEY (LoadBatchID),
    CONSTRAINT CK_LB_Status CHECK (
        LoadStatus IN ('STARTED', 'STAGED', 'VALIDATED', 'COMPLETED', 'FAILED')
    ),
    CONSTRAINT CK_LB_Counts CHECK (
        RowsRead >= 0
        AND RowsAccepted >= 0
        AND RowsRejected >= 0
        AND RowsAccepted + RowsRejected <= RowsRead
        AND (LoadStatus <> 'COMPLETED'
             OR RowsAccepted + RowsRejected = RowsRead)
    ),
    CONSTRAINT CK_LB_Times CHECK (
        LoadEndTime IS NULL OR LoadEndTime >= LoadStartTime
    )
);

CREATE TABLE StgPlayer (
    StgPlayerID       NUMBER(12)       NOT NULL,
    LoadBatchID       NUMBER(12)       NOT NULL,
    SourceFileName    VARCHAR2(255)    NOT NULL,
    SourceRowNumber   NUMBER(12)       NOT NULL,
    RawPlayerID       VARCHAR2(40),
    RawUsername       VARCHAR2(400),
    RawEmail          VARCHAR2(400),
    RawRegion         VARCHAR2(200),
    RawAccountLevel   VARCHAR2(100),
    RawAccountStatus  VARCHAR2(100),
    ProcessingStatus  VARCHAR2(20)     DEFAULT 'NEW' NOT NULL,
    TargetRecordID    NUMBER(12),
    CONSTRAINT PK_StgPlayer PRIMARY KEY (StgPlayerID),
    CONSTRAINT FK_SP_LoadBatch FOREIGN KEY (LoadBatchID)
        REFERENCES LoadBatch (LoadBatchID),
    CONSTRAINT UQ_SP_SourceRow UNIQUE (
        LoadBatchID, SourceFileName, SourceRowNumber
    ),
    CONSTRAINT CK_SP_RowNumber CHECK (SourceRowNumber > 0),
    CONSTRAINT CK_SP_Status CHECK (
        ProcessingStatus IN ('NEW', 'VALID', 'REJECTED', 'LOADED')
    ),
    CONSTRAINT CK_SP_Target CHECK (
        ProcessingStatus <> 'LOADED' OR TargetRecordID IS NOT NULL
    )
);

CREATE TABLE StgMatch (
    StgMatchID        NUMBER(12)       NOT NULL,
    LoadBatchID       NUMBER(12)       NOT NULL,
    SourceFileName    VARCHAR2(255)    NOT NULL,
    SourceRowNumber   NUMBER(12)       NOT NULL,
    RawMatchID        VARCHAR2(40),
    RawSeasonID       VARCHAR2(40),
    RawMatchStatus    VARCHAR2(100),
    RawMatchDateTime  VARCHAR2(100),
    ProcessingStatus  VARCHAR2(20)     DEFAULT 'NEW' NOT NULL,
    TargetRecordID    NUMBER(12),
    CONSTRAINT PK_StgMatch PRIMARY KEY (StgMatchID),
    CONSTRAINT FK_SM_LoadBatch FOREIGN KEY (LoadBatchID)
        REFERENCES LoadBatch (LoadBatchID),
    CONSTRAINT UQ_SM_SourceRow UNIQUE (
        LoadBatchID, SourceFileName, SourceRowNumber
    ),
    CONSTRAINT CK_SM_RowNumber CHECK (SourceRowNumber > 0),
    CONSTRAINT CK_SM_Status CHECK (
        ProcessingStatus IN ('NEW', 'VALID', 'REJECTED', 'LOADED')
    ),
    CONSTRAINT CK_SM_Target CHECK (
        ProcessingStatus <> 'LOADED' OR TargetRecordID IS NOT NULL
    )
);

CREATE TABLE StgParticipation (
    StgParticipationID  NUMBER(12)       NOT NULL,
    LoadBatchID         NUMBER(12)       NOT NULL,
    SourceFileName      VARCHAR2(255)    NOT NULL,
    SourceRowNumber     NUMBER(12)       NOT NULL,
    RawMatchID          VARCHAR2(40),
    RawPlayerID         VARCHAR2(40),
    RawRoleID           VARCHAR2(40),
    RawTeamAssignment   VARCHAR2(100),
    RawCharacterSelected VARCHAR2(200),
    RawPlayerResult     VARCHAR2(100),
    RawRatingChange     VARCHAR2(100),
    ProcessingStatus    VARCHAR2(20)     DEFAULT 'NEW' NOT NULL,
    TargetRecordID      NUMBER(12),
    CONSTRAINT PK_StgParticipation PRIMARY KEY (StgParticipationID),
    CONSTRAINT FK_SPart_LoadBatch FOREIGN KEY (LoadBatchID)
        REFERENCES LoadBatch (LoadBatchID),
    CONSTRAINT UQ_SPart_SourceRow UNIQUE (
        LoadBatchID, SourceFileName, SourceRowNumber
    ),
    CONSTRAINT CK_SPart_RowNumber CHECK (SourceRowNumber > 0),
    CONSTRAINT CK_SPart_Status CHECK (
        ProcessingStatus IN ('NEW', 'VALID', 'REJECTED', 'LOADED')
    ),
    CONSTRAINT CK_SPart_Target CHECK (
        ProcessingStatus <> 'LOADED' OR TargetRecordID IS NOT NULL
    )
);

CREATE TABLE StgSeasonRanking (
    StgSeasonRankingID NUMBER(12)       NOT NULL,
    LoadBatchID        NUMBER(12)       NOT NULL,
    SourceFileName     VARCHAR2(255)    NOT NULL,
    SourceRowNumber    NUMBER(12)       NOT NULL,
    RawPlayerID        VARCHAR2(40),
    RawSeasonID        VARCHAR2(40),
    RawCurrentRating   VARCHAR2(100),
    RawRankTier        VARCHAR2(100),
    ProcessingStatus   VARCHAR2(20)     DEFAULT 'NEW' NOT NULL,
    TargetRecordID     NUMBER(12),
    CONSTRAINT PK_StgSeasonRanking PRIMARY KEY (StgSeasonRankingID),
    CONSTRAINT FK_SSR_LoadBatch FOREIGN KEY (LoadBatchID)
        REFERENCES LoadBatch (LoadBatchID),
    CONSTRAINT UQ_SSR_SourceRow UNIQUE (
        LoadBatchID, SourceFileName, SourceRowNumber
    ),
    CONSTRAINT CK_SSR_RowNumber CHECK (SourceRowNumber > 0),
    CONSTRAINT CK_SSR_Status CHECK (
        ProcessingStatus IN ('NEW', 'VALID', 'REJECTED', 'LOADED')
    ),
    CONSTRAINT CK_SSR_Target CHECK (
        ProcessingStatus <> 'LOADED' OR TargetRecordID IS NOT NULL
    )
);

/*
  One staging row can have multiple error rows. SourceTable plus
  StagingRecordID is a logical polymorphic pointer and is intentionally not a
  physical foreign key to one staging table.
*/
CREATE TABLE RejectedRecord (
    RejectedRecordID  NUMBER(12)       NOT NULL,
    LoadBatchID       NUMBER(12)       NOT NULL,
    SourceFileName    VARCHAR2(255)    NOT NULL,
    SourceTable       VARCHAR2(30)     NOT NULL,
    SourceRowNumber   NUMBER(12)       NOT NULL,
    StagingRecordID   NUMBER(12)       NOT NULL,
    ErrorCode         VARCHAR2(50)     NOT NULL,
    ErrorFieldName    VARCHAR2(100),
    ErrorReason       VARCHAR2(1000)   NOT NULL,
    RawValue          VARCHAR2(4000),
    RejectedDateTime  TIMESTAMP(0)     DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT PK_RejectedRecord PRIMARY KEY (RejectedRecordID),
    CONSTRAINT FK_RR_LoadBatch FOREIGN KEY (LoadBatchID)
        REFERENCES LoadBatch (LoadBatchID),
    CONSTRAINT CK_RR_SourceTable CHECK (
        SourceTable IN (
            'STGPLAYER', 'STGMATCH', 'STGPARTICIPATION', 'STGSEASONRANKING'
        )
    ),
    CONSTRAINT CK_RR_RowNumber CHECK (SourceRowNumber > 0)
);

CREATE SEQUENCE LoadBatch_Seq
    START WITH 1 INCREMENT BY 1 CACHE 100;
CREATE SEQUENCE StgPlayer_Seq
    START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE StgMatch_Seq
    START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE StgParticipation_Seq
    START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE StgSeasonRanking_Seq
    START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE RejectedRecord_Seq
    START WITH 1 INCREMENT BY 1 CACHE 1000;

CREATE INDEX IX_SP_Batch_Status
    ON StgPlayer (LoadBatchID, ProcessingStatus);
CREATE INDEX IX_SM_Batch_Status
    ON StgMatch (LoadBatchID, ProcessingStatus);
CREATE INDEX IX_SPart_Batch_Status
    ON StgParticipation (LoadBatchID, ProcessingStatus);
CREATE INDEX IX_SSR_Batch_Status
    ON StgSeasonRanking (LoadBatchID, ProcessingStatus);
CREATE INDEX IX_RR_Batch_Source
    ON RejectedRecord (LoadBatchID, SourceTable, StagingRecordID);

/*
  Phase 2 validation domains for the raw text columns:
  - StgSeasonRanking.RawCurrentRating: numeric 0.00 through 5000.00 inclusive.
  - StgParticipation.RawRatingChange: numeric -100.00 through +100.00
    inclusive, with WIN > 0, LOSS < 0, and DRAW = 0.
  Raw columns intentionally have no numeric CHECK constraints because malformed
  text must reach staging and be logged in RejectedRecord.
*/
COMMENT ON COLUMN StgSeasonRanking.RawCurrentRating IS
    'Raw text; Phase 2 valid domain is numeric 0.00 through 5000.00 inclusive';

COMMENT ON COLUMN StgParticipation.RawRatingChange IS
    'Raw text; Phase 2 valid domain is numeric -100.00 through +100.00 inclusive';

COMMENT ON COLUMN StgParticipation.RawPlayerResult IS
    'Raw text; completed match requires opposing five-player WIN/LOSS teams or ten DRAW rows';

COMMENT ON COLUMN StgPlayer.RawPlayerID IS
    'Same PlayerID may recur in a later LoadBatch as a legitimate SCD2 update candidate';
