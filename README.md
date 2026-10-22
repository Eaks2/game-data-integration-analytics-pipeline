# Game Data Integration and Analytics Pipeline

**Oracle SQL / PL/SQL · Python · ETL · Dimensional Modeling · Tableau**

An end-to-end data integration and analytics project developed for **MET CS779: Advanced Database Management** at Boston University (2026).

The project follows competitive multiplayer game data from generated CSV files into an Oracle staging area, validates and tracks every record, loads trusted data into a normalized operational database, and builds a dimensional warehouse for Tableau analysis. The focus is not simply producing dashboards: it is making the underlying data **traceable, consistent, and repeatable**.

## At a glance

| Verified outcome | Result |
| --- | ---: |
| Staged source records | **50,007** |
| Accepted records | **49,709** |
| Rejected records | **298** |
| Recorded rejection reasons | **307** |
| Match participation facts | **36,000** |
| Player-season snapshot facts | **7,500** |
| Additional player history versions (SCD Type 2) | **25** |
| Duplicate business keys / orphan foreign keys in the verified operational load | **0 / 0** |
| Warehouse row-count changes on unchanged-source rerun | **0** |

*These are results from the completed local Oracle verification, not benchmarks of a deployed production service.*

## Pipeline architecture

```text
Python synthetic-data generation
             |
             v
CSV source files (players, matches, participation, season rankings)
             |
             v
Oracle staging (raw values + batch/source-row lineage)
             |
             v
Validation and rejection handling
       |                  |
       | accepted         +--> RejectedRecord + rejection reasons
       v
Normalized Oracle operational (OLTP) tables
             |
             v
Dimensional ETL (dimensions, SCD Type 2 history, facts)
             |
             v
Analytics-ready warehouse --> Tableau dashboards
```

The stages have different purposes: staging preserves what arrived; validation establishes which rows can be trusted; normalized tables enforce operational relationships; and the warehouse structures that trusted data for historical reporting.

## What I built

### 1. Reproducible source generation and staging

- Generated controlled, synthetic competitive-game records in CSV files, including intentional data-quality edge cases.
- Loaded source rows into Oracle staging with batch identity and source-file/row lineage so rejected data remained traceable.
- Kept incoming values separate from cleaned operational records rather than silently discarding invalid rows.

### 2. Data validation and operational database

- Checked required values, ranges, business keys, foreign-key references, match participation rules, and relationships between match results and rating changes.
- Preserved failed rows and their explanations for auditing and troubleshooting: **298 rejected records** with **307 recorded rejection reasons**.
- Loaded accepted data into normalized relational tables with database constraints and consistency checks.
- Verified **zero orphan foreign keys**, **zero duplicate business keys**, and **zero completed-match 5v5 rule violations** in the final operational load.

### 3. Dimensional warehouse and historical tracking

- Built a dimensional analytical layer with two fact grains:
  - **`FactMatchParticipation`** — one row per accepted match participation, for outcome and rating-change analysis.
  - **`FactPlayerSeasonSnapshot`** — one row per player-season snapshot, for season-level analysis.
- Added conformed dimensions for dates, times, players, seasons, roles, characters, and rank tiers.
- Used **Slowly Changing Dimension Type 2 (SCD2)** in `DimPlayer` to preserve previous player attributes after a later valid update. The 25 updated players each receive an additional historical dimension version.
- Resolved player dimension versions against event time so earlier facts retain their original historical context.

### 4. Reconciliation and rerun verification

- Reconciled **36,000** match participation facts and **7,500** player-season snapshot facts with their operational sources.
- Verified key warehouse participation measures against OLTP: **17,230 wins** and **−1,077 total rating change**, with exact agreement.
- Re-ran dimensional ETL against the same unchanged source state; confirmed **zero row-count changes** and **zero final verification failures**.

### 5. Tableau analytics

Prepared validated warehouse data for Tableau dashboards exploring competitive-game participation, match outcomes, rating changes, and player-season performance.

See the repository's [`Results`](./Results/) folder for project outputs and visual evidence.

## Repository structure

```text
game-data-integration-analytics-pipeline/
├── Design/                         # Database and pipeline design materials
├── Results/                        # Verification and analytics outputs
├── phase2_generation_and_staging/   # Synthetic sources and staging work
├── phase3_validation_and_oltp/      # Validation, rejection handling, OLTP loading
├── phase4_dimensional_warehouse/    # Dimensional ETL and warehouse verification
├── LICENSE
└── README.md
```

The directory names above match the published repository. Refer to the scripts and documentation inside each phase folder for the specific files and dependencies.

## Exploring or reproducing the project

**Environment:** Oracle Database with a SQL client such as DBeaver; Python for source generation and processing; Tableau for the analytical outputs. The project was validated in a local Oracle schema (`CS779PROJ`).

1. Start in [`Design`](./Design/) to review the database relationships, business rules, and intended ETL flow.
2. Review [`phase2_generation_and_staging`](./phase2_generation_and_staging/) for the generated source data and raw staging steps.
3. Review [`phase3_validation_and_oltp`](./phase3_validation_and_oltp/) for validation, rejected-record tracking, and normalized operational loading.
4. Review [`phase4_dimensional_warehouse`](./phase4_dimensional_warehouse/) for dimension/fact loading, SCD2, reconciliation, and idempotency verification.
5. Inspect [`Results`](./Results/) for saved outputs and Tableau-related artifacts.

**Note:** This repository represents a phased academic implementation, not a one-command hosted application. Run SQL and data-load scripts in their documented dependency order and use a separate Oracle environment for reproduction. The verified counts above came from the completed project run; they should not be treated as the results of a fresh execution on another machine.

## Key lessons

- A successful load is not enough: **accepted and rejected records should both be accounted for**.
- A dimensional model needs explicit **fact grains and historical version rules** to support trustworthy reporting.
- **Reconciliation** checks correctness against the operational source; **idempotency** checks repeatability. Both matter.
- Dashboards become more dependable when data quality is resolved upstream rather than in the visualization layer.

## Development note

This academic project involved AI-assisted coding and iteration (sometimes called *vibe coding*). The submitted pipeline was checked through data-quality verification, operational constraints, warehouse reconciliation, and rerun testing; the results reported here are from those completed checks.

---

**Project:** Boston University MET CS779 — Advanced Database Management · 2026  
**Data:** Synthetic competitive multiplayer game records; no real player records are represented.
