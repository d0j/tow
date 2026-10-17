// updates.js on Settings: while an update job runs and the tab is seen, its steps are asked for
// every 2 s (a short update goes through them in ~10 s and a 5 s poll skipped some); hidden, after
// a failed read, while the page's poll waits after a failure (sync.js), or while an install whose
// answer was lost is not yet seen to run, every 5 s as before; nothing once the job has ended.
// Prints a JSON verdict.
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };

const elements = new Map();
for (const name of ["release-badge", "release-status", "update-progress", "update-support", "update-reload",
  "update-version", "update-apply"]) {
  elements.set(`[data-${name}]`, { hidden: true, disabled: false, textContent: "", events: {},
    setAttribute() {}, removeAttribute() {}, addEventListener(event, callback) { this.events[event] = callback; } });
}
const listeners = {};
const document = {
  hidden: false,
  querySelector: (selector) => elements.get(selector),
  documentElement: { lang: "en" },
  addEventListener: (event, callback) => { (listeners[event] ||= []).push(callback); },
};
const timers = new Map();
let timerId = 0;
let backingOff = false;
const state = { job: { supported: true, active: true, status: "stopping", id: "job-1", target: "1.99.0" }, fail: false, reads: 0 };
Object.assign(globalThis, {
  document,
  t: (key, vars = {}) => `${key}:${JSON.stringify(vars)}`, AbortSignal, Date, URLSearchParams,
  window: {
    location: { reload() {} }, setInterval() {}, confirm: () => true,
    setTimeout(callback, ms) { const id = ++timerId; timers.set(id, { callback, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    towSync: { on() {}, onPoll() {}, area: () => null, client: { backingOff: () => backingOff } },
  },
  fetch: async (url) => {
    if (url === "/updates/install") throw new Error("the answer was lost");
    if (url === "/updates/status") {
      state.reads++;
      if (state.fail) return { ok: false, json: async () => ({}) };
      return { ok: true, json: async () => ({ ...state.job }) };
    }
    return { ok: true, json: async () => ({ ok: true, available: false, current: "1.98.0", latest: "1.98.0" }) };
  },
});
require("../../src/tow/static/updates.js");
await flush();

const waits = () => [...timers.values()].map((timer) => timer.ms);
const poll = async () => {
  const [id, timer] = timers.entries().next().value;
  timers.delete(id);
  timer.callback();
  await flush();
};
const shown = (hidden) => {
  document.hidden = hidden;
  (listeners.visibilitychange || []).forEach((callback) => callback());
};
const verdict = {};

// A job running, the tab seen: its steps every 2 s, each poll again.
assert.equal(state.reads, 1);
assert.deepEqual(waits(), [2000]);
state.job = { ...state.job, status: "backup" };
await poll();
assert.match(elements.get("[data-update-progress]").textContent, /js.releases.backup/);
assert.deepEqual(waits(), [2000]);
verdict.everyTwoSecondsWhileActive = true;

// Hidden: back to 5 s at once - also the read already planned in 2 s; seen again: 2 s at once.
shown(true);
assert.deepEqual(waits(), [5000]);
state.job = { ...state.job, status: "installing" };
await poll();
assert.deepEqual(waits(), [5000]);
shown(false);
assert.deepEqual(waits(), [2000]);
verdict.onlyWhileSeen = true;

// A failed read, or the page's poll waiting after a failure: 5 s, never sooner.
state.fail = true;
await poll();
assert.deepEqual(waits(), [5000]);
state.fail = false;
await poll();
assert.deepEqual(waits(), [2000]);
backingOff = true;
await poll();
assert.deepEqual(waits(), [5000]);
backingOff = false;
verdict.keepsTheBackoff = true;

// The job ended: no more fast polling (the page's own poll follows the release area again).
state.job = { ...state.job, active: false, status: "ok", current: "1.99.0" };
await poll();
assert.deepEqual(waits(), []);
shown(true);
shown(false);
assert.deepEqual(waits(), []);
verdict.normalWhenItEnds = true;

// An install whose answer was lost, not yet seen to run: confirmed every 5 s, as before.
elements.get("[data-update-version]").value = "2.0.0";
await elements.get("[data-update-apply]").events.click();
await flush();
assert.match(elements.get("[data-update-progress]").textContent, /js.releases.confirming/);
assert.deepEqual(waits(), [5000]);
state.job = { supported: true, active: true, status: "preparing", id: "job-2", target: "2.0.0" };
await poll();
assert.deepEqual(waits(), [2000]);
verdict.unconfirmedInstallAsBefore = true;

console.log(JSON.stringify(verdict));
