// sync.js on a page: what /sync.json says reaches the header (site icons with their words and
// membership, the client, the bell), the rows of checks started elsewhere (and Check all), the one
// Undo (its server time, removed when gone or not allowed here), the theme, the language (an idle
// page reloads, a page in use is told) and the version (a reload 5 s later that activity cancels,
// never a loop). A new row comes in its place and the visible rows keep theirs. Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { build, fakeDocument, fakeTimers, FakeElement, memoryStorage } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/sync.js", import.meta.url), "utf8");
const REV = (n) => String(n).repeat(32).slice(0, 32);
const flush = async () => { for (let i = 0; i < 40; i += 1) await Promise.resolve(); };

// What the fake DOM lacks and sync.js uses.
FakeElement.prototype.insertBefore = function insertBefore(node, ref) { if (ref) ref.before(node); else this.append(node); };
Object.defineProperty(FakeElement.prototype, "parentNode", { get() { return this.parent; } });
Object.defineProperty(FakeElement.prototype, "lang", { get() { return this.getAttribute("lang") || ""; } });

const page = () => {
  const document = fakeDocument();
  document.readyState = "interactive";
  document.documentElement = document.root;
  document.root.setAttribute("lang", "en");
  const header = build(document, ["header", { class: "app" },
    ["div", { class: "hdr-sites" },
      ["span", { class: "trk ok", title: "Rutor: answers" }, "Rutor", ["span", { class: "sr-only" }, ": answers"]]],
    ["div", { class: "hdr-svc" },
      ["span", { class: "trk ok" }, "qBittorrent", ["span", { class: "sr-only" }, ": Connected"]],
      ["a", { class: "trk trk-ico mut", href: "/settings?open=bots" }]],
    ["span", { class: "hdr-clock", id: "next-check" }],
    ["form", { method: "post", action: "/check" }, ["button", { class: "ico" }]],
  ]);
  const row = (id) => build(document, ["div", { class: "row-wrap", id: `row-${id}`, "data-row-hash": `h-${id}`, "data-q": id },
    ["details", { class: "row-edit" }, ["summary", {}, id], ["div", { class: "edit-panel" }]],
    ["span", { class: "row-ops" }, ["form", { method: "post", action: `/topics/${id}/check` }, ["button", { class: "row-ico" }, ["svg", {}, ["use", { href: "#i-refresh" }]]]]]]);
  const main = build(document, ["main", { id: "main" },
    ["div", { id: "msg-host", "data-namespace": "ns" }],
    ["div", { id: "topics" }, ["form", { method: "post", action: "/topics/add", id: "addform" }, ["input", { name: "url", id: "field" }]]],
  ]);
  const footer = build(document, ["footer", { "data-page-version": "1.33.0", "data-static-version": "s1" }]);
  const texts = build(document, ["script", { id: "tow-i18n", type: "application/json" }, JSON.stringify({
    "js.messages.undo_available": "You can undo {what}", "js.sync.updated": "TOW was updated to {version}. Reload the page.",
    "js.sync.reload_language": "The language was changed. Reload the page.", "js.messages.reload": "Reload the page",
    "base.header.site_ok": "answers", "base.header.site_warn": "trouble", "base.header.not_checked": "not checked",
    "web.header.connected": "Connected", "web.header.disconnected": "Disconnected",
    "js.sync.row_changed": "This topic was changed on another device.",
  })]);
  document.body.append(texts, header, main, footer);
  const list = document.getElementById("topics");
  list.append(row("a"), row("b"));
  // The add form above is not a row: drafts in it count, rows only by their own controls.
  return { document, list, row };
};

const { document, list, row } = page();
const clock = fakeTimers();
const syncAnswers = [];
const rowAnswers = [];
const sent = [];
const shown = [];
const removedMessages = [];
const undos = [];
const posted = [];
const scrolled = [];
let reloads = 0;
let refreshed = [];
const built = new Map();
const realCreate = document.createElement;
document.createElement = (tag) => {
  if (tag !== "template") return realCreate(tag);
  const template = { content: { querySelector: () => built.get(template.html) || null } };
  Object.defineProperty(template, "innerHTML", { set(value) { template.html = value; } });
  return template;
};
const respond = (status, body = null, headers = {}) => ({ status, headers: { get: (name) => headers[name] ?? null }, json: async () => body });
const window = {
  setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, setInterval: clock.setInterval, clearInterval: clock.clearInterval,
  addEventListener() {},
  scrollBy: (x, y) => scrolled.push(y),
  location: null,
  sessionStorage: memoryStorage(),
  towMessages: {
    center: { setUndo: (value) => undos.push(value) },
    showMessage: (record) => shown.push(record),
    remove: (id) => removedMessages.push(id),
    refresh() {},
    signOut() {},
  },
  towList: { refresh: (options) => { refreshed.push(options); window.onRefresh?.(); } },
  towRows: { prepare() {}, timers() {} },
};
window.location = { pathname: "/", search: "", reload: () => { reloads += 1; } };
class Channel { constructor(name) { this.name = name; } postMessage(data) { posted.push([this.name, data]); } close() {} }
const context = {
  window, document, location: window.location, URLSearchParams, Promise, Math, JSON, Object, Number, String, Array, Map, Set, Boolean,
  crypto: globalThis.crypto, BroadcastChannel: Channel,
  Date: { now: clock.now, parse: Date.parse },
  fetch: async (url, init = {}) => {
    sent.push({ url: String(url), init });
    if (String(url).startsWith("/sync.json")) return syncAnswers.shift()?.() || respond(304);
    if (String(url) === "/topics/rows") return rowAnswers.shift()?.(JSON.parse(init.body)) || respond(500);
    return respond(404);
  },
};
vm.runInNewContext(source, context);
const sync = window.towSync;
const fire = (type) => (document.listeners[type] || []).forEach(({ fn }) => fn({ target: document.body }));
const run = async (ms) => {
  const end = clock.now() + ms;
  while (clock.now() < end) {
    clock.advance(Math.min(50, end - clock.now()));
    await flush();
  }
};
const answer = (areas, nowTs = clock.now() / 1000) => syncAnswers.push(() => respond(200, { ok: true, schema: 1, now_ts: nowTs, tz_now: 60, rev: {}, areas }, { ETag: 'W/"x"' }));
const verdict = {};

const rutor = document.querySelector(".hdr-sites .trk");
fire("DOMContentLoaded");
// ---- the header: tones, words, membership, the client and the bell, in one answer
answer({
  header: {
    rev: REV(1), sites: { rutor: "warn", kinozal: "ok" }, site_titles: { rutor: "Rutor", kinozal: "Kinozal" },
    qbit_tone: "bad", qbit: "Disconnected", client_label: "qBittorrent", bot: "Telegram: delivery fails", bot_ok: false,
  },
});
await run(5100);
assert.ok(sent.some((r) => r.url.startsWith("/sync.json")), "the first poll within 5 s");
assert.equal(sent[0].init.headers["X-TOW-Tab"].length, 24);
{
  const sites = [...document.querySelectorAll(".hdr-sites .trk")];
  assert.equal(sites[0], rutor); // the page's own icon, patched in place
  assert.deepEqual(sites.map((node) => [node.dataset.hdrSite, node.className, node.title, node.textContent]), [
    ["rutor", "trk warn", "Rutor: trouble", "Rutor: trouble"],
    ["kinozal", "trk ok", "Kinozal: answers", "Kinozal: answers"],
  ]);
  const client = document.querySelector(".hdr-svc .trk");
  assert.equal(client.className, "trk bad");
  assert.equal(client.title, "qBittorrent: Disconnected");
  assert.equal(client.querySelector(".sr-only").textContent, ": Disconnected");
  const bell = document.querySelector(".hdr-svc .trk-ico");
  assert.equal(bell.className, "trk trk-ico bad");
  assert.equal(bell.getAttribute("aria-label"), "Telegram: delivery fails");
}
answer({ header: { rev: REV(2), sites: { kinozal: "mut" }, site_titles: { kinozal: "Kinozal" }, qbit_tone: "ok", qbit: "Connected", client_label: "qBittorrent", bot: "", bot_ok: null } });
await run(5000);
{
  const sites = [...document.querySelectorAll(".hdr-sites .trk")];
  assert.deepEqual(sites.map((node) => node.dataset.hdrSite + "=" + node.className), ["kinozal=trk mut"]);
  assert.equal(sites[0].title, "Kinozal: not checked");
  assert.equal(document.querySelector(".hdr-svc .trk-ico").className, "trk trk-ico mut");
}
verdict.headerPatchedTogether = true;

// ---- checks started elsewhere: the row turns, Check all turns the header; when they end the
// row is drawn again (its words) and the marks go.
answer({ jobs: { rev: REV(3), waiting: [{ id: "j1", topic_id: "a", apply: true, status: "running", position: 1 }, { id: "j2", topic_id: "", apply: true, status: "queued", position: 2 }], ended: [] } });
await run(5000);
const checkButton = () => document.getElementById("row-a").querySelector("button");
assert.equal(checkButton().classList.contains("spinning"), true);
assert.equal(document.querySelector('header.app form[action="/check"] button').classList.contains("spinning"), true);
assert.equal(document.querySelector(".hdr-sites .trk").classList.contains("busy"), true);
answer({ jobs: { rev: REV(4), waiting: [], ended: [{ id: "j1", topic_id: "a", apply: true, status: "done", cancelled: false }] } });
await run(5000);
assert.equal(checkButton().classList.contains("spinning"), false);
assert.equal(document.querySelector('header.app form[action="/check"] button').classList.contains("spinning"), false);
assert.equal(document.querySelector(".hdr-sites .trk").classList.contains("busy"), false);
verdict.jobsElsewhereShownAndCleaned = true;

// ---- Undo: live from the server's clock; gone when used or not allowed on this device.
answer({ undo: { rev: REV(5), undo: { stamp: "S1", until_ts: 1000, kind: "topic_pause", label: "the pause of A", local_only: false } } }, 880);
await run(5000);
{
  const last = undos.at(-1);
  assert.equal(last.stamp, "S1");
  assert.equal(last.until - clock.now() <= 120000 && last.until - clock.now() > 110000, true);
  assert.ok(last.text.includes("the pause of A"));
}
answer({ undo: { rev: REV(6), undo: { stamp: "S1", until_ts: 1000, kind: "access", label: "x", local_only: true } } }, 890);
await run(5000);
assert.equal(JSON.stringify(undos.at(-1)), "{}");
assert.ok(removedMessages.includes("undo-S1"));
verdict.undoLiveAndRemoteEligibility = true;

// ---- the theme follows; Auto is each device's own
answer({ config: { rev: REV(7), theme: "dark", language: "en", language_resolved: "en", interval_sec: 3600 } });
await run(5000);
assert.equal(document.root.dataset.theme, "dark");
answer({ config: { rev: REV(8), theme: "auto", language: "auto", language_resolved: "en", interval_sec: 3600 } });
await run(5000);
assert.equal(document.root.dataset.theme, undefined);
verdict.themeFollows = true;

// ---- the language: a page in use is told (no reload); idle again, it reloads once
const field = document.getElementById("field");
field.focus();
answer({ config: { rev: REV(1).replace("1", "2"), theme: "auto", language: "ru", language_resolved: "ru", interval_sec: 3600 } });
await run(5000);
assert.equal(reloads, 0);
assert.ok(shown.some((record) => record.id === "sync-language"));
field.ownerDocument.activeElement = document.body;
fire("focusout");
await run(2000);
assert.equal(reloads, 1);
verdict.languageNoticeThenIdleReload = true;

// ---- a row another device added comes in its place; the visible row keeps its place
const fresh = build(document, ["div", { class: "row-wrap", id: "row-n", "data-q": "n" }, ["details", { class: "row-edit" }, ["summary", {}, "n"]]]);
built.set("<n>", fresh);
const anchor = document.getElementById("row-a");
anchor.rect = { top: 100, bottom: 140, height: 40 };
window.onRefresh = () => { anchor.rect = { top: 140, bottom: 180, height: 40 }; };
answer({ topics: { rev: REV(3).replace("3", "a"), full: false, order: ["n", "a", "b"], changed: [], removed: [] } });
rowAnswers.push(({ ids, rev }) => respond(200, { ok: true, rev, order: ["n", "a", "b"], rows: ids.map((id) => ({ id, html: `<${id}>`, hash: `h2-${id}` })), missing: [], deferred: [] }));
await run(5000);
const order = list.querySelectorAll(".row-wrap").map((each) => each.id);
assert.deepEqual(order, ["row-n", "row-a", "row-b"]);
assert.equal(fresh.dataset.rowHash, "h2-n");
assert.deepEqual(refreshed.at(-1).order, ["n", "a", "b"]);
assert.equal(refreshed.at(-1).keep, true);
assert.deepEqual(scrolled.slice(-1), [40]);
window.onRefresh = null;
await run(5000);
assert.ok(sent.filter((r) => r.url.startsWith("/sync.json")).at(-1).url.includes("topics%3A"), "applied: named in have");
verdict.rowInsertedAnchorKept = true;

// ---- a focus left on a row's summary does not hold the row: it is drawn again, the focus kept on
// its summary; a field with the focus does hold it (a note says it changed elsewhere)
{
  const summary = document.getElementById("row-b").querySelector("summary");
  summary.focus();
  const redrawn = build(document, ["div", { class: "row-wrap", id: "row-b", "data-q": "b" }, ["details", { class: "row-edit" }, ["summary", {}, "b2"], ["div", { class: "edit-panel" }]]]);
  built.set("<b-2>", redrawn);
  answer({ topics: { rev: REV(4).replace("4", "c"), full: false, order: ["n", "a", "b"], changed: ["b"], removed: [] } });
  rowAnswers.push(({ rev }) => respond(200, { ok: true, rev, order: ["n", "a", "b"], rows: [{ id: "b", html: "<b-2>", hash: "h3-b" }], missing: [], deferred: [] }));
  await run(5000);
  assert.equal(document.getElementById("row-b"), redrawn);
  assert.equal(document.activeElement, redrawn.querySelector("summary"));
  const input = build(document, ["input", { name: "title" }]);
  redrawn.querySelector(".edit-panel").append(input);
  input.focus();
  answer({ topics: { rev: REV(4).replace("4", "d"), full: false, order: ["n", "a", "b"], changed: ["b"], removed: [] } });
  rowAnswers.push(({ rev }) => respond(200, { ok: true, rev, order: ["n", "a", "b"], rows: [{ id: "b", html: "<b-3>", hash: "h4-b" }], missing: [], deferred: [] }));
  await run(5000);
  assert.equal(document.getElementById("row-b"), redrawn);
  assert.equal(redrawn.dataset.syncState, "changed");
  assert.ok(redrawn.querySelector("[data-sync-note]"));
  document.activeElement = document.body;
}
verdict.focusKeptFieldProtects = true;

// ---- the version: 5 s later an idle page reloads; activity in those 5 s cancels it and says why
const before = reloads;
answer({ version: { rev: REV(1).replace("1", "b"), app: "1.33.0", static: "s2" } });
while (syncAnswers.length) await run(50); // the answer has arrived: the reload is 5 s away
await run(1000);
field.focus(); // the owner starts typing: the reload waits, the page says why
fire("focusin");
await run(10000);
assert.equal(reloads, before);
assert.ok(shown.some((record) => record.id === "sync-version" && record.text.includes("1.33.0")));
document.activeElement = document.body;
fire("focusout");
await run(4000); // 5 s after the last activity, not sooner
assert.equal(reloads, before);
await run(2000);
assert.equal(reloads, before + 1);
verdict.versionDeferredAndCancelled = true;

// ---- the hint other tabs get: scoped to this install and sign-in, no data in it
sync.hint();
assert.deepEqual(posted.at(-1), ["tow-sync.ns", "changed"]);
verdict.hintScopedWithoutData = true;

// ---- signing out by the form: nothing asked any more, nothing applied
const polls = sent.filter((r) => r.url.startsWith("/sync.json")).length;
const logout = build(document, ["form", { method: "post", action: "/logout" }]);
document.body.append(logout);
(document.listeners.submit || []).forEach(({ fn }) => fn({ target: logout }));
await run(20000);
assert.equal(sent.filter((r) => r.url.startsWith("/sync.json")).length, polls);
sync.hint(); // signed out: no hint any more
assert.equal(posted.length, 1);
verdict.signOutStopsPolling = true;

console.log(JSON.stringify(verdict));
