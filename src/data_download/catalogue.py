"""What a dataset code means, asked from the vendor once and remembered.

`GLBX.MDP3` is vendor vocabulary. It is the only handle the request book and the
Datasets page have on a dataset, and it is not a name: a reader who has not
memorised Databento's code list learns nothing from it. The driver's `describe`
verb is the vendor's own answer to "what is this?", and this module is the app's
half of it -- a cache, a display vocabulary, and an off-the-request-path refresh.

Three deliberate choices:

1. **A cache, not a second source of truth.** The description is the vendor's,
   stored with `fetched_at`. Nothing here authors a label by hand: a typed
   description is a copy that goes stale silently, and it cannot exist for a
   dataset nobody has filed yet.

2. **`described: false` is an answer.** A code the vendor does not describe is
   *said* to be undescribed, never papered over and never an error. A page that
   hid it would show a bare code and let the reader guess whether the lookup had
   failed.

3. **Never on the request's path.** `describe` needs a driver session, and a
   cold session can spend a minute importing the vendor SDK. A page that waited
   for it would be a page whose speed depends on somebody else's API, so the
   read routes serve the cache and `schedule()` asks in the background. The cost
   is that the very first render of a new code shows it bare; the second one
   does not.

The window a dataset covers is worth saying in the same breath as its name, and
it is where the half-open convention earns its keep: `range.start` is the first
instant the vendor has data for and `range.end` is exclusive, exactly like the
window on a request (`format.js: windowOf`). Here it is only ever a boundary
between two dates, so the display says `available 2010-06-06 → now`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import driver_client

log = logging.getLogger(__name__)

#: What the page says for a code the vendor answered it cannot describe. The
#: vendor's silence, in the reader's words -- one string, so three surfaces
#: cannot each invent a friendlier half-truth.
NOT_DESCRIBED = "the vendor does not describe this code"

#: How long a description is believed. Dataset ranges move (a new day lands, a
#: new schema appears), so "current" is a week's claim, not a permanent one.
STALE_AFTER_S = 7 * 24 * 3600

#: After a failed ask, how long before trying again. Without this, every page
#: load of an unreachable runtime would spawn a session and fail -- a retry
#: storm aimed at a container that is already having a bad day.
RETRY_AFTER_S = 60.0

#: Live tasks, kept referenced: asyncio holds only a weak reference to a task,
#: so a background refresh nobody kept could be collected mid-await.
_TASKS: set[asyncio.Task] = set()

#: (vendor, dataset) -> monotonic time of the last failed ask.
_FAILED_AT: dict[tuple[str, str], float] = {}


def _json(value: Any) -> str | None:
    return json.dumps(value) if value is not None else None


def _loads(text: Any, fallback: Any) -> Any:
    if not isinstance(text, str) or not text:
        return fallback
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback


def _instant_utc(value: Any) -> date | None:
    """One end of a dataset range as a date.

    Databento answers nanoseconds since the epoch; an ISO string is accepted too
    so a driver that formats its own answer is not wrong. `None` (including a
    negative or absurd value) reads as "the vendor did not say", which the page
    then simply omits rather than drawing a date from 1677.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    try:
        seconds = int(value) / 1_000_000_000
        return datetime.fromtimestamp(seconds, UTC).date()
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _date_of(value: Any) -> str | None:
    moment = _instant_utc(value)
    return moment.isoformat() if moment else None


def entry(
    vendor: str,
    answer: dict[str, Any],
    *,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """One driver `describe` answer, in the shape every page reads.

    The vendor's words survive verbatim; only the range is translated, because
    nanoseconds are a wire detail and a reader needs a day.
    """
    moment = fetched_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    range_ = answer.get("range") if isinstance(answer.get("range"), dict) else {}
    start = _instant_utc(range_.get("start"))
    end = _instant_utc(range_.get("end"))
    # A range that runs to the present is the normal shape for a live dataset
    # (measured 2026-11-23: GLBX.MDP3 ends yesterday, because "today" is still
    # arriving). Printing that date invites the reader to believe the dataset
    # stopped; "now" is what the vendor means.
    end_phrase: str | None = None
    if end is not None:
        try:
            fetched = date.fromisoformat(moment[:10])
        except ValueError:
            fetched = None
        if fetched is not None and abs((fetched - end).days) <= 1:
            end_phrase = "now"
        else:
            end_phrase = end.isoformat()
    return {
        "vendor": vendor,
        "dataset": str(answer.get("dataset") or ""),
        "described": bool(answer.get("described")),
        "description": str(answer.get("description") or ""),
        # What the page prints after the code. Computed here so the "vendor has
        # no name for it" sentence lives in one place rather than in every view
        # that draws a dataset.
        "label": str(answer.get("description") or "") or NOT_DESCRIBED,
        "venue": str(answer.get("venue") or ""),
        "venues": [str(v) for v in answer.get("venues") or [] if v],
        "available_from": start.isoformat() if start else None,
        "available_to": end_phrase,
        "schemas": [str(s) for s in answer.get("schemas") or []],
        "fetched_at": moment,
    }


def row_of(entry_row: dict[str, Any]) -> dict[str, Any]:
    """A page entry -> the `dataset_catalogue` row that stores it."""
    return {
        "vendor": entry_row["vendor"],
        "dataset": entry_row["dataset"],
        "description": entry_row["description"],
        "described": 1 if entry_row["described"] else 0,
        "venue": entry_row["venue"],
        "venues_json": _json(entry_row["venues"]),
        "range_json": _json(
            {
                "start": entry_row["available_from"],
                "end": entry_row["available_to"],
            }
        ),
        "schemas_json": _json(entry_row["schemas"]),
        "fetched_at": entry_row["fetched_at"],
    }


def from_row(row: dict[str, Any]) -> dict[str, Any]:
    """A `dataset_catalogue` row -> the page entry. Never invents a description."""
    range_ = _loads(row.get("range_json"), {})
    return {
        "vendor": row["vendor"],
        "dataset": row["dataset"],
        "described": bool(row.get("described")),
        "description": str(row.get("description") or ""),
        "label": str(row.get("description") or "") or NOT_DESCRIBED,
        "venue": str(row.get("venue") or ""),
        "venues": [str(v) for v in _loads(row.get("venues_json"), []) if v],
        "available_from": (range_ or {}).get("start"),
        "available_to": (range_ or {}).get("end"),
        "schemas": [str(s) for s in _loads(row.get("schemas_json"), [])],
        "fetched_at": str(row.get("fetched_at") or ""),
    }


def is_stale(entry_row: dict[str, Any] | None, *, now: float | None = None) -> bool:
    """Whether a cached description is worth asking for again.

    A missing row is stale by definition; an unparseable `fetched_at` is too,
    because "I cannot tell how old this is" is the same answer as "old".
    """
    if entry_row is None:
        return True
    try:
        fetched = datetime.strptime(
            str(entry_row["fetched_at"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC).timestamp()
    except (KeyError, TypeError, ValueError):
        return True
    return (now if now is not None else time.time()) - fetched > STALE_AFTER_S


async def lookup(
    store: Any, pairs: list[tuple[str, str]] | set[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """The cached entries for `(vendor, dataset)` pairs, keyed the same way."""
    wanted = [(str(v), str(d)) for v, d in pairs if v and d]
    if not wanted:
        return {}
    rows = await store.catalogue_get(wanted)
    return {(row["vendor"], row["dataset"]): from_row(row) for row in rows}


def _ask_driver(vendor: str, dataset: str, root: Path | str | None) -> dict[str, Any]:
    """Ask the installed driver for `vendor` what `dataset` is.

    The session needs a root inside the dataset store (the runtime refuses
    anything else), and `describe` writes nothing -- so it gets a directory of
    its own, named so nobody reads it as a landing.
    """
    if root is None:
        raise driver_client.DriverUnavailable(
            "no datasets root is configured, so there is nowhere to root a driver session"
        )
    artifact = driver_client.driver_for(vendor)
    if artifact is None:
        raise driver_client.DriverUnavailable(f"no driver is installed for {vendor!r}")
    session_root = Path(root) / "_catalogue"
    with driver_client.RemoteDriver(
        session_root, driver=artifact.driver, driver_version=artifact.version
    ) as driver:
        return driver.describe(dataset=dataset)


async def refresh(
    store: Any,
    vendor: str,
    dataset: str,
    *,
    describe: Callable[[str, str, Path | str | None], dict[str, Any]] | None = None,
    root: Path | str | None = None,
) -> dict[str, Any] | None:
    """Ask, store, return. `None` when the ask failed -- a courtesy, not a Job.

    `describe` is injectable so a test can answer without a driver runtime; the
    real one runs in a thread because it is a blocking HTTP conversation.
    """
    ask = describe or _ask_driver
    try:
        if describe is None:
            answer = await asyncio.to_thread(ask, vendor, dataset, root)
        else:
            answer = ask(vendor, dataset, root)
    except Exception as exc:  # noqa: BLE001 - see docstring
        _FAILED_AT[(vendor, dataset)] = time.monotonic()
        log.warning("catalogue: could not describe %s for %s: %s", dataset, vendor, exc)
        return None
    _FAILED_AT.pop((vendor, dataset), None)
    described = entry(vendor, answer)
    await store.catalogue_put(row_of(described))
    return described


def schedule(
    store: Any,
    pairs: list[tuple[str, str]] | set[tuple[str, str]],
    *,
    root: Path | str | None = None,
    describe: Callable[[str, str, Path | str | None], dict[str, Any]] | None = None,
) -> None:
    """Ask about these pairs in the background. Best effort; never raises.

    The caller passes only pairs that are missing or stale, so a page that polls
    does not re-ask a description it already has.
    """
    now = time.monotonic()
    for vendor, dataset in pairs:
        vendor, dataset = str(vendor), str(dataset)
        if not vendor or not dataset:
            continue
        failed = _FAILED_AT.get((vendor, dataset))
        if failed is not None and now - failed < RETRY_AFTER_S:
            continue
        task = asyncio.create_task(
            refresh(store, vendor, dataset, describe=describe, root=root)
        )
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)


# -- the free pre-flight ------------------------------------------------------
#
# `describe` answers three questions a *submit* cares about -- is this a code the
# vendor knows, which schemas does it carry, and when does its data begin -- so a
# request the vendor could never satisfy can be refused here, against the cache,
# before a metered `cost` call. What it must never do is refuse on a guess: one
# wrong refusal is a request nobody can file, and the vendor's own 400 is a
# better answer than ours when the cache is cold, stale, or simply older than
# today's dataset list.


def _as_date(value: Any) -> date | None:
    """A date or an ISO instant as a UTC date.

    A timezone matters by a day at the edges: `2010-06-05T23:00-05:00` is
    2010-06-06 UTC, which is the day the vendor means.
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.date()


def refusal(
    entry_row: dict[str, Any] | None,
    *,
    dataset: str,
    schema: str | None = None,
    start: str | None = None,
) -> dict[str, Any] | None:
    """Why the catalogue says this request cannot be satisfied, or `None`.

    Only positive, current evidence refuses:

    * no cached row, an unparseable `fetched_at`, or a description older than
      `STALE_AFTER_S` -> `None`. The vendor gets to answer; a stale cache must
      not be the reason a valid request is impossible;
    * `described: false` -> `None` too, deliberately. A dataset code can be
      released *after* we asked, and refusing a new code for a week is worse
      than the typo this would have caught -- which the vendor already catches
      for free at `cost`;
    * a schema the dataset does not carry -> refuse. A dataset's schema set is
      the vendor's and effectively static;
    * a window starting before the dataset's first day -> refuse. Data does not
      move backwards (a backfill could, but that is rarer than the mistake);
    * a window ending after the range's end -> **allowed**. Measured live
      2026-11-24: the range end sits hours behind the clock, so "ends today" is
      a real request against a range that has not caught up. The end is a clock;
      the start is a fact.

    The answer is a dict rather than an exception so this module stays free of
    HTTP vocabulary; the route turns `message` into the caller's 422.
    """
    if entry_row is None or is_stale(entry_row):
        return None
    if not entry_row.get("described"):
        return None
    schemas = [str(s) for s in entry_row.get("schemas") or []]
    if schema and schemas and schema not in schemas:
        return {
            "reason": "schema",
            "dataset": dataset,
            "schema": schema,
            "schemas": schemas,
            "message": (
                f"{dataset} does not carry {schema!r}; it carries: "
                + ", ".join(schemas)
            ),
        }
    first = _as_date(entry_row.get("available_from"))
    asked = _as_date(start)
    if first is not None and asked is not None and asked < first:
        return {
            "reason": "window",
            "dataset": dataset,
            "available_from": first.isoformat(),
            "start": asked.isoformat(),
            "message": (
                f"{dataset} starts {first.isoformat()}; this window starts "
                f"{asked.isoformat()}"
            ),
        }
    return None
