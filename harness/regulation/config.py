"""Opt-in production settings, independent of grounded-agency research."""
from pydantic import BaseModel, ConfigDict, Field, StrictBool


class RegulationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    enabled: StrictBool = False
    autonomous_enabled: StrictBool = True
    minimum_reconsideration_interval: float = Field(default=60, ge=60, le=86400)
    maximum_model_calls_per_hour: int = Field(default=12, ge=1, le=60)
    maximum_plan_steps: int = Field(default=32, ge=1, le=128)
    maximum_no_progress: int = Field(default=3, ge=1, le=10)
    review_seconds: float = Field(default=300, ge=60, le=86400)
    stability_seconds: float = Field(default=180, ge=180, le=3600)

