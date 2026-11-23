"""Archive: take a finished row off the book without rewriting what happened.

The gap this fills: a `failed` row had one verb, `retry`, and no way to stop
looking at it. `withdraw` is not that verb -- it writes `state`, and a failed
attempt's state is the outcome of something that really happened (the vendor was
asked, the driver refused, the errors are on the row). Setting that to
`withdrawn` would make the record claim the request was cancelled before it ran.

So archive writes a *date*: the row keeps `state = 'failed'`, keeps its badge,
its `errors_json` and its events, and stops appearing in the book's default list.
Restoring is the same route with `{"archived": false}`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import PLAN_BODY, auth, served


def _file(client, body=PLAN_BODY, headers=None):
    res = client.post("/v1/requests", json=body, headers=headers or auth())
    assert res.status_code == 202, res.text
    return res.json()["request_id"]


def _fail(db_path: Path, request_id: str, code: str = "driver_not_ready", detail=None):
    payload = {"error": code} if detail is None else {"error": code, "detail": detail}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE requests SET state = 'failed', errors_json = ?, updated_at = "
            "'2026-11-23T01:00:00Z' WHERE id = ?",
            (json.dumps(payload), request_id),
        )
        conn.commit()
    finally:
        conn.close()


def _row(db_path: Path, request_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(
            conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        )
    finally:
        conn.close()


def _events(db_path: Path, request_id: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT event FROM events WHERE request_id = ? ORDER BY id",
                (request_id,),
            )
        ]
    finally:
        conn.close()


def test_a_failed_row_is_archived_and_still_says_it_failed(client, db_path):
    request_id = _file(client)
    _fail(db_path, request_id, detail="no driver is installed for vendor 'Databento'")

    res = client.post(
        f"/v1/requests/{request_id}/archive",
        json={"note": "the phase-C placeholder refusals; nothing to do here"},
        headers=served(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["state"] == "failed", "archiving must not rewrite the outcome"
    assert body["archived_at"] is not None
    assert body["changed"] is True

    # The badge still says failed, and the reason is still there: this is the
    # difference between archive and withdraw, and the point of the verb.
    detail = client.get(f"/ui/request/{request_id}.json").json()
    assert detail["state"] == "failed"
    assert detail["failure"]["code"] == "driver_not_ready"
    assert _events(db_path, request_id) == ["submitted", "archived"]


def test_an_archived_row_leaves_the_list_but_not_the_store(client, db_path):
    request_id = _file(client)
    _fail(db_path, request_id)
    client.post(f"/v1/requests/{request_id}/archive", headers=served())

    book = client.get("/ui/dashboard.json").json()
    assert request_id not in [r["id"] for r in book["rows"]]
    assert book["total"] == len(book["rows"])
    assert book["archived"] == 1

    row = next(r for r in book["facets"]["archived"] if r["id"] == request_id)
    assert row["facet"] == "archived"
    assert row["facet_label"] == "Archived"
    assert row["state"] == "failed", "the badge is the record; the filter is the list"


def test_restoring_puts_it_back_and_is_one_decision_with_a_toggle(client, db_path):
    request_id = _file(client)
    _fail(db_path, request_id)
    client.post(f"/v1/requests/{request_id}/archive", headers=served())

    res = client.post(
        f"/v1/requests/{request_id}/archive",
        json={"archived": False},
        headers=served(),
    )
    assert res.status_code == 200, res.text
    assert res.json()["archived_at"] is None
    assert _row(db_path, request_id)["archived_at"] is None
    assert _events(db_path, request_id) == ["submitted", "archived", "restored"]

    book = client.get("/ui/dashboard.json").json()
    assert request_id in [r["id"] for r in book["rows"]]
    assert book["archived"] == 0


def test_archiving_twice_is_a_no_op_with_no_second_event(client, db_path):
    request_id = _file(client)
    _fail(db_path, request_id)
    client.post(f"/v1/requests/{request_id}/archive", headers=served())

    res = client.post(f"/v1/requests/{request_id}/archive", headers=served())
    assert res.status_code == 200, res.text
    assert res.json()["changed"] is False
    assert _events(db_path, request_id) == ["submitted", "archived"]


def test_restoring_one_that_was_never_archived_changes_nothing(client, db_path):
    request_id = _file(client)
    _fail(db_path, request_id)

    res = client.post(
        f"/v1/requests/{request_id}/archive",
        json={"archived": False},
        headers=served(),
    )
    assert res.status_code == 200, res.text
    assert res.json()["changed"] is False
    assert _events(db_path, request_id) == ["submitted"]


def test_a_draft_is_withdrawn_rather_than_archived(client, db_path):
    """A draft is a decision nobody has made yet; it is not finished."""
    request_id = _file(client)

    res = client.post(f"/v1/requests/{request_id}/archive", headers=served())
    assert res.status_code == 409, res.text
    error = res.json()["error"]
    assert error["code"] == "archive_refused"
    assert error["reason"] == "wrong_state"
    assert error["state"] == "draft"
    assert "withdrawn" in error["message"]
    assert _row(db_path, request_id)["archived_at"] is None


def test_an_approved_row_is_not_archived(client, db_path):
    """Hiding something that may still spend is the one thing a list must not do."""
    request_id = _file(client)
    client.post(f"/v1/requests/{request_id}/approve", headers=served())

    res = client.post(f"/v1/requests/{request_id}/archive", headers=served())
    assert res.status_code == 409, res.text
    assert res.json()["error"]["state"] == "approved"


def test_a_withdrawn_row_can_be_archived_too(client, db_path):
    request_id = _file(client)
    client.post(f"/v1/requests/{request_id}/withdraw", headers=served())

    res = client.post(f"/v1/requests/{request_id}/archive", headers=served())
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "withdrawn"
    book = client.get("/ui/dashboard.json").json()
    assert book["withdrawn"] == 0
    assert book["archived"] == 1


def test_a_succeeded_row_can_be_archived(client, db_path):
    request_id = _file(client)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE requests SET state = 'succeeded', landed_sha256 = 'abc' "
            "WHERE id = ?",
            (request_id,),
        )
        conn.commit()
    finally:
        conn.close()

    res = client.post(f"/v1/requests/{request_id}/archive", headers=served())
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "succeeded"
    assert res.json()["archived_at"] is not None


def test_an_unknown_id_is_a_404(client):
    res = client.post("/v1/requests/req_nope/archive", headers=served())
    assert res.status_code == 404, res.text


def test_a_read_token_may_not_archive(client, db_path):
    request_id = _file(client)
    _fail(db_path, request_id)

    res = client.post(f"/v1/requests/{request_id}/archive", headers=auth("tok-read"))
    assert res.status_code == 403, res.text
    assert _row(db_path, request_id)["archived_at"] is None


def test_a_stranger_on_the_tailnet_may_not_archive(client, db_path):
    request_id = _file(client)
    _fail(db_path, request_id)

    res = client.post(
        f"/v1/requests/{request_id}/archive",
        headers={"Tailscale-User-Login": "someone@example.com"},
    )
    assert res.status_code == 403, res.text
    assert res.json()["error"]["code"] == "not_an_approver"


def test_an_undeclared_actor_is_refused(client, db_path):
    request_id = _file(client)
    _fail(db_path, request_id)

    res = client.post(
        f"/v1/requests/{request_id}/archive",
        json={"actor": "somebody-else"},
        headers=served(),
    )
    assert res.status_code == 400, res.text
    assert "actor is not accepted" in res.json()["error"]["message"]


def test_a_truthy_string_is_not_a_boolean(client, db_path):
    request_id = _file(client)
    _fail(db_path, request_id)

    res = client.post(
        f"/v1/requests/{request_id}/archive",
        json={"archived": "no"},
        headers=served(),
    )
    assert res.status_code == 400, res.text
    assert "archived must be true or false" in res.json()["error"]["message"]


def test_an_archived_row_can_be_retried_and_the_retry_is_not_archived(
    client, db_path
):
    """Archiving is about the book, not about the row's usefulness."""
    request_id = _file(client)
    _fail(db_path, request_id)
    client.post(f"/v1/requests/{request_id}/archive", headers=served())

    res = client.post(f"/v1/requests/{request_id}/retry", headers=auth())
    assert res.status_code == 202, res.text
    new_id = res.json()["request_id"]

    book = client.get("/ui/dashboard.json").json()
    assert new_id in [r["id"] for r in book["rows"]]
