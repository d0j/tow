// Sites: pasting a topic link fills only empty fields (or ones an earlier link filled), never
// what the owner typed. Runs the checked-in app.js with a tiny DOM; prints a JSON verdict.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const listeners = {};
const field = (id, value = "") => ({
  id,
  value,
  checked: false,
  textContent: "",
  addEventListener: (name, fn) => { (listeners[`${id}:${name}`] ||= []).push(fn); },
});
const fields = Object.fromEntries(
  ["from-url", "guess-msg", "site-name", "site-regex", "site-hosts", "site-dl", "site-login-path",
    "site-topic-path", "site-page-dl", "site-href-rx"].map((id) => [id, field(id)]),
);
let answer = {};
const document = {
  hidden: false,
  getElementById: (id) => fields[id] || null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  createElement: () => ({ append() {}, classList: { add() {} } }),
};
const window = {
  setTimeout: () => 0, setInterval: () => 0, clearTimeout() {}, clearInterval() {},
  location: { assign() {}, origin: "http://127.0.0.1", href: "http://127.0.0.1/sites" },
  addEventListener() {},
};
Object.assign(globalThis, {
  window, document, location: window.location, setTimeout: window.setTimeout, setInterval: window.setInterval,
  clearTimeout: window.clearTimeout, clearInterval: window.clearInterval,
  FormData: class { set() {} },
  fetch: async () => ({ ok: true, json: async () => answer }),
});
require("../../src/tow/static/app.js");

const paste = async (url, reply) => {
  answer = reply;
  fields["from-url"].value = url;
  for (const fn of listeners["from-url:change"] || []) await fn();
};
const guess = (n) => ({
  ok: true, name: `site${n}`, url_regex: `^https://t${n}\\.example/(\\d+)$`, fetch_hosts: `https://t${n}.example`,
  download_path: `/dl${n}/{id}`, page_download: false,
});

fields["site-name"].value = "mine";             // typed by the owner before pasting
await paste("https://t1.example/1", guess(1));
const typedKept = fields["site-name"].value === "mine";
const emptyFilled = fields["site-hosts"].value === "https://t1.example" && fields["site-dl"].value === "/dl1/{id}";
const saysKept = fields["guess-msg"].textContent.includes("js.guess.kept_typed");
await paste("https://t2.example/2", guess(2));  // a second link replaces what the first one filled
const refilled = fields["site-hosts"].value === "https://t2.example" && fields["site-name"].value === "mine";
fields["site-dl"].value = "/mine/{id}";         // edited after the fill: now the owner's
await paste("https://t3.example/3", guess(3));
const editedKept = fields["site-dl"].value === "/mine/{id}" && fields["site-hosts"].value === "https://t3.example";
console.log(JSON.stringify({ typedKept, emptyFilled, saysKept, refilled, editedKept }));
