const { test } = require("node:test");
const assert = require("node:assert/strict");
const { Activity, validRecord } = require("../../extension/activity.js");
const Scheduler = require("../../extension/scheduler.js");
const input = (title = "Actual title") => ({ url: "https://www.youtube.com/watch?v=AAAAAAAAAAA", title, thumbnail: "https://i.ytimg.com/vi/AAAAAAAAAAA/hqdefault.jpg?actual=1" });
const scored = { status: "scored", scores: { irrelevant: 1, clickbait: 60, sensational: 30, valid: 9 }, reason: "", timings_ms: { total: 1234 } };
function setup(options = {}) {
  let receive; let now = 1000; const sent = [];
  const activity = new Activity({ now: () => now, prefix: "worker-A" });
  const scheduler = new Scheduler((callback) => { receive = callback; return { postMessage: (m) => sent.push(m), disconnect() {} }; }, { concurrency: 1, ...options, activity, now: () => now });
  scheduler.addClient("a", () => {}); scheduler.addClient("b", () => {});
  return { scheduler, activity, sent, tick: () => { now += 50; }, reply: (result = scored) => receive({ version: 1, id: scheduler.active.id, ...result }) };
}
test("one dispatch is one attempt; queued and duplicate/cache subscribers aren't runs", () => {
  const t = setup(); const s = t.scheduler;
  s.subscribe("a", "one", input()); s.subscribe("b", "duplicate", input()); s.subscribe("a", "queued", input("Next"));
  assert.equal(t.sent.length, 1); assert.equal(t.activity.records.length, 1); assert.equal(t.activity.records[0].status, "running");
  assert.equal(s.activitySnapshot().queued[0].status, "queued_not_run");
  s.unsubscribe("a", "queued"); assert.equal(t.activity.records.length, 1); assert.equal(s.queue.size, 0);
  t.tick(); t.reply(); assert.equal(t.activity.records[0].status, "scored"); assert.deepEqual(t.activity.records[0].timing, { kind: "analysis_total", ms: 1234 }); assert.deepEqual(t.activity.records[0].scores, scored.scores);
  s.subscribe("a", "cached", input()); assert.equal(t.sent.length, 1); assert.equal(t.activity.records.length, 1); assert.equal(t.activity.records[0].cacheUses, 1);
  s.control("retry"); assert.equal(t.activity.records.length, 1);
});
test("all close paths honestly interrupt active work, never complete queued-only jobs", () => {
  for (const end of [s => s.control("pause"), s => s.control("retry"), s => { s.removeClient("a"); s.removeClient("b"); }, s => s.fail("local_host_disconnected"), s => s.receive({ wrong: true })]) {
    const t = setup({ concurrency: 3 }); const s = t.scheduler;
    for (let n = 0; n < 4; n++) s.subscribe("a", `card-${n}`, input(`Title ${n}`));
    t.tick(); end(s);
    assert.equal(t.activity.records.length, 3);
    for (const record of t.activity.records) { assert.equal(record.status, "interrupted"); assert.equal(record.scores, null); assert.match(record.reason, /unknown/); assert.deepEqual(record.timing, { kind: "observed_elapsed", ms: 50 }); }
  }
});
test("a slow active record survives more than 100 faster completions", () => {
  const activity = new Activity({ prefix: "slow", now: () => 10 });
  const running = Array.from({ length: 3 }, (_, n) => activity.start(input(`Slow ${n}`)));
  for (let n = 0; n < 150; n++) { const id = activity.start(input(`Fast ${n}`)); activity.finish(id, scored); }
  assert.equal(activity.records.length, 100);
  for (const id of running) assert.equal(activity.records.find((item) => item.id === id)?.status, "running");
  activity.clearCompleted(); assert.equal(activity.records.length, 3);
});
test("unavailable retrieval and no-majority preserve total timing and exact outcome", () => {
  const t = setup(); t.scheduler.subscribe("a", "one", input()); t.reply({ status: "unavailable", scores: null, reason: "no_english_captions", timings_ms: { total: 15 } });
  assert.equal(t.activity.records[0].status, "unavailable"); assert.equal(t.activity.records[0].scores, null); assert.equal(t.activity.records[0].reason, "no_english_captions");
  t.scheduler.subscribe("a", "two", input("Second")); t.reply({ ...scored, scores: { irrelevant: 50, clickbait: 50, sensational: 0, valid: 0 } }); assert.equal(t.activity.records[0].scores.clickbait, 50);
});
test("clear completed preserves active work, pause preference and completed cache", () => {
  const t=setup();const s=t.scheduler;s.subscribe("a","first",input());t.reply();s.subscribe("a","running",input("Running"));
  const active=s.active;const cacheSize=s.cache.size;s.activity.clearCompleted();assert.equal(s.active,active);assert.equal(s.paused,false);assert.equal(s.cache.size,cacheSize);assert.equal(t.activity.records.length,1);assert.equal(t.activity.records[0].status,"running");
  s.control("pause");s.activity.clearCompleted();assert.equal(s.paused,true);assert.equal(s.cache.size,cacheSize);assert.equal(s.active,null);
});
test("bounded validated restore interrupts running records; IDs survive distinct worker lifetimes", () => {
  const first = new Activity({ now: () => 100, prefix: "old-worker" }); const id = first.start(input());
  const next = new Activity({ now: () => 200, prefix: "new-worker" }); next.restore(first.snapshot());
  assert.equal(next.records[0].status, "interrupted"); assert.match(next.records[0].reason, /worker_restarted/); assert.equal(next.records[0].timing, null);
  assert.notEqual(next.start(input()), id);
  next.clearCompleted(); assert.equal(next.records.length, 1); assert.equal(next.records[0].status, "running");
  const record = first.snapshot()[0];
  for (const bad of [{ ...record, raw_model_response: "no" }, { ...record, scores: scored.scores }, { ...record, input: input(" ") }, { ...record, input: { ...record.input, thumbnail: "file:///etc/passwd" } }, { ...record, timing: undefined }, { ...record, cacheUses: Infinity }, { ...record, id: "x".repeat(101) }]) {
    assert.ok(!validRecord(bad)); next.restore([bad]); assert.equal(next.records.length, 0);
  }
  for (let i=0; i<150; i++) { const run = next.start(input(`Title ${i}`)); next.finish(run, scored); }
  assert.equal(next.records.length, 100); assert.equal(next.records[0].input.title, "Title 149");
});
