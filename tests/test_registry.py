"""The registry is discoverable, git-defined, and fails honestly."""

from __future__ import annotations

from conftest import auth


def test_registry_ships_exactly_one_type(client):
    listing = client.get("/v1/plan-types", headers=auth()).json()
    assert [item["name"] for item in listing["items"]] == ["databento_batch_download"]
    entry = listing["items"][0]
    assert entry["verify_required"] is True
    assert entry["description"]
    assert "params_schema" not in entry


def test_plan_type_detail_publishes_the_schema(client):
    detail = client.get("/v1/plan-types/databento_batch_download", headers=auth()).json()
    assert detail["name"] == "databento_batch_download"
    assert detail["version"] == 1
    assert detail["params_schema"]["required"] == [
        "dataset",
        "schema",
        "symbols",
        "stype_in",
        "start",
        "end",
    ]
    assert detail["verify"]["required"] is True
    assert set(detail["verify"]["on_failure"]) == {"commit_marked", "quarantine"}
    assert detail["resume"]


def test_unknown_plan_type_detail_is_404(client):
    response = client.get("/v1/plan-types/nope", headers=auth())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "plan_type_unknown"


def test_plan_types_require_read_scope(client):
    assert client.get("/v1/plan-types").status_code == 401
