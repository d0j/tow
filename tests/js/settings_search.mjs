// The header search on Settings (1.32.1, C2): typing hides the sections whose search words do not
// match; clearing it shows them all again, each still open or closed as it was. Runs app.js on a
// small DOM; prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const document = fakeDocument();
document.body.append(
  build(document, ["script", { id: "tow-i18n" }, "{}"]),
  build(document, ["details", { class: "hdr-search" }, ["summary", {}, "Search"], ["input", { id: "q", type: "search" }]]),
  build(document, ["details", { class: "settings-section", id: "acc-clients", "data-q": "torrent client qbittorrent" }, ["summary", {}, "Clients"]]),
  build(document, ["details", { class: "settings-section", id: "acc-bots", "data-q": "telegram notifications", open: "" }, ["summary", {}, "Bots"]]),
  build(document, ["details", { class: "settings-section", id: "acc-transfer", "data-q": "backups restore" }, ["summary", {}, "Backups"]]),
  build(document, ["a", { class: "settings-link", href: "/doctor", "data-q": "diagnostics" }, "Diagnostics"]),
);
const q = document.getElementById("q");
const context = {
  document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
  WeakMap, HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, decodeURIComponent, Intl,
  Event: class { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } },
  location: { href: "http://127.0.0.1/settings", origin: "http://127.0.0.1", pathname: "/settings", search: "", hash: "" },
  history: { replaceState() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  localStorage: { getItem: () => null, setItem() {} },
  performance: { now: () => 0 },
  fetch: () => Promise.reject(new Error("no request expected")),
  setInterval: () => 0, clearInterval() {},
  innerWidth: 1280, innerHeight: 800,
};
// The search waits for a pause in typing: here the pause is over at once.
context.setTimeout = (fn) => { fn(); return 1; };
context.clearTimeout = () => {};
context.window = { ...context, addEventListener() {}, confirm: () => true };
vm.runInNewContext(source, context);
const verdict = {};
const sections = () => Object.fromEntries(document.root.querySelectorAll("[data-q]").map((node) => [node.id || node.getAttribute("href"), node.hidden]));

const type = (text) => { q.value = text; q.listeners.input.forEach((fn) => fn({ target: q })); };
type("telegram");
assert.deepEqual(sections(), { "acc-clients": true, "acc-bots": false, "acc-transfer": true, "/doctor": true });
verdict.matchingSectionsShown = true;

type("restore back");
assert.deepEqual(sections(), { "acc-clients": true, "acc-bots": true, "acc-transfer": false, "/doctor": true });
verdict.allWordsMatch = true;

type("");
assert.deepEqual(sections(), { "acc-clients": false, "acc-bots": false, "acc-transfer": false, "/doctor": false });
// Hidden and shown again, each section is as open or closed as it was.
assert.equal(document.getElementById("acc-bots").open, true);
assert.equal(document.getElementById("acc-clients").open, false);
verdict.clearingKeepsTheirState = true;

console.log(JSON.stringify(verdict));
