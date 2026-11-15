"""The interpreter: a Job in, a verdict out (§3).

It is the highest-blast-radius object in the repo (§6.2), and this phase it is
also the smallest. It walks an ordered Job and calls the **driver** for I/O. It
never imports a vendor SDK, never holds a credential, never prices anything, and
never decides whether a Request succeeded -- it returns a *verdict*, and the
state that verdict maps to belongs to phase 5.

Two rules do most of the work here:

* **The interpreter owns the flow, the driver owns the vendor.** When to poll,
  how many times, and which class a verification outcome is, are shared across
  every Plan. What a vendor's cost call looks like is not.
* **The driver reports evidence, the interpreter classifies it.** `verify`
  returns what the vendor claimed, what the shipped manifest lists, and what is
  actually on disk with its hashes. Deciding that this is a shortfall rather
  than a contradiction is one function, shared, so a Plan cannot invent its own
  vocabulary of failure.

Fail closed: a step that needs evidence it does not have is an error, not a
skip, and not a `None` the next step tolerates.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

# Bumped when behaviour changes. Deliberately NOT part of `job_hash` (§2 rule 4):
# the hash attests to the Job, and which interpreter ran is `code_ref` on the
# Attempt. The version is exposed so a verdict can say which flow produced it.
INTERPRETER_VERSION = "1"

# The verdicts (§3.2). Not states: `succeeded`/`failed` are the Request's, and
# conflating the two is how "a file exists" becomes "integrity-checked".
PASS = "pass"
SHORTFALL = "shortfall"
CONTRADICTION = "contradiction"
VERIFIER_ERROR = "verifier_error"

VERDICTS = (PASS, SHORTFALL, CONTRADICTION, VERIFIER_ERROR)

# What each verdict permits. A shortfall may commit as `landed_with_errors` -- if
# the Plan declared `commit_marked`; a Plan that declared `quarantine` does not
# get to be lenient because the driver said "shortfall".
COMMIT = "commit"
COMMIT_MARKED = "commit_marked"
QUARANTINE = "quarantine"

# Poll states that mean "ask again". Anything else is terminal and the
# interpreter stops; an unknown state is a vendor fact we do not understand, and
# treating it as "done" would download files that are not there.
POLL_PENDING = frozenset({"queued", "submitted", "running", "pending"})
POLL_DONE = "done"

# Bounded, because a loop with no bound is a way to spend an Attempt forever.
MAX_POLLS = 120


class InterpreterError(Exception):
    """A run that cannot produce a verdict: the Job did not get that far.

    Distinct from a failing verdict on purpose. "The vendor job failed" and "the
    bytes disagree with the manifest" are different events, and only the second
    is `verify`'s business.
    """


class Driver(Protocol):
    """The narrow protocol every Plan's driver implements (§3).

    Phase 2 defines it and `dryrun.py` implements it offline. Phase 5 replaces
    that implementation with the real Databento client. The interpreter receives
    one as an argument; it never constructs or imports one.

    `convert` is part of the protocol even though the phase-2 order's list of
    vendor operations omits it: a step kind with no operation behind it is a hole
    in the vocabulary, and local format conversion is still I/O the interpreter
    must not perform itself.
    """

    def cost(self, **kwargs: Any) -> float: ...

    def resolve(self, **kwargs: Any) -> dict[str, Any]: ...

    def submit(self, **kwargs: Any) -> dict[str, Any]: ...

    def poll(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_files(self, **kwargs: Any) -> list[dict[str, Any]]: ...

    def fetch(self, **kwargs: Any) -> list[str]: ...

    def convert(self, **kwargs: Any) -> list[str]: ...

    def verify(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Verdict:
    """What one interpretation of one Job concluded."""

    verdict: str
    disposition: str
    detail: dict[str, Any] = field(default_factory=dict)
    steps: tuple[str, ...] = ()
    interpreter_version: str = INTERPRETER_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "disposition": self.disposition,
            "detail": self.detail,
            "steps": list(self.steps),
            "interpreter_version": self.interpreter_version,
        }


def _disposition_for(
    verdict: str, on_failure: str | None
) -> str:
    if verdict == PASS:
        return COMMIT
    if verdict == SHORTFALL:
        # §3.1: the Plan declares how *this* vendor's verification failures
        # resolve. `commit_marked` is the honest answer for a vendor that
        # legitimately publishes partial days; anything else quarantines.
        return COMMIT_MARKED if on_failure == "commit_marked" else QUARANTINE
    return QUARANTINE


def classify(evidence: Mapping[str, Any] | None, on_failure: str | None = None) -> Verdict:
    """Turn verification evidence into a verdict. The shared half of `verify`.

    Three quantities, and the difference between them is the whole distinction:

    * `manifest` -- what the vendor's own manifest says it published;
    * `staged`   -- what is actually on disk, hashed;
    * `claimed`  -- what the vendor job said it would produce.

    `manifest` vs `staged` is **integrity**: a claim the file does not satisfy is
    a contradiction. `claimed` vs `manifest` is **scope**: the vendor publishing
    less than its job claimed is a shortfall, and withholding it would destroy
    value already paid for. Row counts are compared against the manifest's count,
    never against the Plan's expectation -- comparing to the Plan converts every
    legitimate shortfall into a contradiction.
    """
    if not isinstance(evidence, Mapping):
        return Verdict(
            verdict=VERIFIER_ERROR,
            disposition=QUARANTINE,
            detail={"reason": "the driver returned no evidence"},
        )

    manifest = evidence.get("manifest")
    staged = evidence.get("staged") or {}
    claimed = evidence.get("claimed") or []

    if not isinstance(manifest, list) or not isinstance(staged, Mapping):
        return Verdict(
            verdict=VERIFIER_ERROR,
            disposition=QUARANTINE,
            detail={
                "reason": "unparseable manifest",
                "manifest_type": type(manifest).__name__,
            },
        )

    problems: list[dict[str, Any]] = []
    for entry in manifest:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
            return Verdict(
                verdict=VERIFIER_ERROR,
                disposition=QUARANTINE,
                detail={"reason": "manifest entry is not {name, sha256, rows}"},
            )
        name = entry["name"]
        got = staged.get(name)
        if got is None:
            problems.append({"file": name, "problem": "absent"})
            continue
        if got.get("sha256") != entry.get("sha256"):
            problems.append(
                {
                    "file": name,
                    "problem": "sha256 mismatch",
                    "manifest": entry.get("sha256"),
                    "file_sha256": got.get("sha256"),
                }
            )
        elif entry.get("rows") is not None and got.get("rows") != entry.get("rows"):
            problems.append(
                {
                    "file": name,
                    "problem": "row count mismatch",
                    "manifest": entry.get("rows"),
                    "file_rows": got.get("rows"),
                }
            )

    manifest_names = {str(e.get("name")) for e in manifest}
    for name in sorted(set(staged) - manifest_names):
        problems.append({"file": name, "problem": "not in the manifest"})

    if problems:
        return Verdict(
            verdict=CONTRADICTION,
            disposition=QUARANTINE,
            detail={
                "manifest_files": len(manifest),
                "staged_files": len(staged),
                "problems": problems,
            },
        )

    missing = sorted({str(n) for n in claimed} - manifest_names)
    if missing:
        return Verdict(
            verdict=SHORTFALL,
            disposition=_disposition_for(SHORTFALL, on_failure),
            detail={
                "claimed_files": len(claimed),
                "manifest_files": len(manifest),
                "missing": missing,
            },
        )

    return Verdict(
        verdict=PASS,
        disposition=COMMIT,
        detail={"files": len(manifest), "rows": sum(
            int(e.get("rows") or 0) for e in manifest
        )},
    )


def _poll_until_done(driver: Driver, vendor_job_id: str, kwargs: Mapping[str, Any]) -> str:
    for _ in range(MAX_POLLS):
        answer = driver.poll(vendor_job_id=vendor_job_id, **dict(kwargs))
        state = str((answer or {}).get("state") or "").lower()
        if state == POLL_DONE:
            return state
        if state and state not in POLL_PENDING:
            raise InterpreterError(
                f"vendor job {vendor_job_id} ended in state {state!r}; "
                "the data was never produced, so there is nothing to verify"
            )
    raise InterpreterError(
        f"vendor job {vendor_job_id} did not complete after {MAX_POLLS} polls"
    )


def run(job: Mapping[str, Any], driver: Driver) -> Verdict:
    """Interpret one Job. Never raises for a *verdict*; raises InterpreterError
    when the run cannot get as far as having one."""
    state: dict[str, Any] = {
        "vendor_job_id": None,
        "dest": None,
        "claimed": [],
        "staged": [],
    }
    steps = job.get("steps") or []
    executed: list[str] = []
    on_failure: str | None = None

    for index, step in enumerate(steps):
        kind = step["kind"]
        kwargs = dict(step.get("kwargs") or {})
        executed.append(kind)

        if kind == "resolve":
            state["resolved"] = driver.resolve(**kwargs)
        elif kind == "quote":
            # Recorded, never compared to `estimate_usd` (§5.2).
            state["quote"] = driver.cost(**kwargs)
        elif kind == "submit":
            answer = driver.submit(**kwargs)
            vendor_job_id = (answer or {}).get("vendor_job_id")
            if not vendor_job_id:
                raise InterpreterError(
                    f"steps[{index}] (submit): the driver returned no vendor_job_id"
                )
            state["vendor_job_id"] = vendor_job_id
        elif kind == "poll":
            if not state["vendor_job_id"]:
                raise InterpreterError(
                    f"steps[{index}] (poll): no vendor job has been submitted"
                )
            _poll_until_done(driver, state["vendor_job_id"], kwargs)
        elif kind == "download":
            dest = kwargs.get("dest")
            if not dest:
                raise InterpreterError(
                    f"steps[{index}] (download): dest is required"
                )
            # What the vendor's job says it produced, then what actually
            # arrived. Both are kept: verify needs the difference (§3.2).
            files = driver.list_files(vendor_job_id=state["vendor_job_id"] or "")
            state["claimed"] = [
                f.get("name") for f in files if isinstance(f, Mapping)
            ]
            state["dest"] = dest
            state["staged"] = driver.fetch(
                vendor_job_id=state["vendor_job_id"] or "",
                dest=dest,
                files=files,
            )
        elif kind == "convert":
            if state["dest"] is None:
                raise InterpreterError(
                    f"steps[{index}] (convert): nothing has been downloaded"
                )
            state["converted"] = driver.convert(dest=state["dest"], **kwargs)
        elif kind == "verify":
            on_failure = kwargs.get("on_failure")
            if on_failure not in {"commit_marked", "quarantine"}:
                raise InterpreterError(
                    f"steps[{index}] (verify): on_failure must be "
                    "'commit_marked' or 'quarantine'"
                )
            state["verified"] = True
            evidence = driver.verify(
                dest=state["dest"],
                claimed=state["claimed"],
                staged=state["staged"],
            )
            verdict = classify(evidence, on_failure)
            return Verdict(
                verdict=verdict.verdict,
                disposition=verdict.disposition,
                detail={**verdict.detail, "quote_usd": state.get("quote")},
                steps=tuple(executed),
            )
        else:  # unreachable: job.validate refuses unknown kinds
            raise InterpreterError(f"steps[{index}]: unknown step kind {kind!r}")

    raise InterpreterError(
        "the Job has no verify step, so it cannot produce a verdict; "
        "a download that is never checked is not a landing"
    )
