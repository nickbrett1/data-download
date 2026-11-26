"""The MCP surface: the tools, the protocol, and the scopes that bound them.

Three things are worth a test here, and they are three different claims:

* **The tools work.** Each one is an HTTP call to this same app's `/v1`, so a
  test with a real `Settings` and a real store exercises the same code path an
  agent would -- auth, validation, pricing, the event trail.
* **The mount works.** A `POST /mcp/` that speaks the protocol is the only way
  to know the streamable-HTTP app was mounted *and* its lifespan entered; a
  missing lifespan answers 500 "Task group is not initialized" and every other
  test in this file would still pass.
* **The scopes are the enforcement.** A token with only `read` cannot file,
  because `/v1` says so -- not because the tool chose not to.
"""

from __future__ import annotations

import json

import httpx2
import pytest
from fastapi.testclient import TestClient

from conftest import APPROVERS, CODE_REF, PLAN_BODY, PLAN_TYPES_DIR, TOKENS, fake_quote
from data_download import mcp_surface
from data_download.app import create_app
from data_download.config import Settings, load_settings

# The MCP token is one of the tokens the API already knows, so the tools hold
# exactly the scopes an operator wrote down. `tok-submit` is submit + read.
MCP_TOKEN = "tok-submit"

TOOL_NAMES = [
    "list_requests",
    "get_request",
    "whats_landed",
    "list_plan_types",
    "quote",
    "submit_request",
]


@pytest.fixture
def mcp_settings(db_path, tmp_path):
    return Settings(
        db_path=str(db_path),
        tokens=TOKENS,
        plan_types_dir=PLAN_TYPES_DIR,
        approvers=APPROVERS,
        code_ref=CODE_REF,
        quote=fake_quote,
        datasets_dir=tmp_path / "datasets",
        mcp_token=MCP_TOKEN,
    )


@pytest.fixture
def surface(mcp_settings):
    """The tool layer over an in-process transport -- the production wiring."""
    app = create_app(mcp_settings)
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://data-download",
    )
    with TestClient(app, client=("127.0.0.1", 51234)):
        # The TestClient's `with` is what runs the app's lifespan (store, worker,
        # MCP session manager). The tools talk to the same app object.
        yield mcp_surface.Surface(client, MCP_TOKEN)


def _call(name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": name, "params": arguments}


def _result(response) -> dict:
    """The RPC result out of a streamable-HTTP reply.

    The transport answers `text/event-stream` unless it was built with
    `json_response=True`, so the payload is the last `data:` line of the body
    and `response.json()` raises on the framing rather than on the content.
    """
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()["result"]
    data = [
        line[len("data:") :].strip()
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    assert data, response.text
    return json.loads(data[-1])["result"]


# -- the protocol ---------------------------------------------------------


def test_the_surface_answers_the_protocol_and_lists_its_tools(mcp_settings):
    app = create_app(mcp_settings)
    with TestClient(app, client=("127.0.0.1", 51234)) as client:
        init = client.post(
            "/mcp/",
            json=_call(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            ),
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert init.status_code == 200, init.text
        # The session id is what a streamable-HTTP client sends back on every
        # later call; without it there is no session and no tools.
        session = init.headers.get("mcp-session-id")
        assert session, dict(init.headers)

        listed = client.post(
            "/mcp/",
            json=_call("tools/list", {}),
            headers={
                "Accept": "application/json, text/event-stream",
                "mcp-session-id": session,
            },
        )
        assert listed.status_code == 200, listed.text
        names = [tool["name"] for tool in _result(listed)["tools"]]
        assert names == TOOL_NAMES


def test_no_token_means_no_surface_at_all(settings):
    # `settings` (conftest) has no mcp_token. A surface that answers 401 to
    # everything reads as broken tools, so it is not mounted.
    app = create_app(settings)
    assert not hasattr(app.state, "mcp_app")
    with TestClient(app, client=("127.0.0.1", 51234)) as client:
        assert client.post("/mcp/", json=_call("tools/list", {})).status_code == 404


def test_a_token_the_api_does_not_know_is_refused_at_startup(monkeypatch, caplog):
    env = {
        "DATA_DOWNLOAD_DB": ":memory:",
        "DATA_DOWNLOAD_API_TOKENS": "tok-a:read",
        "DATA_DOWNLOAD_MCP_TOKEN": "tok-a-typo",
    }
    settings = load_settings(env)
    assert settings.mcp_token is None
    assert any("DATA_DOWNLOAD_MCP_TOKEN" in record.message for record in caplog.records)


def test_a_token_the_api_knows_is_kept(monkeypatch):
    env = {
        "DATA_DOWNLOAD_DB": ":memory:",
        "DATA_DOWNLOAD_API_TOKENS": "tok-a:read",
        "DATA_DOWNLOAD_MCP_TOKEN": "tok-a",
    }
    assert load_settings(env).mcp_token == "tok-a"


# -- the tools ------------------------------------------------------------


def test_submit_files_a_draft_and_the_list_shows_what_it_is_for(surface):
    import asyncio

    filed = asyncio.run(surface.submit_request(dict(PLAN_BODY)))
    assert filed["state"] == "draft"
    assert filed["quote_usd"] == pytest.approx(0.42)

    listed = asyncio.run(surface.list_requests(dataset="GLBX.MDP3"))
    assert listed["total"] == 1
    row = listed["items"][0]
    assert row["request_id"] == filed["request_id"]
    # The reason this tool exists: a state and a price do not answer "which
    # days of which dataset is this?".
    assert row["vendor"] == "Databento"
    assert row["dataset"] == "GLBX.MDP3"
    assert row["window"] == {"start": "2026-10-01", "end": "2026-10-03", "days": 2}
    assert row["label"] == "market_data_window"


def test_the_list_filters_by_dataset_and_by_when_it_was_filed(surface, db_path):
    import asyncio
    from datetime import timedelta

    from conftest import iso, seed

    seed(
        db_path,
        [
            {
                "id": "req_old",
                "params_json": json.dumps({"vendor": "Databento", "dataset": "GLBX.MDP3"}),
                "created_at": "2026-11-01T00:00:00Z",
            },
            {
                "id": "req_new",
                "params_json": json.dumps({"vendor": "Databento", "dataset": "OPRA.PILLAR"}),
                "created_at": iso(timedelta(0)),
            },
        ],
    )
    glbx = asyncio.run(surface.list_requests(dataset="GLBX.MDP3"))
    assert [row["request_id"] for row in glbx["items"]] == ["req_old"]
    recent = asyncio.run(surface.list_requests(since=iso(timedelta(days=-1))))
    assert [row["request_id"] for row in recent["items"]] == ["req_new"]
    # `until` is exclusive: nothing was filed *before* the old row.
    assert asyncio.run(surface.list_requests(until="2026-11-01T00:00:00Z"))["total"] == 0


def test_get_request_carries_the_trail_in_sentences(surface):
    import asyncio

    filed = asyncio.run(surface.submit_request(dict(PLAN_BODY)))
    detail = asyncio.run(surface.get_request(filed["request_id"]))
    assert detail["request"]["state"] == "draft"
    assert detail["events_total"] == 1
    assert detail["events"][0]["event"] == "submitted"
    # The machine name is the record; the sentence is what a model reads.
    assert detail["events"][0]["summary"] == "filed as a plan"


def test_whats_landed_is_empty_rather_than_absent_before_anything_runs(surface):
    import asyncio

    body = asyncio.run(surface.whats_landed())
    assert body["configured"] is True
    assert body["vendors"] == []
    assert body["totals"]["requests"] == 0
    assert body["totals"]["driver_jobs_open"] == 0


def test_quote_prices_a_filed_row_and_refuses_one_with_no_job(surface, db_path):
    import asyncio

    import pytest as _pytest
    from mcp.server.mcpserver.exceptions import ToolError

    filed = asyncio.run(surface.submit_request(dict(PLAN_BODY)))
    priced = asyncio.run(surface.quote(filed["request_id"]))
    assert priced["estimate_usd"] == pytest.approx(0.42)
    assert priced["currency"] == "USD"

    # A blocked intent has no Job, so there is no number to give -- an answer of
    # zero would read as "free".
    from conftest import seed

    seed(db_path, [{"id": "req_blocked", "state": "blocked", "job_json": None,
                    "estimate_usd": None, "blocked_reason": "awaiting_plan"}])
    with _pytest.raises(ToolError) as refused:
        asyncio.run(surface.quote("req_blocked"))
    assert "quote_unavailable" in str(refused.value)


def test_a_read_only_token_cannot_file_and_the_refusal_says_so(db_path, tmp_path):
    import asyncio

    import pytest as _pytest
    from mcp.server.mcpserver.exceptions import ToolError

    settings = Settings(
        db_path=str(db_path),
        tokens=TOKENS,
        plan_types_dir=PLAN_TYPES_DIR,
        approvers=APPROVERS,
        code_ref=CODE_REF,
        quote=fake_quote,
        datasets_dir=tmp_path / "datasets",
        mcp_token="tok-read",
    )
    app = create_app(settings)
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://data-download"
    )
    with TestClient(app, client=("127.0.0.1", 51234)):
        read_only = mcp_surface.Surface(client, "tok-read")
        # Reading is allowed...
        assert asyncio.run(read_only.list_requests())["total"] == 0
        # ...filing is not, and the tool passes the API's own sentence through
        # with a hint that names the setting an operator has to change.
        with _pytest.raises(ToolError) as refused:
            asyncio.run(read_only.submit_request(dict(PLAN_BODY)))
    assert "insufficient_scope" in str(refused.value)
    assert "DATA_DOWNLOAD_API_TOKENS" in str(refused.value)


def test_a_ceiling_refusal_is_readable(surface):
    import asyncio

    import pytest as _pytest
    from mcp.server.mcpserver.exceptions import ToolError

    body = {**PLAN_BODY, "max_usd": 0.01}
    with _pytest.raises(ToolError) as refused:
        asyncio.run(surface.submit_request(body))
    assert "over_ceiling" in str(refused.value)
    assert "Raise max_usd" in str(refused.value)


def test_an_idempotency_key_makes_a_second_file_a_conflict(surface):
    import asyncio

    import pytest as _pytest
    from mcp.server.mcpserver.exceptions import ToolError

    first = asyncio.run(surface.submit_request(dict(PLAN_BODY), idempotency_key="ticket-1"))
    with _pytest.raises(ToolError) as refused:
        asyncio.run(surface.submit_request(dict(PLAN_BODY), idempotency_key="ticket-1"))
    assert "duplicate_request" in str(refused.value)
    assert first["request_id"] in str(refused.value)


def test_the_tools_never_offer_a_way_to_approve_or_run(surface):
    """The one invariant a reader should be able to check by looking.

    Approval is a tailnet identity, not a credential any tool holds, so a tool
    that could approve would make the allowlist decorative.
    """
    import asyncio

    import pytest as _pytest
    from mcp.server.mcpserver.exceptions import ToolError

    assert not hasattr(surface, "approve")
    assert not hasattr(surface, "run")
    # And the route itself refuses a token even with every other scope: `read`
    # is not `approve`, and there is no scope that is.
    with _pytest.raises(ToolError) as refused:
        asyncio.run(
            surface._call("POST", f"/v1/requests/{'req_' + '0' * 32}/approve", json={})
        )
    # The refusal is `not_attested`, not `insufficient_scope`: even the token
    # with every scope on this deployment is turned away, because what approve
    # wants is an identity (`Tailscale-User-Login` from the Serve hop) and no
    # header a client can set will do.
    assert "not_attested" in str(refused.value)
    assert "Tailscale-User-Login" in str(refused.value)


def test_a_tool_call_can_say_who_is_filing(surface):
    """`filed_by` travels from the tool to the row a reader looks at.

    The point of the field is that a row says where it came from, and the MCP
    path is the one that needs it most: an agent filing through the hub is not
    distinguishable from any other caller by token.
    """
    import asyncio

    filed = asyncio.run(
        surface.submit_request(
            dict(PLAN_BODY), filed_by="data-sourcing-agent"
        )
    )
    detail = asyncio.run(surface.get_request(filed["request_id"]))
    assert detail["request"]["filed_by"] == "data-sourcing-agent"


def test_an_unfiled_body_is_told_to_ask_the_sourcing_agent(surface):
    """The refusals a caller cannot repair by editing JSON name who can.

    A body the vocabulary cannot express is a *shape* problem, and the repair is
    a conversation with the agent that knows the vendor's vocabulary -- not a
    second guess typed into `params`.
    """
    import asyncio

    import pytest as _pytest
    from mcp.server.mcpserver.exceptions import ToolError

    with _pytest.raises(ToolError) as refused:
        asyncio.run(
            surface.submit_request(
                {
                    "kind": "intent",
                    "intent": {"dataset": "GLBX.MDP3"},
                    "max_usd": 5,
                    "why": "under-specified on purpose",
                }
            )
        )
    assert "data-sourcing agent" in str(refused.value)
