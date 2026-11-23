"""The plan-types registry: definitions live in git, not the database (§7.1).

Each type is one YAML file in `plan_types/` with a name, a version, a one-line
description, a JSON-Schema parameter schema, and what `verify`/`resume` mean for
it. Loading happens once at startup; a malformed file is a code defect and
raises, because silently shipping an empty registry would turn every plan
submission into `plan_type_unknown`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# §3.1/§3.2: these two are the only legal handlers for a verification failure.
ON_FAILURE_VALUES = frozenset({"commit_marked", "quarantine"})


@dataclass(frozen=True)
class PlanType:
    name: str
    version: int
    description: str
    source: str
    verify_required: bool
    on_failure_allowed: frozenset[str]
    params_schema: dict[str, Any] = field(default_factory=dict)
    verify_meaning: str = ""
    resume_meaning: str = ""
    # Who sells the data, and the dataset codes they speak for. Declared in git
    # with the type, because the read model needs a vendor name on an intent
    # row -- which has no plan type, and so no other handle on its vendor.
    vendor: str = ""
    datasets: tuple[str, ...] = ()
    #: Dataset pattern -> seller, for a type that serves more than one vendor.
    #: `datasets` above is always the union, so matching keeps one question.
    vendors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Which shared form this type's params schema came from, if any, and which
    # committed binder turns its params into a Job. `binder` is a *name*, so two
    # vendors whose question has the same shape can share one binder module --
    # adding the second vendor is then a YAML file and a driver artefact, and no
    # change to this repo's code.
    form: str = ""
    binder: str = ""

    def index_entry(self) -> dict[str, Any]:
        """What `GET /v1/plan-types` returns: enough to decide, not the schema."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "source": self.source,
            "vendor": self.vendor,
            "verify_required": self.verify_required,
        }

    def full_entry(self) -> dict[str, Any]:
        return {
            **self.index_entry(),
            "params_schema": self.params_schema,
            "vendor": self.vendor,
            "datasets": list(self.datasets),
            "vendors": {name: list(patterns) for name, patterns in self.vendors.items()},
            "verify": {
                "required": self.verify_required,
                "on_failure": sorted(self.on_failure_allowed),
                "meaning": self.verify_meaning,
            },
            "resume": self.resume_meaning,
        }


class Registry:
    def __init__(self, types: dict[str, PlanType], directory: Path | None) -> None:
        self._types = types
        self.directory = directory

    def __len__(self) -> int:
        return len(self._types)

    def __contains__(self, name: object) -> bool:
        return name in self._types

    def get(self, name: str) -> PlanType | None:
        return self._types.get(name)

    def all(self) -> list[PlanType]:
        return [self._types[k] for k in sorted(self._types)]

    def vendor_of(self, plan_type: str | None) -> str:
        """The vendor behind a plan type, or "" when the type is not one of ours."""
        entry = self._types.get(plan_type) if plan_type else None
        return entry.vendor if entry else ""

    def vendor_for_dataset(self, dataset: str | None) -> str:
        """Who sells this dataset code, as declared by the types that claim it.

        An intent names a dataset in vendor vocabulary before any plan exists,
        so the only handle on its vendor is the namespace the registered types
        declare. No match is not an error about the request -- it means the
        vocabulary in git has nothing to say about this code yet.
        """
        if not dataset:
            return ""
        for entry in self.all():
            for name, patterns in entry.vendors.items():
                if any(fnmatch(dataset, pattern) for pattern in patterns):
                    return name
            if entry.vendor and any(
                fnmatch(dataset, pattern) for pattern in entry.datasets
            ):
                return entry.vendor
        return ""

    def vendor_of_request(
        self, plan_type: str | None, params: dict[str, Any] | None
    ) -> str:
        """Who this request asks, in the order the answer becomes specific.

        `params.vendor` first: the generic plan type serves any seller, so the
        request itself says who is being asked. Then the type's pinned vendor,
        for the single-seller shape. Then the dataset code, which is the last
        handle an intent has before a plan exists.

        One function because three callers need the same answer -- the row
        (`_vendor_of`), the pricer, and the runner's destination -- and three
        copies would eventually disagree about which driver to call.
        """
        fields = params or {}
        declared = str(fields.get("vendor") or "").strip()
        if declared:
            return declared
        pinned = self.vendor_of(plan_type)
        if pinned:
            return pinned
        return self.vendor_for_dataset(str(fields.get("dataset") or ""))


def _load_form(path: Path) -> tuple[str, dict[str, Any]]:
    """One shared form: a params schema a plan type may reference by name.

    A form is the *question* -- which fields a person fills in and how they are
    validated -- with no vendor in it. The vendor, its dataset codes and its
    verify policy stay on the plan type, because those differ per vendor even
    when the question does not.
    """
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    name = str(doc.get("name") or path.stem)
    if name != path.stem:
        raise ValueError(f"{path}: name {name!r} must match the filename")
    schema = doc.get("params_schema")
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: params_schema must be a mapping")
    return name, schema


def _load_forms(directory: Path) -> dict[str, dict[str, Any]]:
    forms: dict[str, dict[str, Any]] = {}
    if not directory.is_dir():
        return forms
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        name, schema = _load_form(path)
        if name in forms:
            raise ValueError(f"{path}: duplicate form {name!r}")
        forms[name] = schema
    return forms


def _load_one(path: Path, forms: dict[str, dict[str, Any]]) -> PlanType:
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top level must be a mapping")

    for key in ("name", "description"):
        if key not in doc:
            raise ValueError(f"{path}: missing required key {key!r}")
    if doc["name"] != path.stem:
        raise ValueError(f"{path}: name {doc['name']!r} must match the filename")

    form = str(doc.get("form") or "").strip()
    if form and "params_schema" in doc:
        # One or the other: a type that both references a shared form and ships
        # its own schema has two answers to "what may I be asked", and which one
        # validation used would depend on the loader.
        raise ValueError(
            f"{path}: declare `form:` or `params_schema:`, not both"
        )
    if form:
        shared = forms.get(form)
        if shared is None:
            known = ", ".join(sorted(forms)) or "none"
            raise ValueError(
                f"{path}: form {form!r} is not declared in the forms directory "
                f"(defined: {known})"
            )
        params_schema: Any = shared
    elif "params_schema" in doc:
        params_schema = doc["params_schema"]
    else:
        raise ValueError(
            f"{path}: missing required key 'params_schema' (or `form:` naming a "
            "shared form)"
        )
    if not isinstance(params_schema, dict):
        raise ValueError(f"{path}: params_schema must be a mapping")

    verify = doc.get("verify") or {}
    if not isinstance(verify, dict):
        raise ValueError(f"{path}: verify must be a mapping")
    allowed = verify.get("on_failure") or sorted(ON_FAILURE_VALUES)
    if not isinstance(allowed, list) or not allowed:
        raise ValueError(f"{path}: verify.on_failure must be a non-empty list")
    bad = set(allowed) - ON_FAILURE_VALUES
    if bad:
        raise ValueError(f"{path}: verify.on_failure has unknown value(s) {sorted(bad)}")

    # Attribution, two accepted shapes:
    #
    #   vendor: Databento          one seller for every pattern below
    #   datasets: ["*.ITCH"]
    #
    # or the generic one, where a single plan type serves several sellers:
    #
    #   vendors:
    #     Databento: ["*.ITCH", "*.MDP3"]
    #     Refinitiv: ["RIC/*"]
    #
    # `datasets` is always the union -- matching asks "does some registered type
    # claim this code", which does not care who claims it. The mapping is what
    # names the vendor on a row that has no plan type yet.
    vendors_doc = doc.get("vendors")
    vendors: dict[str, tuple[str, ...]] = {}
    if vendors_doc is not None:
        if not isinstance(vendors_doc, dict) or not vendors_doc:
            raise ValueError(f"{path}: vendors must be a non-empty mapping")
        if "datasets" in doc or "vendor" in doc:
            raise ValueError(
                f"{path}: declare `vendors:` or `vendor:`+`datasets:`, not both"
            )
        for name, patterns in vendors_doc.items():
            if not isinstance(patterns, list) or not all(
                isinstance(pattern, str) for pattern in patterns
            ):
                raise ValueError(
                    f"{path}: vendors.{name} must be a list of dataset patterns"
                )
            vendors[str(name).strip()] = tuple(patterns)
        datasets = [pattern for patterns in vendors.values() for pattern in patterns]
    else:
        datasets = doc.get("datasets") or []
        if not isinstance(datasets, list) or not all(
            isinstance(d, str) for d in datasets
        ):
            raise ValueError(f"{path}: datasets must be a list of strings")

    return PlanType(
        name=str(doc["name"]),
        version=int(doc.get("version", 1)),
        description=str(doc["description"]).strip(),
        source=path.name,
        # A pinned vendor (the single-seller shape) if declared, else "" -- the
        # vendor then comes from the request's params or from the dataset map.
        vendor=str(doc.get("vendor") or "").strip(),
        datasets=tuple(datasets),
        vendors=vendors,
        verify_required=bool(verify.get("required", True)),
        on_failure_allowed=frozenset(allowed),
        params_schema=params_schema,
        form=form,
        binder=str(doc.get("binder") or "").strip(),
        verify_meaning=str(verify.get("meaning", "")).strip(),
        resume_meaning=str(doc.get("resume", "")).strip(),
    )


def load_registry(directory: Path | None) -> Registry:
    if directory is None or not directory.is_dir():
        log.error("plan-types directory not found - the registry is empty")
        return Registry({}, directory)

    forms = _load_forms(directory / "forms")
    types: dict[str, PlanType] = {}
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        entry = _load_one(path, forms)
        if entry.name in types:
            raise ValueError(f"{path}: duplicate plan type {entry.name!r}")
        types[entry.name] = entry
    log.info(
        "loaded %d plan type(s) and %d shared form(s) from %s",
        len(types),
        len(forms),
        directory,
    )
    return Registry(types, directory)
