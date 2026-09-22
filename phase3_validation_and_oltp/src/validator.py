"""Deliberate Phase 3 staging validation independent of Oracle constraints."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from phase3_model import PlayerState, SeasonRef, StageRow, ValidationBundle


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ACCOUNT_STATUSES = {"ACTIVE", "SUSPENDED", "BANNED"}
MATCH_STATUSES = {"SCHEDULED", "IN_PROGRESS", "COMPLETED", "CANCELLED"}
TEAM_ASSIGNMENTS = {"TEAM_A", "TEAM_B"}
PLAYER_RESULTS = {"WIN", "LOSS", "DRAW"}
RANK_TIERS = {"BRONZE", "SILVER", "GOLD", "PLATINUM", "DIAMOND"}
MIN_RATING = Decimal("0.00")
MAX_RATING = Decimal("5000.00")
MIN_CHANGE = Decimal("-100.00")
MAX_CHANGE = Decimal("100.00")


def raw_text(row: StageRow, field: str) -> str | None:
    value = row.raw.get(field)
    return None if value is None else str(value)


def required_text(
    row: StageRow,
    field: str,
    required_code: str,
    label: str,
    *,
    maximum_length: int | None = None,
    length_code: str | None = None,
) -> str | None:
    raw = raw_text(row, field)
    if raw is None or not raw.strip():
        row.add_error(required_code, field, f"{label} is required.", raw)
        return None
    value = raw.strip()
    if maximum_length is not None and len(value) > maximum_length:
        row.add_error(
            length_code or required_code,
            field,
            f"{label} exceeds {maximum_length} characters.",
            raw,
        )
        return None
    return value


def required_integer(
    row: StageRow,
    field: str,
    required_code: str,
    type_code: str,
    label: str,
) -> int | None:
    raw = required_text(row, field, required_code, label)
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        row.add_error(type_code, field, f"{label} must be an integer.", raw)
        return None
    if not value.is_finite() or value != value.to_integral_value():
        row.add_error(type_code, field, f"{label} must be an integer.", raw)
        return None
    try:
        return int(value)
    except (OverflowError, ValueError):
        row.add_error(type_code, field, f"{label} is outside the supported integer range.", raw)
        return None


def required_decimal(
    row: StageRow,
    field: str,
    required_code: str,
    type_code: str,
    label: str,
) -> Decimal | None:
    raw = required_text(row, field, required_code, label)
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        row.add_error(type_code, field, f"{label} must be numeric.", raw)
        return None
    if not value.is_finite():
        row.add_error(type_code, field, f"{label} must be a finite numeric value.", raw)
        return None
    return value


def has_at_most_two_fractional_digits(value: Decimal) -> bool:
    """Return true when Oracle NUMBER(*,2) can store a value without rounding."""
    scaled = value * Decimal("100")
    return scaled == scaled.to_integral_value()


def duplicate_later_rows(
    rows: Iterable[StageRow],
    value_getter,
    error_code: str,
    field_name: str,
    reason: str,
) -> None:
    seen: set[Any] = set()
    for row in sorted(rows, key=lambda item: (item.source_file_name, item.source_row_number)):
        value = value_getter(row)
        if value is None:
            continue
        if value in seen:
            row.add_error(error_code, field_name, reason, raw_text(row, field_name))
        else:
            seen.add(value)


def validate_player_basics(rows: list[StageRow]) -> None:
    for row in rows:
        player_id = required_integer(
            row, "PlayerID", "PLY_ID_REQUIRED", "PLY_ID_TYPE", "PlayerID"
        )
        username = required_text(
            row,
            "Username",
            "PLY_USERNAME_REQUIRED",
            "Username",
            maximum_length=50,
            length_code="PLY_USERNAME_LENGTH",
        )
        email = required_text(
            row,
            "Email",
            "PLY_EMAIL_REQUIRED",
            "Email",
            maximum_length=255,
            length_code="PLY_EMAIL_LENGTH",
        )
        region = required_text(
            row,
            "Region",
            "PLY_REGION_REQUIRED",
            "Region",
            maximum_length=50,
            length_code="PLY_REGION_LENGTH",
        )
        level = required_integer(
            row,
            "AccountLevel",
            "PLY_LEVEL_REQUIRED",
            "PLY_LEVEL_TYPE",
            "AccountLevel",
        )
        status = required_text(
            row,
            "AccountStatus",
            "PLY_STATUS_REQUIRED",
            "AccountStatus",
            maximum_length=20,
            length_code="PLY_STATUS_INVALID",
        )
        if email is not None and not EMAIL_PATTERN.fullmatch(email):
            row.add_error(
                "PLY_EMAIL_FORMAT",
                "Email",
                "Email must contain one local part, one @, and a dotted domain.",
                raw_text(row, "Email"),
            )
        if level is not None and not 0 <= level <= 9999:
            row.add_error(
                "PLY_LEVEL_RANGE",
                "AccountLevel",
                "AccountLevel must be an integer from 0 through 9999.",
                raw_text(row, "AccountLevel"),
            )
        normalized_status = status.upper() if status is not None else None
        if normalized_status is not None and normalized_status not in ACCOUNT_STATUSES:
            row.add_error(
                "PLY_STATUS_INVALID",
                "AccountStatus",
                "AccountStatus must be ACTIVE, SUSPENDED, or BANNED.",
                raw_text(row, "AccountStatus"),
            )
        row.canonical.update(
            {
                "PlayerID": player_id,
                "Username": username,
                "Email": email,
                "Region": region,
                "AccountLevel": level,
                "AccountStatus": normalized_status,
            }
        )


def player_state(row: StageRow) -> PlayerState:
    return PlayerState(
        player_id=row.canonical["PlayerID"],
        username=row.canonical["Username"],
        email=row.canonical["Email"],
        region=row.canonical["Region"],
        account_level=row.canonical["AccountLevel"],
        account_status=row.canonical["AccountStatus"],
    )


def validate_initial_players(
    rows: list[StageRow], existing_players: dict[int, PlayerState]
) -> dict[int, PlayerState]:
    validate_player_basics(rows)
    duplicate_later_rows(
        rows,
        lambda row: row.canonical.get("PlayerID"),
        "PLY_ID_DUP",
        "PlayerID",
        "PlayerID duplicates an earlier player row in the same source load.",
    )
    duplicate_later_rows(
        rows,
        lambda row: row.canonical.get("Username", "").casefold()
        if row.canonical.get("Username")
        else None,
        "PLY_USERNAME_DUP",
        "Username",
        "Username duplicates an earlier value case-insensitively.",
    )
    duplicate_later_rows(
        rows,
        lambda row: row.canonical.get("Email", "").casefold()
        if row.canonical.get("Email")
        else None,
        "PLY_EMAIL_DUP",
        "Email",
        "Email duplicates an earlier value case-insensitively.",
    )

    existing_username = {
        state.username.casefold(): state.player_id for state in existing_players.values()
    }
    existing_email = {
        state.email.casefold(): state.player_id for state in existing_players.values()
    }
    for row in rows:
        player_id = row.canonical.get("PlayerID")
        username = row.canonical.get("Username")
        email = row.canonical.get("Email")
        if player_id in existing_players and row.valid:
            if player_state(row) != existing_players[player_id]:
                row.add_error(
                    "PLY_OLTP_CONFLICT",
                    "PlayerID",
                    "Existing OLTP PlayerID has attributes that differ from this initial source row.",
                    raw_text(row, "PlayerID"),
                )
        if username and username.casefold() in existing_username and existing_username[username.casefold()] != player_id:
            row.add_error(
                "PLY_USERNAME_OLTP_DUP",
                "Username",
                "Username is already owned by another OLTP player.",
                raw_text(row, "Username"),
            )
        if email and email.casefold() in existing_email and existing_email[email.casefold()] != player_id:
            row.add_error(
                "PLY_EMAIL_OLTP_DUP",
                "Email",
                "Email is already owned by another OLTP player.",
                raw_text(row, "Email"),
            )

    # Source foreign-key-style references must resolve to this source load,
    # not merely to an unrelated pre-existing OLTP row.
    accepted: dict[int, PlayerState] = {}
    for row in rows:
        if row.valid:
            accepted[row.canonical["PlayerID"]] = player_state(row)
    return accepted


def validate_update_players(
    rows: list[StageRow], accepted_players: dict[int, PlayerState]
) -> None:
    validate_player_basics(rows)
    duplicate_later_rows(
        rows,
        lambda row: row.canonical.get("PlayerID"),
        "PLY_UPDATE_DUP",
        "PlayerID",
        "PlayerID appears more than once in this later player batch.",
    )
    username_owners = {
        state.username.casefold(): player_id for player_id, state in accepted_players.items()
    }
    email_owners = {
        state.email.casefold(): player_id for player_id, state in accepted_players.items()
    }
    for row in rows:
        player_id = row.canonical.get("PlayerID")
        if player_id is None:
            continue
        prior = accepted_players.get(player_id)
        if prior is None:
            row.add_error(
                "PLY_UPDATE_REF",
                "PlayerID",
                "A later player version must reference an accepted existing PlayerID.",
                raw_text(row, "PlayerID"),
            )
            continue
        username = row.canonical.get("Username")
        email = row.canonical.get("Email")
        if username and username_owners.get(username.casefold(), player_id) != player_id:
            row.add_error(
                "PLY_UPDATE_USERNAME_DUP",
                "Username",
                "Updated Username belongs to another PlayerID.",
                raw_text(row, "Username"),
            )
        if email and email_owners.get(email.casefold(), player_id) != player_id:
            row.add_error(
                "PLY_UPDATE_EMAIL_DUP",
                "Email",
                "Updated Email belongs to another PlayerID.",
                raw_text(row, "Email"),
            )
        if row.valid:
            changed = any(
                row.canonical[field] != getattr(prior, attribute)
                for field, attribute in (
                    ("Username", "username"),
                    ("Region", "region"),
                    ("AccountLevel", "account_level"),
                    ("AccountStatus", "account_status"),
                )
            )
            if not changed:
                row.add_error(
                    "PLY_UPDATE_NO_CHANGE",
                    None,
                    "Later player row does not change any DimPlayer Type 2 tracked attribute.",
                    row.raw_snapshot(),
                )


def validate_matches(
    rows: list[StageRow], seasons: dict[int, SeasonRef]
) -> dict[int, StageRow]:
    for row in rows:
        match_id = required_integer(
            row, "MatchID", "MAT_ID_REQUIRED", "MAT_ID_TYPE", "MatchID"
        )
        season_id = required_integer(
            row, "SeasonID", "MAT_SEASON_REQUIRED", "MAT_SEASON_TYPE", "SeasonID"
        )
        status = required_text(
            row,
            "MatchStatus",
            "MAT_STATUS_REQUIRED",
            "MatchStatus",
            maximum_length=20,
            length_code="MAT_STATUS_INVALID",
        )
        timestamp_text = required_text(
            row,
            "MatchDateTime",
            "MAT_TIME_REQUIRED",
            "MatchDateTime",
            maximum_length=100,
            length_code="MAT_TIME_FORMAT",
        )
        normalized_status = status.upper() if status is not None else None
        if normalized_status is not None and normalized_status not in MATCH_STATUSES:
            row.add_error(
                "MAT_STATUS_INVALID",
                "MatchStatus",
                "MatchStatus must be SCHEDULED, IN_PROGRESS, COMPLETED, or CANCELLED.",
                raw_text(row, "MatchStatus"),
            )
        event_time = None
        if timestamp_text is not None:
            try:
                event_time = datetime.strptime(timestamp_text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                row.add_error(
                    "MAT_TIME_FORMAT",
                    "MatchDateTime",
                    "MatchDateTime must use YYYY-MM-DD HH24:MI:SS and be a real timestamp.",
                    raw_text(row, "MatchDateTime"),
                )
        season = seasons.get(season_id) if season_id is not None else None
        if season_id is not None and season is None:
            row.add_error(
                "MAT_SEASON_REF",
                "SeasonID",
                "SeasonID does not reference a controlled Season row.",
                raw_text(row, "SeasonID"),
            )
        if season is not None and event_time is not None:
            if not season.start_date <= event_time.date() <= season.end_date:
                row.add_error(
                    "MAT_TIME_WINDOW",
                    "MatchDateTime",
                    "MatchDateTime calendar date must fall within the referenced season, inclusively.",
                    raw_text(row, "MatchDateTime"),
                )
        row.canonical.update(
            {
                "MatchID": match_id,
                "SeasonID": season_id,
                "MatchStatus": normalized_status,
                "MatchDateTime": event_time,
            }
        )
    duplicate_later_rows(
        rows,
        lambda row: row.canonical.get("MatchID"),
        "MAT_ID_DUP",
        "MatchID",
        "MatchID duplicates an earlier match row in the same source load.",
    )
    return {
        row.canonical["MatchID"]: row
        for row in rows
        if row.valid and row.canonical.get("MatchID") is not None
    }


def validate_participation_basics(
    rows: list[StageRow],
    accepted_players: dict[int, PlayerState],
    matches_by_id: dict[int, StageRow],
    role_ids: set[int],
    characters_by_name: dict[str, int],
) -> None:
    for row in rows:
        match_id = required_integer(
            row, "MatchID", "PAR_MATCH_REQUIRED", "PAR_MATCH_TYPE", "MatchID"
        )
        player_id = required_integer(
            row, "PlayerID", "PAR_PLAYER_REQUIRED", "PAR_PLAYER_TYPE", "PlayerID"
        )
        role_id = required_integer(
            row, "RoleID", "PAR_ROLE_REQUIRED", "PAR_ROLE_TYPE", "RoleID"
        )
        character = required_text(
            row,
            "CharacterSelected",
            "PAR_CHARACTER_REQUIRED",
            "CharacterSelected",
            maximum_length=200,
            length_code="PAR_CHARACTER_REF",
        )
        team = required_text(
            row,
            "TeamAssignment",
            "PAR_TEAM_REQUIRED",
            "TeamAssignment",
            maximum_length=100,
            length_code="PAR_TEAM_INVALID",
        )
        result = required_text(
            row,
            "PlayerResult",
            "PAR_RESULT_REQUIRED",
            "PlayerResult",
            maximum_length=100,
            length_code="PAR_RESULT_INVALID",
        )
        delta = required_decimal(
            row,
            "RatingChange",
            "PAR_RATING_REQUIRED",
            "PAR_RATING_TYPE",
            "RatingChange",
        )
        normalized_character = character.upper() if character is not None else None
        normalized_team = team.upper() if team is not None else None
        normalized_result = result.upper() if result is not None else None

        if match_id is not None and match_id not in matches_by_id:
            row.add_error(
                "PAR_MATCH_REF",
                "MatchID",
                "MatchID does not reference an accepted source match.",
                raw_text(row, "MatchID"),
            )
        if player_id is not None and player_id not in accepted_players:
            row.add_error(
                "PAR_PLAYER_REF",
                "PlayerID",
                "PlayerID does not reference an accepted source player.",
                raw_text(row, "PlayerID"),
            )
        if role_id is not None and role_id not in role_ids:
            row.add_error(
                "PAR_ROLE_REF",
                "RoleID",
                "RoleID does not reference a controlled Role row.",
                raw_text(row, "RoleID"),
            )
        character_id = characters_by_name.get(normalized_character or "")
        if normalized_character is not None and character_id is None:
            row.add_error(
                "PAR_CHARACTER_REF",
                "CharacterSelected",
                "CharacterSelected does not map to a controlled Character row after trim/uppercase normalization.",
                raw_text(row, "CharacterSelected"),
            )
        if normalized_team is not None and normalized_team not in TEAM_ASSIGNMENTS:
            row.add_error(
                "PAR_TEAM_INVALID",
                "TeamAssignment",
                "TeamAssignment must be TEAM_A or TEAM_B.",
                raw_text(row, "TeamAssignment"),
            )
        if normalized_result is not None and normalized_result not in PLAYER_RESULTS:
            row.add_error(
                "PAR_RESULT_INVALID",
                "PlayerResult",
                "PlayerResult must be WIN, LOSS, or DRAW.",
                raw_text(row, "PlayerResult"),
            )
        if delta is not None and not MIN_CHANGE <= delta <= MAX_CHANGE:
            row.add_error(
                "PAR_RATING_RANGE",
                "RatingChange",
                "RatingChange must be from -100.00 through 100.00, inclusive.",
                raw_text(row, "RatingChange"),
            )
        if delta is not None and not has_at_most_two_fractional_digits(delta):
            row.add_error(
                "PAR_RATING_SCALE",
                "RatingChange",
                "RatingChange may have at most two fractional digits.",
                raw_text(row, "RatingChange"),
            )
        if normalized_result in PLAYER_RESULTS and delta is not None and MIN_CHANGE <= delta <= MAX_CHANGE:
            sign_valid = (
                (normalized_result == "WIN" and delta > 0)
                or (normalized_result == "LOSS" and delta < 0)
                or (normalized_result == "DRAW" and delta == 0)
            )
            if not sign_valid:
                row.add_error(
                    "PAR_RESULT_SIGN",
                    "RatingChange",
                    "WIN requires positive RatingChange, LOSS negative, and DRAW zero.",
                    raw_text(row, "RatingChange"),
                )
        match = matches_by_id.get(match_id) if match_id is not None else None
        row.canonical.update(
            {
                "MatchID": match_id,
                "PlayerID": player_id,
                "RoleID": role_id,
                "CharacterID": character_id,
                "CharacterSelected": normalized_character,
                "TeamAssignment": normalized_team,
                "PlayerResult": normalized_result,
                "RatingChange": delta,
                "SeasonID": match.canonical["SeasonID"] if match else None,
                "MatchDateTime": match.canonical["MatchDateTime"] if match else None,
            }
        )

    duplicate_later_rows(
        rows,
        lambda row: (
            row.canonical.get("MatchID"),
            row.canonical.get("PlayerID"),
        )
        if row.canonical.get("MatchID") is not None
        and row.canonical.get("PlayerID") is not None
        else None,
        "PAR_DUP",
        "PlayerID",
        "PlayerID already has an earlier participation row for this MatchID.",
    )


def validate_completed_groups(
    matches_by_id: dict[int, StageRow], participation: list[StageRow]
) -> None:
    grouped: dict[int, list[StageRow]] = defaultdict(list)
    for row in participation:
        if row.valid and row.canonical.get("MatchID") is not None:
            grouped[row.canonical["MatchID"]].append(row)

    for match_id, match in matches_by_id.items():
        if not match.valid or match.canonical.get("MatchStatus") != "COMPLETED":
            continue
        rows = grouped.get(match_id, [])
        team_a = sum(row.canonical.get("TeamAssignment") == "TEAM_A" for row in rows)
        team_b = sum(row.canonical.get("TeamAssignment") == "TEAM_B" for row in rows)
        unique_players = len({row.canonical.get("PlayerID") for row in rows})
        if len(rows) != 10 or team_a != 5 or team_b != 5 or unique_players != 10:
            match.add_error(
                "MAT_GROUP_INVALID",
                "MatchStatus",
                "COMPLETED match does not have ten valid unique participants with five per team.",
                match.raw_snapshot(),
            )
            for row in rows:
                row.add_error(
                    "PAR_COMPLETED_GROUP",
                    None,
                    "Participation belongs to a COMPLETED match with invalid 10-player/5v5 composition.",
                    row.raw_snapshot(),
                )
            continue

        counts = {
            (team, result): sum(
                row.canonical.get("TeamAssignment") == team
                and row.canonical.get("PlayerResult") == result
                for row in rows
            )
            for team in TEAM_ASSIGNMENTS
            for result in PLAYER_RESULTS
        }
        valid_win_loss = (
            counts[("TEAM_A", "WIN")] == 5
            and counts[("TEAM_B", "LOSS")] == 5
        ) or (
            counts[("TEAM_A", "LOSS")] == 5
            and counts[("TEAM_B", "WIN")] == 5
        )
        valid_draw = (
            counts[("TEAM_A", "DRAW")] == 5
            and counts[("TEAM_B", "DRAW")] == 5
        )
        if not valid_win_loss and not valid_draw:
            has_draw = any(row.canonical.get("PlayerResult") == "DRAW" for row in rows)
            part_code = "PAR_DRAW_MIX" if has_draw else "PAR_TEAM_RESULT"
            reason = (
                "COMPLETED match mixes DRAW with WIN/LOSS; DRAW requires all ten results."
                if has_draw
                else "COMPLETED match has contradictory results within or across its two teams."
            )
            match.add_error(
                "MAT_GROUP_INVALID",
                "MatchStatus",
                reason,
                match.raw_snapshot(),
            )
            for row in rows:
                row.add_error(part_code, "PlayerResult", reason, raw_text(row, "PlayerResult"))


def tier_for_rating(rating: Decimal) -> str:
    if rating < Decimal("1200.00"):
        return "BRONZE"
    if rating < Decimal("1600.00"):
        return "SILVER"
    if rating < Decimal("2000.00"):
        return "GOLD"
    if rating < Decimal("2400.00"):
        return "PLATINUM"
    return "DIAMOND"


def validate_rankings(
    rows: list[StageRow],
    accepted_players: dict[int, PlayerState],
    seasons: dict[int, SeasonRef],
    matches_by_id: dict[int, StageRow],
    participation: list[StageRow],
) -> tuple[
    dict[tuple[int, int], Decimal],
    dict[tuple[int, int], list[tuple[datetime, int, Decimal]]],
]:
    for row in rows:
        player_id = required_integer(
            row, "PlayerID", "RNK_PLAYER_REQUIRED", "RNK_PLAYER_TYPE", "PlayerID"
        )
        season_id = required_integer(
            row, "SeasonID", "RNK_SEASON_REQUIRED", "RNK_SEASON_TYPE", "SeasonID"
        )
        rating = required_decimal(
            row,
            "CurrentRating",
            "RNK_RATING_REQUIRED",
            "RNK_RATING_TYPE",
            "CurrentRating",
        )
        tier = required_text(
            row,
            "RankTier",
            "RNK_TIER_REQUIRED",
            "RankTier",
            maximum_length=30,
            length_code="RNK_TIER_INVALID",
        )
        normalized_tier = tier.upper() if tier is not None else None
        if player_id is not None and player_id not in accepted_players:
            row.add_error(
                "RNK_PLAYER_REF",
                "PlayerID",
                "PlayerID does not reference an accepted source player.",
                raw_text(row, "PlayerID"),
            )
        if season_id is not None and season_id not in seasons:
            row.add_error(
                "RNK_SEASON_REF",
                "SeasonID",
                "SeasonID does not reference a controlled Season row.",
                raw_text(row, "SeasonID"),
            )
        if rating is not None and not MIN_RATING <= rating <= MAX_RATING:
            row.add_error(
                "RNK_RATING_RANGE",
                "CurrentRating",
                "CurrentRating must be from 0.00 through 5000.00, inclusive.",
                raw_text(row, "CurrentRating"),
            )
        if rating is not None and not has_at_most_two_fractional_digits(rating):
            row.add_error(
                "RNK_RATING_SCALE",
                "CurrentRating",
                "CurrentRating may have at most two fractional digits.",
                raw_text(row, "CurrentRating"),
            )
        if normalized_tier is not None and normalized_tier not in RANK_TIERS:
            row.add_error(
                "RNK_TIER_INVALID",
                "RankTier",
                "RankTier must be BRONZE, SILVER, GOLD, PLATINUM, or DIAMOND.",
                raw_text(row, "RankTier"),
            )
        if rating is not None and MIN_RATING <= rating <= MAX_RATING and normalized_tier in RANK_TIERS:
            expected_tier = tier_for_rating(rating)
            if normalized_tier != expected_tier:
                row.add_error(
                    "RNK_TIER_MISMATCH",
                    "RankTier",
                    f"RankTier {normalized_tier} does not match rating band {expected_tier}.",
                    raw_text(row, "RankTier"),
                )
        row.canonical.update(
            {
                "PlayerID": player_id,
                "SeasonID": season_id,
                "CurrentRating": rating,
                "RankTier": normalized_tier,
            }
        )

    duplicate_later_rows(
        rows,
        lambda row: (
            row.canonical.get("PlayerID"),
            row.canonical.get("SeasonID"),
        )
        if row.canonical.get("PlayerID") is not None
        and row.canonical.get("SeasonID") is not None
        else None,
        "RNK_DUP",
        "PlayerID",
        "PlayerID/SeasonID duplicates an earlier season-ranking row.",
    )

    replay: dict[tuple[int, int], list[tuple[datetime, int, Decimal]]] = defaultdict(list)
    valid_match_ids = {match_id for match_id, match in matches_by_id.items() if match.valid}
    for row in participation:
        if not row.valid or row.canonical.get("MatchID") not in valid_match_ids:
            continue
        key = (row.canonical["PlayerID"], row.canonical["SeasonID"])
        replay[key].append(
            (
                row.canonical["MatchDateTime"],
                row.staging_id,
                row.canonical["RatingChange"],
            )
        )
    for events in replay.values():
        events.sort(key=lambda item: (item[0], item[1]))

    starts: dict[tuple[int, int], Decimal] = {}
    accepted_ranking_keys: set[tuple[int, int]] = set()
    for row in rows:
        if not row.valid:
            continue
        key = (row.canonical["PlayerID"], row.canonical["SeasonID"])
        final_rating = row.canonical["CurrentRating"]
        total_change = sum((event[2] for event in replay.get(key, [])), Decimal("0"))
        starting_rating = final_rating - total_change
        if not MIN_RATING <= starting_rating <= MAX_RATING:
            row.add_error(
                "RNK_START_RANGE",
                "CurrentRating",
                "Derived starting rating falls outside 0.00 through 5000.00.",
                raw_text(row, "CurrentRating"),
            )
            continue
        running = starting_rating
        for event_time, staging_id, delta in replay.get(key, []):
            running += delta
            if not MIN_RATING <= running <= MAX_RATING:
                row.add_error(
                    "RNK_REPLAY_RANGE",
                    "CurrentRating",
                    f"Rating replay leaves the valid domain at participation staging ID {staging_id}.",
                    raw_text(row, "CurrentRating"),
                )
                break
        if row.valid and running != final_rating:
            row.add_error(
                "RNK_RECONCILE",
                "CurrentRating",
                "Final rating does not reconcile with the ordered accepted RatingChange values.",
                raw_text(row, "CurrentRating"),
            )
        if row.valid:
            starts[key] = starting_rating
            accepted_ranking_keys.add(key)

    for row in participation:
        if row.valid:
            key = (row.canonical["PlayerID"], row.canonical["SeasonID"])
            if key not in accepted_ranking_keys:
                row.add_error(
                    "PAR_RANKING_MISSING",
                    None,
                    "Accepted rating event has no valid player-season ranking summary.",
                    row.raw_snapshot(),
                )
    return starts, dict(replay)


def validate_all(
    initial_players: list[StageRow],
    update_players: list[StageRow],
    matches: list[StageRow],
    participation: list[StageRow],
    rankings: list[StageRow],
    seasons: dict[int, SeasonRef],
    role_ids: set[int],
    characters_by_name: dict[str, int],
    existing_players: dict[int, PlayerState] | None = None,
) -> ValidationBundle:
    existing_players = existing_players or {}
    accepted_players = validate_initial_players(initial_players, existing_players)
    validate_update_players(update_players, accepted_players)
    matches_by_id = validate_matches(matches, seasons)
    validate_participation_basics(
        participation,
        accepted_players,
        matches_by_id,
        role_ids,
        characters_by_name,
    )
    validate_completed_groups(matches_by_id, participation)
    rating_start, rating_replay = validate_rankings(
        rankings,
        accepted_players,
        seasons,
        matches_by_id,
        participation,
    )
    return ValidationBundle(
        initial_players=initial_players,
        update_players=update_players,
        matches=matches,
        participation=participation,
        rankings=rankings,
        rating_start=rating_start,
        rating_replay=rating_replay,
    )
