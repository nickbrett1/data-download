"""The FastAPI application: the read + write surface, and nothing behind it.

Every mutation returns an id and a state immediately (§7.9). No route executes,
prices, or advances a request. Approval has no endpoint; `?wait` exists on the
read and nowhere else.

Phase 2 adds one thing inside a submit and no surface at all: the Plan's binder
runs, the Job it emits is validated against the closed step vocabulary, hashed,
and stored write-once. Nothing interprets it here -- phase 3 owns execution, and
the offline dry run (`dryrun.py`) is a CLI harness this module does not import.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (
    auth,
    bind,
    catalogue,
    datasets,
    driver_client,
    errors,
    match,
    profiles,
    rematch,
    vendor,
)
from . import job as job_mod
from .config import Settings, load_settings
from .db import (
    ApproveRefused,
    CancelRefused,
    EscalateRefused,
    Store,
    canonical_json,
    payload_hash,
    utcnow,
)
from .models import validate_intent, validate_plan
from .registry import Registry, load_registry
from .worker import Worker

log = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    """Give this package's logger somewhere to write.

    uvicorn configures `uvicorn.*` and leaves the root logger untouched, so
    every `log.info` here propagated to a root logger with no handler: dropped
    silently, unless it was a warning and logging's last-resort handler printed
    it. The cost was concrete -- the line that names the code the process is
    running (`code_ref ...`), and the line that says the store opened, never
    appeared in a container's logs, which is the one place they are needed.
    Idempotent, and harmless under pytest where a handler may already exist.
    """
    # The package logger, not this module's: `db`, `auth` and `coderef` log too,
    # and one handler on the parent covers all of them as they appear.
    package = logging.getLogger(__package__ or __name__)
    if package.handlers:
        return
    handler = logging.StreamHandler()
    # The same shape as uvicorn's own lines, so one `docker logs` reads as one
    # log rather than two formats interleaved.
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    package.addHandler(handler)
    package.setLevel(level.upper())

STATIC_DIR = Path(__file__).with_name("static")

# The built frontend (web/dist, copied next to this module by the image). Vite
# emits `index.html` plus `assets/`, and FastAPI serves both -- the routes, the
# auth and the read-model stay here, and the view is just a file. There is no
# second server: SvelteKit would have duplicated every one of those, and a node
# process in production to do it.
WEB_DIST_DIR = Path(
    os.environ.get("DATA_DOWNLOAD_WEB_DIR") or Path(__file__).with_name("web")
)
VIEW_NAMES = ("v_pending", "v_done", "v_stuck", "v_blocked")

# The dashboard is one list. These facets are how a row is *labelled*, not how
# the page is laid out: a broken request and a finished one sit in the same
# column, and the badge is what differs. Order is by what needs a human first.
FACET_ORDER = (
    "broken",
    "needs_params",
    "no_plan",
    "requested",
    "todo",
    "working",
    "done",
)
FACET_LABELS = {
    "broken": "Broken",
    # One blocked row used to carry one label, "Waiting on a plan", which
    # conflated three different owners of the next move: the caller who omitted
    # a field, the plan author who has no type for this dataset, and the person
    # who has already been asked. Phase 2.5 stores the verdict that separates
    # them, so the label does too.
    "needs_params": "Needs parameters",
    "no_plan": "No plan yet",
    "requested": "Plan requested",
    # A bucket, not a row: it holds a `draft` waiting on a person *and* an
    # `approved` one no worker has claimed. "Awaiting approval" would be true of
    # half of it and false of the other half, so the label names the stage --
    # which is also why a draft row can say "Awaiting approval" without the two
    # words meaning the same thing twice.
    "todo": "Approval",
    "working": "Running",
    "done": "Finished",
    # Not in FACET_ORDER, because a state this code has never heard of is a
    # render-last case, not a position in the workflow. But it is in the labels,
    # because this map is handed over as "the names of the facets": a row in
    # `other` with no name in it would render as the word `other`.
    "other": "Other",
    # Settled and needing nothing, so it is not in FACET_ORDER either; the
    # route puts it in the filter bar whenever the count is non-zero.
    "cancelled": "Cancelled",
    "archived": "Archived",
}
TERMINAL_STATES = frozenset({"succeeded", "landed_with_errors", "failed"})

# The pages and their view-models are a view of live state. A cached copy would
# show a stale book to a reader who has no idea it is stale, so: never store.
# Browsers were otherwise free to heuristically cache these because we send no
# validators at all.
NO_STORE = {"Cache-Control": "no-store, must-revalidate"}


def _with_facet(row: dict[str, Any]) -> dict[str, Any]:
    """Attach the one label that explains why this row is interesting.

    Precedence matters and is deliberate: a stuck condition outranks the state
    it is stuck in (`running` + `lease_expired` is *broken*, not *running*), and
    `blocked` outranks nothing but is not breakage - it has its own labels.

    A blocked row is split three ways, by who moves next (§2.5): the caller's
    own gap (`needs_params`), a decision about code that has been asked for
    (`requested`), and one that has not (`no_plan`). `needs_params` comes first
    because it is the cheapest move and the one a caller can make without
    waiting for anybody. A row stored before the match existed has no verdict,
    and `no_plan` is the honest reading of that: it has no plan, and nobody has
    asked for one. Inventing a verdict at read time would date it to the read.
    """
    if row.get("archived_at"):
        # Archived is a place in the book, not an outcome: the row keeps its
        # state and its badge, and the list stops listing it. Checked before the
        # state branches because a broken condition on a row somebody has
        # finished with is not an alarm either.
        facet = "archived"
    elif row.get("stuck_condition"):
        facet = "broken"
    elif row.get("state") == "blocked":
        # `needs_params` outranks `requested`, and the reconciliation pass is
        # why: a row escalated as `not_claimed` whose new verdict is "add two
        # fields" has its short path back, and a label saying "plan requested"
        # would hide it. Nothing collides at submit time -- `escalate` refuses
        # while any candidate is merely short of params.
        if row.get("match_verdict") == match.NEEDS_PARAMS:
            facet = "needs_params"
        elif row.get("escalated_at"):
            facet = "requested"
        else:
            facet = "no_plan"
    elif row.get("state") in ("draft", "approved"):
        facet = "todo"
    elif row.get("state") == "running":
        facet = "working"
    elif row.get("state") == "cancelled":
        # Its own bucket, and the only one the book does not list by default: a
        # cancelled draft needs nothing and landed nothing, so it is noise in a
        # list whose whole job is "what needs me". It is kept -- a row that
        # vanished would read as a request that was never filed -- and it is
        # reachable by name from the filter bar, which is where "did we cancel
        # that?" gets answered.
        facet = "cancelled"
    elif row.get("state") in TERMINAL_STATES:
        facet = "done"
    else:  # a state added later must still render, not vanish
        facet = "other"
    return {
        **row,
        "facet": facet,
        "facet_label": FACET_LABELS.get(facet, facet.title()),
    }


def _type_label(spec: dict[str, Any]) -> str:
    """A human word for a JSON-Schema node, without inventing a type system."""
    kind = spec.get("type")
    if kind is None and "enum" in spec:
        return "one of"
    if kind is None and "const" in spec:
        return "fixed"
    kind = kind or "any"
    if kind == "array":
        items = spec.get("items") or {}
        inner = _type_label(items) if isinstance(items, dict) and items else "any"
        return f"list of {inner}"
    return str(kind)


def _constraints(spec: dict[str, Any]) -> list[str]:
    """The bits of a schema node a person needs to fill it in correctly.

    Deliberately not a general JSON-Schema renderer: this prints the keywords
    our registry actually uses, so an unexpected keyword shows up as a visible
    gap rather than being silently prettified into something untrue.
    """
    out: list[str] = []
    if "enum" in spec:
        out.append("one of " + ", ".join(str(v) for v in spec["enum"]))
    if "const" in spec:
        out.append(f"must be {spec['const']!r}")
    if "pattern" in spec:
        out.append(f"pattern {spec['pattern']}")
    if "minLength" in spec:
        out.append(f"min length {spec['minLength']}")
    if "minItems" in spec:
        out.append(f"at least {spec['minItems']} item(s)")
    if "default" in spec:
        out.append(f"defaults to {spec['default']!r}")
    return out


def _schema_view(schema: dict[str, Any]) -> dict[str, Any]:
    """Flatten one params_schema into rows a person can read.

    Property order follows the YAML, which is the author's ordering and usually
    the order you would fill the fields in. `required` is a set, so it becomes a
    badge rather than its own section - knowing a field exists matters more than
    which paragraph of the spec declared it mandatory.
    """
    required = set(schema.get("required") or [])
    properties = schema.get("properties") or {}
    fields = []
    for name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        fields.append(
            {
                "name": name,
                "required": name in required,
                "type": _type_label(spec),
                "constraints": _constraints(spec),
                "description": (spec.get("description") or "").strip(),
            }
        )
    return {
        "fields": fields,
        "required_count": len(required),
        "optional_count": max(len(fields) - len(required), 0),
        "raw": json.dumps(schema, indent=2, sort_keys=False),
    }


#: How long the page will wait for the driver runtime. It is a *nice to have* on
#: this page -- the registry is what the page is about -- so it gets a short
#: deadline and a sentence when it does not answer, rather than a slow page.
DRIVER_LIST_TIMEOUT_S = 2.0


def _driver_artifacts(settings: Settings) -> tuple[list[Any] | None, str]:
    """What the runtime has installed, or `None` and why it could not be asked.

    Never a 500: a page that cannot see the runtime must still say what a plan
    type *is*, and "no driver installed" is only meaningful when the question
    was actually answered. `None` says "unknown", `[]` says "nothing installed"
    -- a distinction the page draws rather than blurs.
    """
    if not (settings.driver_runtime_url and settings.driver_runtime_token):
        return None, "no driver runtime is configured on this deployment"
    try:
        return (
            driver_client.installed(
                url=settings.driver_runtime_url,
                token=settings.driver_runtime_token,
                timeout=DRIVER_LIST_TIMEOUT_S,
            ),
            "",
        )
    except driver_client.DriverUnavailable as exc:
        return None, str(exc)


def _implementations(entry: Any, artifacts: list[Any] | None) -> list[dict[str, Any]]:
    """Who can answer this type, as one row per seller.

    A plan type is the **question**; a driver is an **implementation** of it for
    one seller. The page used to draw them as one thing -- a "vendor-agnostic"
    pill beside a table of dataset codes, with the installed artefact invisible
    -- so it read as if Databento were part of the type's definition. The split
    is drawn here, where both facts are actually known: the registry says who
    the question accepts, the runtime says who can currently answer it.
    """
    sellers = {name: list(patterns) for name, patterns in entry.vendors.items()}
    if not sellers and entry.vendor:
        # A pinned single-seller type names its one seller in `vendor:`.
        sellers = {entry.vendor: []}
    installed = artifacts if artifacts is not None else []
    rows = []
    for seller, patterns in sellers.items():
        found = driver_client.driver_for(seller, artifacts=installed)
        rows.append(
            {
                "vendor": seller,
                "datasets": patterns,
                # A driver is an artefact with a version, so the page can say
                # *which* implementation and when it is out of date.
                "driver": (
                    {
                        "name": found.driver,
                        "version": found.version,
                        "protocol": found.protocol,
                    }
                    if found
                    else None
                ),
                "installed": found is not None,
            }
        )
    return rows


def _plan_type_view(entry: Any, artifacts: list[Any] | None = None) -> dict[str, Any]:
    """The page's view of one plan type: a card, its parameter rows, its schema.

    Not `full_entry()` and not `index_entry()` either. The full entry carries
    the verify/resume prose, the failure verbs and the file path, which the
    page no longer draws - a reader of the page gets the fields and the raw
    schema, and a caller who wants the semantics asks `GET
    /v1/plan-types/{type}`, which is unchanged. A view-model is the page's
    shape; the moment it is the registry's shape it has to be kept in step
    with a page that does not read half of it.
    """
    # `vendor` is the pinned single-seller shape; `vendors` is the generic one.
    # Two different facts, and only the first is the type's identity:
    #
    # * `vendor`  -- the type is *for* one seller (`vendor:` in its YAML);
    # * `vendors` -- a `vendors:` attribution table, which is who claims which
    #   dataset codes for *matching*, not who the question is for.
    #
    # There used to be a `vendor_agnostic` flag beside them, named after the
    # page's old pill. It is gone with the pill: the page draws "who answers
    # it" from `implementations` below, and a boolean describing a label that
    # no longer exists is the same blend in a quieter shape.
    implementations = _implementations(entry, artifacts)
    return {
        "name": entry.name,
        "version": entry.version,
        # The pinned seller, when the *type* is for one. Empty means the
        # question has no vendor in it -- which is a property of the question,
        # and no longer the page's headline pill.
        "vendor": entry.vendor,
        "vendors": {name: list(p) for name, p in entry.vendors.items()},
        "description": entry.description,
        "anchor": entry.name,
        "schema": _schema_view(entry.params_schema or {}),
        # The second half of the model the page now draws: who answers it.
        "implementations": implementations,
        "installed_count": sum(1 for row in implementations if row["installed"]),
    }


# A connect that failed at startup is not necessarily permanent. The usual cause
# is SQLite's per-connection schema cache going stale while a deploy's old
# container still held the database (`error in view v_pending: no such table:
# main.requests`), and a *new* connection is the only cure. So a request that
# finds no store retries -- single-flight, because six concurrent routes must not
# open six stores, and rate-limited, because a genuinely broken file must not be
# hammered once per request. This is the difference between a store fault that
# lasts until someone restarts the container and one that heals itself the
# moment the old process exits.
RECONNECT_INTERVAL_SECONDS = 5.0


async def _store(request: Request) -> Store:
    state = request.app.state
    if state.store is None:
        await _reconnect(state)
    if state.store is None:
        raise errors.store_unavailable(state.store_error or "store is not connected")
    return state.store


async def _refuse_from_catalogue(
    request: Request,
    store: Any,
    vendor_name: str | None,
    params: dict[str, Any],
) -> None:
    """Refuse a request the vendor's own catalogue says cannot be satisfied.

    Free, and that is the whole point: `cost` answers the same mistake with a
    paid call, and a window that starts before a dataset existed is refused by
    the vendor *after* the quote is charged for -- or worse, after a job is
    approved for a download that can never land. The catalogue is what we
    already asked the vendor, so the refusal costs nothing.

    Asymmetric on purpose (see `catalogue.refusal`): the first day is a fact and
    a start before it is refused; the last day is a clock that visibly trails
    "now", so an end past it is allowed. And only a positive, current answer
    refuses: no cache row, a stale row, or an undescribed code all mean the
    vendor gets to answer.
    """
    dataset = str(params.get("dataset") or "")
    if not dataset or not vendor_name:
        return
    found = await catalogue.lookup(store, [(vendor_name, dataset)])
    refused = catalogue.refusal(
        found.get((vendor_name, dataset)),
        dataset=dataset,
        schema=params.get("schema"),
        start=_start_of(params),
    )
    if refused:
        detail = {key: value for key, value in refused.items() if key != "message"}
        raise errors.not_in_catalogue(refused["message"], **detail)


def _start_of(params: dict[str, Any]) -> str | None:
    """The window start, flat on a plan's params or inside an intent's window."""
    window = params.get("window")
    if isinstance(window, dict) and window.get("start"):
        return str(window["start"])
    start = params.get("start")
    return str(start) if start else None


async def _price(request: Request, job_text: str, vendor_name: str | None) -> float:
    """Price a bound Job for `vendor`, or refuse the submit (§5.2, phase 4).

    The pricer lives in the settings so a test can inject one and the vendor
    client stays inside `vendor.py`. A refusal here is a refusal of the *body*:
    nothing is stored, so there is no half-created row to reconcile with a
    number that may never arrive.

    `vendor_name` is what selects the pricer: the driver installed for that vendor
    answers `cost`, so pricing does not have to live in this repo. It is a
    blocking call (a real HTTP verb, or an SDK read), so it runs in a thread --
    the submit route is async and a 30 s quote must not freeze the page.
    """
    try:
        return await asyncio.to_thread(
            vendor.price, job_text, request.app.state.settings.quote, vendor_name
        )
    except vendor.QuoteUnavailable as exc:
        raise errors.quote_unavailable(str(exc)) from None


async def _reconnect(state: Any) -> None:
    async with state.store_lock:
        if state.store is not None:
            return
        now = time.monotonic()
        if now < state.store_next_attempt:
            return
        state.store_next_attempt = now + RECONNECT_INTERVAL_SECONDS
        store = Store(state.settings.db_path)
        try:
            await store.connect()
        except Exception as exc:  # noqa: BLE001 - the reason is reported, not raised
            state.store_error = f"cannot open the request store: {exc}"
            log.warning("store reconnect failed: %s", exc)
            await store._close_quietly()
            return
        state.store = store
        state.store_error = None
        log.warning("request store reconnected at %s", state.settings.db_path)


def _request_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": row["id"],
        "state": row["state"],
        "body_kind": row["body_kind"],
        "plan_type": row["plan_type"],
        "job": json.loads(row["job_json"]) if row["job_json"] else None,
        "params": json.loads(row["params_json"]),
        "intent": json.loads(row["intent_json"]) if row["intent_json"] else None,
        "estimate_usd": row["estimate_usd"],
        "blocked_reason": row["blocked_reason"],
        "plan_origin": row["plan_origin"],
        "matched_profile": row["matched_profile"],
        # Phase 2.5: why this Request has no Job, and whether anyone has asked
        # for a plan. A blocked row that cannot say which is a dead end.
        "match_verdict": row["match_verdict"] if "match_verdict" in row.keys() else None,
        "match": json.loads(row["match_report_json"])
        if row.keys() and "match_report_json" in row.keys() and row["match_report_json"]
        else None,
        "escalated_at": row["escalated_at"] if "escalated_at" in row.keys() else None,
        "why": row["why"] if "why" in row.keys() else None,
        "max_usd": row["max_usd"] if "max_usd" in row.keys() else None,
        "job_hash": row["job_hash"],
        "code_ref": row["code_ref"],
        "landed_sha256": row["landed_sha256"],
        # Phase 5.5: what is happening to this request's driver, when the
        # vendor has none installed. Null is the normal case.
        "driver_state": row["driver_state"] if "driver_state" in row.keys() else None,
        "errors": json.loads(row["errors_json"]) if row["errors_json"] else [],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _summary_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": row["id"],
        "state": row["state"],
        "body_kind": row["body_kind"],
        "plan_type": row["plan_type"],
        "estimate_usd": row["estimate_usd"],
        "blocked_reason": row["blocked_reason"],
        "plan_origin": row["plan_origin"],
        "match_verdict": row["match_verdict"] if "match_verdict" in row.keys() else None,
        "escalated_at": row["escalated_at"] if "escalated_at" in row.keys() else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        # Phase 5.5: null unless a driver is being built for this row's vendor.
        "driver_state": row["driver_state"] if "driver_state" in row.keys() else None,
    }


def _new_id() -> str:
    return "req_" + uuid.uuid4().hex


def _params_of(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("params_json")
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _label(row: dict[str, Any]) -> str:
    """What the request *is*, in one line, for a page that shows a list of them.

    The id is a key: 32 hex characters chosen so two requests cannot collide,
    which is exactly what makes it useless as a name. The thing a reader wants
    is the subject -- the plan type, or the dataset and window an intent names.
    """
    params = _params_of(row)
    if row.get("plan_type"):
        return str(row["plan_type"])
    parts = [str(params.get(k)) for k in ("dataset", "schema") if params.get(k)]
    window = params.get("window") or {}
    if isinstance(window, dict) and window.get("start") and window.get("end"):
        parts.append(f"{window['start']}→{window['end']}")
    return " · ".join(parts) if parts else "—"


def _vendor_of(row: dict[str, Any], registry: Registry) -> str:
    """Who is being asked for the data: the params, the type, or the dataset code.

    Since the plan type is generic, the request itself names its vendor, so that
    is the first answer. A type that pins a vendor (the single-seller shape) is
    the second. An intent that names only a dataset code gets the vendor from
    whichever registered type claims the code, and an unclaimed code gets "" --
    the row says nothing rather than guessing, and the detail page still prints
    the dataset verbatim.
    """
    return registry.vendor_of_request(row.get("plan_type"), _params_of(row))


def _dataset_of(row: dict[str, Any]) -> str:
    """The dataset code this request downloads, or "".

    The Job is asked first because it is what ran; params are the earlier draft
    of the same fact, and a draft that never bound has only those. Parsed here
    rather than stored as a column: it is one field of a write-once blob, and a
    copy would be a second place for it to be wrong.
    """
    job: Any = None
    if row.get("job_json"):
        try:
            job = json.loads(row["job_json"])
        except (TypeError, ValueError):
            job = None
    facts = datasets.job_facts(job if isinstance(job, dict) else None)
    return str(facts.get("dataset") or _params_of(row).get("dataset") or "")


async def _catalogue_for(
    store: Store,
    pairs: set[tuple[str, str]] | list[tuple[str, str]],
    settings: Settings,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Cached dataset descriptions, and a background ask for the rest.

    Never blocks on the vendor: the page draws what is known and a task fills in
    what is not, so a fresh code is bare on its first render and named on the
    next. Only deployments that can reach a driver runtime schedule anything --
    there is no one to ask otherwise.
    """
    found = await catalogue.lookup(store, pairs)
    if settings.driver_ready:
        todo = [pair for pair in pairs if catalogue.is_stale(found.get(pair))]
        if todo:
            catalogue.schedule(store, todo, root=settings.datasets_dir)
    return found


def _gap_of(row: dict[str, Any]) -> dict[str, Any] | None:
    """The one short sentence for why a blocked row has no Job.

    Two fields, because it rides on every row of the book: the type that came
    closest and what it was short of. The whole report stays on the row's page,
    where there is room for every candidate and the example that would fit.

    `missing` comes from `would_fit` rather than from a candidate of our own
    choosing, because `would_fit` is already the read-model's "best candidate"
    rule (fewest gaps, then name) and a second rule here would disagree with the
    refusal message a caller gets from `escalate`.
    """
    raw = row.get("match_report_json")
    if not raw:
        return None
    try:
        report = json.loads(raw)
    except json.JSONDecodeError:
        return None
    would = report.get("would_fit") or {}
    rejected = sorted(
        {
            where
            for candidate in report.get("candidates") or []
            if candidate.get("verdict") == match.CANNOT_EXPRESS
            for where in (candidate.get("rejected") or {})
        }
    )
    return {
        "verdict": report.get("verdict"),
        "dataset": report.get("dataset"),
        "plan_type": would.get("plan_type"),
        "missing": list(would.get("missing") or []),
        "rejected": rejected,
    }


# A retry is only honest for a row that has finished. `draft`/`approved` have
# not run yet (approve or run them), and `running` is somebody's work in flight.
RETRYABLE_STATES = frozenset({"failed", "landed_with_errors"})

# The failures a retry cannot fix. The verifier compares the vendor's bytes
# against the manifest, and both are immutable -- so the same body, filed again,
# is checked to the same verdict. Everything else that settles is worth another
# attempt (`driver_not_ready` once the artefact is installed, `vendor_error`,
# `timeout`, `budget_refused` once the ceiling is raised), and the page keys its
# button off this same set so the two cannot disagree about what is retryable.
UNRETRYABLE_FAILURES = frozenset({"verify_shortfall", "verify_contradiction"})


def _retry_body(row: Any) -> dict[str, Any]:
    """The body to file again, rebuilt from what the row stored.

    An intent kept its whole verbatim body, so re-filing it is exact. A plan
    kept `params_json` and the sealed Job; the Job is the only place the verify
    policy survives (`params` do not carry it), and a Job cannot be re-bound
    without one -- so it is read back off the verify step rather than guessed.
    A plan whose Job lost its verify step is a hand-edited row: filing it
    without a policy would have `validate_plan` refuse it, naming the field.
    """
    if row["body_kind"] == "intent":
        return json.loads(row["intent_json"])
    body = {
        "kind": "plan",
        "plan_type": row["plan_type"],
        "params": json.loads(row["params_json"]),
        "max_usd": row["max_usd"],
        "why": row["why"] if "why" in row.keys() else None,
    }
    job = json.loads(row["job_json"]) if row["job_json"] else {}
    verify = next(
        (
            step.get("kwargs") or {}
            for step in job.get("steps", [])
            if step.get("kind") == "verify"
        ),
        {},
    )
    if verify.get("on_failure"):
        body["verify"] = {"on_failure": verify["on_failure"]}
    return body


def _failure_of(raw: Any) -> dict[str, str] | None:
    """Why a settled row did not land cleanly, in the reader's words.

    Two shapes sit in `errors_json`: the runner's `{"error": …, "detail": …}`
    and the older list of `{"code": …}`. Both become one code and one sentence,
    because a badge-sized line can carry a code and only a page can carry the
    sentence. `None` when there is nothing to say, so a page can ask rather
    than print an empty heading.

    The code is not prettified here: `driver_not_ready` is a phrase this code
    owns, and the reader's version of it ("driver not ready") belongs one layer
    out, where the page can also decide whether to look it up.
    """
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            parsed: Any = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Not JSON: a runner that wrote prose still said something.
            return {"code": "unrecorded", "detail": raw}
    else:
        parsed = raw
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not parsed:
        return None
    first = parsed[0] if isinstance(parsed[0], dict) else {"detail": str(parsed[0])}
    code = first.get("error") or first.get("code") or "unrecorded"
    detail = first.get("detail") or first.get("message") or ""
    more = len(parsed) - 1
    text = str(detail)
    if more > 0:
        text = f"{text} (+{more} more on the row)".strip()
    return {"code": str(code), "detail": text}


def _book_row(
    row: dict[str, Any],
    registry: Registry,
    descriptions: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One row of the request book.

    `params_json` is the raw column, not the page's business: the label and the
    vendor are derived here (server-side, because "what is this request" is a
    read-model opinion) and the payload stays small enough to hand the whole
    book over. The id is not in the row at all -- it is a key, not a name, and
    it is on the link's tooltip and in full on the detail page, which is where
    anyone copies it into a curl.

    `match_report_json` is dropped for the same reason, reduced to `gap`: the
    book has a badge's worth of room for "why not", and one row's worth of
    schema failures is not 500 rows' worth of payload. The page for a single
    request carries the whole report, alongside the params it arrived with.
    """
    out = {
        k: v for k, v in row.items() if k not in ("params_json", "match_report_json")
    }
    # The column is a string of JSON; the page wants the reason. Reduced here so
    # every caller -- the book and the page -- gets the same reading of it.
    out["failure"] = _failure_of(out.pop("errors_json", None))
    out["label"] = _label(row)
    out["vendor"] = _vendor_of(row, registry)
    out["gap"] = _gap_of(row)
    # The dataset code and what the vendor calls it. Both are on every row --
    # `dataset` is the code the page shows and `catalogue` is the description it
    # may show beside it -- because the code alone is vendor vocabulary that a
    # reader cannot be expected to have memorised. `None` means "not asked yet",
    # which is different from a vendor that answered "I do not describe it".
    out["dataset"] = _dataset_of(row) or None
    out["catalogue"] = (
        (descriptions or {}).get((out["vendor"], out["dataset"]))
        if out["dataset"]
        else None
    )
    return out


def _start_worker(
    app: FastAPI, store: Store, registry: Registry, settings: Settings
) -> Worker | None:
    """Start the pick-up loop, or say why there is none.

    Not started when the process cannot finish the job: no datasets directory
    (nowhere to land) or no driver runtime token (nothing to drive). Starting it
    anyway would mean a pass every interval that refuses the same sentence and
    leaves the row `approved` -- the row would sit exactly where it sits now, but
    the log would be full of a refusal that reads like a failure. `runner` being
    off is an operator's decision; the other two are missing configuration, and
    both are loud.
    """
    if not settings.runner:
        log.info("worker: DATA_DOWNLOAD_RUNNER is off - approved requests will wait")
        return None
    if not settings.datasets_dir:
        log.warning(
            "worker: not started - DATA_DOWNLOAD_DATASETS_DIR is unset, so an "
            "approved request has nowhere to land"
        )
        return None
    if not settings.driver_ready:
        log.warning(
            "worker: not started - DRIVER_RUNTIME_TOKEN is unset, so no driver "
            "can be reached"
        )
        return None

    worker = Worker(
        store,
        datasets_dir=settings.datasets_dir,
        registry=registry,
        driver_url=settings.driver_runtime_url,
        driver_token=settings.driver_runtime_token,
        interval=settings.runner_interval,
    )
    worker.start()
    return worker


def create_app(settings: Settings | None = None) -> FastAPI:
    _configure_logging(os.environ.get("LOG_LEVEL", "info"))
    settings = settings or load_settings()
    registry: Registry = load_registry(settings.plan_types_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.registry = registry
        app.state.store = None
        app.state.store_error = None
        # Reconnect bookkeeping: see `_store`. The lock makes the retry
        # single-flight and the deadline makes it rate-limited.
        app.state.store_lock = asyncio.Lock()
        app.state.store_next_attempt = 0.0
        # Phase 6: the thing that picks an approved row up. `None` means this
        # deployment does not run, which is a state the page can report honestly
        # ("no worker has picked it up") rather than a lie about a loop.
        app.state.worker = None
        # Resolved once per process, not per request: a long-lived service must
        # not record two commits for one deployment (phase 3 order §5).
        app.state.code_ref = settings.code_ref
        log.info("code_ref %s", settings.code_ref or "unknown (no checkout, no build arg)")

        store = Store(settings.db_path)
        try:
            await store.connect()
            app.state.store = store
            log.info("request store ready at %s", settings.db_path)
            # Phase 2.6: a verdict is a claim about a particular registry, and
            # a plan arrives with code -- so "a plan that would have matched
            # now exists" is exactly this moment. Rows whose report was
            # computed against a different digest are re-examined before the
            # app serves anything; ones that now match bind.
            try:
                summary = await rematch.reconcile(
                    store, registry, code_ref=settings.code_ref
                )
                rematch.log_summary(summary)
            except Exception as exc:  # noqa: BLE001 - never block a start on it
                log.error("rematch pass failed at start: %s", exc)
            # The worker starts last, after the store is open and the rematch
            # pass has had its say: a row that binds during rematch should be
            # visible to the first pass, not raced by it.
            app.state.worker = _start_worker(app, store, registry, settings)
        except Exception as exc:  # noqa: BLE001 - the reason is reported, not raised
            # Up but broken is a real state: /healthz stays a liveness check that
            # does not touch the DB, so a store fault does not restart-loop the
            # container, and API routes answer `store_unavailable`.
            app.state.store_error = f"cannot open the request store: {exc}"
            log.error("%s", app.state.store_error)
        try:
            yield
        finally:
            # Before the store closes: a pass inside a vendor call is cancelled
            # and its lease left to expire, which is the recovery path a crash
            # already takes. Stopping it afterwards would let it use a closed
            # connection on the way out.
            if app.state.worker is not None:
                await app.state.worker.stop()
            if app.state.store is not None:
                await app.state.store.close()

    app = FastAPI(
        title="data-download",
        version="0.1.0",
        description=(
            "Files and tracks archival download requests. Phase 2: a request's "
            "params are bound into a Job and validated; nothing executes."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(errors.ApiError)
    async def _api_error(_: Request, exc: errors.ApiError) -> JSONResponse:  # noqa: RUF029
        return JSONResponse(status_code=exc.status, content=exc.envelope())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', []))}: {err.get('msg', '')}"
            for err in exc.errors()
        )
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": errors.INVALID_BODY,
                    "message": detail or "invalid query",
                }
            },
        )

    # -- liveness ----------------------------------------------------------

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, str]:
        # Still a liveness check that does not touch the store: "up but broken"
        # must not look like "down", or the healthcheck restart-loops a working
        # service. What changed is that it stops *claiming* to be well: a store
        # that never opened used to report `{"status": "ok"}` while every route
        # answered 503, so `docker ps` said `(healthy)` about a service that
        # could not read its own database.
        state = request.app.state
        return {"status": "ok", "store": "ready" if state.store else "unavailable"}

    # -- writes ------------------------------------------------------------

    @app.post("/v1/requests", status_code=202)
    async def submit_request(request: Request) -> JSONResponse:
        auth.require_scope(request, "submit")
        store = await _store(request)

        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise errors.invalid_body("body is not valid JSON") from None
        if not isinstance(body, dict):
            raise errors.invalid_body("body must be a JSON object")

        idempotency_key = (request.headers.get("Idempotency-Key") or "").strip() or None
        if idempotency_key and len(idempotency_key) > 200:
            raise errors.invalid_body("Idempotency-Key must be at most 200 characters")
        return await _accept_submission(
            request, store, body, idempotency_key=idempotency_key
        )

    @app.post("/v1/requests/{request_id}/retry", status_code=202)
    async def retry_request(request: Request, request_id: str) -> JSONResponse:
        """File the same body again, as a new Request.

        A retry is a *new* request, never a rewound one. `failed` is history:
        its events, its `errors_json` and its output directory describe an
        attempt that happened, and the approval that preceded it authorised
        that attempt and no other. So this rebuilds the body from what was
        stored and sends it down exactly the path a submit takes -- re-priced,
        re-checked against the same ceiling, `duplicate_of` pointing back at
        the row it came from -- and leaves the new row in `draft`.

        That last part is the point: a retry spends nothing by itself. Somebody
        still approves it, with the new quote in front of them, which is how
        every other purchase on this service is authorised.

        Two doors, because there are two callers. A token holding `submit` is
        an agent re-filing its own work; an attested tailnet approver is the
        button on the page, which holds no credential by design.
        """
        auth.require_submit_or_approver(request)
        store = await _store(request)
        row = await store.get_request(request_id)
        if row is None:
            raise errors.not_found(f"no request {request_id!r}")
        if row["state"] not in RETRYABLE_STATES:
            raise errors.not_retryable("not_settled", row["state"], request_id)
        failure = _failure_of(row["errors_json"])
        if failure and failure["code"] in UNRETRYABLE_FAILURES:
            # The same bytes, checked again, by a verifier that has not changed.
            raise errors.not_retryable(
                "same_bytes", row["state"], request_id, failure=failure["code"]
            )
        return await _accept_submission(
            request,
            store,
            _retry_body(row),
            idempotency_key=None,
            retried_from=request_id,
        )

    async def _accept_submission(
        request: Request,
        store: Any,
        body: dict[str, Any],
        *,
        idempotency_key: str | None,
        retried_from: str | None = None,
    ) -> JSONResponse:
        """One body, from validation to a stored row -- submit and retry alike.

        Split out when retry arrived, and deliberately not copied: pricing, the
        ceiling, binding and duplicate detection are the rules of *filing*, and
        a second implementation would be a second set of them.
        """
        kind = body.get("kind")
        now = utcnow()
        digest = payload_hash(body)

        # What the vendor said this request costs, for the response only. The
        # row carries it as `estimate_usd`, except where the ceiling refused it
        # and the CHECK forbids a blocked row an estimate (§7.9.2) -- then this
        # is the only place the number the caller was refused on survives.
        submit_quote: float | None = None

        if kind == "plan":
            raw_type = body.get("plan_type")
            plan = profiles.resolve(registry, raw_type)
            if plan is None:
                # Refused *before* anything is bound or stored: an unregistered
                # name is not a candidate for later, it is an unknown plan.
                raise errors.plan_type_unknown(raw_type)
            validated = validate_plan(body, plan.type)
            # §7.2: the body carries params; the Plan's committed binder emits
            # the Job. Binding is where "no plan, no call" becomes real -- a
            # call the vocabulary does not model is not expressible.
            try:
                bound = plan.binder(validated["params"], validated["verify"])
                job_text, job_digest = job_mod.bind_and_seal(bound)
            except job_mod.JobInvalid as exc:
                raise exc.as_api_error() from None
            except (ValueError, KeyError) as exc:
                raise errors.plan_not_valid(
                    f"the binder for {plan.name!r} refused these params: {exc}"
                ) from None
            # Phase 4 (§5.2): price it before it can be approved. One free
            # vendor read, one comparison against the ceiling the caller set,
            # and no row if either fails -- an unpriced draft is a request
            # nobody can authorise honestly, because approving it means
            # approving a number that does not exist.
            vendor_name = registry.vendor_of_request(plan.name, validated["params"])
            # Slice 3: ask the cache before asking the vendor. A request whose
            # dataset cannot carry this schema, or whose window opens before the
            # dataset existed, has no quote worth fetching.
            await _refuse_from_catalogue(request, store, vendor_name, validated["params"])
            estimate = await _price(request, job_text, vendor_name)
            submit_quote = estimate
            ceiling = body.get("max_usd")
            if isinstance(ceiling, (int, float)) and estimate > ceiling:
                raise errors.over_ceiling(estimate, ceiling)
            row = {
                "id": _new_id(),
                "state": "draft",
                "body_kind": "plan",
                "plan_type": plan.name,
                "job_json": job_text,
                "params_json": canonical_json(validated["params"]),
                "idempotency_key": idempotency_key,
                "payload_hash": digest,
                "intent_json": None,
                "estimate_usd": estimate,
                "blocked_reason": None,
                "plan_origin": "adhoc",  # no profile registry exists yet
                "matched_profile": None,
                "code_ref": getattr(request.app.state, "code_ref", None),
                "job_hash": job_digest,
                "lease_until": None,
                "landed_sha256": None,
                "errors_json": None,
                "why": body.get("why"),
                "max_usd": body.get("max_usd"),
                "created_at": now,
                "updated_at": now,
            }
            event_detail = {
                "body_kind": "plan",
                "plan_type": plan.name,
                "job_hash": job_digest,
            }
        elif kind == "intent":
            validate_intent(body)
            intent_obj = body["intent"]
            params = {
                key: intent_obj[key]
                for key in ("dataset", "schema", "window", "symbols", "stype_in")
                if key in intent_obj
            }
            # The match decides whether this Request needs a plan at all, and
            # says why; a match binds, so there is nothing to ask a human and
            # nothing to invent. Matching and binding live in `bind.py` because
            # the reconciliation pass at start (phase 2.6) must bind exactly the
            # same way -- one implementation, or the two drift on which verify
            # policy a Job nobody chose gets bound with.
            try:
                bound = bind.bind_intent(registry, body)
            except bind.InvalidVerify as exc:
                raise errors.plan_not_valid(str(exc)) from None
            report = bound.report
            report_text = match.as_json(report)
            job_text = bound.job_text
            job_digest = bound.job_digest
            plan_type = bound.plan_type
            # Phase 4: a matched intent binds, so it can be priced too. Over the
            # ceiling is not a refused submit -- the row exists and the caller
            # must not have it vanish (§7.9.2), so it lands `blocked` with the
            # reason and no Job. Raising `max_usd` is the repair, which is a
            # human's decision, and a later re-match binds it again.
            estimate = None
            ceiling_reason = None
            refused_quote = None
            if job_text:
                vendor_name = registry.vendor_of_request(plan_type, params)
                await _refuse_from_catalogue(request, store, vendor_name, params)
                estimate = await _price(request, job_text, vendor_name)
                ceiling = body.get("max_usd")
                if isinstance(ceiling, (int, float)) and estimate > ceiling:
                    ceiling_reason = "ceiling_exceeded"
                    refused_quote = estimate
                    # `blocked` requires no Job and no estimate (the CHECK), so
                    # the number is reported in the response and not stored.
                    job_text = None
                    job_digest = None
                    estimate = None
            submit_quote = refused_quote if refused_quote is not None else estimate
            state = "blocked" if ceiling_reason else ("draft" if job_text else "blocked")
            row = {
                "id": _new_id(),
                "state": state,
                "body_kind": "intent",
                "plan_type": plan_type,
                "job_json": job_text,
                "params_json": canonical_json(params),
                "idempotency_key": idempotency_key,
                "payload_hash": digest,
                "intent_json": canonical_json(body),
                "estimate_usd": estimate,
                "blocked_reason": ceiling_reason
                or (None if job_text else "awaiting_plan"),
                "plan_origin": "matched" if job_text else None,
                "matched_profile": None,
                "code_ref": getattr(request.app.state, "code_ref", None)
                if job_text
                else None,
                "job_hash": job_digest,
                "lease_until": None,
                "landed_sha256": None,
                "errors_json": None,
                "match_verdict": report["verdict"],
                "match_report_json": report_text,
                "escalated_at": None,
                "why": body.get("why"),
                "max_usd": body.get("max_usd"),
                "created_at": now,
                "updated_at": now,
            }
            event_detail = {
                "body_kind": "intent",
                "match_verdict": report["verdict"],
                "plan_type": plan_type,
                "blocked_reason": row["blocked_reason"],
            }
        else:
            raise errors.invalid_body("kind must be 'plan' or 'intent'")

        if retried_from is not None:
            # On the event, not in `why`: the human's sentence is theirs, and a
            # provenance note pasted into it would be a second author.
            event_detail["retried_from"] = retried_from

        stored, created, duplicate_of = await store.insert_request(
            row=row, event_detail=event_detail
        )
        if not created:
            # Only reachable via a repeated Idempotency-Key: the caller said this
            # is the same attempt, and 409 is the honest answer.
            raise errors.duplicate_request(stored["id"])

        return JSONResponse(
            status_code=202,
            content={
                "request_id": stored["id"],
                "state": stored["state"],
                "blocked_reason": stored["blocked_reason"],
                "match_verdict": stored.get("match_verdict"),
                "match": json.loads(stored["match_report_json"])
                if stored.get("match_report_json")
                else None,
                # What the vendor quoted. A plan body carries it on the row
                # too; an intent refused by its ceiling has nowhere to put it.
                "quote_usd": submit_quote,
                # Informational: an earlier Request with the same body. This one
                # still exists, with its own id and its own output directory.
                "duplicate_of": duplicate_of,
                # Present only on a retry: the row this one was rebuilt from.
                **({"retried_from": retried_from} if retried_from else {}),
            },
        )

    @app.post("/v1/quotes")
    async def quote_request(request: Request) -> JSONResponse:
        """Price a request's Job without changing anything (§8.4).

        `read` scope, because this is a question and not a mutation: it buys
        nothing, downloads nothing, and writes nothing. It exists so a caller
        such as `data-sourcing-agent` can ask "what would this cost?" instead of
        holding a vendor key of its own -- the spend-capable credential lives in
        one deployment (§13 Q3).

        A request with no Job cannot be priced, and says so rather than
        answering zero.
        """
        auth.require_scope(request, "read")
        store = await _store(request)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise errors.invalid_body("body is not valid JSON") from None
        if not isinstance(body, dict):
            raise errors.invalid_body("body must be a JSON object")
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            raise errors.invalid_body("request_id is required and must be a string")

        row = await store.get_request(request_id)
        if row is None:
            raise errors.not_found(f"no request {request_id!r}")
        job_text = row["job_json"] if "job_json" in row.keys() else None
        if not job_text:
            raise errors.quote_unavailable(
                f"request {request_id!r} is {row['state']} with no Job, so there "
                "is nothing to price yet"
            )
        estimate = await _price(request, job_text, _vendor_of(row, registry))
        return JSONResponse(
            content={
                "request_id": request_id,
                "estimate_usd": estimate,
                "max_usd": row["max_usd"] if "max_usd" in row.keys() else None,
                "currency": "USD",
                "priced_at": utcnow(),
            }
        )

    @app.post("/v1/requests/{request_id}/cancel")
    async def cancel_request(request: Request, request_id: str) -> JSONResponse:
        """Cancel a filed draft before anyone approves or runs it.

        The door is retry's: a `submit` token or an attested approver. Filing,
        cancelling and re-filing are three halves of one decision and none of
        them spends money -- the approval is still the spend, and it is still a
        person's.

        There is no DELETE. The row, its quote and its events stay and its badge
        changes to `cancelled`, which is the difference between "I changed my
        mind" and "this never happened". `note` is accepted because *why* a
        request was dropped is the sort of thing nobody remembers in a month;
        `actor` in the body is refused for the reason the approve route refuses
        it: the attested login is the actor, and a self-declared second one would
        be a second way in.
        """
        actor = auth.require_submit_or_approver(request)
        store = await _store(request)

        raw = await request.body()
        note = None
        if raw.strip():
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise errors.invalid_body("body is not valid JSON") from None
            if not isinstance(body, dict):
                raise errors.invalid_body("body must be a JSON object")
            if body.get("actor") is not None:
                raise errors.invalid_body(
                    "actor is not accepted: the actor is the token's name or "
                    "the tailnet login `tailscale serve` asserts, so it cannot "
                    "be declared in the body"
                )
            note = body.get("note")
            if note is not None and not isinstance(note, str):
                raise errors.invalid_body("note must be a string")

        # An approved row is refused by the store, because an approval
        # authorises one attempt and only the runner may stop work it holds.
        # The one exception is a row no runner can ever hold: its plan type is
        # no longer in git, so there is no binder to bind it and nothing to
        # claim. That is the only case this door can decide, because the
        # registry is here and the store has none.
        existing = await store.get_request(request_id)
        unbindable = (
            existing is not None
            and existing["state"] == "approved"
            and profiles.resolve(registry, existing.get("plan_type")) is None
        )

        try:
            row, changed = await store.cancel(
                request_id,
                actor,
                note,
                unbindable=unbindable,
                context={"via": "serve"},
            )
        except CancelRefused as exc:
            if exc.reason == "not_found":
                raise errors.not_found(f"no request with id {request_id!r}") from None
            raise errors.cancel_refused(exc.reason, exc.state) from None

        return JSONResponse(
            status_code=200,
            content={
                "request_id": row["id"],
                "state": row["state"],
                # False for a second cancelal: idempotent, no second event.
                "cancelled": changed,
            },
        )

    @app.post("/v1/requests/{request_id}/archive")
    async def archive_request(request: Request, request_id: str) -> JSONResponse:
        """Archive a finished request, or restore it: `{"archived": false}`.

        The verb a `failed` row was missing. It is not `cancel`: cancelling
        writes `state`, and a failed attempt's state is the outcome of something
        that really happened. This writes a date instead, so the row keeps its
        badge and its errors and simply stops appearing in the book's default
        list.

        Restoring is the same route in reverse rather than a separate one: the
        two are one decision with a toggle, and a page that can hide a row
        without being able to bring it back is a page that loses things.
        """
        actor = auth.require_submit_or_approver(request)
        store = await _store(request)

        raw = await request.body()
        archived = True
        note = None
        if raw.strip():
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise errors.invalid_body("body is not valid JSON") from None
            if not isinstance(body, dict):
                raise errors.invalid_body("body must be a JSON object")
            if body.get("actor") is not None:
                raise errors.invalid_body(
                    "actor is not accepted: the actor is the token's name or "
                    "the tailnet login `tailscale serve` asserts, so it cannot "
                    "be declared in the body"
                )
            if "archived" in body:
                if not isinstance(body["archived"], bool):
                    raise errors.invalid_body("archived must be true or false")
                archived = body["archived"]
            note = body.get("note")
            if note is not None and not isinstance(note, str):
                raise errors.invalid_body("note must be a string")

        try:
            row, changed = await store.archive(
                request_id, actor, note, archived=archived, context={"via": "serve"}
            )
        except CancelRefused as exc:
            if exc.reason == "not_found":
                raise errors.not_found(f"no request with id {request_id!r}") from None
            raise errors.archive_refused(exc.reason, exc.state) from None

        return JSONResponse(
            status_code=200,
            content={
                "request_id": row["id"],
                "state": row["state"],
                "archived_at": row["archived_at"],
                # False for a second archive of the same row: no second event.
                "changed": changed,
            },
        )

    @app.post("/v1/requests/{request_id}/escalate", status_code=202)
    async def escalate_request(request: Request, request_id: str) -> JSONResponse:
        """Ask for a new plan. Records demand; writes no plan.

        A caller may do this only when no registered plan type could take the
        intent even with every parameter it declares -- otherwise the answer is
        409 with the shortest path to a plan that already exists, because a
        caller who can escalate will escalate and adding two fields has to look
        cheaper than waiting for new code.
        """
        auth.require_scope(request, "escalate")
        store = await _store(request)
        note = None
        raw = await request.body()
        if raw.strip():
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise errors.invalid_body("body is not valid JSON") from None
            if not isinstance(body, dict):
                raise errors.invalid_body("body must be a JSON object")
            note = body.get("note")
            if note is not None and not isinstance(note, str):
                raise errors.invalid_body("note must be a string")
        try:
            row, changed = await store.escalate(request_id, note=note)
        except EscalateRefused as exc:
            if exc.reason == "not_found":
                raise errors.not_found(
                    f"no request with id {request_id!r}"
                ) from None
            raise errors.ApiError(
                409,
                "escalate_refused",
                exc.as_text(),
                reason=exc.reason,
                state=exc.state,
                would_fit=exc.would_fit,
            ) from None
        return JSONResponse(
            status_code=202,
            content={
                "request_id": row["id"],
                "state": row["state"],
                "blocked_reason": row["blocked_reason"],
                "match_verdict": row["match_verdict"],
                "escalated_at": row["escalated_at"],
                "created": changed,
            },
        )

    @app.get("/v1/whoami")
    async def whoami(request: Request) -> JSONResponse:
        """Who `tailscale serve` says is calling, and whether that login may spend.

        The approve button's readiness check, and the reason it can be one: the
        page must not offer a verb the door will refuse, and it cannot work out
        the answer for itself -- only the loopback hop knows who is on the other
        side of the tunnel. Two refusals are distinguishable on purpose: no
        attestation at all is an error (403), not-an-approver is an answer
        (200 with `approver: false`), because the second reader is signed in and
        needs to be told why the button is off, not shown a failure.
        """
        settings = request.app.state.settings
        login = auth.tailnet_login(request, settings)
        if login is None:
            raise errors.not_attested(
                request.client.host if request.client else None,
                bool(request.headers.get(auth.LOGIN_HEADER, "").strip()),
            )
        allowed = login.lower() in settings.approvers
        return JSONResponse(content={"login": login, "approver": allowed})

    @app.post("/v1/requests/{request_id}/approve")
    async def approve_request(request: Request, request_id: str) -> JSONResponse:
        """Approve one request: the door the CLI verb never had.

        §7.4 and §7.8.3 argued against this route, and the argument has been
        overruled deliberately rather than quietly. It rested on a dashboard
        that opened the store `mode=ro` in a container with no credentials, so
        a button on it could not have approved anything even if one existed.
        That container was never built: the read surface is now this process's
        own view-models, in the same process that owns the writer. The choice
        was therefore never "safe page or unsafe page" -- it was "a route with
        a scope check, or a `docker exec` with no authz at all", and the shell
        command is the weaker of the two: it authenticates nothing, leaves no
        request log, and records whatever `--actor` the operator typed.

        What has changed since: the token is gone, and the door is a person.
        Approval is authorised by the login `tailscale serve` asserts on the
        loopback hop in front of this process, checked against
        `DATA_DOWNLOAD_APPROVERS` (`auth.require_approver`). That is a better
        answer than the token for both questions it was being asked: it names
        *who* rather than *someone holding a secret*, and it cannot be spent by
        a page in the approver's own browser, because a cross-origin POST has no
        way to arrive from Serve's loopback hop.

        `actor` in the body is refused rather than ignored. The attested login
        is the actor; a second, self-declared one would be a second way in, and
        the record would have to choose which to believe.
        """
        actor = auth.require_approver(request)
        store = await _store(request)

        raw = await request.body()
        body: dict[str, Any] = {}
        if raw.strip():
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise errors.invalid_body("body is not valid JSON") from None
            if not isinstance(body, dict):
                raise errors.invalid_body("body must be a JSON object")
        if body.get("actor") is not None:
            raise errors.invalid_body(
                "actor is not accepted: the approver is the tailnet login "
                "`tailscale serve` asserts, so it cannot be declared in the body"
            )
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise errors.invalid_body("note must be a string")

        try:
            row, changed = await store.approve(
                request_id,
                actor,
                note,
                context={"via": "serve"},
            )
        except ApproveRefused as exc:
            if exc.reason == "not_found":
                raise errors.not_found(f"no request with id {request_id!r}") from None
            raise errors.approve_refused(exc.reason, exc.state) from None

        if changed:
            # The worker's latency, not its correctness: without this a row
            # approved here waits for the next idle pass (up to `interval`), and
            # the person who just pressed the button watches nothing happen.
            # Told, not awaited -- the loop is the one that runs it.
            worker = getattr(request.app.state, "worker", None)
            if worker is not None:
                worker.wake()

        return JSONResponse(
            status_code=200,
            content={
                "request_id": row["id"],
                "state": row["state"],
                "job_hash": row["job_hash"],
                # False for a second approval: idempotent, and no second event.
                "approved": changed,
            },
        )

    # -- reads -------------------------------------------------------------

    @app.get("/v1/requests")
    async def list_requests(
        request: Request,
        state: str | None = Query(default=None),
        plan_type: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        auth.require_scope(request, "read")
        store = await _store(request)
        rows, total = await store.list_requests(
            state=state, plan_type=plan_type, limit=limit, offset=offset
        )
        return {
            "items": [_summary_payload(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/v1/requests/{request_id}")
    async def get_request(
        request: Request,
        request_id: str,
        wait: float = Query(default=0, ge=0, le=60),
    ) -> dict[str, Any]:
        auth.require_scope(request, "read")
        store = await _store(request)
        row = await store.get_request(request_id)
        if row is None:
            raise errors.not_found(f"no request with id {request_id!r}")
        # Long poll on the read only, never on a mutation (§7.9.5).
        if wait:
            row = (
                await store.wait_for_state_change(request_id, row["state"], wait) or row
            )
        return _request_payload(row)

    @app.get("/v1/requests/{request_id}/events")
    async def get_events(request: Request, request_id: str) -> dict[str, Any]:
        auth.require_scope(request, "read")
        store = await _store(request)
        if await store.get_request(request_id) is None:
            raise errors.not_found(f"no request with id {request_id!r}")
        events = await store.events_for(request_id)
        return {
            "items": [
                {
                    "id": e["id"],
                    "request_id": e["request_id"],
                    "ts": e["ts"],
                    "actor_kind": e["actor_kind"],
                    "event": e["event"],
                    "detail": json.loads(e["detail_json"])
                    if e["detail_json"]
                    else None,
                }
                for e in events
            ]
        }

    @app.get("/v1/plan-types")
    async def plan_types(request: Request) -> dict[str, Any]:
        auth.require_scope(request, "read")
        return {"items": [t.index_entry() for t in registry.all()]}

    @app.get("/v1/plan-types/{plan_type}")
    async def plan_type_detail(request: Request, plan_type: str) -> dict[str, Any]:
        auth.require_scope(request, "read")
        entry = registry.get(plan_type)
        if entry is None:
            raise errors.plan_type_unknown(plan_type, status=404)
        return entry.full_entry()

    # -- dashboard ---------------------------------------------------------

    # -- the frontend ------------------------------------------------------
    #
    # Two HTML routes became one built file plus two view-models. What stays
    # exactly where it was: the token, the scopes, the SQL views, the facet
    # precedence, the plan-type registry, and `/v1/*` -- the API the CLI and the
    # agents already call. What moved into the browser: `$%.4f`, "1h 04m", and
    # the badge colour. That split is the whole argument for keeping FastAPI.
    #
    # The pages and their view-models are **public**, and deliberately so. What
    # they show is a read-only projection: a request book, a registry, an
    # append-only trail. The one verb the app has -- `approve` -- is a `/v1`
    # route that only answers a loopback peer carrying the login `tailscale
    # serve` asserted, so no page and no agent can reach it. Gating the reads
    # instead would protect the least valuable thing in the system with the most
    # fragile mechanism in it; it was tried, and the cost was a live credential
    # in a Docker label, a rotation, and a bookmark that stopped working.
    #
    # The door that matters is the one in front of `/v1/*`, which still needs a
    # token in an `Authorization` header. "Can reach port 8820" is the trust
    # boundary for reading, and it is expected to be the LAN or the tailnet.

    @app.get("/ui/dashboard.json")
    async def dashboard_json(request: Request) -> dict[str, Any]:
        store = await _store(request)
        raw = [_with_facet(row) for row in await store.overview()]
        # Two passes because a description is looked up by (vendor, dataset) and
        # both come off the row: one query for the whole book, then the row that
        # needs a description gets the one it asked for.
        pairs = {
            (_vendor_of(row, registry), _dataset_of(row))
            for row in raw
            if _dataset_of(row)
        }
        descriptions = await _catalogue_for(store, pairs, settings)
        rows = [_book_row(row, registry, descriptions) for row in raw]
        keys = (*FACET_ORDER, "cancelled", "archived", "other")
        # `total` is what the book is *showing*, and the list it shows is the one
        # minus the requests nobody needs to look at. Claiming 5 while listing 4
        # would be the old bug in a new place. The facet counts come off the full
        # list, so "Cancelled (1)" stays reachable from the filter bar.
        hidden = {"cancelled", "archived"}
        listed = [r for r in rows if r["facet"] not in hidden]
        return {
            "rows": listed,
            "total": len(listed),
            "cancelled": sum(1 for r in rows if r["facet"] == "cancelled"),
            "archived": sum(1 for r in rows if r["facet"] == "archived"),
            "facets": {key: [r for r in rows if r["facet"] == key] for key in keys},
            "facet_order": list(FACET_ORDER),
            "facet_labels": FACET_LABELS,
            "showing": "all",
        }

    @app.get("/ui/request/{request_id}.json")
    async def request_detail_json(request: Request, request_id: str) -> dict[str, Any]:
        """One request, in full: the why, the body it arrived as, its events.

        The book is a list of keys; this is the answer to clicking one. What it
        adds over `GET /v1/requests/{id}` is the *page's* shape -- the facet,
        the label, the age as the view computes it -- and what it adds over the
        list is everything the list deliberately drops: the human `why`, the
        intent or the plan, and the append-only trail.
        """
        store = await _store(request)
        row = await store.get_request(request_id)
        if row is None:
            raise errors.not_found(f"no request with id {request_id!r}")
        # The view, not the table: a stuck condition and the age are the
        # read-model's answers, and recomputing them here would be a second
        # opinion that drifts.
        view = await store.overview_one(request_id) or row
        events = await store.events_for(request_id)
        book = _with_facet(view)
        dataset = _dataset_of(book)
        vendor_name = _vendor_of(book, registry)
        descriptions = await _catalogue_for(
            store, {(vendor_name, dataset)} if dataset else set(), settings
        )
        return {
            **_book_row(book, registry, descriptions),
            # The page draws the code once, beside what the vendor calls it, so
            # `GLBX.MDP3` never appears alone. `None` = not asked yet; an entry
            # with `described: false` = the vendor answered that it has no name.
            "dataset": dataset or None,
            "catalogue": descriptions.get((vendor_name, dataset)) if dataset else None,
            "payload": {
                "body_kind": row["body_kind"],
                "plan_type": row["plan_type"],
                "params": _params_of(row),
                "intent": json.loads(row["intent_json"]) if row["intent_json"] else None,
                "job": json.loads(row["job_json"]) if row["job_json"] else None,
                "max_usd": row["max_usd"],
                "why": view["why"] if "why" in view.keys() else row["why"],
            },
            # Phase 2.5: the verdict, every candidate, and the example that
            # would have fitted. Parsed, not the stored string -- this is the
            # one page with room for the report, and it is what turns "waiting
            # on a plan" into "add symbols and stype_in".
            "match": json.loads(row["match_report_json"])
            if row["match_report_json"]
            else None,
            "identity": {
                "job_hash": row["job_hash"],
                "code_ref": row["code_ref"],
                "landed_sha256": row["landed_sha256"],
                "errors": json.loads(row["errors_json"]) if row["errors_json"] else [],
            },
            "events": [
                {
                    "id": e["id"],
                    "ts": e["ts"],
                    "actor_kind": e["actor_kind"],
                    "event": e["event"],
                    "detail": json.loads(e["detail_json"]) if e["detail_json"] else None,
                }
                for e in events
            ],
        }

    @app.get("/ui/datasets.json")
    async def datasets_json(request: Request) -> dict[str, Any]:
        """What landed, and what is landing (§7.8).

        Two sources joined read-only: the store names the vendor, the dataset and
        the state; the datasets root holds the bytes and the `landing.json` that
        says which driver wrote them. The tree mirrors `runner.destination`, so
        the page cannot show a path the runner would not have written.
        """
        store = await _store(request)
        rows, _ = await store.list_requests(
            states=sorted(datasets.VISIBLE_STATES), limit=500
        )
        rows.sort(key=lambda r: str(r["created_at"]), reverse=True)
        body = datasets.tree(rows, root=settings.datasets_dir, registry=registry)
        # One description per dataset group, not per request: the group heading
        # is where the code is drawn, and twenty requests for GLBX.MDP3 should
        # cost one lookup, not twenty. A group whose vendor is unknown has
        # nobody to ask, so it keeps a bare code.
        pairs = {
            (vendor["vendor"], group["dataset"])
            for vendor in body["vendors"]
            if vendor["vendor_slug"]
            for group in vendor["datasets"]
        }
        # A request waiting for a driver is not "in flight" by the requests
        # table's vocabulary, so the page needs this second number or it
        # freezes through exactly the minutes it should be moving (§7 of the
        # phase 5.5 order).
        body["totals"]["driver_jobs_open"] = await store.driver_jobs_open()
        descriptions = await _catalogue_for(store, pairs, settings)
        for bucket in body["vendors"]:
            for group in bucket["datasets"]:
                group["catalogue"] = descriptions.get(
                    (bucket["vendor"], group["dataset"])
                )
        return body

    @app.get("/datasets/file/{vendor}/{dataset}/{request_id}/{name}")
    async def dataset_file(
        vendor: str, dataset: str, request_id: str, name: str
    ) -> FileResponse:
        """One landed file, as a download.

        Every segment goes through `resolve_under`, so `..` and a symlink out of
        the root are both refused by where they end up, not by how they are
        spelled. The body is `application/octet-stream` with an attachment
        disposition: a phone should save it, never render it.
        """
        path = datasets.resolve_under(
            settings.datasets_dir, vendor, dataset, request_id, "files", name
        )
        if path is None or not path.is_file():
            raise errors.not_found(
                f"no landed file {vendor}/{dataset}/{request_id}/{name}"
            )
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=path.name,
            headers={
                "Cache-Control": "private, max-age=60",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/ui/plan-types.json")
    async def plan_types_json(request: Request) -> dict[str, Any]:
        artifacts, detail = _driver_artifacts(settings)
        return {
            "types": [_plan_type_view(entry, artifacts) for entry in registry.all()],
            # A separate fact from the registry, and stated as such: what is
            # *installed* is the runtime's answer. `known: false` means nobody
            # could ask, which is not the same as "nothing is installed".
            "drivers": {
                "known": artifacts is not None,
                "detail": detail,
                "count": len(artifacts or []),
            },
        }

    def _index() -> FileResponse:
        index = WEB_DIST_DIR / "index.html"
        if not index.is_file():
            # A 500 in the shape of a sentence: the usual cause is running the
            # service from a checkout that has not had `npm run build` in web/.
            raise HTTPException(
                status_code=503,
                detail=f"the frontend is not built: {index} is missing (run `npm run build` in web/)",
            )
        return FileResponse(index, media_type="text/html", headers=NO_STORE)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> FileResponse:
        return _index()

    @app.get("/plan-types", response_class=HTMLResponse)
    async def plan_types_page(request: Request) -> FileResponse:
        """The browse view for the registry: what may be filed, and as what.

        The same data is on `GET /v1/plan-types` for machines; this exists
        because "which fields does a Databento plan need?" should not require
        reading raw JSON in a terminal. The page itself is an empty shell; the
        rows arrive from the gated view-model below.
        """
        return _index()

    @app.get("/datasets", response_class=HTMLResponse)
    async def datasets_page(request: Request) -> FileResponse:
        """The browse view for what landed: vendor, dataset, request, files.

        A phone-sized answer to "what do I have?", which the request book cannot
        give: the book knows what was asked for, this page knows what exists.
        """
        return _index()

    @app.get("/requests/{request_id}", response_class=HTMLResponse)
    async def request_page(request: Request, request_id: str) -> FileResponse:
        """One request: what it asked for, why, and what has happened to it.

        A list of keys with nowhere to click is half a surface. §7.8's argument
        is that the service deleted `ls` and owes the human the answers back;
        "which one is this?" is one of them, and it is the one a 32-character id
        cannot answer on its own.
        """
        return _index()

    # -- static & home-screen icons (public, no auth required) -------------

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # The bundle's own files. Public on purpose: hashed filenames under an
    # authenticated HTML page are not a secret, and a check here would only mean
    # a browser with an expired token renders an unstyled page.
    if (WEB_DIST_DIR / "assets").is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(WEB_DIST_DIR / "assets")),
            name="assets",
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon-32.png", media_type="image/png")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    async def apple_touch_icon() -> FileResponse:
        return FileResponse(STATIC_DIR / "apple-touch-icon.png", media_type="image/png")

    @app.get("/site.webmanifest", include_in_schema=False)
    async def webmanifest() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "site.webmanifest",
            media_type="application/manifest+json",
        )

    return app


def serve(host: str, port: int) -> None:
    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


app = create_app()
