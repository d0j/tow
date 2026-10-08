// Esc folds an open Home/Sites row whose panel holds the focus and puts the focus back on the
// row's summary (round-5 audit, A15); not while a dialog is open, not for an Esc a list already
// took, not when the focus is outside an open row. Runs the checked-in app.js with a tiny DOM.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const keydown = [];
let focused = null;
let dialogOpen = false;
const summary = { focus() { focused = summary; } };
const row = {
  open: true,
  querySelector: (selector) => (selector === ":scope > summary" ? summary : null),
};
const field = { closest: (selector) => (selector === "details.row-edit[open]" && row.open ? row : null) };
const elsewhere = { closest: () => null };
const document = {
  hidden: false,
  body: { name: "body" },
  activeElement: field,
  getElementById: () => null,
  querySelector: (selector) => (selector === "dialog[open]" ? (dialogOpen ? {} : null) : null),
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
const otherKeysIgnored = row.open === true;
dialogOpen = true;
press("Escape");
const dialogFirst = row.open === true;
dialogOpen = false;
press("Escape", { defaultPrevented: true });
const takenEscKept = row.open === true;
document.activeElement = elsewhere;
press("Escape");
const focusElsewhereKept = row.open === true;
document.activeElement = field;
const event = press("Escape");
const closes = row.open === false && focused === summary && event.defaultPrevented;
console.log(JSON.stringify({ otherKeysIgnored, dialogFirst, takenEscKept, focusElsewhereKept, closes }));
