// app.js sends a Check with the action id checks.js keeps before sending; an accepted check is
// followed where the page is (no page opens, the button is back), a lost answer leaves it to the
// lookup by that id (never sent again), and a page answer (a refusal) forgets it. Prints a JSON
// verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

const page = (reply) => {
  const document = fakeDocument();
  const row = build(document, ["div", { class: "row-wrap", id: "row-a", "data-subject": "Show A" },
    ["span", { class: "row-ops" }, ["form", { method: "post", action: "/topics/a/check" }, ["button", { class: "row-ico" }, "Check"]]]]);
  document.body.append(row);
  const shown = [];
  const sent = [];
  const assigned = [];
  const checks = { begun: [], acknowledged: [], forgotten: [] };
  class FormData {
    constructor() { this.fields = {}; }
    set(name, value) { this.fields[name] = value; }
  }
  const context = {
    document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
    HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, FormData,
    location: { href: "http://127.0.0.1/?s=name", origin: "http://127.0.0.1", pathname: "/", search: "?s=name", hash: "" },
    history: { replaceState() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    localStorage: { getItem: () => null, setItem() {} },
    performance: { now: () => 0 },
    fetch: (url, init) => { sent.push({ url: String(url), fields: { ...init.body.fields } }); return reply(); },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    decodeURIComponent,
  };
  context.window = {
    ...context, addEventListener() {}, confirm: () => true,
    location: { ...context.location, assign: (url) => assigned.push(String(url)) },
    towMessages: { showMessage: (record) => shown.push(record) },
    towChecks: {
      begin: (meta) => { checks.begun.push(meta); return `act${checks.begun.length}xyz`; },
      acknowledge: (action, ack) => checks.acknowledged.push([action, ack.job]),
      forget: (action) => checks.forgotten.push(action),
    },
  };
  vm.runInNewContext(source, context);
  const submit = async () => {
    const form = row.querySelector("form");
    const button = form.querySelector("button");
    document.dispatch(form, "submit", { submitter: button });
    await settle();
    return { form, button };
  };
  return { shown, sent, assigned, checks, submit };
};
const json = (body) => Promise.resolve({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => body });
const verdict = {};

// Accepted: followed in place.
{
  const message = { id: "check-J1", kind: "info", subject: "Show A", text: "check started" };
  const test = page(() => json({ accepted: true, action: "act1xyz", job: "J1", status: "queued", message }));
  const { button } = await test.submit();
  assert.equal(test.sent[0].fields.action_id, "act1xyz");
  assert.equal(JSON.stringify(test.checks.begun), JSON.stringify([{ subject: "Show A", topic: "a" }]));
  assert.equal(JSON.stringify(test.checks.acknowledged), JSON.stringify([["act1xyz", "J1"]]));
  assert.equal(test.assigned.length, 0); // no page opens
  assert.equal(test.shown[0], message);
  assert.equal(button.disabled, false);
  assert.ok(!button.classList.contains("spinning"));
}
verdict.acceptedInPlace = true;

// The answer was lost: it is looked up by the kept id; nothing is sent again.
{
  const test = page(() => Promise.reject(new TypeError("Failed to fetch")));
  await test.submit();
  assert.equal(test.sent.length, 1);
  assert.equal(test.checks.forgotten.length, 0);
  assert.equal(test.shown[0].id, "check-act1xyz");
  assert.equal(test.shown[0].kind, "info");
  assert.equal(test.shown[0].text, "js.checks.sent");
}
verdict.lostAnswerLookedUp = true;

// Answered with a page (a refusal, a full queue): not followed; the page opens as before.
{
  const test = page(() => json({ redirect: "/?flash=T" }));
  await test.submit();
  assert.equal(JSON.stringify(test.checks.forgotten), JSON.stringify(["act1xyz"]));
  assert.equal(test.assigned.length, 1);
}
verdict.pageAnswerForgets = true;

// The answer's headers arrived but its body broke off: TOW may have accepted it - still followed
// by its id, never forgotten, never sent again.
{
  const test = page(() => Promise.resolve({
    ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => { throw new TypeError("network error"); },
  }));
  await test.submit();
  assert.equal(test.checks.forgotten.length, 0);
  assert.equal(test.sent.length, 1);
  assert.equal(test.shown[0].id, "check-act1xyz");
}
verdict.brokenBodyStillFollowed = true;

// An error page (no words of TOW's): nobody knows whether it was accepted - still followed.
{
  const test = page(() => Promise.resolve({ ok: false, status: 502, headers: { get: () => "text/html" }, text: async () => "<html>bad gateway</html>" }));
  await test.submit();
  assert.equal(test.checks.forgotten.length, 0);
  assert.equal(test.shown[0].id, "check-act1xyz");
}
verdict.errorPageStillFollowed = true;

// TOW refused it in words (an update is running): definitely not accepted - forgotten, said why.
{
  const test = page(() => Promise.resolve({ ok: false, status: 409, headers: { get: () => "application/json" }, text: async () => JSON.stringify({ error: "An update is running" }) }));
  await test.submit();
  assert.equal(JSON.stringify(test.checks.forgotten), JSON.stringify(["act1xyz"]));
  assert.equal(test.shown[0].text, "js.submit.failed");
}
verdict.refusalForgets = true;

console.log(JSON.stringify(verdict));
