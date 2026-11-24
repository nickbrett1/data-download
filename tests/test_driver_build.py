"""The driver loop: the state machine, the install, and the shrink to a probe.

The loop's own decisions are callables (`Steps`), so these tests are the five
answers written down: green CI, allowed paths, a passing probe, a merge and an
install. What is *not* faked is the store -- the transitions are rows, and a
state machine that only exists in a test's imagination is not one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conftest import seed
from data_download import driver_build
from data_download.db import Store


def _run(coro):
    return asyncio.run(coro)


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "loop.db"))
    _run(store.connect())
    return store


def _steps(**over):
    base = {
        "author": lambda row: {"branch": "driver/databento", "commit_sha": "a" * 40},
        "gate": lambda row: {"ok": True, "detail": "green"},
        "changed": lambda row: ["drivers/databento/1/driver.py"],
        "probe": lambda row: {"verdict": "pass"},
        "merge": lambda row: {"number": 7},
        "install": lambda row: {"artifact_sha256": "d" * 64},
    }
    base.update(over)
    return driver_build.Steps(**base)


def _job(store, **over):
    row = _run(store.driver_job_enqueue(vendor="Databento", dataset="GLBX.MDP3"))
    for key, value in over.items():
        row = _run(store.driver_job_update(row["id"], **{key: value}))
    return row


# -- the install -------------------------------------------------------------


def _artifact(root: Path, *, vendor="Databento", name="databento", version="1") -> Path:
    source = root / "source"
    source.mkdir(parents=True)
    (source / "manifest.json").write_text(
        json.dumps({"name": name, "version": version, "vendor": vendor, "protocol": 1}),
        encoding="utf-8",
    )
    (source / "driver.py").write_text("def cost(**kwargs): return 0.0\n", encoding="utf-8")
    return source


def test_install_is_atomic_and_refuses_to_overwrite(tmp_path):
    source = _artifact(tmp_path)
    drivers = tmp_path / "drivers"
    first = driver_build.install_tree(source, drivers)
    assert (drivers / "databento" / "1" / "manifest.json").is_file()
    assert first["artifact_sha256"]
    # No staging directory survives: the rename is the only way in.
    assert sorted(p.name for p in drivers.iterdir()) == ["databento"]

    with pytest.raises(driver_build.LoopRefused, match="already exists"):
        driver_build.install_tree(source, drivers)


def test_install_refuses_a_tree_without_a_manifest(tmp_path):
    source = tmp_path / "empty"
    source.mkdir()
    with pytest.raises(driver_build.LoopRefused, match="manifest"):
        driver_build.install_tree(source, tmp_path / "drivers")


def test_driver_in_tree_picks_the_artefact_for_the_vendor(tmp_path):
    for name, vendor, version in (
        ("stub", "Stub", "1"),
        ("databento", "Databento", "1"),
        ("databento", "Databento", "2"),
    ):
        folder = tmp_path / "drivers" / name / version
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(
            json.dumps({"name": name, "version": version, "vendor": vendor}),
            encoding="utf-8",
        )
    found = driver_build.driver_in_tree(tmp_path, "Databento")
    assert found is not None and found.name == "2"
    assert driver_build.driver_in_tree(tmp_path, "Nobody") is None


# -- the shrink --------------------------------------------------------------


def test_a_probe_is_the_waiting_job_shrunk_to_one_day_and_one_symbol():
    job = {
        "plan": "market_data_window",
        "steps": [
            {"kind": "resolve", "kwargs": {"dataset": "GLBX.MDP3", "symbols": ["ES.c.0", "NQ.c.0"]}},
            {"kind": "quote", "kwargs": {"symbols": ["ES.c.0", "NQ.c.0"], "start": "2026-10-01", "end": "2026-10-05"}},
            {"kind": "submit", "kwargs": {"symbols": ["ES.c.0"], "start": "2026-10-01", "end": "2026-10-05"}},
        ],
    }
    shrunk = driver_build.probe_job(json.dumps(job))
    assert [s["kwargs"]["symbols"] for s in shrunk["steps"][:2]] == [["ES.c.0"], ["ES.c.0"]]
    for step in shrunk["steps"]:
        if "start" in step["kwargs"]:
            assert step["kwargs"]["start"] == "2026-10-01"
            assert step["kwargs"]["end"] == "2026-10-02"


# -- the state machine -------------------------------------------------------


def test_a_green_branch_installs(tmp_path):
    store = _store(tmp_path)
    try:
        job = _job(store)
        done = _run(driver_build.run_once(store, _steps()))
        assert done["state"] == "installed"
        assert done["branch"] == "driver/databento"
        assert done["commit_sha"] == "a" * 40
        assert done["artifact_sha256"] == "d" * 64
        assert done["attempts"] == 1
        assert _run(store.driver_jobs_open()) == 0
        assert job["state"] == "queued"  # the row we read was the *before* copy
    finally:
        _run(store.close())


def test_red_ci_stops_for_a_human(tmp_path):
    store = _store(tmp_path)
    try:
        _job(store)
        done = _run(
            driver_build.run_once(
                store, _steps(gate=lambda row: {"ok": False, "detail": "build 12 red"})
            )
        )
        assert done["state"] == "needs_human"
        assert "build 12 red" in done["last_error"]
    finally:
        _run(store.close())


def test_a_pending_build_waits_instead_of_becoming_a_human(tmp_path):
    """A push is pending for minutes; that is not a refusal, it is "not yet".

    The first pass must leave the job in `gated` (still work, still the loop's),
    must not spend an authoring attempt on the wait, and the second pass must
    carry on from the same branch without authoring again.
    """
    store = _store(tmp_path)
    try:
        _job(store)
        authored = {"n": 0}

        def author(row):
            authored["n"] += 1
            return {"branch": "driver/databento", "commit_sha": "a" * 40}

        waiting = _steps(
            author=author,
            gate=lambda row: {"ok": False, "pending": True, "detail": "still running: build"},
        )
        first = _run(driver_build.run_once(store, waiting))
        assert first["state"] == "gated"
        assert first["attempts"] == 1
        assert authored["n"] == 1

        # The next pass finds it again (gated is work) and the build has finished.
        done = _run(
            driver_build.run_once(
                store, _steps(author=author), job_id=first["id"])
        )
        assert done["state"] == "installed"
        assert authored["n"] == 1, "a CI wait must not author again"
        assert done["attempts"] == 1, "a CI wait is not an attempt"
    finally:
        _run(store.close())


def test_the_probe_runs_the_waiting_requests_own_job(tmp_path):
    """Not a "small run" invented for the loop: the request's Job, shrunk.

    This is the seam that was broken when the probe read `job_json` off the
    `driver_jobs` row: it went through the real store, so the join in
    `driver_job_*` is what supplies it.
    """
    store = _store(tmp_path)
    try:
        bound = json.dumps({"steps": [{"kind": "quote", "kwargs": {}}]}, sort_keys=True)
        seed(
            tmp_path / "loop.db",
            [{"id": "req_waiting", "state": "approved", "job_json": bound}],
        )
        _job(store, request_id="req_waiting")
        seen = {}

        def probe(row):
            seen["job"] = row.get("request_job_json")
            return {"verdict": "pass"}

        done = _run(driver_build.run_once(store, _steps(probe=probe)))
        assert done["state"] == "installed"
        assert seen["job"] == bound
    finally:
        _run(store.close())


def test_a_stop_says_so_on_the_request_too(tmp_path):
    """`driver_state` exists so the row can say why it is still `approved`.

    A job that gave up after writing "building" there would leave the request
    page claiming the loop is still working -- the one sentence that page exists
    to get right.
    """
    store = _store(tmp_path)
    try:
        job = _job(store)
        _run(store.driver_job_update(job["id"], request_id="req_stopped"))
        notes = []

        async def spy(request_id, value, *, detail=None, now=None):
            notes.append((request_id, value, detail))

        store.note_driver_state = spy
        done = _run(
            driver_build.run_once(
                store, _steps(gate=lambda row: {"ok": False, "detail": "build 12 red"})
            )
        )
        assert done["state"] == "needs_human"
        assert [n[:2] for n in notes] == [("req_stopped", "needs_human")]
        assert "build 12 red" in notes[0][2]["stopped"]
    finally:
        _run(store.close())


def test_the_loop_may_replace_its_own_install_but_not_a_strangers(tmp_path):
    """A shortfall retry installs the same name and version as the turn before."""
    source = _artifact(tmp_path)
    drivers = tmp_path / "drivers"
    first = driver_build.install_tree(source, drivers)

    # A stranger's install: same path, digest this job never recorded.
    with pytest.raises(driver_build.LoopRefused):
        driver_build.install_tree(source, drivers)
    # Its own: the digest it recorded last turn.
    again = driver_build.install_tree(
        source, drivers, replace_sha256=first["artifact_sha256"]
    )
    assert again["artifact_sha256"] == first["artifact_sha256"]
    assert again["path"] == first["path"]
    # And a different digest is not "its own" either.
    with pytest.raises(driver_build.LoopRefused):
        driver_build.install_tree(source, drivers, replace_sha256="e" * 64)


def test_a_file_outside_drivers_stops_the_merge(tmp_path):
    store = _store(tmp_path)
    try:
        _job(store)
        done = _run(
            driver_build.run_once(
                store,
                _steps(
                    changed=lambda row: [
                        "drivers/databento/1/driver.py",
                        "src/data_download/runner.py",
                    ]
                ),
            )
        )
        assert done["state"] == "needs_human"
        assert "src/data_download/runner.py" in done["last_error"]
    finally:
        _run(store.close())


def test_a_shortfall_sends_it_back_to_the_queue_until_attempts_run_out(tmp_path):
    store = _store(tmp_path)
    try:
        _job(store)
        steps = _steps(probe=lambda row: {"verdict": "shortfall"})
        first = _run(driver_build.run_once(store, steps))
        assert first["state"] == "queued"
        assert first["branch"] is None  # the next turn starts from a fresh branch
        assert first["attempts"] == 1
        assert _run(driver_build.run_once(store, steps))["attempts"] == 2
        third = _run(driver_build.run_once(store, steps))
        assert third["state"] == "needs_human"
        assert "shortfall" in third["last_error"]
        assert third["attempts"] == driver_build.MAX_ATTEMPTS
    finally:
        _run(store.close())


def test_a_contradiction_stops_immediately(tmp_path):
    store = _store(tmp_path)
    try:
        _job(store)
        done = _run(
            driver_build.run_once(store, _steps(probe=lambda row: {"verdict": "contradiction"}))
        )
        assert done["state"] == "needs_human"
        assert done["attempts"] == 1
    finally:
        _run(store.close())


def test_an_empty_queue_is_not_an_error(tmp_path):
    store = _store(tmp_path)
    try:
        assert _run(driver_build.run_once(store, _steps())) is None
    finally:
        _run(store.close())


def test_the_author_contract_is_a_json_line_and_a_nonzero_exit_is_a_stop(tmp_path):
    script = tmp_path / "author.sh"
    script.write_text(
        "#!/bin/sh\ncat > /dev/null\necho '{\"branch\": \"b\", \"commit_sha\": \"c\"}'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    author = driver_build._author_command(str(script))
    assert author({"vendor": "Databento"}) == {"branch": "b", "commit_sha": "c"}

    failing = tmp_path / "fail.sh"
    failing.write_text("#!/bin/sh\necho 'no idea' >&2\nexit 3\n", encoding="utf-8")
    failing.chmod(0o755)
    with pytest.raises(driver_build.LoopRefused, match="exited 3"):
        driver_build._author_command(str(failing))({"vendor": "Databento"})
