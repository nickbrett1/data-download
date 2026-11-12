"""Bearer-token authentication and server-side scope enforcement (§7.3).

Three scopes exist in the design. This phase uses `submit` and `read`; `approve`
is never granted by any endpoint and `deploy` has no verb yet. A token's scopes
come from `DATA_DOWNLOAD_API_TOKENS`; the check is here, not by convention.
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


def _token_from_query(request: Request) -> str | None:
    token = request.query_params.get("token")
    return token.strip() if token and token.strip() else None


def authenticate(request: Request, *, allow_query_token: bool = False) -> tuple[str, frozenset[str]]:
    token = _token_from_header(request)
    if token is None and allow_query_token:
        # Browser links (the dashboard, the Homepage tile) cannot set a header.
        token = _token_from_query(request)
    if token is None:
        raise errors.unauthenticated()
    scopes = request.app.state.settings.scopes_for(token)
    if scopes is None:
        raise errors.unauthenticated("the bearer token is not recognised")
    return token, scopes


def require_scope(
    request: Request, scope: str, *, allow_query_token: bool = False
) -> tuple[str, frozenset[str]]:
    token, scopes = authenticate(request, allow_query_token=allow_query_token)
    if scope not in scopes:
        raise errors.insufficient_scope(scope, scopes)
    return token, scopes
