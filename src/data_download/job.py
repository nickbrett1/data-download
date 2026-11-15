"""The Job: what a Plan binds a Request's params into (§2 of the phase-2 order).

A Job is **data**, not code: an ordered list of steps, each a closed *kind* plus
kwargs, canonicalised to exactly one byte sequence and hashed. Two properties do
all the work:

* **The vocabulary is closed.** A kind outside {quote, resolve, submit, poll,
  download, convert, verify} is a typed refusal naming the step index and the
  kind -- not a warning, not a skip. A vocabulary that tolerates what it does not
  recognise is not closed.
* **There is exactly one byte sequence per Job.** `job_hash` is only meaningful
  if canonicalisation is single-valued: sorted keys, fixed indent, one trailing
  newline, produced by one function used everywhere. No reader may
  re-canonicalise before comparing.

What a Job deliberately does **not** carry: a module path, an entrypoint, a URL
or an import. It names its Plan by *name*, resolved through the registry. A code
pointer would be an injection surface, would make a Job bound in one checkout
unexecutable in another, and would make `job_hash` attest to a pointer whose
target can change. Which code ran is `code_ref` on the Attempt (phase 3).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import errors

# §3: the closed set. Vendor detail rides in `kwargs`; the kind is the
# abstraction. If a second source needs a new KIND rather than new kwargs, the
# abstraction is wrong and that is a design finding, not a new member here.
STEP_KINDS = (
    "quote",
    "resolve",
    "submit",
    "poll",
    "download",
    "convert",
    "verify",
)
STEP_KIND_SET = frozenset(STEP_KINDS)

# Order, where order is a fact rather than a preference (§3). Deliberately
# minimal: only sequences that are nonsense if reversed are encoded. `quote`
# costing nothing before a `submit`, and `download` needing a `submit` at all,
# are per-Plan choices -- a streamed range fetch has no vendor job to submit.
REQUIRES_BEFORE: dict[str, frozenset[str]] = {
    "poll": frozenset({"submit"}),
    "convert": frozenset({"download"}),
    "verify": frozenset({"download"}),
}

# Kinds that may appear at most once. `verify` is the gate: two verifiers
# disagreeing about the same bytes is a question the verdict cannot answer.
AT_MOST_ONCE = frozenset({"submit", "verify"})

# A Plan is named, never pathed (§2 rule 3). Checked rather than trusted.
_PLAN_NAME_FORBIDDEN = ("/", "\\", ":", " ")


class JobInvalid(Exception):
    """A Job the gate refuses, with the step index and kind when there is one.

    Carries its own payload so a test can assert on the *index and kind* -- the
    point of the refusal is that it names what is wrong, not that it happened.
    """

    def __init__(
        self,
        message: str,
        *,
        step_index: int | None = None,
        kind: str | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.step_index = step_index
        self.kind = kind
        self.field = field

    def extra(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.step_index is not None:
            out["step_index"] = self.step_index
        if self.kind is not None:
            out["kind"] = self.kind
        if self.field is not None:
            out["field"] = self.field
        return out

    def as_api_error(self) -> errors.ApiError:
        return errors.plan_not_valid(self.message, **self.extra())


def canonical(value: Any) -> str:
    """The one byte sequence that represents a Job (§2 rule 1).

    `indent=2` because a Job is read by humans in a diff and on a detail page,
    and a hash does not care. Sorted keys because dict order is an accident of
    construction. One trailing newline so the stored text is a well-formed file.
    """
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def job_hash(job_bytes: str) -> str:
    """`job_hash` is derived from the stored bytes, never authored (§4.2).

    Takes the **text**, not the object: hashing what will be stored is what
    makes the hash an attestation rather than a second opinion.
    """
    return hashlib.sha256(job_bytes.encode("utf-8")).hexdigest()


def _check_plan_name(job: dict[str, Any]) -> None:
    plan = job.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        raise JobInvalid("job.plan must be a non-empty string", field="plan")
    if any(ch in plan for ch in _PLAN_NAME_FORBIDDEN) or plan.endswith(".py"):
        raise JobInvalid(
            f"job.plan {plan!r} must be a Plan name, never a path or a module: "
            "a Job does not point at code (§2 rule 3)",
            field="plan",
        )


def _check_step(index: int, step: Any) -> None:
    if not isinstance(step, dict):
        raise JobInvalid(
            f"steps[{index}]: a step must be an object with kind and kwargs",
            step_index=index,
        )
    unknown = set(step) - {"kind", "kwargs"}
    if unknown:
        raise JobInvalid(
            f"steps[{index}]: unknown step key(s) {sorted(unknown)}; "
            "a step carries kind and kwargs and nothing else",
            step_index=index,
        )
    kind = step.get("kind")
    if not isinstance(kind, str):
        raise JobInvalid(f"steps[{index}]: kind must be a string", step_index=index)
    if kind not in STEP_KIND_SET:
        raise JobInvalid(
            f"steps[{index}]: unknown step kind {kind!r}; the vocabulary is "
            f"closed: {', '.join(STEP_KINDS)}",
            step_index=index,
            kind=kind,
        )
    kwargs = step.get("kwargs", {})
    if not isinstance(kwargs, dict):
        raise JobInvalid(
            f"steps[{index}] ({kind}): kwargs must be an object",
            step_index=index,
            kind=kind,
        )


def _check_order(steps: list[Any]) -> None:
    seen: set[str] = set()
    counts: dict[str, int] = {}
    for index, step in enumerate(steps):
        kind = step["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        missing = REQUIRES_BEFORE.get(kind, frozenset()) - seen
        if missing:
            raise JobInvalid(
                f"steps[{index}] ({kind}): must follow "
                f"{', '.join(sorted(missing))}",
                step_index=index,
                kind=kind,
            )
        seen.add(kind)

    for kind in sorted(AT_MOST_ONCE & set(counts)):
        if counts[kind] > 1:
            index = next(
                i for i, s in enumerate(steps) if s["kind"] == kind
            )
            raise JobInvalid(
                f"steps[{index}] ({kind}): appears {counts[kind]} times; "
                f"{kind} may appear at most once",
                step_index=index,
                kind=kind,
            )

    if "verify" in counts and steps[-1]["kind"] != "verify":
        index = next(i for i, s in enumerate(steps) if s["kind"] == "verify")
        raise JobInvalid(
            f"steps[{index}] (verify): must be the final step -- it gates the "
            "rename, so a step after it is a step nothing checked",
            step_index=index,
            kind="verify",
        )


def validate(job: Any) -> dict[str, Any]:
    """Validate a Job against the closed vocabulary, returning it unchanged.

    Returns the *same* object: normalising here would mean two representations
    of one Job, and `job_hash` would depend on which path produced the bytes.
    """
    if not isinstance(job, dict):
        raise JobInvalid("a Job must be an object with plan and steps")
    unknown = set(job) - {"plan", "steps"}
    if unknown:
        raise JobInvalid(
            f"unknown top-level key(s) {sorted(unknown)}; a Job carries plan "
            "and steps and nothing else"
        )
    _check_plan_name(job)

    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        raise JobInvalid("job.steps must be a non-empty list", field="steps")
    for index, step in enumerate(steps):
        _check_step(index, step)
    _check_order(steps)
    return job


def bind_and_seal(job: Any) -> tuple[str, str]:
    """Validate, canonicalise, hash. The one path from a binder's output to a row.

    Kept in one function so a caller cannot store bytes it did not validate, or
    a hash of an object rather than of the bytes that were stored.
    """
    validate(job)
    text = canonical(job)
    return text, job_hash(text)
