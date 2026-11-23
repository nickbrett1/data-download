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


# -- forms and binders: adding a vendor should be data -------------------------


def _copy_registry(tmp_path):
    """The repo's registry in a scratch directory, so a test can add a type."""
    import shutil

    from conftest import PLAN_TYPES_DIR

    dest = tmp_path / "registry"
    shutil.copytree(PLAN_TYPES_DIR, dest)
    return dest


def test_the_shipped_type_gets_its_schema_from_the_shared_form(tmp_path):
    from data_download.registry import load_registry

    registry = load_registry(_copy_registry(tmp_path))
    entry = registry.get("databento_batch_download")
    assert entry.form == "instruments_window"
    assert entry.binder == "databento_batch_download"
    assert entry.params_schema["required"] == [
        "dataset",
        "schema",
        "symbols",
        "stype_in",
        "start",
        "end",
    ]


def test_a_second_vendor_can_reuse_the_form_and_the_binder(tmp_path):
    """The whole point: a new vendor is a YAML file, not a module.

    It reuses the shared form (same question) and names the existing binder
    (same params → Job mapping), so `profiles.resolve` finds it with no new
    Python anywhere.
    """
    from data_download import profiles
    from data_download.registry import load_registry

    dest = _copy_registry(tmp_path)
    (dest / "refinitiv_history.yaml").write_text(
        """
name: refinitiv_history
version: 1
description: Refinitiv's version of the same question.
vendor: Refinitiv
datasets: ["RIC/*"]
form: instruments_window
binder: databento_batch_download
verify:
  required: true
  on_failure: [quarantine]
""",
        encoding="utf-8",
    )

    registry = load_registry(dest)
    assert profiles.unbound_types(registry) == []
    plan = profiles.resolve(registry, "refinitiv_history")
    assert plan is not None
    assert plan.type.params_schema["required"] == [
        "dataset",
        "schema",
        "symbols",
        "stype_in",
        "start",
        "end",
    ]
    # And it binds: the reused binder is the one that produced the Job.
    from conftest import PLAN_BODY

    job = plan.binder(PLAN_BODY["params"], {"on_failure": "quarantine"})
    assert [step["kind"] for step in job["steps"]][:2] == ["resolve", "quote"]


def test_a_type_may_ship_its_own_schema_instead_of_a_form(tmp_path):
    """A genuinely different question is a schema, not a lie about sharing."""
    from data_download.registry import load_registry

    dest = _copy_registry(tmp_path)
    (dest / "catalogue_pick.yaml").write_text(
        """
name: catalogue_pick
version: 1
description: Pick a published file by id -- no symbols, no window.
vendor: Someone
datasets: ["FILE/*"]
binder: catalogue_pick
params_schema:
  type: object
  additionalProperties: false
  required: [file_id]
  properties:
    file_id: {type: string, minLength: 1}
verify:
  required: true
  on_failure: [quarantine]
""",
        encoding="utf-8",
    )
    entry = load_registry(dest).get("catalogue_pick")
    assert entry.form == ""
    assert entry.params_schema["required"] == ["file_id"]


def test_a_type_may_not_ship_a_form_and_a_schema(tmp_path):
    import pytest

    from data_download.registry import load_registry

    dest = _copy_registry(tmp_path)
    (dest / "both.yaml").write_text(
        """
name: both
description: two answers to one question.
form: instruments_window
params_schema: {type: object}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not both"):
        load_registry(dest)


def test_an_unknown_form_fails_at_load(tmp_path):
    import pytest

    from data_download.registry import load_registry

    dest = _copy_registry(tmp_path)
    (dest / "typo.yaml").write_text(
        """
name: typo
description: names a form nobody wrote.
form: instruments_windows
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="is not declared"):
        load_registry(dest)
