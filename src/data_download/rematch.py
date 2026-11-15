"""Re-running the match when the registry moves (phase 2.6).

A verdict is a claim about a particular day's `plan_types/`: `not_claimed` means
"no *registered* type speaks for this dataset", not "nothing ever will". Plans
arrive with code, and a plan that would have bound a Request filed last month is
exactly the thing an escalation asks for. So a stored verdict has a freshness
question, and this module answers it: at start, every blocked row whose report
was computed against a different registry digest is re-examined.

**The trigger is the deploy, not a button.** A plan type is a params schema plus
a committed binder, both baked into the image, so "we wrote a new plan" and "a
new process started" are the same moment. That is stronger than a control
somebody has to remember: the reconciliation cannot lag the plan's existence,
because the plan cannot exist without the process restarting. The CLI here is
the escape hatch -- inspection (`--dry-run`) and one-row emergencies -- not the
mechanism.

A re-match that comes back `matched` **binds**, through the same `bind.py` the
API uses, and the row moves to `draft`: safe (a bind spends nothing; approval is
still a human CLI verb) and consistent with what `POST /v1/requests` does with
the same intent today. A re-match that comes back short or refused only rewrites
the verdict and its report. Either way the row gains one `rematched` event, and
`escalated_at` survives untouched: the demand was made, and it is history.

Nothing here decides that a row *should* have bound. It asks the question again
and records the answer; a plan that still does not cover the request leaves the
row `blocked` with a fresh verdict and a page that says why.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from . import bind, match
from .config import find_plan_types_dir, load_settings
from .db import RematchRefused, Store
from .registry import Registry, load_registry

log = logging.getLogger(__name__)


def stored_digest(row: dict[str, Any]) -> str | None:
    """The registry a row's verdict was computed against, or None if unknown.

    `None` covers two cases that want the same treatment: a row filed before
    phase 2.5 stored no report at all, and a report from before the digest
    covered schemas. Both have a claim of unknown age, so both are re-examined.
    """
    raw = row.get("match_report_json")
    if not raw:
        return None
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        return None
    value = report.get("registry") if isinstance(report, dict) else None
    return value if isinstance(value, str) else None


def decide(
    registry: Registry,
    *,
    force: bool = False,
    code_ref: str | None = None,
) -> Any:
    """Build the pure decision `Store.rematch` calls inside its transaction.

    `None` means "this row's verdict is not stale", which is the common case on
    every start after the first: the digest on the row equals the digest of the
    registry in memory, so a restart does no writes at all.
    """
    digest = match.registry_digest(registry)

    def decide_row(row: dict[str, Any]) -> dict[str, Any] | None:
        # Only an intent has a match, and only a blocked row has no Job. The
        # state guard is in `Store.rematch`; `body_kind` is checked here because
        # a plan Request was never matched in the first place.
        if row.get("body_kind") != "intent" or not row.get("intent_json"):
            return None
        if not force and stored_digest(row) == digest:
            return None
        body = json.loads(row["intent_json"])
        bound = bind.bind_intent(registry, body)
        return {
            "verdict": bound.report["verdict"],
            "report_json": match.as_json(bound.report),
            "registry": bound.report.get("registry"),
            "job_text": bound.job_text,
            "job_digest": bound.job_digest,
            "plan_type": bound.plan_type,
            # The Job was bound by *this* code, so this commit is what a claim
            # has to check. Unchanged when nothing bound.
            "code_ref": code_ref if bound.binds else None,
        }

    return decide_row


async def reconcile(
    store: Store,
    registry: Registry,
    *,
    code_ref: str | None = None,
    dry_run: bool = False,
    only: list[str] | None = None,
    force: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """Re-examine every blocked row (or `only`), oldest first.

    One row's failure never stops the pass: a stored body this code cannot parse
    is a defect in that row, and turning it into a failed start would take the
    service down for a request nobody is looking at. It is reported instead.
    """
    decide_row = decide(registry, force=force, code_ref=code_ref)
    summary: dict[str, Any] = {
        "examined": 0,
        "moved": 0,
        "bound": 0,
        "skipped": 0,
        "dry_run": dry_run,
        "registry": match.registry_digest(registry),
        "rows": [],
        "errors": [],
    }
    for request_id in await store.blocked_ids(only):
        summary["examined"] += 1
        try:
            before, after, changed = await store.rematch(
                request_id, decide_row, dry_run=dry_run, now=now
            )
        except RematchRefused:
            # Lost a race with an approval or a claim between the list and the
            # write. Not an error: the row moved on, which is what we wanted.
            summary["skipped"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            summary["errors"].append({"request_id": request_id, "error": str(exc)})
            log.warning("rematch: %s failed: %s", request_id, exc)
            continue
        if not changed:
            continue
        summary["moved"] += 1
        bound = after.get("state") == "draft"
        if bound:
            summary["bound"] += 1
        summary["rows"].append(
            {
                "request_id": request_id,
                "from": before.get("match_verdict"),
                "to": after.get("match_verdict"),
                "bound": bound,
                "plan_type": after.get("plan_type"),
            }
        )
    return summary


def log_summary(summary: dict[str, Any]) -> None:
    """One line, because the point of a pass is to be visible in `docker logs`.

    A broad plan type can sweep many rows into `draft` at once. That is safe --
    a bind spends nothing -- but it should not be something a person discovers by
    noticing the dashboard changed.
    """
    if not summary["examined"] and not summary["errors"]:
        return
    note = " (dry run: nothing written)" if summary["dry_run"] else ""
    log.info(
        "rematch: %d blocked row(s) examined against registry %s, %d moved, "
        "%d bound%s",
        summary["examined"],
        summary["registry"],
        summary["moved"],
        summary["bound"],
        note,
    )
    for row in summary["rows"]:
        log.info(
            "rematch: %s %s -> %s%s",
            row["request_id"],
            row["from"] or "no verdict",
            row["to"],
            f" (bound {row['plan_type']})" if row["bound"] else "",
        )


def _describe(row: dict[str, Any]) -> str:
    if row["bound"]:
        return f"{row['request_id']}  {row['from']} -> {row['to']} (bound {row['plan_type']})"
    return f"{row['request_id']}  {row['from']} -> {row['to']}"


async def _run(db_path: str, registry: Registry, args: argparse.Namespace) -> dict:
    store = Store(db_path)
    await store.connect()
    try:
        return await reconcile(
            store,
            registry,
            code_ref=args.code_ref,
            dry_run=args.dry_run,
            only=args.request_ids or None,
            force=args.force,
        )
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m data_download.rematch",
        description=(
            "Re-run the match on blocked Requests against the registry on disk. "
            "The service does this itself at start; this is for inspection and "
            "for one row at a time."
        ),
    )
    parser.add_argument(
        "request_ids",
        nargs="*",
        help="specific Requests (default: every blocked row, oldest first)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change and write nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-match even when the registry digest has not moved",
    )
    parser.add_argument(
        "--plan-types-dir",
        default=None,
        help="where the registry lives (default: the repo's plan_types/)",
    )
    parser.add_argument("--db", default=None, help="store path (default: $DATA_DOWNLOAD_DB)")
    args = parser.parse_args(argv)

    registry = load_registry(find_plan_types_dir(args.plan_types_dir))
    settings = load_settings()
    args.code_ref = settings.code_ref
    summary = asyncio.run(_run(args.db or settings.db_path, registry, args))

    for row in summary["rows"]:
        print(_describe(row))
    for error in summary["errors"]:
        print(f"{error['request_id']}  error: {error['error']}", file=sys.stderr)
    suffix = " — dry run, nothing written" if summary["dry_run"] else ""
    print(
        f"{summary['examined']} blocked row(s) examined against registry "
        f"{summary['registry']}: {summary['moved']} moved, "
        f"{summary['bound']} bound{suffix}"
    )
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
