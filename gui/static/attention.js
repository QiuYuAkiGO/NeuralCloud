/* User control is independent of the dialogue queue and model availability. */
(() => {
  const element = id => document.getElementById(id);
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  async function refresh() {
    const response = await fetch("/api/qq/attention");
    if (!response.ok) throw new Error(`群聊接收控制不可用 (${response.status})`);
    const data = await response.json();
    element("attention-policies").innerHTML = data.policies.map(policy => `<article class="card">
      <h3>群 ${escape(policy.group_id)} · ${policy.mode === "closed" ? (policy.origin === "model" ? "自主关闭" : "用户关闭") : "接收开放"}</h3>
      <p>${escape(policy.reason || "默认接收")}</p>
      <p class="hint">策略版本 ${escape(policy.revision)} · 复查 ${escape(policy.next_review_at || "无需复查")}</p>
      <button class="btn ghost" data-attention-group="${escape(policy.group_id)}" data-attention-mode="${policy.mode === "closed" ? "open" : "closed"}">${policy.mode === "closed" ? "手动恢复接收" : "手动关闭接收"}</button>
    </article>`).join("");
    const counts = data.dispositions || {};
    const outcomes = data.outcomes || {};
    element("attention-status").textContent = `最近一小时：预算暂缓 ${counts.attention_deferred || 0}；策略关闭 ${counts.attention_closed || 0}；模型选择不回复 ${outcomes.model_wait || 0}；运行故障 ${outcomes.runtime_failure || 0}。` + (data.review_error ? ` 复查异常：${data.review_error}` : "");
  }
  const report = error => { element("attention-status").textContent = error.message; };
  element("btn-attention").addEventListener("click", () => { element("attention-dialog").showModal(); refresh().catch(report); });
  element("btn-attention-close").addEventListener("click", () => element("attention-dialog").close());
  element("btn-attention-refresh").addEventListener("click", () => refresh().catch(report));
  element("attention-policies").addEventListener("click", async event => {
    const button = event.target.closest("[data-attention-group]");
    if (!button) return;
    button.disabled = true;
    try {
      const response = await fetch("/api/qq/attention", {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({group_id:button.dataset.attentionGroup, mode:button.dataset.attentionMode, reason:"用户通过控制面板手动调整接收"})});
      if (!response.ok) throw new Error(`接收策略更新失败 (${response.status})`);
      await refresh();
    } catch (error) { report(error); button.disabled = false; }
  });
})();
