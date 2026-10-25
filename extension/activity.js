(function (root) {
  "use strict";
  const core = root.TriageCore || require("./core.js");
  const MAX_RECORDS = 100;
  function validRecord(value) {
    if (!value || Object.keys(value).sort().join() !== "cacheUses,completedAt,id,input,reason,scores,status,submittedAt,timing" ||
        typeof value.id !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(value.id) || !core.packaging(value.input) ||
        !["running", "scored", "unavailable", "interrupted"].includes(value.status) ||
        !Number.isInteger(value.submittedAt) || value.submittedAt < 0 || value.submittedAt > 1e14 ||
        (value.completedAt !== null && (!Number.isInteger(value.completedAt) || value.completedAt < value.submittedAt || value.completedAt > 1e14)) ||
        typeof value.reason !== "string" || value.reason.length > 200 || !Number.isInteger(value.cacheUses) || value.cacheUses < 0 || value.cacheUses > 1000000) return false;
    if (value.status === "scored" ? !core.validScores(value.scores) || value.reason !== "" : value.scores !== null) return false;
    if (value.status === "running" ? value.completedAt !== null : value.completedAt === null) return false;
    return value.timing === null || (value.timing && typeof value.timing === "object" && !Array.isArray(value.timing) && Object.keys(value.timing).sort().join() === "kind,ms" && ["analysis_total", "observed_elapsed"].includes(value.timing.kind) && Number.isInteger(value.timing.ms) && value.timing.ms >= 0 && value.timing.ms <= 1e9);
  }
  class Activity {
    constructor(options = {}) {
      this.now = options.now || Date.now;
      this.prefix = options.prefix || (root.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`);
      this.sequence = 0;
      this.records = [];
      this.changed = options.changed || (() => {});
    }
    snapshot() { return JSON.parse(JSON.stringify(this.records)); }
    start(input) {
      const record = { id: `${this.prefix}-${++this.sequence}`, input: core.packaging(input).input, submittedAt: this.now(), completedAt: null,
        status: "running", reason: "", scores: null, timing: null, cacheUses: 0 };
      this.records.unshift(record);
      while (this.records.length > MAX_RECORDS) {
        let index = this.records.findLastIndex((item) => item.status !== "running");
        if (index < 0) index = this.records.length - 1;
        this.records.splice(index, 1);
      }
      this.changed();
      return record.id;
    }
    finish(id, result) {
      const record = this.records.find((item) => item.id === id);
      if (!record || record.status !== "running") return;
      record.status = result.status;
      record.scores = result.scores;
      record.reason = result.reason;
      record.completedAt = this.now();
      if (Number.isInteger(result.timings_ms?.total)) record.timing = { kind: "analysis_total", ms: result.timings_ms.total };
      this.changed();
    }
    interrupt(id, reason) {
      const record = this.records.find((item) => item.id === id);
      if (!record || record.status !== "running") return;
      record.status = "interrupted";
      record.reason = reason.slice(0, 200);
      record.completedAt = this.now();
      record.timing = { kind: "observed_elapsed", ms: Math.max(0, record.completedAt - record.submittedAt) };
      this.changed();
    }
    reuse(id) {
      const record = this.records.find((item) => item.id === id);
      if (record) { record.cacheUses = Math.min(1000000, record.cacheUses + 1); this.changed(); }
    }
    restore(value) {
      const seen = new Set();
      this.records = Array.isArray(value) ? value.slice(0, MAX_RECORDS).filter((item) => validRecord(item) && !seen.has(item.id) && seen.add(item.id)).map((item) => JSON.parse(JSON.stringify(item))) : [];
      for (const record of this.records) if (record.status === "running") {
        record.status = "interrupted"; record.reason = "background_worker_restarted_outcome_unknown"; record.completedAt = Math.max(this.now(), record.submittedAt); record.timing = null;
      }
      this.changed();
    }
    clearCompleted() { this.records = this.records.filter((record) => record.status === "running"); this.changed(); }
  }
  root.TriageActivity = Activity;
  root.TriageActivityValid = validRecord;
  if (typeof module !== "undefined") module.exports = { Activity, validRecord, MAX_RECORDS };
})(globalThis);
