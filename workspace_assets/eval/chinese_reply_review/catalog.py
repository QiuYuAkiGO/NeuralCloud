"""Frozen, documentation-checked candidates; importing this module performs no I/O.

Gateway availability is checked separately by ``preflight``. A documented request
shape is not proof of the identity of the gateway's upstream model or of whether
the gateway actually honored a thinking flag. See candidate-research.md.
"""

from __future__ import annotations

from copy import deepcopy

CHECKED_AT = "2026-09-16"
OUTPUT_CAP = 8192
GATEWAY_CHAT = "https://docs.apiyi.com/api-capabilities/openai/compatible"
GATEWAY_CLAUDE = "https://docs.apiyi.com/api-capabilities/claude-effort-thinking"
ARENA = "https://arena.ai/leaderboard/text/chinese"
OPENAI_GUIDE = "https://developers.openai.com/api/docs/guides/latest-model"
CLAUDE_THINKING = "https://platform.claude.com/docs/en/build-with-claude/thinking"
CLAUDE_EFFORT = "https://platform.claude.com/docs/en/build-with-claude/effort"
DEEPSEEK_API = "https://api-docs.deepseek.com/api/create-chat-completion/"
DEEPSEEK_PRICING = "https://api-docs.deepseek.com/quick_start/pricing/"
QWEN_API = "https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-api-via-openai-chat-completions"
KIMI_API = "https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart"
GLM_THINKING = "https://docs.bigmodel.cn/cn/guide/capabilities/thinking"
MINIMAX_API = "https://platform.minimax.io/docs/api-reference/text-openai-api"


def _source(url: str, kind: str, note: str) -> dict:
    return {"url": url, "kind": kind, "checked_at": CHECKED_AT, "note": note}


def _model(
    model_id: str,
    provider: str,
    options: dict,
    reasoning_policy: str,
    selection_reason: str,
    sources: list[dict],
    *,
    protocol: str = "chat",
    release_date: str | None = None,
    version_date: str | None = None,
    limitations: str = "",
) -> dict:
    gateway = GATEWAY_CLAUDE if protocol == "anthropic" else GATEWAY_CHAT
    return {
        "id": model_id,
        "provider": provider,
        "protocol": protocol,
        "path": "/messages" if protocol == "anthropic" else "/chat/completions",
        "options": deepcopy(options),
        "reasoning_policy": reasoning_policy,
        "selection_reason": selection_reason,
        "sources": [*sources, _source(gateway, "gateway_protocol", "APIyi documents this endpoint and protocol.")],
        "release_date": release_date,
        "version_date": version_date,
        "date_note": "Unknown dates are null; a snapshot/version date is not a verified publication date.",
        "parameter_verification": "documentation_only; no generation probe",
        "identity_verification": "gateway_catalog_and_returned_id_only",
        "limitations": limitations or "Gateway forwarding and upstream identity cannot be independently established from documentation.",
    }


MODELS: list[dict] = [
    _model(
        "gpt-6-astra", "openai",
        {"max_completion_tokens": OUTPUT_CAP, "reasoning_effort": "low"},
        "Required thinking; low is the lowest documented effort (none/minimal unsupported).",
        "Current OpenAI flagship selected for recency, not an established role-chat preference claim.",
        [_source(OPENAI_GUIDE, "official_api", "Chat Completions supported; low minimum; omit sampling controls.")],
    ),
    _model(
        "gpt-5.6-sol", "openai",
        {"max_completion_tokens": OUTPUT_CAP, "reasoning_effort": "none"},
        "Thinking disabled using reasoning_effort=none.",
        "Recent flagship with independent Chinese preference evidence at a different reasoning setting.",
        [_source("https://developers.openai.com/api/docs/models/gpt-5.6-sol", "official_model", "Supports Chat Completions and effort none."),
         _source(ARENA, "human_preference", "Chinese list includes Sol xhigh; our none setting differs.")],
    ),
    _model(
        "gpt-4o-2024-11-20", "openai", {"max_tokens": OUTPUT_CAP},
        "Non-reasoning model; no thinking parameter.",
        "Fixed historical GPT-4o snapshot, retained as a conversational baseline.",
        [_source("https://developers.openai.com/api/docs/models/gpt-4o", "official_model", "GPT-4o snapshot and Chat Completions reference.")],
        version_date="2024-11-20",
    ),
    _model(
        "claude-fable-5-1", "anthropic",
        {"max_tokens": OUTPUT_CAP, "thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}},
        "Thinking cannot be disabled; adaptive with lowest documented effort low.",
        "Recent Claude flagship with Chinese human preference evidence at max, not at this run's low effort.",
        [_source(CLAUDE_THINKING, "official_api", "Fable 5.1 rejects disabled thinking."),
         _source(CLAUDE_EFFORT, "official_api", "Fable 5.1 supports low effort."),
         _source(ARENA, "human_preference", "Chinese list includes Fable 5.1 max; setting differs.")],
        protocol="anthropic",
    ),
    _model(
        "claude-opus-5", "anthropic",
        {"max_tokens": OUTPUT_CAP, "thinking": {"type": "disabled"}, "output_config": {"effort": "high"}},
        "Thinking disabled; explicitly retain documented default high effort, a legal disabled combination.",
        "Recent flagship with independent Chinese preference evidence; thinking settings differ.",
        [_source(CLAUDE_THINKING, "official_api", "Disabled accepted only at effort high or below."),
         _source(CLAUDE_EFFORT, "official_api", "Default effort is high."),
         _source(ARENA, "human_preference", "Chinese list includes Opus 5 high/max; thinking may differ.")],
        protocol="anthropic",
    ),
    _model(
        "claude-opus-4-6", "anthropic",
        {"max_tokens": OUTPUT_CAP, "thinking": {"type": "disabled"}},
        "Thinking explicitly disabled; other generation controls retain defaults.",
        "Earlier Claude baseline supported by independent Chinese preference evidence.",
        [_source(CLAUDE_THINKING, "official_api", "Opus 4.6 supports optional thinking."),
         _source(ARENA, "human_preference", "Chinese list includes Opus 4.6.")],
        protocol="anthropic",
    ),
    _model(
        "deepseek-v4.1-flash", "deepseek",
        {"max_tokens": OUTPUT_CAP, "reasoning_effort": "none"},
        "Thinking disabled using documented reasoning_effort=none.",
        "Latest DeepSeek Flash generation, selected for recency.",
        [_source(DEEPSEEK_API, "official_api", "reasoning_effort none disables thinking."),
         _source("https://api-docs.deepseek.com/news/news260910/", "official_release", "V4.1 Flash release announcement."),
         _source(DEEPSEEK_PRICING, "official_model", "Current Flash model version is V4.1 Flash.")],
        release_date="2026-09-10",
        limitations="APIyi ID differs from official deepseek-flash alias. Runtime catalog presence is required; returned ID is only an identity clue.",
    ),
    _model(
        "deepseek-v4-pro-0813", "deepseek",
        {"max_tokens": OUTPUT_CAP, "reasoning_effort": "none"},
        "Thinking disabled using reasoning_effort=none; do not replace with moving deepseek-v4-pro alias.",
        "Dated V4 Pro comparison; independent Chinese evidence concerns high effort, not this non-thinking request.",
        [_source(DEEPSEEK_API, "official_api", "none disables thinking; max_tokens is output cap."),
         _source(DEEPSEEK_PRICING, "official_model", "Pricing page says V4 Pro 0813 service continues after September 14."),
         _source("https://api-docs.deepseek.com/updates/", "official_release", "August 13 update; older retirement notice conflicts with current pricing."),
         _source(ARENA, "human_preference", "Chinese list includes V4 Pro high 20260813; effort differs.")],
        release_date="2026-08-13", version_date="2026-08-13",
        limitations="Official older changelog planned redirect after September 14; current pricing reversed retirement. APIyi dated route is not independently attested. Preserve requested and returned IDs.",
    ),
    _model(
        "qwen3.8-max-0902", "qwen",
        {"max_tokens": OUTPUT_CAP, "reasoning_effort": "none"},
        "Thinking disabled via APIyi-documented reasoning_effort=none.",
        "Recent Qwen flagship snapshot; Chinese preference evidence is for the model family.",
        [_source("https://docs.apiyi.com/api-capabilities/qwen-3-8/chat-completions", "gateway_api", "none disables thinking; max_tokens limits visible output, not reasoning."),
         _source("https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen3-8-max", "official_model", "Lists qwen3.8-max-0902 snapshot."),
         _source(ARENA, "human_preference", "Chinese list includes Qwen3.8 Max; exact snapshot/settings not proven.")],
        version_date="2026-09-02",
    ),
    _model(
        "qwen3.7-plus", "qwen", {"max_tokens": OUTPUT_CAP, "enable_thinking": False},
        "Thinking disabled with documented top-level enable_thinking=false.",
        "Recent Qwen Plus tier with independent Chinese preference coverage.",
        [_source(QWEN_API, "official_api", "Hybrid thinking supports enable_thinking=false."),
         _source(ARENA, "human_preference", "Chinese list includes Qwen3.7 Plus; settings not proven identical.")],
    ),
    _model(
        "qwen3-235b-a22b-instruct-2507", "qwen", {"max_tokens": OUTPUT_CAP},
        "Instruct-2507 is a non-thinking model; do not send a hybrid-thinking switch.",
        "Fixed open-weight instruction model as a non-thinking historical control.",
        [_source("https://huggingface.co/Qwen/Qwen3-235B-A22B-Instruct-2507", "official_model", "Official Qwen model card says non-thinking only.")],
        version_date="2025-07",
    ),
    _model(
        "kimi-k3", "kimi", {"max_tokens": OUTPUT_CAP, "reasoning_effort": "low"},
        "K3 always thinks; low is the lowest documented effort.",
        "Latest Kimi flagship, with independent Chinese preference evidence at max effort.",
        [_source("https://platform.kimi.ai/docs/guide/kimi-k3-quickstart", "official_api", "Always thinking; supports low/high/max."),
         _source(ARENA, "human_preference", "Chinese list includes Kimi K3 max; effort differs.")],
    ),
    _model(
        "kimi-k2.6", "kimi", {"max_tokens": OUTPUT_CAP, "thinking": {"type": "disabled"}},
        "Thinking disabled with Kimi's documented thinking.type=disabled.",
        "Recent general-purpose Kimi model with Chinese preference coverage.",
        [_source(KIMI_API, "official_api", "K2.6 supports thinking.type disabled."),
         _source(ARENA, "human_preference", "Chinese list includes Kimi K2.6; thinking mode not proven identical.")],
        limitations="APIyi documents a general compatible endpoint; exact K2.6 upstream route/extension forwarding is not independently verified.",
    ),
    _model(
        "kimi-k2.5", "kimi", {"max_tokens": OUTPUT_CAP, "enable_thinking": False},
        "Use APIyi's Alibaba route enable_thinking=false (gateway default Instant), not Moonshot's native flag.",
        "Historical Kimi conversational control, retained only on the explicitly documented APIyi route.",
        [_source("https://docs.apiyi.com/api-capabilities/kimi-k2-5", "gateway_api", "APIyi Alibaba route uses enable_thinking, default false."),
         _source("https://platform.kimi.ai/docs/models", "official_model", "Moonshot-native K2.5 retired August 31; gateway availability is separate.")],
        release_date="2026-01-27",
        limitations="Moonshot native service deprecated 2026-08-31. APIyi documents Alibaba deployment; successful catalog/response does not independently establish weights.",
    ),
    _model(
        "glm-5.3", "glm",
        {"max_tokens": OUTPUT_CAP, "thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        "GLM-5.3 requires thinking; explicitly enabled with lowest documented effort low.",
        "Recent GLM flagship with Chinese preference evidence at max effort.",
        [_source("https://docs.z.ai/guides/llm/glm-5.3", "official_api", "Disabling thinking fails; low/high/max supported."),
         _source(ARENA, "human_preference", "Chinese list includes GLM-5.3 max; effort differs.")],
    ),
    _model(
        "glm-4.7", "glm", {"max_tokens": OUTPUT_CAP, "thinking": {"type": "disabled"}},
        "Thinking disabled using thinking.type=disabled.",
        "Earlier GLM comparison with independent Chinese preference coverage.",
        [_source(GLM_THINKING, "official_api", "GLM-4.7 supports disabling thinking."),
         _source(ARENA, "human_preference", "Chinese list includes GLM-4.7; settings may differ.")],
    ),
    _model(
        "glm-4.5", "glm", {"max_tokens": OUTPUT_CAP, "thinking": {"type": "disabled"}},
        "Thinking disabled using thinking.type=disabled.",
        "Historical GLM generation retained for role-expression comparison; no blanket popularity claim.",
        [_source(GLM_THINKING, "official_api", "GLM-4.5 supports disabling thinking.")],
    ),
    _model(
        "MiniMax-M3", "minimax",
        {"max_completion_tokens": OUTPUT_CAP, "thinking": {"type": "disabled"}, "reasoning_split": True},
        "M3 thinking disabled; reasoning_split keeps any exposed reasoning outside visible content.",
        "Current MiniMax flagship selected for recency.",
        [_source(MINIMAX_API, "official_api", "M3 accepts thinking disabled and max_completion_tokens."),
         _source("https://docs.apiyi.com/news/minimax-m3-launch", "gateway_model", "APIyi M3 availability announcement.")],
    ),
    _model(
        "MiniMax-M2.7", "minimax",
        {"max_completion_tokens": OUTPUT_CAP, "reasoning_split": True},
        "Required thinking with no documented effort control; sole documented/default mode retained. This is not low effort.",
        "Prior MiniMax model with official dialogue/role consistency claims, recorded as vendor claims.",
        [_source(MINIMAX_API, "official_api", "M2.x cannot disable thinking; no documented effort field."),
         _source("https://www.minimax.io/news/minimax-m27-en", "official_capability_claim", "Vendor describes complex role and dialogue capability.")],
        limitations="No documented lowest-effort setting exists. Do not invent one or silently describe default thinking as low. reasoning_split is only output formatting.",
    ),
    _model(
        "MiniMax-M2.5", "minimax",
        {"max_completion_tokens": OUTPUT_CAP, "reasoning_split": True},
        "Required thinking with no documented effort control; sole documented/default mode retained. This is not low effort.",
        "Historical MiniMax generation control.",
        [_source(MINIMAX_API, "official_api", "Lists M2.5 support; M2.x thinking cannot be disabled.")],
        limitations="No documented effort switch; fixed/default thinking is the only documented mode. Gateway identity and forwarding remain unverified.",
    ),
]

EXCLUDED: list[dict] = [{
    "id": "deepseek-v3-0324",
    "provider": "deepseek",
    "reason": "No independent APIyi catalog entry; deepseek-v3 snapshot mapping is unconfirmed. Do not substitute.",
    "sources": [_source("https://api-docs.deepseek.com/updates/", "official_release", "Historical V3-0324 release does not prove a gateway alias mapping.")],
}]
