"use strict";

const $ = (id) => document.getElementById(id);

const LIFE_LABELS = { RUNNING: "运行", RECOVERING: "恢复", FAULTED: "故障", PAUSED: "暂停" };
const LIFE_COLORS = { RUNNING: "var(--green)", RECOVERING: "var(--amber)", FAULTED: "var(--red)", PAUSED: "var(--muted)" };
const PATH_LABELS = {
  fast: "快速路径", deep: "深路径", unknown: "未分类", completed: "完成",
  failed: "失败", error: "失败", write: "写入", wait: "等待",
  goal_context_too_large: "上下文过大", goal_revision_conflict: "修订冲突",
  goal_invalid_request: "请求无效", goal_runtime_error: "运行时错误",
  goal_planned_step_required: "缺计划步骤",
};
const CHANNEL_LABELS = {
  unknown: "未分类", goal: "目标决策", board: "留言板", qq: "QQ",
  web: "网页", desktop: "桌面", voice: "语音",
};
const PATH_TONES = { fast: "green", deep: "purple", unknown: "amber", completed: "green", failed: "red" };
const STAGE_LABELS = {
  ingest: "接入", parse: "解析", retrieval: "检索", state_build: "状态", gate: "分流",
  interpret: "解释", compile: "编译", generate: "生成", validate: "校验",
  prepared_segment: "分段", final: "收尾",
};
const KIND_LABELS = {
  board_reflection: "留言板反思",
  capability_opportunity: "能力机会",
  reply: "回复",
};
const NEED_LABELS = {
  novelty: "新奇",
  social_connection: "社交连接",
  energy_reserve: "能量储备",
  cognitive_load: "认知负荷",
  safety_predictability: "安全可预期",
  competence: "胜任",
  autonomy: "自主",
};
const AFFECT_LABELS = { valence: "效价", arousal: "唤醒", dominance: "主导" };

function today() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(new Date());
}

function number(value, digits = 0) {
  if (value == null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(n);
}

function percent(value, digits = 1) {
  if (value == null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const ratio = n > 1 ? n : n * 100;
  return `${ratio.toFixed(digits)}%`;
}

function compact(value) {
  if (value == null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1e8) return `${number(n / 1e8, 2)} 亿`;
  if (n >= 1e4) return `${number(n / 1e4, n >= 1e6 ? 0 : 1)} 万`;
  return number(n);
}

function duration(seconds) {
  if (seconds == null) return "—";
  const n = Number(seconds);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 60) return `${Math.round(n)} 秒`;
  if (n < 3600) return `${(n / 60).toFixed(n < 600 ? 1 : 0)} 分钟`;
  return `${(n / 3600).toFixed(n < 36000 ? 1 : 0)} 小时`;
}

function bytes(value) {
  if (value == null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = n;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 10 || unit === 0 ? 0 : 2)} ${units[unit]}`;
}

function millis(value) {
  if (value == null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n < 1000) return `${Math.round(n)} ms`;
  return `${(n / 1000).toFixed(1)} 秒`;
}

function beijing(iso) {
  if (!iso) return "—";
  const date = new Date(String(iso).replace("Z", "+00:00"));
  if (Number.isNaN(date.getTime())) return String(iso);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date).replace(/\//g, "-");
}

function label(map, key) {
  return map[key] || key;
}

function toast(message, error = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = error ? "toast error" : "toast";
  node.hidden = false;
  setTimeout(() => { node.hidden = true; }, 3200);
}

async function api(path, options) {
  const response = await fetch(path, options);
  let payload = {};
  try { payload = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(payload.detail || response.statusText);
  return payload;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function metric(title, value, hint) {
  const card = el("article", "metric-card");
  card.append(el("span", "", title), el("strong", "", value));
  if (hint) card.append(el("em", "", hint));
  return card;
}

function panel(title, children) {
  const article = el("article", "report-panel");
  article.append(el("h2", "", title));
  for (const child of children) article.append(child);
  return article;
}

function note(text) {
  return el("p", "note", text);
}

function bars(entries, toneFor) {
  const total = entries.reduce((sum, [, value]) => sum + (Number(value) || 0), 0);
  const list = el("div", "bar-list");
  if (!entries.length || total <= 0) {
    list.append(note("无记录"));
    return list;
  }
  for (const [key, raw] of entries) {
    const value = Number(raw) || 0;
    const row = el("div", "bar-row");
    row.append(el("span", "", label({ ...PATH_LABELS, ...NEED_LABELS, ...LIFE_LABELS }, key)));
    const track = el("div", "bar-track");
    const fill = el("i", toneFor ? toneFor(key) : "");
    fill.style.width = `${Math.max(1, (value / total) * 100)}%`;
    track.append(fill);
    row.append(track, el("em", "", number(value)));
    list.append(row);
  }
  return list;
}

function stack(parts, total) {
  const wrap = el("div");
  const bar = el("div", "stack");
  const legend = el("div", "stack-legend");
  const denom = total > 0 ? total : parts.reduce((sum, part) => sum + part.value, 0);
  for (const part of parts) {
    if (part.value <= 0) continue;
    const slice = el("i");
    slice.style.width = `${(part.value / denom) * 100}%`;
    slice.style.background = part.color;
    slice.title = `${part.label} ${duration(part.value)}`;
    bar.append(slice);
    legend.append(el("span", "", `${part.label} ${duration(part.value)}`));
  }
  if (!bar.childElementCount) wrap.append(note("无生命周期采样"));
  else wrap.append(bar, legend);
  return wrap;
}

function kv(rows) {
  const grid = el("div", "kv-grid");
  for (const [title, value] of rows) {
    const item = el("div");
    item.append(el("span", "", title), el("strong", "", value));
    grid.append(item);
  }
  return grid;
}

function details(rows) {
  const list = el("div", "detail-list");
  for (const [title, value] of rows) {
    const row = el("div", "detail-row");
    row.append(el("span", "", title), el("strong", "", value));
    list.append(row);
  }
  return list;
}

function table(headers, rows) {
  if (!rows.length) return note("无记录");
  const wrap = el("div", "table-wrap");
  const tableNode = el("table", "report-table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const header of headers) headRow.append(el("th", "", header));
  head.append(headRow);
  const body = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const cell of row) {
      const td = el("td");
      if (cell instanceof Node) td.append(cell);
      else td.textContent = cell == null ? "—" : String(cell);
      tr.append(td);
    }
    body.append(tr);
  }
  tableNode.append(head, body);
  wrap.append(tableNode);
  return wrap;
}

function objectEntries(value) {
  if (!value || typeof value !== "object") return [];
  return Object.entries(value).filter(([, item]) => item != null && item !== "");
}

function countMap(value) {
  if (!value || typeof value !== "object") return [];
  return Object.entries(value).map(([key, item]) => {
    if (item && typeof item === "object") {
      const total = Object.values(item).reduce((sum, inner) => sum + (Number(inner) || 0), 0);
      return [key, total];
    }
    return [key, Number(item) || 0];
  });
}

function renderDays(days, selected, lastFinalized) {
  const root = $("day-chips");
  root.replaceChildren();
  if (!days.length) {
    root.hidden = true;
    return;
  }
  root.hidden = false;
  for (const day of days.slice().reverse()) {
    const chip = el("button", `day-chip${day === selected ? " active" : ""}`, day);
    chip.type = "button";
    if (day === lastFinalized) chip.title = "最近归档";
    chip.addEventListener("click", () => {
      $("day").value = day;
      refresh();
    });
    root.append(chip);
  }
}

function renderSummary(report) {
  const run = report.current_run || {};
  const compression = run.compaction || {};
  const failures = report.failures || {};
  $("summary").replaceChildren(
    metric("本次运行", duration(run.uptime_seconds), run.started_at ? `启动于 ${beijing(run.started_at)}` : "该日未记录运行起点"),
    metric("连续健康运行", duration(run.stable_seconds), run.fresh ? "依据连续 RUNNING 健康采样" : "采样不足或已中断"),
    metric("已压缩上下文", compression.turns == null ? "—" : `${number(compression.turns)} 轮`, `${number(compression.operations)} 次压缩 · ${number(compression.messages)} 条消息`),
    metric("本次异常记录", run.available ? number((run.failures || []).length) : "—", "失败、重试与拒绝分项记录"),
    metric("今日异常记录", number((failures.items || []).length), failures.background_available ? "含后台任务记录" : "后台数据尚不可用"),
    metric("日记", report.diary && report.diary.wrote ? "已写" : "未写", "仅统计是否主动落笔"),
  );
}

function renderUptime(report) {
  const uptime = report.uptime || {};
  const life = uptime.lifecycle_seconds || {};
  const parts = Object.keys(LIFE_LABELS).map((key) => ({
    key,
    label: LIFE_LABELS[key],
    value: Number(life[key]) || 0,
    color: LIFE_COLORS[key],
  }));
  const run = report.current_run || {};
  const compression = run.compaction || {};
  return panel("运行状态", [
    stack(parts, Number(uptime.window_seconds) || 0),
    kv([
      ["采样缺口", `${number(uptime.gap_count)} 段 · ${duration(uptime.gap_seconds)}`],
      ["本次运行起点", beijing(run.started_at)],
      ["健康采样起点", beijing(run.stable_since)],
      ["上下文压缩轮数", number(compression.turns)],
      ["压缩次数 / 消息数", `${number(compression.operations)} / ${number(compression.messages)}`],
      ["今日压缩轮数", number((report.compaction_today || {}).turns)],
      ["重启估计", number(uptime.restart_estimate)],
      ["样本数", number(uptime.sample_count)],
    ]),
    note("压缩轮数按已提交摘要覆盖的用户输入事件去重统计；包含本次启动后压缩的旧对话。健康运行依据采样，不表示后台从未发生失败。"),
    note(report.telemetry_started_at ? `压缩与后台失败计时自 ${beijing(report.telemetry_started_at)} 开始记录。`
      : "旧版没有完整的压缩与后台失败时间记录。"),
    ...(compression.legacy_undated_operations ? [note(`另有 ${number(compression.legacy_undated_operations)} 次历史压缩未记录完成时间，不计入本次运行。`)] : []),
    ...(report.finalized ? [note("本次运行数据为归档时的快照；记忆与事件解释按当前有效来源重新读取。")] : []),
  ]);
}

function renderResources(report) {
  const resources = report.resources || {};
  const host = resources.host || {};
  const roles = objectEntries(resources.roles || {});
  const children = [
    kv([
      ["主机 CPU", `${number(host.cpu_mean, 1)}% / 峰 ${number(host.cpu_peak, 1)}%`],
      ["主机内存", `${number(host.mem_mean, 1)}% / 峰 ${number(host.mem_peak, 1)}%`],
      ["状态目录", bytes(resources.state_bytes)],
      ["采集", resources.available ? "可用" : "不可用"],
    ]),
  ];
  if (roles.length) {
    children.push(table(
      ["进程", "CPU 均值", "CPU 峰值", "RSS 均值", "RSS 峰值"],
      roles.map(([name, role]) => [
        name,
        `${number(role.cpu_mean, 1)}%`,
        `${number(role.cpu_peak, 1)}%`,
        bytes(role.rss_mean_bytes),
        bytes(role.rss_peak_bytes),
      ]),
    ));
  }
  const sqlite = objectEntries(resources.sqlite_bytes || {}).sort((a, b) => (b[1] || 0) - (a[1] || 0));
  if (sqlite.length) {
    children.push(table(["数据库", "大小"], sqlite.map(([name, size]) => [name, bytes(size)])));
  }
  return panel("资源占用", children);
}

function renderHomeostasis(report) {
  const homeo = report.homeostasis || {};
  const children = [
    kv([
      ["轨迹", number(homeo.trace_count)],
      ["显著 marked", number((homeo.salience || {}).marked)],
      ["episode", number(homeo.episode_count)],
      ["采集", homeo.available ? "可用" : "不可用"],
    ]),
  ];
  const needs = objectEntries(homeo.dominant_needs || {});
  children.push(needs.length ? bars(needs) : note("没有主导需要统计"));
  const affect = objectEntries(homeo.core_affect_mean || {}).filter(([key, value]) => key in AFFECT_LABELS && value != null);
  if (affect.length) {
    const list = el("div", "bar-list");
    for (const [key, value] of affect) {
      const n = Number(value) || 0;
      const row = el("div", "bar-row");
      row.append(el("span", "", label(AFFECT_LABELS, key)));
      const track = el("div", "bar-track");
      const fill = el("i", "cyan");
      fill.style.width = `${Math.max(2, Math.min(100, Math.abs(n) * 100))}%`;
      track.append(fill);
      row.append(track, el("em", "", number(n, 3)));
      list.append(row);
    }
    children.push(list);
  }
  const dims = objectEntries(homeo.dimensions || {}).filter(([, dim]) => dim && Object.values(dim).some((item) => item != null));
  if (dims.length) {
    children.push(table(
      ["维度", "起", "止", "最低", "最高", "净变化"],
      dims.map(([name, dim]) => [
        label(NEED_LABELS, name),
        number(dim.start, 3),
        number(dim.end, 3),
        number(dim.min, 3),
        number(dim.max, 3),
        number(dim.delta, 3),
      ]),
    ));
  } else {
    children.push(note("七维起止值当天未写入，无法画出变化幅度。"));
  }
  return panel("内稳态", children);
}

function intentText(intent) {
  const rows = [];
  const idle = intent.idle || {};
  rows.push(["空闲表达", `${number(idle.completed)} 完成 / ${number(idle.deferred)} 推迟 / ${number(idle.failed)} 失败`]);
  const board = intent.board || {};
  rows.push(["留言板", board.available === false ? "未接入" : `${number(board.write)} 写入 / ${number(board.wait)} 等待 / ${number(board.failed)} 失败`]);
  const qq = intent.qq || {};
  rows.push(["QQ 账本", qq.available === false ? "未接入" : "已接入"]);
  const marked = countMap(intent.marked || {});
  rows.push(["marked 触发", marked.length ? marked.map(([key, value]) => `${key} ${number(value)}`).join(" · ") : "无"]);
  const caps = intent.capabilities || {};
  if (caps.available === false) rows.push(["能力通道", "未接入"]);
  else {
    const channels = objectEntries(caps.by_channel || {});
    rows.push(["能力通道", channels.length
      ? channels.map(([name, stats]) => {
          const parts = objectEntries(stats || {}).map(([key, value]) => `${label(PATH_LABELS, key)} ${number(value)}`);
          return `${label(CHANNEL_LABELS, name)}：${parts.join(" / ")}`;
        }).join("；")
      : "无"]);
  }
  return details(rows);
}

function renderIntent(report) {
  const tokens = report.tokens || {};
  const qqTrace = tokens.qq_trace || {};
  const children = [intentText(report.intent || {})];
  if (qqTrace.turns) {
    children.push(note(`QQ trace 记录了 ${number(qqTrace.turns)} 轮，缓存命中 ${percent(qqTrace.hit_rate)}。`));
  }
  return panel("主动意图与通道", children);
}

function renderPipelines(report) {
  const pipes = report.pipelines || {};
  const children = [
    bars(objectEntries(pipes.paths || {}), (key) => PATH_TONES[key] || ""),
    kv([
      ["完成 / 失败", `${number((pipes.status || {}).completed)} / ${number((pipes.status || {}).failed)}`],
      ["局部重生成", number(pipes.revisions)],
      ["p50", millis((pipes.latency_ms || {}).p50)],
      ["p95", millis((pipes.latency_ms || {}).p95)],
      ["p99", millis((pipes.latency_ms || {}).p99)],
    ]),
  ];
  const byPath = objectEntries(pipes.latency_by_path || {});
  if (byPath.length) {
    children.push(table(
      ["路径", "p50", "p95"],
      byPath.map(([name, stats]) => [label(PATH_LABELS, name), millis(stats.p50), millis(stats.p95)]),
    ));
  }
  const stages = objectEntries(pipes.stages || {});
  if (stages.length) {
    const max = Math.max(...stages.map(([, stats]) => Number(stats.p95) || 0), 1);
    const list = el("div", "bar-list");
    for (const [name, stats] of stages.sort((a, b) => (b[1].p95 || 0) - (a[1].p95 || 0))) {
      const row = el("div", "bar-row");
      row.append(el("span", "", `${label(STAGE_LABELS, name)} ×${number(stats.count)}`));
      const track = el("div", "bar-track");
      const fill = el("i");
      fill.style.width = `${Math.max(2, ((Number(stats.p95) || 0) / max) * 100)}%`;
      track.append(fill);
      row.append(track, el("em", "", `均值 ${millis(stats.mean)} · p95 ${millis(stats.p95)}`));
      list.append(row);
    }
    children.push(list);
  }
  const events = objectEntries(pipes.event_types || {});
  if (events.length === 1 && events[0][0] === "unknown") {
    children.push(note(`事件类型未分类 ${number(events[0][1])} 条，日报没有更细的来源标签。`));
  } else if (events.length) {
    children.push(bars(events));
  }
  return panel("推理链路", children);
}

function renderTokens(report) {
  const tokens = report.tokens || {};
  const sources = [
    ["monitor 运行", tokens.monitor_runs],
    ["QQ trace", tokens.qq_trace],
  ].filter(([, value]) => value && (value.total_tokens || value.prompt_tokens));
  const combined = tokens.combined || {};
  const children = [
    kv([
      ["Prompt", number(combined.prompt_tokens)],
      ["Completion", number(combined.completion_tokens)],
      ["缓存命中", number(combined.cache_hit_tokens)],
      ["缓存未命中", number(combined.cache_miss_tokens)],
    ]),
  ];
  const list = el("div", "bar-list");
  for (const [name, stats] of sources) {
    const hit = Number(stats.hit_rate) || 0;
    const row = el("div", "bar-row");
    row.append(el("span", "", name));
    const track = el("div", "bar-track");
    const fill = el("i", "green");
    fill.style.width = `${Math.max(2, hit * 100)}%`;
    track.append(fill);
    row.append(track, el("em", "", `${percent(hit)} · ${number(stats.total_tokens || stats.prompt_tokens)} token`));
    list.append(row);
  }
  if (sources.length) children.push(list);
  const revision = report.memory_revision || {};
  children.push(note(
    revision.available
      ? `每日记忆修订 ${number(revision.job_count)} 个任务，${number(revision.actual_tokens)} token。`
      : "记忆修订数据不可用。",
  ));
  return panel("Token 与缓存", children);
}

function renderExtras(report) {
  const extras = report.extras || {};
  const alerts = extras.alerts || [];
  const children = [
    kv([
      ["warning / error", `${number(extras.warnings)} / ${number(extras.errors)}`],
      ["watchdog", number(extras.watchdog)],
      ["ingest 丢弃", number(extras.ingest_dropped)],
      ["超时 / 截断", `${number(extras.timeouts)} / ${number(extras.truncated)}`],
    ]),
  ];
  const goals = extras.goals || {};
  if (goals.available) {
    const budget = goals.budget || {};
    children.push(note(
      `目标系统${goals.enabled ? "已启用" : "未启用"}，最近一小时 ${number(budget.calls_last_hour)} / ${number(budget.maximum_calls_per_hour)} 次，episode ${number(goals.episode_count)}。`,
    ));
  }
  if (!alerts.length) {
    children.push(note("没有保留告警样本。"));
  } else {
    const list = el("div");
    for (const alert of alerts) {
      const payload = alert.payload || {};
      const detail = payload.detail || {};
      const item = el("div", "alert-item");
      const kind = label(KIND_LABELS, payload.decision_kind || alert.topic);
      const error = detail.error_type || detail.error_code || payload.status || alert.severity;
      item.append(el("strong", "", `${kind} · ${error}`));
      item.append(el("small", "", `${beijing(alert.observed_at)} · ${alert.event_id || payload.event_id || "无事件 ID"}`));
      list.append(item);
    }
    children.push(list);
  }
  return panel("告警与其它", children);
}

function render(report) {
  $("caption").textContent = `${report.day} · ${report.timezone || "Asia/Shanghai"} · 生成于 ${beijing(report.generated_at)}`;
  $("finalized").textContent = report.finalized ? "已归档" : "实时预览";
  $("finalized").className = report.finalized ? "status-chip" : "status-chip muted";
  renderSummary(report);
  const dashboard = $("dashboard");
  dashboard.replaceChildren(
    renderTimeline(report),
    renderMemories(report),
    renderUptime(report),
    el("div", "report-split"),
  );
  const split = dashboard.querySelector(".report-split");
  split.append(renderResources(report), renderHomeostasis(report));
  dashboard.append(
    el("div", "report-split"),
  );
  dashboard.lastElementChild.append(renderIntent(report), renderTokens(report));
  dashboard.append(renderFailures(report), renderPipelines(report), renderExtras(report));
  $("raw-json").textContent = JSON.stringify(report, null, 2);
}

async function loadReport() {
  const day = $("day").value || today();
  $("day").value = day;
  const report = await api(`/api/report/daily?day=${encodeURIComponent(day)}`);
  render(report);
}

async function loadDays() {
  try {
    const payload = await api("/api/report/days");
    const days = payload.days || [];
    renderDays(days, $("day").value, payload.last_finalized_day);
    return payload;
  } catch (_) {
    return { days: [] };
  }
}

async function loadDiary() {
  const status = await api("/api/diary");
  $("diary-status").textContent = status.wrote_today
    ? `今天已写日记（${status.today_count}）`
    : `今天未写日记；窗口 ${status.window_local}；${status.paused ? "已暂停" : (status.autonomous_enabled ? "可自主写" : "默认不主动写")}`;
  $("diary-pause").textContent = status.paused ? "恢复" : "暂停";
  $("diary-pause").dataset.paused = status.paused ? "1" : "0";
}

async function refresh() {
  try {
    await Promise.all([loadReport(), loadDiary(), loadDays()]);
    $("banner").hidden = true;
  } catch (error) {
    $("banner").hidden = false;
    $("banner").textContent = error.message;
  }
}

$("day").value = today();
$("day").addEventListener("change", refresh);
$("refresh").addEventListener("click", refresh);
$("finalize").addEventListener("click", async () => {
  try {
    await api("/api/report/finalize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ day: $("day").value || today() }),
    });
    toast("已归档");
    await refresh();
  } catch (error) {
    toast(error.message, true);
  }
});
$("diary-invite").addEventListener("click", async () => {
  try {
    await api("/api/diary/invite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: crypto.randomUUID() }),
    });
    toast("已递交日记机会");
    await loadDiary();
  } catch (error) {
    toast(error.message, true);
  }
});
$("diary-pause").addEventListener("click", async () => {
  const paused = $("diary-pause").dataset.paused === "1";
  try {
    await api(`/api/diary/${paused ? "resume" : "pause"}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    await loadDiary();
  } catch (error) {
    toast(error.message, true);
  }
});

(async () => {
  const days = await loadDays();
  if (!$("day").value && days.last_finalized_day) $("day").value = days.last_finalized_day;
  else if (days.last_finalized_day && $("day").value === today()) $("day").value = days.last_finalized_day;
  await refresh();
})();
