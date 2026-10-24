"use strict";

const $ = (id) => document.getElementById(id);
const fieldNames = ["provider", "base_url", "body_instance_id", "timeout_seconds", "max_results", "daily_requests", "autonomous_daily_requests"];
const authorizationNames = ["enabled", "conversation_enabled", "autonomous_enabled"];
let savedRevision = 0;
let ready = false;

function message(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
}

function busy(value) {
  ["save", "test", "refresh"].forEach((id) => { $(id).disabled = value; });
}

const errors = {
  web_config_revision_conflict: "配置已被其他页面更新，请刷新后重新修改。",
  web_enabled_requires_api_key: "启用联网前需要配置 API Key。",
  web_api_key_required: "请先输入 API Key，或使用已保存的密钥。",
  web_config_request_invalid: "配置无效，请检查端点与调用限额。",
  web_api_key_invalid: "API Key 格式不正确。",
  web_connection_test_failed: "连接测试失败，请检查端点、密钥和服务状态。",
  same_origin_required: "请求来源不匹配，请从当前 GUI 页面操作。",
};

async function request(path, options) {
  const response = await fetch(path, options);
  let body;
  try { body = await response.json(); } catch { throw new Error("服务返回无效响应。"); }
  if (!response.ok) throw new Error(errors[body.detail] || "请求失败，请检查 Runtime 是否在线。");
  return body;
}

function fill(config, status) {
  savedRevision = config.saved_revision;
  fieldNames.forEach((name) => { $(name).value = config.settings[name]; });
  const auth = config.authorization || status?.authorization || {};
  authorizationNames.forEach((name) => { $(name).checked = Boolean(auth[name]); });
  $("api_key").value = "";
  $("api_key").placeholder = config.api_key_configured ? "已配置，留空则保留" : "输入服务商 API Key";
  $("clear_api_key").checked = false;
  $("active_revision").textContent = config.active_revision || "尚未配置";
  $("saved_revision").textContent = config.saved_revision || "尚未保存";
  $("key_status").textContent = config.api_key_configured ? "已配置" : "未配置";
  $("restart").textContent = config.restart_required ? "有待生效版本。请正常停止并重新启动共享 Runtime。" : config.active_revision ? "当前版本已生效。" : "尚未配置联网服务。";
  if (config.load_error) $("restart").textContent += " 配置或密钥读取失败，可重新填写并保存修复。";
  renderStatus(status);
}

function renderStatus(status) {
  const body = status?.body;
  const capabilities = body?.effective_capabilities || [];
  $("body-status").textContent = status?.enabled ? "已启用" : "未启用";
  $("search-status").textContent = !status?.enabled ? "未启用" : capabilities.includes("web.search.v1") ? "可用" : "暂不可用";
  $("read-status").textContent = !status?.enabled ? "未启用" : capabilities.includes("web.read.v1") ? "可用" : "暂不可用";
  const usage = status?.usage || {};
  $("request-usage").textContent = `${usage.requests ?? 0} / ${usage.daily_limit ?? "—"}`;
  $("autonomous-usage").textContent = `${usage.autonomous_requests ?? 0} / ${usage.autonomous_daily_limit ?? "—"}`;
  $("autonomous-status").textContent = status?.autonomous_enabled ? "自主联网已授权，仍需经过协商规则。" : status?.authorization?.autonomous_enabled && !status?.autonomous_global_enabled ? "已授权自主联网，全局自主能力开关尚未开启。" : "当前未启用自主联网。";
  $("runtime-status").textContent = JSON.stringify(status || {}, null, 2);
}

function testSummary(result) {
  const checks = Array.isArray(result?.checks) ? result.checks : [];
  const passed = result?.status === "completed" && ["web.search.v1", "web.read.v1"].every((capability) => checks.some((check) => check.capability === capability && check.status === "completed"));
  return { passed, text: passed ? "搜索与网页提取测试通过。配置尚未保存。" : "测试仅部分完成，尚未验证搜索与网页提取均可用。请查看诊断详情。" };
}

function payload() {
  const settings = {};
  fieldNames.forEach((name) => { settings[name] = ["provider", "base_url", "body_instance_id"].includes(name) ? $(name).value.trim() : Number($(name).value); });
  const authorization = {};
  authorizationNames.forEach((name) => { authorization[name] = $(name).checked; });
  return { base_revision: savedRevision, settings, authorization, api_key: $("api_key").value.trim() || null, clear_api_key: $("clear_api_key").checked };
}

async function refresh() {
  busy(true);
  try {
    const [config, status] = await Promise.all([request("/api/web/config"), request("/api/web/status")]);
    fill(config, status);
    ready = true;
    message(config.restart_required ? "配置已保存，重启 Runtime 后生效。" : "配置已载入。连接测试会发起少量实际请求。");
  } catch (error) { message(error.message, true); }
  finally { busy(false); }
}

$("provider").addEventListener("change", () => {
  $("base_url").value = $("provider").value === "deepseek" ? "https://api.deepseek.com" : "https://api.tavily.com";
  $("api_key").value = "";
  message("服务商已切换，请输入对应 API Key 后测试并保存。");
});

$("clear_api_key").addEventListener("change", () => {
  if ($("clear_api_key").checked) {
    $("api_key").value = "";
    authorizationNames.forEach((name) => { $(name).checked = false; });
  }
});

$("web-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!ready) return message("请先成功读取当前配置。", true);
  busy(true);
  try {
    const config = await request("/api/web/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
    const status = await request("/api/web/status").catch(() => ({}));
    fill(config, status);
    message("新版本已保存。当前运行版本保持不变，重启 Runtime 后应用。");
  } catch (error) { message(error.message, true); }
  finally { busy(false); }
});

$("test").addEventListener("click", async () => {
  if (!ready || !$("web-form").reportValidity()) return;
  busy(true);
  message("正在测试搜索与网页提取…");
  try {
    const result = await request("/api/web/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
    const summary = testSummary(result);
    const status = await request("/api/web/status").catch(() => null);
    if (status) renderStatus(status);
    $("test-status").textContent = summary.text;
    $("runtime-status").textContent = JSON.stringify({ status, test: result }, null, 2);
    message(summary.text, !summary.passed);
  } catch (error) { message(error.message, true); }
  finally { busy(false); }
});

$("refresh").addEventListener("click", refresh);
refresh();
