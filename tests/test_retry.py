"""Retry: the same body, filed again, as a new request.

A retry exists because a failure is often repaired *outside* the request —
a driver gets installed, a vendor key is fixed, a ceiling is raised — and the
row it happened to must not be the thing that has to change. What it must not
be is a rewind: `failed` is history, with events, an `errors_json` and (when it
got that far) an output directory that describe an attempt which really
happened, and the approval that preceded it authorised that attempt and no
other.

So every test here is about the new row and the old row at once: a new `draft`
with a fresh quote and its own id, and the failed row untouched beside it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import PLAN_BODY, QUOTE_USD, auth, served


def _file(client, body=PLAN_BODY, headers=None):
    res = client.post("/v1/requests", json=body, headers=headers or auth())
    assert res.status_code == 202, res.text
    return res.json()["request_id"]


def _fail(db_path: Path, request_id: str, code: str, detail: str | None = None):
    """Settle a row the way the runner does, without running it.

    Same column, same shape: one dict with `error` and `detail`. Nothing else
    on the row changes, because nothing else does when a run refuses.
    """
    payload = {"error": code} if detail is None else {"error": code, "detail": detail}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE requests SET state = 'failed', errors_json = ?, updated_at = "
            "'2026-11-22T21:00:00Z' WHERE id = ?",
            (json.dumps(payload), request_id),
        )
        conn.commit()
    finally:
        conn.close()


def _row(client, request_id):
    res = client.get(f"/v1/requests/{request_id}", headers=auth())
    assert res.status_code == 200, res.text
    return res.json()


def test_a_retry_files_the_same_body_as_a_new_draft(client, db_path):
    """New id, same request, and nothing authorised until somebody approves."""
    old = _file(client)
    _fail(
        db_path,
        old,
        "driver_not_ready",
        "no driver is installed for vendor 'Databento' (installed: stub v1 (Stub))",
    )

    res = client.post(f"/v1/requests/{old}/retry", headers=auth())
    assert res.status_code == 202, res.text
    body = res.json()

    new = body["request_id"]
    assert new != old
    # A retry writes a draft: the quote is taken again and a human still has to
    # authorise it, with the new number in front of them.
    assert body["state"] == "draft"
    assert body["quote_usd"] == QUOTE_USD
    assert body["retried_from"] == old
    # `duplicate_of` is *not* asserted as `old`, and the reason is worth having
    # written down: the rebuilt body carries `max_usd` as the REAL the column
    # stores (5.0) where the caller sent the JSON integer (5), so the canonical
    # bytes -- and therefore the payload hash -- need not agree. `retried_from`
    # is the link to rely on; `duplicate_of` stays what it always was,
    # informational and present only when the bodies happen to match exactly.

    fresh = _row(client, new)
    before = _row(client, old)
    assert fresh["params"] == before["params"]
    assert fresh["max_usd"] == before["max_usd"] == 5
    assert fresh["why"] == PLAN_BODY["why"]
    # Same body, same Job: the seal is over what was asked for, not over when.
    assert fresh["job_hash"] == before["job_hash"]


def test_the_verify_policy_comes_back_off_the_job(client, db_path):
    """A plan's `verify` is not in `params`, so the Job is where it survives.

    Filing the retry without it would either fail validation or silently change
    the policy the run is judged by — the one field where "same request" has to
    mean the same rule.
    """
    old = _file(client)
    _fail(db_path, old, "driver_not_ready")
    new = client.post(f"/v1/requests/{old}/retry", headers=auth()).json()["request_id"]

    job = _row(client, new)["job"]
    verify = [step for step in job["steps"] if step["kind"] == "verify"]
    assert len(verify) == 1
    assert verify[0]["kwargs"]["on_failure"] == "quarantine"


def test_the_row_that_failed_is_left_exactly_as_it_was(client, db_path):
    old = _file(client)
    detail = "no driver is installed for vendor 'Databento'"
    _fail(db_path, old, "driver_not_ready", detail)
    before = _row(client, old)
    events_before = client.get(
        f"/v1/requests/{old}/events", headers=auth()
    ).json()["items"]

    client.post(f"/v1/requests/{old}/retry", headers=auth())

    after = _row(client, old)
    assert after["state"] == "failed"
    assert after["errors"] == before["errors"] == {
        "error": "driver_not_ready",
        "detail": detail,
    }
    assert after["updated_at"] == before["updated_at"]
    # And no event landed on it: the retry is a fact about the new row.
    assert client.get(f"/v1/requests/{old}/events", headers=auth()).json()[
        "items"
    ] == events_before


def test_the_retry_is_recorded_as_coming_from_somewhere(client, db_path):
    old = _file(client)
    _fail(db_path, old, "driver_not_ready")
    new = client.post(f"/v1/requests/{old}/retry", headers=auth()).json()["request_id"]

    events = client.get(f"/v1/requests/{new}/events", headers=auth()).json()["items"]
    assert events[0]["event"] == "submitted"
    assert events[0]["detail"]["retried_from"] == old


def test_only_a_settled_row_can_be_retried(client):
    """A draft has not run: the move is approve, not retry."""
    draft = _file(client)
    res = client.post(f"/v1/requests/{draft}/retry", headers=auth())
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "not_retryable"
    assert res.json()["error"]["reason"] == "not_settled"
    assert res.json()["error"]["state"] == "draft"


def test_a_verifier_failure_is_not_retried_with_the_same_bytes(client, db_path):
    """`verify_shortfall` is a property of the bytes, and the bytes do not change.

    The escape hatch is a deliberate submit with a different request (a wider
    window, another schema) — not a button that spends the same money twice.
    """
    old = _file(client)
    _fail(db_path, old, "verify_shortfall")
    res = client.post(f"/v1/requests/{old}/retry", headers=auth())

    assert res.status_code == 409
    error = res.json()["error"]
    assert error["reason"] == "same_bytes"
    assert error["failure"] == "verify_shortfall"
    assert error["request_id"] == old
    # Nothing was filed.
    book = client.get("/v1/requests", headers=auth()).json()
    assert book["total"] == 1


def test_the_ceiling_is_checked_again_at_re_retry(client, db_path):
    """The quote is taken fresh, so a ceiling that no longer covers it refuses.

    Prices move and windows age; reusing the old estimate would authorise a
    number nobody asked for.
    """
    old = _file(client)
    _fail(db_path, old, "driver_not_ready")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE requests SET max_usd = 0.1 WHERE id = ?", (old,))
        conn.commit()
    finally:
        conn.close()

    res = client.post(f"/v1/requests/{old}/retry", headers=auth())
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "over_ceiling"


# A bindable intent: the conftest `INTENT_BODY` is the *blocked* one (a dataset
# with no plan and no symbols), which is the case a retry must refuse, not the
# one it must rebuild.
INTENT_OK = {
    "kind": "intent",
    "intent": {
        "dataset": "GLBX.MDP3",
        "schema": "trades",
        "window": {"start": "2026-10-01", "end": "2026-10-03"},
        "symbols": ["ES.c.0"],
        "stype_in": "continuous",
    },
    "max_usd": 5,
    "why": "the same window, filed as an intent",
}


def test_an_intent_is_retried_from_its_verbatim_body(client, db_path):
    """An intent kept the whole body, so a retry re-files it exactly."""
    old = _file(client, body=INTENT_OK)
    _fail(db_path, old, "vendor_error", "the vendor answered 502")
    body = client.post(f"/v1/requests/{old}/retry", headers=auth()).json()
    new = body["request_id"]

    fresh = _row(client, new)
    before = _row(client, old)
    assert fresh["body_kind"] == "intent"
    assert fresh["intent"] == before["intent"]
    assert fresh["max_usd"] == 5
    assert fresh["blocked_reason"] is None
    # Here the bytes really are identical -- the whole body was stored verbatim,
    # so there is no column type to reinterpret -- and the store's own duplicate
    # detection points back at the row this came from. That contrast with the
    # plan case is the point.
    assert body["duplicate_of"] == old


def test_a_read_token_cannot_retry(client, db_path):
    old = _file(client)
    _fail(db_path, old, "driver_not_ready")
    res = client.post(f"/v1/requests/{old}/retry", headers=auth("tok-read"))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "insufficient_scope"


def test_the_page_retries_with_the_identity_serve_asserted(client, db_path):
    """The button on the page holds no token, and must still work.

    Same door as `approve`: loopback plus the login `tailscale serve` added.
    It is not a weaker door in practice — a retry writes a draft, and the
    spend is authorised afterwards, by an approval.
    """
    old = _file(client)
    _fail(db_path, old, "driver_not_ready")
    res = client.post(f"/v1/requests/{old}/retry", headers=served())
    assert res.status_code == 202, res.text
    assert res.json()["retried_from"] == old


def test_a_tailnet_stranger_cannot_retry(client, db_path):
    old = _file(client)
    _fail(db_path, old, "driver_not_ready")
    res = client.post(
        f"/v1/requests/{old}/retry", headers=served("someone-else@example.com")
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "not_an_approver"


def test_an_unattested_loopback_guest_cannot_retry(client, db_path):
    """No token, no login header: the peer is loopback but nobody said who."""
    old = _file(client)
    _fail(db_path, old, "driver_not_ready")
    res = client.post(f"/v1/requests/{old}/retry")
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "not_attested"


def test_retrying_an_id_that_does_not_exist_is_a_404(client):
    res = client.post("/v1/requests/req_nope/retry", headers=auth())
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"
