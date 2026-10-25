"use strict";
importScripts("core.js", "activity.js", "scheduler.js");
let serial = 0;
const scheduler = new TriageScheduler((receive, disconnect) => {
  const port = chrome.runtime.connectNative("com.youtube_clickbait_triage.local");
  port.onMessage.addListener((reply) => { if (scheduler.native === port) receive(reply); });
  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;
    // An intentional old-port close must not fail a newer connection.
    if (scheduler.native === port) disconnect();
  });
  return port;
});
// Session RAM only: Pause plus at most100 submitted-input/result summaries, no raw evidence.
scheduler.paused = true;
let initialized = false;
let pendingSave = null;
let saving = false;
async function saveActivity() {
  if (saving) return;
  saving = true;
  try {
    while (pendingSave) {
      const snapshot = pendingSave;
      pendingSave = null;
      await chrome.storage.session.set({ triageActivity: snapshot });
    }
  } catch { scheduler.error = "activity_storage_unavailable"; scheduler.broadcast(); }
  finally { saving = false; }
}
function activityChanged() {
  if (initialized) { pendingSave = { version: 1, records: scheduler.activity.snapshot() }; void saveActivity(); }
  scheduler.broadcast();
}
scheduler.activity.changed = activityChanged;
const ready = chrome.storage.session.get(["triagePaused", "triageActivity"]).then((value) => {
  scheduler.paused = value.triagePaused === true;
  scheduler.activity.restore(value.triageActivity?.version === 1 ? value.triageActivity.records : []);
  initialized = true;
  activityChanged();
}).catch(() => { scheduler.error = "pause_preference_unavailable"; });
let controls = ready;
function control(op) {
  controls = controls.then(async () => {
    if (op === "pause") { scheduler.paused = true; scheduler.close("paused_outcome_unknown"); }
    try {
      await chrome.storage.session.set({ triagePaused: op === "pause" });
      scheduler.control(op);
    } catch { scheduler.fail("pause_preference_unavailable"); }
  });
}
chrome.runtime.onConnect.addListener((port) => {
  const sender = port.sender;
  if (sender?.id !== chrome.runtime.id) return port.disconnect();
  if (port.name === "triage-activity" && sender.url === chrome.runtime.getURL("runs.html")) {
    const send = (snapshot) => { try { port.postMessage(snapshot); } catch {} };
    let connected = true;
    controls.then(() => { if (connected) { scheduler.activityObservers.add(send); send(scheduler.activitySnapshot()); } });
    port.onMessage.addListener((message) => {
      if (message?.op === "clear-completed") controls.then(() => { scheduler.activity.clearCompleted(); scheduler.broadcast(); });
    });
    port.onDisconnect.addListener(() => { connected = false; scheduler.activityObservers.delete(send); });
    return;
  }
  if (port.name === "triage-popup" && sender.url === chrome.runtime.getURL("popup.html")) {
    const send = (state) => { try { port.postMessage(state); } catch {} };
    let connected = true;
    controls.then(() => { if (connected) { scheduler.observers.add(send); send(scheduler.status()); } });
    port.onMessage.addListener((message) => { if (["pause", "resume", "retry"].includes(message?.op)) control(message.op); });
    port.onDisconnect.addListener(() => { connected = false; scheduler.observers.delete(send); });
    return;
  }
  let origin;
  try { origin = new URL(sender.url).origin; } catch { return port.disconnect(); }
  if (port.name !== "triage-cards" || sender.frameId !== 0 || !sender.tab ||
      !["https://www.youtube.com", "https://youtube.com", "https://m.youtube.com"].includes(origin)) return port.disconnect();
  const id = `client-${++serial}`;
  let connected = true;
  controls.then(() => {
    if (!connected) return;
    scheduler.addClient(id, (message) => { try { port.postMessage(message); } catch {} });
    scheduler.send(id, { op: "reset", paused: scheduler.paused });
  });
  port.onMessage.addListener((message) => {
    controls.then(() => {
      if (!connected) return;
      if (message?.op === "subscribe") scheduler.subscribe(id, message.token, message.input);
      if (message?.op === "unsubscribe") scheduler.unsubscribe(id, message.token);
    });
  });
  port.onDisconnect.addListener(() => { connected = false; scheduler.removeClient(id); });
});
