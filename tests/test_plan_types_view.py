"""The plan-types browse view: the registry, as data and as a page.

Two surfaces, two homes. `/v1/plan-types` (the machine API) is tested in
`test_registry.py`; the *rendered* page is `web/e2e/app.spec.js`; this file
tests the view-model in between -- `/ui/plan-types.json` -- which is the thing
that has to say what may be filed and what each parameter demands. The
assertions are about fields, enums and prose appearing, not about markup,
because none of it is markup any more: the page is a built file that draws
whatever this JSON holds.

The prose assertions are deliberately the same sentences the old page test
made. Moving a page from Jinja to Svelte must not quietly drop the words that
explained a constraint to a person.
"""

from __future__ import annotations

import json

from conftest import auth


def _view(client) -> dict:
    res = client.get("/ui/plan-types.json?token=tok-read")
    assert res.status_code == 200
    return res.json()


def test_the_view_requires_read_scope(client):
    assert client.get("/ui/plan-types.json").status_code == 401
    assert client.get("/ui/plan-types.json?token=nonsense").status_code == 401
    assert (
        client.get("/ui/plan-types.json", headers=auth("tok-approve")).status_code
        == 403
    )
    # The page itself keeps the same rule it had when it was a template.
    assert client.get("/plan-types").status_code == 401
    assert client.get("/plan-types?token=nonsense").status_code == 401
    assert client.get("/plan-types", headers=auth("tok-approve")).status_code == 403


def test_the_view_is_read_only(client):
    """No verb to change a plan type: the registry is git, not the DB."""
    assert client.post("/ui/plan-types.json", headers=auth()).status_code == 405


def test_a_type_carries_its_schema_field_by_field(client):
    text = json.dumps(_view(client))
    body = _view(client)

    assert "databento_batch_download" in text
    # every required param is visible, flagged as required
    fields = {f["name"]: f for f in body["types"][0]["schema"]["fields"]}
    for name in ("dataset", "schema", "symbols", "stype_in", "start", "end"):
        assert name in fields, f"{name} missing from the schema view"
    assert fields["dataset"]["required"] is True
    assert body["types"][0]["schema"]["required_count"] == 6
    assert body["types"][0]["schema"]["optional_count"] == 3

    # an enum's legal values are spelled out, not just "enum"
    for value in ("raw_symbol", "continuous", "instrument_id", "dbn", "zstd"):
        assert value in text, f"{value} not spelled out"

    # the prose that tells you how to fill it in survives
    assert "GLBX.MDP3" in text
    assert "No open-ended windows" in text

    # verify/resume semantics are on the page, not only in the YAML
    assert "commit_marked" in text and "quarantine" in text
    assert "job_id is the anchor" in text


def test_a_type_is_anchored_and_closed(client):
    body = _view(client)
    first = body["types"][0]

    # a jump target per type, so a dashboard row can link straight to it
    assert first["anchor"] == first["name"]
    # `additionalProperties: false` is stated as what it means, not as jargon
    assert first["schema"]["closed"] is True
    # the raw schema is available (the page collapses it in a <details>)
    assert (
        "properties" in json.loads(first["schema"]["raw"])
        or "closed" in first["schema"]["raw"]
    )
    # the file a reader would open to change it
    assert first["source"].endswith(".yaml")


def test_the_schema_declares_its_own_dialect(client):
    """The spec version is a property of the document, not a caption.

    A reader who copies the schema out of the page gets the dialect with it, so
    no one has to guess which JSON Schema rules apply - and the UI does not have
    to print a bare date that reads like data provenance.
    """
    first = _view(client)["types"][0]
    schema = json.loads(first["schema"]["raw"])
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
