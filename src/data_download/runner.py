"""The runner: an approved row in, bytes under `/datasets`, a settled row out (§8).

Phases 2-4 built everything up to the point where a request *could* run: a Job
that is bound and sealed, a quote, a ceiling, an approval, a claim with a lease
that can be reclaimed. Nothing before this module puts a byte anywhere. This is
the one place in the repo where an approved request becomes files on disk.

Four rules, in the order they bite:

1. **The claim is the permission.** The runner does not re-check the state, the
   ceiling or the code ref: `Store.claim` already did, in one statement, and a
   second check would be a second vocabulary for the same refusal. A row it did
   not claim is a row it does not run.
2. **Only a leaseholder may settle.** `Store.settle` carries the same
   `AND owner = ?` as `release`, so a runner whose lease expired and was
   reclaimed cannot write `succeeded` over the new claimant's attempt. Losing
   that CAS is the reclaim working, not an error.
3. **`dest` is relative and the root is not.** The Job stages into `files/`;
   the absolute directory is `datasets/<vendor>/<dataset>/<request_id>/`, built
   here and nowhere earlier (§5.7 -- a `job_hash` must never be a path segment).
   The vendor is the top level because a dataset code is vendor vocabulary
   (`XNAS.ITCH` means nothing without knowing who sells it) and because the
   datasets root already holds data from more than one vendor; the request id
   underneath keeps two requests for one dataset apart. The vendor comes from
   the plan type in the registry, not from the Job, which is a flow and knows no
   seller.
4. **The verdict decides the state, not the runner.** `pass` -> `succeeded`,
   `commit_marked` -> `landed_with_errors`, `quarantine` -> `failed`. The
   runner maps; it never overrules `interpreter.classify`.

5. **The driver is in another container.** Everything the runner needs to know
   about it — that one is installed for this vendor, which version, and what it
   answers — is asked over the wire (`driver_client`). The app does not mount
   the artefacts directory and does not import a vendor SDK: a new driver is an
   install in the other container, not a deploy of this one.

The driver is injected. `run()` hands over a `driver_client.RemoteDriver`
pointed at whatever the runtime says it has installed for this row's vendor; a
test hands over its own factory instead. The eight verbs are the interface, so
this module does not know whether they are answered in this process or in
another container — which is what made moving the stub out cheap.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import driver_client, interpreter
from . import job as job_mod
from .claim import default_owner
from .config import find_plan_types_dir, load_settings
from .db import LEASE_SECONDS, ClaimRefused, Store, utcnow
from .registry import Registry, load_registry

# Bumped when this module's behaviour changes, and recorded in `landing.json` and
# the `landed` event. `interpreter_version` is separate: which flow ran and which
# shell settled it are different facts.
RUNNER_VERSION = "1"

# A vendor or dataset names a directory under the datasets root, so both are
# constrained: `..` or a slash would land bytes outside the store, and the
# dataset is submitter-controlled (params.dataset) until a binder refuses it.
# Dot is allowed (`GLBX.MDP3`); a leading dot is not.
DATASET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

LANDING_FILE = "landing.json"


class RunRefused(Exception):
    """The runner said no before (or instead of) spending."

    A closed set of reasons, for the same purpose as `ClaimRefused`: the CLI and
    any future supervisor must not invent their own words for "the store said
    the row is not mine" and "the Job does not validate".
    """

    REASONS = frozenset(
        {
            "not_configured",
            "bad_request_id",
            "bad_dataset",
            "bad_vendor",
            "vendor_unknown",
            "job_missing",
            "job_invalid",
            "job_hash_mismatch",
            "driver_not_ready",
            # Phase 5.5: the runtime answered and nobody drives this vendor. The
            # request is not failed -- it is waiting for the loop to build one.
            "driver_missing",
        }
    )

    def __init__(self, reason: str, detail: str | None = None) -> None:
        if reason not in self.REASONS:
            raise ValueError(f"unknown refusal {reason!r}")
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class RunResult:
    """What one run did, in the words the CLI prints and the tests assert."""

    request_id: str
    state: str
    verdict: str | None
    disposition: str | None
    owner: str
    dest: Path | None = None
    files: tuple[dict[str, Any], ...] = ()
    total_bytes: int = 0
    landed_sha256: str | None = None
    errors: dict[str, Any] | None = None
    settled: bool = True
    steps: tuple[str, ...] = ()
    #: Set when the runner declined to run: `driver_missing` is the only one
    #: that is not a failure, because it is a queue.
    refused: str | None = None

    @property
    def landed(self) -> bool:
        return self.state in {"succeeded", "landed_with_errors"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "state": self.state,
            "verdict": self.verdict,
            "disposition": self.disposition,
            "owner": self.owner,
            "dest": str(self.dest) if self.dest else None,
            "files": list(self.files),
            "total_bytes": self.total_bytes,
            "landed_sha256": self.landed_sha256,
            "errors": self.errors,
            "settled": self.settled,
            "steps": list(self.steps),
            "refused": self.refused,
        }


# -- pure helpers ---------------------------------------------------------


def dataset_of(job: Any) -> str | None:
    """The dataset to file this Job's bytes under: the `resolve` step's argument.

    Read off the Job rather than off params, because the Job is what was sealed
    and approved; params are the earlier draft of the same fact.
    """
    steps = job.get("steps") if isinstance(job, dict) else None
    for step in steps or []:
        if isinstance(step, dict) and step.get("kind") == "resolve":
            dataset = (step.get("kwargs") or {}).get("dataset")
            return str(dataset) if dataset else None
    return None


def vendor_slug(vendor: str | None) -> str:
    """`Databento` -> `databento`: the top level of the store, lowercased.

    Case is not meaningful in a directory name and two spellings of one vendor
    would be two folders, so the registry's display name is folded once, here.
    """
    slug = " ".join((vendor or "").split()).lower().replace(" ", "-").replace("_", "-")
    if not DATASET_RE.match(slug):
        raise RunRefused("bad_vendor", f"{vendor!r} is not a directory name")
    return slug


def destination(
    datasets_dir: Path, vendor: str, dataset: str, request_id: str
) -> Path:
    """`<datasets>/<vendor>/<dataset>/<request_id>/` -- validated, then joined."""
    if not DATASET_RE.match(dataset or ""):
        raise RunRefused("bad_dataset", f"{dataset!r} is not a directory name")
    if not REQUEST_ID_RE.match(request_id or ""):
        raise RunRefused("bad_request_id", f"{request_id!r} is not a request id")
    return Path(datasets_dir) / vendor_slug(vendor) / dataset / request_id


def bundle_digest(files: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    """One hash for the landing: the sorted `sha256  name` lines.

    Per-file hashes are recorded too, but the row has one `landed_sha256`
    column, and "which bytes landed for this request" has to be one value that
    can be compared later -- to a re-run, or to a copy on another host.
    """
    manifest = "".join(
        f"{entry['sha256']}  {entry['name']}\n"
        for entry in sorted(files, key=lambda e: str(e.get("name")))
    )
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()


def staged_files(dest: Path) -> dict[str, dict[str, Any]]:
    """What is on disk under `dest`, hashed.

    The runner reads this to record what it landed, and it is deliberately the
    same reading the verifier does: a landing recorded by a different rule than
    the one that judges it is two beliefs about one directory. (It lived in the
    driver until the driver moved out of this process; a driver that is asked
    "what did you write" could answer anything, and this half is trusted.)
    """
    staged: dict[str, dict[str, Any]] = {}
    if not dest.is_dir():
        return staged
    for path in sorted(dest.iterdir()):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        staged[path.name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "rows": payload.count(b"\n") - 1,  # the header line is not a row
        }
    return staged


def driver_name(driver: Any) -> str:
    """Who produced the bytes, in the words the operator can act on.

    For a `RemoteDriver` that is the **artefact** name the runtime reports
    (`stub`, `databento`), not the class name `RemoteDriver` -- the class is the
    same for every driver on the planet and would make `landing.json` say
    nothing about which code wrote the files.
    """
    name = getattr(driver, "driver", None)
    return str(name) if name else type(driver).__name__


def driver_version(driver: Any) -> str | None:
    """The version the session actually ran, preferring the runtime's answer."""
    for attribute in ("session_driver_version", "driver_version"):
        value = getattr(driver, attribute, None)
        if value:
            return str(value)
    return None


def landed_files(root: Path, dest: str) -> tuple[dict[str, Any], ...]:
    """Every file the run put under its `dest`, with size and hash."""
    base = Path(str(dest))
    base = base if base.is_absolute() else root / base
    staged = staged_files(base)
    out: list[dict[str, Any]] = []
    for name in sorted(staged):
        path = base / name
        out.append(
            {
                "name": name,
                "sha256": staged[name]["sha256"],
                "rows": staged[name]["rows"],
                "bytes": path.stat().st_size,
            }
        )
    return tuple(out)


def dest_of(job: Any) -> str | None:
    steps = job.get("steps") if isinstance(job, dict) else None
    for step in steps or []:
        if isinstance(step, dict) and step.get("kind") == "download":
            dest = (step.get("kwargs") or {}).get("dest")
            return str(dest) if dest else None
    return None


def state_for(disposition: str) -> str:
    """`interpreter`'s disposition -> the Request's state (§3.1)."""
    if disposition == interpreter.COMMIT:
        return "succeeded"
    if disposition == interpreter.COMMIT_MARKED:
        return "landed_with_errors"
    return "failed"


# -- the lease, kept while the work outlives it ---------------------------


#: How often the heartbeat renews, as a fraction of the lease: three beats per
#: lease, so two can be lost to a slow statement before the row is reclaimable.
HEARTBEAT_FRACTION = 3


async def keep_lease(
    store: Store,
    request_id: str,
    owner: str,
    *,
    lease_seconds: int,
    stop: asyncio.Event,
    lost: asyncio.Event,
) -> None:
    """Say "still working" until `stop` is set, or until the row is not ours.

    A task rather than a loop inside the run because the run is a blocking call:
    `interpreter.run` sits in the driver's HTTP session for as long as the vendor
    takes, so the heart has to beat on the event loop beside it (`to_thread`),
    not inside it. Losing the lease sets `lost` rather than cancelling anything:
    there is nothing safe to kill mid-vendor-call, and the caller checks the flag
    before it settles -- `Store.settle`'s CAS is the real guard either way.
    """
    interval = max(1.0, lease_seconds / HEARTBEAT_FRACTION)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        if not await store.renew(request_id, owner, lease_seconds=lease_seconds):
            lost.set()
            return


# -- one run --------------------------------------------------------------


def _vendor_and_dataset(row: dict[str, Any], registry: Registry) -> tuple[str, str | None]:
    """Who is being asked, and for what -- read from the row, before any claim."""
    try:
        params = json.loads(row.get("params_json") or "{}")
    except (TypeError, ValueError):
        params = {}
    if not isinstance(params, dict):
        params = {}
    vendor = registry.vendor_of_request(row.get("plan_type"), params)
    job: Any = None
    if row.get("job_json"):
        try:
            job = json.loads(row["job_json"])
        except (TypeError, ValueError):
            job = None
    dataset = dataset_of(job) if job is not None else params.get("dataset")
    return vendor, str(dataset) if dataset else None


async def _queue_for_missing_driver(
    request_id: str,
    *,
    store: Store,
    row: dict[str, Any],
    registry: Registry,
    driver_url: str | None,
    token: str | None,
) -> RunResult | None:
    """Leave the row `approved` and ask the loop for a driver, or return `None`.

    This runs **before the claim**, because the claim is the permission to run
    and there is nothing to run: settling `failed` would say a vendor attempt
    happened, and creating the request directory would leave an empty one that
    reads as "an attempt produced nothing".

    Two absences are deliberately different. No runtime token is
    `driver_not_ready` -- a configuration fault that a queue cannot fix, so the
    runner refuses and the row stays `approved`. A reachable runtime with no
    artefact for the vendor is `driver_missing` -- a driver to write, which is
    what phase 5.5's loop exists to do.
    """
    vendor, dataset = _vendor_and_dataset(row, registry)
    if not vendor:
        return None  # let the claimed path settle `vendor_unknown` as it always did
    try:
        entries = driver_client.installed(url=driver_url, token=token)
    except driver_client.DriverUnavailable as exc:
        raise RunRefused("driver_not_ready", str(exc)) from None
    if driver_client.driver_for(vendor, artifacts=entries) is not None:
        return None

    job = await store.driver_job_enqueue(
        vendor=vendor,
        plan_types=[row["plan_type"]] if row.get("plan_type") else [],
        dataset=dataset,
        request_id=request_id,
    )
    await store.note_driver_state(
        request_id,
        "building",
        detail={
            "vendor": vendor,
            "driver_job": (job or {}).get("id"),
            "queued": job is not None,
            "installed": [str(entry) for entry in entries],
        },
    )
    available = ", ".join(str(entry) for entry in entries) or "nothing"
    return RunResult(
        request_id=request_id,
        state=str(row.get("state")),
        verdict=None,
        disposition=None,
        owner="",
        errors={
            "error": "driver_missing",
            "detail": f"no driver is installed for vendor {vendor!r} "
            f"(installed: {available}); queued for the driver loop",
        },
        settled=False,
        steps=("driver_missing",),
        refused="driver_missing",
    )


async def run_one(
    request_id: str,
    *,
    store: Store,
    datasets_dir: Path | str | None,
    owner: str | None = None,
    mode: str = "clean",
    lease_seconds: int | None = None,
    driver_factory: Callable[..., Any] | None = None,
    driver_url: str | None = None,
    driver_token: str | None = None,
    registry: Registry | None = None,
    now: str | None = None,
) -> RunResult:
    """Claim one row, run its Job, settle it. Raises `ClaimRefused` if not ours.

    `driver_factory` is for tests that want to stand in for the runtime. Left
    `None`, the driver is discovered and driven over the wire — the path every
    deployment takes, so the tests that use it are testing what runs.
    """
    token = driver_token if driver_token is not None else (os.environ.get("DRIVER_RUNTIME_TOKEN") or None)
    if datasets_dir is None:
        # Fail closed: writing bytes somewhere nobody named is worse than not
        # running, because the Request then looks done and the bytes are lost.
        raise RunRefused(
            "not_configured",
            "no datasets directory: set DATA_DOWNLOAD_DATASETS_DIR or --datasets-dir",
        )
    if not REQUEST_ID_RE.match(request_id or ""):
        raise RunRefused("bad_request_id", f"{request_id!r} is not a request id")

    if driver_factory is None and not token:
        # Before the claim, like the datasets check: a row that cannot be run
        # must stay `approved`, not become a `failed` row that looks like a
        # vendor outcome. The runtime refuses an unauthenticated caller, so
        # there is nothing this process could do with the row anyway.
        raise RunRefused(
            "driver_not_ready",
            "no driver runtime token: set DRIVER_RUNTIME_TOKEN to the driver "
            "runtime's token (and DATA_DOWNLOAD_DRIVER_RUNTIME_URL if it is not "
            "http://data-download-drivers:8831)",
        )

    owner = owner or default_owner()
    if driver_factory is None:
        # Discovery before the claim: a row nobody can drive stays `approved`
        # and gains a queue entry, rather than failing for a reason that is
        # ours to fix (phase 5.5).
        wanted = await store.get_request(request_id)
        if wanted is not None and wanted.get("state") == "approved":
            queued = await _queue_for_missing_driver(
                request_id,
                store=store,
                row=wanted,
                registry=registry or load_registry(find_plan_types_dir()),
                driver_url=driver_url,
                token=token,
            )
            if queued is not None:
                return queued
    row = await store.claim(request_id, owner, lease_seconds=lease_seconds)
    if row.get("driver_state"):
        # The loop's answer arrived (or somebody installed a driver by hand) and
        # this row is running: the waiting label is no longer true, and a
        # `succeeded` row still saying "building" is a lie that outlives it.
        await store.note_driver_state(
            request_id, None, detail={"cleared": "running", "owner": owner}
        )

    try:
        job_text = row.get("job_json")
        if not job_text:
            return await _settle_refusal(
                store, request_id, owner, "failed", {"error": "job_missing"}, now=now
            )

        try:
            job = json.loads(job_text)
            job_mod.validate(job)
        except (json.JSONDecodeError, job_mod.JobInvalid) as exc:
            # A stored Job that does not validate is a code or store fault, not
            # a vendor outcome: settle `failed` with the reason rather than
            # leaving the row `running` until the lease expires.
            message = getattr(exc, "message", str(exc))
            return await _settle_refusal(
                store, request_id, owner, "failed",
                {"error": "job_invalid", "detail": message}, now=now,
            )

        if job_mod.job_hash(job_text) != row.get("job_hash"):
            return await _settle_refusal(
                store, request_id, owner, "failed",
                {"error": "job_hash_mismatch"}, now=now,
            )

        dataset = dataset_of(job)
        # The vendor is the request's fact, on the params: one plan type serves
        # every seller, so the type cannot say who is being asked. Parsed here
        # because the destination path and the driver are both chosen by it --
        # guessing one would file bytes under a vendor that never sold them.
        try:
            params = json.loads(row.get("params_json") or "{}")
        except (TypeError, ValueError):
            params = {}
        if not isinstance(params, dict):
            params = {}
        vendor = (registry or load_registry(find_plan_types_dir())).vendor_of_request(
            row.get("plan_type"), params
        )
        if not vendor:
            # The datasets root is organised by seller, and the seller is the
            # plan type's declaration. A row whose type is not in the registry
            # (code moved on, row stayed) has no place in that tree, and
            # guessing one would file bytes under a vendor that never sold them.
            return await _settle_refusal(
                store, request_id, owner, "failed",
                {
                    "error": "vendor_unknown",
                    "detail": f"plan_type {row.get('plan_type')!r} declares no vendor",
                },
                now=now,
            )
        root = destination(Path(datasets_dir), vendor, dataset or "", request_id)

        dest = dest_of(job) or "files/"
        relative = Path(dest)
        if relative.is_absolute() or ".." in relative.parts:
            # A Job that stages outside its own request directory is not one
            # this runner will execute: `dest` is relative by design (§5.7).
            return await _settle_refusal(
                store, request_id, owner, "failed",
                {"error": "job_invalid", "detail": f"dest {dest!r} is not relative"},
                now=now,
            )
        # Discovery before anything is created: a row with no driver for its
        # vendor must not leave an empty request directory behind, which would
        # read as "an attempt happened and produced nothing" when in fact no
        # attempt could start.
        if driver_factory is not None:
            driver = driver_factory(root, mode)
        else:
            try:
                entries = driver_client.installed(url=driver_url, token=token)
            except driver_client.DriverUnavailable as exc:
                return await _settle_refusal(
                    store, request_id, owner, "failed",
                    {"error": "driver_not_ready", "detail": str(exc)},
                    dest=root, dataset=dataset, mode=mode, now=now,
                )
            artifact = driver_client.driver_for(vendor, artifacts=entries)
            if artifact is None:
                # Named, with what *is* installed: the operator's next move is
                # "install the driver for this vendor", and the container that
                # can do that is not this one.
                available = ", ".join(str(entry) for entry in entries) or "nothing"
                return await _settle_refusal(
                    store, request_id, owner, "failed",
                    {
                        "error": "driver_not_ready",
                        "detail": f"no driver is installed for vendor {vendor!r} "
                        f"(installed: {available})",
                    },
                    dest=root, dataset=dataset, mode=mode, now=now,
                )
            driver = driver_client.RemoteDriver(
                root,
                mode,
                url=driver_url,
                token=token,
                driver=artifact.driver,
                driver_version=artifact.version,
            )
        root.mkdir(parents=True, exist_ok=True)
        # The staging directory exists for the attempt even when the vendor
        # publishes nothing: `absent` is a file fact, not a missing directory.
        (root / relative).mkdir(parents=True, exist_ok=True)

        # The flow is blocking: `interpreter.run` sits inside the driver's HTTP
        # session for as long as the vendor takes -- 120 polls of 30 s is two
        # hours of wall clock, none of it ours. In a thread, so the loop that
        # serves the page (and can still take a cancel) keeps running; with a
        # heartbeat beside it, so the lease is renewed while the work outlives
        # it. Without the heartbeat a healthy run is reclaimed at 900 s and the
        # vendor is asked twice (§5.2).
        lease = LEASE_SECONDS if lease_seconds is None else lease_seconds
        stop = asyncio.Event()
        lost = asyncio.Event()
        beat = asyncio.create_task(
            keep_lease(
                store,
                request_id,
                owner,
                lease_seconds=lease,
                stop=stop,
                lost=lost,
            ),
            name=f"lease:{request_id}",
        )
        try:
            verdict = await asyncio.to_thread(interpreter.run, job, driver)
        except interpreter.InterpreterError as exc:
            return await _settle_refusal(
                store, request_id, owner, "failed",
                {"error": "interpreter_error", "detail": str(exc)},
                dest=root, dataset=dataset, mode=mode, now=now,
            )
        finally:
            stop.set()
            # Not cancelled: the heartbeat may be mid-statement, and a cancelled
            # task would leave the write lock to be released by the unwinder.
            await beat
            # A session is a process in the other container. Nothing else closes
            # it, and a leaked one holds a child until its deadline.
            close = getattr(driver, "close", None)
            if callable(close):
                close()

        # Hashed off the loop too: this reads every landed byte, and a landing
        # is the one thing here that is as large as the download.
        files = await asyncio.to_thread(landed_files, root, dest)
        state = state_for(verdict.disposition)
        digest = bundle_digest(files) if state != "failed" else None
        errors = None if state == "succeeded" else dict(verdict.detail)

        _write_landing(
            root,
            {
                "request_id": request_id,
                "job_hash": row.get("job_hash"),
                "owner": owner,
                "state": state,
                "verdict": verdict.verdict,
                "disposition": verdict.disposition,
                "driver": driver_name(driver),
                "driver_version": driver_version(driver),
                "driver_mode": mode,
                "dest": str(root / dest),
                "files": list(files),
                "landed_sha256": digest,
                "interpreter_version": verdict.interpreter_version,
                "runner_version": RUNNER_VERSION,
                "settled_at": now or utcnow(),
            },
        )

        after = await store.settle(
            request_id,
            owner,
            state,
            landed_sha256=digest,
            errors=errors,
            detail={
                "verdict": verdict.verdict,
                "disposition": verdict.disposition,
                "dest": str(root / dest),
                "files": len(files),
                "bytes": sum(int(f["bytes"]) for f in files),
                "driver": driver_name(driver),
                "driver_version": driver_version(driver),
                # Only when it happened: the row was reclaimed while this
                # attempt was still inside the vendor call, so this settle will
                # lose its CAS and these bytes are an orphan the next claimant
                # overwrites. Says so instead of leaving a mystery.
                **({"lease_lost": True} if lost.is_set() else {}),
            },
            now=now,
        )
        return RunResult(
            request_id=request_id,
            state=state,
            verdict=verdict.verdict,
            disposition=verdict.disposition,
            owner=owner,
            dest=root / dest,
            files=files,
            total_bytes=sum(int(f["bytes"]) for f in files),
            landed_sha256=digest,
            errors=errors,
            settled=bool(after.get("settled")),
            steps=verdict.steps,
        )
    except Exception:
        # A crash after the claim but before the settle would hold the lease for
        # 15 minutes on a request nobody is working on. Give it back — best
        # effort, and only if it is still ours — then let the caller see why.
        await store.release(request_id, owner, reason="runner_crashed")
        raise


async def _settle_refusal(
    store: Store,
    request_id: str,
    owner: str,
    state: str,
    errors: dict[str, Any],
    *,
    dest: Path | None = None,
    dataset: str | None = None,
    mode: str | None = None,
    now: str | None = None,
) -> RunResult:
    after = await store.settle(
        request_id, owner, state, errors=errors, detail={"reason": errors.get("error")}, now=now
    )
    return RunResult(
        request_id=request_id,
        state=state,
        verdict=None,
        disposition=None,
        owner=owner,
        dest=dest,
        errors=errors,
        settled=bool(after.get("settled")),
    )


def _write_landing(root: Path, payload: dict[str, Any]) -> None:
    """Provenance beside `files/`, never inside it.

    Inside `dest` it would be staged, not in the vendor manifest, and `classify`
    would call a correct landing a contradiction — the same trap `convert`
    avoids. Written even when the settle then loses its CAS: bytes on disk with
    no record are worse than bytes with an honest record naming the attempt.
    """
    (Path(root) / LANDING_FILE).write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# -- the command line -----------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    datasets_dir = args.datasets_dir or settings.datasets_dir
    registry = load_registry(settings.plan_types_dir or find_plan_types_dir())
    store = Store(args.db or settings.db_path)
    await store.connect()
    try:
        if not len(registry):
            # Every landing path starts with the vendor, and the vendor is the
            # plan type's declaration. A runner with no registry would have to
            # invent one; refuse before claiming anything.
            print(
                "error: no plan types are loaded, so no row has a vendor to file "
                "under; is DATA_DOWNLOAD_PLAN_TYPES_DIR right?",
                file=sys.stderr,
            )
            return 2
        ids = [args.request_id] if args.request_id else await _approved_ids(store)
        if not ids:
            print("nothing to do: no approved request is waiting for a runner")
            return 0
        for request_id in ids:
            try:
                result = await run_one(
                    request_id,
                    store=store,
                    datasets_dir=datasets_dir,
                    owner=args.owner,
                    mode=args.mode,
                    lease_seconds=args.lease_seconds,
                    driver_url=settings.driver_runtime_url,
                    driver_token=settings.driver_runtime_token,
                    registry=registry,
                )
            except ClaimRefused as exc:
                if args.request_id:
                    print(f"refused: {exc.as_text()}", file=sys.stderr)
                    return 1 if exc.reason != "not_found" else 2
                continue  # `--next`: another runner won this row; try the next
            except RunRefused as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 2
            if result.refused == "driver_missing":
                # Not a failure: the row is still `approved` and the loop has
                # been asked for a driver. Printing it as a state would claim
                # something happened to the request that did not.
                detail = (result.errors or {}).get("detail", "")
                print(f"{result.request_id}: waiting for a driver -- {detail}")
                return 0
            print(
                f"{result.request_id}: {result.state} ({result.verdict or 'no verdict'}, "
                f"{len(result.files)} file(s), {result.total_bytes} bytes) "
                f"owner={result.owner}"
            )
            if result.dest:
                print(f"  dest: {result.dest}")
            if result.landed_sha256:
                print(f"  landed_sha256: {result.landed_sha256}")
            if not result.settled:
                print(
                    "  note: the lease was reclaimed before this run settled; "
                    "the row belongs to another attempt",
                    file=sys.stderr,
                )
                return 1
            return 0 if result.state != "failed" else 1
        print("no approved request could be claimed")
        return 0
    finally:
        await store.close()


async def _approved_ids(store: Store) -> list[str]:
    # Oldest first: a queue, not a stack. The store answers in queue order
    # rather than the page's newest-first, so `--next` and the worker take the
    # same row when a backlog is larger than one page.
    return await store.approved_ids(limit=100)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_download.runner",
        description=(
            "Claim one approved request, run its Job through the driver runtime, "
            "land the bytes under the datasets directory, and settle the row."
        ),
    )
    parser.add_argument("request_id", nargs="?", default=None)
    parser.add_argument(
        "--next",
        action="store_true",
        help="run the oldest approved request instead of a named one",
    )
    parser.add_argument("--owner", default=None, help="the claimant (default: host:pid:uuid)")
    parser.add_argument("--lease-seconds", type=int, default=None)
    parser.add_argument(
        "--mode",
        choices=("clean", "short", "nothing"),
        default="clean",
        help="driver option passed to the runtime: how the stub should fail",
    )
    parser.add_argument(
        "--datasets-dir",
        default=None,
        help="where datasets/ lives (default: $DATA_DOWNLOAD_DATASETS_DIR)",
    )
    parser.add_argument("--db", default=None, help="the store (default: $DATA_DOWNLOAD_DB)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.request_id and not args.next:
        print("error: name a request id or pass --next", file=sys.stderr)
        return 2
    if args.request_id and args.next:
        print("error: --next and a request id are mutually exclusive", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
