(function (root) {
  "use strict";
  const core = root.TriageCore || require("./core.js");
  class Frames {
    constructor(document) {
      this.document = document;
      this.window = document.defaultView;
      this.entries = new Map();
      this.pending = null;
      this.host = null;
      this.layer = null;
      this.onChange = () => this.refresh();
      this.window.addEventListener("scroll", this.onChange, true);
      this.window.addEventListener("resize", this.onChange);
      this.resize = this.window.ResizeObserver ? new this.window.ResizeObserver(this.onChange) : null;
    }
    ensureLayer() {
      if (this.host) return;
      this.host = this.document.createElement("div");
      this.host.setAttribute("data-yct-overlay", "");
      this.host.setAttribute("aria-hidden", "true");
      this.host.style.cssText = "position:fixed!important;inset:0!important;z-index:2147483647!important;pointer-events:none!important;overflow:hidden!important;contain:strict!important;";
      this.layer = this.host.attachShadow({ mode: "open" });
      const style = this.document.createElement("style");
      style.textContent = ":host,*{pointer-events:none!important} .frame{position:absolute;box-sizing:border-box;overflow:hidden;}";
      this.layer.append(style);
      this.document.documentElement.append(this.host);
    }
    show(card, target, category) {
      if (!target || !core.colors[category]) return this.remove(card);
      this.ensureLayer();
      let entry = this.entries.get(card);
      if (entry?.target !== target) this.remove(card);
      entry = this.entries.get(card);
      if (!entry) {
        const frame = this.document.createElement("div");
        frame.className = "frame";
        this.layer.append(frame);
        entry = { target, frame };
        this.entries.set(card, entry);
        this.resize?.observe(target);
      }
      const color = core.colors[category];
      entry.frame.setAttribute("data-yct-frame", category);
      const rgb = color.slice(1).match(/../g).map((hex) => parseInt(hex, 16)).join(",");
      entry.frame.style.boxShadow = `inset 0 0 0 3px ${color}`;
      entry.frame.style.background = ["right", "left", "bottom", "top"].map((direction) => `linear-gradient(to ${direction},rgba(${rgb},.7),transparent 18px)`).join(",");
      this.refresh();
    }
    remove(card) {
      const entry = this.entries.get(card);
      if (!entry) return;
      this.resize?.unobserve(entry.target);
      entry.frame.remove();
      this.entries.delete(card);
    }
    refresh() {
      if (this.pending !== null) return;
      const raf = this.window.requestAnimationFrame || ((fn) => this.window.setTimeout(fn, 0));
      this.pending = raf.call(this.window, () => {
        this.pending = null;
        for (const [card, entry] of this.entries) {
          if (!card.isConnected || !entry.target.isConnected) { this.remove(card); continue; }
          const rect = entry.target.getBoundingClientRect();
          const targetStyle = this.window.getComputedStyle(entry.target);
          const visible = rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0 && rect.top < this.window.innerHeight && rect.left < this.window.innerWidth && targetStyle.visibility !== "hidden" && targetStyle.opacity !== "0";
          entry.frame.hidden = !visible;
          if (!visible) continue;
          let left = Math.max(0, rect.left), top = Math.max(0, rect.top), right = Math.min(this.window.innerWidth, rect.right), bottom = Math.min(this.window.innerHeight, rect.bottom);
          let hidden = false;
          const rootStyle = this.window.getComputedStyle(this.document.documentElement);
          const bodyStyle = this.document.body && this.window.getComputedStyle(this.document.body);
          // CSS Overflow's HTML/BODY propagation rule: BODY's *used* overflow
          // is visible when its computed overflow is transferred to the viewport.
          // This is not a general zero-size exemption for real clipping boxes.
          const bodyPropagates = bodyStyle && rootStyle.overflowX === "visible" && rootStyle.overflowY === "visible" && bodyStyle.display !== "none" &&
            (!rootStyle.contain || rootStyle.contain === "none") && (!bodyStyle.contain || bodyStyle.contain === "none");
          const rounded = (value) => /[1-9]/.test(value || "");
          let radius = targetStyle.borderRadius || "0px";
          let hasRadius = rounded(radius);
          for (let ancestor = entry.target.parentElement; ancestor && ancestor !== this.document.documentElement; ancestor = ancestor.parentElement) {
            const css = this.window.getComputedStyle(ancestor);
            if (css.visibility === "hidden" || css.display === "none" || css.opacity === "0") hidden = true;
            const bounds = ancestor.getBoundingClientRect();
            if (!hasRadius && rounded(css.borderRadius) && /hidden|clip|auto|scroll/.test(`${css.overflowX} ${css.overflowY}`) &&
                Math.abs(bounds.left-rect.left)<1 && Math.abs(bounds.top-rect.top)<1 && Math.abs(bounds.width-rect.width)<1 && Math.abs(bounds.height-rect.height)<1) { radius=css.borderRadius;hasRadius=true; }
            if (ancestor === this.document.body && bodyPropagates) continue;
            if (/hidden|clip|auto|scroll/.test(css.overflowX)) { left = Math.max(left, bounds.left); right = Math.min(right, bounds.right); }
            if (/hidden|clip|auto|scroll/.test(css.overflowY)) { top = Math.max(top, bounds.top); bottom = Math.min(bottom, bounds.bottom); }
          }
          entry.frame.hidden = hidden || right <= left || bottom <= top;
          Object.assign(entry.frame.style, { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px`, borderRadius: radius,
            clipPath: `inset(${Math.max(0, top - rect.top)}px ${Math.max(0, rect.right - right)}px ${Math.max(0, rect.bottom - bottom)}px ${Math.max(0, left - rect.left)}px)` });
        }
      });
    }
    destroy() {
      const cancel = this.window.cancelAnimationFrame || this.window.clearTimeout;
      if (this.pending !== null) cancel.call(this.window, this.pending);
      this.window.removeEventListener("scroll", this.onChange, true);
      this.window.removeEventListener("resize", this.onChange);
      this.resize?.disconnect();
      this.entries.clear();
      this.host?.remove();
      this.host = this.layer = null;
    }
  }
  root.TriageFrames = Frames;
  if (typeof module !== "undefined") module.exports = Frames;
})(globalThis);
