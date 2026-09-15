"use strict";

const $ = (id) => document.getElementById(id);

function today() {
  const now = new Date();
  const offset = now.getTimezoneOffset();
  const local = new Date(now.getTime() - offset * 60000);
  return local.toISOString().slice(0, 10);
}

function number(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat("zh-CN").format(n);
}

function percent(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
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

function card(title, rows) {
  const article = document.createElement("article");
  article.className = "report-card";
  const heading = document.createElement("h2");
  heading.textContent = title;
  article.append(heading);
  const list = document.createElement("dl");
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  }
  article.append(list);
  return article;
}

function render(report) {
  $("caption").textContent = `${report.day} · ${report.timezone} · 生成于 ${report.generated_at || "—"}`;
  $("finalized").textContent = report.finalized ? "已归档" : "实时预览";
  $("finalized").className = report.finalized ? "status-chip" : "status-chip muted";
  const uptime = report.uptime || {};
  const resources = report.resources || {};
  const homeo = report.homeostasis || {};
  const intent = report.intent || {};
  const pipes = report.pipelines || {};
  const tokens = (report.tokens || {}).combined || {};
  const revision = report.memory_revision || {};
  const extras = report.extras || {};
  const host = resources.host || {};
  const cards = $("cards");
  cards.replaceChildren(
    card("运行时长", [
      ["采样覆盖（秒）", number(uptime.sampled_seconds)],
      ["采样缺口", `${number(uptime.gap_count)} / ${number(uptime.gap_seconds)} 秒`],
      ["当前连续运行（秒）", number(uptime.current_continuous_seconds)],
      ["重启估计", number(uptime.restart_estimate)],
    ]),
    card("资源占用", [
      ["主机 CPU 均值/峰值", `${number(host.cpu_mean)} / ${number(host.cpu_peak)}`],
      ["主机内存均值/峰值", `${number(host.mem_mean)} / ${number(host.mem_peak)}`],
      ["状态目录", number(resources.state_bytes)],
      ["可用", resources.available ? "是" : "否"],
    ]),
    card("内稳态", [
      ["轨迹条数", number(homeo.trace_count)],
      ["显著度", JSON.stringify(homeo.salience || {})],
      ["主导需要", JSON.stringify(homeo.dominant_needs || {})],
      ["episode", number(homeo.episode_count)],
    ]),
    card("主动意图", [
      ["marked", JSON.stringify(intent.marked || {})],
      ["IDLE", JSON.stringify(intent.idle || {})],
      ["留言板", JSON.stringify(intent.board || {})],
      ["QQ", JSON.stringify(intent.qq || {})],
    ]),
    card("链路", [
      ["运行次数", number(pipes.run_count)],
      ["fast/deep", JSON.stringify(pipes.paths || {})],
      ["p50 / p95 ms", `${number((pipes.latency_ms || {}).p50)} / ${number((pipes.latency_ms || {}).p95)}`],
      ["事件类型", JSON.stringify(pipes.event_types || {})],
    ]),
    card("记忆整理", [
      ["可用", revision.available ? "是" : "否"],
      ["任务数", number(revision.job_count)],
      ["实际 tokens", number(revision.actual_tokens)],
    ]),
    card("Token 与缓存", [
      ["prompt", number(tokens.prompt_tokens)],
      ["completion", number(tokens.completion_tokens)],
      ["缓存命中率", percent(tokens.hit_rate)],
      ["估算成本 USD", number(tokens.estimated_usd)],
    ]),
    card("告警与其它", [
      ["warning / error", `${number(extras.warnings)} / ${number(extras.errors)}`],
      ["watchdog", number(extras.watchdog)],
      ["ingest_dropped", number(extras.ingest_dropped)],
      ["日记", (report.diary && report.diary.wrote) ? "是" : "否"],
    ]),
  );
}

async function loadReport() {
  const day = $("day").value || today();
  $("day").value = day;
  const report = await api(`/api/report/daily?day=${encodeURIComponent(day)}`);
  render(report);
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
    await Promise.all([loadReport(), loadDiary()]);
    $("banner").hidden = true;
  } catch (error) {
    $("banner").hidden = false;
    $("banner").textContent = error.message;
  }
}

$("day").value = today();
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
refresh();
