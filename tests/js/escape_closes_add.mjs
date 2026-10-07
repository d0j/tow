// Esc folds the open add form back to its "+" (QA 1.24.1: it did nothing), but not while a
// dialog is open, not for an Esc a list already took, and not when the focus is elsewhere.
// Runs the checked-in app.js with a tiny DOM; prints a JSON verdict.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const keydown = [];
let focused = null;
let dialogOpen = false;
const plus = { focus() { focused = plus; } };  // the header's "+" that opens the form
const inside = { name: "url field" };
const outside = { name: "search" };
const add = {
  open: true,
  contains: (node) => node === inside,
  querySelector: () => null,
};
const document = {
  hidden: false,
  body: { name: "body" },
  activeElement: inside,
  getElementById: (id) => (id === "new" ? add : null),
  querySelector: (selector) => (selector === "dialog[open]" ? (dialogOpen ? {} : null) : selector === 'a.plus[href="#new"]' ? plus : null),
  querySelectorAll: () => [],
  addEventListener: (name, fn) => { if (name === "keydown") keydown.push(fn); },
  createElement: () => ({ append() {}, setAttribute() {}, classList: { add() {} } }),
};
const window = {
  setTimeout: () => 0, setInterval: () => 0, clearTimeout() {}, clearInterval() {},
  location: { assign() {}, origin: "http://127.0.0.1", href: "http://127.0.0.1/" },
  addEventListener() {},
};
Object.assign(globalThis, {
  window, document, location: window.location, history: { replaceState() {} },
  setTimeout: window.setTimeout, setInterval: window.setInterval,
  clearTimeout: window.clearTimeout, clearInterval: window.clearInterval,
  FormData: class {}, fetch: async () => ({ ok: true, json: async () => ({}) }),
});
require("../../src/tow/static/app.js");

const press = (key, extra = {}) => {
  const event = { key, defaultPrevented: false, isComposing: false, preventDefault() { this.defaultPrevented = true; }, ...extra };
  for (const fn of keydown) fn(event);
  return event;
};

press("Enter");
const otherKeysIgnored = add.open === true;
dialogOpen = true;
press("Escape");
const dialogFirst = add.open === true;
dialogOpen = false;
press("Escape", { defaultPrevented: true });
const takenEscKept = add.open === true;
document.activeElement = outside;
press("Escape");
const focusElsewhereKept = add.open === true;
document.activeElement = inside;
press("Escape");
const closes = add.open === false && focused === plus;
console.log(JSON.stringify({ otherKeysIgnored, dialogFirst, takenEscKept, focusElsewhereKept, closes }));
