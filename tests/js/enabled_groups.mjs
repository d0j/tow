// Fields that count only with a box in one state (app.js, data-enabled-by="<box id>"): a site's
// user name and password while browser sign-in is not chosen, Setup's network password while
// "open from other devices" is ticked. Otherwise they are disabled - the browser neither sends nor
// validates what is hidden - and they follow the box when it changes and when the form is reset.
// Runs app.js on a small DOM; prints a JSON verdict.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { FakeElement, build, fakeDocument } from "./fake_dom.mjs";

const source = readFileSync(new URL("../../src/tow/static/app.js", import.meta.url), "utf8");
const document = fakeDocument();
const site = build(document, ["form", { id: "ed-site-rutor", class: "site-form" },
  ["input", { id: "site-browser-rutor", type: "checkbox", name: "browser_auth" }],
  ["div", { class: "field-row password-group", "data-enabled-by": "site-browser-rutor", "data-enabled-when": "unchecked" },
    ["input", { id: "site-user-rutor", name: "username" }],
    ["input", { id: "site-pass-rutor", name: "password", type: "password" }],
    ["button", { type: "button", class: "ico reveal", "data-reveal": "site-pass-rutor" }]],
  ["textarea", { id: "site-hosts-rutor", name: "fetch_hosts" }]]);
const setup = build(document, ["form", { class: "setup-form" },
  ["input", { id: "allow-lan", type: "checkbox", name: "allow_lan" }],
  ["div", { class: "form-grid lan-password", "data-enabled-by": "allow-lan" },
    ["input", { id: "lan-password", name: "lan_password", type: "password", minlength: "8" }],
    ["input", { id: "lan-hint", name: "hint" }]]]);
document.body.append(site, setup, build(document, ["script", { id: "tow-i18n" }, "{}"]));
const box = (id) => document.getElementById(id);
box("site-browser-rutor").checked = true; // browser sign-in saved for this site
box("allow-lan").checked = false;
const timers = [];
const context = {
  document, URL, URLSearchParams, JSON, Promise, Error, TypeError, Math, Number, String, Array, Object, Date, Map, Set, Boolean, RegExp,
  WeakMap, HTMLFormElement: FakeElement, HTMLDetailsElement: FakeElement, decodeURIComponent,
  location: { href: "http://127.0.0.1/sites", origin: "http://127.0.0.1", pathname: "/sites", search: "", hash: "" },
  history: { replaceState() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  localStorage: { getItem: () => null, setItem() {} },
  performance: { now: () => 0 },
  fetch: () => Promise.reject(new Error("no request expected")),
  setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  requestAnimationFrame: (fn) => { timers.push(fn); return timers.length; },
};
context.window = { ...context, addEventListener() {}, confirm: () => true, location: { ...context.location, assign() {} } };
vm.runInNewContext(source, context);
const verdict = {};
const disabled = (...ids) => ids.map((id) => box(id).disabled);

// As the page opens: what the boxes say.
assert.deepEqual(disabled("site-user-rutor", "site-pass-rutor"), [true, true]);
assert.equal(box("site-hosts-rutor").disabled, false); // only the group
assert.equal(document.querySelector('[data-reveal="site-pass-rutor"]').disabled, true);
assert.deepEqual(disabled("lan-password", "lan-hint"), [true, true]);
verdict.atStart = true;

// The boxes changed: the groups follow.
box("site-browser-rutor").checked = false;
document.dispatch(box("site-browser-rutor"), "change");
assert.deepEqual(disabled("site-user-rutor", "site-pass-rutor"), [false, false]);
box("allow-lan").checked = true;
document.dispatch(box("allow-lan"), "change");
assert.deepEqual(disabled("lan-password", "lan-hint"), [false, false]);
box("allow-lan").checked = false;
document.dispatch(box("allow-lan"), "change");
assert.deepEqual(disabled("lan-password", "lan-hint"), [true, true]);
verdict.followsTheBox = true;

// Cancel resets the card: the box goes back to what was saved, and so does the group.
document.dispatch(site, "reset");
box("site-browser-rutor").checked = true; // what the browser's reset does after the event
timers.splice(0).forEach((fn) => fn());
assert.deepEqual(disabled("site-user-rutor", "site-pass-rutor"), [true, true]);
verdict.followsAReset = true;

console.log(JSON.stringify(verdict));
