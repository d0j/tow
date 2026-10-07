import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
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
Object.assign(globalThis, context);
require("../../src/tow/static/updates.js");
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
job = { ...job, active: false, status: "rolled_back", backup_cleanup_pending: true, started_at: 1700000000, finished_at: 1700000060, error_message: "Safe rollback reason" };
await poll();
assert.match(el("update-progress").textContent, /rolled_back/);
assert.match(el("update-progress").textContent, /Safe rollback reason/);
assert.match(el("update-progress").textContent, /js.releases.backup_cleanup_pending/);
assert.match(el("update-progress").textContent, /js.releases.started/);
assert.match(el("update-progress").textContent, /js.releases.finished/);
assert.equal(reloads, 0);
assert.equal(el("update-reload").hidden, true);
assert.equal(el("release-install").disabled, false);
loseResponse = false;
await click("release-install");
job = { ...job, active: false, status: "ok", backup_cleanup_pending: "false", started_at: "invalid", finished_at: 1e300 };
await poll();
assert.doesNotMatch(el("update-progress").textContent, /Invalid Date|js.releases.started|js.releases.finished/);
assert.doesNotMatch(el("update-progress").textContent, /js.releases.backup_cleanup_pending/);
assert.equal(el("update-reload").hidden, false);
assert.equal(reloads, 0);
await click("update-reload");
assert.equal(reloads, 1);
latest = { ok: false, available: false };
await click("release-check");
assert.match(el("release-status").textContent, /unavailable/);
assert.equal(el("release-check").disabled, false);
assert.equal(el("release-badge").hidden, true);
// Overlay geometry must not turn into an invisible button or cover a real control.
const frames = [];
const listeners = {};
let obstructing = false;
let visible = true;
let controlRect = { left: 0, right: 20, top: 0, bottom: 20 };
let closed = false;
let measuredControls = 0;
// The browser's hit test: a hidden control, or one in a closed accordion, is never hit.
const control = { closest: (selector) => (selector.includes("button") ? control : null) };
const page = { closest: () => null };
const floating = {
  getBoundingClientRect: () => ({ left: 200, right: 300, top: 650, bottom: 680, width: 100, height: 30 }),
  contains: (element) => element === floating,
  classList: { toggle: (_name, value) => { obstructing = value; } },
};
const elementsFromPoint = (x, y) => {
  const hit = visible && !closed && x > controlRect.left && x < controlRect.right && y > controlRect.top && y < controlRect.bottom;
  return hit ? [floating, control, page] : [floating, page];
};
const floatingBadge = { hidden: true, textContent: "" };
const guardContext = {
  ...context,
  document: { querySelector: (selector) => selector === ".app-version" ? floating :
    (selector === "[data-release-badge]" ? floatingBadge : (selector === "main" ? {} : null)),
    querySelectorAll: () => { measuredControls += 1; return []; }, elementsFromPoint,
    documentElement: { lang: "en" }, body: {},
    addEventListener: (name, callback) => { listeners[name] = callback; }, },
  window: { ...context.window, requestAnimationFrame: (callback) => { frames.push(callback); },
    addEventListener: (name, callback) => { listeners[name] = callback; }, },
  ResizeObserver: class { constructor(callback) { listeners.resizeObserver = callback; } observe() {} },
  MutationObserver: class { constructor(callback) { listeners.mutationObserver = callback; } observe() {} },
  fetch: async () => ({ ok: true, json: async () => ({ ok: true, available: false }) }),
};
Object.assign(globalThis, guardContext);
delete require.cache[require.resolve("../../src/tow/static/updates.js")];
require("../../src/tow/static/updates.js");
await flush();
assert.equal(frames.length, 1); // badge discovery and initial layout are coalesced
frames.shift()();
assert.equal(obstructing, false);
controlRect = { left: 240, right: 310, top: 650, bottom: 690 };
listeners.scroll(); listeners.resize();
assert.equal(frames.length, 1);
frames.shift()();
assert.equal(obstructing, true);
visible = false; listeners.toggle(); frames.shift()();
assert.equal(obstructing, false); // hidden controls are not obstacles
visible = true; closed = true; listeners.resizeObserver(); frames.shift()();
assert.equal(obstructing, false); // collapsed details do not obscure the indicator
controlRect = { left: 245, right: 255, top: 660, bottom: 670 }; closed = false; listeners.resize(); frames.shift()();
assert.equal(obstructing, true); // a small control inside the badge, away from its corners
controlRect = { left: 240, right: 310, top: 650, bottom: 690 };
closed = false; listeners.toggle(); frames.shift()();
assert.equal(obstructing, true);
controlRect = { left: 0, right: 20, top: 0, bottom: 20 };
const text = { nodeType: 3 };
listeners.mutationObserver([{ type: "childList", addedNodes: [text], removedNodes: [text] }]);
listeners.mutationObserver([{ type: "attributes", attributeName: "class", oldValue: "bad", target: { getAttribute: () => "bad" } }]);
assert.equal(frames.length, 0); // a countdown's text or an unchanged class does not measure the page
listeners.mutationObserver([{ type: "childList", addedNodes: [{ nodeType: 1 }], removedNodes: [] }]); frames.shift()();
assert.equal(obstructing, false); // dynamic row changes also restore the indicator
controlRect = { left: 300, right: 320, top: 650, bottom: 690 };
listeners.scroll(); frames.shift()();
assert.equal(obstructing, false); // touching edges are not overlaps
assert.equal(measuredControls, 0); // QA 1.24.1: every control of the page was measured on each frame
console.log(JSON.stringify({ confirmed: true, singleJob: true, lostResponse: true, rollback: true, explicitReload: true, offline: true, nonObstructingOverlay: true }));
