"""The gate before the worker loop: file a request through the generic YAML and
watch it sit there, approved and unclaimed.

Every other test in this suite starts from a seeded row or calls the store
directly. This one starts where a person does -- a token, a body naming the plan
type and the seller -- and ends where the queue does: `approved`, nobody's
lease, `approved_unclaimed` in the read model, and the words the page uses for
it.

It is the acceptance case for the vendor-neutral type: nothing here mentions
Databento except the params, and nothing in Python had to mention it at all.
"""

from __future__ import annotations

from conftest import PLAN_BODY, auth, served


def test_a_generic_plan_request_is_filed_priced_and_sits_awaiting_pickup(client):
    filed = client.post("/v1/requests", json=PLAN_BODY, headers=auth())
    assert filed.status_code == 202, filed.text
    body = filed.json()
    request_id = body["request_id"]
    # Priced before it could be approved: the number comes from the pricer the
    # app was composed with, and the ceiling it was checked against is the body's.
    assert body["state"] == "draft"
    assert body["quote_usd"] > 0

    draft = client.get(f"/ui/request/{request_id}.json").json()
    # The type answered, the binder sealed a Job, and the row names its seller.
    assert draft["payload"]["plan_type"] == "market_data_window"
    assert draft["vendor"] == "Databento"
    job = draft["payload"]["job"]
    assert job["plan"] == "market_data_window"
    steps = [step["kind"] for step in job["steps"]]
    assert steps == ["resolve", "quote", "submit", "poll", "download", "convert", "verify"]

    approved = client.post(
        f"/v1/requests/{request_id}/approve", json={}, headers=served()
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"
    assert approved.json()["approved"] is True

    detail = client.get(f"/ui/request/{request_id}.json").json()
    assert detail["state"] == "approved"
    # Nobody has claimed it: this is the sentence the page prints, and the only
    # thing standing between the row and the worker loop.
    assert detail["approved_unclaimed"] == 1
    assert detail["facet"] == "todo"

    # The book agrees, and does not call it stuck -- money authorised and waiting
    # for a worker is a queue, not a fault.
    dashboard = client.get("/ui/dashboard.json").json()
    row = next(r for r in dashboard["rows"] if r["id"] == request_id)
    assert row["facet"] == "todo"
    assert row["approved_unclaimed"] == 1
