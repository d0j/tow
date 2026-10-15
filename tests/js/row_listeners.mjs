// A Home row drawn again (checks.js puts a fresh row in place of the old one) behaves like a row the
// page opened with: its progress link opens the downloads dialog, its icons keep their clicks to
// themselves, its Check and Pause forms are sent by the page, its edit panel loads when it opens.
// Runs app.js on a small DOM; prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

const rowSpec = (stamp) => ["div", { class: "row-wrap", id: "row-a", "data-subject": "Show", "data-q": "show", "data-stamp": stamp },
  ["details", { class: "row-edit" }, ["summary", {}, ["span", { class: "dot lg ok" }], "Show"],
    ["div", { class: "edit-panel", "data-edit-src": "/topics/a/edit-panel" }, ["p", {}, ["a", { href: "/topics/a/edit" }, "Open"]]]],
  ["span", { class: "row-progress" }, ["a", { class: "download-details", href: "/topics/a/downloads.json" }, "3/10"]],
  ["span", { class: "row-ops" },
    ["form", { method: "post", action: "/topics/a/pause" }, ["button", { class: "row-ico" }, "Pause"]],
    ["form", { method: "post", action: "/topics/a/check" }, ["button", { class: "row-ico" }, "Check"]]]];

const document = fakeDocument();
const list = build(document, ["div", { id: "topics" }, rowSpec("first")]);
const dialog = build(document, ["dialog", { id: "download-pop" }, ["div", { id: "download-body" }], ["span", { id: "download-title" }]]);
let modal = 0;
dialog.showModal = () => { modal += 1; };
dialog.close = () => {};
document.body.append(list, dialog);
const fetched = [];
class FormData { constructor() { this.fields = {}; } set(name, value) { this.fields[name] = value; } }
const context = {
  document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
  WeakMap, HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, FormData, decodeURIComponent,
  location: { href: "http://127.0.0.1/", origin: "http://127.0.0.1", pathname: "/", search: "", hash: "" },
  history: { replaceState() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  localStorage: { getItem: () => null, setItem() {} },
  performance: { now: () => 0 },
  fetch: (url) => {
    fetched.push(String(url));
    return Promise.resolve({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({ ok: true, items: [] }), text: async () => "" });
  },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
};
context.window = { ...context, addEventListener() {}, confirm: () => true, location: { ...context.location, assign() {} } };
vm.runInNewContext(source, context);

// checks.js's swap: a fresh row in place of the old one, then the page is told.
const old = list.querySelector("#row-a");
const fresh = build(document, rowSpec("fresh"));
old.replaceWith(fresh);
context.window.towRows.replaced(old, fresh);
const verdict = {};

// Its progress link opens the dialog (not a page of JSON).
const linkClick = document.dispatch(fresh.querySelector("a.download-details"), "click");
await settle();
assert.equal(linkClick.defaultPrevented, true);
assert.equal(modal, 1);
assert.ok(fetched.includes("http://127.0.0.1/topics/a/downloads.json") || fetched.some((url) => url.endsWith("/topics/a/downloads.json")));
verdict.downloadsDialog = true;

// A click on its icons stays with them (as on the rows the page opened with).
const iconClick = document.dispatch(fresh.querySelector(".row-ops button"), "click");
assert.equal(iconClick.stopped, true);
verdict.iconsKeepTheirClicks = true;

// Its Check is sent by the page.
const form = fresh.querySelector('form[action="/topics/a/check"]');
document.dispatch(form, "submit", { submitter: form.querySelector("button") });
await settle();
assert.ok(fetched.some((url) => url.endsWith("/topics/a/check")));
verdict.formsSent = true;

// Its edit panel loads when the row opens.
fresh.querySelector("details").open = true;
await settle();
assert.ok(fetched.includes("/topics/a/edit-panel"));
verdict.editPanelLoads = true;

console.log(JSON.stringify(verdict));
