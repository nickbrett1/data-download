"""Shared fixtures: a real app per test, on a temp-file store (CI is parallel)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data_download import coderef
from data_download.app import create_app
from data_download.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_TYPES_DIR = REPO_ROOT / "plan_types"

# What the app records as `code_ref` (§4.1). The real HEAD where there is a
# checkout (this dev container), and a placeholder where there is not (CI runs
# the suite from a ticket, with no `.git`). Both are claimable: with a checkout
# the sha resolves, without one the check can only answer "cannot tell".
CODE_REF = coderef.resolve(env={}) or "0" * 40

TOKENS = {
    "tok-submit": frozenset({"submit", "read"}),
    "tok-read": frozenset({"read"}),
    "tok-submit-only": frozenset({"submit"}),
    "tok-escalate": frozenset({"submit", "read", "escalate"}),
}

PLAN_BODY = {
    "kind": "plan",
    "plan_type": "market_data_window",
    "params": {
        # The plan type serves every seller, so the request names its own vendor.
        "vendor": "Databento",
        "dataset": "GLBX.MDP3",
        "schema": "trades",
        "symbols": ["ES.c.0"],
        "stype_in": "continuous",
        "start": "2026-10-01",
        "end": "2026-10-03",
    },
    "verify": {"on_failure": "quarantine"},
    # Phase 4: the ceiling, and the reason. A plan is priced at submit and
    # refused above its ceiling, so the number is not optional here either.
    "max_usd": 5,
    "why": "ES continuous, two days, to check the binder and the quote",
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


# The one login allowed to approve, and the header `tailscale serve` would have
# added for it.
APPROVERS = frozenset({"nick@example.com"})


def served(login: str = "nick@example.com") -> dict[str, str]:
    """The headers Serve puts on a request it proxied for `login`."""
    return {"Tailscale-User-Login": login}


# What the fake vendor answers (phase 4). A request must be priced before it can
# be approved, and `pytest` must never reach a vendor -- no client, no key, no
# network -- so every app in this suite gets a pricer that answers from here. A
# test that cares about the number passes its own `Settings` with its own.
QUOTE_USD = 0.42


def fake_quote(kwargs: dict) -> float:
    """A pricer with no client and no network, for tests that only need a number."""
    return QUOTE_USD


def iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "requests.db"


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return Settings(
        db_path=str(db_path),
        tokens=TOKENS,
        plan_types_dir=PLAN_TYPES_DIR,
        approvers=APPROVERS,
        code_ref=CODE_REF,
        quote=fake_quote,
    )


@pytest.fixture
def built_web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for the built bundle.

    The HTML routes are public and serve one file, so a test that asserts the
    shell is reachable needs a dist to serve -- a checkout has not necessarily
    run `npm run build` in web/, and CI has no node at all.
    """
    from data_download import app as app_module

    built = tmp_path / "web"
    (built / "assets").mkdir(parents=True)
    (built / "index.html").write_text("<html><body>built</body></html>")
    monkeypatch.setattr(app_module, "WEB_DIST_DIR", built)
    return built


@pytest.fixture
def client(settings: Settings):
    # The peer address is Serve's: `require_approver` only trusts the identity
    # header from loopback, so a suite that arrived from anywhere else could
    # never exercise the approve door -- and, more usefully, a test that forgets
    # to say who is calling gets a refusal rather than a silent pass.
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 51234)) as test_client:
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
        "plan_type": "market_data_window",
        "job_json": json.dumps({"kind": "plan"}, sort_keys=True),
        # A plan row's params name their own vendor now (the plan type is
        # generic), so a seeded row that is displayed as priced-and-attributed
        # carries one.
        "params_json": json.dumps(
            {"vendor": "Databento", "dataset": "GLBX.MDP3"}, sort_keys=True
        ),
        "intent_json": None,
        "estimate_usd": 1.0,
        "blocked_reason": None,
        "plan_origin": "adhoc",
        "matched_profile": None,
        "code_ref": None,
        "job_hash": None,
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
