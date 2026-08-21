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
  debug: { open: false, tab: "relationship", data: null },
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

function renderGenerate(events) {
  const blocks = events
    .map((ev) => {
      const data = ev.data || {};
      return `<div class="kv-row"><span>revision ${esc(data.revision ?? ev.meta?.revision ?? 0)}</span>
        <span>${esc(ev.latency_ms)} ms</span></div>
        <div class="quote">${esc(data.text)}</div>`;
    })
    .join("");
  return withRaw(blocks, events.map((ev) => ev.data));
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
  const segs = Array.isArray(data.segments) ? data.segments : [];
  const quotes = segs.length
    ? segs
        .map(
          (text, i) =>
            `<div class="quote"><small>段 ${i + 1}</small>${esc(text)}</div>`,
        )
        .join("")
    : `<div class="quote">${esc(data.text)}</div>`;
  return withRaw(
    `<div class="kv">
      <div class="kv-row"><span>path</span><span class="badge ${data.path}">${esc(data.path)}</span></div>
      <div class="kv-row"><span>revisions</span><span>${esc(data.revisions)}</span></div>
      <div class="kv-row"><span>stopped_by</span><span>${esc(data.stopped_by || "—")}</span></div>
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
      return `<li class="stage ${stage.status} ${open}" data-stage="${stage.id}">
        <button type="button" class="stage-head">
          <span class="idx">${i + 1}</span>
          <span class="stage-title"><strong>${esc(stage.title)}</strong><small>${esc(stage.sub)}</small></span>
          <span class="stage-flags">${esc(latency)}</span>
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

function renderTranscript() {
  const root = $("transcript");
  if (!state.messages.length) {
    root.innerHTML = `<div class="empty-chat">还没有对话。发送一句，观察九阶段如何装配语境。</div>`;
    return;
  }
  root.innerHTML = state.messages
    .map((msg) => {
      if (msg.pending) {
        return `<div class="bubble character pending pulse">推理中</div>`;
      }
      const meta =
        msg.role === "character" && msg.path
          ? `<div class="meta"><span class="badge ${msg.path}">${esc(msg.path)}</span>
             <span class="badge ghost">rev ${esc(msg.revisions ?? 0)}</span></div>`
          : "";
      return `<div class="bubble ${msg.role}">${esc(msg.text)}${meta}</div>`;
    })
    .join("");
  root.scrollTop = root.scrollHeight;
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
  $("mode-badge").textContent = llm.mode || snap.mode || "demo";
  const llmBadge = $("llm-badge");
  llmBadge.textContent = `${llm.model || "?"} · ${llm.reachable ? "可达" : "离线"}`;
  llmBadge.className = `badge ${llm.reachable ? "ok" : "bad"}`;
  $("cfg-mode").value = llm.mode || "demo";
  $("cfg-url").value = llm.base_url || "";
  $("cfg-model").value = llm.model || "";
  $("cfg-key").placeholder = llm.api_key_configured ? "已配置，留空则保留" : "sk-…（live 模式需要）";

  root.innerHTML = `
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

function applySnapshot(snap) {
  state.snapshot = snap;
  if (snap?.session?.turns && !state.messages.length) {
    state.messages = snap.session.turns.map((turn) => ({
      role: turn.role,
      text: turn.text,
    }));
  }
  renderSubsystems(snap);
  renderTranscript();
}

function beginRun() {
  state.stages = resetStages();
  state.stages[0].status = "running";
  state.expanded = "ingest";
  $("run-meta").textContent = "运行中…";
  renderTimeline();
}

function applyStageEvent(msg) {
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
      const pendingIdx = state.messages.findIndex((item) => item.pending);
      if (pendingIdx >= 0) {
        const rawSegs = msg.data?.segments;
        const segs =
          Array.isArray(rawSegs) && rawSegs.length ? rawSegs : [msg.data?.text || ""];
        const base = {
          role: "character",
          pending: false,
          path: msg.data?.path,
          revisions: msg.data?.revisions,
        };
        state.messages.splice(
          pendingIdx,
          1,
          ...segs.map((text) => ({ ...base, text })),
        );
      }
      const stopped = msg.data?.stopped_by ? ` · ${msg.data.stopped_by}` : "";
      $("run-meta").textContent = `${msg.data?.path || "?"} path · ${msg.data?.revisions ?? 0} revisions${stopped}`;
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

async function fetchStatus() {
  const sessionId = $("session-id").value.trim() || "sess_0881";
  const res = await fetch(`/api/status?session_id=${encodeURIComponent(sessionId)}`);
  applySnapshot(await res.json());
}

async function sendChat(event) {
  event.preventDefault();
  if (state.busy) return;
  const text = $("input").value.trim();
  if (!text) return;
  const sessionId = $("session-id").value.trim() || "sess_0881";
  state.busy = true;
  $("btn-send").disabled = true;
  $("input").value = "";
  state.messages.push({ role: "user", text });
  state.messages.push({ role: "character", text: "", pending: true });
  renderTranscript();
  beginRun();
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, text }),
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

$("btn-settings").addEventListener("click", () => $("settings-dialog").showModal());
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
    }),
  });
    const body = await res.json();
    if (!res.ok) {
      window.alert(typeof body.detail === "string" ? body.detail : "保存失败");
      return;
    }
    applySnapshot(body);
    state.messages = [];
    renderTranscript();
    $("cfg-key").value = "";
    $("settings-dialog").close();
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
      <label>speaker
        <select data-field="speaker">
          <option value="user" selected>user</option>
          <option value="character">character</option>
        </select>
      </label>
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
    const sessionId = () => $("session-id")?.value?.trim() || "sess_0881";
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
