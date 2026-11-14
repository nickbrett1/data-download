"""The plan-types browse page: the registry, rendered for a person.

The JSON surface (`/v1/plan-types`) is tested in `test_registry.py`. This file
tests the HTML page, whose only job is to say what may be filed and what each
parameter demands - so the assertions are about fields, enums and prose
appearing, not about exact markup.
"""

from __future__ import annotations

from conftest import auth


def test_plan_types_page_requires_read_scope(client):
    assert client.get("/plan-types").status_code == 401
    assert client.get("/plan-types?token=nonsense").status_code == 401
    assert client.get("/plan-types", headers=auth("tok-approve")).status_code == 403


def test_plan_types_page_shows_the_type_and_its_schema(client):
    res = client.get("/plan-types?token=tok-read")
    assert res.status_code == 200
    html = res.text

    assert "databento_batch_download" in html
    # every required param is visible, with the required badge
    for field in ("dataset", "schema", "symbols", "stype_in", "start", "end"):
        assert field in html
    assert "required" in html
    assert "6 required" in html and "3 optional" in html

    # an enum's legal values are spelled out, not just "enum"
    for value in ("raw_symbol", "continuous", "instrument_id", "dbn", "zstd"):
        assert value in html

    # the prose that tells you how to fill it in survives
    assert "GLBX.MDP3" in html
    assert "No open-ended windows" in html

    # verify/resume semantics are on the page, not only in the YAML
    assert "commit_marked" in html and "quarantine" in html
    assert "job_id is the anchor" in html


def test_plan_types_page_is_browsable_and_closed(client):
    html = client.get("/plan-types?token=tok-read").text
    # a jump link to each type
    assert 'href="#databento_batch_download"' in html
    # additionalProperties: false is stated, because it is a real constraint
    assert "closed params" in html
    # the raw schema is available but collapsed
    assert "<details>" in html
    assert "Draft 2020-12" in html


def test_plan_types_page_is_read_only(client):
    """No affordance to change a plan type: the registry is git, not the DB."""
    html = client.get("/plan-types?token=tok-read").text
    for forbidden in ("<form", "<button", "<input"):
        assert forbidden not in html


def test_dashboard_links_to_the_plan_type_browser(client):
    html = client.get("/?token=tok-read").text
    assert 'href="/plan-types?token=tok-read"' in html


def test_html_pages_are_never_cached(client):
    """A stale page would show a book that is not the book."""
    for path in ("/?token=tok-read", "/plan-types?token=tok-read"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert "no-store" in res.headers.get("cache-control", ""), path
