"""The match: does a plan we already have take this intent? (§phase 2.5)

Pure, so it is tested as a function of (intent, registry) with no store in the
way -- which is also the property that lets a recorded verdict mean the same
thing a year later.
"""

from __future__ import annotations

import dataclasses
import json

from conftest import PLAN_TYPES_DIR
from data_download import match
from data_download.registry import Registry, load_registry

REGISTRY = load_registry(PLAN_TYPES_DIR)

FULL = {
    "dataset": "XNAS.ITCH",
    "schema": "mbo",
    "window": {"start": "2026-09-01", "end": "2026-09-02"},
    "symbols": ["AAPL"],
    "stype_in": "raw_symbol",
}

PARTIAL = {
    "dataset": "XNAS.ITCH",
    "schema": "mbo",
    "window": {"start": "2026-09-01", "end": "2026-09-02"},
}


def test_a_complete_intent_matches_the_one_registered_plan():
    report = match.attempt(FULL, REGISTRY)
    assert report["verdict"] == match.MATCHED
    assert report["plan_type"] == "databento_batch_download"
    chosen = next(c for c in report["candidates"] if c["verdict"] == match.MATCHED)
    # The window became start/end, and nothing was invented.
    assert chosen["params"] == {
        "dataset": "XNAS.ITCH",
        "schema": "mbo",
        "symbols": ["AAPL"],
        "stype_in": "raw_symbol",
        "start": "2026-09-01",
        "end": "2026-09-02",
    }


def test_a_missing_param_is_named_and_an_example_is_offered():
    report = match.attempt(PARTIAL, REGISTRY)
    assert report["verdict"] == match.NEEDS_PARAMS
    assert report["plan_type"] is None
    assert sorted(report["would_fit"]["missing"]) == ["stype_in", "symbols"]
    example = report["would_fit"]["params"]
    # What the caller supplied is kept; only the gaps are placeholders.
    assert example["dataset"] == "XNAS.ITCH"
    assert example["start"] == "2026-09-01"
    assert example["symbols"] == ["<symbols>"]
    # `stype_in` is an enum: the example uses a legal value, not a placeholder.
    assert example["stype_in"] in REGISTRY.get("databento_batch_download").params_schema[
        "properties"
    ]["stype_in"]["enum"]


def test_a_rejected_value_is_not_a_missing_param():
    """An empty symbol list is a value the intent supplied and the plan cannot take."""
    report = match.attempt({**FULL, "symbols": []}, REGISTRY)
    assert report["verdict"] == match.CANNOT_EXPRESS
    candidate = report["candidates"][0]
    assert candidate["verdict"] == match.CANNOT_EXPRESS
    assert "symbols" in candidate["rejected"]
    assert candidate["missing"] == []
    assert "would_fit" not in report


def test_a_dataset_no_plan_claims_is_not_a_near_miss():
    report = match.attempt({**FULL, "dataset": "NOTAVENDOR.X"}, REGISTRY)
    assert report["verdict"] == match.NOT_CLAIMED
    assert report["candidates"][0]["verdict"] == match.NOT_CLAIMED
    assert "would_fit" not in report


def test_the_report_is_reproducible():
    first = match.as_json(match.attempt(PARTIAL, REGISTRY))
    second = match.as_json(match.attempt(PARTIAL, REGISTRY))
    assert first == second
    assert json.loads(first)["verdict"] == match.NEEDS_PARAMS


def test_the_registry_digest_moves_with_a_version_bump():
    """An old verdict must not read as a claim about today's registry."""
    before = match.attempt(PARTIAL, REGISTRY)["registry"]
    plan = REGISTRY.get("databento_batch_download")
    bumped = Registry(
        {plan.name: dataclasses.replace(plan, version=plan.version + 1)},
        REGISTRY.directory,
    )
    after = match.attempt(PARTIAL, bumped)["registry"]
    assert before != after


def test_a_plan_that_claims_nothing_is_not_consulted_for_any_dataset():
    """`datasets: []` means "no claim", not "claims everything"."""
    plan = REGISTRY.get("databento_batch_download")
    silent = Registry(
        {plan.name: dataclasses.replace(plan, datasets=())}, REGISTRY.directory
    )
    report = match.attempt(PARTIAL, silent)
    assert report["candidates"][0]["verdict"] == match.NEEDS_PARAMS
