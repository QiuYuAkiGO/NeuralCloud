const $ = (id) => document.getElementById(id);

const state = {
  window: "24h",
  overview: null,
  events: [],
  eventCursor: null,
  runs: [],
  runCursor: null,
  actions: [],
  selectedEvent: null,
  selectedRun: null,
  ws: null,
  available: false,
  refreshTimer: null,
};

const EVENT_TYPES = [
  "emergency_stop", "pause", "resume", "stop", "recover", "permission_revoked",
  "watchdog", "approval_result", "action_result", "user_message", "environment_change",
  "perception", "body_state", "goal_reconsideration", "agency_terminal",
  "resume_observation", "heartbeat", "maintenance", "idle", "reconciliation",
  "time_discontinuity", "diagnostic",
];
const PRIORITIES = [
  [0, "CONTROL"], [10, "ACTION_RESULT"], [20, "USER"], [30, "IMPORTANT_ENVIRONMENT"],
  [40, "PERCEPTION"], [50, "HEARTBEAT"], [60, "IDLE"], [70, "TELEMETRY"],
];

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function asError(detail, fallback) {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return fallback;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) throw new Error(asError(payload.detail, response.statusText));
  return payload;
}

function jsonOptions(body) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) };
}

function timeLabel(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(date);
}

function duration(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n >= 1000 ? `${(n / 1000).toFixed(2)} s` : `${n.toFixed(1)} ms`;
}

function number(value) {
  const n = Number(value || 0);
  return new Intl.NumberFormat("zh-CN").format(n);
}

function toast(message, error = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = error ? "toast error" : "toast";
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 3600);
}

function setLive(label, tone = "muted") {
  const node = $("live-state");
  node.className = `status-chip ${tone}`;
  node.innerHTML = `<i class="live-dot"></i>${esc(label)}`;
}

function setView(name) {
  document.querySelectorAll(".ops-nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === name);
  });
  document.querySelectorAll(".view").forEach((view) => {
    const active = view.id === `view-${name}`;
    view.classList.toggle("active", active);
    view.hidden = !active;
  });
  if (name === "overview") requestAnimationFrame(drawOverviewCharts);
  if (name === "events" && !state.events.length) loadEvents();
  if (name === "runs" && !state.runs.length) loadRuns();
  if (name === "actions") loadActions();
}

async function loadAvailability() {
  try {
    const availability = await api("/api/ops/availability");
    state.available = Boolean(availability.available);
    if (!state.available) {
      const banner = $("availability-banner");
      banner.textContent = availability.reason || "运维面板需要完整 Runtime 拓扑。";
      banner.hidden = false;
      setLive("Runtime 不可用", "error");
      document.querySelectorAll("button, select, input, textarea").forEach((node) => { node.disabled = true; });
      return false;
    }
    return true;
  } catch (error) {
    const banner = $("availability-banner");
    banner.textContent = `无法连接运维接口：${error.message}`;
    banner.hidden = false;
    setLive("连接失败", "error");
    return false;
  }
}

async function loadOverview() {
  if (!state.available) return;
  try {
    const data = await api(`/api/ops/overview?window=${encodeURIComponent(state.window)}`);
    state.overview = data;
    renderOverview();
  } catch (error) {
    toast(`概览加载失败：${error.message}`, true);
  }
}

function healthCard(name, detail, ok, warning = false) {
  const tone = ok ? "" : warning ? "warning" : "error";
  return `<article class="health-card ${tone}"><i></i><strong>${esc(name)}</strong><span>${esc(detail)}</span></article>`;
}

function renderOverview() {
  const data = state.overview;
  if (!data) return;
  const runtime = data.runtime || {};
  const production = runtime.production;
  const health = runtime.health || {};
  const embodiment = runtime.embodiment || health.embodiment || {};
  const manifest = data.manifest || {};
  const services = data.services || {};
  const homeostasis = data.homeostasis || {};
  const monitor = data.monitor || {};
  const aggregates = data.aggregates || {};
  const lifecycle = String(runtime.lifecycle || "unknown");
  $("lifecycle-chip").textContent = `Runtime · ${lifecycle}`;
  $("lifecycle-chip").className = `status-chip ${lifecycle === "RUNNING" ? "" : lifecycle === "FAULTED" ? "error" : "warning"}`;
  $("mode-chip").textContent = `模式 · ${manifest.control_mode || "unknown"}`;
  $("mode-chip").className = `status-chip ${manifest.control_mode === "development_debug" ? "warning" : "muted"}`;
  $("instance-chip").textContent = `实例 · ${runtime.runtime_instance_id || "—"}`;
  $("overview-caption").textContent = `${state.window} · lifecycle since ${timeLabel(runtime.lifecycle_since)} · manifest r${runtime.manifest_revision ?? "—"}`;

  const embodimentState = String(embodiment.state || "disabled");
  $("health-grid").innerHTML = [
    healthCard("GUI", "本页在线", true),
    healthCard("Runtime", lifecycle, lifecycle === "RUNNING", lifecycle !== "FAULTED"),
    healthCard("Cognitive", services.cognitive?.revision != null ? `revision ${services.cognitive.revision}` : health.cognitive_healthy === false ? "不可用" : "healthy", health.cognitive_healthy !== false),
    production ? healthCard("具身内稳态", production.body_available ? production.resting ? "休息中" : production.exhausted ? "资源耗尽" : "可用" : "不可用", production.body_available && !production.exhausted, production.resting)
      : healthCard("Homeostasis", homeostasis.revision != null ? `revision ${homeostasis.revision}` : health.homeostasis_healthy === false ? "不可用" : "healthy", health.homeostasis_healthy !== false),
    healthCard("LLM", health.llm_healthy === false ? "不可用" : "healthy", health.llm_healthy !== false),
    healthCard("Embodiment", embodimentState, ["available", "disabled"].includes(embodimentState), embodimentState === "degraded"),
  ].join("");

  const queue = runtime.queue || {};
  const metricData = [
    [queue.size || 0, "队列深度", `容量 ${queue.capacity ?? "—"}`],
    [Math.max(0, Number(runtime.receive_seq || 0) - Number(runtime.process_seq || 0)), "处理差值", `receive ${runtime.receive_seq || 0} / process ${runtime.process_seq || 0}`],
    [aggregates.events || 0, "窗口事件", `${Number(aggregates.throughput_per_minute || 0).toFixed(2)}/min · ${aggregates.completed || 0} 完成`],
    [aggregates.runs || 0, "推理运行", `${aggregates.run_failures || 0} 失败`],
    [duration(aggregates.latency_p95_ms), "推理 P95", `P50 ${duration(aggregates.latency_p50_ms)}`],
    [number(aggregates.total_tokens), "Token", `${aggregates.revisions || 0} 次重生成`],
    [aggregates.warnings || 0, "警告", `${aggregates.errors || 0} 错误 · ${(Number(aggregates.failure_rate || 0) * 100).toFixed(1)}% 失败`],
    [runtime.pending_actions || 0, "待处理动作", `current ${runtime.current_action?.state || "none"}`],
  ];
  if (production) metricData.push(
    [production.budget?.remaining ?? "—", "自主模型预算剩余", `${production.budget?.used ?? "—"} / ${production.budget?.limit ?? "—"}`],
    [production.usage_pending ?? "—", "用量待确认", `任务 ${production.tasks ?? "—"} · 队列 ${production.queued ?? "—"}`],
  );
  $("metric-grid").innerHTML = metricData.map(([value, label, detail]) =>
    `<article class="metric-card"><strong>${esc(value)}</strong><span>${esc(label)}</span><em>${esc(detail)}</em></article>`
  ).join("");

  const monitorBanner = $("monitor-banner");
  if (!monitor.healthy || monitor.ingest_dropped) {
    monitorBanner.textContent = `监控投影降级：${monitor.last_error || `${monitor.ingest_dropped} 条实时事件未写入`}。Runtime 本身仍继续运行。`;
    monitorBanner.hidden = false;
  } else {
    monitorBanner.hidden = true;
  }

  const alerts = data.alerts || [];
  $("alert-list").innerHTML = alerts.length ? alerts.map((item) => `
    <button type="button" class="compact-item text-button" data-alert-seq="${item.seq}">
      <span class="severity ${esc(item.severity)}">${esc(item.severity)}</span>
      <span>${esc(item.topic)}<small>${esc(item.correlation_id || item.event_id || "无关联标识")}</small></span>
      <small>${esc(timeLabel(item.observed_at))}</small>
    </button>`).join("") : `<p class="empty">当前窗口没有告警。</p>`;

  const vector = runtime.version_vector || {};
  $("version-detail").innerHTML = [
    ["Runtime instance", runtime.runtime_instance_id],
    ["Character", runtime.character_id],
    ["Manifest", `${manifest.experiment_id || "—"}@${manifest.revision || runtime.manifest_revision || "—"}`],
    ["Version vector", Object.entries(vector).map(([key, value]) => `${key}:${value}`).join(" · ") || "—"],
    ...(production ? [
      ["Body observation age", production.body_age_seconds == null ? "未知" : `${Number(production.body_age_seconds).toFixed(1)} s`],
      ["Body identity", `${production.body_identity?.body_instance_id || "—"} / ${production.body_identity?.episode_id || "—"}`],
      ["Energy", production.body_available && production.energy ? `${production.energy.available_units} / ${production.energy.capacity_units} · ${production.energy.delta_units_per_minute} / min` : "未知"],
      ["Sample / ledger", `${production.sample_sequence ?? "未知"} / ${production.ledger_revision ?? "未知"}`],
      ["Resting / exhausted", [production.resting, production.exhausted].map(value => !production.body_available || value == null ? "未知" : String(value)).join(" / ")],
      ["Rest receipts pending", String(production.rest_pending ?? "—")],
      ["Unassigned usage", String(production.usage_unassigned ?? "—")],
      ["Production reason", production.last_reason || production.body_error || "—"],
      ["Goals", (production.goals || []).slice(0, 32).map(goal => `${goal.description || goal.id} (${goal.status})`).join(" · ") || "暂无"],
    ] : [
      ["Homeostasis", homeostasis.profile_id ? `${homeostasis.profile_id}@${homeostasis.profile_revision} · r${homeostasis.revision}` : "—"],
      ["Emotion", homeostasis.emotion?.label ? `${homeostasis.emotion.label} · ${Number(homeostasis.emotion.intensity || 0).toFixed(2)}` : "—"],
    ]),
    ["Journal", health.journal_writable === false ? "不可写" : `可写 · ${timeLabel(health.last_journal_append_at)}`],
    ["Embodiment provider", embodiment.provider_id || "disabled"],
    ["Body", embodiment.body_instance_id || "—"],
    ["Capabilities", (embodiment.effective_capabilities || []).join(", ") || "—"],
  ].map(([label, value]) => `<div class="detail-row"><span>${esc(label)}</span><code>${esc(value || "—")}</code></div>`).join("");

  const debug = manifest.control_mode === "development_debug";
  document.querySelectorAll(".debug-only").forEach((node) => node.classList.toggle("debug-hidden", !debug));
  $("control-caption").textContent = debug
    ? "development_debug：完整调试控制可直接执行。"
    : "study_runtime：事件注入与人工结果改写已禁用。";
  drawOverviewCharts();
}

function canvasColors() {
  const style = getComputedStyle(document.documentElement);
  return {
    ink: style.getPropertyValue("--muted").trim(),
    line: style.getPropertyValue("--line").trim(),
    blue: style.getPropertyValue("--blue").trim(),
    purple: style.getPropertyValue("--purple").trim(),
    cyan: style.getPropertyValue("--cyan").trim(),
    amber: style.getPropertyValue("--amber").trim(),
    red: style.getPropertyValue("--red").trim(),
  };
}

function drawLineChart(canvas, samples, series) {
  if (!canvas || !canvas.clientWidth) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = 220;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);
  const colors = canvasColors();
  const margin = { left: 38, right: 12, top: 12, bottom: 27 };
  const plotWidth = Math.max(1, width - margin.left - margin.right);
  const plotHeight = height - margin.top - margin.bottom;
  const values = samples.flatMap((sample) => series.map((item) => Number(sample[item.key] || 0)));
  const maxValue = Math.max(1, ...values);
  ctx.strokeStyle = colors.line;
  ctx.fillStyle = colors.ink;
  ctx.font = "11px system-ui";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const y = margin.top + (plotHeight * i) / 4;
    ctx.beginPath(); ctx.moveTo(margin.left, y); ctx.lineTo(width - margin.right, y); ctx.stroke();
    const value = Math.round(maxValue * (1 - i / 4));
    ctx.fillText(String(value), 4, y + 4);
  }
  if (!samples.length) {
    ctx.fillText("等待样本", margin.left + 8, margin.top + 20);
    return;
  }
  series.forEach((item) => {
    ctx.strokeStyle = colors[item.color];
    ctx.lineWidth = 2;
    ctx.beginPath();
    samples.forEach((sample, index) => {
      const x = margin.left + (plotWidth * index) / Math.max(1, samples.length - 1);
      const y = margin.top + plotHeight - (Number(sample[item.key] || 0) / maxValue) * plotHeight;
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
  ctx.fillStyle = colors.ink;
  ctx.fillText(timeLabel(samples[0].observed_at), margin.left, height - 6);
  const end = timeLabel(samples[samples.length - 1].observed_at);
  const endWidth = ctx.measureText(end).width;
  ctx.fillText(end, Math.max(margin.left, width - margin.right - endWidth), height - 6);
}

function drawOverviewCharts() {
  const samples = state.overview?.samples || [];
  drawLineChart($("queue-chart"), samples, [
    { key: "queue_size", color: "blue" }, { key: "receive_process_lag", color: "purple" },
  ]);
  drawLineChart($("load-chart"), samples, [
    { key: "coalesced", color: "cyan" }, { key: "dropped", color: "amber" }, { key: "rejected", color: "red" },
  ]);
}

function eventQuery(cursor = null) {
  const params = new URLSearchParams({ window: state.window, limit: "100" });
  let topic = $("event-topic").value.trim();
  if ($("event-source").value === "journal") topic = topic ? `journal.${topic.replace(/^journal\./, "")}` : "journal.";
  if (topic) params.set("topic", topic);
  if ($("event-severity").value) params.set("severity", $("event-severity").value);
  if ($("event-status").value.trim()) params.set("status", $("event-status").value.trim());
  if ($("event-correlation").value.trim()) params.set("correlation_id", $("event-correlation").value.trim());
  if (cursor) params.set("cursor", cursor);
  return params;
}

async function loadEvents(append = false) {
  if (!state.available) return;
  try {
    const cursor = append ? state.eventCursor : null;
    const data = await api(`/api/ops/events?${eventQuery(cursor).toString()}`);
    state.events = append ? [...state.events, ...(data.items || [])] : (data.items || []);
    state.eventCursor = data.next_cursor || null;
    renderEvents();
  } catch (error) { toast(`事件加载失败：${error.message}`, true); }
}

function renderEvents() {
  $("event-rows").innerHTML = state.events.length ? state.events.map((item) => `
    <tr data-event-seq="${item.seq}" class="${state.selectedEvent?.seq === item.seq ? "selected" : ""}">
      <td>${esc(timeLabel(item.observed_at))}</td>
      <td><span class="severity ${esc(item.severity)}">${esc(item.severity)}</span></td>
      <td><code>${esc(item.topic)}</code></td>
      <td><code>${esc(item.correlation_id || item.run_id || "—")}</code></td>
      <td><code>${esc(item.event_id || "—")}</code></td>
    </tr>`).join("") : `<tr><td colspan="5" class="empty">当前筛选没有事件。</td></tr>`;
  $("events-more").hidden = !state.eventCursor;
}

function selectEvent(seq) {
  const item = state.events.find((event) => Number(event.seq) === Number(seq));
  if (!item) return;
  state.selectedEvent = item;
  renderEvents();
  $("event-summary").innerHTML = [
    ["seq", item.seq], ["observed", timeLabel(item.observed_at)], ["source", item.source],
    ["topic", item.topic], ["severity", item.severity], ["correlation", item.correlation_id || item.run_id],
    ["event", item.event_id],
    ["journal checksum", item.payload?.integrity ? (item.payload.integrity.checksum_valid ? "valid" : "INVALID") : "—"],
  ].map(([label, value]) => `<div class="detail-row"><span>${esc(label)}</span><code>${esc(value || "—")}</code></div>`).join("");
  $("event-json").textContent = JSON.stringify(item.payload, null, 2);
  $("event-export").disabled = !(item.correlation_id || item.run_id);
}

async function loadRuns(append = false) {
  if (!state.available) return;
  try {
    const params = new URLSearchParams({ window: state.window, limit: "100" });
    if (append && state.runCursor) params.set("cursor", state.runCursor);
    const data = await api(`/api/ops/runs?${params.toString()}`);
    state.runs = append ? [...state.runs, ...(data.items || [])] : (data.items || []);
    state.runCursor = data.next_cursor || null;
    renderRuns();
  } catch (error) { toast(`运行记录加载失败：${error.message}`, true); }
}

function renderRuns() {
  $("run-rows").innerHTML = state.runs.length ? state.runs.map((run) => `
    <tr data-run-id="${esc(run.run_id)}" class="${state.selectedRun?.run_id === run.run_id ? "selected" : ""}">
      <td>${esc(timeLabel(run.started_at))}</td><td><code>${esc(run.run_id)}</code></td>
      <td><span class="state-label ${esc(String(run.status).toLowerCase())}">${esc(run.status)}</span></td>
      <td>${esc(run.path || "—")}</td><td>${esc(duration(run.latency_ms))}</td>
      <td>${esc(number(run.total_tokens))}</td><td>${esc(run.revisions || 0)}</td>
    </tr>`).join("") : `<tr><td colspan="7" class="empty">当前窗口没有推理运行。</td></tr>`;
  $("runs-more").hidden = !state.runCursor;
}

async function selectRun(runId) {
  try {
    const run = await api(`/api/ops/runs/${encodeURIComponent(runId)}`);
    state.selectedRun = run;
    renderRuns();
    const stages = run.stages || [];
    const maxLatency = Math.max(1, ...stages.map((stage) => Number(stage.latency_ms || 0)));
    $("run-detail").innerHTML = `
      <div class="detail-list">
        <div class="detail-row"><span>Run</span><code>${esc(run.run_id)}</code></div>
        <div class="detail-row"><span>状态</span><code>${esc(run.status)}</code></div>
        <div class="detail-row"><span>路径</span><code>${esc(run.path || "—")}</code></div>
        <div class="detail-row"><span>总延迟</span><code>${esc(duration(run.latency_ms))}</code></div>
        <div class="detail-row"><span>Token</span><code>${esc(`${run.total_tokens} · in ${run.prompt_tokens} / out ${run.completion_tokens}`)}</code></div>
      </div>
      <div class="waterfall">${stages.length ? stages.map((stage, index) => {
        const latency = Number(stage.latency_ms || 0);
        return `<div class="stage-row">
          <button type="button" data-stage-index="${index}">${esc(stage.stage || stage.kind || "step")}</button>
          <div class="stage-track"><i style="width:${Math.max(2, Math.round((latency / maxLatency) * 100))}%"></i></div>
          <span>${esc(duration(latency))}</span>
          <pre class="raw-json stage-json" data-stage-json="${index}" hidden>${esc(JSON.stringify(stage, null, 2))}</pre>
        </div>`;
      }).join("") : `<p class="empty">没有阶段数据。</p>`}</div>`;
    $("run-export").disabled = false;
  } catch (error) { toast(`运行详情加载失败：${error.message}`, true); }
}

async function loadActions() {
  if (!state.available) return;
  try {
    const data = await api("/api/ops/actions?limit=200");
    state.actions = data.items || [];
    renderActions();
  } catch (error) { toast(`动作加载失败：${error.message}`, true); }
}

function renderActions() {
  $("action-list").innerHTML = state.actions.length ? state.actions.map((entry) => {
    const action = entry.action || {};
    const stateName = String(action.state || entry.state || "UNKNOWN");
    const canApprove = stateName === "AWAITING_APPROVAL";
    const canReject = !["SUCCEEDED", "FAILED", "CANCELLED"].includes(stateName);
    const canResolve = stateName === "UNKNOWN" && state.overview?.manifest?.control_mode === "development_debug";
    return `<article class="action-card" data-action-id="${esc(action.action_id || entry.action_id)}">
      <div class="action-card-head"><code>${esc(action.action_id || entry.action_id)}</code><span class="state-label ${esc(stateName.toLowerCase())}">${esc(stateName)}</span></div>
      <p>${esc(action.goal || action.error || action.outcome?.summary || "无摘要")}</p>
      <div class="detail-list">
        <div class="detail-row"><span>risk / reversible</span><code>${esc(`${action.risk_class || "—"} / ${action.reversible ?? "—"}`)}</code></div>
        <div class="detail-row"><span>approvals</span><code>${esc((action.approvals || []).join(", ") || "—")}</code></div>
        <div class="detail-row"><span>updated</span><code>${esc(timeLabel(action.updated_at || entry.updated_at))}</code></div>
      </div>
      <div class="control-row">
        ${canApprove ? `<button type="button" class="button" data-action-op="approve">批准</button>` : ""}
        ${canReject ? `<button type="button" class="button danger" data-action-op="reject">拒绝</button>` : ""}
        ${canResolve ? `<button type="button" class="button" data-action-resolve-fill>填入对账</button>` : ""}
      </div>
      <pre class="raw-json">${esc(JSON.stringify(action, null, 2))}</pre>
    </article>`;
  }).join("") : `<p class="empty">没有动作记录。</p>`;
}

async function runControl(operation) {
  try {
    const result = await api(`/api/ops/runtime/${operation}`, jsonOptions({ reason: `ops ${operation}` }));
    toast(`${operation} 已提交${result.event_id ? ` · ${result.event_id}` : ""}`);
    scheduleRefresh(true);
  } catch (error) { toast(`${operation} 失败：${error.message}`, true); }
}

async function actionControl(actionId, operation) {
  const body = operation === "approve" ? { approval: "researcher" } : { reason: "rejected from ops" };
  try {
    await api(`/api/ops/actions/${encodeURIComponent(actionId)}/${operation}`, jsonOptions(body));
    toast(`Action ${operation} 已提交`);
    await loadActions();
    await loadOverview();
  } catch (error) { toast(`Action ${operation} 失败：${error.message}`, true); }
}

async function exportBundle(correlationId) {
  if (!correlationId) return;
  try {
    const bundle = await api(`/api/ops/export/${encodeURIComponent(correlationId)}`);
    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `neuralcloud-diagnostic-${correlationId.replace(/[^a-zA-Z0-9_.-]/g, "_")}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
    toast("诊断包已导出");
  } catch (error) { toast(`导出失败：${error.message}`, true); }
}

function scheduleRefresh(immediate = false) {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(async () => {
    await Promise.all([loadOverview(), loadEvents(), loadRuns(), loadActions()]);
  }, immediate ? 80 : 1200);
}

function connectWebSocket() {
  if (!state.available) return;
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ops/ws`);
  state.ws = socket;
  socket.onopen = () => setLive("实时", "");
  socket.onclose = () => {
    setLive("重连中", "warning");
    setTimeout(connectWebSocket, 1500);
  };
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "ping") return;
    if (message.type === "ops.error") {
      setLive("事件流降级", "error");
      return;
    }
    if (message.type === "ops.event") scheduleRefresh(false);
  };
}

function bindInteractions() {
  document.querySelectorAll(".ops-nav button").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  document.querySelectorAll("[data-goto]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.goto)));
  $("window-select").addEventListener("change", async (event) => {
    state.window = event.target.value;
    state.events = []; state.runs = []; state.eventCursor = null; state.runCursor = null;
    await Promise.all([loadOverview(), loadEvents(), loadRuns()]);
  });
  $("refresh-overview").addEventListener("click", loadOverview);
  $("event-filters").addEventListener("submit", (event) => { event.preventDefault(); loadEvents(false); });
  $("events-more").addEventListener("click", () => loadEvents(true));
  $("runs-more").addEventListener("click", () => loadRuns(true));
  $("event-rows").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-event-seq]");
    if (row) selectEvent(row.dataset.eventSeq);
  });
  $("run-rows").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-run-id]");
    if (row) selectRun(row.dataset.runId);
  });
  $("run-detail").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-stage-index]");
    if (!button) return;
    const pre = document.querySelector(`[data-stage-json="${button.dataset.stageIndex}"]`);
    if (pre) pre.hidden = !pre.hidden;
  });
  $("event-export").addEventListener("click", () => exportBundle(state.selectedEvent?.correlation_id || state.selectedEvent?.run_id));
  $("run-export").addEventListener("click", () => exportBundle(state.selectedRun?.correlation_id || state.selectedRun?.run_id));
  $("alert-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-alert-seq]");
    if (!button) return;
    const alert = state.overview?.alerts?.find((item) => Number(item.seq) === Number(button.dataset.alertSeq));
    if (alert) {
      state.events = [alert];
      setView("events");
      selectEvent(alert.seq);
    }
  });
  document.querySelectorAll("[data-runtime-op]").forEach((button) => button.addEventListener("click", () => runControl(button.dataset.runtimeOp)));
  $("refresh-actions").addEventListener("click", loadActions);
  $("action-list").addEventListener("click", (event) => {
    const card = event.target.closest("[data-action-id]");
    if (!card) return;
    const op = event.target.closest("[data-action-op]");
    if (op) actionControl(card.dataset.actionId, op.dataset.actionOp);
    if (event.target.closest("[data-action-resolve-fill]")) {
      $("resolve-action-id").value = card.dataset.actionId;
      $("resolve-action-summary").focus();
    }
  });
  $("inject-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const payload = JSON.parse($("inject-payload").value || "{}");
      const body = {
        event_type: $("inject-type").value,
        priority: Number($("inject-priority").value),
        source: $("inject-source").value.trim() || "ops_debug",
        correlation_id: $("inject-correlation").value.trim() || null,
        idempotency_key: $("inject-idempotency").value.trim() || null,
        payload,
      };
      const result = await api("/api/ops/events", jsonOptions(body));
      toast(`事件已注入 · ${result.event_id || "accepted"}`);
      scheduleRefresh(true);
    } catch (error) { toast(`事件注入失败：${error.message}`, true); }
  });
  $("resolve-action-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const actionId = $("resolve-action-id").value.trim();
    const body = {
      success: $("resolve-action-success").value === "true",
      summary: $("resolve-action-summary").value.trim(),
      unknown: $("resolve-action-unknown").value === "true",
      retry_safe: $("resolve-action-retry").value === "true",
    };
    try {
      await api(`/api/ops/actions/${encodeURIComponent(actionId)}/resolve`, jsonOptions(body));
      toast("Action 对账已提交");
      await loadActions();
    } catch (error) { toast(`Action 对账失败：${error.message}`, true); }
  });
  $("resolve-delivery-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const prepareId = $("resolve-prepare-id").value.trim();
    const success = $("resolve-delivery-success").value === "true";
    const segments = $("resolve-delivered").value.split("\n").map((item) => item.trim()).filter(Boolean);
    const body = success ? { success: true, delivered_segments: segments } : { success: false, error: $("resolve-delivery-error").value.trim() || "delivery failed" };
    try {
      await api(`/api/ops/deliveries/${encodeURIComponent(prepareId)}/resolve`, jsonOptions(body));
      toast("Delivery 对账已提交");
    } catch (error) { toast(`Delivery 对账失败：${error.message}`, true); }
  });
  window.addEventListener("resize", () => requestAnimationFrame(drawOverviewCharts));
}

async function initialize() {
  $("inject-type").innerHTML = EVENT_TYPES.map((value) => `<option value="${value}">${value}</option>`).join("");
  $("inject-priority").innerHTML = PRIORITIES.map(([value, label]) => `<option value="${value}">${value} · ${label}</option>`).join("");
  bindInteractions();
  if (!(await loadAvailability())) return;
  await Promise.all([loadOverview(), loadEvents(), loadRuns(), loadActions()]);
  connectWebSocket();
}

initialize();
