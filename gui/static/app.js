const STAGE_DEFS = [
  { id: "ingest", title: "Gateway", sub: "InteractionEvent" },
  { id: "parse", title: "Parser", sub: "ParsedInteraction" },
  { id: "state_build", title: "状态 + 检索", sub: "SituationState" },
  { id: "gate", title: "Complexity Gate", sub: "Fast / Deep" },
  { id: "interpret", title: "Social Interpreter", sub: "StructuredInsight" },
  { id: "compile", title: "Context Compiler", sub: "ModelContext" },
  { id: "generate", title: "Character Model", sub: "CandidateResponse" },
  { id: "validate", title: "Validator", sub: "ValidationResult" },
  { id: "final", title: "Final Response", sub: "角色输出" },
];

const $ = (id) => document.getElementById(id);

const state = {
  busy: false,
  runId: null,
  expanded: null,
  stages: resetStages(),
  messages: [],
  snapshot: null,
  ws: null,
  speakers: { user_name: "用户", character_name: "" },
  activePresetId: "",
  debug: { open: false, tab: "relationship", data: null },
  homeostasis: { profile: null },
};

function resetStages() {
  return STAGE_DEFS.map((def) => ({ ...def, status: "idle", events: [], latency: 0 }));
}

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const PROTOCOL_MARKER_SRC =
  "[ \\t]*(?:\\*{1,2}|`)?[\\[【](?:CONTINUE|END)[\\]】](?:\\*{1,2}|`)?[。．.!！?？]*";

function hideProtocolMarkers(text) {
  return String(text ?? "")
    .replace(new RegExp(PROTOCOL_MARKER_SRC, "gi"), " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}

function splitVisibleSegments(text) {
  const raw = String(text ?? "");
  if (!raw.trim()) return [];
  if (!new RegExp(PROTOCOL_MARKER_SRC, "i").test(raw)) {
    const cleaned = hideProtocolMarkers(raw);
    return cleaned ? [cleaned] : [];
  }
  return raw
    .split(new RegExp(PROTOCOL_MARKER_SRC, "gi"))
    .map((part) => hideProtocolMarkers(part))
    .filter(Boolean);
}

function replySegmentsOf(data) {
  const rawSegs = Array.isArray(data?.segments) && data.segments.length ? data.segments : [data?.text || ""];
  const out = [];
  rawSegs.forEach((seg) => {
    splitVisibleSegments(seg).forEach((part) => out.push(part));
  });
  return out;
}

function meter(value) {
  const n = Number(value);
  const pct = Number.isFinite(n) ? Math.round(Math.max(0, Math.min(1, n)) * 100) : 0;
  return `<div class="meter-wrap"><div class="meter"><i style="width:${pct}%"></i></div><b>${pct}%</b></div>`;
}

function chips(items) {
  return `<div class="chip-row">${(items || []).map((item) => `<span class="chip">${esc(item)}</span>`).join("")}</div>`;
}

function jsonBlock(obj) {
  return `<pre class="json">${esc(JSON.stringify(obj, null, 2))}</pre>`;
}

function withRaw(html, data) {
  const rawId = `raw-${Math.random().toString(16).slice(2)}`;
  return `${html}<button type="button" class="raw-toggle" data-raw="${rawId}">原始 JSON</button><div id="${rawId}" hidden>${jsonBlock(data)}</div>`;
}

function renderIngest(data) {
  return withRaw(
    `<div class="kv">
      <div class="kv-row"><span>event_id</span><code>${esc(data.event_id)}</code></div>
      <div class="kv-row"><span>session</span><code>${esc(data.session_id)}</code></div>
      <div class="kv-row"><span>channel</span><span>${esc(data.channel)} / ${esc(data.speaker)}</span></div>
      <div class="quote">${esc(data.raw_text)}</div>
    </div>`,
    data,
  );
}

function renderParse(data) {
  const intent = data.possible_social_intent || {};
  const emotions = Object.entries(data.emotion_cues || {});
  return withRaw(
    `<div class="kv">
      <div class="kv-row"><span>speech_act</span><span class="chip">${esc(data.speech_act)}</span></div>
      <div class="kv-row"><span>社交意图假设</span><span>${esc(intent.value || "—")} · ${esc(intent.confidence ?? "")}</span></div>
      ${intent.confidence != null ? meter(intent.confidence) : ""}
      ${emotions.map(([k, v]) => `<div class="kv-row"><span>${esc(k)}</span></div>${meter(v)}`).join("")}
      ${chips((data.references || []).map((r) => `${r.type}${r.time_hint ? ":" + r.time_hint : ""}`))}
    </div>`,
    data,
  );
}

function renderMemories(memories) {
  if (!memories || !memories.length) return `<p class="hint">未命中记忆</p>`;
  return memories
    .map((item) => {
      const epi = item.episodic || {};
      return `<article class="memory">
        <h3>${esc(item.ref)} · relevance ${Number(item.relevance || 0).toFixed(2)}</h3>
        <p><strong>客观事实</strong> ${esc(epi.objective_fact || item.semantic?.value || "—")}</p>
        <p><strong>角色解读</strong> ${esc(epi.persona_conditioned_insight || "—")}
          ${epi.insight_confidence != null ? `（${esc(epi.insight_confidence)}）` : ""}</p>
        ${chips(item.why_retrieved || [])}
      </article>`;
    })
    .join("");
}

function renderStateBuild(data) {
  const snap = data.snapshot || {};
  const persona = snap.persona?.identity || {};
  const rel = data.relationship || snap.relationship || {};
  const psych = (snap.psych && snap.psych.short) || {};
  return withRaw(
    `<div class="kv">
      <div class="kv-row"><span>persona</span><span>${esc(persona.name || "—")}</span></div>
      <div class="kv-row"><span>关系紧张</span><span>${esc(rel.recent_tension ?? "—")}</span></div>
      <div class="kv-row"><span>心理 short</span><span>${esc(psych.mood || psych.label || "—")}</span></div>
    </div>
    ${renderMemories(data.relevant_memories)}`,
    data,
  );
}

function renderGate(data) {
  const req = data.request || {};
  const cx = data.complexity || {};
  const path = req.path || "fast";
  return withRaw(
    `<div class="kv">
      <div class="kv-row"><span>路径</span><span class="badge ${path}">${esc(path)}</span></div>
      <div class="kv-row"><span>complexity</span></div>
      ${meter(req.score ?? cx.score)}
      ${chips(req.reasons || cx.reasons || [])}
    </div>`,
    data,
  );
}

function renderInterpret(data) {
  const intents = (data.possible_intents || [])
    .map((it) => `<div class="kv-row"><span>${esc(it.intent)}</span></div>${meter(it.confidence)}`)
    .join("");
  return withRaw(
    `<div class="kv">
      ${intents}
      <div class="kv-row"><span>recommended_stance</span><span>${esc(data.recommended_stance || "—")}</span></div>
      <div class="kv-row"><span>delta</span><span>${esc(data.relationship_delta_expected || "—")}</span></div>
      ${data.constraint ? `<span class="chip red">红线 ${esc(data.constraint)}</span>` : ""}
    </div>`,
    data,
  );
}

function renderCompile(data) {
  const budget = data.budget || {};
  const used = budget.used || 0;
  const total = budget.total || 1;
  const segs = (data.segments || [])
    .map(
      (seg) => `<article class="segment"><div class="kind">${esc(seg.kind)} · ${esc(seg.token_estimate)} tok</div>
        <div>${esc(seg.text)}</div></article>`,
    )
    .join("");
  return withRaw(
    `<div class="kv">
      <div class="kv-row"><span>token 预算</span><span>${esc(used)} / ${esc(total)}</span></div>
      ${meter(total ? used / total : 0)}
    </div>${segs}`,
    data,
  );
}

function usageOf(data, meta) {
  if (data?.usage) return data.usage;
  if (meta?.total_tokens == null) return null;
  return {
    prompt_tokens: meta.prompt_tokens || 0,
    completion_tokens: meta.completion_tokens || 0,
    total_tokens: meta.total_tokens || 0,
    source: meta.token_source,
  };
}

function tokenSourceLabel(source) {
  if (source === "api") return "API";
  if (source === "mixed") return "混合";
  return "估算";
}

function formatTokenLine(usage) {
  if (!usage) return "";
  const total = Number(usage.total_tokens || 0);
  const prompt = Number(usage.prompt_tokens || 0);
  const completion = Number(usage.completion_tokens || 0);
  const src = tokenSourceLabel(usage.source);
  return `${total} tok（in ${prompt} + out ${completion} · ${src}）`;
}

function formatLatencyMs(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return "";
  if (n >= 1000) return `${(n / 1000).toFixed(2)} s`;
  return `${n.toFixed(1)} ms`;
}

function sumGenerateUsage(events) {
  return (events || []).reduce(
    (sum, ev) => sum + Number(ev.data?.usage?.total_tokens || ev.meta?.total_tokens || 0),
    0,
  );
}

function renderGenerate(events) {
  const blocks = events
    .map((ev) => {
      const data = ev.data || {};
      const usage = usageOf(data, ev.meta);
      const revision = data.revision ?? ev.meta?.revision ?? 0;
      const tag = Number(revision) > 0 ? "validator 重生成" : "初稿";
      return `<div class="kv-row"><span>revision ${esc(revision)} · ${esc(tag)}</span>
        <span>${esc(ev.latency_ms)} ms</span></div>
        ${usage ? `<div class="kv-row"><span>tokens</span><span>${esc(formatTokenLine(usage))}</span></div>` : ""}
        <div class="quote">${esc(hideProtocolMarkers(data.text))}</div>`;
    })
    .join("");
  const total = events.reduce(
    (acc, ev) => {
      const usage = ev.data?.usage;
      if (!usage) return acc;
      acc.total += Number(usage.total_tokens || 0);
      acc.prompt += Number(usage.prompt_tokens || 0);
      acc.completion += Number(usage.completion_tokens || 0);
      acc.calls += Number(usage.calls || 1);
      acc.revisionTokens += Number(usage.revision_tokens || 0);
      acc.revisionCalls += Number(usage.revision_calls || 0);
      return acc;
    },
    { total: 0, prompt: 0, completion: 0, calls: 0, revisionTokens: 0, revisionCalls: 0 },
  );
  const summary = total.calls
    ? `<div class="kv token-summary">
        <div class="kv-row"><span>Character Model 合计</span><span>${esc(total.total)} tok · ${esc(total.calls)} 次</span></div>
        <div class="kv-row"><span>prompt / completion</span><span>${esc(total.prompt)} / ${esc(total.completion)}</span></div>
        <div class="kv-row"><span>Validator 打回重生成</span><span>${esc(total.revisionTokens)} tok · ${esc(total.revisionCalls)} 次</span></div>
      </div>`
    : "";
  return withRaw(`${summary}${blocks}`, events.map((ev) => ev.data));
}

function renderValidate(events) {
  const blocks = events
    .map((ev) => {
      const data = ev.data || {};
      if (data.skipped) return `<p class="hint">本轮采样跳过 Validator（Fast Path）</p>`;
      return `<div class="kv">
        <div class="kv-row"><span>verdict</span><span class="badge ${data.verdict === "pass" ? "fast" : "deep"}">${esc(data.verdict)}</span></div>
        <div class="kv-row"><span>severity</span></div>${meter(data.severity || 0)}
        ${chips(data.violations || [])}
        ${data.instruction ? `<p class="hint">${esc(data.instruction)}</p>` : ""}
      </div>`;
    })
    .join("");
  return withRaw(blocks, events.map((ev) => ev.data));
}

function renderFinal(data) {
  const segs = replySegmentsOf(data);
  const quotes = segs.length
    ? segs
        .map(
          (text, i) =>
            `<div class="quote"><small>段 ${i + 1}</small>${esc(text)}</div>`,
        )
        .join("")
    : `<div class="quote">${esc(hideProtocolMarkers(data.text))}</div>`;
  return withRaw(
    `<div class="kv">
      <div class="kv-row"><span>path</span><span class="badge ${data.path}">${esc(data.path)}</span></div>
      <div class="kv-row"><span>revisions</span><span>${esc(data.revisions)}</span></div>
      <div class="kv-row"><span>stopped_by</span><span>${esc(data.stopped_by || "—")}</span></div>
      ${data.latency_ms != null ? `<div class="kv-row"><span>合计耗时</span><span>${esc(formatLatencyMs(data.latency_ms))}</span></div>` : ""}
      ${data.usage ? `<div class="kv-row"><span>Character Model</span><span>${esc(formatTokenLine(data.usage))} · ${esc(data.usage.calls ?? 0)} 次</span></div>` : ""}
      ${data.usage?.revision_calls
        ? `<div class="kv-row"><span>Validator 重生成</span><span>${esc(data.usage.revision_tokens)} tok · ${esc(data.usage.revision_calls)} 次</span></div>`
        : ""}
      ${quotes}
    </div>`,
    data,
  );
}

function renderStageBody(stage) {
  if (stage.status === "skipped") {
    return `<p class="hint">Fast Path 未触发此阶段</p>`;
  }
  if (!stage.events.length) return `<p class="hint">等待输出…</p>`;
  const latest = stage.events[stage.events.length - 1];
  switch (stage.id) {
    case "ingest":
      return renderIngest(latest.data);
    case "parse":
      return renderParse(latest.data);
    case "state_build":
      return renderStateBuild(latest.data);
    case "gate":
      return renderGate(latest.data);
    case "interpret":
      return renderInterpret(latest.data);
    case "compile":
      return renderCompile(latest.data);
    case "generate":
      return renderGenerate(stage.events);
    case "validate":
      return renderValidate(stage.events);
    case "final":
      return renderFinal(latest.data);
    default:
      return jsonBlock(latest.data);
  }
}

function renderTimeline() {
  const root = $("timeline");
  root.innerHTML = state.stages
    .map((stage, i) => {
      const open = state.expanded === stage.id ? "open" : "";
      const latency = stage.events.length
        ? `${stage.latency.toFixed(1)} ms${stage.events.length > 1 ? ` ×${stage.events.length}` : ""}`
        : "";
      const tokens =
        stage.id === "generate" && stage.events.length ? `${sumGenerateUsage(stage.events)} tok` : "";
      const flags = [latency, tokens].filter(Boolean).join(" · ");
      return `<li class="stage ${stage.status} ${open}" data-stage="${stage.id}">
        <button type="button" class="stage-head">
          <span class="idx">${i + 1}</span>
          <span class="stage-title"><strong>${esc(stage.title)}</strong><small>${esc(stage.sub)}</small></span>
          <span class="stage-flags">${esc(flags)}</span>
        </button>
        <div class="stage-body">${renderStageBody(stage)}</div>
      </li>`;
    })
    .join("");
  root.querySelectorAll(".stage-head").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.parentElement.dataset.stage;
      state.expanded = state.expanded === id ? null : id;
      renderTimeline();
    });
  });
  root.querySelectorAll(".raw-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const node = document.getElementById(btn.dataset.raw);
      if (node) node.hidden = !node.hidden;
    });
  });
}

function speakerLabel(msg) {
  if (msg?.speaker) return msg.speaker;
  if (msg?.role === "character") return state.speakers.character_name || "character";
  return state.speakers.user_name || "用户";
}

function fillIfIdle(id, value) {
  const node = $(id);
  if (!node || document.activeElement === node) return;
  node.value = value ?? "";
}

function syncSpeakerFields(snap) {
  const speakers = snap?.speakers || {};
  state.speakers = {
    user_name: speakers.user_name || "用户",
    character_name: speakers.character_name || "",
  };
  fillIfIdle("user-name", state.speakers.user_name);
  fillIfIdle("character-name", state.speakers.character_name);
  fillIfIdle("cfg-user-name", state.speakers.user_name);
  fillIfIdle("cfg-character-name", state.speakers.character_name);
}

function renderTranscript() {
  const root = $("transcript");
  if (!state.messages.length) {
    root.innerHTML = `<div class="empty-chat">还没有对话。发送一句，观察九阶段如何装配语境。</div>`;
    return;
  }
  root.innerHTML = state.messages
    .map((msg) => {
      if (msg.pending) {
        return `<div class="bubble character pending pulse"><div class="who">${esc(speakerLabel({ role: "character" }))}</div>推理中</div>`;
      }
      const meta =
        msg.role === "character" && (msg.path || msg.usage)
          ? `<div class="meta">${msg.path ? `<span class="badge ${msg.path}">${esc(msg.path)}</span>` : ""}
             <span class="badge ghost">rev ${esc(msg.revisions ?? 0)}</span>
             ${msg.usage?.total_tokens != null ? `<span class="badge ghost">${esc(msg.usage.total_tokens)} tok</span>` : ""}</div>`
          : "";
      const body = msg.role === "character" ? hideProtocolMarkers(msg.text) : msg.text;
      return `<div class="bubble ${msg.role}"><div class="who">${esc(speakerLabel(msg))}</div>${esc(body)}${meta}</div>`;
    })
    .join("");
  root.scrollTop = root.scrollHeight;
}

function renderHomeostasisCards(home) {
  if (!home?.dimensions) {
    return `<article class="card"><h2>Homeostasis</h2><p class="hint">内稳态服务尚未提供状态</p></article>`;
  }
  const factors = Object.entries(home.dimensions)
    .map(([name, factor]) => {
      const value = Number(factor.value || 0);
      const low = Number(factor.target_band?.[0] || 0);
      const high = Number(factor.target_band?.[1] || 0);
      const posterior = factor.posterior || {};
      return `<div class="homeo-factor">
        <div class="homeo-factor-head"><strong>${esc(name)}</strong><b>${value.toFixed(3)}</b></div>
        <div class="homeo-track">
          <i class="homeo-target" style="left:${Math.round(low * 100)}%;width:${Math.max(1, Math.round((high - low) * 100))}%"></i>
          <i class="homeo-value" style="left:${Math.round(value * 100)}%"></i>
        </div>
        <div class="homeo-meta">
          <span>Δ ${Number(factor.velocity || 0).toFixed(3)}</span>
          <span>P ${Number(factor.precision || 0).toFixed(2)}</span>
          <span>L/I/H ${Number(posterior.low || 0).toFixed(2)} / ${Number(posterior.in_band || 0).toFixed(2)} / ${Number(posterior.high || 0).toFixed(2)}</span>
        </div>
      </div>`;
    })
    .join("");
  const affect = home.core_affect || {};
  const emotion = home.emotion || {};
  const candidates = (home.candidates || []).map((item) => `${item.rank}. ${item.label} (${Number(item.efe?.total || 0).toFixed(3)})`);
  return `<article class="card homeostasis-card">
    <h2>Homeostasis <span class="badge">rev ${esc(home.revision)}</span></h2>
    <p class="hint">${esc(home.profile_id)}@${esc(home.profile_revision)} · ${esc(home.updated_at)}</p>
    ${factors}
    <div class="kv homeo-affect">
      <div class="kv-row"><span>affect</span><span>V ${Number(affect.valence || 0).toFixed(2)} · A ${Number(affect.arousal || 0).toFixed(2)} · D ${Number(affect.dominance || 0).toFixed(2)}</span></div>
      <div class="kv-row"><span>emotion</span><span>${esc(emotion.label || "—")} · ${Number(emotion.intensity || 0).toFixed(2)}</span></div>
      <div class="kv-row"><span>heartbeat</span><span>${esc(home.last_heartbeat_at || "尚未触发")}</span></div>
    </div>
    ${candidates.length ? `<p class="hint">候选：${esc(candidates.join(" · "))}</p>` : ""}
    ${(home.watchdog || []).length ? `<p class="homeo-warning">watchdog: ${esc(home.watchdog.join(", "))}</p>` : ""}
  </article>`;
}

function renderSubsystems(snap) {
  const root = $("subsystems");
  if (!snap) {
    root.innerHTML = `<div class="card"><p class="hint">等待状态快照</p></div>`;
    return;
  }
  const persona = snap.persona?.identity || {};
  const rel = snap.relationship || {};
  const psych = snap.psych || {};
  const world = snap.world || {};
  const counts = snap.counts || {};
  const llm = snap.llm || {};
  const runtime = snap.runtime || {};
  const home = snap.homeostasis || {};
  const manifest = snap.manifest || {};
  const services = snap.services || {};
  $("mode-badge").textContent = llm.mode || snap.mode || "demo";
  const llmBadge = $("llm-badge");
  llmBadge.textContent = `${llm.model || "?"} · ${llm.reachable ? "可达" : "离线"}`;
  llmBadge.className = `badge ${llm.reachable ? "ok" : "bad"}`;
  const aux = llm.aux || {};
  const auxBadge = $("aux-badge");
  if (auxBadge) {
    if (aux.active) {
      const bits = [aux.model || "aux"];
      bits.push(aux.reachable ? "可达" : "离线");
      if (aux.preloaded) bits.push("已预载");
      else if (aux.reachable) bits.push("预载中");
      auxBadge.textContent = bits.join(" · ");
      auxBadge.className = `badge ${aux.preloaded ? "ok" : aux.reachable ? "ghost" : "bad"}`;
    } else {
      auxBadge.textContent = "辅助 · 未启用";
      auxBadge.className = "badge ghost";
    }
  }
  const embed = llm.embedding || {};
  const embedBadge = $("embed-badge");
  if (embedBadge) {
    if (embed.backend === "openai") {
      embedBadge.textContent = `${embed.model || "embedding"} · 可达`;
      embedBadge.className = "badge ok";
    } else if ((llm.mode || snap.mode) === "demo") {
      embedBadge.textContent = "向量 · mock";
      embedBadge.className = "badge ghost";
    } else {
      embedBadge.textContent = "向量 · 离线(mock)";
      embedBadge.className = "badge bad";
    }
  }
  if (!$("settings-dialog")?.open) {
    fillSettingsForm(llm, aux);
  }
  const runtimeBadge = $("runtime-badge");
  if (runtimeBadge) {
    runtimeBadge.textContent = `Runtime · ${runtime.lifecycle || "legacy"}`;
    runtimeBadge.className = `badge ${String(runtime.lifecycle || "").toLowerCase() === "running" ? "ok" : runtime.lifecycle ? "bad" : "ghost"}`;
  }
  const guiBadge = $("gui-badge");
  if (guiBadge) {
    guiBadge.textContent = services.gui?.healthy ? "GUI · 在线" : "GUI · legacy";
    guiBadge.className = `badge ${services.gui?.healthy ? "ok" : "ghost"}`;
  }
  const cognitiveBadge = $("cognitive-badge");
  if (cognitiveBadge) {
    const cognitiveRevision = services.cognitive?.revision;
    const cognitiveHealthy = runtime.health?.cognitive_healthy !== false && cognitiveRevision != null;
    cognitiveBadge.textContent = cognitiveRevision != null ? `Cognitive · r${cognitiveRevision}` : "Cognitive · 未接入";
    cognitiveBadge.className = `badge ${cognitiveHealthy ? "ok" : cognitiveRevision != null ? "bad" : "ghost"}`;
  }
  const homeBadge = $("homeostasis-badge");
  if (homeBadge) {
    const homeHealthy = runtime.health?.homeostasis_healthy !== false && !(home.watchdog || []).length;
    homeBadge.textContent = home.profile_id ? `内稳态 · r${home.revision}` : "内稳态 · 未接入";
    homeBadge.className = `badge ${home.profile_id && homeHealthy ? "ok" : home.profile_id ? "bad" : "ghost"}`;
  }
  const controlBadge = $("control-badge");
  if (controlBadge) {
    controlBadge.textContent = `模式 · ${manifest.control_mode || "legacy"}`;
    controlBadge.className = `badge ${manifest.control_mode === "study_runtime" ? "bad" : "ghost"}`;
  }
  const homeButton = $("btn-homeostasis");
  if (homeButton) homeButton.disabled = !home.profile;

  const embodiment = runtime.embodiment || runtime.health?.embodiment;
  const embodimentCard = embodiment
    ? `<article class="card">
        <h2>Embodiment <span class="badge ${["available", "disabled"].includes(embodiment.state) ? "ok" : "bad"}">${esc(embodiment.state)}</span></h2>
        <div class="kv">
          <div class="kv-row"><span>provider</span><code>${esc(embodiment.provider_id || "—")}</code></div>
          <div class="kv-row"><span>body</span><code>${esc(embodiment.body_instance_id || "—")}</code></div>
          <div class="kv-row"><span>effective</span><span>${esc((embodiment.effective_capabilities || []).join(", ") || "—")}</span></div>
          <div class="kv-row"><span>reason</span><span>${esc(embodiment.reason || "—")}</span></div>
        </div>
      </article>`
    : "";

  root.innerHTML = `
    ${renderHomeostasisCards(home)}
    ${embodimentCard}
    <article class="card">
      <h2>Persona Core <span class="badge">${persona.locked ? "LOCKED" : "open"}</span></h2>
      <p><strong>${esc(persona.name || "—")}</strong></p>
      ${chips(persona.core_traits)}
      ${chips(persona.speech_style)}
    </article>
    <article class="card">
      <h2>Relationship</h2>
      ${["closeness", "trust", "attachment", "recent_tension"]
        .map((key) => {
          const pct = Math.round((rel[key] || 0) * 100);
          return `<div class="bar-row"><span>${key}</span><div class="meter"><i style="width:${pct}%"></i></div><b>${pct}</b></div>`;
        })
        .join("")}
      <p class="hint">last: ${esc(rel.last_major_event || "—")}</p>
    </article>
    <article class="card">
      <h2>Psych</h2>
      <p class="hint">short ${esc(JSON.stringify(psych.short || {}))}</p>
      <p class="hint">mid ${esc(JSON.stringify(psych.mid || {}))}</p>
      <p class="hint">long ${esc(JSON.stringify(psych.long || {}))}</p>
    </article>
    <article class="card">
      <h2>World</h2>
      <div class="kv">
        <div class="kv-row"><span>channel</span><span>${esc(world.channel)}</span></div>
        <div class="kv-row"><span>presence</span><span>${esc(world.user_presence)}</span></div>
        <div class="kv-row"><span>app</span><span>${esc(world.active_application || "—")}</span></div>
      </div>
    </article>
    <article class="card">
      <h2>Memory stores</h2>
      <div class="stat-grid">
        <div class="stat"><b>${esc(counts.episodic)}</b><span>episodic</span></div>
        <div class="stat"><b>${esc(counts.semantic)}</b><span>semantic</span></div>
        <div class="stat"><b>${esc(counts.candidates)}</b><span>candidates</span></div>
        <div class="stat"><b>${esc(counts.audit)}</b><span>audit log</span></div>
      </div>
    </article>`;
}

function turnsFromSnapshot(snap) {
  return (snap?.session?.turns || []).map((turn) => ({
    role: turn.role,
    text: turn.role === "character" ? hideProtocolMarkers(turn.text) : turn.text,
    speaker: turn.speaker,
  }));
}

function applySnapshot(snap, opts = {}) {
  const nextId = snap?.presets?.active_id || snap?.session?.preset_id || state.activePresetId;
  const presetChanged = Boolean(nextId) && nextId !== state.activePresetId;
  const replaceTranscript = Boolean(opts.replaceTranscript) || presetChanged || !state.messages.length;
  state.snapshot = snap;
  if (nextId) state.activePresetId = nextId;
  syncSpeakerFields(snap);
  if (replaceTranscript) {
    state.messages = turnsFromSnapshot(snap);
  }
  renderChatTabs(snap);
  renderSubsystems(snap);
  renderTranscript();
  if ($("presets-dialog")?.open) renderPresetList(snap);
}

function beginRun() {
  state.stages = resetStages();
  state.stages[0].status = "running";
  state.expanded = "ingest";
  $("run-meta").textContent = "运行中…";
  renderTimeline();
}

function lastUserIndex() {
  for (let i = state.messages.length - 1; i >= 0; i -= 1) {
    if (state.messages[i].role === "user") return i;
  }
  return -1;
}

function commitCharacterSegments(segs, extra) {
  const start = lastUserIndex() + 1;
  const base = {
    role: "character",
    pending: false,
    speaker: state.speakers.character_name,
    ...extra,
  };
  state.messages.splice(
    start,
    state.messages.length - start,
    ...segs.map((text) => ({ ...base, text })),
  );
}

function applyReplySegment(msg) {
  const text = hideProtocolMarkers(msg.data?.text || "");
  if (!text) return;
  const pendingIdx = state.messages.findIndex((item) => item.pending);
  const bubble = {
    role: "character",
    text,
    speaker: state.speakers.character_name,
  };
  if (pendingIdx >= 0) {
    state.messages.splice(pendingIdx, 0, bubble);
  } else {
    state.messages.push(bubble);
  }
  renderTranscript();
}

function applyStageEvent(msg) {
  if (msg.stage === "reply_segment") {
    applyReplySegment(msg);
    return;
  }
  const idx = state.stages.findIndex((item) => item.id === msg.stage);
  if (idx < 0) return;
  for (let i = 0; i < idx; i += 1) {
    const prev = state.stages[i];
    if (!prev.events.length && prev.id === "interpret") prev.status = "skipped";
  }
  const stage = state.stages[idx];
  stage.status = "done";
  stage.events.push(msg);
  stage.latency += Number(msg.latency_ms || 0);
  state.expanded = msg.stage;
  const next = state.stages[idx + 1];
  if (next && next.status === "idle") next.status = "running";
  if (msg.stage === "final") {
    const interpret = state.stages.find((item) => item.id === "interpret");
    if (interpret && !interpret.events.length) interpret.status = "skipped";
    const segs = replySegmentsOf(msg.data);
    commitCharacterSegments(segs, {
      path: msg.data?.path,
      revisions: msg.data?.revisions,
      usage: msg.data?.usage,
    });
    const stopped = msg.data?.stopped_by ? ` · ${msg.data.stopped_by}` : "";
    const usage = msg.data?.usage;
    const tok = usage?.total_tokens != null ? ` · ${usage.total_tokens} tok` : "";
    const revTok =
      usage?.revision_tokens && usage.revision_calls
        ? ` · 重生成 ${usage.revision_tokens} tok`
        : "";
    const elapsed = formatLatencyMs(msg.data?.latency_ms);
    const elapsedPart = elapsed ? ` · ${elapsed}` : "";
    $("run-meta").textContent = `${msg.data?.path || "?"} path · ${msg.data?.revisions ?? 0} revisions${tok}${revTok}${elapsedPart}${stopped}`;
    renderTranscript();
  }
  renderTimeline();
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  ws.onopen = () => {
    $("ws-dot").className = "dot on";
  };
  ws.onclose = () => {
    $("ws-dot").className = "dot off";
    setTimeout(connectWs, 1500);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "ping") return;
    if (msg.type === "stage" || msg.type === "final") applyStageEvent(msg);
    if (msg.type === "state") applySnapshot(msg.data);
    if (msg.type === "error") {
      const pending = state.messages.find((item) => item.pending);
      if (pending) {
        pending.pending = false;
        pending.text = `错误：${msg.data?.message || "unknown"}`;
      }
      $("run-meta").textContent = "失败";
      renderTranscript();
    }
  };
}

async function fetchStatus(opts = {}) {
  const res = await fetch("/api/status");
  applySnapshot(await res.json(), opts);
}

async function sendChat(event) {
  event.preventDefault();
  if (state.busy) return;
  const text = $("input").value.trim();
  if (!text) return;
  state.busy = true;
  $("btn-send").disabled = true;
  $("input").value = "";
  state.messages.push({ role: "user", text, speaker: state.speakers.user_name });
  state.messages.push({ role: "character", text: "", pending: true, speaker: state.speakers.character_name });
  renderTranscript();
  beginRun();
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const body = await res.json();
    state.runId = body.run_id;
    if (!res.ok) {
      const detail = body.detail || body.error || res.statusText;
      throw new Error(typeof detail === "string" ? detail : res.statusText);
    }
  } catch (err) {
    const pending = state.messages.find((item) => item.pending);
    if (pending) {
      pending.pending = false;
      pending.text = `错误：${err.message}`;
    }
    $("run-meta").textContent = "失败";
    renderTranscript();
  } finally {
    state.busy = false;
    $("btn-send").disabled = false;
  }
}

$("composer").addEventListener("submit", sendChat);
$("input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});

const OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1";
const LIVE_BASE_URL = "http://127.0.0.1:8080/v1";
let modelsSeq = 0;

function looksLikeOllamaUrl(url) {
  return String(url || "").includes(":11434");
}

function fillDatalist(id, models) {
  const node = $(id);
  if (!node) return;
  node.innerHTML = (models || []).map((name) => `<option value="${esc(name)}"></option>`).join("");
}

function syncModelTabActive(containerId, value) {
  const root = $(containerId);
  if (!root) return;
  const current = String(value || "").trim();
  root.querySelectorAll("[data-model]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.model === current);
  });
}

function renderModelTabs(containerId, inputId, models) {
  const root = $(containerId);
  const input = $(inputId);
  if (!root) return;
  const names = models || [];
  if (!names.length) {
    root.hidden = true;
    root.innerHTML = "";
    return;
  }
  const current = (input?.value || "").trim();
  root.hidden = false;
  root.innerHTML = names
    .map((name) => {
      const active = name === current ? " active" : "";
      return `<button type="button" class="chat-tab${active}" data-model="${esc(name)}">${esc(name)}</button>`;
    })
    .join("");
  root.querySelectorAll("[data-model]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (input) input.value = btn.dataset.model || "";
      syncModelTabActive(containerId, input?.value);
    });
  });
}

function clearModelChoices() {
  fillDatalist("cfg-model-list", []);
  fillDatalist("cfg-aux-model-list", []);
  renderModelTabs("cfg-model-tabs", "cfg-model", []);
  renderModelTabs("cfg-aux-model-tabs", "cfg-aux-model", []);
  const mainStatus = $("cfg-model-status");
  const auxStatus = $("cfg-aux-model-status");
  if (mainStatus) mainStatus.textContent = "";
  if (auxStatus) auxStatus.textContent = "";
}

async function fetchEndpointModels(baseUrl) {
  const url = String(baseUrl || "").trim();
  if (!url) return [];
  const res = await fetch(`/api/models?base_url=${encodeURIComponent(url)}`);
  const body = await res.json().catch(() => ({}));
  return Array.isArray(body.models) ? body.models : [];
}

async function refreshEndpointModels() {
  const seq = ++modelsSeq;
  const mode = $("cfg-mode")?.value || "demo";
  const mainStatus = $("cfg-model-status");
  const auxStatus = $("cfg-aux-model-status");
  if (mode === "demo") {
    clearModelChoices();
    return;
  }
  const mainUrl = $("cfg-url")?.value.trim() || "";
  const auxUrl = $("cfg-aux-url")?.value.trim() || mainUrl;
  if (mainStatus) mainStatus.textContent = mainUrl ? "正在查询已安装模型…" : "填写 Base URL 后查询模型";
  if (auxStatus) auxStatus.textContent = "";
  let mainModels = [];
  try {
    mainModels = await fetchEndpointModels(mainUrl);
  } catch {
    mainModels = [];
  }
  if (seq !== modelsSeq) return;
  fillDatalist("cfg-model-list", mainModels);
  renderModelTabs("cfg-model-tabs", "cfg-model", mainModels);
  if (mainStatus) {
    mainStatus.textContent = mainModels.length
      ? `已发现 ${mainModels.length} 个模型，点击选项卡填入`
      : mainUrl
        ? "未查询到模型，可手动填写"
        : "填写 Base URL 后查询模型";
  }
  let auxModels = mainModels;
  if (auxUrl && auxUrl !== mainUrl) {
    if (auxStatus) auxStatus.textContent = "正在查询已安装模型…";
    try {
      auxModels = await fetchEndpointModels(auxUrl);
    } catch {
      auxModels = [];
    }
    if (seq !== modelsSeq) return;
  }
  fillDatalist("cfg-aux-model-list", auxModels);
  renderModelTabs("cfg-aux-model-tabs", "cfg-aux-model", auxModels);
  if (auxStatus) {
    auxStatus.textContent = auxModels.length
      ? `已发现 ${auxModels.length} 个模型，点击选项卡填入`
      : auxUrl
        ? "未查询到模型，可手动填写"
        : "";
  }
}

function applyModeDefaults(mode) {
  const url = $("cfg-url");
  const key = $("cfg-key");
  if (mode === "ollama") {
    if (url && !looksLikeOllamaUrl(url.value)) url.value = OLLAMA_BASE_URL;
    if (url) url.placeholder = OLLAMA_BASE_URL;
    if (key) key.placeholder = "本地 ollama 通常可留空";
  } else if (mode === "live") {
    if (url) url.placeholder = LIVE_BASE_URL;
    if (key) key.placeholder = "sk-…（live 模式需要，可留空沿用）";
  } else if (key) {
    key.placeholder = "演示模式不需要";
  }
}

function fillSettingsForm(llm, aux) {
  const mode = llm.mode || "demo";
  const modeEl = $("cfg-mode");
  if (modeEl) modeEl.value = mode;
  const urlEl = $("cfg-url");
  if (urlEl) urlEl.value = llm.base_url || "";
  const modelEl = $("cfg-model");
  if (modelEl) modelEl.value = llm.model || "";
  applyModeDefaults(mode);
  const key = $("cfg-key");
  if (key && mode !== "ollama") {
    key.placeholder = llm.api_key_configured ? "已配置，留空则保留" : "sk-…（live 模式需要）";
  }
  const auxUrl = $("cfg-aux-url");
  const auxModel = $("cfg-aux-model");
  const auxKey = $("cfg-aux-key");
  if (auxUrl) auxUrl.value = aux?.active ? (aux.base_url || "") : "";
  if (auxModel) auxModel.value = aux?.active ? (aux.model || "") : "";
  if (auxKey) auxKey.placeholder = aux?.active ? "已配置时可留空保留" : "可留空";
}

$("btn-settings").addEventListener("click", () => {
  const llm = state.snapshot?.llm || {};
  fillSettingsForm(llm, llm.aux || {});
  $("settings-dialog").showModal();
  refreshEndpointModels();
});
$("cfg-mode")?.addEventListener("change", () => {
  applyModeDefaults($("cfg-mode").value);
  refreshEndpointModels();
});
$("cfg-url")?.addEventListener("change", () => refreshEndpointModels());
$("cfg-aux-url")?.addEventListener("change", () => refreshEndpointModels());
$("cfg-model")?.addEventListener("input", () => syncModelTabActive("cfg-model-tabs", $("cfg-model").value));
$("cfg-aux-model")?.addEventListener("input", () => {
  syncModelTabActive("cfg-aux-model-tabs", $("cfg-aux-model").value);
});
$("btn-presets")?.addEventListener("click", () => openPresetDialog());
$("btn-presets-close")?.addEventListener("click", () => $("presets-dialog")?.close());

function renderChatTabs(snap) {
  const root = $("chat-tabs");
  if (!root) return;
  const presets = snap?.presets || {};
  const items = presets.items || [];
  const openIds = presets.open_ids || [];
  const active = presets.active_id;
  const byId = Object.fromEntries(items.map((item) => [item.preset_id, item]));
  const tabs = openIds.map((id) => {
    const item = byId[id] || { preset_id: id, name: id };
    const cls = id === active ? "chat-tab active" : "chat-tab";
    return `<button type="button" class="${cls}" data-preset-open="${esc(id)}">${esc(item.name || id)}</button>`;
  });
  tabs.push(`<button type="button" class="chat-tab add" id="btn-tab-add" title="预设库">+</button>`);
  root.innerHTML = tabs.join("");
  root.querySelectorAll("[data-preset-open]").forEach((btn) => {
    btn.addEventListener("click", () => switchPreset(btn.dataset.presetOpen));
  });
  $("btn-tab-add")?.addEventListener("click", () => openPresetDialog());
}

function renderPresetList(snap) {
  const root = $("preset-list");
  if (!root) return;
  const items = snap?.presets?.items || [];
  root.innerHTML =
    items
      .map((item) => {
        const bits = [];
        if (item.builtin) bits.push("出厂");
        if (item.open) bits.push("已打开");
        if (item.active) bits.push("当前");
        bits.push(`${item.turn_count || 0} 条聊天`);
        const actions = [
          `<button type="button" class="btn ghost" data-act="open" data-id="${esc(item.preset_id)}">打开</button>`,
          `<button type="button" class="btn ghost" data-act="apply" data-id="${esc(item.preset_id)}">套用到当前</button>`,
          `<button type="button" class="btn ghost" data-act="new" data-id="${esc(item.preset_id)}">从模板新建</button>`,
        ];
        if (item.active && item.builtin_id) {
          actions.push(
            `<button type="button" class="btn ghost" data-act="restore" data-id="${esc(item.preset_id)}">恢复出厂</button>`,
          );
        }
        if (!item.protected) {
          actions.push(
            `<button type="button" class="btn ghost danger" data-act="delete" data-id="${esc(item.preset_id)}">删除</button>`,
          );
        }
        return `<article class="preset-row">
      <div>
        <h3>${esc(item.name || item.preset_id)}</h3>
        <div class="meta">${esc(item.preset_id)} · ${esc(bits.join(" · "))}</div>
      </div>
      <div class="preset-actions">${actions.join("")}</div>
    </article>`;
      })
      .join("") || `<p class="hint">没有预设</p>`;
  root.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", () => presetAction(btn.dataset.act, btn.dataset.id));
  });
}

async function openPresetDialog() {
  await fetchStatus();
  renderPresetList(state.snapshot);
  $("presets-dialog")?.showModal();
}

async function switchPreset(presetId) {
  if (!presetId || presetId === state.activePresetId || state.busy) return;
  await presetAction("open", presetId);
}

async function presetAction(act, presetId) {
  const headers = { "Content-Type": "application/json" };
  let res;
  if (act === "open") {
    res = await fetch("/api/presets/open", {
      method: "POST",
      headers,
      body: JSON.stringify({ preset_id: presetId }),
    });
  } else if (act === "apply") {
    res = await fetch("/api/presets/apply", {
      method: "POST",
      headers,
      body: JSON.stringify({ preset_id: presetId }),
    });
  } else if (act === "new") {
    res = await fetch("/api/presets/new", {
      method: "POST",
      headers,
      body: JSON.stringify({ from_id: presetId }),
    });
  } else if (act === "restore") {
    res = await fetch("/api/presets/restore", { method: "POST" });
  } else if (act === "delete") {
    if (!window.confirm("删除该用户预设？出厂模板无法删除。")) return;
    res = await fetch(`/api/presets/${encodeURIComponent(presetId)}`, { method: "DELETE" });
  } else {
    return;
  }
  const body = await res.json();
  if (!res.ok) {
    window.alert(typeof body.detail === "string" ? body.detail : "预设操作失败");
    return;
  }
  applySnapshot(body, { replaceTranscript: true });
}

async function saveSpeakers() {
  const payload = { user_name: $("user-name").value.trim() };
  const characterName = $("character-name").value.trim();
  if (characterName) payload.character_name = characterName;
  const res = await fetch("/api/speakers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json();
  if (!res.ok) {
    window.alert(typeof body.detail === "string" ? body.detail : "保存名称失败");
    return;
  }
  applySnapshot(body);
}
["user-name", "character-name"].forEach((id) => {
  $(id)?.addEventListener("change", saveSpeakers);
});
$("settings-form").addEventListener("submit", async (event) => {
  const value = event.submitter?.value;
  if (value !== "save") return;
  event.preventDefault();
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      mode: $("cfg-mode").value,
      base_url: $("cfg-url").value.trim() || null,
      model: $("cfg-model").value.trim() || null,
      api_key: $("cfg-key").value.trim() || null,
      aux_base_url: $("cfg-aux-url").value.trim() || null,
      aux_model: $("cfg-aux-model").value.trim() || null,
      aux_api_key: $("cfg-aux-key").value.trim() || null,
      user_name: $("cfg-user-name").value.trim() || null,
      character_name: $("cfg-character-name").value.trim() || null,
    }),
  });
    const body = await res.json();
    if (!res.ok) {
      window.alert(typeof body.detail === "string" ? body.detail : "保存失败");
      return;
    }
    applySnapshot(body, { replaceTranscript: true });
    $("cfg-key").value = "";
    $("cfg-aux-key").value = "";
    $("settings-dialog").close();
    if (body.llm?.aux?.active && !body.llm.aux.preloaded) {
      [2000, 8000, 20000].forEach((ms) => setTimeout(() => fetchStatus(), ms));
    }
});

$("btn-reset").addEventListener("click", async () => {
  const res = await fetch("/api/reset", { method: "POST" });
  state.messages = [];
  state.stages = resetStages();
  state.expanded = null;
  $("run-meta").textContent = "已重置";
  applySnapshot(await res.json());
  renderTimeline();
});

renderTimeline();
connectWs();
fetchStatus();

const WEIGHT_KEYS = ["semantic", "entity", "recency", "importance", "relationship", "persona"];
const REL_KEYS = ["closeness", "trust", "attachment", "recent_tension"];
const PSYCH_SKIP = new Set(["set_at", "expired"]);

function sliderRow(field, min, max, step, value) {
  const n = Number(value);
  const v = Number.isFinite(n) ? n : Number(min) || 0;
  return `<div class="slider-row">
    <span>${esc(field)}</span>
    <input type="range" min="${min}" max="${max}" step="${step}" value="${v}" data-field="${esc(field)}" />
    <b>${v.toFixed(2)}</b>
  </div>`;
}

function tagEditor(field, items) {
  const chips = (items || [])
    .map(
      (item, idx) =>
        `<span class="chip">${esc(item)}<button type="button" class="x" data-field="${esc(field)}" data-idx="${idx}" aria-label="删除">×</button></span>`,
    )
    .join("");
  return `<div class="tag-block">
    <span class="hint">${esc(field)}</span>
    <div class="tag-editor" data-field="${esc(field)}">
      ${chips}
      <input class="tag-input" data-field="${esc(field)}" placeholder="回车添加" />
    </div>
  </div>`;
}

function isUnitFloat(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function parseLooseValue(raw) {
  const text = String(raw).trim();
  if (!text) return "";
  if (text === "true") return true;
  if (text === "false") return false;
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
  return text;
}

function setDebugStatus(message, isError) {
  const node = $("debug-status");
  node.textContent = message;
  node.className = isError ? "hint err" : "hint";
}

function syncDebugSnapshot(data) {
  if (!state.snapshot || !data) return;
  state.snapshot.persona = data.persona;
  state.snapshot.relationship = data.relationship;
  state.snapshot.psych = data.psych;
  if (state.snapshot.counts) {
    state.snapshot.counts.episodic = (data.episodic || []).length;
    state.snapshot.counts.semantic = (data.semantic || []).length;
    state.snapshot.counts.candidates = (data.candidates || []).length;
  }
  renderSubsystems(state.snapshot);
}

async function loadDebugState() {
  const res = await fetch("/api/debug/state");
  const data = await res.json();
  if (!res.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : "加载失败");
  }
  state.debug.data = data;
  renderDebugBody();
}

async function debugPost(path, body, okMessage) {
  const res = await fetch(`/api/debug/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  state.debug.data = data;
  setDebugStatus(okMessage || "已保存");
  syncDebugSnapshot(data);
  renderDebugBody();
  return data;
}

function renderRelationshipTab(data) {
  const rel = data.relationship || {};
  return `<p class="hint">target · ${esc(rel.target || "user")}</p>
    ${REL_KEYS.map((key) => sliderRow(key, 0, 1, 0.01, rel[key] ?? 0)).join("")}`;
}

function renderPsychLayer(name, layer) {
  const entries = Object.entries(layer || {}).filter(([key]) => !PSYCH_SKIP.has(key));
  const rows = entries
    .map(([key, value]) => {
      const control = isUnitFloat(value)
        ? sliderRow(key, 0, 1, 0.01, value)
        : `<label>${esc(key)}<input data-text-key="${esc(key)}" value="${esc(value)}" /></label>`;
      return `<div class="key-row" data-key="${esc(key)}">${control}<button type="button" class="btn ghost danger" data-del-key="${esc(key)}">删除</button></div>`;
    })
    .join("");
  return `<article class="debug-item" data-layer="${esc(name)}">
    <h3>${esc(name)}</h3>
    ${rows || `<p class="hint">空</p>`}
  </article>`;
}

function renderPsychTab(data) {
  const psych = data.psych || { long: {}, mid: {}, short: {} };
  const shortMeta = [];
  if (psych.short?.set_at) shortMeta.push(`set_at ${psych.short.set_at}`);
  if (data.short_psych_expired) shortMeta.push("short 已过期");
  return `${renderPsychLayer("long", psych.long)}
    ${renderPsychLayer("mid", psych.mid)}
    ${renderPsychLayer("short", psych.short)}
    ${shortMeta.length ? `<p class="hint">${esc(shortMeta.join(" · "))}</p>` : ""}
    <div class="psych-add">
      <select id="psych-add-layer">
        <option value="long">long</option>
        <option value="mid">mid</option>
        <option value="short">short</option>
      </select>
      <input id="psych-add-key" placeholder="key" />
      <input id="psych-add-value" placeholder="value" />
      <button type="button" id="psych-add-btn" class="btn">添加</button>
    </div>`;
}

function renderPersonaTab(data) {
  const identity = data.persona?.identity || {};
  return `<p class="hint lock-note">LOCKED · debug 绕锁写入，审计标记 bypass=persona_lock</p>
    <label>name<input data-persona-name value="${esc(identity.name || "")}" /></label>
    ${tagEditor("core_traits", identity.core_traits)}
    ${tagEditor("values", identity.values)}
    ${tagEditor("speech_style", identity.speech_style)}
    ${tagEditor("knowledge_boundary", identity.knowledge_boundary)}`;
}

function renderEpisodicTab(data) {
  const events = data.episodic || [];
  const create = `<article class="debug-item debug-create" data-kind="episodic-create">
      <h3>新建事件</h3>
      <p class="hint">写入内存 episodic。勾选后会走 Dual Memory / Curator，与对话落库同一套管线。</p>
      <label>说话者
        <input data-field="speaker" list="speaker-presets" value="user" placeholder="user / character / 自定义名称" />
      </label>
      <datalist id="speaker-presets">
        <option value="user"></option>
        <option value="character"></option>
      </datalist>
      <label>event_type<input data-field="event_type" placeholder="如 commitment_or_statement" /></label>
      ${sliderRow("importance", 0, 1, 0.01, 0.5)}
      ${sliderRow("emotional_valence", -1, 1, 0.01, 0)}
      <label>summary<textarea data-field="summary" placeholder="发生了什么（必填）"></textarea></label>
      <label>objective_fact<textarea data-field="objective_fact" placeholder="默认与 summary 相同"></textarea></label>
      <label>character_action<textarea data-field="character_action"></textarea></label>
      <label>outcome<textarea data-field="outcome"></textarea></label>
      <label>insight<textarea data-field="insight" placeholder="留空并可勾选「生成解读」"></textarea></label>
      <div class="debug-checks">
        <label><input type="checkbox" data-field="regenerate_insight" checked /> 生成解读</label>
        <label><input type="checkbox" data-field="curate" /> 跑 Curator</label>
      </div>
      <div class="debug-actions">
        <button type="button" class="btn primary" data-create-episodic>创建</button>
      </div>
    </article>`;
  const cards = events
    .map((ev) => {
      const who = (ev.participants || []).join(", ") || "—";
      return `<article class="debug-item" data-kind="episodic" data-id="${esc(ev.event_id)}">
        <h3>${esc(ev.event_id)}<span class="meta">${esc(who)}</span></h3>
        <p class="hint">${esc(ev.time || "")} · ${esc(ev.session_id || "—")} · ${esc((ev.evidence || [])[0] || "no evidence")}</p>
        ${sliderRow("importance", 0, 1, 0.01, ev.importance)}
        ${sliderRow("emotional_valence", -1, 1, 0.01, ev.emotional_valence)}
        ${sliderRow("insight_confidence", 0, 1, 0.01, ev.insight_confidence ?? 0)}
        <label>event_type<input data-field="event_type" value="${esc(ev.event_type || "")}" placeholder="如 commitment_or_statement" /></label>
        <label>summary<textarea data-field="summary">${esc(ev.summary || "")}</textarea></label>
        <label>objective_fact<textarea data-field="objective_fact">${esc(ev.objective_fact || "")}</textarea></label>
        <label>character_action<textarea data-field="character_action">${esc(ev.character_action || "")}</textarea></label>
        <label>outcome<textarea data-field="outcome">${esc(ev.outcome || "")}</textarea></label>
        <label>insight<textarea data-field="insight">${esc(ev.persona_conditioned_insight || "")}</textarea></label>
        <div class="debug-actions">
          <button type="button" class="btn" data-regen="insight">重新生成解读</button>
          <button type="button" class="btn ghost" data-regen="curate">重新跑 Curator</button>
        </div>
      </article>`;
    })
    .join("");
  return `${create}${cards || `<p class="hint">暂无事件。可在上方新建，或先发一条对话。</p>`}`;
}

function renderSemanticTab(data) {
  const facts = data.semantic || [];
  if (!facts.length) return `<p class="hint">暂无语义事实。对话写入候选并固化后会出现在这里。</p>`;
  return facts
    .map(
      (fact) => `<article class="debug-item" data-kind="semantic" data-id="${esc(fact.fact_id)}">
        <h3>${esc(fact.fact_id)}</h3>
        <p class="hint">${esc(fact.subject)} · ${esc(fact.predicate)}</p>
        ${sliderRow("confidence", 0, 1, 0.01, fact.confidence)}
        <label>value<input data-field="value" value="${esc(fact.value || "")}" /></label>
      </article>`,
    )
    .join("");
}

function renderCandidatesTab(data) {
  const rows = data.candidates || [];
  if (!rows.length) return `<p class="hint">暂无候选记忆。</p>`;
  return rows
    .map(
      (row) => `<article class="debug-item" data-kind="candidate" data-id="${esc(row.candidate_id)}">
        <h3>${esc(row.candidate_id)}</h3>
        <p class="hint">${esc(row.claim || "")}</p>
        ${sliderRow("confidence", 0, 1, 0.01, row.confidence)}
        <label>status
          <select data-field="status">
            ${["candidate", "consolidated", "dropped"]
              .map((st) => `<option value="${st}" ${row.status === st ? "selected" : ""}>${st}</option>`)
              .join("")}
          </select>
        </label>
      </article>`,
    )
    .join("");
}

function renderTuningTab(data) {
  const tuning = data.tuning || {};
  const weights = tuning.retrieval_weights || {};
  const sum = Number(tuning.weight_sum ?? WEIGHT_KEYS.reduce((acc, key) => acc + Number(weights[key] || 0), 0));
  const ok = Math.abs(sum - 1) <= 0.01;
  return `<p class="hint">检索六权重 · 保存时自动归一化为 1.0</p>
    ${WEIGHT_KEYS.map((key) => sliderRow(key, 0, 1, 0.01, weights[key] ?? 0)).join("")}
    <p class="weight-sum ${ok ? "ok" : "bad"}" id="weight-sum">sum ${sum.toFixed(2)}</p>
    ${sliderRow("consolidation_threshold", 0, 1, 0.01, tuning.consolidation_threshold)}
    ${sliderRow("initial_candidate_confidence", 0, 1, 0.01, tuning.initial_candidate_confidence)}
    ${sliderRow("confidence_increment", 0, 1, 0.01, tuning.confidence_increment)}
    <label>retrieval_top_k<input type="number" data-int="retrieval_top_k" min="1" max="50" value="${esc(tuning.retrieval_top_k)}" /></label>
    <label>retrieval_pool_size<input type="number" data-int="retrieval_pool_size" min="1" max="500" value="${esc(tuning.retrieval_pool_size)}" /></label>
    <label>recency_half_life_hours<input type="number" data-float="recency_half_life_hours" min="0.1" max="720" step="0.5" value="${esc(tuning.recency_half_life_hours)}" /></label>`;
}

function renderDebugBody() {
  const root = $("debug-body");
  const data = state.debug.data;
  $("debug-tabs")
    .querySelectorAll("button")
    .forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === state.debug.tab));
  if (!data) {
    root.innerHTML = `<p class="hint">加载中…</p>`;
    return;
  }
  switch (state.debug.tab) {
    case "relationship":
      root.innerHTML = renderRelationshipTab(data);
      break;
    case "psych":
      root.innerHTML = renderPsychTab(data);
      break;
    case "persona":
      root.innerHTML = renderPersonaTab(data);
      break;
    case "episodic":
      root.innerHTML = renderEpisodicTab(data);
      break;
    case "semantic":
      root.innerHTML = renderSemanticTab(data);
      break;
    case "candidates":
      root.innerHTML = renderCandidatesTab(data);
      break;
    case "tuning":
      root.innerHTML = renderTuningTab(data);
      break;
    default:
      root.innerHTML = "";
  }
  bindDebugEvents(root);
}

function collectTags(field) {
  const identity = state.debug.data?.persona?.identity || {};
  return [...(identity[field] || [])];
}

function updateWeightSumDisplay(root) {
  const node = $("weight-sum");
  if (!node) return;
  let sum = 0;
  root.querySelectorAll("input[type=range][data-field]").forEach((input) => {
    if (WEIGHT_KEYS.includes(input.dataset.field)) sum += Number(input.value);
  });
  node.textContent = `sum ${sum.toFixed(2)}`;
  node.className = `weight-sum ${Math.abs(sum - 1) <= 0.01 ? "ok" : "bad"}`;
}

function collectTuningPayload(root) {
  const weights = {};
  WEIGHT_KEYS.forEach((key) => {
    const input = root.querySelector(`input[type=range][data-field="${key}"]`);
    weights[key] = Number(input?.value || 0);
  });
  const payload = { retrieval_weights: weights, normalize: true };
  root.querySelectorAll("input[type=range][data-field]").forEach((input) => {
    const field = input.dataset.field;
    if (!WEIGHT_KEYS.includes(field) && ["consolidation_threshold", "initial_candidate_confidence", "confidence_increment"].includes(field)) {
      payload[field] = Number(input.value);
    }
  });
  const topK = root.querySelector("[data-int=retrieval_top_k]");
  const pool = root.querySelector("[data-int=retrieval_pool_size]");
  const half = root.querySelector("[data-float=recency_half_life_hours]");
  if (topK) payload.retrieval_top_k = Number(topK.value);
  if (pool) payload.retrieval_pool_size = Number(pool.value);
  if (half) payload.recency_half_life_hours = Number(half.value);
  return payload;
}

function bindRangeEcho(root) {
  root.querySelectorAll("input[type=range]").forEach((input) => {
    input.addEventListener("input", () => {
      const display = input.parentElement.querySelector("b");
      if (display) display.textContent = Number(input.value).toFixed(2);
      if (state.debug.tab === "tuning") updateWeightSumDisplay(root);
    });
  });
}

function collectEpisodicFields(card) {
  const body = {};
  if (card.dataset.id) body.event_id = card.dataset.id;
  card.querySelectorAll("input[type=range][data-field]").forEach((el) => {
    body[el.dataset.field] = Number(el.value);
  });
  card.querySelectorAll("textarea[data-field], input:not([type=range])[data-field], select[data-field]").forEach((el) => {
    if (el.type === "checkbox") {
      body[el.dataset.field] = el.checked;
      return;
    }
    body[el.dataset.field] = el.value;
  });
  return body;
}

function bindDebugEvents(root) {
  bindRangeEcho(root);
  const save = (path, body, okMessage) =>
    debugPost(path, body, okMessage).catch((err) => setDebugStatus(err.message, true));

  if (state.debug.tab === "relationship") {
    root.querySelectorAll("input[type=range]").forEach((input) => {
      input.addEventListener("change", () => {
        const body = { target: state.debug.data?.relationship?.target || "user" };
        root.querySelectorAll("input[type=range]").forEach((el) => {
          body[el.dataset.field] = Number(el.value);
        });
        save("relationship", body);
      });
    });
  }

  if (state.debug.tab === "psych") {
    const layerOf = (node) => node.closest("[data-layer]")?.dataset.layer;
    const layerDict = (name) => {
      const source = state.debug.data?.psych?.[name] || {};
      const next = {};
      Object.entries(source).forEach(([key, value]) => {
        if (!PSYCH_SKIP.has(key)) next[key] = value;
      });
      return next;
    };
    root.querySelectorAll("article[data-layer] input[type=range]").forEach((input) => {
      input.addEventListener("change", () => {
        const layer = layerOf(input);
        const next = layerDict(layer);
        next[input.dataset.field] = Number(input.value);
        save("psych", { [layer]: next });
      });
    });
    root.querySelectorAll("article[data-layer] input[data-text-key]").forEach((input) => {
      input.addEventListener("change", () => {
        const layer = layerOf(input);
        const next = layerDict(layer);
        next[input.dataset.textKey] = parseLooseValue(input.value);
        save("psych", { [layer]: next });
      });
    });
    root.querySelectorAll("[data-del-key]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const layer = layerOf(btn);
        const next = layerDict(layer);
        delete next[btn.dataset.delKey];
        save("psych", { [layer]: next });
      });
    });
    $("psych-add-btn")?.addEventListener("click", () => {
      const layer = $("psych-add-layer").value;
      const key = $("psych-add-key").value.trim();
      if (!key) return;
      const next = layerDict(layer);
      next[key] = parseLooseValue($("psych-add-value").value);
      save("psych", { [layer]: next });
    });
  }

  if (state.debug.tab === "persona") {
    const nameInput = root.querySelector("[data-persona-name]");
    nameInput?.addEventListener("change", () => save("persona", { name: nameInput.value.trim() }));
    root.querySelectorAll(".tag-editor .x").forEach((btn) => {
      btn.addEventListener("click", () => {
        const field = btn.dataset.field;
        const tags = collectTags(field);
        tags.splice(Number(btn.dataset.idx), 1);
        save("persona", { [field]: tags });
      });
    });
    root.querySelectorAll(".tag-input").forEach((input) => {
      input.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        const value = input.value.trim();
        if (!value) return;
        const field = input.dataset.field;
        save("persona", { [field]: [...collectTags(field), value] });
      });
    });
  }

  if (state.debug.tab === "episodic") {
    const sessionId = () => state.snapshot?.session?.session_id || "sess_0881";
    root.querySelectorAll(".debug-item[data-kind=episodic]").forEach((card) => {
      const submit = (extra, message) => {
        const fields = collectEpisodicFields(card);
        const body = extra?.regenerate_insight || extra?.curate
          ? {
              event_id: card.dataset.id,
              summary: fields.summary,
              objective_fact: fields.objective_fact,
              importance: fields.importance,
              emotional_valence: fields.emotional_valence,
              character_action: fields.character_action,
              outcome: fields.outcome,
              event_type: fields.event_type,
              ...extra,
            }
          : { ...fields, event_id: card.dataset.id, ...extra };
        const status = extra?.regenerate_insight
          ? "正在重新生成解读…"
          : extra?.curate
            ? "正在跑 Curator…"
            : null;
        if (status) setDebugStatus(status);
        save("episodic", body, message);
      };
      card.querySelectorAll("input[type=range], textarea[data-field], input:not([type=range])[data-field]").forEach((el) => {
        el.addEventListener("change", () => submit({}, "已保存"));
      });
      card.querySelector("[data-regen=insight]")?.addEventListener("click", () => {
        submit({ regenerate_insight: true }, "已重新生成解读");
      });
      card.querySelector("[data-regen=curate]")?.addEventListener("click", () => {
        submit({ curate: true }, "已重新跑 Curator");
      });
    });
    const createCard = root.querySelector("[data-kind=episodic-create]");
    createCard?.querySelector("[data-create-episodic]")?.addEventListener("click", () => {
      const body = collectEpisodicFields(createCard);
      body.session_id = sessionId();
      delete body.event_id;
      if (!String(body.summary || "").trim()) {
        setDebugStatus("新建事件需要填写 summary", true);
        return;
      }
      setDebugStatus("正在创建事件…");
      save("episodic", body, "已创建事件");
    });
  }

  if (state.debug.tab === "semantic") {
    root.querySelectorAll(".debug-item").forEach((card) => {
      const submit = () => {
        save("semantic", {
          fact_id: card.dataset.id,
          confidence: Number(card.querySelector("input[type=range]")?.value),
          value: card.querySelector("input[data-field=value]")?.value ?? "",
        });
      };
      card.querySelector("input[type=range]")?.addEventListener("change", submit);
      card.querySelector("input[data-field=value]")?.addEventListener("change", submit);
    });
  }

  if (state.debug.tab === "candidates") {
    root.querySelectorAll(".debug-item").forEach((card) => {
      const submit = () => {
        save("candidate", {
          candidate_id: card.dataset.id,
          confidence: Number(card.querySelector("input[type=range]")?.value),
          status: card.querySelector("select[data-field=status]")?.value,
        });
      };
      card.querySelector("input[type=range]")?.addEventListener("change", submit);
      card.querySelector("select")?.addEventListener("change", submit);
    });
  }

  if (state.debug.tab === "tuning") {
    const submit = () => save("tuning", collectTuningPayload(root));
    root.querySelectorAll("input[type=range]").forEach((el) => el.addEventListener("change", submit));
    root.querySelectorAll("input[type=number]").forEach((el) => el.addEventListener("change", submit));
  }
}

function closeDebugDialog() {
  state.debug.open = false;
  const dialog = $("debug-dialog");
  if (dialog.open) dialog.close();
}

$("btn-debug").addEventListener("click", async () => {
  state.debug.open = true;
  setDebugStatus("加载中…");
  $("debug-dialog").showModal();
  renderDebugBody();
  try {
    await loadDebugState();
    setDebugStatus("就绪");
  } catch (err) {
    setDebugStatus(err.message, true);
  }
});
$("btn-debug-close").addEventListener("click", closeDebugDialog);
$("debug-dialog").addEventListener("close", () => {
  state.debug.open = false;
});
$("debug-tabs").addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-tab]");
  if (!btn) return;
  event.preventDefault();
  state.debug.tab = btn.dataset.tab;
  renderDebugBody();
});

function setHomeostasisStatus(message, error = false) {
  const node = $("homeostasis-status");
  node.textContent = message;
  node.className = `hint ${error ? "err" : ""}`;
}

function homeostasisNumber(value, step = "0.01") {
  return `type="number" min="0" max="1" step="${step}" value="${esc(value)}"`;
}

function renderHomeostasisEditor(profile) {
  const root = $("homeostasis-editor");
  if (!profile) {
    root.innerHTML = `<p class="hint">没有可编辑的 profile。</p>`;
    return;
  }
  const dimensions = Object.entries(profile.dimensions || {})
    .map(([name, spec]) => `<fieldset class="homeo-editor-dimension" data-dimension="${esc(name)}">
      <legend>${esc(name)}</legend>
      <label>目标下界<input data-field="baseline_low" ${homeostasisNumber(spec.baseline_band?.[0])}></label>
      <label>目标上界<input data-field="baseline_high" ${homeostasisNumber(spec.baseline_band?.[1])}></label>
      <label>硬下界<input data-field="hard_low" ${homeostasisNumber(spec.hard_bounds?.[0])}></label>
      <label>硬上界<input data-field="hard_high" ${homeostasisNumber(spec.hard_bounds?.[1])}></label>
      <label>每小时漂移<input data-field="drift_per_hour" ${homeostasisNumber(spec.drift_per_hour, "0.001")}></label>
      <label>低侧权重<input data-field="lower_weight" type="number" min="0" max="10" step="0.05" value="${esc(spec.lower_weight)}"></label>
      <label>高侧权重<input data-field="upper_weight" type="number" min="0" max="10" step="0.05" value="${esc(spec.upper_weight)}"></label>
      <label>单事件最大增量<input data-field="max_event_delta" ${homeostasisNumber(spec.max_event_delta, "0.01")}></label>
    </fieldset>`)
    .join("");
  root.innerHTML = `<div class="homeo-profile-meta">
      <span class="badge">${esc(profile.profile_id)}@${esc(profile.revision)}</span>
      <span class="badge ghost">${esc(profile.mode)}</span>
      <span class="badge ghost">A ${esc(profile.observation_model_version)} · B ${esc(profile.transition_model_version)} · C ${esc(profile.preference_model_version)}</span>
    </div>
    <div class="homeo-editor-grid">${dimensions}</div>
    <div class="homeo-advanced">
      <label>Source precision<textarea data-json="source_precision">${esc(JSON.stringify(profile.source_precision || {}, null, 2))}</textarea></label>
      <label>Coupling matrix<textarea data-json="coupling">${esc(JSON.stringify(profile.coupling || {}, null, 2))}</textarea></label>
      <label>Observation model (A)<textarea data-json="observation_model">${esc(JSON.stringify(profile.observation_model || {}, null, 2))}</textarea></label>
      <label>Transition model (B)<textarea data-json="transition_model">${esc(JSON.stringify(profile.transition_model || {}, null, 2))}</textarea></label>
      <label>Preferences (C)<textarea data-json="preferences">${esc(JSON.stringify(profile.preferences || {}, null, 2))}</textarea></label>
    </div>
    <div class="homeo-editor-settings">
      <label>情绪阈值<input data-profile="emotion_threshold" ${homeostasisNumber(profile.emotion_threshold, "0.01")}></label>
      <label>最短保持秒数<input data-profile="emotion_minimum_hold_seconds" type="number" min="0" max="86400" step="1" value="${esc(profile.emotion_minimum_hold_seconds)}"></label>
      <label>最大心跳步长<input data-profile="max_heartbeat_step_seconds" type="number" min="1" max="3600" step="1" value="${esc(profile.max_heartbeat_step_seconds)}"></label>
    </div>`;
}

function collectHomeostasisProfile() {
  const profile = structuredClone(state.homeostasis.profile);
  $("homeostasis-editor").querySelectorAll("[data-dimension]").forEach((field) => {
    const name = field.dataset.dimension;
    const get = (key) => Number(field.querySelector(`[data-field=${key}]`).value);
    profile.dimensions[name] = {
      ...profile.dimensions[name],
      baseline_band: [get("baseline_low"), get("baseline_high")],
      hard_bounds: [get("hard_low"), get("hard_high")],
      drift_per_hour: get("drift_per_hour"),
      lower_weight: get("lower_weight"),
      upper_weight: get("upper_weight"),
      max_event_delta: get("max_event_delta"),
    };
  });
  $("homeostasis-editor").querySelectorAll("textarea[data-json]").forEach((field) => {
    profile[field.dataset.json] = JSON.parse(field.value);
  });
  $("homeostasis-editor").querySelectorAll("[data-profile]").forEach((field) => {
    profile[field.dataset.profile] = Number(field.value);
  });
  return profile;
}

async function validateHomeostasisProfile() {
  const profile = collectHomeostasisProfile();
  const res = await fetch("/api/homeostasis/profile/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profile }),
  });
  const body = await res.json();
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail));
  return body.profile;
}

$("btn-homeostasis").addEventListener("click", async () => {
  setHomeostasisStatus("加载中…");
  $("homeostasis-dialog").showModal();
  try {
    const res = await fetch("/api/homeostasis/profile");
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || res.statusText);
    state.homeostasis.profile = body.profile;
    renderHomeostasisEditor(body.profile);
    setHomeostasisStatus(body.pending_start ? `已有待激活版本：${body.pending_start.profile_ref}` : "就绪");
  } catch (err) {
    setHomeostasisStatus(err.message, true);
  }
});
$("btn-homeostasis-close").addEventListener("click", () => $("homeostasis-dialog").close());
$("btn-homeostasis-validate").addEventListener("click", async () => {
  try {
    await validateHomeostasisProfile();
    setHomeostasisStatus("校验通过；保存后将创建新版本。", false);
  } catch (err) {
    setHomeostasisStatus(err.message, true);
  }
});
$("homeostasis-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const profile = await validateHomeostasisProfile();
    const res = await fetch("/api/homeostasis/profile/revisions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_revision: state.homeostasis.profile.revision, profile }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail));
    setHomeostasisStatus(`已保存 ${body.profile_ref}；请重新运行 start.ps1 激活。`);
    await fetchStatus();
  } catch (err) {
    setHomeostasisStatus(err.message, true);
  }
});
