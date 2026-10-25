const { test } = require("node:test");
const assert = require("node:assert/strict");
const core = require("../../extension/core.js");
const input = { url: "https://www.youtube.com/watch?v=AAAAAAAAAAA&list=PL&index=3", title: "Actual  Title!", thumbnail: "https://i.ytimg.com/vi/AAAAAAAAAAA/hqdefault.jpg?q=1" };
test("strict majority, exact score contract, all four categories", () => {
  for (const category of core.classes) {
    const scores = Object.fromEntries(core.classes.map((key) => [key, key === category ? 51 : 0]));
    scores[core.classes.find((key) => key !== category)] = 49;
    assert.equal(core.majority({ status: "scored", scores }), category);
    assert.equal(core.majority({ status: "unavailable", scores }), null);
  }
  const scores = { irrelevant: 50, clickbait: 50, sensational: 0, valid: 0 };
  assert.equal(core.majority({ status: "scored", scores }), null);
  for (const invalid of [null, { ...scores, clickbait: 51 }, { ...scores, clickbait: NaN }, { ...scores, clickbait: "50" }, { ...scores, irrelevant: 49.5, clickbait: 50.5 }, { ...scores, extra: 0 }, { clickbait: 100 }, [100]]) {
    assert.equal(core.majority({ status: "scored", scores: invalid }), null);
  }
});
test("canonical individual links and full packaging identity", () => {
  const item = core.packaging(input);
  assert.equal(item.input.url, "https://www.youtube.com/watch?v=AAAAAAAAAAA");
  assert.equal(item.input.title, input.title);
  assert.equal(core.packaging({ ...input, url: "/shorts/AAAAAAAAAAA?feature=share" }).key, item.key);
  assert.notEqual(core.packaging({ ...input, title: "Actual Title!" }).key, item.key);
  assert.notEqual(core.packaging({ ...input, thumbnail: input.thumbnail + "&x=2" }).key, item.key);
  for (const url of ["http://youtube.com/watch?v=AAAAAAAAAAA", "https://evil.test/watch?v=AAAAAAAAAAA", "https://youtube.com:123/watch?v=AAAAAAAAAAA", "https://u@youtube.com/watch?v=AAAAAAAAAAA", "/playlist?list=PL", "/watch?v=AAAAAAAAAAA&v=BBBBBBBBBBB", "/shorts/AAAAAAAAAAA/extra", "javascript:alert(1)"]) assert.equal(core.videoURL(url), null);
  for (const thumbnail of ["data:image/gif;base64,AA", "/local/file", "https://i.ytimg.com.evil.test/x"]) assert.equal(core.packaging({ ...input, thumbnail }), null);
});
test("native result validation rejects malformed protocol", () => {
  const value = { version: 1, id: "job-1", status: "scored", scores: { irrelevant: 0, clickbait: 100, sensational: 0, valid: 0 }, reason: "", timings_ms: { total: 2 } };
  assert.ok(core.validReply(value, "job-1"));
  for (const bad of [{ ...value, version: 2 }, { ...value, id: "old" }, { ...value, extra: 1 }, { ...value, timings_ms: { raw: "input" } }, { ...value, status: "unavailable" }, { ...value, scores: { ...value.scores, valid: 1 } }]) assert.equal(core.validReply(bad, "job-1"), false);
});
