"""Bearer-token authentication and server-side scope enforcement (§7.3).

The design names three scopes; `escalate` is the fourth (phase 2.5) and exists
because asking for a new plan is a different act from filing a request.

`approve` was the fifth and the only one that spends. It guarded a CLI verb for
all of phase 3 and then a route (`POST /v1/requests/{id}/approve`), and it is
now **retired**: the scope is still parseable so an old `.env` starts, and it
gates nothing. Approval is authorised by identity instead -- see
`require_approver` below, and the argument for the swap in the main README.
`deploy` has no verb yet.

A token's scopes come from `DATA_DOWNLOAD_API_TOKENS`; the check is here, not by
convention.

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

import ipaddress

from fastapi import Request

from . import errors

# The header `tailscale serve` adds, and the only kind of peer that can be
# trusted to have added it. Serve proxies to a local port, so the hop that
# asserts identity is this process's own loopback -- except inside a container,
# where Docker's port forward re-originates the connection and the container
# sees the bridge gateway instead. That address is what
# `DATA_DOWNLOAD_TRUSTED_PROXIES` is for; the default trusts nothing but
# loopback, and an address is only worth adding if nothing else can originate
# from it.
LOGIN_HEADER = "tailscale-user-login"


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


def _addr(host: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not host:
        return None
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None
    # A v6 socket reports an IPv4 peer in mapped form; compare what it means.
    if addr.version == 6 and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_trusted(host: str | None, networks: tuple) -> bool:
    """Is this peer a hop allowed to assert identity?

    Compared as addresses, not strings: `127.0.0.1` and `127.0.0.2` are both
    loopback, and a proxy may connect from any address in its range.
    """
    addr = _addr(host)
    return addr is not None and any(addr in net for net in networks)


def tailnet_login(request: Request, settings=None) -> str | None:
    """The tailnet identity behind this request, or None.

    None is the answer to several different questions -- no header, an
    untrusted peer, no tailnet at all -- because from here they are one fact:
    nobody vouched for this request. `require_approver` asks the questions
    separately so its refusal can name the one that failed.
    """
    settings = settings or request.app.state.settings
    peer = request.client.host if request.client else None
    if not _is_trusted(peer, settings.trusted_proxies):
        return None
    return request.headers.get(LOGIN_HEADER, "").strip() or None


def require_approver(request: Request) -> str:
    """Who is spending, or a refusal.

    Two doors, and neither is a token: the request must have arrived through
    `tailscale serve` (loopback + header), and that login must be named in
    `DATA_DOWNLOAD_APPROVERS`. The allowlist is the part that replaces the
    token: a tailnet is a network, not a person, and a shared-in device gets a
    perfectly valid identity of its own.
    """
    settings = request.app.state.settings
    login = tailnet_login(request, settings)
    if login is None:
        peer = request.client.host if request.client else None
        had_login = bool(request.headers.get(LOGIN_HEADER, "").strip())
        raise errors.not_attested(peer, had_login)
    approvers = settings.approvers
    if login.lower() not in approvers:
        raise errors.not_an_approver()
    return login
