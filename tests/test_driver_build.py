"""The driver loop: the state machine, the install, and the shrink to a probe.

The loop's own decisions are callables (`Steps`), so these tests are the five
answers written down: green CI, allowed paths, a passing probe, a merge and an
install. What is *not* faked is the store -- the transitions are rows, and a
state machine that only exists in a test's imagination is not one.
"""

from __future__ import annotations

import asyncio
import json
import time
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


def _fake_runtime(monkeypatch, *, sha="", status=200, answer=None):
    """A runtime that answers, and the request it was sent."""
    seen: dict = {}

    def request(host, port, method, path, *, token, body=None, timeout=30.0):
        seen.update(host=host, port=port, method=method, path=path, token=token, body=body)
        if answer is not None:
            return status, answer
        return status, {"name": "databento", "version": "1", "path": "/drivers/databento/1",
                        "sha256": sha, "replaced": False}

    monkeypatch.setattr(driver_build.runtime_http, "request", request)
    return seen


def test_the_tree_crosses_the_boundary_as_data_and_the_digest_comes_back(tmp_path, monkeypatch):
    """The runtime owns the volume, so the app sends bytes and records its digest."""
    source = _artifact(tmp_path)
    digest = driver_build.artifact_digest(source)
    seen = _fake_runtime(monkeypatch, sha=digest)

    installed = driver_build.install_over_runtime(
        source, url="http://data-download-drivers:8831", token="rt"
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/drivers/install"
    assert seen["token"] == "rt"
    assert seen["body"]["name"] == "databento"
    assert seen["body"]["version"] == "1"
    # base64 of the bytes on disk, under their relative names.
    import base64

    assert base64.b64decode(seen["body"]["files"]["driver.py"]) == (
        source / "driver.py"
    ).read_bytes()
    assert installed["artifact_sha256"] == digest
    assert installed["path"] == "/drivers/databento/1"
    # Nothing was written here: the local directory does not exist.
    assert not (tmp_path / "drivers").exists()


def test_install_refuses_a_tree_without_a_manifest(tmp_path, monkeypatch):
    source = tmp_path / "empty"
    source.mkdir()
    seen = _fake_runtime(monkeypatch)
    with pytest.raises(driver_build.LoopRefused, match="manifest"):
        driver_build.install_over_runtime(source, url="http://r:1", token="rt")
    assert seen == {}, "a tree with no manifest must not reach the runtime"


def test_a_refusal_from_the_runtime_keeps_its_code(tmp_path, monkeypatch):
    source = _artifact(tmp_path)
    _fake_runtime(
        monkeypatch,
        status=409,
        answer={"error": {"code": "install_refused", "detail": "/drivers/databento/1 already exists"}},
    )
    with pytest.raises(driver_build.LoopRefused) as caught:
        driver_build.install_over_runtime(source, url="http://r:1", token="rt")
    assert "install_refused" in str(caught.value)
    assert "already exists" in str(caught.value)


def test_a_digest_the_tree_does_not_have_is_refused(tmp_path, monkeypatch):
    """If what landed is not what was sent, no later replace could be trusted."""
    source = _artifact(tmp_path)
    _fake_runtime(monkeypatch, sha="e" * 64)
    with pytest.raises(driver_build.LoopRefused, match="digest"):
        driver_build.install_over_runtime(source, url="http://r:1", token="rt")


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


def test_a_probe_is_rooted_in_the_runtimes_view_not_the_hosts(tmp_path):
    """The loop may run on the host; the runtime knows only `/datasets`.

    Getting this wrong is not exotic: on ryzen it produced `400 bad_request:
    root '/srv/datasets/_probe/...' is outside /datasets`, which reads like a
    pricing failure and was a path translation.
    """
    assert (
        str(driver_build.probe_root("drv_1", "/datasets")) == "/datasets/_probe/drv_1"
    )
    assert (
        str(driver_build.probe_root("drv_1", "/data/datasets"))
        == "/data/datasets/_probe/drv_1"
    )
    # The default is the runtime's own mount point, never the caller's path.
    assert str(driver_build.probe_root("drv_1")) == "/datasets/_probe/drv_1"


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
        # A harness that keeps no transcript leaves no reference behind rather
        # than inventing one.
        assert done["author_ref"] is None
        assert _run(store.driver_jobs_open()) == 0
        assert job["state"] == "queued"  # the row we read was the *before* copy
    finally:
        _run(store.close())


def test_the_authoring_transcript_is_kept_when_the_harness_names_one(tmp_path):
    """The page can link the conversation that wrote a driver, but the job keeps
    the *id*: which host serves the canvas is the deployment's fact, and a stored
    URL would rot the day it moved."""
    store = _store(tmp_path)
    try:
        _job(store)
        done = _run(
            driver_build.run_once(
                store,
                _steps(
                    author=lambda row: {
                        "branch": "driver-loop/databento-1",
                        "commit_sha": "a" * 40,
                        "author_ref": "conv-9",
                    }
                ),
            )
        )
        assert done["author_ref"] == "conv-9"
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


def test_a_retry_replaces_exactly_the_install_it_recorded(tmp_path, monkeypatch):
    """A shortfall retry installs the same name and version as the turn before.

    The digest is the permission: the runtime refuses to overwrite a version it
    was not told about, and this is where the job's own record of its last
    install is carried into that request.
    """
    source = _artifact(tmp_path)
    digest = driver_build.artifact_digest(source)
    seen = _fake_runtime(monkeypatch, sha=digest)

    driver_build.install_over_runtime(
        source, url="http://r:1", token="rt", replace_sha256=digest
    )
    assert seen["body"]["replace_sha256"] == digest

    driver_build.install_over_runtime(source, url="http://r:1", token="rt")
    assert seen["body"]["replace_sha256"] is None, (
        "a first install must not claim to replace anything"
    )


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


# -- the worker --------------------------------------------------------------


def test_the_driver_worker_drains_the_queue_then_reports_nothing_to_do(tmp_path):
    """The host loop called `--once` every minute; this is that, in-process."""
    store = _store(tmp_path)
    try:
        _job(store)
        worker = driver_build.DriverWorker(store, _steps(), interval=0.01)
        row = _run(worker.once())
        assert row["state"] == "installed"
        assert _run(worker.once()) is None
    finally:
        _run(store.close())


def test_a_job_waiting_on_ci_is_a_state_the_loop_idles_on(tmp_path):
    """A pending gate is not work: spinning on it would hammer the forge."""
    store = _store(tmp_path)
    try:
        _job(store)
        worker = driver_build.DriverWorker(
            store, _steps(gate=lambda row: {"ok": False, "pending": True}), interval=0.01
        )
        row = _run(worker.once())
        assert row["state"] == "gated"
        assert row["state"] in driver_build.WAITING_STATES
    finally:
        _run(store.close())


def test_the_worker_stops_without_waiting_for_a_conversation(tmp_path):
    store = _store(tmp_path)
    try:
        async def go():
            worker = driver_build.DriverWorker(store, _steps(), interval=0.01)
            worker.start()
            await asyncio.sleep(0.05)
            await worker.stop()
            return worker

        assert _run(go())._task is None
    finally:
        _run(store.close())


def test_stopping_still_abandons_a_pass_that_outlives_the_grace(tmp_path):
    """The grace period must not become a wait: a conversation outlives it."""
    store = _store(tmp_path)
    try:
        def slow(row):
            time.sleep(5)
            return {"branch": "driver/x", "commit_sha": "a" * 40}

        async def go():
            worker = driver_build.DriverWorker(
                store, _steps(author=slow), interval=0.01, grace=0.01
            )
            worker.start()
            await asyncio.sleep(0.05)
            started = time.monotonic()
            await worker.stop()
            return time.monotonic() - started

        assert _run(go()) < 2.0
    finally:
        _run(store.close())


def test_a_pass_that_is_ending_is_not_cancelled(tmp_path):
    """The common case: the loop is between passes, so nothing is cut off.

    Cancelling inside a store call is what kills aiosqlite's worker thread (see
    `DriverWorker.stop`), so the grace period has to actually work.
    """
    store = _store(tmp_path)
    try:
        def author(row):
            raise AssertionError("no job is queued, so no step should run")

        async def go():
            worker = driver_build.DriverWorker(
                store, _steps(author=author), interval=0.01, grace=5.0
            )
            worker.start()
            await asyncio.sleep(0.05)
            started = time.monotonic()
            await worker.stop()
            return time.monotonic() - started, worker._task

        elapsed, task = _run(go())
        assert elapsed < 1.0
        assert task is None
    finally:
        _run(store.close())


def test_the_settings_name_the_key_file_the_authoring_step_hands_over(tmp_path, monkeypatch):
    """`DATA_DOWNLOAD_OPENHANDS_KEY_FILE` is only load-bearing if it is passed.

    It was set on the deployment and read by nobody: `author` called `converse`
    without it, `converse` fell back to `docker exec`, and the app container has
    no docker. The setting is now threaded through the authoring closure, which
    is the only place that knows both the settings and the call.
    """
    from types import SimpleNamespace

    seen = {}
    monkeypatch.setattr(
        driver_build.driver_author,
        "author",
        lambda ticket, **kwargs: seen.update(kwargs)
        or {"branch": "driver/x", "commit_sha": "a" * 40},
    )
    settings = SimpleNamespace(
        openhands_key_file=Path("/run/openhands/api-key.txt"),
        openhands_workdir="/projects/driver-loop",
        openhands_projects_dir="/projects",
        openhands_url="http://openhands:8000",
        openhands_agent_uid=10001,
        openhands_agent_gid=10001,
        driver_runtime_url="http://drivers:8831",
        driver_runtime_token="t",
        datasets_dir=tmp_path / "datasets",
    )
    steps = driver_build.default_steps(
        repo="o/r",
        settings=settings,
        registry=None,
        token="tok",
        workdir=tmp_path,
    )

    assert steps.author({"id": "drv_1", "vendor": "Databento"})["branch"] == "driver/x"
    assert seen["key_file"] == "/run/openhands/api-key.txt"
    assert seen["url"] == "http://openhands:8000"


def test_a_key_file_that_is_not_there_stops_the_worker_at_start_up(tmp_path):
    """The mount that did not land is found at boot, not at the first ticket."""
    from types import SimpleNamespace

    given = SimpleNamespace(
        driver_ready=True,
        driver_runtime_token="t",
        github_token="tok",
        openhands_url="http://openhands:8000",
        openhands_key_file=tmp_path / "not-mounted" / "api-key.txt",
    )
    with pytest.raises(driver_build.LoopRefused, match="is not a file this container can read"):
        driver_build.steps_from_settings(given, registry=None, workdir=tmp_path)

    given.openhands_key_file = tmp_path / "api-key.txt"
    given.openhands_key_file.write_text("k")
    given.driver_repo = "o/r"
    given.driver_base = "main"
    given.openhands_workdir = "/projects/driver-loop"
    given.openhands_projects_dir = "/projects"
    given.openhands_agent_uid = 10001
    given.openhands_agent_gid = 10001
    given.author_timeout = 60.0
    given.author_budget = 1.0
    given.author_iterations = 10
    given.driver_runtime_url = "http://drivers:8831"
    assert driver_build.steps_from_settings(given, registry=None, workdir=tmp_path)
