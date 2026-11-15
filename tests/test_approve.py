"""Approval is a verb, not a route (phase 3 order §2).

The state machine starts moving here: `draft` -> `approved`, one human event,
and nothing else. Approval of a `blocked` row is refused because there is no Job
to approve -- that row is waiting on a plan, not on a person (§7.9.4).
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


def test_the_cli_is_not_a_route(client):
    """§7.4: no approve endpoint, and therefore no token that could reach one."""
    paths = {route.path for route in client.app.routes}
    assert not any("approve" in path for path in paths)
