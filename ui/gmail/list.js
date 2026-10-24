/* Gmail view: labels sidebar, Gmail search, a virtualized message list and bulk actions.
 *
 * Every button runs the same panel RPC as the matching MCP tool (gmail_list, gmail_search,
 * gmail_labels_list, gmail_unread_count, gmail_mark_read, gmail_archive, gmail_trash…).
 * Opening a message emits "gmail:open" {id, threadId, account} for the reading pane.
 * Snippets, subjects and senders are mail data: always escaped, never run.
 */
(function (root) {
  "use strict";

  const GW = root.GW;
  const ROW_H = 56;
  const OVERSCAN = 8;
  const PAGE = 50;
  const MODIFY_CHUNK = 50;
  const POLL_MS = 60000;
  const COUNT_LABELS_MAX = 20;
  const COUNT_POOL = 4;
  const SYSTEM_LABELS = [
    { id: "INBOX", name: "Inbox", count: true },
    { id: "STARRED", name: "Starred" },
    { id: "SENT", name: "Sent" },
    { id: "DRAFT", name: "Drafts" },
  ];
  const SYNC_EVENTS = ["gmail_synced", "gmail_changed", "google_mail_synced", "google_sync"];
  const BULK = [
    { act: "read", label: "Mark read" },
    { act: "unread", label: "Mark unread" },
    { act: "archive", label: "Archive" },
    { act: "star", label: "Star" },
    { act: "trash", label: "Trash" },
  ];

  const st = {
    accounts: [],
    account: "",
    label: "INBOX",
    query: "",
    rows: [],
    tokens: {},
    loading: false,
    error: "",
    warning: "",
    selected: new Set(),
    anchor: "",
    active: "",
    labels: [],
    counts: {},
    gen: 0,
    labelGen: 0,
    loadedAt: 0,
  };

  let el = null;
  let stopPoll = null;
  let rafPending = false;

  const keyOf = (r) => `${r.account}\u0001${r.id}`;
  const q = (sel) => el.querySelector(sel);

  function sender(r) {
    const raw = String(r.from || "");
    const name = raw.replace(/\s*<[^>]*>\s*$/, "").replace(/^"(.*)"$/, "$1").trim();
    return name || r.from_email || raw || "(unknown sender)";
  }

  function scope() {
    return st.account ? [st.account] : st.accounts.map((a) => a.email);
  }

  function labelName(id) {
    const sys = SYSTEM_LABELS.find((l) => l.id === id);
    return sys ? sys.name : id;
  }

  // ── data ──────────────────────────────────────────────────────────────

  function fetchPage(account, token) {
    const base = { max_results: PAGE, page_token: token || "", account };
    return st.query
      ? GW.rpc("gmail_search", Object.assign({ query: st.query }, base))
      : GW.rpc("gmail_list", Object.assign({ label: st.label }, base));
  }

  function merge(existing, incoming) {
    const seen = new Map(existing.map((r) => [keyOf(r), r]));
    for (const r of incoming) seen.set(keyOf(r), r);
    return Array.from(seen.values()).sort((a, b) => Number(b.internalDate || 0) - Number(a.internalDate || 0));
  }

  /** First page (fresh = true) or the next page of every account that has one. */
  async function load(fresh) {
    if (!st.accounts.length) return;
    const gen = ++st.gen;
    const accts = fresh ? scope() : scope().filter((a) => st.tokens[a]);
    if (!accts.length) return;
    st.loading = true;
    renderStatus();
    const results = await Promise.all(accts.map((a) =>
      fetchPage(a, fresh ? "" : st.tokens[a]).then((out) => ({ a, out }), (err) => ({ a, err }))));
    if (gen !== st.gen) return;
    const incoming = [];
    const errors = [];
    const tokens = fresh ? {} : Object.assign({}, st.tokens);
    for (const { a, out, err } of results) {
      if (err) { errors.push(`${a}: ${err.message}`); delete tokens[a]; continue; }
      for (const m of (out && out.messages) || []) incoming.push(Object.assign({}, m, { account: m.account || a }));
      if (out && out.next_page_token) tokens[a] = out.next_page_token;
      else delete tokens[a];
    }
    st.tokens = tokens;
    st.rows = merge(fresh ? [] : st.rows, incoming);
    const keys = new Set(st.rows.map(keyOf));
    st.selected = new Set(Array.from(st.selected).filter((k) => keys.has(k)));
    st.error = errors.length && !st.rows.length ? errors[0] : "";
    st.warning = errors.length && st.rows.length ? `Some mail didn't load (${errors.length} account${errors.length > 1 ? "s" : ""}).` : "";
    st.loading = false;
    st.loadedAt = Date.now();
    renderList();
  }

  async function pool(items, size, fn) {
    const queue = items.slice();
    const workers = Array.from({ length: Math.min(size, queue.length) }, async () => {
      while (queue.length) await fn(queue.shift());
    });
    await Promise.all(workers);
  }

  /** User labels (merged by name across the accounts in view) and unread counts. */
  async function loadLabels() {
    const gen = ++st.labelGen;
    const accts = scope();
    const byName = new Map();
    await Promise.all(accts.map((a) => GW.rpc("gmail_labels_list", { account: a }).then((out) => {
      for (const lab of (out && out.labels) || []) {
        if (String(lab.type || "").toLowerCase() !== "user" || !lab.name) continue;
        byName.set(String(lab.name), true);
      }
    }, () => {})));
    if (gen !== st.labelGen) return;
    st.labels = Array.from(byName.keys()).sort((x, y) => x.localeCompare(y));
    renderSide();
    const counted = ["INBOX"].concat(st.labels.slice(0, COUNT_LABELS_MAX));
    const counts = {};
    const jobs = [];
    counted.forEach((label) => accts.forEach((account) => jobs.push({ label, account })));
    await pool(jobs, COUNT_POOL, ({ label, account }) =>
      GW.rpc("gmail_unread_count", { label, account }).then((out) => {
        counts[label] = (counts[label] || 0) + Number((out && out.unread) || 0);
      }, () => {}));
    if (gen !== st.labelGen) return;
    st.counts = counts;
    renderSide();
  }

  function refresh() {
    load(true);
    loadLabels();
  }

  // ── bulk actions ──────────────────────────────────────────────────────

  function selectedRows() {
    return st.rows.filter((r) => st.selected.has(keyOf(r)));
  }

  function byAccount(rows) {
    const groups = new Map();
    for (const r of rows) {
      if (!groups.has(r.account)) groups.set(r.account, []);
      groups.get(r.account).push(r.id);
    }
    return groups;
  }

  async function runBulk(act) {
    const rows = selectedRows();
    if (!rows.length) return;
    let extra = {};
    if (act === "trash") {
      const n = rows.length;
      const answer = await GW.call("ui.confirm", {
        title: "Move to Trash?",
        message: `${n} message${n > 1 ? "s" : ""} go to Trash. Gmail keeps them 30 days, and Revert in Changes brings them back.`,
        confirm: "Move to Trash",
        danger: true,
      }).catch(() => ({ confirmed: false }));
      if (!answer || !answer.confirmed) return;
      extra = { confirmed: true };
    }
    const tool = { read: "gmail_mark_read", unread: "gmail_mark_unread", archive: "gmail_archive", star: "gmail_star", trash: "gmail_trash" }[act];
    if (act === "star") extra = { starred: true };
    const failed = [];
    for (const [account, ids] of byAccount(rows)) {
      for (let i = 0; i < ids.length; i += MODIFY_CHUNK) {
        const chunk = ids.slice(i, i + MODIFY_CHUNK);
        try {
          await GW.rpc(tool, Object.assign({ ids: chunk, account }, extra));
          applyLocal(act, account, chunk);
        } catch (err) {
          failed.push(err.message);
        }
      }
    }
    st.selected.clear();
    renderList();
    loadLabels();
    if (failed.length) {
      GW.call("ui.alert", { title: "Some mail didn't change", message: failed[0] }).catch(() => {});
    }
  }

  function applyLocal(act, account, ids) {
    const set = new Set(ids);
    const hit = (r) => r.account === account && set.has(r.id);
    const leaves = (act === "archive" && st.label === "INBOX" && !st.query) || act === "trash";
    if (leaves) {
      st.rows = st.rows.filter((r) => !hit(r));
      return;
    }
    for (const r of st.rows) {
      if (!hit(r)) continue;
      if (act === "read") r.unread = false;
      if (act === "unread") r.unread = true;
      if (act === "star") r.starred = true;
    }
  }

  // ── rendering ─────────────────────────────────────────────────────────

  function renderSide() {
    const side = q(".gw-side");
    const accts = st.accounts.length > 1 ? `<div class="gw-side__group" role="group" aria-label="Accounts">
      ${st.accounts.map((a) => `<button type="button" class="gw-side__item ${st.account === a.email ? "is-on" : ""}" data-account="${GW.esc(a.email)}">
        <span class="gw-dot" style="background:${GW.accountColor(a.email)}"></span>
        <span class="gw-side__name">${GW.esc(a.email)}</span>
      </button>`).join("")}
    </div>` : "";
    const item = (id, name) => {
      const n = st.counts[id] || 0;
      const on = !st.query && st.label === id;
      return `<button type="button" class="gw-side__item ${on ? "is-on" : ""}" data-label="${GW.esc(id)}" ${on ? 'aria-current="true"' : ""}>
        <span class="gw-side__name">${GW.esc(name)}</span>${n ? `<span class="gw-side__count">${n > 999 ? "999+" : n}</span>` : ""}
      </button>`;
    };
    const system = SYSTEM_LABELS.map((l) => item(l.id, l.name)).join("");
    const user = st.labels.length ? `<div class="gw-side__heading">Labels</div>${st.labels.map((n) => item(n, n)).join("")}` : "";
    side.innerHTML = `${accts}<nav class="gw-side__group" aria-label="Labels">${system}${user}</nav>`;
  }

  function renderStatus() {
    const status = q(".gw-mail__status");
    const head = q(".gw-mail__title");
    head.textContent = st.query ? `Search: ${st.query}` : labelName(st.label);
    if (st.error) {
      status.innerHTML = `<div class="dk-error"><div class="dk-error__title">Couldn't load mail</div>${GW.esc(st.error)}
        <div><button class="dk-btn gw-btn--small" type="button" data-mail="retry">Try again</button></div></div>`;
    } else if (st.loading && !st.rows.length) {
      status.innerHTML = '<div class="dk-loading">Loading mail…</div>';
    } else if (!st.rows.length) {
      status.innerHTML = `<div class="dk-empty"><div class="dk-empty__title">${st.query ? "No mail matches" : "Nothing here"}</div>
        ${st.query ? "Try other words, or Gmail search like from:sam newer_than:7d." : "New mail shows up here on its own."}</div>`;
    } else {
      status.innerHTML = st.warning ? `<div class="dk-muted gw-mail__warn">${GW.esc(st.warning)}</div>` : "";
    }
    const more = Object.keys(st.tokens).length;
    q(".gw-mail__more").textContent = st.loading && st.rows.length ? "Loading more…" : (more ? "" : (st.rows.length ? "That's all of it." : ""));
  }

  function renderBulk() {
    const bar = q(".gw-bulk");
    const n = st.selected.size;
    bar.hidden = !n;
    q(".gw-bulk__count").textContent = `${n} selected`;
    const all = q(".gw-bulk__all");
    all.checked = n > 0 && n === st.rows.length;
    all.indeterminate = n > 0 && n < st.rows.length;
  }

  function rowHtml(r, combined) {
    const key = keyOf(r);
    const sel = st.selected.has(key);
    const cls = ["gw-row", r.unread ? "is-unread" : "", sel ? "is-sel" : "", st.active === key ? "is-active" : ""].join(" ");
    const dot = combined
      ? `<span class="gw-dot" style="background:${GW.accountColor(r.account)}" title="${GW.esc(r.account)}"></span>`
      : '<span class="gw-row__nodot"></span>';
    return `<div class="${cls}" role="option" aria-selected="${sel}" data-key="${GW.esc(key)}">
      <input class="gw-row__check" type="checkbox" tabindex="-1" aria-label="Select" ${sel ? "checked" : ""}>
      ${dot}
      <span class="gw-row__from">${GW.esc(sender(r))}</span>
      <span class="gw-row__text"><span class="gw-row__subject">${r.starred ? '<span class="gw-row__star" aria-label="Starred">★</span>' : ""}${GW.esc(r.subject || "(no subject)")}</span>
        <span class="gw-row__snippet">${GW.esc(r.snippet || "")}</span></span>
      <span class="gw-row__date">${GW.esc(GW.when(r.internalDate || r.date))}</span>
    </div>`;
  }

  function renderRows() {
    rafPending = false;
    const list = q(".gw-list");
    const spacer = q(".gw-list__spacer");
    const rowsEl = q(".gw-list__rows");
    spacer.style.height = `${st.rows.length * ROW_H}px`;
    const start = Math.max(0, Math.floor(list.scrollTop / ROW_H) - OVERSCAN);
    const end = Math.min(st.rows.length, start + Math.ceil((list.clientHeight || 600) / ROW_H) + OVERSCAN * 2);
    const combined = !st.account && st.accounts.length > 1;
    rowsEl.style.transform = `translateY(${start * ROW_H}px)`;
    rowsEl.innerHTML = st.rows.slice(start, end).map((r) => rowHtml(r, combined)).join("");
    if (Object.keys(st.tokens).length && !st.loading && list.scrollTop + list.clientHeight > st.rows.length * ROW_H - ROW_H * 10) {
      load(false);
    }
  }

  function scheduleRows() {
    if (rafPending) return;
    rafPending = true;
    root.requestAnimationFrame(renderRows);
  }

  function renderList() {
    renderStatus();
    renderBulk();
    renderRows();
  }

  // ── interaction ───────────────────────────────────────────────────────

  function rowByKey(key) {
    return st.rows.find((r) => keyOf(r) === key);
  }

  function toggle(key, shift) {
    if (shift && st.anchor) {
      const keys = st.rows.map(keyOf);
      const a = keys.indexOf(st.anchor);
      const b = keys.indexOf(key);
      if (a >= 0 && b >= 0) {
        keys.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((k) => st.selected.add(k));
        renderBulk();
        scheduleRows();
        return;
      }
    }
    if (st.selected.has(key)) st.selected.delete(key);
    else st.selected.add(key);
    st.anchor = key;
    renderBulk();
    scheduleRows();
  }

  function openRow(key) {
    const r = rowByKey(key);
    if (!r) return;
    st.active = key;
    scheduleRows();
    GW.emit("gmail:open", { id: r.id, threadId: r.threadId, account: r.account, message: r });
  }

  function scrollTo(key) {
    const i = st.rows.findIndex((r) => keyOf(r) === key);
    if (i < 0) return;
    const list = q(".gw-list");
    const top = i * ROW_H;
    if (top < list.scrollTop) list.scrollTop = top;
    else if (top + ROW_H > list.scrollTop + list.clientHeight) list.scrollTop = top + ROW_H - list.clientHeight;
    scheduleRows();
  }

  function moveActive(step) {
    if (!st.rows.length) return;
    const i = st.rows.findIndex((r) => keyOf(r) === st.active);
    const next = st.rows[Math.max(0, Math.min(st.rows.length - 1, (i < 0 ? -1 : i) + step))];
    st.active = keyOf(next);
    scrollTo(st.active);
  }

  function pickLabel(label) {
    st.label = label;
    st.query = "";
    q(".gw-search input").value = "";
    resetList();
    load(true);
    renderSide();
  }

  function pickAccount(account) {
    if (st.account === account) return;
    st.account = account;
    resetList();
    renderSide();
    refresh();
  }

  function resetList() {
    st.rows = [];
    st.tokens = {};
    st.selected.clear();
    st.error = "";
    q(".gw-list").scrollTop = 0;
    renderList();
  }

  function onClick(ev) {
    const t = ev.target;
    const side = t.closest(".gw-side__item");
    if (side) {
      if (side.dataset.label !== undefined) return pickLabel(side.dataset.label);
      if (side.dataset.account !== undefined) return pickAccount(side.dataset.account);
    }
    const btn = t.closest("[data-mail]");
    if (btn) {
      const act = btn.dataset.mail;
      if (act === "retry" || act === "refresh") return refresh();
      if (act === "clear") { st.selected.clear(); renderBulk(); return scheduleRows(); }
      return runBulk(act);
    }
    if (t.classList.contains("gw-bulk__all")) {
      if (t.checked) st.rows.forEach((r) => st.selected.add(keyOf(r)));
      else st.selected.clear();
      renderBulk();
      return scheduleRows();
    }
    const row = t.closest(".gw-row");
    if (!row) return;
    if (t.classList.contains("gw-row__check") || ev.ctrlKey || ev.metaKey || ev.shiftKey) {
      if (!t.classList.contains("gw-row__check")) ev.preventDefault();
      return toggle(row.dataset.key, ev.shiftKey);
    }
    openRow(row.dataset.key);
  }

  function onKey(ev) {
    if (ev.target.closest(".gw-search")) return;
    if (ev.key === "ArrowDown" || ev.key === "j") { moveActive(1); ev.preventDefault(); }
    else if (ev.key === "ArrowUp" || ev.key === "k") { moveActive(-1); ev.preventDefault(); }
    else if (ev.key === "Enter" && st.active) { openRow(st.active); ev.preventDefault(); }
    else if ((ev.key === " " || ev.key === "x") && st.active) { toggle(st.active, ev.shiftKey); ev.preventDefault(); }
  }

  function onSearch(ev) {
    ev.preventDefault();
    const value = q(".gw-search input").value.trim();
    st.query = value;
    resetList();
    renderSide();
    load(true);
  }

  // ── view contract ─────────────────────────────────────────────────────

  function mount(container) {
    el = container;
    el.classList.add("gw-gmail");
    el.innerHTML = `
      <aside class="gw-side" aria-label="Mailboxes"></aside>
      <div class="gw-mail">
        <div class="gw-mail__bar">
          <h2 class="gw-mail__title">Inbox</h2>
          <form class="gw-search" role="search">
            <input class="dk-input" type="search" placeholder="Search mail (from:sam is:unread newer_than:7d)" aria-label="Search mail">
          </form>
          <button class="dk-btn gw-btn--small" type="button" data-mail="refresh" title="Check for new mail">Refresh</button>
        </div>
        <div class="gw-bulk" hidden>
          <input class="gw-bulk__all" type="checkbox" aria-label="Select all">
          <span class="gw-bulk__count"></span>
          ${BULK.map((b) => `<button class="dk-btn gw-btn--small" type="button" data-mail="${b.act}">${b.label}</button>`).join("")}
          <button class="dk-btn gw-btn--small" type="button" data-mail="clear">Clear</button>
        </div>
        <div class="gw-mail__status"></div>
        <div class="gw-list" role="listbox" aria-multiselectable="true" aria-label="Messages" tabindex="0">
          <div class="gw-list__spacer"><div class="gw-list__rows"></div></div>
          <div class="gw-mail__more dk-muted"></div>
        </div>
      </div>`;
    el.addEventListener("click", onClick);
    q(".gw-list").addEventListener("keydown", onKey);
    q(".gw-list").addEventListener("scroll", scheduleRows, { passive: true });
    q(".gw-search").addEventListener("submit", onSearch);
    q(".gw-search input").addEventListener("search", (ev) => { if (!ev.target.value && st.query) onSearch(ev); });
    GW.subscribe(SYNC_EVENTS);
    SYNC_EVENTS.forEach((t) => GW.on(t, () => { if (GW.visible() && el && !el.hidden) refresh(); }));
    renderSide();
    renderList();
  }

  function update(accounts) {
    const next = (accounts || []).filter((a) => a.gmail);
    const before = st.accounts.map((a) => a.email).join(",");
    st.accounts = next;
    if (st.account && !next.some((a) => a.email === st.account)) st.account = "";
    if (!st.account && next.length) st.account = next[0].email;
    if (!el) return;
    renderSide();
    if (before !== next.map((a) => a.email).join(",") || !st.loadedAt) {
      resetList();
      refresh();
    }
  }

  function show() {
    if (!stopPoll) stopPoll = GW.every(POLL_MS, refresh);
    if (st.loadedAt && Date.now() - st.loadedAt > POLL_MS / 2) refresh();
  }

  function hide() {
    if (stopPoll) { stopPoll(); stopPoll = null; }
  }

  /** An open request: {id, query, account} from google_open or an [Open] link. */
  function open(req) {
    const r = req || {};
    const account = String(r.account || "");
    if (account && st.accounts.some((a) => a.email.toLowerCase() === account.toLowerCase())) {
      const match = st.accounts.find((a) => a.email.toLowerCase() === account.toLowerCase()).email;
      if (match !== st.account) pickAccount(match);
    }
    if (r.query) {
      q(".gw-search input").value = r.query;
      st.query = r.query;
      resetList();
      renderSide();
      load(true);
    }
    const id = String(r.id || r.item_id || "");
    if (id) {
      const row = st.rows.find((x) => x.id === id || x.threadId === id);
      const acct = row ? row.account : (account || st.account);
      st.active = `${acct}\u0001${row ? row.id : id}`;
      if (row) scrollTo(st.active);
      GW.emit("gmail:open", { id: row ? row.id : id, threadId: row ? row.threadId : "", account: acct, message: row || null });
    }
  }

  const view = { mount, update, show, hide, open, refresh, state: st };
  GW.registerView("gmail", view);
  if (typeof module !== "undefined" && module.exports) module.exports = view;
})(typeof window !== "undefined" ? window : globalThis);
