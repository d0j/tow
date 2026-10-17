// updates.js on Settings: the tab that starts an update tells the other tabs of this browser at
// once (sync.js hint: the scoped BroadcastChannel, "changed" and nothing else), so their next poll
// brings the job now, not up to 5 s later. A refused start says nothing; nor does reading a job.
// Prints a JSON verdict.
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const SCRIPT = require.resolve("../../src/tow/static/updates.js");
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };

// One Settings tab with updates.js loaded afresh; ``install`` is how POST /updates/install answers.
const tab = async (install) => {
  const elements = new Map();
  for (const name of ["release-badge", "release-status", "update-progress", "update-support", "update-reload",
    "update-version", "update-apply"]) {
    elements.set(`[data-${name}]`, { hidden: true, disabled: false, textContent: "", events: {},
      setAttribute() {}, removeAttribute() {}, addEventListener(event, callback) { this.events[event] = callback; } });
  }
  const seen = { hints: [], order: [] };
  Object.assign(globalThis, {
    document: {
      hidden: false,
      querySelector: (selector) => elements.get(selector),
      documentElement: { lang: "en" },
      addEventListener() {},
    },
    t: (key, vars = {}) => `${key}:${JSON.stringify(vars)}`, AbortSignal, Date, URLSearchParams,
    window: {
      location: { reload() {} }, setInterval() {}, confirm: () => true,
      setTimeout() { return 1; }, clearTimeout() {},
      towSync: {
        on() {}, onPoll() {}, area: () => null, client: { backingOff: () => false },
        hint: (...args) => { seen.hints.push(args); seen.order.push("hint"); },
      },
    },
    fetch: async (url) => {
      if (url === "/updates/install") {
        seen.order.push("install");
        if (install === "refused") return { ok: false, json: async () => ({ ok: false, error: "busy" }) };
        if (install === "lost") throw new Error("the answer was lost");
        return { ok: true, json: async () => ({ ok: true, id: "job-1", target: "1.99.0", status: "queued" }) };
      }
      if (url === "/updates/status") {
        seen.order.push("status");
        return { ok: true, json: async () => ({ supported: true, active: false, status: "ok", id: "job-0", target: "1.98.0", current: "1.98.0" }) };
      }
      return { ok: true, json: async () => ({ ok: true, available: false, current: "1.98.0", latest: "1.98.0" }) };
    },
  });
  delete require.cache[SCRIPT];
  require(SCRIPT);
  await flush();
  seen.apply = async (version) => {
    elements.get("[data-update-version]").value = version;
    await elements.get("[data-update-apply]").events.click();
    await flush();
  };
  return seen;
};
const verdict = {};

// Accepted: one hint, without anything in it, once the server took the job. Reading the job
// before (the page's first status read) was no news for the other tabs.
let seen = await tab("accepted");
assert.deepEqual(seen.hints, []);
await seen.apply("1.99.0");
assert.deepEqual(seen.hints, [[]]);
assert.deepEqual(seen.order, ["status", "install", "hint"]);
verdict.acceptedStartHintsSiblings = true;

// The answer was lost: the job may run, so the other tabs look too (they find out themselves).
seen = await tab("lost");
await seen.apply("1.99.0");
assert.deepEqual(seen.hints, [[]]);
verdict.lostAnswerHintsSiblings = true;

// Refused: nothing changed, nothing said.
seen = await tab("refused");
await seen.apply("1.99.0");
assert.deepEqual(seen.hints, []);
assert.ok(seen.order.includes("install"));
verdict.refusedStartSaysNothing = true;

console.log(JSON.stringify(verdict));
