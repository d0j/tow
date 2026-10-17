// updates.js on Settings: a page that loses TOW while it knows an update job runs - its own status
// read, or the release area of its poll (sync.js) said so - says "TOW is restarting" instead of
// keeping an older update's result as the current one. A poll TOW answered with a refusal (busy,
// limiting) is not a lost TOW, nor is a lost TOW without a running job. Once TOW answers again the
// page reads the job and follows its result as before. Prints a JSON verdict.
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const SCRIPT = require.resolve("../../src/tow/static/updates.js");
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };
const OLD = { supported: true, active: false, status: "refused", id: "job-1", target: "1.99.0", current: "1.33.1", error_message: "Refused before installation" };

// One Settings page drawn by 1.33.1 whose last update was refused; ``server`` is TOW as it answers
// /updates/status now: a job, or "down" (the connection fails).
const page = async (first = { id: OLD.id, status: OLD.status, active: false }) => {
  const elements = new Map();
  for (const name of ["release-badge", "release-status", "update-progress", "update-support", "update-unavailable",
    "update-reload", "update-version", "update-apply"]) {
    elements.set(`[data-${name}]`, { hidden: true, disabled: false, textContent: "", events: {},
      setAttribute() {}, removeAttribute() {}, addEventListener(event, callback) { this.events[event] = callback; } });
  }
  elements.set("[data-page-version]", { dataset: { pageVersion: "1.33.1" } });
  const it = { server: OLD, readers: {}, pollers: [], reads: 0, reloads: 0, timers: [] };
  Object.assign(globalThis, {
    document: {
      hidden: false,
      querySelector: (selector) => elements.get(selector),
      documentElement: { lang: "en" },
      addEventListener() {},
    },
    t: (key, vars = {}) => `${key}:${JSON.stringify(vars)}`, AbortSignal, Date, URLSearchParams,
    window: {
      location: { reload() { it.reloads++; } }, setInterval() {}, confirm: () => true,
      setTimeout(callback, ms) { it.timers.push({ callback, ms }); return it.timers.length; },
      clearTimeout(id) { if (it.timers[id - 1]) it.timers[id - 1].cleared = true; },
      towSync: {
        on(area, fn) { it.readers[area] = fn; }, onPoll(fn) { it.pollers.push(fn); }, area: () => null,
        client: { backingOff: () => false }, hint() {},
      },
    },
    fetch: async (url) => {
      if (url === "/updates/status") {
        it.reads++;
        if (it.server === "down") throw new TypeError("Failed to fetch");
        return { ok: true, json: async () => ({ ...it.server }) };
      }
      if (it.server === "down") throw new TypeError("Failed to fetch");
      return { ok: true, json: async () => ({ ok: true, available: false, current: "1.33.1", latest: "1.33.1" }) };
    },
  });
  delete require.cache[SCRIPT];
  require(SCRIPT);
  await flush();
  it.progress = () => elements.get("[data-update-progress]").textContent;
  it.release = async (job) => {
    it.readers.release({ ok: true, available: false, current: "1.33.1", latest: "1.33.1", job });
    await flush();
  };
  it.polled = async (ok, status = 0) => {
    it.pollers.forEach((fn) => fn({}, ok, status));
    await flush();
  };
  await it.release(first); // the page's first poll: what it says is the page's start
  return it;
};
const verdict = {};

// The poll brings a new job, and TOW is gone before this page reads its first step (a normal
// ~10 s update): "TOW is restarting", not the refusal before it.
let it = await page();
assert.match(it.progress(), /Refused before installation/);
it.server = "down";
await it.release({ id: "job-2", status: "queued", active: true });
assert.match(it.progress(), /js\.releases\.reconnecting/);
verdict.lostWhileTheNewJobRuns = true;

// TOW answers again: the page reads the job at once and says its result, as before.
it.server = { supported: true, active: false, status: "ok", id: "job-2", target: "1.99.0", current: "1.99.0" };
const before = it.reads;
await it.polled(true, 200);
assert.equal(it.reads, before + 1);
assert.match(it.progress(), /js\.releases\.ok:/);
verdict.followsTheResultWhenBack = true;

// Only the poll knew of the job (its status read answered before the job), and the poll lost TOW.
it = await page({ id: "job-2", status: "stopping", active: true });
assert.match(it.progress(), /Refused before installation/);
it.server = "down";
await it.polled(false, 0);
assert.match(it.progress(), /js\.releases\.reconnecting/);
verdict.pollLostWhileAJobRuns = true;

// TOW refused a poll (busy, limiting) while the job runs: it answers, so no restart is said.
it = await page();
it.server = { supported: true, active: true, status: "preparing", id: "job-2", target: "1.99.0", current: "1.33.1" };
await it.release({ id: "job-2", status: "preparing", active: true });
assert.match(it.progress(), /js\.releases\.preparing/);
await it.polled(false, 503);
await it.polled(false, 429);
assert.match(it.progress(), /js\.releases\.preparing/);
// ... and the next lost connection is a restart.
await it.polled(false, 0);
assert.match(it.progress(), /js\.releases\.reconnecting/);
verdict.refusedPollIsNoRestart = true;

// TOW goes quiet instead (a browser holds the read while the port has no server: Chromium on this
// computer): a read of a running job unanswered after 3 s is a restart too; its late answer wins.
it = await page();
it.server = { supported: true, active: true, status: "stopping", id: "job-2", target: "1.99.0", current: "1.33.1" };
await it.release({ id: "job-2", status: "stopping", active: true });
assert.match(it.progress(), /js\.releases\.stopping/);
let answer;
it.server = "quiet";
globalThis.fetch = ((real) => async (url, options) => {
  if (url === "/updates/status" && it.server === "quiet") return new Promise((resolve) => { answer = resolve; });
  return real(url, options);
})(globalThis.fetch);
it.timers.filter((timer) => timer.ms === 2000 && !timer.cleared).at(-1).callback();
await flush();
assert.match(it.progress(), /js\.releases\.stopping/); // not yet
const quiet = it.timers.filter((timer) => timer.ms === 3000 && !timer.cleared);
assert.equal(quiet.length, 1);
quiet[0].callback();
await flush();
assert.match(it.progress(), /js\.releases\.reconnecting/);
answer({ ok: true, json: async () => ({ supported: true, active: true, status: "checking", id: "job-2", target: "1.99.0", current: "1.99.0" }) });
await flush();
assert.match(it.progress(), /js\.releases\.phase_checking/);
verdict.quietTowIsARestart = true;

// A lost TOW without a running job says nothing of a restart.
it = await page();
it.server = "down";
await it.polled(false, 0);
assert.match(it.progress(), /Refused before installation/);
assert.doesNotMatch(it.progress(), /reconnecting/);
verdict.noJobNoRestart = true;
assert.equal(it.reloads, 0);

console.log(JSON.stringify(verdict));
