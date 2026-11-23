"""A row filed under a plan type git has since retired still renders."""

from __future__ import annotations

import json

from conftest import iso, seed


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
