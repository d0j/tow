import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

const events = {};
const badge = { hidden: true };
const notice = { hidden: true };
const status = {};
const check = { setAttribute() {}, removeAttribute() {}, addEventListener: (name, callback) => { events[name] = callback; } };
const elements = new Map([["[data-release-badge]", badge], ["[data-release-notice]", notice], ["[data-release-status]", status], ["[data-release-check]", check]]);
let result = { available: true, latest: "99.0.0", ok: true, checks_enabled: true };
let interval;
const requests = [];
Object.assign(globalThis, {
  document: { querySelector: (selector) => elements.get(selector), documentElement: { lang: "en" } },
  t: (key, vars = {}) => key + JSON.stringify(vars), AbortSignal, Date,
  window: { setInterval: (callback) => { interval = callback; } },
  fetch: async (url, options) => { requests.push({ url, options }); return { ok: true, json: async () => result }; },
});
require("../../src/tow/static/updates.js");
const flush = async () => { for (let n = 0; n < 6; n++) await new Promise((resolve) => setImmediate(resolve)); };
await flush();
assert.equal(notice.hidden, false);
assert.match(notice.textContent, /99\.0\.0/);
assert.equal(badge.hidden, true, "Home does not duplicate the top notice in the footer");
result = { available: false, latest: "", ok: false, checks_enabled: false };
interval(); await flush();
assert.equal(notice.hidden, true);
assert.match(status.textContent, /js.releases.disabled/);
assert.doesNotMatch(status.textContent, /unavailable|current/);
events.click(); await flush();
assert.equal(requests.at(-1).url, "/updates/check");
assert.equal(requests.at(-1).options.method, "POST");
console.log(JSON.stringify({ homeNotice: true, noDuplicateBadge: true, disabledStatus: true, manualCheck: true }));
