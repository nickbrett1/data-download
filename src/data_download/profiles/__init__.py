"""Per-Plan committed code, resolved by name (§1, §5.1).

A **Plan** has two halves, and this package is the executable one:

* the declarative half -- param schema, vendor, dataset patterns, verify/resume
  prose -- lives in `plan_types/*.yaml` and is loaded by `registry.py`;
* this half -- the **binder**, `bind(params, verify) -> Job` -- is code, one
  module per Plan, resolved by Plan *name*, never by module path.

`bind` is deterministic and pure: same params in, byte-identical Job out, no
clock, no random, no vendor client, no network. That is what makes `job_hash`
worth comparing across processes, and it is asserted in `tests/test_gate.py`.

The directory is named `profiles/` because the design named it (§0's reading
key); `plan_types/` is where the Plan is *declared*. Two trees, two different
jobs -- a declaration is reviewable by a non-programmer, a binder is not.

Phase 2 ships no driver factory here. The real Databento driver is phase 5; the
offline driver phase 2 exercises lives in `dryrun.py`, deliberately outside the
service's import graph.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..registry import PlanType, Registry
from . import databento_batch_download

# The signature every binder implements. `verify` is the normalised body field
# (`{"on_failure": ...}`), passed in rather than read from params: the Request
# carries it at the top level (§7.2), the Plan's schema constrains it, and the
# Job records the outcome policy of *this* request (§5.4).
Binder = Callable[[Mapping[str, Any], Mapping[str, Any] | None], dict[str, Any]]

BINDERS: dict[str, Binder] = {
    databento_batch_download.PLAN_NAME: databento_batch_download.bind,
}


@dataclass(frozen=True)
class Plan:
    """A registry entry with its committed binder attached: §1's Plan record."""

    name: str
    binder: Binder
    type: PlanType


def resolve(registry: Registry, name: object) -> Plan | None:
    """Resolve a Plan by name. `None` means the registry has never heard of it.

    The binder is looked up by the type's declared `binder:`, defaulting to its
    own name. That indirection is what lets a second vendor whose question has
    the same shape reuse an existing binder: its YAML names the binder instead
    of requiring a module, so adding it is data plus a driver artefact.

    A Plan that exists in YAML but has no binder is a **code defect**, not an
    unknown plan: the registry would be advertising a capability nothing can
    bind. Raising is the fail-closed answer -- the alternative is a 500 later,
    at submit time, from a request nobody changed.
    """
    if not isinstance(name, str) or name not in registry:
        return None
    entry = registry.get(name)
    binder_name = (entry.binder if entry else "") or name
    binder = BINDERS.get(binder_name)
    if entry is None or binder is None:
        raise RuntimeError(
            f"plan type {name!r} is declared in plan_types/ but names binder "
            f"{binder_name!r}, which is not in data_download/profiles/; add it "
            "before shipping the type"
        )
    return Plan(name=name, binder=binder, type=entry)


def unbound_types(registry: Registry) -> list[str]:
    """Registry entries naming no committed binder -- a gate assertion, not a route."""
    return [
        entry.name
        for entry in registry.all()
        if (entry.binder or entry.name) not in BINDERS
    ]
