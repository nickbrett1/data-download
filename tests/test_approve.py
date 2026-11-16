"""Approval: `draft` -> `approved`, one human event, and nothing else.

The state machine starts moving here. Approval of a `blocked` row is refused
because there is no Job to approve -- that row is waiting on a plan, not on a
person (§7.9.4).

Approval began as a CLI verb with no route at all (phase 3, §7.4). It now has
both, deliberately: see the comment above the HTTP tests for why the argument
against a route stopped holding, and what of it was kept.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import INTENT_BODY, PLAN_BODY, REPO_ROOT, auth, seed
from data_download.db import ApproveRefused, Store


def _run(coro):
    return asyncio.run(coro)


def _store(path: Path) -> Store:
    store = Store(str(path))
    _run(store.connect())
    return store


def _file(client, body=None) -> str:
    response = client.post("/v1/requests", json=body or PLAN_BODY, headers=auth())
    assert response.status_code == 202, response.text
    return response.json()["request_id"]


def _events(path: Path, request_id: str) -> list[dict]:
    store = _store(path)

    async def go():
        try:
            return await store.events_for(request_id)
        finally:
            await store.close()

    return _run(go())


def test_a_draft_becomes_approved_with_one_human_event(client, settings):
    request_id = _file(client)
    store = _store(settings.db_path)

    async def go():
        try:
            return await store.approve(request_id, "nick", "cheap window")
        finally:
            await store.close()

    row, changed = _run(go())
    assert changed is True
    assert row["state"] == "approved"

    events = _events(settings.db_path, request_id)
    approved = [e for e in events if e["event"] == "approved"]
    assert len(approved) == 1
    assert approved[0]["actor_kind"] == "human"
    detail = json.loads(approved[0]["detail_json"])
    assert detail["actor"] == "nick"
    assert detail["note"] == "cheap window"
    # The approval is of a Job, not of a body (§5.7's audit identity).
    assert detail["job_hash"] == row["job_hash"]


def test_approving_twice_writes_no_second_event(client, settings):
    request_id = _file(client)
    store = _store(settings.db_path)

    async def go():
        try:
            return (
                await store.approve(request_id, "nick"),
                await store.approve(request_id, "someone-else", "again"),
            )
        finally:
            await store.close()

    (_, first), (row, second) = _run(go())
    assert first is True
    assert second is False
    assert row["state"] == "approved"
    assert len([e for e in _events(settings.db_path, request_id) if e["event"] == "approved"]) == 1


def test_approval_does_not_touch_the_job(client, settings):
    request_id = _file(client)
    before = client.get(f"/v1/requests/{request_id}", headers=auth()).json()
    store = _store(settings.db_path)

    async def go():
        try:
            await store.approve(request_id, "nick")
        finally:
            await store.close()

    _run(go())
    after = client.get(f"/v1/requests/{request_id}", headers=auth()).json()
    assert after["job"] == before["job"]
    assert after["job_hash"] == before["job_hash"]


def test_a_blocked_row_cannot_be_approved(client, settings):
    """There is no Job; approval would approve nothing."""
    request_id = _file(client, INTENT_BODY)
    store = _store(settings.db_path)

    async def go():
        try:
            await store.approve(request_id, "nick")
        finally:
            await store.close()

    with pytest.raises(ApproveRefused) as caught:
        _run(go())
    assert caught.value.reason == "blocked"
    assert client.get(f"/v1/requests/{request_id}", headers=auth()).json()["state"] == "blocked"


def test_a_settled_row_is_history_and_is_refused(client, settings):
    seed(settings.db_path, [{"id": "req_done", "state": "succeeded", "job_json": "{}"}])
    store = _store(settings.db_path)

    async def go():
        try:
            await store.approve("req_done", "nick")
        finally:
            await store.close()

    with pytest.raises(ApproveRefused) as caught:
        _run(go())
    assert caught.value.reason == "wrong_state"
    assert caught.value.state == "succeeded"


def test_an_unknown_id_is_not_found(settings):
    store = _store(settings.db_path)

    async def go():
        try:
            await store.approve("req_nope", "nick")
        finally:
            await store.close()

    with pytest.raises(ApproveRefused) as caught:
        _run(go())
    assert caught.value.reason == "not_found"


def test_the_cli_exit_codes_are_the_contract(client, settings):
    """0 approved, 0 again (no-op), 1 refused, 2 not found."""

    def approve(request_id: str, actor: str = "nick") -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "data_download.approve",
                request_id,
                "--actor",
                actor,
                "--db",
                str(settings.db_path),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    request_id = _file(client)
    first = approve(request_id)
    assert first.returncode == 0, first.stderr
    assert "approved" in first.stdout

    second = approve(request_id)
    assert second.returncode == 0, second.stderr
    assert "already approved" in second.stdout
    assert len([e for e in _events(settings.db_path, request_id) if e["event"] == "approved"]) == 1

    blocked_id = _file(client, INTENT_BODY)
    refused = approve(blocked_id)
    assert refused.returncode == 1
    assert "approve_refused: blocked" in refused.stderr

    missing = approve("req_nope")
    assert missing.returncode == 2
    assert "not_found" in missing.stderr


# -- the route, and the argument it overrules ------------------------------
#
# `test_the_cli_is_not_a_route` used to assert that no approve path existed at
# all (§7.4, §7.8.3). That argument rested on a read surface that opened the
# store `mode=ro` in a credential-free container, so a button on it could not
# have approved anything. That container was never built -- the read surface is
# the writer's own process -- so the choice became "a route with a scope check
# or a `docker exec` with no authz", and the shell command lost.


def _approve_http(client, request_id, **kwargs):
    body = {"actor": "nick", **kwargs}
    return client.post(
        f"/v1/requests/{request_id}/approve",
        json=body,
        headers=auth("tok-approve"),
    )


def test_the_route_approves_a_draft_and_records_a_human_event(client, settings):
    request_id = _file(client)
    before = client.get(f"/v1/requests/{request_id}", headers=auth()).json()

    response = _approve_http(client, request_id, note="cheap window")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "request_id": request_id,
        "state": "approved",
        "job_hash": before["job_hash"],
        "approved": True,
    }

    approved = [e for e in _events(settings.db_path, request_id) if e["event"] == "approved"]
    assert len(approved) == 1
    assert approved[0]["actor_kind"] == "human"
    detail = json.loads(approved[0]["detail_json"])
    assert detail["actor"] == "nick"
    assert detail["note"] == "cheap window"
    assert detail["job_hash"] == before["job_hash"]
    # How the approval arrived, without either credential being stored.
    assert detail["via"] == "http"
    assert len(detail["token"]) == 12


def test_the_fingerprint_is_not_the_token(client, settings):
    request_id = _file(client)
    assert _approve_http(client, request_id).status_code == 200
    approved = [e for e in _events(settings.db_path, request_id) if e["event"] == "approved"]
    assert "tok-approve" not in approved[0]["detail_json"]


def test_a_second_approval_through_the_route_writes_nothing(client, settings):
    request_id = _file(client)
    assert _approve_http(client, request_id).json()["approved"] is True
    again = _approve_http(client, request_id)
    assert again.status_code == 200
    assert again.json()["approved"] is False
    assert len([e for e in _events(settings.db_path, request_id) if e["event"] == "approved"]) == 1


@pytest.mark.parametrize("token", ["tok-submit", "tok-read", "tok-escalate"])
def test_only_the_approve_scope_can_approve(client, token):
    """The route is a machine door on a human key: no other scope opens it."""
    request_id = _file(client)
    response = client.post(
        f"/v1/requests/{request_id}/approve",
        json={"actor": "nick"},
        headers=auth(token),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "insufficient_scope"
    assert client.get(f"/v1/requests/{request_id}", headers=auth()).json()["state"] == "draft"


def test_the_route_needs_a_token_at_all(client):
    request_id = _file(client)
    response = client.post(
        f"/v1/requests/{request_id}/approve", json={"actor": "nick"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_a_bare_token_never_gets_approve():
    """The invariant behind the route: an unnamed token is a machine token, and
    a machine token cannot approve (§7.3, `config.DEFAULT_SCOPES`)."""
    from data_download.config import parse_tokens

    assert "approve" not in parse_tokens("a-bare-token")["a-bare-token"]


@pytest.mark.parametrize("body", [{}, {"actor": "   "}, {"actor": 7}])
def test_an_approval_without_an_actor_is_refused(client, body):
    """The token says someone holds the key; the actor says who. An approval
    with neither is an anonymous spend, so it is refused before the store."""
    request_id = _file(client)
    response = client.post(
        f"/v1/requests/{request_id}/approve",
        json=body,
        headers=auth("tok-approve"),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_body"
    assert client.get(f"/v1/requests/{request_id}", headers=auth()).json()["state"] == "draft"


def test_the_route_refuses_a_blocked_row_with_the_cli_vocabulary(client, settings):
    request_id = _file(client, INTENT_BODY)
    response = _approve_http(client, request_id)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "approve_refused"
    assert error["reason"] == "blocked"
    assert error["state"] == "blocked"


def test_the_route_404s_an_unknown_id(client):
    response = _approve_http(client, "req_nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
