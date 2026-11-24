"""The datasets browser: what landed, what is landing, and what is gone (§7.8).

Two sources have to agree for this page to be worth having: the store, which
knows the vendor, dataset and state, and the datasets root, which knows the
bytes. These tests build both and assert the join -- including the cases a
listing usually gets wrong: a run that has half its files, a row whose
directory was deleted, and a path that tries to climb out of the root.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import PLAN_BODY, auth
from data_download.app import create_app
from data_download.db import Store
from data_download.runner import run_one
from fake_runtime import FakeRuntime


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def wire(tmp_path):
    """The driver runtime, sharing the store path the page reads (§the one
    invariant: both containers mount it at the same path)."""
    with FakeRuntime(tmp_path / "datasets", vendor="Databento", driver="databento") as runtime:
        yield runtime


def _client_with(settings, datasets_dir: Path) -> TestClient:
    narrowed = dataclasses.replace(settings, datasets_dir=datasets_dir)
    return TestClient(create_app(narrowed), client=("127.0.0.1", 51234))


def _file(client, body=None) -> str:
    response = client.post("/v1/requests", json=body or PLAN_BODY, headers=auth())
    assert response.status_code == 202, response.text
    return response.json()["request_id"]


async def _land(settings, datasets_dir: Path, request_id: str, wire, *, mode="clean"):
    store = Store(str(settings.db_path))
    await store.connect()
    try:
        return await run_one(
            request_id,
            store=store,
            datasets_dir=datasets_dir,
            owner="host:1:abc",
            mode=mode,
            driver_url=wire.url,
            driver_token=wire.token,
        )
    finally:
        await store.close()


async def _approve(settings, request_id: str):
    store = Store(str(settings.db_path))
    await store.connect()
    try:
        return await store.approve(request_id, "nick")
    finally:
        await store.close()


async def _approve_and_claim(settings, request_id: str, owner="host:1:abc"):
    store = Store(str(settings.db_path))
    await store.connect()
    try:
        await store.approve(request_id, "nick")
        return await store.claim(request_id, owner)
    finally:
        await store.close()


def test_a_landed_request_is_listed_under_its_vendor_and_dataset(settings, tmp_path, wire):
    datasets = tmp_path / "datasets"
    with _client_with(settings, datasets) as client:
        request_id = _file(client)
        _run(_approve(settings, request_id))
        _run(_land(settings, datasets, request_id, wire))

        body = client.get("/ui/datasets.json").json()

    assert body["configured"] is True
    assert body["totals"]["vendors"] == 1
    assert body["totals"]["requests"] == 1
    assert body["totals"]["files"] == 2
    assert body["totals"]["bytes"] > 0
    assert body["totals"]["in_flight"] == 0

    vendor = body["vendors"][0]
    assert vendor["vendor_slug"] == "databento"
    assert vendor["vendor"] == "Databento"
    group = vendor["datasets"][0]
    assert group["dataset"] == "GLBX.MDP3"

    request = group["requests"][0]
    assert request["request_id"] == request_id
    assert request["state"] == "succeeded"
    assert request["verdict"] == "pass"
    # The driver is a fact in the payload; the page does not turn it into a
    # badge (whether the bytes are real is read here, or in landing.json).
    assert request["driver"] == "databento"
    assert "stub" not in request
    assert request["schema"] == "trades"
    assert request["symbols"] == ["ES.c.0"]
    assert request["stype_in"] == "continuous"
    assert request["start"] == "2026-10-01"
    assert request["on_disk"] is True
    assert [f["name"] for f in request["files"]] == [
        "GLBX.MDP3-2026-10-01.dbn.zst",
        "GLBX.MDP3-2026-10-02.dbn.zst",
    ]
    for entry in request["files"]:
        assert entry["bytes"] == 4079
        assert entry["sha256"]
        assert entry["href"] == (
            f"/datasets/file/databento/GLBX.MDP3/{request_id}/{entry['name']}"
        )


def test_a_running_request_lists_the_bytes_it_already_has(settings, tmp_path):
    """Half a download must not look like none of one."""
    datasets = tmp_path / "datasets"
    with _client_with(settings, datasets) as client:
        request_id = _file(client)
        _run(_approve_and_claim(settings, request_id))

        landed_dir = datasets / "databento" / "GLBX.MDP3" / request_id / "files"
        landed_dir.mkdir(parents=True)
        (landed_dir / "GLBX.MDP3-2026-10-01.dbn.zst").write_bytes(b"x" * 1234)

        body = client.get("/ui/datasets.json").json()

    assert body["totals"]["in_flight"] == 1
    request = body["vendors"][0]["datasets"][0]["requests"][0]
    assert request["state"] == "running"
    assert request["verdict"] is None
    assert request["on_disk"] is True
    assert [f["name"] for f in request["files"]] == ["GLBX.MDP3-2026-10-01.dbn.zst"]
    assert request["files"][0]["bytes"] == 1234
    assert request["files"][0]["sha256"] is None  # never hashed for a listing


def test_a_settled_row_whose_directory_is_gone_says_so(settings, tmp_path, wire):
    """`succeeded` with no bytes is the one state a browser must not paper over."""
    datasets = tmp_path / "datasets"
    with _client_with(settings, datasets) as client:
        request_id = _file(client)
        _run(_approve(settings, request_id))
        _run(_land(settings, datasets, request_id, wire))
        # Somebody cleaned the share.
        import shutil

        shutil.rmtree(datasets / "databento" / "GLBX.MDP3" / request_id)

        body = client.get("/ui/datasets.json").json()

    request = body["vendors"][0]["datasets"][0]["requests"][0]
    assert request["state"] == "succeeded"
    assert request["on_disk"] is False
    assert request["files"] == []


def test_a_draft_is_not_on_the_datasets_page(settings, tmp_path):
    """A draft has no Job, so it has no vendor, no dataset and no directory."""
    with _client_with(settings, tmp_path / "datasets") as client:
        request_id = _file(client)
        body = client.get("/ui/datasets.json").json()

    assert body["vendors"] == []
    assert body["totals"]["requests"] == 0
    assert request_id  # the row exists; it is simply not a dataset


def test_a_quarantined_landing_is_not_what_you_have(settings, tmp_path, wire):
    """`failed` means the verifier refused the bytes; this page is holdings.

    The row can have files on disk -- a quarantine does not delete them -- and
    the page used to list them under a `failed` badge with "no landing is on
    disk", which answers "what do I have?" with something the pipeline has
    already rejected. The request book is where an attempt belongs.
    """
    datasets = tmp_path / "datasets"
    with _client_with(settings, datasets) as client:
        request_id = _file(client)
        _run(_approve(settings, request_id))
        # `short` ships a manifest narrower than the staged files: a
        # contradiction, so PLAN_BODY's `quarantine` decides -> `failed`.
        result = _run(_land(settings, datasets, request_id, wire, mode="short"))
        assert result.state == "failed"
        files = datasets / "databento" / "GLBX.MDP3" / request_id / "files"
        assert sorted(p.name for p in files.iterdir())  # bytes really are there

        body = client.get("/ui/datasets.json").json()
        book = client.get("/ui/dashboard.json").json()

    assert body["vendors"] == []
    assert body["totals"]["requests"] == 0
    listed = {row["id"]: row for row in book["rows"]}
    assert listed[request_id]["state"] == "failed"


def test_an_unconfigured_root_is_reported_rather_than_empty(settings):
    with _client_with(settings, None) as client:
        body = client.get("/ui/datasets.json").json()

    assert body["configured"] is False
    assert body["root"] is None
    assert body["vendors"] == []


def test_a_landed_file_downloads_as_an_attachment(settings, tmp_path, wire):
    datasets = tmp_path / "datasets"
    with _client_with(settings, datasets) as client:
        request_id = _file(client)
        _run(_approve(settings, request_id))
        _run(_land(settings, datasets, request_id, wire))

        url = f"/datasets/file/databento/GLBX.MDP3/{request_id}/GLBX.MDP3-2026-10-01.dbn.zst"
        response = client.get(url)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"STUB:")


@pytest.mark.parametrize(
    "url",
    [
        "/datasets/file/..%2F..%2Fetc/GLBX.MDP3/req_x/passwd",
        "/datasets/file/databento/..%2F..%2F/req_x/passwd",
        "/datasets/file/databento/GLBX.MDP3/..%2F..%2F/passwd",
        "/datasets/file/databento/GLBX.MDP3/req_x/..%2F..%2F..%2Fetc%2Fpasswd",
    ],
)
def test_a_file_url_cannot_climb_out_of_the_root(settings, tmp_path, url):
    with _client_with(settings, tmp_path / "datasets") as client:
        assert client.get(url).status_code == 404


def test_the_landing_json_is_not_downloadable_as_data(settings, tmp_path, wire):
    """Provenance is beside `files/`, so the only files a URL can reach are the
    ones the manifest lists -- `landing.json` is not under `files/`."""
    datasets = tmp_path / "datasets"
    with _client_with(settings, datasets) as client:
        request_id = _file(client)
        _run(_approve(settings, request_id))
        _run(_land(settings, datasets, request_id, wire))
        response = client.get(
            f"/datasets/file/databento/GLBX.MDP3/{request_id}/landing.json"
        )
    assert response.status_code == 404


def test_the_page_is_served_and_reads_nothing_itself(settings, tmp_path, built_web):
    with _client_with(settings, tmp_path / "datasets") as client:
        response = client.get("/datasets")
    assert response.status_code == 200
    assert "built" in response.text


def test_the_tree_matches_where_the_runner_writes(settings, tmp_path, wire):
    """The page and the runner must not disagree about the layout."""
    datasets = tmp_path / "datasets"
    with _client_with(settings, datasets) as client:
        request_id = _file(client)
        _run(_approve(settings, request_id))
        result = _run(_land(settings, datasets, request_id, wire))
        body = client.get("/ui/datasets.json").json()

    listed = body["vendors"][0]["datasets"][0]["requests"][0]
    assert result.dest is not None
    assert listed["request_id"] == request_id
    for entry in listed["files"]:
        assert (result.dest / entry["name"]).is_file()
    landing = json.loads((Path(result.dest).parent / "landing.json").read_text())
    assert landing["landed_sha256"] == listed["landed_sha256"]
