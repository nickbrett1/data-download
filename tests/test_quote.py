"""Phase 4: the quote, the ceiling, and the one place a vendor client lives.

The rules these tests defend, in the order they matter:

1. **A request is priced before it can be approved.** `estimate_usd` is a number
   on every row that has a Job, and the number came from the Job's own `quote`
   step -- not from the params, not from a default.
2. **No price is a refused submit.** With no key, no pricer, a Job without a
   quote step, or a vendor that answers nothing, the body is refused and no row
   is written. An unpriced draft would be a request whose approval authorises
   nothing.
3. **The ceiling refuses, and it is the only check.** A quote above `max_usd` is
   `over_ceiling` at submit (plan body) or `blocked_reason='ceiling_exceeded'`
   (a bound intent, where the row must not vanish). There is no aggregate, no
   claim-time `sum()`, and no re-price: D1, 2026-11-19.
4. **The vendor stays in `vendor.py`.** Nothing here reaches a network; the
   whole suite injects a pricer.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from conftest import PLAN_BODY, PLAN_TYPES_DIR, QUOTE_USD, auth, fake_quote, seed
from data_download import vendor
from data_download.app import create_app
from data_download.config import Settings, load_settings

TOKENS = {
    "tok-submit": frozenset({"submit", "read"}),
    "tok-read": frozenset({"read"}),
}


def make_settings(db_path, quote=fake_quote) -> Settings:
    return Settings(
        db_path=str(db_path),
        tokens=TOKENS,
        plan_types_dir=PLAN_TYPES_DIR,
        approvers=frozenset({"nick@example.com"}),
        code_ref="0" * 40,
        quote=quote,
    )


def make_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), client=("127.0.0.1", 51234))


# -- the pricer, in isolation -------------------------------------------------


def _job(**kwargs) -> str:
    return json.dumps(
        {"plan": "p", "steps": [{"kind": "quote", "kwargs": kwargs}]}, sort_keys=True
    )


def test_the_quote_step_is_what_gets_priced():
    seen = {}

    def pricer(kwargs):
        seen.update(kwargs)
        return 1.25

    estimate = vendor.price(
        _job(dataset="GLBX.MDP3", schema="trades", symbols=["ES.c.0"]), pricer
    )
    assert estimate == 1.25
    assert seen["dataset"] == "GLBX.MDP3"
    assert seen["symbols"] == ["ES.c.0"]


def test_no_pricer_is_a_refusal_and_says_which_setting():
    with pytest.raises(vendor.QuoteUnavailable) as excinfo:
        vendor.price(_job(dataset="GLBX.MDP3"), None)
    assert "DATA_DOWNLOAD_DATABENTO_KEY" in str(excinfo.value)


def test_a_job_with_no_quote_step_cannot_be_priced():
    with pytest.raises(vendor.QuoteUnavailable):
        vendor.price(json.dumps({"plan": "p", "steps": []}), fake_quote)


@pytest.mark.parametrize("answer", [0.0, -1.0, float("inf"), float("nan")])
def test_an_unusable_price_is_refused_rather_than_recorded(answer):
    with pytest.raises(vendor.QuoteUnavailable):
        vendor.price(_job(dataset="GLBX.MDP3"), lambda kwargs: answer)


def test_a_vendor_exception_becomes_a_refusal_not_a_crash():
    def pricer(kwargs):
        raise RuntimeError("metadata.get_cost exploded")

    with pytest.raises(vendor.QuoteUnavailable):
        vendor.price(_job(dataset="GLBX.MDP3"), pricer)


# -- at submit ----------------------------------------------------------------


def test_a_plan_request_is_priced_at_submit(tmp_path):
    with make_client(make_settings(tmp_path / "a.db")) as client:
        response = client.post("/v1/requests", json=PLAN_BODY, headers=auth())
        assert response.status_code == 202
        assert response.json()["quote_usd"] == QUOTE_USD

        detail = client.get(
            f"/v1/requests/{response.json()['request_id']}", headers=auth()
        ).json()
        assert detail["estimate_usd"] == QUOTE_USD


def test_no_pricer_refuses_the_body_and_writes_nothing(tmp_path):
    settings = make_settings(tmp_path / "b.db", quote=None)
    with make_client(settings) as client:
        response = client.post("/v1/requests", json=PLAN_BODY, headers=auth())
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "quote_unavailable"
        # No row: the refusal is of the body, so there is nothing half-created.
        assert client.get("/v1/requests", headers=auth()).json()["items"] == []


def test_a_quote_above_the_ceiling_is_refused_at_submit(tmp_path):
    settings = make_settings(tmp_path / "c.db", quote=lambda kwargs: 99.0)
    body = dict(PLAN_BODY, max_usd=5.0)
    with make_client(settings) as client:
        response = client.post("/v1/requests", json=body, headers=auth())
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "over_ceiling"
        assert error["estimate_usd"] == 99.0
        assert error["max_usd"] == 5.0
        assert client.get("/v1/requests", headers=auth()).json()["items"] == []


def test_a_quote_under_the_ceiling_is_accepted(tmp_path):
    body = dict(PLAN_BODY, max_usd=5.0)
    with make_client(make_settings(tmp_path / "d.db")) as client:
        assert client.post("/v1/requests", json=body, headers=auth()).status_code == 202


def test_a_matched_intent_is_priced_and_over_ceiling_is_blocked_not_refused(tmp_path):
    """The row exists, so it must not vanish (§7.9.2): `ceiling_exceeded`.

    `blocked` requires no Job and no estimate (the CHECK), so the number is
    reported in the response and the row waits for a human to raise `max_usd`.
    """
    settings = make_settings(tmp_path / "e.db", quote=lambda kwargs: 500.0)
    body = {
        "kind": "intent",
        "intent": {
            "dataset": "GLBX.MDP3",
            "schema": "trades",
            "window": {"start": "2026-10-01", "end": "2026-10-03"},
            "symbols": ["ES.c.0"],
            "stype_in": "continuous",
        },
        "max_usd": 10.0,
        "why": "the ES window for the October review",
    }
    with make_client(settings) as client:
        response = client.post("/v1/requests", json=body, headers=auth())
        assert response.status_code == 202
        payload = response.json()
        assert payload["state"] == "blocked"
        assert payload["blocked_reason"] == "ceiling_exceeded"
        assert payload["quote_usd"] == 500.0

        detail = client.get(
            f"/v1/requests/{payload['request_id']}", headers=auth()
        ).json()
        assert detail["state"] == "blocked"
        assert detail["blocked_reason"] == "ceiling_exceeded"
        assert detail["estimate_usd"] is None


# -- POST /v1/quotes ----------------------------------------------------------


def test_the_quote_endpoint_answers_without_changing_anything(tmp_path):
    with make_client(make_settings(tmp_path / "f.db")) as client:
        request_id = client.post(
            "/v1/requests", json=PLAN_BODY, headers=auth()
        ).json()["request_id"]
        before = client.get(f"/v1/requests/{request_id}", headers=auth()).json()

        response = client.post(
            "/v1/quotes", json={"request_id": request_id}, headers=auth("tok-read")
        )
        assert response.status_code == 200
        body = response.json()
        assert body["request_id"] == request_id
        assert body["estimate_usd"] == QUOTE_USD
        assert body["currency"] == "USD"

        after = client.get(f"/v1/requests/{request_id}", headers=auth()).json()
        assert after["state"] == before["state"]
        assert after["updated_at"] == before["updated_at"]


def test_the_quote_endpoint_requires_read_scope(tmp_path):
    with make_client(make_settings(tmp_path / "g.db")) as client:
        response = client.post("/v1/quotes", json={"request_id": "x"})
        assert response.status_code == 401


def test_an_unknown_request_is_not_found(tmp_path):
    with make_client(make_settings(tmp_path / "h.db")) as client:
        response = client.post(
            "/v1/quotes", json={"request_id": "req_nope"}, headers=auth("tok-read")
        )
        assert response.status_code == 404


def test_a_blocked_request_has_no_job_to_price(client, settings):
    # Through the shared app first, so the store exists before a row is seeded
    # into it (the API is what creates the schema).
    seed(
        settings.db_path,
        [
            {
                "id": "blocked_one",
                "state": "blocked",
                "body_kind": "intent",
                "job_json": None,
                "intent_json": json.dumps({"intent": {}, "max_usd": 5, "why": "w"}),
                "estimate_usd": None,
                "blocked_reason": "awaiting_plan",
            }
        ],
    )
    response = client.post(
        "/v1/quotes", json={"request_id": "blocked_one"}, headers=auth("tok-read")
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "quote_unavailable"


# -- config -------------------------------------------------------------------


def test_a_key_builds_a_pricer_and_no_key_builds_none():
    with_key = load_settings({"DATA_DOWNLOAD_DATABENTO_KEY": "db-test"})
    # Pricing resolves by vendor now, so the setting is a resolver rather than a
    # bare callable; a keyed deployment answers for every vendor through the
    # local bridge until a driver for a vendor is installed.
    assert isinstance(with_key.quote, vendor.Pricing)
    assert callable(with_key.quote.for_vendor("Databento"))
    without = load_settings({})
    assert without.quote is None
