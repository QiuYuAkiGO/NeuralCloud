"""Durable conservative ledger; interrupted requests retain their reservation."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .models import BudgetSpec, PriceSpec
from harness.files import replace_path


class BudgetStopped(RuntimeError):
    pass


class BudgetLedger:
    def __init__(self, path: Path, spec: BudgetSpec, price: PriceSpec):
        self.path, self.spec, self.price = path, spec, price

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise BudgetStopped("budget_ledger_locked; inspect interrupted owner before recovery") from exc
        os.close(fd)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
                "schema_version": "expression-budget-v1", "scope_id": self.spec.scope_id,
                "spent_cny": "0", "reservations": {}, "requests": 0, "stopped": False,
                "hard_limit_cny": "50", "stop_limit_cny": "45",
            }
            if data.get("scope_id") != self.spec.scope_id:
                raise BudgetStopped("budget_scope_mismatch")
            yield data
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                replace_path(temporary, self.path)
            except PermissionError as exc:
                raise BudgetStopped("budget_ledger_atomic_write_unavailable") from exc
        finally:
            lock.unlink()

    def reserve(self, request_id: str, *, max_output_tokens: int) -> str:
        amount = (Decimal(self.spec.max_input_tokens) * Decimal(str(self.price.input_per_million))
                  + Decimal(max_output_tokens) * Decimal(str(self.price.output_per_million))) / Decimal(1_000_000)
        with self.locked() as data:
            exposure = Decimal(data["spent_cny"]) + sum((Decimal(item["amount_cny"]) for item in data["reservations"].values()), Decimal(0))
            if data["stopped"] or data["requests"] >= self.spec.max_requests:
                raise BudgetStopped("request_limit_or_prior_stop")
            if request_id in data["reservations"]:
                raise BudgetStopped("duplicate_request_reservation")
            if exposure + amount > Decimal("45"):
                raise BudgetStopped("45_CNY_stop_including_unsettled_reservations")
            data["reservations"][request_id] = {"amount_cny": str(amount), "max_output_tokens": max_output_tokens,
                                                 "max_input_tokens": self.spec.max_input_tokens,
                                                 "price": self.price.model_dump(mode="json")}
            data["requests"] += 1
        return str(amount)

    def settle(self, request_id: str, usage: dict[str, Any] | None, *, never_sent: bool = False) -> dict[str, Any]:
        with self.locked() as data:
            reserved = data["reservations"].get(request_id)
            if reserved is None:
                raise ValueError("request has no reservation")
            if never_sent:
                del data["reservations"][request_id]
                return {"status": "not_sent", "charge_cny": "0"}
            if not usage or any(type(usage.get(key)) is not int or usage[key] < 0 for key in ("prompt_tokens", "completion_tokens")):
                return {"status": "unsettled", "reserved_cny": reserved["amount_cny"]}
            price = reserved["price"]
            charge = (Decimal(usage["prompt_tokens"]) * Decimal(str(price["input_per_million"]))
                      + Decimal(usage["completion_tokens"]) * Decimal(str(price["output_per_million"]))) / Decimal(1_000_000)
            data["spent_cny"] = str(Decimal(data["spent_cny"]) + charge)
            del data["reservations"][request_id]
            violated = usage["prompt_tokens"] > reserved.get("max_input_tokens", self.spec.max_input_tokens) or usage["completion_tokens"] > reserved["max_output_tokens"]
            if violated or Decimal(data["spent_cny"]) >= Decimal("45"):
                data["stopped"] = True
            return {"status": "settled", "conservative_charge_cny": str(charge), "provider_cap_violation": violated}

    def stop_new_requests(self, reason: str) -> None:
        """Persist an admission stop without discarding any in-flight reservation."""
        with self.locked() as data:
            data["stopped"] = True
            data.setdefault("stop_reason", reason)

    def snapshot(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"spent_cny": "0", "reserved_cny": "0", "requests": 0}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["reserved_cny"] = str(sum((Decimal(item["amount_cny"]) for item in data["reservations"].values()), Decimal(0)))
        data["exposure_cny"] = str(Decimal(data["spent_cny"]) + Decimal(data["reserved_cny"]))
        return data
