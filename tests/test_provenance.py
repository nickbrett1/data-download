"""Who filed it, and what approving it would do.

Two claims about a row that the state and the price cannot make:

* **Provenance.** Two drafts can look identical on the book and not be the same
  thing -- one came out of a validation pass, one was typed from memory. The
  token decides *whether* a caller may file; `filed_by` says *what* filed, and
  it is a declaration, so the tests are about it being recorded and sanitised
  rather than about it being trusted.

* **The prospect.** "If you approve" is answered from the sealed Job and the
  numbers already stored at submit, never from a description of what a plan
  type usually does. The test that matters is the one that would fail if the
  page started inventing: a row with no Job must say it would run nothing, and
  a draft's steps must be the Job's own kinds.
"""

from __future__ import annotations

import json

from conftest import PLAN_BODY, auth, iso, seed

FILED_BY = "X-Data-Download-Filed-By"

STEP_KINDS = ["resolve", "quote", "submit", "poll", "download", "convert", "verify"]


def _file(client, **extra):
    response = client.post(
        "/v1/requests", json=dict(PLAN_BODY), headers={**auth(), **extra}
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_a_header_says_who_filed_it(client):
    filed = _file(client, **{FILED_BY: "data-sourcing-agent"})
    detail = client.get(f"/ui/request/{filed['request_id']}.json").json()
    assert detail["filed_by"] == "data-sourcing-agent"
    # And it is in the list an agent reads, not only on the page: a caller
    # deciding whether to trust a row needs it as much as a reader does.
    listed = client.get("/v1/requests", headers=auth()).json()
    assert listed["items"][0]["filed_by"] == "data-sourcing-agent"


def test_filing_with_nothing_to_say_is_recorded_as_the_api(client):
    filed = _file(client)
    detail = client.get(f"/ui/request/{filed['request_id']}.json").json()
    assert detail["filed_by"] == "api"


def test_the_declaration_is_bounded_and_printable(client):
    """It is stored text that a page renders, so it is trimmed to a name.

    Not a security boundary -- the token is that -- but "whatever the caller
    sent" would put control characters and unbounded strings into a page.
    """
    noisy = "  agent\n<script>alert(1)</script>" + "x" * 200
    filed = _file(client, **{FILED_BY: noisy})
    got = client.get(f"/ui/request/{filed['request_id']}.json").json()["filed_by"]
    assert len(got) <= 64
    assert "<" not in got and ">" not in got
    assert "\n" not in got
    assert got.startswith("agentscriptalert1")


def test_the_prospect_is_the_sealed_jobs_own_steps(client):
    filed = _file(client)
    detail = client.get(f"/ui/request/{filed['request_id']}.json").json()
    prospect = detail["prospect"]
    assert prospect["ready"] is True
    # The Job is the published artefact: the page cannot promise a pipeline the
    # runner will not execute, because it is reading the runner's own script.
    assert [step["kind"] for step in prospect["steps"]] == STEP_KINDS
    assert all(step["verb"] for step in prospect["steps"])
    # The dataset comes from the Job's resolve step, not from a guess.
    assert detail["dataset"] == "GLBX.MDP3"
    # `landing` is `None` here on purpose: this suite's settings name no
    # datasets root, and the page must not promise a path that nobody chose.
    assert prospect["landing"] is None


def test_the_landing_path_is_the_one_the_runner_writes(db_path, tmp_path):
    """With a root configured, the promise is the runner's own path."""
    from fastapi.testclient import TestClient

    from conftest import APPROVERS, CODE_REF, PLAN_TYPES_DIR, TOKENS, fake_quote
    from data_download.app import create_app
    from data_download.config import Settings

    app = create_app(
        Settings(
            db_path=str(db_path),
            tokens=TOKENS,
            plan_types_dir=PLAN_TYPES_DIR,
            approvers=APPROVERS,
            code_ref=CODE_REF,
            quote=fake_quote,
            datasets_dir=tmp_path / "datasets",
            runner=False,
        )
    )
    with TestClient(app, client=("127.0.0.1", 51234)) as client:
        filed = _file(client)
        prospect = client.get(
            f"/ui/request/{filed['request_id']}.json"
        ).json()["prospect"]
    assert prospect["landing"]["dataset"] == "GLBX.MDP3"
    assert prospect["landing"]["path"].endswith(
        f"/GLBX.MDP3/{filed['request_id']}/"
    )


def test_the_checks_are_recorded_facts_not_forecasts(client):
    filed = _file(client)
    checks = {
        check["label"]: check
        for check in client.get(f"/ui/request/{filed['request_id']}.json").json()[
            "prospect"
        ]["checks"]
    }
    # Priced and under its ceiling at submit, so the check restates a stored
    # comparison rather than re-pricing anything.
    assert checks["ceiling"]["verdict"] == "ok"
    assert checks["verify"]["verdict"] == "ok"


def test_a_row_with_no_job_says_it_would_run_nothing(client, db_path):
    """A blocked intent has no Job. The honest answer is not a pipeline."""
    seed(
        db_path,
        [
            {
                "id": "seed_blocked",
                "state": "blocked",
                "body_kind": "intent",
                "intent_json": json.dumps({"kind": "intent", "intent": {}}),
                "job_json": None,
                "estimate_usd": None,
                "blocked_reason": "awaiting_plan",
                "created_at": iso(),
            }
        ],
    )
    prospect = client.get("/ui/request/seed_blocked.json").json()["prospect"]
    assert prospect["ready"] is False
    assert "awaiting_plan" in prospect["why"]
    assert prospect["steps"] == []


def test_a_page_filed_retry_says_the_page_filed_it(client, db_path):
    """The retry button files a new row, so the new row records the click.

    It is the one retry that has no token behind it -- the approver's identity
    is the credential -- so "api" would be the wrong answer.
    """
    seed(
        db_path,
        [
            {
                "id": "seed_failed",
                "state": "failed",
                "job_json": json.dumps(
                    {
                        "kind": "plan",
                        "steps": [
                            {"kind": "verify", "kwargs": {"on_failure": "quarantine"}}
                        ],
                    },
                    sort_keys=True,
                ),
                "errors_json": json.dumps({"code": "vendor_error", "detail": "nope"}),
                "max_usd": 5,
                "params_json": json.dumps(PLAN_BODY["params"], sort_keys=True),
                "created_at": iso(),
            }
        ],
    )
    response = client.post(
        "/v1/requests/seed_failed/retry",
        headers={"Tailscale-User-Login": "nick@example.com", FILED_BY: "page"},
    )
    assert response.status_code == 202, response.text
    new_id = response.json()["request_id"]
    assert client.get(f"/ui/request/{new_id}.json").json()["filed_by"] == "page"
