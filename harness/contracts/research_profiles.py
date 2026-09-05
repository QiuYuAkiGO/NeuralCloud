"""Frozen research profiles independent of runtime service imports."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ConsolidationProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always", allow_inf_nan=False)

    version: Literal["body-belief-consolidation-v1"] = "body-belief-consolidation-v1"
    minimum_support: int = Field(default=3, ge=2, le=32, strict=True)
    minimum_windows: int = Field(default=2, ge=2, le=16, strict=True)
    window_seconds: int = Field(default=60, ge=1, le=86_400, strict=True)
    minimum_precision: float = Field(default=.7, ge=.5, le=1)
    maximum_contradiction_ratio: float = Field(default=.34, ge=0, le=.5)
    drop_contradiction_ratio: float = Field(default=.5, ge=.5, le=1)
