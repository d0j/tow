import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../src/tow/static/updates.js", import.meta.url), "utf8");
const elements = new Map();
for (const name of ["release-badge", "release-status", "release-check", "release-notes", "release-install", "update-progress", "update-support", "update-rollback", "update-version", "update-apply", "update-previous", "update-reload"]) {
  elements.set(`[data-${name}]`, { hidden: true, disabled: false, textContent: "", value: "", events: {}, attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; }, removeAttribute(key) { delete this.attributes[key]; },
    addEventListener(key, callback) { this.events[key] = callback; }, });
}
const el = (name) => elements.get(`[data-${name}]`);
let job = { supported: true, active: false, status: "idle", rollback_version: "1.22.20" };
let latest = { ok: true, available: true, latest: "1.22.21", url: "https://github.com/d0j/tow/releases/tag/v1.22.21" };
let approve = false;
let loseResponse = false;
let reloads = 0;
const fetches = [];
const timers = new Map();
let timerId = 0;
const context = {
  document: { querySelector: (selector) => elements.get(selector), documentElement: { lang: "en" }, hidden: false },
  t: (key, vars = {}) => `${key}:${JSON.stringify(vars)}`, URLSearchParams, AbortSignal, Date,
  window: { confirm: () => approve, location: { reload: () => { reloads++; } },
    setTimeout: (callback) => { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout: (id) => timers.delete(id), setInterval: () => {}, },
  fetch: async (url, options = {}) => {
    fetches.push({ url, method: options.method || "GET", body: options.body });
    if (url === "/updates/install") {
      job = { supported: true, active: true, status: "preparing", id: "one-job", target: "1.22.21" };
      if (loseResponse) throw new Error("connection lost after accepted POST");
      return { ok: true, json: async () => ({ ok: true, id: "one-job" }) };
    }
    return { ok: true, json: async () => url === "/updates/status" ? job : latest };
  },
};
vm.createContext(context);
vm.runInContext(source, context);
const flush = async () => { for (let n = 0; n < 6; n++) await new Promise((resolve) => setImmediate(resolve)); };
const click = async (name) => { el(name).events.click(); await flush(); };
const poll = async () => { const entry = timers.entries().next().value; assert.ok(entry); timers.delete(entry[0]); entry[1](); await flush(); };
await flush();
assert.equal(el("release-badge").hidden, false);
assert.equal(el("release-install").hidden, false);
assert.match(el("release-badge").textContent, /1.22.21/);
await click("release-install");
assert.equal(fetches.filter((entry) => entry.method === "POST").length, 0); // confirmation declined
approve = true;
loseResponse = true;
await click("release-install");
assert.equal(el("release-install").disabled, true); // lost response, job discovered by polling
await click("release-install");
assert.equal(fetches.filter((entry) => entry.url === "/updates/install").length, 1);
job = { ...job, active: false, status: "rolled_back" };
await poll();
assert.match(el("update-progress").textContent, /rolled_back/);
assert.equal(reloads, 0);
assert.equal(el("update-reload").hidden, true);
assert.equal(el("release-install").disabled, false);
loseResponse = false;
await click("release-install");
job = { ...job, active: false, status: "ok" };
await poll();
assert.equal(el("update-reload").hidden, false);
assert.equal(reloads, 0);
await click("update-reload");
assert.equal(reloads, 1);
latest = { ok: false, available: false };
await click("release-check");
assert.match(el("release-status").textContent, /unavailable/);
assert.equal(el("release-check").disabled, false);
assert.equal(el("release-badge").hidden, true);
console.log(JSON.stringify({ confirmed: true, singleJob: true, lostResponse: true, rollback: true, explicitReload: true, offline: true }));
