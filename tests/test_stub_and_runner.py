"""Phase 5: the stub vendor and the runner that settles a request (§8).

The runner is the first thing in the repo that writes bytes on behalf of an
approved request, so what is asserted here is not "the download works" -- it
cannot be, the driver is a stub -- but the four things that are true whatever
the vendor does:

* the claim is the permission, and a row that was not claimed is not run;
* only the leaseholder can settle, so a reclaimed attempt writes nothing;
* the bytes land under `datasets/<dataset>/<request_id>/files/`, and the
  `landed_sha256` on the row is the hash of exactly those bytes;
* the verdict decides the state, and `failed` never carries a landing digest.

Plus the honesty test the stub exists for: it and `dryrun.py` must believe the
same shapes, or one of them is lying about a download nobody has run yet.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import PLAN_BODY, REPO_ROOT, auth, iso
from data_download import interpreter
from data_download import job as job_mod
from data_download.db import ClaimRefused, Store, shift_seconds
from data_download.dryrun import OfflineDatabentoDriver
from data_download.profiles.databento_batch_download import bind as bind_plan
from data_download.runner import (
    RUNNER_VERSION,
    RunRefused,
    bundle_digest,
    destination,
    run_one,
)
from data_download.stub import StubDatabentoDriver


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


async def _run_in_store(path: Path, request_id: str, *, datasets_dir, owner, mode="clean"):
    store = Store(str(path))
    await store.connect()
    try:
        return await run_one(
            request_id,
            store=store,
            datasets_dir=datasets_dir,
            owner=owner,
            mode=mode,
        )
    finally:
        await store.close()


def _land(client, settings, tmp_path, *, mode="clean", body=None, owner="host:1:abc"):
    request_id = _approved(client, settings, body)
    datasets = tmp_path / "datasets"
    result = _run(
        _run_in_store(
            settings.db_path, request_id, datasets_dir=datasets, owner=owner, mode=mode
        )
    )
    return request_id, datasets, result


# -- the clean run --------------------------------------------------------


def test_a_clean_run_lands_bytes_under_the_dataset_and_settles(client, settings, tmp_path):
    request_id, datasets, result = _land(client, settings, tmp_path)

    assert result.state == "succeeded"
    assert result.verdict == interpreter.PASS
    assert result.settled is True
    assert len(result.files) == 2  # 2026-10-01 -> 2026-10-03, split by day

    root = datasets / "GLBX.MDP3" / request_id
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


def test_the_landed_digest_is_the_hash_of_the_landed_bytes(client, settings, tmp_path):
    request_id, datasets, result = _land(client, settings, tmp_path)

    root = datasets / "GLBX.MDP3" / request_id / "files"
    manifest = "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
        for p in sorted(root.iterdir())
    )
    assert result.landed_sha256 == hashlib.sha256(manifest.encode()).hexdigest()


def test_provenance_sits_beside_files_and_never_inside_it(client, settings, tmp_path):
    """Inside `dest`, `landing.json` would be staged and absent from the vendor
    manifest -- the verifier would correctly call a good landing a contradiction."""
    request_id, datasets, result = _land(client, settings, tmp_path)
    root = datasets / "GLBX.MDP3" / request_id

    landing = json.loads((root / "landing.json").read_text())
    assert landing["runner_version"] == RUNNER_VERSION
    assert landing["driver"] == "StubDatabentoDriver"
    assert landing["state"] == "succeeded"
    assert len(landing["files"]) == 2
    assert {f["name"] for f in landing["files"]} == {
        "GLBX.MDP3-2026-10-01.dbn.zst",
        "GLBX.MDP3-2026-10-02.dbn.zst",
    }
    assert "landing.json" not in {p.name for p in (root / "files").iterdir()}


def test_the_landed_event_names_the_owner_and_the_digest(client, settings, tmp_path):
    request_id, _, result = _land(client, settings, tmp_path)
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


def test_a_shortfall_commits_marked_and_carries_its_errors(client, settings, tmp_path):
    request_id, datasets, result = _land(
        client, settings, tmp_path, mode="short", body=_marked_body()
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


def test_a_contradiction_settles_failed_and_carries_no_digest(client, settings, tmp_path):
    request_id, datasets, result = _land(
        client, settings, tmp_path, mode="nothing", body=_marked_body()
    )

    assert result.state == "failed"
    assert result.verdict == interpreter.CONTRADICTION
    assert result.landed_sha256 is None
    assert (datasets / "GLBX.MDP3" / request_id / "files").is_dir()
    assert list((datasets / "GLBX.MDP3" / request_id / "files").iterdir()) == []

    stored = _run(_call(settings.db_path, "get_request", request_id))
    assert stored["state"] == "failed"
    assert stored["landed_sha256"] is None
    assert stored["errors_json"] is not None


# -- the claim is the permission ------------------------------------------


def test_a_draft_is_not_run_and_stays_draft(client, settings, tmp_path):
    request_id = _file(client)
    with pytest.raises(ClaimRefused) as caught:
        _run(
            _run_in_store(
                settings.db_path,
                request_id,
                datasets_dir=tmp_path / "datasets",
                owner="host:1:abc",
            )
        )
    assert caught.value.reason == "draft"

    stored = _run(_call(settings.db_path, "get_request", request_id))
    assert stored["state"] == "draft"
    assert not (tmp_path / "datasets").exists()


def test_a_row_claimed_by_another_runner_is_not_run(client, settings, tmp_path):
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "host:9:other"))

    with pytest.raises(ClaimRefused) as caught:
        _run(
            _run_in_store(
                settings.db_path,
                request_id,
                datasets_dir=tmp_path / "datasets",
                owner="host:1:abc",
            )
        )
    assert caught.value.reason == "leased"
    assert not (tmp_path / "datasets").exists()


def test_a_settled_row_is_not_run_again(client, settings, tmp_path):
    request_id, _, _ = _land(client, settings, tmp_path)
    with pytest.raises(ClaimRefused) as caught:
        _run(
            _run_in_store(
                settings.db_path,
                request_id,
                datasets_dir=tmp_path / "datasets",
                owner="host:1:abc",
            )
        )
    assert caught.value.reason == "settled"


def test_no_datasets_directory_means_no_run_at_all(client, settings, tmp_path):
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
            )
        )
    assert caught.value.reason == "not_configured"
    assert _run(_call(settings.db_path, "get_request", request_id))["state"] == "approved"


def test_a_dataset_cannot_be_a_path(tmp_path):
    for dataset in ("..", "../etc", "a/b", ".hidden", ""):
        with pytest.raises(RunRefused) as caught:
            destination(tmp_path, dataset, "req_abc")
        assert caught.value.reason == "bad_dataset"

    with pytest.raises(RunRefused) as caught:
        destination(tmp_path, "GLBX.MDP3", "../../etc/passwd")
    assert caught.value.reason == "bad_request_id"

    # The shapes that do occur are accepted, including the dotted one.
    assert destination(tmp_path, "GLBX.MDP3", "req_abc") == tmp_path / "GLBX.MDP3" / "req_abc"


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


def test_a_lost_lease_is_reported_not_raised(client, settings, tmp_path):
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
    interpreter.run(job, StubDatabentoDriver(root, "clean"))

    after = _run(store.settle(request_id, "owner-a", "succeeded", landed_sha256="0" * 64))
    assert after["settled"] is False
    assert after["state"] == "running"
    assert _run(store.get_request(request_id))["landed_sha256"] is None


# -- the stub is honest about being one -----------------------------------


def test_the_stub_and_the_dry_run_believe_the_same_shapes(tmp_path):
    """Two fakes that disagree would make "the path works" prove nothing."""
    text, _ = job_mod.bind_and_seal(bind_plan(PLAN_BODY["params"], PLAN_BODY["verify"]))
    parsed = json.loads(text)

    offline = interpreter.run(parsed, OfflineDatabentoDriver("clean", tmp_path / "a"))
    stub = interpreter.run(parsed, StubDatabentoDriver(tmp_path / "b", "clean"))

    assert offline.verdict == stub.verdict == interpreter.PASS
    assert offline.steps == stub.steps
    assert offline.detail["files"] == stub.detail["files"] == 2
    assert offline.detail["rows"] == stub.detail["rows"]


def test_the_stub_cannot_reach_a_vendor(client, settings, tmp_path):
    """No client, no key, no network: asserted by import in the egress gate, and
    here by the fact that the run completes with no key in the settings at all."""
    request_id, _, result = _land(client, settings, tmp_path)
    assert settings.quote is not None  # the app's pricer, never the runner's
    assert result.state == "succeeded"
    landing = json.loads(
        (Path(result.dest).parent / "landing.json").read_text()
    )
    assert landing["driver"] == "StubDatabentoDriver"
    assert landing["driver_mode"] == "clean"


# -- the command line -----------------------------------------------------


def test_the_cli_runs_the_oldest_approved_request(client, settings, tmp_path):
    request_id = _approved(client, settings)
    env = {
        **os.environ,
        "DATA_DOWNLOAD_DB": str(settings.db_path),
        "DATA_DOWNLOAD_DATASETS_DIR": str(tmp_path / "datasets"),
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
    assert (tmp_path / "datasets" / "GLBX.MDP3" / request_id / "files").is_dir()


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
