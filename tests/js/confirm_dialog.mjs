// UI:C1 (1.32.1): a form's question (data-confirm) is asked in the page's own dialog, not the
// browser's confirm: the action's words on its button, Cancel focused first for a destructive one,
// Escape or the backdrop is Cancel, the focus goes back; once confirmed the same button sends the
// same fields once. Runs app.js on a small DOM; prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const document = fakeDocument();
const make = document.createElement;
document.createElement = (tag) => {
  const node = make(tag);
  if (tag === "dialog") {
    node.showModal = () => { if (node.open) throw new Error("already open"); node.open = true; };
    node.close = () => { node.open = false; };
  }
  return node;
};
document.body.append(
  build(document, ["script", { id: "tow-i18n" }, JSON.stringify({ "js.confirm.ok": "Continue", "js.confirm.cancel": "Cancel" })]),
  build(document, ["form", { id: "ed-1", method: "post", action: "/topics/1/edit" }, ["input", { name: "title", value: "Show A" }]]),
  build(document, ["form", { id: "del-1", method: "post", action: "/topics/1/delete", "data-confirm": "Remove this topic?", hidden: "" }]),
  build(document, ["button", { class: "danger", type: "submit", form: "del-1", name: "why", value: "owner" }, "Remove from TOW"]),
  build(document, ["form", { id: "restart", method: "post", action: "/settings/service/restart", "data-confirm": "Restart TOW?", "data-confirm-label": "Restart" },
    ["button", { type: "submit" }, "Restart TOW"]]),
);
const requests = [];
class FakeFormData {
  constructor(form, submitter) { this.form = form; this.submitter = submitter || null; this.entries = new Map(); }
  set(key, value) { this.entries.set(key, value); }
  get(key) { return this.entries.get(key); }
}
const context = {
  document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
  WeakMap, HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, decodeURIComponent, Intl, FormData: FakeFormData,
  location: { href: "http://127.0.0.1/", origin: "http://127.0.0.1", pathname: "/", search: "", hash: "", assign() {} },
  history: { replaceState() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  localStorage: { getItem: () => null, setItem() {} },
  performance: { now: () => 0 },
  fetch: (url, options) => { requests.push({ url: String(url), options }); return new Promise(() => {}); },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  innerWidth: 1280, innerHeight: 800,
};
context.window = { ...context, addEventListener() {}, confirm: () => { throw new Error("the browser's confirm is not used"); } };
vm.runInNewContext(source, context);
const tick = () => new Promise((resolve) => setImmediate(resolve));
const verdict = {};

const del = document.getElementById("del-1");
const remove = document.root.querySelector('button[form="del-1"]');
// requestSubmit: the browser sends a submit event with the same submitter.
let sent = 0;
for (const form of document.root.querySelectorAll("form")) {
  form.requestSubmit = (submitter) => { sent += 1; document.dispatch(form, "submit", { submitter }); };
}
const submit = (form, submitter) => document.dispatch(form, "submit", { submitter });

// A destructive question: the page's dialog, the action's words, Cancel focused; nothing sent.
remove.focus();
const first = submit(del, remove);
assert.equal(first.defaultPrevented, true);
const dialog = document.getElementById("tow-confirm");
assert.ok(dialog && dialog.open);
assert.equal(dialog.querySelector("#tow-confirm-text").textContent, "Remove this topic?");
const ok = dialog.querySelector("[data-confirm-ok]");
const cancel = dialog.querySelector("[data-confirm-cancel]");
assert.equal(ok.textContent, "Remove from TOW");
assert.equal(ok.className, "ghost danger");
assert.equal(cancel.textContent, "Cancel");
assert.equal(document.activeElement, cancel);
assert.equal(requests.length, 0);
verdict.dangerAsksWithCancelFirst = true;

// A second press while it is open asks nothing more and sends nothing.
assert.equal(submit(del, remove).defaultPrevented, true);
assert.equal(requests.length, 0);
verdict.noDoubleSubmit = true;

// Cancel: closed, nothing sent, the focus back on the button.
document.dispatch(cancel, "click");
await tick();
assert.equal(dialog.open, false);
assert.equal(requests.length, 0);
assert.equal(sent, 0);
assert.equal(document.activeElement, remove);
verdict.cancelRestoresFocus = true;

// Escape (the dialog's cancel) and a click on the backdrop are Cancel too.
submit(del, remove);
document.dispatch(dialog, "cancel", { bubbles: false });
await tick();
assert.equal(dialog.open, false);
submit(del, remove);
document.dispatch(dialog, "click");
await tick();
assert.equal(dialog.open, false);
assert.equal(requests.length, 0);
verdict.escapeAndBackdropCancel = true;

// Confirmed: the same button sends the form once, its name and value with it.
submit(del, remove);
document.dispatch(ok, "click");
await tick();
assert.equal(sent, 1);
assert.equal(requests.length, 1);
assert.equal(requests[0].url, "http://127.0.0.1/topics/1/delete");
assert.equal(requests[0].options.body.submitter, remove);
assert.equal(requests[0].options.body.form, del);
assert.equal(del.dataset.confirmed, undefined);
verdict.confirmedSendsOnceWithSubmitter = true;

// A plain question: its own label, the action focused first, not red.
const restartForm = document.getElementById("restart");
const restart = restartForm.querySelector("button");
submit(restartForm, restart);
assert.equal(ok.textContent, "Restart");
assert.equal(ok.className, "");
assert.equal(document.activeElement, ok);
document.dispatch(cancel, "click");
await tick();
verdict.labelAndFocusForAPlainQuestion = true;

// The function itself, for scripts (updates.js, content.js).
const answer = context.window.towConfirm({ text: "Install TOW 1.33.0?", confirmLabel: "Install" });
assert.equal(ok.textContent, "Install");
assert.equal(await context.window.towConfirm({ text: "again" }), false); // one question at a time
document.dispatch(ok, "click");
assert.equal(await answer, true);
const plain = context.window.towConfirm({ text: "Go on?" });
assert.equal(ok.textContent, "Continue");
document.dispatch(cancel, "click");
assert.equal(await plain, false);
verdict.scriptApi = true;

console.log(JSON.stringify(verdict));
