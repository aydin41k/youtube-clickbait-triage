const { test } = require("node:test");
const assert = require("node:assert/strict");
const Scheduler = require("../../extension/scheduler.js");
function input(n = 0) { return { url: "https://www.youtube.com/watch?v=AAAAAAAAAAA", title: `Title ${n}`, thumbnail: `https://i.ytimg.com/vi/AAAAAAAAAAA/hqdefault.jpg?q=${n}` }; }
function setup(options) {
  const messages = { a: [], b: [] }, sent = [];
  let receive, disconnected, connects = 0;
  const scheduler = new Scheduler((r, d) => { receive = r; disconnected = d; connects++; return { postMessage: (value) => sent.push(value), disconnect() {} }; }, options);
  scheduler.addClient("a", (m) => messages.a.push(m));
  scheduler.addClient("b", (m) => messages.b.push(m));
  const reply = (id = scheduler.active.id) => receive({ version: 1, id, status: "scored", scores: { irrelevant: 0, clickbait: 51, sensational: 49, valid: 0 }, reason: "", timings_ms: {} });
  return { scheduler, messages, sent, reply, fail: () => disconnected(), malformed: (value) => receive(value), connects: () => connects };
}
test("full-key fanout, cache and bounds at a configurable single lane", () => {
  const { scheduler: s, sent, messages, reply, connects } = setup({ cacheLimit: 2, concurrency: 1 });
  s.subscribe("a", "a1", input()); s.subscribe("b", "b1", input()); s.subscribe("a", "a2", input(2));
  assert.equal(sent.length, 1); assert.equal(s.queue.size, 1);
  reply(); assert.equal(sent.length, 2);
  assert.equal(messages.a.filter((m) => m.op === "result").length, 1);
  assert.equal(messages.b.filter((m) => m.op === "result").length, 1);
  s.subscribe("a", "cached", input()); assert.equal(sent.length, 2);
  reply(); s.subscribe("a", "three", input(3)); reply();
  assert.equal(s.cache.size, 2); assert.equal(connects(), 1);
});
test("backpressure capacity retry, unsubscribe and last-tab release", () => {
  const { scheduler: s, messages, reply } = setup({ limit: 1, concurrency: 1 });
  s.subscribe("a", "active", input()); s.subscribe("a", "waiting", input(1)); s.subscribe("b", "full", input(2));
  assert.equal(messages.b.at(-1).op, "deferred"); assert.equal(s.queue.size, 1);
  s.unsubscribe("a", "waiting"); assert.equal(s.queue.size, 0);
  s.subscribe("b", "full", input(2)); reply(); reply();
  assert.equal(messages.b.filter((m) => m.op === "result").length, 1);
  s.removeClient("a"); assert.ok(s.native); s.removeClient("b"); assert.equal(s.native, null);
});
test("disconnect, controlled retry, stale/duplicate/malformed results, pause", () => {
  const t = setup(); const s = t.scheduler;
  s.subscribe("a", "one", input()); t.fail();
  assert.equal(s.active, null); assert.ok(s.error);
  s.subscribe("a", "one", input()); assert.equal(t.connects(), 1);
  s.control("retry"); s.subscribe("a", "two", input(2));
  t.reply("job-1"); assert.equal(s.active.id, "job-2");
  t.malformed({}); assert.equal(s.error, "invalid_local_reply"); assert.equal(s.active, null);
  s.control("resume"); s.subscribe("a", "three", input(3)); t.reply(); t.reply("job-3");
  s.control("pause"); assert.equal(s.native, null); assert.ok(s.paused);
  s.subscribe("a", "four", input(4)); assert.equal(t.messages.a.at(-1).reason, "paused");
});
test("expired cache is reanalysed and detached subscribers get no late result", () => {
  let now = 0; const t = setup({ now: () => now }); const s = t.scheduler;
  s.subscribe("a", "one", input()); s.unsubscribe("a", "one"); t.reply();
  assert.equal(t.messages.a.filter((m) => m.op === "result").length, 0);
  now = 3600001; s.subscribe("a", "two", input()); assert.equal(t.sent.length, 2);
});
test("three default lanes route reverse completions and refill without losing earlier IDs", () => {
  const t = setup(); const s = t.scheduler;
  for (let n = 0; n < 4; n++) s.subscribe("a", `card-${n}`, input(n));
  assert.equal(t.sent.length, 3); assert.equal(s.queue.size, 1);
  t.reply("job-3"); assert.equal(t.sent.length, 4);
  for (const id of ["job-2", "job-1", "job-4"]) t.reply(id);
  assert.equal(t.messages.a.filter((m) => m.op === "result").length, 4);
  assert.equal(s.status().active, 0);
});
test("three in-flight plus 64 waiting, duplicate fanout, detach, and capacity recovery", () => {
  const t = setup(); const s = t.scheduler;
  for (let n = 0; n < 68; n++) s.subscribe("a", `card-${n}`, input(n));
  assert.equal(s.status().active, 3); assert.equal(s.queue.size, 64);
  assert.equal(t.messages.a.at(-1).reason, "queue_full");
  s.subscribe("b", "duplicate", input(1)); assert.equal(t.sent.length, 3);
  s.unsubscribe("a", "card-66"); assert.equal(s.queue.size, 63);
  s.subscribe("a", "card-67", input(67)); assert.equal(s.queue.size, 64);
  s.unsubscribe("a", "card-1");
  t.reply("job-2"); assert.equal(s.status().active, 3); assert.equal(t.sent.length, 4);
  assert.equal(t.messages.b.filter((m) => m.op === "result").length, 1);
  assert.equal(t.messages.a.filter((m) => m.op === "result").length, 0);
  t.malformed({ version: 1, id: "job-1", status: "unavailable", scores: null, reason: "no_english_captions", timings_ms: {} });
  assert.equal(t.messages.a.find((m) => m.token === "card-0" && m.op === "result").result.status, "unavailable");
  t.reply("job-2"); assert.equal(s.error, "");
  t.reply("job-999"); assert.equal(s.error, "invalid_local_reply"); assert.equal(s.status().active, 0);
});
test("concurrency is hard-capped at three and removal visits every active subscriber", () => {
  const t = setup({ concurrency: 100 }); const s = t.scheduler;
  for (let n = 0; n < 4; n++) { s.subscribe("a", `a-${n}`, input(n)); s.subscribe("b", `b-${n}`, input(n)); }
  assert.equal(t.sent.length, 3);
  s.removeClient("a");
  for (const id of ["job-3", "job-2", "job-1", "job-4"]) t.reply(id);
  assert.equal(t.messages.a.filter((m) => m.op === "result").length, 0);
  assert.equal(t.messages.b.filter((m) => m.op === "result").length, 4);
});
test("three default lanes and short unavailable caching", () => {
  let now = 0; const t = setup({ now: () => now }); const s = t.scheduler;
  for (let n = 0; n < 4; n++) s.subscribe("a", `card-${n}`, input(n));
  assert.equal(t.sent.length, 3); assert.equal(s.queue.size, 1);
  t.malformed({ version: 1, id: "job-1", status: "unavailable", scores: null, reason: "retrieval_error", timings_ms: {} });
  const before = t.sent.length;
  s.subscribe("b", "negative-cache", input(0)); assert.equal(t.sent.length, before);
  now = 300000; s.subscribe("b", "retry-negative", input(0));
  t.reply("job-2"); assert.equal(t.sent.length, before + 1);
});
test("unavailable does not remain cached for five minutes", () => {
  let now = 0; const t = setup({ now: () => now }); const s = t.scheduler;
  s.subscribe("a", "first", input());
  t.malformed({ version: 1, id: "job-1", status: "unavailable", scores: null, reason: "retrieval_error", timings_ms: {} });
  now = 300000; s.subscribe("a", "second", input());
  assert.equal(t.sent.length, 2);
});
test("negative TTL is 60 seconds without automatic retry; positive TTL remains one hour", () => {
  let now = 0; const t = setup({ now: () => now }); const s = t.scheduler;
  s.subscribe("a", "negative", input());
  t.malformed({ version: 1, id: "job-1", status: "unavailable", scores: null, reason: "caption_access_blocked", timings_ms: {} });
  now = 59999; s.subscribe("b", "cached", input()); assert.equal(t.sent.length, 1);
  assert.equal(s.activity.records[0].cacheUses, 1);
  now = 60000; assert.equal(t.sent.length, 1, "expiry alone dispatches nothing");
  s.subscribe("a", "retry", input()); assert.equal(t.sent.length, 2); t.reply();
  now = 120000; s.subscribe("a", "positive", input()); assert.equal(t.sent.length, 2);
  now = 3660000; s.control("pause"); s.subscribe("a", "paused", input());
  assert.equal(t.sent.length, 2); assert.equal(t.messages.a.at(-1).reason, "paused");
});
