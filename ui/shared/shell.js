/* Google Workspace tab shell: Gmail | Drive pills, Google accounts, sign-in and Turn on Gmail.
 *
 * Views register with GW.registerView(id, view) and get:
 *   view.mount(el, shell)   once, the first time their pill shows
 *   view.show() / hide()    pill switches and panel visibility
 *   view.update(accounts)   the connected accounts changed
 *   view.open(request)      an open request (google_open, an [Open] link)
 * The shell keeps every mounted view alive, so a pill switch never loses what is open.
 */
(function (root) {
  "use strict";

  const GW = root.GW;
  const VIEWS = ["gmail", "drive"];
  const LABELS = { gmail: "Gmail", drive: "Drive" };
  const CONNECT_TIMEOUT_MS = 320000;
  const ACCOUNT_EVENTS = ["google_accounts_changed", "google_connected", "google_disconnected"];

  const state = {
    view: "gmail",
    accounts: [],
    loaded: false,
    error: "",
    connecting: "",
    mounted: new Set(),
  };

  const $ = (sel) => root.document.querySelector(sel);

  function scopeHas(acct, word) {
    const scopes = acct.scopes || acct.granted_scopes || acct.scope || [];
    const list = Array.isArray(scopes) ? scopes : String(scopes).split(/\s+/);
    return list.some((s) => String(s).toLowerCase().includes(word));
  }

  function flag(...values) {
    for (const v of values) if (v !== undefined && v !== null) return Boolean(v);
    return undefined;
  }

  /** Accounts from google_accounts_list / google_status, whatever shape they come in. */
  function normalizeAccounts(out) {
    if (!out || typeof out !== "object") return [];
    let list = out.accounts !== undefined ? out.accounts : (out.items || []);
    if (list && !Array.isArray(list) && typeof list === "object") {
      list = Object.entries(list).map(([email, v]) => Object.assign({ email }, v || {}));
    }
    if ((!list || !list.length) && out.connected && out.account) list = [{ email: out.account }];
    return (list || []).map((a) => {
      if (typeof a === "string") return { email: a, gmail: false, manage_drive: false };
      const access = a.access || {};
      const gmail = flag(a.gmail, a.gmail_enabled, access.gmail);
      const manage = flag(a.manage_drive, a.drive_manage, access.manage_drive);
      return {
        email: String(a.email || a.account || a.id || ""),
        name: String(a.name || ""),
        gmail: gmail !== undefined ? gmail : scopeHas(a, "gmail"),
        manage_drive: manage !== undefined ? manage : scopeHas(a, "auth/drive "),
      };
    }).filter((a) => a.email);
  }

  async function fetchAccounts() {
    try {
      return normalizeAccounts(await GW.rpc("google_accounts_list"));
    } catch (first) {
      try {
        return normalizeAccounts(await GW.rpc("google_status"));
      } catch (_second) {
        throw first;
      }
    }
  }

  async function reloadAccounts() {
    try {
      state.accounts = await fetchAccounts();
      state.error = "";
    } catch (err) {
      state.error = (err && err.message) || "Couldn't reach Google.";
    }
    state.loaded = true;
    state.accounts.forEach((a) => GW.accountColor(a.email));
    render();
    for (const id of state.mounted) {
      const view = GW.views.get(id);
      if (view && view.update) view.update(state.accounts.slice());
    }
    GW.emit("gw:accounts", { accounts: state.accounts.slice() });
    return state.accounts;
  }

  function gmailAccounts() {
    return state.accounts.filter((a) => a.gmail);
  }

  /** Sign in (no options), add Gmail ({gmail: true}) or full Drive ({manage_drive: true}). */
  async function connect(options, label) {
    if (state.connecting) return;
    state.connecting = label || "Signing in";
    render();
    try {
      await GW.rpc("google_connect", options || {}, { timeout: CONNECT_TIMEOUT_MS });
    } catch (err) {
      state.connecting = "";
      await GW.call("ui.alert", {
        title: "Google sign-in didn't finish",
        message: (err && err.message) || "Try again.",
      }).catch(() => {});
    }
    state.connecting = "";
    await reloadAccounts();
  }

  function setView(id, opts) {
    if (!VIEWS.includes(id)) return;
    const changed = state.view !== id;
    state.view = id;
    if (changed && !(opts && opts.silent)) GW.call("prefs.set", { id: "view", value: id }).catch(() => {});
    render();
  }

  // ── rendering ─────────────────────────────────────────────────────────

  function gate() {
    if (!state.loaded) {
      return '<div class="dk-loading">Loading your Google accounts…</div>';
    }
    if (state.connecting) {
      return `<div class="dk-empty gw-gate__card">
        <div class="dk-empty__title">${GW.esc(state.connecting)}…</div>
        <p>Finish the Google sign-in in your browser. This tab updates on its own.</p>
      </div>`;
    }
    if (state.error && !state.accounts.length) {
      return `<div class="dk-error gw-gate__card">
        <div class="dk-error__title">Couldn't load your Google accounts</div>
        <p>${GW.esc(state.error)}</p>
        <button class="dk-btn" type="button" data-act="retry">Try again</button>
      </div>`;
    }
    if (!state.accounts.length) {
      return `<div class="dk-empty gw-gate__card">
        <div class="dk-empty__title">Sign in with Google</div>
        <p>Gmail and Drive in one tab. Sign-in opens in your browser: pick an account and approve.
        It starts with read-only Drive; Gmail is added only when you turn it on.</p>
        <button class="dk-btn dk-btn--primary" type="button" data-act="signin">Sign in with Google</button>
      </div>`;
    }
    if (state.view === "gmail" && !gmailAccounts().length) {
      const only = state.accounts.length === 1 ? state.accounts[0].email : "";
      return `<div class="dk-empty gw-gate__card">
        <div class="dk-empty__title">Turn on Gmail</div>
        <p>${only ? `${GW.esc(only)} is signed in with Drive only. ` : ""}Turn on Gmail to read, search,
        draft and organize mail here. Google asks you to approve it; sending and trash always ask you first.</p>
        <button class="dk-btn dk-btn--primary" type="button" data-act="gmail" data-account="${GW.esc(only)}">Turn on Gmail</button>
      </div>`;
    }
    if (state.view === "drive" && !GW.views.has("drive")) {
      return `<div class="dk-empty gw-gate__card">
        <div class="dk-empty__title">Drive</div>
        <p>Your Drive files show here. Meanwhile, ask Ducky to list or search your Drive.</p>
      </div>`;
    }
    return "";
  }

  function renderTop() {
    root.document.querySelectorAll(".gw-pill").forEach((pill) => {
      const on = pill.dataset.view === state.view;
      pill.classList.toggle("is-on", on);
      pill.setAttribute("aria-selected", on ? "true" : "false");
      pill.tabIndex = on ? 0 : -1;
    });
    const side = $("#gw-account-bar");
    if (!side) return;
    if (!state.accounts.length) {
      side.innerHTML = "";
      return;
    }
    const dots = state.accounts.map((a) =>
      `<span class="gw-dot" style="background:${GW.accountColor(a.email)}" title="${GW.esc(a.email)}"></span>`).join("");
    side.innerHTML = `<span class="gw-account-bar__dots" aria-hidden="true">${dots}</span>
      <span class="dk-muted">${state.accounts.length === 1 ? GW.esc(state.accounts[0].email) : `${state.accounts.length} accounts`}</span>
      <button class="dk-btn gw-btn--small" type="button" data-act="signin" ${state.connecting ? "disabled" : ""}>Add account</button>`;
  }

  function render() {
    renderTop();
    const gateEl = $("#gw-gate");
    const html = gate();
    gateEl.hidden = !html;
    if (html) gateEl.innerHTML = html;
    for (const id of VIEWS) {
      const el = root.document.querySelector(`.gw-view[data-view="${id}"]`);
      const view = GW.views.get(id);
      const show = !html && id === state.view && Boolean(view);
      if (show && !state.mounted.has(id)) {
        state.mounted.add(id);
        try {
          view.mount(el, shell);
          if (view.update) view.update(state.accounts.slice());
        } catch (err) {
          console.error(`Google tab: ${LABELS[id]} view failed to start:`, err && err.message);
        }
      }
      const was = !el.hidden;
      el.hidden = !show;
      if (view && state.mounted.has(id) && was !== show) {
        if (show && view.show) view.show();
        if (!show && view.hide) view.hide();
      }
    }
  }

  function onClick(ev) {
    const pill = ev.target.closest(".gw-pill");
    if (pill) return setView(pill.dataset.view);
    const btn = ev.target.closest("[data-act]");
    if (!btn || btn.disabled) return;
    const act = btn.dataset.act;
    if (act === "retry") {
      state.loaded = false;
      render();
      reloadAccounts();
    } else if (act === "signin") {
      connect({}, "Signing in with Google");
    } else if (act === "gmail") {
      const account = btn.dataset.account || "";
      connect(Object.assign({ gmail: true }, account ? { account } : {}), "Turning on Gmail");
    }
  }

  function onPillKey(ev) {
    if (!ev.target.classList.contains("gw-pill")) return;
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    const i = VIEWS.indexOf(state.view);
    const next = VIEWS[(i + (ev.key === "ArrowRight" ? 1 : VIEWS.length - 1)) % VIEWS.length];
    setView(next);
    const el = root.document.querySelector(`.gw-pill[data-view="${next}"]`);
    if (el) el.focus();
    ev.preventDefault();
  }

  async function start() {
    root.document.addEventListener("click", onClick);
    root.document.addEventListener("keydown", onPillKey);
    GW.subscribe(ACCOUNT_EVENTS);
    ACCOUNT_EVENTS.forEach((t) => GW.on(t, () => reloadAccounts()));
    GW.on("gw:view-registered", () => { if (state.loaded) render(); });
    GW.on("gw:hidden", () => {
      const view = GW.views.get(state.view);
      if (view && state.mounted.has(state.view) && view.hide) view.hide();
    });
    GW.on("gw:shown", () => {
      const view = GW.views.get(state.view);
      if (view && state.mounted.has(state.view) && view.show) view.show();
    });
    const saved = await GW.call("prefs.get", { id: "view" }).catch(() => null);
    const pick = saved && typeof saved === "object" ? saved.value : saved;
    if (VIEWS.includes(pick)) state.view = pick;
    render();
    await reloadAccounts();
    GW.emit("gw:ready", {});
  }

  const shell = { state, start, setView, connect, reloadAccounts, gmailAccounts, normalizeAccounts, render };
  GW.shell = shell;
  if (typeof module !== "undefined" && module.exports) module.exports = shell;
})(typeof window !== "undefined" ? window : globalThis);
