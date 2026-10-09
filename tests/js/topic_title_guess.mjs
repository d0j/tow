// Home's add form: the title TOW guesses for a pasted link fills the name field and is kept in
// the hidden "guessed_title" field, so the server tells a name the owner typed from the guess.
// Runs the checked-in app.js with a tiny DOM; prints a JSON verdict.
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const listeners = {};
const field = (id, value = "") => ({
  id,
  value,
  textContent: "",
  addEventListener: (name, fn) => { (listeners[`${id}:${name}`] ||= []).push(fn); },
  dispatchEvent() {},
});
const fields = Object.fromEntries(
  ["topic-url", "topic-title", "topic-guessed-title", "guess-msg"].map((id) => [id, field(id)]),
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
  location: { assign() {}, origin: "http://127.0.0.1", href: "http://127.0.0.1/" },
  addEventListener() {},
};
Object.assign(globalThis, {
  window, document, location: window.location, setTimeout: window.setTimeout, setInterval: window.setInterval,
  clearTimeout: window.clearTimeout, clearInterval: window.clearInterval,
  Event: class { constructor(type) { this.type = type; } },
  FormData: class { set() {} },
  fetch: async () => ({ ok: true, json: async () => answer }),
});
require("../../src/tow/static/app.js");

const paste = async (url, reply) => {
  answer = reply;
  fields["topic-url"].value = url;
  for (const fn of listeners["topic-url:change"] || []) await fn();
};

await paste("https://tracker.example/1234567", { ok: true, title: "Show A" });
const guessFilled = fields["topic-title"].value === "Show A";
const guessKept = fields["topic-guessed-title"].value === "Show A";
fields["topic-title"].value = "My show";        // the owner types over it: the guess stays recorded
await paste("https://tracker.example/7654321", { ok: true, title: "Show B" });
const typedNotReplaced = fields["topic-title"].value === "My show" && fields["topic-guessed-title"].value === "Show A";
fields["topic-title"].value = "";
fields["topic-guessed-title"].value = "";
await paste("https://tracker.example/1111111", { ok: false, title: "" });  // no guess: nothing recorded
const noGuessNothing = fields["topic-guessed-title"].value === "" && fields["topic-title"].value === "";
console.log(JSON.stringify({ guessFilled, guessKept, typedNotReplaced, noGuessNothing }));
