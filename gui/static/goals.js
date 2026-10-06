"use strict";

// Every runtime/model string is rendered as text. Evidence IDs never become URLs.
const GoalView = (() => {
  const statusLabels = {
    candidate: "待评估", active: "进行中", observing: "观察中", queued: "等待处理",
    planned: "计划中", pending: "待确认", running: "进行中", executing: "执行中",
    completed: "已完成", succeeded: "已核验", committed: "已记录", cancelled: "已取消",
    canceled: "已取消", abandoned: "已放弃", paused: "已暂停", blocked: "暂时受阻",
    waiting: "等待复查", wait: "等待", deferred: "稍后复查", review_pending: "等待复查",
    reviewing: "复查中", unknown: "结果未知", failed: "未完成", error: "出现错误",
    improved: "代理状态改善", unchanged: "无明确变化", worsened: "代理状态恶化",
    stable: "保持稳定", self_report_only: "仅有自评", effect_pending: "效果待验证",
  };
  const dimensionLabels = {
    energy_reserve: "可用精力", cognitive_load: "认知负荷", safety_predictability: "安全与可预测性",
    competence: "胜任感", autonomy: "自主性", social_connection: "社会连接", novelty: "新鲜感",
  };
  const terminal = new Set(["completed", "cancelled", "canceled", "abandoned"]);
  const expanded = new Set();
  const array = value => Array.isArray(value) ? value : [];
  const object = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const words = value => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value);
  const number = value => typeof value === "number" && Number.isFinite(value) ? value.toFixed(3) : "—";
  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = words(text);
    if (className) item.className = className;
    return item;
  }
  function time(value) {
    if (!value) return "尚未安排";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? words(value) : parsed.toLocaleString("zh-CN", {hour12: false});
  }
  function badge(status) {
    const kind = ["completed", "succeeded", "committed", "improved", "stable"].includes(status) ? "good"
      : ["failed", "error", "worsened", "unknown"].includes(status) ? "bad"
        : ["waiting", "wait", "blocked", "paused", "pending", "deferred", "effect_pending"].includes(status) ? "wait" : "";
    return node("span", statusLabels[status] || status || "尚未开始", `tag ${kind}`);
  }
  function evidence(refs) {
    const items = array(refs);
    if (!items.length) return node("p", "尚无独立证据。", "meta");
    const list = node("ul", undefined, "evidence-list");
    for (const ref of items) {
      const li = node("li");
      li.append(node("code", typeof ref === "object" ? ref.ref || ref.id || words(ref) : ref));
      list.append(li);
    }
    return list;
  }
  function evidenceDetails(refs, label = "证据引用") {
    if (!array(refs).length) return node("p", "结果尚无证据引用。", "meta");
    const details = node("details", undefined, "evidence-details");
    rememberExpansion(details, JSON.stringify([label, refs]));
    details.append(node("summary", `${label} · ${refs.length}`), evidence(refs));
    return details;
  }
  function rememberExpansion(details, key) {
    details.open = expanded.has(key);
    details.addEventListener("toggle", () => {
      if (details.open) expanded.add(key); else expanded.delete(key);
    });
  }
  function metadata(record, container) {
    if (record.blocked_reason) container.append(node("p", `等待原因：${words(record.blocked_reason)}`, "meta"));
    if (record.next_review_at) container.append(node("p", `下次复查：${time(record.next_review_at)}`, "meta"));
  }
  function goalCard(goal, {enabled = false, busy = false, onControl = () => {}} = {}) {
    const card = node("article", undefined, "card");
    const heading = node("div", undefined, "card-heading");
    heading.append(node("h3", goal.statement || "未命名目标", "statement"), badge(goal.status));
    card.append(heading);
    const origin = {user_task: "用户目标", user: "用户目标", model_proposal: "自主提议", maintenance: "持续调节"}[goal.origin] || goal.origin;
    if (origin || goal.created_at) card.append(node("p", [origin, goal.created_at && time(goal.created_at)].filter(Boolean).join(" · "), "meta"));
    if (array(goal.success_criteria).length) {
      const list = node("ul", undefined, "goal-criteria");
      array(goal.success_criteria).forEach(item => list.append(node("li", item)));
      card.append(list);
    }
    metadata(goal, card);
    const id = goal.id || goal.goal_id;
    if (id && !terminal.has(goal.status)) {
      const actions = node("div", undefined, "actions");
      const operation = goal.status === "paused" ? "resume" : "pause";
      const controls = [[operation, operation === "resume" ? "恢复目标" : "暂停目标"]];
      if (id !== "psychological-balance") controls.push(["cancel", "取消目标"]);
      for (const [op, label] of controls) {
        const button = node("button", label, op === "cancel" ? "quiet danger" : "quiet");
        button.type = "button";
        button.disabled = busy || !enabled;
        button.addEventListener("click", () => onControl(id, op));
        actions.append(button);
      }
      card.append(actions);
    }
    return card;
  }
  function episodeCard(episode) {
    if (!episode) return node("p", "目前没有进行中的调节。Misty 可以先观察或等待。", "empty");
    const card = node("article", undefined, "card");
    const heading = node("div", undefined, "card-heading");
    heading.append(node("h3", episode.statement || "调节尝试", "statement"), badge(episode.status));
    card.append(heading);
    if (array(episode.target_dimensions).length) card.append(node("p", `关注：${episode.target_dimensions.map(key => dimensionLabels[key] || key).join(" · ")}`, "meta"));
    metadata(episode, card);
    if (episode.evaluation_started_at) card.append(node("p", `本次计划评价起点：${time(episode.evaluation_started_at)}`, "meta"));
    if (episode.end_reason) card.append(node("p", `结束原因：${words(episode.end_reason)}`, "meta"));
    if (episode.effect_status === "pending") card.append(node("p", "行动已结束，调节效果仍在复查。", "meta"));
    if (array(episode.steps).length) {
      const list = node("ol", undefined, "steps");
      for (const step of episode.steps) {
        const li = node("li"), title = node("div", undefined, "step-heading");
        title.append(node("span", step.statement || "待补充步骤"), badge(step.status));
        li.append(title);
        if (step.completion_criteria) li.append(node("p", `核验标准：${array(step.completion_criteria).length ? step.completion_criteria.join("；") : words(step.completion_criteria)}`, "meta"));
        if (array(step.evidence_refs).length) li.append(evidenceDetails(step.evidence_refs));
        list.append(li);
      }
      card.append(list);
    } else card.append(node("p", "尚未形成执行步骤。", "meta"));
    if (array(episode.results).length) {
      const results = node("details", undefined, "evidence-details");
      rememberExpansion(results, `results:${episode.id || episode.episode_id}`);
      results.append(node("summary", `实际执行记录 · ${episode.results.length}`));
      for (const result of episode.results) {
        const item = node("div", undefined, "result-group");
        item.append(node("p", [result.at && time(result.at), statusLabels[result.status] || result.status, result.call_id].filter(Boolean).join(" · "), "meta"));
        item.append(evidence(result.evidence_refs));
        results.append(item);
      }
      card.append(results);
    }
    return card;
  }
  function evaluationCard(review) {
    const card = node("article", undefined, "card"), heading = node("div", undefined, "card-heading");
    heading.append(node("h3", time(review.at)), badge(review.outcome || review.status));
    card.append(heading);
    if (review.episode_id) card.append(node("p", `调节记录：${review.episode_id}`, "meta"));
    card.append(node("p", "此结果比较当前计划的起始状态，不代表每次复查都再次改善或恶化。", "meta"));
    if (review.interval_outcome) card.append(node("p", `与上次观测比较：${statusLabels[review.interval_outcome] || review.interval_outcome}`, "meta"));
    const before = object(review.before), after = object(review.after);
    const dimensions = [...new Set([...Object.keys(before), ...Object.keys(after)])];
    if (dimensions.length) {
      const trends = node("div", undefined, "result-group");
      trends.append(node("h4", "代理状态 · 计划起点 → 复查时"));
      for (const key of dimensions) {
        const row = node("div", undefined, "trend");
        const delta = typeof before[key] === "number" && typeof after[key] === "number" ? after[key] - before[key] : null;
        row.append(node("span", dimensionLabels[key] || key), node("span", `${number(before[key])} → ${number(after[key])}`),
          node("span", delta == null ? "—" : `${delta > 0 ? "+" : ""}${number(delta)}`, "trend-change"));
        trends.append(row);
      }
      trends.append(node("p", "数值变化的方向不直接代表改善；应结合目标区间与下面的反馈来源。", "meta"));
      card.append(trends);
    }
    const groups = node("div", undefined, "result-grid");
    const facts = node("div", undefined, "result-group"), self = node("div", undefined, "result-group"), drift = node("div", undefined, "result-group wide");
    const sources = object(review.sources);
    facts.append(node("h4", "事实证据"), evidence(sources.fact));
    self.append(node("h4", "自我评价"), array(sources.self_report).length ? evidence(sources.self_report) : node("p", "未记录自评。", "meta"));
    drift.append(node("h4", "时间漂移"), node("p", sources.drift === true ? "本次变化包含随时间产生的漂移，不能全部归因于行动。" : sources.drift === false ? "本次记录未标记时间漂移。" : "尚未完成漂移归因。", "meta"));
    groups.append(facts, self, drift);
    card.append(groups);
    const contributions = object(review.contributions);
    const amounts = node("details", undefined, "evidence-details");
    rememberExpansion(amounts, `contributions:${review.id}`);
    amounts.append(node("summary", "状态变化来源 · 本次活动累计"));
    let hasAmounts = false;
    for (const [source, label] of [["fact", "已验证事件"], ["self_report", "受限自评"], ["drift", "自然漂移"], ["legacy_event", "其他已记录事件"], ["coupling", "维度耦合"]]) {
      const entries = Object.entries(object(contributions[source])).filter(([key, value]) => key !== "energy_reserve" && typeof value === "number" && Number.isFinite(value));
      if (!entries.length) continue;
      hasAmounts = true;
      const group = node("div", undefined, "result-group");
      group.append(node("h4", label));
      entries.forEach(([key, value]) => group.append(node("p", `${dimensionLabels[key] || key}：${value > 0 ? "+" : ""}${number(value)}`, "meta")));
      amounts.append(group);
    }
    if (hasAmounts) {
      amounts.append(node("p", "这里分解已记录的状态更新；尚未独立证实行动造成了改善。多次复查是累计值，不能逐次相加。", "meta"));
      card.append(amounts);
    }
    if (review.stable_since) card.append(node("p", `稳定记录起点：${time(review.stable_since)}`, "meta"));
    if (array(review.evidence_refs).length) card.append(evidenceDetails(review.evidence_refs, "复查证据"));
    return card;
  }
  function render(snapshot, options = {}) {
    const el = id => document.getElementById(id);
    const goals = array(snapshot.goals), episodes = array(snapshot.episodes), reviews = array(snapshot.evaluations);
    const writable = {enabled: snapshot.enabled === true && !options.offline, busy: options.busy, onControl: options.onControl};
    el("goal-count").textContent = `${goals.filter(goal => !terminal.has(goal.status)).length} 项未结束`;
    el("goals").replaceChildren(...(goals.length ? goals.map(goal => goalCard(goal, writable)) : [node("p", "还没有用户目标。可以从右侧添加一个具体的小目标。", "empty")]));
    el("active-episode").replaceChildren(episodeCard(snapshot.active_episode));
    el("evaluations").replaceChildren(...(reviews.length ? [...reviews].reverse().map(evaluationCard) : [node("p", "还没有复查记录。不会把行动成功自动写成调节成功。", "empty")]));
    const history = episodes.filter(episode => (episode.id || episode.episode_id) !== (snapshot.active_episode?.id || snapshot.active_episode?.episode_id));
    el("episode-count").textContent = `${history.length} 次`;
    el("episodes").replaceChildren(...(history.length ? [...history].reverse().map(episodeCard) : [node("p", "还没有过往调节记录。", "empty")]));
    const standing = snapshot.standing_goal;
    el("standing-goal").replaceChildren(standing ? goalCard(standing, writable) : node("p", "尚未配置持续方向。", "empty"));
    const budget = object(snapshot.budget), schedule = node("dl", undefined, "schedule-list");
    for (const [label, value] of [["最近一小时目标评估", budget.calls_last_hour], ["每小时目标评估上限", budget.maximum_calls_per_hour]]) {
      schedule.append(node("dt", label), node("dd", value == null ? "未提供" : value));
    }
    el("schedule").replaceChildren(schedule, node("p", "此预算统计目标评估；各渠道另有既有的执行期限与冷却。", "meta"), node("p", `下次复查：${time(snapshot.next_review_at)}`, "schedule-next"));
    for (const fault of array(snapshot.health?.request_faults)) {
      el("schedule").append(node("p", `自主请求受阻：${fault.code}。已停止重复尝试，等待请求契约或配置修复。`, "error"));
    }
    if (snapshot.activity_metrics) el("schedule").append(node("p",
      `最近一小时：${snapshot.activity_metrics.attempts} 次机会，${snapshot.activity_metrics.valid_decisions} 次有效决策。`, "meta"));
    if (budget.next_available_at) el("schedule").append(node("p", `预算可用时间：${time(budget.next_available_at)}`, "meta"));
    el("create").disabled = !writable.enabled || Boolean(options.busy);
  }
  return {render, goalCard, episodeCard, evaluationCard, time, statusLabels};
})();

if (typeof module !== "undefined") module.exports = GoalView;
if (typeof window !== "undefined" && typeof document !== "undefined") {
  const el = id => document.getElementById(id);
  let snapshot = null, busy = false, offline = true, refreshTask = null, createPending = null;
  const operations = new Map();
  function paint() { if (snapshot) GoalView.render(snapshot, {busy, offline, onControl: control}); }
  async function call(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
      headers: body === undefined ? {} : {"Content-Type": "application/json"},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
      signal: AbortSignal.timeout(65000),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : `请求未完成（${response.status}）`);
    return result;
  }
  async function refresh() {
    if (refreshTask) return refreshTask;
    refreshTask = (async () => {
      try {
        snapshot = await call("/api/goals");
        offline = false;
        el("connection").textContent = snapshot.enabled ? "已连接 · 目标与调节已启用" : "已连接 · 目标与调节尚未启用";
        el("updated").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", {hour12: false})}`;
      } catch (_) {
        offline = true;
        el("connection").textContent = "连接暂时不可用 · 保留上次状态，控制操作已停用";
        el("create").disabled = true;
      }
      paint();
    })();
    try { await refreshTask; } finally { refreshTask = null; }
  }
  async function control(id, operation) {
    if (busy || offline) return;
    const key = `${id}/${operation}`;
    if (!operations.has(key)) operations.set(key, crypto.randomUUID());
    busy = true;
    paint();
    try {
      await call(`/api/goals/${encodeURIComponent(id)}/${operation}`, {request_id: operations.get(key)});
      operations.delete(key);
      el("notice").textContent = {pause: "暂停请求已记录。已开始的行动会保留核验结果。", resume: "恢复请求已记录，将重新检查当前条件。", cancel: "取消请求已记录，已发生的行动和证据仍保留。"}[operation];
    } catch (error) {
      el("notice").textContent = `操作尚未确认：${error.message}。重试会沿用同一请求，不重复创建操作。`;
    } finally {
      busy = false;
      if (refreshTask) await refreshTask;
      await refresh();
    }
  }
  el("goal-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || offline || !snapshot?.enabled) return;
    const statement = el("statement").value.trim();
    const success_criteria = el("criteria").value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    if (!statement || !success_criteria.length) { el("notice").textContent = "请填写目标和至少一项完成标准。"; return; }
    const signature = JSON.stringify({statement, success_criteria});
    if (!createPending || createPending.signature !== signature) createPending = {signature, request_id: crypto.randomUUID()};
    busy = true;
    paint();
    try {
      await call("/api/goals", {statement, success_criteria, request_id: createPending.request_id});
      createPending = null;
      el("goal-form").reset();
      el("notice").textContent = "目标已记录，Misty 会根据当前条件安排下一步。";
    } catch (error) {
      el("notice").textContent = `目标尚未确认：${error.message}。保留原文重试会沿用同一请求。`;
    } finally {
      busy = false;
      if (refreshTask) await refreshTask;
      await refresh();
    }
  });
  el("refresh").addEventListener("click", refresh);
  async function poll() { await refresh(); setTimeout(poll, document.hidden ? 15000 : 5000); }
  poll();
}
