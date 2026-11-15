"""Print the Job a Plan binds from these params (`python -m data_download.bind`).

A convenience over the same code path the service uses -- the exit test has to
show a binding and a rejection without explaining pytest flags. Not a second
implementation: it resolves through the registry, binds through the Plan's
committed binder, and seals through `job.bind_and_seal`.

    python -m data_download.bind databento_batch_download '{"dataset": ...}' \
        --on-failure quarantine

Prints the canonical Job bytes to stdout and its sha256 to stderr, so
`run it twice and diff the two outputs` is an empty diff.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

from . import job as job_mod
from . import match, profiles
from .config import find_plan_types_dir
from .models import validate_plan
from .registry import Registry, load_registry


class InvalidVerify(ValueError):
    """The caller chose a verify policy this plan type does not allow."""


@dataclass(frozen=True)
class Binding:
    """What an intent came to: the report, and the Job if one could be bound."""

    report: dict[str, Any]
    job_text: str | None = None
    job_digest: str | None = None
    plan_type: str | None = None

    @property
    def binds(self) -> bool:
        return self.job_text is not None


def bind_intent(registry: Registry, body: dict[str, Any]) -> Binding:
    """Match an intent body and, when a registered plan takes it, bind the Job.

    One implementation, three callers: `POST /v1/requests` (submit), the start
    of the process (phase 2.6 reconciliation), and the `rematch` CLI. Two copies
    of this would drift on the detail that matters most -- which verify policy a
    Job is bound with when the caller did not choose one.

    `body` is the whole intent *request* body, as stored in `intent_json`:
    `{"kind": "intent", "intent": {...}, "max_usd": ..., "verify": ...}`. Shape
    validation is the caller's (the API and the store both hold bodies that were
    validated before they were written); this is matching and binding only.
    """
    intent_obj = body.get("intent") or {}
    report = match.attempt(intent_obj, registry)
    if report["verdict"] != match.MATCHED:
        return Binding(report=report)
    plan = profiles.resolve(registry, report["plan_type"])
    if plan is None:  # pragma: no cover - `MATCHED` came from this registry
        return Binding(report=report)
    chosen = next(
        (c for c in report["candidates"] if c["plan_type"] == report["plan_type"]),
        {},
    )
    verify = body.get("verify") or {}
    on_failure = verify.get("on_failure") if isinstance(verify, dict) else None
    if on_failure is None:
        # An intent carries no `verify`, and a Job cannot be bound without one.
        # The default is the conservative policy: a failed verification
        # quarantines, never commits, so a matched intent can never land
        # half-verified bytes on the strength of a value nobody chose.
        allowed = plan.type.on_failure_allowed
        on_failure = (
            "quarantine" if "quarantine" in allowed else sorted(allowed)[0]
        )
    elif on_failure not in plan.type.on_failure_allowed:
        raise InvalidVerify(
            f"verify.on_failure {on_failure!r} is not allowed for "
            f"plan_type {plan.name!r}"
        )
    try:
        bound = plan.binder(dict(chosen.get("params") or {}), {"on_failure": on_failure})
        job_text, job_digest = job_mod.bind_and_seal(bound)
    except (job_mod.JobInvalid, ValueError, KeyError) as exc:
        # A binder that refuses params its own schema accepted is a plan defect,
        # not the caller's: record it and stay blocked rather than blaming the
        # request.
        report["verdict"] = match.CANNOT_EXPRESS
        report["error"] = f"binder refused: {exc}"
        return Binding(report=report)
    return Binding(
        report=report,
        job_text=job_text,
        job_digest=job_digest,
        plan_type=plan.name,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m data_download.bind",
        description="Bind params into a Job and print the canonical bytes.",
    )
    parser.add_argument("plan", help="a Plan name from plan_types/")
    parser.add_argument("params", help="the params object, as JSON")
    parser.add_argument(
        "--on-failure",
        choices=["commit_marked", "quarantine"],
        default="quarantine",
        help="verify.on_failure for this binding (default: quarantine)",
    )
    parser.add_argument(
        "--plan-types-dir",
        default=None,
        help="where the registry lives (default: the repo's plan_types/)",
    )
    args = parser.parse_args(argv)

    registry = load_registry(find_plan_types_dir(args.plan_types_dir))
    plan = profiles.resolve(registry, args.plan)
    if plan is None:
        print(f"plan_type_unknown: {args.plan!r} is not in the registry", file=sys.stderr)
        return 2

    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as exc:
        print(f"invalid_body: params is not JSON ({exc})", file=sys.stderr)
        return 2

    body = {
        "kind": "plan",
        "plan_type": plan.name,
        "params": params,
        "verify": {"on_failure": args.on_failure},
    }
    try:
        validated = validate_plan(body, plan.type)
        bound = plan.binder(validated["params"], validated["verify"])
        text, digest = job_mod.bind_and_seal(bound)
    except job_mod.JobInvalid as exc:
        print(f"plan_not_valid: {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - the refusal is the output
        print(f"plan_not_valid: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(text)
    print(f"job_hash: {digest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
