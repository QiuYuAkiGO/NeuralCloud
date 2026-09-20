"use strict";

const $ = (id) => document.getElementById(id);

const STATUS_TONE = {
  pass: "",
  fail: "error",
  unavailable: "error",
  "skipped(disabled)": "muted",
  "skipped(not_configured)": "muted",
  "no_recent_sample": "warning",
};

async function api(path, options) {
  const response = await fetch(path, options);
  let payload = {};
  try { payload = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(payload.detail || response.statusText);
  return payload;
}

function showError(message) {
  const banner = $("banner");
  banner.hidden = !message;
  banner.textContent = message || "";
}

function render(snapshot, latestOverride) {
  const latest = latestOverride || snapshot.latest;
  $("enabled-chip").textContent = snapshot.enabled ? "已启用" : "已关闭";
  $("enabled-chip").className = snapshot.enabled ? "status-chip" : "status-chip muted";
  $("generated").textContent = latest?.generated_at || "尚未运行";
  $("generated").className = latest ? "status-chip" : "status-chip muted";
  const grid = $("paths");
  grid.replaceChildren();
  for (const item of latest?.paths || []) {
    const card = document.createElement("article");
    card.className = "path-card";
    card.dataset.status = item.status;
    const title = document.createElement("h2");
    title.textContent = item.path;
    const status = document.createElement("span");
    status.className = `status-chip ${STATUS_TONE[item.status] || "muted"}`;
    status.textContent = item.status;
    const detail = document.createElement("p");
    const evidence = item.evidence || {};
    detail.textContent = Object.keys(evidence).slice(0, 4).map((key) => `${key}=${JSON.stringify(evidence[key])}`).join(" · ") || "无附加证据";
    card.append(title, status, detail);
    grid.append(card);
  }
  const history = $("history");
  history.replaceChildren();
  for (const name of snapshot.history || []) {
    const row = document.createElement("li");
    row.textContent = name;
    history.append(row);
  }
  $("raw-json").textContent = JSON.stringify(latestOverride || snapshot, null, 2);
}

async function load() {
  showError("");
  const snapshot = await api("/api/selfcheck");
  render(snapshot);
}

async function run() {
  showError("");
  const latest = await api("/api/selfcheck/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  const snapshot = await api("/api/selfcheck");
  render(snapshot, latest);
}

$("refresh").addEventListener("click", () => load().catch((error) => showError(error.message)));
$("run").addEventListener("click", () => run().catch((error) => showError(error.message)));
load().catch((error) => showError(error.message));
