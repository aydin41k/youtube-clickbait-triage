const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { JSDOM } = require("jsdom");
const { Cards, extract } = require("../../extension/content.js");
const Scheduler = require("../../extension/scheduler.js");
const fixture = fs.readFileSync(require.resolve("../fixtures/cards.html"), "utf8");
const sleep = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));
function setup(html = fixture) {
  const dom = new JSDOM(html, { url: "https://www.youtube.com/" });
  const sent = []; let listener;
  const port = { onMessage: { addListener(fn) { listener = fn; } }, onDisconnect: { addListener() {} }, postMessage(m) { sent.push(m); }, disconnect() {} };
  const cards = new Cards(dom.window.document, () => port, { delay: 1 }).start();
  return { dom, doc: dom.window.document, cards, sent, reply: (m) => listener(m), stop() { cards.stop(); dom.window.close(); } };
}
const scored = (category, amount = 51) => ({ status: "scored", scores: Object.fromEntries(["irrelevant", "clickbait", "sensational", "valid"].map((key) => [key, key === category ? amount : key === (category === "valid" ? "clickbait" : "valid") ? 100 - amount : 0])) });
test("initial legacy/modern/Shorts/playlist discovery, exclusions and native DOM preservation", async () => {
  const t = setup(); await sleep();
  assert.equal(t.sent.filter((m) => m.op === "subscribe").length, 8);
  for (const id of ["collection", "channel-only", "ad"]) assert.equal(extract(t.doc.getElementById(id)), null);
  const home = t.doc.getElementById("home"); const native = home.querySelector("#video-title");
  const original = native.outerHTML; const first = t.sent.find((m) => m.input?.title.startsWith("Clickbait"));
  assert.equal(first.input.title, "Clickbait  displayed title!");
  assert.equal(first.input.thumbnail, "https://i.ytimg.com/vi/AAAAAAAAAAA/hqdefault.jpg?actual=1");
  t.reply({ op: "result", token: first.token, result: scored("clickbait") });
  assert.equal(home.dataset.yctCategory, "clickbait"); assert.equal(native.outerHTML, original);
  assert.match(home.querySelector("[data-yct-badge]").textContent, /51%/);
  t.stop();
});
test("lazy loading/currentSrc, mutation/title changes, recycled cards and stale replies", async () => {
  const t = setup(); await sleep(); const home = t.doc.getElementById("home");
  const first = t.sent.find((m) => m.input?.title.startsWith("Clickbait"));
  t.reply({ op: "result", token: first.token, result: scored("valid") });
  home.querySelector("#video-title").setAttribute("title", "New displayed title");
  await sleep(0); assert.equal(home.hasAttribute("data-yct-category"), false); await sleep();
  t.reply({ op: "result", token: first.token, result: scored("clickbait") }); assert.equal(home.hasAttribute("data-yct-category"), false);
  const latest = t.sent.filter((m) => m.input?.title === "New displayed title").at(-1);
  t.reply({ op: "result", token: latest.token, result: scored("sensational") }); assert.equal(home.dataset.yctCategory, "sensational");
  const image = home.querySelector("img"); Object.defineProperty(image, "currentSrc", { configurable: true, value: "https://i.ytimg.com/vi/AAAAAAAAAAA/hq720.jpg?responsive=1" });
  image.dispatchEvent(new t.dom.window.Event("load")); await sleep();
  assert.equal(t.sent.filter((m) => m.op === "subscribe").at(-1).input.thumbnail, image.currentSrc);
  const lazy = home.cloneNode(true); lazy.id = "lazy"; lazy.querySelector("img").src = "data:image/gif;base64,AA"; t.doc.body.append(lazy); await sleep();
  assert.equal(extract(lazy), null); lazy.querySelector("img").src = "https://i.ytimg.com/vi/AAAAAAAAAAA/hqdefault.jpg?lazy=1"; await sleep(); assert.ok(extract(lazy));
  const token = t.cards.states.get(lazy).token; lazy.remove(); await sleep(); assert.ok(t.sent.some((m) => m.op === "unsubscribe" && m.token === token));
  t.stop();
});
test("unavailable/no-majority remains uncoloured; hostile reason is inert", async () => {
  const t = setup(); await sleep(); const card = t.doc.getElementById("home"); const token = t.cards.states.get(card).token;
  t.reply({ op: "result", token, result: scored("clickbait", 50) }); assert.equal(card.hasAttribute("data-yct-category"), false); assert.match(card.textContent, /above 50%/);
  t.reply({ op: "result", token, result: { status: "unavailable", scores: null, reason: "<img src=x onerror=alert(1)>" } });
  assert.equal(card.querySelectorAll("img").length, 1); assert.match(card.querySelector("[data-yct-badge]").textContent, /<img/); assert.equal(card.hasAttribute("data-yct-category"), false);
  t.reply({ op: "reset", paused: true }); assert.match(card.textContent, /paused/); t.stop();
});
test("more than queue capacity eventually processes every still-present card", async () => {
  const dom = new JSDOM(fixture, { url: "https://www.youtube.com/" }); const doc = dom.window.document;
  for (let n = 0; n < 12; n++) { const card = doc.getElementById("home").cloneNode(true); card.id = `extra-${n}`; card.querySelector("#video-title").title = `Extra ${n}`; doc.body.append(card); }
  let handler; let processed = 0;
  const scheduler = new Scheduler((receive) => ({ postMessage(request) { setTimeout(() => { processed++; receive({ version: 1, id: request.id, ...scored("valid"), reason: "", timings_ms: {} }); }, 2); }, disconnect() {} }), { limit: 2 });
  const port = { onMessage: { addListener(fn) { handler = fn; } }, onDisconnect: { addListener() {} }, postMessage(m) { if (m.op === "subscribe") scheduler.subscribe("one", m.token, m.input); else scheduler.unsubscribe("one", m.token); }, disconnect() { scheduler.removeClient("one"); } };
  scheduler.addClient("one", (m) => handler?.(m));
  const cards = new Cards(doc, () => port, { delay: 1 }).start();
  for (let tries=0; tries<100 && processed<20; tries++) await sleep(20);
  assert.equal(processed, 20); assert.equal(doc.querySelectorAll('[data-yct-category="valid"]').length, 20);
  cards.stop(); dom.window.close();
});
test("initial connect failure recovers without a DOM mutation", async () => {
  const dom = new JSDOM(fixture, { url: "https://www.youtube.com/" }); let calls = 0; const sent = [];
  const port = { onMessage: { addListener() {} }, onDisconnect: { addListener() {} }, postMessage(m) { sent.push(m); }, disconnect() {} };
  const cards = new Cards(dom.window.document, () => { if (!calls++) throw Error("worker restarting"); return port; }, { delay: 1 }).start();
  await sleep(2100); assert.equal(sent.filter((m) => m.op === "subscribe").length, 8);
  cards.stop(); dom.window.close();
});
test("production pagehide/pageshow BFCache lifecycle restores discovery without stale controllers", async () => {
  const dom = new JSDOM(fixture, { url: "https://www.youtube.com/", runScripts: "outside-only" });
  const ports = [];
  dom.window.chrome = { runtime: { id: "own-extension", connect() {
    const port = { sent: [], closed: false, onMessage: { addListener(fn) { port.receive = fn; } }, onDisconnect: { addListener(fn) { port.disconnected = fn; } },
      postMessage(m) { port.sent.push(m); }, disconnect() { port.closed = true; port.disconnected?.(); } };
    ports.push(port); return port;
  } } };
  dom.window.eval(fs.readFileSync(require.resolve("../../extension/core.js"), "utf8"));
  dom.window.eval(fs.readFileSync(require.resolve("../../extension/frames.js"), "utf8"));
  dom.window.eval(fs.readFileSync(require.resolve("../../extension/content.js"), "utf8"));
  await sleep(70); assert.equal(ports.length, 1); assert.equal(ports[0].sent.filter((m) => m.op === "subscribe").length, 8);
  for (let cycle = 0; cycle < 3; cycle++) {
    const old = ports.at(-1); const token = old.sent.find((m) => m.op === "subscribe").token;
    dom.window.dispatchEvent(new dom.window.PageTransitionEvent("pagehide", { persisted: true }));
    assert.ok(old.closed); assert.equal(dom.window.document.querySelectorAll("[data-yct-badge]").length, 0);
    dom.window.dispatchEvent(new dom.window.PageTransitionEvent("pageshow", { persisted: true }));
    dom.window.dispatchEvent(new dom.window.PageTransitionEvent("pageshow", { persisted: true }));
    await sleep(70); assert.equal(ports.length, cycle + 2); assert.equal(ports.at(-1).sent.filter((m) => m.op === "subscribe").length, 8);
    old.receive({ op: "result", token, result: scored("clickbait") }); assert.equal(dom.window.document.querySelectorAll("[data-yct-category]").length, 0);
    const title = dom.window.document.querySelector("#home #video-title"); title.title = `Restored title ${cycle}`;
    await sleep(70); assert.ok(ports.at(-1).sent.some((m) => m.input?.title === title.title));
  }
  const extra = dom.window.document.querySelector("#recommendation").cloneNode(true); extra.id = "restored-extra"; extra.querySelector("#video-title").textContent = "New restored card";
  dom.window.document.body.append(extra); await sleep(70); assert.ok(ports.at(-1).sent.some((m) => m.input?.title === "New restored card"));
  dom.window.dispatchEvent(new dom.window.PageTransitionEvent("pagehide", { persisted: true })); dom.window.close();
});
