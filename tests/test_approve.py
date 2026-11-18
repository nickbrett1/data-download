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
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from conftest import INTENT_BODY, PLAN_BODY, REPO_ROOT, auth, seed, served
from data_download.app import create_app
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


# -- the route, and the argument it kept overruling ------------------------
#
# `test_the_cli_is_not_a_route` used to assert that no approve path existed at
# all (§7.4, §7.8.3). That argument rested on a read surface that opened the
# store `mode=ro` in a credential-free container, so a button on it could not
# have approved anything. That container was never built -- the read surface is
# the writer's own process -- so the choice became "a route with an authz check
# or a `docker exec` with none", and the shell command lost.
#
# The check itself has moved twice: from nothing, to an `approve` token no agent
# held, and now to identity -- the login `tailscale serve` asserts on the
# loopback hop. A token proved *someone* held the key; this proves *who* is
# asking, and a page in the approver's browser cannot borrow it.


def _approve_http(client, request_id, **kwargs):
    # No token and no actor: who is asking comes from the hop, not the body.
    return client.post(
        f"/v1/requests/{request_id}/approve",
        json=kwargs,
        headers=served(),
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
    # The login Serve asserted, not a name typed into a form.
    assert detail["actor"] == "nick@example.com"
    assert detail["note"] == "cheap window"
    assert detail["job_hash"] == before["job_hash"]
    # How the approval arrived. There is no credential to fingerprint now.
    assert detail["via"] == "serve"
    assert "token" not in detail


def test_a_second_approval_through_the_route_writes_nothing(client, settings):
    request_id = _file(client)
    assert _approve_http(client, request_id).json()["approved"] is True
    again = _approve_http(client, request_id)
    assert again.status_code == 200
    assert again.json()["approved"] is False
    assert len([e for e in _events(settings.db_path, request_id) if e["event"] == "approved"]) == 1


@pytest.mark.parametrize(
    "peer, header, expected",
    [
        # The Serve hop, and the two spellings a loopback peer can have.
        ("127.0.0.1", "nick@example.com", "nick@example.com"),
        ("::1", "nick@example.com", "nick@example.com"),
        ("::ffff:127.0.0.1", "nick@example.com", "nick@example.com"),
        # No header: nothing was asserted.
        ("127.0.0.1", "", None),
        # A header, from a peer that could have written it itself. The tailnet
        # address is the case that matters: that is what a curl on the tailnet,
        # a sibling container, or a rebound domain would look like.
        ("100.64.0.9", "nick@example.com", None),
        ("192.168.1.5", "nick@example.com", None),
        (None, "nick@example.com", None),
    ],
)
def test_the_identity_header_only_counts_from_loopback(peer, header, expected):
    """The header is the identity only when the peer that sent it is the local
    Serve hop. From anywhere else it is a string the caller wrote."""
    from data_download import auth

    request = type(
        "Req",
        (),
        {
            "client": type("Client", (), {"host": peer})(),
            "headers": {"tailscale-user-login": header} if header else {},
        },
    )()
    assert auth.tailnet_login(request) == expected


def test_a_direct_request_is_refused_with_the_repair_in_the_message(client):
    request_id = _file(client)
    response = client.post(f"/v1/requests/{request_id}/approve", json={})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "not_attested"
    assert "tailscale serve" in response.json()["error"]["message"]
    assert client.get(f"/v1/requests/{request_id}", headers=auth()).json()["state"] == "draft"


def test_a_tailnet_login_not_on_the_allowlist_may_read_but_not_spend(client, settings):
    """A tailnet is a network, not a person: a shared-in device arrives with a
    perfectly valid identity of its own, and a valid identity is not an
    approver."""
    request_id = _file(client)
    response = client.post(
        f"/v1/requests/{request_id}/approve",
        json={},
        headers=served("someone-else@example.com"),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "not_an_approver"
    # Not reflected back at the caller: it is header input.
    assert "someone-else@example.com" not in response.text
    assert client.get(f"/v1/requests/{request_id}", headers=auth()).json()["state"] == "draft"


def test_nobody_may_approve_until_the_allowlist_names_somebody(client, settings):
    """Fail closed. An env without `DATA_DOWNLOAD_APPROVERS` is a door that
    refuses everybody, not a door that trusts the tailnet."""
    app = create_app(replace(settings, approvers=frozenset()))
    with TestClient(app, client=("127.0.0.1", 51234)) as locked:
        request_id = _file(locked)
        response = locked.post(
            f"/v1/requests/{request_id}/approve", json={}, headers=served()
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "not_an_approver"


def test_the_login_is_matched_case_insensitively(client):
    """An IdP that changes the case of a login must not lock the only approver
    out of the door."""
    request_id = _file(client)
    response = client.post(
        f"/v1/requests/{request_id}/approve",
        json={},
        headers=served("Nick@Example.com"),
    )
    assert response.status_code == 200, response.text


def test_the_route_refuses_an_actor_in_the_body(client):
    """Identity comes from the hop. A second, self-declared one in the body is
    refused rather than ignored: one way in, so one thing to audit."""
    request_id = _file(client)
    response = client.post(
        f"/v1/requests/{request_id}/approve",
        json={"actor": "somebody-else"},
        headers=served(),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_body"
    assert "tailscale serve" in response.json()["error"]["message"]


def test_whoami_names_the_login_and_whether_it_may_spend(client):
    body = client.get("/v1/whoami", headers=served()).json()
    assert body == {"login": "nick@example.com", "approver": True}

    # Signed in, not an approver: an answer, not an error -- the page needs to
    # say why the button is off, not show a failure.
    stranger = client.get("/v1/whoami", headers=served("someone-else@example.com"))
    assert stranger.status_code == 200
    assert stranger.json() == {"login": "someone-else@example.com", "approver": False}

    # Not through Serve at all: nothing to name.
    direct = client.get("/v1/whoami")
    assert direct.status_code == 403
    assert direct.json()["error"]["code"] == "not_attested"


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
