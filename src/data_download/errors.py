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

# Not one of §7.4's submit-time codes. A read for an id that does not exist has
# to answer something, and the closed set is about what a *submit* can be
# refused with; `not_found` is the read-surface answer.
NOT_FOUND = "not_found"


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


def not_found(message: str) -> ApiError:
    return ApiError(404, NOT_FOUND, message)
