import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const scenario = JSON.parse(process.argv[2]);
const source = readFileSync(new URL("../../src/tow/static/updates.js", import.meta.url), "utf8");
const elements = new Map();
for (const name of ["release-badge", "release-status", "update-progress", "update-support", "update-reload", "update-log", "update-log-text"]) {
  elements.set(`[data-${name}]`, { hidden: true, textContent: "", open: true, events: {},
    addEventListener(name, callback) { this.events[name] = callback; } });
}
const page = { dataset: { pageVersion: scenario.page } };
if (!scenario.noMarker) elements.set("[data-page-version]", page);
const job = { supported: true, active: scenario.active || false, status: scenario.status, id: "test-job",
  current: scenario.current, target: scenario.target, started_at: 1700000000, finished_at: 1700000010,
  error_message: scenario.error || "" };
let reloads = 0;
const posts = [];
vm.runInNewContext(source, {
  document: { querySelector: selector => elements.get(selector), documentElement: { lang: "en" } },
  t: (key, vars = {}) => `${key}:${JSON.stringify(vars)}`, AbortSignal, Date,
  window: { location: { reload() { reloads++; } }, setTimeout() {}, clearTimeout() {}, setInterval() {} },
  fetch: async (url, options = {}) => {
    if (options.method === "POST") posts.push(url);
    // Even a later marker change is not the identity of the originally loaded document.
    if (scenario.mutate) page.dataset.pageVersion = scenario.current;
    return { ok: true, json: async () => url === "/updates/status" ? job :
      url === "/updates/log" ? { text: "retained operation log" } :
      { ok: true, available: false, current: scenario.current, latest: scenario.current } };
  },
});
for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));
const progress = elements.get("[data-update-progress]");
const reload = elements.get("[data-update-reload]");
assert.equal(reload.hidden, !scenario.reload);
if (scenario.key) {
  assert.ok(progress.textContent.includes(scenario.key + ":"), progress.textContent);
}
if (scenario.key || scenario.error) {
  assert.ok(progress.textContent.includes("js.releases.started:"));
  assert.ok(progress.textContent.includes("js.releases.finished:"));
}
if (scenario.error) assert.ok(progress.textContent.includes(scenario.error));
assert.equal(elements.get("[data-update-log-text]").textContent, "retained operation log");
assert.equal(reloads, 0); // never reload automatically, including after rollback
assert.deepEqual(posts, []); // observing a completed operation never installs anything
console.log(JSON.stringify({ pageVersion: true, explicitReload: true, datesAndLogRetained: true, noInstallation: true }));
