"""The four views, exercised against directly seeded rows.

Three of `v_stuck`'s conditions cannot be reached through the API this phase -
nothing is ever approved, running or failed - so seeding is the only way to tell
a correct view from a broken one. Rows are written with plain `sqlite3` and the
views read back the same way, never through the app.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from conftest import auth, iso, seed

PLAN_JSON = json.dumps({"kind": "plan"}, sort_keys=True)


def read_view(db_path: Path, name: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {name}")]
    finally:
        conn.close()


def _seed_all(path: Path) -> None:
    seed(
        path,
        [
            # 1. approved, old, nobody claimed it
            {
                "id": "seed_approved",
                "state": "approved",
                "created_at": iso(-timedelta(hours=2)),
                "plan_json": PLAN_JSON,
                "estimate_usd": 1.5,
            },
            # 2. running, lease expired
            {
                "id": "seed_lease",
                "state": "running",
                "created_at": iso(-timedelta(hours=3)),
                "lease_until": iso(-timedelta(hours=1)),
                "plan_json": PLAN_JSON,
            },
            # 3. running, valid lease, no progress for 7h
            {
                "id": "seed_hung",
                "state": "running",
                "created_at": iso(-timedelta(hours=9)),
                "updated_at": iso(-timedelta(hours=7)),
                "lease_until": iso(timedelta(hours=1)),
                "plan_json": PLAN_JSON,
            },
            # 4. the same plan failing three times
            {
                "id": "seed_fail_1",
                "state": "failed",
                "created_at": iso(-timedelta(hours=5)),
                "plan_json": PLAN_JSON,
                "plan_hash": "hash_same_plan",
                "errors_json": json.dumps([{"code": "verify_contradiction"}]),
            },
            {
                "id": "seed_fail_2",
                "state": "failed",
                "created_at": iso(-timedelta(hours=4)),
                "plan_json": PLAN_JSON,
                "plan_hash": "hash_same_plan",
                "errors_json": json.dumps([{"code": "verify_contradiction"}]),
            },
            {
                "id": "seed_fail_3",
                "state": "failed",
                "created_at": iso(-timedelta(hours=3)),
                "plan_json": PLAN_JSON,
                "plan_hash": "hash_same_plan",
                "errors_json": json.dumps([{"code": "verify_contradiction"}]),
            },
            # one landed with errors, to prove v_done distinguishes it
            {
                "id": "seed_short",
                "state": "landed_with_errors",
                "plan_json": PLAN_JSON,
                "errors_json": json.dumps([{"code": "verify_shortfall"}]),
            },
            # a draft, for v_pending
            {"id": "seed_draft", "state": "draft", "plan_json": PLAN_JSON},
            # a blocked intent: not broken, so never in v_stuck
            {
                "id": "seed_blocked",
                "state": "blocked",
                "body_kind": "intent",
                "plan_type": None,
                "plan_json": None,
                "estimate_usd": None,
                "blocked_reason": "awaiting_plan",
                "plan_origin": None,
                "intent_json": json.dumps({"max_usd": 120}),
                "created_at": iso(-timedelta(hours=6)),
            },
        ],
    )


def test_v_stuck_is_exactly_the_four_breakage_conditions(client, db_path):
    _seed_all(db_path)
    rows = read_view(db_path, "v_stuck")
    assert {row["condition"] for row in rows} == {
        "approved_unclaimed",
        "lease_expired",
        "running_too_long",
        "repeat_failure",
    }


def test_v_stuck_excludes_blocked(client, db_path):
    _seed_all(db_path)
    stuck = read_view(db_path, "v_stuck")
    assert "seed_blocked" not in {row["request_id"] for row in stuck}


def test_v_blocked_is_separate_and_says_what_clears_it(client, db_path):
    _seed_all(db_path)
    blocked = read_view(db_path, "v_blocked")
    assert [row["id"] for row in blocked] == ["seed_blocked"]
    assert blocked[0]["blocked_reason"] == "awaiting_plan"
    assert blocked[0]["merge_can_clear"] == 1
    assert blocked[0]["max_usd"] == 120
    assert blocked[0]["age_seconds"] > 5 * 3600


def test_v_done_flags_landed_with_errors(client, db_path):
    _seed_all(db_path)
    by_id = {row["id"]: row for row in read_view(db_path, "v_done")}
    assert by_id["seed_short"]["had_errors"] == 1
    assert by_id["seed_short"]["landed_clean"] == 0
    assert by_id["seed_fail_1"]["had_errors"] == 0
    assert "seed_draft" not in by_id


def test_v_pending_holds_drafts_and_approved(client, db_path):
    _seed_all(db_path)
    rows = read_view(db_path, "v_pending")
    ids = {row["id"] for row in rows}
    assert ids == {"seed_draft", "seed_approved"}
    approved = next(row for row in rows if row["id"] == "seed_approved")
    assert approved["approved_unclaimed"] == 1


def test_repeat_failure_does_not_fire_at_two(client, db_path):
    seed(
        db_path,
        [
            {"id": "two_1", "state": "failed", "plan_json": PLAN_JSON, "plan_hash": "h2"},
            {"id": "two_2", "state": "failed", "plan_json": PLAN_JSON, "plan_hash": "h2"},
        ],
    )
    assert read_view(db_path, "v_stuck") == []


def test_dashboard_renders_four_views_with_seeded_rows(client, db_path):
    _seed_all(db_path)

    response = client.get("/?token=tok-read")
    assert response.status_code == 200
    html = response.text
    for heading in ("v_pending", "v_done", "v_stuck", "v_blocked"):
        assert heading in html
    for condition in ("approved_unclaimed", "lease_expired", "running_too_long", "repeat_failure"):
        assert condition in html
    assert "seed_blocked" in html


def test_dashboard_requires_read_scope(client):
    assert client.get("/").status_code == 401
    assert client.get("/?token=nonsense").status_code == 401
    assert client.get("/", headers=auth("tok-approve")).status_code == 403


def test_dashboard_is_get_only(client):
    assert client.post("/", headers=auth()).status_code == 405


def test_icons_and_manifest_are_publicly_accessible(client):
    """Browsers fetch icons without auth headers; these must return 200."""
    for path in (
        "/favicon.ico",
        "/apple-touch-icon.png",
        "/apple-touch-icon-precomposed.png",
        "/site.webmanifest",
        "/static/apple-touch-icon.png",
        "/static/favicon.svg",
        "/static/icon-192.png",
        "/static/icon-512.png",
    ):
        res = client.get(path)
        assert res.status_code == 200, f"{path} returned {res.status_code}"
        assert len(res.content) > 0


def test_dashboard_links_apple_touch_icon_and_manifest(client):
    res = client.get("/?token=tok-read")
    assert res.status_code == 200
    assert 'rel="apple-touch-icon"' in res.text
    assert 'apple-mobile-web-app-capable' in res.text
    assert 'apple-mobile-web-app-title' in res.text
    assert 'rel="manifest"' in res.text
