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

from . import job as job_mod
from . import profiles
from .config import find_plan_types_dir
from .models import validate_plan
from .registry import load_registry


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
