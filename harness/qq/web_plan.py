"""A gate may request public research; only Runtime grants and executes it."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class QQWebSearchStep(_Frozen):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    action: Literal["search"]
    query: str = Field(min_length=1, max_length=1000)
    max_results: int = Field(default=3, ge=1, le=10)
    time_range: Literal["day", "week", "month", "year"] | None = None


class QQWebSearchSelection(_Frozen):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    result_indices: list[int] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def valid_indices(self):
        if len(set(self.result_indices)) != len(self.result_indices) or any(i < 0 or i > 9 for i in self.result_indices):
            raise ValueError("invalid search result indices")
        return self


class QQWebReadStep(_Frozen):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    action: Literal["read"]
    source_refs: list[str] = Field(default_factory=list, max_length=3)
    search_results: list[QQWebSearchSelection] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def bounded_sources(self):
        count = len(self.source_refs) + sum(len(item.result_indices) for item in self.search_results)
        if not 1 <= count <= 3 or len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("a batch read requires one to three distinct sources")
        if any(not ref.startswith("web-source:") or len(ref) > 256 or any(c.isspace() for c in ref)
               for ref in self.source_refs):
            raise ValueError("read requires registered source references")
        return self


QQWebStep = Annotated[QQWebSearchStep | QQWebReadStep, Field(discriminator="action")]


class QQWebPlan(_Frozen):
    steps: list[QQWebStep] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def bounded_dependencies(self):
        seen, searches, reads = set(), set(), 0
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError("duplicate web step id")
            seen.add(step.step_id)
            if step.action == "search":
                searches.add(step.step_id)
            else:
                reads += 1
                if any(item.step_id not in searches for item in step.search_results):
                    raise ValueError("read may reference only an earlier search")
        if len(searches) > 2 or reads > 1:
            raise ValueError("web plan exceeds the per-turn operation budget")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def execution_id(self, prepare_id: str, step_id: str) -> str:
        if step_id not in {step.step_id for step in self.steps}:
            raise ValueError("unknown web step")
        return "qq-web:" + hashlib.sha256((prepare_id + "\0" + self.digest + "\0" + step_id).encode()).hexdigest()
