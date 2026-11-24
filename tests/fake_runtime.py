"""A driver runtime in-process, for tests: the wire, and nothing else.

This is a **deliberate duplicate** of `drivers/stub/1/` in
`data-download-drivers`. The app must be testable without the driver container
(no image, no network, no subprocess), and the way that stays honest is that
each side tests its own implementation of the same written protocol: this file
against `data-download-driver-protocol`, and the artefact against the same
document. Where they disagree, phase D's conformance suite is the tiebreak --
and until it exists, a disagreement shows up as a failing parity test rather
than as a surprise in production.

What it is *not*: a mock of `RemoteDriver`. It is a real HTTP server on a real
port, so the client's parsing, deadlines, bearer header and error handling are
all exercised. It just does not spawn a child process, because there is no
artefact here to spawn -- the stub's behaviour is reimplemented inline.

**The token is minted per instance** (`secrets.token_urlsafe`), never a
constant. A fixed test token is a token in a repository, and a client that
learns to send `Bearer test` will one day send it somewhere real.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
FAKE_QUOTE_USD = 12.34
ROWS_PER_FILE = 1000
MODES = ("clean", "short", "nothing")


def staged_files(dest: Path) -> dict[str, dict[str, Any]]:
    """What is on disk, hashed. The same reading the verifier does."""
    staged: dict[str, dict[str, Any]] = {}
    if not dest.is_dir():
        return staged
    for path in sorted(dest.iterdir()):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        staged[path.name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "rows": payload.count(b"\n") - 1,
        }
    return staged


def _content(name: str, vendor_job_id: str) -> bytes:
    header = f"STUB: not market data. file={name} job={vendor_job_id}\n".encode()
    return header + b"row\n" * ROWS_PER_FILE


class FakeRuntime:
    """One HTTP server, one session per `POST /v1/sessions`, stub semantics."""

    def __init__(
        self,
        datasets_dir: Path | str,
        vendor: str = "Stub",
        driver: str = "stub",
    ) -> None:
        self.datasets_dir = Path(datasets_dir)
        # Deliberately not created here: the runner creates the request root
        # before it opens a session, and "nothing was written at all" is a fact
        # some tests assert by the directory's absence.
        #: Which vendor this fake stands in for. A real runtime declares the
        #: vendor an artefact serves; the runner binds drivers by vendor, so a
        #: test that wants the Databento plan type to find a driver says so here
        #: rather than teaching `driver_for` a special case.
        self.vendor = vendor
        self.driver = driver
        #: When set, what `GET /v1/drivers` answers instead. Discovery is the
        #: one route a test needs to bend to see a protocol mismatch or two
        #: installed versions.
        self.drivers: dict[str, Any] | None = None
        self.token = secrets.token_urlsafe(24)
        self.requests: list[tuple[str, str]] = []  # (method, path), for assertions
        self.sessions: dict[str, dict[str, Any]] = {}
        #: verb -> body to answer with instead of running the verb. Used to make
        #: the runtime answer something the stub would never say (a broken
        #: verifier), which is how the client's "do not sanitise" rule is tested.
        self.overrides: dict[str, dict[str, Any]] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> FakeRuntime:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None, "use FakeRuntime as a context manager"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # -- routes --------------------------------------------------------------

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # keep pytest quiet
                pass

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _fail(self, status: int, code: str, detail: str = "") -> None:
                self._json(status, {"error": {"code": code, "detail": detail}})

            def _authorised(self) -> bool:
                header = self.headers.get("Authorization") or ""
                expected = f"Bearer {runtime.token}"
                if not secrets.compare_digest(header, expected):
                    self._fail(401, "unauthorised", "bearer token")
                    return False
                return True

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            # -- GET ---------------------------------------------------------

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                runtime.requests.append(("GET", path))
                if path == "/healthz":
                    self._json(200, {"status": "ok", "protocol": PROTOCOL_VERSION})
                    return
                if path == "/v1/drivers":
                    if not self._authorised():
                        return
                    if runtime.drivers is not None:
                        self._json(200, dict(runtime.drivers))
                        return
                    self._json(200, {
                        "protocol": PROTOCOL_VERSION,
                        "drivers": [{
                            "name": runtime.driver, "version": "1",
                            "vendor": runtime.vendor,
                            "protocol": PROTOCOL_VERSION,
                            "installed_at": "2026-11-22T00:00:00Z",
                        }],
                    })
                    return
                self._fail(404, "not_found", path)

            # -- POST --------------------------------------------------------

            def do_POST(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                runtime.requests.append(("POST", path))
                if not self._authorised():
                    return
                try:
                    body = self._body()
                    if path == "/v1/sessions":
                        self._json(201, runtime._create(body))
                    else:
                        verb = path.removeprefix("/v1/")
                        self._json(200, runtime._verb(verb, body))
                except _Failure as exc:
                    self._fail(exc.status, exc.code, exc.detail)

            def do_DELETE(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                runtime.requests.append(("DELETE", path))
                if not self._authorised():
                    return
                session_id = path.rsplit("/", 1)[-1]
                runtime.sessions.pop(session_id, None)
                self._json(200, {"ok": True})

        return Handler

    # -- the protocol --------------------------------------------------------

    def _create(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("protocol") != PROTOCOL_VERSION:
            raise _Failure(400, "bad_request", "unsupported protocol version")
        if body.get("driver") != self.driver:
            raise _Failure(409, "driver_missing", f"no driver {body.get('driver')!r}")
        root = Path(str(body.get("root") or ""))
        if root != self.datasets_dir and self.datasets_dir not in root.parents:
            raise _Failure(400, "bad_request", f"{root} is outside the dataset store")
        mode = str((body.get("driver_options") or {}).get("mode") or "clean")
        if mode not in MODES:
            raise _Failure(400, "bad_request", f"unknown mode {mode!r}")
        session_id = "sess_" + secrets.token_hex(8)
        self.sessions[session_id] = {
            "root": root, "mode": mode, "submitted": None, "args": {},
        }
        return {
            "session_id": session_id,
            "driver_version": "1",
            "protocol": PROTOCOL_VERSION,
        }

    def _session(self, body: dict[str, Any]) -> dict[str, Any]:
        session = self.sessions.get(str(body.get("session_id") or ""))
        if session is None:
            raise _Failure(404, "unknown_session", "no such session")
        return session

    def _verb(self, verb: str, body: dict[str, Any]) -> dict[str, Any]:
        session = self._session(body)
        args = {k: v for k, v in body.items() if k != "session_id"}
        if verb in self.overrides:
            return dict(self.overrides[verb])
        if verb == "describe":
            # The catalogue's verb: no Job, no spend, just the vendor's words.
            dataset = str(args.get("dataset") or "")
            venue = dataset.split(".", 1)[0]
            return {
                "dataset": dataset,
                "described": True,
                "description": f"Fake dataset {dataset}",
                "venue": venue,
                "venues": [venue],
                "publishers": [],
                "range": {
                    "start": 1275782400000000000,
                    "end": 1795392000000000000,
                },
                "schemas": ["ohlcv-1d"],
                "fetched_at": "2026-11-23T00:00:00Z",
                "stub": True,
            }
        if verb == "resolve":
            symbols = args.get("symbols") or []
            return {"symbols": list(symbols), "resolved": len(symbols), "stub": True}
        if verb == "cost":
            return {"usd": FAKE_QUOTE_USD, "detail": {"stub": True}}
        if verb == "submit":
            if session["submitted"] is not None:
                return {**session["submitted"], "replayed": True}
            seed = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
            vendor_job_id = "stub_" + hashlib.sha256(seed).hexdigest()[:12]
            # The arguments are kept separately from the answer: `_claimed()`
            # derives the vendor's manifest from what was submitted, exactly as
            # the artefact does, while the answer keeps the wire's shape.
            session["args"] = args
            session["submitted"] = {
                "vendor_job_id": vendor_job_id,
                "state": "submitted",
                "authorised_usd": args.get("authorised_usd"),
                "declared_usd": args.get("declared_usd"),
            }
            return dict(session["submitted"])
        if verb == "poll":
            return {"state": "done"}
        if verb == "list_files":
            return {"files": [dict(e) for e in self._claimed(session)]}
        if verb == "fetch":
            return {"written": self._fetch(session, args)}
        if verb == "convert":
            dest = self._dest(session, args)
            return {"staged": sorted(p.name for p in dest.iterdir() if p.is_file())}
        if verb == "verify":
            claimed = self._claimed(session)
            shipped = claimed[:1] if session["mode"] == "short" else claimed
            return {
                "claimed": [e["name"] for e in claimed],
                "manifest": shipped,
                "staged": staged_files(self._dest(session, args)),
            }
        raise _Failure(404, "not_found", f"no verb {verb!r}")

    # -- the stub's beliefs --------------------------------------------------

    def _dest(self, session: dict[str, Any], args: dict[str, Any]) -> Path:
        dest = Path(str(args.get("dest") or ""))
        resolved = dest if dest.is_absolute() else session["root"] / dest
        resolved = resolved.resolve()
        root = session["root"].resolve()
        if resolved != root and root not in resolved.parents:
            raise _Failure(400, "bad_request", f"dest {dest} escapes the request root")
        return resolved

    def _fetch(self, session: dict[str, Any], args: dict[str, Any]) -> list[str]:
        dest = self._dest(session, args)
        claimed = self._claimed(session)
        if session["mode"] == "nothing":
            published: list[dict[str, Any]] = []
        elif session["mode"] == "short":
            published = claimed[:1]
        else:
            published = claimed

        dest.mkdir(parents=True, exist_ok=True)
        submitted = session["submitted"] or {}
        vendor_job_id = str(submitted.get("vendor_job_id") or "")
        written: list[str] = []
        for entry in published:
            (dest / entry["name"]).write_bytes(_content(entry["name"], vendor_job_id))
            written.append(entry["name"])
        return written

    def _claimed(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        submitted = session.get("args") or {}
        start = _as_date(submitted.get("start"))
        end = _as_date(submitted.get("end"))
        dataset = submitted.get("dataset") or "DATASET"
        encoding = submitted.get("encoding") or "dbn"
        compression = submitted.get("compression") or "zstd"
        suffix = f".{encoding}" + (".zst" if compression != "none" else "")

        days: list[date] = []
        if start and end and end > start:
            cursor = start
            while cursor < end and len(days) <= 31:
                days.append(cursor)
                cursor += timedelta(days=1)
        if not days:
            days = [start or date(2026, 1, 1)]

        vendor_job_id = str((session["submitted"] or {}).get("vendor_job_id") or "")
        return [
            {
                "name": f"{dataset}-{day.isoformat()}{suffix}",
                "sha256": hashlib.sha256(
                    _content(f"{dataset}-{day.isoformat()}{suffix}", vendor_job_id)
                ).hexdigest(),
                "rows": ROWS_PER_FILE,
            }
            for day in days
        ]


class _Failure(Exception):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"{status} {code} {detail}")


def _as_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
