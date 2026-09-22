"""Shared configuration, Oracle, and JSON helpers for CS779 Phase 4."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=json_default)
        handle.write("\n")
    temporary.replace(path)


def connect(config: dict[str, Any]):
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt before using Oracle") from exc

    thick_dir = str(config.get("thick_mode_lib_dir", "")).strip()
    if thick_dir:
        oracledb.init_oracle_client(lib_dir=thick_dir)

    password_name = str(config.get("password_env", "CS779_ORACLE_PASSWORD"))
    password = os.environ.get(password_name)
    if not password:
        raise RuntimeError(
            f"Set environment variable {password_name} to the Oracle password"
        )
    return oracledb.connect(
        user=config["user"], password=password, dsn=config["dsn"]
    )


def scalar(connection: Any, sql: str, **binds: Any) -> Any:
    cursor = connection.cursor()
    cursor.execute(sql, binds)
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("Scalar query returned no row")
    return row[0]


def table_counts(connection: Any) -> dict[str, int]:
    tables = (
        "DimDate",
        "DimTime",
        "DimPlayer",
        "DimRole",
        "DimCharacter",
        "DimSeason",
        "DimRankTier",
        "FactMatchParticipation",
        "FactPlayerSeasonSnapshot",
    )
    return {
        table: int(scalar(connection, f"SELECT COUNT(*) FROM {table}"))
        for table in tables
    }
