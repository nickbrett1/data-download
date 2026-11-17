"""Phase 2.6: re-running the match when the registry moves.

The scenario these tests exist for is the one that made the phase necessary: a
Request is filed while nothing registered can take it, a plan that would take it
lands later, and the Request should stop being stuck without anybody remembering
to press anything.

Plans arrive as code, so the fixture for "a plan landed" is a registry directory
built from the repo's own `plan_types/` with one field changed -- the real YAML
loader, the real schema, and the real binder, which is the part a hand-rolled
fake would not exercise.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import yaml

from conftest import PLAN_TYPES_DIR, auth
from data_download import match, rematch
from data_download.db import Store
from data_download.registry import load_registry

# A dataset no registered type claims, with the six params the Databento plan
# wants already supplied. The only thing standing between this Request and a
# bound Job is a registry that speaks for the dataset.
UNCLAIMED = {
    "kind": "intent",
    "intent": {
        "dataset": "ZZZZ.UNKNOWN",
        "schema": "trades",
        "window": {"start": "2026-09-01", "end": "2026-09-02"},
        "symbols": ["AAPL"],
        "stype_in": "raw_symbol",
    },
    "max_usd": 5,
    "why": "a dataset no plan claims yet, with everything else supplied",
}


def registry_with(tmp_path: Path, **changes):
    """The repo's registry, with fields of one plan type changed."""
    dest = tmp_path / "registry"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(PLAN_TYPES_DIR, dest)
    path = dest / "databento_batch_download.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        doc[key] = value(doc[key]) if callable(value) else value
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return load_registry(dest)


def run_rematch(db_path: Path, registry, **kwargs) -> dict:
    async def go() -> dict:
        store = Store(str(db_path))
        await store.connect()
        try:
            return await rematch.reconcile(store, registry, **kwargs)
        finally:
            await store.close()

    return asyncio.run(go())


def fetch(db_path: Path, request_id: str) -> dict:
    async def go() -> dict:
        store = Store(str(db_path))
        await store.connect()
        try:
            return await store.get_request(request_id)
        finally:
            await store.close()

    return asyncio.run(go())


def events(db_path: Path, request_id: str) -> list[dict]:
    async def go() -> list[dict]:
        store = Store(str(db_path))
        await store.connect()
        try:
            return await store.events_for(request_id)
        finally:
            await store.close()

    return asyncio.run(go())


def file_it(client) -> str:
    response = client.post("/v1/requests", json=UNCLAIMED, headers=auth())
    assert response.status_code == 202, response.text
    return response.json()["request_id"]


def test_a_plan_that_lands_later_binds_a_request_that_was_stuck(client, db_path):
    """The whole point: no button, no second submit, no lost request."""
    request_id = file_it(client)
    before = fetch(db_path, request_id)
    assert before["state"] == "blocked"
    assert before["match_verdict"] == match.NOT_CLAIMED

    summary = run_rematch(
        db_path,
        registry_with(tmp_path=db_path.parent, datasets=lambda d: [*d, "ZZZZ.*"]),
        code_ref="c0ffee",
    )

    assert summary["examined"] == 1
    assert summary["moved"] == 1
    assert summary["bound"] == 1

    after = fetch(db_path, request_id)
    assert after["state"] == "draft"
    assert after["blocked_reason"] is None
    assert after["plan_type"] == "databento_batch_download"
    # Provenance, not a paraphrase: this code bound this Job.
    assert after["plan_origin"] == "rematched"
    assert after["code_ref"] == "c0ffee"
    assert after["job_hash"] and after["job_json"]
    assert after["match_verdict"] == match.MATCHED

    # One event, saying what moved and on the strength of which registry.
    moved = [e for e in events(db_path, request_id) if e["event"] == "rematched"]
    assert len(moved) == 1
    detail = json.loads(moved[0]["detail_json"])
    assert moved[0]["actor_kind"] == "system"
    assert (detail["from_verdict"], detail["to_verdict"]) == (
        match.NOT_CLAIMED,
        match.MATCHED,
    )
    assert detail["bound"] is True
    assert detail["registry"] == match.registry_digest(
        registry_with(tmp_path=db_path.parent, datasets=lambda d: [*d, "ZZZZ.*"])
    )


def test_the_bound_row_is_then_ordinary_work_to_approve(client, db_path):
    """A re-matched row is a draft with a Job, not a special case anywhere."""
    request_id = file_it(client)
    registry = registry_with(tmp_path=db_path.parent, datasets=lambda d: [*d, "ZZZZ.*"])
    run_rematch(db_path, registry)

    book = client.get("/ui/dashboard.json", headers=auth("tok-read")).json()
    row = next(r for r in book["rows"] if r["id"] == request_id)
    assert row["facet"] == "todo"
    assert row["facet_label"] == "Approval"


def test_a_second_pass_writes_nothing_at_all(client, db_path):
    """Idempotent, because every start runs it: equal digests mean no work."""
    request_id = file_it(client)
    registry = registry_with(tmp_path=db_path.parent, datasets=lambda d: [*d, "ZZZZ.*"])
    run_rematch(db_path, registry)
    trail = events(db_path, request_id)

    again = run_rematch(db_path, registry)
    assert again["examined"] == 0  # the row is a draft now, not a blocked row
    assert again["moved"] == 0
    assert events(db_path, request_id) == trail

    # And a blocked row whose digest is current is examined but not rewritten.
    # This row was filed under the registry the app was built with, so the pass
    # runs against that same registry: nothing about the world moved, so the
    # verdict it already carries is still the answer.
    other = client.post("/v1/requests", json=UNCLAIMED, headers=auth()).json()[
        "request_id"
    ]
    same = run_rematch(db_path, registry_with(tmp_path=db_path.parent))
    assert same["examined"] == 1
    assert same["moved"] == 0
    assert len(events(db_path, other)) == 1  # `submitted`, and nothing else


def test_a_plan_that_still_does_not_fit_rewrites_the_verdict_and_says_why(client, db_path):
    """The failure mode is a fresh answer, not a bind: a request that now wants
    a different field is closer than it was, and the page can say so."""
    request_id = file_it(client)
    registry = registry_with(
        tmp_path=db_path.parent,
        datasets=lambda d: [*d, "ZZZZ.*"],
        params_schema=lambda schema: {
            **schema,
            "required": [*schema["required"], "book"],
            "properties": {
                **schema["properties"],
                "book": {"type": "string", "minLength": 1},
            },
        },
    )

    summary = run_rematch(db_path, registry)
    assert summary["bound"] == 0
    assert summary["moved"] == 1

    after = fetch(db_path, request_id)
    assert after["state"] == "blocked"
    assert after["job_json"] is None
    assert after["match_verdict"] == match.NEEDS_PARAMS
    report = json.loads(after["match_report_json"])
    assert report["would_fit"]["missing"] == ["book"]
    assert report["registry"] == match.registry_digest(registry)

    detail = json.loads(
        [e for e in events(db_path, request_id) if e["event"] == "rematched"][0][
            "detail_json"
        ]
    )
    assert detail["bound"] is False

    # `needs_params` outranks `requested`: the caller has a move again, and
    # saying "plan requested" would hide it.
    book = client.get("/ui/dashboard.json", headers=auth("tok-read")).json()
    row = next(r for r in book["rows"] if r["id"] == request_id)
    assert row["facet"] == "needs_params"


def test_an_escalated_row_moves_on_without_losing_the_ask(client, db_path):
    """The demand is history; the verdict is the present."""
    request_id = file_it(client)
    escalated = client.post(
        f"/v1/requests/{request_id}/escalate", json={}, headers=auth("tok-escalate")
    )
    assert escalated.status_code == 202, escalated.text
    assert fetch(db_path, request_id)["escalated_at"]

    run_rematch(
        db_path,
        registry_with(tmp_path=db_path.parent, datasets=lambda d: [*d, "ZZZZ.*"]),
    )

    after = fetch(db_path, request_id)
    assert after["state"] == "draft"
    assert after["escalated_at"], "the ask happened, and it stays on the record"


def test_a_row_with_a_job_is_never_re_matched(client, db_path):
    """A Job is write-once. A new plan version implies a different Job, which is
    a new Request, not an edit of this one."""
    request_id = file_it(client)
    registry = registry_with(tmp_path=db_path.parent, datasets=lambda d: [*d, "ZZZZ.*"])
    run_rematch(db_path, registry)
    bound = fetch(db_path, request_id)
    assert bound["state"] == "draft"

    summary = run_rematch(db_path, registry, only=[request_id])
    assert summary["examined"] == 0
    assert fetch(db_path, request_id)["job_hash"] == bound["job_hash"]


def test_a_dry_run_reports_the_sweep_and_writes_nothing(client, db_path):
    """What a person wants before a deploy moves a hundred rows into `draft`."""
    request_id = file_it(client)
    trail = events(db_path, request_id)
    before = fetch(db_path, request_id)

    summary = run_rematch(
        db_path,
        registry_with(tmp_path=db_path.parent, datasets=lambda d: [*d, "ZZZZ.*"]),
        dry_run=True,
    )
    assert summary["moved"] == 1
    assert summary["bound"] == 1
    assert summary["rows"][0]["bound"] is True

    assert fetch(db_path, request_id) == before
    assert events(db_path, request_id) == trail


def test_one_broken_row_does_not_stop_the_pass(client, db_path):
    """A stored body this code cannot read is that row's defect, not a reason to
    leave every other stuck request stuck."""
    good = file_it(client)
    bad = client.post(
        "/v1/requests",
        json={**UNCLAIMED, "why": "the one with an unparseable body"},
        headers=auth(),
    ).json()["request_id"]
    # A body that was valid when it was stored and is not now: the row is
    # evidence, not something to rewrite.
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE requests SET intent_json = ? WHERE id = ?", ("{not json", bad)
        )
        conn.commit()
    finally:
        conn.close()

    summary = run_rematch(
        db_path,
        registry_with(tmp_path=db_path.parent, datasets=lambda d: [*d, "ZZZZ.*"]),
    )
    assert summary["bound"] == 1
    assert [e["request_id"] for e in summary["errors"]] == [bad]
    assert fetch(db_path, good)["state"] == "draft"


# -- the digest that decides all of this ---------------------------------------


def test_the_digest_moves_when_the_schema_moves_without_a_version_bump(tmp_path):
    """A version is a promise; the digest is the mechanism.

    A schema edited without a bump changes the verdict just the same, and a
    digest that only counted versions would keep certifying the old answer --
    which is exactly the staleness this phase exists to catch.
    """
    base = load_registry(PLAN_TYPES_DIR)
    edited = registry_with(
        tmp_path=tmp_path,
        params_schema=lambda schema: {
            **schema,
            "properties": {**schema["properties"], "book": {"type": "string"}},
        },
    )
    assert match.registry_digest(base) != match.registry_digest(edited)


def test_the_digest_moves_when_the_claimed_datasets_move(tmp_path):
    """Which datasets a type claims is what `attempt` reads first."""
    base = load_registry(PLAN_TYPES_DIR)
    edited = registry_with(tmp_path=tmp_path, datasets=lambda d: [*d, "ZZZZ.*"])
    assert match.registry_digest(base) != match.registry_digest(edited)


def test_the_same_registry_gives_the_same_digest_every_time():
    """It is compared across processes and across deploys, so it cannot drift."""
    assert match.registry_digest(load_registry(PLAN_TYPES_DIR)) == match.registry_digest(
        load_registry(PLAN_TYPES_DIR)
    )
