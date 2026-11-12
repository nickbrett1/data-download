"""Shared fixtures: a real app per test, on a temp-file store (CI is parallel)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data_download.app import create_app
from data_download.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_TYPES_DIR = REPO_ROOT / "plan_types"

TOKENS = {
    "tok-submit": frozenset({"submit", "read"}),
    "tok-read": frozenset({"read"}),
    "tok-approve": frozenset({"approve"}),
}

PLAN_BODY = {
    "kind": "plan",
    "plan_type": "databento_batch_download",
    "params": {
        "dataset": "GLBX.MDP3",
        "schema": "trades",
        "symbols": ["ES.c.0"],
        "stype_in": "continuous",
        "start": "2026-10-01",
        "end": "2026-10-03",
    },
    "verify": {"on_failure": "quarantine"},
}

INTENT_BODY = {
    "kind": "intent",
    "intent": {
        "dataset": "OPRA.PILLAR",
        "schema": "trades",
        "window": {"start": "2026-10-01", "end": "2026-10-08"},
    },
    "max_usd": 120,
    "why": "OPRA.PILLAR trades for the Oct expiry cycle",
}


def auth(token: str = "tok-submit") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "requests.db"


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return Settings(db_path=str(db_path), tokens=TOKENS, plan_types_dir=PLAN_TYPES_DIR)


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def seed(db_path: Path, rows: list[dict]) -> None:
    """Insert rows straight into the store, bypassing the API.

    The dashboard's unreachable-in-phase-1 conditions can only be exercised
    this way; going through the API would make a broken view and a correct one
    look identical.
    """
    base = {
        "state": "draft",
        "body_kind": "plan",
        "plan_type": "databento_batch_download",
        "plan_json": json.dumps({"kind": "plan"}, sort_keys=True),
        "params_json": "{}",
        "intent_json": None,
        "estimate_usd": 1.0,
        "blocked_reason": None,
        "plan_origin": "adhoc",
        "matched_profile": None,
        "code_ref": None,
        "plan_hash": None,
        "lease_until": None,
        "landed_sha256": None,
        "errors_json": None,
        "created_at": iso(),
        "updated_at": iso(),
    }
    now = iso()
    prepared = []
    for i, row in enumerate(rows):
        merged = {**base, **row}
        merged.setdefault("id", f"seed_{i}")
        merged.setdefault("payload_hash", f"seedhash_{i}")
        merged["created_at"] = merged.get("created_at") or now
        merged["updated_at"] = merged.get("updated_at") or now
        prepared.append(merged)

    keys = sorted({k for row in prepared for k in row})
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            f"INSERT INTO requests ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
            [[row.get(k) for k in keys] for row in prepared],
        )
        conn.commit()
    finally:
        conn.close()
