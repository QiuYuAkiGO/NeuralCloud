"use strict";

const FEELING_LABELS = {
  anger: "愤怒", frustration: "挫折", sadness: "悲伤", fear: "害怕", joy: "喜悦",
  interest: "兴趣", loneliness: "孤独", shame: "羞怯", disgust: "厌恶",
  calm: "平静", affection: "亲近", surprise: "惊讶",
};
const REPORT_STATUS = {
  failed: "失败", retrying: "退避重试", rejected: "拒绝", applied: "处理完成",
  queued: "排队中", running: "处理中", cancelled: "已取消", unknown: "未确认",
};
const JOB_LABELS = { memory: "记忆", compact: "上下文压缩", affect: "情绪评估", diary: "日记" };

function svgNode(tag, attrs = {}, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  if (text != null) node.textContent = text;
  return node;
}

// Min/max in each time-ordered bucket retain short peaks instead of averaging
// them away. Full points remain available for inspection and raw JSON export.
function chartEnvelope(points, key, cap = 900) {
  const rows = points.map((point) => ({ at: Date.parse(point.at), value: point.values[key] }))
    .filter((point) => Number.isFinite(point.at) && typeof point.value === "number");
  if (rows.length <= cap * 2) return rows;
  const out = [rows[0]];
  const width = Math.ceil(rows.length / cap);
  for (let index = 0; index < rows.length; index += width) {
    const chunk = rows.slice(index, index + width);
    let low = 0, high = 0;
    chunk.forEach((point, i) => {
      if (point.value < chunk[low].value) low = i;
      if (point.value > chunk[high].value) high = i;
    });
    for (const i of [...new Set([low, high])].sort((a, b) => a - b)) out.push(chunk[i]);
  }
  out.push(rows[rows.length - 1]);
  return out;
}

function renderTimeline(report) {
  const data = report.timeline || {};
  const root = panel("情绪与内稳态 · 事件时间轴", []);
  const subtitle = data.mode === "shadow"
    ? "影子记录：新情绪尚未参与对话决策。点击事件，查看幅度、来源与 Misty 的解释。"
    : "点击图例选择曲线，悬停查看读数，点击事件查看来源与变化。";
  root.append(note(subtitle));
  if (data.affect_available === false) root.append(note("情绪数据暂不可用；内稳态曲线仍单独展示。"));
  if (!data.homeostasis?.length) root.append(note("当日暂无可用的内稳态前后值。旧轨迹没有完整记录这些数值，曲线从启用完整记录后开始。"));
  else root.append(note("内稳态使用实际记录的前后值，保留每分钟峰谷及事件转折；未记录的历史区间不补画。"));
  if (!data.available) {
    root.append(note("该日没有可用的情绪或内稳态轨迹。缺失数据不按 0 绘制。"));
    return root;
  }
  const labels = { ...NEED_LABELS, mood_valence: "心境效价", mood_arousal: "心境唤醒" };
  Object.entries(FEELING_LABELS).forEach(([key, value]) => { labels[`feeling_${key}`] = value; });
  const palette = ["#8f70ec", "#e29542", "#53c5b0", "#ee758f", "#63a7f0", "#a7bb60", "#d28bd3", "#c0a285"];
  const groups = [
    { rows: data.affect || [], keys: ["mood_valence", "mood_arousal", ...Object.keys(FEELING_LABELS).map((key) => `feeling_${key}`)] },
    { rows: data.homeostasis || [], keys: Object.keys(NEED_LABELS) },
  ];
  const series = [];
  groups.forEach(({ rows, keys }) => keys.forEach((key) => {
    if (rows.some((row) => typeof row.values[key] === "number")) {
      series.push({ key, rows, color: palette[series.length % palette.length],
        selected: ["mood_valence", "mood_arousal", "energy_reserve", "cognitive_load"].includes(key) });
    }
  }));
  const legend = el("div", "trend-legend");
  const plot = svgNode("svg", { viewBox: "0 0 1040 340", role: "img", "aria-label": "情绪与内稳态时间曲线" });
  plot.append(svgNode("title", {}, "横轴为时间，效价范围 −1 至 1，其余状态为 0 至 1。"));
  const canvas = el("div", "trend-canvas");
  canvas.append(plot);
  const readout = el("div", "trend-readout", "悬停查看曲线读数；下方事件列表支持键盘操作。");
  readout.setAttribute("aria-live", "polite");
  const detail = el("div", "trend-detail");
  detail.setAttribute("aria-live", "polite");
  detail.append(note("同一时刻出现的事件不一定是变化原因；只有明确的来源引用才表示关联。"));
  const start = Date.parse(report.window.since);
  const end = Date.parse(report.window.until);
  const x = (at) => 56 + ((at - start) / Math.max(1, end - start)) * 960;
  const y = (value) => 282 - ((value + 1) / 2) * 252;
  for (const value of [-1, -0.5, 0, 0.5, 1]) {
    plot.append(svgNode("line", { x1: 56, x2: 1016, y1: y(value), y2: y(value), class: "trend-grid" }));
    plot.append(svgNode("text", { x: 45, y: y(value) + 4, "text-anchor": "end", class: "trend-axis" }, value.toFixed(1)));
  }
  for (let i = 0; i <= 6; i += 1) {
    const at = start + (end - start) * i / 6;
    const labelText = new Intl.DateTimeFormat("zh-CN", {
      timeZone: report.timezone || "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).format(new Date(at));
    plot.append(svgNode("text", { x: x(at), y: 322, "text-anchor": "middle", class: "trend-axis" }, labelText));
  }
  const paths = svgNode("g");
  plot.append(paths);
  function draw() {
    paths.replaceChildren();
    for (const line of series.filter((entry) => entry.selected)) {
      const points = chartEnvelope(line.rows, line.key);
      let prior = null;
      const path = points.map((point) => {
        const gap = prior == null || point.at - prior > 15 * 60 * 1000;
        prior = point.at;
        return `${gap ? "M" : "L"}${x(point.at).toFixed(2)},${y(point.value).toFixed(2)}`;
      }).join(" ");
      paths.append(svgNode("path", { d: path, fill: "none", stroke: line.color, "stroke-width": 2,
        "vector-effect": "non-scaling-stroke" }));
      // A single observation is still visible.
      if (points.length === 1) paths.append(svgNode("circle", {
        cx: x(points[0].at), cy: y(points[0].value), r: 3, fill: line.color,
      }));
    }
  }
  for (const line of series) {
    const field = el("label", "trend-choice");
    const box = el("input");
    box.type = "checkbox";
    box.checked = line.selected;
    const swatch = el("i");
    swatch.style.background = line.color;
    field.append(box, swatch, el("span", "", labels[line.key] || line.key));
    box.addEventListener("change", () => { line.selected = box.checked; draw(); });
    legend.append(field);
  }
  const cursor = svgNode("line", { x1: 56, x2: 56, y1: 20, y2: 304, class: "trend-cursor", visibility: "hidden" });
  plot.append(cursor);
  plot.addEventListener("pointermove", (event) => {
    const rect = plot.getBoundingClientRect();
    const position = Math.max(56, Math.min(1016, (event.clientX - rect.left) / rect.width * 1040));
    const at = start + (position - 56) / 960 * (end - start);
    cursor.setAttribute("x1", position); cursor.setAttribute("x2", position);
    cursor.setAttribute("visibility", "visible");
    const values = [];
    for (const line of series.filter((entry) => entry.selected)) {
      let closest = null, distance = Infinity;
      for (const point of line.rows) {
        const diff = Math.abs(Date.parse(point.at) - at);
        if (diff <= distance && typeof point.values[line.key] === "number") {
          closest = point; distance = diff;
        }
      }
      if (closest && distance <= 15 * 60 * 1000) values.push(
        `${labels[line.key]} ${number(closest.values[line.key], 3)}（${beijing(closest.at)}）`);
    }
    readout.textContent = values.length ? values.join(" · ") : "附近没有采样记录";
  });
  function showEvent(event) {
    detail.replaceChildren(
      el("strong", "", `${beijing(event.at)} · ${event.title || "事件"}`),
      el("p", "", event.reason || "原因未记录"),
    );
    const authority = event.authority === "subjective_interpretation" ? "Misty 的主观解释"
      : event.kind === "homeostasis" ? "已记录的状态更新" : "事件回执，未确认因果";
    detail.append(note(`${authority}${event.confidence == null ? "" : ` · 解释置信度 ${number(event.confidence, 2)}`}`));
    if (event.changes) detail.append(table(["状态", "本次变化"], Object.entries(event.changes)
      .map(([key, value]) => [labels[key] || key, `${value > 0 ? "+" : ""}${number(value, 4)}`])));
    if (event.values) detail.append(note(Object.entries(event.values)
      .map(([key, value]) => `${labels[key] || key} ${number(value, 3)}`).join(" · ")));
    if ((event.source_refs || []).length) detail.append(el("code", "source-refs", event.source_refs.join(" · ")));
    const at = x(Date.parse(event.at));
    cursor.setAttribute("x1", at); cursor.setAttribute("x2", at); cursor.setAttribute("visibility", "visible");
  }
  const eventRows = data.events || [];
  // Render all events in a paged accessible list, and use a bounded set of
  // markers per minute on the chart. No event is removed from the list.
  const minutes = new Set();
  for (const event of eventRows) {
    const minute = Math.floor(Date.parse(event.at) / 60000);
    if (minutes.has(minute)) continue;
    minutes.add(minute);
    const mark = svgNode("circle", { cx: x(Date.parse(event.at)), cy: 299, r: 4, class: "trend-event" });
    mark.append(svgNode("title", {}, `${beijing(event.at)} · ${event.title}`));
    mark.addEventListener("click", () => showEvent(event));
    plot.append(mark);
  }
  root.append(legend, canvas, readout, note("效价为 −1～1，其余状态为 0～1；超过 15 分钟的采样缺口断开。绘图保留峰谷。"), detail);
  if (data.homeostasis_limit_reached) root.append(note("内稳态轨迹达到读取上限，本图可能不覆盖当天全部变化。"));
  const list = el("div", "event-list");
  root.append(el("h3", "", `事件记录 · ${number(eventRows.length)}`), list);
  pagedRows(list, [...eventRows].reverse(), (rows) => {
    const wrapper = el("div");
    for (const event of rows) {
      const button = el("button", "event-row");
      button.type = "button";
      button.append(el("time", "", beijing(event.at)), el("span", "", event.title || "事件"),
        el("small", "", event.kind === "appraisal" ? "情绪解释" : event.kind === "homeostasis" ? "内稳态" : "经历"));
      button.addEventListener("click", () => showEvent(event));
      wrapper.append(button);
    }
    return wrapper;
  });
  draw();
  return root;
}

function pagedRows(root, items, build, size = 25) {
  let offset = 0;
  function drawPage() {
    root.replaceChildren(build(items.slice(offset, offset + size)));
    if (!items.length) { root.append(note("没有记录")); return; }
    const controls = el("div", "report-pagination");
    const previous = el("button", "button secondary", "上一页");
    const next = el("button", "button secondary", "下一页");
    previous.disabled = offset === 0;
    next.disabled = offset + size >= items.length;
    previous.addEventListener("click", () => { offset -= size; drawPage(); });
    next.addEventListener("click", () => { offset += size; drawPage(); });
    controls.append(previous, el("span", "", `${offset + 1}–${Math.min(offset + size, items.length)} / ${items.length}`), next);
    root.append(controls);
  }
  drawPage();
}

function renderMemories(report) {
  const root = panel("今日的记忆与印象", []);
  root.append(note("默认按已记录的记忆强度降序，同强度按时间从新到旧。未记录强度的条目排在末尾；强度不表示事实可信度。"));
  root.append(note("印象按写入时间归属日期，原始经历按发生时间。仅展示当前仍有效、当前可见的记录。"));
  root.append(note("翻页时保持阅读位置；刷新日报可查看最新写入的记忆。"));
  const contents = el("div");
  root.append(contents);
  let page = report.memories || {}, loading = false, pageIndex = 0;
  const boundaries = [null];
  async function load(index, boundary = boundaries[index]) {
    if (loading) return;
    loading = true;
    try {
      const size = page.limit || 25;
      const query = new URLSearchParams({ day: report.day, offset: String(index * size), limit: String(size) });
      if (boundary) query.set("after", boundary);
      page = await api(`/api/report/memories?${query}`);
      pageIndex = index;
      boundaries[index] = boundary;
      boundaries.length = index + 1;
      draw();
    } catch (error) { toast(error.message, true); }
    finally { loading = false; }
  }
  function draw() {
    contents.replaceChildren();
    if (!page.available) { contents.append(note("记忆数据暂不可用，不能判断今日是否有新增。")); return; }
    const rows = page.items || [];
    contents.append(table(["时间", "类型", "内容与来源", "记忆强度（记录值）"], rows.map((entry) => {
      const body = el("details", "memory-detail");
      body.append(el("summary", "", entry.text || "空记录"));
      body.append(note(`${entry.kind === "impression" ? "来源支持的解释" : "原始经历"} · ${entry.id}`));
      body.append(el("code", "source-refs", (entry.source_refs || []).join(" · ")));
      if (entry.text_truncated) body.append(note("长文本仅展示前 4,000 字。"));
      if (entry.pinned || entry.important) body.append(note(`${entry.pinned ? "已固定 " : ""}${entry.important ? "重要记录" : ""}`));
      return [beijing(entry.at), entry.kind === "impression" ? "印象" : "记忆", body,
        entry.strength == null ? "未记录" : number(entry.strength, 3)];
    })));
    if (!rows.length) contents.append(note("该日没有可展示的记忆或印象。"));
    const controls = el("div", "report-pagination");
    const prev = el("button", "button secondary", "上一页");
    const next = el("button", "button secondary", "下一页");
    prev.disabled = pageIndex === 0;
    next.disabled = !page.has_more;
    prev.addEventListener("click", () => load(pageIndex - 1));
    next.addEventListener("click", () => load(pageIndex + 1, page.next_cursor));
    controls.append(prev, el("span", "", rows.length ? `第 ${page.offset + 1}–${page.offset + rows.length} 条` : "0 条"), next);
    contents.append(controls);
  }
  draw();
  return root;
}

function renderFailures(report) {
  const root = panel("失败与重试记录", []);
  const day = report.failures || {}, run = report.current_run || {};
  const tabs = el("div", "report-pagination"), list = el("div");
  function draw(items) {
    pagedRows(list, items, (rows) => table(
      ["发生时间", "环节", "当时状态 → 当前状态", "原因", "次数 / 关联记录"],
      rows.map((entry) => [beijing(entry.at), JOB_LABELS[entry.component] || entry.component,
        `${REPORT_STATUS[entry.status] || entry.status} → ${REPORT_STATUS[entry.current_status] || "未确认"}`,
        failureReason(entry), `${entry.count || 1} · ${entry.job_id || entry.event_id || entry.id}`]),
    ));
  }
  for (const [label, rows] of [["所选日期", day.items || []], ["本次运行", run.failures || []]]) {
    const button = el("button", "button secondary", `${label} · ${rows.length}`);
    button.disabled = label === "本次运行" && !run.available;
    button.addEventListener("click", () => draw(rows));
    tabs.append(button);
  }
  root.append(tabs, note("重试次数按实际记录的失败计算；排队、租约尝试及正常维护等待不算模型失败。拒绝会单独标明。"), list);
  if (!day.background_available) root.append(note("后台失败数据暂不可用，下表仅含已读取的运行记录。"));
  root.append(note("这里统计异常回执，并按事件与原因去重。启动恢复可能再次报告历史任务的失败；记录数不等于本次新发生的模型失败次数。"));
  draw(day.items || []);
  if ((day.legacy_undated || []).length) {
    root.append(el("h3", "", "历史失败 · 未记录发生时间"));
    root.append(note("以下失败仍未解决，但无法归入今天或本次运行，不计入上方数量。"));
    root.append(table(["环节", "原因", "任务"], day.legacy_undated.map((entry) => [
      JOB_LABELS[entry.component] || entry.component, entry.reason, entry.id,
    ])));
  }
  return root;
}

function failureReason(entry) {
  const detail = el("div", "", entry.reason);
  const validation = entry.validation;
  if (validation && typeof validation === "object") {
    const more = el("details");
    more.append(el("summary", "", "输出校验详情"));
    more.append(el("pre", "source-refs", JSON.stringify(validation, null, 2)));
    detail.append(more);
  }
  return detail;
}
