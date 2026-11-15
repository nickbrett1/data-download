"""Plan submission: 202, idempotence, validation, scopes, and no advancement."""

from __future__ import annotations

import copy
import json
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
    assert row["params"] == PLAN_BODY["params"]
    assert row["intent"] is None
    assert row["estimate_usd"] is None
    # The Job, not the params and not the body: the Plan's binder emitted it.
    assert row["job"]["plan"] == "databento_batch_download"
    kinds = [step["kind"] for step in row["job"]["steps"]]
    assert kinds == [
        "resolve", "quote", "submit", "poll", "download", "convert", "verify",
    ]
    assert row["job"]["steps"][-1]["kwargs"]["on_failure"] == "quarantine"
    assert row["job_hash"]

    # §2 rule 3: the Job names its Plan and carries no code pointer.
    assert "module" not in json.dumps(row["job"])
    assert "/" not in row["job"]["plan"]


def test_a_re_request_creates_a_second_row_and_says_what_it_duplicates(client):
    """§5.6: a re-request is a Request. Two ids, two rows, one job_hash."""
    first = _submit(client)
    assert first.status_code == 202
    assert first.json()["duplicate_of"] is None
    first_id = first.json()["request_id"]

    again = _submit(client)
    assert again.status_code == 202, again.text
    assert again.json()["request_id"] != first_id
    assert again.json()["duplicate_of"] == first_id

    listing = client.get("/v1/requests", headers=auth()).json()
    assert listing["total"] == 2
    hashes = {
        client.get(f"/v1/requests/{r['request_id']}", headers=auth()).json()["job_hash"]
        for r in listing["items"]
    }
    assert len(hashes) == 1


def test_a_repeated_idempotency_key_is_409_naming_the_same_request(client):
    """Exactly-once is opt-in, by the caller: only they know retry from re-request."""
    headers = {**auth(), "Idempotency-Key": "attempt-1"}
    first = client.post("/v1/requests", json=PLAN_BODY, headers=headers)
    assert first.status_code == 202
    request_id = first.json()["request_id"]

    again = client.post("/v1/requests", json=PLAN_BODY, headers=headers)
    assert again.status_code == 409
    error = again.json()["error"]
    assert error["code"] == "duplicate_request"
    assert error["request_id"] == request_id

    # A *different* key is a different attempt: it creates its own row.
    other = client.post(
        "/v1/requests", json=PLAN_BODY, headers={**auth(), "Idempotency-Key": "attempt-2"}
    )
    assert other.status_code == 202
    assert other.json()["request_id"] != request_id


def test_reordered_keys_hash_identically_and_bind_the_same_job(client):
    """Body key order is not content: same job_hash, and `duplicate_of` notices."""
    first = _submit(client)
    assert first.status_code == 202

    reordered = {
        "verify": PLAN_BODY["verify"],
        "params": dict(reversed(list(PLAN_BODY["params"].items()))),
        "plan_type": PLAN_BODY["plan_type"],
        "kind": PLAN_BODY["kind"],
    }
    again = _submit(client, reordered)
    assert again.status_code == 202
    assert again.json()["duplicate_of"] == first.json()["request_id"]


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
    detail = events[0]["detail"]
    assert detail["body_kind"] == "plan"
    assert detail["plan_type"] == "databento_batch_download"
    # The Job's identity is in the audit trail: what was filed, at byte level.
    assert detail["job_hash"]


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


def test_a_plan_keeps_the_why_and_the_ceiling_it_was_filed_with(client):
    """A plan body is `{plan, max_usd, why}` (§7.2), and both survived only in
    `intent_json` before this -- so a *plan* submission dropped them silently.

    That was invisible while nothing read them: the book showed a derived badge
    and the ceiling came out of the intent body. It stopped being invisible when
    the detail page had a `why` to print and a `$0.0000` ceiling on a request
    whose caller had sent 120.
    """
    body = {
        **PLAN_BODY,
        "max_usd": 40,
        "why": "a two-day trades tape for the Oct expiry, to test the interpreter",
    }
    filed = _submit(client, body).json()

    row = client.get(f"/v1/requests/{filed['request_id']}", headers=auth()).json()
    assert row["why"] == body["why"]
    assert row["max_usd"] == 40

    detail = client.get(f"/ui/request/{filed['request_id']}.json?token=tok-read").json()
    assert detail["payload"]["why"] == body["why"]
    assert detail["max_usd"] == 40
    assert detail["label"] == "databento_batch_download"
