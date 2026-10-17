// checks.js on Home: a row whose check ended is drawn again by sync.js's row sync (towSync.rows), the
// one writer of Home's rows - for the topics revision the page has, never over a row the owner works
// in (sync_rows.mjs and sync_page.mjs test that writer) - and never by a request of its own
// (/topics/<id>/row): a late answer of that request replaced a row another device had changed since,
// unchecked. After Check all: the rows in sight at once, the others as they come into sight.
// Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { build, fakeDocument, fakeTimers, memoryStorage } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/checks.js", import.meta.url), "utf8");
const settle = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };

const page = ({ withSync = true } = {}) => {
  const document = fakeDocument();
  const timers = fakeTimers();
  const row = (id, top) => {
    const element = build(document, ["div", { class: "row-wrap", id: `row-${id}` },
      ["span", { class: "row-ops" }, ["form", { method: "post", action: `/topics/${id}/check` }, ["button", { class: "row-ico" }, ["svg", {}, ["use", { href: "#i-refresh" }]]]]]]);
    element.rect = { top, bottom: top + 40, height: 40 };
    return element;
  };
  const list = build(document, ["div", { id: "topics" }]);
  list.append(row("a", 100), row("b", 140), row("far", 5000));
  document.body.append(build(document, ["div", { id: "msg-host", "data-namespace": "ns" }]), list);
  const fetched = [];
  const redrawn = [];
  const listRefreshed = [];
  const status = new Map(); // job -> the body /check/status answers
  const window = {
    setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    setInterval: timers.setInterval, clearInterval: timers.clearInterval,
    addEventListener() {}, innerHeight: 800,
    sessionStorage: memoryStorage(),
    towMessages: { showMessage() {}, remove() {}, refresh() {}, signOut() {} },
    towRows: { health() {} },
    towList: { refresh: (options) => listRefreshed.push(options) },
    ...(withSync ? { towSync: { rows: { redraw: (ids) => { redrawn.push([...ids]); return Promise.resolve(); } } } } : {}),
  };
  const context = {
    window, document, URL, URLSearchParams, JSON, Math, Number, String, Array, Object, Map, Set, Boolean, Promise, Uint8Array,
    crypto: globalThis.crypto,
    Date: { now: timers.now },
    setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    location: { href: "http://127.0.0.1/" },
    history: { state: null, replaceState() {} },
    fetch: async (url) => {
      fetched.push(String(url));
      const job = new URLSearchParams(String(url).split("?")[1] || "").get("job");
      const body = status.get(job) || { status: "running", job };
      return { ok: true, status: 200, json: async () => body, text: async () => "<div class='row-wrap' id='row-a'>old</div>" };
    },
  };
  vm.runInNewContext(source, context);
  const follow = async (job, topic) => {
    window.towChecks.scheduler.adopt(job, { subject: "Show", topic, first: 0 });
    timers.advance(10);
    await settle();
  };
  return { document, timers, fetched, redrawn, listRefreshed, status, follow, window };
};
const verdict = {};

// A check of one topic ends: its row is drawn again through towSync.rows, and the list's tools
// follow; nothing asks /topics/<id>/row.
{
  const test = page();
  test.status.set("J1", { status: "done", job: "J1", topic_id: "a", message: { id: "check-J1", kind: "ok", subject: "Show", text: "no changes" } });
  await test.follow("J1", "a");
  assert.deepEqual(test.redrawn, [["a"]]);
  assert.ok(test.fetched.every((url) => !url.includes("/row")), String(test.fetched));
  assert.equal(JSON.stringify(test.listRefreshed.at(-1)), '{"keep":true}');
}
verdict.oneWriterForTheEndedCheck = true;

// Check all ends: the rows in sight are drawn again at once (one request of the row sync), a row
// further down when it comes into sight.
{
  const test = page();
  test.status.set("J2", { status: "done", job: "J2", topic_id: "", message: { id: "check-J2", kind: "ok", text: "checked" } });
  await test.follow("J2", "");
  assert.deepEqual(test.redrawn, [["a", "b"]]);
  test.document.getElementById("row-far").rect = { top: 300, bottom: 340, height: 40 };
  (test.document.listeners.click || []).forEach(({ fn }) => fn({ target: test.document.body }));
  test.timers.advance(250);
  await settle();
  assert.deepEqual(test.redrawn, [["a", "b"], ["far"]]);
  assert.ok(test.fetched.every((url) => !url.includes("/row")), String(test.fetched));
}
verdict.checkAllRowsInSightThenLater = true;

// A page without the row sync (no list: Sites, Settings…): nothing is drawn, nothing asked.
{
  const test = page({ withSync: false });
  test.status.set("J3", { status: "failed", job: "J3", topic_id: "a", message: { id: "check-J3", kind: "err", subject: "Show", text: "failed" } });
  await test.follow("J3", "a");
  assert.deepEqual(test.redrawn, []);
  assert.ok(test.fetched.every((url) => !url.includes("/row")), String(test.fetched));
}
verdict.noRowSyncNoRowRequest = true;

console.log(JSON.stringify(verdict));
