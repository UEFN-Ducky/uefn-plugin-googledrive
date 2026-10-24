/* Google Workspace tab: the host bridge every view shares (window.GW).
 *
 *   GW.call(method, params, {timeout})   one host bridge call (plugin.call, prefs.get, ui.confirm…)
 *   GW.rpc(name, params, {timeout})      one plugin panel RPC; rejects on {ok: false, error}
 *   GW.on(type, fn) / GW.emit(type, e)   host push events and in-page events on one bus
 *   GW.subscribe(types)                  ask the host for push events of these types
 *   GW.visible() / GW.every(ms, fn)      panel.visibility, and a timer that sleeps while hidden
 *   GW.registerView(id, view)            views (gmail, drive, reader…) plug into the shell
 *   GW.esc(text) / GW.when(ms) / GW.accountColor(email)
 *
 * Mail text shown in the tab is data from outside: always put it through GW.esc.
 */
(function (root) {
  "use strict";

  const CHANNEL = "uefn-plugin-ui";
  const DEFAULT_TIMEOUT_MS = 60000;
  const pending = new Map();
  const handlers = new Map();
  const views = new Map();
  const subscribed = new Set();
  let visible = true;
  let seq = 0;

  const host = root.parent && root.parent !== root ? root.parent : null;

  function nextId() {
    seq += 1;
    return (root.crypto && root.crypto.randomUUID) ? root.crypto.randomUUID() : `gw-${Date.now()}-${seq}`;
  }

  function call(method, params, opts) {
    const timeout = (opts && opts.timeout) || DEFAULT_TIMEOUT_MS;
    if (!host) return Promise.reject(new Error("The Google tab runs inside Ducky."));
    const id = nextId();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`${method} took too long`));
      }, timeout);
      pending.set(id, { resolve, reject, timer });
      host.postMessage({ channel: CHANNEL, id, method, params: params || {} }, "*");
    });
  }

  function rpc(name, params, opts) {
    return call("plugin.call", { method: name, params: params || {} }, opts).then((out) => {
      let value = out;
      if (typeof value === "string") {
        try { value = JSON.parse(value); } catch (_e) { /* plain text result */ }
      }
      if (value && typeof value === "object" && value.ok === false) {
        throw new Error(String(value.error || `${name} failed`));
      }
      return value;
    });
  }

  function on(type, fn) {
    if (!handlers.has(type)) handlers.set(type, new Set());
    handlers.get(type).add(fn);
    return () => handlers.get(type).delete(fn);
  }

  function emit(type, event) {
    const e = Object.assign({ type }, event || {});
    for (const key of [type, "*"]) {
      for (const fn of Array.from(handlers.get(key) || [])) {
        try { fn(e); } catch (err) { console.warn("Google tab handler failed:", err && err.message); }
      }
    }
  }

  function subscribe(types) {
    const fresh = (types || []).filter((t) => !subscribed.has(t));
    fresh.forEach((t) => subscribed.add(t));
    if (!fresh.length || !host) return Promise.resolve();
    return call("plugin.subscribe", { types: Array.from(subscribed) }).catch(() => {});
  }

  function onMessage(ev) {
    const d = ev.data;
    if (!d || d.channel !== CHANNEL) return;
    if (d.id && pending.has(d.id)) {
      const p = pending.get(d.id);
      pending.delete(d.id);
      clearTimeout(p.timer);
      if (d.ok) p.resolve(d.result);
      else p.reject(new Error(d.error || "bridge error"));
      return;
    }
    const e = d.event;
    if (!e || !e.type) return;
    if (e.type === "panel.visibility") {
      const was = visible;
      visible = e.visible !== false;
      if (visible !== was) emit(visible ? "gw:shown" : "gw:hidden", {});
    }
    emit(e.type, e);
  }

  /** setInterval that skips ticks while the tab is hidden and runs once when it comes back. */
  function every(ms, fn) {
    let missed = false;
    const tick = () => {
      if (!visible) { missed = true; return; }
      missed = false;
      fn();
    };
    const timer = setInterval(tick, ms);
    const off = on("gw:shown", () => { if (missed) tick(); });
    return () => { clearInterval(timer); off(); };
  }

  function registerView(id, view) {
    views.set(id, view);
    emit("gw:view-registered", { id });
  }

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, (c) => ESC[c]);
  }

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  /** 14:05 today, Oct 24 this year, 2025-10-24 before (Gmail internalDate ms or a date string). */
  function when(value, now) {
    let d = null;
    if (value != null && value !== "") {
      const n = Number(value);
      d = new Date(Number.isFinite(n) && String(value).trim() !== "" ? n : String(value));
    }
    if (!d || Number.isNaN(d.getTime())) return "";
    const today = now || new Date();
    const pad = (x) => String(x).padStart(2, "0");
    if (d.toDateString() === today.toDateString()) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (d.getFullYear() === today.getFullYear()) return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  /* Account dots use the Appearance accent palette, in the order accounts were connected. */
  const PALETTE = ["blue", "purple", "green", "amber", "yellow", "red"];
  const colorOf = new Map();
  function accountColor(email) {
    const key = String(email || "").toLowerCase();
    if (!colorOf.has(key)) colorOf.set(key, PALETTE[colorOf.size % PALETTE.length]);
    return `var(--${colorOf.get(key)})`;
  }

  if (root.addEventListener) root.addEventListener("message", onMessage);

  const GW = {
    CHANNEL, call, rpc, on, emit, subscribe, every, registerView, esc, when, accountColor,
    views, visible: () => visible,
    _onMessage: onMessage,
  };
  root.GW = GW;
  if (typeof module !== "undefined" && module.exports) module.exports = GW;
})(typeof window !== "undefined" ? window : globalThis);
