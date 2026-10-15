// A failed action of app.js is a message (window.towMessages.showMessage) about its topic, never a
// paragraph inside the row's icon form (it covered the row); a lost answer to a change says the
// result is not confirmed and is never sent again by itself. Prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

const page = (reply) => {
  const document = fakeDocument();
  const row = build(document, ["div", { class: "row-wrap", id: "row-a", "data-subject": "Show A" },
    ["details", { class: "row-edit" }, ["summary", {}, "Show A"],
      ["div", { class: "edit-panel", "data-edit-src": "/topics/a/edit-panel" }, ["p", { class: "sub" }, ["a", { href: "/topics/a/edit" }, "Open"]]]],
    ["span", { class: "row-ops" }, ["form", { method: "post", action: "/topics/a/check" }, ["button", { class: "row-ico" }, "Check"]]]]);
  document.body.append(row);
  const shown = [];
  const fetched = [];
  const context = {
    document, URL, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
    HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement,
    FormData: class { constructor() {} },
    location: { href: "http://127.0.0.1/?s=name", origin: "http://127.0.0.1", pathname: "/", search: "?s=name", hash: "" },
    history: { replaceState() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    localStorage: { getItem: () => null, setItem() {} },
    performance: { now: () => 0 },
    fetch: (url) => { fetched.push(String(url)); return reply(String(url)); },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  };
  context.window = {
    ...context, addEventListener() {}, confirm: () => true, location: context.location,
    towMessages: { showMessage: (record) => shown.push(record) },
  };
  vm.runInNewContext(source, context);
  const submit = async () => {
    const form = row.querySelector("form");
    const button = form.querySelector("button");
    document.dispatch(form, "submit", { submitter: button });
    await settle();
    return { form, button };
  };
  return { document, row, shown, fetched, submit };
};
const verdict = {};

// The answer is lost (the connection dropped): the change may have been made - not confirmed,
// with a way to look (reload), and never sent again by itself.
{
  const test = page(() => Promise.reject(new TypeError("Failed to fetch")));
  const { form, button } = await test.submit();
  assert.equal(test.shown.length, 1);
  const [message] = test.shown;
  assert.equal(message.kind, "err");
  assert.equal(message.subject, "Show A");
  assert.equal(message.text, "js.messages.unconfirmed");
  assert.equal(message.href, "/?s=name");
  assert.equal(message.link, "js.messages.reload");
  assert.equal(form.querySelector("[data-form-error]"), null); // nothing laid over the row
  assert.equal(form.querySelectorAll("p").length, 0);
  assert.equal(button.disabled, false);
  assert.equal(test.fetched.length, 1);
}
verdict.lostAnswerUnconfirmed = true;

// TOW refused it and said why: that reason, about the topic.
{
  const test = page(() => Promise.resolve({
    ok: false, status: 409, headers: { get: () => "application/json" },
    text: async () => JSON.stringify({ error: "An update is being installed" }),
  }));
  await test.submit();
  assert.equal(test.shown.length, 1);
  assert.equal(test.shown[0].text, "js.submit.failed");
  assert.equal(test.shown[0].subject, "Show A");
}
verdict.refusalSaysWhy = true;

// A server error page: its HTML is never shown; the result is not confirmed.
{
  const test = page(() => Promise.resolve({
    ok: false, status: 500, headers: { get: () => "text/html" },
    text: async () => "<html><body><script>x()</script>Internal Server Error</body></html>",
  }));
  await test.submit();
  assert.equal(test.shown.length, 1);
  assert.equal(test.shown[0].text, "js.messages.unconfirmed");
  assert.ok(!JSON.stringify(test.shown[0]).includes("<"));
}
verdict.noResponseHtml = true;

// The edit panel does not load: a message about the topic with the panel as a page of its own.
{
  const test = page(() => Promise.reject(new TypeError("Failed to fetch")));
  const details = test.row.querySelector("details");
  details.open = true;
  await settle();
  assert.equal(test.shown.length, 1);
  assert.equal(test.shown[0].subject, "Show A");
  assert.equal(test.shown[0].text, "js.edit.load_failed");
  assert.equal(test.shown[0].href, "/topics/a/edit");
  assert.equal(details.querySelector("[role=alert]"), null);
  assert.ok(details.querySelector(".edit-panel[data-edit-src]")); // opening it again tries again
}
verdict.editPanelFailure = true;

console.log(JSON.stringify(verdict));
