"""Non-functional gates: store failure, hashing, config, and no vendor egress."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import auth
from data_download.app import create_app
from data_download.config import Settings, load_settings, parse_tokens
from data_download.db import payload_hash

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "data_download"

FORBIDDEN_IMPORTS = ("databento", "httpx", "aiohttp", "urllib3", "requests", "socket", "http.client")


def test_unusable_store_is_up_but_broken(tmp_path):
    settings = Settings(
        db_path="/proc/definitely-not-writable/requests.db",
        tokens={"tok": frozenset({"read", "submit"})},
        plan_types_dir=REPO_ROOT / "plan_types",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        # liveness stays honest: /healthz must not touch the store, or a DB
        # fault puts the container in a restart loop
        assert client.get("/healthz").json() == {"status": "ok"}

        response = client.get("/v1/requests", headers=auth("tok"))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "store_unavailable"


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
    # approve is never implied by a bare token
    assert "approve" not in parse_tokens("bare")["bare"]


def test_bad_token_config_does_not_crash_startup():
    settings = load_settings({"DATA_DOWNLOAD_API_TOKENS": "{not json", "DATA_DOWNLOAD_DB": ":memory:"})
    assert settings.tokens == {}


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
        assert not module.startswith(FORBIDDEN_IMPORTS), f"{path.name} imports {module}"
