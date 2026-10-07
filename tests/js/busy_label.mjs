// A slow action's button says "Checking…" while it runs - also a "Check" that stays enabled
// (data-keep) - and gets its own label back when the request fails. Runs the checked-in app.js
// with a tiny DOM; prints a JSON verdict.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const handlers = {};
const element = (props = {}) => ({
  attributes: {},
  dataset: {},
  children: [],
  hidden: false,
  disabled: false,
  setAttribute(name, value) { this.attributes[name] = String(value); },
  removeAttribute(name) { delete this.attributes[name]; },
  getAttribute(name) { return this.attributes[name] ?? null; },
  querySelector: () => null,
  querySelectorAll: () => [],
  closest: () => null,
  append(...nodes) { this.children.push(...nodes); },
  replaceChildren(...nodes) {
    this.children = nodes;
    this.textContent = nodes.map((node) => (typeof node === "string" ? node : node.textContent || "")).join("");
    this.firstChild = nodes[0];
  },
  ...props,
});
class HTMLFormElement {}
const makeForm = (action) => Object.assign(Object.create(HTMLFormElement.prototype), element({
  method: "post",
  action,
  attributes: { action, method: "post" },
  classList: { contains: () => false },
  matches: () => false,
}));
const document = {
  hidden: false,
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: (name, fn) => { (handlers[name] ||= []).push(fn); },
  createElement: () => element(),
};
const assigned = [];
const window = {
  setTimeout: () => 0, setInterval: () => 0, clearTimeout() {}, clearInterval() {},
  location: { assign: (url) => assigned.push(String(url)), origin: "http://127.0.0.1", href: "http://127.0.0.1/settings" },
  addEventListener() {},
  confirm: () => true,
};
let reply = null;
Object.assign(globalThis, {
  window, document, location: window.location, HTMLFormElement, history: { replaceState() {} },
  setTimeout: window.setTimeout, setInterval: window.setInterval,
  clearTimeout: window.clearTimeout, clearInterval: window.clearInterval,
  FormData: class { constructor() {} },
  fetch: () => new Promise((resolve) => { reply = resolve; }),
});
require("../../src/tow/static/app.js");

const submit = async (form, button) => {
  const event = { target: form, submitter: button, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  for (const fn of handlers.submit || []) fn(event);
  await Promise.resolve();
};
const settle = async (response) => {
  reply(response);
  for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

// "Check" of a torrent client (data-keep is "" in a browser): it only greyed out for 24 s.
const ping = makeForm("/settings/client/ping");
const check = element({ textContent: "Check", dataset: { keep: "", busyLabel: "Checking…" } });
await submit(ping, check);
const keepShowsBusy = check.textContent === "Checking…" && check.attributes["aria-busy"] === "true";
await settle({ ok: false, status: 502, text: async () => "no answer", headers: { get: () => "" } });
const failureRestores = check.textContent === "Check" && !("aria-busy" in check.attributes) && !check.disabled;

// "Restore": disabled while it runs, the next page opens when it is done.
const restore = makeForm("/settings/restore-points/p1/restore");
const button = element({ textContent: "Restore", dataset: { busyLabel: "Restoring…" } });
await submit(restore, button);
const plainShowsBusy = button.textContent === "Restoring…" && button.disabled;
await settle({ ok: true, headers: { get: () => "application/json" }, json: async () => ({ redirect: "/settings?open=transfer" }) });
const opensNext = assigned.length === 1;

// A button without a busy label keeps its text.
const save = makeForm("/settings/language");
const plain = element({ textContent: "Save", dataset: {} });
await submit(save, plain);
const quietStaysQuiet = plain.textContent === "Save";
console.log(JSON.stringify({ keepShowsBusy, failureRestores, plainShowsBusy, opensNext, quietStaysQuiet }));
