"""The four views, exercised against directly seeded rows.

Three of `v_stuck`'s conditions cannot be reached through the API this phase -
nothing is ever approved, running or failed - so seeding is the only way to tell
a correct view from a broken one. Rows are written with plain `sqlite3` and the
views read back the same way, never through the app.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from conftest import INTENT_BODY, auth, iso, seed

PLAN_JSON = json.dumps({"kind": "plan"}, sort_keys=True)


def read_view(db_path: Path, name: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {name}")]
    finally:
        conn.close()


def _seed_all(path: Path) -> None:
    seed(
        path,
        [
            # 1. approved, old, nobody claimed it
            {
                "id": "seed_approved",
                "state": "approved",
                "created_at": iso(-timedelta(hours=2)),
                "job_json": PLAN_JSON,
                "estimate_usd": 1.5,
            },
            # 2. running, lease expired
            {
                "id": "seed_lease",
                "state": "running",
                "created_at": iso(-timedelta(hours=3)),
                "lease_until": iso(-timedelta(hours=1)),
                "job_json": PLAN_JSON,
            },
            # 3. running, valid lease, no progress for 7h
            {
                "id": "seed_hung",
                "state": "running",
                "created_at": iso(-timedelta(hours=9)),
                "updated_at": iso(-timedelta(hours=7)),
                "lease_until": iso(timedelta(hours=1)),
                "job_json": PLAN_JSON,
            },
            # 4. the same plan failing three times
            {
                "id": "seed_fail_1",
                "state": "failed",
                "created_at": iso(-timedelta(hours=5)),
                "job_json": PLAN_JSON,
                "job_hash": "hash_same_plan",
                "errors_json": json.dumps([{"code": "verify_contradiction"}]),
            },
            {
                "id": "seed_fail_2",
                "state": "failed",
                "created_at": iso(-timedelta(hours=4)),
                "job_json": PLAN_JSON,
                "job_hash": "hash_same_plan",
                "errors_json": json.dumps([{"code": "verify_contradiction"}]),
            },
            {
                "id": "seed_fail_3",
                "state": "failed",
                "created_at": iso(-timedelta(hours=3)),
                "job_json": PLAN_JSON,
                "job_hash": "hash_same_plan",
                "errors_json": json.dumps([{"code": "verify_contradiction"}]),
            },
            # one landed with errors, to prove v_done distinguishes it
            {
                "id": "seed_short",
                "state": "landed_with_errors",
                "job_json": PLAN_JSON,
                "errors_json": json.dumps([{"code": "verify_shortfall"}]),
            },
            # a draft, for v_pending
            {"id": "seed_draft", "state": "draft", "job_json": PLAN_JSON},
            # a blocked intent: not broken, so never in v_stuck
            {
                "id": "seed_blocked",
                "state": "blocked",
                "body_kind": "intent",
                "plan_type": None,
                "job_json": None,
                "estimate_usd": None,
                "blocked_reason": "awaiting_plan",
                "plan_origin": None,
                "intent_json": json.dumps({"max_usd": 120}),
                "created_at": iso(-timedelta(hours=6)),
            },
        ],
    )


def test_v_stuck_is_exactly_the_four_breakage_conditions(client, db_path):
    _seed_all(db_path)
    rows = read_view(db_path, "v_stuck")
    assert {row["condition"] for row in rows} == {
        "approved_unclaimed",
        "lease_expired",
        "running_too_long",
        "repeat_failure",
    }


def test_v_stuck_excludes_blocked(client, db_path):
    _seed_all(db_path)
    stuck = read_view(db_path, "v_stuck")
    assert "seed_blocked" not in {row["request_id"] for row in stuck}


def test_v_blocked_is_separate_and_says_what_clears_it(client, db_path):
    _seed_all(db_path)
    blocked = read_view(db_path, "v_blocked")
    assert [row["id"] for row in blocked] == ["seed_blocked"]
    assert blocked[0]["blocked_reason"] == "awaiting_plan"
    assert blocked[0]["merge_can_clear"] == 1
    assert blocked[0]["max_usd"] == 120
    assert blocked[0]["age_seconds"] > 5 * 3600


def test_v_done_flags_landed_with_errors(client, db_path):
    _seed_all(db_path)
    by_id = {row["id"]: row for row in read_view(db_path, "v_done")}
    assert by_id["seed_short"]["had_errors"] == 1
    assert by_id["seed_short"]["landed_clean"] == 0
    assert by_id["seed_fail_1"]["had_errors"] == 0
    assert "seed_draft" not in by_id


def test_v_pending_holds_drafts_and_approved(client, db_path):
    _seed_all(db_path)
    rows = read_view(db_path, "v_pending")
    ids = {row["id"] for row in rows}
    assert ids == {"seed_draft", "seed_approved"}
    approved = next(row for row in rows if row["id"] == "seed_approved")
    assert approved["approved_unclaimed"] == 1


def test_repeat_failure_does_not_fire_at_two(client, db_path):
    seed(
        db_path,
        [
            {
                "id": "two_1",
                "state": "failed",
                "job_json": PLAN_JSON,
                "job_hash": "h2",
            },
            {
                "id": "two_2",
                "state": "failed",
                "job_json": PLAN_JSON,
                "job_hash": "h2",
            },
        ],
    )
    assert read_view(db_path, "v_stuck") == []


def _book(client) -> dict:
    res = client.get("/ui/dashboard.json?token=tok-read")
    assert res.status_code == 200
    return res.json()


def test_dashboard_is_one_list_with_a_badge_per_row(client, db_path):
    """Every request appears exactly once, whatever its state.

    The page used to be four sections; it is now one list where the reason a
    row matters is a badge. The four SQL views remain the semantic truth and
    are asserted separately above - what this pins is that flattening them did
    not drop a row on the floor.

    This is the *view-model*, not the markup. The page that draws it is
    e2e/app.spec.js; what has to be true of the data is here, because the
    precedence below is the read-model's opinion and not a rendering choice.
    """
    _seed_all(db_path)
    book = _book(client)
    ids = [row["id"] for row in book["rows"]]

    assert sorted(ids) == sorted(set(ids)), "a row appeared twice"
    for request_id in (
        "seed_approved",
        "seed_lease",
        "seed_hung",
        "seed_short",
        "seed_draft",
        "seed_blocked",
    ):
        assert request_id in ids, f"{request_id} missing from the list"

    by_id = {row["id"]: row for row in book["rows"]}

    # All four breakage conditions are labelled inline, not sectioned.
    for request_id, condition in (
        ("seed_lease", "lease_expired"),
        ("seed_hung", "running_too_long"),
        ("seed_approved", "approved_unclaimed"),
    ):
        assert by_id[request_id]["stuck_condition"] == condition
        assert by_id[request_id]["facet"] == "broken"
    # `repeat_failure` fires on the plan hash, not on a row, so it is one
    # condition over three rows.
    repeats = [
        row for row in book["rows"] if row["stuck_condition"] == "repeat_failure"
    ]
    assert repeats, "the three-failure plan hash is not labelled"
    assert all(row["facet"] == "broken" for row in repeats)

    # A row that merely landed badly is not breakage.
    assert by_id["seed_short"]["facet"] == "done"
    assert by_id["seed_short"]["had_errors"]  # sqlite gives 1, not True

    # The blocked row says what it is waiting for, and that it is not breakage.
    assert by_id["seed_blocked"]["facet"] == "waiting"
    assert by_id["seed_blocked"]["blocked_reason"] == "awaiting_plan"
    assert by_id["seed_blocked"]["facet_label"] == "Waiting on a plan"

    # No four-section names survive in the view.
    assert not {row["facet"] for row in book["rows"]} & {
        "v_pending",
        "v_done",
        "v_stuck",
        "v_blocked",
    }
    assert book["total"] == len(book["rows"])


def test_the_facet_index_partitions_the_rows(client, db_path):
    """The page filters in the browser, so the index has to be exhaustive: a
    row in no bucket is a row that vanishes when any filter is on."""
    _seed_all(db_path)
    book = _book(client)

    bucketed = [row["id"] for bucket in book["facets"].values() for row in bucket]
    assert sorted(bucketed) == sorted(row["id"] for row in book["rows"])

    broken = {row["id"] for row in book["facets"]["broken"]}
    assert {"seed_lease", "seed_approved"} <= broken
    assert "seed_blocked" not in broken and "seed_draft" not in broken

    waiting = {row["id"] for row in book["facets"]["waiting"]}
    assert waiting == {"seed_blocked"}

    todo = {row["id"] for row in book["facets"]["todo"]}
    assert todo == {"seed_draft"}


def test_a_row_needs_a_human_in_the_order_the_ui_labels_it(client, db_path):
    """Breakage outranks the state it happens in: an approved-but-unclaimed
    request is broken, not "to approve", or it reads as work waiting on a
    human rather than as a request nobody picked up."""
    _seed_all(db_path)
    book = _book(client)
    assert book["facet_order"][0] == "broken"
    assert book["facet_order"][-1] == "done"


def test_the_view_carries_no_server_trivia(client, db_path):
    """The page renders a request book and nothing else.

    `generated_at`, `db_path` and `registry_count` existed only to fill the
    footer, and the footer is gone: a timestamp on a page that is never cached
    always says "now", the store path is the host's business rather than the
    reader's, and the registry count repeated what the other tab already shows.
    A field nothing renders is a field that silently rots, so they are not
    sent either.
    """
    _seed_all(db_path)
    book = _book(client)
    for gone in ("generated_at", "db_path", "registry_count"):
        assert gone not in book, f"{gone} is back in the view-model"

    types = client.get("/ui/plan-types.json?token=tok-read").json()
    assert "generated_at" not in types


def test_a_plan_type_card_names_its_vendor(client):
    """The type's name carries the vendor as a prefix, which is not the same
    thing as saying who sells the data to a reader who does not decode
    `databento_batch_download`."""
    types = client.get("/ui/plan-types.json?token=tok-read").json()["types"]
    assert [t["vendor"] for t in types] == ["Databento"]


def test_the_view_accepts_a_query_token(client, db_path):
    """A browser cannot set an Authorization header on a plain navigation, so
    the token has to be accepted in the URL - on the view as well as the page."""
    _seed_all(db_path)
    assert client.get("/ui/dashboard.json").status_code == 401
    assert client.get("/ui/dashboard.json?token=nonsense").status_code == 401
    assert client.get("/ui/dashboard.json?token=tok-read").status_code == 200
    assert client.get("/ui/dashboard.json", headers=auth()).status_code == 200


def test_the_page_is_the_built_bundle(client, db_path, tmp_path, monkeypatch):
    """The HTML route serves a file, not a template. If the bundle is missing
    the failure has to say so; a blank page would look like an empty book."""
    from data_download import app as app_module

    built = tmp_path / "web"
    (built / "assets").mkdir(parents=True)
    (built / "index.html").write_text("<html><body>built</body></html>")
    monkeypatch.setattr(app_module, "WEB_DIST_DIR", built)

    res = client.get("/?token=tok-read")
    assert res.status_code == 200
    assert "built" in res.text
    assert "no-store" in res.headers.get("cache-control", "")

    monkeypatch.setattr(app_module, "WEB_DIST_DIR", tmp_path / "absent")
    res = client.get("/?token=tok-read")
    assert res.status_code == 503
    assert "npm run build" in res.json()["detail"]


def test_dashboard_requires_read_scope(client):
    assert client.get("/").status_code == 401
    assert client.get("/?token=nonsense").status_code == 401
    assert client.get("/", headers=auth("tok-approve")).status_code == 403


def test_dashboard_is_get_only(client):
    assert client.post("/", headers=auth()).status_code == 405


def test_icons_and_manifest_are_publicly_accessible(client):
    """Browsers fetch icons without auth headers; these must return 200."""
    for path in (
        "/favicon.ico",
        "/apple-touch-icon.png",
        "/apple-touch-icon-precomposed.png",
        "/site.webmanifest",
        "/static/apple-touch-icon.png",
        "/static/favicon.svg",
        "/static/icon-192.png",
        "/static/icon-512.png",
    ):
        res = client.get(path)
        assert res.status_code == 200, f"{path} returned {res.status_code}"
        assert len(res.content) > 0


# -- the request book as a list of names, and the page behind a row -----------


def test_a_row_leads_with_the_subject_and_keeps_the_key(client, db_path):
    """`#seq` and what was asked for, not 32 hex characters.

    The id is a key: opaque on purpose, because it has to be collision-free and
    stable forever. That is exactly what makes it unfit to be the first thing a
    reader sees, so the row carries a label too -- derived server-side, because
    "what is this request" is the read-model's opinion, not a rendering choice.
    """
    seed(
        db_path,
        [
            {
                "id": "req_0123456789abcdef0123456789abcdef",
                "state": "blocked",
                "body_kind": "intent",
                "plan_type": None,
                "job_json": None,
                "estimate_usd": None,
                "plan_origin": None,
                "blocked_reason": "awaiting_plan",
                "params_json": json.dumps(
                    {
                        "dataset": "XNAS.ITCH",
                        "schema": "mbo",
                        "window": {"start": "2026-09-01", "end": "2026-09-02"},
                    }
                ),
                "intent_json": json.dumps({"max_usd": 120}),
                "why": "one September day of MBO for XNAS",
            }
        ],
    )

    row = _book(client)["rows"][0]
    assert row["label"] == "XNAS.ITCH · mbo · 2026-09-01→2026-09-02"
    assert row["id"] == "req_0123456789abcdef0123456789abcdef"
    # Not even abbreviated: a row shows no part of the key.
    assert "short_id" not in row
    assert row["why"] == "one September day of MBO for XNAS"
    assert "params_json" not in row, "the raw column is not the page's business"


def test_a_plan_row_is_labelled_by_its_type(client, db_path):
    seed(db_path, [{"id": "req_plan", "state": "draft"}])
    row = _book(client)["rows"][0]
    assert row["label"] == "databento_batch_download"


def test_a_row_names_the_vendor_the_dataset_belongs_to(client, db_path):
    """`XNAS.ITCH` is vendor vocabulary: without the vendor it means nothing.

    A plan row has a plan type, and the type declares its vendor. An intent has
    no type -- not until a plan exists -- so its vendor comes from whichever
    registered type claims the dataset code.
    """
    seed(
        db_path,
        [
            {
                "id": "req_intent",
                "state": "blocked",
                "body_kind": "intent",
                "plan_type": None,
                "job_json": None,
                "estimate_usd": None,
                "blocked_reason": "awaiting_plan",
                "params_json": json.dumps(
                    {"dataset": "XNAS.ITCH", "schema": "mbo", "window": {}}
                ),
                "intent_json": json.dumps({"max_usd": 10}),
            },
            {"id": "req_plan", "state": "draft"},
        ],
    )
    by_id = {row["id"]: row for row in _book(client)["rows"]}
    assert by_id["req_intent"]["vendor"] == "Databento"
    assert by_id["req_plan"]["vendor"] == "Databento"


def test_an_unclaimed_dataset_shows_no_vendor_rather_than_a_guess(client, db_path):
    """No registered type speaks for this code, and the row says so.

    The alternative -- defaulting to the vendor of the only type we have --
    would be right today and quietly wrong the day a second one is added.
    """
    seed(
        db_path,
        [
            {
                "id": "req_odd",
                "state": "blocked",
                "body_kind": "intent",
                "plan_type": None,
                "job_json": None,
                "estimate_usd": None,
                "blocked_reason": "awaiting_plan",
                "params_json": json.dumps(
                    {"dataset": "NOTAVENDOR.SET", "schema": "mbo", "window": {}}
                ),
                "intent_json": json.dumps({"max_usd": 10}),
            }
        ],
    )
    assert _book(client)["rows"][0]["vendor"] == ""


def _detail(client, request_id, token="tok-read"):
    return client.get(f"/ui/request/{request_id}.json?token={token}")


def test_the_detail_view_carries_what_the_list_drops(client):
    """The list is keys and badges; this is the why, the body, and the trail."""
    filed = client.post("/v1/requests", json=INTENT_BODY, headers=auth()).json()
    request_id = filed["request_id"]

    detail = _detail(client, request_id)
    assert detail.status_code == 200
    body = detail.json()

    assert body["id"] == request_id
    assert body["facet"] == "waiting"
    assert body["label"].startswith("OPRA.PILLAR · trades")
    assert body["payload"]["why"] == INTENT_BODY["why"]
    assert body["payload"]["max_usd"] == 120
    assert body["payload"]["intent"]["intent"]["dataset"] == "OPRA.PILLAR"
    assert body["payload"]["job"] is None
    assert [e["event"] for e in body["events"]] == ["submitted"]
    assert body["events"][0]["actor_kind"] == "submitter"


def test_the_detail_view_agrees_with_the_list_it_was_reached_from(client):
    """Same age, same facet, same condition -- two answers to "why is this row
    interesting" is the drift §7.8 warns about."""
    filed = client.post("/v1/requests", json=INTENT_BODY, headers=auth()).json()
    row = _book(client)["rows"][0]
    detail = _detail(client, filed["request_id"]).json()
    assert (detail["facet"], detail["blocked_reason"]) == (
        row["facet"],
        row["blocked_reason"],
    )
    assert abs(detail["age_seconds"] - row["age_seconds"]) <= 2


def test_the_detail_view_is_read_scope_and_404s_cleanly(client):
    filed = client.post("/v1/requests", json=INTENT_BODY, headers=auth()).json()
    denied = _detail(client, filed["request_id"], token="tok-approve")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "insufficient_scope"

    missing = _detail(client, "req_nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
