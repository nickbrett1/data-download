"""Bearer-token authentication and server-side scope enforcement (§7.3).

The design names three scopes; `escalate` is the fourth (phase 2.5) and exists
because asking for a new plan is a different act from filing a request. Endpoints
use `submit`, `read` and `escalate`; `approve` is never granted by any endpoint
(it is a CLI verb) and `deploy` has no verb yet. A token's scopes
come from `DATA_DOWNLOAD_API_TOKENS`; the check is here, not by convention.

A token comes from the `Authorization` header and from nowhere else. The query
string used to be accepted for the HTML pages, because a browser navigation
cannot set a header -- but the cost was a live credential in every place a URL
is copied to: the Homepage tile's Docker label, the compose file, proxy access
logs, `Referer`s and browser history. The UI is now a public read-only
projection instead, so no browser ever needs a token; this module guards
`/v1/*`, which is where the verbs are. A query token is refused outright: one
way in is one thing to audit.
"""

from __future__ import annotations

from fastapi import Request

from . import errors


def _token_from_header(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    return token or None


def authenticate(request: Request) -> tuple[str, frozenset[str]]:
    token = _token_from_header(request)
    if token is None:
        raise errors.unauthenticated()
    scopes = request.app.state.settings.scopes_for(token)
    if scopes is None:
        raise errors.unauthenticated("the bearer token is not recognised")
    return token, scopes


def require_scope(request: Request, scope: str) -> tuple[str, frozenset[str]]:
    token, scopes = authenticate(request)
    if scope not in scopes:
        raise errors.insufficient_scope(scope, scopes)
    return token, scopes
