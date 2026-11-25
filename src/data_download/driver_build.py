"""The driver loop: an open `driver_jobs` row in, an installed artefact out.

Phase 5.5's second half. The runner refuses a request whose vendor has no
driver and leaves a job in `driver_jobs`; this module is what works that queue.
It is **run to exit** -- `python -m data_download.driver_build --once` takes one
job, walks it as far as it can, and returns. Nothing here idles.

The states are the phase order's, and each transition is a step that can refuse:

    queued -> building -> gated -> probing -> merging -> installing -> installed
                    \\-> needs_human   (a stop, never a retry loop)

Five properties worth stating, because each is a thing this is *not*:

1. **It does not write the driver.** `author` is a callable, and the default one
   hands the ticket to an agent -- an OpenHands conversation in this deployment,
   a command on stdin if `--author-cmd` names one. Either way what comes back is
   a branch name and the sha the *forge* reports for it, so the pipeline never
   acquires an opinion about which agent wrote the code.
2. **It does not trust its own gate.** `gate` asks GitHub for the commit's check
   runs and refuses on anything not green. The probe after it is the part CI
   cannot give: a real vendor call.
3. **The probe is the triggering request, shrunk.** One day, one symbol, from
   the very Job that is waiting -- no second vocabulary for "a small run", and
   no invented symbols for a vendor the loop has never driven.
4. **It does not merge what it was not allowed to merge.** Every changed path
   must be under `drivers/` or `tests/fixtures/`; one file outside and the job
   stops for a human. That is checked before the merge, from GitHub's own list.
5. **It does not write the drivers volume.** That volume belongs to the
   container that runs the code, and this app mounts it read-only. Installation
   is a POST of the merged tree to the runtime (`install_over_runtime`), which
   writes it atomically and refuses a version it did not install. The app keeps
   the digest the runtime answered with, which is what lets a retry replace its
   own previous attempt and nothing else.

This is worked by `DriverWorker` on the app's event loop (the same shape as
`worker.Worker`), or one job at a time from the command line with `--once`.
The probe therefore runs after the install -- the runtime can only drive an
artefact it can discover -- which costs nothing now that discovery re-globs on
every request instead of being fixed at startup.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import driver_author, driver_client, github_api, interpreter, runtime_http
from .config import find_plan_types_dir, load_settings
from .db import Store
from .registry import Registry, load_registry

log = logging.getLogger(__name__)

#: How many authoring turns before a human is asked. The design's rule is
#: "fail, record, stop" -- three attempts, then a stop that says why.
MAX_ATTEMPTS = 3

#: What a probe may spend. It is a run of a real Job, so it is not free; it is
#: also not the caller's ceiling, because the driver is being built before the
#: request it serves can be priced honestly.
PROBE_BUDGET_USD = 1.0

#: Where the loop may write, in the driver repo. Everything else is a human's.
ALLOWED_PREFIXES = ("drivers/", "tests/fixtures/")


class LoopRefused(Exception):
    """A step said no. The job stops at `needs_human` with this sentence."""


@dataclass
class Steps:
    """The loop's five decisions, as callables, so a test can be one of them.

    Every callable takes the job row (a dict from `driver_jobs`) and returns
    what the next state needs. The defaults are the real ones; `--author-cmd`
    replaces exactly one of them.
    """

    author: Callable[[dict[str, Any]], dict[str, Any]]
    gate: Callable[[dict[str, Any]], dict[str, Any]]
    changed: Callable[[dict[str, Any]], list[str]]
    probe: Callable[[dict[str, Any]], dict[str, Any]]
    merge: Callable[[dict[str, Any]], dict[str, Any]]
    install: Callable[[dict[str, Any]], dict[str, Any]]


# -- the default install -----------------------------------------------------


def artifact_digest(directory: Path) -> str:
    """One digest over a directory's files, in a stable order.

    The unit of installation is the directory, so the digest is over names as
    well as bytes: two artefacts with the same code under different names are
    different installations, and a digest that ignored the name would say they
    were the same.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(directory)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def install_over_runtime(
    source: Path,
    *,
    url: str | None,
    token: str | None,
    replace_sha256: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """`source` (a directory holding `manifest.json`) into the drivers volume.

    The volume belongs to the **runtime**, not to this process: the container
    that runs model-authored code is the container that owns the directory it
    runs from, and this app's mount is read-only. So the tree crosses the
    boundary as data -- `path -> base64` in one POST -- and the runtime does the
    writing (`data-download-drivers`' `install.py`), including the atomic rename
    and the refusal to overwrite an install it did not make.

    `replace_sha256` is the digest of the install this job put there last time.
    A probe that came back `shortfall` sends the job round again and the second
    turn's artefact is the same name and version; passing the digest the runtime
    gave us then is how the loop may replace exactly its own work and nothing
    else. A directory a person installed has a digest this job has never
    recorded, and stays refused.

    The digest the runtime answers with is checked against the tree that was
    sent, so "installed" means the merged bytes, not the bytes some layer of
    JSON encoding produced.
    """
    manifest_path = source / "manifest.json"
    if not manifest_path.is_file():
        raise LoopRefused(f"{source} has no manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise LoopRefused(f"{manifest_path} is not JSON: {exc}") from None
    name = str(manifest.get("name") or "").strip()
    version = str(manifest.get("version") or "").strip()
    vendor = str(manifest.get("vendor") or "").strip()
    if not name or not version:
        raise LoopRefused("the manifest must name a driver and a version")

    files = {
        path.relative_to(source).as_posix(): base64.b64encode(path.read_bytes()).decode()
        for path in sorted(p for p in source.rglob("*") if p.is_file())
    }
    if not files:
        raise LoopRefused(f"{source} is empty")

    host, port = driver_client._endpoint(url)
    try:
        status, answer = runtime_http.request(
            host,
            port,
            "POST",
            "/v1/drivers/install",
            token=token or "",
            body={
                "name": name,
                "version": version,
                "vendor": vendor,
                "files": files,
                "replace_sha256": replace_sha256 or None,
            },
            timeout=timeout,
        )
    except runtime_http.RuntimeUnreachable as exc:
        raise LoopRefused(f"the driver runtime could not be reached: {exc}") from None

    if not 200 <= status < 300:
        error = answer.get("error") if isinstance(answer, dict) else {}
        code = (error or {}).get("code") or "unknown"
        detail = (error or {}).get("detail") or ""
        raise LoopRefused(
            f"the runtime refused the install: {status} {code}"
            + (f" ({detail})" if detail else "")
        )

    installed = str(answer.get("sha256") or "")
    sent = artifact_digest(source)
    if installed != sent:
        # Not fatal to the runtime, fatal to trust: the artefact on disk is not
        # the artefact that passed CI, and every later "replace my own install"
        # would be judged against a digest this job never recorded.
        raise LoopRefused(
            f"the runtime installed digest {installed or '(none)'} but the tree "
            f"sent has {sent}; refusing to record an install it cannot own"
        )
    return {
        "driver": name,
        "version": version,
        "vendor": vendor,
        "artifact_sha256": installed,
        "path": str(answer.get("path") or ""),
    }


def driver_in_tree(root: Path, vendor: str) -> Path | None:
    """The highest-versioned `drivers/*/*/` in a checkout whose manifest is `vendor`."""
    wanted = " ".join(vendor.split()).lower()
    best: tuple[tuple[Any, ...], Path] | None = None
    for manifest_path in sorted(root.glob("drivers/*/*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if " ".join(str(manifest.get("vendor") or "").split()).lower() != wanted:
            continue
        key = driver_client._version_key(str(manifest.get("version") or "0"))
        if best is None or key > best[0]:
            best = (key, manifest_path.parent)
    return best[1] if best else None


# -- the probe ---------------------------------------------------------------


def probe_job(job_text: str, *, days: int = 1) -> dict[str, Any]:
    """The triggering Job, shrunk to one symbol and one day.

    Shrunk rather than rebuilt: the request's own binder already chose the
    vendor's vocabulary (symbology, schema, split), and inventing a second
    "small run" here would be a second set of rules for what a valid Job is.
    """
    job = json.loads(job_text)
    steps = []
    first_start: date | None = None
    for step in job.get("steps") or []:
        kwargs = dict(step.get("kwargs") or {})
        if kwargs.get("symbols"):
            kwargs["symbols"] = list(kwargs["symbols"])[:1]
        if kwargs.get("start"):
            # The start is kept: the probe asks for real data on a real day, so
            # it must not be moved into the future by the shrink. The end is
            # what moves; the window stays half-open, one day long.
            if first_start is None:
                first_start = _as_date(kwargs["start"])
            if first_start is not None:
                kwargs["start"] = first_start.isoformat()
        if kwargs.get("end") and first_start is not None:
            kwargs["end"] = (first_start + timedelta(days=days)).isoformat()
        steps.append({"kind": step["kind"], "kwargs": kwargs})
    return {**job, "steps": steps}


def _as_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def probe_root(job_id: str, runtime_root: str = "/datasets") -> Path:
    """Where the probe's bytes go **in the driver runtime's** view of the world.

    Not the loop's own datasets directory. The loop may run anywhere -- on ryzen
    it is a host process, so its datasets path is `/srv/datasets` -- while the
    runtime is a container that knows only `/datasets`, and it validates every
    root against its own mount. Handing it the host path earns `400 bad_request:
    root '/srv/datasets/_probe/...' is outside /datasets`, which reads like a
    pricing failure and is a path translation.

    So the split is explicit: the loop names where the *runtime* should write,
    and `DATA_DOWNLOAD_DRIVER_DATASETS_ROOT` is how a deployment whose mount
    differs says so. The loop deletes nothing it did not create -- the probe's
    own directory is the runtime's to clean.
    """
    return Path(runtime_root) / "_probe" / job_id


def run_probe(
    row: dict[str, Any],
    artifact: Any,
    *,
    runtime_root: str = "/datasets",
    driver_url: str | None,
    driver_token: str | None,
    registry: Registry,
    budget: float = PROBE_BUDGET_USD,
) -> dict[str, Any]:
    """One real run of the waiting request, one day, one symbol.

    The quote is compared to the probe's budget **before** `interpreter.run`
    reaches `submit`, because the budget exists to bound a spend the loop
    decides on, not to police the caller's ceiling.
    """
    # `driver_jobs` carries the vendor and the request id; the Job itself is on
    # the request, and `Store.driver_job_*` joins it in as `request_job_json`.
    source = row.get("request_job_json") or row.get("job_json") or ""
    if not source:
        raise LoopRefused("no bound Job on the waiting request, so there is nothing to probe")
    job = probe_job(source)
    root = probe_root(str(row.get("id") or "probe"), runtime_root)
    driver = driver_client.RemoteDriver(
        root,
        "clean",
        url=driver_url,
        token=driver_token,
        driver=artifact.driver,
        driver_version=artifact.version,
    )
    quote_step = next(
        (step for step in job["steps"] if step["kind"] == "quote"), None
    )
    if quote_step is not None:
        try:
            usd = float(driver.cost(**dict(quote_step.get("kwargs") or {})) or 0.0)
        except Exception as exc:  # noqa: BLE001 - the sentence is the result
            raise LoopRefused(f"the probe could not be priced: {exc}") from None
        if usd > budget:
            raise LoopRefused(
                f"the probe would cost ${usd:.2f}, above its ${budget:.2f} budget"
            )
    try:
        verdict = interpreter.run(job, driver)
    except interpreter.InterpreterError as exc:
        raise LoopRefused(f"the probe could not produce a verdict: {exc}") from None
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return verdict.as_dict()


# -- the state machine -------------------------------------------------------


def _ticket(row: dict[str, Any]) -> str:
    """What the harness is told. Plain text, not JSON: a prompt, not a schema."""
    try:
        plan_types = json.loads(row.get("plan_types_json") or "[]")
    except ValueError:
        plan_types = []
    return "\n".join(
        [
            f"Write a driver for the vendor {row['vendor']!r}.",
            "",
            f"Plan types waiting on it: {', '.join(plan_types) or '(none recorded)'}",
            f"The first request waiting is for dataset {row.get('dataset') or '(unknown)'}.",
            "",
            "The driver is a directory drivers/<name>/<version>/ containing manifest.json",
            "and the entrypoint the runtime spawns. It must answer every verb in the",
            "protocol: describe, resolve, cost, submit, poll, list_files, fetch, convert,",
            "verify. Look at drivers/stub/ and drivers/databento/ for the shape.",
            "Add tests under tests/fixtures/ or beside the driver. Do not modify",
            "src/ or tests/ outside tests/fixtures/.",
        ]
    )


def _author_command(cmd: str, *, cwd: Path | None = None) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """The harness seam: a ticket on stdin, a branch and a sha on stdout."""

    def author(row: dict[str, Any]) -> dict[str, Any]:
        try:
            finished = subprocess.run(  # noqa: S603 - an operator-supplied command
                cmd,
                shell=True,
                input=_ticket(row),
                capture_output=True,
                text=True,
                cwd=str(cwd) if cwd else None,
                timeout=3600,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise LoopRefused("the authoring harness did not finish within an hour") from None
        if finished.returncode != 0:
            detail = (finished.stderr or finished.stdout or "").strip()[-800:]
            raise LoopRefused(
                f"the authoring harness exited {finished.returncode}: {detail}"
            )
        try:
            answer = json.loads(finished.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise LoopRefused(
                "the authoring harness did not answer with a JSON line "
                '{"branch": ..., "commit_sha": ...}'
            ) from None
        if not answer.get("branch") or not answer.get("commit_sha"):
            raise LoopRefused("the authoring harness answered without a branch and sha")
        return answer

    return author


def default_steps(
    *,
    repo: str,
    settings: Any,
    registry: Registry,
    token: str,
    workdir: Path,
    base: str = "main",
    author_cmd: str = "",
    author_timeout: float = 3600.0,
    author_budget: float = 2.0,
    author_iterations: int = 200,
) -> Steps:
    """The real pipeline, wired to GitHub, the agent server and the runtime.

    Every blocking call here is made from a worker thread (see `_step`), because
    the queue is worked on the app's own event loop: an authoring conversation
    can run for tens of minutes, and the web server must keep answering while it
    does.
    """

    def author(row: dict[str, Any]) -> dict[str, Any]:
        try:
            return driver_author.author(
                _ticket(row),
                repo=repo,
                base=base,
                token=token,
                timeout=author_timeout,
                workdir_prefix=str(
                    settings.openhands_workdir or driver_author.DEFAULT_WORKDIR
                ),
                container=driver_author.DEFAULT_CONTAINER,
                projects_dir=str(settings.openhands_projects_dir or ""),
                owner=(settings.openhands_agent_uid, settings.openhands_agent_gid),
                agent_cmd=author_cmd,
                url=str(settings.openhands_url or ""),
                budget=author_budget,
                iterations=author_iterations,
                say=lambda line: log.info("driver job %s: %s", row.get("id"), line),
            )
        except driver_author.Misconfigured as exc:
            # Nothing to retry: the deployment is missing something. It still
            # stops the job (there is no state for "the loop is broken"), but it
            # reads as what it is rather than as a refused driver.
            raise LoopRefused(f"misconfigured: {exc}") from None
        except driver_author.Refused as exc:
            raise LoopRefused(str(exc)) from None

    def gate(row: dict[str, Any]) -> dict[str, Any]:
        return github_api.gate(repo, str(row["commit_sha"]), token=token)

    def changed(row: dict[str, Any]) -> list[str]:
        return github_api.changed_paths(repo, str(row["commit_sha"]), token=token)

    def probe(row: dict[str, Any]) -> dict[str, Any]:
        artifact = driver_client.driver_for(
            str(row["vendor"]),
            url=settings.driver_runtime_url,
            token=settings.driver_runtime_token,
        )
        if artifact is None:
            raise LoopRefused(
                f"no artefact for {row['vendor']!r} is visible to the runtime after install"
            )
        return run_probe(
            row,
            artifact,
            runtime_root=settings.driver_datasets_root or "/datasets",
            driver_url=settings.driver_runtime_url,
            driver_token=settings.driver_runtime_token,
            registry=registry,
        )

    def merge(row: dict[str, Any]) -> dict[str, Any]:
        return github_api.merge(repo, str(row["branch"]), token=token)

    def install(row: dict[str, Any]) -> dict[str, Any]:
        data = github_api.tarball(repo, str(row["commit_sha"]), token=token)
        with tempfile.TemporaryDirectory(dir=workdir) as tmp:
            root = Path(tmp)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
                archive.extractall(root, filter="data")  # noqa: S202 - a tarball we asked for
            checkout = next((p for p in root.iterdir() if p.is_dir()), root)
            source = driver_in_tree(checkout, str(row["vendor"]))
            if source is None:
                raise LoopRefused(
                    f"the merged tree has no driver declaring vendor {row['vendor']!r}"
                )
            return install_over_runtime(
                source,
                url=settings.driver_runtime_url,
                token=settings.driver_runtime_token,
                replace_sha256=row.get("artifact_sha256"),
            )

    return Steps(
        author=author,
        gate=gate,
        changed=changed,
        probe=probe,
        merge=merge,
        install=install,
    )


def steps_from_settings(
    settings: Any, *, registry: Registry, workdir: Path | None = None
) -> Steps:
    """The pipeline for a deployment that has everything the settings name."""
    if not settings.driver_ready:
        raise LoopRefused("DRIVER_RUNTIME_TOKEN is unset, so nothing can be installed or probed")
    if not settings.github_token:
        raise LoopRefused("DATA_DOWNLOAD_GITHUB_TOKEN is unset, so the forge cannot be asked anything")
    if not settings.openhands_url:
        raise LoopRefused("DATA_DOWNLOAD_OPENHANDS_URL is unset, so no driver can be authored")
    return default_steps(
        repo=settings.driver_repo,
        base=settings.driver_base,
        settings=settings,
        registry=registry,
        token=settings.github_token,
        workdir=workdir or Path(tempfile.gettempdir()),
        author_timeout=settings.author_timeout,
        author_budget=settings.author_budget,
        author_iterations=settings.author_iterations,
    )


async def _step(step: Callable[[dict[str, Any]], Any], row: dict[str, Any]) -> Any:
    """Run a step off the event loop. Every step here is network-bound blocking
    code -- a vendor call, a forge call, a conversation that runs for minutes --
    so awaiting it on the loop would stop the app serving pages for the
    duration."""
    return await asyncio.to_thread(step, row)


async def run_once(
    store: Store,
    steps: Steps,
    *,
    job_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Take one job as far as it goes. `None` when there is nothing to do.

    A job in `gated` is *waiting on CI*, not finished with: it is picked up
    again and re-checked from the same branch and sha, without another authoring
    turn. That is the difference between a push and a build -- the gate is red,
    green, or **not yet**, and only the first of those is a human's.

    A stop leaves the job in `needs_human` with `last_error`, and says so on the
    request too: `driver_state` exists so the row can be honest about *why* it
    is still `approved`, and a stale "building" after the loop gave up would be
    the page lying about the one thing it is there to explain.
    """
    row = await store.driver_job_get(job_id) if job_id else await store.driver_job_next()
    if row is None or (job_id and row["state"] not in ("queued", "building", "gated")):
        return None
    waiting_on_ci = row["state"] == "gated"
    if waiting_on_ci:
        # A CI wait is not an authoring attempt: counting it would exhaust the
        # three tries on a slow pipeline rather than on a wrong driver.
        attempts = int(row.get("attempts") or 0)
    else:
        attempts = int(row.get("attempts") or 0) + 1
        row = await store.driver_job_update(
            row["id"], state="building", attempts=attempts, now=now
        ) or row
    try:
        if not row.get("branch"):
            authored = await _step(steps.author, row)
            row = await store.driver_job_update(
                row["id"],
                branch=str(authored["branch"]),
                commit_sha=str(authored["commit_sha"]),
                # Provenance, not a decision: the harness may be a plain shell
                # agent with no transcript, and the loop must not care.
                author_ref=str(authored.get("author_ref") or "") or None,
                now=now,
            ) or row
        gate = await _step(steps.gate, row)
        if not gate.get("ok"):
            if gate.get("pending"):
                # Not yet. Leave it in `gated` and let the next pass look again.
                row = await store.driver_job_update(
                    row["id"], state="gated", now=now
                ) or row
                return await store.driver_job_get(row["id"])
            raise LoopRefused(f"CI is not green: {gate.get('detail') or 'no green check'}")
        row = await store.driver_job_update(row["id"], state="gated", now=now) or row
        outside = [
            path
            for path in await _step(steps.changed, row)
            if not any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES)
        ]
        if outside:
            raise LoopRefused(
                "the branch touches paths the loop may not merge: " + ", ".join(outside[:5])
            )
        # Installed before the probe: the runtime can only drive an artefact it
        # can discover (see this module's docstring).
        installed = await _step(steps.install, row)
        row = await store.driver_job_update(
            row["id"],
            artifact_sha256=str(installed.get("artifact_sha256") or ""),
            now=now,
        ) or row
        row = await store.driver_job_update(row["id"], state="probing", now=now) or row
        verdict = await _step(steps.probe, row)
        name = str(verdict.get("verdict") or "")
        await store.driver_job_update(
            row["id"], verdict_json=json.dumps(verdict), now=now
        )
        if name == "pass":
            row = await store.driver_job_update(row["id"], state="merging", now=now) or row
            await _step(steps.merge, row)
        elif name == "shortfall" and attempts < MAX_ATTEMPTS:
            # The driver is wrong in a way another turn can fix. Forget the
            # branch so the next attempt starts from a fresh one -- but keep
            # `artifact_sha256`, which is what lets that attempt replace the
            # artefact this one installed instead of tripping over it.
            await store.driver_job_update(
                row["id"], state="queued", branch=None, commit_sha=None, now=now
            )
            return await store.driver_job_get(row["id"])
        else:
            raise LoopRefused(
                f"the probe verdict was {name or 'missing'}"
                + ("" if attempts < MAX_ATTEMPTS else " and the attempts are exhausted")
            )
        row = await store.driver_job_update(row["id"], state="installing", now=now) or row
        return await store.driver_job_update(row["id"], state="installed", now=now)
    except LoopRefused as exc:
        log.warning("driver job %s stopped: %s", row["id"], exc)
        row = await store.driver_job_update(
            row["id"], state="needs_human", last_error=str(exc), now=now
        ) or row
        if row.get("request_id"):
            await store.note_driver_state(
                str(row["request_id"]),
                "needs_human",
                detail={"job": row["id"], "stopped": str(exc)[:400]},
                now=now,
            )
        return row


#: A job in one of these states has handed the work to something outside this
#: process -- CI, or the next attempt -- so the loop waits rather than spins.
WAITING_STATES = frozenset({"queued", "gated"})


class DriverWorker:
    """The driver queue, worked from the app's own event loop.

    Same shape as `worker.Worker`, and for the same reasons: the queue is a
    query on the store this process already has open, a pass is one job, and the
    loop does not sleep while there is work. What a pass *does* is different --
    author a conversation, wait on CI, probe a vendor, install a tree -- so
    every step runs in a worker thread (`_step`) and nothing here blocks the
    loop the web server is using.

    It is nudged by an approval that finds no driver, so the first authoring
    turn starts in milliseconds rather than on the next beat; the interval is
    the idle poll that catches a job queued by something that could not nudge.
    """

    def __init__(self, store: Store, steps: Steps, *, interval: float = 60.0) -> None:
        self.store = store
        self.steps = steps
        self.interval = interval
        self._nudge = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def wake(self) -> None:
        """Somebody queued a job (or thinks they did). Never raises."""
        self._nudge.set()

    async def once(self) -> dict[str, Any] | None:
        """One job, as far as it goes. Exceptions are the loop's, not a pass's."""
        try:
            return await run_once(self.store, self.steps)
        except Exception:  # noqa: BLE001 - the loop outlives a bad job
            log.exception("driver worker: pass failed")
            return None

    async def run_forever(self) -> None:
        log.info("driver worker: watching for queued driver jobs (idle interval %ss)", self.interval)
        while not self._stopping.is_set():
            row = await self.once()
            if row is not None and row.get("state") not in WAITING_STATES:
                # Finished with this one: the next job may already be queued.
                continue
            self._nudge.clear()
            try:
                await asyncio.wait_for(self._nudge.wait(), timeout=self.interval)
            except TimeoutError:
                pass
        log.info("driver worker: stopped")

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self.run_forever(), name="data-download-driver-jobs")

    async def stop(self) -> None:
        """Cancelled, not awaited politely: a pass can be inside a conversation.

        The job it was holding is `building` with no lease of its own, so the
        next pass picks it up from the same row -- the branch it already pushed
        is on the forge, and the gate is re-asked rather than re-authored.
        """
        self._stopping.set()
        self._nudge.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.exception("driver worker: failed on the way down")


def main(argv: list[str] | None = None) -> int:
    """One job from the command line, for a deployment without the worker.

    `--once` takes one job and exits, which is what the old host loop called
    every minute. It is kept because it is the readable way to run a job by hand
    -- and because it is how the same state machine is exercised without an app.
    """
    parser = argparse.ArgumentParser(prog="data_download.driver_build")
    parser.add_argument("--db", default=None)
    parser.add_argument("--once", action="store_true", help="take one job and exit")
    parser.add_argument("--job", default=None, help="a specific driver job id")
    parser.add_argument("--author-cmd", default=os.environ.get("AUTHOR_AGENT_CMD", ""))
    parser.add_argument("--repo", default=os.environ.get("DATA_DOWNLOAD_DRIVER_REPO", ""))
    parser.add_argument("--base", default=os.environ.get("DATA_DOWNLOAD_DRIVER_BASE", "main"))
    parser.add_argument("--workdir", default=os.environ.get("DATA_DOWNLOAD_WORK_DIR", ""))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = load_settings()
    token = (os.environ.get("DRIVER_LOOP_GITHUB_TOKEN") or settings.github_token or "").strip()
    if not token:
        print(
            "no GitHub token: set DATA_DOWNLOAD_GITHUB_TOKEN (or GITHUB_TOKEN)",
            file=sys.stderr,
        )
        return 2
    registry = load_registry(settings.plan_types_dir or find_plan_types_dir())
    repo = args.repo or settings.driver_repo
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.gettempdir())

    async def go() -> int:
        store = Store(args.db or settings.db_path)
        await store.connect()
        try:
            steps = default_steps(
                repo=repo,
                base=args.base,
                settings=settings,
                registry=registry,
                token=token,
                workdir=workdir,
                author_cmd=args.author_cmd,
                author_timeout=settings.author_timeout,
                author_budget=settings.author_budget,
                author_iterations=settings.author_iterations,
            )
            done = await run_once(store, steps, job_id=args.job)
            if done is None:
                print("nothing to do: no queued driver job")
                return 0
            print(
                f"{done['id']}: {done['state']}"
                + (f" -- {done['last_error']}" if done.get("last_error") else "")
            )
            return 0 if done["state"] == "installed" else 1
        finally:
            await store.close()

    return asyncio.run(go())


if __name__ == "__main__":  # pragma: no cover - the module entry point
    raise SystemExit(main())
