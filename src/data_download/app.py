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
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, errors
from .config import Settings, load_settings
from .db import Store, canonical_json, payload_hash, plan_hash, utcnow
from .models import validate_intent, validate_plan
from .registry import Registry, load_registry

log = logging.getLogger(__name__)

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
FACET_ORDER = ("broken", "waiting", "todo", "working", "done")
FACET_LABELS = {
    "broken": "Broken",
    "waiting": "Waiting on a plan",
    "todo": "To approve",
    "working": "Running",
    "done": "Finished",
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
        "description": entry.description,
        "anchor": entry.name,
        "schema": _schema_view(entry.params_schema or {}),
    }


def _store(request: Request) -> Store:
    store = getattr(request.app.state, "store", None)
    if store is None or request.app.state.store_error:
        raise errors.store_unavailable(
            request.app.state.store_error or "store is not connected"
        )
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
        "why": row["why"] if "why" in row.keys() else None,
        "max_usd": row["max_usd"] if "max_usd" in row.keys() else None,
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


def _book_row(row: dict[str, Any]) -> dict[str, Any]:
    """One row of the request book.

    `params_json` is the raw column, not the page's business: the label is
    derived here (server-side, because "what is this request" is a read-model
    opinion) and the payload stays small enough to hand the whole book over.
    """
    out = {k: v for k, v in row.items() if k != "params_json"}
    out["label"] = _label(row)
    # Eight characters is enough to tell two rows apart by eye and short enough
    # to sit under a name. The full id is on the link's tooltip and in full on
    # the detail page, which is where anyone copies it.
    stem = row["id"][4:] if row["id"].startswith("req_") else row["id"]
    out["short_id"] = stem[:8]
    return out


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
                "why": body.get("why"),
                "max_usd": body.get("max_usd"),
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
                "why": body.get("why"),
                "max_usd": body.get("max_usd"),
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
            row = (
                await store.wait_for_state_change(request_id, row["state"], wait) or row
            )
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
        rows = [_book_row(_with_facet(row)) for row in await _store(request).overview()]
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
        store = _store(request)
        row = await store.get_request(request_id)
        if row is None:
            raise errors.not_found(f"no request with id {request_id!r}")
        # The view, not the table: a stuck condition and the age are the
        # read-model's answers, and recomputing them here would be a second
        # opinion that drifts.
        view = await store.overview_one(request_id) or row
        events = await store.events_for(request_id)
        return {
            **_book_row(_with_facet(view)),
            "payload": {
                "body_kind": row["body_kind"],
                "plan_type": row["plan_type"],
                "params": _params_of(row),
                "intent": json.loads(row["intent_json"]) if row["intent_json"] else None,
                "plan": json.loads(row["plan_json"]) if row["plan_json"] else None,
                "max_usd": row["max_usd"],
                "why": row["why"],
            },
            "identity": {
                "plan_hash": row["plan_hash"],
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
