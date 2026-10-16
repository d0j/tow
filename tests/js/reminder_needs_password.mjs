// §2 (QA Q6): on the computer with TOW a new reminder needs a new password with it. The server
// refuses one without, and the page came back with the saved reminder: what the owner typed was
// gone. app.js says it under the reminder before anything is sent and keeps the typed text;
// keeping or clearing the reminder, or a new password with it, go through. Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const document = fakeDocument();
// No method on the form: app.js's own sending stays out, so a prevented submit is the guard's.
document.body.append(
  build(document, ["script", { id: "tow-i18n" }, "{}"]),
  build(document, ["div", { class: "settings-page" },
    ["details", { class: "settings-section settings-accordion", id: "acc-access", open: "" },
      ["summary", {}, "Network access"],
      ["details", { class: "technical-details access-password", id: "password-change", open: "" },
        ["summary", {}, "Password set · Change"],
        ["form", { action: "/settings/password", class: "settings-form form-grid", "data-return-target": "acc-access" },
          ["input", { id: "lan-password", name: "lan_password", type: "password" }],
          ["input", { id: "lan-hint", name: "hint", type: "text", value: "horse", "aria-describedby": "lan-hint-help" }],
          ["p", { class: "field-error", id: "lan-hint-error", hidden: "" }, "To change the reminder, enter a new password too"],
          ["button", { type: "submit" }, "Save password"]]]]]),
);
const password = document.getElementById("lan-password");
const hint = document.getElementById("lan-hint");
const error = document.getElementById("lan-hint-error");
const form = hint.closest("form");
hint.defaultValue = "horse"; // the saved reminder, as the page brought it
const context = {
  document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
  WeakMap, HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, decodeURIComponent, Intl,
  Event: class { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } },
  location: { href: "http://127.0.0.1/settings?open=access", origin: "http://127.0.0.1", pathname: "/settings", search: "?open=access", hash: "" },
  history: { replaceState() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  localStorage: { getItem: () => null, setItem() {} },
  performance: { now: () => 0 },
  fetch: () => Promise.reject(new Error("no request expected")),
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  innerWidth: 1280, innerHeight: 800,
};
context.window = { ...context, addEventListener() {}, confirm: () => true };
vm.runInNewContext(source, context);
const submit = () => document.dispatch(form, "submit", { submitter: form.querySelector("button") });
const verdict = {};

// A new reminder alone: not sent, said under the reminder, the typed text kept, the focus on the
// new password (failed before: sent, and the refusal brought back the saved reminder).
hint.value = "a different reminder";
const refused = submit();
assert.equal(refused.defaultPrevented, true);
assert.equal(error.hidden, false);
assert.equal(error.getAttribute("role"), "alert"); // a refused submit is announced
assert.equal(hint.value, "a different reminder");
assert.equal(hint.getAttribute("aria-invalid"), "true");
assert.equal(hint.getAttribute("aria-describedby"), "lan-hint-help lan-hint-error");
assert.equal(document.activeElement, password);
verdict.newReminderAloneIsHeld = true;

// With a new password it goes, and the note goes away.
password.value = "new-horse-battery";
document.dispatch(password, "input");
assert.equal(error.hidden, true);
assert.equal(submit().defaultPrevented, false);
assert.equal(hint.hasAttribute("aria-invalid"), false);
assert.equal(hint.getAttribute("aria-describedby"), "lan-hint-help");
verdict.withANewPasswordItGoes = true;

// Kept as it was (spaces aside) or cleared: no password needed.
password.value = "";
for (const value of ["  horse ", ""]) {
  hint.value = value;
  assert.equal(submit().defaultPrevented, false);
  assert.equal(error.hidden, true);
}
verdict.keptOrClearedGoes = true;

console.log(JSON.stringify(verdict));
