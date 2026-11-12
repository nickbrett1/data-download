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

    def index_entry(self) -> dict[str, Any]:
        """What `GET /v1/plan-types` returns: enough to decide, not the schema."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "source": self.source,
            "verify_required": self.verify_required,
        }

    def full_entry(self) -> dict[str, Any]:
        return {
            **self.index_entry(),
            "params_schema": self.params_schema,
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


def _load_one(path: Path) -> PlanType:
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top level must be a mapping")

    for key in ("name", "description", "params_schema"):
        if key not in doc:
            raise ValueError(f"{path}: missing required key {key!r}")
    if doc["name"] != path.stem:
        raise ValueError(f"{path}: name {doc['name']!r} must match the filename")

    verify = doc.get("verify") or {}
    if not isinstance(verify, dict):
        raise ValueError(f"{path}: verify must be a mapping")
    allowed = verify.get("on_failure") or sorted(ON_FAILURE_VALUES)
    if not isinstance(allowed, list) or not allowed:
        raise ValueError(f"{path}: verify.on_failure must be a non-empty list")
    bad = set(allowed) - ON_FAILURE_VALUES
    if bad:
        raise ValueError(f"{path}: verify.on_failure has unknown value(s) {sorted(bad)}")

    return PlanType(
        name=str(doc["name"]),
        version=int(doc.get("version", 1)),
        description=str(doc["description"]).strip(),
        source=path.name,
        verify_required=bool(verify.get("required", True)),
        on_failure_allowed=frozenset(allowed),
        params_schema=doc["params_schema"],
        verify_meaning=str(verify.get("meaning", "")).strip(),
        resume_meaning=str(doc.get("resume", "")).strip(),
    )


def load_registry(directory: Path | None) -> Registry:
    if directory is None or not directory.is_dir():
        log.error("plan-types directory not found - the registry is empty")
        return Registry({}, directory)

    types: dict[str, PlanType] = {}
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        entry = _load_one(path)
        if entry.name in types:
            raise ValueError(f"{path}: duplicate plan type {entry.name!r}")
        types[entry.name] = entry
    log.info("loaded %d plan type(s) from %s", len(types), directory)
    return Registry(types, directory)
