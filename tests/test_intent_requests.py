"""Intent submission: it lands blocked, and each of the four constraints bites."""

from __future__ import annotations

import copy

import pytest

from conftest import INTENT_BODY, auth


def _submit(client, body):
    return client.post("/v1/requests", json=body, headers=auth())


def test_intent_lands_blocked_not_draft(client):
    response = _submit(client, INTENT_BODY)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["state"] == "blocked"
    assert payload["blocked_reason"] == "awaiting_plan"

    row = client.get(f"/v1/requests/{payload['request_id']}", headers=auth()).json()
    assert row["state"] == "blocked"
    assert row["blocked_reason"] == "awaiting_plan"
    assert row["plan"] is None
    assert row["plan_hash"] is None
    assert row["estimate_usd"] is None
    assert row["body_kind"] == "intent"
    assert row["intent"]["max_usd"] == 120
    assert row["intent"]["why"] == INTENT_BODY["why"]
    # params are the fetch-relevant subset of the intent, never a plan
    assert row["params"]["dataset"] == "OPRA.PILLAR"


def test_intent_writes_exactly_one_submitted_event(client):
    request_id = _submit(client, INTENT_BODY).json()["request_id"]
    events = client.get(f"/v1/requests/{request_id}/events", headers=auth()).json()["items"]
    assert [e["event"] for e in events] == ["submitted"]


def _mutate(**changes):
    body = copy.deepcopy(INTENT_BODY)
    body.update(changes)
    return body


@pytest.mark.parametrize(
    ("changes", "constraint", "fragment"),
    [
        ({}, "bounded_window", "window"),
        ({}, "named_dataset", "dataset"),
        ({}, "finite_max_usd", "max_usd"),
        ({}, "human_why", "why"),
    ],
)
def test_each_of_the_four_constraints_fails_independently(client, changes, constraint, fragment):
    """Four failing cases, four distinct messages - and each alone."""
    body = copy.deepcopy(INTENT_BODY)
    if constraint == "bounded_window":
        body["intent"]["window"] = {"start": "2026-10-01"}
    elif constraint == "named_dataset":
        body["intent"]["dataset"] = "all"
    elif constraint == "finite_max_usd":
        body["max_usd"] = None
    elif constraint == "human_why":
        body["why"] = "   "

    response = _submit(client, body)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "intent_underconstrained"
    assert error["constraints"] == [constraint]
    assert fragment in error["message"]
    # one constraint failing must be reported alone, not smeared into four
    assert error["message"].count(":") >= 1


def test_end_before_start_is_underconstrained(client):
    body = copy.deepcopy(INTENT_BODY)
    body["intent"]["window"] = {"start": "2026-10-08", "end": "2026-10-01"}
    response = _submit(client, body)
    assert response.status_code == 422
    assert response.json()["error"]["constraints"] == ["bounded_window"]


def test_unlimited_window_word_is_underconstrained(client):
    body = copy.deepcopy(INTENT_BODY)
    body["intent"]["window"] = "everything"
    response = _submit(client, body)
    assert response.status_code == 422
    assert response.json()["error"]["constraints"] == ["bounded_window"]


def test_unlimited_max_usd_word_is_underconstrained(client):
    body = _mutate(max_usd="unlimited")
    response = _submit(client, body)
    assert response.status_code == 422
    assert response.json()["error"]["constraints"] == ["finite_max_usd"]


def test_multiple_failures_are_all_named(client):
    body = {
        "kind": "intent",
        "intent": {"dataset": "all", "schema": "all"},
        "max_usd": -1,
    }
    response = _submit(client, body)
    assert response.status_code == 422
    assert set(response.json()["error"]["constraints"]) == {
        "bounded_window",
        "named_dataset",
        "finite_max_usd",
        "human_why",
    }


def test_intent_with_different_why_is_a_different_request(client):
    first = _submit(client, INTENT_BODY)
    assert first.status_code == 202

    second = _submit(client, _mutate(why="a different stated need"))
    assert second.status_code == 202
    assert second.json()["request_id"] != first.json()["request_id"]
