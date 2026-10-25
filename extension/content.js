(function (root) {
  "use strict";
  const core = root.TriageCore || require("./core.js");
  const Frames = root.TriageFrames || require("./frames.js");
  const selector = ["ytd-rich-grid-media", "ytd-video-renderer", "ytd-grid-video-renderer", "ytd-compact-video-renderer",
    "ytd-playlist-video-renderer", "yt-lockup-view-model", "ytd-reel-item-renderer", "yt-shorts-lockup-view-model",
    "ytd-shorts-lockup-view-model", "ytm-shorts-lockup-view-model", "ytm-shorts-lockup-view-model-v2"].join(",");
  const titleSelector = "#video-title,.yt-lockup-metadata-view-model__title,.ytLockupMetadataViewModelTitle,.ytLockupMetadataViewModelHeadingReset,.shortsLockupViewModelHostMetadataTitle,.shorts-lockup-view-model-wiz__title";
  const adSelector = "ytd-ad-slot-renderer,ytd-promoted-video-renderer,ytd-display-ad-renderer,ytd-in-feed-ad-layout-renderer,[is-ad]";
  function media(card, url) {
    const id = core.videoURL(url)?.id;
    return [...card.querySelectorAll("a[href]")].find((anchor) => core.videoURL(anchor.href)?.id === id && anchor.querySelector("img"))?.querySelector("img");
  }
  function extract(card) {
    if (!card.isConnected || card.closest(adSelector) || card.querySelector("yt-collection-thumbnail-view-model,ytm-collection-thumbnail-view-model,.yt-collection-thumbnail-view-model,.yt-collection-thumbnail-view-model__thumbnail,.ytCollectionThumbnailViewModelHost")) return null;
    const titleNode = card.querySelector(titleSelector);
    if (!titleNode) return null;
    // Dedicated title only: ARIA labels often append channel, views and duration.
    const title = titleNode.getAttribute("title") || titleNode.textContent;
    const anchors = [...card.querySelectorAll("a[href]")];
    const titleAnchor = titleNode.closest("a[href]") || titleNode.querySelector("a[href]");
    if (titleAnchor && !core.videoURL(titleAnchor.href)) return null;
    const link = (titleAnchor && core.videoURL(titleAnchor.href) && titleAnchor) ||
      anchors.find((anchor) => core.videoURL(anchor.href) && (anchor.querySelector("img") || anchor.contains(titleNode)));
    if (!link) return null;
    const video = core.videoURL(link.href);
    // Thumbnail is an actual image in the individual-video link, not an avatar.
    const image = media(card, video.url);
    if (!image) return null;
    const thumbnail = image.currentSrc || image.getAttribute("src");
    return core.packaging({ url: video.url, title, thumbnail });
  }
  class Cards {
    constructor(document, connect, options = {}) {
      this.document = document;
      this.connect = connect;
      this.delay = options.delay ?? 40;
      this.states = new Map();
      this.frames = new Frames(document);
      this.dirty = new Set();
      this.sequence = 0;
      this.port = null;
      this.paused = false;
      this.stopped = false;
      this.timer = null;
      this.retryTimer = null;
      this.onLoad = (event) => this.mark(event.target);
      this.onNavigate = () => this.scan(document);
      this.observer = new document.defaultView.MutationObserver((mutations) => {
        for (const mutation of mutations) {
          // Ignore our own decorations entirely.
          const target = mutation.target.nodeType === 1 ? mutation.target : mutation.target.parentElement;
          if (target?.closest("[data-yct-badge],[data-yct-overlay]")) continue;
          if (mutation.type === "attributes" && mutation.attributeName.startsWith("data-yct-")) continue;
          this.mark(mutation.target);
          for (const added of mutation.addedNodes) if (added.nodeType === 1 && !added.matches("[data-yct-overlay]")) this.scan(added);
          for (const removed of mutation.removedNodes) if (removed.nodeType === 1) this.prune(removed);
        }
        this.frames.refresh();
      });
    }
    start() {
      this.open();
      this.observer.observe(this.document.documentElement, { subtree: true, childList: true, characterData: true,
        attributes: true, attributeFilter: ["href", "title", "src", "srcset", "is-ad", "class", "style", "hidden"] });
      this.document.addEventListener("load", this.onLoad, true);
      this.document.addEventListener("yt-navigate-finish", this.onNavigate);
      this.scan(this.document);
      return this;
    }
    open() {
      if (this.stopped) return;
      try {
        const port = this.connect();
        this.port = port;
        port.onMessage.addListener((message) => { if (!this.stopped && this.port === port) this.message(message); });
        port.onDisconnect.addListener(() => {
          if (this.port !== port || this.stopped) return;
          this.port = null;
          for (const state of this.states.values()) { state.accepted = false; state.done = false; }
          this.retryTimer = setTimeout(() => { this.open(); this.retry(); }, 2000);
        });
        this.retry();
      } catch { this.retryTimer = setTimeout(() => { this.open(); this.retry(); }, 2000); }
    }
    post(message) { try { this.port?.postMessage(message); } catch {} }
    clear(card) { card.removeAttribute("data-yct-category"); this.frames.remove(card); }
    label(card, text) {
      let badge = card.querySelector(":scope > [data-yct-badge]");
      if (!badge) {
        badge = this.document.createElement("span");
        badge.setAttribute("data-yct-badge", "");
        card.append(badge);
      }
      if (badge.textContent !== text) badge.textContent = text;
      if (badge.title !== text) badge.title = text;
    }
    mark(node) {
      const element = node.nodeType === 1 ? node : node.parentElement;
      const card = element?.closest(selector);
      if (card) {
        // Clear immediately, before the debounced queue operation, if recycled.
        const state = this.states.get(card);
        if (state && extract(card)?.key !== state.key) this.clear(card);
        this.dirty.add(card);
        this.schedule();
      }
    }
    scan(node) {
      if (node.matches?.(selector)) this.dirty.add(node);
      for (const card of node.querySelectorAll?.(selector) || []) this.dirty.add(card);
      this.schedule();
    }
    prune(node) {
      for (const [card, state] of this.states) if (node === card || node.contains(card)) {
        if (!card.isConnected) { this.post({ op: "unsubscribe", token: state.token }); this.clear(card); card.querySelector(":scope > [data-yct-badge]")?.remove(); this.states.delete(card); }
      }
    }
    schedule() { if (!this.timer) this.timer = setTimeout(() => { this.timer = null; this.flush(); }, this.delay); }
    flush() {
      for (const card of this.dirty) {
        // Only the innermost supported video-card wrapper submits work.
        const item = card.querySelector(selector) ? null : extract(card);
        let state = this.states.get(card);
        if (state && state.key !== item?.key) {
          this.post({ op: "unsubscribe", token: state.token });
          this.clear(card);
          card.querySelector(":scope > [data-yct-badge]")?.remove();
          this.states.delete(card);
          state = null;
        }
        if (!item) continue;
        if (!state) { state = { key: item.key, input: item.input, token: `card-${++this.sequence}`, accepted: false, done: false }; this.states.set(card, state); this.label(card, this.paused ? "Triage: paused" : "Triage: pending"); }
        if (state.category) this.frames.show(card, media(card, state.input.url), state.category);
        this.submit(state);
      }
      this.dirty.clear();
    }
    submit(state) {
      if (this.port && !this.paused && !state.accepted && !state.done) {
        state.accepted = true;
        this.post({ op: "subscribe", token: state.token, input: state.input });
      }
    }
    retry() { for (const state of this.states.values()) this.submit(state); }
    message(message) {
      if (this.stopped) return;
      if (message?.op === "capacity") { this.retry(); return; }
      if (message?.op === "reset") {
        this.paused = !!message.paused;
        for (const [card, state] of this.states) { state.accepted = false; state.done = false; state.category = null; this.clear(card); this.label(card, this.paused ? "Triage: paused" : "Triage: pending"); }
        this.retry();
        return;
      }
      for (const [card, state] of this.states) if (state.token === message?.token) {
        if (!card.isConnected || extract(card)?.key !== state.key) { this.mark(card); return; }
        if (message.op === "deferred") { state.accepted = false; this.label(card, `Triage: ${typeof message.reason === "string" ? message.reason.slice(0, 200) : "waiting"}`); return; }
        if (message.op !== "result") return;
        state.done = true;
        const category = core.majority(message.result);
        state.category = category;
        this.clear(card);
        if (category) { card.setAttribute("data-yct-category", category); this.frames.show(card, media(card, state.input.url), category); }
        this.label(card, category ? `Triage: ${category} ${message.result.scores[category]}%` :
          message.result?.status === "scored" && core.validScores(message.result.scores) ? "Triage: no estimate above 50%" :
          `Triage unavailable: ${typeof message.result?.reason === "string" ? message.result.reason.slice(0, 200) : "invalid result"}`);
        return;
      }
    }
    stop() {
      if (this.stopped) return;
      this.stopped = true;
      clearTimeout(this.timer);
      clearTimeout(this.retryTimer);
      this.observer.disconnect();
      this.document.removeEventListener("load", this.onLoad, true);
      this.document.removeEventListener("yt-navigate-finish", this.onNavigate);
      this.port?.disconnect();
      this.port = null;
      for (const card of this.states.keys()) { this.clear(card); card.querySelector(":scope > [data-yct-badge]")?.remove(); }
      this.states.clear();
      this.dirty.clear();
      this.frames.destroy();
    }
  }
  function lifecycle(window, connect, options) {
    let cards = null;
    const start = () => { if (!cards) cards = new Cards(window.document, connect, options).start(); };
    const hide = () => { cards?.stop(); cards = null; };
    const show = (event) => { if (event.persisted) start(); };
    window.addEventListener("pagehide", hide);
    window.addEventListener("pageshow", show);
    start();
    return { stop() { hide(); window.removeEventListener("pagehide", hide); window.removeEventListener("pageshow", show); } };
  }
  const api = { Cards, extract, selector, lifecycle };
  root.TriageCards = api;
  if (typeof module !== "undefined") module.exports = api;
  else if (typeof chrome !== "undefined" && chrome.runtime?.id) {
    lifecycle(window, () => chrome.runtime.connect({ name: "triage-cards" }));
  }
})(globalThis);
