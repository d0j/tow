// The site sign-in prompt (Home, ?credential_topic=<id>) closed by Esc: the address loses the
// prompt's values, so a reload does not open it again, and the focus goes to the topic's row
// instead of staying on a field of the closed dialog (qa8). Runs the checked-in app.js with a tiny DOM.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const listeners = {};
let focused = null;
let replaced = null;
const summary = { focus() { focused = "summary"; } };
const row = { querySelector: (selector) => (selector === ":scope > details > summary" ? summary : null) };
const dialog = {
  open: true,
  close() { this.open = false; },
  showModal() { this.open = true; },
  addEventListener: (name, fn) => { listeners[name] = fn; },
};
const document = {
  hidden: false,
  body: { name: "body" },
  activeElement: { blur() { focused = "blurred"; } },
  getElementById: (id) => (id === "row-t1" ? row : null),
  querySelector: () => null,
  querySelectorAll: (selector) => (selector === "dialog.credential-prompt[open]" ? [dialog] : []),
  addEventListener() {},
  createElement: () => ({ append() {}, setAttribute() {}, classList: { add() {} } }),
};
const window = {
  setTimeout: () => 0, setInterval: () => 0, clearTimeout() {}, clearInterval() {},
  location: { assign() {}, origin: "http://127.0.0.1", href: "http://127.0.0.1/?credential_topic=t1&browser_auth_id=op1&s=name" },
  addEventListener() {},
};
Object.assign(globalThis, {
  window, document, location: window.location, history: { replaceState: (_state, _title, url) => { replaced = url; } },
  setTimeout: window.setTimeout, setInterval: window.setInterval,
  clearTimeout: window.clearTimeout, clearInterval: window.clearInterval,
  FormData: class {}, fetch: async () => ({ ok: true, json: async () => ({}) }),
});
require("../../src/tow/static/app.js");

const modal = dialog.open === true;
// The close event of the reopening (close, then showModal) arrives while the dialog is open.
listeners.close();
const reopeningIgnored = replaced === null && focused === null;
dialog.close();
listeners.close();
const address = replaced;
const rowFocused = focused === "summary";

console.log(JSON.stringify({ modal, reopeningIgnored, address, rowFocused }));
