"""Conservative, byte-bounded budget arithmetic without any API calls."""
from decimal import Decimal
import json
from pathlib import Path

from tools.production_acceptance.frozen_model_comparison import peak_cost, reserve_cost, save


def test_peak_tariff_and_cache_accounting():
    assert peak_cost(1_000_000, 0) == Decimal('2.00')
    assert peak_cost(1_000_000, 0, cached=1_000_000) == Decimal('0.04')
    assert peak_cost(0, 125_000) == Decimal('1.00')


def test_rounding_small_calls_upwards_preserves_budget_margin():
    assert peak_cost(100, 100) == Decimal('0.01')
    assert peak_cost(500_001, 0) == Decimal('1.01')


def test_reservation_covers_multilingual_text_and_full_output_limit():
    payload = {'messages': [{'role': 'user', 'content': '中文与 emoji：🦊' * 20}], 'max_tokens': 4096}
    reserved = reserve_cost(payload)
    assert reserved >= peak_cost(1000, 4096)
    assert reserved >= peak_cost(1000, 2048)


def test_windows_read_sharing_retries_only_file_replace(monkeypatch, tmp_path):
    original = Path.replace
    attempts = []

    def temporarily_locked(path, target):
        attempts.append(target)
        if len(attempts) < 3:
            raise PermissionError('temporary file sharing lock')
        return original(path, target)

    monkeypatch.setattr(Path, 'replace', temporarily_locked)
    monkeypatch.setattr('tools.production_acceptance.frozen_model_comparison.time.sleep', lambda _: None)
    target = tmp_path / 'report.json'
    save(target, {'reserved_slots': 3})
    assert len(attempts) == 3
    assert json.loads(target.read_text(encoding='utf-8')) == {'reserved_slots': 3}
