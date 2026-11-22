"""Withdraw: a filed row nobody has run can be taken back.

The motivating case was the duplicate the retry proof left behind: two drafts,
same body, one of them never wanted. There was no verb for that, so the only
ways out were approving a request nobody wanted (a spend) or editing the
database by hand (a lie about the record).

What withdraw is *not*: a delete. The row, its quote, its `why` and its events
all stay and the badge changes to `withdrawn` -- because "I changed my mind" is
a fact about the request, and a missing row reads as a request that was never
filed. And it is not the inverse of approval: `approved` is refused, because an
approval authorises one attempt and once the runner may hold the lease, stopping
the work is the runner's business.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from conftest import INTENT_BODY, PLAN_BODY, auth, served


def _file(client, body=PLAN_BODY, headers=None):
    res = client.post("/v1/requests", json=body, headers=headers or auth())
    assert res.status_code == 202, res.text
    return res.json()["request_id"]


def _state(db_path: Path, request_id: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT state FROM requests WHERE id = ?", (request_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _events(db_path: Path, request_id: str) -> list[tuple[str, dict]]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            (row[0], json.loads(row[1] or "{}"))
            for row in conn.execute(
                "SELECT event, detail_json FROM events WHERE request_id = ? ORDER BY id",
                (request_id,),
            )
        ]
    finally:
        conn.close()


def test_a_draft_is_withdrawn_and_says_so(client, db_path):
    request_id = _file(client)

    res = client.post(
        f"/v1/requests/{request_id}/withdraw",
        json={"note": "filed twice by the retry proof; this is the spare"},
        headers=served(),
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "request_id": request_id,
        "state": "withdrawn",
        "withdrawn": True,
    }
    assert _state(db_path, request_id) == "withdrawn"

    events = _events(db_path, request_id)
    assert [e for e, _ in events] == ["submitted", "withdrawn"]
    detail = events[-1][1]
    assert detail["actor"] == "nick@example.com"
    assert detail["was"] == "draft"
    assert detail["note"].startswith("filed twice")
    assert detail["via"] == "serve"


def test_a_withdrawn_row_keeps_its_body_and_its_reason(client, db_path):
    """The record of what was asked for survives the withdrawal."""
    request_id = _file(client)
    client.post(f"/v1/requests/{request_id}/withdraw", headers=served())

    row = client.get(f"/v1/requests/{request_id}", headers=auth()).json()
    assert row["state"] == "withdrawn"
    assert row["why"] == PLAN_BODY["why"]
    assert row["params"]["dataset"] == "GLBX.MDP3"
    assert row["max_usd"] == 5


def test_withdrawing_twice_is_a_no_op_with_no_second_event(client, db_path):
    request_id = _file(client)
    client.post(f"/v1/requests/{request_id}/withdraw", headers=served())

    res = client.post(f"/v1/requests/{request_id}/withdraw", headers=served())
    assert res.status_code == 200, res.text
    assert res.json()["withdrawn"] is False
    assert [e for e, _ in _events(db_path, request_id)] == ["submitted", "withdrawn"]


def test_a_blocked_intent_is_not_withdrawable_yet(client, db_path):
    """Deliberate: two CHECKs say "a blocked row has no Job and no estimate".

    A withdrawn row that never had a Job would have to relax both, and that is a
    decision about what a never-bound row *is*, not a side effect of adding a
    button. Pinned here so the refusal is a choice on the record rather than an
    accident somebody later "fixes" by dropping the constraints.
    """
    request_id = _file(client, INTENT_BODY)
    assert _state(db_path, request_id) == "blocked"

    res = client.post(f"/v1/requests/{request_id}/withdraw", headers=served())
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "withdraw_refused"
    assert res.json()["error"]["state"] == "blocked"
    assert _state(db_path, request_id) == "blocked"


def test_an_approved_row_cannot_be_withdrawn(client, db_path):
    """An approval authorised one attempt; taking it back is not book-keeping."""
    request_id = _file(client)
    assert (
        client.post(f"/v1/requests/{request_id}/approve", headers=served()).status_code
        == 200
    )

    res = client.post(f"/v1/requests/{request_id}/withdraw", headers=served())
    assert res.status_code == 409, res.text
    error = res.json()["error"]
    assert error["code"] == "withdraw_refused"
    assert error["reason"] == "wrong_state"
    assert error["state"] == "approved"
    assert "authorised" in error["message"]
    assert _state(db_path, request_id) == "approved"


def test_a_failed_row_cannot_be_withdrawn(client, db_path):
    """It already ran, and retry is the verb for "try that again"."""
    request_id = _file(client)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE requests SET state = 'failed', errors_json = ? WHERE id = ?",
            (json.dumps({"error": "driver_not_ready"}), request_id),
        )
        conn.commit()
    finally:
        conn.close()

    res = client.post(f"/v1/requests/{request_id}/withdraw", headers=served())
    assert res.status_code == 409, res.text
    assert res.json()["error"]["state"] == "failed"


def test_an_unknown_id_is_a_404(client):
    res = client.post("/v1/requests/req_nope/withdraw", headers=served())
    assert res.status_code == 404, res.text
    assert res.json()["error"]["code"] == "not_found"


def test_a_read_token_may_not_withdraw(client):
    request_id = _file(client)

    res = client.post(
        f"/v1/requests/{request_id}/withdraw", headers=auth("tok-read")
    )
    assert res.status_code == 403, res.text
    assert _state_from(client, request_id) == "draft"


def _state_from(client, request_id: str) -> str:
    return client.get(f"/v1/requests/{request_id}", headers=auth()).json()["state"]


def test_a_submit_token_may_withdraw_without_being_a_person(client, db_path):
    """Filing and withdrawing are two halves of one decision, both spend-free."""
    request_id = _file(client)

    res = client.post(
        f"/v1/requests/{request_id}/withdraw", headers=auth("tok-submit-only")
    )
    assert res.status_code == 200, res.text
    detail = _events(db_path, request_id)[-1][1]
    assert detail["actor"] == "tok-submit-only"


def test_a_stranger_on_the_tailnet_may_not_withdraw(client):
    request_id = _file(client)

    res = client.post(
        f"/v1/requests/{request_id}/withdraw",
        headers={"Tailscale-User-Login": "someone@example.com"},
    )
    assert res.status_code == 403, res.text
    assert res.json()["error"]["code"] == "not_an_approver"


def test_the_page_door_works_with_no_credential(client):
    """The button on the page holds no token; Serve's login is the credential."""
    request_id = _file(client)

    res = client.post(f"/v1/requests/{request_id}/withdraw", headers=served())
    assert res.status_code == 200, res.text


def test_an_undeclared_actor_is_refused(client):
    request_id = _file(client)

    res = client.post(
        f"/v1/requests/{request_id}/withdraw",
        json={"actor": "somebody-else"},
        headers=served(),
    )
    assert res.status_code == 400, res.text
    assert res.json()["error"]["code"] == "invalid_body"
    assert "actor is not accepted" in res.json()["error"]["message"]


def test_a_withdrawn_row_is_not_pending_and_is_not_retryable(client):
    request_id = _file(client)
    client.post(f"/v1/requests/{request_id}/withdraw", headers=served())

    pending = client.get("/v1/requests", params={"state": "draft"}, headers=auth())
    assert request_id not in [r["id"] for r in pending.json()["items"]]

    res = client.post(f"/v1/requests/{request_id}/retry", headers=auth())
    assert res.status_code == 409, res.text
    error = res.json()["error"]
    assert error["code"] == "not_retryable"
    assert error["reason"] == "not_settled"
    assert "withdrawn" in error["message"]
    assert "still in flight" not in error["message"]


def test_a_withdrawn_row_is_in_the_book_with_a_badge_of_its_own(client):
    """It landed nothing, but it must not vanish from the list."""
    request_id = _file(client)
    client.post(f"/v1/requests/{request_id}/withdraw", headers=served())

    book = client.get("/ui/dashboard.json").json()
    row = next(r for r in book["rows"] if r["id"] == request_id)
    assert row["state"] == "withdrawn"
    assert row["facet"] == "done"
    assert row["failure"] is None


@pytest.mark.parametrize("note", [123, ["a", "list"]])
def test_a_note_that_is_not_a_string_is_refused(client, note):
    request_id = _file(client)

    res = client.post(
        f"/v1/requests/{request_id}/withdraw", json={"note": note}, headers=served()
    )
    assert res.status_code == 400, res.text
    assert "note must be a string" in res.json()["error"]["message"]


def test_an_old_store_is_rebuilt_so_it_can_hold_a_withdrawn_row(tmp_path: Path):
    """The migration, against a table that has the old CHECK.

    The deployed store is the one that matters: `CREATE TABLE IF NOT EXISTS`
    never re-runs, so a store created before this change keeps a `state` CHECK
    that refuses the new value *and* a `v_done` that cannot show the row. Both
    are fixed by one rebuild, and this test pins that the row survives it.
    """
    import asyncio

    from data_download import db as dbmod

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    old_ddl = dbmod._rows_final_said_requests_ddl().replace(
        "CREATE TABLE requests_widened (", "CREATE TABLE requests ("
    )
    # The definition as it shipped before `withdrawn`: the migration's whole
    # condition is the absence of that word from the live table's SQL.
    conn.executescript(old_ddl.replace(",'withdrawn'", ""))
    conn.execute(
        "INSERT INTO requests (id, state, body_kind, params_json, payload_hash, "
        "job_json, created_at, updated_at) VALUES ('req_old', 'draft', 'plan', "
        "'{}', 'abc', '{}', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
    )
    conn.commit()
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'requests'"
    ).fetchone()[0]
    assert "'withdrawn'" not in sql
    conn.close()

    from data_download.db import Store

    async def go() -> None:
        store = Store(str(path))
        await store.connect()
        try:
            row, changed = await store.withdraw("req_old", "nick@example.com")
            assert changed is True
            assert row["state"] == "withdrawn"
        finally:
            await store.close()

    asyncio.run(go())

    conn = sqlite3.connect(path)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'requests'"
        ).fetchone()[0]
        assert "'withdrawn'" in sql
        assert (
            conn.execute("SELECT state FROM requests WHERE id = 'req_old'").fetchone()[0]
            == "withdrawn"
        )
        # v_done came back with the new definition, which is the half of the
        # migration a CHECK-only fix would have missed.
        assert (
            conn.execute("SELECT count(*) FROM v_done WHERE id = 'req_old'").fetchone()[
                0
            ]
            == 1
        )
    finally:
        conn.close()
