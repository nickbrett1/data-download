"""A row filed under a plan type git has since retired still renders."""

from __future__ import annotations

import json

from conftest import iso, seed, served


def test_a_retired_plan_type_does_not_break_the_pages(client, db_path):
    # The client first: the store is created by the app, and a row cannot be
    # seeded into a database that does not exist yet.
    assert client.get("/healthz").status_code == 200
    seed(
        db_path,
        [
            {
                "id": "old_type",
                "state": "approved",
                "plan_type": "databento_batch_download",
                "params_json": json.dumps(
                    {"dataset": "GLBX.MDP3", "schema": "trades"}, sort_keys=True
                ),
                "estimate_usd": 1.5,
                "created_at": iso(),
            }
        ],
    )

    detail = client.get("/ui/request/old_type.json")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["approved_unclaimed"] == 1
    # No registered type claims it, so the seller is simply not known -- honest
    # for a type that no longer exists in git, and not a 500 on a page somebody
    # may still click.
    assert body["vendor"] in ("", "Databento")
    assert client.get("/ui/dashboard.json").status_code == 200
    assert client.get("/ui/datasets.json").status_code == 200


def test_an_approval_that_cannot_be_run_can_be_withdrawn(client, db_path):
    """`approved` is normally not cancellable. This is the one case that is.

    The store refuses `cancel` on an approved row because an approval authorises
    one attempt and only the runner may stop work it holds. A row whose plan
    type is gone has no attempt to authorise: no binder, so no Job a runner
    could claim, so nothing this approval could ever spend. Left alone it would
    sit in the queue for good, and the queue would be describing money that is
    going nowhere as work waiting to happen.
    """
    assert client.get("/healthz").status_code == 200
    seed(
        db_path,
        [
            {
                "id": "stranded",
                "state": "approved",
                "plan_type": "databento_batch_download",
                "params_json": json.dumps({"dataset": "GLBX.MDP3"}, sort_keys=True),
                "created_at": iso(),
            }
        ],
    )

    response = client.post(
        "/v1/requests/stranded/cancel",
        json={"note": "its plan type was retired"},
        headers=served(),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "request_id": "stranded",
        "state": "cancelled",
        "cancelled": True,
    }

    detail = client.get("/ui/request/stranded.json").json()
    assert detail["state"] == "cancelled"
    assert detail["approved_unclaimed"] == 0
    cancelled = [e for e in detail["events"] if e["event"] == "cancelled"]
    assert len(cancelled) == 1
    # The trail says an authorisation was withdrawn, not that live work was
    # stopped -- those are different events and only one of them happened.
    assert cancelled[0]["detail"]["was"] == "approved"
    assert cancelled[0]["detail"]["unbindable"] is True


def test_an_approved_row_whose_type_still_exists_is_still_refused(client, db_path):
    """The widening is exactly as wide as "no binder can ever run this"."""
    assert client.get("/healthz").status_code == 200
    seed(
        db_path,
        [
            {
                "id": "live",
                "state": "approved",
                "plan_type": "market_data_window",
                "params_json": json.dumps({"dataset": "GLBX.MDP3"}, sort_keys=True),
                "created_at": iso(),
            }
        ],
    )

    response = client.post("/v1/requests/live/cancel", json={}, headers=served())
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "cancel_refused"
    assert client.get("/ui/request/live.json").json()["state"] == "approved"
