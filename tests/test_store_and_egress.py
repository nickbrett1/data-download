"""Non-functional gates: store failure, hashing, config, and no vendor egress."""

from __future__ import annotations

import ast
import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import auth
from data_download.app import create_app
from data_download.config import Settings, load_settings, parse_approvers, parse_tokens
from data_download.db import Store, payload_hash

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "data_download"

FORBIDDEN_IMPORTS = ("httpx", "aiohttp", "urllib3", "requests", "socket")

# Phase 4's one exception, and it is one named file rather than a package:
# `vendor.py` is the only place a vendor client exists (§5.3). Everything else
# -- `interpreter.py` included, which takes a `Driver` it is given -- stays
# vendor-free, so this gate is still the thing that keeps a stray `import
# databento` out of the wrong module.
VENDOR_IMPORTS = ("databento",)
VENDOR_MODULE = "vendor.py"

# Phase 5's split gives `http.client` the same treatment, for the same reason:
# one named module may open a connection to the driver runtime, and it refuses
# anything but loopback or a single-label compose service name (`runtime_http`).
# A second import of `http.client` anywhere under `src/` is a second way out of
# the container, so it fails here rather than in a review.
RUNTIME_IMPORTS = ("http.client",)
RUNTIME_MODULE = "runtime_http.py"

# Phase 5.5's loop is the one part of this service with business at a forge:
# it reads a commit's checks and diff, merges a branch, and downloads the tree
# it installs from. One named module holds that, so there is one egress path to
# audit and one credential to revoke.
#
# Phase 5.6 adds a second egress path and keeps the same rule: the authoring
# agent's server, spoken to over HTTP by `openhands_agent.py`. Two files, both
# named, both reachable only from the loop on its own host -- a third
# `urllib.request` anywhere else is a third way out of the container.
EGRESS_IMPORTS = ("urllib.request", "urllib.error")
EGRESS_MODULES = ("github_api.py", "openhands_agent.py")

# The MCP surface is the one place that imports an HTTP *client* without a way
# out: `mcp_surface.py` builds it with `ASGITransport(app=app)`, so a tool call
# goes to this process's own routes and never opens a socket. The import is
# named here rather than waved through, and the test below holds it to the
# in-process transport -- an `AsyncHTTPTransport` or a real `base_url` added
# later fails that test.
IN_PROCESS_CLIENTS = ("httpx2",)
IN_PROCESS_MODULE = "mcp_surface.py"


def test_unusable_store_is_up_but_broken(tmp_path):
    settings = Settings(
        db_path="/proc/definitely-not-writable/requests.db",
        tokens={"tok": frozenset({"read", "submit"})},
        plan_types_dir=REPO_ROOT / "plan_types",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        # liveness stays honest: /healthz must not fail, or a DB fault puts the
        # container in a restart loop -- but it stops claiming the store is fine
        assert client.get("/healthz").json() == {
            "status": "ok",
            "store": "unavailable",
        }

        response = client.get("/v1/requests", headers=auth("tok"))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "store_unavailable"
        # A path that cannot exist is not a stale cache: the request-time retry
        # must not turn a permanent fault into a 200 or a crash.
        assert client.get("/v1/requests", headers=auth("tok")).status_code == 503


def test_a_view_over_a_missing_table_is_healed(tmp_path):
    """The exact shape build 48 died of: views standing over no `requests`.

    Two containers during a deploy share one file; the old one's DDL can leave
    the new one holding `v_pending` while the table it selects is gone. The views
    are queries, so dropping them is free and the schema can be re-applied --
    without the heal, `connect` fails and every route answers 503.
    """
    path = str(tmp_path / "requests.db")
    store = Store(path)
    asyncio.run(store.connect())
    asyncio.run(store.close())

    conn = sqlite3.connect(path)
    views = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'view'"
    ).fetchall()
    assert len(views) == 5
    for name, _ in views:
        conn.execute(f"DROP VIEW {name}")
    conn.execute("DROP TABLE requests")
    for _, sql in views:  # the views survive their table, which SQLite allows
        conn.execute(sql)
    conn.commit()
    conn.close()

    healed = Store(path)
    asyncio.run(healed.connect())
    assert asyncio.run(healed.overview()) == []
    asyncio.run(healed.close())


def test_a_store_that_could_not_open_retries_on_the_next_request(tmp_path, monkeypatch):
    """A startup failure is not a sentence; a request is allowed to try again."""
    # The real interval is 5s, which is right for a service and useless in a
    # test: here the point is that a *later* request succeeds, not that it waits.
    monkeypatch.setattr("data_download.app.RECONNECT_INTERVAL_SECONDS", 0.0)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")  # mkdir now fails, not the file check
    settings = Settings(
        db_path=str(blocked / "requests.db"),
        tokens={"tok": frozenset({"read", "submit"})},
        plan_types_dir=REPO_ROOT / "plan_types",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/healthz").json()["store"] == "unavailable"
        assert client.get("/v1/requests", headers=auth("tok")).status_code == 503

        # Whatever the obstruction was, it is gone now -- as an old container
        # exiting is -- and the process must heal rather than wait for a restart.
        blocked.unlink()
        assert client.get("/v1/requests", headers=auth("tok")).status_code == 200
        assert client.get("/healthz").json()["store"] == "ready"


def test_payload_hash_is_order_and_whitespace_independent():
    a = {"kind": "plan", "params": {"b": 2, "a": [1, 2]}, "verify": {"on_failure": "quarantine"}}
    b = {"verify": {"on_failure": "quarantine"}, "params": {"a": [1, 2], "b": 2}, "kind": "plan"}
    assert payload_hash(a) == payload_hash(b)

    c = {"kind": "plan", "params": {"a": [2, 1], "b": 2}, "verify": {"on_failure": "quarantine"}}
    assert payload_hash(a) != payload_hash(c)


def test_token_parsing_forms():
    assert parse_tokens(None) == {}
    assert parse_tokens("bare-token") == {"bare-token": frozenset({"submit", "read"})}
    assert parse_tokens("tok-a:submit+read;tok-b:read") == {
        "tok-a": frozenset({"submit", "read"}),
        "tok-b": frozenset({"read"}),
    }
    assert parse_tokens('{"tok-a": ["submit"], "tok-b": "read"}') == {
        "tok-a": frozenset({"submit"}),
        "tok-b": frozenset({"read"}),
    }
    # A retired scope is still *parseable*, so a deployed .env written for the
    # old approve token does not fail as a whole and take the agent's token with
    # it -- but it grants nothing.
    assert parse_tokens("tok-a:approve") == {"tok-a": frozenset({"approve"})}


def test_bad_token_config_does_not_crash_startup():
    settings = load_settings({"DATA_DOWNLOAD_API_TOKENS": "{not json", "DATA_DOWNLOAD_DB": ":memory:"})
    assert settings.tokens == {}


def test_approvers_are_parsed_from_one_line_and_compared_lowercased():
    assert parse_approvers(None) == frozenset()
    assert parse_approvers("   ") == frozenset()
    assert parse_approvers("nick@example.com") == frozenset({"nick@example.com"})
    assert parse_approvers("Nick@Example.com, other@example.com") == frozenset(
        {"nick@example.com", "other@example.com"}
    )
    # Whitespace is as good a separator as a comma: this is a line in a .env.
    assert parse_approvers("a@example.com  b@example.com") == frozenset(
        {"a@example.com", "b@example.com"}
    )


def test_an_unset_approver_list_is_an_empty_one_not_a_crash():
    """Fail closed, and say so at startup: a door that refuses everybody looks
    like a broken button unless the log names the missing setting."""
    settings = load_settings({"DATA_DOWNLOAD_DB": ":memory:"})
    assert settings.approvers == frozenset()


def test_unset_db_path_falls_back_to_memory(monkeypatch):
    monkeypatch.delenv("DATA_DOWNLOAD_DB", raising=False)
    settings = load_settings({})
    assert settings.db_path == ":memory:"
    assert settings.port == 8820


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: p.name)
def test_no_vendor_client_or_network_import(path: Path):
    """No code path in this phase can reach a vendor: no client, no socket."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for module in imported:
        if module.startswith(IN_PROCESS_CLIENTS):
            assert path.name == IN_PROCESS_MODULE, (
                f"{path.name} imports {module}; only {IN_PROCESS_MODULE} may, and "
                "only over the in-process ASGI transport"
            )
            continue
        assert not module.startswith(FORBIDDEN_IMPORTS), f"{path.name} imports {module}"
        if module.startswith(VENDOR_IMPORTS):
            assert path.name == VENDOR_MODULE, (
                f"{path.name} imports {module}; only {VENDOR_MODULE} may, because "
                "the client stays in one place (§5.3)"
            )
        if module.startswith(RUNTIME_IMPORTS):
            assert path.name == RUNTIME_MODULE, (
                f"{path.name} imports {module}; only {RUNTIME_MODULE} may open a "
                "connection to the driver runtime, so there is one call site to audit"
            )
        if module.startswith(EGRESS_IMPORTS):
            assert path.name in EGRESS_MODULES, (
                f"{path.name} imports {module}; only {', '.join(EGRESS_MODULES)} "
                "may leave this process, so the loop's egress and credentials "
                "live in named files"
            )


def test_the_mcp_client_cannot_leave_this_process():
    """The MCP surface's one import of an HTTP client buys no socket.

    `mcp_surface.py` is exempt from the egress gate because it names itself a
    client only to speak to this app, in-process. That excuse is worth exactly
    as much as the transport it builds, so the transport is what is checked:
    the module may touch `AsyncClient`, `ASGITransport` and `Response` from
    `httpx2` and nothing else. A `AsyncHTTPTransport`, an `HTTPTransport`, or a
    client built without a transport fails here, which is the difference between
    "a client whose address is a name for an app object" and a way out.
    """
    tree = ast.parse((SRC / IN_PROCESS_MODULE).read_text(encoding="utf-8"))
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "httpx2"
    }
    assert used <= {"AsyncClient", "ASGITransport", "Response"}, used

    source = (SRC / IN_PROCESS_MODULE).read_text(encoding="utf-8")
    assert "httpx2.ASGITransport(app=app)" in source, (
        "the MCP client must be built over the app object, not an address"
    )


def test_an_older_store_gains_the_new_columns_on_start(tmp_path):
    """A database created before `why`/`max_usd` were columns must keep working.

    `CREATE TABLE IF NOT EXISTS` is a no-op on an existing file, so the two
    columns have to be added before the schema -- whose `v_requests` selects
    them -- is applied. The deployed store has rows in it, which makes this the
    one migration path that actually runs in anger.
    """
    import sqlite3

    from data_download.db import SCHEMA_PATH

    path = tmp_path / "older.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute(
        "INSERT INTO requests (id, state, body_kind, plan_type, job_json, "
        "params_json, payload_hash, intent_json, estimate_usd, blocked_reason, "
        "plan_origin, why, max_usd, created_at, updated_at) VALUES "
        "('req_old', 'blocked', 'intent', NULL, NULL, '{}', 'hash-old', ?, NULL, "
        "'awaiting_plan', NULL, NULL, NULL, '2026-11-14T00:00:00Z', '2026-11-14T00:00:00Z')",
        ('{"intent": {"dataset": "XNAS.ITCH"}, "max_usd": 120, "why": "old"}',),
    )
    # Back to the shape the first deployment has: no columns, old view.
    conn.executescript(
        "DROP VIEW v_requests; "
        "ALTER TABLE requests DROP COLUMN why; "
        "ALTER TABLE requests DROP COLUMN max_usd;"
    )
    conn.commit()
    conn.close()

    settings = Settings(
        db_path=str(path),
        tokens={"tok": frozenset({"read"})},
        plan_types_dir=REPO_ROOT / "plan_types",
    )
    with TestClient(create_app(settings)) as client:
        book = client.get("/ui/dashboard.json", headers=auth("tok")).json()

    assert [row["id"] for row in book["rows"]] == ["req_old"]
    # ... and so does the reason, out of the same verbatim body. A row written
    # before the column must not read as "no reason was given".
    assert book["rows"][0]["why"] == "old" 
    # ... and the ceiling still comes out of the verbatim intent body.
    assert book["rows"][0]["max_usd"] == 120


def test_the_service_gives_its_logger_somewhere_to_write(monkeypatch):
    """uvicorn configures `uvicorn.*` and leaves root alone.

    So an `info` line from this package propagated to a root logger with no
    handler: dropped silently, unless it was a warning and logging's last-resort
    handler printed it. The one that mattered was `code_ref`, which names the
    commit a container is running -- absent from every deployment's log, which
    is the one place it is needed.

    Asserted on the installer rather than through a live app because pytest's
    logging plugin replaces the handlers it finds.
    """
    import logging

    from data_download.app import _configure_logging

    logger = logging.getLogger("data_download")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logging.WARNING)

    _configure_logging("info")
    assert len(logger.handlers) == 1
    assert logger.isEnabledFor(logging.INFO)

    # Idempotent, or a second app (a test client, a reload) doubles every line.
    _configure_logging("info")
    assert len(logger.handlers) == 1
