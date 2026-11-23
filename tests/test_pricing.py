"""Pricing by vendor: the driver answers, the local SDK is only the bridge.

The rules these tests defend:

1. **The driver whose vendor it is answers the quote.** `cost` is one of the
   eight verbs, so a vendor with an installed artefact is priceable with no SDK
   and no vendor key in this repo -- and no code change here.
2. **An installed driver wins over the local bridge.** Installing the artefact
   is how the bridge stops being used; a stale SDK must never price over a
   driver that is present.
3. **No pricer means no price, and the refusal names the vendor.** An unpriced
   request is one nobody can approve honestly, so it is refused, not stored.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from conftest import (
    PLAN_BODY,
    PLAN_TYPES_DIR,
    QUOTE_USD,
    TOKENS,
    auth,
    fake_quote,
)
from data_download import vendor
from data_download.app import create_app
from data_download.config import Settings
from fake_runtime import FAKE_QUOTE_USD, FakeRuntime


def _job(**kwargs) -> str:
    """A bound Job with the one thing pricing reads: a `quote` step."""
    return json.dumps(
        {"plan": "p", "steps": [{"kind": "quote", "kwargs": kwargs}]}, sort_keys=True
    )


def _pricing(
    runtime: FakeRuntime, datasets_dir, fallback=None
) -> vendor.DriverPricing:
    return vendor.DriverPricing(
        datasets_dir=datasets_dir,
        url=runtime.url,
        token=runtime.token,
        fallback=fallback,
    )


class _EmptyPricing(vendor.Pricing):
    """A resolver with nothing for anybody: what an empty runtime looks like."""

    def __init__(self) -> None:
        super().__init__(lambda _vendor: None)


# -- the driver answers -------------------------------------------------------


def test_the_driver_for_the_vendor_answers_the_quote(tmp_path):
    with FakeRuntime(tmp_path, vendor="Databento") as runtime:
        pricer = _pricing(runtime, tmp_path).for_vendor("Databento")
        assert pricer is not None
        assert pricer({"dataset": "GLBX.MDP3", "schema": "trades"}) == pytest.approx(
            FAKE_QUOTE_USD
        )


def test_binding_is_by_vendor_not_by_plan_type(tmp_path):
    """A vendor with no artefact cannot be priced by another vendor's driver."""
    with FakeRuntime(tmp_path, vendor="Databento") as runtime:
        assert _pricing(runtime, tmp_path).for_vendor("Refinitiv") is None


def test_an_installed_driver_beats_the_local_bridge(tmp_path):
    """The bridge is what an un-installed vendor falls back to, not a preference."""
    with FakeRuntime(tmp_path, vendor="Databento") as runtime:
        pricing = _pricing(runtime, tmp_path, fallback=vendor.Pricing.one(fake_quote))
        pricer = pricing.for_vendor("Databento")
        assert pricer is not None
        assert pricer({}) == pytest.approx(FAKE_QUOTE_USD)
        assert FAKE_QUOTE_USD != QUOTE_USD


def test_a_vendor_with_no_artefact_falls_back_to_the_bridge(tmp_path):
    with FakeRuntime(tmp_path, vendor="Databento") as runtime:
        pricing = _pricing(runtime, tmp_path, fallback=vendor.Pricing.one(fake_quote))
        pricer = pricing.for_vendor("Refinitiv")
        assert pricer is not None
        assert pricer({}) == pytest.approx(QUOTE_USD)


def test_an_unreachable_runtime_falls_back_rather_than_refusing(tmp_path):
    """A driver container that is down must not make an existing vendor unfilable."""
    with FakeRuntime(tmp_path, vendor="Databento") as runtime:
        url, token = runtime.url, runtime.token
    # The runtime is stopped now, so the call fails at the transport rather than
    # at the absence of a driver -- and the bridge answers either way.
    pricing = vendor.DriverPricing(
        datasets_dir=tmp_path,
        url=url,
        token=token,
        fallback=vendor.Pricing.one(fake_quote),
    )
    pricer = pricing.for_vendor("Databento")
    assert pricer is not None
    assert pricer({}) == pytest.approx(QUOTE_USD)


# -- the refusal ---------------------------------------------------------------


def test_no_pricer_at_all_refuses_the_submit():
    with pytest.raises(vendor.QuoteUnavailable):
        vendor.price(_job(), None, "Refinitiv")


def test_a_vendor_nothing_can_price_is_named_in_the_refusal():
    with pytest.raises(vendor.QuoteUnavailable) as excinfo:
        vendor.price(_job(), _EmptyPricing(), "Refinitiv")
    assert "Refinitiv" in str(excinfo.value)


def test_a_job_with_no_quote_step_is_refused_before_any_pricer_is_called():
    called: list[dict] = []
    with pytest.raises(vendor.QuoteUnavailable):
        vendor.price(
            json.dumps({"plan": "p", "steps": [{"kind": "resolve"}]}),
            vendor.Pricing.one(lambda kwargs: called.append(kwargs) or 1.0),
            "Databento",
        )
    assert called == []


# -- through the route ---------------------------------------------------------


def test_a_plan_submit_is_priced_through_the_driver(tmp_path):
    """The submit route passes the plan type's vendor, so the driver answers."""
    with FakeRuntime(tmp_path, vendor="Databento") as runtime:
        settings = Settings(
            db_path=str(tmp_path / "priced.db"),
            tokens=TOKENS,
            plan_types_dir=PLAN_TYPES_DIR,
            approvers=frozenset({"nick@example.com"}),
            code_ref="0" * 40,
            quote=_pricing(runtime, tmp_path),
        )
        with TestClient(create_app(settings), client=("127.0.0.1", 51234)) as client:
            # The fixture's ceiling is $5, and the driver quotes $12.34 -- so the
            # refusal itself proves which number was priced. Raise it for the
            # accept case.
            refused = client.post("/v1/requests", json=PLAN_BODY, headers=auth())
            assert refused.status_code == 422
            assert refused.json()["error"]["code"] == "over_ceiling"
            assert refused.json()["error"]["estimate_usd"] == pytest.approx(
                FAKE_QUOTE_USD
            )
            body = dict(PLAN_BODY, max_usd=50.0)
            response = client.post("/v1/requests", json=body, headers=auth())
            assert response.status_code == 202
            assert response.json()["quote_usd"] == pytest.approx(FAKE_QUOTE_USD)
