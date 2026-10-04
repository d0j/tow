import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// The argument selects a checked-in fixture; it never supplies values to the VM.
const fixtures = new Map([
  ["old-success", { page: "1.22.29", current: "1.22.30", target: "1.22.30", status: "ok", reload: true, key: "js.releases.ok" }],
  ["current-success", { page: "1.22.30", current: "1.22.30", target: "1.22.30", status: "ok", reload: false, key: "js.releases.ok_current" }],
  ["empty-page", { page: "", current: "1.22.30", target: "1.22.30", status: "ok", reload: true, key: "js.releases.ok" }],
  ["legacy-old", { page: "1.22.29", current: "", target: "1.22.30", status: "ok", reload: true, key: "js.releases.ok" }],
  ["legacy-current", { page: "1.22.30", current: "", target: "1.22.30", status: "ok", reload: false, key: "js.releases.ok_current" }],
  ["rollback-old", { page: "1.22.30", current: "1.22.29", target: "1.22.30", status: "rolled_back", reload: true, key: "js.releases.rolled_back" }],
  ["rollback-current", { page: "1.22.29", current: "1.22.29", target: "1.22.30", status: "rolled_back", reload: false, key: "js.releases.rolled_back" }],
  ["rollback-unknown", { page: "1.22.30", current: "", target: "1.22.30", status: "rolled_back", reload: false, key: "js.releases.rolled_back" }],
  ["recovered-old", { page: "1.22.29", current: "1.22.30", target: "1.22.30", status: "recovered", reload: true, key: "js.releases.recovered" }],
  ["recovered-current", { page: "1.22.30", current: "1.22.30", target: "1.22.30", status: "recovered", reload: false, key: "js.releases.recovered" }],
  ["superseded-old", { page: "1.22.31", current: "1.22.30", target: "1.22.29", status: "superseded", reload: true, key: "js.releases.superseded" }],
  ["superseded-current", { page: "1.22.30", current: "1.22.30", target: "1.22.29", status: "superseded", reload: false, key: "js.releases.superseded" }],
  ["failed-current", { page: "1.22.29", current: "1.22.29", target: "1.22.30", status: "failed", reload: false, key: "js.releases.failed" }],
  ["failed-old", { page: "1.22.29", current: "1.22.30", target: "1.22.30", status: "failed", reload: true, key: "js.releases.failed" }],
  ["refused-current", { page: "1.22.29", current: "1.22.29", target: "1.22.30", status: "refused", reload: false, key: "js.releases.refused" }],
  ["interrupted-old", { page: "1.22.29", current: "1.22.30", target: "1.22.30", status: "interrupted", reload: true, key: "js.releases.interrupted" }],
  ["idle-old", { page: "1.22.29", current: "1.22.30", target: "1.22.30", status: "idle", reload: true }],
  ["idle-current", { page: "1.22.30", current: "1.22.30", target: "1.22.30", status: "idle", reload: false }],
  ["active-preparing", { status: "preparing", active: true, reload: false, key: "js.releases.preparing" }],
  ["active-checking", { status: "checking", active: true, reload: false, key: "js.releases.phase_checking" }],
  ["active-rollback", { status: "rolling_back", active: true, reload: false, key: "js.releases.rolling_back" }],
  ["retained-error", { status: "failed", reload: true, error: "Retained failure details" }],
  ["missing-marker", { status: "ok", reload: true, key: "js.releases.ok", noMarker: true }],
  ["null-current", { status: "ok", current: null, reload: true, key: "js.releases.ok" }],
  ["boolean-current", { status: "ok", current: true, reload: true, key: "js.releases.ok" }],
  ["number-current", { status: "ok", current: 123, reload: true, key: "js.releases.ok" }],
  ["rollback-null-current", { status: "rolled_back", current: null, reload: false, key: "js.releases.rolled_back" }],
]);
const selected = fixtures.get(process.argv[2]);
assert.ok(selected, "Unknown fixture name");
const scenario = { page: "1.22.29", current: "1.22.30", target: "1.22.30", mutate: true, ...selected };
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
