"""The dry run: the whole path offline, and the refusals around it (§4.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import PLAN_BODY, auth, fake_quote
from data_download import dryrun, interpreter
from data_download.app import create_app
from data_download.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def filed(tmp_path):
    """A real request, filed through the real API into a temp store."""
    db = tmp_path / "requests.db"
    settings = Settings(
        db_path=str(db),
        tokens={"tok-submit": frozenset({"submit", "read"})},
        plan_types_dir=REPO_ROOT / "plan_types",
        # Phase 4: a submit is priced. The dry run is offline, so the pricer is
        # the suite's fake -- still an injected callable, still no network.
        quote=fake_quote,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/requests", json=PLAN_BODY, headers=auth("tok-submit"))
        assert response.status_code == 202, response.text
        yield str(db), response.json()["request_id"]


def _run(db: str, request_id: str, *extra: str):
    return dryrun.main(["--request", request_id, "--db", db, *extra])


@pytest.mark.parametrize(
    ("mode", "expected", "exit_code"),
    [
        ("nothing", interpreter.CONTRADICTION, 1),
        ("short", interpreter.SHORTFALL, 1),
        ("clean", interpreter.PASS, 0),
    ],
)
def test_each_mode_terminates_in_its_typed_verdict(filed, capsys, mode, expected, exit_code):
    db, request_id = filed
    assert _run(db, request_id, "--mode", mode) == exit_code
    out = capsys.readouterr().out
    assert f"verdict: {expected}" in out
    # Not an exception, and not a silent success: the run reaches a verdict.
    assert "error:" not in out


def test_shortfall_is_not_read_as_a_contradiction(filed, capsys):
    """Both end badly and are different classes; a harness that prints the same
    thing for both has not implemented the distinction."""
    db, request_id = filed
    _run(db, request_id, "--mode", "short")
    short = capsys.readouterr().out
    _run(db, request_id, "--mode", "nothing")
    nothing = capsys.readouterr().out
    assert "verdict: shortfall" in short
    assert "verdict: contradiction" in nothing
    assert "shortfall" not in nothing


def test_verify_can_succeed_at_all(filed, capsys):
    """`clean` is the only evidence in this phase that verify passes anything."""
    db, request_id = filed
    assert _run(db, request_id, "--mode", "clean") == 0
    out = capsys.readouterr().out
    assert "verdict: pass (commit)" in out
    assert "state: unchanged" in out


def test_a_dry_run_changes_nothing(filed, capsys):
    import sqlite3

    db, request_id = filed

    def snapshot():
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT state, updated_at, job_hash, errors_json, landed_sha256 "
                "FROM requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            events = conn.execute(
                "SELECT COUNT(*) FROM events WHERE request_id = ?", (request_id,)
            ).fetchone()[0]
            return row, events
        finally:
            conn.close()

    before = snapshot()
    _run(db, request_id, "--mode", "clean")
    assert snapshot() == before
    assert "state: unchanged" in capsys.readouterr().out


def test_the_same_request_twice_gives_the_same_job_and_verdict(filed, capsys):
    db, request_id = filed
    _run(db, request_id, "--mode", "clean")
    first = capsys.readouterr().out
    _run(db, request_id, "--mode", "clean")
    second = capsys.readouterr().out
    # The worker path differs (a fresh temp dir each run); everything that is
    # about the Job does not.
    assert _without_worker(first) == _without_worker(second)
    assert "job: sha256" in first


def _without_worker(text: str) -> list[str]:
    return [
        line for line in text.splitlines()
        if not line.startswith("worker:") and not line.startswith("kept:")
    ]


def test_an_unregistered_plan_is_refused_before_anything_is_bound(filed, capsys):
    db, request_id = filed
    assert _run(db, request_id, "--plan", "not_a_plan") == 2
    out = capsys.readouterr().out
    assert "plan_type_unknown" in out
    assert "job:" not in out
    assert "verdict:" not in out


def test_a_missing_request_is_an_error_not_a_verdict(tmp_path, capsys):
    assert dryrun.main(["--request", "req_nope", "--db", str(tmp_path / "none.db")]) == 2
    assert "no request" in capsys.readouterr().err


def test_the_dry_run_writes_nothing_outside_its_temp_directory(filed, tmp_path, capsys):
    db, request_id = filed
    before = sorted(p.name for p in tmp_path.iterdir())
    _run(db, request_id, "--mode", "clean")
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_the_offline_driver_is_unreachable_from_the_service():
    """No route and no import path reaches the fake: it is not a second surface."""
    import ast
    import subprocess
    import sys

    app_path = REPO_ROOT / "src" / "data_download" / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    # A docstring may mention the dry run; an import would wire it in.
    assert not any("dryrun" in name or "interpreter" in name for name in imported)

    code = (
        "import sys, data_download.app;"
        "print('data_download.dryrun' in sys.modules, "
        "'data_download.interpreter' in sys.modules)"
    )
    finished = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert finished.stdout.strip() == "False False"


def test_the_fake_driver_makes_the_calls_it_says_it_makes(tmp_path):
    driver = dryrun.OfflineDatabentoDriver("clean", tmp_path / "worker")
    (tmp_path / "worker").mkdir()
    job = {
        "plan": "databento_batch_download",
        "steps": [
            {"kind": "resolve", "kwargs": {"dataset": "D", "symbols": ["S"], "stype_in": "raw_symbol"}},
            {"kind": "quote", "kwargs": {}},
            {"kind": "submit", "kwargs": {"dataset": "D", "start": "2026-01-01", "end": "2026-01-03"}},
            {"kind": "poll", "kwargs": {"interval_s": 30}},
            {"kind": "download", "kwargs": {"dest": str(tmp_path / "worker")}},
            {"kind": "convert", "kwargs": {"from": "dbn", "to": "parquet"}},
            {"kind": "verify", "kwargs": {"on_failure": "quarantine"}},
        ],
    }
    result = interpreter.run(job, driver)
    assert result.verdict == interpreter.PASS
    assert driver.calls == [
        "resolve", "cost", "submit", "poll", "list_files", "fetch", "convert", "verify",
    ]
    # Two placeholder files, hashed for real, and nothing written outside dest.
    assert sorted(p.name for p in (tmp_path / "worker").iterdir()) == [
        "D-2026-01-01.dbn.zst", "D-2026-01-02.dbn.zst",
    ]


def test_the_fake_asserts_nothing_about_price(filed, capsys):
    db, request_id = filed
    _run(db, request_id, "--mode", "clean")
    out = capsys.readouterr().out
    assert "quote_usd" in out
    assert str(dryrun.FAKE_QUOTE_USD) in out
    assert "estimate_usd" not in out


def test_the_harness_refuses_to_change_state_even_when_the_verdict_fails(filed, capsys):
    db, request_id = filed
    _run(db, request_id, "--mode", "nothing")
    out = capsys.readouterr().out
    assert "verdict: contradiction" in out
    assert "state: unchanged" in out
