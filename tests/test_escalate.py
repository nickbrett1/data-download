"""The right to ask for a new plan (§phase 2.5).

Two facts have to stay apart for the whole feature to be worth anything: "you
have not finished specifying this" and "nothing we have can express this". Only
the second may be escalated, and the refusal for the first has to be more useful
than the escalation would have been.
"""

from __future__ import annotations

import sqlite3

from conftest import INTENT_BODY, auth

# Everything the registered plan declares, so the intent binds without a human.
FULL_INTENT = {
    "kind": "intent",
    "intent": {
        "dataset": "XNAS.ITCH",
        "schema": "mbo",
        "window": {"start": "2026-09-01", "end": "2026-09-02"},
        "symbols": ["AAPL"],
        "stype_in": "raw_symbol",
    },
    "max_usd": 40,
    "why": "one MBO day for AAPL",
}

# A dataset nobody claims: no param would help, which is the one case a caller
# may escalate.
UNCLAIMED_INTENT = {
    "kind": "intent",
    "intent": {
        "dataset": "NOTAVENDOR.X",
        "schema": "trades",
        "window": {"start": "2026-09-01", "end": "2026-09-02"},
    },
    "max_usd": 40,
    "why": "data from a source we have no plan for",
}


def _intent(client, body):
    response = client.post("/v1/requests", json=body, headers=auth())
    assert response.status_code == 202, response.text
    return response.json()


def _escalate(client, request_id, token="tok-escalate", note=None):
    payload = {"note": note} if note else {}
    return client.post(
        f"/v1/requests/{request_id}/escalate", json=payload, headers=auth(token)
    )


def _events(db_path, request_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT actor_kind, event, detail_json FROM events "
                "WHERE request_id = ? ORDER BY id",
                (request_id,),
            )
        ]
    finally:
        conn.close()


def test_an_intent_that_a_plan_could_take_says_so_and_stays_blocked(client):
    body = _intent(client, INTENT_BODY)
    assert body["state"] == "blocked"
    assert body["match_verdict"] == "needs_params"
    assert sorted(body["match"]["would_fit"]["missing"]) == ["stype_in", "symbols"]
    assert body["blocked_reason"] == "awaiting_plan"


def test_escalating_a_near_miss_is_refused_with_the_shorter_path(client):
    request_id = _intent(client, INTENT_BODY)["request_id"]
    response = _escalate(client, request_id)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "escalate_refused"
    assert error["reason"] == "a_plan_matches"
    # The point of the refusal: what to send instead.
    would_fit = error["would_fit"]
    assert would_fit["plan_type"] == "databento_batch_download"
    assert would_fit["missing"] == ["stype_in", "symbols"] or sorted(
        would_fit["missing"]
    ) == ["stype_in", "symbols"]
    assert would_fit["params"]["dataset"] == "OPRA.PILLAR"


def test_a_complete_intent_binds_instead_of_waiting(client):
    body = _intent(client, FULL_INTENT)
    assert body["state"] == "draft"
    assert body["match_verdict"] == "matched"
    detail = client.get(
        f"/v1/requests/{body['request_id']}", headers=auth()
    ).json()
    assert detail["plan_type"] == "databento_batch_download"
    assert detail["job_hash"]
    assert detail["job"]["steps"]


def test_an_unclaimed_dataset_may_be_escalated(client, db_path):
    request_id = _intent(client, UNCLAIMED_INTENT)["request_id"]
    response = _escalate(client, request_id, note="no vendor speaks for this code")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["created"] is True
    assert body["escalated_at"]
    assert body["blocked_reason"] == "awaiting_plan"  # a request, not a plan

    events = _events(db_path, request_id)
    assert [e["event"] for e in events] == ["submitted", "escalated"]
    detail = events[-1]["detail_json"]
    assert "no vendor speaks" in detail
    # The evidence travels with the request: which registry said no.
    assert '"registry":"' in detail


def test_escalating_twice_writes_one_event(client, db_path):
    request_id = _intent(client, UNCLAIMED_INTENT)["request_id"]
    assert _escalate(client, request_id).status_code == 202
    second = _escalate(client, request_id)
    assert second.status_code == 202
    assert second.json()["created"] is False
    assert [e["event"] for e in _events(db_path, request_id)].count("escalated") == 1


def test_a_read_token_cannot_escalate(client):
    request_id = _intent(client, UNCLAIMED_INTENT)["request_id"]
    response = _escalate(client, request_id, token="tok-submit")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"


def test_escalating_something_that_is_not_waiting_is_refused(client):
    # A plan row is `draft`, not `blocked`: nothing to escalate.
    plan = client.post(
        "/v1/requests",
        json={
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
        },
        headers=auth(),
    ).json()
    response = _escalate(client, plan["request_id"])
    assert response.status_code == 409
    assert response.json()["error"]["reason"] == "wrong_state"


def test_escalating_an_unknown_request_is_404(client):
    response = _escalate(client, "req_nope")
    assert response.status_code == 404


def test_the_book_row_carries_the_verdict_and_the_ask(client):
    request_id = _intent(client, UNCLAIMED_INTENT)["request_id"]
    _escalate(client, request_id)
    rows = client.get("/ui/dashboard.json", headers=auth("tok-read")).json()["rows"]
    row = next(r for r in rows if r["id"] == request_id)
    assert row["match_verdict"] == "not_claimed"
    assert row["escalated_at"]
