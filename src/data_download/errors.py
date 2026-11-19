"""The closed error set of §7.4, and the envelope clients branch on."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """An error that is meant to reach the caller as `{"error": {...}}`."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def envelope(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


# Phase 1 implements exactly these. The rest of §7.4's table is unreachable
# this phase and must not be invented.
INVALID_BODY = "invalid_body"
UNAUTHENTICATED = "unauthenticated"
INSUFFICIENT_SCOPE = "insufficient_scope"
DUPLICATE_REQUEST = "duplicate_request"
PLAN_NOT_VALID = "plan_not_valid"
PLAN_TYPE_UNKNOWN = "plan_type_unknown"
INTENT_UNDERCONSTRAINED = "intent_underconstrained"
STORE_UNAVAILABLE = "store_unavailable"

# Phase 4, and both are submit-time codes because both refuse a body before a
# row exists (§7.4). `quote_unavailable` is not "the vendor is down" but "this
# request cannot be priced", and an unpriced request is not approvable: the
# human is being asked to authorise a number, so no number means no request.
QUOTE_UNAVAILABLE = "quote_unavailable"
OVER_CEILING = "over_ceiling"

# Not one of §7.4's submit-time codes. A read for an id that does not exist has
# to answer something, and the closed set is about what a *submit* can be
# refused with; `not_found` is the read-surface answer.
NOT_FOUND = "not_found"

# Not §7.4's either. `approve` is the one verb that spends, so its refusals get
# their own names rather than being folded into `unauthenticated`: "no token"
# and "no tailnet identity" are different repairs, and a reader of the response
# should not have to guess which one they are looking at.
NOT_ATTESTED = "not_attested"
NOT_AN_APPROVER = "not_an_approver"


def invalid_body(message: str) -> ApiError:
    return ApiError(400, INVALID_BODY, message)


def unauthenticated(message: str = "a bearer token is required") -> ApiError:
    return ApiError(401, UNAUTHENTICATED, message)


def insufficient_scope(required: str, granted: frozenset[str]) -> ApiError:
    return ApiError(
        403,
        INSUFFICIENT_SCOPE,
        f"token lacks the '{required}' scope (granted: {', '.join(sorted(granted)) or 'none'})",
    )


def not_attested(peer: str | None = None, had_login: bool = False) -> ApiError:
    """Refuse, and say which of the two hops is missing.

    "Not through Serve" has three causes that used to share one sentence: the
    header never arrived (the page was reached by IP), the header arrived from a
    peer that is not the Serve hop (a proxy, a sibling container, a rebound
    domain), or the peer *is* Serve but its address is not on the trusted list
    (in Docker, the port forward re-originates the connection, so Serve's
    loopback connection reaches the container from the bridge gateway). Naming
    the peer is what makes the third case one env var instead of a mystery.
    """
    if had_login:
        message = (
            f"a Tailscale-User-Login header arrived from {peer or 'an unknown peer'}, "
            "which is not a hop this service trusts, so its identity is not "
            "believed. If that address is `tailscale serve` in front of this "
            "container, name it in DATA_DOWNLOAD_TRUSTED_PROXIES."
        )
    else:
        message = (
            f"no Tailscale-User-Login header: this request arrived from "
            f"{peer or 'an unknown peer'} directly, not through `tailscale serve`. "
            "Open the page at the serve URL (https://<node>.<tailnet>.ts.net), "
            "not the host and port."
        )
    return ApiError(403, NOT_ATTESTED, message, peer=peer)


def not_an_approver() -> ApiError:
    # The login is deliberately not echoed: it is header input, and the operator
    # can see their own name in `tailscale status` without this response
    # repeating an unvetted string back at them.
    return ApiError(
        403,
        NOT_AN_APPROVER,
        "this tailnet login is not named in DATA_DOWNLOAD_APPROVERS, "
        "so it may read but not approve",
    )


def duplicate_request(request_id: str) -> ApiError:
    # The existing id rides along so a retrying client can carry on (§7.4).
    return ApiError(
        409,
        DUPLICATE_REQUEST,
        "a request with this payload already exists",
        request_id=request_id,
    )


def plan_not_valid(message: str, **extra: Any) -> ApiError:
    return ApiError(422, PLAN_NOT_VALID, message, **extra)


def plan_type_unknown(plan_type: object, status: int = 422) -> ApiError:
    return ApiError(
        status,
        PLAN_TYPE_UNKNOWN,
        f"plan_type {plan_type!r} is not in the registry; "
        "file an intent if no plan type covers this source",
    )


def intent_underconstrained(message: str, constraints: list[str]) -> ApiError:
    return ApiError(
        422,
        INTENT_UNDERCONSTRAINED,
        message,
        constraints=constraints,
    )


def store_unavailable(message: str = "the request store is unavailable") -> ApiError:
    return ApiError(503, STORE_UNAVAILABLE, message)


def quote_unavailable(message: str) -> ApiError:
    """This request cannot be priced (§5.2).

    Not a 503: the service is fine, this body has no number, and a retry with
    the same body will fail the same way until the cause (a missing key, a Job
    with no quote step) is repaired.
    """
    return ApiError(422, QUOTE_UNAVAILABLE, message)


def over_ceiling(estimate_usd: float, max_usd: float) -> ApiError:
    """The quote is above the ceiling the caller set (§5.2).

    Both numbers ride along so the caller can see what happened without a
    second call: this is a refusal to *authorise*, not a refusal to price, and
    the repair is either a smaller window or a larger `max_usd`.
    """
    return ApiError(
        422,
        OVER_CEILING,
        f"this request prices at ${estimate_usd:.4f}, above its "
        f"ceiling of ${max_usd:.4f}",
        estimate_usd=estimate_usd,
        max_usd=max_usd,
    )


def not_found(message: str) -> ApiError:
    return ApiError(404, NOT_FOUND, message)


def approve_refused(reason: str, state: object) -> ApiError:
    """A refusal from `Store.approve`, carrying the CLI's closed set of reasons.

    `blocked` means there is no Job to approve (§7.9.2); `wrong_state` means the
    row is history; `job_missing` is a `draft` with no `job_json`, which the
    CHECK forbids and a hand-edited store can still hold. The reason is the same
    vocabulary the CLI prints, so one refusal has one name whichever door it
    came through.
    """
    messages = {
        "blocked": "this request has no Job; there is nothing to approve",
        "wrong_state": "this request is not waiting for approval",
        "job_missing": "this request has no stored Job to approve",
    }
    return ApiError(
        409,
        "approve_refused",
        messages.get(reason, "the request could not be approved"),
        reason=reason,
        state=state,
    )
