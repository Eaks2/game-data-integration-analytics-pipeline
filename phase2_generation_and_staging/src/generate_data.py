#!/usr/bin/env python3
"""Generate reproducible clean game data, then inject controlled source errors."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from phase2_common import INITIAL_FILES, SOURCE_DEFINITIONS, sha256_file, write_csv, write_json


REGIONS = ["NA", "EU", "APAC", "LATAM"]
PLAYER_HEADERS = SOURCE_DEFINITIONS["player.csv"]["headers"]
MATCH_HEADERS = SOURCE_DEFINITIONS["match.csv"]["headers"]
PARTICIPATION_HEADERS = SOURCE_DEFINITIONS["participation.csv"]["headers"]
RANKING_HEADERS = SOURCE_DEFINITIONS["season_ranking.csv"]["headers"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "generator_config.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "generated",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def tier_for_rating(rating: int) -> str:
    if rating < 1200:
        return "BRONZE"
    if rating < 1600:
        return "SILVER"
    if rating < 2000:
        return "GOLD"
    if rating < 2400:
        return "PLATINUM"
    return "DIAMOND"


def random_match_datetime(
    rng: random.Random, start: date, end: date, hour_weights: list[int]
) -> datetime:
    day = start + timedelta(days=rng.randint(0, (end - start).days))
    hour = rng.choices(range(24), weights=hour_weights, k=1)[0]
    return datetime.combine(
        day,
        time(hour=hour, minute=rng.randint(0, 59), second=rng.randint(0, 59)),
    )


def clean_identifier(file_name: str, row: dict[str, str]) -> str:
    if file_name.startswith("player"):
        return f"PlayerID={row.get('PlayerID', '')}"
    if file_name == "match.csv":
        return f"MatchID={row.get('MatchID', '')}"
    if file_name == "participation.csv":
        return f"MatchID={row.get('MatchID', '')};PlayerID={row.get('PlayerID', '')}"
    return f"PlayerID={row.get('PlayerID', '')};SeasonID={row.get('SeasonID', '')}"


def attach_issue(
    row: dict[str, str],
    error_type: str,
    fields: list[str],
    expected: str,
    multi_error: bool = False,
) -> None:
    row["__issue"] = {
        "error_type": error_type,
        "fields": fields,
        "expected": expected,
        "multi_error": multi_error,
    }


def verify_clean_dataset(
    config: dict[str, Any],
    players: list[dict[str, str]],
    matches: list[dict[str, str]],
    participation: list[dict[str, str]],
    rankings: list[dict[str, str]],
    starting_ratings: dict[tuple[int, int], int],
) -> list[str]:
    """Fail before injection unless every clean baseline rule is true."""
    domains = config["domains"]
    ranked_players = config["scale"]["ranked_players"]
    seasons = {int(item["season_id"]): item for item in config["seasons"]}
    role_ids = {str(item["role_id"]) for item in config["roles"]}
    character_names = {item["character_name"] for item in config["characters"]}
    player_ids = [int(row["PlayerID"]) for row in players]
    player_id_set = set(player_ids)

    if len(player_ids) != len(set(player_ids)):
        raise ValueError("Clean PlayerID values are not unique")
    if len({row["Username"] for row in players}) != len(players):
        raise ValueError("Clean Username values are not unique")
    if len({row["Email"] for row in players}) != len(players):
        raise ValueError("Clean Email values are not unique")
    for row in players:
        if not all(row[field] for field in PLAYER_HEADERS):
            raise ValueError("Clean player contains a missing required source field")
        if row["Email"].count("@") != 1 or "." not in row["Email"].split("@", 1)[1]:
            raise ValueError("Clean player contains a malformed email")
        if not domains["account_level_min"] <= int(row["AccountLevel"]) <= domains["account_level_max"]:
            raise ValueError("Clean AccountLevel is outside its domain")
        if row["AccountStatus"] not in domains["account_statuses"]:
            raise ValueError("Clean AccountStatus is outside its controlled vocabulary")

    match_by_id: dict[int, dict[str, str]] = {}
    for row in matches:
        match_id = int(row["MatchID"])
        season_id = int(row["SeasonID"])
        if match_id in match_by_id or season_id not in seasons:
            raise ValueError("Clean match key or season reference is invalid")
        if row["MatchStatus"] not in domains["match_statuses"]:
            raise ValueError("Clean MatchStatus is outside its controlled vocabulary")
        event_time = datetime.fromisoformat(row["MatchDateTime"])
        season = seasons[season_id]
        if not (date.fromisoformat(season["start_date"]) <= event_time.date() <= date.fromisoformat(season["end_date"])):
            raise ValueError(f"Clean match {match_id} is outside its season")
        match_by_id[match_id] = row

    grouped: dict[int, list[dict[str, str]]] = defaultdict(list)
    calculated = dict(starting_ratings)
    for row in participation:
        match_id = int(row["MatchID"])
        player_id = int(row["PlayerID"])
        if match_id not in match_by_id or player_id not in player_id_set:
            raise ValueError("Clean participation has a missing source reference")
        if row["RoleID"] not in role_ids or row["CharacterSelected"] not in character_names:
            raise ValueError("Clean participation has invalid controlled reference data")
        delta = int(row["RatingChange"])
        if not domains["rating_change_min"] <= delta <= domains["rating_change_max"]:
            raise ValueError("Clean RatingChange is outside its domain")
        season_id = int(match_by_id[match_id]["SeasonID"])
        calculated[(season_id, player_id)] += delta
        if not domains["season_current_rating_min"] <= calculated[(season_id, player_id)] <= domains["season_current_rating_max"]:
            raise ValueError("Clean replay produced a rating outside its domain")
        grouped[match_id].append(row)

    for match_id, match in match_by_id.items():
        if match["MatchStatus"] != "COMPLETED":
            continue
        rows = grouped[match_id]
        if len(rows) != 10 or len({row["PlayerID"] for row in rows}) != 10:
            raise ValueError(f"Completed match {match_id} is not ten unique players")
        if Counter(row["TeamAssignment"] for row in rows) != {"TEAM_A": 5, "TEAM_B": 5}:
            raise ValueError(f"Completed match {match_id} does not have 5v5 teams")
        team_results = {
            team: {row["PlayerResult"] for row in rows if row["TeamAssignment"] == team}
            for team in ("TEAM_A", "TEAM_B")
        }
        if team_results == {"TEAM_A": {"DRAW"}, "TEAM_B": {"DRAW"}}:
            if any(int(row["RatingChange"]) != 0 for row in rows):
                raise ValueError(f"Draw match {match_id} has a nonzero rating change")
        elif set(map(frozenset, team_results.values())) != {frozenset({"WIN"}), frozenset({"LOSS"})}:
            raise ValueError(f"Completed match {match_id} has contradictory team results")
        for row in rows:
            delta = int(row["RatingChange"])
            expected_sign = {"WIN": delta > 0, "LOSS": delta < 0, "DRAW": delta == 0}
            if not expected_sign[row["PlayerResult"]]:
                raise ValueError(f"Result/rating sign mismatch in clean match {match_id}")

    ranking_keys: set[tuple[int, int]] = set()
    for row in rankings:
        key = (int(row["SeasonID"]), int(row["PlayerID"]))
        if key in ranking_keys:
            raise ValueError("Clean season ranking contains a duplicate player-season")
        ranking_keys.add(key)
        rating = int(row["CurrentRating"])
        if not domains["season_current_rating_min"] <= rating <= domains["season_current_rating_max"]:
            raise ValueError(f"Clean season ranking {key} is outside the rating domain")
        if rating != calculated[key] or row["RankTier"] != tier_for_rating(rating):
            raise ValueError(f"Season ranking {key} does not reconcile with match changes")
    expected_keys = {(season_id, player_id) for season_id in seasons for player_id in range(1, ranked_players + 1)}
    if ranking_keys != expected_keys:
        raise ValueError("Clean season-ranking coverage is incomplete")

    return [
        "required player fields, source formats, domains, and unique business identifiers",
        "valid season windows and controlled match statuses",
        "exactly ten unique players and 5v5 teams per completed match",
        "consistent WIN/LOSS or all-DRAW completed-match outcomes",
        "valid role and character prerequisites",
        "rating changes within -100..100 and replayed ratings within 0..5000",
        "season-ranking totals reconciled to the clean match-time replay",
    ]


def build_clean_data(config: dict[str, Any], rng: random.Random) -> tuple[
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, str]],
    dict[tuple[int, int], int],
    list[int],
]:
    scale = config["scale"]
    ranked_count = int(scale["ranked_players"])
    fixture_count = int(scale["ranking_error_fixture_players"])
    total_players = ranked_count + fixture_count
    region_weights = {
        "NA": [38, 24, 20, 18],
        "EU": [22, 42, 22, 14],
        "APAC": [20, 21, 45, 14],
    }

    players: list[dict[str, str]] = []
    for player_id in range(1, total_players + 1):
        players.append(
            {
                "PlayerID": str(player_id),
                "Username": f"player_{player_id:05d}",
                "Email": f"player_{player_id:05d}@game.example",
                "Region": rng.choices(
                    REGIONS,
                    weights=region_weights[REGIONS[((player_id - 1) // 700) % 3]],
                    k=1,
                )[0],
                "AccountLevel": str(rng.randint(1, 300)),
                "AccountStatus": rng.choices(
                    ["ACTIVE", "SUSPENDED", "BANNED"], weights=[94, 4, 2], k=1
                )[0],
            }
        )

    roles = config["roles"]
    characters = config["characters"]
    matches: list[dict[str, str]] = []
    participation: list[dict[str, str]] = []
    starting_ratings: dict[tuple[int, int], int] = {}
    current_ratings: dict[tuple[int, int], int] = {}
    scheduled_ids: list[int] = []
    hour_weights = [1, 1, 1, 1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 8, 9, 10, 12, 14, 18, 22, 25, 24, 18, 10]

    for season in config["seasons"]:
        season_id = int(season["season_id"])
        start = date.fromisoformat(season["start_date"])
        end = date.fromisoformat(season["end_date"])
        for player_id in range(1, ranked_count + 1):
            rating = rng.randint(1100, 2300)
            starting_ratings[(season_id, player_id)] = rating
            current_ratings[(season_id, player_id)] = rating

        match_times = sorted(
            random_match_datetime(rng, start, end, hour_weights)
            for _ in range(int(scale["completed_matches_per_season"]))
        )
        for sequence, event_time in enumerate(match_times, start=1):
            match_id = season_id * 100_000 + sequence
            matches.append(
                {
                    "MatchID": str(match_id),
                    "SeasonID": str(season_id),
                    "MatchDateTime": event_time.isoformat(sep=" ", timespec="seconds"),
                    "MatchStatus": "COMPLETED",
                }
            )
            selected = rng.sample(range(1, ranked_count + 1), 10)
            is_draw = rng.random() < float(scale["draw_probability"])
            winner = rng.choice(["TEAM_A", "TEAM_B"]) if not is_draw else ""
            for position, player_id in enumerate(selected):
                team = "TEAM_A" if position < 5 else "TEAM_B"
                role = rng.choices(roles, weights=[18, 32, 24, 16, 10], k=1)[0]
                character = rng.choices(characters, weights=[22, 20, 26, 17, 15], k=1)[0]
                if is_draw:
                    result, delta = "DRAW", 0
                else:
                    result = "WIN" if team == winner else "LOSS"
                    magnitude = rng.randint(6, 30)
                    delta = magnitude if result == "WIN" else -magnitude
                key = (season_id, player_id)
                current_ratings[key] += delta
                participation.append(
                    {
                        "MatchID": str(match_id),
                        "PlayerID": str(player_id),
                        "RoleID": str(role["role_id"]),
                        "TeamAssignment": team,
                        "CharacterSelected": character["character_name"],
                        "PlayerResult": result,
                        "RatingChange": str(delta),
                    }
                )

        for fixture_number in range(1, int(scale["scheduled_error_fixture_matches_per_season"]) + 1):
            match_id = season_id * 100_000 + 90_000 + fixture_number
            scheduled_ids.append(match_id)
            fixture_day = start + timedelta(days=min((end - start).days, 7 + fixture_number))
            event_time = datetime.combine(fixture_day, time(hour=10 + fixture_number))
            matches.append(
                {
                    "MatchID": str(match_id),
                    "SeasonID": str(season_id),
                    "MatchDateTime": event_time.isoformat(sep=" ", timespec="seconds"),
                    "MatchStatus": "SCHEDULED",
                }
            )

    rankings = [
        {
            "PlayerID": str(player_id),
            "SeasonID": str(season_id),
            "CurrentRating": str(current_ratings[(season_id, player_id)]),
            "RankTier": tier_for_rating(current_ratings[(season_id, player_id)]),
        }
        for season_id in sorted(int(item["season_id"]) for item in config["seasons"])
        for player_id in range(1, ranked_count + 1)
    ]
    return players, matches, participation, rankings, starting_ratings, scheduled_ids


def inject_errors(
    config: dict[str, Any],
    rng: random.Random,
    data: dict[str, list[dict[str, str]]],
    scheduled_ids: list[int],
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []

    def record(
        file_name: str,
        row: dict[str, str],
        error_type: str,
        fields: list[str],
        expected: str,
        multi_error: bool = False,
    ) -> None:
        attach_issue(row, error_type, fields, expected, multi_error)
        pending.append(
            {
                "source_file": file_name,
                "row_ref": row,
                "error_type": error_type,
                "affected_fields": fields,
                "expected_phase3_reason": expected,
                "multi_error": multi_error,
            }
        )

    players = data["player.csv"]
    next_player = 700_000

    def fresh_player() -> dict[str, str]:
        nonlocal next_player
        next_player += 1
        return {
            "PlayerID": str(next_player),
            "Username": f"fixture_{next_player}",
            "Email": f"fixture_{next_player}@game.example",
            "Region": "NA",
            "AccountLevel": "50",
            "AccountStatus": "ACTIVE",
        }

    for _ in range(3):
        row = fresh_player(); row["PlayerID"] = ""; players.append(row)
        record("player.csv", row, "MISSING_PLAYER_ID", ["PlayerID"], "required PlayerID is missing")
    for index in range(3):
        row = fresh_player(); row["PlayerID"] = players[index]["PlayerID"]; players.append(row)
        record("player.csv", row, "DUPLICATE_PLAYER_ID", ["PlayerID"], "PlayerID duplicates another row in the same load")
    for index in range(2):
        row = fresh_player(); row["Username"] = players[10 + index]["Username"]; players.append(row)
        record("player.csv", row, "DUPLICATE_USERNAME", ["Username"], "Username duplicates an existing business value")
    for index in range(2):
        row = fresh_player(); row["Email"] = players[20 + index]["Email"]; players.append(row)
        record("player.csv", row, "DUPLICATE_EMAIL", ["Email"], "Email duplicates an existing business value")
    for field, error_type in (("Username", "MISSING_USERNAME"), ("Email", "MISSING_EMAIL"), ("Region", "MISSING_REGION")):
        for _ in range(2):
            row = fresh_player(); row[field] = ""; players.append(row)
            record("player.csv", row, error_type, [field], f"required {field} is missing")
    for value in ("not-an-email", "missing-at.example", "@no-local-part", "two@@ats.example"):
        row = fresh_player(); row["Email"] = value; players.append(row)
        record("player.csv", row, "MALFORMED_EMAIL", ["Email"], "Email does not have the required source format")
    for value in ("level-fifty", "10.5", "1O0", "--7"):
        row = fresh_player(); row["AccountLevel"] = value; players.append(row)
        record("player.csv", row, "MALFORMED_ACCOUNT_LEVEL", ["AccountLevel"], "AccountLevel is not an integer")
    for _ in range(2):
        row = fresh_player(); row["AccountLevel"] = "-1"; players.append(row)
        record("player.csv", row, "ACCOUNT_LEVEL_OUT_OF_RANGE", ["AccountLevel"], "AccountLevel is outside 0..9999")
    for value in ("DELETED", "PAUSED", "ACTIVE?", "UNKNOWN"):
        row = fresh_player(); row["AccountStatus"] = value; players.append(row)
        record("player.csv", row, "INVALID_ACCOUNT_STATUS", ["AccountStatus"], "AccountStatus is outside the controlled vocabulary")
    for _ in range(2):
        row = fresh_player(); row["Email"] = "bad"; row["AccountLevel"] = "NaN"; players.append(row)
        record("player.csv", row, "MULTI_ERROR_PLAYER", ["Email", "AccountLevel"], "malformed Email and AccountLevel", True)

    matches = data["match.csv"]
    next_match = 800_000
    season_one = config["seasons"][0]

    def fresh_match() -> dict[str, str]:
        nonlocal next_match
        next_match += 1
        return {
            "MatchID": str(next_match),
            "SeasonID": "1",
            "MatchDateTime": f"{season_one['start_date']} 12:00:00",
            "MatchStatus": "SCHEDULED",
        }

    for _ in range(3):
        row = fresh_match(); row["MatchID"] = ""; matches.append(row)
        record("match.csv", row, "MISSING_MATCH_ID", ["MatchID"], "required MatchID is missing")
    completed_matches = [row for row in matches if row["MatchStatus"] == "COMPLETED"]
    for index in range(3):
        row = copy.deepcopy(completed_matches[index]); matches.append(row)
        record("match.csv", row, "DUPLICATE_MATCH_ID", ["MatchID"], "MatchID duplicates another row in the same load")
    for _ in range(2):
        row = fresh_match(); row["SeasonID"] = ""; matches.append(row)
        record("match.csv", row, "MISSING_SEASON_ID", ["SeasonID"], "required SeasonID is missing")
    for value in ("9999", "-5", "NO_SEASON", "4"):
        row = fresh_match(); row["SeasonID"] = value; matches.append(row)
        record("match.csv", row, "NONEXISTENT_OR_MALFORMED_SEASON", ["SeasonID"], "SeasonID is malformed or does not reference a controlled season")
    for value in ("not-a-timestamp", "2024-13-40 25:61:00", "03/99/2024", "2024-02-30T10:00", "yesterday"):
        row = fresh_match(); row["MatchDateTime"] = value; matches.append(row)
        record("match.csv", row, "MALFORMED_MATCH_TIMESTAMP", ["MatchDateTime"], "MatchDateTime cannot be parsed with the Phase 1 format")
    for value in ("2023-12-31 23:59:59", "2025-01-01 00:00:00", "2026-06-15 12:00:00", "2022-01-01 08:00:00", "2030-01-01 00:00:00"):
        row = fresh_match(); row["MatchDateTime"] = value; matches.append(row)
        record("match.csv", row, "MATCH_DATE_OUTSIDE_SEASON", ["MatchDateTime"], "MatchDateTime falls outside the referenced season")
    for value in ("FINISHED", "complete", "ABANDONED", "UNKNOWN"):
        row = fresh_match(); row["MatchStatus"] = value; matches.append(row)
        record("match.csv", row, "INVALID_MATCH_STATUS", ["MatchStatus"], "MatchStatus is outside the controlled vocabulary")
    for _ in range(2):
        row = fresh_match(); row["MatchDateTime"] = ""; matches.append(row)
        record("match.csv", row, "MISSING_MATCH_TIMESTAMP", ["MatchDateTime"], "required MatchDateTime is missing")
    for _ in range(2):
        row = fresh_match(); row["SeasonID"] = "NO_SEASON"; row["MatchDateTime"] = "not-a-date"; matches.append(row)
        record("match.csv", row, "MULTI_ERROR_MATCH", ["SeasonID", "MatchDateTime"], "malformed SeasonID and MatchDateTime", True)

    parts = data["participation.csv"]
    fixture_index = 0

    def fresh_part() -> dict[str, str]:
        nonlocal fixture_index
        match_id = scheduled_ids[fixture_index % len(scheduled_ids)]
        player_id = 300 + fixture_index
        role_id = (fixture_index % 5) + 1
        character = {1: "ATLAS", 2: "SERAPH", 3: "PYRO", 4: "VEX", 5: "NYX"}[role_id]
        fixture_index += 1
        return {
            "MatchID": str(match_id),
            "PlayerID": str(player_id),
            "RoleID": str(role_id),
            "TeamAssignment": "TEAM_A",
            "CharacterSelected": character,
            "PlayerResult": "WIN",
            "RatingChange": "10",
        }

    def append_part_errors(count: int, error_type: str, field: str, values: list[str], expected: str) -> None:
        for index in range(count):
            row = fresh_part(); row[field] = values[index % len(values)]; parts.append(row)
            record("participation.csv", row, error_type, [field], expected)

    append_part_errors(8, "MISSING_MATCH_ID", "MatchID", [""], "required MatchID is missing")
    append_part_errors(8, "MISSING_PLAYER_ID", "PlayerID", [""], "required PlayerID is missing")
    for index in range(8):
        row = copy.deepcopy(parts[index]); parts.append(row)
        record("participation.csv", row, "DUPLICATE_PARTICIPATION", ["MatchID", "PlayerID"], "player participates more than once in the same match")
    append_part_errors(10, "NONEXISTENT_PLAYER", "PlayerID", [str(990_000 + i) for i in range(10)], "PlayerID does not reference a source player")
    append_part_errors(10, "NONEXISTENT_MATCH", "MatchID", [str(990_100 + i) for i in range(10)], "MatchID does not reference a source match")
    append_part_errors(10, "NONEXISTENT_ROLE", "RoleID", ["999", "0", "ROLE_X"], "RoleID is malformed or does not reference controlled Role data")
    append_part_errors(12, "INVALID_CHARACTER_NAME", "CharacterSelected", ["UNKNOWN_HERO", "ATLAS??", "SERAPH_V2"], "CharacterSelected does not reference controlled Character data")
    append_part_errors(12, "INCONSISTENT_CHARACTER_NAME", "CharacterSelected", ["AT LAS", "PYR0", "N-Y-X"], "CharacterSelected has no controlled mapping after permitted case/space normalization")
    append_part_errors(10, "INVALID_TEAM_ASSIGNMENT", "TeamAssignment", ["TEAM_C", "A", "RED"], "TeamAssignment is outside TEAM_A/TEAM_B")
    append_part_errors(10, "INVALID_PLAYER_RESULT", "PlayerResult", ["VICTORY", "DEFEAT", "TIE"], "PlayerResult is outside WIN/LOSS/DRAW")
    append_part_errors(10, "MALFORMED_RATING_CHANGE", "RatingChange", ["NOT_A_NUMBER", "12..5", "+-8"], "RatingChange is not numeric")
    append_part_errors(8, "RATING_CHANGE_ABOVE_MAX", "RatingChange", ["101", "250"], "RatingChange exceeds 100")
    append_part_errors(8, "RATING_CHANGE_BELOW_MIN", "RatingChange", ["-101", "-250"], "RatingChange is below -100")
    for _ in range(6):
        row = fresh_part(); row["PlayerResult"] = "WIN"; row["RatingChange"] = "-12"; parts.append(row)
        record("participation.csv", row, "RESULT_RATING_SIGN_MISMATCH", ["PlayerResult", "RatingChange"], "WIN must have a positive RatingChange")
    for _ in range(2):
        row = fresh_part(); row["RoleID"] = "BAD_ROLE"; row["RatingChange"] = "NaN"; parts.append(row)
        record("participation.csv", row, "MULTI_ERROR_PARTICIPATION", ["RoleID", "RatingChange"], "malformed RoleID and RatingChange", True)

    # Four isolated completed-match groups exercise cross-row result consistency.
    # They are injected after clean ranking reconciliation, and every one of the
    # 40 context rows is identified in the manifest as part of the invalid group.
    for group_index in range(4):
        match_id = 880_001 + group_index
        match_row = {
            "MatchID": str(match_id),
            "SeasonID": "1",
            "MatchDateTime": "2024-04-15 18:00:00",
            "MatchStatus": "COMPLETED",
        }
        matches.append(match_row)
        mixed_draw = group_index >= 2
        for position in range(10):
            team = "TEAM_A" if position < 5 else "TEAM_B"
            if mixed_draw:
                if position == 0:
                    result, delta = "DRAW", "0"
                elif team == "TEAM_A":
                    result, delta = "WIN", "10"
                else:
                    result, delta = "LOSS", "-10"
                error_type = "MIXED_DRAW_RESULT"
                expected = "completed match mixes DRAW with WIN/LOSS instead of using ten DRAW rows"
            else:
                if position == 0:
                    result, delta = "LOSS", "-10"
                elif team == "TEAM_A":
                    result, delta = "WIN", "10"
                else:
                    result, delta = "LOSS", "-10"
                error_type = "CONTRADICTORY_TEAM_RESULT"
                expected = "completed match has contradictory results within TEAM_A"
            role_id = (position % 5) + 1
            row = {
                "MatchID": str(match_id),
                "PlayerID": str(2_000 + (group_index * 20) + position),
                "RoleID": str(role_id),
                "TeamAssignment": team,
                "CharacterSelected": {1: "ATLAS", 2: "SERAPH", 3: "PYRO", 4: "VEX", 5: "NYX"}[role_id],
                "PlayerResult": result,
                "RatingChange": delta,
            }
            parts.append(row)
            record(
                "participation.csv",
                row,
                error_type,
                ["PlayerResult", "RatingChange"],
                expected,
            )
        attach_issue(
            match_row,
            error_type,
            ["MatchStatus"],
            expected,
        )

    rankings = data["season_ranking.csv"]
    fixture_player = int(config["scale"]["ranked_players"]) + 1

    def fresh_ranking() -> dict[str, str]:
        nonlocal fixture_player
        row = {
            "PlayerID": str(fixture_player),
            "SeasonID": str(((fixture_player - 1) % 3) + 1),
            "CurrentRating": "1500",
            "RankTier": "SILVER",
        }
        fixture_player += 1
        if fixture_player > int(config["scale"]["ranked_players"]) + int(config["scale"]["ranking_error_fixture_players"]):
            fixture_player = int(config["scale"]["ranked_players"]) + 1
        return row

    def append_rank_errors(count: int, error_type: str, field: str, values: list[str], expected: str) -> None:
        for index in range(count):
            row = fresh_ranking(); row[field] = values[index % len(values)]; rankings.append(row)
            record("season_ranking.csv", row, error_type, [field], expected)

    append_rank_errors(5, "MISSING_PLAYER_ID", "PlayerID", [""], "required PlayerID is missing")
    append_rank_errors(5, "MISSING_SEASON_ID", "SeasonID", [""], "required SeasonID is missing")
    for index in range(6):
        row = copy.deepcopy(rankings[index]); rankings.append(row)
        record("season_ranking.csv", row, "DUPLICATE_PLAYER_SEASON", ["PlayerID", "SeasonID"], "PlayerID/SeasonID duplicates another row in the load")
    append_rank_errors(6, "NONEXISTENT_PLAYER", "PlayerID", [str(995_000 + i) for i in range(6)], "PlayerID does not reference a source player")
    append_rank_errors(6, "NONEXISTENT_SEASON", "SeasonID", ["999", "0", "SEASON_X"], "SeasonID is malformed or does not reference a controlled season")
    append_rank_errors(10, "MALFORMED_CURRENT_RATING", "CurrentRating", ["NOT_A_NUMBER", "1500..5", "15OO"], "CurrentRating is not numeric")
    append_rank_errors(6, "CURRENT_RATING_BELOW_MIN", "CurrentRating", ["-1", "-100"], "CurrentRating is below 0")
    append_rank_errors(6, "CURRENT_RATING_ABOVE_MAX", "CurrentRating", ["5001", "9000"], "CurrentRating exceeds 5000")
    append_rank_errors(8, "INVALID_RANK_TIER", "RankTier", ["MASTER", "WOOD", "G0LD", ""], "RankTier is outside the controlled vocabulary or missing")
    for _ in range(2):
        row = fresh_ranking(); row["CurrentRating"] = "NaN"; row["RankTier"] = "MASTER"; rankings.append(row)
        record("season_ranking.csv", row, "MULTI_ERROR_RANKING", ["CurrentRating", "RankTier"], "malformed CurrentRating and invalid RankTier", True)

    row_numbers = {
        file_name: {id(row): index for index, row in enumerate(rows, start=1)}
        for file_name, rows in data.items()
    }
    errors: list[dict[str, Any]] = []
    for item in pending:
        row = item.pop("row_ref")
        file_name = item["source_file"]
        fields = item["affected_fields"]
        errors.append(
            {
                "source_file": file_name,
                "source_row_number": row_numbers[file_name][id(row)],
                "generated_identifier": clean_identifier(file_name, row),
                "error_type": item["error_type"],
                "affected_fields": "|".join(fields),
                "field_values_json": json.dumps({field: row.get(field, "") for field in fields}, sort_keys=True),
                "expected_phase3_reason": item["expected_phase3_reason"],
                "multi_error": "Y" if item["multi_error"] else "N",
            }
        )
    return errors


def build_scd2_batch(
    config: dict[str, Any], players: list[dict[str, str]]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    clean_by_id = {int(row["PlayerID"]): row for row in players[: int(config["scale"]["ranked_players"])]}
    regions = REGIONS
    updates: list[dict[str, str]] = []
    documentation: list[dict[str, str]] = []
    count = int(config["scale"]["scd2_player_updates"])
    selected = [101 + 73 * index for index in range(count)]
    for player_id in selected:
        old = clean_by_id[player_id]
        new = copy.deepcopy(old)
        new["Region"] = regions[(regions.index(old["Region"]) + 1) % len(regions)]
        new["AccountLevel"] = str(min(999, int(old["AccountLevel"]) + 7))
        new["AccountStatus"] = "SUSPENDED" if old["AccountStatus"] == "ACTIVE" else "ACTIVE"
        updates.append(new)
        documentation.append(
            {
                "PlayerID": new["PlayerID"],
                "Username": new["Username"],
                "OldRegion": old["Region"],
                "NewRegion": new["Region"],
                "OldAccountLevel": old["AccountLevel"],
                "NewAccountLevel": new["AccountLevel"],
                "OldAccountStatus": old["AccountStatus"],
                "NewAccountStatus": new["AccountStatus"],
                "ExpectedLaterBehavior": "Expire current DimPlayer row and insert a new current Type 2 row; do not reject as a duplicate.",
            }
        )
    return updates, documentation


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(int(config["seed"]))

    players, matches, parts, rankings, starting_ratings, scheduled_ids = build_clean_data(config, rng)
    clean_counts = {
        "player.csv": len(players),
        "match.csv": len(matches),
        "participation.csv": len(parts),
        "season_ranking.csv": len(rankings),
    }
    clean_checks = verify_clean_dataset(
        config, players, matches, parts, rankings, starting_ratings
    )
    data = {
        "player.csv": players,
        "match.csv": matches,
        "participation.csv": parts,
        "season_ranking.csv": rankings,
    }
    errors = inject_errors(config, rng, data, scheduled_ids)
    scd2_rows, scd2_documentation = build_scd2_batch(config, players)

    row_expectations = {
        file_name: [
            {
                "source_row_number": row_number,
                **(
                    {
                        "expected_disposition": "REJECT",
                        "error_type": row["__issue"]["error_type"],
                        "expected_phase3_reason": row["__issue"]["expected"],
                    }
                    if "__issue" in row
                    else {
                        "expected_disposition": "ACCEPT",
                        "error_type": "",
                        "expected_phase3_reason": "",
                    }
                ),
            }
            for row_number, row in enumerate(rows, start=1)
        ]
        for file_name, rows in data.items()
    }

    row_counts: dict[str, int] = {}
    for file_name in INITIAL_FILES:
        row_counts[file_name] = write_csv(
            output_dir / file_name,
            SOURCE_DEFINITIONS[file_name]["headers"],
            data[file_name],
        )
    row_counts["player_scd2_update.csv"] = write_csv(
        output_dir / "player_scd2_update.csv", PLAYER_HEADERS, scd2_rows
    )

    error_headers = [
        "source_file",
        "source_row_number",
        "generated_identifier",
        "error_type",
        "affected_fields",
        "field_values_json",
        "expected_phase3_reason",
        "multi_error",
    ]
    write_csv(output_dir / "error_manifest.csv", error_headers, errors)
    expectation_headers = [
        "source_row_number",
        "expected_disposition",
        "error_type",
        "expected_phase3_reason",
    ]
    expectation_metadata: dict[str, Any] = {}
    for file_name, expectations in row_expectations.items():
        expectation_name = f"{Path(file_name).stem}_row_expectations.csv"
        write_csv(output_dir / expectation_name, expectation_headers, expectations)
        expectation_metadata[file_name] = {
            "file": expectation_name,
            "row_count": len(expectations),
            "expected_accept_rows": sum(row["expected_disposition"] == "ACCEPT" for row in expectations),
            "expected_reject_rows": sum(row["expected_disposition"] == "REJECT" for row in expectations),
            "sha256": sha256_file(output_dir / expectation_name),
        }
    scd2_headers = list(scd2_documentation[0])
    write_csv(output_dir / "scd2_update_manifest.csv", scd2_headers, scd2_documentation)
    write_json(
        output_dir / "reference_prerequisites.json",
        {
            "decision": "Season, Role, and Character remain controlled reference prerequisites. The Phase 2 raw loader does not create or alter them.",
            "seasons": config["seasons"],
            "roles": config["roles"],
            "characters": config["characters"],
        },
    )

    config_digest = sha256_file(args.config)
    generator_digest = sha256_file(Path(__file__).resolve())
    common_digest = sha256_file(Path(__file__).resolve().with_name("phase2_common.py"))
    generation_id = hashlib.sha256(
        f"{config['seed']}:{config_digest}:{generator_digest}:{common_digest}".encode("utf-8")
    ).hexdigest()[:16]
    initial_run_key = f"{config['output']['initial_run_key_prefix']}_{generation_id}"
    scd2_run_key = f"{config['output']['scd2_run_key_prefix']}_{generation_id}"
    dirty_rows_by_file = Counter(
        {
            file_name: sum(row["expected_disposition"] == "REJECT" for row in expectations)
            for file_name, expectations in row_expectations.items()
        }
    )
    error_case_counts_by_file = Counter(item["source_file"] for item in errors)
    error_types = Counter(item["error_type"] for item in errors)
    files = {}
    for file_name, row_count in row_counts.items():
        files[file_name] = {
            "row_count": row_count,
            "sha256": sha256_file(output_dir / file_name),
            "injected_error_rows": dirty_rows_by_file.get(file_name, 0),
            "rows_without_injected_error": row_count - dirty_rows_by_file.get(file_name, 0),
        }
    manifest = {
        "schema_version": "2.0",
        "generation_id": generation_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": config["seed"],
        "config_file": args.config.name,
        "config_sha256": config_digest,
        "generator_sha256": generator_digest,
        "phase2_common_sha256": common_digest,
        "scale_rationale": (
            "2,560 valid initial players (2,500 ranked plus 60 isolated ranking-error fixtures), "
            "3,600 completed matches, 36,000 clean participation rows, and 7,500 clean "
            "player-season rankings, plus a small number of isolated dirty-data context rows, "
            "provide roughly 50,000 staged rows: enough for bulk-loading and later analytics "
            "while remaining comfortable on a graduate-project laptop."
        ),
        "domains": config["domains"],
        "reference_prerequisites": "reference_prerequisites.json",
        "clean_baseline": {
            "status": "PASSED before error injection",
            "row_counts": clean_counts,
            "checks": clean_checks,
        },
        "batches": {
            "initial": {
                "run_key": initial_run_key,
                "files": INITIAL_FILES,
                "expected_rows": sum(row_counts[name] for name in INITIAL_FILES),
            },
            "scd2": {
                "run_key": scd2_run_key,
                "files": ["player_scd2_update.csv"],
                "expected_rows": row_counts["player_scd2_update.csv"],
                "valid_update_rows": True,
            },
        },
        "files": files,
        "error_manifest": {
            "file": "error_manifest.csv",
            "error_case_count": len(errors),
            "multi_error_case_count": sum(item["multi_error"] == "Y" for item in errors),
            "counts_by_source_file": dict(sorted(error_case_counts_by_file.items())),
            "expected_reject_rows_by_source_file": dict(sorted(dirty_rows_by_file.items())),
            "counts_by_error_type": dict(sorted(error_types.items())),
            "sha256": sha256_file(output_dir / "error_manifest.csv"),
        },
        "row_expectation_manifests": expectation_metadata,
        "scd2_update_manifest": {
            "file": "scd2_update_manifest.csv",
            "row_count": len(scd2_documentation),
            "sha256": sha256_file(output_dir / "scd2_update_manifest.csv"),
        },
    }
    write_json(output_dir / "generation_manifest.json", manifest)
    print(json.dumps({"status": "PASS", "output_dir": str(output_dir), "row_counts": row_counts, "error_cases": len(errors)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
