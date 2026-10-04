import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

// Run only checked-in fixtures; no external arguments or dynamically evaluated source.
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
  ["external-manual", { page: "1.22.29", current: "1.22.29", target: "1.22.29", status: "ok", reload: true, key: "js.releases.ok", trigger: "manual" }],
  ["external-hourly", { page: "1.22.29", current: "1.22.29", target: "1.22.29", status: "ok", reload: true, key: "js.releases.ok", trigger: "hourly" }],
  ["hidden-hourly", { page: "1.22.29", current: "1.22.29", target: "1.22.29", status: "ok", reload: false, key: "js.releases.ok_current", trigger: "hidden" }],
]);
for (const selected of fixtures.values()) {
  const scenario = { page: "1.22.29", current: "1.22.30", target: "1.22.30", mutate: true, ...selected };
  const elements = new Map();
  for (const name of ["release-badge", "release-status", "release-check", "update-progress", "update-support", "update-reload", "update-log", "update-log-text"]) {
    elements.set(`[data-${name}]`, { hidden: true, textContent: "", open: true, events: {},
      setAttribute() {}, removeAttribute() {},
      addEventListener(name, callback) { this.events[name] = callback; } });
  }
  const page = { dataset: { pageVersion: scenario.page } };
  if (!scenario.noMarker) elements.set("[data-page-version]", page);
  const job = { supported: true, active: scenario.active || false, status: scenario.status, id: "test-job",
    current: scenario.current, target: scenario.target, started_at: 1700000000, finished_at: 1700000010,
    error_message: scenario.error || "" };
  let reloads = 0;
  const posts = [];
  let statusReads = 0;
  let hourly;
  const document = { querySelector: selector => elements.get(selector), documentElement: { lang: "en" }, hidden: false };
  Object.assign(globalThis, {
    document,
    t: (key, vars = {}) => `${key}:${JSON.stringify(vars)}`, AbortSignal, Date,
    window: { location: { reload() { reloads++; } }, setTimeout() {}, clearTimeout() {}, setInterval(callback) { hourly = callback; } },
    fetch: async (url, options = {}) => {
      if (options.method === "POST") posts.push(url);
      if (url === "/updates/status") statusReads++;
      // Even a later marker change is not the identity of the originally loaded document.
      if (scenario.mutate) page.dataset.pageVersion = scenario.current;
      return { ok: true, json: async () => url === "/updates/status" ? job :
        url === "/updates/log" ? { text: "retained operation log" } :
        { ok: true, available: false, current: scenario.current, latest: scenario.current } };
    },
  });
  // Load the fixed, checked-in module normally; never evaluate file contents as a string.
  delete require.cache[require.resolve("../../src/tow/static/updates.js")];
  require("../../src/tow/static/updates.js");
  for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));
  if (scenario.trigger) {
    assert.equal(elements.get("[data-update-reload]").hidden, true);
    job.current = "1.22.30";
    job.target = "1.22.30";
    if (scenario.trigger === "manual") elements.get("[data-release-check]").events.click();
    else { document.hidden = scenario.trigger === "hidden"; hourly(); }
    for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));
    assert.equal(statusReads, scenario.trigger === "hidden" ? 1 : 2);
  }
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
  assert.deepEqual(posts, scenario.trigger === "manual" ? ["/updates/check"] : []);
  assert.ok(!posts.includes("/updates/install")); // observing never installs anything
}
console.log(JSON.stringify({ scenarios: fixtures.size, pageVersion: true, explicitReload: true, datesAndLogRetained: true, noInstallation: true }));
