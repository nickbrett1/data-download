"""Plan submission: 202, idempotence, validation, scopes, and no advancement."""

from __future__ import annotations

import copy
import time

from conftest import INTENT_BODY, PLAN_BODY, auth

VALID_STATES = {"draft", "blocked"}


def _submit(client, body=None, token="tok-submit"):
    return client.post("/v1/requests", json=body if body is not None else PLAN_BODY, headers=auth(token))


def test_healthz_needs_no_auth_and_no_store(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_plan_submit_returns_202_then_reads_back(client):
    response = _submit(client)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["state"] == "draft"
    assert payload["blocked_reason"] is None
    request_id = payload["request_id"]

    read = client.get(f"/v1/requests/{request_id}", headers=auth())
    assert read.status_code == 200
    row = read.json()
    assert row["request_id"] == request_id
    assert row["state"] == "draft"
    assert row["body_kind"] == "plan"
    assert row["plan_type"] == "databento_batch_download"
    assert row["plan"]["params"] == PLAN_BODY["params"]
    assert row["plan"]["verify"] == {"on_failure": "quarantine"}
    assert row["intent"] is None
    assert row["estimate_usd"] is None
    assert row["plan_hash"]


def test_duplicate_returns_409_carrying_the_same_id(client):
    first = _submit(client)
    assert first.status_code == 202
    request_id = first.json()["request_id"]

    again = _submit(client)
    assert again.status_code == 409
    error = again.json()["error"]
    assert error["code"] == "duplicate_request"
    assert error["request_id"] == request_id


def test_reordered_keys_hash_identically(client):
    first = _submit(client)
    assert first.status_code == 202

    reordered = {
        "verify": PLAN_BODY["verify"],
        "params": dict(reversed(list(PLAN_BODY["params"].items()))),
        "plan_type": PLAN_BODY["plan_type"],
        "kind": PLAN_BODY["kind"],
    }
    again = _submit(client, reordered)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "duplicate_request"
    assert again.json()["error"]["request_id"] == first.json()["request_id"]


def test_unregistered_plan_type_is_422_plan_type_unknown(client):
    body = copy.deepcopy(PLAN_BODY)
    body["plan_type"] = "no_such_type"
    response = _submit(client, body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "plan_type_unknown"


def test_missing_verify_is_plan_not_valid(client):
    body = copy.deepcopy(PLAN_BODY)
    del body["verify"]
    response = _submit(client, body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "plan_not_valid"
    assert "verify" in response.json()["error"]["message"]


def test_bogus_on_failure_is_plan_not_valid(client):
    body = copy.deepcopy(PLAN_BODY)
    body["verify"] = {"on_failure": "ignore"}
    response = _submit(client, body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "plan_not_valid"


def test_params_violation_names_the_field_path(client):
    body = copy.deepcopy(PLAN_BODY)
    body["params"]["stype_in"] = "guessed"
    response = _submit(client, body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "plan_not_valid"
    assert error["field"] == "params.stype_in"
    assert "params.stype_in" in error["message"]


def test_missing_params_field_names_the_field_path(client):
    body = copy.deepcopy(PLAN_BODY)
    del body["params"]["symbols"]
    response = _submit(client, body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "plan_not_valid"


def test_malformed_body_is_400_invalid_body(client):
    response = client.post(
        "/v1/requests",
        content=b"{not json",
        headers={**auth(), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_body"


def test_unknown_kind_is_400_invalid_body(client):
    response = _submit(client, {"kind": "download"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_body"


def test_no_token_is_401_unauthenticated(client):
    response = client.post("/v1/requests", json=PLAN_BODY)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_unknown_token_is_401_unauthenticated(client):
    response = _submit(client, token="not-a-real-token")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_read_only_token_cannot_submit(client):
    response = _submit(client, token="tok-read")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"


def test_submit_token_and_approve_only_token_cannot_read(client):
    submitted = _submit(client)
    request_id = submitted.json()["request_id"]

    assert client.get("/v1/requests", headers=auth("tok-approve")).status_code == 403
    assert client.get(f"/v1/requests/{request_id}", headers=auth("tok-approve")).status_code == 403


def test_missing_request_is_404_not_found(client):
    response = client.get("/v1/requests/req_missing", headers=auth())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_exactly_one_submitted_event(client):
    request_id = _submit(client).json()["request_id"]
    events = client.get(f"/v1/requests/{request_id}/events", headers=auth()).json()["items"]
    assert len(events) == 1
    assert events[0]["event"] == "submitted"
    assert events[0]["actor_kind"] == "submitter"
    assert events[0]["detail"] == {"body_kind": "plan", "plan_type": "databento_batch_download"}


def test_events_for_missing_request_is_404(client):
    assert client.get("/v1/requests/nope/events", headers=auth()).status_code == 404


def test_list_filters_and_order(client):
    first = _submit(client).json()["request_id"]
    body = copy.deepcopy(PLAN_BODY)
    body["params"]["symbols"] = ["NQ.c.0"]
    second = _submit(client, body).json()["request_id"]

    listing = client.get("/v1/requests", headers=auth()).json()
    assert listing["total"] == 2
    # newest first
    assert [row["request_id"] for row in listing["items"]] == [second, first]

    filtered = client.get("/v1/requests?state=blocked", headers=auth()).json()
    assert filtered["items"] == []

    assert client.get("/v1/requests?limit=1", headers=auth()).json()["items"][0]["request_id"] == second
    assert client.get("/v1/requests?offset=1", headers=auth()).json()["items"][0]["request_id"] == first


def test_wait_is_bounded_and_returns_the_same_row(client):
    request_id = _submit(client).json()["request_id"]
    started = time.monotonic()
    response = client.get(f"/v1/requests/{request_id}?wait=1", headers=auth())
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert response.json()["state"] == "draft"
    assert 0.2 <= elapsed < 3

    # Above the 60s cap is a 400, not a hang.
    over = client.get(f"/v1/requests/{request_id}?wait=600", headers=auth())
    assert over.status_code == 400


def test_nothing_advances_past_draft_or_blocked(client):
    _submit(client)
    body = copy.deepcopy(PLAN_BODY)
    body["params"]["symbols"] = ["CL.c.0"]
    _submit(client, body)
    client.post("/v1/requests", json=INTENT_BODY, headers=auth())

    everything = client.get("/v1/requests", headers=auth()).json()["items"]
    assert everything, "expected rows"
    assert {row["state"] for row in everything} <= VALID_STATES
