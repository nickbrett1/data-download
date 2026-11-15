"""Does a plan we already have take this intent? Answered mechanically, or not at all.

An intent names a dataset, a schema and a window; it does not name a plan type.
Before this module, that meant every intent landed in `awaiting_plan` whether or
not `plan_types/` could have taken it -- the row could not say *why* it was
waiting, and a caller could not tell "add two fields" apart from "this needs new
code". Those are not the same fact and they have different owners.

The answer here is a **schema comparison**, not a judgement:

* `matched` -- some registered type's `params_schema` accepts what the intent
  supplies. The Job can be bound, so the Request stops waiting.
* `needs_params` -- a type claims this dataset, nothing it was given is wrong,
  and it is only missing required fields. The *caller's* move: supply them.
* `cannot_express` -- a type claims the dataset but rejects a value that **was**
  supplied (outside an enum, wrong shape). More params will not help.
* `not_claimed` -- no registered type claims this dataset code at all.

Everything is pure: no store, no clock, no network. That is what makes the
report reproducible a year later, and it is why `store` keeps the whole thing
(with a digest of the registry it was computed against) rather than a verdict.

**This module never decides that a new plan is needed.** It decides that no
*existing* plan can take the intent, which is a different proposition, and the
only one a program is entitled to make. Requesting new code is the caller's act
(`POST /v1/requests/{id}/escalate`), and it is refused while any candidate is
merely `needs_params`.
"""

from __future__ import annotations

import hashlib
import json
from fnmatch import fnmatch
from typing import Any

from jsonschema import Draft202012Validator

from .registry import PlanType, Registry

MATCHED = "matched"
NEEDS_PARAMS = "needs_params"
CANNOT_EXPRESS = "cannot_express"
NOT_CLAIMED = "not_claimed"

# Verdicts an intent row can carry. `matched` means a Job exists; the other
# three mean it does not, and each names a different owner for the next move.
VERDICTS = (MATCHED, NEEDS_PARAMS, CANNOT_EXPRESS, NOT_CLAIMED)

# Which intent field fills which plan param. Name-identical where the two
# vocabularies agree, plus the one explicit rule: an intent names a `window`,
# a plan names a `start` and an `end`. A plan type whose params share no names
# with the intent vocabulary is not broken -- it simply reports `needs_params`
# for everything it needs, which is visible rather than silent.
_IDENTICAL = ("dataset", "schema", "symbols", "stype_in")


def _fingerprint(entry: PlanType) -> str:
    """Everything about one type that changes what `attempt` would answer.

    The version is the promise, but a promise is not a mechanism: a schema or a
    dataset pattern edited without a bump changes the verdict just the same, and
    a digest that only counted versions would keep certifying the old answer
    (phase 2.6 reconciles on a change of digest, so this is what decides whether
    a row is re-examined).
    """
    payload = {
        "name": entry.name,
        "version": entry.version,
        "params_schema": entry.params_schema,
        "datasets": sorted(entry.datasets),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def registry_digest(registry: Registry) -> str:
    """Which registry this verdict was computed against, in one short string.

    Recorded in every report so an old verdict is not read as a claim about
    today's registry -- and compared at start (phase 2.6) to decide which rows
    are worth re-examining. It covers what the match actually reads: the name,
    the version, the params schema and the claimed dataset patterns.
    """
    pairs = sorted(
        f"{entry.name}:{entry.version}:{_fingerprint(entry)}"
        for entry in registry.all()
    )
    digest = hashlib.sha256("\n".join(pairs).encode("utf-8")).hexdigest()
    return digest[:16]


def candidate_params(intent: dict[str, Any], plan_type: PlanType) -> dict[str, Any]:
    """The params this intent supplies for this type -- and nothing invented."""
    declared = (plan_type.params_schema.get("properties") or {})
    params: dict[str, Any] = {}
    for key in _IDENTICAL:
        if intent.get(key) is not None and key in declared:
            params[key] = intent[key]
    window = intent.get("window")
    if isinstance(window, dict):
        for bound in ("start", "end"):
            if window.get(bound) is not None and bound in declared:
                params[bound] = window[bound]
    return params


def _placeholder(name: str, spec: dict[str, Any]) -> Any:
    """A value that says "put yours here", for the `would_fit` example.

    Taken from the schema where the schema is explicit (an enum's own values),
    and otherwise shaped by `type` so the example validates as a *shape* even
    when the value is not real.
    """
    if spec.get("examples"):
        return spec["examples"][0]
    if spec.get("enum"):
        return spec["enum"][0]
    kind = spec.get("type")
    if kind == "array":
        return [f"<{name}>"]
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    return f"<{name}>"


def _failures(
    params: dict[str, Any], plan_type: PlanType
) -> tuple[list[str], dict[str, str]]:
    """Split schema failures into (absent required fields, rejected values).

    The distinction is the whole verdict rule, so it is made from the validator's
    own report rather than by guessing: a `required` failure names fields that
    were not supplied, and any other failure is about something that was.
    """
    validator = Draft202012Validator(plan_type.params_schema)
    missing: list[str] = []
    rejected: dict[str, str] = {}
    for error in sorted(validator.iter_errors(params), key=lambda e: list(e.absolute_path)):
        if error.validator == "required":
            for name in error.validator_value:
                if name not in params and name not in missing:
                    missing.append(name)
            continue
        where = "/".join(str(part) for part in error.absolute_path) or "(params)"
        rejected[where] = error.message
    return missing, rejected


def attempt_type(intent: dict[str, Any], plan_type: PlanType) -> dict[str, Any]:
    """One candidate: what this registered type says about this intent."""
    dataset = str(intent.get("dataset") or "")
    entry: dict[str, Any] = {
        "plan_type": plan_type.name,
        "version": plan_type.version,
        "vendor": plan_type.vendor,
        "missing": [],
        "rejected": {},
    }
    if plan_type.datasets and not any(
        fnmatch(dataset, pattern) for pattern in plan_type.datasets
    ):
        entry["verdict"] = NOT_CLAIMED
        entry["detail"] = f"does not claim dataset {dataset!r}"
        return entry

    params = candidate_params(intent, plan_type)
    missing, rejected = _failures(params, plan_type)
    entry["missing"] = missing
    entry["rejected"] = rejected
    # Kept for every candidate, not just a matching one: `would_fit` is built by
    # merging an example over what the caller already supplied.
    entry["params"] = params
    if rejected:
        entry["verdict"] = CANNOT_EXPRESS
        entry["detail"] = "rejects a value the intent supplied"
    elif missing:
        entry["verdict"] = NEEDS_PARAMS
        entry["detail"] = "missing required params"
    else:
        entry["verdict"] = MATCHED
    return entry


def would_fit(candidates: list[dict[str, Any]], plan_type_of) -> dict[str, Any] | None:
    """The cheapest thing a caller could do instead of asking for new code.

    One candidate, the fewest gaps, with the supplied params kept and the missing
    ones filled from the schema as obviously-placeholder values. It is an
    example, not a quote: nothing here has been priced or approved, and the
    caller still has to say what the symbols actually are.
    """
    gapped = [c for c in candidates if c["verdict"] == NEEDS_PARAMS]
    if not gapped:
        return None
    best = min(gapped, key=lambda c: (len(c["missing"]), c["plan_type"]))
    plan_type = plan_type_of(best["plan_type"])
    declared = (plan_type.params_schema.get("properties") or {}) if plan_type else {}
    params = dict(best.get("params") or {})
    for name in best["missing"]:
        params[name] = _placeholder(name, declared.get(name) or {})
    return {
        "plan_type": best["plan_type"],
        "params": params,
        "missing": list(best["missing"]),
        "note": "an example shape, not a quote: the missing values are placeholders",
    }


def attempt(intent: dict[str, Any], registry: Registry) -> dict[str, Any]:
    """The whole report: every candidate, plus the verdict the row is stored under."""
    candidates = [attempt_type(intent, entry) for entry in registry.all()]
    matched = [c for c in candidates if c["verdict"] == MATCHED]
    if matched:
        # First by registry order, which is sorted by name: deterministic, and
        # with one type registered there is nothing to choose. The others stay
        # in the report, so an ambiguous match is visible rather than resolved
        # by a dict's insertion order.
        verdict = MATCHED
    elif any(c["verdict"] == NEEDS_PARAMS for c in candidates):
        verdict = NEEDS_PARAMS
    elif any(c["verdict"] == CANNOT_EXPRESS for c in candidates):
        verdict = CANNOT_EXPRESS
    else:
        verdict = NOT_CLAIMED

    report: dict[str, Any] = {
        "verdict": verdict,
        "dataset": intent.get("dataset"),
        "registry": registry_digest(registry),
        "plan_type": matched[0]["plan_type"] if matched else None,
        "candidates": candidates,
    }
    if verdict == NEEDS_PARAMS:
        report["would_fit"] = would_fit(
            candidates, lambda name: registry.get(name)
        )
    return report


def as_json(report: dict[str, Any]) -> str:
    """Stored form: stable key order, so two identical attempts are one string."""
    return json.dumps(report, sort_keys=True, separators=(",", ":"))
