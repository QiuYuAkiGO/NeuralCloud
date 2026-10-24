"""Private configuration for protocol QQ; GUI mode keeps its own configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from harness.files import replace_path
from harness.interruption import InterruptionOptions

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from harness.qq.memory import QQID
from harness.qq.onebot import validate_endpoint
from harness.qq.roster import QQGroupRosterEntry


class QQPrivateContact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    peer_id: QQID
    label: str = Field(default="", max_length=128)
    observe: StrictBool = True
    send: StrictBool = False


class DirectQQSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport: str = "onebot"
    qqbot_app_id: str | None = None
    qqbot_credentials_file: Path | None = None
    enabled: StrictBool = False
    ws_url: str = "ws://127.0.0.1:6301"
    token_file: Path
    self_member_id: QQID
    group_roster: list[QQGroupRosterEntry] = Field(default_factory=list, max_length=16)
    private_contacts: list[QQPrivateContact] = Field(default_factory=list, max_length=16)
    session_config_path: Path
    state_db: Path
    status_file: Path
    control_file: Path
    attention_db: Path | None = None
    attention_budget_enabled: StrictBool = False
    evidence_dir: Path
    image_cache_dir: Path
    desktop_status_file: Path
    shared_runtime_url: str = "http://127.0.0.1:8770"
    shared_control_token_file: Path
    model_config_path: Path | None = None
    vision_model: str | None = None
    allowed_local_image_roots: list[Path] = Field(default_factory=list, max_length=8)
    history_count: int = Field(default=20, ge=1, le=50)
    history_poll_seconds: int = Field(default=300, ge=60, le=3600)
    qq_reply_scheduler: Literal["legacy", "shadow", "coalesced"] = "shadow"
    text_interruption: InterruptionOptions = Field(default_factory=InterruptionOptions)
    text_interruption_private: InterruptionOptions = Field(default_factory=InterruptionOptions)
    qq_media_cache_enabled: StrictBool = False
    qq_media_parallel_enabled: StrictBool = False
    qq_image_mode: Literal["all", "one", "off"] = "all"
    qq_image_analysis: Literal["eager", "on_demand"] = "eager"
    qq_latency_conversations: list[str] | None = Field(default=None, max_length=128)

    @field_validator("qq_latency_conversations")
    @classmethod
    def latency_conversations(cls, value):
        from harness.qq.policy import QQLatencyOptions
        return QQLatencyOptions.validate_conversations(value)

    @model_validator(mode="after")
    def binding(self):
        if self.transport not in {"onebot", "qqbot"}:
            raise ValueError("unsupported_direct_qq_transport")
        validate_endpoint(self.ws_url)
        if self.transport == "qqbot":
            from harness.qq.official_identity import identity, split_identity
            if (self.qqbot_app_id is None or self.qqbot_credentials_file is None
                    or self.self_member_id != identity(self.qqbot_app_id, "bot", self.qqbot_app_id)
                    ):
                raise ValueError("qqbot_configuration_scope_invalid")
            for peer in self.private_contacts:
                split_identity(peer.peer_id, kind="user", app_id=self.qqbot_app_id)
            for group in self.group_roster:
                split_identity(group.group_id, kind="group", app_id=self.qqbot_app_id)
        elif (self.qqbot_app_id is not None or self.qqbot_credentials_file is not None
                or self.self_member_id.startswith("qqbot:")
                or any(g.group_id.startswith("qqbot:") for g in self.group_roster)
                or any(p.peer_id.startswith("qqbot:") for p in self.private_contacts)):
            raise ValueError("onebot_official_identity_mismatch")
        from urllib.parse import urlsplit
        endpoint = urlsplit(self.shared_runtime_url)
        if (endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "::1"}
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError("direct_runtime_requires_loopback")
        groups = [group.group_id for group in self.group_roster]
        if len(groups) != len(set(groups)) or any(group.send and not group.observe for group in self.group_roster):
            raise ValueError("direct_group_roster_invalid")
        peers = [contact.peer_id for contact in self.private_contacts]
        if (len(peers) != len(set(peers)) or self.self_member_id in peers
                or any(contact.send and not contact.observe for contact in self.private_contacts)):
            raise ValueError("direct_private_contacts_invalid")
        if self.enabled and not any(entry.observe for entry in [*self.group_roster, *self.private_contacts]):
            raise ValueError("direct_observed_conversation_required")
        paths = [self.token_file, self.session_config_path, self.state_db, self.status_file, self.control_file,
            self.evidence_dir, self.image_cache_dir, self.desktop_status_file, self.shared_control_token_file,
            *self.allowed_local_image_roots]
        if self.attention_db is not None:
            paths.append(self.attention_db)
        if self.qqbot_credentials_file is not None:
            paths.append(self.qqbot_credentials_file)
        if any(not path.is_absolute() or path.is_symlink() for path in paths):
            raise ValueError("direct_configuration_requires_absolute_private_paths")
        return self

    @classmethod
    def read(cls, path):
        path = Path(path)
        if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError("direct_configuration_invalid")
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def atomic_status(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    replace_path(temporary, path)
