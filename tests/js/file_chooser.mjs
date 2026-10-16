// B2 (1.32.1): a file field in the page's language (_file.html). Once app.js runs, the wrapper is
// ready (the browser's field hidden but focusable), "No file chosen" becomes the chosen file's
// name as text, and the buttons that need a file wait for one - only then: without the script they
// stay enabled. A reset or a script that emptied the field puts the words back. Runs app.js on a
// small DOM; prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const document = fakeDocument();
document.body.append(
  build(document, ["script", { id: "tow-i18n" }, "{}"]),
  build(document, ["form", { id: "import", method: "post", action: "/settings/portable/import" },
    ["label", { for: "backup-file" }, "TOW file"],
    ["span", { class: "file-pick", "data-file-pick": "" },
      ["input", { type: "file", id: "backup-file", class: "file-native", name: "backup_file" }],
      ["label", { for: "backup-file", class: "btn ghost file-button", "aria-hidden": "true" }, "Choose a file"],
      ["span", { class: "file-name", "data-file-name": "" }, "No file chosen"]],
    ["button", { type: "submit", name: "operation", value: "check", "data-needs-file": "" }, "Check"],
    ["button", { type: "submit", name: "operation", value: "restore", "data-needs-file": "" }, "Restore"],
    ["button", { type: "button" }, "Other"]]),
);
const form = document.getElementById("import");
const field = document.getElementById("backup-file");
const [check, restore, other] = form.querySelectorAll("button");
field.form = form;
form.elements = [field, check, restore, other];
let chosen = [];
Object.defineProperty(field, "files", { get: () => chosen });
// Without the script: nothing is disabled, the wrapper is not ready (the browser's field shows).
assert.equal(check.disabled, false);
assert.equal(form.querySelector("[data-file-pick]").hasAttribute("data-ready"), false);

const timers = [];
const context = {
  document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
  WeakMap, HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, decodeURIComponent, Intl,
  location: { href: "http://127.0.0.1/settings", origin: "http://127.0.0.1", pathname: "/settings", search: "", hash: "" },
  history: { replaceState() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  localStorage: { getItem: () => null, setItem() {} },
  performance: { now: () => 0 },
  fetch: () => Promise.reject(new Error("no request expected")),
  setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  innerWidth: 1280, innerHeight: 800,
};
context.window = { ...context, addEventListener() {}, confirm: () => true };
vm.runInNewContext(source, context);
const verdict = {};
const pick = form.querySelector("[data-file-pick]");
const name = form.querySelector("[data-file-name]");

// Ready: the dependent buttons wait for a file, the other one does not.
assert.equal(pick.hasAttribute("data-ready"), true);
assert.equal(check.disabled, true);
assert.equal(restore.disabled, true);
assert.equal(other.disabled, false);
verdict.waitsForAFile = true;

// A chosen file: its name as text, the buttons enabled.
chosen = [{ name: "tow-2026-10-16 <b>.towx" }];
document.dispatch(field, "change");
assert.equal(name.textContent, "tow-2026-10-16 <b>.towx");
assert.equal(name.children.length, 0);
assert.equal(name.classList.contains("chosen"), true);
assert.equal(check.disabled, false);
assert.equal(restore.disabled, false);
verdict.showsTheName = true;

// A reset (Cancel) empties it: the words and the waiting come back once the reset is done.
chosen = [];
document.dispatch(form, "reset");
timers.splice(0).forEach((fn) => fn());
assert.equal(name.textContent, "No file chosen");
assert.equal(check.disabled, true);
verdict.resetPutsWordsBack = true;

// A script that emptied the field itself says so.
chosen = [{ name: "a.torrent" }];
document.dispatch(field, "change");
chosen = [];
context.window.towFiles.sync(field);
assert.equal(name.textContent, "No file chosen");
assert.equal(restore.disabled, true);
verdict.scriptSync = true;

console.log(JSON.stringify(verdict));
