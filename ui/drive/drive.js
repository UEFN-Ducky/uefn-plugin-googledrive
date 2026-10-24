/* Drive view: folder tree, grid / list, search, breadcrumbs, previews, drag-in upload,
 * a right-click menu and a storage bar, per Google account.
 *
 * Every action runs the same panel RPC as the matching MCP tool (gdrive_list, gdrive_search,
 * gdrive_upload, gdrive_folder_create, gdrive_move, gdrive_rename, gdrive_trash, gdrive_share,
 * gdrive_import_to_uefn, gdrive_storage_usage), so Revert in Changes undoes what the tab did.
 * File names come from Drive: always escaped, never run.
 */
(function (root) {
  "use strict";

  const GW = root.GW;
  const PAGE = 100;
  const FOLDER_MIME = "application/vnd.google-apps.folder";
  const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp|tga|tiff?)$/i;
  const SYNC_EVENTS = ["gdrive_changed", "drive_changed", "google_drive_changed"];
  const LAYOUT_PREF = "drive_layout";
  const DRAG_TYPE = "application/x-gw-drive";

  const st = {
    accounts: [],
    account: "",
    crumbs: [],
    rows: [],
    token: "",
    query: "",
    layout: "grid",
    selected: new Set(),
    anchor: "",
    loading: false,
    error: "",
    gen: 0,
    tree: new Map(),
    usage: null,
    busy: "",
  };

  let el = null;
  let menuEl = null;
  let dialogEl = null;
  let lastFocus = null;

  const q = (sel) => el.querySelector(sel);
  const isFolder = (r) => Boolean(r && (r.folder || r.kind === "folder" || r.mime === FOLDER_MIME || r.mimeType === FOLDER_MIME));
  const isImage = (r) => Boolean(r && (r.kind === "texture" || /^image\//.test(r.mime || r.mimeType || "") || IMAGE_EXT.test(r.name || "")));
  const current = () => st.crumbs[st.crumbs.length - 1] || { id: "", name: "My Drive" };
  const acct = () => st.accounts.find((a) => a.email === st.account) || null;
  const canManage = () => Boolean(acct() && acct().manage_drive);
  const rowById = (id) => st.rows.find((r) => r.id === id);

  // ── data ──────────────────────────────────────────────────────────────

  /** One Drive RPC for the picked account. Read tools from 1.0 take no account: retried without it. */
  async function rpc(name, params, opts) {
    const p = Object.assign({}, params || {});
    if (st.account) p.account = st.account;
    try {
      return await GW.rpc(name, p, opts);
    } catch (err) {
      if (p.account && /unexpected keyword|got an unexpected/i.test(String(err && err.message))) {
        delete p.account;
        return GW.rpc(name, p, opts);
      }
      throw err;
    }
  }

  function rowsOf(out) {
    return ((out && (out.files || out.items)) || []).map((r) => Object.assign({}, r, { folder: isFolder(r) }));
  }

  async function load(fresh) {
    if (!st.account) return;
    const gen = ++st.gen;
    st.loading = true;
    st.error = "";
    if (fresh) { st.rows = []; st.token = ""; st.selected.clear(); }
    renderMain();
    try {
      const out = st.query
        ? await rpc("gdrive_search", { query: st.query, page_size: 50, page_token: fresh ? "" : st.token })
        : await rpc("gdrive_list", { folder: current().id, page_size: PAGE, page_token: fresh ? "" : st.token });
      if (gen !== st.gen) return;
      const rows = rowsOf(out);
      st.rows = fresh ? rows : st.rows.concat(rows);
      st.token = String((out && out.next_page_token) || "");
      if (fresh && !st.query && st.crumbs.length === 1 && out && out.folder_id && !st.crumbs[0].id) {
        st.crumbs[0].id = String(out.folder_id);
      }
      if (!st.query) rememberFolders(current().id, rows.filter(isFolder));
    } catch (err) {
      if (gen !== st.gen) return;
      st.error = (err && err.message) || "Couldn't load Drive.";
    }
    st.loading = false;
    renderMain();
    renderTree();
  }

  async function loadUsage() {
    if (!st.account) return;
    const account = st.account;
    try {
      const out = await rpc("gdrive_storage_usage", {});
      if (account !== st.account) return;
      const row = ((out && out.accounts) || []).find((a) => a.account === account) || ((out && out.accounts) || [])[0];
      st.usage = row || null;
    } catch (_err) {
      st.usage = null;
    }
    renderUsage();
  }

  // ── folder tree ───────────────────────────────────────────────────────

  function node(id, name, parent) {
    const key = id || "";
    if (!st.tree.has(key)) st.tree.set(key, { id: key, name, parent, children: null, open: false, loading: false });
    const n = st.tree.get(key);
    if (name) n.name = name;
    if (parent !== undefined) n.parent = parent;
    return n;
  }

  function rememberFolders(parentId, folders) {
    const p = node(parentId, "", undefined);
    p.children = folders.map((f) => node(f.id, f.name, parentId).id);
  }

  async function expand(id) {
    const n = node(id, "", undefined);
    n.open = !n.open;
    if (n.open && !n.children && !n.loading) {
      n.loading = true;
      renderTree();
      try {
        const out = await rpc("gdrive_list", { folder: id, page_size: PAGE });
        rememberFolders(id, rowsOf(out).filter(isFolder));
      } catch (_err) {
        n.children = [];
      }
      n.loading = false;
    }
    renderTree();
  }

  function pathTo(id) {
    const out = [];
    let cur = st.tree.get(id || "");
    const rootId = st.crumbs[0] ? st.crumbs[0].id : "";
    for (let guard = 0; cur && guard < 64; guard += 1) {
      out.unshift({ id: cur.id, name: cur.name || "Folder" });
      if (cur.id === rootId || cur.parent === undefined || cur.parent === null) break;
      cur = st.tree.get(cur.parent);
    }
    if (!out.length || out[0].id !== rootId) out.unshift(st.crumbs[0] || { id: "", name: "My Drive" });
    return out;
  }

  // ── navigation ────────────────────────────────────────────────────────

  function resetRoot() {
    st.crumbs = [{ id: "", name: "My Drive" }];
    st.tree = new Map();
    node("", "My Drive", null).open = true;
  }

  function openFolder(id, name) {
    const known = st.tree.get(id);
    if (known || id === (st.crumbs[0] && st.crumbs[0].id)) {
      st.crumbs = pathTo(id);
    } else {
      node(id, name, current().id);
      st.crumbs = st.crumbs.concat([{ id, name: name || "Folder" }]);
    }
    st.query = "";
    const search = q(".gwd-search input");
    if (search) search.value = "";
    load(true);
  }

  function goCrumb(i) {
    st.crumbs = st.crumbs.slice(0, i + 1);
    st.query = "";
    q(".gwd-search input").value = "";
    load(true);
  }

  function pickAccount(email) {
    if (!email || email === st.account) return;
    st.account = email;
    GW.call("prefs.set", { id: "drive_account", value: email }).catch(() => {});
    resetRoot();
    renderSide();
    load(true);
    loadUsage();
  }

  // ── actions (each one is the AI's tool) ───────────────────────────────

  async function needManage() {
    if (canManage()) return true;
    const answer = await GW.call("ui.confirm", {
      title: "Turn on Manage Drive?",
      message: `${st.account} can only read Drive. Manage Drive lets this tab (and Ducky) upload, make folders, move, rename, share and trash files. Google asks you to approve it.`,
      confirm: "Turn on Manage Drive",
    }).catch(() => ({ confirmed: false }));
    if (answer && answer.confirmed && GW.shell) {
      await GW.shell.connect({ manage_drive: true, account: st.account }, "Turning on Manage Drive");
    }
    return canManage();
  }

  async function run(label, fn) {
    st.busy = label;
    renderStatus();
    try {
      const out = await fn();
      return out;
    } catch (err) {
      GW.call("ui.alert", { title: `${label} didn't work`, message: (err && err.message) || "Try again." }).catch(() => {});
      return null;
    } finally {
      st.busy = "";
      renderStatus();
    }
  }

  async function upload(paths) {
    const list = (paths || []).filter((f) => f && f.path);
    if (!list.length || !(await needManage())) return;
    const folder = st.query ? "" : current().id;
    let done = 0;
    for (const f of list) {
      const out = await run(`Uploading ${f.name || "file"} (${done + 1}/${list.length})`,
        () => rpc("gdrive_upload", { local_path: f.path, folder }, { timeout: 30 * 60 * 1000 }));
      if (out) done += 1;
    }
    if (done) load(true);
  }

  async function pickAndUpload() {
    const out = await GW.call("files.pick", { accept: "any", multiple: true }).catch(() => null);
    await upload((out && out.files) || []);
  }

  async function onDropFiles(ev) {
    const names = Array.from((ev.dataTransfer && ev.dataTransfer.files) || []).map((f) => f.name);
    if (!names.length) return;
    const out = await GW.call("files.dropped", { names }).catch(() => null);
    await upload((out && out.files) || []);
  }

  async function newFolder() {
    if (!(await needManage())) return;
    const v = await ask({ title: "New folder", fields: [{ id: "name", label: "Folder name", value: "New folder" }], ok: "Create" });
    if (!v) return;
    const out = await run("Making the folder", () => rpc("gdrive_folder_create", { name: v.name, parent: st.query ? "" : current().id }));
    if (out) { node(current().id, "", undefined).children = null; load(true); }
  }

  async function rename(r) {
    if (!r || !(await needManage())) return;
    const v = await ask({ title: "Rename", fields: [{ id: "name", label: "New name", value: r.name }], ok: "Rename" });
    if (!v || v.name === r.name) return;
    const out = await run("Renaming", () => rpc("gdrive_rename", { file: r.id, name: v.name }));
    if (out) {
      r.name = (out.file && out.file.name) || v.name;
      const t = st.tree.get(r.id);
      if (t) t.name = r.name;
      renderMain();
      renderTree();
    }
  }

  async function move(ids, dest) {
    const list = (ids || []).filter((id) => id && id !== dest);
    if (!list.length || !dest || !(await needManage())) return;
    const out = await run("Moving", () => rpc("gdrive_move", { file: list, folder: dest }));
    if (out) {
      st.tree.forEach((n) => { if (n.id === dest || n.id === current().id) n.children = null; });
      load(true);
    }
  }

  async function moveDialog(ids) {
    if (!ids.length || !(await needManage())) return;
    const folders = [];
    const walk = (id, depth) => {
      const n = st.tree.get(id);
      if (!n) return;
      folders.push({ value: n.id || "root", label: `${"\u2003".repeat(depth)}${n.name || "My Drive"}` });
      (n.children || []).forEach((c) => { if (!ids.includes(c)) walk(c, depth + 1); });
    };
    walk(st.crumbs[0] ? st.crumbs[0].id : "", 0);
    const v = await ask({
      title: `Move ${ids.length === 1 ? (rowById(ids[0]) || {}).name || "1 item" : `${ids.length} items`}`,
      fields: [{ id: "folder", label: "Move to", type: "select", options: folders, value: folders[0] && folders[0].value }],
      note: "Open a folder in the tree first to see what's inside it. You can also drag files onto a folder.",
      ok: "Move",
    });
    if (v && v.folder) await move(ids, v.folder);
  }

  async function trash(ids) {
    const rows = ids.map(rowById).filter(Boolean);
    if (!rows.length || !(await needManage())) return;
    const names = rows.slice(0, 5).map((r) => r.name).join(", ") + (rows.length > 5 ? ` and ${rows.length - 5} more` : "");
    const answer = await GW.call("ui.confirm", {
      title: "Move to Trash?",
      message: `${names} go${rows.length === 1 ? "es" : ""} to the Drive trash. Google keeps them 30 days, and Revert in Changes brings them back.`,
      confirm: "Move to Trash",
      danger: true,
    }).catch(() => ({ confirmed: false }));
    if (!answer || !answer.confirmed) return;
    const out = await run("Moving to Trash", () => rpc("gdrive_trash", { files: rows.map((r) => r.id), confirmed: true }));
    if (out) {
      const gone = new Set(rows.map((r) => r.id));
      st.rows = st.rows.filter((r) => !gone.has(r.id));
      st.selected.clear();
      st.tree.forEach((n) => { if (n.children) n.children = n.children.filter((c) => !gone.has(c)); });
      renderMain();
      renderTree();
      loadUsage();
    }
  }

  async function share(r) {
    if (!r || !(await needManage())) return;
    const v = await ask({
      title: `Share “${r.name}”`,
      fields: [
        { id: "email", label: "Email", type: "email", value: "" },
        { id: "role", label: "Access", type: "select", value: "reader", options: [
          { value: "reader", label: "Can view" },
          { value: "commenter", label: "Can comment" },
          { value: "writer", label: "Can edit" },
        ] },
        { id: "notify", label: "Email them a link", type: "checkbox", value: true },
        { id: "message", label: "Note (optional)", type: "textarea", value: "" },
      ],
      note: "Revert can't take sharing back: remove access in Drive if you change your mind.",
      ok: "Share",
    });
    if (!v || !v.email) return;
    const out = await run("Sharing", () => rpc("gdrive_share", { file: r.id, email: v.email, role: v.role, notify: v.notify, message: v.message }));
    if (out && out.message) GW.call("ui.alert", { title: "Shared", message: out.message }).catch(() => {});
  }

  async function importToUefn(ids) {
    const rows = ids.map(rowById).filter((r) => r && !isFolder(r));
    if (!rows.length) return;
    const saved = await GW.call("prefs.get", { id: "drive_import_dest" }).catch(() => null);
    const dest = (saved && typeof saved === "object" ? saved.value : saved) || "Imported";
    const v = await ask({
      title: rows.length === 1 ? `Import “${rows[0].name}” to UEFN` : `Import ${rows.length} files to UEFN`,
      fields: [{ id: "dest", label: "Content folder", value: dest }],
      note: "Models, textures and .wav import into this folder of the open island (e.g. Imported/Props). UEFN must be open.",
      ok: "Import",
    });
    if (!v || !v.dest) return;
    GW.call("prefs.set", { id: "drive_import_dest", value: v.dest }).catch(() => {});
    const lines = [];
    for (const r of rows) {
      const out = await run(`Importing ${r.name}`,
        () => rpc("gdrive_import_to_uefn", { file: r.id, destination_path: v.dest }, { timeout: 20 * 60 * 1000 }));
      if (out) lines.push(out.message || `${r.name}: imported`);
    }
    if (lines.length) GW.call("ui.alert", { title: "Imported to UEFN", message: lines.join("\n") }).catch(() => {});
  }

  async function preview(r) {
    if (!r) return;
    let info = r;
    let url = "";
    try { info = Object.assign({}, r, await rpc("gdrive_file_info", { file: r.id })); } catch (_e) { /* show what the list has */ }
    if (isImage(r)) {
      try {
        const out = await rpc("gdrive_preview", { file: r.id });
        url = String((out && (out.url || out.media_url)) || "");
      } catch (_e) { url = ""; }
    }
    const facts = [
      ["Type", info.kind || info.mime || "file"],
      ["Size", info.size || "—"],
      ["Modified", GW.when(info.modified) || "—"],
      ["Imports to UEFN", info.importable_to_uefn === true || info.importable ? "Yes" : "No"],
    ];
    await ask({
      title: r.name,
      html: `${url ? `<img class="gwd-preview__img" src="${GW.esc(url)}" alt="${GW.esc(r.name)}">` : ""}
        <dl class="gwd-preview__facts">${facts.map(([k, val]) => `<dt>${GW.esc(k)}</dt><dd>${GW.esc(val)}</dd>`).join("")}</dl>`,
      ok: "Close",
      cancel: false,
      wide: Boolean(url),
    });
  }

  // ── dialog (inputs only; confirms use Ducky's own ui.confirm) ─────────

  function fieldHtml(f) {
    const id = `gwd-f-${f.id}`;
    if (f.type === "select") {
      return `<label class="gwd-field" for="${id}"><span>${GW.esc(f.label)}</span>
        <select class="dk-input" id="${id}" name="${GW.esc(f.id)}">${(f.options || []).map((o) =>
          `<option value="${GW.esc(o.value)}" ${o.value === f.value ? "selected" : ""}>${GW.esc(o.label)}</option>`).join("")}</select></label>`;
    }
    if (f.type === "checkbox") {
      return `<label class="gwd-field gwd-field--check" for="${id}"><input type="checkbox" id="${id}" name="${GW.esc(f.id)}" ${f.value ? "checked" : ""}>
        <span>${GW.esc(f.label)}</span></label>`;
    }
    if (f.type === "textarea") {
      return `<label class="gwd-field" for="${id}"><span>${GW.esc(f.label)}</span>
        <textarea class="dk-input" id="${id}" name="${GW.esc(f.id)}" rows="3">${GW.esc(f.value)}</textarea></label>`;
    }
    return `<label class="gwd-field" for="${id}"><span>${GW.esc(f.label)}</span>
      <input class="dk-input" id="${id}" name="${GW.esc(f.id)}" type="${f.type === "email" ? "email" : "text"}" value="${GW.esc(f.value)}" ${f.type === "email" ? "required" : ""}></label>`;
  }

  /** A small form card over the tab. Resolves with the field values, or null when cancelled. */
  function ask(spec) {
    closeDialog(null);
    lastFocus = root.document.activeElement;
    return new Promise((resolve) => {
      dialogEl = root.document.createElement("div");
      dialogEl.className = "gwd-dialog";
      dialogEl._resolve = resolve;
      dialogEl.innerHTML = `<form class="gwd-dialog__card ${spec.wide ? "is-wide" : ""}" role="dialog" aria-modal="true" aria-label="${GW.esc(spec.title)}">
        <h3 class="gwd-dialog__title">${GW.esc(spec.title)}</h3>
        ${spec.html || ""}
        ${(spec.fields || []).map(fieldHtml).join("")}
        ${spec.note ? `<p class="dk-muted gwd-dialog__note">${GW.esc(spec.note)}</p>` : ""}
        <div class="gwd-dialog__buttons">
          ${spec.cancel === false ? "" : '<button class="dk-btn" type="button" data-dlg="cancel">Cancel</button>'}
          <button class="dk-btn dk-btn--primary" type="submit">${GW.esc(spec.ok || "OK")}</button>
        </div>
      </form>`;
      el.appendChild(dialogEl);
      const form = dialogEl.querySelector("form");
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        if (!form.reportValidity()) return;
        const values = {};
        (spec.fields || []).forEach((f) => {
          const input = form.elements[f.id];
          values[f.id] = f.type === "checkbox" ? Boolean(input.checked) : String(input.value || "").trim();
        });
        closeDialog(values);
      });
      dialogEl.addEventListener("click", (ev) => {
        if (ev.target === dialogEl || ev.target.closest('[data-dlg="cancel"]')) closeDialog(null);
      });
      dialogEl.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); closeDialog(null); } });
      const first = form.querySelector("input, select, textarea, button[type=submit]");
      if (first) { first.focus(); if (first.select && first.type === "text") first.select(); }
    });
  }

  function closeDialog(value) {
    if (!dialogEl) return;
    const d = dialogEl;
    dialogEl = null;
    d.remove();
    if (d._resolve) d._resolve(value);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  // ── context menu ──────────────────────────────────────────────────────

  function menuItems(ids) {
    const rows = ids.map(rowById).filter(Boolean);
    const one = rows.length === 1 ? rows[0] : null;
    const files = rows.filter((r) => !isFolder(r));
    const items = [];
    if (one) items.push({ act: "open", label: isFolder(one) ? "Open" : "Preview" });
    if (one) items.push({ act: "rename", label: "Rename…", manage: true });
    if (rows.length) items.push({ act: "move", label: "Move…", manage: true });
    items.push({ act: "newfolder", label: "New folder…", manage: true });
    if (one) items.push({ act: "share", label: "Share…", manage: true });
    if (files.length && files.some((r) => r.importable !== false)) items.push({ act: "import", label: "Import to UEFN…" });
    if (rows.length) items.push({ act: "sep" }, { act: "trash", label: "Move to Trash", manage: true, danger: true });
    return items;
  }

  function openMenu(x, y, ids) {
    closeMenu();
    menuEl = root.document.createElement("div");
    menuEl.className = "gwd-menu";
    menuEl.setAttribute("role", "menu");
    menuEl._ids = ids;
    menuEl.innerHTML = menuItems(ids).map((it) => (it.act === "sep"
      ? '<div class="gwd-menu__sep" role="separator"></div>'
      : `<button type="button" role="menuitem" class="gwd-menu__item ${it.danger ? "is-danger" : ""}" data-menu="${it.act}">
          ${GW.esc(it.label)}${it.manage && !canManage() ? '<span class="gwd-menu__hint">needs Manage Drive</span>' : ""}</button>`)).join("");
    el.appendChild(menuEl);
    const box = el.getBoundingClientRect();
    const w = menuEl.offsetWidth || 200;
    const h = menuEl.offsetHeight || 240;
    menuEl.style.left = `${Math.max(4, Math.min(x - box.left, box.width - w - 4))}px`;
    menuEl.style.top = `${Math.max(4, Math.min(y - box.top, box.height - h - 4))}px`;
    const first = menuEl.querySelector(".gwd-menu__item");
    if (first) first.focus();
  }

  function closeMenu() {
    if (menuEl) { menuEl.remove(); menuEl = null; }
  }

  function runMenu(act, ids) {
    closeMenu();
    const one = ids.length === 1 ? rowById(ids[0]) : null;
    if (act === "open") return one && (isFolder(one) ? openFolder(one.id, one.name) : preview(one));
    if (act === "rename") return rename(one);
    if (act === "move") return moveDialog(ids);
    if (act === "newfolder") return newFolder();
    if (act === "share") return share(one);
    if (act === "import") return importToUefn(ids);
    if (act === "trash") return trash(ids);
    return null;
  }

  function onMenuKey(ev) {
    if (!menuEl || !menuEl.contains(ev.target)) return;
    const items = Array.from(menuEl.querySelectorAll(".gwd-menu__item"));
    const i = items.indexOf(ev.target);
    if (ev.key === "Escape") { closeMenu(); ev.preventDefault(); }
    else if (ev.key === "ArrowDown") { items[(i + 1) % items.length].focus(); ev.preventDefault(); }
    else if (ev.key === "ArrowUp") { items[(i - 1 + items.length) % items.length].focus(); ev.preventDefault(); }
  }

  // ── rendering ─────────────────────────────────────────────────────────

  const ICONS = {
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    model: '<path d="M12 3 4 7.5v9L12 21l8-4.5v-9z"/><path d="M4 7.5 12 12l8-4.5M12 12v9"/>',
    texture: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 17-5-5-9 8"/>',
    audio: '<path d="M9 18V6l10-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="16" r="2.5"/>',
    archive: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M12 3v2m0 2v2m0 2v2"/><rect x="10.5" y="13" width="3" height="4" rx="1"/>',
    file: '<path d="M6 3h8l5 5v13H6z"/><path d="M14 3v5h5"/>',
  };
  const TINT = { folder: "amber", model: "purple", texture: "green", audio: "blue", archive: "yellow", file: "fg" };

  function kindOf(r) {
    if (isFolder(r)) return "folder";
    if (isImage(r)) return "texture";
    return ICONS[r.kind] ? r.kind : "file";
  }

  function icon(r, size) {
    const k = kindOf(r);
    return `<svg class="gwd-icon" viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true" style="color:var(--${TINT[k]})">${ICONS[k]}</svg>`;
  }

  function renderSide() {
    const sel = q(".gwd-account");
    if (!sel) return;
    sel.innerHTML = st.accounts.map((a) => `<option value="${GW.esc(a.email)}" ${a.email === st.account ? "selected" : ""}>${GW.esc(a.email)}</option>`).join("");
    sel.hidden = st.accounts.length < 2;
    const one = q(".gwd-account-one");
    one.hidden = st.accounts.length !== 1;
    one.textContent = st.accounts.length === 1 ? st.accounts[0].email : "";
    const dot = q(".gwd-side__dot");
    dot.hidden = !st.account;
    if (st.account) dot.style.background = GW.accountColor(st.account);
    const manage = q(".gwd-manage");
    manage.hidden = !st.account || canManage();
    renderTree();
    renderUsage();
  }

  function treeHtml(id, depth) {
    const n = st.tree.get(id);
    if (!n) return "";
    const on = !st.query && current().id === n.id;
    const has = n.children === null || (n.children && n.children.length);
    const kids = n.open && n.children ? n.children.map((c) => treeHtml(c, depth + 1)).join("") : "";
    return `<div class="gwd-tree__node" role="treeitem" aria-expanded="${n.open ? "true" : "false"}" ${on ? 'aria-selected="true"' : ""}>
      <div class="gwd-tree__row ${on ? "is-on" : ""}" data-tree="${GW.esc(n.id)}" style="padding-left:${6 + depth * 14}px">
        <button type="button" class="gwd-tree__twist" data-twist="${GW.esc(n.id)}" aria-label="${n.open ? "Collapse" : "Expand"}" ${has ? "" : "disabled"}>${has ? (n.loading ? "…" : (n.open ? "▾" : "▸")) : ""}</button>
        ${icon({ folder: true }, 14)}
        <span class="gwd-tree__name">${GW.esc(n.name || "My Drive")}</span>
      </div>
      ${kids ? `<div role="group">${kids}</div>` : ""}
    </div>`;
  }

  function renderTree() {
    const t = q(".gwd-tree");
    if (!t) return;
    t.innerHTML = treeHtml(st.crumbs[0] ? st.crumbs[0].id : "", 0);
  }

  function renderUsage() {
    const u = q(".gwd-usage");
    if (!u) return;
    if (!st.usage) { u.hidden = true; return; }
    u.hidden = false;
    const pct = st.usage.percent == null ? 0 : Math.max(0, Math.min(100, Number(st.usage.percent)));
    u.innerHTML = `<div class="gwd-usage__bar" role="progressbar" aria-label="Drive storage" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}">
        <span class="gwd-usage__fill ${pct >= 90 ? "is-full" : ""}" style="width:${pct}%"></span></div>
      <div class="dk-muted gwd-usage__text">${GW.esc(st.usage.text || "")} used</div>`;
  }

  function renderCrumbs() {
    const c = q(".gwd-crumbs");
    if (st.query) {
      c.innerHTML = `<span class="gwd-crumb is-on">Search: ${GW.esc(st.query)}</span>`;
      return;
    }
    c.innerHTML = st.crumbs.map((cr, i) => {
      const last = i === st.crumbs.length - 1;
      return `${i ? '<span class="gwd-crumb__sep" aria-hidden="true">›</span>' : ""}<button type="button" class="gwd-crumb ${last ? "is-on" : ""}" data-crumb="${i}" data-drop="${GW.esc(cr.id || "root")}" ${last ? 'aria-current="page"' : ""}>${GW.esc(cr.name || "My Drive")}</button>`;
    }).join("");
  }

  function renderStatus() {
    const s = q(".gwd-status");
    if (!s) return;
    if (st.busy) s.innerHTML = `<span class="dk-muted">${GW.esc(st.busy)}…</span>`;
    else s.innerHTML = st.selected.size ? `<span class="dk-muted">${st.selected.size} selected</span>` : "";
  }

  function itemHtml(r) {
    const sel = st.selected.has(r.id);
    const meta = isFolder(r) ? "Folder" : [r.size, GW.when(r.modified)].filter((x) => x && x !== "—").join(" · ");
    if (st.layout === "list") {
      return `<div class="gwd-item gwd-item--row ${sel ? "is-sel" : ""}" role="option" aria-selected="${sel}" tabindex="-1" draggable="true"
        data-id="${GW.esc(r.id)}" ${isFolder(r) ? `data-drop="${GW.esc(r.id)}"` : ""}>
        ${icon(r, 18)}<span class="gwd-item__name">${GW.esc(r.name)}</span>
        <span class="gwd-item__kind dk-muted">${GW.esc(isFolder(r) ? "Folder" : (r.kind || ""))}</span>
        <span class="gwd-item__size dk-muted">${GW.esc(isFolder(r) ? "" : (r.size || ""))}</span>
        <span class="gwd-item__date dk-muted">${GW.esc(GW.when(r.modified))}</span></div>`;
    }
    return `<div class="gwd-item gwd-item--tile ${sel ? "is-sel" : ""}" role="option" aria-selected="${sel}" tabindex="-1" draggable="true"
      data-id="${GW.esc(r.id)}" ${isFolder(r) ? `data-drop="${GW.esc(r.id)}"` : ""} title="${GW.esc(r.name)}">
      <div class="gwd-item__thumb">${icon(r, 36)}</div>
      <div class="gwd-item__name">${GW.esc(r.name)}</div>
      <div class="gwd-item__meta dk-muted">${GW.esc(meta)}</div></div>`;
  }

  function renderMain() {
    if (!el) return;
    renderCrumbs();
    renderStatus();
    q(".gwd-layout").querySelectorAll("[data-layout]").forEach((b) => {
      const on = b.dataset.layout === st.layout;
      b.classList.toggle("is-on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    const body = q(".gwd-body");
    const files = q(".gwd-files");
    files.className = `gwd-files gwd-files--${st.layout}`;
    if (st.error && !st.rows.length) {
      body.innerHTML = `<div class="dk-error"><div class="dk-error__title">Couldn't load Drive</div>${GW.esc(st.error)}
        <div><button class="dk-btn gw-btn--small" type="button" data-drive="refresh">Try again</button></div></div>`;
    } else if (st.loading && !st.rows.length) {
      body.innerHTML = '<div class="dk-loading">Loading files…</div>';
    } else if (!st.rows.length) {
      body.innerHTML = `<div class="dk-empty"><div class="dk-empty__title">${st.query ? "No files match" : "This folder is empty"}</div>
        ${st.query ? "Try another part of the name." : "Drag files here from Explorer to upload them."}</div>`;
    } else {
      body.innerHTML = "";
    }
    const head = st.layout === "list" && st.rows.length
      ? '<div class="gwd-files__head dk-muted"><span></span><span>Name</span><span>Type</span><span>Size</span><span>Modified</span></div>' : "";
    files.innerHTML = head + st.rows.map(itemHtml).join("");
    const more = q(".gwd-more");
    more.hidden = !st.token;
    more.disabled = st.loading;
    more.textContent = st.loading ? "Loading…" : "Load more";
  }

  // ── interaction ───────────────────────────────────────────────────────

  function select(id, ev) {
    const ids = st.rows.map((r) => r.id);
    if (ev && ev.shiftKey && st.anchor) {
      const a = ids.indexOf(st.anchor);
      const b = ids.indexOf(id);
      if (a >= 0 && b >= 0) {
        if (!(ev.ctrlKey || ev.metaKey)) st.selected.clear();
        ids.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((x) => st.selected.add(x));
      }
    } else if (ev && (ev.ctrlKey || ev.metaKey)) {
      if (st.selected.has(id)) st.selected.delete(id); else st.selected.add(id);
      st.anchor = id;
    } else {
      st.selected = new Set([id]);
      st.anchor = id;
    }
    renderMain();
    const item = q(`.gwd-item[data-id="${cssEsc(id)}"]`);
    if (item) item.focus();
  }

  function cssEsc(v) {
    return root.CSS && root.CSS.escape ? root.CSS.escape(v) : String(v).replace(/["\\]/g, "\\$&");
  }

  function activate(id) {
    const r = rowById(id);
    if (!r) return;
    if (isFolder(r)) openFolder(r.id, r.name);
    else preview(r);
  }

  function onClick(ev) {
    const t = ev.target;
    if (menuEl && !menuEl.contains(t)) closeMenu();
    const m = t.closest("[data-menu]");
    if (m && menuEl) return runMenu(m.dataset.menu, menuEl._ids || []);
    const twist = t.closest("[data-twist]");
    if (twist) return expand(twist.dataset.twist);
    const tree = t.closest("[data-tree]");
    if (tree) {
      const n = st.tree.get(tree.dataset.tree);
      return openFolder(tree.dataset.tree, n ? n.name : "");
    }
    const crumb = t.closest("[data-crumb]");
    if (crumb) return goCrumb(Number(crumb.dataset.crumb));
    const lay = t.closest("[data-layout]");
    if (lay) {
      st.layout = lay.dataset.layout;
      GW.call("prefs.set", { id: LAYOUT_PREF, value: st.layout }).catch(() => {});
      return renderMain();
    }
    const btn = t.closest("[data-drive]");
    if (btn) {
      const act = btn.dataset.drive;
      if (act === "refresh") { load(true); return loadUsage(); }
      if (act === "upload") return pickAndUpload();
      if (act === "newfolder") return newFolder();
      if (act === "more") return load(false);
      if (act === "manage") return needManage().then(() => renderSide());
      return null;
    }
    const item = t.closest(".gwd-item");
    if (item) return select(item.dataset.id, ev);
    if (t.closest(".gwd-files") && !ev.ctrlKey && !ev.metaKey) { st.selected.clear(); renderMain(); }
    return null;
  }

  function onDblClick(ev) {
    const item = ev.target.closest(".gwd-item");
    if (item) activate(item.dataset.id);
  }

  function onContext(ev) {
    if (!ev.target.closest(".gwd-main")) return;
    ev.preventDefault();
    const item = ev.target.closest(".gwd-item");
    if (item && !st.selected.has(item.dataset.id)) select(item.dataset.id, null);
    if (!item) { st.selected.clear(); renderMain(); }
    openMenu(ev.clientX, ev.clientY, Array.from(st.selected));
  }

  function onKey(ev) {
    if (dialogEl) return;
    if (menuEl) return onMenuKey(ev);
    if (ev.target.closest("input, textarea, select")) return;
    const item = ev.target.closest(".gwd-item");
    const ids = st.rows.map((r) => r.id);
    const i = item ? ids.indexOf(item.dataset.id) : -1;
    const step = st.layout === "grid" ? gridColumns() : 1;
    if (ev.key === "Enter" && item) { activate(item.dataset.id); ev.preventDefault(); }
    else if ((ev.key === "Delete") && st.selected.size) { trash(Array.from(st.selected)); ev.preventDefault(); }
    else if (ev.key === "F2" && st.selected.size === 1) { rename(rowById(Array.from(st.selected)[0])); ev.preventDefault(); }
    else if ((ev.key === "ContextMenu" || (ev.shiftKey && ev.key === "F10")) && item) {
      const box = item.getBoundingClientRect();
      openMenu(box.left + 12, box.bottom, Array.from(st.selected));
      ev.preventDefault();
    } else if (ev.key === "Backspace" && st.crumbs.length > 1 && !st.query) { goCrumb(st.crumbs.length - 2); ev.preventDefault(); }
    else if (["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(ev.key) && ids.length && ev.target.closest(".gwd-files")) {
      const d = { ArrowDown: step, ArrowUp: -step, ArrowLeft: -1, ArrowRight: 1 }[ev.key];
      const next = ids[Math.max(0, Math.min(ids.length - 1, (i < 0 ? 0 : i + d)))];
      select(next, ev.shiftKey ? { shiftKey: true } : null);
      ev.preventDefault();
    }
  }

  function gridColumns() {
    const items = el.querySelectorAll(".gwd-item--tile");
    if (items.length < 2) return 1;
    const top = items[0].offsetTop;
    let n = 0;
    for (const it of items) { if (it.offsetTop !== top) break; n += 1; }
    return Math.max(1, n);
  }

  function dropTarget(ev) {
    const t = ev.target.closest("[data-drop]");
    return t ? t.dataset.drop : "";
  }

  function onDragStart(ev) {
    const item = ev.target.closest(".gwd-item");
    if (!item) return;
    if (!st.selected.has(item.dataset.id)) { st.selected = new Set([item.dataset.id]); renderStatus(); }
    ev.dataTransfer.setData(DRAG_TYPE, JSON.stringify(Array.from(st.selected)));
    ev.dataTransfer.effectAllowed = "move";
  }

  function isInternal(ev) {
    return Array.from((ev.dataTransfer && ev.dataTransfer.types) || []).includes(DRAG_TYPE);
  }

  function isExternal(ev) {
    return Array.from((ev.dataTransfer && ev.dataTransfer.types) || []).includes("Files");
  }

  function clearDropMarks() {
    el.querySelectorAll(".is-drop").forEach((x) => x.classList.remove("is-drop"));
  }

  function onDragOver(ev) {
    if (isInternal(ev)) {
      const dest = dropTarget(ev);
      clearDropMarks();
      if (!dest) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      const t = ev.target.closest("[data-drop]");
      if (t) t.classList.add("is-drop");
    } else if (isExternal(ev) && ev.target.closest(".gwd-main")) {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "copy";
      q(".gwd-main").classList.add("is-drop");
    }
  }

  function onDragLeave(ev) {
    if (!ev.relatedTarget || !el.contains(ev.relatedTarget)) clearDropMarks();
  }

  function onDrop(ev) {
    clearDropMarks();
    if (isInternal(ev)) {
      const dest = dropTarget(ev);
      if (!dest) return;
      ev.preventDefault();
      let ids = [];
      try { ids = JSON.parse(ev.dataTransfer.getData(DRAG_TYPE) || "[]"); } catch (_e) { ids = []; }
      move(ids, dest);
    } else if (isExternal(ev) && ev.target.closest(".gwd-main")) {
      ev.preventDefault();
      onDropFiles(ev);
    }
  }

  function onSearch(ev) {
    ev.preventDefault();
    st.query = q(".gwd-search input").value.trim();
    load(true);
    renderTree();
  }

  // ── view contract ─────────────────────────────────────────────────────

  function ensureCss() {
    const doc = root.document;
    if (doc.querySelector('link[href$="drive/drive.css"]')) return;
    const link = doc.createElement("link");
    link.rel = "stylesheet";
    link.href = "drive/drive.css";
    doc.head.appendChild(link);
  }

  function mount(container) {
    el = container;
    ensureCss();
    el.classList.add("gw-drive");
    el.innerHTML = `
      <aside class="gwd-side" aria-label="Drive folders">
        <div class="gwd-side__account">
          <span class="gw-dot gwd-side__dot" aria-hidden="true"></span>
          <select class="dk-input gwd-account" aria-label="Google account" hidden></select>
          <span class="gwd-account-one dk-muted" hidden></span>
        </div>
        <button class="dk-btn gw-btn--small gwd-manage" type="button" data-drive="manage" hidden>Turn on Manage Drive</button>
        <div class="gwd-tree" role="tree" aria-label="Folders"></div>
        <div class="gwd-usage" hidden></div>
      </aside>
      <div class="gwd-main">
        <div class="gwd-bar">
          <nav class="gwd-crumbs" aria-label="Folder path"></nav>
          <form class="gwd-search" role="search">
            <input class="dk-input" type="search" placeholder="Search Drive by name" aria-label="Search Drive">
          </form>
          <div class="gwd-layout" role="group" aria-label="Layout">
            <button type="button" class="gwd-layout__btn" data-layout="grid" title="Grid" aria-label="Grid">
              <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/><rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/></svg></button>
            <button type="button" class="gwd-layout__btn" data-layout="list" title="List" aria-label="List">
              <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg></button>
          </div>
          <button class="dk-btn gw-btn--small" type="button" data-drive="newfolder">New folder</button>
          <button class="dk-btn gw-btn--small" type="button" data-drive="upload">Upload</button>
          <button class="dk-btn gw-btn--small" type="button" data-drive="refresh" title="Reload this folder">Refresh</button>
        </div>
        <div class="gwd-status"></div>
        <div class="gwd-scroll">
          <div class="gwd-body"></div>
          <div class="gwd-files" role="listbox" aria-multiselectable="true" aria-label="Files" tabindex="0"></div>
          <button class="dk-btn gw-btn--small gwd-more" type="button" data-drive="more" hidden>Load more</button>
        </div>
      </div>`;
    el.addEventListener("click", onClick);
    el.addEventListener("dblclick", onDblClick);
    el.addEventListener("contextmenu", onContext);
    el.addEventListener("keydown", onKey);
    el.addEventListener("dragstart", onDragStart);
    el.addEventListener("dragover", onDragOver);
    el.addEventListener("dragleave", onDragLeave);
    el.addEventListener("drop", onDrop);
    q(".gwd-search").addEventListener("submit", onSearch);
    q(".gwd-search input").addEventListener("search", (ev) => { if (!ev.target.value && st.query) onSearch(ev); });
    q(".gwd-account").addEventListener("change", (ev) => pickAccount(ev.target.value));
    q(".gwd-scroll").addEventListener("scroll", closeMenu, { passive: true });
    GW.subscribe(SYNC_EVENTS);
    SYNC_EVENTS.forEach((t) => GW.on(t, () => { if (GW.visible() && el && !el.hidden) load(true); }));
    GW.call("prefs.get", { id: LAYOUT_PREF }).then((v) => {
      const pick = v && typeof v === "object" ? v.value : v;
      if (pick === "grid" || pick === "list") { st.layout = pick; renderMain(); }
    }).catch(() => {});
    resetRoot();
    renderSide();
    renderMain();
  }

  async function update(accounts) {
    const before = st.account;
    st.accounts = (accounts || []).slice();
    if (st.account && !st.accounts.some((a) => a.email === st.account)) st.account = "";
    if (!st.account && st.accounts.length) {
      const saved = await GW.call("prefs.get", { id: "drive_account" }).catch(() => null);
      const pick = saved && typeof saved === "object" ? saved.value : saved;
      st.account = (st.accounts.find((a) => a.email === pick) || st.accounts[0]).email;
    }
    if (!el) return;
    if (st.account !== before) {
      resetRoot();
      renderSide();
      load(true);
      loadUsage();
    } else {
      renderSide();
    }
  }

  function show() {
    if (st.account && !st.rows.length && !st.loading) load(true);
  }

  function hide() {
    closeMenu();
  }

  /** An open request: {id, query, account} from google_open or an [Open] link. */
  async function open(req) {
    const r = req || {};
    const account = String(r.account || "").toLowerCase();
    const match = account && st.accounts.find((a) => a.email.toLowerCase() === account);
    if (match) pickAccount(match.email);
    if (r.query) {
      q(".gwd-search input").value = r.query;
      st.query = String(r.query);
      await load(true);
    }
    const id = String(r.id || r.item_id || "");
    if (!id) return;
    let info = rowById(id);
    if (!info) {
      try { info = await rpc("gdrive_file_info", { file: id }); } catch (_e) { info = null; }
    }
    if (info && isFolder(info)) openFolder(info.id || id, info.name);
    else if (info) preview(Object.assign({ id }, info));
  }

  const view = { mount, update, show, hide, open, refresh: () => load(true), state: st, _isFolder: isFolder, _isImage: isImage };
  GW.registerView("drive", view);
  if (typeof module !== "undefined" && module.exports) module.exports = view;
})(typeof window !== "undefined" ? window : globalThis);
