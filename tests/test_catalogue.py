"""The dataset catalogue: what a vendor calls a code, remembered (§`describe`).

Three things are worth testing apart from each other: the translation of the
driver's answer into what a page reads, the cache (a second read must not ask
again, a failed ask must not be stored as "undescribed"), and the three display
points carrying the description -- the book row, the request page and the
Datasets page.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from conftest import PLAN_BODY, auth, seed
from data_download import catalogue
from data_download.app import create_app
from data_download.db import Store


def _run(coro):
    return asyncio.run(coro)


GLBX = {
    "dataset": "GLBX.MDP3",
    "described": True,
    "description": "CME Globex MDP 3.0",
    "venue": "GLBX",
    "venues": ["GLBX"],
    # 2010-06-06T00:00:00Z and 2026-11-23T00:00:00Z, in nanoseconds: the shape
    # `get_dataset_range` really answers.
    "range": {"start": 1275782400000000000, "end": 1795392000000000000},
    "schemas": ["ohlcv-1d", "trades"],
    "fetched_at": "2026-11-23T03:00:00Z",
}


def test_a_driver_answer_becomes_days_and_a_name():
    answer = catalogue.entry("Databento", GLBX, fetched_at="2026-11-23T03:00:00Z")
    assert answer["described"] is True
    assert answer["label"] == "CME Globex MDP 3.0"
    assert answer["available_from"] == "2010-06-06"
    # A live dataset ends at the present, and saying yesterday's date would read
    # as "this dataset stopped".
    assert answer["available_to"] == "now"
    assert answer["venues"] == ["GLBX"]


def test_an_undescribed_code_is_an_answer_not_an_error():
    answer = catalogue.entry(
        "Databento",
        {
            "dataset": "NOPE.UNKNOWN",
            "described": False,
            "description": "",
            "venues": [],
            "range": {"start": None, "end": None, "unavailable": "no such dataset"},
        },
    )
    assert answer["described"] is False
    assert answer["label"] == catalogue.NOT_DESCRIBED
    assert answer["available_from"] is None
    assert answer["label"] != ""


def test_a_row_round_trips_without_inventing_a_description(settings):
    async def scenario():
        store = Store(str(settings.db_path))
        await store.connect()
        try:
            answer = catalogue.entry("Databento", GLBX)
            await store.catalogue_put(catalogue.row_of(answer))
            rows = await store.catalogue_get([("Databento", "GLBX.MDP3")])
            assert len(rows) == 1
            back = catalogue.from_row(rows[0])
            assert back["described"] is True
            assert back["description"] == "CME Globex MDP 3.0"
            assert back["available_from"] == "2010-06-06"
            # A code the vendor refused stays refused, and the flag -- not the
            # empty string -- is what says so.
            await store.catalogue_put(
                catalogue.row_of(
                    catalogue.entry(
                        "Databento", {"dataset": "NOPE.UNKNOWN", "described": False}
                    )
                )
            )
            rows = await store.catalogue_get([("Databento", "NOPE.UNKNOWN")])
            assert catalogue.from_row(rows[0])["described"] is False
            assert catalogue.from_row(rows[0])["label"] == catalogue.NOT_DESCRIBED
        finally:
            await store.close()

    _run(scenario())


def test_the_second_answer_comes_from_the_cache(settings):
    asked: list[str] = []

    def describe(vendor, dataset, root):
        asked.append(dataset)
        return GLBX

    async def scenario():
        store = Store(str(settings.db_path))
        await store.connect()
        try:
            first = await catalogue.refresh(
                store, "Databento", "GLBX.MDP3", describe=describe
            )
            assert first["label"] == "CME Globex MDP 3.0"
            found = await catalogue.lookup(store, [("Databento", "GLBX.MDP3")])
            assert found[("Databento", "GLBX.MDP3")]["description"] == (
                "CME Globex MDP 3.0"
            )
            # The cache is the point: a page that polls must not poll a vendor.
            assert asked == ["GLBX.MDP3"]
            assert catalogue.is_stale(found[("Databento", "GLBX.MDP3")]) is False
        finally:
            await store.close()

    _run(scenario())


def test_a_failed_ask_is_not_stored_and_is_not_retried_at_once(settings):
    catalogue._FAILED_AT.clear()

    def broken(vendor, dataset, root):
        raise RuntimeError("the runtime is down")

    async def scenario():
        store = Store(str(settings.db_path))
        await store.connect()
        try:
            assert (
                await catalogue.refresh(
                    store, "Databento", "GLBX.MDP3", describe=broken
                )
                is None
            )
            assert await store.catalogue_get([("Databento", "GLBX.MDP3")]) == []
            # The page keeps drawing; the failure is not re-asked on every load.
            catalogue.schedule(store, [("Databento", "GLBX.MDP3")], describe=broken)
            assert catalogue._TASKS == set()
        finally:
            await store.close()
            catalogue._FAILED_AT.clear()

    _run(scenario())


def test_refresh_asks_the_driver_installed_for_the_vendor(settings, tmp_path, monkeypatch):
    """The real path, not the injected one: discovery, a session of its own, the
    wire. `describe` writes nothing, so its session is rooted in a directory
    named for what it is rather than in a request's."""
    from fake_runtime import FakeRuntime

    datasets_dir = tmp_path / "datasets"
    with FakeRuntime(datasets_dir, vendor="Databento", driver="databento") as runtime:
        monkeypatch.setenv("DATA_DOWNLOAD_DRIVER_RUNTIME_URL", runtime.url)
        monkeypatch.setenv("DRIVER_RUNTIME_TOKEN", runtime.token)

        async def scenario():
            store = Store(str(settings.db_path))
            await store.connect()
            try:
                return await catalogue.refresh(
                    store, "Databento", "GLBX.MDP3", root=datasets_dir
                )
            finally:
                await store.close()

        answer = _run(scenario())

    assert answer["label"] == "Fake dataset GLBX.MDP3"
    assert answer["available_from"] == "2010-06-06"
    # Nothing was landed: describing is a question, not a download.
    assert not (datasets_dir / "databento").exists()


def _job(dataset: str) -> str:
    return json.dumps(
        {
            "kind": "plan",
            "steps": [
                {
                    "kind": "resolve",
                    "kwargs": {"dataset": dataset, "symbols": ["ES.c.0"]},
                }
            ],
        }
    )


def _seed_glbx(db_path, state="approved", request_id="seed_glbx"):
    seed(
        db_path,
        [
            {
                "id": request_id,
                "payload_hash": f"hash_{request_id}",
                "state": state,
                "job_json": _job("GLBX.MDP3"),
            }
        ],
    )
    return request_id


def test_the_book_row_names_the_dataset(client, settings):
    request_id = _seed_glbx(settings.db_path)
    _run(_put_catalogue(settings.db_path, catalogue.entry("Databento", GLBX)))

    book = client.get("/ui/dashboard.json").json()
    row = next(r for r in book["rows"] if r["id"] == request_id)
    assert row["dataset"] == "GLBX.MDP3"
    assert row["catalogue"]["label"] == "CME Globex MDP 3.0"

    detail = client.get(f"/ui/request/{request_id}.json").json()
    assert detail["dataset"] == "GLBX.MDP3"
    assert detail["catalogue"]["description"] == "CME Globex MDP 3.0"
    assert detail["catalogue"]["available_from"] == "2010-06-06"


def test_the_datasets_page_heads_each_group_with_the_name(client, settings, tmp_path):
    _seed_glbx(settings.db_path, state="running")
    _run(_put_catalogue(settings.db_path, catalogue.entry("Databento", GLBX)))

    # The page needs a root to look at; the group is built from the row either
    # way, and the description is the part under test.
    app = create_app(dataclasses.replace(settings, datasets_dir=tmp_path))
    with TestClient(app, client=("127.0.0.1", 51234)) as page:
        body = page.get("/ui/datasets.json").json()
    group = body["vendors"][0]["datasets"][0]
    assert group["dataset"] == "GLBX.MDP3"
    assert group["catalogue"]["label"] == "CME Globex MDP 3.0"
    assert group["catalogue"]["available_to"] == "now"


def test_a_dataset_never_asked_about_is_null_not_invented(client, settings):
    request_id = _seed_glbx(settings.db_path)

    detail = client.get(f"/ui/request/{request_id}.json").json()
    assert detail["dataset"] == "GLBX.MDP3"
    # `None` is "not asked yet"; the page says so rather than inventing a name
    # or claiming the vendor has none.
    assert detail["catalogue"] is None


async def _put_catalogue(db_path, answer):
    store = Store(str(db_path))
    await store.connect()
    try:
        await store.catalogue_put(catalogue.row_of(answer))
    finally:
        await store.close()


@pytest.fixture(autouse=True)
def _clean_failures():
    catalogue._FAILED_AT.clear()
    yield
    catalogue._FAILED_AT.clear()



# -- slice 3: the free pre-flight ---------------------------------------------
#
# `refusal` is pure, so the asymmetry is testable without a store: a start
# before the first day refuses, an end past the last measured instant does not,
# and nothing refuses on an answer we do not currently hold.


# The answer `_fresh` builds from, kept apart from `GLBX` only in `fetched_at`:
# these tests need a row that is current, not one dated to a session.
GLBX_ANSWER = {
    "dataset": "GLBX.MDP3",
    "described": True,
    "description": "CME Globex MDP 3.0",
    "venue": "GLBX",
    "venues": ["GLBX"],
    "range": {"start": 1275782400000000000, "end": 1795392000000000000},
    "schemas": ["ohlcv-1d", "trades"],
}


def _fresh(**changes):
    """A described entry fetched now, so `is_stale` cannot be the reason."""
    answer = dict(GLBX_ANSWER, **changes)
    return catalogue.entry("Databento", answer)


def test_a_schema_the_dataset_does_not_carry_is_refused():
    refused = catalogue.refusal(_fresh(), dataset="GLBX.MDP3", schema="mbo")
    assert refused["reason"] == "schema"
    assert refused["schema"] == "mbo"
    # The sentence names what it *does* carry: a refusal should say what to ask
    # instead, not just that the question was wrong.
    assert "ohlcv-1d" in refused["message"]


def test_a_window_before_the_datasets_first_day_is_refused():
    refused = catalogue.refusal(
        _fresh(), dataset="GLBX.MDP3", schema="trades", start="2009-01-01"
    )
    assert refused["reason"] == "window"
    assert refused["available_from"] == "2010-06-06"
    assert "2009-01-01" in refused["message"]


def test_a_window_ending_after_the_range_is_allowed():
    # The range end trails the clock by hours on a live dataset, so "ends
    # today" is a real request against a range that has not caught up. The
    # end is a clock; the start is a fact.
    assert (
        catalogue.refusal(
            _fresh(), dataset="GLBX.MDP3", schema="trades", start="2026-11-01"
        )
        is None
    )
    assert (
        catalogue.refusal(
            _fresh(), dataset="GLBX.MDP3", schema="trades", start="2010-06-06"
        )
        is None
    )


def test_a_cold_stale_or_undescribed_cache_never_refuses():
    # A cache that refuses is worse than a vendor that refuses: in each of
    # these cases the vendor still gets to answer.
    assert catalogue.refusal(None, dataset="GLBX.MDP3", schema="mbo") is None
    stale = _fresh()
    stale["fetched_at"] = "2026-01-01T00:00:00Z"
    assert catalogue.refusal(stale, dataset="GLBX.MDP3", schema="mbo") is None
    undescribed = _fresh(described=False, description="", schemas=[])
    assert catalogue.refusal(undescribed, dataset="GLBX.MDP3", schema="mbo") is None


def test_submit_refuses_a_schema_the_catalogue_does_not_carry(client, settings):
    _run(_put_catalogue(settings.db_path, catalogue.entry("Databento", GLBX_ANSWER)))
    body = dict(PLAN_BODY, params=dict(PLAN_BODY["params"], schema="mbo"))

    refused = client.post("/v1/requests", json=body, headers=auth())

    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "not_in_catalogue"
    assert error["reason"] == "schema"
    # Refused before it was priced, so nothing was filed.
    assert client.get("/v1/requests", headers=auth("tok-read")).json()["total"] == 0


def test_submit_refuses_a_window_before_the_data_starts(client, settings):
    _run(_put_catalogue(settings.db_path, catalogue.entry("Databento", GLBX_ANSWER)))
    body = dict(PLAN_BODY, params=dict(PLAN_BODY["params"], start="2009-01-01"))

    refused = client.post("/v1/requests", json=body, headers=auth())

    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "not_in_catalogue"
    assert error["reason"] == "window"
    assert error["available_from"] == "2010-06-06"


def test_submit_proceeds_when_the_catalogue_has_not_been_asked(client):
    # No cached row at all: the pre-flight must stay silent and let the vendor
    # answer, or a brand-new deployment could file nothing.
    assert client.post("/v1/requests", json=PLAN_BODY, headers=auth()).status_code == 202
