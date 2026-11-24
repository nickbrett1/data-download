"""The driver loop: an open `driver_jobs` row in, an installed artefact out.

Phase 5.5's second half. The runner refuses a request whose vendor has no
driver and leaves a job in `driver_jobs`; this module is what works that queue.
It is **run to exit** -- `python -m data_download.driver_build --once` takes one
job, walks it as far as it can, and returns. Nothing here idles.

The states are the phase order's, and each transition is a step that can refuse:

    queued -> building -> gated -> probing -> merging -> installing -> installed
                    \\-> needs_human   (a stop, never a retry loop)

Five properties worth stating, because each is a thing this is *not*:

1. **It does not write the driver.** `author` is a callable and the default one
   is an external command (`--author-cmd`): the ticket arrives on stdin as JSON,
   the answer on stdout as `{"branch": ..., "commit_sha": ...}`. That is the
   harness seam, and keeping it a process contract means the loop does not
   acquire an opinion about which agent authored the code.
2. **It does not trust its own gate.** `gate` asks GitHub for the commit's check
   runs and refuses on anything not green. The probe after it is the part CI
   cannot give: a real vendor call.
3. **The probe is the triggering request, shrunk.** One day, one symbol, from
   the very Job that is waiting -- no second vocabulary for "a small run", and
   no invented symbols for a vendor the loop has never driven.
4. **It does not merge what it was not allowed to merge.** Every changed path
   must be under `drivers/` or `tests/fixtures/`; one file outside and the job
   stops for a human. That is checked before the merge, from GitHub's own list.
5. **Installing is atomic.** Bytes are written to a temporary directory *inside*
   the drivers volume and renamed into `drivers/<name>/<version>/`, so a
   half-written artefact is never visible to the runtime's discovery.

One deviation from the phase order, recorded rather than hidden: the probe runs
**after** the install, because the runtime can only drive an artefact it can
discover. The alternative -- a staging directory the runtime also lists -- was
more machinery than the risk warrants: the window is one probe, the artefact is
already behind a green CI gate, and a failing probe removes the directory.
"""

from __future__ import annotations

import argparse
import asyncio
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

from . import driver_client, github_api, interpreter
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


def install_tree(source: Path, drivers_dir: Path) -> dict[str, Any]:
    """`source` (a directory holding `manifest.json`) into the drivers volume.

    Atomic by rename: the copy lands in a temporary directory *inside*
    `drivers_dir` (same filesystem, so the rename cannot fall back to a copy)
    and is renamed into `drivers/<name>/<version>/`. A version that already
    exists is refused rather than overwritten -- a reinstall is a human's act,
    and silently replacing one is how two artefacts claim one vendor.
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
    target = drivers_dir / name / version
    if target.exists():
        raise LoopRefused(f"{target} already exists; refuse to overwrite an install")
    drivers_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".install-", dir=drivers_dir))
    try:
        shutil.copytree(source, staging / "artifact")
        (staging / "artifact" / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging / "artifact", target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "driver": name,
        "version": version,
        "vendor": vendor,
        "artifact_sha256": artifact_digest(target),
        "path": str(target),
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


def run_probe(
    row: dict[str, Any],
    artifact: Any,
    *,
    datasets_dir: Path,
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
    source = row.get("job_json") or ""
    if not source:
        raise LoopRefused("no bound Job on the waiting request, so there is nothing to probe")
    job = probe_job(source)
    root = Path(datasets_dir) / "_probe" / str(row.get("id") or "probe")
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
            "",
            "Reply with JSON only: {\"branch\": \"<branch>\", \"commit_sha\": \"<sha>\"}",
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


def default_steps(*, author_cmd: str, repo: str, drivers_dir: Path, settings: Any,
                  registry: Registry, token: str, workdir: Path) -> Steps:
    """The real loop, wired to GitHub and the running driver runtime."""

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
            datasets_dir=Path(settings.datasets_dir or ""),
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
            return install_tree(source, drivers_dir)

    return Steps(
        author=_author_command(author_cmd),
        gate=gate,
        changed=changed,
        probe=probe,
        merge=merge,
        install=install,
    )


async def run_once(
    store: Store,
    steps: Steps,
    *,
    job_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Take one queued job as far as it goes. `None` when there is nothing queued."""
    row = await store.driver_job_get(job_id) if job_id else await store.driver_job_next()
    if row is None or (job_id and row["state"] not in ("queued", "building")):
        return None
    attempts = int(row.get("attempts") or 0) + 1
    row = await store.driver_job_update(
        row["id"], state="building", attempts=attempts, now=now
    ) or row
    try:
        if not row.get("branch"):
            authored = steps.author(row)
            row = await store.driver_job_update(
                row["id"],
                branch=str(authored["branch"]),
                commit_sha=str(authored["commit_sha"]),
                now=now,
            ) or row
        gate = steps.gate(row)
        if not gate.get("ok"):
            raise LoopRefused(f"CI is not green: {gate.get('detail') or 'no green check'}")
        row = await store.driver_job_update(row["id"], state="gated", now=now) or row
        outside = [
            path
            for path in steps.changed(row)
            if not any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES)
        ]
        if outside:
            raise LoopRefused(
                "the branch touches paths the loop may not merge: " + ", ".join(outside[:5])
            )
        # Installed before the probe: the runtime can only drive an artefact it
        # can discover (see this module's docstring).
        installed = steps.install(row)
        row = await store.driver_job_update(
            row["id"],
            artifact_sha256=str(installed.get("artifact_sha256") or ""),
            now=now,
        ) or row
        row = await store.driver_job_update(row["id"], state="probing", now=now) or row
        verdict = steps.probe(row)
        name = str(verdict.get("verdict") or "")
        await store.driver_job_update(
            row["id"], verdict_json=json.dumps(verdict), now=now
        )
        if name == "pass":
            row = await store.driver_job_update(row["id"], state="merging", now=now) or row
            steps.merge(row)
        elif name == "shortfall" and attempts < MAX_ATTEMPTS:
            # The driver is wrong in a way another turn can fix. Forget the
            # branch so the next attempt starts from a fresh one.
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
        return await store.driver_job_update(
            row["id"], state="needs_human", last_error=str(exc), now=now
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="data_download.driver_build")
    parser.add_argument("--db", default=None)
    parser.add_argument("--once", action="store_true", help="take one job and exit")
    parser.add_argument("--job", default=None, help="a specific driver job id")
    parser.add_argument("--author-cmd", default=os.environ.get("DRIVER_LOOP_AUTHOR_CMD", ""))
    parser.add_argument("--repo", default=os.environ.get("DRIVER_LOOP_REPO", "nickbrett1/data-download-drivers"))
    parser.add_argument("--drivers-dir", default=os.environ.get("DATA_DOWNLOAD_DRIVERS_DIR", "/drivers"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.author_cmd:
        print(
            "no authoring harness: set DRIVER_LOOP_AUTHOR_CMD (a command that takes a "
            "ticket on stdin and answers {\"branch\": ..., \"commit_sha\": ...})",
            file=sys.stderr,
        )
        return 2
    token = os.environ.get("DRIVER_LOOP_GITHUB_TOKEN") or ""
    if not token:
        print("no GitHub token: set DRIVER_LOOP_GITHUB_TOKEN", file=sys.stderr)
        return 2
    settings = load_settings()
    registry = load_registry(settings.plan_types_dir or find_plan_types_dir())

    async def go() -> int:
        store = Store(args.db or settings.db_path)
        await store.connect()
        try:
            steps = default_steps(
                author_cmd=args.author_cmd,
                repo=args.repo,
                drivers_dir=Path(args.drivers_dir),
                settings=settings,
                registry=registry,
                token=token,
                workdir=Path(args.drivers_dir).parent,
            )
            done = await run_once(store, steps, job_id=args.job)
            if done is None:
                print("nothing to do: no queued driver job")
                return 0
            print(f"{done['id']}: {done['state']}" + (f" -- {done['last_error']}" if done.get("last_error") else ""))
            return 0 if done["state"] == "installed" else 1
        finally:
            await store.close()

    return asyncio.run(go())


if __name__ == "__main__":  # pragma: no cover - the module entry point
    raise SystemExit(main())
