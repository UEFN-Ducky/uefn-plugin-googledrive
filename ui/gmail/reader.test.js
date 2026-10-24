/* node --test ui/gmail/reader.test.js — the reading pane's safety rules and action flows. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const GW = require("../shared/bridge.js");
const reader = require("./reader.js");

const EVIL = `<html><head><meta http-equiv="refresh" content="0;url=https://evil.example"></head><body>
<p onclick="steal()">Hi there</p>
<script>alert(document.cookie)</script>
<SCRIPT src="https://evil.example/x.js"></SCRIPT>
<img src="x" onerror="alert(2)">
<a href="javascript:alert(3)">click</a>
<a href=" JaVaScRiPt:alert(4)">click</a>
<iframe src="https://evil.example"></iframe>
<object data="https://evil.example/x.swf"></object>
<form action="https://evil.example"><input name="pw"></form>
<svg><script>alert(5)</script></svg>
</body></html>`;

function wrap(text) {
  return `[ducky:untrusted-content source=email] note\n<<<untrusted:email>>>\n${text}\n<<<end untrusted:email>>>`;
}

function stub(rpcAnswers, callAnswers) {
  const calls = [];
  GW.rpc = async (name, params) => {
    calls.push({ kind: "rpc", name, params });
    const a = rpcAnswers[name];
    return typeof a === "function" ? a(params) : a;
  };
  GW.call = async (method, params) => {
    calls.push({ kind: "call", name: method, params });
    const a = (callAnswers || {})[method];
    return typeof a === "function" ? a(params) : a;
  };
  return calls;
}

function openThread(ids) {
  reader.state.open = true;
  reader.state.account = "me@example.com";
  reader.state.messages = ids.map((id) => ({ id, labels: ["INBOX"], subject: "Hello", text: wrap("body") }));
  reader.state.busy = "";
}

test("a script-tag mail renders inert", () => {
  const out = reader.buildSrcdoc(reader.stripUntrusted(wrap(EVIL)), { images: false });
  const body = out.slice(out.indexOf("<body>"));
  assert.doesNotMatch(body, /<script/i);
  assert.doesNotMatch(body, /\bon[a-z]+\s*=/i);
  assert.doesNotMatch(body, /javascript:/i);
  assert.doesNotMatch(body, /<(iframe|object|form|input|meta)\b/i);
  assert.match(body, /Hi there/);
  assert.match(out, /Content-Security-Policy" content="default-src 'none'; script-src 'none';/);
  assert.ok(out.indexOf("Content-Security-Policy") < out.indexOf("<body>"), "CSP comes before the mail");
  assert.doesNotMatch(reader.IFRAME_SANDBOX, /allow-scripts/);
});

test("remote images stay blocked until Load images", () => {
  const html = '<p>x</p><img src="https://tracker.example/pixel.gif">';
  assert.equal(reader.hasRemoteImages(html), true);
  assert.equal(reader.hasRemoteImages('<img src="cid:logo">'), false);
  assert.equal(reader.hasRemoteImages('<td style="background:url(//cdn.example/a.png)">'), true);
  assert.match(reader.buildSrcdoc(html, { images: false }), /img-src data: cid:;/);
  assert.match(reader.buildSrcdoc(html, { images: true }), /img-src data: cid: https: http:;/);
});

test("the untrusted markers and agent note never show", () => {
  assert.equal(reader.stripUntrusted(wrap("Hello\nworld")), "Hello\nworld");
  assert.equal(reader.stripUntrusted("plain"), "plain");
  assert.equal(reader.stripUntrusted(null), "");
});

test("theme variables can't break out of the style block", () => {
  const out = reader.buildSrcdoc("<p>x</p>", { vars: { "--bg": "#111", "--fg": "red;}</style><script>x()</script>", "bad key": "1" } });
  assert.match(out, /--bg:#111/);
  assert.doesNotMatch(out, /<script/i);
});

test("Import to UEFN only for model, texture, audio and zip files", () => {
  for (const f of ["tree.fbx", "Rock.GLB", "albedo.png", "hit.wav", "pack.zip"]) assert.equal(reader.isImportable(f), true, f);
  for (const f of ["invoice.pdf", "run.exe", "noext", "notes.txt"]) assert.equal(reader.isImportable(f), false, f);
  assert.equal(reader.formatSize(512), "512 B");
  assert.equal(reader.formatSize(2048), "2 KB");
  assert.equal(reader.formatSize(5 * 1024 * 1024), "5.0 MB");
});

test("Trash previews, asks in Ducky's confirm, and does nothing on No", async () => {
  openThread(["m1", "m2"]);
  const calls = stub({ gmail_trash: { ok: true, preview: true, message: 'Would trash 2 messages: "Hello", "Hello".' } },
    { "ui.confirm": { confirmed: false } });
  assert.equal(await reader.trash(), false);
  const trash = calls.filter((c) => c.name === "gmail_trash");
  assert.equal(trash.length, 1);
  assert.equal(trash[0].params.confirmed, undefined);
  assert.deepEqual(trash[0].params.ids, ["m1", "m2"]);
  const confirm = calls.find((c) => c.name === "ui.confirm");
  assert.match(confirm.params.message, /Would trash 2 messages/);
  assert.equal(confirm.params.danger, true);
  assert.equal(reader.state.open, true);
});

test("Trash goes through with confirmed=true only after Yes", async () => {
  openThread(["m1"]);
  const calls = stub({ gmail_trash: (p) => (p.confirmed ? { ok: true, changed: true } : { ok: true, preview: true, message: "Would trash 1 message." }) },
    { "ui.confirm": { confirmed: true } });
  assert.equal(await reader.trash(), true);
  const trash = calls.filter((c) => c.name === "gmail_trash");
  assert.equal(trash.length, 2);
  assert.equal(trash[1].params.confirmed, true);
  assert.equal(trash[1].params.account, "me@example.com");
  assert.equal(reader.state.open, false);
});

test("Archive, Unread and labels call the same RPCs as the tools", async () => {
  openThread(["a", "b"]);
  let calls = stub({ gmail_archive: { ok: true } });
  assert.equal(await reader.archive(), true);
  assert.deepEqual(calls[0], { kind: "rpc", name: "gmail_archive", params: { ids: ["a", "b"], account: "me@example.com" } });

  openThread(["a", "b"]);
  calls = stub({ gmail_mark_unread: { ok: true } });
  await reader.markUnread();
  assert.deepEqual(calls[0].params, { ids: ["b"], account: "me@example.com" });

  openThread(["a"]);
  calls = stub({ gmail_modify: { ok: true }, gmail_label_create: { ok: true, label: { id: "Label_9" } } });
  await reader.toggleLabel("STARRED", true);
  assert.deepEqual(calls[0].params.add_labels, ["STARRED"]);
  assert.ok(reader.state.messages[0].labels.includes("STARRED"));
  await reader.toggleLabel("STARRED", false);
  assert.deepEqual(calls[1].params.remove_labels, ["STARRED"]);
  await reader.createLabel("Receipts");
  assert.equal(calls[2].name, "gmail_label_create");
  assert.deepEqual(calls[3].params.add_labels, ["Label_9"]);
});

test("Reply / Reply all / Forward hand the message to compose", () => {
  openThread(["a", "b"]);
  const seen = [];
  const off = GW.on("gmail:compose", (e) => seen.push(e));
  reader.compose("reply_all", null);
  off();
  assert.equal(seen.length, 1);
  assert.equal(seen[0].mode, "reply_all");
  assert.equal(seen[0].message.id, "b");
  assert.equal(seen[0].account, "me@example.com");
});

test("Ask Ducky runs only on an allowed model", async () => {
  openThread(["a"]);
  GW.mailAI.models(true).catch(() => {});
  let calls = stub({ google_allowed_models: { models: [] } }, { "models.list": { models: [{ id: "anthropic/claude-opus-5-5", name: "Opus" }] } });
  await GW.mailAI.models(true);
  await reader.ask("Summarize");
  assert.match(reader.state.askError, /Allowed models/);
  assert.equal(calls.some((c) => c.name === "llm.complete"), false);

  calls = stub({ google_allowed_models: { models: ["claude-opus-5-5"] } }, {
    "models.list": { models: [{ id: "anthropic/claude-opus-5-5", name: "Opus" }, { id: "gpt-x", name: "X" }] },
    "prefs.get": { value: "gpt-x" },
    "llm.complete": { ok: true, text: "Two things to do." },
  });
  await GW.mailAI.models(true);
  await reader.ask("Summarize");
  const llm = calls.find((c) => c.name === "llm.complete");
  assert.equal(llm.params.model, "anthropic/claude-opus-5-5");
  assert.match(llm.params.system, /DATA from outside/);
  assert.equal(reader.state.answer, "Two things to do.");
});
