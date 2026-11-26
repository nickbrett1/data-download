"""The MCP surface: the same `/v1` doors, for a caller with no browser.

Why this exists. The request book and the datasets page answer "what have I
got?" for a person; an agent asking the same question has no page, and the
alternative -- teaching it `curl` with a bearer token it was pasted -- is a
credential in a prompt. So the tools are thin and the rules stay where they
are:

* **Every tool is an HTTP call to this same process's `/v1` over an in-process
  ASGI transport**, with a token the operator named in
  `DATA_DOWNLOAD_API_TOKENS`. Nothing here opens the store. A second writer
  would be a second copy of pricing, the ceiling, idempotency and the event
  trail, and the copy that drifted would be the one an agent trusted.
* **The scopes are the enforcement.** A deployment that gives the MCP token
  `read` has a read-only surface, whatever these tools are willing to call --
  `auth.require_scope` refuses the rest. That is also why `approve` is not
  here: it is not a scope at all any more, it is the identity `tailscale serve`
  asserts, and no tool can produce one.
* **Mounted only when configured.** `DATA_DOWNLOAD_MCP_TOKEN` unset means no
  `/mcp` at all, rather than a surface that answers 401 to every question and
  reads as "the tools are broken".

The tool descriptions are prompt text -- they are what a model reads before it
decides to call one -- so they say what the tool is *for* and what it will not
do, not just what it returns.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from .config import Settings

log = logging.getLogger(__name__)

# How many events a `get_request` answer carries. A trail can be hundreds of
# rows long and the useful part is the end of it; the whole thing is one call
# away at `/v1/requests/<id>/events` for anything that needs it.
EVENTS_SHOWN = 20


class Surface:
    """The tools, as plain async methods, so a test can call them directly.

    `build` registers these on the MCP server; nothing about them depends on
    the protocol, which is what makes them testable without a transport.
    """

    def __init__(self, client: httpx2.AsyncClient, token: str) -> None:
        self._client = client
        self._token = token

    # -- plumbing ---------------------------------------------------------

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        """One `/v1` call, with the refusal turned into something readable.

        A tool that raises `KeyError: 'error'` on a 403 tells the model nothing;
        the API already writes the sentence, so it is passed through with its
        code, and the two codes an agent can actually fix get a hint.
        """
        headers = {"Authorization": f"Bearer {self._token}", **kwargs.pop("headers", {})}
        response = await self._client.request(
            method,
            path,
            headers=headers,
            **kwargs,
        )
        if response.status_code >= 400:
            raise ToolError(_explain(response))
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    # -- reads ------------------------------------------------------------

    async def list_requests(
        self,
        state: str | None = None,
        plan_type: str | None = None,
        dataset: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List download requests, newest first, with what each one is for.

        Use this to answer "what have we asked for?" and "what is in flight?".
        Each row carries its state, the vendor and dataset code, the window it
        covers (`window.start`, `window.end` exclusive, `window.days`) and the
        price the vendor quoted, so a list is readable without a second call.

        Filters are all optional and combine: `state` (draft, blocked, approved,
        running, succeeded, landed_with_errors, failed, cancelled), `plan_type`,
        `dataset` (a vendor code such as `GLBX.MDP3`), `since` and `until`
        (ISO dates or timestamps against when the request was *filed*; `since`
        is inclusive, `until` exclusive). `limit` caps at 200.

        This is the request book. It does not say whether the bytes are on
        disk -- for that use `whats_landed`.
        """
        params = _compact(
            state=state, plan_type=plan_type, dataset=dataset,
            since=since, until=until, limit=limit,
        )
        return await self._call("GET", "/v1/requests", params=params)

    async def get_request(self, request_id: str) -> dict[str, Any]:
        """One request in full: its state, Job, params, price, and its trail.

        The trail is the last 20 events with a plain-English `summary` beside
        each machine `event` name -- the same sentences the request page shows.
        Repeated identical events are *not* collapsed here: an agent reading a
        run of `driver_state` rows learns how long something was stuck, which
        the page's `×N` hides.

        Use it after submitting (to see whether the row bound a Job or was
        blocked, and why), and before approving (to read what actually ran).
        """
        request = await self._call("GET", f"/v1/requests/{request_id}")
        trail = await self._call("GET", f"/v1/requests/{request_id}/events")
        events = trail.get("items", [])
        return {
            "request": request,
            "events": events[-EVENTS_SHOWN:],
            "events_total": len(events),
            "events_shown": min(len(events), EVENTS_SHOWN),
        }

    async def whats_landed(self) -> dict[str, Any]:
        """What is on disk right now: vendor -> dataset -> request -> files.

        Sizes are three decimal units (`4.1 GB`) because that is what a disk
        reports, `in_flight` counts rows that are approved or running, and a
        request whose directory has gone says `on_disk: false` rather than
        pretending. A `failed` request is deliberately absent: its bytes were
        refused by the verifier, so they are not part of "what I have".

        Use this to answer "do we already have this data?" before filing a
        request for it.
        """
        return await self._call("GET", "/v1/datasets")

    async def quote(self, request_id: str) -> dict[str, Any]:
        """Re-price a filed request against the vendor. Writes nothing.

        A draft is priced when it is filed and the number comes back from
        `submit_request`, so this is for a second opinion later -- after a
        vendor price change, or before deciding whether an old draft is still
        worth approving. It refuses a row with no Job (nothing to price) rather
        than answering zero.

        Note it takes a `request_id`: this deployment cannot price a body that
        has not been filed. Filing is free and lands in `draft`, so the way to
        price a hypothetical is to submit it and read `quote_usd` off the
        answer -- then cancel it if the number is wrong.
        """
        return await self._call("POST", "/v1/quotes", json={"request_id": request_id})

    # -- the one write ----------------------------------------------------

    async def submit_request(
        self,
        body: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """File a download request. It lands in `draft` or `blocked`; it does
        not run, and it does not spend.

        The body is a `plan` (you know the plan type and its parameters) or an
        `intent` (you know what you want and the matcher decides):

            {"kind": "plan", "plan_type": "market_data_window",
             "params": {"vendor": "Databento", "dataset": "GLBX.MDP3",
                        "schema": "trades", "symbols": ["ES.c.0"],
                        "stype_in": "continuous",
                        "start": "2026-10-01", "end": "2026-10-03"},
             "verify": {"on_failure": "quarantine"},
             "max_usd": 5,
             "why": "one sentence on what this is for"}

            {"kind": "intent",
             "intent": {"dataset": "OPRA.PILLAR", "schema": "trades",
                        "window": {"start": "2026-10-01", "end": "2026-10-08"}},
             "max_usd": 120, "why": "..."}

        Always set `max_usd`: it is the ceiling the price is checked against at
        submit, and a plan priced above it is refused rather than filed. Always
        set `why` in one sentence -- it is what the human approving it reads.

        Call `list_plan_types` first if you do not know which `plan_type` and
        `params` a need maps to; a plan body with the wrong parameter names is
        refused with the binder's own message.

        `idempotency_key` (<=200 chars) makes a repeated call return 409 with
        the row that already exists, instead of filing a second identical
        request. Pass something stable for the attempt -- not a random value.

        What happens next is not this tool's business: a human approves on the
        request page (approval is a tailnet identity, not a credential any tool
        holds), and the worker runs it. Poll `get_request` to watch.
        """
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return await self._call("POST", "/v1/requests", json=body, headers=headers)

    async def list_plan_types(self) -> dict[str, Any]:
        """The plan types this deployment accepts, and the fields each one takes.

        Use it to choose a `plan_type` (and its exact parameter names) before
        calling `submit_request`. A plan type is a shape of order -- the
        vocabulary a request must be expressible in -- so a need that matches
        none of them is a need this service cannot file, and saying so is
        better than inventing a body the binder will refuse.
        """
        return await self._call("GET", "/v1/plan-types")


def _compact(**values: Any) -> dict[str, Any]:
    """Query params, minus the ones nobody set. `dataset=None` must not become
    the string `"None"`, which would filter every row away."""
    return {key: value for key, value in values.items() if value not in (None, "")}


def _explain(response: httpx2.Response) -> str:
    """The API's own refusal, as one sentence a model can act on."""
    try:
        envelope = response.json()
    except (json.JSONDecodeError, ValueError):
        envelope = {}
    error = envelope.get("error") if isinstance(envelope, dict) else None
    if not isinstance(error, dict):
        return f"{response.status_code} from data-download: {response.text[:400]}"
    code = str(error.get("code") or "")
    message = str(error.get("message") or "").strip() or response.text[:400]
    # Some refusals carry the row as a field rather than in the sentence -- the
    # duplicate-filing 409 names the request that already exists that way. A
    # model reading only `message` would file again under a new key.
    existing = error.get("request_id")
    if existing and str(existing) not in message:
        message = f"{message} ({existing})"
    hint = {
        "insufficient_scope": (
            " The MCP token lacks this scope; an operator has to add it to "
            "DATA_DOWNLOAD_API_TOKENS."
        ),
        "over_ceiling": " Raise max_usd, or ask for less data.",
        "plan_not_valid": " Check the plan type's parameter names with list_plan_types.",
        "quote_unavailable": (
            " Nothing can be priced right now: no installed driver answers "
            "`cost` for this vendor and no local key is configured."
        ),
        "duplicate_request": (
            " The same Idempotency-Key was already used; read that request "
            "with get_request rather than filing again."
        ),
    }.get(code, "")
    return f"{code or response.status_code}: {message}{hint}"


def transport_security() -> TransportSecuritySettings:
    """The transport's Host allowlist: none, deliberately.

    The SDK turns on DNS-rebinding protection by default and refuses any `Host`
    it was not told about -- `421 Invalid Host header` with no tool ever run.
    That default is for a server a *browser* reaches with an ambient credential
    (a cookie), where an attacker's page rebinds DNS to the victim's localhost
    and the browser presents the cookie for the attacker's hostname. Neither
    half of that holds here: there is no cookie, every tool call carries a
    bearer token that must already be listed in `DATA_DOWNLOAD_API_TOKENS`, and
    a cross-origin page cannot set that header without a preflight this surface
    never grants. So the protection would refuse honest callers and stop no one.

    Refusing honest callers is the concrete harm: the only ingress is
    `tailscale serve`, so the Host is whatever node name the caller reached --
    the node's MagicDNS name, its IP, `localhost` from inside the container,
    `testserver` under `pytest` -- and an allowlist that omits one of them makes
    the surface look broken rather than unreachable. That is the failure mode we
    already refused for an unset token.
    """
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def build(*, settings: Settings, app: Any) -> MCPServer | None:
    """The MCP server, or `None` when this deployment has not configured one.

    The transport is in-process (ASGI) rather than a loopback socket: the tool
    call goes through the real routes -- auth, scopes, validation, the store --
    without needing a port, a URL, or a second listener, and a test gets the
    same path without opening anything.
    """
    if not settings.mcp_token:
        return None

    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://data-download",
        timeout=60.0,
    )
    surface = Surface(client, settings.mcp_token)
    server = MCPServer(
        settings.mcp_name,
        version="0.1.0",
        instructions=(
            "data-download files and tracks archival market-data downloads. "
            "Filing a request costs nothing and runs nothing: it lands in "
            "`draft` or `blocked` and waits for a human to approve it. Use "
            "`whats_landed` before filing, `list_plan_types` to choose a body, "
            "and `get_request` to watch a row. There is no tool that approves "
            "or runs anything, by design."
        ),
    )
    for method in (
        surface.list_requests,
        surface.get_request,
        surface.whats_landed,
        surface.list_plan_types,
        surface.quote,
        surface.submit_request,
    ):
        server.tool()(method)
    log.info(
        "MCP surface at /mcp with %d tools (token scopes: %s)",
        6,
        " ".join(sorted(settings.tokens.get(settings.mcp_token, frozenset()))) or "none",
    )
    return server
