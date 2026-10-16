// The Updates card (updates.js) writes its times as every other page does: a <time> by the one time
// rule (app.js towTime) from the server's words for it (<field>_time), not the long written label
// of before; a server without them keeps its label. Runs app.js and updates.js on a small DOM;
// prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const app = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const updates = readFileSync(new URL("../../src/tow/static/updates.js", import.meta.url), "utf8");
const TEXTS = {
  "js.releases.current": "Up to date", "js.releases.checked": "checked {when}", "js.releases.ok": "Updated to {version}",
  "js.releases.started": "started {when}", "js.releases.finished": "finished {when}", "js.releases.supported": "Supported",
  _tz_now: "120",
};
const words = (short) => ({ iso: "2026-01-10T12:00:00+01:00", short, full: "2026-01-10 12:00:00 UTC+01:00" });

const run = async (withWords) => {
  const document = fakeDocument();
  document.body.append(
    build(document, ["script", { id: "tow-i18n" }, JSON.stringify(TEXTS)]),
    build(document, ["span", { "data-release-badge": "", hidden: "" }]),
    build(document, ["p", { "data-release-status": "" }]),
    build(document, ["p", { "data-update-progress": "", hidden: "" }]),
    build(document, ["p", { "data-update-history": "", hidden: "" }]),
  );
  const at = 1768046400; // 2026-01-10 12:00 in TOW's winter
  const latest = { ok: true, available: false, latest: "1.32.0", checked_at: at, checked_at_label: "2026-01-10 12:00:00 CET UTC+01:00" };
  const job = { supported: true, active: false, status: "ok", id: "job", target: "1.32.0", current: "1.32.0",
    started_at: at, started_at_label: "S-label", finished_at: at, finished_at_label: "F-label" };
  if (withWords) Object.assign(latest, { checked_at_time: words("checked-short") }) && Object.assign(job, { started_at_time: words("started-short"), finished_at_time: words("finished-short") });
  const context = {
    document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
    WeakMap, HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, decodeURIComponent, AbortSignal, Intl,
    location: { href: "http://127.0.0.1/settings", origin: "http://127.0.0.1", pathname: "/settings", search: "", hash: "" },
    history: { replaceState() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    localStorage: { getItem: () => null, setItem() {} },
    performance: { now: () => 0 },
    fetch: async (url) => ({ ok: true, json: async () => (String(url).startsWith("/updates/status") ? job : latest) }),
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  };
  context.window = context;
  context.window.addEventListener = () => {};
  vm.createContext(context);
  vm.runInContext(app, context);
  vm.runInContext(updates, context);
  for (let n = 0; n < 8; n++) await new Promise((resolve) => setImmediate(resolve));
  return document;
};

const verdict = {};
{
  const document = await run(true);
  const status = document.querySelector("[data-release-status]");
  assert.equal(status.textContent, "Up to date · checked checked-short");
  const time = status.querySelector("time");
  assert.equal(time.getAttribute("datetime"), "2026-01-10T12:00:00+01:00");
  assert.equal(time.title, "2026-01-10 12:00:00 UTC+01:00");
  const progress = document.querySelector("[data-update-history]");
  assert.equal(progress.textContent, "Updated to 1.32.0 · started started-short · finished finished-short");
  assert.equal(progress.querySelectorAll("time").length, 2);
  assert.equal(document.querySelector("[data-update-progress]").querySelectorAll("time").length, 2);
  verdict.timesByTheOneRule = true;
}
{
  const document = await run(false);
  assert.equal(document.querySelector("[data-release-status]").textContent, "Up to date · checked 2026-01-10 12:00:00 CET UTC+01:00");
  assert.equal(document.querySelector("[data-update-history]").textContent, "Updated to 1.32.0 · started S-label · finished F-label");
  verdict.olderServerKeepsItsLabel = true;
}
console.log(JSON.stringify(verdict));
