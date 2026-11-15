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

from . import auth, errors, match, profiles
from . import job as job_mod
from .config import Settings, load_settings
from .db import EscalateRefused, Store, canonical_json, payload_hash, utcnow
from .models import validate_intent, validate_plan
from .registry import Registry, load_registry

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
    "todo": "To approve",
    "working": "Running",
    "done": "Finished",
    # Not in FACET_ORDER, because a state this code has never heard of is a
    # render-last case, not a position in the workflow. But it is in the labels,
    # because this map is handed over as "the names of the facets": a row in
    # `other` with no name in it would render as the word `other`.
    "other": "Other",
}
TERMINAL_STATES = frozenset({"succeeded", "landed_with_errors", "failed"})

# Both HTML pages are a view of live state, and both may carry `?token=` in the
# URL. A cached copy would show a stale book to a reader who has no idea it is
# stale, so: never store. Browsers were otherwise free to heuristically cache
# these because we send no validators at all.
NO_STORE = {"Cache-Control": "no-store, must-revalidate"}


def _with_facet(row: dict[str, Any]) -> dict[str, Any]:
    """Attach the one label that explains why this row is interesting.

    Precedence matters and is deliberate: a stuck condition outranks the state
    it is stuck in (`running` + `lease_expired` is *broken*, not *running*), and
    `blocked` outranks nothing but is not breakage - it has its own labels.

    A blocked row is split three ways, in the order of who moves next (§2.5):
    escalated first, because once someone has asked for a plan the caller's
    options are spent; then `needs_params`, which is the caller's own gap; then
    `no_plan`, which is a decision about code. A row stored before the match
    existed has no verdict, and `no_plan` is the honest reading of that: it has
    no plan, and nobody asked for one. Backfilling a verdict now would date it
    to the moment of the scrape rather than the moment of the request.
    """
    if row.get("stuck_condition"):
        facet = "broken"
    elif row.get("state") == "blocked":
        if row.get("escalated_at"):
            facet = "requested"
        elif row.get("match_verdict") == match.NEEDS_PARAMS:
            facet = "needs_params"
        else:
            facet = "no_plan"
    elif row.get("state") in ("draft", "approved"):
        facet = "todo"
    elif row.get("state") == "running":
        facet = "working"
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


def _plan_type_view(entry: Any) -> dict[str, Any]:
    """The page's view of one plan type: a card, its parameter rows, its schema.

    Not `full_entry()` and not `index_entry()` either. The full entry carries
    the verify/resume prose, the failure verbs and the file path, which the
    page no longer draws - a reader of the page gets the fields and the raw
    schema, and a caller who wants the semantics asks `GET
    /v1/plan-types/{type}`, which is unchanged. A view-model is the page's
    shape; the moment it is the registry's shape it has to be kept in step
    with a page that does not read half of it.
    """
    return {
        "name": entry.name,
        "version": entry.version,
        "vendor": entry.vendor,
        "description": entry.description,
        "anchor": entry.name,
        "schema": _schema_view(entry.params_schema or {}),
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
    """Who is being asked for the data.

    A plan row is filed against a plan type, and the type declares its vendor.
    An intent names a dataset in vendor vocabulary and has no type yet, so the
    vendor comes from whichever registered type claims that dataset code. An
    unclaimed code gets "" -- the row says nothing rather than guessing, and
    the detail page still prints the dataset verbatim.
    """
    plan_type = row.get("plan_type")
    if plan_type:
        return registry.vendor_of(str(plan_type))
    return registry.vendor_for_dataset(str(_params_of(row).get("dataset") or ""))


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


def _book_row(row: dict[str, Any], registry: Registry) -> dict[str, Any]:
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
    out["label"] = _label(row)
    out["vendor"] = _vendor_of(row, registry)
    out["gap"] = _gap_of(row)
    return out


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
        # Resolved once per process, not per request: a long-lived service must
        # not record two commits for one deployment (phase 3 order §5).
        app.state.code_ref = settings.code_ref
        log.info("code_ref %s", settings.code_ref or "unknown (no checkout, no build arg)")

        store = Store(settings.db_path)
        try:
            await store.connect()
            app.state.store = store
            log.info("request store ready at %s", settings.db_path)
        except Exception as exc:  # noqa: BLE001 - the reason is reported, not raised
            # Up but broken is a real state: /healthz stays a liveness check that
            # does not touch the DB, so a store fault does not restart-loop the
            # container, and API routes answer `store_unavailable`.
            app.state.store_error = f"cannot open the request store: {exc}"
            log.error("%s", app.state.store_error)
        try:
            yield
        finally:
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

        kind = body.get("kind")
        now = utcnow()
        digest = payload_hash(body)
        # §5.6: exactly-once is opt-in, by the caller, because the caller is the
        # only party who knows whether a repeat is a retry or a second request.
        idempotency_key = (request.headers.get("Idempotency-Key") or "").strip() or None
        if idempotency_key and len(idempotency_key) > 200:
            raise errors.invalid_body("Idempotency-Key must be at most 200 characters")

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
                "estimate_usd": None,  # nothing prices anything this phase
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
            # says why. It is a schema comparison over the registry -- pure,
            # cheap, and the same answer every time.
            report = match.attempt(intent_obj, registry)
            report_text = match.as_json(report)
            job_text = job_digest = None
            plan_type = None
            if report["verdict"] == match.MATCHED:
                plan = profiles.resolve(registry, report["plan_type"])
                chosen = next(
                    (
                        c
                        for c in report["candidates"]
                        if c["plan_type"] == report["plan_type"]
                    ),
                    {},
                )
                if plan is not None:
                    # The intent supplied everything the plan declares, so there
                    # is nothing to ask a human and nothing to invent: bind it.
                    # `verify` is empty here and the plan's own default applies;
                    # a caller who wants to choose says so by filing a plan.
                    # An intent carries no `verify`, and a Job cannot be bound
                    # without one. The default is the conservative policy: a
                    # failed verification quarantines, never commits, so a
                    # matched intent can never land half-verified bytes.
                    verify = body.get("verify") or {}
                    on_failure = (
                        verify.get("on_failure") if isinstance(verify, dict) else None
                    )
                    if on_failure is None:
                        allowed = plan.type.on_failure_allowed
                        on_failure = (
                            "quarantine"
                            if "quarantine" in allowed
                            else sorted(allowed)[0]
                        )
                    elif on_failure not in plan.type.on_failure_allowed:
                        raise errors.plan_not_valid(
                            f"verify.on_failure {on_failure!r} is not allowed for "
                            f"plan_type {plan.name!r}"
                        )
                    try:
                        bound = plan.binder(
                            dict(chosen.get("params") or {}),
                            {"on_failure": on_failure},
                        )
                        job_text, job_digest = job_mod.bind_and_seal(bound)
                        plan_type = plan.name
                    except (job_mod.JobInvalid, ValueError, KeyError) as exc:
                        # A binder that refuses params its own schema accepted is
                        # a plan defect, not the caller's: record it and stay
                        # blocked rather than blaming the request.
                        report["verdict"] = match.CANNOT_EXPRESS
                        report["error"] = f"binder refused: {exc}"
                        report_text = match.as_json(report)
            row = {
                "id": _new_id(),
                "state": "draft" if job_text else "blocked",
                "body_kind": "intent",
                "plan_type": plan_type,
                "job_json": job_text,
                "params_json": canonical_json(params),
                "idempotency_key": idempotency_key,
                "payload_hash": digest,
                "intent_json": canonical_json(body),
                "estimate_usd": None,
                "blocked_reason": None if job_text else "awaiting_plan",
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
                # Informational: an earlier Request with the same body. This one
                # still exists, with its own id and its own output directory.
                "duplicate_of": duplicate_of,
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

    @app.get("/ui/dashboard.json")
    async def dashboard_json(request: Request) -> dict[str, Any]:
        auth.require_scope(request, "read", allow_query_token=True)
        store = await _store(request)
        rows = [
            _book_row(_with_facet(row), registry) for row in await store.overview()
        ]
        keys = (*FACET_ORDER, "other")
        return {
            "rows": rows,
            "total": len(rows),
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
        auth.require_scope(request, "read", allow_query_token=True)
        store = await _store(request)
        row = await store.get_request(request_id)
        if row is None:
            raise errors.not_found(f"no request with id {request_id!r}")
        # The view, not the table: a stuck condition and the age are the
        # read-model's answers, and recomputing them here would be a second
        # opinion that drifts.
        view = await store.overview_one(request_id) or row
        events = await store.events_for(request_id)
        return {
            **_book_row(_with_facet(view), registry),
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

    @app.get("/ui/plan-types.json")
    async def plan_types_json(request: Request) -> dict[str, Any]:
        auth.require_scope(request, "read", allow_query_token=True)
        return {"types": [_plan_type_view(entry) for entry in registry.all()]}

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
        # `read` scope, GET only, no form that mutates. The query token exists
        # because a browser link cannot set an Authorization header.
        auth.require_scope(request, "read", allow_query_token=True)
        return _index()

    @app.get("/plan-types", response_class=HTMLResponse)
    async def plan_types_page(request: Request) -> FileResponse:
        """The browse view for the registry: what may be filed, and as what.

        Read scope only. The same data is on `GET /v1/plan-types` for machines;
        this exists because "which fields does a Databento plan need?" should not
        require reading raw JSON in a terminal.
        """
        auth.require_scope(request, "read", allow_query_token=True)
        return _index()

    @app.get("/requests/{request_id}", response_class=HTMLResponse)
    async def request_page(request: Request, request_id: str) -> FileResponse:
        """One request: what it asked for, why, and what has happened to it.

        A list of keys with nowhere to click is half a surface. §7.8's argument
        is that the service deleted `ls` and owes the human the answers back;
        "which one is this?" is one of them, and it is the one a 32-character id
        cannot answer on its own.
        """
        auth.require_scope(request, "read", allow_query_token=True)
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
