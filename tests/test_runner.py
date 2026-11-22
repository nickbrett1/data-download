"""The runner: an approved row in, bytes under `datasets`, a settled row out (§8).

The runner is the first thing in the repo that writes bytes on behalf of an
approved request, so what is asserted here is not "the download works" -- it
cannot be, the driver is a stub -- but what is true whatever the vendor does:

* the claim is the permission, and a row that was not claimed is not run;
* only the leaseholder can settle, so a reclaimed attempt writes nothing;
* the bytes land under `datasets/<vendor>/<dataset>/<request_id>/files/`, and the
  `landed_sha256` on the row is the hash of exactly those bytes;
* the verdict decides the state, and `failed` never carries a landing digest;
* **the driver is found, driven and released over the wire** -- discovery by
  vendor, a session per run, and `driver_not_ready` when the runtime has no
  driver for this vendor or this process has no token.

Since phase C the driver is in another container, so every test here runs the
real path: `FakeRuntime` is a real HTTP server on a real port, and the runner
reaches it exactly as it reaches the runtime in production. That is why the
fixture exists rather than a stub driver injected into `run_one` -- an injected
fake would test the injection, not the deployment.

Plus the honesty test the stub exists for: `dryrun.py` and the driver artefact
must believe the same shapes, or one of them is lying about a download nobody
has run yet.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import PLAN_BODY, REPO_ROOT, auth, iso
from data_download import interpreter
from data_download import job as job_mod
from data_download.db import ClaimRefused, Store, shift_seconds
from data_download.driver_client import RemoteDriver
from data_download.dryrun import OfflineDatabentoDriver
from data_download.profiles.databento_batch_download import bind as bind_plan
from data_download.runner import (
    RUNNER_VERSION,
    RunRefused,
    bundle_digest,
    destination,
    run_one,
)
from fake_runtime import FakeRuntime


@pytest.fixture
def wire(tmp_path):
    """The driver runtime, in this process but not in this call stack.

    It shares the runner's datasets directory because in production both
    containers mount the store at the same absolute path -- the one deployment
    invariant the protocol depends on. It calls itself Databento's driver so the
    registry's vendor actually binds to it.
    """
    with FakeRuntime(tmp_path / "datasets", vendor="Databento", driver="databento") as runtime:
        yield runtime


def _run(coro):
    return asyncio.run(coro)


async def _call(path: Path, method: str, *args, **kwargs):
    store = Store(str(path))
    await store.connect()
    try:
        return await getattr(store, method)(*args, **kwargs)
    finally:
        await store.close()


def _file(client, body=None) -> str:
    response = client.post("/v1/requests", json=body or PLAN_BODY, headers=auth())
    assert response.status_code == 202, response.text
    return response.json()["request_id"]


def _approved(client, settings, body=None) -> str:
    request_id = _file(client, body)
    _run(_call(settings.db_path, "approve", request_id, "nick"))
    return request_id


def _events(path: Path, request_id: str) -> list[dict]:
    return _run(_call(path, "events_for", request_id))


async def _run_in_store(
    path: Path, request_id: str, *, datasets_dir, owner, mode="clean", wire
):
    store = Store(str(path))
    await store.connect()
    try:
        return await run_one(
            request_id,
            store=store,
            datasets_dir=datasets_dir,
            owner=owner,
            mode=mode,
            driver_url=wire.url,
            driver_token=wire.token,
        )
    finally:
        await store.close()


def _land(client, settings, tmp_path, wire, *, mode="clean", body=None, owner="host:1:abc"):
    request_id = _approved(client, settings, body)
    datasets = tmp_path / "datasets"
    result = _run(
        _run_in_store(
            settings.db_path,
            request_id,
            datasets_dir=datasets,
            owner=owner,
            mode=mode,
            wire=wire,
        )
    )
    return request_id, datasets, result


# -- the clean run --------------------------------------------------------


def test_a_clean_run_lands_bytes_under_the_dataset_and_settles(
    client, settings, tmp_path, wire
):
    request_id, datasets, result = _land(client, settings, tmp_path, wire)

    assert result.state == "succeeded"
    assert result.verdict == interpreter.PASS
    assert result.settled is True
    assert len(result.files) == 2  # 2026-10-01 -> 2026-10-03, split by day

    root = datasets / "databento" / "GLBX.MDP3" / request_id
    landed = sorted(p.name for p in (root / "files").iterdir())
    assert landed == ["GLBX.MDP3-2026-10-01.dbn.zst", "GLBX.MDP3-2026-10-02.dbn.zst"]

    for entry in result.files:
        path = root / "files" / entry["name"]
        payload = path.read_bytes()
        assert payload.startswith(b"STUB:")
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
        assert entry["bytes"] == len(payload)

    stored = _run(_call(settings.db_path, "get_request", request_id))
    assert stored["state"] == "succeeded"
    assert stored["owner"] == "host:1:abc"
    assert stored["lease_until"] is None
    assert stored["landed_sha256"] == bundle_digest(result.files)
    assert stored["errors_json"] is None


def test_the_landed_digest_is_the_hash_of_the_landed_bytes(
    client, settings, tmp_path, wire
):
    request_id, datasets, result = _land(client, settings, tmp_path, wire)

    root = datasets / "databento" / "GLBX.MDP3" / request_id / "files"
    manifest = "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
        for p in sorted(root.iterdir())
    )
    assert result.landed_sha256 == hashlib.sha256(manifest.encode()).hexdigest()


def test_provenance_sits_beside_files_and_never_inside_it(
    client, settings, tmp_path, wire
):
    """Inside `dest`, `landing.json` would be staged and absent from the vendor
    manifest -- the verifier would correctly call a good landing a contradiction."""
    request_id, datasets, result = _land(client, settings, tmp_path, wire)
    root = datasets / "databento" / "GLBX.MDP3" / request_id

    landing = json.loads((root / "landing.json").read_text())
    assert landing["runner_version"] == RUNNER_VERSION
    assert landing["state"] == "succeeded"
    assert len(landing["files"]) == 2
    assert {f["name"] for f in landing["files"]} == {
        "GLBX.MDP3-2026-10-01.dbn.zst",
        "GLBX.MDP3-2026-10-02.dbn.zst",
    }
    assert "landing.json" not in {p.name for p in (root / "files").iterdir()}


def test_the_landed_event_names_the_owner_and_the_digest(
    client, settings, tmp_path, wire
):
    request_id, _, result = _land(client, settings, tmp_path, wire)
    landed = [e for e in _events(settings.db_path, request_id) if e["event"] == "landed"]
    assert len(landed) == 1
    assert landed[0]["actor_kind"] == "runner"
    detail = json.loads(landed[0]["detail_json"])
    assert detail["owner"] == "host:1:abc"
    assert detail["landed_sha256"] == result.landed_sha256
    assert detail["files"] == 2
    assert detail["dest"].endswith(f"/{request_id}/files")


# -- the verdict decides the state ----------------------------------------


def _marked_body() -> dict:
    body = json.loads(json.dumps(PLAN_BODY))
    body["verify"] = {"on_failure": "commit_marked"}
    return body


def test_a_shortfall_commits_marked_and_carries_its_errors(
    client, settings, tmp_path, wire
):
    request_id, datasets, result = _land(
        client, settings, tmp_path, wire, mode="short", body=_marked_body()
    )

    assert result.state == "landed_with_errors"
    assert result.verdict == interpreter.SHORTFALL
    assert result.errors and result.errors["missing"] == ["GLBX.MDP3-2026-10-02.dbn.zst"]
    assert result.landed_sha256 is not None
    assert len(result.files) == 1

    stored = _run(_call(settings.db_path, "get_request", request_id))
    assert stored["state"] == "landed_with_errors"
    assert json.loads(stored["errors_json"])["missing"]
    assert stored["landed_sha256"] == result.landed_sha256

    events = _events(settings.db_path, request_id)
    assert [e["event"] for e in events].count("landed") == 1
    # `landed_with_errors` is a landing, not a failure, and the trail says which.
    assert json.loads(events[-1]["detail_json"])["state"] == "landed_with_errors"


def test_a_contradiction_settles_failed_and_carries_no_digest(
    client, settings, tmp_path, wire
):
    request_id, datasets, result = _land(
        client, settings, tmp_path, wire, mode="nothing", body=_marked_body()
    )

    assert result.state == "failed"
    assert result.verdict == interpreter.CONTRADICTION
    assert result.landed_sha256 is None
    assert (datasets / "databento" / "GLBX.MDP3" / request_id / "files").is_dir()
    assert list((datasets / "databento" / "GLBX.MDP3" / request_id / "files").iterdir()) == []

    stored = _run(_call(settings.db_path, "get_request", request_id))
    assert stored["state"] == "failed"
    assert stored["landed_sha256"] is None
    assert stored["errors_json"] is not None


# -- the claim is the permission ------------------------------------------


def test_a_draft_is_not_run_and_stays_draft(client, settings, tmp_path, wire):
    request_id = _file(client)
    with pytest.raises(ClaimRefused) as caught:
        _run(
            _run_in_store(
                settings.db_path,
                request_id,
                datasets_dir=tmp_path / "datasets",
                owner="host:1:abc",
                wire=wire,
            )
        )
    assert caught.value.reason == "draft"

    stored = _run(_call(settings.db_path, "get_request", request_id))
    assert stored["state"] == "draft"
    assert not (tmp_path / "datasets").exists()


def test_a_row_claimed_by_another_runner_is_not_run(client, settings, tmp_path, wire):
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "host:9:other"))

    with pytest.raises(ClaimRefused) as caught:
        _run(
            _run_in_store(
                settings.db_path,
                request_id,
                datasets_dir=tmp_path / "datasets",
                owner="host:1:abc",
                wire=wire,
            )
        )
    assert caught.value.reason == "leased"
    assert not (tmp_path / "datasets").exists()


def test_a_settled_row_is_not_run_again(client, settings, tmp_path, wire):
    request_id, _, _ = _land(client, settings, tmp_path, wire)
    with pytest.raises(ClaimRefused) as caught:
        _run(
            _run_in_store(
                settings.db_path,
                request_id,
                datasets_dir=tmp_path / "datasets",
                owner="host:1:abc",
                wire=wire,
            )
        )
    assert caught.value.reason == "settled"


def test_no_datasets_directory_means_no_run_at_all(client, settings, tmp_path, wire):
    """A refusal before the claim: the row must not be left `running` (and
    leased) because the operator forgot an environment variable."""
    request_id = _approved(client, settings)
    with pytest.raises(RunRefused) as caught:
        _run(
            _run_in_store(
                settings.db_path,
                request_id,
                datasets_dir=None,
                owner="host:1:abc",
                wire=wire,
            )
        )
    assert caught.value.reason == "not_configured"
    assert _run(_call(settings.db_path, "get_request", request_id))["state"] == "approved"


def test_a_row_with_no_vendor_in_the_registry_fails_instead_of_guessing(
    client, settings, tmp_path, wire
):
    """The tree starts with the seller. If the registry cannot name one, the
    runner must not invent a folder -- and must not leave the row `running`."""
    request_id = _approved(client, settings)
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute(
            "UPDATE requests SET plan_type = 'retired_plan' WHERE id = ?", (request_id,)
        )
        conn.commit()
    finally:
        conn.close()

    result = _run(
        _run_in_store(
            settings.db_path,
            request_id,
            datasets_dir=tmp_path / "datasets",
            owner="host:1:abc",
            wire=wire,
        )
    )
    assert result.state == "failed"
    assert result.errors and result.errors["error"] == "vendor_unknown"
    assert not (tmp_path / "datasets").exists()

    stored = _run(_call(settings.db_path, "get_request", request_id))
    assert stored["state"] == "failed"
    assert stored["lease_until"] is None


def test_a_dataset_cannot_be_a_path(tmp_path):
    for dataset in ("..", "../etc", "a/b", ".hidden", ""):
        with pytest.raises(RunRefused) as caught:
            destination(tmp_path, "Databento", dataset, "req_abc")
        assert caught.value.reason == "bad_dataset"

    with pytest.raises(RunRefused) as caught:
        destination(tmp_path, "Databento", "GLBX.MDP3", "../../etc/passwd")
    assert caught.value.reason == "bad_request_id"

    with pytest.raises(RunRefused) as caught:
        destination(tmp_path, "../vendor", "GLBX.MDP3", "req_abc")
    assert caught.value.reason == "bad_vendor"

    # The shapes that do occur are accepted: the vendor is folded to a slug and
    # the dataset keeps its case, because that is the vendor's own vocabulary.
    assert (
        destination(tmp_path, "Databento", "GLBX.MDP3", "req_abc")
        == tmp_path / "databento" / "GLBX.MDP3" / "req_abc"
    )


def test_the_vendor_is_the_top_level_and_comes_from_the_plan_type(
    client, settings, tmp_path, wire
):
    """A dataset code is vendor vocabulary; the tree says who sold it first."""
    _, datasets, result = _land(client, settings, tmp_path, wire)
    assert result.dest is not None
    relative = result.dest.relative_to(datasets)
    assert relative.parts[:2] == ("databento", "GLBX.MDP3")


# -- settlement is the leaseholder's --------------------------------------


def test_settle_by_a_runner_that_lost_its_lease_is_refused(client, settings, tmp_path):
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "owner-a", lease_seconds=1))
    _run(_call(settings.db_path, "claim", request_id, "owner-b", now=shift_seconds(iso(), 5)))

    after = _run(_call(settings.db_path, "settle", request_id, "owner-a", "succeeded"))
    assert after["settled"] is False
    assert after["state"] == "running"
    assert after["owner"] == "owner-b"

    stored = _run(_call(settings.db_path, "get_request", request_id))
    assert stored["state"] == "running"
    assert [e["event"] for e in _events(settings.db_path, request_id)].count("landed") == 0


def test_settle_refuses_a_state_that_is_not_terminal(client, settings):
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "owner-a"))
    with pytest.raises(ValueError):
        _run(_call(settings.db_path, "settle", request_id, "owner-a", "approved"))


def test_a_lost_lease_is_reported_not_raised(client, settings, tmp_path, wire):
    """A run that finishes after being reclaimed really did finish; the row is
    simply not its to write. That is the reclaim working, so it is a result."""
    request_id = _approved(client, settings)
    store = Store(str(settings.db_path))
    _run(store.connect())
    claimed = _run(store.claim(request_id, "owner-a", lease_seconds=1))
    assert claimed["state"] == "running"
    _run(store.claim(request_id, "owner-b", now=shift_seconds(iso(), 5)))

    # Finish the attempt as the now-stale claimant. Claim is not re-taken: the
    # runner uses the lease it holds, which is exactly the case this covers.
    row = _run(store.get_request(request_id))
    job = json.loads(row["job_json"])
    job_mod.validate(job)
    root = tmp_path / "datasets" / "GLBX.MDP3" / request_id
    root.mkdir(parents=True)
    driver = RemoteDriver(
        root, "clean", url=wire.url, token=wire.token,
        driver="databento", driver_version="1",
    )
    try:
        interpreter.run(job, driver)
    finally:
        driver.close()

    after = _run(store.settle(request_id, "owner-a", "succeeded", landed_sha256="0" * 64))
    assert after["settled"] is False
    assert after["state"] == "running"
    assert _run(store.get_request(request_id))["landed_sha256"] is None


# -- the planned shapes and the executed shapes agree ----------------------


def test_the_dry_run_and_the_driver_believe_the_same_shapes(tmp_path, wire):
    """A planning fake and an executing one that disagree would make "the path
    works" prove nothing: the offline run prices two files and the real one
    lands a different number."""
    text, _ = job_mod.bind_and_seal(bind_plan(PLAN_BODY["params"], PLAN_BODY["verify"]))
    parsed = json.loads(text)

    offline = interpreter.run(parsed, OfflineDatabentoDriver("clean", tmp_path / "a"))
    root = tmp_path / "datasets" / "b" / "databento" / "GLBX.MDP3" / "req_shapes"
    root.mkdir(parents=True)
    driver = RemoteDriver(
        root, "clean", url=wire.url, token=wire.token,
        driver="databento", driver_version="1",
    )
    try:
        driven = interpreter.run(parsed, driver)
    finally:
        driver.close()

    assert offline.verdict == driven.verdict == interpreter.PASS
    assert offline.steps == driven.steps
    assert offline.detail["files"] == driven.detail["files"] == 2
    assert offline.detail["rows"] == driven.detail["rows"]


def test_the_landing_names_the_artefact_that_wrote_the_bytes(client, settings, tmp_path, wire):
    """`RemoteDriver` is the class for every driver on the planet, so the landing
    records the name and version the runtime reported instead."""
    request_id, _, result = _land(client, settings, tmp_path, wire)
    assert settings.quote is not None  # the app's pricer, never the runner's
    assert result.state == "succeeded"

    landing = json.loads((Path(result.dest).parent / "landing.json").read_text())
    assert landing["driver"] == "databento"
    assert landing["driver_version"] == "1"
    assert landing["driver_mode"] == "clean"

    detail = json.loads(
        next(
            e for e in _events(settings.db_path, request_id) if e["event"] == "landed"
        )["detail_json"]
    )
    assert detail["driver"] == "databento"


# -- the command line -----------------------------------------------------


def test_the_cli_runs_the_oldest_approved_request(client, settings, tmp_path, wire):
    request_id = _approved(client, settings)
    env = {
        **os.environ,
        "DATA_DOWNLOAD_DB": str(settings.db_path),
        "DATA_DOWNLOAD_DATASETS_DIR": str(tmp_path / "datasets"),
        "DATA_DOWNLOAD_DRIVER_RUNTIME_URL": wire.url,
        "DRIVER_RUNTIME_TOKEN": wire.token,
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "data_download.runner", "--next"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert request_id in proc.stdout
    assert "succeeded" in proc.stdout
    assert (
        tmp_path / "datasets" / "databento" / "GLBX.MDP3" / request_id / "files"
    ).is_dir()


def test_the_cli_with_nothing_to_do_is_not_an_error(settings, tmp_path):
    env = {
        **os.environ,
        "DATA_DOWNLOAD_DB": str(settings.db_path),
        "DATA_DOWNLOAD_DATASETS_DIR": str(tmp_path / "datasets"),
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    # Create the store so the CLI is not racing the fixture that made the path.
    _run(_call(settings.db_path, "view", "v_pending"))
    proc = subprocess.run(
        [sys.executable, "-m", "data_download.runner", "--next"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "nothing to do" in proc.stdout


def test_the_cli_refuses_to_guess_where_bytes_go(client, settings, tmp_path):
    request_id = _approved(client, settings)
    env = {
        **os.environ,
        "DATA_DOWNLOAD_DB": str(settings.db_path),
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    env.pop("DATA_DOWNLOAD_DATASETS_DIR", None)
    proc = subprocess.run(
        [sys.executable, "-m", "data_download.runner", request_id],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 2
    assert "not_configured" in proc.stderr
    # And the refusal happened before the claim.
    assert _run(_call(settings.db_path, "get_request", request_id))["state"] == "approved"


def test_the_cli_refuses_without_a_driver_token(client, settings, tmp_path):
    """The refusal is a sentence about a setting, before the claim."""
    request_id = _approved(client, settings)
    env = {
        **os.environ,
        "DATA_DOWNLOAD_DB": str(settings.db_path),
        "DATA_DOWNLOAD_DATASETS_DIR": str(tmp_path / "datasets"),
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    env.pop("DRIVER_RUNTIME_TOKEN", None)
    proc = subprocess.run(
        [sys.executable, "-m", "data_download.runner", request_id],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 2
    assert "driver_not_ready" in proc.stderr
    assert "DRIVER_RUNTIME_TOKEN" in proc.stderr
    assert _run(_call(settings.db_path, "get_request", request_id))["state"] == "approved"


# -- there is no token, and no driver, and both are refusals -------------


def test_no_token_refuses_before_the_claim(
    client, settings, tmp_path, wire, monkeypatch
):
    """Same shape as the datasets refusal: a row that cannot be run stays
    `approved`. Settling it `failed` would blame the vendor for a setting."""
    monkeypatch.delenv("DRIVER_RUNTIME_TOKEN", raising=False)
    request_id = _approved(client, settings)
    store = Store(str(settings.db_path))
    _run(store.connect())
    try:
        with pytest.raises(RunRefused) as caught:
            _run(
                run_one(
                    request_id,
                    store=store,
                    datasets_dir=tmp_path / "datasets",
                    owner="host:1:abc",
                    driver_url=wire.url,
                    driver_token=None,
                )
            )
    finally:
        _run(store.close())
    assert caught.value.reason == "driver_not_ready"
    assert "DRIVER_RUNTIME_TOKEN" in str(caught.value)
    assert _run(_call(settings.db_path, "get_request", request_id))["state"] == "approved"
    assert wire.requests == []  # nothing was sent at all


def test_a_vendor_with_no_installed_driver_fails_and_names_what_is_installed(
    client, settings, tmp_path
):
    """The registry says Databento sells this; the runtime has only `stub`.
    That is a refusal the operator can act on, not a vendor failure."""
    request_id = _approved(client, settings)
    with FakeRuntime(tmp_path / "datasets", vendor="Stub", driver="stub") as runtime:
        store = Store(str(settings.db_path))
        _run(store.connect())
        try:
            result = _run(
                run_one(
                    request_id,
                    store=store,
                    datasets_dir=tmp_path / "datasets",
                    owner="host:1:abc",
                    driver_url=runtime.url,
                    driver_token=runtime.token,
                )
            )
        finally:
            _run(store.close())

    assert result.state == "failed"
    assert result.verdict is None  # no driver, so no Job ran at all
    assert result.errors and result.errors["error"] == "driver_not_ready"
    assert "Databento" in result.errors["detail"]
    assert "stub v1 (Stub)" in result.errors["detail"]  # says what IS installed
    # Nothing was created: no driver means no attempt, and an empty request
    # directory would read as an attempt that produced nothing.
    assert not (tmp_path / "datasets" / "databento").exists()


def test_the_session_is_closed_when_the_run_finishes(client, settings, tmp_path, wire):
    """A session is a process in the other container. Nothing else kills it, and
    a leaked one holds a child until its deadline."""
    request_id, _, result = _land(client, settings, tmp_path, wire)
    assert result.state == "succeeded"
    assert [path for method, path in wire.requests if method == "DELETE"]
    assert wire.sessions == {}
