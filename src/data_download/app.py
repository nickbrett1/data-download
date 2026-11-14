"""The FastAPI application: the read + write surface, and nothing behind it.

Every mutation returns an id and a state immediately (§7.9). No route executes,
prices, or advances a request. Approval has no endpoint; `?wait` exists on the
read and nowhere else.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, errors
from .config import Settings, load_settings
from .db import Store, canonical_json, payload_hash, plan_hash, utcnow
from .models import validate_intent, validate_plan
from .registry import Registry, load_registry

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).with_name("templates")
STATIC_DIR = Path(__file__).with_name("static")
VIEW_NAMES = ("v_pending", "v_done", "v_stuck", "v_blocked")

# The dashboard is one list. These facets are how a row is *labelled*, not how
# the page is laid out: a broken request and a finished one sit in the same
# column, and the badge is what differs. Order is by what needs a human first.
FACET_ORDER = ("broken", "waiting", "todo", "working", "done")
FACET_LABELS = {
    "broken": "Broken",
    "waiting": "Waiting on a plan",
    "todo": "To approve",
    "working": "Running",
    "done": "Finished",
}
TERMINAL_STATES = frozenset({"succeeded", "landed_with_errors", "failed"})


def _with_facet(row: dict[str, Any]) -> dict[str, Any]:
    """Attach the one label that explains why this row is interesting.

    Precedence matters and is deliberate: a stuck condition outranks the state
    it is stuck in (`running` + `lease_expired` is *broken*, not *running*), and
    `blocked` outranks nothing but is not breakage - it has its own label.
    """
    if row.get("stuck_condition"):
        facet = "broken"
    elif row.get("state") == "blocked":
        facet = "waiting"
    elif row.get("state") in ("draft", "approved"):
        facet = "todo"
    elif row.get("state") == "running":
        facet = "working"
    elif row.get("state") in TERMINAL_STATES:
        facet = "done"
    else:  # a state added later must still render, not vanish
        facet = "other"
    return {**row, "facet": facet, "facet_label": FACET_LABELS.get(facet, facet.title())}

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _humanise_age(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h {(total % 3600) // 60}m"
    return f"{total // 86400}d {(total % 86400) // 3600}h"


templates.env.filters["age"] = _humanise_age


def _store(request: Request) -> Store:
    store = getattr(request.app.state, "store", None)
    if store is None or request.app.state.store_error:
        raise errors.store_unavailable(request.app.state.store_error or "store is not connected")
    return store


def _request_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": row["id"],
        "state": row["state"],
        "body_kind": row["body_kind"],
        "plan_type": row["plan_type"],
        "plan": json.loads(row["plan_json"]) if row["plan_json"] else None,
        "params": json.loads(row["params_json"]),
        "intent": json.loads(row["intent_json"]) if row["intent_json"] else None,
        "estimate_usd": row["estimate_usd"],
        "blocked_reason": row["blocked_reason"],
        "plan_origin": row["plan_origin"],
        "matched_profile": row["matched_profile"],
        "plan_hash": row["plan_hash"],
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
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _new_id() -> str:
    return "req_" + uuid.uuid4().hex


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    registry: Registry = load_registry(settings.plan_types_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.registry = registry
        app.state.store = None
        app.state.store_error = None

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
            "Files and tracks archival download requests. Phase 1: requests are "
            "validated and persisted, and nothing executes."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(errors.ApiError)
    async def _api_error(_: Request, exc: errors.ApiError) -> JSONResponse:  # noqa: RUF029
        return JSONResponse(status_code=exc.status, content=exc.envelope())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', []))}: {err.get('msg', '')}"
            for err in exc.errors()
        )
        return JSONResponse(
            status_code=400,
            content={"error": {"code": errors.INVALID_BODY, "message": detail or "invalid query"}},
        )

    # -- liveness ----------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Deliberately does not touch the store: "up but broken" must not look
        # like "down", or the healthcheck restart-loops a working service.
        return {"status": "ok"}

    # -- writes ------------------------------------------------------------

    @app.post("/v1/requests", status_code=202)
    async def submit_request(request: Request) -> JSONResponse:
        auth.require_scope(request, "submit")
        store = _store(request)

        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise errors.invalid_body("body is not valid JSON") from None
        if not isinstance(body, dict):
            raise errors.invalid_body("body must be a JSON object")

        kind = body.get("kind")
        now = utcnow()
        digest = payload_hash(body)

        if kind == "plan":
            raw_type = body.get("plan_type")
            plan_type = registry.get(raw_type) if isinstance(raw_type, str) else None
            if plan_type is None:
                raise errors.plan_type_unknown(raw_type)
            plan = validate_plan(body, plan_type)
            row = {
                "id": _new_id(),
                "state": "draft",
                "body_kind": "plan",
                "plan_type": plan_type.name,
                "plan_json": canonical_json(plan),
                "params_json": canonical_json(plan["params"]),
                "payload_hash": digest,
                "intent_json": None,
                "estimate_usd": None,  # nothing prices anything this phase
                "blocked_reason": None,
                "plan_origin": "adhoc",  # no profile registry exists yet
                "matched_profile": None,
                "code_ref": None,
                "plan_hash": plan_hash(plan),
                "lease_until": None,
                "landed_sha256": None,
                "errors_json": None,
                "created_at": now,
                "updated_at": now,
            }
            event_detail = {"body_kind": "plan", "plan_type": plan_type.name}
        elif kind == "intent":
            validate_intent(body)
            intent_obj = body["intent"]
            params = {
                key: intent_obj[key]
                for key in ("dataset", "schema", "window", "symbols", "stype_in")
                if key in intent_obj
            }
            row = {
                "id": _new_id(),
                "state": "blocked",
                "body_kind": "intent",
                "plan_type": None,
                "plan_json": None,
                "params_json": canonical_json(params),
                "payload_hash": digest,
                "intent_json": canonical_json(body),
                "estimate_usd": None,
                "blocked_reason": "awaiting_plan",
                "plan_origin": None,
                "matched_profile": None,
                "code_ref": None,
                "plan_hash": None,
                "lease_until": None,
                "landed_sha256": None,
                "errors_json": None,
                "created_at": now,
                "updated_at": now,
            }
            event_detail = {"body_kind": "intent", "blocked_reason": "awaiting_plan"}
        else:
            raise errors.invalid_body("kind must be 'plan' or 'intent'")

        stored, created = await store.insert_request(row=row, event_detail=event_detail)
        if not created:
            raise errors.duplicate_request(stored["id"])

        return JSONResponse(
            status_code=202,
            content={
                "request_id": stored["id"],
                "state": stored["state"],
                "blocked_reason": stored["blocked_reason"],
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
        store = _store(request)
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
        store = _store(request)
        row = await store.get_request(request_id)
        if row is None:
            raise errors.not_found(f"no request with id {request_id!r}")
        # Long poll on the read only, never on a mutation (§7.9.5).
        if wait:
            row = await store.wait_for_state_change(request_id, row["state"], wait) or row
        return _request_payload(row)

    @app.get("/v1/requests/{request_id}/events")
    async def get_events(request: Request, request_id: str) -> dict[str, Any]:
        auth.require_scope(request, "read")
        store = _store(request)
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
                    "detail": json.loads(e["detail_json"]) if e["detail_json"] else None,
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

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        # `read` scope, GET only, no form that mutates. The query token exists
        # because a browser link cannot set an Authorization header.
        auth.require_scope(request, "read", allow_query_token=True)
        store = _store(request)

        rows = [_with_facet(row) for row in await store.overview()]
        keys = (*FACET_ORDER, "other")
        facets = {key: [r for r in rows if r["facet"] == key] for key in keys}
        wanted = request.query_params.get("show") or "all"
        if wanted != "all" and wanted not in keys:
            wanted = "all"
        visible = rows if wanted == "all" else facets[wanted]

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "rows": visible,
                "total": len(rows),
                "facets": facets,
                "facet_labels": FACET_LABELS,
                "facet_order": FACET_ORDER,
                "showing": wanted,
                # Preserved so filter links keep working when the page was
                # opened with ?token=. A browser cannot set a header on a
                # plain navigation.
                "query_token": request.query_params.get("token") or "",
                "registry": registry.all(),
                "generated_at": utcnow(),
                "db_path": settings.db_path,
            },
        )

    # -- static & home-screen icons (public, no auth required) -------------

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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
    uvicorn.run(create_app(), host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "info"))


app = create_app()
