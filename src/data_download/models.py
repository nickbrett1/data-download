"""Body validation: shape only, never interpretation.

A plan is checked for being a well-formed instance of a declared type. It is
not resolved, executed, or judged correct. Phase 2 reconciles the registry
against §3's closed step vocabulary.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from jsonschema import Draft202012Validator

from . import errors
from .config import finite_positive
from .registry import ON_FAILURE_VALUES, PlanType

# A word that names a universe rather than a dataset.
UNNAMED = {"all", "any", "*", "everything", "whatever", "unknown", ""}

# Constraint names are part of the 422 payload so a client can branch on which
# of §7.9.1's four failed, without parsing prose.
C_WINDOW = "bounded_window"
C_DATASET = "named_dataset"
C_MAX_USD = "finite_max_usd"
C_WHY = "human_why"

CONSTRAINT_MESSAGES = {
    C_WINDOW: "bounded_window: intent.window must have absolute `start` and `end`, with end > start",
    C_DATASET: "named_dataset: intent.dataset and intent.schema must both name something",
    C_MAX_USD: "finite_max_usd: max_usd must be a finite number greater than 0",
    C_WHY: "human_why: why must be non-empty free text from the person asking",
}


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _field_path(error) -> str:
    path = "params"
    for item in error.absolute_path:
        path += f"[{item}]" if isinstance(item, int) else f".{item}"
    return path


def _validate_params(plan_type: PlanType, params: dict[str, Any]) -> None:
    validator = Draft202012Validator(plan_type.params_schema)
    failures = sorted(
        validator.iter_errors(params), key=lambda e: list(e.absolute_path)
    )
    if not failures:
        return
    first = failures[0]
    path = _field_path(first)
    raise errors.plan_not_valid(
        f"{path}: {first.message}",
        plan_type=plan_type.name,
        field=path,
    )


def validate_plan(body: dict[str, Any], plan_type: PlanType) -> dict[str, Any]:
    """Validate the plan body against its type, returning the normalised plan."""
    raw_type = body.get("plan_type")
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise errors.plan_not_valid("plan_type is required and must be a non-empty string")

    params = body.get("params")
    if not isinstance(params, dict):
        raise errors.plan_not_valid("params is required and must be an object")

    _validate_params(plan_type, params)

    verify = body.get("verify")
    if plan_type.verify_required and verify is None:
        raise errors.plan_not_valid(
            "verify is required: declare verify.on_failure as 'commit_marked' or 'quarantine'"
        )
    if verify is not None:
        if not isinstance(verify, dict):
            raise errors.plan_not_valid("verify must be an object")
        on_failure = verify.get("on_failure")
        if on_failure not in ON_FAILURE_VALUES:
            raise errors.plan_not_valid(
                "verify.on_failure must be exactly 'commit_marked' or 'quarantine'"
            )
        if on_failure not in plan_type.on_failure_allowed:
            raise errors.plan_not_valid(
                f"verify.on_failure {on_failure!r} is not allowed for plan_type "
                f"{plan_type.name!r} (allowed: {', '.join(sorted(plan_type.on_failure_allowed))})"
            )

    return {
        "kind": "plan",
        "plan_type": plan_type.name,
        "params": params,
        "verify": verify,
    }


def intent_failures(body: dict[str, Any]) -> list[str]:
    """Which of §7.9.1's four constraints fail; each is independently testable."""
    intent = body.get("intent")
    intent_obj = intent if isinstance(intent, dict) else {}
    failed: list[str] = []

    window = intent_obj.get("window")
    ok = False
    if isinstance(window, dict):
        start = _parse_ts(window.get("start"))
        end = _parse_ts(window.get("end"))
        ok = start is not None and end is not None and end > start
    if not ok:
        failed.append(C_WINDOW)

    dataset = intent_obj.get("dataset")
    schema = intent_obj.get("schema")
    ok = (
        isinstance(dataset, str)
        and dataset.strip().lower() not in UNNAMED
        and isinstance(schema, str)
        and schema.strip().lower() not in UNNAMED
    )
    if not ok:
        failed.append(C_DATASET)

    if not finite_positive(body.get("max_usd")):
        failed.append(C_MAX_USD)

    why = body.get("why")
    if not (isinstance(why, str) and why.strip()):
        failed.append(C_WHY)

    return failed


def validate_intent(body: dict[str, Any]) -> None:
    failed = intent_failures(body)
    if failed:
        raise errors.intent_underconstrained(
            "intent is underconstrained: "
            + "; ".join(CONSTRAINT_MESSAGES[name] for name in failed),
            failed,
        )
