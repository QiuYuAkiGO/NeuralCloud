"""Persistent, model-owned goals with evidence-gated scheduling and recovery."""

from harness.agency.models import AgencyConfig, GoalTrigger, ResumeObservation

__all__ = ["AgencyConfig", "AgencyService", "GoalTrigger", "ResumeObservation"]


def __getattr__(name: str):
    if name == "AgencyService":
        from harness.agency.planner import AgencyService
        return AgencyService
    raise AttributeError(name)
