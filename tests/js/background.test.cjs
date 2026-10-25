const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const input = { url: "https://www.youtube.com/watch?v=AAAAAAAAAAA", title: "Actual title", thumbnail: "https://i.ytimg.com/vi/AAAAAAAAAAA/hqdefault.jpg" };
function worker(storage, delayed = false, delayedSaves = false) {
  let connectListener, restore; const natives = []; const writes = []; const saves=[];
  const context = vm.createContext({ URL, console, chrome: { storage: { session: {
    get(key) { assert.equal(Array.from(key).join(), "triagePaused,triageActivity"); return delayed ? new Promise((resolve) => { restore = () => resolve({ ...storage }); }) : Promise.resolve({ ...storage }); },
    async set(value) { writes.push(value); if(delayedSaves)await new Promise(resolve=>saves.push(()=>{Object.assign(storage,value);resolve();}));else Object.assign(storage, value); }
  } }, runtime: { id: "own", getURL: (name) => `chrome-extension://own/${name}`, onConnect: { addListener(fn) { connectListener = fn; } },
    connectNative() { const port = { sent:[],onMessage: { addListener(fn) {port.receive=fn;} }, onDisconnect: { addListener() {} }, postMessage(m) {port.sent.push(m);}, disconnect() {} }; natives.push(port); return port; }
  } } });
  context.importScripts = (...names) => names.forEach((name) => vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../../extension", name), "utf8"), context));
  vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../../extension/background.js"), "utf8"), context);
  function connect(popup = false) {
    const port = { sender: popup ? { id: "own", url: "chrome-extension://own/popup.html" } : { id: "own", url: "https://www.youtube.com/", frameId: 0, tab: { id: 1 } },
      name: popup ? "triage-popup" : "triage-cards", sent: [], onMessage: { addListener(fn) { port.message = fn; } }, onDisconnect: { addListener(fn) { port.close = fn; } }, postMessage(m) { port.sent.push(m); }, disconnect() {} };
    connectListener(port); return port;
  }
  return { connect, natives, writes, flushSave:()=>saves.shift()?.(),restore: () => restore(), status: () => vm.runInContext("scheduler.status()", context) };
}
test("pause survives worker recreation; initial state agrees, only preference stored", async () => {
  const stored = {}; const first = worker(stored); const popup = first.connect(true); await tick();
  popup.message({ op: "pause" }); await tick(); assert.equal(stored.triagePaused, true);
  const next = worker(stored, true); const cards = next.connect(); const nextPopup = next.connect(true);
  cards.message({ op: "subscribe", token: "first", input }); await tick(); assert.equal(next.natives.length, 0); assert.equal(nextPopup.sent.length, 0);
  next.restore(); await tick(); assert.equal(next.natives.length, 0); assert.ok(cards.sent.some((m) => m.op === "reset" && m.paused)); assert.ok(nextPopup.sent.at(-1).paused);
  nextPopup.message({ op: "resume" }); await tick(); cards.message({ op: "subscribe", token: "resumed", input }); await tick(); assert.equal(next.natives.length, 1);
  assert.equal(Object.keys(stored).sort().join(), "triageActivity,triagePaused");
  assert.ok(next.writes.some((value) => Object.keys(value).join() === "triagePaused"));
  assert.ok(next.writes.every((value) => ["triagePaused", "triageActivity"].includes(Object.keys(value).join())));
});
test("Pause arriving during asynchronous initialization prevents any job dispatch", async () => {
  const stored = {}; const next = worker(stored, true); const popup = next.connect(true); const cards = next.connect();
  popup.message({ op: "pause" }); cards.message({ op: "subscribe", token: "early", input });
  await tick(); assert.equal(next.natives.length, 0); next.restore(); await tick(); assert.equal(next.natives.length, 0); assert.ok(next.status().paused);
  assert.equal(stored.triagePaused, true);
});
test("session activity writes are serialized/coalesced, never overwrite scored outcome with stale running data",async()=>{
  const stored={};const next=worker(stored,false,true);const cards=next.connect();await tick();
  cards.message({op:"subscribe",token:"one",input});await tick();assert.equal(next.natives.length,1);
  const native=next.natives[0];native.receive({version:1,id:native.sent[0].id,status:"scored",scores:{irrelevant:0,clickbait:100,sensational:0,valid:0},reason:"",timings_ms:{total:20}});await tick();
  assert.equal(next.writes.length,1,"only one storage write may be in flight");next.flushSave();await tick();assert.equal(next.writes.length,2);next.flushSave();await tick();
  assert.equal(stored.triageActivity.records[0].status,"scored");assert.equal(stored.triageActivity.records.length,1);assert.equal(stored.triageActivity.records[0].timing.kind,"analysis_total");
  assert.deepEqual(Object.keys(stored),["triageActivity"]);assert.equal(Object.keys(stored.triageActivity.records[0]).includes("raw_model_response"),false);
});
