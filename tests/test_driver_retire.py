"""Retiring a driver build that stopped, so its vendor can be tried again.

`needs_human` is the loop saying it gave up, and nothing in the loop leaves it:
a job that stopped once stops again the same way. The case with no way out is
the *deployment* fault -- the loop stopped because a container could not read a
key, somebody fixed the key, and every later request for that vendor was still
refused with the old job's reason. Retiring is a person's verb, not a retry.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from conftest import PLAN_BODY, auth


def _job(db_path, *, state="needs_human", request_id=None):
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO driver_jobs (id, vendor, state, dataset, request_id, "
            "attempts, last_error, created_at, updated_at) VALUES "
            "(?, 'Databento', ?, 'GLBX.MDP3', ?, 1, ?, ?, ?)",
            (
                "drv_stopped",
                state,
                request_id,
                "misconfigured: cannot run docker to read the session key",
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_stopped_build_is_retired_and_the_request_stops_waiting(client, db_path):
    request_id = client.post("/v1/requests", json=PLAN_BODY, headers=auth()).json()[
        "request_id"
    ]
    _job(db_path, request_id=request_id)

    res = client.post(
        "/v1/driver-jobs/drv_stopped/retire",
        json={"note": "the key file is mounted now"},
        headers=auth(),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["retired_at"]
    assert body["request_id"] == request_id

    conn = sqlite3.connect(db_path)
    try:
        retired_at, last_error = conn.execute(
            "SELECT retired_at, last_error FROM driver_jobs WHERE id = 'drv_stopped'"
        ).fetchone()
        driver_state = conn.execute(
            "SELECT driver_state FROM requests WHERE id = ?", (request_id,)
        ).fetchone()[0]
        events = [
            row[0]
            for row in conn.execute(
                "SELECT detail_json FROM events WHERE request_id = ? AND "
                "event = 'driver_state' ORDER BY ts",
                (request_id,),
            )
        ]
    finally:
        conn.close()
    assert retired_at
    assert "misconfigured" in last_error and "the key file is mounted now" in last_error
    # The page stops claiming a build is coming, and the trail says who ended it.
    assert driver_state is None
    assert any("the key file is mounted now" in row for row in events)


def test_a_build_that_is_still_being_worked_is_not_retirable(client, db_path):
    _job(db_path, state="building")
    res = client.post("/v1/driver-jobs/drv_stopped/retire", headers=auth())
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "driver_retire_refused"
    assert res.json()["error"]["reason"] == "not_stopped"


def test_retiring_needs_a_token_and_says_when_there_is_no_job(client, db_path):
    # No bearer at all: the guard answers 403 (no credential) rather than 401.
    assert client.post("/v1/driver-jobs/drv_nope/retire").status_code == 403
    res = client.post("/v1/driver-jobs/drv_nope/retire", headers=auth())
    assert res.status_code == 404, res.text
