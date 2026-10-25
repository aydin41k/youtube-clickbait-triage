"use strict";
let port;
let paused = false;
const pauseButton = document.getElementById("pause");
const retryButton = document.getElementById("retry");
pauseButton.disabled = retryButton.disabled = true;
function receive(state) {
  pauseButton.disabled = retryButton.disabled = false;
  paused = state.paused;
  document.getElementById("pause").textContent = paused ? "Resume" : "Pause";
  document.getElementById("status").textContent = paused ? "Paused — private runtime released." : state.error ?
    `Local connection error: ${state.error}. Retry when the companion is ready.` :
    `${state.connected ? "Local companion connected" : "Waiting for video cards"}${state.active ? ` — analysing ${state.active} card${state.active === 1 ? "" : "s"}` : ""}. ${state.queued} queued.`;
}
function open() {
  const current = chrome.runtime.connect({ name: "triage-popup" });
  port = current;
  current.onMessage.addListener((state) => { if (port === current) receive(state); });
  current.onDisconnect.addListener(() => {
    if (port !== current) return;
    void chrome.runtime.lastError;
    port = null;
    pauseButton.disabled = retryButton.disabled = true;
    document.getElementById("status").textContent = "Reconnecting to background…";
    setTimeout(open, 500);
  });
}
pauseButton.addEventListener("click", () => port?.postMessage({ op: paused ? "resume" : "pause" }));
retryButton.addEventListener("click", () => port?.postMessage({ op: "retry" }));
document.getElementById("runs").addEventListener("click", () => chrome.tabs.create({ url: chrome.runtime.getURL("runs.html") }));
open();
