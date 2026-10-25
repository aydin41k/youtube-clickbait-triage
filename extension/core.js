(function (root) {
  "use strict";
  const classes = ["irrelevant", "clickbait", "sensational", "valid"];
  const colors = { clickbait: "#dc2626", irrelevant: "#eab308", sensational: "#4b5563", valid: "#16a34a" };
  function videoURL(value, base = "https://www.youtube.com/") {
    try {
      const url = new URL(value, base);
      if (url.protocol !== "https:" || url.username || url.password || url.port) return null;
      let id;
      if (url.hostname === "youtu.be") id = url.pathname.slice(1);
      else if (["www.youtube.com", "youtube.com", "m.youtube.com"].includes(url.hostname)) {
        if (url.pathname === "/watch" && url.searchParams.getAll("v").length === 1) id = url.searchParams.get("v");
        else if (url.pathname.startsWith("/shorts/")) id = url.pathname.slice(8);
      }
      return /^[A-Za-z0-9_-]{11}$/.test(id || "") ? { id, url: `https://www.youtube.com/watch?v=${id}` } : null;
    } catch { return null; }
  }
  function thumbnailURL(value) {
    try {
      const url = new URL(value);
      return typeof value === "string" && value.length <= 4096 && url.protocol === "https:" &&
        !url.username && !url.password && !url.port && ["i.ytimg.com", "img.youtube.com"].includes(url.hostname) ? value : null;
    } catch { return null; }
  }
  function packaging(value) {
    if (!value || typeof value !== "object" || Object.keys(value).sort().join() !== "thumbnail,title,url") return null;
    const video = videoURL(value.url);
    if (!video || typeof value.url !== "string" || value.url.length > 4096 || typeof value.title !== "string" ||
        !value.title.trim() || value.title.length > 1000 || !thumbnailURL(value.thumbnail)) return null;
    const input = { url: video.url, title: value.title, thumbnail: value.thumbnail };
    return { input, key: JSON.stringify([video.id, input.title, input.thumbnail]) };
  }
  function validScores(scores) {
    return scores && typeof scores === "object" && !Array.isArray(scores) &&
      Object.keys(scores).sort().join() === [...classes].sort().join() &&
      classes.every((key) => Number.isInteger(scores[key]) && scores[key] >= 0 && scores[key] <= 100) &&
      classes.reduce((sum, key) => sum + scores[key], 0) === 100;
  }
  function majority(result) {
    return result?.status === "scored" && validScores(result.scores) ? classes.find((key) => result.scores[key] > 50) || null : null;
  }
  function validReply(value, id) {
    if (!value || Object.keys(value).sort().join() !== "id,reason,scores,status,timings_ms,version" || value.version !== 1 || value.id !== id ||
        typeof value.reason !== "string" || value.reason.length > 200 || !value.timings_ms || typeof value.timings_ms !== "object" || Array.isArray(value.timings_ms) ||
        Object.keys(value.timings_ms).some((key) => key !== "total") ||
        Object.values(value.timings_ms).some((n) => !Number.isInteger(n) || n < 0 || n > 2000000)) return false;
    return value.status === "scored" ? validScores(value.scores) && value.reason === "" : value.status === "unavailable" && value.scores === null;
  }
  const api = { classes, colors, videoURL, thumbnailURL, packaging, validScores, majority, validReply };
  root.TriageCore = api;
  if (typeof module !== "undefined") module.exports = api;
})(globalThis);
