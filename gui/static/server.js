"use strict";

const $ = (id) => document.getElementById(id);
const fieldNames = ["host", "port", "username", "host_key", "body_instance_id", "connect_timeout_seconds", "default_timeout_seconds", "daily_commands", "autonomous_daily_commands", "approval_timeout_seconds"];
const authorizationNames = ["enabled", "autonomous_enabled"];
let savedRevision = 0;
let ready = false;

function message(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
}

function busy(value) {
  ["save", "test", "refresh", "generate"].forEach((id) => { $(id).disabled = value; });
}

const errors = {
  server_config_revision_conflict: "配置已被其他页面更新，请刷新后重新修改。",
  server_enabled_requires_key_and_host_key: "启用前需要配置私钥和主机公钥或指纹。",
  server_private_key_required: "请先提供私钥，或使用已保存 / 刚生成的密钥。",
  server_config_request_invalid: "配置无效，请检查主机、密钥与限额。",
  server_private_key_invalid: "私钥格式不正确。",
  server_host_key_invalid: "主机公钥或指纹格式不正确。",
  server_connection_test_failed: "连接测试失败，请检查主机、密钥和指纹。",
  server_host_key_unverified: "主机密钥与配置的公钥或指纹不匹配。",
  server_authentication_failed: "SSH 认证失败。",
  server_key_generation_failed: "密钥生成失败。",
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
  const settings = config.settings || {};
  fieldNames.forEach((name) => {
    if ($(name).type === "checkbox") $(name).checked = Boolean(settings[name]);
    else $(name).value = settings[name] ?? "";
  });
  $("sudo_requires_approval").checked = Boolean(settings.sudo_requires_approval);
  $("extra_high_risk_patterns").value = (settings.extra_high_risk_patterns || []).join("\n");
  $("forbidden_patterns").value = (settings.forbidden_patterns || []).join("\n");
  const auth = config.authorization || status?.authorization || {};
  authorizationNames.forEach((name) => { $(name).checked = Boolean(auth[name]); });
  $("private_key").value = "";
  $("private_key").placeholder = config.private_key_configured ? "已配置，留空则保留" : "粘贴 OpenSSH 私钥";
  $("clear_private_key").checked = false;
  $("use_generated_key").checked = Boolean(config.pending_generated_key);
  $("active_revision").textContent = config.active_revision || "尚未配置";
  $("saved_revision").textContent = config.saved_revision || "尚未保存";
  $("key_status").textContent = config.private_key_configured ? "已配置" : "未配置";
  $("restart").textContent = config.restart_required ? "有待生效版本。请正常停止并重新启动共享 Runtime。" : config.active_revision ? "当前版本已生效。" : "尚未配置服务器身体。";
  if (config.load_error) $("restart").textContent += " 配置或密钥读取失败，可重新填写并保存修复。";
  renderStatus(status);
}

function renderStatus(status) {
  const body = status?.body;
  const capabilities = body?.effective_capabilities || [];
  $("body-status").textContent = status?.enabled ? "已启用" : "未启用";
  $("observe-status").textContent = !status?.enabled ? "未启用" : capabilities.includes("server.observe.v1") ? "可用" : "暂不可用";
  $("execute-status").textContent = !status?.enabled ? "未启用" : capabilities.includes("server.execute.v1") ? "可用" : "暂不可用";
  const usage = status?.usage || {};
  $("command-usage").textContent = `${usage.commands ?? 0} / ${usage.daily_limit ?? "—"}`;
  $("pending-count").textContent = String(status?.pending_approvals ?? 0);
  $("autonomous-status").textContent = status?.autonomous_enabled ? "自主服务器能力已授权，仍需经过协商与高危审批。" : status?.authorization?.autonomous_enabled && !status?.autonomous_global_enabled ? "已授权自主服务器能力，全局自主能力开关尚未开启。" : "当前未启用自主服务器能力。";
  $("runtime-status").textContent = JSON.stringify(status || {}, null, 2);
}

function payload() {
  const settings = { sudo_requires_approval: $("sudo_requires_approval").checked };
  fieldNames.forEach((name) => {
    const numeric = ["port", "connect_timeout_seconds", "default_timeout_seconds", "daily_commands", "autonomous_daily_commands", "approval_timeout_seconds"];
    settings[name] = numeric.includes(name) ? Number($(name).value) : $(name).value.trim();
  });
  const lines = (id) => $(id).value.split(/\n/).map((item) => item.trim()).filter(Boolean);
  settings.extra_high_risk_patterns = lines("extra_high_risk_patterns");
  settings.forbidden_patterns = lines("forbidden_patterns");
  const authorization = {};
  authorizationNames.forEach((name) => { authorization[name] = $(name).checked; });
  return {
    base_revision: savedRevision,
    settings,
    authorization,
    private_key: $("private_key").value.trim() || null,
    clear_private_key: $("clear_private_key").checked,
    use_generated_key: $("use_generated_key").checked,
  };
}

function clearNode(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function addText(parent, tag, text, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  parent.appendChild(node);
  return node;
}

function renderCommands(target, items, pending) {
  clearNode(target);
  if (!items.length) {
    addText(target, "p", pending ? "没有等待批准的命令。" : "暂无记录。", "hint");
    return;
  }
  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "card";
    addText(card, "p", `${item.status} · ${item.risk_class || ""} · ${item.command_id}`);
    const command = addText(card, "pre", item.command || "");
    command.setAttribute("spellcheck", "false");
    if (item.stdout) addText(card, "pre", item.stdout);
    if (pending) {
      const actions = document.createElement("div");
      actions.className = "card-actions";
      const approve = document.createElement("button");
      approve.type = "button";
      approve.className = "primary";
      approve.textContent = "批准执行";
      approve.addEventListener("click", () => operate(item.command_id, "approve"));
      const reject = document.createElement("button");
      reject.type = "button";
      reject.className = "danger";
      reject.textContent = "拒绝";
      reject.addEventListener("click", () => operate(item.command_id, "reject"));
      actions.appendChild(approve);
      actions.appendChild(reject);
      card.appendChild(actions);
    }
    target.appendChild(card);
  });
}

async function operate(commandId, operation) {
  busy(true);
  try {
    await request(`/api/server/commands/${encodeURIComponent(commandId)}/${operation}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    message(operation === "approve" ? "已批准并执行。" : "已拒绝该命令。");
    await refresh();
  } catch (error) { message(error.message, true); }
  finally { busy(false); }
}

async function refresh() {
  busy(true);
  try {
    const [config, status, pending, recent] = await Promise.all([
      request("/api/server/config"),
      request("/api/server/status"),
      request("/api/server/commands?status=pending_approval"),
      request("/api/server/commands"),
    ]);
    fill(config, status);
    renderCommands($("pending-list"), pending.commands || [], true);
    renderCommands($("recent-list"), (recent.commands || []).slice(0, 12), false);
    ready = true;
    message(config.restart_required ? "配置已保存，重启 Runtime 后生效。" : "配置已载入。");
  } catch (error) { message(error.message, true); }
  finally { busy(false); }
}

$("clear_private_key").addEventListener("change", () => {
  if ($("clear_private_key").checked) {
    $("private_key").value = "";
    $("use_generated_key").checked = false;
    authorizationNames.forEach((name) => { $(name).checked = false; });
  }
});

$("server-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!ready) return message("请先成功读取当前配置。", true);
  busy(true);
  try {
    const config = await request("/api/server/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
    const status = await request("/api/server/status").catch(() => ({}));
    fill(config, status);
    message("新版本已保存。当前运行版本保持不变，重启 Runtime 后应用。");
  } catch (error) { message(error.message, true); }
  finally { busy(false); }
});

$("test").addEventListener("click", async () => {
  if (!ready || !$("server-form").reportValidity()) return;
  busy(true);
  message("正在测试 SSH 连接…");
  try {
    const result = await request("/api/server/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
    const passed = result?.status === "completed";
    const status = await request("/api/server/status").catch(() => null);
    if (status) renderStatus(status);
    $("test-status").textContent = passed ? "连接测试通过。配置尚未保存。" : "测试未完全成功，请查看诊断详情。";
    $("runtime-status").textContent = JSON.stringify({ status, test: result }, null, 2);
    message($("test-status").textContent, !passed);
  } catch (error) { message(error.message, true); }
  finally { busy(false); }
});

$("generate").addEventListener("click", async () => {
  busy(true);
  try {
    const result = await request("/api/server/keys/generate", { method: "POST" });
    $("use_generated_key").checked = true;
    $("public-key").textContent = "请把下面的公钥安装到目标账号的 authorized_keys：\n" + (result.public_key || "");
    message("已生成密钥。保存配置时勾选“使用刚生成的密钥对”。公钥不会写入浏览器存储。");
  } catch (error) { message(error.message, true); }
  finally { busy(false); }
});

$("refresh").addEventListener("click", refresh);
refresh();
