"""Prefixed identifiers matching 01-cognitive-harness.md examples."""

from __future__ import annotations

import secrets

PREFIXES: dict[str, str] = {
    "event": "evt",
    "fact": "sem",
    "relation": "rel",
    "candidate": "cand",
    "situation": "sit",
    "persona": "persona",
    "trace": "tr",
    "insight": "ins",
    "context": "ctx",
    "response": "resp",
    "parse": "prs",
    "inference": "inf",
    "call": "call",
}


def new_id(kind: str) -> str:
    prefix = PREFIXES.get(kind)
    if prefix is None:
        raise ValueError(f"unknown id kind: {kind}")
    return f"{prefix}_{secrets.token_hex(4)}"
