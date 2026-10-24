/* Open requests: google_open (the AI) and [Open] links in chat put the tab on one view/item.
 *
 * The backend keeps the last request for 120 s and hands it out once through the
 * google_open_pending panel RPC: {view, id (or item_id), query, account}. The tab takes it
 * when the shell is ready, whenever the panel comes back on screen (panel.visibility) and on
 * a google_open push. A plugin:googledrive/main?view=…&id=…&q=…&account=… link that opened
 * the page is read from location the same way.
 */
(function (root) {
  "use strict";

  const GW = root.GW;
  const VIEWS = ["gmail", "drive"];
  const OPEN_EVENTS = ["google_open", "google_open_requested"];
  let queued = null;
  let taking = false;

  /** {view, id, item_id, query, account} from an RPC result, a push event or a link. */
  function normalize(raw) {
    let r = raw;
    if (r && typeof r === "object" && "request" in r) r = r.request;
    if (!r || typeof r !== "object") return null;
    const view = String(r.view || "").trim().toLowerCase();
    if (!VIEWS.includes(view)) return null;
    const id = String(r.item_id || r.id || "").trim();
    return {
      view,
      id,
      item_id: id,
      query: String(r.query || r.q || "").trim(),
      account: String(r.account || "").trim(),
    };
  }

  function fromLocation(loc) {
    if (!loc) return null;
    const raw = String(loc.search || "").replace(/^\?/, "") || String(loc.hash || "").replace(/^#\??/, "");
    if (!raw) return null;
    const p = new URLSearchParams(raw);
    return normalize({ view: p.get("view"), id: p.get("id") || p.get("item_id"), query: p.get("q") || p.get("query"), account: p.get("account") });
  }

  /** Switch the pill and hand the request to the view; waits for accounts when the shell isn't ready. */
  function apply(req) {
    const shell = GW.shell;
    if (!req || !shell) return false;
    shell.setView(req.view);
    const view = GW.views.get(req.view);
    if (!view || !shell.state.mounted.has(req.view)) {
      queued = shell.state.loaded ? null : req;
      return false;
    }
    queued = null;
    if (view.open) {
      Promise.resolve()
        .then(() => view.open(Object.assign({}, req)))
        .catch((err) => console.warn("Google tab: couldn't open", req.view, err && err.message));
    }
    return true;
  }

  async function takePending() {
    if (taking) return null;
    taking = true;
    try {
      const req = normalize(await GW.rpc("google_open_pending"));
      if (req) apply(req);
      return req;
    } catch (_err) {
      return null;
    } finally {
      taking = false;
    }
  }

  function start() {
    GW.subscribe(OPEN_EVENTS);
    OPEN_EVENTS.forEach((t) => GW.on(t, (e) => {
      const direct = normalize(e);
      if (direct) apply(direct);
      takePending();
    }));
    GW.on("gw:ready", () => {
      const link = fromLocation(root.location);
      if (link) apply(link);
      takePending();
    });
    GW.on("gw:shown", () => takePending());
    GW.on("gw:accounts", () => { if (queued) apply(queued); });
  }

  const open = { normalize, fromLocation, apply, takePending };
  GW.open = open;
  start();
  if (typeof module !== "undefined" && module.exports) module.exports = open;
})(typeof window !== "undefined" ? window : globalThis);
