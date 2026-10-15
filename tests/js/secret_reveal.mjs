// The eye of a secret field (_secret.html, app.js): it shows what was typed and hides it again,
// says its state and what it does next, keeps the focus and the caret where they were, works for
// a panel loaded later (delegated), hides the secret again when its form is sent or reset, and
// never asks the server for anything. Runs app.js on a small DOM; prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const TEXTS = { "web.secret.show": "Show “{field}”", "web.secret.hide": "Hide “{field}”" };

const secret = (id, name) => ["span", { class: "secret" },
  ["input", { type: "password", id, name: id, autocomplete: "off" }],
  ["button", { type: "button", class: "ico reveal", "data-reveal": id, "data-reveal-name": name, "aria-controls": id,
    "aria-pressed": "false", "aria-label": `Show “${name}”`, title: `Show “${name}”` }, ["svg", {}, ["use", { href: "#i-eye" }]]]];

const document = fakeDocument();
const form = build(document, ["form", { method: "post", action: "/settings/notify" },
  ["label", { for: "tok" }, "Bot token"], secret("tok", "Bot token")]);
document.body.append(form, build(document, ["script", { id: "tow-i18n" }, JSON.stringify(TEXTS)]));
const fetched = [];
class FormData { constructor() { this.fields = {}; } set(name, value) { this.fields[name] = value; } }
const context = {
  document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
  WeakMap, HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, FormData, decodeURIComponent,
  location: { href: "http://127.0.0.1/settings", origin: "http://127.0.0.1", pathname: "/settings", search: "", hash: "" },
  history: { replaceState() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  localStorage: { getItem: () => null, setItem() {} },
  performance: { now: () => 0 },
  fetch: (url) => {
    fetched.push(String(url));
    return Promise.resolve({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({ redirect: "/settings" }), text: async () => "" });
  },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
};
context.window = { ...context, addEventListener() {}, confirm: () => true, location: { ...context.location, assign() {} } };
vm.runInNewContext(source, context);

const $ = (selector) => document.root.querySelector(selector);
const field = $("#tok");
const eye = $('[data-reveal="tok"]');
const verdict = {};

// Typed, then shown: the field shows its text; the eye says it is pressed and what it does next.
field.value = "123:abc";
field.selectionStart = 2;
field.selectionEnd = 5;
field.selectionDirection = "forward";
let caret = null;
field.setSelectionRange = (start, end, direction) => { caret = [start, end, direction]; };
field.focus();
const press = document.dispatch(eye, "pointerdown");
assert.equal(press.defaultPrevented, true); // a click does not take the focus from the field
const click = document.dispatch(eye.querySelector("use"), "click");
assert.equal(click.defaultPrevented, true);
assert.equal(field.type, "text");
assert.equal(field.value, "123:abc");
assert.equal(eye.getAttribute("aria-pressed"), "true");
assert.equal(eye.getAttribute("aria-label"), "Hide “Bot token”");
assert.equal(eye.title, "Hide “Bot token”");
assert.equal(eye.querySelector("use").getAttribute("href"), "#i-eye-off");
verdict.shows = true;

// The focus and the caret stay in the field.
assert.equal(document.activeElement, field);
assert.deepEqual(caret, [2, 5, "forward"]);
verdict.keepsFocusAndCaret = true;

// Pressed again: hidden, and said so.
document.dispatch(eye, "click");
assert.equal(field.type, "password");
assert.equal(eye.getAttribute("aria-pressed"), "false");
assert.equal(eye.getAttribute("aria-label"), "Show “Bot token”");
assert.equal(eye.querySelector("use").getAttribute("href"), "#i-eye");
verdict.hidesAgain = true;

// From the keyboard (the eye has the focus): the focus stays on the eye.
eye.focus();
document.dispatch(eye, "click");
assert.equal(field.type, "text");
assert.equal(document.activeElement, eye);
document.dispatch(eye, "click");
verdict.keyboardKeepsTheEyesFocus = true;

// A panel loaded after the page (an edit panel): its eye works too; without data-reveal-name the
// field's label names it.
const panel = build(document, ["form", { method: "post", action: "/sites/x" },
  ["label", { for: "site-pass" }, "Password"], secret("site-pass", "")]);
panel.querySelector('[data-reveal="site-pass"]').removeAttribute("data-reveal-name");
document.body.append(panel);
const late = panel.querySelector('[data-reveal="site-pass"]');
document.dispatch(late, "click");
assert.equal($("#site-pass").type, "text");
assert.equal(late.getAttribute("aria-label"), "Hide “Password”");
verdict.delegated = true;

// Reset (Cancel) and sent: the secret is hidden again first.
document.dispatch(panel, "reset");
assert.equal($("#site-pass").type, "password");
assert.equal(late.getAttribute("aria-pressed"), "false");
document.dispatch(eye, "click");
assert.equal(field.type, "text");
document.dispatch(form, "submit", { submitter: null });
assert.equal(field.type, "password");
assert.equal(eye.getAttribute("aria-pressed"), "false");
verdict.hiddenWhenSentOrReset = true;

// Only the form's own submit asked the server; the eye never did (a saved secret is never shown).
assert.deepEqual(fetched.filter((url) => !url.endsWith("/settings/notify")), []);
verdict.neverFetches = true;

console.log(JSON.stringify(verdict));
