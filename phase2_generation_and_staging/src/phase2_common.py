"""Shared Phase 2 file definitions and small utility functions."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


SOURCE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "player.csv": {
        "table": "StgPlayer",
        "sequence": "StgPlayer_Seq",
        "id_column": "StgPlayerID",
        "headers": [
            "PlayerID",
            "Username",
            "Email",
            "Region",
            "AccountLevel",
            "AccountStatus",
        ],
        "raw_columns": [
            "RawPlayerID",
            "RawUsername",
            "RawEmail",
            "RawRegion",
            "RawAccountLevel",
            "RawAccountStatus",
        ],
    },
    "player_scd2_update.csv": {
        "table": "StgPlayer",
        "sequence": "StgPlayer_Seq",
        "id_column": "StgPlayerID",
        "headers": [
            "PlayerID",
            "Username",
            "Email",
            "Region",
            "AccountLevel",
            "AccountStatus",
        ],
        "raw_columns": [
            "RawPlayerID",
            "RawUsername",
            "RawEmail",
            "RawRegion",
            "RawAccountLevel",
            "RawAccountStatus",
        ],
    },
    "match.csv": {
        "table": "StgMatch",
        "sequence": "StgMatch_Seq",
        "id_column": "StgMatchID",
        "headers": ["MatchID", "SeasonID", "MatchStatus", "MatchDateTime"],
        "raw_columns": [
            "RawMatchID",
            "RawSeasonID",
            "RawMatchStatus",
            "RawMatchDateTime",
        ],
    },
    "participation.csv": {
        "table": "StgParticipation",
        "sequence": "StgParticipation_Seq",
        "id_column": "StgParticipationID",
        "headers": [
            "MatchID",
            "PlayerID",
            "RoleID",
            "TeamAssignment",
            "CharacterSelected",
            "PlayerResult",
            "RatingChange",
        ],
        "raw_columns": [
            "RawMatchID",
            "RawPlayerID",
            "RawRoleID",
            "RawTeamAssignment",
            "RawCharacterSelected",
            "RawPlayerResult",
            "RawRatingChange",
        ],
    },
    "season_ranking.csv": {
        "table": "StgSeasonRanking",
        "sequence": "StgSeasonRanking_Seq",
        "id_column": "StgSeasonRankingID",
        "headers": ["PlayerID", "SeasonID", "CurrentRating", "RankTier"],
        "raw_columns": [
            "RawPlayerID",
            "RawSeasonID",
            "RawCurrentRating",
            "RawRankTier",
        ],
    },
}

INITIAL_FILES = ["player.csv", "match.csv", "participation.csv", "season_ranking.csv"]
SCD2_FILES = ["player_scd2_update.csv"]


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(path: Path, headers: list[str], rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=headers,
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in headers})
            count += 1
    return count


def read_csv(path: Path, expected_headers: list[str]) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != expected_headers:
            raise ValueError(
                f"{path.name}: expected headers {expected_headers}, got {reader.fieldnames}"
            )
        return list(reader)


def oracle_text(value: str) -> str | None:
    """Oracle stores an empty VARCHAR2 value as NULL; all other text is unchanged."""
    return None if value == "" else value
