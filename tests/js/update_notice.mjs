import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

const events = {};
const badge = { hidden: true };
const status = {};
const check = { setAttribute() {}, removeAttribute() {}, addEventListener: (name, callback) => { events[name] = callback; } };
const elements = new Map([["[data-release-badge]", badge], ["[data-release-status]", status], ["[data-release-check]", check]]);
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
// Home has no strip of its own any more: the footer's badge says it on every page.
assert.equal(badge.hidden, false);
assert.match(badge.textContent, /99\.0\.0/);
result = { available: false, latest: "", ok: false, checks_enabled: false };
interval(); await flush();
assert.equal(badge.hidden, true);
// U1: automatic checks off and never checked: nothing under the unticked box (it said "off" twice).
assert.equal(status.textContent, "");
events.click(); await flush();
assert.equal(requests.at(-1).url, "/updates/check");
assert.equal(requests.at(-1).options.method, "POST");
// A check asked by hand still says its result, a failed one too.
assert.match(status.textContent, /js.releases.unavailable/);
console.log(JSON.stringify({ footerBadge: true, badgeHidesWhenCurrent: true, disabledStatusQuiet: true, manualCheck: true }));
