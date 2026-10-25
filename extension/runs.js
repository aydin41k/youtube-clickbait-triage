"use strict";
let port;
const core = TriageCore;
const clear = document.getElementById("clear");
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function time(value) { return Number.isInteger(value) ? new Date(value).toLocaleString() : "Unknown"; }
function packaging(input) {
  const row = element("div", undefined, "packaging");
  const item = core.packaging(input);
  if (!item) { row.append(element("p", "Invalid restored packaging")); return row; }
  const image = element("img"); image.src = item.input.thumbnail; image.alt = "Submitted thumbnail"; image.loading = "lazy"; image.referrerPolicy = "no-referrer";
  const details = element("div");
  const link = element("a", item.input.title); link.href = item.input.url; link.target = "_blank"; link.rel = "noopener noreferrer";
  details.append(link, element("p", item.input.url, "url"));
  const thumbnail = element("a", `Thumbnail URL: ${item.input.thumbnail}`, "url"); thumbnail.href = item.input.thumbnail; thumbnail.target = "_blank"; thumbnail.rel = "noopener noreferrer"; details.append(thumbnail);
  row.append(image, details); return row;
}
function attempt(record) {
  const node = element("article", undefined, "attempt");
  node.dataset.attemptId = record.id;
  node.append(packaging(record.input));
  const outcome = record.status === "running" ? "Running — local analysis attempt" : record.status === "interrupted" ? "Interrupted — outcome / inference extent unknown" :
    record.status === "unavailable" ? "Unavailable — analysis could not complete" : `Scored — ${core.majority(record) || "no estimate above 50%"}`;
  const outcomeNode = element("p", outcome, `outcome ${record.status}`);
  const category = core.majority(record);
  if (category) outcomeNode.dataset.category = category;
  node.append(outcomeNode);
  if (record.reason) node.append(element("p", `Reason: ${record.reason}`));
  if (core.validScores(record.scores)) {
    const scores = element("dl", undefined, "scores");
    for (const category of core.classes) { scores.append(element("dt", category), element("dd", `${record.scores[category]}%`)); }
    node.append(scores);
  }
  const timing = record.timing ? `${(record.timing.ms /1000).toFixed(2)}s ${record.timing.kind === "analysis_total" ? "total analysis duration (retrieval + model work)" : "observed browser elapsed; not inference duration"}` : "Duration unknown";
  node.append(element("p", `Submitted: ${time(record.submittedAt)} · Completed: ${record.completedAt === null ? "Not completed" : time(record.completedAt)} · ${timing}`, "meta"));
  node.append(element("p", `Attempt ${record.id} · Cached result reused ${record.cacheUses} time(s); cache reuse is not another run`, "meta"));
  return node;
}
function render(snapshot) {
  if (!snapshot?.state || !Array.isArray(snapshot.records) || !Array.isArray(snapshot.queued)) return;
  clear.disabled = false;
  const state = snapshot.state;
  document.getElementById("connection").textContent = state.error ? `Connection error: ${state.error}` : state.paused ? "Paused — no new local analysis" : `${state.connected ? "Local companion connected" : "Companion idle"} · ${state.active || 0} active attempts · ${state.queued} queued`;
  const current = document.getElementById("current"); const recent = document.getElementById("recent"); current.replaceChildren(); recent.replaceChildren();
  // Validate restored/user/model strings again before building DOM; never use HTML interpolation.
  for (const record of snapshot.records.slice(0,100)) if (TriageActivityValid(record)) (record.status === "running" ? current : recent).append(attempt(record));
  for (const queued of snapshot.queued.slice(0,64)) if (core.packaging(queued.input)) { const card = element("article", undefined, "attempt queued"); card.append(packaging(queued.input), element("p", "Queued — not run")); current.append(card); }
  if (!current.childElementCount) current.append(element("p", "No running or queued work.", "empty"));
  if (!recent.childElementCount) recent.append(element("p", "No completed attempts in this browser session.", "empty"));
}
function open() {
  const current = chrome.runtime.connect({ name: "triage-activity" }); port = current;
  current.onMessage.addListener((snapshot) => { if (port === current) render(snapshot); });
  current.onDisconnect.addListener(() => { if (port !== current) return; void chrome.runtime.lastError; port = null; clear.disabled = true; document.getElementById("connection").textContent = "Reconnecting to background…"; setTimeout(open, 500); });
}
clear.addEventListener("click", () => port?.postMessage({ op: "clear-completed" }));
open();
