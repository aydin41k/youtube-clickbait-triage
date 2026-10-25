(function (root) {
  "use strict";
  const core = root.TriageCore || require("./core.js");
  const Activity = root.TriageActivity || require("./activity.js").Activity;
  class Scheduler {
    constructor(connect, options = {}) {
      this.connect = connect;
      this.limit = options.limit ?? 64;
      this.cacheLimit = options.cacheLimit ?? 512;
      this.now = options.now || Date.now;
      this.clients = new Map();
      this.queue = new Map();
      this.cache = new Map();
      this.inFlight = new Map();
      this.concurrency = Math.min(3, Math.max(1, Number.isInteger(options.concurrency) ? options.concurrency : 3));
      this.native = null;
      this.sequence = 0;
      this.paused = false;
      this.error = "";
      this.observers = new Set();
      this.activity = options.activity || new Activity({ now: this.now });
      this.activityObservers = new Set();
    }
    get active() { return this.inFlight.values().next().value || null; }
    status() { return { connected: !!this.native, working: this.inFlight.size > 0, active: this.inFlight.size, queued: this.queue.size, paused: this.paused, error: this.error }; }
    activitySnapshot() { return { state: this.status(), records: this.activity.snapshot(), queued: [...this.queue.values()].map((job) => ({ input: job.input, status: "queued_not_run" })) }; }
    broadcast() { const state = this.status(); for (const send of this.observers) send(state); for (const send of this.activityObservers) send(this.activitySnapshot()); }
    addClient(id, send) { this.clients.set(id, send); this.broadcast(); }
    send(id, message) { this.clients.get(id)?.(message); }
    unsubscribe(client, token) {
      for (const [key, job] of this.queue) {
        job.subscribers.delete(`${client}:${token}`);
        if (!job.subscribers.size) this.queue.delete(key);
      }
      for (const job of this.inFlight.values()) job.subscribers.delete(`${client}:${token}`);
      this.capacity();
      this.broadcast();
    }
    removeClient(client) {
      this.clients.delete(client);
      for (const [key, job] of this.queue) {
        for (const [id, subscriber] of job.subscribers) if (subscriber.client === client) job.subscribers.delete(id);
        if (!job.subscribers.size) this.queue.delete(key);
      }
      for (const job of this.inFlight.values()) for (const [id, subscriber] of job.subscribers) if (subscriber.client === client) job.subscribers.delete(id);
      if (!this.clients.size) this.close("last_youtube_tab_disconnected_outcome_unknown");
      this.capacity();
      this.broadcast();
    }
    subscribe(client, token, value) {
      if (!this.clients.has(client) || typeof token !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(token)) return;
      const item = core.packaging(value);
      if (!item) return this.send(client, { op: "result", token, result: { status: "unavailable", scores: null, reason: "invalid_packaging" } });
      if (this.paused || this.error) return this.send(client, { op: "deferred", token, reason: this.paused ? "paused" : this.error });
      const cached = this.cache.get(item.key);
      const ttl = cached?.result.status === "scored" ? 3600000 : 60000;
      if (cached && this.now() - cached.at < ttl) { this.activity.reuse(cached.attemptId); this.send(client, { op: "result", token, result: cached.result }); this.broadcast(); return; }
      this.cache.delete(item.key);
      let job = [...this.inFlight.values()].find((job) => job.key === item.key) || this.queue.get(item.key);
      if (!job && this.queue.size >= this.limit) return this.send(client, { op: "deferred", token, reason: "queue_full" });
      if (!job) {
        job = { ...item, subscribers: new Map() };
        this.queue.set(item.key, job);
      }
      job.subscribers.set(`${client}:${token}`, { client, token });
      this.send(client, { op: "accepted", token });
      this.pump();
      this.broadcast();
    }
    pump() {
      if (this.inFlight.size >= this.concurrency || this.paused || this.error || !this.queue.size) return;
      try {
        if (!this.native) this.native = this.connect((reply) => this.receive(reply), () => this.fail("local_host_disconnected"));
        while (this.inFlight.size < this.concurrency && this.queue.size && this.native && !this.error && !this.paused) {
          const [key, queued] = this.queue.entries().next().value;
          this.queue.delete(key);
          const job = { ...queued, id: `job-${++this.sequence}` };
          this.inFlight.set(job.id, job);
          job.attemptId = this.activity.start(job.input);
          this.native.postMessage({ version: 1, op: "classify", id: job.id, ...job.input });
        }
      } catch { this.fail("local_host_unavailable"); }
    }
    receive(reply) {
      const job = this.inFlight.get(reply?.id);
      if (!job) {
        // A known older job may be duplicated; anything else is protocol failure.
        const sequence = typeof reply?.id === "string" && /^job-([1-9]\d*)$/.exec(reply.id);
        if (sequence && Number(sequence[1]) <= this.sequence) return;
        return this.fail("invalid_local_reply");
      }
      if (!core.validReply(reply, job.id)) return this.fail("invalid_local_reply");
      this.inFlight.delete(job.id);
      const result = { status: reply.status, scores: reply.scores, reason: reply.reason, timings_ms: reply.timings_ms };
      this.activity.finish(job.attemptId, result);
      this.cache.set(job.key, { at: this.now(), result, attemptId: job.attemptId });
      while (this.cache.size > this.cacheLimit) this.cache.delete(this.cache.keys().next().value);
      for (const sub of job.subscribers.values()) this.send(sub.client, { op: "result", token: sub.token, result });
      this.pump();
      this.capacity();
      this.broadcast();
    }
    capacity() { for (const send of this.clients.values()) send({ op: "capacity" }); }
    close(reason = "connection_closed_outcome_unknown") {
      const active = [...this.inFlight.values()];
      const port = this.native;
      this.native = null;
      this.inFlight.clear();
      this.queue.clear();
      for (const job of active) this.activity.interrupt(job.attemptId, reason);
      port?.disconnect();
    }
    fail(reason) {
      this.error = reason;
      const jobs = [...this.queue.values(), ...this.inFlight.values()];
      this.close(`${reason}_outcome_unknown`);
      for (const job of jobs) for (const sub of job.subscribers.values()) this.send(sub.client, { op: "deferred", token: sub.token, reason });
      this.broadcast();
    }
    control(op) {
      if (op === "pause") { this.paused = true; this.close("paused_outcome_unknown"); }
      if (op === "resume" || op === "retry") { this.close(`${op}_outcome_unknown`); this.paused = false; this.error = ""; if (op === "retry") this.cache.clear(); }
      for (const send of this.clients.values()) send({ op: "reset", paused: this.paused });
      this.broadcast();
    }
  }
  root.TriageScheduler = Scheduler;
  if (typeof module !== "undefined") module.exports = Scheduler;
})(globalThis);
