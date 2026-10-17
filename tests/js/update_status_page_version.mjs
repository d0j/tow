// updates.js on Settings: the status line under "Check for updates" speaks for the page, not only
// for the server. A page drawn by an older TOW than the one running now (it was not reloaded after
// an update) says to reload it to use the new version, never "You have the latest version" beside
// the card that says to reload (1.33.1 QA O-2). Prints a JSON verdict.
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };

const load = async (pageVersion, answer) => {
  const elements = new Map();
  for (const name of ["release-badge", "release-status", "release-check"]) {
    elements.set(`[data-${name}]`, { hidden: true, textContent: "", events: {},
      setAttribute() {}, removeAttribute() {}, addEventListener(event, callback) { this.events[event] = callback; } });
  }
  if (pageVersion !== null) elements.set("[data-page-version]", { dataset: { pageVersion } });
  Object.assign(globalThis, {
    document: { querySelector: (selector) => elements.get(selector), documentElement: { lang: "en" }, hidden: false },
    t: (key, vars = {}) => `${key}:${JSON.stringify(vars)}`, AbortSignal, Date,
    window: { setTimeout() {}, clearTimeout() {}, setInterval() {} },
    fetch: async () => ({ ok: true, json: async () => ({ checks_enabled: true, checked_at: 0, ...answer }) }),
  });
  delete require.cache[require.resolve("../../src/tow/static/updates.js")];
  require("../../src/tow/static/updates.js");
  await flush();
  return elements.get("[data-release-status]").textContent;
};
const verdict = {};

// The page is 1.33.0, TOW runs 1.99.0, the latest: reload words, with the version it opens.
let said = await load("1.33.0", { ok: true, available: false, comparable: true, current: "1.99.0", latest: "1.99.0" });
assert.equal(said, 'js.releases.reload_to_use:{"version":"1.99.0"}');
verdict.oldPageSaysReload = true;

// The page is the running version: "You have the latest version", as before.
said = await load("1.99.0", { ok: true, available: false, comparable: true, current: "1.99.0", latest: "1.99.0" });
assert.equal(said, "js.releases.current:{}");
// No page version to compare (an older page), or none from the server: as before.
said = await load(null, { ok: true, available: false, comparable: true, current: "1.99.0", latest: "1.99.0" });
assert.equal(said, "js.releases.current:{}");
said = await load("1.33.0", { ok: true, available: false, comparable: true, latest: "1.99.0" });
assert.equal(said, "js.releases.current:{}");
verdict.currentPageUnchanged = true;

// A newer release, a failed check and a build that cannot be compared still say so.
said = await load("1.33.0", { ok: true, available: true, comparable: true, current: "1.99.0", latest: "2.0.0" });
assert.equal(said, 'js.releases.available:{"version":"2.0.0"}');
said = await load("1.33.0", { ok: false, available: false, comparable: true, current: "1.99.0", latest: "" });
assert.equal(said, "js.releases.unavailable:{}");
said = await load("1.33.0", { ok: true, available: false, comparable: false, current: "1.99.0+dev", latest: "1.99.0" });
assert.equal(said, "js.releases.unknown_build:{}");
verdict.otherAnswersUnchanged = true;

console.log(JSON.stringify(verdict));
