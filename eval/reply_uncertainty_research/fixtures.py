"""Reuse isolated QQ adapters with fixed source IDs/timestamps across experiment arms."""

from datetime import datetime, timezone
from itertools import count
from uuid import UUID

from common import digest
from eval.reply_internalization import fixtures as prior
from adapters import install


class Fixtures(prior.Fixtures):
    def __init__(self, decision, settings, job, scenario, preset, arm, config):
        from harness.qq import memory

        stamp = datetime.fromisoformat(config["fixture_clock"])

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return stamp.astimezone(tz or timezone.utc)

        prior.datetime = Clock
        memory.datetime = Clock
        serial = count()
        memory.uuid4 = lambda: UUID(digest({"scenario": scenario["id"], "serial": next(serial)})[:32])
        # The older fixture's prompt_only patch is deliberately not used here.
        super().__init__(decision, settings, job, scenario, preset, "baseline", config)
        if config.get("receipt_ablation"):
            if arm not in {"control", "cards"}:
                raise ValueError("receipt_ablation_uses_two_arms")
            if arm == "cards":
                from receipts import install as install_receipts

                install_receipts()
        else:
            install(arm, decision)
