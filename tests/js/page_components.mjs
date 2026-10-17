// Settings' scripts as components of pages.js's lifecycle (app.js on a small DOM): each section's
// listeners are attached once however often the page mounts, removed when pages.js takes the
// section out, and a section drawn again (another device's change) gets them anew - its form tracks
// unsaved words again, its accordion still closes the others. Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const pages = readFileSync(new URL("../../src/tow/static/pages.js", import.meta.url), "utf8");
const app = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const document = fakeDocument();
const section = (value) => build(document, ["details", { class: "settings-section settings-accordion", id: "acc-intervals", open: "" },
  ["summary", {}, "Checks"],
  ["form", { method: "post", action: "/settings/interval", class: "settings-form" },
    ["input", { id: "interval_min", name: "interval_min", value }],
    ["p", { class: "unsaved-state", hidden: "" }, "Unsaved"]]]);
const other = build(document, ["details", { class: "settings-section settings-accordion", id: "acc-bots" }, ["summary", {}, "Bots"]]);
const page = build(document, ["div", { class: "settings-page" }, ["div", { id: "settings-acc" }]]);
page.querySelector("#settings-acc").append(section("60"), other);
document.body.append(build(document, ["script", { id: "tow-i18n" }, "{}"]), page);
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
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  innerWidth: 1280, innerHeight: 800,
};
context.window = { ...context, addEventListener() {}, removeEventListener() {}, confirm: () => true };
vm.runInNewContext(pages, context);
vm.runInNewContext(app, context);
const life = context.window.towLifecycle;
const verdict = {};
const count = (element, type) => (element.listeners[type] || []).length;

// The page's own: one listener each, however often it is mounted.
{
  const form = page.querySelector("form.settings-form");
  const accordion = page.querySelector("#acc-intervals");
  assert.equal(count(form, "input"), 1);
  assert.equal(count(accordion, "toggle"), 1);
  assert.equal(form.dataset.dirty, "0");
  assert.equal(life.mount(document.root), 0);
  life.mount(accordion);
  assert.equal(count(form, "input"), 1);
  assert.equal(count(accordion, "toggle"), 1);
  verdict.mountedOnceEach = true;
}

// Taken out: its listeners go (once); drawn again: the fresh section has them, its form tracks
// unsaved words from what it shows now.
{
  const old = page.querySelector("#acc-intervals");
  const oldForm = old.querySelector("form");
  assert.ok(life.unmount(old) >= 2);
  assert.equal(life.unmount(old), 0);
  assert.equal(count(oldForm, "input"), 0);
  assert.equal(count(old, "toggle"), 0);
  const fresh = section("15");
  old.replaceWith(fresh);
  life.mount(fresh);
  const form = fresh.querySelector("form");
  assert.equal(count(form, "input"), 1);
  assert.equal(count(fresh, "toggle"), 1);
  assert.equal(form.dataset.dirty, "0");
  const field = fresh.querySelector("#interval_min");
  field.value = "20";
  document.dispatch(field, "input");
  assert.equal(form.dataset.dirty, "1");
  assert.equal(fresh.querySelector(".unsaved-state").hidden, false);
  verdict.remountedAfterReplace = true;
  // Its accordion still closes the others (read when it opens, not kept from the page's start).
  other.open = true;
  fresh.open = false;
  fresh.open = true;
  assert.equal(other.open, false);
  verdict.accordionReadsTheSectionsNow = true;
}

console.log(JSON.stringify(verdict));
