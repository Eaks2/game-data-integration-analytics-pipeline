"""Data structures and shared constants for CS779 Phase 3."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class ValidationError:
    code: str
    field_name: str | None
    reason: str
    raw_value: str | None = None


@dataclass
class StageRow:
    entity: str
    source_table: str
    staging_id: int
    load_batch_id: int
    source_file_name: str
    source_row_number: int
    raw: dict[str, str | None]
    canonical: dict[str, Any] = field(default_factory=dict)
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def add_error(
        self,
        code: str,
        field_name: str | None,
        reason: str,
        raw_value: str | None = None,
    ) -> None:
        candidate = ValidationError(code, field_name, reason, raw_value)
        if candidate not in self.errors:
            self.errors.append(candidate)

    def raw_snapshot(self) -> str:
        return json.dumps(self.raw, sort_keys=True, default=str)[:4000]


@dataclass(frozen=True)
class SeasonRef:
    season_id: int
    name: str
    start_date: Any
    end_date: Any
    status: str


@dataclass(frozen=True)
class PlayerState:
    player_id: int
    username: str
    email: str
    region: str
    account_level: int
    account_status: str


@dataclass
class ValidationBundle:
    initial_players: list[StageRow]
    update_players: list[StageRow]
    matches: list[StageRow]
    participation: list[StageRow]
    rankings: list[StageRow]
    rating_start: dict[tuple[int, int], Decimal]
    rating_replay: dict[tuple[int, int], list[tuple[datetime, int, Decimal]]]

    def all_rows(self) -> list[StageRow]:
        return [
            *self.initial_players,
            *self.update_players,
            *self.matches,
            *self.participation,
            *self.rankings,
        ]

    def batch_rows(self, batch_id: int) -> list[StageRow]:
        return [row for row in self.all_rows() if row.load_batch_id == batch_id]


SOURCE_SPECS: dict[str, dict[str, Any]] = {
    "player.csv": {
        "entity": "player",
        "source_table": "STGPLAYER",
        "stage_table": "StgPlayer",
        "id_column": "StgPlayerID",
        "fields": {
            "PlayerID": "RawPlayerID",
            "Username": "RawUsername",
            "Email": "RawEmail",
            "Region": "RawRegion",
            "AccountLevel": "RawAccountLevel",
            "AccountStatus": "RawAccountStatus",
        },
    },
    "player_scd2_update.csv": {
        "entity": "player_update",
        "source_table": "STGPLAYER",
        "stage_table": "StgPlayer",
        "id_column": "StgPlayerID",
        "fields": {
            "PlayerID": "RawPlayerID",
            "Username": "RawUsername",
            "Email": "RawEmail",
            "Region": "RawRegion",
            "AccountLevel": "RawAccountLevel",
            "AccountStatus": "RawAccountStatus",
        },
    },
    "match.csv": {
        "entity": "match",
        "source_table": "STGMATCH",
        "stage_table": "StgMatch",
        "id_column": "StgMatchID",
        "fields": {
            "MatchID": "RawMatchID",
            "SeasonID": "RawSeasonID",
            "MatchStatus": "RawMatchStatus",
            "MatchDateTime": "RawMatchDateTime",
        },
    },
    "participation.csv": {
        "entity": "participation",
        "source_table": "STGPARTICIPATION",
        "stage_table": "StgParticipation",
        "id_column": "StgParticipationID",
        "fields": {
            "MatchID": "RawMatchID",
            "PlayerID": "RawPlayerID",
            "RoleID": "RawRoleID",
            "TeamAssignment": "RawTeamAssignment",
            "CharacterSelected": "RawCharacterSelected",
            "PlayerResult": "RawPlayerResult",
            "RatingChange": "RawRatingChange",
        },
    },
    "season_ranking.csv": {
        "entity": "ranking",
        "source_table": "STGSEASONRANKING",
        "stage_table": "StgSeasonRanking",
        "id_column": "StgSeasonRankingID",
        "fields": {
            "PlayerID": "RawPlayerID",
            "SeasonID": "RawSeasonID",
            "CurrentRating": "RawCurrentRating",
            "RankTier": "RawRankTier",
        },
    },
}


ENTITY_STAGE_SPEC = {
    spec["entity"]: spec for spec in SOURCE_SPECS.values()
}


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def count_by(items: Iterable[Any], key) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        label = str(key(item))
        result[label] = result.get(label, 0) + 1
    return dict(sorted(result.items()))
